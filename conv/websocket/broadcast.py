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
        # FIRST CHOICE: push to PrinterPilot and ClaudeControl (native iOS push).
        push_title = event_dict.get("printer", "Printer")
        threading.Thread(
            target=_send_push_notification,
            args=(push_title, alert_msg, "com.timtrailor.printerpilot"),
            daemon=True,
        ).start()
        threading.Thread(
            target=_send_push_notification,
            args=(push_title, alert_msg, "com.timtrailor.claudecontrol"),
            daemon=True,
        ).start()
        # BACKUP: Slack + email so alerts still arrive if push fails.
        # Lazy import — _notify_slack/_notify_email still live in conversation_server.py
        # and importing them at module-load time would create a cycle.
        try:
            from conversation_server import _notify_email, _notify_slack

            threading.Thread(
                target=_notify_slack, args=(alert_msg,), daemon=True
            ).start()
            threading.Thread(
                target=_notify_email,
                args=(f"[Printer] {alert_msg[:60]}", alert_msg),
                daemon=True,
            ).start()
        except ImportError as exc:
            _log.warning("_broadcast_ws: critical-alert backup import failed: %s", exc)


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
