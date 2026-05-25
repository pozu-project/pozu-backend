"""
DANDI annotation ingest service - single-file Flask + Flask-RESTX app.

Designed to drop into PythonAnywhere as the project's flask app. PA's WSGI
file should import the module-level `app`:

Check `/api/v1/docs` for API reference.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import uuid
import http
import pathlib

import flask
import flask_cors
import flask_restx
import werkzeug.datastructures

# =============================================================================
# Config
# =============================================================================

VENV_BIN = "/home/CodyCBakerPhD/.virtualenvs/pose-zoo/bin"
DANDI_BIN = f"{VENV_BIN}/dandi"

api_key_file_path = pathlib.Path("/home/CodyCBakerPhD/dandi_token")  # chmod 600
EMBER_DANDI_API_KEY = api_key_file_path.read_text().strip()

DANDISET_ROOT = pathlib.Path("/home/CodyCBakerPhD/mysite/000469")
DANDI_INSTANCE = "https://api-dandi.emberarchive.org/api"
MAX_SLP_BYTES = int(os.environ.get("MAX_SLP_BYTES", str(500 * 1024 * 1024)))
LOG_LEVEL = "INFO"

# TODO: replace with HTTP call to ember-cache once that URL exists.
CONTENT_ID_TO_DANDI_PATH = {
    "59e7d85b-6827-4e62-977a-bab97c54df82": "emberset-test0/sub-test1/sub-test1_ses-test2.nwb",
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


_handler.addFilter(RedactFilter([EMBER_DANDI_API_KEY]))


# =============================================================================
# Helper: invoke `dandi upload` for a file inside the dandiset
# =============================================================================


def dandi_upload(file_path: pathlib.Path) -> tuple[int, str, str]:
    """Upload `file_path` to the configured DANDI instance. Returns (rc, stdout, stderr)."""
    env = os.environ.copy()
    env["EMBER_DANDI_API_KEY"] = EMBER_DANDI_API_KEY
    env["PATH"] = f"{VENV_BIN}:{env.get('PATH', '')}"

    cmd = [DANDI_BIN, "upload", "--dandi-instance", DANDI_INSTANCE]
    logger.info("Running: %s (cwd=%s)", " ".join(cmd), DANDISET_ROOT)
    proc = subprocess.run(
        cmd,
        cwd=DANDISET_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    logger.info(
        "dandi upload rc=%d\nstdout: %s\nstderr: %s",
        proc.returncode,
        proc.stdout,
        proc.stderr,
    )
    return proc.returncode, proc.stdout, proc.stderr


# =============================================================================
# BBox namespace
# =============================================================================


bbox_ns = Nflask_restx.amespace(
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
        "bbox_file_path": flask_restx.fields.String,
        "submission_id": flask_restx.fields.String,
        "push_status": flask_restx.fields.String,
        "push_message": flask_restx.fields.String,
    },
)


@bbox_ns.route("")
class BBoxAnnotation(flask_restx.Resource):
    @bbox_ns.expect(bbox_request, validate=False)
    @bbox_ns.marshal_with(bbox_response, code=http.HTTPStatus.ACCEPTED)
    def post(self):
        """Store one bounding-box annotation and push to DANDI."""
        body = flask.request.get_json(silent=True)
        if not isinstance(body, dict):
            raise BadRequest("Request body must be a JSON object")

        content_id = body["video_url"].split("/")[-1]
        dandi_path = CONTENT_ID_TO_DANDI_PATH.get(content_id)
        if dandi_path is None:
            raise BadRequest(f"Unknown content_id: {content_id}")

        frame_index = int(body["frame_index"])
        submission_id = uuid.uuid4().hex
        body["submission_id"] = submission_id

        bbox_file_path = DANDISET_ROOT / "derivatives" / "incoming" / f"id-{submission_id}.json"
        bbox_file_path.parent.mkdir(parents=True, exist_ok=True)
        bbox_file_path.write_text(json.dumps(body, indent=2, sort_keys=True))

        rc, stdout, stderr = dandi_upload(bbox_file_path)

        return {
            "content_id": content_id,
            "bbox_file_path": str(bbox_file_path),
            "submission_id": submission_id,
            "push_status": "succeeded" if rc == 0 else "failed",
        }, http.HTTPStatus.ACCEPTED


# =============================================================================
# Pose (.slp) namespace
# =============================================================================


pose_ns = flask_restx.Namespace("annotations-pose", description="SLEAP .slp pose annotation uploads")

upload_parser = flask_restx.reqparse.RequestParser()
upload_parser.add_argument(
    "file",
    location="files",
    type=werkzeug.datastructures.FileStorage,
    required=True,
    help="The .slp file produced by SLEAP",
)
upload_parser.add_argument(
    "video_url",
    location="form",
    type=str,
    required=True,
    help="Blob URL of the source video; trailing segment is the content-id.",
)

pose_response = pose_ns.model(
    "PoseAnnotationResponse",
    {
        "content_id": flask_restx.fields.String,
        "slp_file_path": flask_restx.fields.String,
        "submission_id": flask_restx.fields.String,
        "push_status": flask_restx.fields.String,
        "push_message": flask_restx.fields.String,
    },
)


@pose_ns.route("")
class PoseAnnotation(flask_restx.Resource):
    @pose_ns.expect(upload_parser)
    @pose_ns.marshal_with(pose_response, code=http.HTTPStatus.ACCEPTED)
    def post(self):
        """Store a SLEAP .slp file and push to DANDI."""
        args = upload_parser.parse_args()
        upload: werkzeug.datastructures.FileStorage = args["file"]
        video_url: str = args["video_url"]

        if not (upload.filename or "").lower().endswith(".slp"):
            raise BadRequest("Upload must be a .slp file")

        content_id = video_url.rsplit("/", maxsplit=1)[-1]
        try:
            dandi_path = CONTENT_ID_TO_DANDI_PATH[content_id]
        except KeyError:
            raise BadRequest(f"Unknown content_id: {content_id}")

        submission_id = uuid.uuid4().hex
        slp_file_path = DANDISET_ROOT / dandi_path.removesuffix(".nwb") / f"pose_id-{submission_id}.slp"
        slp_file_path.parent.mkdir(parents=True, exist_ok=True)

        # Stream to disk with a size cap so a huge upload can't fill PA's quota.
        written = 0
        with slp_file_path.open("wb") as out:
            while True:
                chunk = upload.stream.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_SLP_BYTES:
                    slp_file_path.unlink(missing_ok=True)
                    raise BadRequest(f"SLP upload exceeds limit of {MAX_SLP_BYTES} bytes")
                out.write(chunk)
        logger.info("Wrote SLP (%d bytes) -> %s", written, slp_file_path)

        rc, stdout, stderr = dandi_upload(slp_file_path)

        return {
            "content_id": content_id,
            "slp_file_path": str(slp_file_path),
            "submission_id": submission_id,
            "push_status": "succeeded" if rc == 0 else "failed",
        }, http.HTTPStatus.ACCEPTED


# =============================================================================
# Health namespace
# =============================================================================


health_ns = flask_restx.Namespace("health", description="Liveness")


@health_ns.route("")
class Health(flask_restx.Resource):
    def get(self):
        checks = {
            "token_present": bool(EMBER_DANDI_API_TOKEN),
            "dandiset_root_exists": DANDISET_ROOT.exists(),
            "dandiset_yaml_exists": (DANDISET_ROOT / "dandiset.yaml").exists(),
            "dandi_bin_exists": pathlib.Path(DANDI_BIN).exists(),
        }
        ok = all(checks.values())
        return {"status": "ok"}, http.HTTPStatus.OK


# =============================================================================
# App
# =============================================================================


def create_app() -> flask.Flask:
    logging.basicConfig(
        level=LOG_LEVEL,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )

    flask_app = flask.Flask(__name__)
    flask.CORS(
        flask_app,
        resources={r"/api/*": {"origins": ["https://codycbakerphd.github.io"]}},
        methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type"],
    )
    api = flask_restx.Api(
        flask_app,
        version="1.0",
        title="DANDI Annotation Ingest",
        description=(
            "Accepts per-frame bounding-box annotations and SLEAP .slp files, "
            "organizes them inside a local dandiset clone, and uploads to DANDI."
        ),
        doc="/api/v1/docs",
        prefix="/api/v1",
    )

    api.add_namespace(bbox_ns, path="/annotations/bbox")
    api.add_namespace(pose_ns, path="/annotations/pose")
    api.add_namespace(health_ns, path="/health")

    @api.errorhandler(BadRequest)
    def _bad_request(err):
        return {"message": str(err)}, http.HTTPStatus.BAD_REQUEST

    @flask_app.route("/")
    def _index():
        return (
            """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>DANDI Annotation Ingest</title>
  <style>
    body { font-family: system-ui, sans-serif; max-width: 720px;
           margin: 4em auto; padding: 0 1em; line-height: 1.5; color: #222; }
    h1 { margin-bottom: 0.2em; }
    .sub { color: #666; margin-top: 0; }
    code { background: #f4f4f4; padding: 0.1em 0.35em; border-radius: 3px; }
    ul { padding-left: 1.2em; }
    a { color: #0b66c2; text-decoration: none; }
    a:hover { text-decoration: underline; }
  </style>
</head>
<body>
  <h1>DANDI Annotation Ingest</h1>
  <p class="sub">Flask + Flask-RESTX service for ingesting frame annotations
  into a local dandiset and pushing to the DANDI archive.</p>

  <h2>Endpoints</h2>
  <ul>
    <li><a href="/api/v1/docs">Swagger UI</a> &mdash; interactive API docs</li>
    <li><code>POST /api/v1/annotations/bbox</code> &mdash; JSON bbox annotation</li>
    <li><code>POST /api/v1/annotations/pose</code> &mdash; SLEAP <code>.slp</code> upload</li>
    <li><a href="/api/v1/health">GET /api/v1/health</a> &mdash; liveness</li>
  </ul>
</body>
</html>""",
            200,
            {"Content-Type": "text/html; charset=utf-8"},
        )

    return flask_app


app = create_app()


if __name__ == "__main__":
    app.run(
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
        debug=os.environ.get("FLASK_DEBUG") == "1",
    )
