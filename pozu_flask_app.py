"""
DANDI annotation ingest service - single-file Flask + Flask-RESTX app.

Designed to drop into PythonAnywhere as the project's flask app. PA's WSGI
file should import the module-level `app`:

Check `/api/v1/docs` for API reference.
"""

from __future__ import annotations

import contextlib
import datetime
import functools
import json
import logging
import os
import pathlib
import secrets
import sqlite3
import sys
import urllib.parse
import uuid
import http

import filelock
import flask
import flask_cors
import flask_restx
import jwt
import requests

# =============================================================================
# Config
# =============================================================================

VENV_BIN = "/home/CodyCBakerPhD/.virtualenvs/pozu/bin"
DANDI_BIN = f"{VENV_BIN}/dandi"


def load_secret(*, env_var: str, file_path: str) -> str:
    """Load a secret from an environment variable, falling back to a chmod-600 file.

    Returns an empty string when neither source is present. This keeps the module
    importable in development and CI, where the deployment secret files do not exist.
    Liveness of each secret is surfaced through the ``/api/v1/health`` endpoint.
    """
    value = os.environ.get(env_var)
    if value:
        return value.strip()
    path = pathlib.Path(file_path)
    if path.exists():
        return path.read_text().strip()
    return ""


EMBER_DANDI_API_KEY = load_secret(env_var="EMBER_DANDI_API_KEY", file_path="/home/CodyCBakerPhD/dandi_token")

# -- GitHub OAuth (web application / authorization-code flow) -----------------
GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"  # noqa: S105
GITHUB_USER_URL = "https://api.github.com/user"
GITHUB_OAUTH_SCOPES = "read:user"
GITHUB_OAUTH_CALLBACK_URL = os.environ.get(
    "GITHUB_OAUTH_CALLBACK_URL",
    "https://pozu-codycbakerphd.pythonanywhere.com/auth/github/callback",
)

# A historical deployment hard-coded these literal strings as the credential
# defaults. Treat them as "unconfigured" so a stale value cannot leak into the
# GitHub handshake (the client id into the redirect, the secret into the token
# exchange); the startup check below also warns loudly if either is ever seen.
PLACEHOLDER_CLIENT_ID = "<client id>"
PLACEHOLDER_CLIENT_SECRET = "<client secret>"  # noqa: S105

GITHUB_CLIENT_ID = load_secret(env_var="GITHUB_CLIENT_ID", file_path="/home/CodyCBakerPhD/github_oauth_client_id")
if GITHUB_CLIENT_ID == PLACEHOLDER_CLIENT_ID:
    GITHUB_CLIENT_ID = ""
GITHUB_CLIENT_SECRET = load_secret(
    env_var="GITHUB_CLIENT_SECRET", file_path="/home/CodyCBakerPhD/github_oauth_client_secret"
)
if GITHUB_CLIENT_SECRET == PLACEHOLDER_CLIENT_SECRET:
    GITHUB_CLIENT_SECRET = ""

# Signs both the short-lived OAuth `state` (Flask session cookie) and the app JWT.
APP_SECRET_KEY = load_secret(env_var="APP_SECRET_KEY", file_path="/home/CodyCBakerPhD/app_secret_key")

# Where the SPA lives. The callback redirects here with the freshly minted JWT.
FRONTEND_URL = os.environ.get("FRONTEND_URL", "https://pozu-project.github.io/pozu/")

# -- Persistent user store (SQLite) -------------------------------------------
# The DB path is configuration rather than a credential, but it reuses the same
# env-var-then-file loading pattern as the secrets above for consistency.
POZU_DB_PATH = (
    load_secret(env_var="POZU_DB_PATH", file_path="/home/CodyCBakerPhD/pozu_db_path")
    or "/home/CodyCBakerPhD/mysite/pozu_app.db"
)

# Comma-separated allow-list of GitHub logins granted the "admin" role on every
# login. This bootstraps the first admin without any DB surgery and avoids the
# chicken-and-egg lockout where nobody holds the permission needed to grant it.
POZU_ADMIN_LOGINS = load_secret(env_var="POZU_ADMIN_LOGINS", file_path="/home/CodyCBakerPhD/pozu_admin_logins")

# Canonical permission and role catalogs, seeded idempotently into the DB on
# every connection. Add future permissions and roles here in one place.
PERMISSIONS = (
    "users:read",
    "users:write",
    "roles:read",
    "roles:write",
)
ADMIN_ROLE_NAME = "admin"
ROLE_DEFINITIONS = {
    ADMIN_ROLE_NAME: PERMISSIONS,
    "labeler": (),
}

JWT_ALGORITHM = "HS256"
JWT_ISSUER = "pozu-backend"
JWT_TTL_SECONDS = 3600

BBOX_DANDISET_ROOT = pathlib.Path("/home/CodyCBakerPhD/mysite/000469")
LABELS_DANDISET_ROOT = pathlib.Path("/home/CodyCBakerPhD/mysite/000470")
# "No subject present" is a legitimate annotation outcome (the frame genuinely
# contains no subject to box/label), so it is real data that uploads to DANDI like
# the other annotation routes. Dandiset 000472 is reserved for it; provision it on
# the deployment (a `dandiset.yaml` plus a `derivatives/` tree) the same way as the
# bbox/labels dandisets.
NO_SUBJECT_DANDISET_ROOT = pathlib.Path("/home/CodyCBakerPhD/mysite/000472")
# Frame reports flag content as inappropriate/problematic. Dandiset 000473 is
# reserved for them, so they buffer inside that dandiset and are swept into the
# hourly DANDI upload by cron_snapshot.py like the other annotation routes.
# Provision 000473 on the deployment (a `dandiset.yaml` plus a `derivatives/`
# tree) the same way as the bbox/labels dandisets.
REPORTS_DANDISET_ROOT = pathlib.Path("/home/CodyCBakerPhD/mysite/000473")
DANDI_INSTANCE = "https://api-dandi.emberarchive.org/api"
LOG_LEVEL = "INFO"

# TODO: replace with HTTP call to ember-cache once that URL exists.
CONTENT_ID_TO_DANDI_PATH = {
    "59e7d85b-6827-4e62-977a-bab97c54df82": "emberset-test0/sub-test1/sub-test1_ses-test2.nwb",
    "b2871cfe-b785-41cf-9a72-4a94a625fd26": "emberset-test0/sub-test1/sub-test1_ses-test2.nwb",
}


# =============================================================================
# Logging
# =============================================================================

root_logger = logging.getLogger()
root_logger.setLevel(LOG_LEVEL)
for h in list(root_logger.handlers):
    root_logger.removeHandler(h)
_handler = logging.StreamHandler(sys.stderr)
_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s :: %(message)s"))
root_logger.addHandler(_handler)

logger = logging.getLogger(__name__)


class BadRequest(Exception):
    """Raised to return a 400 with a clean JSON body."""


class Unauthorized(Exception):
    """Raised to return a 401 with a clean JSON body."""


class RedactFilter(logging.Filter):
    """Redact all secrets from server logs."""

    def __init__(self, secrets):
        super().__init__()
        self._secrets = sorted([s for s in secrets if s], key=len, reverse=True)

    def filter(self, record):
        msg = record.getMessage()
        for s in self._secrets:
            if s in msg:
                msg = msg.replace(s, "***REDACTED***")
                record.msg = msg
                record.args = ()
        return True


_handler.addFilter(RedactFilter([EMBER_DANDI_API_KEY, GITHUB_CLIENT_SECRET, APP_SECRET_KEY]))


def _validate_oauth_config() -> None:
    """Warn loudly at startup when the GitHub OAuth credentials are missing.

    On PythonAnywhere, env vars set in a Bash console or the Web tab are not
    visible to the web worker unless the WSGI file loads them; the worker then
    silently falls back to an empty client id and 404s at GitHub. Surface that
    here instead of failing only at request time. ``PLACEHOLDER_CLIENT_ID`` is
    normalised to an empty string above, so an empty value covers both cases.

    Only the *presence* of each secret is logged, never its value.
    """
    if not GITHUB_CLIENT_ID:
        logger.warning(
            "GitHub OAuth is NOT configured: client id is empty (or the placeholder %r). "
            "The /auth/github/login route will reject requests with a 400 until a real "
            "client id is supplied via the GITHUB_CLIENT_ID env var (loaded by the WSGI "
            "file) or the /home/CodyCBakerPhD/github_oauth_client_id file, after which the "
            "web app must be reloaded from the PythonAnywhere Web tab.",
            PLACEHOLDER_CLIENT_ID,
        )
        return
    missing = [
        name
        for name, value in (("GITHUB_CLIENT_SECRET", GITHUB_CLIENT_SECRET), ("APP_SECRET_KEY", APP_SECRET_KEY))
        if not value
    ]
    if missing:
        logger.warning("GitHub OAuth client id present, but these secrets are missing: %s", ", ".join(missing))
    else:
        logger.info("GitHub OAuth configured: client id and all signing secrets are present.")


_validate_oauth_config()


# =============================================================================
# Helper: append a record to the current hour's JSONL buffer file
# =============================================================================


def append_to_hourly_jsonl(record: dict, buffer_dir: pathlib.Path) -> pathlib.Path:
    """Append *record* as a JSON line to the current-hour JSONL buffer.

    Uses a per-file lock so concurrent WSGI workers don't interleave writes.
    Returns the path of the JSONL file written to.
    """
    hour_tag = datetime.datetime.utcnow().strftime("%Y-%m-%d-%H")
    buffer_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = buffer_dir / f"{hour_tag}.jsonl"
    lock_path = buffer_dir / f"{hour_tag}.jsonl.lock"

    with filelock.FileLock(lock_path):
        with jsonl_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")

    return jsonl_path


# =============================================================================
# User store (SQLite): users, roles, permissions
# =============================================================================


_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS users (
        github_id   INTEGER PRIMARY KEY,
        login       TEXT NOT NULL,
        name        TEXT,
        avatar_url  TEXT,
        first_seen  TEXT NOT NULL,
        last_seen   TEXT NOT NULL,
        login_count INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS roles (
        id   INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS permissions (
        id   INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS role_permissions (
        role_id       INTEGER NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
        permission_id INTEGER NOT NULL REFERENCES permissions(id) ON DELETE CASCADE,
        PRIMARY KEY (role_id, permission_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS user_roles (
        github_id INTEGER NOT NULL REFERENCES users(github_id) ON DELETE CASCADE,
        role_id   INTEGER NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
        PRIMARY KEY (github_id, role_id)
    )
    """,
)


def _utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string, the format stored in the DB."""
    return datetime.datetime.now(tz=datetime.timezone.utc).isoformat()


def _seed_roles_and_permissions(connection, /) -> None:
    """Idempotently seed the canonical roles and permissions from the module-top catalogs."""
    connection.executemany("INSERT OR IGNORE INTO permissions (name) VALUES (?)", [(name,) for name in PERMISSIONS])
    connection.executemany("INSERT OR IGNORE INTO roles (name) VALUES (?)", [(name,) for name in ROLE_DEFINITIONS])
    for role_name, permission_names in ROLE_DEFINITIONS.items():
        for permission_name in permission_names:
            connection.execute(
                """
                INSERT OR IGNORE INTO role_permissions (role_id, permission_id)
                SELECT roles.id, permissions.id FROM roles, permissions
                WHERE roles.name = ? AND permissions.name = ?
                """,
                (role_name, permission_name),
            )


@contextlib.contextmanager
def _open_db():
    """Yield a short-lived SQLite connection with the schema ensured and seeded.

    A fresh connection per read/write avoids sharing connections across WSGI
    threads/workers. SQLite's own file locking arbitrates concurrent access.
    The schema is created lazily and the seed is idempotent, so first use on a
    new deployment (or a new POZU_DB_PATH) bootstraps everything. Foreign-key
    enforcement is per-connection in SQLite, so it is enabled here every time.
    """
    connection = sqlite3.connect(POZU_DB_PATH)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        for statement in _SCHEMA_STATEMENTS:
            connection.execute(statement)
        _seed_roles_and_permissions(connection)
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def upsert_user(github_user, /) -> None:
    """Insert or refresh the persistent user row for a GitHub profile at login time."""
    now = _utc_now_iso()
    with _open_db() as connection:
        connection.execute(
            """
            INSERT INTO users (github_id, login, name, avatar_url, first_seen, last_seen, login_count)
            VALUES (?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(github_id) DO UPDATE SET
                login = excluded.login,
                name = excluded.name,
                avatar_url = excluded.avatar_url,
                last_seen = excluded.last_seen,
                login_count = users.login_count + 1
            """,
            (
                int(github_user["id"]),
                github_user.get("login"),
                github_user.get("name"),
                github_user.get("avatar_url"),
                now,
                now,
            ),
        )


def _admin_bootstrap_logins() -> frozenset[str]:
    """Parse the comma-separated POZU_ADMIN_LOGINS allow-list into a set of logins."""
    return frozenset(login.strip() for login in POZU_ADMIN_LOGINS.split(",") if login.strip())


def apply_admin_bootstrap(*, github_id: int, login: str | None) -> None:
    """Ensure the user holds the "admin" role when *login* is on the ops allow-list."""
    if login not in _admin_bootstrap_logins():
        return
    with _open_db() as connection:
        connection.execute(
            """
            INSERT OR IGNORE INTO user_roles (github_id, role_id)
            SELECT ?, roles.id FROM roles WHERE roles.name = ?
            """,
            (github_id, ADMIN_ROLE_NAME),
        )


def record_user_login(github_user, /) -> None:
    """Persist a successful GitHub login: UPSERT the user row, then apply the admin bootstrap."""
    upsert_user(github_user)
    apply_admin_bootstrap(github_id=int(github_user["id"]), login=github_user.get("login"))


def resolve_roles(github_id, /) -> list[str]:
    """Return the sorted role names held by the user, read from the DB."""
    with _open_db() as connection:
        rows = connection.execute(
            """
            SELECT roles.name FROM roles
            JOIN user_roles ON user_roles.role_id = roles.id
            WHERE user_roles.github_id = ?
            ORDER BY roles.name
            """,
            (int(github_id),),
        ).fetchall()
    return [row["name"] for row in rows]


def resolve_permissions(github_id, /) -> set[str]:
    """Return the union of permissions granted by all of the user's roles.

    This is the single authoritative permission check. It always reads the DB,
    never the JWT, so role changes take effect on the very next request.
    """
    with _open_db() as connection:
        rows = connection.execute(
            """
            SELECT DISTINCT permissions.name FROM permissions
            JOIN role_permissions ON role_permissions.permission_id = permissions.id
            JOIN user_roles ON user_roles.role_id = role_permissions.role_id
            WHERE user_roles.github_id = ?
            """,
            (int(github_id),),
        ).fetchall()
    return {row["name"] for row in rows}


def _user_record(*, connection, row) -> dict:
    """Build the API-facing user record (including role names) from a users table row."""
    role_rows = connection.execute(
        """
        SELECT roles.name FROM roles
        JOIN user_roles ON user_roles.role_id = roles.id
        WHERE user_roles.github_id = ?
        ORDER BY roles.name
        """,
        (row["github_id"],),
    ).fetchall()
    return {
        "github_id": row["github_id"],
        "login": row["login"],
        "name": row["name"],
        "avatar_url": row["avatar_url"],
        "first_seen": row["first_seen"],
        "last_seen": row["last_seen"],
        "login_count": row["login_count"],
        "roles": [role_row["name"] for role_row in role_rows],
    }


def get_user(github_id, /) -> dict | None:
    """Return the API-facing record for one user, or None when the user is unknown."""
    with _open_db() as connection:
        row = connection.execute("SELECT * FROM users WHERE github_id = ?", (int(github_id),)).fetchone()
        if row is None:
            return None
        return _user_record(connection=connection, row=row)


# Whitelisted ORDER BY clauses per public sort key. Interpolating from this
# fixed mapping (never from raw user input) keeps the query injection-safe.
USER_SORT_ORDERS = {
    "last_seen": "last_seen DESC",
    "first_seen": "first_seen DESC",
    "login_count": "login_count DESC",
    "login": "login COLLATE NOCASE ASC",
}


def list_users(*, limit: int, offset: int, sort: str) -> dict:
    """Return one page of users plus the total user count. *sort* must be a USER_SORT_ORDERS key."""
    order_by = USER_SORT_ORDERS[sort]
    with _open_db() as connection:
        total = connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        user_rows = connection.execute(
            f"SELECT * FROM users ORDER BY {order_by} LIMIT ? OFFSET ?",  # noqa: S608
            (limit, offset),
        ).fetchall()
        users = [_user_record(connection=connection, row=row) for row in user_rows]
    return {"total": total, "users": users}


def list_roles() -> list[dict]:
    """Return every role with its granted permission names."""
    with _open_db() as connection:
        role_rows = connection.execute("SELECT id, name FROM roles ORDER BY name").fetchall()
        roles = []
        for role_row in role_rows:
            permission_rows = connection.execute(
                """
                SELECT permissions.name FROM permissions
                JOIN role_permissions ON role_permissions.permission_id = permissions.id
                WHERE role_permissions.role_id = ?
                ORDER BY permissions.name
                """,
                (role_row["id"],),
            ).fetchall()
            roles.append({"name": role_row["name"], "permissions": [row["name"] for row in permission_rows]})
    return roles


def replace_user_roles(*, github_id: int, role_names: list[str]) -> dict:
    """Replace the user's role set with *role_names* and return the updated user record.

    Every requested role name must exist. Refuses to strip the "admin" role
    from the only remaining admin in the system, so an admin cannot lock
    everyone (including themselves) out of role management.
    """
    with _open_db() as connection:
        user_row = connection.execute("SELECT * FROM users WHERE github_id = ?", (int(github_id),)).fetchone()
        if user_row is None:
            raise BadRequest(f"Unknown user: {github_id}")

        role_ids = {}
        for role_name in role_names:
            row = connection.execute("SELECT id FROM roles WHERE name = ?", (role_name,)).fetchone()
            if row is None:
                raise BadRequest(f"Unknown role: {role_name}")
            role_ids[role_name] = row["id"]

        if ADMIN_ROLE_NAME not in role_ids:
            admin_holders = connection.execute(
                """
                SELECT user_roles.github_id FROM user_roles
                JOIN roles ON roles.id = user_roles.role_id
                WHERE roles.name = ?
                """,
                (ADMIN_ROLE_NAME,),
            ).fetchall()
            admin_ids = {row["github_id"] for row in admin_holders}
            if admin_ids == {int(github_id)}:
                raise BadRequest("cannot remove the last admin")

        connection.execute("DELETE FROM user_roles WHERE github_id = ?", (int(github_id),))
        connection.executemany(
            "INSERT INTO user_roles (github_id, role_id) VALUES (?, ?)",
            [(int(github_id), role_id) for role_id in role_ids.values()],
        )
        return _user_record(connection=connection, row=user_row)


# =============================================================================
# Auth helpers (JWT enforcement)
# =============================================================================


def decode_app_token(token, /) -> dict:
    """Verify and decode an app JWT, returning its claims.

    Enforces the signing algorithm, the expected issuer, and the presence of the
    ``exp``, ``iss``, and ``sub`` claims. Raises ``jwt.PyJWTError`` on any failure.
    """
    return jwt.decode(
        token,
        APP_SECRET_KEY,
        algorithms=[JWT_ALGORITHM],
        issuer=JWT_ISSUER,
        options={"require": ["exp", "iss", "sub"]},
    )


def require_auth(handler, /):
    """Decorate a resource handler to require a valid app JWT.

    Reads the ``Authorization: Bearer <jwt>`` header, rejecting a missing or
    malformed header and any invalid or expired token with ``Unauthorized``. On
    success the decoded claims are stashed on ``flask.g.user`` for the handler.
    """

    @functools.wraps(handler)
    def wrapper(*args, **kwargs):
        header = flask.request.headers.get("Authorization", "")
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise Unauthorized("Missing or malformed Authorization header")
        try:
            claims = decode_app_token(token)
        except jwt.PyJWTError:
            raise Unauthorized("Invalid or expired token")
        flask.g.user = claims
        return handler(*args, **kwargs)

    return wrapper


def require_permission(permission_name, /):
    """Build a decorator enforcing a valid app JWT plus one DB-resolved permission.

    Authentication reuses ``require_auth``. Authorization is then resolved from
    the database on EVERY request via ``resolve_permissions``. The ``roles`` and
    ``permissions`` claims inside the JWT are UI hints only and are deliberately
    never consulted here, since they can be up to JWT_TTL_SECONDS stale. A user
    store failure fails closed (the request is denied, never allowed through).
    """

    def decorator(handler):
        @require_auth
        @functools.wraps(handler)
        def wrapper(*args, **kwargs):
            try:
                granted = resolve_permissions(flask.g.user["sub"])
            except sqlite3.Error:
                logger.exception("Permission resolution failed; denying the request")
                raise Unauthorized("Permission check failed") from None
            if permission_name not in granted:
                raise Unauthorized(f"Missing required permission: {permission_name}")
            return handler(*args, **kwargs)

        return wrapper

    return decorator


# =============================================================================
# BBox namespace
# =============================================================================


bbox_ns = flask_restx.Namespace(
    "annotations-bbox",
    description="Frame-level bounding-box annotations against a source video",
)

box_model = bbox_ns.model(
    "Box",
    {
        "x": flask_restx.fields.Float(required=True),
        "y": flask_restx.fields.Float(required=True),
        "width": flask_restx.fields.Float(required=True, min=0),
        "height": flask_restx.fields.Float(required=True, min=0),
    },
)

bbox_request = bbox_ns.model(
    "BBoxAnnotation",
    {
        "video_url": flask_restx.fields.String(required=True),
        "frame_index": flask_restx.fields.Integer(required=True, min=0),
        "total_frames": flask_restx.fields.Integer(required=True, min=1),
        "fps": flask_restx.fields.Float(required=True, min=0),
        "frame_width": flask_restx.fields.Integer(required=True, min=1),
        "frame_height": flask_restx.fields.Integer(required=True, min=1),
        "timestamp": flask_restx.fields.String(required=True),
        "box": flask_restx.fields.Nested(box_model, required=True),
    },
)

bbox_response = bbox_ns.model(
    "BBoxAnnotationResponse",
    {
        "content_id": flask_restx.fields.String,
        "submission_id": flask_restx.fields.String,
        "push_status": flask_restx.fields.String,
    },
)


@bbox_ns.route("")
class BBoxAnnotation(flask_restx.Resource):
    @require_auth
    @bbox_ns.expect(bbox_request, validate=False)
    @bbox_ns.marshal_with(bbox_response, code=http.HTTPStatus.ACCEPTED)
    def post(self):
        """Queue one bounding-box annotation for the next hourly DANDI upload."""
        body = flask.request.get_json(silent=True)
        if not isinstance(body, dict):
            raise BadRequest("Request body must be a JSON object")

        content_id = body["video_url"].split("/")[-1]
        dandi_path = CONTENT_ID_TO_DANDI_PATH.get(content_id)
        if dandi_path is None:
            raise BadRequest(f"Unknown content_id: {content_id}")

        submission_id = uuid.uuid4().hex
        body["submission_id"] = submission_id
        body["submitted_by"] = flask.g.user.get("login") or flask.g.user["sub"]

        buffer_dir = BBOX_DANDISET_ROOT / "derivatives" / "buffer"
        append_to_hourly_jsonl(body, buffer_dir)
        logger.info("Queued bbox annotation submission_id=%s content_id=%s", submission_id, content_id)

        return {
            "content_id": content_id,
            "submission_id": submission_id,
            "push_status": "queued",
        }, http.HTTPStatus.ACCEPTED


# =============================================================================
# Labels (.slp) namespace
# =============================================================================


labels_ns = flask_restx.Namespace("annotations-labels", description="Keypoint label annotations")

keypoint_model = labels_ns.model(
    "Keypoint",
    {
        "id": flask_restx.fields.String(required=True, description="Machine identifier, e.g. 'left_front_paw'"),
        "name": flask_restx.fields.String(required=True, description="Human-readable label name"),
        "placed": flask_restx.fields.Boolean(required=True, description="Whether the keypoint was placed by the user"),
        "pixel_x": flask_restx.fields.Float(required=True, description="X coordinate in pixels"),
        "pixel_y": flask_restx.fields.Float(required=True, description="Y coordinate in pixels"),
    },
)

labels_request = labels_ns.model(
    "LabelsAnnotation",
    {
        "video_url": flask_restx.fields.String(required=True),
        "frame_index": flask_restx.fields.Integer(required=True, min=0),
        "total_frames": flask_restx.fields.Integer(required=True, min=1),
        "fps": flask_restx.fields.Float(required=True, min=0),
        "frame_width": flask_restx.fields.Integer(required=True, min=1),
        "frame_height": flask_restx.fields.Integer(required=True, min=1),
        "timestamp": flask_restx.fields.String(required=True),
        "labels": flask_restx.fields.List(flask_restx.fields.Nested(keypoint_model), required=True),
    },
)

labels_record = labels_ns.model(
    "LabelsRecord",
    {
        "submission_id": flask_restx.fields.String(description="UUID hex identifying this submission"),
        "content_id": flask_restx.fields.String(description="Asset identifier extracted from video_url"),
        "video_url": flask_restx.fields.String,
        "frame_index": flask_restx.fields.Integer,
        "total_frames": flask_restx.fields.Integer,
        "fps": flask_restx.fields.Float,
        "frame_width": flask_restx.fields.Integer,
        "frame_height": flask_restx.fields.Integer,
        "timestamp": flask_restx.fields.String,
        "labels": flask_restx.fields.List(flask_restx.fields.Nested(keypoint_model)),
    },
)

labels_response = labels_ns.model(
    "LabelsAnnotationResponse",
    {
        "content_id": flask_restx.fields.String,
        "submission_id": flask_restx.fields.String,
        "push_status": flask_restx.fields.String,
    },
)


@labels_ns.route("")
class LabelsAnnotation(flask_restx.Resource):
    @require_auth
    @labels_ns.expect(labels_request, validate=False)
    @labels_ns.marshal_with(labels_response, code=http.HTTPStatus.ACCEPTED)
    def post(self):
        """Queue a keypoint label annotation for the next hourly DANDI upload."""
        body = flask.request.get_json(silent=True)
        if not isinstance(body, dict):
            raise BadRequest("Request body must be a JSON object")

        video_url = body["video_url"]
        content_id = video_url.rsplit("/", maxsplit=1)[-1]
        if content_id not in CONTENT_ID_TO_DANDI_PATH:
            raise BadRequest(f"Unknown content_id: {content_id}")

        if not isinstance(body.get("labels"), list):
            raise BadRequest("'labels' must be a list of keypoint objects")

        submission_id = uuid.uuid4().hex
        submitted_by = flask.g.user.get("login") or flask.g.user["sub"]
        record: dict = {"submission_id": submission_id, "content_id": content_id, "submitted_by": submitted_by, **body}

        buffer_dir = LABELS_DANDISET_ROOT / "derivatives" / "buffer"
        append_to_hourly_jsonl(record, buffer_dir)
        logger.info("Queued labels submission_id=%s content_id=%s", submission_id, content_id)

        return {
            "content_id": content_id,
            "submission_id": submission_id,
            "push_status": "queued",
        }, http.HTTPStatus.ACCEPTED


# =============================================================================
# No-subject namespace
# =============================================================================


no_subject_ns = flask_restx.Namespace(
    "annotations-no-subject",
    description="Records a frame as containing no subject to annotate (the negative case)",
)

no_subject_request = no_subject_ns.model(
    "NoSubjectFrame",
    {
        "video_url": flask_restx.fields.String(required=True),
        "frame_index": flask_restx.fields.Integer(required=True, min=0),
    },
)

no_subject_response = no_subject_ns.model(
    "NoSubjectFrameResponse",
    {
        "content_id": flask_restx.fields.String,
        "submission_id": flask_restx.fields.String,
        "push_status": flask_restx.fields.String,
    },
)


@no_subject_ns.route("")
class NoSubjectFrame(flask_restx.Resource):
    @require_auth
    @no_subject_ns.expect(no_subject_request, validate=False)
    @no_subject_ns.marshal_with(no_subject_response, code=http.HTTPStatus.ACCEPTED)
    def post(self):
        """Queue a 'no subject present' annotation for the next hourly DANDI upload."""
        body = flask.request.get_json(silent=True)
        if not isinstance(body, dict):
            raise BadRequest("Request body must be a JSON object")

        video_url = body.get("video_url")
        if not isinstance(video_url, str) or not video_url:
            raise BadRequest("'video_url' is required")
        content_id = video_url.rsplit("/", maxsplit=1)[-1]
        if content_id not in CONTENT_ID_TO_DANDI_PATH:
            raise BadRequest(f"Unknown content_id: {content_id}")

        submission_id = uuid.uuid4().hex
        submitted_by = flask.g.user.get("login") or flask.g.user["sub"]
        # Stamp the determination into the record so the buffered JSONL is
        # self-describing regardless of which route produced it.
        record: dict = {
            "submission_id": submission_id,
            "content_id": content_id,
            "submitted_by": submitted_by,
            **body,
            "no_subject": True,
        }

        buffer_dir = NO_SUBJECT_DANDISET_ROOT / "derivatives" / "buffer"
        append_to_hourly_jsonl(record, buffer_dir)
        logger.info("Queued no-subject annotation submission_id=%s content_id=%s", submission_id, content_id)

        return {
            "content_id": content_id,
            "submission_id": submission_id,
            "push_status": "queued",
        }, http.HTTPStatus.ACCEPTED


# =============================================================================
# Frame reports namespace
# =============================================================================


reports_ns = flask_restx.Namespace(
    "reports",
    description="User reports flagging a video frame as inappropriate or otherwise problematic",
)

# Free-text catch-all reason. The frontend modal surfaces a list of canned
# suggestions plus an 'Other' choice with a write-in box (see pozu issue #56).
REPORT_OTHER_REASON = "other"

# Suggested reason codes, documented for the Swagger UI. The backend intentionally
# does NOT hard-enforce this set so the frontend can refine its suggestion list
# without a coordinated backend deploy; the only enforced rule is that the
# free-text 'other' reason must carry a written explanation in `details`.
SUGGESTED_REPORT_REASONS = (
    "inappropriate_content",
    "graphic_or_violent",
    "corrupted_frame",
    REPORT_OTHER_REASON,
)

reported_frame_request = reports_ns.model(
    "ReportedFrame",
    {
        "video_url": flask_restx.fields.String(required=True),
        "frame_index": flask_restx.fields.Integer(required=True, min=0),
        "timestamp": flask_restx.fields.String(),
        "reason": flask_restx.fields.String(
            required=True,
            enum=list(SUGGESTED_REPORT_REASONS),
            description="Why the frame is being reported. One of the suggested codes, or 'other'.",
        ),
        "details": flask_restx.fields.String(
            required=False,
            description="Free-text explanation. Required when `reason` is 'other'.",
        ),
    },
)

reported_frame_response = reports_ns.model(
    "ReportedFrameResponse",
    {
        "content_id": flask_restx.fields.String,
        "submission_id": flask_restx.fields.String,
        "push_status": flask_restx.fields.String,
    },
)


@reports_ns.route("")
class ReportedFrame(flask_restx.Resource):
    @require_auth
    @reports_ns.expect(reported_frame_request, validate=False)
    @reports_ns.marshal_with(reported_frame_response, code=http.HTTPStatus.ACCEPTED)
    def post(self):
        """Queue a report flagging a single frame as inappropriate or problematic."""
        body = flask.request.get_json(silent=True)
        if not isinstance(body, dict):
            raise BadRequest("Request body must be a JSON object")

        video_url = body.get("video_url")
        if not isinstance(video_url, str) or not video_url:
            raise BadRequest("'video_url' is required")
        content_id = video_url.rsplit("/", maxsplit=1)[-1]
        if content_id not in CONTENT_ID_TO_DANDI_PATH:
            raise BadRequest(f"Unknown content_id: {content_id}")

        reason = body.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise BadRequest("'reason' is required")
        reason = reason.strip()

        details = body.get("details")
        if details is not None and not isinstance(details, str):
            raise BadRequest("'details' must be a string when provided")
        # The 'Other' suggestion is meaningless without the write-in text.
        if reason.lower() == REPORT_OTHER_REASON and not (isinstance(details, str) and details.strip()):
            raise BadRequest("'details' is required when reason is 'other'")

        submission_id = uuid.uuid4().hex
        submitted_by = flask.g.user.get("login") or flask.g.user["sub"]
        record: dict = {
            "submission_id": submission_id,
            "content_id": content_id,
            "submitted_by": submitted_by,
            **body,
            "reason": reason,
        }

        buffer_dir = REPORTS_DANDISET_ROOT / "derivatives" / "buffer"
        append_to_hourly_jsonl(record, buffer_dir)
        logger.info("Queued frame report submission_id=%s content_id=%s reason=%s", submission_id, content_id, reason)

        return {
            "content_id": content_id,
            "submission_id": submission_id,
            "push_status": "queued",
        }, http.HTTPStatus.ACCEPTED


# =============================================================================
# Health namespace
# =============================================================================


health_ns = flask_restx.Namespace("health", description="Liveness")


@health_ns.route("")
class Health(flask_restx.Resource):
    def get(self):
        checks = {
            "token_present": bool(EMBER_DANDI_API_KEY),
            "dandiset_root_exists": BBOX_DANDISET_ROOT.exists(),
            "dandiset_yaml_exists": (BBOX_DANDISET_ROOT / "dandiset.yaml").exists(),
            "dandi_bin_exists": pathlib.Path(DANDI_BIN).exists(),
        }
        ok = all(checks.values())
        return {"status": "ok" if ok else "degraded", "checks": checks}, http.HTTPStatus.OK


# =============================================================================
# Admin namespace (user store and role management)
# =============================================================================


admin_ns = flask_restx.Namespace("admin", description="User store administration: users, roles, permissions")

admin_user_model = admin_ns.model(
    "AdminUser",
    {
        "github_id": flask_restx.fields.Integer,
        "login": flask_restx.fields.String,
        "name": flask_restx.fields.String,
        "avatar_url": flask_restx.fields.String,
        "first_seen": flask_restx.fields.String(description="ISO-8601 UTC"),
        "last_seen": flask_restx.fields.String(description="ISO-8601 UTC"),
        "login_count": flask_restx.fields.Integer,
        "roles": flask_restx.fields.List(flask_restx.fields.String),
    },
)

admin_users_response = admin_ns.model(
    "AdminUsersResponse",
    {
        "total": flask_restx.fields.Integer,
        "users": flask_restx.fields.List(flask_restx.fields.Nested(admin_user_model)),
    },
)

admin_role_model = admin_ns.model(
    "AdminRole",
    {
        "name": flask_restx.fields.String,
        "permissions": flask_restx.fields.List(flask_restx.fields.String),
    },
)

admin_roles_response = admin_ns.model(
    "AdminRolesResponse",
    {"roles": flask_restx.fields.List(flask_restx.fields.Nested(admin_role_model))},
)

admin_user_roles_request = admin_ns.model(
    "AdminUserRolesUpdate",
    {"roles": flask_restx.fields.List(flask_restx.fields.String, required=True)},
)

admin_me_response = admin_ns.model(
    "AdminMe",
    {
        "github_id": flask_restx.fields.Integer,
        "login": flask_restx.fields.String,
        "roles": flask_restx.fields.List(flask_restx.fields.String),
        "permissions": flask_restx.fields.List(flask_restx.fields.String),
    },
)

USERS_DEFAULT_LIMIT = 50
USERS_MAX_LIMIT = 200


def _query_int(*, name: str, default: int) -> int:
    """Read an integer query parameter, rejecting non-integer values with a 400."""
    raw = flask.request.args.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        raise BadRequest(f"Query parameter '{name}' must be an integer") from None


@admin_ns.route("/users")
class AdminUsers(flask_restx.Resource):
    @require_permission("users:read")
    @admin_ns.marshal_with(admin_users_response)
    def get(self):
        """List users with pagination and sorting."""
        limit = max(1, min(USERS_MAX_LIMIT, _query_int(name="limit", default=USERS_DEFAULT_LIMIT)))
        offset = max(0, _query_int(name="offset", default=0))
        sort = flask.request.args.get("sort", "last_seen")
        if sort not in USER_SORT_ORDERS:
            raise BadRequest(f"Invalid sort: {sort!r}; expected one of {sorted(USER_SORT_ORDERS)}")
        return list_users(limit=limit, offset=offset, sort=sort)


@admin_ns.route("/roles")
class AdminRoles(flask_restx.Resource):
    @require_permission("roles:read")
    @admin_ns.marshal_with(admin_roles_response)
    def get(self):
        """List every role with its granted permissions."""
        return {"roles": list_roles()}


@admin_ns.route("/users/<int:github_id>/roles")
@admin_ns.param("github_id", "GitHub user id (the JWT `sub`)")
class AdminUserRoles(flask_restx.Resource):
    @require_permission("roles:write")
    @admin_ns.expect(admin_user_roles_request, validate=False)
    @admin_ns.marshal_with(admin_user_model)
    def put(self, github_id):
        """Replace the user's role set with the named roles."""
        body = flask.request.get_json(silent=True)
        if not isinstance(body, dict):
            raise BadRequest("Request body must be a JSON object")
        role_names = body.get("roles")
        if not isinstance(role_names, list) or not all(isinstance(name, str) for name in role_names):
            raise BadRequest("'roles' must be a list of role names")
        return replace_user_roles(github_id=github_id, role_names=role_names)


@admin_ns.route("/me")
class AdminMe(flask_restx.Resource):
    @require_auth
    @admin_ns.marshal_with(admin_me_response)
    def get(self):
        """Return the caller's own roles and permissions, resolved authoritatively from the DB."""
        github_id = int(flask.g.user["sub"])
        return {
            "github_id": github_id,
            "login": flask.g.user.get("login"),
            "roles": resolve_roles(github_id),
            "permissions": sorted(resolve_permissions(github_id)),
        }


# =============================================================================
# Auth (GitHub OAuth)
# =============================================================================


def mint_app_token(github_user: dict, /) -> str:
    """Mint a short-lived signed JWT identifying the authenticated GitHub user.

    The SPA is hosted cross-site from this backend, so rather than a third-party
    session cookie the token travels back to the frontend and is replayed as a
    ``Authorization: Bearer`` header on later API calls.
    """
    # The "roles"/"permissions" claims are a snapshot for UI hints ONLY (e.g.
    # showing or hiding admin views). They can be up to JWT_TTL_SECONDS stale
    # and MUST NOT be trusted for server-side authorization; require_permission
    # re-resolves permissions from the database on every protected request. A
    # user store failure degrades the snapshot to empty rather than blocking
    # token issuance.
    try:
        roles = resolve_roles(github_user["id"])
        permissions = sorted(resolve_permissions(github_user["id"]))
    except sqlite3.Error:
        logger.exception("Could not snapshot roles/permissions into the token; defaulting to none")
        roles, permissions = [], []
    now = datetime.datetime.now(tz=datetime.timezone.utc)
    payload = {
        "iss": JWT_ISSUER,
        "sub": str(github_user["id"]),
        "login": github_user.get("login"),
        "name": github_user.get("name"),
        "avatar_url": github_user.get("avatar_url"),
        "roles": roles,
        "permissions": permissions,
        "iat": now,
        "exp": now + datetime.timedelta(seconds=JWT_TTL_SECONDS),
    }
    return jwt.encode(payload, APP_SECRET_KEY, algorithm=JWT_ALGORITHM)


def register_github_oauth_routes(flask_app: flask.Flask, /) -> None:
    """Register the top-level GitHub OAuth login and callback routes.

    These are plain Flask routes rather than Flask-RESTX resources because they
    serve browser redirects, not the JSON API under ``/api/v1``.
    """

    @flask_app.route("/auth/github/login")
    def github_login():
        """Kick off the OAuth handshake by redirecting the browser to GitHub."""
        if (
            not GITHUB_CLIENT_ID
            or GITHUB_CLIENT_ID == PLACEHOLDER_CLIENT_ID
            or not GITHUB_CLIENT_SECRET
            or GITHUB_CLIENT_SECRET == PLACEHOLDER_CLIENT_SECRET
        ):
            raise BadRequest("GitHub OAuth is not configured on this server")

        state = secrets.token_urlsafe(32)
        flask.session["oauth_state"] = state
        params = urllib.parse.urlencode(
            {
                "client_id": GITHUB_CLIENT_ID,
                "redirect_uri": GITHUB_OAUTH_CALLBACK_URL,
                "scope": GITHUB_OAUTH_SCOPES,
                "state": state,
            }
        )
        return flask.redirect(f"{GITHUB_AUTHORIZE_URL}?{params}")

    @flask_app.route("/auth/github/callback")
    def github_callback():
        """Complete the handshake: verify state, exchange code, mint a JWT."""
        error = flask.request.args.get("error")
        if error:
            raise BadRequest(f"GitHub OAuth error: {error}")

        state = flask.request.args.get("state")
        expected_state = flask.session.pop("oauth_state", None)
        if not expected_state or state != expected_state:
            raise BadRequest("Invalid or missing OAuth state")

        code = flask.request.args.get("code")
        if not code:
            raise BadRequest("Missing OAuth code")

        token_response = requests.post(
            GITHUB_TOKEN_URL,
            headers={"Accept": "application/json"},
            data={
                "client_id": GITHUB_CLIENT_ID,
                "client_secret": GITHUB_CLIENT_SECRET,
                "code": code,
                "redirect_uri": GITHUB_OAUTH_CALLBACK_URL,
            },
            timeout=10,
        )
        token_response.raise_for_status()
        access_token = token_response.json().get("access_token")
        if not access_token:
            raise BadRequest("GitHub did not return an access token")

        user_response = requests.get(
            GITHUB_USER_URL,
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/vnd.github+json"},
            timeout=10,
        )
        user_response.raise_for_status()
        github_user = user_response.json()

        # Persist/refresh the user row and apply the admin bootstrap BEFORE
        # minting the token, so the snapshot claims reflect this login. A user
        # store failure is logged (the RedactFilter still scrubs the handler's
        # output) but must never block issuing the token.
        try:
            record_user_login(github_user)
        except Exception:
            logger.exception(
                "User store update failed for login=%s; issuing the token anyway", github_user.get("login")
            )

        app_token = mint_app_token(github_user)
        logger.info("Authenticated GitHub user login=%s id=%s", github_user.get("login"), github_user.get("id"))

        fragment = urllib.parse.urlencode({"token": app_token})
        return flask.redirect(f"{FRONTEND_URL}#{fragment}")


# =============================================================================
# App
# =============================================================================


def create_app() -> flask.Flask:
    logging.basicConfig(
        level=LOG_LEVEL,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )

    flask_app = flask.Flask(__name__)
    # Signs the OAuth `state` session cookie. Falls back to an ephemeral key in
    # development so the module stays importable without the deployment secret.
    flask_app.secret_key = APP_SECRET_KEY or secrets.token_urlsafe(32)
    flask_cors.CORS(
        flask_app,
        resources={r"/api/.*": {"origins": ["https://pozu-project.github.io"]}},
        # PUT is required by the admin role-assignment endpoint.
        methods=["GET", "POST", "PUT", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization"],
    )
    api = flask_restx.Api(
        flask_app,
        version="1.0",
        title="DANDI Annotation Ingest",
        description=(
            "Accepts per-frame bounding-box annotations and SLEAP .slp label files, "
            "queues them in hourly JSONL buffers, and uploads to DANDI via a scheduled CRON job. "
            "Also accepts frame reports flagging inappropriate or problematic content."
        ),
        doc="/api/v1/docs",
        prefix="/api/v1",
    )

    api.add_namespace(bbox_ns, path="/annotations/bbox")
    api.add_namespace(labels_ns, path="/annotations/labels")
    api.add_namespace(no_subject_ns, path="/annotations/no-subject")
    api.add_namespace(reports_ns, path="/reports")
    api.add_namespace(health_ns, path="/health")
    api.add_namespace(admin_ns, path="/admin")

    @api.errorhandler(BadRequest)
    def _bad_request(err):
        return {"message": str(err)}, http.HTTPStatus.BAD_REQUEST

    @api.errorhandler(Unauthorized)
    def _unauthorized(err):
        return {"message": str(err)}, http.HTTPStatus.UNAUTHORIZED

    # The RESTX error handlers above only cover resources under the API; the
    # top-level OAuth routes need Flask-level handlers for the same exceptions.
    @flask_app.errorhandler(BadRequest)
    def _bad_request_flask(err):
        return flask.jsonify({"message": str(err)}), http.HTTPStatus.BAD_REQUEST

    @flask_app.errorhandler(Unauthorized)
    def _unauthorized_flask(err):
        return flask.jsonify({"message": str(err)}), http.HTTPStatus.UNAUTHORIZED

    register_github_oauth_routes(flask_app)

    @flask_app.route("/")
    def _index():
        return flask.redirect("/api/v1/docs", code=301)

    return flask_app


app = create_app()


if __name__ == "__main__":
    app.run(
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
        debug=os.environ.get("FLASK_DEBUG") == "1",
    )
