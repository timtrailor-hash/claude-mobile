"""Terminal slice — ttyd auth + window management.

Extracted from conversation_server.py 2026-04-27.

Public surface re-exported for back-compat:
  _terminal_claude_cmd  — build the tmux send-keys command for `claude`
  _auth_session         — in-progress OAuth state (mutable dict)
  _cleanup_auth_session — kill auth process and reset state
  bp                    — Flask blueprint with the four /terminal-* routes
"""

from .auth import (  # noqa: F401
    _auth_session,
    _cleanup_auth_session,
    _terminal_claude_cmd,
)
from .routes import bp  # noqa: F401
