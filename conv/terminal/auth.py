"""Terminal auth state and command-builder helpers.

Module-level state:
  _auth_session — mutable dict holding the in-progress `claude auth login`
                  PTY session. Routes mutate this; _cleanup_auth_session
                  resets it.

Functions:
  _terminal_claude_cmd  — build the tmux send-keys command that launches
                          `claude` with subscription auth (strips API key
                          and OAuth token to prevent billing).
  _cleanup_auth_session — kill auth process, close PTY, reset state.
"""

from __future__ import annotations

import os


# In-progress claude auth login PTY session. Mutated by routes.
_auth_session: dict = {
    "process": None,
    "master_fd": None,
    "session_id": None,
    "timer": None,
}


def _terminal_claude_cmd() -> str:
    """Build the tmux send-keys command to launch claude with subscription auth.

    Uses keychain auth (from `claude auth login` OAuth flow).
    CRITICAL: strips ANTHROPIC_API_KEY and CLAUDE_CODE_OAUTH_TOKEN to prevent
    API billing — Claude Team subscription only.
    """
    try:
        from credentials import KEYCHAIN_PASSWORD

        unlock = (
            f"security unlock-keychain -p {KEYCHAIN_PASSWORD} "
            "~/Library/Keychains/login.keychain-db"
        )
    except (ImportError, AttributeError):
        unlock = "true"  # no-op if no password
    return (
        f"{unlock} && cd ~/Documents/Claude\\\\ code"
        " && export PATH=$HOME/.local/bin:/opt/homebrew/bin:$PATH"
        " && unset ANTHROPIC_API_KEY && unset CLAUDE_CODE_OAUTH_TOKEN && claude"
    )


def _cleanup_auth_session() -> None:
    """Kill the in-progress auth process and reset state."""
    proc = _auth_session.get("process")
    if proc and proc.poll() is None:
        try:
            proc.kill()
        except Exception:
            pass
    fd = _auth_session.get("master_fd")
    if fd is not None:
        try:
            os.close(fd)
        except Exception:
            pass
    _auth_session.update(
        {"process": None, "master_fd": None, "session_id": None, "timer": None}
    )
