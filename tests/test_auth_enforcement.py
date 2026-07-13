"""Tests for JWT enforcement on the annotation endpoints."""

import datetime
import http

import jwt
import pytest

import pozu_flask_app

CLIENT_ID = "test-client-id"
CLIENT_SECRET = "test-client-secret"
APP_SECRET = "test-app-secret-at-least-32-bytes-long"

# Any key in CONTENT_ID_TO_DANDI_PATH works; its last path segment is the content_id.
CONTENT_ID = next(iter(pozu_flask_app.CONTENT_ID_TO_DANDI_PATH))
VIDEO_URL = f"https://example.org/videos/{CONTENT_ID}"

ENDPOINTS = [
    "/api/v1/annotations/bbox",
    "/api/v1/annotations/labels",
    "/api/v1/annotations/no-subject",
]

ALLOWED_ORIGIN = "https://pozu-project.github.io"


@pytest.fixture
def client(monkeypatch):
    """A Flask test client with deterministic credentials and a no-op buffer write."""
    monkeypatch.setattr(pozu_flask_app, "GITHUB_CLIENT_ID", CLIENT_ID)
    monkeypatch.setattr(pozu_flask_app, "GITHUB_CLIENT_SECRET", CLIENT_SECRET)
    monkeypatch.setattr(pozu_flask_app, "APP_SECRET_KEY", APP_SECRET)
    monkeypatch.setattr(pozu_flask_app, "append_to_hourly_jsonl", lambda record, buffer_dir: None)
    flask_app = pozu_flask_app.create_app()
    flask_app.config.update(TESTING=True)
    return flask_app.test_client()


def _annotation_body():
    """A minimally valid annotation payload accepted by both endpoints."""
    return {
        "video_url": VIDEO_URL,
        "frame_index": 0,
        "total_frames": 1,
        "fps": 30.0,
        "frame_width": 640,
        "frame_height": 480,
        "timestamp": "2026-06-24T00:00:00Z",
        "box": {"x": 1.0, "y": 2.0, "width": 3.0, "height": 4.0},
        "labels": [],
    }


def _valid_token():
    """Mint a valid, unexpired app token for a fake GitHub user."""
    return pozu_flask_app.mint_app_token(
        github_user={"id": 4242, "login": "octocat", "name": "Mona"}, roles=[], permissions=[]
    )


def _expired_token():
    """Craft a structurally valid token whose ``exp`` is already in the past."""
    now = datetime.datetime.now(tz=datetime.timezone.utc)
    payload = {
        "iss": pozu_flask_app.JWT_ISSUER,
        "sub": "4242",
        "login": "octocat",
        "iat": now - datetime.timedelta(hours=2),
        "exp": now - datetime.timedelta(hours=1),
    }
    return jwt.encode(payload, APP_SECRET, algorithm=pozu_flask_app.JWT_ALGORITHM)


def _bad_signature_token():
    """A structurally valid token signed with the wrong secret, so verification fails."""
    now = datetime.datetime.now(tz=datetime.timezone.utc)
    payload = {
        "iss": pozu_flask_app.JWT_ISSUER,
        "sub": "4242",
        "iat": now,
        "exp": now + datetime.timedelta(hours=1),
    }
    return jwt.encode(payload, "the-wrong-secret", algorithm=pozu_flask_app.JWT_ALGORITHM)


@pytest.mark.ai_generated
@pytest.mark.parametrize("endpoint", ENDPOINTS)
@pytest.mark.parametrize(
    ("headers_factory", "reason"),
    [
        pytest.param(lambda: {}, "no-header", id="no-header"),
        pytest.param(lambda: {"Authorization": "token abc"}, "malformed-header", id="malformed-header"),
        pytest.param(
            lambda: {"Authorization": f"Bearer {_bad_signature_token()}"},
            "invalid-signature",
            id="invalid-signature",
        ),
        pytest.param(
            lambda: {"Authorization": f"Bearer {_expired_token()}"},
            "expired-token",
            id="expired-token",
        ),
    ],
)
def test_post_rejects_unauthenticated_requests(client, endpoint, headers_factory, reason):
    response = client.post(endpoint, json=_annotation_body(), headers=headers_factory())

    assert response.status_code == http.HTTPStatus.UNAUTHORIZED
    assert "message" in response.get_json()


@pytest.mark.ai_generated
@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_unauthorized_response_carries_cors_header(client, endpoint):
    # Without the CORS header on the 401 the browser masks it as an opaque
    # network error, so the SPA never sees the status and cannot prompt a re-login.
    response = client.post(endpoint, json=_annotation_body(), headers={"Origin": ALLOWED_ORIGIN})

    assert response.status_code == http.HTTPStatus.UNAUTHORIZED
    assert response.headers.get("Access-Control-Allow-Origin") == ALLOWED_ORIGIN


@pytest.mark.ai_generated
@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_post_accepts_valid_token(client, endpoint):
    headers = {"Authorization": f"Bearer {_valid_token()}"}

    response = client.post(endpoint, json=_annotation_body(), headers=headers)

    assert response.status_code == http.HTTPStatus.ACCEPTED
    assert response.get_json()["push_status"] == "queued"
