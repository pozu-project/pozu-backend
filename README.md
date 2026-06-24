<p align="center">
  <img alt="Pozu logo" src="https://raw.githubusercontent.com/CodyCBakerPhD/pozu-branding/main/v1/name-pozu+logo_clipped.svg" width="320">
</p>

# Pozu (Backend)

Backend Flask server for the Pozu web app.


### Setup

Backend is currently deployed using [PythonAnywhere by Anaconda](https://www.pythonanywhere.com/).

Swagger: https://pozu-codycbakerphd.pythonanywhere.com/api/v1/docs

The tracked file `pozu-codycbakerphd_pythonanywhere_com_wsgi.py` is placed at `/var/www/pozu-codycbakerphd_pythonanywhere_com_wsgi.py` until a custom domain is set up.

The main Flask app is imported from `/home/CodyCBakerPhD/mysite/pozu-backend/pozu_flask_app.py`.

The virtual environment is configured on the app to read from `/home/CodyCBakerPhD/.virtualenvs/pozu`, though the app also had to set some `PATH` values correspondingly to get `subprocess` to work correctly.

That environment was set up using:

```
mkvirtualenv --python=/usr/bin/python3.11 pozu

pip install --upgrade pip
pip install flask flask-restx flask-cors requests filelock dandi pyjwt
```

from a bash console.


### GitHub OAuth

The backend supports signing in with GitHub using the web application (authorization code) flow.

Register an OAuth app at https://github.com/settings/developers with this Authorization callback URL.

```
https://pozu-codycbakerphd.pythonanywhere.com/auth/github/callback
```

The flow uses two top-level routes. `GET /auth/github/login` redirects the browser to GitHub, and `GET /auth/github/callback` completes the handshake. The callback exchanges the code for a GitHub token, reads the user profile, then mints a short-lived signed JWT. It redirects back to the frontend with that token in the URL fragment. The frontend stores the token and replays it as an `Authorization: Bearer <token>` header on later API calls. A cross-site session cookie is deliberately avoided because the frontend (GitHub Pages) and backend (PythonAnywhere) are on different sites, where third-party cookies are unreliable.

Secrets are read from an environment variable first, then from a chmod-600 file, matching the existing `dandi_token` pattern. Create these files on the deployment.

```
/home/CodyCBakerPhD/github_oauth_client_id
/home/CodyCBakerPhD/github_oauth_client_secret
/home/CodyCBakerPhD/app_secret_key
```

`app_secret_key` signs both the OAuth `state` cookie and the JWT. Generate at least 32 random bytes for it, for example with `python -c "import secrets; print(secrets.token_urlsafe(48))"`. After creating or changing any of these files, reload the web app from the PythonAnywhere **Web** tab.

> **PythonAnywhere env vars.** Variables exported in a Bash console or written to a `.env` file are **not** visible to the web worker on their own. Either use the chmod-600 secret files above (the worker reads them directly), or have the WSGI file load them — `pozu-codycbakerphd_pythonanywhere_com_wsgi.py` reads `/home/CodyCBakerPhD/.env` before importing the app. Either way the env var/file change only takes effect after a **Reload** from the Web tab; PythonAnywhere does not hot-reload them.

At startup the app logs whether the GitHub OAuth client id and signing secrets are present (it never logs the values). If the client id is missing or still the literal `<client id>` placeholder, it logs a warning and `GET /auth/github/login` returns a `400` instead of redirecting to GitHub with a bad `client_id` — so a misconfiguration fails loudly in the server log rather than 404-ing silently at GitHub.
