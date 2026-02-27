#!/usr/bin/env python3
"""Conversation server — owns Claude CLI subprocess lifecycle.

Runs as a persistent launchd daemon on port 8081.
The wrapper (app.py) proxies to this server.
Sessions survive wrapper restarts and iOS Safari connection drops.

Architecture (v2 — persistent subprocess with permission bridge):
  Claude CLI runs as a persistent subprocess with bidirectional stream-json.
  Messages are written to stdin, events read from stdout.
  Permission requests are bridged to the iOS app via an MCP approval server.

Routes:
  POST /send          — send message to persistent Claude subprocess
  GET  /stream?offset=N — SSE stream from event N (reconnectable)
  WS   /ws            — WebSocket: real-time bidirectional (native iOS app)
  GET  /status        — current session status
  POST /cancel        — kill active subprocess
  POST /new-session   — clear session (start fresh conversation)
  GET  /last-response — last completed response text
  POST /internal/permission-request — MCP approval server long-poll
"""

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime

# Add shared_utils to path
sys.path.insert(0, os.path.expanduser("~/Documents/Claude code"))
from shared_utils import env_for_claude_cli, work_dir, configure_logging

_log = configure_logging(
    "conversation_server",
    log_file="/tmp/conversation_server_structured.log",
)

# Auth token — imported from credentials.py (gitignored), fallback to None (no auth)
try:
    from credentials import AUTH_TOKEN
except ImportError:
    AUTH_TOKEN = None

from flask import Flask, Response, jsonify, request
from flask_sock import Sock

app = Flask(__name__)
# Enable protocol-level WebSocket pings every 25s. simple-websocket's
# internal thread handles ping/pong properly — URLSessionWebSocketTask
# responds to pings automatically. This replaces our custom keepalive.
app.config['SOCK_SERVER_OPTIONS'] = {'ping_interval': 25}
sock = Sock(app)


@app.before_request
def check_auth():
    """Require auth token for non-localhost requests (iOS app via Tailscale)."""
    if not AUTH_TOKEN:
        return  # No token configured, skip auth
    # Trust localhost (MCP server, health check, etc.)
    if request.remote_addr in ('127.0.0.1', '::1'):
        return
    # Check Authorization header or query param
    auth = request.headers.get('Authorization', '')
    if auth == f'Bearer {AUTH_TOKEN}':
        return
    if request.args.get('token') == AUTH_TOKEN:
        return
    return jsonify({"error": "Unauthorized"}), 401

# ── Config ──
WORK_DIR = work_dir()
SESSIONS_DIR = "/tmp/claude_sessions"
os.makedirs(SESSIONS_DIR, exist_ok=True)

MCP_CONFIG = os.path.join(os.path.dirname(os.path.realpath(__file__)),
                          "mcp-approval.json")

# ── Session state (single active conversation) ──
_lock = threading.Lock()
_session = {
    "id": None,            # Current turn UUID (changes per message)
    "claude_sid": None,    # Claude conversation session ID
    "proc": None,          # subprocess.Popen (persistent)
    "stdin_pipe": None,    # stdin of persistent subprocess
    "pid": None,           # PID (survives proc object loss)
    "events_file": None,   # Current turn's events (JSONL, one per line)
    "event_count": 0,      # Number of events written for current turn
    "status": "idle",      # idle | running | completed | error
    "started": None,
    "last_activity": None,
    "reader_running": False,  # Is the reader thread active?
    "generation": 0,       # Incremented on each subprocess spawn; prevents stale reader cleanup
}

# Last completed response for recovery
_last_response = {"text": "", "cost": "", "session_id": None, "seq": 0}
_last_response_lock = threading.Lock()

# Condition variable — signalled every time _append_event writes a line.
# WebSocket handlers wait on this instead of polling with sleep.
_event_condition = threading.Condition()

# ── Permission bridge ──
# Pending permission requests from MCP approval server.
# Key: request_id, Value: threading.Event + response dict
_permission_requests = {}  # {request_id: {"event": Event, "response": None, "data": {...}}}
_permission_lock = threading.Lock()

# ── Permission level ──
# Configurable from iOS Settings tab. Controls which tools auto-approve vs
# prompt via the MCP permission bridge. Most → least permissive:
#
#   "approve_all"     → everything auto-approved, no prompts
#   "terminal_only"   → reads+edits+web auto, only terminal commands prompt
#   "terminal_and_web"→ reads+edits auto, terminal+web prompt
#   "most_actions"    → reads auto, writes/terminal/web all prompt
_permission_level = "terminal_only"  # Default: prompt only for terminal commands


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
    except Exception as e:
        _log.error("Failed to write event: %s", e)

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
                tool_name = block.get("name", "")
                # Filter out the MCP approval tool — permission flow is handled
                # separately via WebSocket permission_request events
                if tool_name == "mcp__approval__approve":
                    yield {"type": "activity", "content": "requesting_permission"}
                    continue
                summary = _format_tool(tool_name, block.get("input", {}))
                yield {"type": "tool", "content": summary}
                # Detect image file reads and emit an image event
                if tool_name == "Read":
                    fpath = block.get("input", {}).get("file_path", "")
                    if fpath and any(fpath.lower().endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".gif", ".webp")):
                        yield {"type": "image", "content": f"/file?path={fpath}"}

    elif etype == "tool":
        tool_name = event.get("tool", "")
        # Filter out MCP approval tool results
        if tool_name and tool_name != "mcp__approval__approve":
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


def _keepalive_thread(session_id, events_file_arg, activity_ref, done_flag):
    """Writes periodic ping events to prevent Safari idle-kill.

    During tool execution (Bash, Read, etc.), Claude CLI goes silent for
    10-30s. Safari kills idle SSE connections. This thread writes a ping
    every 10s when no real events have flowed, keeping the stream alive.

    For persistent subprocess mode, events_file_arg is None and we read
    the current events file from session state.
    """
    while not done_flag[0]:
        time.sleep(10)
        if done_flag[0]:
            break
        idle = time.time() - activity_ref[0]
        if idle >= 8:
            ef = events_file_arg
            if ef is None:
                with _lock:
                    ef = _session.get("events_file")
            if ef:
                _append_event(ef, {"type": "ping"})
                with _lock:
                    _session["last_activity"] = time.time()


def _reader_thread(proc, generation):
    """Background thread: reads from persistent Claude CLI subprocess stdout.

    Runs for the entire lifetime of the subprocess. Routes events to the
    current turn's events file. When a 'result' event arrives, the turn is
    complete — a 'done' event is emitted and the session is marked idle.

    The subprocess stays alive between turns (messages). On process exit
    (normal or crash), marks session as error and cleans up.

    The generation parameter prevents a stale reader from overwriting state
    that belongs to a newer subprocess (e.g. after cancel + respawn).
    """
    spawn_time = time.time()
    first_byte_logged = False

    # Per-turn state (reset on each new turn)
    response_text = ""
    response_cost = ""
    last_event_type = None

    # Keepalive state
    activity_ref = [time.time()]
    done_flag = [False]

    def _current_events_file():
        with _lock:
            return _session.get("events_file")

    def _current_turn_id():
        with _lock:
            return _session.get("id")

    # Start keepalive thread for the persistent subprocess
    ka = threading.Thread(
        target=_keepalive_thread,
        args=("persistent", None, activity_ref, done_flag),
        daemon=True)
    ka.start()

    try:
        for raw_line in proc.stdout:
            if not first_byte_logged:
                elapsed = time.time() - spawn_time
                _log.info("[persistent] First output after %.1fs", elapsed)
                first_byte_logged = True

            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue

            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            etype = event.get("type", "")

            # Extract claude session ID from init event
            if etype == "system" and event.get("subtype") == "init":
                claude_sid = event.get("session_id", "")
                with _lock:
                    _session["claude_sid"] = claude_sid

            # Get current turn's events file
            events_file = _current_events_file()
            if not events_file:
                # No active turn — skip (shouldn't happen in normal flow)
                continue

            # Process and write events
            for ev in _process_event(event):
                _append_event(events_file, ev)
                activity_ref[0] = time.time()

                with _lock:
                    _session["event_count"] = _session.get("event_count", 0) + 1
                    _session["last_activity"] = time.time()

                if ev.get("type") == "text":
                    response_text += ev.get("content", "")
                elif ev.get("type") == "cost":
                    response_cost = ev.get("content", "")
                last_event_type = ev.get("type")

            # 'result' event = turn complete
            if etype == "result":
                truncated = last_event_type == "tool"
                _append_event(events_file, {"type": "done", "truncated": truncated})

                with _lock:
                    _session["status"] = "completed"

                # Save last response
                if response_text:
                    with _last_response_lock:
                        _last_response["text"] = response_text
                        _last_response["cost"] = response_cost
                        _last_response["session_id"] = _session.get("claude_sid")
                        _last_response["seq"] += 1

                turn_id = _current_turn_id()
                _log.info("[turn:%s] Turn complete", turn_id)

                # Reset per-turn state for next message
                response_text = ""
                response_cost = ""
                last_event_type = None

    except Exception as e:
        events_file = _current_events_file()
        if events_file:
            _append_event(events_file, {"type": "error", "content": str(e)})
        _log.error("Reader thread error: %s", e)
        with _last_claude_error_lock:
            _last_claude_error["message"] = str(e)
            _last_claude_error["timestamp"] = datetime.now().isoformat()

    done_flag[0] = True

    # Subprocess exited — write done if turn was in progress.
    # Only clear session state if our generation still matches — a newer
    # subprocess may have already been spawned (e.g. after cancel + respawn).
    with _lock:
        if _session.get("generation") == generation:
            if _session.get("status") == "running":
                events_file = _session.get("events_file")
                if events_file:
                    _append_event(events_file, {"type": "done", "truncated": False})
                _session["status"] = "completed"
            _session["proc"] = None
            _session["stdin_pipe"] = None
            _session["pid"] = None
            _session["reader_running"] = False
        else:
            _log.info("Reader thread (gen %d) skipping cleanup — newer gen %s active",
                      generation, _session.get('generation'))

    _log.info("Reader thread (gen %d) exited (subprocess ended)", generation)


_spawn_lock = threading.Lock()  # Serialises check-and-spawn in _ensure_subprocess


def _ensure_subprocess():
    """Ensure a persistent Claude CLI subprocess is running. Spawn if needed.

    The subprocess uses bidirectional stream-json: messages go in via stdin,
    events come out via stdout. It stays alive across multiple messages.
    Permission requests are handled via the MCP approval server.

    Uses _spawn_lock to prevent the race between "is it running?" check and
    the Popen call — without this, two threads could both see "not running"
    and spawn duplicate subprocesses.

    Returns True if subprocess is running, False on error.
    """
    with _spawn_lock:
        with _lock:
            if (_session.get("proc") and _session["proc"].poll() is None
                    and _session.get("reader_running")):
                return True  # Already running

        # Build command — persistent subprocess with bidirectional streaming
        #
        # --setting-sources "project" loads CLAUDE.md and MEMORY.md but skips
        # "user" (which loads cloud MCP servers that conflict with --mcp-config)
        # and "local" (.claude/settings.local.json with permissive allowedTools).
        #
        # We use explicit --allowedTools per tier so safe tools are auto-approved
        # at Layer 1 (static rules) BEFORE the --permission-prompt-tool (Layer 3)
        # gets called. Without this, every tool call routes through the MCP bridge.
        #
        # Permission tiers (most → least permissive):
        #   approve_all      → bypassPermissions — never prompt
        #   terminal_only    → reads+edits+web auto, only terminal commands prompt
        #   terminal_and_web → reads+edits auto, terminal+web prompt
        #   most_actions     → reads auto, writes/terminal/web all prompt
        level = _permission_level

        # Tool groups for --allowedTools whitelist
        READ_TOOLS = "Read Glob Grep"
        EDIT_TOOLS = "Read Write Edit Glob Grep"
        EDIT_AND_WEB_TOOLS = "Read Write Edit Glob Grep WebFetch WebSearch Task"

        cmd = [
            "claude", "-p",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--model", "claude-opus-4-6",
            "--max-turns", "200",
            "--verbose",
            "--setting-sources", "project",
        ]

        if level == "approve_all":
            cmd += ["--permission-mode", "bypassPermissions"]
        elif level == "terminal_only":
            # Auto-approve file ops + web, prompt for terminal only
            cmd += [
                "--permission-mode", "default",
                "--allowedTools", EDIT_AND_WEB_TOOLS,
                "--permission-prompt-tool", "mcp__approval__approve",
                "--mcp-config", MCP_CONFIG,
                "--strict-mcp-config",
            ]
        elif level == "terminal_and_web":
            # Auto-approve file ops, prompt for terminal + web
            cmd += [
                "--permission-mode", "default",
                "--allowedTools", EDIT_TOOLS,
                "--permission-prompt-tool", "mcp__approval__approve",
                "--mcp-config", MCP_CONFIG,
                "--strict-mcp-config",
            ]
        else:  # most_actions
            # Auto-approve reads only, prompt for writes/terminal/web
            cmd += [
                "--permission-mode", "default",
                "--allowedTools", READ_TOOLS,
                "--permission-prompt-tool", "mcp__approval__approve",
                "--mcp-config", MCP_CONFIG,
                "--strict-mcp-config",
            ]

        cmd += [
            "--append-system-prompt",
            "You are running in a mobile chat app but have FULL terminal capabilities. "
            "Always run commands directly (bash, ping, curl, file reads, etc.) instead "
            "of suggesting the user run them. You have full access to the filesystem, "
            "network, and all tools — use them proactively just as you would in an "
            "interactive terminal session. Never ask the user to run a command when "
            "you can run it yourself.",
        ]

        allowed_map = {
            "approve_all": "all",
            "terminal_only": EDIT_AND_WEB_TOOLS,
            "terminal_and_web": EDIT_TOOLS,
            "most_actions": READ_TOOLS,
        }
        _log.info("Permission level: %s (allowed: %s)", level, allowed_map.get(level, '?'))

        # Clean environment — subscription auth only, no API key leakage
        env = env_for_claude_cli()

        try:
            stderr_path = os.path.join(SESSIONS_DIR, "claude_stderr.log")
            stderr_handle = open(stderr_path, "ab")

            try:
                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=stderr_handle,
                    cwd=WORK_DIR,
                    env=env,
                    preexec_fn=os.setsid
                )
            finally:
                stderr_handle.close()

            with _lock:
                _session["generation"] += 1
                _session["proc"] = proc
                _session["stdin_pipe"] = proc.stdin
                _session["pid"] = proc.pid
                _session["reader_running"] = True
                gen = _session["generation"]

            # Start persistent reader thread (with generation for stale-cleanup guard)
            reader = threading.Thread(
                target=_reader_thread,
                args=(proc, gen),
                daemon=True)
            reader.start()

            _log.info("Spawned persistent Claude subprocess (PID %d, gen %d)", proc.pid, gen)
            return True

        except FileNotFoundError:
            _log.error("Claude CLI not found")
            return False
        except Exception as e:
            _log.error("Failed to spawn subprocess: %s", e)
            return False


def _start_session(message, image_paths=None, client_session_id=None):
    """Send a message to the persistent Claude subprocess.

    Creates a new turn (events file) and writes the message to stdin.
    Returns (result_dict, error_string). On success error_string is None.
    """
    image_paths = image_paths or []

    # Ensure subprocess is running
    if not _ensure_subprocess():
        return None, "Failed to start Claude subprocess"

    # Prepare new turn
    sid = str(uuid.uuid4())[:8]
    session_dir = os.path.join(SESSIONS_DIR, sid)
    os.makedirs(session_dir, exist_ok=True)
    events_file = os.path.join(session_dir, "events.jsonl")
    open(events_file, "w").close()

    with _lock:
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

    # Write message to stdin as NDJSON (stream-json input format)
    ndjson_msg = json.dumps({
        "type": "user",
        "message": {
            "role": "user",
            "content": message,
        }
    })

    try:
        with _lock:
            stdin_pipe = _session.get("stdin_pipe")
        if not stdin_pipe:
            return None, "No stdin pipe available"

        stdin_pipe.write((ndjson_msg + "\n").encode("utf-8"))
        stdin_pipe.flush()
    except (BrokenPipeError, OSError) as e:
        # Subprocess died — try respawn on next message
        with _lock:
            _session["proc"] = None
            _session["stdin_pipe"] = None
        return None, f"Subprocess pipe error: {e}"

    _log.info("[turn:%s] Message sent to persistent subprocess", sid)

    return {"session_id": sid, "pid": _session.get("pid")}, None


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


@app.route("/permission-level")
def get_permission_level():
    """Return current permission level."""
    return jsonify({"level": _permission_level})


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
        _session["proc"] = None
        _session["stdin_pipe"] = None
        return jsonify({"cancelled": True})


@app.route("/new-session", methods=["POST"])
def new_session():
    """Kill subprocess and clear session — next /send starts fresh."""
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
        _session["claude_sid"] = None
        _session["proc"] = None
        _session["stdin_pipe"] = None
        _session["pid"] = None
        _session["status"] = "idle"
    return jsonify({"ok": True})


# ── Permission bridge endpoint ──────────────────────────────────────

@app.route("/internal/permission-request", methods=["POST"])
def permission_request():
    """Long-poll endpoint called by MCP approval server.

    Receives a permission request, pushes it to iOS clients via WebSocket,
    then blocks until the user responds (or times out after 120s).
    """
    data = request.get_json(force=True)
    request_id = data.get("request_id", str(uuid.uuid4())[:8])
    tool_name = data.get("tool_name", "")
    tool_input = data.get("input", {})
    summary = data.get("summary", f"Use tool: {tool_name}")

    _log.info("Permission request %s: %s — %s", request_id, tool_name, summary)

    # Create a pending request with an Event for synchronisation
    wait_event = threading.Event()
    with _permission_lock:
        _permission_requests[request_id] = {
            "event": wait_event,
            "response": None,
            "data": data,
        }

    # Push to all WebSocket clients
    _broadcast_ws({
        "type": "permission_request",
        "request_id": request_id,
        "tool_name": tool_name,
        "input": tool_input,
        "summary": summary,
    })

    # Block until user responds or timeout
    responded = wait_event.wait(timeout=120)

    with _permission_lock:
        req = _permission_requests.pop(request_id, None)

    if not responded or not req or not req.get("response"):
        # Timeout — deny by default
        _log.warning("Permission %s: timed out, denying", request_id)
        return jsonify({"allow": False, "message": "Permission request timed out"})

    response = req["response"]
    allow = response.get("allow", False)
    _log.info("Permission %s: %s", request_id, 'allowed' if allow else 'denied')

    if allow:
        return jsonify({
            "allow": True,
            "updatedInput": response.get("updatedInput", tool_input),
        })
    else:
        return jsonify({
            "allow": False,
            "message": response.get("message", "User denied permission"),
        })


@app.route("/last-response")
def last_response():
    """Last completed response text for recovery."""
    with _last_response_lock:
        return jsonify(_last_response)


_start_time = time.time()
# Track WS reconnects (rolling 5-min window)
_ws_connect_times = []  # timestamps of WS connections
_ws_connect_lock = threading.Lock()
# Track last Claude CLI error
_last_claude_error = {"message": "", "timestamp": None}
_last_claude_error_lock = threading.Lock()


@app.route("/health")
def health():
    """Extended health check endpoint for SystemHealthView."""
    now = time.time()
    uptime = round(now - _start_time)

    # Claude auth status
    auth_status = "unknown"
    with _lock:
        proc = _session.get("proc")
        if proc and proc.poll() is None:
            auth_status = "logged_in"
        elif _session.get("status") == "error":
            auth_status = "error"
        else:
            auth_status = "idle"

    # Thread health
    thread_health = {}
    for t in threading.enumerate():
        if t.name and t.name != "MainThread":
            thread_health[t.name] = "alive" if t.is_alive() else "dead"

    # WS reconnects in last 5 minutes
    with _ws_connect_lock:
        cutoff = now - 300
        _ws_connect_times[:] = [t for t in _ws_connect_times if t > cutoff]
        reconnect_count = len(_ws_connect_times)

    # Work cache age
    work_cache_age = None
    try:
        status_file = "/tmp/printer_status/status.json"
        if os.path.exists(status_file):
            work_cache_age = round(now - os.path.getmtime(status_file))
    except OSError:
        pass

    # Last claude error
    with _last_claude_error_lock:
        last_error = _last_claude_error.copy()

    # ttyd check — accepts 401 (auth required) as "running"
    ttyd_ok = False
    try:
        import urllib.request
        req = urllib.request.Request("http://127.0.0.1:7681", method="HEAD")
        with urllib.request.urlopen(req, timeout=2):
            ttyd_ok = True
    except urllib.error.HTTPError as he:
        # 401 means ttyd is running but wants auth — that's fine
        ttyd_ok = (he.code == 401)
    except Exception:
        pass

    return jsonify({
        "ok": True,
        "pid": os.getpid(),
        "uptime_s": uptime,
        "claude_auth_status": auth_status,
        "thread_health": thread_health,
        "ws_reconnect_count_5min": reconnect_count,
        "work_cache_age_seconds": work_cache_age,
        "last_claude_error": last_error["message"],
        "ttyd_ok": ttyd_ok,
    })


@app.route("/terminal-new-window", methods=["POST"])
def terminal_new_window():
    """Create a new tmux window in the claude-terminal session."""
    try:
        result = subprocess.run(
            ["/opt/homebrew/bin/tmux", "new-window", "-t", "claude-terminal"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            # Auto-start claude CLI in the new window
            import time
            time.sleep(0.5)
            subprocess.run(
                ["/opt/homebrew/bin/tmux", "send-keys", "-t", "claude-terminal", "claude", "Enter"],
                capture_output=True, text=True, timeout=5
            )
            _log.info("Created new tmux window in claude-terminal (claude auto-started)")
            return jsonify({"ok": True})
        else:
            return jsonify({"ok": False, "error": result.stderr.strip()}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


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
    _log.info("WS client %s connected", client_id)

    with _ws_connect_lock:
        _ws_connect_times.append(time.time())

    with _ws_clients_lock:
        _ws_clients[client_id] = ws

    # Check auth for non-localhost WebSocket clients
    if ws.environ.get('REMOTE_ADDR') not in ('127.0.0.1', '::1'):
        token_param = request.args.get('token', '')
        if AUTH_TOKEN and token_param != AUTH_TOKEN:
            try:
                ws.send(json.dumps({"type": "ws_error", "content": "Unauthorized \u2014 check auth token in Settings"}))
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
                    ws.send(json.dumps({
                        "type": "permission_request",
                        "request_id": req_id,
                        "tool_name": data.get("tool_name", ""),
                        "input": data.get("input", {}),
                        "summary": data.get("summary", "Use a tool"),
                    }))
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
                        _session["proc"] = None
                        _session["stdin_pipe"] = None
                        _ws_send({"type": "cancelled"})
                    else:
                        _ws_send({"type": "ws_error",
                                  "content": "No active process"})

            elif msg_type == "new_session":
                # Kill persistent subprocess so next message starts fresh
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
                    _session["claude_sid"] = None
                    _session["proc"] = None
                    _session["stdin_pipe"] = None
                    _session["pid"] = None
                    _session["status"] = "idle"
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
                    _ws_send({"type": "permission_acknowledged",
                              "request_id": req_id})
                else:
                    # Stale request (already handled or timed out) — not an error
                    _log.info("Permission %s: stale (already handled/expired), ignoring", req_id)
                    _ws_send({"type": "permission_acknowledged",
                              "request_id": req_id,
                              "stale": True})

            elif msg_type == "set_permission_level":
                # iOS Settings changed the permission level
                global _permission_level
                new_level = msg.get("level", "moderate")
                if new_level not in ("approve_all", "terminal_only", "terminal_and_web", "most_actions"):
                    _ws_send({"type": "ws_error",
                              "content": f"Invalid permission level: {new_level}"})
                    continue

                old_level = _permission_level
                _permission_level = new_level
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
                _ws_send({"type": "ws_error",
                          "content": f"Unknown message type: {msg_type}"})

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
    # Resolve prefixes too — macOS /tmp is a symlink to /private/tmp
    safe_prefixes = [os.path.realpath("/tmp/") + "/", os.path.realpath(os.path.expanduser("~/Documents/")) + "/"]
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
            _log.warning("Obico alert: SV08 failure p=%.3f", failure_p)

    except Exception as e:
        # Don't spam logs for connection errors when Obico isn't set up
        if "URLError" not in type(e).__name__:
            _log.warning("Obico check error: %s", e)


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
            _log.warning("Bambu AI alert: %s", result)
        else:
            _log.info("Bambu AI check: %s", result)

    except Exception as e:
        _log.warning("Bambu AI check error: %s", e)
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
    _log.info("Broadcast: %s", event_dict.get('message', ''))


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

            # Check for alerts from printer_status_fetch (method changes,
            # connection failures, etc.)
            _check_printer_alert_file()

        except Exception as e:
            _log.warning("State monitor error: %s", e)


_PRINTER_ALERT_FILE = "/tmp/printer_status/printer_alerts.jsonl"
_last_alert_pos = [0]  # File offset — only read new lines


def _check_printer_alert_file():
    """Read new alerts written by printer_status_fetch and broadcast them.

    The fetch process appends alerts (method changes, connection lost/restored)
    to printer_alerts.jsonl. We track our read position and only broadcast
    new entries, then truncate the file to prevent unbounded growth.
    """
    if not os.path.exists(_PRINTER_ALERT_FILE):
        return

    try:
        with open(_PRINTER_ALERT_FILE, "r") as f:
            f.seek(_last_alert_pos[0])
            new_lines = f.readlines()
            _last_alert_pos[0] = f.tell()

        for line in new_lines:
            line = line.strip()
            if not line:
                continue
            try:
                alert = json.loads(line)
                # Broadcast to all connected WebSocket clients
                _broadcast_ws(alert)
            except (json.JSONDecodeError, KeyError):
                continue

        # Truncate if file is getting large (> 100KB)
        try:
            if os.path.getsize(_PRINTER_ALERT_FILE) > 100_000:
                with open(_PRINTER_ALERT_FILE, "w") as f:
                    pass  # truncate
                _last_alert_pos[0] = 0
        except OSError:
            pass

    except Exception as e:
        _log.warning("Alert file check error: %s", e)


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
    # Sanitize name to prevent gcode injection — only allow safe characters
    name = re.sub(r'[^a-zA-Z0-9_.\-]', '', name)
    if not name:
        return jsonify({"error": "Invalid object name"}), 400
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


# ── Work status cache (background thread) ──────────────────────────────────
_work_cache = {
    "work_status": None,
    "slack_messages": None,
    "last_refresh": 0,
}
_WORK_CACHE_TTL = 60  # seconds
_slack_user_cache = {}
_slack_team_id = ""


def _fetch_work_status():
    """Fetch Gmail + Calendar data from Google APIs."""
    from datetime import timedelta, timezone as tz
    DIR = os.path.dirname(os.path.abspath(__file__))

    GOOGLE_ACCOUNTS = [
        {"token": os.path.join(DIR, "google_token.json"), "label": "Personal", "color": "#52b788"},
        {"token": os.path.join(DIR, "google_token_work.json"), "label": "Work", "color": "#c9a96e"},
    ]
    active_accounts = [a for a in GOOGLE_ACCOUNTS if os.path.exists(a["token"])]
    if not active_accounts:
        return {"setup_required": True, "setup_message": "Google auth not configured. Run google_auth_setup.py"}

    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    SCOPES = ["https://www.googleapis.com/auth/gmail.readonly", "https://www.googleapis.com/auth/calendar.readonly"]
    result = {"emails": [], "events": [], "accounts": []}

    for account in active_accounts:
        acct_label, acct_color = account["label"], account["color"]
        try:
            creds = Credentials.from_authorized_user_file(account["token"], SCOPES)
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
                with open(account["token"], "w") as f:
                    f.write(creds.to_json())
        except Exception as e:
            result.setdefault("errors", []).append(f"{acct_label}: auth error — {e}")
            continue
        result["accounts"].append({"label": acct_label, "color": acct_color})

        # Gmail: unread + last 7 days
        try:
            gmail = build("gmail", "v1", credentials=creds, cache_discovery=False)
            seven_days_ago = (datetime.now() - timedelta(days=7)).strftime("%Y/%m/%d")
            seen_ids = set()
            all_msg_metas = []
            for query in ["is:unread", f"after:{seven_days_ago}"]:
                try:
                    resp = gmail.users().messages().list(userId="me", q=query, maxResults=50).execute()
                    for m in resp.get("messages", []):
                        if m["id"] not in seen_ids:
                            seen_ids.add(m["id"])
                            all_msg_metas.append(m)
                except Exception:
                    continue
            for msg_meta in all_msg_metas:
                msg = gmail.users().messages().get(
                    userId="me", id=msg_meta["id"], format="metadata",
                    metadataHeaders=["From", "Subject", "Date"]
                ).execute()
                headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
                from_raw = headers.get("From", "")
                from_name = from_raw.split("<")[0].strip().strip('"') if "<" in from_raw else from_raw
                date_str = headers.get("Date", "")
                time_display, sort_ts = "", 0
                try:
                    from email.utils import parsedate_to_datetime
                    dt = parsedate_to_datetime(date_str)
                    sort_ts = dt.timestamp()
                    now = datetime.now(tz.utc)
                    diff = now - dt
                    if diff.days == 0:
                        time_display = dt.strftime("%H:%M")
                    elif diff.days == 1:
                        time_display = "Yesterday " + dt.strftime("%H:%M")
                    elif diff.days < 7:
                        time_display = dt.strftime("%a %H:%M")
                    else:
                        time_display = dt.strftime("%d %b")
                except Exception:
                    time_display = date_str[:20] if date_str else ""
                result["emails"].append({
                    "from": from_name, "subject": headers.get("Subject", "(no subject)"),
                    "snippet": msg.get("snippet", ""), "time": time_display, "sort_ts": sort_ts,
                    "message_id": msg_meta["id"], "thread_id": msg.get("threadId", ""),
                    "account": acct_label, "account_color": acct_color,
                })
        except Exception as e:
            result.setdefault("errors", []).append(f"{acct_label} Gmail: {e}")

        # Calendar: 7 days back to 33 days ahead
        try:
            cal = build("calendar", "v3", credentials=creds, cache_discovery=False)
            now = datetime.now(tz.utc)
            time_min = (now - timedelta(days=7)).isoformat()
            time_max = (now + timedelta(days=33)).isoformat()
            events_result = cal.events().list(
                calendarId="primary", timeMin=time_min, timeMax=time_max,
                singleEvents=True, orderBy="startTime", maxResults=100
            ).execute()
            for ev in events_result.get("items", []):
                start = ev.get("start", {})
                end_t = ev.get("end", {})
                sort_ts = 0
                if "dateTime" in start:
                    try:
                        from dateutil.parser import parse as dt_parse
                        st = dt_parse(start["dateTime"])
                        en = dt_parse(end_t.get("dateTime", start["dateTime"]))
                        sort_ts = st.timestamp()
                        when = st.strftime("%a %d %b %H:%M") + " - " + en.strftime("%H:%M")
                    except Exception:
                        when = start["dateTime"][:16]
                elif "date" in start:
                    when = "All day - " + start["date"]
                else:
                    when = ""
                attendees = ev.get("attendees", [])
                att_names = [a.get("displayName", a.get("email", "")) for a in attendees[:5] if not a.get("self")]
                att_str = ", ".join(att_names)
                if len(attendees) > 5:
                    att_str += f" +{len(attendees) - 5} more"
                result["events"].append({
                    "summary": ev.get("summary", "(no title)"), "when": when,
                    "location": ev.get("location", ""), "attendees": att_str, "sort_ts": sort_ts,
                    "html_link": ev.get("htmlLink", ""), "event_id": ev.get("id", ""),
                    "account": acct_label, "account_color": acct_color,
                })
        except Exception as e:
            result.setdefault("errors", []).append(f"{acct_label} Calendar: {e}")

    result["emails"].sort(key=lambda x: x.get("sort_ts", 0), reverse=True)
    result["events"].sort(key=lambda x: x.get("sort_ts", 0))
    return result


def _fetch_slack_messages():
    """Fetch Slack channel messages."""
    import requests as http_requests
    global _slack_user_cache, _slack_team_id
    DIR = os.path.dirname(os.path.abspath(__file__))
    SLACK_CONFIG = os.path.join(DIR, "slack_config.json")

    slack_cfg = None
    if os.path.exists(SLACK_CONFIG):
        try:
            with open(SLACK_CONFIG) as f:
                slack_cfg = json.load(f)
        except Exception as e:
            return {"error": f"Failed to read slack_config.json: {e}"}
    else:
        try:
            from credentials import SLACK_USER_TOKEN, SLACK_BOT_TOKEN
            slack_cfg = {"token": SLACK_USER_TOKEN, "bot_token": SLACK_BOT_TOKEN, "channels": [], "max_channels": 12}
        except ImportError:
            pass

    if not slack_cfg:
        return {"setup_required": True, "setup_message": "Slack not configured. Create slack_config.json."}

    token = slack_cfg.get("token", "") or slack_cfg.get("bot_token", "")
    bot_token = slack_cfg.get("bot_token", "") or token
    max_channels = slack_cfg.get("max_channels", 20)
    channel_names = slack_cfg.get("channels", [])
    slack_headers = {"Authorization": f"Bearer {token}"}
    bot_headers = {"Authorization": f"Bearer {bot_token}"}
    result = {"messages": [], "channels": []}
    DEADLINE = time.monotonic() + 12

    def _over_budget():
        return time.monotonic() >= DEADLINE

    try:
        # Bulk-fetch users on first request
        if not _slack_user_cache:
            try:
                cursor = ""
                for _ in range(5):
                    params = {"limit": 1000}
                    if cursor:
                        params["cursor"] = cursor
                    users_resp = http_requests.get("https://slack.com/api/users.list", headers=bot_headers, params=params, timeout=10).json()
                    if users_resp.get("ok"):
                        for u in users_resp.get("members", []):
                            uid = u.get("id", "")
                            if not uid:
                                continue
                            profile = u.get("profile", {})
                            name = (profile.get("display_name", "").strip() or u.get("real_name", "").strip() or u.get("name", uid))
                            _slack_user_cache[uid] = name
                    cursor = users_resp.get("response_metadata", {}).get("next_cursor", "")
                    if not cursor:
                        break
            except Exception:
                pass

        def get_username(user_id):
            if not user_id or user_id in _slack_user_cache:
                return _slack_user_cache.get(user_id, user_id)
            if _over_budget():
                return user_id
            try:
                user_resp = http_requests.get("https://slack.com/api/users.info", headers=bot_headers, params={"user": user_id}, timeout=3).json()
                if user_resp.get("ok"):
                    profile = user_resp["user"].get("profile", {})
                    name = (profile.get("display_name", "").strip() or user_resp["user"].get("real_name", "").strip() or user_resp["user"].get("name", user_id))
                    _slack_user_cache[user_id] = name
                    return name
            except Exception:
                pass
            _slack_user_cache[user_id] = user_id
            return user_id

        convos_resp = http_requests.get("https://slack.com/api/users.conversations", headers=slack_headers,
                                         params={"types": "public_channel,private_channel,im,mpim", "limit": 200, "exclude_archived": "true"}, timeout=8).json()
        if not convos_resp.get("ok"):
            return {"error": f"Slack API error: {convos_resp.get('error', 'unknown')}"}
        all_convos = convos_resp.get("channels", [])
        if channel_names:
            name_set = set(channel_names)
            all_convos = [c for c in all_convos if c.get("name", "") in name_set]

        target_channels = []
        for c in all_convos:
            ch_id = c["id"]
            if c.get("is_im"):
                ch_name = "DM: " + get_username(c.get("user", ch_id))
            elif c.get("is_mpim"):
                ch_name = c.get("name", ch_id).replace("mpdm-", "").replace("--", ", ").rstrip("-")
            else:
                ch_name = c.get("name", ch_id)
            target_channels.append((ch_name, ch_id))
        target_channels = target_channels[:max_channels]
        result["channels"] = [{"name": n, "id": cid} for n, cid in target_channels]

        if not _slack_team_id and not _over_budget():
            try:
                auth_resp = http_requests.get("https://slack.com/api/auth.test", headers=bot_headers, timeout=3).json()
                if auth_resp.get("ok"):
                    _slack_team_id = auth_resp.get("team_id", "")
            except Exception:
                pass

        for ch_name, ch_id in target_channels:
            if _over_budget():
                result.setdefault("warnings", []).append("Time limit reached — partial results")
                break
            try:
                history = http_requests.get("https://slack.com/api/conversations.history", headers=slack_headers,
                                             params={"channel": ch_id, "limit": 20}, timeout=5).json()
                if not history.get("ok"):
                    continue
                for msg in history.get("messages", []):
                    if msg.get("subtype") in ("channel_join", "channel_leave", "channel_topic", "channel_purpose", "bot_add", "bot_remove"):
                        continue
                    from datetime import timezone as tz
                    ts = float(msg.get("ts", 0))
                    dt = datetime.fromtimestamp(ts, tz=tz.utc)
                    now = datetime.now(tz.utc)
                    diff = now - dt
                    if diff.days == 0:
                        time_display = dt.strftime("%H:%M")
                    elif diff.days == 1:
                        time_display = "Yesterday " + dt.strftime("%H:%M")
                    elif diff.days < 7:
                        time_display = dt.strftime("%a %H:%M")
                    else:
                        time_display = dt.strftime("%d %b")
                    user_name = get_username(msg.get("user", ""))
                    text = msg.get("text", "")
                    for uid_match in re.findall(r"<@(U[A-Z0-9]+)>", text):
                        text = text.replace(f"<@{uid_match}>", f"@{get_username(uid_match)}")
                    text = re.sub(r"<(https?://[^|>]+)\|([^>]+)>", r"\2", text)
                    text = re.sub(r"<(https?://[^>]+)>", r"\1", text)
                    msg_link = f"slack://channel?team={_slack_team_id}&id={ch_id}&message={msg.get('ts', '')}" if _slack_team_id else ""
                    result["messages"].append({
                        "channel": ch_name, "channel_id": ch_id, "user": user_name,
                        "text": text[:300], "time": time_display, "sort_ts": ts,
                        "link": msg_link, "thread_ts": msg.get("thread_ts", ""), "reply_count": msg.get("reply_count", 0),
                    })
            except Exception as e:
                result.setdefault("errors", []).append(f"#{ch_name}: {e}")
        result["messages"].sort(key=lambda x: x.get("sort_ts", 0), reverse=True)
    except Exception as e:
        result["error"] = str(e)
    return result


def _work_cache_thread():
    """Background thread: refreshes work status + Slack every 60s."""
    time.sleep(5)
    while True:
        try:
            _work_cache["work_status"] = _fetch_work_status()
        except Exception as e:
            _work_cache["work_status"] = {"error": str(e)}
        try:
            _work_cache["slack_messages"] = _fetch_slack_messages()
        except Exception as e:
            _work_cache["slack_messages"] = {"error": str(e)}
        _work_cache["last_refresh"] = time.monotonic()
        time.sleep(_WORK_CACHE_TTL)


@app.route("/work-status")
def work_status():
    """Serve work status (Gmail + Calendar) from background cache."""
    cached = _work_cache.get("work_status")
    if cached is not None:
        resp = jsonify(cached)
        resp.headers["Cache-Control"] = "no-store"
        return resp
    try:
        data = _fetch_work_status()
        _work_cache["work_status"] = data
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e), "setup_required": True}), 502


@app.route("/slack-messages")
def slack_messages():
    """Serve Slack messages from background cache."""
    cached = _work_cache.get("slack_messages")
    if cached is not None:
        return jsonify(cached)
    try:
        data = _fetch_slack_messages()
        _work_cache["slack_messages"] = data
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e), "messages": []}), 502


@app.route("/push-message", methods=["POST"])
def push_message():
    """Push a message to all connected app clients (used by terminal sessions)."""
    data = request.get_json() or {}
    content = data.get("content", "")
    if not content:
        return jsonify({"error": "content required"}), 400
    _broadcast_ws({"type": "push_message", "content": content})
    return jsonify({"ok": True})


@app.route("/system-health")
def system_health():
    """Return timestamps and status of key system components for the iOS health dashboard."""
    from pathlib import Path
    import stat

    items = []

    # Base path: works from both ~/projects/claude/ (cron) and ~/Documents/Claude code/ (interactive)
    _base = Path(__file__).resolve().parent.parent
    _home_base = Path.home() / "Documents" / "Claude code"

    def _find(rel):
        """Try both base paths, return first that exists."""
        for b in [_base, _home_base]:
            p = b / rel
            try:
                if p.exists():
                    return p
            except (PermissionError, OSError):
                continue
        return _base / rel  # fallback

    # 1. Last backup
    manifest_path = _find(".backup_manifest.json")
    if manifest_path.exists():
        try:
            with open(manifest_path) as f:
                manifest = json.load(f)
            last_backup = manifest.get("last_backup")
            file_count = len(manifest.get("files", {}))
            items.append({
                "name": "Google Drive Backup",
                "timestamp": last_backup,
                "detail": f"{file_count} files tracked",
            })
        except Exception:
            items.append({"name": "Google Drive Backup", "timestamp": None, "detail": "manifest unreadable"})
    else:
        items.append({"name": "Google Drive Backup", "timestamp": None, "detail": "no manifest found"})

    # 2. Conversation server uptime
    uptime_s = round(time.time() - _start_time)
    started_iso = datetime.fromtimestamp(_start_time).isoformat()
    items.append({
        "name": "Conversation Server",
        "timestamp": started_iso,
        "detail": f"uptime {uptime_s // 3600}h {(uptime_s % 3600) // 60}m",
    })

    # 3. Printer daemon (check status.json freshness)
    printer_status = Path("/tmp/printer_status/status.json")
    if printer_status.exists():
        mtime = printer_status.stat().st_mtime
        items.append({
            "name": "Printer Daemon",
            "timestamp": datetime.fromtimestamp(mtime).isoformat(),
            "detail": "status.json last updated",
        })
    else:
        items.append({"name": "Printer Daemon", "timestamp": None, "detail": "no status file"})

    # 4. School docs Google Drive sync
    gdrive_links = _find("ofsted-agent/gdrive_links.json")
    if gdrive_links.exists():
        mtime = gdrive_links.stat().st_mtime
        try:
            with open(gdrive_links) as f:
                link_count = len(json.load(f))
        except Exception:
            link_count = 0
        items.append({
            "name": "School Docs Sync",
            "timestamp": datetime.fromtimestamp(mtime).isoformat(),
            "detail": f"{link_count} files mapped to Drive",
        })
    else:
        items.append({"name": "School Docs Sync", "timestamp": None, "detail": "not synced yet"})

    # 5. Memory search DB freshness
    chroma_dir = _find("memory_server/data/chroma")
    if chroma_dir.exists():
        # Find most recent file in chroma dir
        latest = 0
        for p in chroma_dir.rglob("*"):
            if p.is_file():
                latest = max(latest, p.stat().st_mtime)
        if latest > 0:
            items.append({
                "name": "Memory Search DB",
                "timestamp": datetime.fromtimestamp(latest).isoformat(),
                "detail": "ChromaDB last updated",
            })
        else:
            items.append({"name": "Memory Search DB", "timestamp": None, "detail": "empty"})
    else:
        items.append({"name": "Memory Search DB", "timestamp": None, "detail": "not found"})

    # 6. GitHub repos — check last commit dates + uncommitted change counts
    # Use both base paths for git checks (~/projects/claude/ copies may lack .git)
    def _find_repo(rel):
        for b in [_base, _home_base]:
            p = b / rel
            try:
                if (p / ".git").is_dir():
                    return p
            except (PermissionError, OSError):
                continue
        return None

    for repo_name, repo_path in [
        ("GitHub: ClaudeCode", _find_repo("ClaudeCode")),
        ("GitHub: claude-mobile", _find_repo("claude-mobile")),
        ("GitHub: ofsted-agent", _find_repo("ofsted-agent")),
        ("GitHub: sv08-print-tools", _find_repo("sv08-print-tools")),
    ]:
        if repo_path is None:
            items.append({"name": repo_name, "timestamp": None, "detail": "repo not found", "uncommitted_count": -1})
            continue
        if (repo_path / ".git").exists():
            try:
                result = subprocess.run(
                    ["git", "-C", str(repo_path), "log", "-1", "--format=%aI"],
                    capture_output=True, text=True, timeout=5
                )
                # Count uncommitted changes (staged + unstaged + untracked)
                status_result = subprocess.run(
                    ["git", "-C", str(repo_path), "status", "--porcelain"],
                    capture_output=True, text=True, timeout=5
                )
                uncommitted = len([l for l in status_result.stdout.strip().split("\n") if l.strip()]) if status_result.returncode == 0 and status_result.stdout.strip() else 0
                if result.returncode == 0 and result.stdout.strip():
                    detail = f"{uncommitted} uncommitted" if uncommitted > 0 else "clean"
                    items.append({
                        "name": repo_name,
                        "timestamp": result.stdout.strip(),
                        "detail": detail,
                        "uncommitted_count": uncommitted,
                    })
                else:
                    items.append({"name": repo_name, "timestamp": None, "detail": "no commits", "uncommitted_count": 0})
            except Exception:
                items.append({"name": repo_name, "timestamp": None, "detail": "git error", "uncommitted_count": -1})

    return jsonify({"items": items})


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
        _log.info("AI config updated: %s", _ai_config)
        return jsonify(_ai_config)


@app.route("/work-search")
def work_search():
    """Search Gmail + Calendar across configured accounts."""
    from datetime import timezone as tz
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"results": []})

    DIR = os.path.dirname(os.path.abspath(__file__))
    results = []

    GOOGLE_ACCOUNTS = [
        {"token": os.path.join(DIR, "google_token.json"), "label": "Personal", "color": "#52b788"},
        {"token": os.path.join(DIR, "google_token_work.json"), "label": "Work", "color": "#c9a96e"},
    ]
    active_accounts = [a for a in GOOGLE_ACCOUNTS if os.path.exists(a["token"])]
    if not active_accounts:
        return jsonify({"results": [], "error": "No Google accounts configured"})

    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    SCOPES = ["https://www.googleapis.com/auth/gmail.readonly", "https://www.googleapis.com/auth/calendar.readonly"]

    for account in active_accounts:
        try:
            creds = Credentials.from_authorized_user_file(account["token"], SCOPES)
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
        except Exception:
            continue

        # Search Gmail
        try:
            gmail = build("gmail", "v1", credentials=creds, cache_discovery=False)
            msgs = gmail.users().messages().list(userId="me", q=q, maxResults=10).execute()
            for msg_meta in msgs.get("messages", []):
                msg = gmail.users().messages().get(userId="me", id=msg_meta["id"], format="metadata",
                    metadataHeaders=["From", "Subject", "Date"]).execute()
                headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
                from_raw = headers.get("From", "")
                from_name = from_raw.split("<")[0].strip().strip('"') if "<" in from_raw else from_raw
                sort_ts = 0
                try:
                    from email.utils import parsedate_to_datetime
                    dt = parsedate_to_datetime(headers.get("Date", ""))
                    sort_ts = dt.timestamp()
                    time_display = dt.strftime("%d %b %H:%M")
                except Exception:
                    time_display = ""
                results.append({"type": "email", "from": from_name, "subject": headers.get("Subject", ""),
                    "snippet": msg.get("snippet", ""), "time": time_display, "sort_ts": sort_ts,
                    "account": account["label"], "account_color": account["color"]})
        except Exception:
            pass

        # Search Calendar
        try:
            cal = build("calendar", "v3", credentials=creds, cache_discovery=False)
            events = cal.events().list(calendarId="primary", q=q, singleEvents=True, orderBy="startTime", maxResults=10).execute()
            for ev in events.get("items", []):
                start = ev.get("start", {})
                sort_ts = 0
                if "dateTime" in start:
                    try:
                        from dateutil.parser import parse as dt_parse
                        st = dt_parse(start["dateTime"])
                        sort_ts = st.timestamp()
                        when = st.strftime("%a %d %b %H:%M")
                    except Exception:
                        when = start["dateTime"][:16]
                elif "date" in start:
                    when = start["date"]
                else:
                    when = ""
                results.append({"type": "event", "summary": ev.get("summary", ""), "when": when,
                    "sort_ts": sort_ts, "account": account["label"], "account_color": account["color"]})
        except Exception:
            pass

    results.sort(key=lambda x: x.get("sort_ts", 0), reverse=True)
    return jsonify({"results": results})


@app.after_request
def add_headers(response):
    # No CORS header — native iOS app uses WebSocket (not browser CORS).
    # Service worker scope
    if response.content_type and 'javascript' in response.content_type:
        response.headers['Service-Worker-Allowed'] = '/'
    return response


def _cleanup_thread():
    """Background thread: periodically cleans up stale session dirs and upload files.

    Runs every hour. Deletes:
      - Session directories in SESSIONS_DIR older than 24 hours
      - Upload files in /tmp/claude_uploads older than 1 hour
    """
    import tempfile
    upload_dir = os.path.join(tempfile.gettempdir(), "claude_uploads")

    while True:
        time.sleep(3600)  # Run every hour
        now = time.time()

        # Clean session directories older than 24 hours
        try:
            for entry in os.listdir(SESSIONS_DIR):
                entry_path = os.path.join(SESSIONS_DIR, entry)
                if not os.path.isdir(entry_path):
                    continue
                try:
                    mtime = os.path.getmtime(entry_path)
                    if now - mtime > 86400:  # 24 hours
                        import shutil
                        shutil.rmtree(entry_path, ignore_errors=True)
                        _log.info("Cleanup: removed stale session dir %s", entry)
                except OSError:
                    pass
        except Exception as e:
            _log.warning("Cleanup error (sessions): %s", e)

        # Clean upload files older than 1 hour
        try:
            if os.path.isdir(upload_dir):
                for entry in os.listdir(upload_dir):
                    entry_path = os.path.join(upload_dir, entry)
                    if not os.path.isfile(entry_path):
                        continue
                    try:
                        mtime = os.path.getmtime(entry_path)
                        if now - mtime > 3600:  # 1 hour
                            os.remove(entry_path)
                            _log.info("Cleanup: removed stale upload %s", entry)
                    except OSError:
                        pass
        except Exception as e:
            _log.warning("Cleanup error (uploads): %s", e)


def _signal_handler(signum, frame):
    """Log signal before exiting — helps diagnose unexpected daemon restarts."""
    sig_name = signal.Signals(signum).name
    uptime = round(time.time() - _start_time)
    _log.info("Received %s (uptime %ds), shutting down", sig_name, uptime)

    # Kill persistent Claude subprocess cleanly
    with _lock:
        proc = _session.get("proc")
        if proc and proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except Exception:
                pass

    # Unblock any pending permission requests
    with _permission_lock:
        for req_id, req in _permission_requests.items():
            req["response"] = {"allow": False, "message": "Server shutting down"}
            req["event"].set()

    sys.exit(0)


if __name__ == "__main__":
    # Register signal handlers for diagnostics
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGHUP, _signal_handler)

    _log.info("Conversation server starting (PID %d)", os.getpid())

    # Start printer state monitor in background
    monitor = threading.Thread(target=_check_printer_state, daemon=True)
    monitor.start()
    _log.info("Printer state monitor started")

    # Start session/upload cleanup thread (hourly)
    cleaner = threading.Thread(target=_cleanup_thread, daemon=True)
    cleaner.start()
    _log.info("Cleanup thread started (sessions >24h, uploads >1h)")

    # Start work status cache thread (Gmail + Calendar + Slack)
    work_t = threading.Thread(target=_work_cache_thread, daemon=True, name="work-cache")
    work_t.start()
    _log.info("Work cache thread started (60s refresh)")

    app.run(host="0.0.0.0", port=8081, threaded=True)
