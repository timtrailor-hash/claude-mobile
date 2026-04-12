"""Shared Flask app, auth middleware, config, and session state.

Every other conv/ module imports from here. This is the foundation —
do not add route handlers or business logic to this file.

Names preserve the monolith's internal conventions (_lock, _session, _log etc.)
so that code extracted from conversation_server.py needs zero renames.
"""

import os
import sys
import threading

# Add shared_utils to path
sys.path.insert(0, os.path.expanduser("~/code"))
from shared_utils import env_for_claude_cli, work_dir, configure_logging  # noqa: F401
from printer_registry import registry as _printer_registry  # noqa: F401

_log = configure_logging(
    "conversation_server",
    log_file="/tmp/conversation_server_structured.log",
)

# Auth token — imported from credentials.py (gitignored), fallback to None
try:
    from credentials import AUTH_TOKEN  # noqa: F401
except ImportError:
    AUTH_TOKEN = None

from flask import Flask, Response, jsonify, request  # noqa: F401
from flask_sock import Sock

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50 MB upload limit
app.config['SOCK_SERVER_OPTIONS'] = {'ping_interval': 25}
sock = Sock(app)


@app.before_request
def check_auth():
    """Require auth token for non-localhost/Tailscale requests."""
    if not AUTH_TOKEN:
        return
    if request.remote_addr in ('127.0.0.1', '::1'):
        return
    if request.remote_addr and request.remote_addr.startswith('100.'):
        return
    auth = request.headers.get('Authorization', '')
    if auth == f'Bearer {AUTH_TOKEN}':
        return
    if request.args.get('token') == AUTH_TOKEN:
        return
    return jsonify({"error": "Unauthorized"}), 401


# ── Config ──
WORK_DIR = work_dir()
SESSIONS_DIR = os.path.join(
    os.path.expanduser("~/.claude/projects/-Users-timtrailor-code"),
    "phone_sessions",
)
_TMP_LOG_DIR = "/tmp/claude_sessions"
os.makedirs(SESSIONS_DIR, exist_ok=True)
os.makedirs(_TMP_LOG_DIR, exist_ok=True)

# MCP config — one level up from conv/ to find mcp-approval.json
MCP_CONFIG = os.path.join(
    os.path.dirname(os.path.realpath(__file__)),
    os.pardir,
    "mcp-approval.json",
)

# ── Session state (single active conversation) ──
_lock = threading.Lock()
_session = {
    "id": None,
    "claude_sid": None,
    "proc": None,
    "stdin_pipe": None,
    "pid": None,
    "events_file": None,
    "event_count": 0,
    "status": "idle",
    "started": None,
    "last_activity": None,
    "reader_running": False,
    "generation": 0,
}

_last_response = {"text": "", "cost": "", "session_id": None, "seq": 0}
_last_response_lock = threading.Lock()

# Condition variable — signalled every time an event is appended.
_event_condition = threading.Condition()
