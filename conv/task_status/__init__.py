"""Task status / heartbeat endpoint slice.

Extracted from conversation_server.py 2026-04-27.

Public surface re-exported for back-compat:
  _task_status        — module-level state dict
  _task_status_lock   — threading.Lock() guarding the dict
  _update_task_status — helper called by Claude event handlers
  bp                  — Flask blueprint with GET /task-status
"""

from .routes import (  # noqa: F401
    _task_status,
    _task_status_lock,
    _update_task_status,
    bp,
)
