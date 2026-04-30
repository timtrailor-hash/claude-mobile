"""WebSocket handler — real-time bidirectional Claude communication.

Extracted from conversation_server.py 2026-04-30 (slice 1O — final
deferred slice from plan §13). The handler owns subprocess lifecycle
during a streamed conversation; some dependencies still live in the
monolith and are passed in via set_server_deps() rather than imported
at module-load time (the same dependency-injection pattern alert_responder
and la_reaper use; see those modules' docstrings for the rationale).

Move-only — no logic changes. The @sock.route("/ws") decorator runs at
module import time and registers the handler with the shared flask_sock
instance from conv.app, so importing this module is sufficient to wire
the route.
"""

from __future__ import annotations

import json
import os
import signal
import threading
import time
import uuid

from flask import request

from conv.app import (
    AUTH_TOKEN,
    _event_condition,
    _lock,
    _log,
    _session,
    sock,
)
from conv.websocket import _ws_clients, _ws_clients_lock
from conv.websocket.state import _ws_connect_lock, _ws_connect_times


# ── Dependency injection from conversation_server ──────────────────────────
# These are set by conversation_server.set_server_deps() at startup, after
# the relevant functions/state are defined in the monolith. Importing them
# at module-load time would create a cycle (conversation_server imports
# conv.websocket.handler which would re-import conversation_server).
_start_session = None  # type: ignore[assignment]
_ensure_subprocess = None  # type: ignore[assignment]
_kill_subprocess = None  # type: ignore[assignment]
_events_path = None  # type: ignore[assignment]
_permission_lock = None  # type: ignore[assignment]
_permission_requests = None  # type: ignore[assignment]
_get_permission_level = None  # type: ignore[assignment]
_set_permission_level = None  # type: ignore[assignment]


def set_server_deps(
    *,
    start_session,
    ensure_subprocess,
    kill_subprocess,
    events_path,
    permission_lock,
    permission_requests,
    get_permission_level,
    set_permission_level,
):
    """Called by conversation_server at startup, after the relevant
    functions and state are defined, to wire websocket_handler to real impls."""
    global _start_session, _ensure_subprocess, _kill_subprocess, _events_path
    global _permission_lock, _permission_requests
    global _get_permission_level, _set_permission_level
    _start_session = start_session
    _ensure_subprocess = ensure_subprocess
    _kill_subprocess = kill_subprocess
    _events_path = events_path
    _permission_lock = permission_lock
    _permission_requests = permission_requests
    _get_permission_level = get_permission_level
    _set_permission_level = set_permission_level


@sock.route("/ws")
def websocket_handler(ws):
    """WebSocket endpoint — real-time bidirectional Claude communication.

    On connect, sends catch-up events for the current session.
    Then streams new events in real-time as they arrive.
    Accepts commands (message, resume, cancel, new_session) from the client.
    """
    client_id = str(uuid.uuid4())[:8]
    _log.info("WS client %s connected", client_id)

    with _ws_connect_lock:
        _ws_connect_times.append(time.time())

    with _ws_clients_lock:
        _ws_clients[client_id] = ws

    # Check auth for non-localhost WebSocket clients
    if ws.environ.get("REMOTE_ADDR") not in ("127.0.0.1", "::1"):
        token_param = request.args.get("token", "")
        if AUTH_TOKEN and token_param != AUTH_TOKEN:
            try:
                ws.send(
                    json.dumps(
                        {
                            "type": "ws_error",
                            "content": "Unauthorized \u2014 check auth token in Settings",
                        }
                    )
                )
                ws.close()
            except Exception:
                pass
            return

    # Send immediate welcome so iOS app knows connection is live
    try:
        ws.send(json.dumps({"type": "pong"}))
    except Exception:
        pass

    # Re-broadcast any pending permission requests so the new client can respond.
    # This handles the case where a WebSocket disconnects while a permission prompt
    # is waiting — without this, the prompt is lost and times out (auto-denied).
    with _permission_lock:
        for req_id, req in _permission_requests.items():
            if req.get("response") is None:  # Still waiting
                data = req.get("data", {})
                try:
                    ws.send(
                        json.dumps(
                            {
                                "type": "permission_request",
                                "request_id": req_id,
                                "tool_name": data.get("tool_name", ""),
                                "input": data.get("input", {}),
                                "summary": data.get("summary", "Use a tool"),
                            }
                        )
                    )
                except Exception:
                    pass

    # Replay active prompt_state so reconnecting clients see their bar.
    try:
        from conv.prompt_state import replay_active_prompts as _ps_replay

        _ps_replay(lambda d: ws.send(json.dumps(d)))
    except Exception as _ps_exc:
        _log.debug("prompt_state replay skipped: %s", _ps_exc)

    # Track what we've sent so the sender thread knows where the file cursor is
    sender_state = {
        "session_id": None,
        "events_file": None,
        "offset": 0,
        "stop": False,
    }
    sender_state_lock = threading.Lock()

    def _ws_send(data):
        """Send JSON to client, silently ignore if connection is dead."""
        try:
            ws.send(json.dumps(data))
        except Exception:
            sender_state["stop"] = True

    def _sender_loop():
        """Background thread: tails events.jsonl, sends to WS client.

        Uses _event_condition to wake up immediately when new events arrive
        instead of polling with sleep. Falls back to 0.5s timeout to handle
        edge cases (missed signals, session changes).
        """
        while not sender_state["stop"]:
            with sender_state_lock:
                sid = sender_state["session_id"]
                ef = sender_state["events_file"]
                offset = sender_state["offset"]

            if not sid or not ef or not os.path.exists(ef):
                # No active session to stream — wait for one
                with _event_condition:
                    _event_condition.wait(timeout=1.0)
                # Check if session has started since we last looked
                with _lock:
                    current_sid = _session.get("id")
                    current_ef = _session.get("events_file")
                if current_sid and current_ef:
                    with sender_state_lock:
                        if sender_state["session_id"] != current_sid:
                            sender_state["session_id"] = current_sid
                            sender_state["events_file"] = current_ef
                            sender_state["offset"] = 0
                continue

            # Read events from file starting at our offset
            line_num = 0
            done = False
            try:
                with open(ef, "rb") as f:
                    raw_buffer = b""
                    while not sender_state["stop"] and not done:
                        chunk = f.read(8192)
                        if chunk:
                            raw_buffer += chunk

                        while b"\n" in raw_buffer:
                            line_bytes, raw_buffer = raw_buffer.split(b"\n", 1)
                            line = line_bytes.decode("utf-8", errors="replace").strip()
                            if not line:
                                continue

                            if line_num >= offset:
                                try:
                                    ev = json.loads(line)
                                    ev["offset"] = line_num
                                    _ws_send(ev)
                                    if ev.get("type") == "done":
                                        done = True
                                        break
                                except json.JSONDecodeError:
                                    pass
                            line_num += 1

                        if done or sender_state["stop"]:
                            break

                        if not chunk:
                            # No new data — update our offset and wait
                            with sender_state_lock:
                                sender_state["offset"] = line_num

                            # Check if session has changed
                            with _lock:
                                current_sid = _session.get("id")
                                current_ef = _session.get("events_file")

                            if current_sid != sid:
                                # Session changed — switch to new one
                                with sender_state_lock:
                                    sender_state["session_id"] = current_sid
                                    sender_state["events_file"] = current_ef
                                    sender_state["offset"] = 0
                                break

                            # Wait for _append_event to signal new data
                            with _event_condition:
                                _event_condition.wait(timeout=0.5)

            except Exception as e:
                _ws_send({"type": "ws_error", "content": str(e)})

            # Update offset after finishing a file read pass
            with sender_state_lock:
                sender_state["offset"] = line_num

            if done:
                # Session complete — wait for next session to start
                with _event_condition:
                    _event_condition.wait(timeout=1.0)
                # Check for new session
                with _lock:
                    current_sid = _session.get("id")
                    current_ef = _session.get("events_file")
                with sender_state_lock:
                    if current_sid != sender_state["session_id"]:
                        sender_state["session_id"] = current_sid
                        sender_state["events_file"] = current_ef
                        sender_state["offset"] = 0

    # NOTE: Protocol-level pings are handled by simple-websocket's _thread
    # via SOCK_SERVER_OPTIONS ping_interval=25. No custom keepalive needed.

    # Start sender thread
    sender = threading.Thread(target=_sender_loop, daemon=True)
    sender.start()

    # Seed sender with current session (catch-up)
    with _lock:
        current_sid = _session.get("id")
        current_ef = _session.get("events_file")
    if current_sid and current_ef:
        with sender_state_lock:
            sender_state["session_id"] = current_sid
            sender_state["events_file"] = current_ef
            sender_state["offset"] = 0

    # Receive loop — handle incoming messages from client
    try:
        while True:
            raw = ws.receive(timeout=None)
            if raw is None:
                break

            try:
                msg = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                _ws_send({"type": "ws_error", "content": "Invalid JSON"})
                continue

            msg_type = msg.get("type", "")

            if msg_type == "message":
                text = msg.get("text", "").strip()
                if not text:
                    _ws_send({"type": "ws_error", "content": "Empty message"})
                    continue

                image_paths = msg.get("image_paths", [])
                client_sid = msg.get("session_id")

                result, error = _start_session(text, image_paths, client_sid)
                if error:
                    _ws_send({"type": "ws_error", "content": error})
                else:
                    _ws_send(
                        {
                            "type": "session_started",
                            "session_id": result["session_id"],
                            "pid": result["pid"],
                        }
                    )
                    # Point sender at the new session
                    with sender_state_lock:
                        sender_state["session_id"] = result["session_id"]
                        sender_state["events_file"] = _events_path(result["session_id"])
                        sender_state["offset"] = 0
                    # Wake sender
                    with _event_condition:
                        _event_condition.notify_all()

            elif msg_type == "resume":
                resume_sid = msg.get("session_id", "").strip()
                if resume_sid:
                    _log.info(
                        "WS resume: killing subprocess and restarting with --resume %s",
                        resume_sid,
                    )
                    if _ensure_subprocess(resume_session_id=resume_sid):
                        with _lock:
                            _session["claude_sid"] = resume_sid
                        _ws_send({"type": "resumed", "session_id": resume_sid})
                    else:
                        _ws_send(
                            {
                                "type": "ws_error",
                                "content": "Failed to restart Claude with --resume",
                            }
                        )
                else:
                    _ws_send(
                        {"type": "ws_error", "content": "Missing session_id for resume"}
                    )

            elif msg_type == "cancel":
                with _lock:
                    proc = _session.get("proc")
                    if proc and proc.poll() is None:
                        try:
                            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                        except Exception:
                            try:
                                proc.terminate()
                            except Exception:
                                pass
                        _session["status"] = "completed"
                        _session["proc"] = None
                        _session["stdin_pipe"] = None
                        _ws_send({"type": "cancelled"})
                    else:
                        _ws_send({"type": "ws_error", "content": "No active process"})

            elif msg_type == "new_session":
                # Kill persistent subprocess so next message starts fresh
                _kill_subprocess()
                with _lock:
                    _session["claude_sid"] = None
                _ws_send({"type": "new_session_ok"})

            elif msg_type == "permission_response":
                # User approved/denied a permission request from their phone
                req_id = msg.get("request_id", "")
                allow = msg.get("allow", False)
                with _permission_lock:
                    pending = _permission_requests.get(req_id)
                if pending:
                    pending["response"] = {
                        "allow": allow,
                        "message": msg.get("message", ""),
                        "updatedInput": msg.get("updatedInput"),
                    }
                    pending["event"].set()  # Wake the long-poll
                    _ws_send({"type": "permission_acknowledged", "request_id": req_id})
                else:
                    # Stale request (already handled or timed out) — not an error
                    _log.info(
                        "Permission %s: stale (already handled/expired), ignoring",
                        req_id,
                    )
                    _ws_send(
                        {
                            "type": "permission_acknowledged",
                            "request_id": req_id,
                            "stale": True,
                        }
                    )

            elif msg_type == "set_permission_level":
                # iOS Settings changed the permission level
                new_level = msg.get("level", "moderate")
                if new_level not in (
                    "approve_all",
                    "terminal_only",
                    "terminal_and_web",
                    "most_actions",
                ):
                    _ws_send(
                        {
                            "type": "ws_error",
                            "content": f"Invalid permission level: {new_level}",
                        }
                    )
                    continue

                old_level = _get_permission_level()
                _set_permission_level(new_level)
                _log.info("Permission level changed: %s → %s", old_level, new_level)

                # Kill subprocess so next message spawns with new mode
                if old_level != new_level:
                    with _lock:
                        proc = _session.get("proc")
                        if proc and proc.poll() is None:
                            try:
                                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                            except Exception:
                                try:
                                    proc.terminate()
                                except Exception:
                                    pass
                        _session["proc"] = None
                        _session["stdin_pipe"] = None
                        _session["pid"] = None

                _ws_send({"type": "permission_level_set", "level": new_level})

            elif msg_type == "ping":
                _ws_send({"type": "pong"})

            else:
                _ws_send(
                    {"type": "ws_error", "content": f"Unknown message type: {msg_type}"}
                )

    except Exception as e:
        # Log ALL disconnect reasons for debugging
        _log.warning("WS client %s exception: %s: %s", client_id, type(e).__name__, e)

    finally:
        sender_state["stop"] = True
        # Wake sender so it exits promptly
        with _event_condition:
            _event_condition.notify_all()

        with _ws_clients_lock:
            _ws_clients.pop(client_id, None)

        _log.info("WS client %s disconnected", client_id)
