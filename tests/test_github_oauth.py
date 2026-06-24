"""Tests for the GitHub OAuth web-application flow."""

import http
import unittest.mock
import urllib.parse

import jwt
import pytest

import pozu_flask_app

CLIENT_ID = "test-client-id"
CLIENT_SECRET = "test-client-secret"
APP_SECRET = "test-app-secret-at-least-32-bytes-long"


@pytest.fixture
def client(monkeypatch):
    """A Flask test client with deterministic OAuth credentials patched in."""
    monkeypatch.setattr(pozu_flask_app, "GITHUB_CLIENT_ID", CLIENT_ID)
    monkeypatch.setattr(pozu_flask_app, "GITHUB_CLIENT_SECRET", CLIENT_SECRET)
    monkeypatch.setattr(pozu_flask_app, "APP_SECRET_KEY", APP_SECRET)
    flask_app = pozu_flask_app.create_app()
    flask_app.config.update(TESTING=True)
    return flask_app.test_client()


def _mock_response(payload, /):
    """Build a stand-in for a ``requests`` response returning *payload* as JSON."""
    response = unittest.mock.Mock()
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    return response


@pytest.mark.ai_generated
def test_login_redirects_to_github_with_expected_params(client):
    response = client.get("/auth/github/login")

    assert response.status_code == http.HTTPStatus.FOUND
    location = response.headers["Location"]
    assert location.startswith(pozu_flask_app.GITHUB_AUTHORIZE_URL)

    query = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)
    assert query["client_id"] == [CLIENT_ID]
    assert query["redirect_uri"] == [pozu_flask_app.GITHUB_OAUTH_CALLBACK_URL]
    assert query["scope"] == [pozu_flask_app.GITHUB_OAUTH_SCOPES]
    assert query["state"]  # a non-empty CSRF state was generated


@pytest.mark.ai_generated
@pytest.mark.parametrize("client_id", ["", pozu_flask_app.PLACEHOLDER_CLIENT_ID])
def test_login_returns_400_when_client_id_unconfigured(client, monkeypatch, client_id):
    # Both an empty client id and the historical "<client id>" placeholder must be
    # rejected with a clean 400 rather than redirected to GitHub (which 404s there).
    monkeypatch.setattr(pozu_flask_app, "GITHUB_CLIENT_ID", client_id)

    response = client.get("/auth/github/login")

    assert response.status_code == http.HTTPStatus.BAD_REQUEST


@pytest.mark.ai_generated
@pytest.mark.parametrize("client_secret", ["", pozu_flask_app.PLACEHOLDER_CLIENT_SECRET])
def test_login_returns_400_when_client_secret_unconfigured(client, monkeypatch, client_secret):
    # A present client id but a missing/placeholder secret must also refuse at the
    # front door, rather than redirecting and then failing the token exchange.
    monkeypatch.setattr(pozu_flask_app, "GITHUB_CLIENT_SECRET", client_secret)

    response = client.get("/auth/github/login")

    assert response.status_code == http.HTTPStatus.BAD_REQUEST


@pytest.mark.ai_generated
def test_placeholder_constants_match_deployed_defaults():
    # The historical deployment's hard-coded fallbacks. If these drift, the import-
    # time normalisation and the route guard would stop catching the real defaults.
    assert pozu_flask_app.PLACEHOLDER_CLIENT_ID == "<client id>"
    assert pozu_flask_app.PLACEHOLDER_CLIENT_SECRET == "<client secret>"


@pytest.mark.ai_generated
def test_callback_happy_path_mints_jwt_and_redirects_to_frontend(client):
    with client.session_transaction() as session:
        session["oauth_state"] = "good-state"

    github_user = {"id": 4242, "login": "octocat", "name": "Mona", "avatar_url": "https://avatars/x.png"}
    token_response = _mock_response({"access_token": "gho_test_token"})
    user_response = _mock_response(github_user)

    with (
        unittest.mock.patch.object(pozu_flask_app.requests, "post", return_value=token_response) as mock_post,
        unittest.mock.patch.object(pozu_flask_app.requests, "get", return_value=user_response),
    ):
        response = client.get("/auth/github/callback?code=abc123&state=good-state")

    # The authorization code was exchanged using the configured secret.
    sent_data = mock_post.call_args.kwargs["data"]
    assert sent_data["code"] == "abc123"
    assert sent_data["client_secret"] == CLIENT_SECRET

    assert response.status_code == http.HTTPStatus.FOUND
    location = response.headers["Location"]
    assert location.startswith(f"{pozu_flask_app.FRONTEND_URL}#")

    fragment = urllib.parse.urlparse(location).fragment
    token = urllib.parse.parse_qs(fragment)["token"][0]
    claims = jwt.decode(token, APP_SECRET, algorithms=[pozu_flask_app.JWT_ALGORITHM])
    assert claims["sub"] == "4242"
    assert claims["login"] == "octocat"
    assert claims["iss"] == pozu_flask_app.JWT_ISSUER


@pytest.mark.ai_generated
@pytest.mark.parametrize(
    ("query", "session_state"),
    [
        pytest.param("code=abc&state=wrong", "good-state", id="state-mismatch"),
        pytest.param("code=abc", "good-state", id="state-missing-from-query"),
        pytest.param("code=abc&state=good-state", None, id="no-state-in-session"),
        pytest.param("state=good-state", "good-state", id="missing-code"),
        pytest.param("error=access_denied&state=good-state", "good-state", id="github-error"),
    ],
)
def test_callback_rejects_invalid_requests(client, query, session_state):
    if session_state is not None:
        with client.session_transaction() as session:
            session["oauth_state"] = session_state

    response = client.get(f"/auth/github/callback?{query}")

    assert response.status_code == http.HTTPStatus.BAD_REQUEST


@pytest.mark.ai_generated
def test_callback_rejects_when_github_returns_no_access_token(client):
    with client.session_transaction() as session:
        session["oauth_state"] = "good-state"

    with unittest.mock.patch.object(
        pozu_flask_app.requests, "post", return_value=_mock_response({"error": "bad_verification_code"})
    ):
        response = client.get("/auth/github/callback?code=abc&state=good-state")

    assert response.status_code == http.HTTPStatus.BAD_REQUEST
