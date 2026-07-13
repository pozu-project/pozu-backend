"""Tests for the no-subject endpoint (POST /api/v1/annotations/no-subject)."""

import http

import pytest

import pozu_flask_app

APP_SECRET = "test-app-secret-at-least-32-bytes-long"

CONTENT_ID = next(iter(pozu_flask_app.CONTENT_ID_TO_DANDI_PATH))
VIDEO_URL = f"https://example.org/videos/{CONTENT_ID}"

ENDPOINT = "/api/v1/annotations/no-subject"


@pytest.fixture
def captured(monkeypatch):
    """Capture buffered records instead of writing to disk."""
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


def _body(**overrides):
    body = {
        "video_url": VIDEO_URL,
        "frame_index": 7,
    }
    body.update(overrides)
    return body


def _auth_headers():
    token = pozu_flask_app.mint_app_token(
        github_user={"id": 4242, "login": "octocat", "name": "Mona"}, roles=[], permissions=[]
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.ai_generated
def test_rejects_unauthenticated_request(client):
    response = client.post(ENDPOINT, json=_body())

    assert response.status_code == http.HTTPStatus.UNAUTHORIZED
    assert "message" in response.get_json()


@pytest.mark.ai_generated
def test_accepts_and_stamps_no_subject(client, captured):
    response = client.post(ENDPOINT, json=_body(), headers=_auth_headers())

    assert response.status_code == http.HTTPStatus.ACCEPTED
    payload = response.get_json()
    assert payload["push_status"] == "queued"
    assert payload["content_id"] == CONTENT_ID
    assert payload["submission_id"]

    record, buffer_dir = captured[0]
    assert record["no_subject"] is True
    assert record["content_id"] == CONTENT_ID
    assert record["submitted_by"] == "octocat"
    # Real annotation data: buffers inside its own dandiset for the DANDI upload.
    assert buffer_dir == pozu_flask_app.NO_SUBJECT_DANDISET_ROOT / "derivatives" / "buffer"


@pytest.mark.ai_generated
def test_unknown_content_id_is_rejected(client, captured):
    body = _body(video_url="https://example.org/videos/not-a-real-id")
    response = client.post(ENDPOINT, json=body, headers=_auth_headers())

    assert response.status_code == http.HTTPStatus.BAD_REQUEST
    assert "content_id" in response.get_json()["message"]
    assert captured == []
