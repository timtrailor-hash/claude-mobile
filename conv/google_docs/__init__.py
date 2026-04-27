"""Google Docs export slice — extracted from conversation_server.py 2026-04-27.

Public surface:
  - bp: Flask blueprint with /terminal-to-doc, /google-docs-auth-start,
    /google-docs-auth-complete routes.

Re-exported for backward compat with code that historically imported these
names from conversation_server.py:
  - GOOGLE_TOKEN_FILE, GOOGLE_ALL_SCOPES, _google_creds, _google_auth_flow
"""

from .auth import (  # noqa: F401
    GOOGLE_TOKEN_FILE,
    GOOGLE_ALL_SCOPES,
    _google_creds,
    _google_auth_flow,
)
from .routes import bp  # noqa: F401
