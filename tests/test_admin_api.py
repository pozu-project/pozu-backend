"""Tests for the persistent user store and the roles/permissions admin API."""

import http

import pytest

import pozu_flask_app

APP_SECRET = "test-app-secret-at-least-32-bytes-long"


@pytest.fixture
def client(monkeypatch, tmp_path):
    """A Flask test client with an isolated on-disk SQLite DB."""
    monkeypatch.setattr(pozu_flask_app, "APP_SECRET_KEY", APP_SECRET)
    monkeypatch.setattr(pozu_flask_app, "POZU_DB_PATH", str(tmp_path / "pozu_app.db"))
    flask_app = pozu_flask_app.create_app()
    flask_app.config.update(TESTING=True)
    return flask_app.test_client()


def _github_user(*, github_id: int, login: str) -> dict:
    return {"id": github_id, "login": login, "name": login.title(), "avatar_url": None}


def _seed_user_with_roles(*, github_id: int, login: str, role_names) -> None:
    pozu_flask_app.upsert_user(_github_user(github_id=github_id, login=login))
    with pozu_flask_app.db_connection() as connection:
        for role_name in role_names:
            role_id = connection.execute("SELECT id FROM roles WHERE name = ?", (role_name,)).fetchone()["id"]
            connection.execute(
                "INSERT OR IGNORE INTO user_roles (github_id, role_id) VALUES (?, ?)", (github_id, role_id)
            )


def _auth_headers(*, github_id: int, login: str, roles=(), permissions=()) -> dict:
    token = pozu_flask_app.mint_app_token(
        github_user=_github_user(github_id=github_id, login=login), roles=list(roles), permissions=list(permissions)
    )
    return {"Authorization": f"Bearer {token}"}


# =============================================================================
# UPSERT behavior
# =============================================================================


@pytest.mark.ai_generated
def test_upsert_user_insert_creates_single_row_with_login_count_one(client):
    pozu_flask_app.upsert_user(_github_user(github_id=1, login="octocat"))

    with pozu_flask_app.db_connection() as connection:
        rows = connection.execute("SELECT * FROM users WHERE github_id = ?", (1,)).fetchall()

    assert len(rows) == 1
    assert rows[0]["login_count"] == 1
    assert rows[0]["first_seen"] == rows[0]["last_seen"]


@pytest.mark.ai_generated
def test_upsert_user_update_increments_login_count_and_refreshes_last_seen_without_duplicating(client):
    pozu_flask_app.upsert_user(_github_user(github_id=1, login="octocat"))
    with pozu_flask_app.db_connection() as connection:
        first_seen = connection.execute("SELECT first_seen, last_seen FROM users WHERE github_id = ?", (1,)).fetchone()

    pozu_flask_app.upsert_user(_github_user(github_id=1, login="octocat-renamed"))

    with pozu_flask_app.db_connection() as connection:
        rows = connection.execute("SELECT * FROM users WHERE github_id = ?", (1,)).fetchall()

    assert len(rows) == 1
    assert rows[0]["login_count"] == 2
    assert rows[0]["login"] == "octocat-renamed"
    assert rows[0]["first_seen"] == first_seen["first_seen"]
    assert rows[0]["last_seen"] >= first_seen["last_seen"]


# =============================================================================
# Admin bootstrap
# =============================================================================


@pytest.mark.ai_generated
def test_admin_bootstrap_grants_admin_role_for_allow_listed_login(client, monkeypatch):
    monkeypatch.setattr(pozu_flask_app, "POZU_ADMIN_LOGINS", "octocat, other-admin")

    pozu_flask_app.record_login(_github_user(github_id=1, login="octocat"))

    assert pozu_flask_app.resolve_roles(1) == ["admin"]


@pytest.mark.ai_generated
def test_admin_bootstrap_does_not_grant_admin_role_for_non_allow_listed_login(client, monkeypatch):
    monkeypatch.setattr(pozu_flask_app, "POZU_ADMIN_LOGINS", "someone-else")

    pozu_flask_app.record_login(_github_user(github_id=1, login="octocat"))

    assert pozu_flask_app.resolve_roles(1) == []


# =============================================================================
# resolve_permissions
# =============================================================================


@pytest.mark.ai_generated
def test_resolve_permissions_returns_union_of_role_permissions(client):
    with pozu_flask_app.db_connection() as connection:
        connection.execute("INSERT INTO roles (name) VALUES ('reviewer')")
        role_id = connection.execute("SELECT id FROM roles WHERE name = 'reviewer'").fetchone()["id"]
        permission_id = connection.execute("SELECT id FROM permissions WHERE name = 'users:read'").fetchone()["id"]
        connection.execute(
            "INSERT INTO role_permissions (role_id, permission_id) VALUES (?, ?)", (role_id, permission_id)
        )
    _seed_user_with_roles(github_id=1, login="octocat", role_names=["reviewer", "labeler"])

    permissions = pozu_flask_app.resolve_permissions(1)

    assert permissions == {"users:read"}


# =============================================================================
# Auth/permission enforcement
# =============================================================================


ADMIN_ENDPOINTS = [
    ("GET", "/api/v1/admin/users"),
    ("GET", "/api/v1/admin/roles"),
]


@pytest.mark.ai_generated
@pytest.mark.parametrize(("method", "endpoint"), ADMIN_ENDPOINTS)
def test_admin_endpoints_reject_missing_or_invalid_token(client, method, endpoint):
    response = client.open(endpoint, method=method)

    assert response.status_code == http.HTTPStatus.UNAUTHORIZED
    assert "message" in response.get_json()


@pytest.mark.ai_generated
@pytest.mark.parametrize(
    ("method", "endpoint", "required_permission"),
    [
        ("GET", "/api/v1/admin/users", "users:read"),
        ("GET", "/api/v1/admin/roles", "roles:read"),
    ],
)
def test_admin_endpoints_reject_valid_token_lacking_permission(client, method, endpoint, required_permission):
    _seed_user_with_roles(github_id=1, login="octocat", role_names=["labeler"])
    headers = _auth_headers(github_id=1, login="octocat")

    response = client.open(endpoint, method=method, headers=headers)

    assert response.status_code == http.HTTPStatus.UNAUTHORIZED


@pytest.mark.ai_generated
@pytest.mark.parametrize(
    ("method", "endpoint", "required_permission"),
    [
        ("GET", "/api/v1/admin/users", "users:read"),
        ("GET", "/api/v1/admin/roles", "roles:read"),
    ],
)
def test_admin_endpoints_accept_valid_token_with_permission(client, method, endpoint, required_permission):
    _seed_user_with_roles(github_id=1, login="octocat", role_names=["admin"])
    headers = _auth_headers(github_id=1, login="octocat")

    response = client.open(endpoint, method=method, headers=headers)

    assert response.status_code == http.HTTPStatus.OK


@pytest.mark.ai_generated
def test_admin_endpoints_ignore_stale_jwt_permission_claim(client):
    # The caller mints a token claiming "users:read" in the JWT, but never actually
    # holds that permission in the DB. Enforcement must ignore the claim entirely.
    _seed_user_with_roles(github_id=1, login="octocat", role_names=["labeler"])
    headers = _auth_headers(github_id=1, login="octocat", roles=["admin"], permissions=["users:read"])

    response = client.get("/api/v1/admin/users", headers=headers)

    assert response.status_code == http.HTTPStatus.UNAUTHORIZED


# =============================================================================
# GET /admin/users: sorting and pagination
# =============================================================================


@pytest.fixture
def seeded_users(client):
    """Three users with distinct logins and login counts, seeded in insertion order."""
    pozu_flask_app.upsert_user(_github_user(github_id=1, login="carol"))
    pozu_flask_app.upsert_user(_github_user(github_id=2, login="alice"))
    pozu_flask_app.upsert_user(_github_user(github_id=2, login="alice"))
    pozu_flask_app.upsert_user(_github_user(github_id=2, login="alice"))
    pozu_flask_app.upsert_user(_github_user(github_id=3, login="bob"))
    pozu_flask_app.upsert_user(_github_user(github_id=3, login="bob"))
    return {"admin_headers": _auth_headers(github_id=999, login="root")}


@pytest.mark.ai_generated
@pytest.mark.parametrize("sort", ["last_seen", "first_seen", "login_count", "login"])
def test_admin_users_accepts_each_valid_sort_option(client, seeded_users, sort):
    _seed_user_with_roles(github_id=999, login="root", role_names=["admin"])

    response = client.get(f"/api/v1/admin/users?sort={sort}", headers=seeded_users["admin_headers"])

    assert response.status_code == http.HTTPStatus.OK
    assert response.get_json()["total"] == 4


@pytest.mark.ai_generated
def test_admin_users_login_sort_is_ascending_alphabetical(client, seeded_users):
    _seed_user_with_roles(github_id=999, login="root", role_names=["admin"])

    response = client.get("/api/v1/admin/users?sort=login", headers=seeded_users["admin_headers"])

    logins = [user["login"] for user in response.get_json()["users"]]
    assert logins == sorted(logins)


@pytest.mark.ai_generated
def test_admin_users_rejects_invalid_sort(client, seeded_users):
    _seed_user_with_roles(github_id=999, login="root", role_names=["admin"])

    response = client.get("/api/v1/admin/users?sort=not-a-real-column", headers=seeded_users["admin_headers"])

    assert response.status_code == http.HTTPStatus.BAD_REQUEST


@pytest.mark.ai_generated
@pytest.mark.parametrize(
    ("limit", "offset", "expected_count"),
    [
        pytest.param(1, 0, 1, id="limit-clamped-below-total"),
        pytest.param(9999, 0, 4, id="limit-clamped-to-max"),
        pytest.param(10, 5, 0, id="offset-past-total"),
        pytest.param(10, -3, 4, id="negative-offset-clamped-to-zero"),
    ],
)
def test_admin_users_pagination_clamping(client, seeded_users, limit, offset, expected_count):
    _seed_user_with_roles(github_id=999, login="root", role_names=["admin"])
    headers = seeded_users["admin_headers"]

    response = client.get(f"/api/v1/admin/users?limit={limit}&offset={offset}", headers=headers)

    assert response.status_code == http.HTTPStatus.OK
    assert len(response.get_json()["users"]) == expected_count


@pytest.mark.ai_generated
def test_admin_users_response_includes_roles(client):
    _seed_user_with_roles(github_id=1, login="octocat", role_names=["admin"])
    headers = _auth_headers(github_id=1, login="octocat")

    response = client.get("/api/v1/admin/users", headers=headers)

    users_by_login = {user["login"]: user for user in response.get_json()["users"]}
    assert users_by_login["octocat"]["roles"] == ["admin"]


# =============================================================================
# GET /admin/roles
# =============================================================================


@pytest.mark.ai_generated
def test_admin_roles_lists_seeded_roles_and_permissions(client):
    _seed_user_with_roles(github_id=1, login="octocat", role_names=["admin"])
    headers = _auth_headers(github_id=1, login="octocat")

    response = client.get("/api/v1/admin/roles", headers=headers)

    roles_by_name = {role["name"]: role["permissions"] for role in response.get_json()["roles"]}
    assert sorted(roles_by_name["admin"]) == sorted(pozu_flask_app.PERMISSIONS)
    assert roles_by_name["labeler"] == []


# =============================================================================
# PUT /admin/users/<github_id>/roles
# =============================================================================


@pytest.mark.ai_generated
def test_put_user_roles_replaces_role_set(client):
    _seed_user_with_roles(github_id=1, login="admin-user", role_names=["admin"])
    _seed_user_with_roles(github_id=2, login="target-user", role_names=[])
    headers = _auth_headers(github_id=1, login="admin-user")

    response = client.put("/api/v1/admin/users/2/roles", json={"roles": ["labeler"]}, headers=headers)

    assert response.status_code == http.HTTPStatus.OK
    assert response.get_json()["roles"] == ["labeler"]
    assert pozu_flask_app.resolve_roles(2) == ["labeler"]


@pytest.mark.ai_generated
def test_put_user_roles_rejects_unknown_role(client):
    _seed_user_with_roles(github_id=1, login="admin-user", role_names=["admin"])
    _seed_user_with_roles(github_id=2, login="target-user", role_names=[])
    headers = _auth_headers(github_id=1, login="admin-user")

    response = client.put("/api/v1/admin/users/2/roles", json={"roles": ["not-a-real-role"]}, headers=headers)

    assert response.status_code == http.HTTPStatus.BAD_REQUEST


@pytest.mark.ai_generated
def test_put_user_roles_rejects_removing_the_last_admin(client):
    _seed_user_with_roles(github_id=1, login="admin-user", role_names=["admin"])
    headers = _auth_headers(github_id=1, login="admin-user")

    response = client.put("/api/v1/admin/users/1/roles", json={"roles": ["labeler"]}, headers=headers)

    assert response.status_code == http.HTTPStatus.BAD_REQUEST
    assert "last admin" in response.get_json()["message"]
    assert pozu_flask_app.resolve_roles(1) == ["admin"]


@pytest.mark.ai_generated
def test_put_user_roles_allows_removing_admin_when_another_admin_remains(client):
    _seed_user_with_roles(github_id=1, login="admin-one", role_names=["admin"])
    _seed_user_with_roles(github_id=2, login="admin-two", role_names=["admin"])
    headers = _auth_headers(github_id=1, login="admin-one")

    response = client.put("/api/v1/admin/users/2/roles", json={"roles": ["labeler"]}, headers=headers)

    assert response.status_code == http.HTTPStatus.OK
    assert pozu_flask_app.resolve_roles(2) == ["labeler"]


@pytest.mark.ai_generated
def test_put_user_roles_requires_roles_write_permission(client):
    _seed_user_with_roles(github_id=1, login="labeler-user", role_names=["labeler"])
    _seed_user_with_roles(github_id=2, login="target-user", role_names=[])
    headers = _auth_headers(github_id=1, login="labeler-user")

    response = client.put("/api/v1/admin/users/2/roles", json={"roles": ["labeler"]}, headers=headers)

    assert response.status_code == http.HTTPStatus.UNAUTHORIZED


# =============================================================================
# GET /admin/me
# =============================================================================


@pytest.mark.ai_generated
def test_admin_me_returns_db_resolved_identity(client):
    _seed_user_with_roles(github_id=1, login="octocat", role_names=["admin"])
    headers = _auth_headers(github_id=1, login="octocat")

    response = client.get("/api/v1/admin/me", headers=headers)

    body = response.get_json()
    assert response.status_code == http.HTTPStatus.OK
    assert body["github_id"] == 1
    assert body["login"] == "octocat"
    assert body["roles"] == ["admin"]
    assert sorted(body["permissions"]) == sorted(pozu_flask_app.PERMISSIONS)


@pytest.mark.ai_generated
def test_admin_me_requires_only_authentication_not_a_specific_permission(client):
    _seed_user_with_roles(github_id=1, login="octocat", role_names=[])
    headers = _auth_headers(github_id=1, login="octocat")

    response = client.get("/api/v1/admin/me", headers=headers)

    assert response.status_code == http.HTTPStatus.OK
    assert response.get_json()["permissions"] == []
