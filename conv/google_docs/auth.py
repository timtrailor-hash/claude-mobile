"""Google OAuth credentials and scope handling.

Module-level state:
  GOOGLE_TOKEN_FILE   — path to google_token.json. Env-overridable via
                        CONV_GOOGLE_TOKEN_PATH (slice 1b).
  GOOGLE_ALL_SCOPES   — canonical OAuth scopes list. Imported from
                        credentials.py if available, otherwise hardcoded.
  _google_auth_flow   — in-progress OAuth flow state (mutated by routes).

NEVER writes the token file from this module. token_refresh.py owns that
on the Mac Mini (separate LaunchAgent). The /google-docs-auth-complete
route does write the token, but only after a fresh user authorisation —
not refresh.
"""

from __future__ import annotations

import os

from conv.app import _log

GOOGLE_TOKEN_FILE = os.environ.get("CONV_GOOGLE_TOKEN_PATH") or os.path.join(
    os.path.dirname(os.path.realpath(__file__)),
    os.pardir,
    os.pardir,
    "google_token.json",
)

# Import canonical scopes from credentials.py — single source of truth.
# NEVER define scopes locally; NEVER write token back (token_refresh.py owns that).
try:
    from credentials import GOOGLE_OAUTH_SCOPES as GOOGLE_ALL_SCOPES
except ImportError:
    GOOGLE_ALL_SCOPES = [
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/calendar.readonly",
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/documents",
    ]


# In-progress OAuth flow state. Routes mutate this dict.
_google_auth_flow: dict = {"flow": None, "state": None}


def _google_creds():
    """Load Google OAuth credentials, refreshing if needed.

    NEVER saves token back. token_refresh.py owns persistence.
    Returns: google.oauth2.credentials.Credentials | None
    """
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request

        if not os.path.exists(GOOGLE_TOKEN_FILE):
            return None
        creds = Credentials.from_authorized_user_file(
            GOOGLE_TOKEN_FILE, GOOGLE_ALL_SCOPES
        )
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        if creds and creds.valid:
            return creds
    except Exception as e:
        _log.warning("Google creds failed: %s", e)
    return None
