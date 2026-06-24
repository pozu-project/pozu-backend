"""Tests for the frame-report endpoint (POST /api/v1/reports)."""

import http

import pytest

import pozu_flask_app

APP_SECRET = "test-app-secret-at-least-32-bytes-long"

# Any key in CONTENT_ID_TO_DANDI_PATH works; its last path segment is the content_id.
CONTENT_ID = next(iter(pozu_flask_app.CONTENT_ID_TO_DANDI_PATH))
VIDEO_URL = f"https://example.org/videos/{CONTENT_ID}"

ENDPOINT = "/api/v1/reports"
ALLOWED_ORIGIN = "https://pozu-project.github.io"


@pytest.fixture
def captured(monkeypatch):
    """Capture the records the endpoint would buffer, instead of touching disk."""
    records = []
    monkeypatch.setattr(pozu_flask_app, "APP_SECRET_KEY", APP_SECRET)
    monkeypatch.setattr(
        pozu_flask_app,
        "append_to_hourly_jsonl",
        lambda record, buffer_dir: records.append((record, buffer_dir)),
    )
    return records


@pytest.fixture
def client(captured):
    flask_app = pozu_flask_app.create_app()
    flask_app.config.update(TESTING=True)
    return flask_app.test_client()


def _report_body(**overrides):
    """A minimally valid ReportedFrame payload."""
    body = {
        "video_url": VIDEO_URL,
        "frame_index": 7,
        "total_frames": 100,
        "fps": 30.0,
        "frame_width": 640,
        "frame_height": 480,
        "timestamp": "2026-06-24T00:00:00Z",
        "reason": "inappropriate_content",
    }
    body.update(overrides)
    return body


def _auth_headers():
    token = pozu_flask_app.mint_app_token({"id": 4242, "login": "octocat", "name": "Mona"})
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.ai_generated
def test_rejects_unauthenticated_request(client):
    response = client.post(ENDPOINT, json=_report_body())

    assert response.status_code == http.HTTPStatus.UNAUTHORIZED
    assert "message" in response.get_json()


@pytest.mark.ai_generated
def test_accepts_valid_report(client, captured):
    response = client.post(ENDPOINT, json=_report_body(), headers=_auth_headers())

    assert response.status_code == http.HTTPStatus.ACCEPTED
    payload = response.get_json()
    assert payload["push_status"] == "queued"
    assert payload["content_id"] == CONTENT_ID
    assert payload["submission_id"]

    assert len(captured) == 1
    record, buffer_dir = captured[0]
    assert record["content_id"] == CONTENT_ID
    assert record["reason"] == "inappropriate_content"
    assert record["submitted_by"] == "octocat"
    # Reports buffer inside their reserved dandiset for the hourly DANDI upload.
    assert buffer_dir == pozu_flask_app.REPORTS_DANDISET_ROOT / "derivatives" / "buffer"


@pytest.mark.ai_generated
def test_other_reason_requires_details(client, captured):
    response = client.post(ENDPOINT, json=_report_body(reason="other"), headers=_auth_headers())

    assert response.status_code == http.HTTPStatus.BAD_REQUEST
    assert "details" in response.get_json()["message"]
    assert captured == []


@pytest.mark.ai_generated
def test_other_reason_with_details_is_accepted(client, captured):
    body = _report_body(reason="other", details="Shows a human face")
    response = client.post(ENDPOINT, json=body, headers=_auth_headers())

    assert response.status_code == http.HTTPStatus.ACCEPTED
    record, _ = captured[0]
    assert record["reason"] == "other"
    assert record["details"] == "Shows a human face"


@pytest.mark.ai_generated
def test_missing_reason_is_rejected(client, captured):
    body = _report_body()
    del body["reason"]
    response = client.post(ENDPOINT, json=body, headers=_auth_headers())

    assert response.status_code == http.HTTPStatus.BAD_REQUEST
    assert "reason" in response.get_json()["message"]
    assert captured == []


@pytest.mark.ai_generated
def test_unknown_content_id_is_rejected(client, captured):
    body = _report_body(video_url="https://example.org/videos/not-a-real-id")
    response = client.post(ENDPOINT, json=body, headers=_auth_headers())

    assert response.status_code == http.HTTPStatus.BAD_REQUEST
    assert "content_id" in response.get_json()["message"]
    assert captured == []


@pytest.mark.ai_generated
def test_unauthorized_response_carries_cors_header(client):
    response = client.post(ENDPOINT, json=_report_body(), headers={"Origin": ALLOWED_ORIGIN})

    assert response.status_code == http.HTTPStatus.UNAUTHORIZED
    assert response.headers.get("Access-Control-Allow-Origin") == ALLOWED_ORIGIN
