#!/usr/bin/env python3
"""Conversation server — owns Claude CLI subprocess lifecycle.

Runs as a persistent launchd daemon on port 8081.
The wrapper (app.py) proxies to this server.
Sessions survive wrapper restarts and iOS Safari connection drops.

Routes:
  POST /send          — send message, spawn Claude CLI
  GET  /stream?offset=N — SSE stream from event N (reconnectable)
  WS   /ws            — WebSocket: real-time bidirectional (native iOS app)
  GET  /status        — current session status
  POST /cancel        — kill active subprocess
  POST /new-session   — clear session (start fresh conversation)
  GET  /last-response — last completed response text
"""

import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime

# Add shared_utils to path
sys.path.insert(0, os.path.expanduser("~/Documents/Claude code"))
from shared_utils import env_for_claude_cli, work_dir

from flask import Flask, Response, jsonify, request
from flask_sock import Sock

app = Flask(__name__)
sock = Sock(app)

# ── Config ──
WORK_DIR = work_dir()
SESSIONS_DIR = "/tmp/claude_sessions"
os.makedirs(SESSIONS_DIR, exist_ok=True)

# ── Session state (single active conversation) ──
_lock = threading.Lock()
_session = {
    "id": None,            # Current session UUID
    "claude_sid": None,    # Claude conversation session ID (for --resume)
    "proc": None,          # subprocess.Popen
    "pid": None,           # PID (survives proc object loss)
    "events_file": None,   # Processed SSE events (JSONL, one per line)
    "event_count": 0,      # Number of events written
    "status": "idle",      # idle | running | completed | error
    "started": None,
    "last_activity": None,
}

# Last completed response for recovery
_last_response = {"text": "", "cost": "", "session_id": None, "seq": 0}
_last_response_lock = threading.Lock()

# Condition variable — signalled every time _append_event writes a line.
# WebSocket handlers wait on this instead of polling with sleep.
_event_condition = threading.Condition()


def _events_path(session_id):
    return os.path.join(SESSIONS_DIR, session_id, "events.jsonl")


def _append_event(events_file, event_data):
    """Atomically append an SSE event to the given events file.

    Takes the file path explicitly (not from _session) to avoid race
    conditions when a new session starts while the old reader is finishing.
    flush+fsync ensures data hits disk before we return (crash-safe).
    After writing, signals the condition variable so WebSocket handlers
    wake up immediately instead of polling.
    """
    line = json.dumps(event_data) + "\n"
    try:
        with open(events_file, "a") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())
    except Exception:
        pass

    # Wake any WebSocket handlers waiting for new events
    with _event_condition:
        _event_condition.notify_all()


def _process_event(event):
    """Convert Claude CLI stream-json event to SSE event dicts."""
    etype = event.get("type", "")

    if etype == "system" and event.get("subtype") == "init":
        sid = event.get("session_id", "")
        model = event.get("model", "unknown")
        yield {"type": "init", "session_id": sid, "model": model}

    elif etype == "assistant":
        msg = event.get("message", {})
        for block in msg.get("content", []):
            if block.get("type") == "text":
                text = block.get("text", "")
                if text:
                    yield {"type": "text", "content": text}
            elif block.get("type") == "tool_use":
                summary = _format_tool(block.get("name", ""), block.get("input", {}))
                yield {"type": "tool", "content": summary}
                # Detect image file reads and emit an image event
                if block.get("name") == "Read":
                    fpath = block.get("input", {}).get("file_path", "")
                    if fpath and any(fpath.lower().endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".gif", ".webp")):
                        yield {"type": "image", "content": f"/file?path={fpath}"}

    elif etype == "tool":
        tool_name = event.get("tool", "")
        if tool_name:
            yield {"type": "tool", "content": f"Result from {tool_name}"}

    elif etype == "result":
        cost = event.get("total_cost_usd", 0)
        duration = event.get("duration_ms", 0)
        turns = event.get("num_turns", 0)
        if cost > 0:
            yield {"type": "cost", "content": f"${cost:.4f} | {duration/1000:.1f}s | {turns} turn(s)"}

    elif etype == "user":
        tool_result = event.get("tool_use_result")
        if tool_result:
            yield {"type": "activity", "content": "tool_result"}
        else:
            yield {"type": "activity", "content": "processing"}

    elif etype == "system":
        subtype = event.get("subtype", "")
        if subtype and subtype != "init":
            yield {"type": "activity", "content": subtype}

    elif etype == "rate_limit_event":
        yield {"type": "activity", "content": "rate_limited"}


def _format_tool(name, input_data):
    """One-line tool call summary."""
    if name == "Bash":
        desc = input_data.get("description", "")
        if desc:
            return f"Running: {desc}"
        cmd = input_data.get("command", "")
        return f"$ {cmd[:77]}..." if len(cmd) > 80 else f"$ {cmd}"
    elif name == "Read":
        return f"Reading: {os.path.basename(input_data.get('file_path', ''))}"
    elif name == "Write":
        return f"Writing: {os.path.basename(input_data.get('file_path', ''))}"
    elif name == "Edit":
        return f"Editing: {os.path.basename(input_data.get('file_path', ''))}"
    elif name == "Glob":
        return f"Searching: {input_data.get('pattern', '')}"
    elif name == "Grep":
        return f"Grep: {input_data.get('pattern', '')}"
    elif name == "Task":
        return f"Agent: {input_data.get('description', '')}"
    elif name == "WebFetch":
        return f"Fetching: {input_data.get('url', '')[:60]}"
    elif name == "WebSearch":
        return f"Searching web: {input_data.get('query', '')}"
    return f"Tool: {name}"


def _keepalive_thread(session_id, events_file, activity_ref, done_flag):
    """Writes periodic ping events to prevent Safari idle-kill.

    During tool execution (Bash, Read, etc.), Claude CLI goes silent for
    10-30s. Safari kills idle SSE connections. This thread writes a ping
    every 10s when no real events have flowed, keeping the stream alive.
    """
    while not done_flag[0]:
        time.sleep(10)
        if done_flag[0]:
            break
        idle = time.time() - activity_ref[0]
        if idle >= 8:
            _append_event(events_file, {"type": "ping"})
            with _lock:
                if _session.get("id") == session_id:
                    _session["last_activity"] = time.time()


def _reader_thread(proc, session_id, events_file):
    """Background thread: reads Claude CLI stdout (pipe), processes events,
    writes to events.jsonl.

    Uses subprocess.PIPE for stdout — Node.js line-buffers pipes (unlike
    regular files where it fully buffers, causing the "0 bytes for minutes"
    bug). The events file provides persistence for reconnection.
    """
    response_text = ""
    response_cost = ""
    claude_sid = None
    event_count = 0
    spawn_time = time.time()
    first_byte_logged = False
    last_event_type = None

    # Shared state for keepalive thread
    activity_ref = [time.time()]
    done_flag = [False]
    ka = threading.Thread(
        target=_keepalive_thread,
        args=(session_id, events_file, activity_ref, done_flag),
        daemon=True)
    ka.start()

    try:
        for raw_line in proc.stdout:
            if not first_byte_logged:
                elapsed = time.time() - spawn_time
                print(f"[{datetime.now().isoformat()}] [{session_id}] First output "
                      f"after {elapsed:.1f}s", flush=True)
                first_byte_logged = True

            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue

            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            # Extract claude session ID from init event
            if event.get("type") == "system" and event.get("subtype") == "init":
                claude_sid = event.get("session_id", "")
                with _lock:
                    if _session.get("id") == session_id:
                        _session["claude_sid"] = claude_sid

            for ev in _process_event(event):
                _append_event(events_file, ev)
                event_count += 1
                activity_ref[0] = time.time()

                # Update session activity (only if still current session)
                with _lock:
                    if _session.get("id") == session_id:
                        _session["event_count"] = event_count
                        _session["last_activity"] = time.time()

                # Accumulate text/cost
                if ev.get("type") == "text":
                    response_text += ev.get("content", "")
                elif ev.get("type") == "cost":
                    response_cost = ev.get("content", "")
                last_event_type = ev.get("type")

    except Exception as e:
        _append_event(events_file, {"type": "error", "content": str(e)})

    done_flag[0] = True

    # Check if response was truncated by turn limit:
    # If last content was a tool_use (not text/cost), the CLI hit --max-turns
    # mid-response. Signal this so the client can auto-continue.
    truncated = last_event_type == "tool"

    # Write terminal done event
    _append_event(events_file, {"type": "done", "truncated": truncated})

    # Mark session complete (only if still current session)
    with _lock:
        if _session.get("id") == session_id:
            _session["status"] = "completed"
            _session["proc"] = None

    # Save last response
    if response_text:
        with _last_response_lock:
            _last_response["text"] = response_text
            _last_response["cost"] = response_cost
            _last_response["session_id"] = claude_sid
            _last_response["seq"] += 1

    print(f"[{datetime.now().isoformat()}] [{session_id}] Session complete: "
          f"{event_count} events", flush=True)


def _start_session(message, image_paths=None, client_session_id=None):
    """Core session logic — spawn Claude CLI subprocess.

    Returns (result_dict, error_string). On success error_string is None.
    Used by both POST /send and the WebSocket handler.
    """
    image_paths = image_paths or []

    with _lock:
        # Kill existing process if still running
        if _session["proc"] and _session["proc"].poll() is None:
            try:
                os.killpg(os.getpgid(_session["proc"].pid), signal.SIGTERM)
            except Exception:
                try:
                    _session["proc"].terminate()
                except Exception:
                    pass

        # Prepare session directory
        sid = str(uuid.uuid4())[:8]
        session_dir = os.path.join(SESSIONS_DIR, sid)
        os.makedirs(session_dir, exist_ok=True)

        events_file = os.path.join(session_dir, "events.jsonl")
        # Clear events file
        open(events_file, "w").close()

        _session["id"] = sid
        _session["events_file"] = events_file
        _session["event_count"] = 0
        _session["status"] = "running"
        _session["started"] = time.time()
        _session["last_activity"] = time.time()

    # Attach images if any
    valid_paths = [p for p in image_paths if os.path.exists(p)]
    if valid_paths:
        paths_str = ", ".join(valid_paths)
        message = (f"I've attached {len(valid_paths)} image(s) at these paths: "
                   f"{paths_str} — please read/view them first. {message}")

    # Build command
    cmd = [
        "claude", "-p", message,
        "--model", "claude-opus-4-6",
        "--max-turns", "50",
        "--output-format", "stream-json",
        "--verbose"
    ]

    # Resume existing conversation
    resume_sid = client_session_id or _session.get("claude_sid")
    if resume_sid:
        cmd.extend(["--resume", resume_sid])

    # Clean environment — subscription auth only, no API key leakage
    env = env_for_claude_cli()

    # Spawn subprocess with PIPE for stdout.
    # Node.js (Claude CLI) line-buffers pipes but fully buffers regular files.
    # Using PIPE ensures streaming output arrives immediately.
    # stderr goes to a log file (never PIPE without a drain thread).
    try:
        stderr_path = os.path.join(SESSIONS_DIR, "claude_stderr.log")
        stderr_handle = open(stderr_path, "ab")

        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=stderr_handle,
                cwd=WORK_DIR,
                env=env,
                preexec_fn=os.setsid
            )
        finally:
            stderr_handle.close()

        with _lock:
            _session["proc"] = proc
            _session["pid"] = proc.pid

        # Start background reader — reads from pipe, writes to events file
        reader = threading.Thread(
            target=_reader_thread,
            args=(proc, sid, events_file),
            daemon=True)
        reader.start()

        print(f"[{datetime.now().isoformat()}] Started session {sid} "
              f"(PID {proc.pid})", flush=True)

        return {"session_id": sid, "pid": proc.pid}, None

    except FileNotFoundError:
        with _lock:
            _session["status"] = "error"
        return None, "Claude CLI not found"
    except Exception as e:
        with _lock:
            _session["status"] = "error"
        return None, str(e)


@app.route("/send", methods=["POST"])
def send():
    """Send a message to Claude. Spawns CLI subprocess."""
    data = request.json or {}
    message = data.get("message", "").strip()
    image_paths = data.get("image_paths", [])
    client_session_id = data.get("session_id")

    if not message:
        return jsonify({"error": "Empty message"}), 400

    result, error = _start_session(message, image_paths, client_session_id)
    if error:
        return jsonify({"error": error}), 500
    return jsonify(result)


@app.route("/stream")
def stream():
    """SSE stream of events. Supports offset for reconnection.

    Query params:
      offset: start from this event number (0-based). Default 0.
    """
    offset = request.args.get("offset", 0, type=int)

    with _lock:
        sid = _session.get("id")
        events_file = _session.get("events_file")

    if not sid or not events_file:
        return jsonify({"error": "No active session"}), 404

    SAFARI_PAD = ": " + "x" * 256 + "\n\n"

    def generate():
        yield ": " + "x" * 2048 + "\n\n"  # Safari prime

        line_num = 0
        raw_buffer = b""
        done = False

        try:
            with open(events_file, "rb") as f:
                while not done:
                    chunk = f.read(8192)
                    if chunk:
                        raw_buffer += chunk

                    # Always process complete lines in buffer — even after
                    # drain adds data via raw_buffer without new chunk
                    while b"\n" in raw_buffer:
                        line_bytes, raw_buffer = raw_buffer.split(b"\n", 1)
                        line = line_bytes.decode("utf-8", errors="replace").strip()
                        if not line:
                            continue

                        if line_num >= offset:
                            try:
                                ev = json.loads(line)
                                if ev.get("type") == "done":
                                    # Forward done event to client (includes truncated flag)
                                    ev["offset"] = line_num
                                    yield (f"data: {json.dumps(ev)}\n\n"
                                           + SAFARI_PAD)
                                    done = True
                                    break
                                # Include offset in event for client tracking
                                ev["offset"] = line_num
                                yield (f"data: {json.dumps(ev)}\n\n"
                                       + SAFARI_PAD)
                            except json.JSONDecodeError:
                                pass
                        line_num += 1

                    if done:
                        break

                    if not chunk:
                        # No new data from file — check session status
                        with _lock:
                            status = _session.get("status", "idle")
                            current_sid = _session.get("id")

                        if current_sid != sid:
                            # Session changed — stop streaming
                            break

                        if status == "completed":
                            # Drain any remaining events
                            remaining = f.read()
                            if remaining:
                                raw_buffer += remaining
                                continue
                            break

                        # Still running — send keepalive
                        yield (f"data: {json.dumps({'type': 'ping'})}\n\n"
                               + SAFARI_PAD)
                        time.sleep(1)

        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'content': str(e)})}\n\n"

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no",
                             "Connection": "keep-alive"})


@app.route("/status")
def status():
    """Current session status."""
    with _lock:
        alive = False
        if _session.get("proc"):
            alive = _session["proc"].poll() is None

        return jsonify({
            "session_id": _session.get("id"),
            "claude_sid": _session.get("claude_sid"),
            "status": _session.get("status", "idle"),
            "event_count": _session.get("event_count", 0),
            "pid": _session.get("pid"),
            "alive": alive,
            "started": _session.get("started"),
            "last_activity": _session.get("last_activity"),
        })


@app.route("/cancel", methods=["POST"])
def cancel():
    """Kill active subprocess."""
    with _lock:
        proc = _session.get("proc")
        if not proc:
            return jsonify({"error": "No active process"}), 404

        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass

        _session["status"] = "completed"
        return jsonify({"cancelled": True})


@app.route("/new-session", methods=["POST"])
def new_session():
    """Clear conversation context — next /send starts fresh."""
    with _lock:
        _session["claude_sid"] = None
    return jsonify({"ok": True})


@app.route("/last-response")
def last_response():
    """Last completed response text for recovery."""
    with _last_response_lock:
        return jsonify(_last_response)


@app.route("/health")
def health():
    """Health check endpoint."""
    return jsonify({"ok": True, "pid": os.getpid(),
                    "uptime_s": round(time.time() - _start_time)})


_start_time = time.time()


# ── WebSocket endpoint ──────────────────────────────────────────────
# Provides real-time bidirectional communication for native iOS app.
# Protocol:
#   Client → Server (JSON):
#     {"type": "message", "text": "...", "session_id": "...", "image_paths": [...]}
#     {"type": "resume", "session_id": "..."}   — resume an existing Claude conversation
#     {"type": "cancel"}                         — kill active subprocess
#     {"type": "new_session"}                    — clear conversation context
#   Server → Client (JSON):
#     On connect: catch-up events from current session, then real-time events
#     Each event: {"type": "text"|"tool"|"cost"|"done"|..., "offset": N, ...}
#     {"type": "ws_error", "content": "..."}     — server-side errors
#     {"type": "session_started", "session_id": "...", "pid": N}

_ws_clients = {}  # {client_id: ws_object}
_ws_clients_lock = threading.Lock()


@sock.route("/ws")
def websocket_handler(ws):
    """WebSocket endpoint — real-time bidirectional Claude communication.

    On connect, sends catch-up events for the current session.
    Then streams new events in real-time as they arrive.
    Accepts commands (message, resume, cancel, new_session) from the client.
    """
    client_id = str(uuid.uuid4())[:8]
    print(f"[{datetime.now().isoformat()}] WS client {client_id} connected",
          flush=True)

    with _ws_clients_lock:
        _ws_clients[client_id] = ws

    # Send immediate welcome so iOS app knows connection is live
    try:
        ws.send(json.dumps({"type": "pong"}))
    except Exception:
        pass

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
                    _ws_send({"type": "session_started",
                              "session_id": result["session_id"],
                              "pid": result["pid"]})
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
                    with _lock:
                        _session["claude_sid"] = resume_sid
                    _ws_send({"type": "resumed", "session_id": resume_sid})
                else:
                    _ws_send({"type": "ws_error",
                              "content": "Missing session_id for resume"})

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
                        _ws_send({"type": "cancelled"})
                    else:
                        _ws_send({"type": "ws_error",
                                  "content": "No active process"})

            elif msg_type == "new_session":
                with _lock:
                    _session["claude_sid"] = None
                _ws_send({"type": "new_session_ok"})

            elif msg_type == "ping":
                _ws_send({"type": "pong"})

            else:
                _ws_send({"type": "ws_error",
                          "content": f"Unknown message type: {msg_type}"})

    except Exception as e:
        # Log ALL disconnect reasons for debugging
        print(f"[{datetime.now().isoformat()}] WS client {client_id} "
              f"exception: {type(e).__name__}: {e}", flush=True)

    finally:
        sender_state["stop"] = True
        # Wake sender so it exits promptly
        with _event_condition:
            _event_condition.notify_all()

        with _ws_clients_lock:
            _ws_clients.pop(client_id, None)

        print(f"[{datetime.now().isoformat()}] WS client {client_id} "
              f"disconnected", flush=True)


# ── PWA static files ──────────────────────────────────────────────
STATIC_DIR = os.path.join(os.path.dirname(os.path.realpath(__file__)), "static")


@app.route("/app")
def pwa_app():
    with open(os.path.join(STATIC_DIR, "app.html")) as f:
        return Response(f.read(), content_type="text/html")


@app.route("/manifest.json")
def pwa_manifest():
    with open(os.path.join(STATIC_DIR, "manifest.json")) as f:
        return Response(f.read(), content_type="application/manifest+json")


@app.route("/sw.js")
def pwa_sw():
    with open(os.path.join(STATIC_DIR, "sw.js")) as f:
        return Response(f.read(), content_type="application/javascript")


@app.route("/offline")
def pwa_offline():
    return Response(
        "<html><body style='background:#1a1a2e;color:#e0e0e0;text-align:center;"
        "padding:40px;font-family:-apple-system,sans-serif'>"
        "<h2>Offline</h2><p>Reconnecting...</p></body></html>",
        content_type="text/html")


@app.route("/icon-192.png")
def pwa_icon_192():
    return _generate_icon(192)


@app.route("/icon-512.png")
def pwa_icon_512():
    return _generate_icon(512)


def _generate_icon(size):
    """Generate a simple SVG-based PNG-substitute icon on the fly."""
    # Return an SVG with the right content-type — browsers accept this for icons
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" viewBox="0 0 {size} {size}">
  <rect width="{size}" height="{size}" rx="{size//6}" fill="#16213e"/>
  <text x="50%" y="54%" dominant-baseline="middle" text-anchor="middle"
        font-family="-apple-system,sans-serif" font-size="{size//3}" font-weight="600"
        fill="#c9a96e">CC</text>
</svg>'''
    return Response(svg, content_type="image/svg+xml")


# ── File upload (for native app image attachments) ──

@app.route("/upload", methods=["POST"])
def upload_file():
    """Accept file upload from native app, return server path."""
    import tempfile
    upload_dir = os.path.join(tempfile.gettempdir(), "claude_uploads")
    os.makedirs(upload_dir, exist_ok=True)

    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "Empty filename"}), 400

    # Save with a unique name to avoid collisions
    import uuid
    ext = os.path.splitext(f.filename)[1] or ".jpg"
    saved_name = f"{uuid.uuid4().hex}{ext}"
    saved_path = os.path.join(upload_dir, saved_name)
    f.save(saved_path)

    return jsonify({"path": saved_path, "filename": f.filename})


# ── Printer status + images (so native app doesn't need wrapper) ──

@app.route("/printer-status")
def printer_status():
    """Serve cached printer status from daemon output."""
    status_file = "/tmp/printer_status/status.json"
    try:
        with open(status_file) as f:
            data = json.load(f)
    except FileNotFoundError:
        return jsonify({"sv08": {"error": "Daemon not running"},
                        "a1": {"error": "Daemon not running"}})
    except Exception as e:
        return jsonify({"sv08": {"error": str(e)},
                        "a1": {"error": str(e)}})

    result = {"sv08": {}, "a1": {}}

    sovol = data.get("printers", {}).get("sovol", {})
    if sovol.get("online"):
        if "state" in sovol:
            sovol["state"] = (sovol["state"] or "unknown").capitalize()
        result["sv08"] = sovol
    else:
        result["sv08"] = {"error": sovol.get("error", "Offline")}

    bambu = data.get("printers", {}).get("bambu", {})
    if bambu.get("online"):
        a1 = dict(bambu)
        a1.setdefault("layer", a1.pop("layer_num", None))
        a1.setdefault("total_layers", a1.pop("total_layer_num", None))
        speed_names = {1: "Silent", 2: "Standard", 3: "Sport", 4: "Ludicrous"}
        a1["speed"] = speed_names.get(a1.get("speed_level"), "?")
        result["a1"] = a1
    else:
        result["a1"] = {"error": bambu.get("error", "Offline")}

    result["daemon_timestamp"] = data.get("timestamp", "")
    return jsonify(result)


@app.route("/printer-image/<name>")
def printer_image(name):
    """Serve camera snapshots and thumbnails."""
    safe_names = {"sovol_camera.jpg", "sovol_thumbnail.png", "a1_camera.jpg"}
    if name not in safe_names:
        return "Not found", 404
    img_dir = "/tmp/printer_status"
    path = os.path.join(img_dir, name)
    if not os.path.exists(path):
        return "Not found", 404
    import mimetypes
    mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
    with open(path, "rb") as f:
        return Response(f.read(), content_type=mime)


@app.route("/file")
def serve_file():
    """Serve files that Claude has read (images only, restricted directories)."""
    import mimetypes
    fpath = request.args.get("path", "")
    if not fpath:
        return "Missing path", 400
    # Resolve to absolute, block path traversal
    fpath = os.path.realpath(fpath)
    # Only allow image files
    if not any(fpath.lower().endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".gif", ".webp")):
        return "Not an image file", 403
    # Only allow files under known safe directories
    safe_prefixes = ["/tmp/", os.path.expanduser("~/Documents/")]
    if not any(fpath.startswith(p) for p in safe_prefixes):
        return "Access denied", 403
    if not os.path.exists(fpath):
        return "Not found", 404
    mime = mimetypes.guess_type(fpath)[0] or "application/octet-stream"
    with open(fpath, "rb") as f:
        return Response(f.read(), content_type=mime)


# ── Printer state monitor + notifications ──

_prev_printer_state = {"sv08": None, "a1": None}
_ai_check_counter = [0]  # Mutable so nested function can modify
_last_obico_alert_ts = [None]  # Track last Obico alert to avoid duplicates
_last_bambu_ai_alert = [0]  # Timestamp of last Bambu AI alert (cooldown)
_last_obico_check = [0]  # Timestamp of last Obico check
_last_bambu_ai_check = [0]  # Timestamp of last Bambu AI check

# Configurable AI check intervals (seconds, 0 = off)
_ai_config = {
    "bambu_interval": 300,   # Default 5 min
    "sv08_interval": 300,    # Default 5 min
}
_ai_config_lock = threading.Lock()


def _check_obico_alerts():
    """Poll Obico API for SV08 failure detection alerts."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
        from printer_config import OBICO_API_TOKEN, OBICO_PRINTER_ID, OBICO_BASE_URL
    except (ImportError, AttributeError):
        return

    if not OBICO_API_TOKEN or not OBICO_PRINTER_ID:
        return  # Not configured yet

    try:
        import urllib.request
        url = f"{OBICO_BASE_URL}/api/v1/printers/{OBICO_PRINTER_ID}/"
        req = urllib.request.Request(url)
        req.add_header("Authorization", f"Token {OBICO_API_TOKEN}")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())

        # Check failure probability
        failure_p = data.get("normalized_p", 0) or 0
        current_print = data.get("current_print") or {}
        alerted_at = current_print.get("alerted_at")

        # Alert if new failure detected (alerted_at changed)
        if alerted_at and alerted_at != _last_obico_alert_ts[0]:
            _last_obico_alert_ts[0] = alerted_at
            confidence_pct = round(failure_p * 100)
            _broadcast_ws({
                "type": "printer_alert",
                "printer": "sv08",
                "event": "failure_detected",
                "message": f"Obico AI detected possible print failure on SV08 Max "
                           f"(confidence: {confidence_pct}%). Check camera immediately.",
            })
            print(f"[{datetime.now().isoformat()}] Obico alert: SV08 failure "
                  f"p={failure_p:.3f}", flush=True)

    except Exception as e:
        # Don't spam logs for connection errors when Obico isn't set up
        if "URLError" not in type(e).__name__:
            print(f"[{datetime.now().isoformat()}] Obico check error: {e}",
                  flush=True)


def _check_bambu_camera_ai(printer_data):
    """Analyse Bambu A1 camera image with Claude Haiku for failure detection."""
    # Only check when A1 is actively printing
    bambu = printer_data.get("a1") or printer_data.get("printers", {}).get("bambu", {})
    a1_state = (bambu.get("state") or "").upper()
    if a1_state != "RUNNING":
        return

    # Cooldown: don't alert more than once per check interval
    with _ai_config_lock:
        interval = _ai_config["bambu_interval"]
    if interval <= 0:
        return
    if time.time() - _last_bambu_ai_alert[0] < interval:
        return

    # Check camera image exists and is fresh
    # Freshness window scales with interval: 2min for 1min, 5min for 5min, 10min for 15min, 30min for 1hr
    freshness_map = {60: 120, 300: 300, 900: 600, 3600: 1800}
    max_age = freshness_map.get(interval, max(120, interval // 2))
    cam_path = "/tmp/printer_status/a1_camera.jpg"
    if not os.path.exists(cam_path):
        return
    age = time.time() - os.path.getmtime(cam_path)
    if age > max_age:
        return

    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
        from printer_config import ANTHROPIC_API_KEY
    except (ImportError, AttributeError):
        return

    if not ANTHROPIC_API_KEY:
        return

    try:
        import anthropic
        import base64

        with open(cam_path, "rb") as f:
            img_data = base64.standard_b64encode(f.read()).decode("utf-8")

        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": img_data,
                        },
                    },
                    {
                        "type": "text",
                        "text": (
                            "You are a 3D print failure detector. Analyse this camera image "
                            "of an FDM 3D printer mid-print. Look for: spaghetti/stringing, "
                            "layer shifting, detachment from bed, blob formation, or any "
                            "other clear print failure. Respond with ONLY one of:\n"
                            "OK - print looks normal\n"
                            "WARN - something looks suspicious but unclear\n"
                            "FAIL - clear print failure detected\n"
                            "Then a brief reason (max 15 words)."
                        ),
                    },
                ],
            }],
        )

        result = response.content[0].text.strip()
        status = result.split()[0].upper() if result else "OK"

        if status in ("FAIL", "WARN"):
            _last_bambu_ai_alert[0] = time.time()
            severity = "failure" if status == "FAIL" else "warning"
            _broadcast_ws({
                "type": "printer_alert",
                "printer": "a1",
                "event": "ai_failure_detected",
                "message": f"AI print analysis ({severity}): {result}",
            })
            print(f"[{datetime.now().isoformat()}] Bambu AI alert: {result}",
                  flush=True)
        else:
            print(f"[{datetime.now().isoformat()}] Bambu AI check: {result}",
                  flush=True)

    except Exception as e:
        print(f"[{datetime.now().isoformat()}] Bambu AI check error: {e}",
              flush=True)
_printer_watches = []  # [{printer, field, op, value, message, id}]
_printer_watches_lock = threading.Lock()


def _broadcast_ws(event_dict):
    """Send event to all connected WebSocket clients."""
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
    print(f"[{datetime.now().isoformat()}] Broadcast: {event_dict.get('message', '')}",
          flush=True)


def _check_printer_state():
    """Check for printer state changes and watch triggers. Runs every 30s."""
    status_file = "/tmp/printer_status/status.json"
    while True:
        try:
            time.sleep(30)
            if not os.path.exists(status_file):
                continue

            with open(status_file) as f:
                data = json.load(f)

            # Check SV08 state changes
            sovol = data.get("printers", {}).get("sovol", {})
            sv08_state = (sovol.get("state") or "unknown").lower()
            old_sv08 = _prev_printer_state["sv08"]
            if old_sv08 and old_sv08 != sv08_state:
                msg = _describe_state_change("SV08 Max", old_sv08, sv08_state, sovol)
                if msg:
                    _broadcast_ws({
                        "type": "printer_alert",
                        "printer": "sv08",
                        "event": "state_change",
                        "old_state": old_sv08,
                        "new_state": sv08_state,
                        "message": msg,
                    })
            _prev_printer_state["sv08"] = sv08_state

            # Check A1 state changes
            bambu = data.get("printers", {}).get("bambu", {})
            a1_state = (bambu.get("state") or "unknown").lower()
            old_a1 = _prev_printer_state["a1"]
            if old_a1 and old_a1 != a1_state:
                msg = _describe_state_change("Bambu A1", old_a1, a1_state, bambu)
                if msg:
                    _broadcast_ws({
                        "type": "printer_alert",
                        "printer": "a1",
                        "event": "state_change",
                        "old_state": old_a1,
                        "new_state": a1_state,
                        "message": msg,
                    })
            _prev_printer_state["a1"] = a1_state

            # Check custom watches
            with _printer_watches_lock:
                triggered = []
                for watch in _printer_watches:
                    printer_data = sovol if watch["printer"] == "sv08" else bambu
                    val = printer_data.get(watch["field"])
                    if val is not None and _check_op(val, watch["op"], watch["value"]):
                        _broadcast_ws({
                            "type": "printer_alert",
                            "printer": watch["printer"],
                            "event": "watch_triggered",
                            "message": watch["message"],
                            "watch_id": watch["id"],
                        })
                        triggered.append(watch["id"])
                _printer_watches[:] = [w for w in _printer_watches
                                       if w["id"] not in triggered]

            # AI failure detection checks (configurable intervals)
            now = time.time()
            with _ai_config_lock:
                sv08_interval = _ai_config["sv08_interval"]
                bambu_interval = _ai_config["bambu_interval"]
            if sv08_interval > 0 and now - _last_obico_check[0] >= sv08_interval:
                _last_obico_check[0] = now
                _check_obico_alerts()
            if bambu_interval > 0 and now - _last_bambu_ai_check[0] >= bambu_interval:
                _last_bambu_ai_check[0] = now
                _check_bambu_camera_ai(data)

        except Exception as e:
            print(f"[{datetime.now().isoformat()}] State monitor error: {e}",
                  flush=True)


def _describe_state_change(name, old, new, data):
    """Generate human-readable notification for state transitions."""
    new_l = new.lower()
    old_l = old.lower()
    filename = data.get("filename", "")
    short_name = filename[:40] + "..." if len(filename) > 40 else filename

    if "print" in new_l and "print" not in old_l:
        return f"{name}: Print started — {short_name}" if short_name else f"{name}: Print started"
    if new_l in ("complete", "standby", "ready") and "print" in old_l:
        return f"{name}: Print complete!"
    if "paus" in new_l and "print" in old_l:
        return f"{name}: Print paused"
    if "print" in new_l and "paus" in old_l:
        return f"{name}: Print resumed"
    if "error" in new_l or "fault" in new_l:
        return f"{name}: Error detected!"
    if "cancel" in new_l:
        return f"{name}: Print cancelled"
    # Only notify on meaningful transitions
    return None


def _check_op(val, op, target):
    """Evaluate a comparison operation."""
    try:
        val = float(val)
        target = float(target)
        if op == ">=":
            return val >= target
        if op == "<=":
            return val <= target
        if op == ">":
            return val > target
        if op == "<":
            return val < target
        if op == "==":
            return val == target
    except (ValueError, TypeError):
        pass
    return False


@app.route("/printer-watch", methods=["POST"])
def add_printer_watch():
    """Add a custom notification watch. Body: {printer, field, op, value, message}"""
    body = request.get_json(force=True)
    watch = {
        "id": str(uuid.uuid4())[:8],
        "printer": body.get("printer", "sv08"),
        "field": body.get("field", "progress"),
        "op": body.get("op", ">="),
        "value": body.get("value", 100),
        "message": body.get("message", "Watch triggered"),
    }
    with _printer_watches_lock:
        _printer_watches.append(watch)
    return jsonify({"ok": True, "watch": watch})


@app.route("/printer-watches", methods=["GET"])
def list_printer_watches():
    """List active watches."""
    with _printer_watches_lock:
        return jsonify({"watches": list(_printer_watches)})


@app.route("/printer-watch/<watch_id>", methods=["DELETE"])
def delete_printer_watch(watch_id):
    """Delete a watch by ID."""
    with _printer_watches_lock:
        _printer_watches[:] = [w for w in _printer_watches if w["id"] != watch_id]
    return jsonify({"ok": True})


# ── Object skip (EXCLUDE_OBJECT via Moonraker) ──

@app.route("/printer-objects")
def printer_objects():
    """Get EXCLUDE_OBJECT data from Klipper via Moonraker."""
    import urllib.request
    moonraker = "http://192.168.87.52:7125"
    try:
        url = f"{moonraker}/printer/objects/query?exclude_object"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        eo = data.get("result", {}).get("status", {}).get("exclude_object", {})
        return jsonify({
            "objects": eo.get("objects", []),
            "current_object": eo.get("current_object"),
            "excluded_objects": eo.get("excluded_objects", []),
        })
    except Exception as e:
        return jsonify({"error": str(e), "objects": []}), 502


@app.route("/exclude-object", methods=["POST"])
def exclude_object():
    """Exclude an object from the current print via Klipper EXCLUDE_OBJECT."""
    import urllib.request
    moonraker = "http://192.168.87.52:7125"
    body = request.get_json(force=True)
    name = body.get("name", "")
    if not name:
        return jsonify({"error": "Missing object name"}), 400
    try:
        gcode = f"EXCLUDE_OBJECT NAME={name}"
        payload = json.dumps({"script": gcode}).encode()
        req = urllib.request.Request(
            f"{moonraker}/printer/gcode/script",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            resp.read()
        return jsonify({"ok": True, "excluded": name})
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/work-status")
def work_status_proxy():
    """Proxy to wrapper's work-status endpoint so native app only needs one port."""
    import urllib.request
    try:
        req = urllib.request.Request("http://127.0.0.1:8080/work-status", method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = resp.read()
            return Response(data, content_type="application/json")
    except Exception as e:
        return jsonify({"error": str(e), "setup_required": True}), 502


@app.route("/slack-messages")
def slack_messages_proxy():
    """Proxy to wrapper's slack-messages endpoint."""
    import urllib.request
    try:
        req = urllib.request.Request("http://127.0.0.1:8080/slack-messages", method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = resp.read()
            return Response(data, content_type="application/json")
    except Exception as e:
        return jsonify({"error": str(e), "messages": []}), 502


@app.route("/governors-reset", methods=["POST"])
def governors_reset():
    """Signal the governors Streamlit app to reset chat."""
    flag = "/tmp/governors_reset_flag"
    with open(flag, "w") as f:
        f.write("reset")
    return jsonify({"ok": True})


@app.route("/ai-check-config", methods=["POST", "GET"])
def ai_check_config():
    """Configure AI failure detection intervals."""
    if request.method == "GET":
        with _ai_config_lock:
            return jsonify(_ai_config)
    data = request.get_json(silent=True) or {}
    with _ai_config_lock:
        if "bambu_interval" in data:
            _ai_config["bambu_interval"] = int(data["bambu_interval"])
        if "sv08_interval" in data:
            _ai_config["sv08_interval"] = int(data["sv08_interval"])
        print(f"[{datetime.now().isoformat()}] AI config updated: {_ai_config}", flush=True)
        return jsonify(_ai_config)


@app.route("/work-search")
def work_search_proxy():
    """Proxy to wrapper's work-search endpoint."""
    import urllib.request, urllib.parse
    q = request.args.get("q", "")
    try:
        url = f"http://127.0.0.1:8080/work-search?q={urllib.parse.quote(q)}"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read()
            return Response(data, content_type="application/json")
    except Exception as e:
        return jsonify({"error": str(e), "results": []}), 502


@app.after_request
def add_headers(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    # Service worker scope
    if response.content_type and 'javascript' in response.content_type:
        response.headers['Service-Worker-Allowed'] = '/'
    return response


def _signal_handler(signum, frame):
    """Log signal before exiting — helps diagnose unexpected daemon restarts."""
    sig_name = signal.Signals(signum).name
    uptime = round(time.time() - _start_time)
    print(f"[{datetime.now().isoformat()}] Received {sig_name} (uptime {uptime}s), "
          f"shutting down", flush=True)

    # Kill any active Claude subprocess cleanly
    with _lock:
        proc = _session.get("proc")
        if proc and proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except Exception:
                pass

    sys.exit(0)


if __name__ == "__main__":
    # Register signal handlers for diagnostics
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGHUP, _signal_handler)

    print(f"[{datetime.now().isoformat()}] Conversation server starting "
          f"(PID {os.getpid()})", flush=True)

    # Start printer state monitor in background
    monitor = threading.Thread(target=_check_printer_state, daemon=True)
    monitor.start()
    print(f"[{datetime.now().isoformat()}] Printer state monitor started", flush=True)

    app.run(host="0.0.0.0", port=8081, threaded=True)
