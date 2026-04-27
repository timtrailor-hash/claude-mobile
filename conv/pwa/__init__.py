"""PWA static files slice — extracted from conversation_server.py 2026-04-27.

Public surface:
  bp — Flask blueprint with /app, /manifest.json, /sw.js, /offline,
       /icon-192.png, /icon-512.png.
"""

from .routes import bp  # noqa: F401
