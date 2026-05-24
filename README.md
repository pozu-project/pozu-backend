# Pose Zoo (Backend)

Backend Flask server for the Pose Zoo web app.


### Setup

Backend is currently deployed using [PythonAnywhere by Anaconda].

Swagger: https://pose-zoo-codycbakerphd.pythonanywhere.com/api/v1/docs

The tracked file `pose-zoo-codycbakerphd_pythonanywhere_com_wsgi.py` is placed at `/var/www/pose-zoo-codycbakerphd_pythonanywhere_com_wsgi.py` until a custom domain is setup.

The main Flask app is imported from `/home/CodyCBakerPhD/mysite/pose_zoo_flask_app.py`.

The virtual environment is configured on the app to read from `/home/CodyCBakerPhD/.virtualenvs/pose-zoo`, though the app also had to set some `PATH` values correspondingly to get `subprocess` to work correctly.

That environment was setup using:

```
mkvirtualenv --python=/usr/bin/python3.11 pose-zoo

pip install --upgrade pip
pip install flask flask-restx requests filelock dandi
```

from a bash console.
