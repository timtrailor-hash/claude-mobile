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
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB upload limit
app.config["SOCK_SERVER_OPTIONS"] = {"ping_interval": 25}
sock = Sock(app)


@app.before_request
def check_auth():
    """Require auth token for non-localhost/Tailscale requests."""
    if not AUTH_TOKEN:
        return
    if request.remote_addr in ("127.0.0.1", "::1"):
        return
    if request.remote_addr and request.remote_addr.startswith("100."):
        return
    auth = request.headers.get("Authorization", "")
    if auth == f"Bearer {AUTH_TOKEN}":
        return
    if request.args.get("token") == AUTH_TOKEN:
        return
    return jsonify({"error": "Unauthorized"}), 401


# ── Mode + Port (staging support, plan §5.3) ──
# All staging flags default to production-equivalent behaviour. A daemon
# launched with no CONV_* env vars behaves exactly as it did pre-slice-1b.
CONV_MODE = os.environ.get("CONV_MODE", "prod").lower()
CONV_PORT = int(os.environ.get("CONV_PORT", 8081))


def is_dry_run(category: str) -> bool:
    """Check whether a side-effecting category is in dry-run mode for staging.

    Categories used: PRINTER (Moonraker/Bambu/resume/alert), CLAUDE (CLI subprocess),
    APNS (Live Activity push). Set CONV_<CATEGORY>_DRY_RUN=1 to enable.
    Default OFF — production behaviour unchanged.
    """
    return os.environ.get(f"CONV_{category}_DRY_RUN") == "1"


# ── Config (env-overridable for staging) ──
WORK_DIR = os.environ.get("CONV_WORK_DIR") or work_dir()
SESSIONS_DIR = os.environ.get("CONV_SESSIONS_DIR") or os.path.join(
    os.path.expanduser("~/.claude/projects/-Users-timtrailor-code"),
    "phone_sessions",
)
_TMP_LOG_DIR = os.environ.get("CONV_TMP_LOG_DIR", "/tmp/claude_sessions")
os.makedirs(SESSIONS_DIR, exist_ok=True)
os.makedirs(_TMP_LOG_DIR, exist_ok=True)

# MCP config — one level up from conv/ to find mcp-approval.json
MCP_CONFIG = os.environ.get("CONV_MCP_CONFIG") or os.path.join(
    os.path.dirname(os.path.realpath(__file__)),
    os.pardir,
    "mcp-approval.json",
)


def assert_staging_safe() -> None:
    """Refuse to start in staging mode if any mutable path resolves under production.

    plan §5.3 boot-time assertion. If CONV_MODE=staging, all mutable paths
    (work dir, sessions dir, log dir, google token) MUST be redirected away
    from ~/code/claude-mobile/ and the production phone_sessions dir.
    """
    if CONV_MODE != "staging":
        return
    prod_paths = [
        os.path.realpath(os.path.expanduser("~/code/claude-mobile")),
        os.path.realpath(
            os.path.expanduser(
                "~/.claude/projects/-Users-timtrailor-code/phone_sessions"
            )
        ),
    ]
    candidates = {
        "CONV_WORK_DIR (WORK_DIR)": WORK_DIR,
        "CONV_SESSIONS_DIR": SESSIONS_DIR,
        "CONV_TMP_LOG_DIR": _TMP_LOG_DIR,
        "CONV_GOOGLE_TOKEN_PATH": os.environ.get("CONV_GOOGLE_TOKEN_PATH", ""),
    }
    for name, path in candidates.items():
        if not path:
            continue
        real = os.path.realpath(path)
        for prod in prod_paths:
            if real == prod or real.startswith(prod + os.sep):
                raise RuntimeError(
                    f"REFUSING TO START in CONV_MODE=staging: {name}={path} "
                    f"resolves under production path '{prod}'. Redirect to /tmp."
                )


def boot_banner() -> str:
    """Emit a [boot] line with git_sha + file_sha256 + port + mode.

    plan §11.3 drift detector. Compared post-deploy against expected HEAD.
    """
    import hashlib
    import subprocess as _sp

    repo = os.path.realpath(
        os.path.join(os.path.dirname(os.path.realpath(__file__)), os.pardir)
    )
    try:
        sha = _sp.check_output(
            ["git", "-C", repo, "rev-parse", "HEAD"],
            stderr=_sp.DEVNULL,
            text=True,
        ).strip()[:7]
    except Exception:
        sha = "unknown"
    try:
        target = os.path.join(repo, "conversation_server.py")
        with open(target, "rb") as fh:
            file_sha = hashlib.sha256(fh.read()).hexdigest()[:12]
    except Exception:
        file_sha = "unknown"
    line = (
        f"[boot] git_sha={sha} file_sha256={file_sha} port={CONV_PORT} mode={CONV_MODE}"
    )
    _log.info(line)
    return line


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
