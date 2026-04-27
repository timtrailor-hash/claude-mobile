"""Task status / heartbeat endpoint."""

from __future__ import annotations

import threading
from datetime import datetime

from flask import Blueprint, jsonify

from conv.app import _lock, _session

bp = Blueprint("task_status", __name__)


_task_status: dict = {
    "description": None,
    "started_at": None,
    "last_activity": None,
    "alive": False,
    "progress_pct": None,
    "event_count": 0,
}
_task_status_lock = threading.Lock()


def _update_task_status(**kwargs) -> None:
    """Update task status fields. Call this when Claude emits events."""
    with _task_status_lock:
        _task_status["last_activity"] = datetime.now().isoformat()
        _task_status["alive"] = True
        for k, v in kwargs.items():
            if k in _task_status:
                _task_status[k] = v


@bp.route("/task-status")
def task_status_endpoint():
    """Heartbeat endpoint — shows current task progress for iOS status indicator."""
    with _task_status_lock:
        status = dict(_task_status)
    # Calculate elapsed time.
    if status.get("started_at"):
        try:
            started = datetime.fromisoformat(status["started_at"])
            elapsed = (datetime.now() - started).total_seconds()
            status["elapsed_seconds"] = int(elapsed)
        except Exception:
            pass
    # Check if subprocess is actually still running.
    with _lock:
        proc = _session.get("proc")
        if proc is not None:
            status["alive"] = proc.poll() is None
        else:
            status["alive"] = False
    return jsonify(status)
