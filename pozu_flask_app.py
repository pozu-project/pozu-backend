"""
DANDI annotation ingest service - single-file Flask + Flask-RESTX app.

Designed to drop into PythonAnywhere as the project's flask app. PA's WSGI
file should import the module-level `app`:

Check `/api/v1/docs` for API reference.
"""

from __future__ import annotations

import datetime
import functools
import json
import logging
import os
import pathlib
import secrets
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

# A historical deployment hard-coded this literal string as the client-id default.
# Treat it as "unconfigured" so a stale value cannot leak into the GitHub redirect
# and 404 there; the startup check below also warns loudly if it is ever seen.
PLACEHOLDER_CLIENT_ID = "<client id>"

GITHUB_CLIENT_ID = load_secret(env_var="GITHUB_CLIENT_ID", file_path="/home/CodyCBakerPhD/github_oauth_client_id")
if GITHUB_CLIENT_ID == PLACEHOLDER_CLIENT_ID:
    GITHUB_CLIENT_ID = ""
GITHUB_CLIENT_SECRET = load_secret(
    env_var="GITHUB_CLIENT_SECRET", file_path="/home/CodyCBakerPhD/github_oauth_client_secret"
)

# Signs both the short-lived OAuth `state` (Flask session cookie) and the app JWT.
APP_SECRET_KEY = load_secret(env_var="APP_SECRET_KEY", file_path="/home/CodyCBakerPhD/app_secret_key")

# Where the SPA lives. The callback redirects here with the freshly minted JWT.
FRONTEND_URL = os.environ.get("FRONTEND_URL", "https://pozu-project.github.io/pozu/")

JWT_ALGORITHM = "HS256"
JWT_ISSUER = "pozu-backend"
JWT_TTL_SECONDS = 3600

BBOX_DANDISET_ROOT = pathlib.Path("/home/CodyCBakerPhD/mysite/000469")
LABELS_DANDISET_ROOT = pathlib.Path("/home/CodyCBakerPhD/mysite/000470")
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
# Auth (GitHub OAuth)
# =============================================================================


def mint_app_token(github_user: dict, /) -> str:
    """Mint a short-lived signed JWT identifying the authenticated GitHub user.

    The SPA is hosted cross-site from this backend, so rather than a third-party
    session cookie the token travels back to the frontend and is replayed as a
    ``Authorization: Bearer`` header on later API calls.
    """
    now = datetime.datetime.now(tz=datetime.timezone.utc)
    payload = {
        "iss": JWT_ISSUER,
        "sub": str(github_user["id"]),
        "login": github_user.get("login"),
        "name": github_user.get("name"),
        "avatar_url": github_user.get("avatar_url"),
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
        if not GITHUB_CLIENT_ID or GITHUB_CLIENT_ID == PLACEHOLDER_CLIENT_ID:
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
        methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization"],
    )
    api = flask_restx.Api(
        flask_app,
        version="1.0",
        title="DANDI Annotation Ingest",
        description=(
            "Accepts per-frame bounding-box annotations and SLEAP .slp label files, "
            "queues them in hourly JSONL buffers, and uploads to DANDI via a scheduled CRON job."
        ),
        doc="/api/v1/docs",
        prefix="/api/v1",
    )

    api.add_namespace(bbox_ns, path="/annotations/bbox")
    api.add_namespace(labels_ns, path="/annotations/labels")
    api.add_namespace(health_ns, path="/health")

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
