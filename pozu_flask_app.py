"""
DANDI annotation ingest service - single-file Flask + Flask-RESTX app.

Designed to drop into PythonAnywhere as the project's flask app. PA's WSGI
file should import the module-level `app`:

Check `/api/v1/docs` for API reference.
"""

from __future__ import annotations

import base64
import binascii
import datetime
import functools
import json
import logging
import os
import pathlib
import secrets
import shutil
import subprocess
import sys
import tempfile
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
# Short MP4 clips assembled server-side (via ffmpeg) from a few frames posted by
# the frontend. Dandiset 000474 is reserved for them. Provision 000474 on the
# deployment (a `dandiset.yaml` plus a `derivatives/` tree) the same way as the
# bbox/labels dandisets. Unlike the JSONL routes, clips can be large, so
# cron_snapshot.py deletes each clip from local disk after a successful upload.
CLIPS_DANDISET_ROOT = pathlib.Path("/home/CodyCBakerPhD/mysite/000474")
DANDI_INSTANCE = "https://api-dandi.emberarchive.org/api"
LOG_LEVEL = "INFO"

# -- Clip encoding (ffmpeg) ---------------------------------------------------
FFMPEG_BIN = os.environ.get("FFMPEG_BIN") or shutil.which("ffmpeg") or "/usr/bin/ffmpeg"
FFMPEG_TIMEOUT_SECONDS = 120

MAX_CLIP_FRAMES = 64
MAX_CLIP_FRAME_BYTES = 5 * 1024 * 1024
MAX_CLIP_TOTAL_BYTES = 64 * 1024 * 1024
CLIP_MIN_FPS = 0.1
CLIP_MAX_FPS = 120.0
CLIP_CODECS = ("libx264", "libx265")
CLIP_DEFAULT_CODEC = "libx264"
CLIP_MIN_CRF = 0
CLIP_MAX_CRF = 51
CLIP_DEFAULT_CRF = 23
CLIP_MIN_DIMENSION = 16
CLIP_MAX_DIMENSION = 4096
# h.264/h.265 with yuv420p require even output dimensions, so when the caller
# does not request an explicit size the source size is rounded down to even.
CLIP_DEFAULT_SCALE_FILTER = "scale=trunc(iw/2)*2:trunc(ih/2)*2"

# Base64 request bodies for a full clip can be large; bound them so an oversized
# upload is rejected with a 413 before it is buffered in worker memory.
MAX_CONTENT_LENGTH_BYTES = 96 * 1024 * 1024

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8\xff"

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
# Clips namespace (frames -> mp4 via ffmpeg)
# =============================================================================


def decode_clip_frames(frames, /) -> tuple[list[bytes], str]:
    """Validate and decode the posted frame list into raw image bytes.

    Frames arrive as base64 strings (a bare payload or a ``data:image/...;base64,``
    URL). Every frame must be a PNG or JPEG, all frames must share one format, and
    per-frame plus total byte budgets are enforced so a hostile payload cannot fill
    the disk. Returns the decoded blobs and the common file extension.
    """
    if not isinstance(frames, list) or not frames:
        raise BadRequest("'frames' must be a non-empty list of base64-encoded images")
    if len(frames) > MAX_CLIP_FRAMES:
        raise BadRequest(f"'frames' may contain at most {MAX_CLIP_FRAMES} images")

    blobs: list[bytes] = []
    extension = None
    total_bytes = 0
    for index, frame in enumerate(frames):
        if not isinstance(frame, str) or not frame:
            raise BadRequest(f"Frame {index} must be a base64-encoded string")
        payload = frame.partition(",")[2] if frame.startswith("data:") else frame
        try:
            blob = base64.b64decode(payload, validate=True)
        except (binascii.Error, ValueError):
            raise BadRequest(f"Frame {index} is not valid base64")

        if blob.startswith(PNG_MAGIC):
            frame_extension = "png"
        elif blob.startswith(JPEG_MAGIC):
            frame_extension = "jpg"
        else:
            raise BadRequest(f"Frame {index} is not a PNG or JPEG image")
        if extension is None:
            extension = frame_extension
        elif frame_extension != extension:
            raise BadRequest("All frames must share the same image format")

        if len(blob) > MAX_CLIP_FRAME_BYTES:
            raise BadRequest(f"Frame {index} exceeds the {MAX_CLIP_FRAME_BYTES} byte per-frame limit")
        total_bytes += len(blob)
        if total_bytes > MAX_CLIP_TOTAL_BYTES:
            raise BadRequest(f"Frames exceed the {MAX_CLIP_TOTAL_BYTES} byte total limit")
        blobs.append(blob)

    return blobs, extension


def parse_clip_encoding_parameters(body, /) -> dict:
    """Validate the caller-supplied ffmpeg parameters, applying defaults.

    Returns a dict with ``fps``, ``codec``, ``crf``, ``width``, ``height``, and the
    derived ``scale_filter``. The codec is restricted to an allowlist so the request
    can never smuggle arbitrary ffmpeg arguments.
    """
    fps = body.get("fps")
    if not isinstance(fps, (int, float)) or isinstance(fps, bool):
        raise BadRequest("'fps' is required and must be a number")
    if not CLIP_MIN_FPS <= fps <= CLIP_MAX_FPS:
        raise BadRequest(f"'fps' must be between {CLIP_MIN_FPS} and {CLIP_MAX_FPS}")

    codec = body.get("codec", CLIP_DEFAULT_CODEC)
    if codec not in CLIP_CODECS:
        raise BadRequest(f"'codec' must be one of: {', '.join(CLIP_CODECS)}")

    crf = body.get("crf", CLIP_DEFAULT_CRF)
    if not isinstance(crf, int) or isinstance(crf, bool) or not CLIP_MIN_CRF <= crf <= CLIP_MAX_CRF:
        raise BadRequest(f"'crf' must be an integer between {CLIP_MIN_CRF} and {CLIP_MAX_CRF}")

    width = body.get("width")
    height = body.get("height")
    if (width is None) != (height is None):
        raise BadRequest("'width' and 'height' must be provided together")
    if width is not None:
        for name, value in (("width", width), ("height", height)):
            if not isinstance(value, int) or isinstance(value, bool):
                raise BadRequest(f"'{name}' must be an integer")
            if not CLIP_MIN_DIMENSION <= value <= CLIP_MAX_DIMENSION:
                raise BadRequest(f"'{name}' must be between {CLIP_MIN_DIMENSION} and {CLIP_MAX_DIMENSION}")
            if value % 2 != 0:
                raise BadRequest(f"'{name}' must be even (required by yuv420p output)")
        scale_filter = f"scale={width}:{height}"
    else:
        scale_filter = CLIP_DEFAULT_SCALE_FILTER

    return {
        "fps": float(fps),
        "codec": codec,
        "crf": crf,
        "width": width,
        "height": height,
        "scale_filter": scale_filter,
    }


def write_clip_mp4(*, frame_blobs, frame_extension, fps, codec, crf, scale_filter, clip_filename) -> pathlib.Path:
    """Encode the decoded frames into an MP4 inside the clips dandiset buffer.

    The frames and the in-progress MP4 live in a temporary directory that is
    always removed, success or failure, so no scratch bytes outlive the request.
    The finished MP4 is first moved next to its destination and then renamed into
    place, so ``cron_snapshot.py`` can only ever see complete ``*.mp4`` files.
    """
    buffer_dir = CLIPS_DANDISET_ROOT / "derivatives" / "buffer"
    buffer_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="pozu-clip-") as tmp:
        tmp_dir = pathlib.Path(tmp)
        for index, blob in enumerate(frame_blobs):
            (tmp_dir / f"frame_{index:05d}.{frame_extension}").write_bytes(blob)

        tmp_output = tmp_dir / clip_filename
        cmd = [
            FFMPEG_BIN,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-framerate",
            str(fps),
            "-start_number",
            "0",
            "-i",
            str(tmp_dir / f"frame_%05d.{frame_extension}"),
            "-frames:v",
            str(len(frame_blobs)),
            "-vf",
            scale_filter,
            "-c:v",
            codec,
            "-crf",
            str(crf),
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(tmp_output),
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=FFMPEG_TIMEOUT_SECONDS, check=False)
        except subprocess.TimeoutExpired:
            raise BadRequest(f"ffmpeg timed out after {FFMPEG_TIMEOUT_SECONDS}s while encoding the clip")
        if proc.returncode != 0:
            stderr_tail = (proc.stderr or "").strip().splitlines()[-3:]
            logger.warning("ffmpeg failed rc=%d for %s: %s", proc.returncode, clip_filename, proc.stderr)
            raise BadRequest("ffmpeg could not encode the supplied frames: " + " | ".join(stderr_tail))

        # Two-step move: shutil.move may copy across filesystems, so land the
        # bytes beside the destination first, then atomically rename into the
        # name cron_snapshot.py sweeps.
        partial_path = buffer_dir / f"{clip_filename}.part"
        final_path = buffer_dir / clip_filename
        shutil.move(str(tmp_output), str(partial_path))
        partial_path.replace(final_path)

    logger.info("Encoded clip %s (%d frames)", clip_filename, len(frame_blobs))
    return final_path


clips_ns = flask_restx.Namespace(
    "clips",
    description="Assemble a few posted video frames into an MP4 clip (via ffmpeg) destined for DANDI",
)

clip_request = clips_ns.model(
    "VideoClip",
    {
        "video_url": flask_restx.fields.String(required=True),
        "frames": flask_restx.fields.List(
            flask_restx.fields.String,
            required=True,
            description=(
                "Ordered base64-encoded PNG or JPEG frames (bare base64 or data: URLs). "
                f"All frames must share one format; at most {MAX_CLIP_FRAMES} frames."
            ),
        ),
        "fps": flask_restx.fields.Float(
            required=True, min=CLIP_MIN_FPS, max=CLIP_MAX_FPS, description="Output frame rate"
        ),
        "codec": flask_restx.fields.String(
            required=False, enum=list(CLIP_CODECS), description=f"Video codec (default {CLIP_DEFAULT_CODEC})"
        ),
        "crf": flask_restx.fields.Integer(
            required=False,
            min=CLIP_MIN_CRF,
            max=CLIP_MAX_CRF,
            description=f"Constant rate factor (default {CLIP_DEFAULT_CRF}; lower is higher quality)",
        ),
        "width": flask_restx.fields.Integer(
            required=False, description="Optional even output width; must be paired with 'height'"
        ),
        "height": flask_restx.fields.Integer(
            required=False, description="Optional even output height; must be paired with 'width'"
        ),
        "timestamp": flask_restx.fields.String(required=False),
    },
)

clip_response = clips_ns.model(
    "VideoClipResponse",
    {
        "content_id": flask_restx.fields.String,
        "submission_id": flask_restx.fields.String,
        "clip_file": flask_restx.fields.String,
        "frame_count": flask_restx.fields.Integer,
        "push_status": flask_restx.fields.String,
    },
)


@clips_ns.route("")
class VideoClip(flask_restx.Resource):
    @require_auth
    @clips_ns.expect(clip_request, validate=False)
    @clips_ns.marshal_with(clip_response, code=http.HTTPStatus.ACCEPTED)
    def post(self):
        """Encode posted frames into an MP4 clip and queue it for the next hourly DANDI upload."""
        body = flask.request.get_json(silent=True)
        if not isinstance(body, dict):
            raise BadRequest("Request body must be a JSON object")

        video_url = body.get("video_url")
        if not isinstance(video_url, str) or not video_url:
            raise BadRequest("'video_url' is required")
        content_id = video_url.rsplit("/", maxsplit=1)[-1]
        if content_id not in CONTENT_ID_TO_DANDI_PATH:
            raise BadRequest(f"Unknown content_id: {content_id}")

        frame_blobs, frame_extension = decode_clip_frames(body.get("frames"))
        parameters = parse_clip_encoding_parameters(body)

        submission_id = uuid.uuid4().hex
        submitted_by = flask.g.user.get("login") or flask.g.user["sub"]
        hour_tag = datetime.datetime.utcnow().strftime("%Y-%m-%d-%H")
        clip_filename = f"{hour_tag}-{submission_id}.mp4"

        clip_path = write_clip_mp4(
            frame_blobs=frame_blobs,
            frame_extension=frame_extension,
            fps=parameters["fps"],
            codec=parameters["codec"],
            crf=parameters["crf"],
            scale_filter=parameters["scale_filter"],
            clip_filename=clip_filename,
        )

        # A JSONL provenance record travels alongside the MP4 so the uploaded
        # clip stays attributable to its source video and submitter.
        record: dict = {
            "submission_id": submission_id,
            "content_id": content_id,
            "submitted_by": submitted_by,
            "video_url": video_url,
            "clip_file": clip_path.name,
            "frame_count": len(frame_blobs),
            "frame_format": frame_extension,
            "fps": parameters["fps"],
            "codec": parameters["codec"],
            "crf": parameters["crf"],
            "width": parameters["width"],
            "height": parameters["height"],
            "timestamp": body.get("timestamp"),
        }
        buffer_dir = CLIPS_DANDISET_ROOT / "derivatives" / "buffer"
        append_to_hourly_jsonl(record, buffer_dir)
        logger.info("Queued clip submission_id=%s content_id=%s file=%s", submission_id, content_id, clip_path.name)

        return {
            "content_id": content_id,
            "submission_id": submission_id,
            "clip_file": clip_path.name,
            "frame_count": len(frame_blobs),
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
            "ffmpeg_bin_exists": pathlib.Path(FFMPEG_BIN).exists(),
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
    flask_app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH_BYTES
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
            "queues them in hourly JSONL buffers, and uploads to DANDI via a scheduled CRON job. "
            "Also accepts frame reports flagging inappropriate or problematic content, and "
            "assembles posted video frames into MP4 clips via ffmpeg for upload to DANDI."
        ),
        doc="/api/v1/docs",
        prefix="/api/v1",
    )

    api.add_namespace(bbox_ns, path="/annotations/bbox")
    api.add_namespace(labels_ns, path="/annotations/labels")
    api.add_namespace(no_subject_ns, path="/annotations/no-subject")
    api.add_namespace(reports_ns, path="/reports")
    api.add_namespace(clips_ns, path="/clips")
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
