"""Push-notification slice — APNs over HTTP/2 + JWT.

Extracted from conversation_server.py 2026-04-27.

Public surface re-exported for the 4 monolith callers:
  _send_push_notification — send an APNs push to a specific bundle
  _summarise_for_push     — Haiku-backed tail summariser for push bodies
"""

from .notify import _send_push_notification, _summarise_for_push  # noqa: F401
