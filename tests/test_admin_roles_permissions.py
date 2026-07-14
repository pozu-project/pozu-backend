"""Tests for the persistent user store, the roles/permissions model, and the admin API."""

import http
import unittest.mock
import urllib.parse

import jwt
import pytest

import pozu_flask_app

CLIENT_ID = "test-client-id"
CLIENT_SECRET = "test-client-secret"
APP_SECRET = "test-app-secret-at-least-32-bytes-long"

ALLOWED_ORIGIN = "https://pozu-project.github.io"

CALLER_ID = 1111
TARGET_ID = 2222

ALL_PERMISSIONS = {"users:read", "users:write", "roles:read", "roles:write"}

# (method, endpoint template) for every permission-protected admin route.
PROTECTED_ENDPOINTS = [
    pytest.param("get", "/api/v1/admin/users", id="get-users"),
    pytest.param("get", "/api/v1/admin/roles", id="get-roles"),
    pytest.param("put", f"/api/v1/admin/users/{CALLER_ID}/roles", id="put-user-roles"),
]


@pytest.fixture
def app_env(monkeypatch, tmp_path):
    """Deterministic secrets plus a temporary SQLite DB so the real DB is never touched."""
    monkeypatch.setattr(pozu_flask_app, "GITHUB_CLIENT_ID", CLIENT_ID)
    monkeypatch.setattr(pozu_flask_app, "GITHUB_CLIENT_SECRET", CLIENT_SECRET)
    monkeypatch.setattr(pozu_flask_app, "APP_SECRET_KEY", APP_SECRET)
    monkeypatch.setattr(pozu_flask_app, "POZU_DB_PATH", str(tmp_path / "pozu_test.db"))
    monkeypatch.setattr(pozu_flask_app, "POZU_ADMIN_LOGINS", "")
    return monkeypatch


@pytest.fixture
def client(app_env):
    """A Flask test client on top of the isolated app environment."""
    flask_app = pozu_flask_app.create_app()
    flask_app.config.update(TESTING=True)
    return flask_app.test_client()


def _github_user(*, github_id, login):
    """A minimal GitHub profile payload as returned by the GitHub user API."""
    return {
        "id": github_id,
        "login": login,
        "name": f"Name {login}",
        "avatar_url": f"https://avatars.example/{login}.png",
    }


def _create_user(*, github_id, login, roles=()):
    """Persist a user (as a login would) and optionally assign roles directly."""
    pozu_flask_app.upsert_user(_github_user(github_id=github_id, login=login))
    if roles:
        pozu_flask_app.replace_user_roles(github_id=github_id, role_names=list(roles))


def _auth_headers(*, github_id, login):
    """Bearer headers with a freshly minted app token for the given user."""
    token = pozu_flask_app.mint_app_token(_github_user(github_id=github_id, login=login))
    return {"Authorization": f"Bearer {token}"}


def _create_role(*, name, permissions):
    """Insert an extra (non-seeded) role with the given permission names."""
    with pozu_flask_app._open_db() as connection:
        connection.execute("INSERT OR IGNORE INTO roles (name) VALUES (?)", (name,))
        for permission in permissions:
            connection.execute("INSERT OR IGNORE INTO permissions (name) VALUES (?)", (permission,))
            connection.execute(
                """
                INSERT OR IGNORE INTO role_permissions (role_id, permission_id)
                SELECT roles.id, permissions.id FROM roles, permissions
                WHERE roles.name = ? AND permissions.name = ?
                """,
                (name, permission),
            )


def _seed_sortable_users():
    """Create three users whose expected order differs for every sort option.

    Upsert sequence (chronological): zoe once, alice once, bob three times,
    then zoe once more. Resulting login counts are bob=3, zoe=2, alice=1.
    """
    pozu_flask_app.upsert_user(_github_user(github_id=3, login="zoe"))
    pozu_flask_app.upsert_user(_github_user(github_id=2, login="alice"))
    for _ in range(3):
        pozu_flask_app.upsert_user(_github_user(github_id=1, login="bob"))
    pozu_flask_app.upsert_user(_github_user(github_id=3, login="zoe"))
    # bob doubles as the caller with read access.
    pozu_flask_app.replace_user_roles(github_id=1, role_names=["admin"])


# -----------------------------------------------------------------------------
# Authentication (missing/invalid token)
# -----------------------------------------------------------------------------


@pytest.mark.ai_generated
@pytest.mark.parametrize(
    ("method", "endpoint"),
    [*PROTECTED_ENDPOINTS, pytest.param("get", "/api/v1/admin/me", id="get-me")],
)
@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({}, id="missing-token"),
        pytest.param({"Authorization": "Bearer not-a-real-token"}, id="invalid-token"),
        pytest.param({"Authorization": "token abc"}, id="malformed-header"),
    ],
)
def test_admin_endpoints_reject_unauthenticated_requests(client, method, endpoint, headers):
    response = getattr(client, method)(endpoint, headers=headers, json={"roles": []})

    assert response.status_code == http.HTTPStatus.UNAUTHORIZED


# -----------------------------------------------------------------------------
# Permission enforcement (valid token, per-endpoint permission from the DB)
# -----------------------------------------------------------------------------


@pytest.mark.ai_generated
@pytest.mark.parametrize(("method", "endpoint"), PROTECTED_ENDPOINTS)
@pytest.mark.parametrize(
    ("roles", "expected_status"),
    [
        pytest.param(("admin",), http.HTTPStatus.OK, id="with-permission"),
        pytest.param(("labeler",), http.HTTPStatus.UNAUTHORIZED, id="role-without-permission"),
        pytest.param((), http.HTTPStatus.UNAUTHORIZED, id="no-roles"),
    ],
)
def test_permission_enforcement_per_endpoint(client, method, endpoint, roles, expected_status):
    _create_user(github_id=CALLER_ID, login="caller", roles=roles)
    headers = _auth_headers(github_id=CALLER_ID, login="caller")

    response = getattr(client, method)(endpoint, headers=headers, json={"roles": ["admin"]})

    assert response.status_code == expected_status


@pytest.mark.ai_generated
def test_permissions_are_resolved_from_db_not_jwt(client):
    # Mint the token while the caller is an admin, then strip the role. The JWT
    # still carries the stale admin snapshot, but enforcement must re-read the DB.
    _create_user(github_id=CALLER_ID, login="caller", roles=("admin",))
    _create_user(github_id=TARGET_ID, login="other-admin", roles=("admin",))
    headers = _auth_headers(github_id=CALLER_ID, login="caller")
    claims = jwt.decode(
        headers["Authorization"].removeprefix("Bearer "), APP_SECRET, algorithms=[pozu_flask_app.JWT_ALGORITHM]
    )
    assert claims["roles"] == ["admin"]

    pozu_flask_app.replace_user_roles(github_id=CALLER_ID, role_names=["labeler"])
    response = client.get("/api/v1/admin/users", headers=headers)

    assert response.status_code == http.HTTPStatus.UNAUTHORIZED


# -----------------------------------------------------------------------------
# GET /admin/users: sorting and pagination
# -----------------------------------------------------------------------------


@pytest.mark.ai_generated
@pytest.mark.parametrize(
    ("sort", "expected_logins"),
    [
        pytest.param(None, ["zoe", "bob", "alice"], id="default-last-seen"),
        pytest.param("last_seen", ["zoe", "bob", "alice"], id="last-seen"),
        pytest.param("first_seen", ["bob", "alice", "zoe"], id="first-seen"),
        pytest.param("login_count", ["bob", "zoe", "alice"], id="login-count"),
        pytest.param("login", ["alice", "bob", "zoe"], id="login"),
    ],
)
def test_users_sort_options(client, sort, expected_logins):
    _seed_sortable_users()
    headers = _auth_headers(github_id=1, login="bob")
    query = {} if sort is None else {"sort": sort}

    response = client.get("/api/v1/admin/users", headers=headers, query_string=query)

    assert response.status_code == http.HTTPStatus.OK
    body = response.get_json()
    assert [user["login"] for user in body["users"]] == expected_logins
    assert body["total"] == 3


@pytest.mark.ai_generated
def test_users_invalid_sort_rejected(client):
    _create_user(github_id=CALLER_ID, login="caller", roles=("admin",))
    headers = _auth_headers(github_id=CALLER_ID, login="caller")

    response = client.get("/api/v1/admin/users", headers=headers, query_string={"sort": "github_id"})

    assert response.status_code == http.HTTPStatus.BAD_REQUEST


@pytest.mark.ai_generated
@pytest.mark.parametrize(
    ("query", "expected_count"),
    [
        pytest.param({"limit": 500}, 3, id="limit-clamped-to-max"),
        pytest.param({"limit": 2}, 2, id="limit-respected"),
        pytest.param({"limit": 0}, 1, id="limit-clamped-up-to-one"),
        pytest.param({"limit": -5}, 1, id="negative-limit-clamped"),
        pytest.param({"offset": -10, "limit": 2}, 2, id="negative-offset-clamped-to-zero"),
        pytest.param({"offset": 2}, 1, id="offset-respected"),
        pytest.param({"offset": 10}, 0, id="offset-past-end"),
    ],
)
def test_users_pagination_clamping(client, query, expected_count):
    _seed_sortable_users()
    headers = _auth_headers(github_id=1, login="bob")

    response = client.get("/api/v1/admin/users", headers=headers, query_string=query)

    assert response.status_code == http.HTTPStatus.OK
    body = response.get_json()
    assert len(body["users"]) == expected_count
    assert body["total"] == 3


@pytest.mark.ai_generated
@pytest.mark.parametrize(
    "query",
    [pytest.param({"limit": "abc"}, id="non-integer-limit"), pytest.param({"offset": "xyz"}, id="non-integer-offset")],
)
def test_users_pagination_rejects_non_integers(client, query):
    _create_user(github_id=CALLER_ID, login="caller", roles=("admin",))
    headers = _auth_headers(github_id=CALLER_ID, login="caller")

    response = client.get("/api/v1/admin/users", headers=headers, query_string=query)

    assert response.status_code == http.HTTPStatus.BAD_REQUEST


# -----------------------------------------------------------------------------
# UPSERT behavior on login
# -----------------------------------------------------------------------------


@pytest.mark.ai_generated
@pytest.mark.parametrize("login_times", [1, 2, 5])
def test_upsert_insert_vs_update(app_env, login_times):
    for index in range(login_times):
        pozu_flask_app.upsert_user(
            {
                "id": 99,
                "login": f"octo-{index}",
                "name": f"Octo v{index}",
                "avatar_url": f"https://avatars.example/octo-{index}.png",
            }
        )

    record = pozu_flask_app.get_user(99)
    assert record["login_count"] == login_times
    # Profile fields refresh on every login; first_seen never changes afterwards.
    assert record["login"] == f"octo-{login_times - 1}"
    assert record["name"] == f"Octo v{login_times - 1}"
    if login_times == 1:
        assert record["last_seen"] == record["first_seen"]
    else:
        assert record["last_seen"] > record["first_seen"]

    listing = pozu_flask_app.list_users(limit=200, offset=0, sort="login")
    assert listing["total"] == 1


@pytest.mark.ai_generated
@pytest.mark.parametrize(
    "profile",
    [
        pytest.param({"id": 77, "login": None, "name": "Anon"}, id="login-none"),
        pytest.param({"id": 77, "name": "Anon"}, id="login-missing"),
    ],
)
def test_upsert_tolerates_missing_login(app_env, profile):
    # users.login is NOT NULL, so a profile without a login must coerce to ""
    # rather than raise an IntegrityError.
    pozu_flask_app.upsert_user(profile)

    record = pozu_flask_app.get_user(77)
    assert record["login"] == ""
    assert record["login_count"] == 1


# -----------------------------------------------------------------------------
# GET /admin/roles
# -----------------------------------------------------------------------------


@pytest.mark.ai_generated
def test_roles_listing_includes_seeded_catalog(client):
    _create_user(github_id=CALLER_ID, login="caller", roles=("admin",))
    headers = _auth_headers(github_id=CALLER_ID, login="caller")

    response = client.get("/api/v1/admin/roles", headers=headers)

    assert response.status_code == http.HTTPStatus.OK
    roles = {role["name"]: role["permissions"] for role in response.get_json()["roles"]}
    assert set(roles["admin"]) == ALL_PERMISSIONS
    assert roles["labeler"] == []


# -----------------------------------------------------------------------------
# PUT /admin/users/<github_id>/roles
# -----------------------------------------------------------------------------


@pytest.mark.ai_generated
def test_put_roles_replaces_role_set(client):
    _create_user(github_id=CALLER_ID, login="caller", roles=("admin",))
    _create_user(github_id=TARGET_ID, login="target", roles=("labeler",))
    headers = _auth_headers(github_id=CALLER_ID, login="caller")

    response = client.put(f"/api/v1/admin/users/{TARGET_ID}/roles", headers=headers, json={"roles": ["admin"]})

    assert response.status_code == http.HTTPStatus.OK
    body = response.get_json()
    assert body["github_id"] == TARGET_ID
    assert body["roles"] == ["admin"]
    assert pozu_flask_app.resolve_roles(TARGET_ID) == ["admin"]


@pytest.mark.ai_generated
@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"roles": ["superuser"]}, id="unknown-role"),
        pytest.param({"roles": "admin"}, id="roles-not-a-list"),
        pytest.param({}, id="roles-missing"),
        pytest.param({"roles": [42]}, id="roles-not-strings"),
    ],
)
def test_put_roles_rejects_invalid_bodies(client, body):
    _create_user(github_id=CALLER_ID, login="caller", roles=("admin",))
    _create_user(github_id=TARGET_ID, login="target")
    headers = _auth_headers(github_id=CALLER_ID, login="caller")

    response = client.put(f"/api/v1/admin/users/{TARGET_ID}/roles", headers=headers, json=body)

    assert response.status_code == http.HTTPStatus.BAD_REQUEST
    assert pozu_flask_app.resolve_roles(TARGET_ID) == []


@pytest.mark.ai_generated
def test_put_roles_unknown_user_rejected(client):
    _create_user(github_id=CALLER_ID, login="caller", roles=("admin",))
    headers = _auth_headers(github_id=CALLER_ID, login="caller")

    response = client.put("/api/v1/admin/users/424242/roles", headers=headers, json={"roles": ["labeler"]})

    assert response.status_code == http.HTTPStatus.BAD_REQUEST


@pytest.mark.ai_generated
@pytest.mark.parametrize(
    ("other_user_roles", "expected_status"),
    [
        pytest.param(("labeler",), http.HTTPStatus.BAD_REQUEST, id="last-admin-protected"),
        pytest.param(("admin",), http.HTTPStatus.OK, id="another-admin-remains"),
    ],
)
def test_put_roles_last_admin_protection(client, other_user_roles, expected_status):
    _create_user(github_id=CALLER_ID, login="caller", roles=("admin",))
    _create_user(github_id=TARGET_ID, login="other", roles=other_user_roles)
    headers = _auth_headers(github_id=CALLER_ID, login="caller")

    response = client.put(f"/api/v1/admin/users/{CALLER_ID}/roles", headers=headers, json={"roles": ["labeler"]})

    assert response.status_code == expected_status
    if expected_status == http.HTTPStatus.BAD_REQUEST:
        assert response.get_json()["message"] == "cannot remove the last admin"
        assert pozu_flask_app.resolve_roles(CALLER_ID) == ["admin"]
    else:
        assert pozu_flask_app.resolve_roles(CALLER_ID) == ["labeler"]


# -----------------------------------------------------------------------------
# GET /admin/me
# -----------------------------------------------------------------------------


@pytest.mark.ai_generated
def test_me_returns_db_resolved_roles_and_permissions(client):
    _create_user(github_id=CALLER_ID, login="caller", roles=("admin",))
    headers = _auth_headers(github_id=CALLER_ID, login="caller")

    response = client.get("/api/v1/admin/me", headers=headers)

    assert response.status_code == http.HTTPStatus.OK
    body = response.get_json()
    assert body["github_id"] == CALLER_ID
    assert body["login"] == "caller"
    assert body["roles"] == ["admin"]
    assert set(body["permissions"]) == ALL_PERMISSIONS


# -----------------------------------------------------------------------------
# resolve_permissions and JWT snapshot
# -----------------------------------------------------------------------------


@pytest.mark.ai_generated
def test_resolve_permissions_returns_union_of_role_permissions(app_env):
    _create_role(name="reader", permissions=("users:read", "roles:read"))
    _create_role(name="writer", permissions=("users:write", "roles:read"))
    _create_user(github_id=CALLER_ID, login="caller", roles=("reader", "writer"))

    permissions = pozu_flask_app.resolve_permissions(CALLER_ID)

    assert permissions == {"users:read", "roles:read", "users:write"}


@pytest.mark.ai_generated
def test_jwt_carries_roles_and_permissions_snapshot(app_env):
    _create_user(github_id=CALLER_ID, login="caller", roles=("admin",))

    token = pozu_flask_app.mint_app_token(_github_user(github_id=CALLER_ID, login="caller"))

    claims = jwt.decode(token, APP_SECRET, algorithms=[pozu_flask_app.JWT_ALGORITHM])
    assert claims["roles"] == ["admin"]
    assert set(claims["permissions"]) == ALL_PERMISSIONS


# -----------------------------------------------------------------------------
# Admin bootstrap on login
# -----------------------------------------------------------------------------


@pytest.mark.ai_generated
@pytest.mark.parametrize(
    ("login", "expected_roles"),
    [
        pytest.param("octocat", ["admin"], id="allow-listed"),
        pytest.param("second-admin", ["admin"], id="allow-listed-with-whitespace"),
        pytest.param("random-user", [], id="not-allow-listed"),
    ],
)
def test_admin_bootstrap_on_login(app_env, login, expected_roles):
    app_env.setattr(pozu_flask_app, "POZU_ADMIN_LOGINS", "octocat, second-admin")

    pozu_flask_app.record_user_login(_github_user(github_id=CALLER_ID, login=login))

    assert pozu_flask_app.resolve_roles(CALLER_ID) == expected_roles


@pytest.mark.ai_generated
def test_callback_persists_user_and_snapshots_roles_into_jwt(client, app_env):
    app_env.setattr(pozu_flask_app, "POZU_ADMIN_LOGINS", "octocat")
    with client.session_transaction() as session:
        session["oauth_state"] = "good-state"

    github_user = _github_user(github_id=4242, login="octocat")
    token_response = unittest.mock.Mock()
    token_response.json.return_value = {"access_token": "gho_test_token"}
    token_response.raise_for_status.return_value = None
    user_response = unittest.mock.Mock()
    user_response.json.return_value = github_user
    user_response.raise_for_status.return_value = None

    with (
        unittest.mock.patch.object(pozu_flask_app.requests, "post", return_value=token_response),
        unittest.mock.patch.object(pozu_flask_app.requests, "get", return_value=user_response),
    ):
        response = client.get("/auth/github/callback?code=abc123&state=good-state")

    assert response.status_code == http.HTTPStatus.FOUND
    fragment = urllib.parse.urlparse(response.headers["Location"]).fragment
    token = urllib.parse.parse_qs(fragment)["token"][0]
    claims = jwt.decode(token, APP_SECRET, algorithms=[pozu_flask_app.JWT_ALGORITHM])
    assert claims["roles"] == ["admin"]
    assert set(claims["permissions"]) == ALL_PERMISSIONS

    record = pozu_flask_app.get_user(4242)
    assert record["login"] == "octocat"
    assert record["login_count"] == 1
    assert record["roles"] == ["admin"]


# -----------------------------------------------------------------------------
# CORS
# -----------------------------------------------------------------------------


@pytest.mark.ai_generated
def test_cors_preflight_allows_put_on_admin_routes(client):
    response = client.options(
        f"/api/v1/admin/users/{TARGET_ID}/roles",
        headers={"Origin": ALLOWED_ORIGIN, "Access-Control-Request-Method": "PUT"},
    )

    assert response.headers.get("Access-Control-Allow-Origin") == ALLOWED_ORIGIN
    assert "PUT" in response.headers.get("Access-Control-Allow-Methods", "")
