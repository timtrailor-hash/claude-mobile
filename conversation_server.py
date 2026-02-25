#!/usr/bin/env python3
"""Conversation server — owns Claude CLI subprocess lifecycle.

Runs as a persistent launchd daemon on port 8081.
The wrapper (app.py) proxies to this server.
Sessions survive wrapper restarts and iOS Safari connection drops.

Routes:
  POST /send          — send message, spawn Claude CLI
  GET  /stream?offset=N — SSE stream from event N (reconnectable)
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

from flask import Flask, Response, jsonify, request

app = Flask(__name__)

# ── Config ──
WORK_DIR = os.path.expanduser("~/Documents/Claude code")
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


def _events_path(session_id):
    return os.path.join(SESSIONS_DIR, session_id, "events.jsonl")


def _append_event(events_file, event_data):
    """Append an SSE event to the given events file.

    Takes the file path explicitly (not from _session) to avoid race
    conditions when a new session starts while the old reader is finishing.
    """
    line = json.dumps(event_data) + "\n"
    try:
        with open(events_file, "a") as f:
            f.write(line)
    except Exception:
        pass


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

    except Exception as e:
        _append_event(events_file, {"type": "error", "content": str(e)})

    # Write terminal done event
    _append_event(events_file, {"type": "done"})

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


@app.route("/send", methods=["POST"])
def send():
    """Send a message to Claude. Spawns CLI subprocess."""
    data = request.json or {}
    message = data.get("message", "").strip()
    image_paths = data.get("image_paths", [])
    client_session_id = data.get("session_id")

    if not message:
        return jsonify({"error": "Empty message"}), 400

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
        "--max-turns", "20",
        "--output-format", "stream-json",
        "--verbose"
    ]

    # Resume existing conversation
    resume_sid = client_session_id or _session.get("claude_sid")
    if resume_sid:
        cmd.extend(["--resume", resume_sid])

    # Clean environment
    env = os.environ.copy()
    for key in ["CLAUDE_CODE_ENTRYPOINT", "CLAUDECODE", "CLAUDE_CODE_SESSION"]:
        env.pop(key, None)
    env["HOME"] = os.path.expanduser("~")
    env["PATH"] = "/Users/timtrailor/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    env.pop("ANTHROPIC_API_KEY", None)

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

        return jsonify({"session_id": sid, "pid": proc.pid})

    except FileNotFoundError:
        with _lock:
            _session["status"] = "error"
        return jsonify({"error": "Claude CLI not found"}), 500
    except Exception as e:
        with _lock:
            _session["status"] = "error"
        return jsonify({"error": str(e)}), 500


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
    app.run(host="127.0.0.1", port=8081, threaded=True)
