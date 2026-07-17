"""Tests for the video clip endpoint (POST /api/v1/clips)."""

import base64
import http
import json
import pathlib
import shutil
import subprocess
import types

import pytest

import pozu_flask_app

APP_SECRET = "test-app-secret-at-least-32-bytes-long"

# Any key in CONTENT_ID_TO_DANDI_PATH works; its last path segment is the content_id.
CONTENT_ID = next(iter(pozu_flask_app.CONTENT_ID_TO_DANDI_PATH))
VIDEO_URL = f"https://example.org/videos/{CONTENT_ID}"

ENDPOINT = "/api/v1/clips"
ALLOWED_ORIGIN = "https://pozu-project.github.io"

# Magic-byte-correct stand-in; the ffprobe and dandi upload steps are
# monkeypatched in these tests, so only the format sniffing needs to see it.
FAKE_MP4 = b"\x00\x00\x00\x18" + pozu_flask_app.MP4_FTYP_TAG + b"isom" + b"\x00" * 16


def _b64(blob, /):
    return base64.b64encode(blob).decode("ascii")


@pytest.fixture
def captured(monkeypatch):
    """Capture written clip files and upload calls, instead of touching disk or DANDI."""
    written = []
    uploaded = []
    monkeypatch.setattr(pozu_flask_app, "APP_SECRET_KEY", APP_SECRET)

    def fake_write_clip_files(**kwargs):
        written.append(kwargs)
        clip_path = pathlib.Path("clips") / kwargs["clip_filename"]
        return [clip_path, clip_path.with_suffix(".mp4.json")]

    monkeypatch.setattr(pozu_flask_app, "write_clip_files", fake_write_clip_files)
    monkeypatch.setattr(pozu_flask_app, "upload_clip_to_dandi", lambda clip_paths: uploaded.append(clip_paths))
    return written, uploaded


@pytest.fixture
def client(captured):
    flask_app = pozu_flask_app.create_app()
    flask_app.config.update(TESTING=True)
    return flask_app.test_client()


def _clip_body(**overrides):
    """A minimally valid VideoClip payload."""
    body = {
        "video_url": VIDEO_URL,
        "mp4": _b64(FAKE_MP4),
        "timestamp": "2026-07-17T00:00:00Z",
    }
    body.update(overrides)
    return body


def _auth_headers():
    token = pozu_flask_app.mint_app_token({"id": 4242, "login": "octocat", "name": "Mona"})
    return {"Authorization": f"Bearer {token}"}


# No sign-in is required for clips at this time; the DANDI upload authenticates
# with the server-stored API key. Attribution is still best-effort from an
# optional Bearer token.
@pytest.mark.ai_generated
@pytest.mark.parametrize(
    ("headers_factory", "expected_submitted_by"),
    [
        pytest.param(dict, "anonymous", id="no-header"),
        pytest.param(lambda: {"Authorization": "token abc"}, "anonymous", id="malformed-header"),
        pytest.param(lambda: {"Authorization": "Bearer not-a-real-jwt"}, "anonymous", id="invalid-token"),
        pytest.param(_auth_headers, "octocat", id="valid-token"),
    ],
)
def test_accepts_request_with_optional_identity(client, captured, headers_factory, expected_submitted_by):
    written, _ = captured
    response = client.post(ENDPOINT, json=_clip_body(), headers=headers_factory())

    assert response.status_code == http.HTTPStatus.CREATED
    assert written[0]["record"]["submitted_by"] == expected_submitted_by


@pytest.mark.ai_generated
def test_response_carries_cors_header(client):
    response = client.post(ENDPOINT, json=_clip_body(), headers={"Origin": ALLOWED_ORIGIN})

    assert response.status_code == http.HTTPStatus.CREATED
    assert response.headers.get("Access-Control-Allow-Origin") == ALLOWED_ORIGIN


@pytest.mark.ai_generated
@pytest.mark.parametrize(
    "mp4_value",
    [
        pytest.param(_b64(FAKE_MP4), id="bare-base64"),
        pytest.param(f"data:video/mp4;base64,{_b64(FAKE_MP4)}", id="data-url"),
    ],
)
def test_accepts_and_uploads_valid_clip(client, captured, mp4_value):
    written, uploaded = captured
    response = client.post(ENDPOINT, json=_clip_body(mp4=mp4_value), headers=_auth_headers())

    assert response.status_code == http.HTTPStatus.CREATED
    payload = response.get_json()
    assert payload["push_status"] == "uploaded"
    assert payload["content_id"] == CONTENT_ID
    assert payload["clip_file"].endswith(".mp4")
    assert payload["clip_size_bytes"] == len(FAKE_MP4)
    assert payload["submission_id"]

    assert len(written) == 1
    assert written[0]["mp4_blob"] == FAKE_MP4
    record = written[0]["record"]
    assert record["content_id"] == CONTENT_ID
    assert record["submitted_by"] == "octocat"
    assert record["video_url"] == VIDEO_URL
    assert record["clip_file"] == payload["clip_file"]
    assert record["clip_size_bytes"] == len(FAKE_MP4)

    # The upload runs synchronously on the exact files that were written.
    assert len(uploaded) == 1
    assert [path.name for path in uploaded[0]] == [payload["clip_file"], payload["clip_file"] + ".json"]


@pytest.mark.ai_generated
@pytest.mark.parametrize(
    ("overrides", "message_snippet"),
    [
        pytest.param({"mp4": None}, "mp4", id="missing-mp4"),
        pytest.param({"mp4": 12345}, "base64-encoded string", id="mp4-not-a-string"),
        pytest.param({"mp4": "!!! not base64 !!!"}, "base64", id="mp4-invalid-base64"),
        pytest.param({"mp4": _b64(b"\x00" * 32)}, "ftyp", id="mp4-missing-ftyp"),
        pytest.param({"video_url": None}, "video_url", id="missing-video-url"),
        pytest.param(
            {"video_url": "https://example.org/videos/not-a-real-id"},
            "content_id",
            id="unknown-content-id",
        ),
    ],
)
def test_invalid_payloads_are_rejected(client, captured, overrides, message_snippet):
    written, uploaded = captured
    body = _clip_body(**overrides)
    for key, value in overrides.items():
        if value is None:
            del body[key]

    response = client.post(ENDPOINT, json=body, headers=_auth_headers())

    assert response.status_code == http.HTTPStatus.BAD_REQUEST
    assert message_snippet in response.get_json()["message"]
    assert written == []
    assert uploaded == []


@pytest.mark.ai_generated
def test_oversized_mp4_is_rejected(client, captured, monkeypatch):
    written, uploaded = captured
    monkeypatch.setattr(pozu_flask_app, "MAX_CLIP_MP4_BYTES", 8)

    response = client.post(ENDPOINT, json=_clip_body(), headers=_auth_headers())

    assert response.status_code == http.HTTPStatus.BAD_REQUEST
    assert "byte limit" in response.get_json()["message"]
    assert written == []
    assert uploaded == []


@pytest.mark.ai_generated
def test_failed_upload_returns_502(client, captured, monkeypatch):
    def failing_upload(clip_paths, /):
        raise pozu_flask_app.UploadFailed("The upload to DANDI failed; please retry the request")

    monkeypatch.setattr(pozu_flask_app, "upload_clip_to_dandi", failing_upload)

    response = client.post(ENDPOINT, json=_clip_body(), headers=_auth_headers())

    assert response.status_code == http.HTTPStatus.BAD_GATEWAY
    assert "retry" in response.get_json()["message"]


@pytest.fixture
def staged_clip_paths(tmp_path, monkeypatch):
    """A clip plus sidecar sitting in a temp clips dandiset, ready for upload."""
    monkeypatch.setattr(pozu_flask_app, "CLIPS_DANDISET_ROOT", tmp_path / "000474")
    upload_dir = tmp_path / "000474" / "derivatives" / "incoming"
    upload_dir.mkdir(parents=True)
    clip_path = upload_dir / "test-clip.mp4"
    clip_path.write_bytes(FAKE_MP4)
    sidecar_path = upload_dir / "test-clip.mp4.json"
    sidecar_path.write_text("{}\n")
    return [clip_path, sidecar_path]


@pytest.mark.ai_generated
def test_upload_clip_to_dandi_deletes_local_copies_on_success(staged_clip_paths, monkeypatch):
    commands = []

    def fake_run(cmd, **kwargs):
        commands.append((cmd, kwargs.get("cwd")))
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(pozu_flask_app.subprocess, "run", fake_run)

    pozu_flask_app.upload_clip_to_dandi(staged_clip_paths)

    assert commands == [
        (
            [pozu_flask_app.DANDI_BIN, "upload", "--dandi-instance", pozu_flask_app.DANDI_INSTANCE],
            pozu_flask_app.CLIPS_DANDISET_ROOT,
        )
    ]
    # No local copies survive the upload.
    assert all(not path.exists() for path in staged_clip_paths)


@pytest.mark.ai_generated
@pytest.mark.parametrize(
    "fake_run",
    [
        pytest.param(
            lambda cmd, **kwargs: types.SimpleNamespace(returncode=1, stdout="", stderr="boom"), id="nonzero-rc"
        ),
        pytest.param(lambda cmd, **kwargs: (_ for _ in ()).throw(OSError("dandi missing")), id="oserror"),
    ],
)
def test_upload_clip_to_dandi_raises_and_cleans_up_on_failure(staged_clip_paths, monkeypatch, fake_run):
    monkeypatch.setattr(pozu_flask_app.subprocess, "run", fake_run)

    with pytest.raises(pozu_flask_app.UploadFailed):
        pozu_flask_app.upload_clip_to_dandi(staged_clip_paths)

    # There is no buffer: even on failure the local copies are deleted, and the
    # client (which still holds the original bytes) retries the request.
    assert all(not path.exists() for path in staged_clip_paths)


requires_ffprobe = pytest.mark.skipif(
    shutil.which("ffmpeg") is None
    or (shutil.which(pozu_flask_app.FFPROBE_BIN) is None and not pathlib.Path(pozu_flask_app.FFPROBE_BIN).exists()),
    reason="ffmpeg/ffprobe are not installed",
)


def _real_mp4_bytes(tmp_path, /):
    """Generate a genuine tiny MP4 with ffmpeg's synthetic test source."""
    source_path = tmp_path / "source.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=1:size=64x64:rate=4",
            "-pix_fmt",
            "yuv420p",
            str(source_path),
        ],
        check=True,
    )
    return source_path.read_bytes()


@pytest.mark.ai_generated
@requires_ffprobe
def test_write_clip_files_accepts_real_video(tmp_path, monkeypatch):
    monkeypatch.setattr(pozu_flask_app, "CLIPS_DANDISET_ROOT", tmp_path / "000474")
    mp4_blob = _real_mp4_bytes(tmp_path)
    record = {"submission_id": "abc123", "content_id": CONTENT_ID}

    clip_path, sidecar_path = pozu_flask_app.write_clip_files(
        mp4_blob=mp4_blob, record=record, clip_filename="real-clip.mp4"
    )

    upload_dir = tmp_path / "000474" / "derivatives" / "incoming"
    assert clip_path == upload_dir / "real-clip.mp4"
    assert clip_path.read_bytes() == mp4_blob
    assert json.loads(sidecar_path.read_text()) == record
    # Only the clip and its sidecar remain; no .part staging file or scratch bytes.
    assert sorted(entry.name for entry in upload_dir.iterdir()) == ["real-clip.mp4", "real-clip.mp4.json"]


@pytest.mark.ai_generated
@requires_ffprobe
def test_write_clip_files_rejects_ftyp_wearing_junk(tmp_path, monkeypatch):
    monkeypatch.setattr(pozu_flask_app, "CLIPS_DANDISET_ROOT", tmp_path / "000474")

    # Passes the magic-byte sniff but ffprobe finds no decodable video stream.
    with pytest.raises(pozu_flask_app.BadRequest):
        pozu_flask_app.write_clip_files(mp4_blob=FAKE_MP4, record={}, clip_filename="junk-clip.mp4")

    upload_dir = tmp_path / "000474" / "derivatives" / "incoming"
    assert list(upload_dir.iterdir()) == []
