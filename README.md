# Pose Zoo (Backend)

Backend Flask server for the Pose Zoo web app.


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
pip install flask flask-restx flask-cors requests filelock dandi
```

from a bash console.
