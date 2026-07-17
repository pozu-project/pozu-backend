"""Tests for the video clip endpoint (POST /api/v1/clips)."""

import base64
import http
import pathlib
import shutil
import struct
import zlib

import pytest

import pozu_flask_app

APP_SECRET = "test-app-secret-at-least-32-bytes-long"

# Any key in CONTENT_ID_TO_DANDI_PATH works; its last path segment is the content_id.
CONTENT_ID = next(iter(pozu_flask_app.CONTENT_ID_TO_DANDI_PATH))
VIDEO_URL = f"https://example.org/videos/{CONTENT_ID}"

ENDPOINT = "/api/v1/clips"
ALLOWED_ORIGIN = "https://pozu-project.github.io"

# Magic-byte-correct stand-ins; the ffmpeg step is monkeypatched in these tests,
# so only the format sniffing needs to see plausible image bytes.
FAKE_PNG = pozu_flask_app.PNG_MAGIC + b"\x00" * 16
FAKE_JPEG = pozu_flask_app.JPEG_MAGIC + b"\xe0" + b"\x00" * 16


def _b64(blob, /):
    return base64.b64encode(blob).decode("ascii")


def _png_bytes(*, width=8, height=8):
    """Build a real, minimal RGB PNG so ffmpeg integration tests need no pillow."""

    def chunk(tag, data):
        payload = tag + data
        return struct.pack(">I", len(data)) + payload + struct.pack(">I", zlib.crc32(payload))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\x7f\x20\xd0" * width for _ in range(height))
    return (
        pozu_flask_app.PNG_MAGIC
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


@pytest.fixture
def captured(monkeypatch):
    """Capture buffered records and encoded clips, instead of touching disk or ffmpeg."""
    records = []
    clips = []
    monkeypatch.setattr(pozu_flask_app, "APP_SECRET_KEY", APP_SECRET)
    monkeypatch.setattr(
        pozu_flask_app,
        "append_to_hourly_jsonl",
        lambda record, buffer_dir: records.append((record, buffer_dir)),
    )

    def fake_write_clip_mp4(**kwargs):
        clips.append(kwargs)
        return pathlib.Path("clips") / kwargs["clip_filename"]

    monkeypatch.setattr(pozu_flask_app, "write_clip_mp4", fake_write_clip_mp4)
    return records, clips


@pytest.fixture
def client(captured):
    flask_app = pozu_flask_app.create_app()
    flask_app.config.update(TESTING=True)
    return flask_app.test_client()


def _clip_body(**overrides):
    """A minimally valid VideoClip payload."""
    body = {
        "video_url": VIDEO_URL,
        "frames": [_b64(FAKE_PNG)] * 3,
        "fps": 12.5,
    }
    body.update(overrides)
    return body


def _auth_headers():
    token = pozu_flask_app.mint_app_token({"id": 4242, "login": "octocat", "name": "Mona"})
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.ai_generated
def test_rejects_unauthenticated_request(client):
    response = client.post(ENDPOINT, json=_clip_body())

    assert response.status_code == http.HTTPStatus.UNAUTHORIZED
    assert "message" in response.get_json()


@pytest.mark.ai_generated
def test_unauthorized_response_carries_cors_header(client):
    response = client.post(ENDPOINT, json=_clip_body(), headers={"Origin": ALLOWED_ORIGIN})

    assert response.status_code == http.HTTPStatus.UNAUTHORIZED
    assert response.headers.get("Access-Control-Allow-Origin") == ALLOWED_ORIGIN


@pytest.mark.ai_generated
def test_accepts_valid_clip(client, captured):
    records, clips = captured
    response = client.post(ENDPOINT, json=_clip_body(), headers=_auth_headers())

    assert response.status_code == http.HTTPStatus.ACCEPTED
    payload = response.get_json()
    assert payload["push_status"] == "queued"
    assert payload["content_id"] == CONTENT_ID
    assert payload["frame_count"] == 3
    assert payload["clip_file"].endswith(".mp4")
    assert payload["submission_id"]

    assert len(clips) == 1
    encoded = clips[0]
    assert encoded["frame_blobs"] == [FAKE_PNG] * 3
    assert encoded["frame_extension"] == "png"
    assert encoded["fps"] == 12.5
    assert encoded["codec"] == pozu_flask_app.CLIP_DEFAULT_CODEC
    assert encoded["crf"] == pozu_flask_app.CLIP_DEFAULT_CRF
    assert encoded["scale_filter"] == pozu_flask_app.CLIP_DEFAULT_SCALE_FILTER

    assert len(records) == 1
    record, buffer_dir = records[0]
    assert record["content_id"] == CONTENT_ID
    assert record["submitted_by"] == "octocat"
    assert record["clip_file"] == payload["clip_file"]
    assert record["frame_count"] == 3
    # Clip provenance buffers inside the reserved dandiset for the hourly upload.
    assert buffer_dir == pozu_flask_app.CLIPS_DANDISET_ROOT / "derivatives" / "buffer"


@pytest.mark.ai_generated
@pytest.mark.parametrize(
    ("frames", "expected_extension"),
    [
        pytest.param([f"data:image/png;base64,{_b64(FAKE_PNG)}"] * 2, "png", id="data-url-png"),
        pytest.param([_b64(FAKE_JPEG)] * 2, "jpg", id="bare-jpeg"),
    ],
)
def test_accepts_alternate_frame_encodings(client, captured, frames, expected_extension):
    _, clips = captured
    response = client.post(ENDPOINT, json=_clip_body(frames=frames), headers=_auth_headers())

    assert response.status_code == http.HTTPStatus.ACCEPTED
    assert clips[0]["frame_extension"] == expected_extension


@pytest.mark.ai_generated
def test_explicit_dimensions_produce_scale_filter(client, captured):
    _, clips = captured
    body = _clip_body(width=640, height=480, codec="libx265", crf=30)
    response = client.post(ENDPOINT, json=body, headers=_auth_headers())

    assert response.status_code == http.HTTPStatus.ACCEPTED
    encoded = clips[0]
    assert encoded["scale_filter"] == "scale=640:480"
    assert encoded["codec"] == "libx265"
    assert encoded["crf"] == 30


def _delete_key(key, /):
    def mutate(body):
        del body[key]

    return mutate


def _set(**overrides):
    def mutate(body):
        body.update(overrides)

    return mutate


@pytest.mark.ai_generated
@pytest.mark.parametrize(
    ("mutate", "message_snippet"),
    [
        pytest.param(_delete_key("frames"), "frames", id="missing-frames"),
        pytest.param(_set(frames="not-a-list"), "frames", id="frames-not-a-list"),
        pytest.param(_set(frames=[]), "frames", id="empty-frames"),
        pytest.param(
            _set(frames=[_b64(FAKE_PNG)] * (pozu_flask_app.MAX_CLIP_FRAMES + 1)),
            "at most",
            id="too-many-frames",
        ),
        pytest.param(_set(frames=["!!! not base64 !!!"]), "base64", id="invalid-base64"),
        pytest.param(_set(frames=[_b64(b"GIF89a" + b"\x00" * 16)]), "PNG or JPEG", id="not-an-image"),
        pytest.param(_set(frames=[_b64(FAKE_PNG), _b64(FAKE_JPEG)]), "same image format", id="mixed-formats"),
        pytest.param(_delete_key("fps"), "fps", id="missing-fps"),
        pytest.param(_set(fps="fast"), "fps", id="fps-not-a-number"),
        pytest.param(_set(fps=0), "fps", id="fps-zero"),
        pytest.param(_set(fps=500), "fps", id="fps-too-high"),
        pytest.param(_set(codec="h264_nvenc"), "codec", id="codec-not-allowlisted"),
        pytest.param(_set(crf=99), "crf", id="crf-out-of-range"),
        pytest.param(_set(crf="high"), "crf", id="crf-not-an-integer"),
        pytest.param(_set(width=640), "together", id="width-without-height"),
        pytest.param(_set(width=641, height=480), "even", id="odd-width"),
        pytest.param(_set(width=640, height=99999), "between", id="height-too-large"),
        pytest.param(
            _set(video_url="https://example.org/videos/not-a-real-id"),
            "content_id",
            id="unknown-content-id",
        ),
    ],
)
def test_invalid_payloads_are_rejected(client, captured, mutate, message_snippet):
    records, clips = captured
    body = _clip_body()
    mutate(body)

    response = client.post(ENDPOINT, json=body, headers=_auth_headers())

    assert response.status_code == http.HTTPStatus.BAD_REQUEST
    assert message_snippet in response.get_json()["message"]
    assert records == []
    assert clips == []


@pytest.mark.ai_generated
def test_oversized_frame_is_rejected(client, captured, monkeypatch):
    records, clips = captured
    monkeypatch.setattr(pozu_flask_app, "MAX_CLIP_FRAME_BYTES", 8)

    response = client.post(ENDPOINT, json=_clip_body(), headers=_auth_headers())

    assert response.status_code == http.HTTPStatus.BAD_REQUEST
    assert "per-frame limit" in response.get_json()["message"]
    assert records == []
    assert clips == []


requires_ffmpeg = pytest.mark.skipif(
    shutil.which(pozu_flask_app.FFMPEG_BIN) is None and not pathlib.Path(pozu_flask_app.FFMPEG_BIN).exists(),
    reason="ffmpeg is not installed",
)


@pytest.mark.ai_generated
@requires_ffmpeg
def test_write_clip_mp4_encodes_and_cleans_up(tmp_path, monkeypatch):
    monkeypatch.setattr(pozu_flask_app, "CLIPS_DANDISET_ROOT", tmp_path / "000474")

    clip_path = pozu_flask_app.write_clip_mp4(
        frame_blobs=[_png_bytes() for _ in range(4)],
        frame_extension="png",
        fps=4.0,
        codec="libx264",
        crf=23,
        scale_filter=pozu_flask_app.CLIP_DEFAULT_SCALE_FILTER,
        clip_filename="test-clip.mp4",
    )

    buffer_dir = tmp_path / "000474" / "derivatives" / "buffer"
    assert clip_path == buffer_dir / "test-clip.mp4"
    assert clip_path.stat().st_size > 0
    # Frames and the .part staging file are cleaned up; only the final MP4 remains.
    assert sorted(entry.name for entry in buffer_dir.iterdir()) == ["test-clip.mp4"]


@pytest.mark.ai_generated
@requires_ffmpeg
def test_write_clip_mp4_rejects_corrupt_frames(tmp_path, monkeypatch):
    monkeypatch.setattr(pozu_flask_app, "CLIPS_DANDISET_ROOT", tmp_path / "000474")

    with pytest.raises(pozu_flask_app.BadRequest):
        pozu_flask_app.write_clip_mp4(
            frame_blobs=[pozu_flask_app.PNG_MAGIC + b"\x00" * 16],
            frame_extension="png",
            fps=4.0,
            codec="libx264",
            crf=23,
            scale_filter=pozu_flask_app.CLIP_DEFAULT_SCALE_FILTER,
            clip_filename="corrupt-clip.mp4",
        )

    buffer_dir = tmp_path / "000474" / "derivatives" / "buffer"
    # Nothing (not even a .part file) is left behind after a failed encode.
    assert list(buffer_dir.iterdir()) == []
