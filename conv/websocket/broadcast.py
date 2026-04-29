"""WebSocket broadcast + smart-notify helpers.

_broadcast_ws        — send event to every connected client; fan critical
                       events out to APNs (PrinterPilot + ClaudeControl) +
                       Slack + email.
_notify_smart        — pick channel based on whether any client is connected.

`_notify_slack` and `_notify_email` are still defined in conversation_server.py
(legacy notification helpers). They're imported lazily inside _broadcast_ws to
avoid an import cycle (conversation_server imports conv.websocket; conv.websocket
must NOT import conversation_server at module-load time).
"""

from __future__ import annotations

import json
import threading

from conv.app import _log
from conv.push import _send_push_notification

from .state import _ws_clients, _ws_clients_lock


_CRITICAL_EVENTS: set[str] = {
    "firmware_error",
    "state_change",
    "config_corruption",
    "ups_warning",
    "ups_critical",
}

# Only certain state changes are critical.
_CRITICAL_STATE_MESSAGES: set[str] = {"error", "cancelled", "complete", "paused"}


def _broadcast_ws(event_dict: dict) -> None:
    """Send event to all connected WebSocket clients.

    Critical alerts (errors, failures, UPS) are also fanned out to APNs +
    Slack + email.
    """
    msg = json.dumps(event_dict)
    with _ws_clients_lock:
        dead = []
        for cid, ws_obj in _ws_clients.items():
            try:
                ws_obj.send(msg)
            except Exception:
                dead.append(cid)
        for cid in dead:
            _ws_clients.pop(cid, None)
    _log.info("Broadcast: %s", event_dict.get("message", ""))

    # Multi-channel alerting for critical events.
    event_type = event_dict.get("event", "")
    alert_msg = event_dict.get("message", "")
    is_critical = event_type in _CRITICAL_EVENTS

    # For state_change events, only alert on critical transitions.
    if event_type == "state_change":
        new_state = event_dict.get("new_state", "").lower()
        is_critical = any(kw in new_state for kw in _CRITICAL_STATE_MESSAGES)

    if is_critical and alert_msg:
        # Push to PrinterPilot (the dedicated 3D-printer surface). Previously
        # this also fanned out to ClaudeControl, producing TWO push notifications
        # per critical printer event (one state change -> two banners on the
        # lock screen). 2026-04-29: dropped the ClaudeControl push because Tim
        # was getting double notifications on every print start/cancel/complete.
        # ClaudeControl is for general Claude/system control, not printer alerts.
        push_title = event_dict.get("printer", "Printer")
        threading.Thread(
            target=_send_push_notification,
            args=(push_title, alert_msg, "com.timtrailor.printerpilot"),
            daemon=True,
        ).start()
        # BACKUP: Slack + email so alerts still arrive if push fails.
        # Lazy import — _notify_slack/_notify_email still live in conversation_server.py
        # and importing them at module-load time would create a cycle.
        # Lazy lookup of legacy notify helpers without triggering an import.
        # The daemon runs as __main__; `from conversation_server import ...` would
        # re-execute conversation_server.py, re-running its app.register_blueprint
        # calls. Flask refuses post-first-request, raising AssertionError. The
        # raised error used to propagate to _check_printer_state, blocking the
        # _prev_printer_state advance and causing every 30s poll to re-fire the
        # same state transition. (Bug 2026-04-28.)
        import sys as _sys

        _ns = _ne = None
        for _modname in ("__main__", "conversation_server"):
            _mod = _sys.modules.get(_modname)
            if _mod is None:
                continue
            _candidate_ns = getattr(_mod, "_notify_slack", None)
            _candidate_ne = getattr(_mod, "_notify_email", None)
            if callable(_candidate_ns) and callable(_candidate_ne):
                _ns = _candidate_ns
                _ne = _candidate_ne
                break
        if _ns is not None:
            try:
                threading.Thread(target=_ns, args=(alert_msg,), daemon=True).start()
                threading.Thread(
                    target=_ne,
                    args=(f"[Printer] {alert_msg[:60]}", alert_msg),
                    daemon=True,
                ).start()
            except Exception as exc:
                _log.warning("_broadcast_ws: critical-alert backup failed: %s", exc)
        else:
            _log.warning(
                "_broadcast_ws: _notify_slack/_notify_email not in sys.modules; "
                "critical-alert backup channels skipped (msg=%s)",
                alert_msg[:80],
            )


def _notify_smart(title: str, message: str) -> None:
    """Smart notification: WebSocket if app is connected, else APNs.

    (Email and Slack are deliberately disabled per the legacy comment in the
    monolith — push-only.)
    """
    with _ws_clients_lock:
        has_clients = len(_ws_clients) > 0

    if has_clients:
        # App is connected — it will show a local notification.
        _broadcast_ws({"type": "push_message", "content": f"{title}: {message}"})
        _log.info("Smart notify via WebSocket: %s", title)
    else:
        # App is offline — push only.
        _log.info("Smart notify via APNs (no WS clients): %s", title)
        threading.Thread(
            target=_send_push_notification,
            args=(title, message, "com.timtrailor.terminal"),
            daemon=True,
        ).start()
