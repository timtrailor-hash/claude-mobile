"""WebSocket client registry — module-level state.

`_ws_clients` is mutated by:
  - websocket_handler (still in conversation_server.py) on connect/disconnect.
  - _broadcast_ws (this slice) when a client send fails (dead-client cleanup).
  - _notify_smart (this slice) for the has-clients check.

All three callers must import from this module so they share the same dict.
"""

from __future__ import annotations

import threading


_ws_clients: dict = {}  # {client_id: ws_object}
_ws_clients_lock = threading.Lock()
