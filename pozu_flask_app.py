"""
DANDI annotation ingest service - single-file Flask + Flask-RESTX app.

Designed to drop into PythonAnywhere as the project's flask app. PA's WSGI
file should import the module-level `app`:

Check `/api/v1/docs` for API reference.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import pathlib
import sys
import uuid
import http

import filelock
import flask
import flask_cors
import flask_restx

# =============================================================================
# Config
# =============================================================================

VENV_BIN = "/home/CodyCBakerPhD/.virtualenvs/pozu/bin"
DANDI_BIN = f"{VENV_BIN}/dandi"

api_key_file_path = pathlib.Path("/home/CodyCBakerPhD/dandi_token")  # chmod 600
EMBER_DANDI_API_KEY = api_key_file_path.read_text().strip()

BBOX_DANDISET_ROOT = pathlib.Path("/home/CodyCBakerPhD/mysite/000469")
LABELS_DANDISET_ROOT = pathlib.Path("/home/CodyCBakerPhD/mysite/000470")
DANDI_INSTANCE = "https://api-dandi.emberarchive.org/api"
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
        record: dict = {"submission_id": submission_id, "content_id": content_id, **body}

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
# App
# =============================================================================


def create_app() -> flask.Flask:
    logging.basicConfig(
        level=LOG_LEVEL,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )

    flask_app = flask.Flask(__name__)
    flask_cors.CORS(
        flask_app,
        resources={r"/api/.*": {"origins": ["https://codycbakerphd.github.io"]}},
        methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type"],
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
