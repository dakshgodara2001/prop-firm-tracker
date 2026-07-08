"""WSGI entry point for production servers.

    waitress-serve --host 0.0.0.0 --port 8050 wsgi:app
    gunicorn -w 2 -b 0.0.0.0:8050 wsgi:app
"""
from proptracker.web import create_app

app = create_app()
