"""WebSocket helper slice — extracted from conversation_server.py 2026-04-27.

Public surface re-exported for back-compat:
  _ws_clients, _ws_clients_lock — module-level state shared with websocket_handler
  _broadcast_ws                 — send event to all connected clients + critical alerts
  _notify_smart                 — WebSocket if any client connected, else APNs
  _CRITICAL_EVENTS, _CRITICAL_STATE_MESSAGES — alert-routing constants

NOT in this slice (deferred):
  websocket_handler (the @sock.route('/ws') itself) — large state-coupled
    function that owns subprocess lifecycle. Stays in conversation_server.py
    until its dependencies are clearer.
"""

from .broadcast import (  # noqa: F401
    _CRITICAL_EVENTS,
    _CRITICAL_STATE_MESSAGES,
    _broadcast_ws,
    _notify_smart,
)
from .state import _ws_clients, _ws_clients_lock  # noqa: F401
