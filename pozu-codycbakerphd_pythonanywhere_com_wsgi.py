import os
import sys

project_home = "/home/CodyCBakerPhD/mysite/pozu-backend"
if project_home not in sys.path:
    sys.path = [project_home] + sys.path

# PythonAnywhere does NOT expose env vars set in a Bash console or a ".env" file
# to this web worker automatically. Load them here, before the app module is
# imported, so the module-level secret loading sees real values instead of
# falling back to an empty/placeholder client id. This is optional/defensive:
# pozu_flask_app also reads chmod-600 secret files under /home/CodyCBakerPhD, so
# the deployment works via either mechanism. Whichever you use, reload the web
# app from the PythonAnywhere Web tab afterwards -- env vars are not hot-reloaded.
_dotenv_path = "/home/CodyCBakerPhD/.env"
if os.path.exists(_dotenv_path):
    with open(_dotenv_path, encoding="utf-8") as _fh:
        for _raw in _fh:
            _line = _raw.strip()
            if not _line or _line.startswith("#") or "=" not in _line:
                continue
            _key, _, _value = _line.partition("=")
            # Do not clobber anything already present in the real environment.
            os.environ.setdefault(_key.strip(), _value.strip().strip("'\""))

# import flask app but need to call it "application" for WSGI to work
from pozu_flask_app import app as application  # noqa: E402
