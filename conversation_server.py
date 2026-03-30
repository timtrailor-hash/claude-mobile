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
import urllib.parse
import urllib.request
import uuid
from datetime import datetime

# Add shared_utils to path
sys.path.insert(0, os.path.expanduser("~/code"))
from shared_utils import env_for_claude_cli, work_dir, configure_logging
from printer_registry import registry as _printer_registry

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
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50 MB upload limit
# Enable protocol-level WebSocket pings every 25s. simple-websocket's
# internal thread handles ping/pong properly — URLSessionWebSocketTask
# responds to pings automatically. This replaces our custom keepalive.
app.config['SOCK_SERVER_OPTIONS'] = {'ping_interval': 25}
sock = Sock(app)


@app.before_request
def check_auth():
    """Require auth token for non-localhost/Tailscale requests."""
    if not AUTH_TOKEN:
        return  # No token configured, skip auth
    # Trust localhost (MCP server, health check, etc.)
    if request.remote_addr in ('127.0.0.1', '::1'):
        return
    # Trust Tailscale network (100.x.x.x) — already authenticated at network level
    if request.remote_addr and request.remote_addr.startswith('100.'):
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


def _kill_subprocess():
    """Kill the persistent Claude CLI subprocess if running."""
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
            # Wait briefly for clean exit
            try:
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        _session["proc"] = None
        _session["stdin_pipe"] = None
        _session["pid"] = None
        _session["status"] = "idle"
        _session["reader_running"] = False


def _ensure_subprocess(resume_session_id=None):
    """Ensure a persistent Claude CLI subprocess is running. Spawn if needed.

    The subprocess uses bidirectional stream-json: messages go in via stdin,
    events come out via stdout. It stays alive across multiple messages.
    Permission requests are handled via the MCP approval server.

    Uses _spawn_lock to prevent the race between "is it running?" check and
    the Popen call — without this, two threads could both see "not running"
    and spawn duplicate subprocesses.

    If resume_session_id is provided, kills any existing subprocess and
    spawns a new one with --resume <id> to continue that conversation.

    Returns True if subprocess is running, False on error.
    """
    with _spawn_lock:
        if resume_session_id:
            _kill_subprocess()
        else:
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

        # Resume a previous session if requested
        if resume_session_id:
            cmd += ["--resume", resume_session_id]

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

    # Attach files if any
    valid_paths = [p for p in image_paths if os.path.exists(p)]
    if valid_paths:
        paths_str = ", ".join(valid_paths)
        file_word = "file" if len(valid_paths) == 1 else "files"
        message = (f"I've attached {len(valid_paths)} {file_word} at these paths: "
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
    _kill_subprocess()
    with _lock:
        _session["claude_sid"] = None
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


def _get_ttyd_tunnel_url():
    """Read the current ttyd cloudflared tunnel URL."""
    try:
        with open("/tmp/ttyd_tunnel_url.txt") as f:
            return f.read().strip()
    except (FileNotFoundError, OSError):
        return ""


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

    return jsonify({
        "ok": True,
        "pid": os.getpid(),
        "uptime_s": uptime,
        "claude_auth_status": auth_status,
        "thread_health": thread_health,
        "ws_reconnect_count_5min": reconnect_count,
        "work_cache_age_seconds": work_cache_age,
        "last_claude_error": last_error["message"],
        "ttyd_tunnel_url": _get_ttyd_tunnel_url(),
    })


def _terminal_claude_cmd():
    """Build the tmux send-keys command to launch claude with subscription auth.

    Uses keychain auth (from `claude auth login` OAuth flow).
    CRITICAL: strips ANTHROPIC_API_KEY and CLAUDE_CODE_OAUTH_TOKEN to prevent
    API billing — Claude Team subscription only.
    """
    try:
        from credentials import KEYCHAIN_PASSWORD
        unlock = f"security unlock-keychain -p {KEYCHAIN_PASSWORD} ~/Library/Keychains/login.keychain-db"
    except (ImportError, AttributeError):
        unlock = "true"  # no-op if no password
    return (f"{unlock} && cd ~/Documents/Claude\\\\ code"
            " && export PATH=$HOME/.local/bin:/opt/homebrew/bin:$PATH"
            " && unset ANTHROPIC_API_KEY && unset CLAUDE_CODE_OAUTH_TOKEN && claude")


# ── Printer control safety helpers ──

def _get_print_state(printer):
    """Fetch live print_stats.state from Moonraker. Returns state string or 'unknown'."""
    try:
        url = f"{printer.moonraker_url}/printer/objects/query?print_stats"
        with urllib.request.urlopen(url, timeout=5) as r:
            data = json.loads(r.read())
        return data["result"]["status"]["print_stats"]["state"]
    except Exception as e:
        _log.warning("_get_print_state failed for %s: %s", printer.id, e)
        return "unknown"


# Commands that are ALWAYS blocked during printing/paused
_DANGEROUS_WHILE_PRINTING = {
    "G28", "QUAD_GANTRY_LEVEL", "BED_MESH_CALIBRATE", "PROBE",
    "FIRMWARE_RESTART", "RESTART",  # Restarts Klipper — kills print
    "SAVE_CONFIG",                    # Restarts Klipper after writing config — destroyed print 2026-03-05
    "SET_KINEMATIC_POSITION",
}

# Commands that are always allowed regardless of state
_ALWAYS_ALLOWED = {"M112", "EMERGENCY_STOP"}


def _validate_gcode_safety(script, state):
    """Check if a gcode command is safe given the current printer state.

    Returns (safe: bool, reason: str).
    """
    if not script or not script.strip():
        return False, "Empty command"

    cmd = script.strip().split()[0].upper()

    if cmd in _ALWAYS_ALLOWED:
        return True, ""

    if state in ("printing", "paused"):
        if cmd in _DANGEROUS_WHILE_PRINTING:
            return False, f"{cmd} is blocked while printer is {state}"
        # Block movement during printing (except speed/temp changes)
        if cmd in ("G1", "G0") and state == "printing":
            return False, "Movement commands blocked while printing"

    return True, ""


def _send_moonraker_gcode(printer, script):
    """Send a gcode script to Moonraker. Returns (ok, result_or_error)."""
    try:
        payload = json.dumps({"script": script}).encode()
        req = urllib.request.Request(
            f"{printer.moonraker_url}/printer/gcode/script",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            result = json.loads(r.read())
        return True, result
    except Exception as e:
        _log.error("Moonraker gcode failed for %s: %s (script=%s)", printer.id, e, script[:80])
        return False, str(e)


def _send_bambu_mqtt(printer, payload_dict):
    """Send a command to Bambu printer via MQTT. Returns (ok, error_or_none)."""
    import ssl
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        return False, "paho-mqtt not installed"

    creds = printer.get_bambu_credentials()
    serial = creds.get("serial", "")
    access_code = creds.get("access_code", "")
    if not serial or not access_code:
        return False, "Bambu credentials not configured"

    result = {"ok": False, "error": None}
    done = [False]

    def on_connect(client, userdata, flags, rc, properties=None):
        if rc == 0:
            client.publish(f"device/{serial}/request", json.dumps(payload_dict))
            result["ok"] = True
            done[0] = True
        else:
            result["error"] = f"MQTT connect failed: rc={rc}"
            done[0] = True

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.username_pw_set("bblp", access_code)
    client.tls_set_context(ctx)
    client.on_connect = on_connect

    try:
        client.connect(printer.host, printer.mqtt_port or 8883, 60)
        client.loop_start()
        timeout_t = time.time() + 5
        while not done[0] and time.time() < timeout_t:
            time.sleep(0.1)
        client.loop_stop()
        client.disconnect()
    except Exception as e:
        return False, str(e)

    return result["ok"], result.get("error")


def _resolve_printer(printer_id=None, printer_type="klipper"):
    """Resolve printer from request. Returns (printer, error_response) — one will be None."""
    try:
        if printer_id:
            return _printer_registry.get(printer_id), None
        if printer_type == "klipper":
            return _printer_registry.list_klipper()[0], None
        elif printer_type == "bambu":
            return _printer_registry.list_bambu()[0], None
        else:
            return _printer_registry.list_enabled()[0], None
    except (KeyError, IndexError):
        return None, (jsonify({"ok": False, "error": f"No {printer_type} printer found"}), 404)


@app.route("/printer-alert", methods=["POST"])
def printer_alert_endpoint():
    """Receive external alerts (e.g. UPS watchdog) and broadcast to iOS app."""
    data = request.get_json(force=True)
    msg = data.get("message", "Unknown alert")
    level = data.get("level", "warning")
    _broadcast_ws({
        "type": "printer_alert",
        "printer": "system",
        "event": f"ups_{level}",
        "message": msg,
    })
    _log.warning("External printer alert: %s", msg)
    return jsonify({"ok": True})


@app.route("/printer-resume-info")
def printer_resume_info():
    """Show last checkpoint data and whether a resume is possible."""
    checkpoint_file = "/tmp/printer_status/print_checkpoint.json"
    try:
        with open(checkpoint_file) as f:
            cp = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return jsonify({"available": False, "reason": "No checkpoint found"})

    # Check if the file still exists on the printer
    filename = cp.get("filename", "")
    file_exists = False
    if filename:
        try:
            klipper = _printer_registry.list_klipper()[0]
            encoded = urllib.parse.quote(filename, safe="")
            url = f"{klipper.moonraker_url}/server/files/metadata?filename={encoded}"
            with urllib.request.urlopen(url, timeout=5) as r:
                meta = json.loads(r.read())
            file_exists = meta.get("result", {}).get("filename") is not None
        except Exception:
            pass

    # Check checkpoint age (only valid for ~24h)
    ts = cp.get("timestamp", "")
    age_hours = 999
    try:
        from datetime import datetime as dt
        cp_time = dt.fromisoformat(ts)
        age_hours = (dt.now() - cp_time).total_seconds() / 3600
    except Exception:
        pass

    # Check if printer is currently idle (print finished successfully)
    printer_idle_after_complete = False
    try:
        with open("/tmp/printer_status/status.json") as sf:
            status = json.load(sf)
        sovol = status.get("printers", {}).get("sovol", {})
        cur_state = (sovol.get("state") or "").lower()
        cur_progress = sovol.get("progress", 0) or 0
        # If printer is idle/standby with 0% progress, print is done — no resume needed
        if cur_state in ("standby", "ready", "idle", "complete") and cur_progress < 1:
            printer_idle_after_complete = True
    except Exception:
        pass

    available = (file_exists and age_hours < 24
                 and cp.get("progress_pct", 0) > 0
                 and not printer_idle_after_complete)
    safe_layer = max(1, cp.get("current_layer", 0) - 2)

    # Estimate time saved
    total_layers = cp.get("total_layers", 1) or 1
    duration_s = cp.get("print_duration_s", 0) or 0
    estimated_total_s = duration_s / max(0.01, cp.get("progress_pct", 1) / 100)
    skip_fraction = safe_layer / total_layers
    time_saved_h = (estimated_total_s * skip_fraction) / 3600

    return jsonify({
        "available": available,
        "checkpoint": cp,
        "file_exists": file_exists,
        "age_hours": round(age_hours, 1),
        "suggested_layer": safe_layer,
        "estimated_time_saved_h": round(time_saved_h, 1),
    })


@app.route("/printer-resume-from-layer", methods=["POST"])
def printer_resume_from_layer():
    """Generate a resume gcode file and optionally start the print.

    Accepts JSON: {"filename": "...", "layer": 52} or {"layer": "auto"}
    """
    body = request.get_json(silent=True) or {}
    layer = body.get("layer", "auto")
    auto_start = body.get("start", False)

    # Get filename from body or checkpoint
    filename = body.get("filename", "")
    if not filename:
        try:
            with open("/tmp/printer_status/print_checkpoint.json") as f:
                cp = json.load(f)
            filename = cp.get("filename", "")
        except Exception:
            return jsonify({"ok": False, "error": "No filename and no checkpoint"}), 400

    if not filename:
        return jsonify({"ok": False, "error": "No filename found"}), 400

    # Run gcode_resume.py as subprocess
    script = os.path.expanduser("~/code/sv08-print-tools/gcode_resume.py")
    cmd = [sys.executable, script, "--file", filename, "--layer", str(layer)]
    if auto_start:
        cmd.append("--start")

    _log.info("printer-resume: running %s", " ".join(cmd))

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=600,
            cwd=os.path.dirname(script),
        )
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "Resume script timed out (>10min)"}), 504

    if result.returncode != 0:
        _log.error("printer-resume failed: %s", result.stderr[:500])
        return jsonify({
            "ok": False,
            "error": result.stderr[:300] or "Unknown error",
            "stdout": result.stdout[-500:],
        }), 500

    # Parse summary JSON from stdout
    summary = {}
    for line in result.stdout.split("\n"):
        if line.startswith("__SUMMARY_JSON__:"):
            try:
                summary = json.loads(line.split(":", 1)[1])
            except json.JSONDecodeError:
                pass

    _log.info("printer-resume: success — %s", summary.get("resume_file", "?"))
    return jsonify({
        "ok": True,
        **summary,
        "auto_started": auto_start,
    })


@app.route("/terminal-new-window", methods=["POST"])
def terminal_new_window():
    """Create a new tmux window in the claude-terminal session."""
    try:
        result = subprocess.run(
            ["/opt/homebrew/bin/tmux", "new-window", "-t", "claude-terminal",
             "-c", os.path.expanduser("~/Documents/Claude code")],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            import time
            time.sleep(0.5)
            subprocess.run(
                ["/opt/homebrew/bin/tmux", "send-keys", "-t", "claude-terminal",
                 _terminal_claude_cmd(), "Enter"],
                capture_output=True, text=True, timeout=5
            )
            _log.info("Created new tmux window in claude-terminal (claude auto-started)")
            return jsonify({"ok": True})
        else:
            return jsonify({"ok": False, "error": result.stderr.strip()}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/terminal-reset", methods=["POST"])
def terminal_reset():
    """Kill ttyd + tmux and restart fresh. Used by iOS 'Reset Terminal' button."""
    try:
        tmux = "/opt/homebrew/bin/tmux"
        # 1. Kill ttyd
        subprocess.run(["pkill", "-f", "ttyd"], capture_output=True, timeout=5)
        # 2. Kill tmux session
        subprocess.run([tmux, "kill-session", "-t", "claude-terminal"],
                       capture_output=True, text=True, timeout=5)
        import time
        time.sleep(2)
        # 3. Restart via wrapper script
        result = subprocess.run(
            ["/bin/zsh", "-l", "-c",
             "export PATH=/opt/homebrew/bin:/usr/local/bin:$PATH && "
             "~/.local/bin/ttyd_wrapper.sh start"],
            capture_output=True, text=True, timeout=15,
        )
        if "Listening on port" in result.stderr or "ttyd started" in result.stdout:
            _log.info("terminal-reset: restarted successfully")
            return jsonify({"ok": True})
        # Wrapper runs ttyd in background, so check if it's actually listening
        time.sleep(2)
        check = subprocess.run(["lsof", "-i", ":7681"], capture_output=True, text=True, timeout=5)
        if "ttyd" in check.stdout:
            _log.info("terminal-reset: restarted (confirmed via lsof)")
            return jsonify({"ok": True})
        _log.warning("terminal-reset: wrapper output: stdout=%s stderr=%s",
                      result.stdout[:300], result.stderr[:300])
        return jsonify({"ok": False, "error": "ttyd may not have started",
                        "stdout": result.stdout[:300], "stderr": result.stderr[:300]}), 500
    except Exception as e:
        _log.error("terminal-reset failed: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500


# ── Terminal OAuth Auth Bridge ─────────────────────────────────────

# Global state for in-progress auth session
_auth_session = {"process": None, "master_fd": None, "session_id": None, "timer": None}


def _cleanup_auth_session():
    """Kill auth process and clean up."""
    proc = _auth_session.get("process")
    if proc and proc.poll() is None:
        try:
            proc.kill()
        except Exception:
            pass
    fd = _auth_session.get("master_fd")
    if fd is not None:
        try:
            os.close(fd)
        except Exception:
            pass
    _auth_session.update({"process": None, "master_fd": None, "session_id": None, "timer": None})


@app.route("/terminal-auth-start", methods=["POST"])
def terminal_auth_start():
    """Start `claude auth login` via PTY and return the OAuth URL."""
    import pty
    import select

    # Clean up any previous session
    _cleanup_auth_session()

    try:
        # Unlock keychain (may already be unlocked — ignore errors)
        try:
            from credentials import KEYCHAIN_PASSWORD
            subprocess.run(
                ["security", "unlock-keychain", "-p", KEYCHAIN_PASSWORD,
                 os.path.expanduser("~/Library/Keychains/login.keychain-db")],
                capture_output=True, timeout=5
            )
        except (ImportError, Exception):
            pass  # No password configured or unlock failed — proceed anyway

        # Create PTY pair
        master_fd, slave_fd = pty.openpty()

        # Start claude auth login with PTY
        env = os.environ.copy()
        env["TERM"] = "dumb"
        # Ensure node and brew binaries are in PATH (needed by claude CLI)
        env["PATH"] = "/Users/timtrailor/.local/bin:/opt/homebrew/bin:/usr/local/bin:" + env.get("PATH", "")
        proc = subprocess.Popen(
            ["/Users/timtrailor/.local/bin/claude", "auth", "login"],
            stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
            env=env, close_fds=True
        )
        os.close(slave_fd)

        # Read output until we find the OAuth URL (timeout 30s)
        output = ""
        url = None
        deadline = time.time() + 30
        while time.time() < deadline:
            ready, _, _ = select.select([master_fd], [], [], 1.0)
            if ready:
                try:
                    chunk = os.read(master_fd, 4096).decode("utf-8", errors="replace")
                    output += chunk
                    # Look for OAuth URL
                    match = re.search(r'(https://claude\.ai/oauth/authorize[^\s\x1b]*)', output)
                    if match:
                        url = match.group(1)
                        break
                except OSError:
                    break
            if proc.poll() is not None:
                break

        if not url:
            # Process died or timed out without producing URL
            try:
                proc.kill()
            except Exception:
                pass
            try:
                os.close(master_fd)
            except Exception:
                pass
            _log.warning("terminal-auth-start: no OAuth URL found. Output: %s", output[:500])
            return jsonify({"ok": False, "error": "No OAuth URL received", "output": output[:500]}), 500

        # Store session for completion
        session_id = str(uuid.uuid4())
        _auth_session.update({
            "process": proc,
            "master_fd": master_fd,
            "session_id": session_id,
        })

        # Auto-cleanup after 5 minutes
        timer = threading.Timer(300, _cleanup_auth_session)
        timer.daemon = True
        timer.start()
        _auth_session["timer"] = timer

        _log.info("terminal-auth-start: got OAuth URL, session=%s", session_id)
        return jsonify({"ok": True, "url": url, "session_id": session_id})

    except Exception as e:
        _cleanup_auth_session()
        _log.error("terminal-auth-start failed: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/terminal-auth-complete", methods=["POST"])
def terminal_auth_complete():
    """Feed the OAuth code to the waiting `claude auth login` process."""
    import select

    data = request.get_json(force=True)
    code = data.get("code", "").strip()
    if not code:
        return jsonify({"ok": False, "error": "No code provided"}), 400

    proc = _auth_session.get("process")
    master_fd = _auth_session.get("master_fd")

    if not proc or proc.poll() is not None or master_fd is None:
        return jsonify({"ok": False, "error": "No active auth session"}), 400

    try:
        # Write the code to the PTY (simulating user pasting it)
        os.write(master_fd, (code + "\n").encode())

        # Wait for process to complete (up to 30s)
        deadline = time.time() + 30
        output = ""
        while time.time() < deadline:
            ready, _, _ = select.select([master_fd], [], [], 1.0)
            if ready:
                try:
                    chunk = os.read(master_fd, 4096).decode("utf-8", errors="replace")
                    output += chunk
                except OSError:
                    break
            if proc.poll() is not None:
                break

        # Cancel the cleanup timer
        timer = _auth_session.get("timer")
        if timer:
            timer.cancel()

        # Check auth status (needs node in PATH)
        auth_env = os.environ.copy()
        auth_env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + auth_env.get("PATH", "")
        auth_check = subprocess.run(
            ["/Users/timtrailor/.local/bin/claude", "auth", "status"],
            capture_output=True, text=True, timeout=10, env=auth_env
        )
        auth_ok = auth_check.returncode == 0

        _cleanup_auth_session()

        if auth_ok:
            # Kill and recreate tmux session so it picks up the new auth.
            # Claude Code TUI ignores "exit" via send-keys, so we nuke the session.
            # Also restart ttyd so it reconnects to the new session.
            _tmux = "/opt/homebrew/bin/tmux"
            try:
                # Kill ttyd first (it's attached to the tmux session)
                subprocess.run(["pkill", "-f", "ttyd"], capture_output=True, timeout=5)
                time.sleep(0.5)
                subprocess.run([_tmux, "kill-session", "-t", "claude-terminal"],
                               capture_output=True, text=True, timeout=5)
                time.sleep(1)
                # Recreate tmux session
                subprocess.run([_tmux, "new-session", "-d", "-s", "claude-terminal",
                               "-x", "120", "-y", "40",
                               "-c", os.path.expanduser("~/Documents/Claude code")],
                               capture_output=True, text=True, timeout=5)
                subprocess.run([_tmux, "set", "-t", "claude-terminal",
                               "history-limit", "50000"],
                               capture_output=True, text=True, timeout=5)
                time.sleep(0.5)
                subprocess.run([_tmux, "send-keys", "-t", "claude-terminal",
                               _terminal_claude_cmd(), "Enter"],
                               capture_output=True, text=True, timeout=5)
                _log.info("terminal-auth-complete: tmux session recreated")
                # Restart ttyd attached to new session
                ttyd_wrapper = os.path.expanduser("~/.local/bin/ttyd_wrapper.sh")
                subprocess.Popen([ttyd_wrapper, "start"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                _log.info("terminal-auth-complete: ttyd restarted")
            except Exception as restart_err:
                _log.warning("terminal-auth-complete: tmux/ttyd restart failed: %s", restart_err)

        _log.info("terminal-auth-complete: auth_ok=%s", auth_ok)
        return jsonify({
            "ok": auth_ok,
            "status": auth_check.stdout.strip() if auth_ok else auth_check.stderr.strip(),
        })

    except Exception as e:
        _cleanup_auth_session()
        _log.error("terminal-auth-complete failed: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500


# ── Google Docs export ─────────────────────────────────────────────

GOOGLE_TOKEN_FILE = os.path.join(os.path.dirname(__file__), "google_token.json")

# Import canonical scopes from credentials.py — single source of truth
# NEVER define scopes locally; NEVER write token back (token_refresh.py owns that)
try:
    from credentials import GOOGLE_OAUTH_SCOPES as GOOGLE_ALL_SCOPES
except ImportError:
    GOOGLE_ALL_SCOPES = [
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/calendar.readonly",
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/documents",
    ]


def _google_creds():
    """Load Google OAuth credentials, refreshing if needed. NEVER saves token back."""
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        if not os.path.exists(GOOGLE_TOKEN_FILE):
            return None
        creds = Credentials.from_authorized_user_file(GOOGLE_TOKEN_FILE, GOOGLE_ALL_SCOPES)
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        if creds and creds.valid:
            return creds
    except Exception as e:
        _log.warning("Google creds failed: %s", e)
    return None


@app.route("/terminal-to-doc", methods=["POST"])
def terminal_to_doc():
    """Create a Google Doc from terminal content.

    Accepts JSON: {"text": "...", "title": "optional title"}
    Also captures tmux pane as fallback.
    Returns: {"ok": true, "doc_url": "https://docs.google.com/..."} or error.
    """
    data = request.get_json(force=True)
    text = data.get("text", "").strip()

    # Fallback: capture tmux pane content if no text provided
    if not text:
        try:
            result = subprocess.run(
                ["/opt/homebrew/bin/tmux", "capture-pane", "-t",
                 "claude-terminal", "-p", "-S", "-"],
                capture_output=True, text=True, timeout=5
            )
            text = result.stdout.strip()
        except Exception:
            pass

    if not text:
        return jsonify({"ok": False, "error": "No text to export"}), 400

    title = data.get("title") or f"Terminal Export {datetime.now().strftime('%Y-%m-%d %H:%M')}"

    creds = _google_creds()
    if not creds:
        # Save text locally as fallback
        export_path = f"/tmp/terminal_export_{int(time.time())}.txt"
        with open(export_path, "w") as f:
            f.write(text)
        return jsonify({
            "ok": False,
            "error": "Google Docs auth not configured. Run /google-docs-setup first.",
            "local_file": export_path,
        }), 503

    try:
        from googleapiclient.discovery import build

        # Create empty doc
        docs_service = build("docs", "v1", credentials=creds)
        doc = docs_service.documents().create(body={"title": title}).execute()
        doc_id = doc["documentId"]

        # Insert text content
        requests_body = [{"insertText": {"location": {"index": 1}, "text": text}}]
        docs_service.documents().batchUpdate(
            documentId=doc_id, body={"requests": requests_body}
        ).execute()

        doc_url = f"https://docs.google.com/document/d/{doc_id}/edit"
        _log.info("Created Google Doc: %s (%d chars)", doc_url, len(text))
        return jsonify({"ok": True, "doc_url": doc_url, "doc_id": doc_id})

    except Exception as e:
        _log.error("Google Docs creation failed: %s", e)
        export_path = f"/tmp/terminal_export_{int(time.time())}.txt"
        with open(export_path, "w") as f:
            f.write(text)
        return jsonify({"ok": False, "error": str(e), "local_file": export_path}), 500


@app.route("/google-docs-auth-start", methods=["POST"])
def google_docs_auth_start():
    """Start Google Docs OAuth flow. Returns authorization URL."""
    try:
        from google_auth_oauthlib.flow import Flow

        # Read client credentials from existing token file
        with open(GOOGLE_TOKEN_FILE) as f:
            token_data = json.load(f)

        client_config = {
            "installed": {
                "client_id": token_data["client_id"],
                "client_secret": token_data["client_secret"],
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["http://localhost:1"],
            }
        }

        flow = Flow.from_client_config(client_config, scopes=GOOGLE_ALL_SCOPES)
        flow.redirect_uri = "http://localhost:1"

        auth_url, state = flow.authorization_url(
            access_type="offline",
            prompt="consent",
            include_granted_scopes="true",
        )

        # Store flow state for completion
        _google_auth_flow.update({"flow": flow, "state": state})

        return jsonify({"ok": True, "url": auth_url})

    except Exception as e:
        _log.error("google-docs-auth-start failed: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500


_google_auth_flow = {"flow": None, "state": None}


@app.route("/google-docs-auth-complete", methods=["POST"])
def google_docs_auth_complete():
    """Complete Google Docs OAuth flow with authorization code."""
    data = request.get_json(force=True)
    code = data.get("code", "").strip()
    if not code:
        return jsonify({"ok": False, "error": "No code provided"}), 400

    flow = _google_auth_flow.get("flow")
    if not flow:
        return jsonify({"ok": False, "error": "No auth flow in progress"}), 400

    try:
        flow.fetch_token(code=code)
        creds = flow.credentials

        # Merge with existing token data (keep client_id, client_secret)
        try:
            with open(GOOGLE_TOKEN_FILE) as f:
                existing = json.load(f)
        except Exception:
            existing = {}

        token_data = json.loads(creds.to_json())
        token_data["client_id"] = existing.get("client_id", token_data.get("client_id"))
        token_data["client_secret"] = existing.get("client_secret", token_data.get("client_secret"))

        # Backup old token
        if os.path.exists(GOOGLE_TOKEN_FILE):
            import shutil
            shutil.copy2(GOOGLE_TOKEN_FILE, GOOGLE_TOKEN_FILE + ".bak")

        with open(GOOGLE_TOKEN_FILE, "w") as f:
            json.dump(token_data, f, indent=2)

        _google_auth_flow.update({"flow": None, "state": None})
        _log.info("Google Docs auth complete — scopes: %s", token_data.get("scopes"))
        return jsonify({"ok": True, "scopes": token_data.get("scopes", [])})

    except Exception as e:
        _log.error("google-docs-auth-complete failed: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500




# ── Task status / heartbeat endpoint ──────────────────────────────────
_task_status = {
    "description": None,      # Current task description
    "started_at": None,       # ISO timestamp when task started
    "last_activity": None,    # ISO timestamp of last event
    "alive": False,           # Is Claude subprocess running?
    "progress_pct": None,     # Optional 0-100 percent
    "event_count": 0,         # Total events emitted
}
_task_status_lock = threading.Lock()


def _update_task_status(**kwargs):
    """Update task status fields. Call this when Claude emits events."""
    with _task_status_lock:
        _task_status["last_activity"] = datetime.now().isoformat()
        _task_status["alive"] = True
        for k, v in kwargs.items():
            if k in _task_status:
                _task_status[k] = v


@app.route("/task-status")
def task_status_endpoint():
    """Heartbeat endpoint — shows current task progress for iOS status indicator."""
    with _task_status_lock:
        status = dict(_task_status)
    # Calculate elapsed time
    if status.get("started_at"):
        try:
            started = datetime.fromisoformat(status["started_at"])
            elapsed = (datetime.now() - started).total_seconds()
            status["elapsed_seconds"] = int(elapsed)
        except Exception:
            pass
    # Check if subprocess is actually still running
    with _lock:
        proc = _session.get("proc")
        if proc is not None:
            status["alive"] = proc.poll() is None
        else:
            status["alive"] = False
    return jsonify(status)

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
                    _log.info("WS resume: killing subprocess and restarting with --resume %s", resume_sid)
                    if _ensure_subprocess(resume_session_id=resume_sid):
                        with _lock:
                            _session["claude_sid"] = resume_sid
                        _ws_send({"type": "resumed", "session_id": resume_sid})
                    else:
                        _ws_send({"type": "ws_error",
                                  "content": "Failed to restart Claude with --resume"})
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
    ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".pdf", ".heic", ".heif"}
    ext = os.path.splitext(f.filename)[1].lower() or ".jpg"
    if ext not in ALLOWED_EXTENSIONS:
        return jsonify({"error": f"Unsupported file type: {ext}"}), 400
    saved_name = f"{uuid.uuid4().hex}{ext}"
    saved_path = os.path.join(upload_dir, saved_name)
    f.save(saved_path)

    return jsonify({"path": saved_path, "filename": f.filename})


# ── Printer control endpoints ──

@app.route("/printer-print-control", methods=["POST"])
def printer_print_control():
    """Pause, resume, or cancel a print.

    Body: {printer_id, action} where action is pause/resume/cancel.
    SV08: Moonraker /printer/print/{action}
    Bambu: MQTT command
    """
    body = request.get_json(force=True) or {}
    action = body.get("action", "").lower()
    printer_id = body.get("printer_id")

    if action not in ("pause", "resume", "cancel"):
        return jsonify({"ok": False, "error": "action must be pause/resume/cancel"}), 400

    # Try to resolve as any printer type
    printer, err = _resolve_printer(printer_id, "klipper")
    if err:
        printer, err = _resolve_printer(printer_id, "bambu")
    if err:
        return err

    if printer.is_klipper:
        state = _get_print_state(printer)
        if action == "pause" and state != "printing":
            return jsonify({"ok": False, "error": f"Cannot pause: state is {state}"}), 409
        if action == "resume" and state != "paused":
            return jsonify({"ok": False, "error": f"Cannot resume: state is {state}"}), 409

        try:
            url = f"{printer.moonraker_url}/printer/print/{action}"
            req = urllib.request.Request(url, data=b"", method="POST")
            with urllib.request.urlopen(req, timeout=10) as r:
                r.read()
            _log.info("printer-print-control: %s on %s (was %s)", action, printer.id, state)
            return jsonify({"ok": True, "action": action})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 502

    elif printer.is_bambu:
        bambu_actions = {"pause": "pause", "resume": "resume", "cancel": "stop"}
        payload = {"print": {"sequence_id": "0", "command": bambu_actions[action]}}
        ok, error = _send_bambu_mqtt(printer, payload)
        if ok:
            _log.info("printer-print-control: %s on %s (bambu)", action, printer.id)
            return jsonify({"ok": True, "action": action})
        return jsonify({"ok": False, "error": error or "MQTT failed"}), 502

    return jsonify({"ok": False, "error": "Unknown printer type"}), 400


@app.route("/printer-estop", methods=["POST"])
def printer_estop():
    """Emergency stop — always allowed, no state check.

    Body: {printer_id}
    """
    body = request.get_json(force=True) or {}
    printer_id = body.get("printer_id")
    printer, err = _resolve_printer(printer_id, "klipper")
    if err:
        return err

    if not printer.is_klipper:
        return jsonify({"ok": False, "error": "E-stop only supported on Klipper printers"}), 400

    try:
        url = f"{printer.moonraker_url}/printer/emergency_stop"
        req = urllib.request.Request(url, data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=5) as r:
            r.read()
        _log.warning("printer-estop: EMERGENCY STOP on %s", printer.id)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502


@app.route("/printer-temp-set", methods=["POST"])
def printer_temp_set():
    """Set extruder or bed temperature.

    Body: {printer_id, heater, target}
    heater: "extruder" or "bed"
    target: temperature in C (0 = off)
    """
    body = request.get_json(force=True) or {}
    printer_id = body.get("printer_id")
    heater = body.get("heater", "").lower()
    target = body.get("target")

    if heater not in ("extruder", "bed"):
        return jsonify({"ok": False, "error": "heater must be 'extruder' or 'bed'"}), 400
    if target is None:
        return jsonify({"ok": False, "error": "Missing 'target' temperature"}), 400

    target = int(target)
    if target < 0:
        return jsonify({"ok": False, "error": "Temperature cannot be negative"}), 400

    # Try klipper first, then bambu
    printer, err = _resolve_printer(printer_id, "klipper")
    if err:
        printer, err = _resolve_printer(printer_id, "bambu")
    if err:
        return err

    # Enforce temp limits from printers.json
    limits = printer._data.get("temp_limits", {})
    if heater == "extruder" and target > limits.get("extruder_max", 310):
        return jsonify({"ok": False, "error": f"Extruder max is {limits.get('extruder_max', 310)}C"}), 400
    if heater == "bed" and target > limits.get("bed_max", 110):
        return jsonify({"ok": False, "error": f"Bed max is {limits.get('bed_max', 110)}C"}), 400

    if printer.is_klipper:
        gcode = f"M104 S{target}" if heater == "extruder" else f"M140 S{target}"
        ok, result = _send_moonraker_gcode(printer, gcode)
        if ok:
            _log.info("printer-temp-set: %s=%dC on %s", heater, target, printer.id)
            return jsonify({"ok": True, "heater": heater, "target": target})
        return jsonify({"ok": False, "error": str(result)}), 502

    elif printer.is_bambu:
        # Bambu supports temp via gcode_line MQTT command
        gcode = f"M104 S{target}" if heater == "extruder" else f"M140 S{target}"
        payload = {"print": {"sequence_id": "0", "command": "gcode_line", "param": gcode}}
        ok, error = _send_bambu_mqtt(printer, payload)
        if ok:
            _log.info("printer-temp-set: %s=%dC on %s (bambu)", heater, target, printer.id)
            return jsonify({"ok": True, "heater": heater, "target": target})
        return jsonify({"ok": False, "error": error or "MQTT failed"}), 502

    return jsonify({"ok": False, "error": "Unknown printer type"}), 400


@app.route("/printer-fan-set", methods=["POST"])
def printer_fan_set():
    """Set fan speed on Klipper printer.

    Body: {printer_id, fan, speed}
    fan: fan key from printers.json fans config (e.g. "front_cooling")
    speed: 0-100 (percentage)
    """
    body = request.get_json(force=True) or {}
    printer_id = body.get("printer_id")
    fan_key = body.get("fan", "")
    speed = body.get("speed")

    if speed is None:
        return jsonify({"ok": False, "error": "Missing 'speed'"}), 400
    speed = max(0, min(100, int(speed)))

    printer, err = _resolve_printer(printer_id, "klipper")
    if err:
        return err

    fans_config = printer._data.get("fans", {})
    fan_cfg = fans_config.get(fan_key)
    if not fan_cfg:
        return jsonify({"ok": False, "error": f"Unknown fan: {fan_key}"}), 400
    if not fan_cfg.get("controllable"):
        return jsonify({"ok": False, "error": f"Fan '{fan_cfg['name']}' is auto-controlled"}), 400

    gcode_template = fan_cfg.get("gcode_set", "")
    if not gcode_template:
        return jsonify({"ok": False, "error": "No gcode template for fan"}), 400

    # fan0 uses M106 S0-255, generic fans use SPEED 0.0-1.0
    if "M106" in gcode_template:
        value = int(speed * 255 / 100)
    else:
        value = round(speed / 100, 2)

    gcode = gcode_template.replace("{value}", str(value))
    ok, result = _send_moonraker_gcode(printer, gcode)
    if ok:
        _log.info("printer-fan-set: %s=%d%% on %s", fan_key, speed, printer.id)
        return jsonify({"ok": True, "fan": fan_key, "speed": speed})
    return jsonify({"ok": False, "error": str(result)}), 502


@app.route("/printer-move", methods=["POST"])
def printer_move():
    """Move toolhead on Klipper printer (relative).

    Body: {printer_id, axis, distance, speed}
    axis: X/Y/Z, distance: mm (signed), speed: mm/s (default 50)
    Requires state == ready.
    """
    body = request.get_json(force=True) or {}
    printer_id = body.get("printer_id")
    axis = body.get("axis", "").upper()
    distance = body.get("distance")
    speed = body.get("speed", 50)

    if axis not in ("X", "Y", "Z"):
        return jsonify({"ok": False, "error": "axis must be X, Y, or Z"}), 400
    if distance is None:
        return jsonify({"ok": False, "error": "Missing 'distance'"}), 400

    printer, err = _resolve_printer(printer_id, "klipper")
    if err:
        return err

    state = _get_print_state(printer)
    if state not in ("ready", "standby", "complete", "cancelled", "error"):
        return jsonify({"ok": False, "error": f"Movement blocked: printer is {state}"}), 409

    feed = int(float(speed) * 60)  # mm/s → mm/min
    gcode = f"G91\nG1 {axis}{float(distance):.2f} F{feed}\nG90"
    ok, result = _send_moonraker_gcode(printer, gcode)
    if ok:
        _log.info("printer-move: %s%s at %smm/s on %s", axis, distance, speed, printer.id)
        return jsonify({"ok": True, "axis": axis, "distance": distance})
    return jsonify({"ok": False, "error": str(result)}), 502


@app.route("/printer-extrude", methods=["POST"])
def printer_extrude():
    """Extrude or retract filament on Klipper printer.

    Body: {printer_id, length, speed}
    length: mm (positive=extrude, negative=retract), speed: mm/s (default 5)
    Requires state == ready AND nozzle >= 170C.
    """
    body = request.get_json(force=True) or {}
    printer_id = body.get("printer_id")
    length = body.get("length")
    speed = body.get("speed", 5)

    if length is None:
        return jsonify({"ok": False, "error": "Missing 'length'"}), 400

    printer, err = _resolve_printer(printer_id, "klipper")
    if err:
        return err

    state = _get_print_state(printer)
    if state not in ("ready", "standby", "complete", "cancelled", "error"):
        return jsonify({"ok": False, "error": f"Extrusion blocked: printer is {state}"}), 409

    # Check nozzle temperature for cold extrusion prevention
    try:
        url = f"{printer.moonraker_url}/printer/objects/query?extruder"
        with urllib.request.urlopen(url, timeout=5) as r:
            data = json.loads(r.read())
        nozzle_temp = data["result"]["status"]["extruder"].get("temperature", 0)
        if nozzle_temp < 170:
            return jsonify({"ok": False, "error": f"Nozzle too cold ({nozzle_temp:.0f}C < 170C)"}), 409
    except Exception as e:
        return jsonify({"ok": False, "error": f"Cannot check nozzle temp: {e}"}), 502

    feed = int(float(speed) * 60)
    gcode = f"M83\nG1 E{float(length):.2f} F{feed}\nM82"
    ok, result = _send_moonraker_gcode(printer, gcode)
    if ok:
        _log.info("printer-extrude: E%s at %smm/s on %s", length, speed, printer.id)
        return jsonify({"ok": True, "length": length})
    return jsonify({"ok": False, "error": str(result)}), 502


@app.route("/printer-home", methods=["POST"])
def printer_home():
    """Home axes on Klipper printer.

    Body: {printer_id, axes}
    axes: "XY", "Z", "ALL" (default "ALL")
    Requires state == ready.
    """
    body = request.get_json(force=True) or {}
    printer_id = body.get("printer_id")
    axes = body.get("axes", "ALL").upper()

    printer, err = _resolve_printer(printer_id, "klipper")
    if err:
        return err

    state = _get_print_state(printer)
    if state not in ("ready", "standby", "complete", "cancelled", "error"):
        return jsonify({"ok": False, "error": f"Homing blocked: printer is {state}"}), 409

    if axes == "ALL":
        gcode = "G28"
    elif axes == "XY":
        gcode = "G28 X Y"
    elif axes == "Z":
        gcode = "G28 Z"
    else:
        gcode = f"G28 {axes}"

    ok, result = _send_moonraker_gcode(printer, gcode)
    if ok:
        _log.info("printer-home: %s on %s", axes, printer.id)
        return jsonify({"ok": True, "axes": axes})
    return jsonify({"ok": False, "error": str(result)}), 502


@app.route("/printer-gcode", methods=["POST"])
def printer_gcode():
    """Send arbitrary gcode to Klipper printer with safety validation.

    Body: {printer_id, script}
    """
    body = request.get_json(force=True) or {}
    printer_id = body.get("printer_id")
    script = body.get("script", "").strip()

    if not script:
        return jsonify({"ok": False, "error": "Empty script"}), 400

    printer, err = _resolve_printer(printer_id, "klipper")
    if err:
        return err

    state = _get_print_state(printer)
    safe, reason = _validate_gcode_safety(script, state)
    if not safe:
        _log.warning("printer-gcode BLOCKED: %s (state=%s, script=%s)", reason, state, script[:80])
        return jsonify({"ok": False, "error": reason, "blocked": True}), 403

    ok, result = _send_moonraker_gcode(printer, script)
    if ok:
        _log.info("printer-gcode: '%s' on %s (state=%s)", script[:60], printer.id, state)
        return jsonify({"ok": True, "script": script, "result": str(result)})
    return jsonify({"ok": False, "error": str(result)}), 502


@app.route("/printer-calibrate", methods=["POST"])
def printer_calibrate():
    """Run calibration command on Klipper printer.

    Body: {printer_id, action}
    action: "qgl" (QUAD_GANTRY_LEVEL) or "bed_mesh" (BED_MESH_CALIBRATE)
    Requires state == ready.
    """
    body = request.get_json(force=True) or {}
    printer_id = body.get("printer_id")
    action = body.get("action", "").lower()

    if action not in ("qgl", "bed_mesh"):
        return jsonify({"ok": False, "error": "action must be 'qgl' or 'bed_mesh'"}), 400

    printer, err = _resolve_printer(printer_id, "klipper")
    if err:
        return err

    state = _get_print_state(printer)
    if state not in ("ready", "standby", "complete", "cancelled", "error"):
        return jsonify({"ok": False, "error": f"Calibration blocked: printer is {state}"}), 409

    gcode = "QUAD_GANTRY_LEVEL" if action == "qgl" else "BED_MESH_CALIBRATE"
    ok, result = _send_moonraker_gcode(printer, gcode)
    if ok:
        _log.info("printer-calibrate: %s on %s", gcode, printer.id)
        return jsonify({"ok": True, "action": action})
    return jsonify({"ok": False, "error": str(result)}), 502


@app.route("/printer-led", methods=["POST"])
def printer_led():
    """Control LED on Bambu printer.

    Body: {printer_id, mode}
    mode: "on", "off", "flash"
    """
    body = request.get_json(force=True) or {}
    printer_id = body.get("printer_id")
    mode = body.get("mode", "").lower()

    if mode not in ("on", "off", "flash"):
        return jsonify({"ok": False, "error": "mode must be 'on', 'off', or 'flash'"}), 400

    printer, err = _resolve_printer(printer_id, "bambu")
    if err:
        return err

    led_modes = {"on": {"led_node": "chamber_light", "led_mode": "on"},
                 "off": {"led_node": "chamber_light", "led_mode": "off"},
                 "flash": {"led_node": "chamber_light", "led_mode": "flashing"}}
    payload = {"system": {"sequence_id": "0", "command": "ledctrl", **led_modes[mode]}}
    ok, error = _send_bambu_mqtt(printer, payload)
    if ok:
        _log.info("printer-led: %s on %s", mode, printer.id)
        return jsonify({"ok": True, "mode": mode})
    return jsonify({"ok": False, "error": error or "MQTT failed"}), 502


@app.route("/printer-files")
def printer_files():
    """List gcode files on Klipper printer.

    Query: ?printer_id=sv08_max
    Returns file list with metadata.
    """
    printer_id = request.args.get("printer_id")
    printer, err = _resolve_printer(printer_id, "klipper")
    if err:
        return err

    try:
        url = f"{printer.moonraker_url}/server/files/list?root=gcodes"
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read())
        files = data.get("result", [])
        # Sort by modified time, most recent first
        files.sort(key=lambda f: f.get("modified", 0), reverse=True)
        return jsonify({"ok": True, "files": files})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "files": []}), 502


@app.route("/printer-file-metadata")
def printer_file_metadata():
    """Get metadata for a specific gcode file.

    Query: ?printer_id=sv08_max&filename=my_print.gcode
    """
    printer_id = request.args.get("printer_id")
    filename = request.args.get("filename", "")
    if not filename:
        return jsonify({"ok": False, "error": "Missing filename"}), 400

    printer, err = _resolve_printer(printer_id, "klipper")
    if err:
        return err

    try:
        enc = urllib.parse.quote(filename, safe="")
        url = f"{printer.moonraker_url}/server/files/metadata?filename={enc}"
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read())
        return jsonify({"ok": True, "metadata": data.get("result", {})})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502


@app.route("/printer-file-thumbnail")
def printer_file_thumbnail():
    """Serve a gcode file's thumbnail image.

    Query: ?printer_id=sv08_max&path=.thumbs/my_print.png
    """
    printer_id = request.args.get("printer_id")
    thumb_path = request.args.get("path", "")
    if not thumb_path:
        return jsonify({"ok": False, "error": "Missing path"}), 400

    printer, err = _resolve_printer(printer_id, "klipper")
    if err:
        return err

    try:
        enc = urllib.parse.quote(thumb_path, safe="/")
        url = f"{printer.moonraker_url}/server/files/gcodes/{enc}"
        with urllib.request.urlopen(url, timeout=10) as r:
            img_data = r.read()
            content_type = r.headers.get("Content-Type", "image/png")
        return Response(img_data, content_type=content_type)
    except Exception as e:
        return Response(f"Thumbnail not found: {e}", status=404)


@app.route("/printer-print-start", methods=["POST"])
def printer_print_start():
    """Start printing a file on Klipper printer.

    Body: {printer_id, filename}
    Requires state == ready.
    """
    body = request.get_json(force=True) or {}
    printer_id = body.get("printer_id")
    filename = body.get("filename", "").strip()

    if not filename:
        return jsonify({"ok": False, "error": "Missing filename"}), 400

    printer, err = _resolve_printer(printer_id, "klipper")
    if err:
        return err

    state = _get_print_state(printer)
    if state not in ("ready", "standby", "complete", "cancelled", "error"):
        return jsonify({"ok": False, "error": f"Cannot start print: printer is {state}"}), 409

    try:
        payload = json.dumps({"filename": filename}).encode()
        req = urllib.request.Request(
            f"{printer.moonraker_url}/printer/print/start",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            r.read()
        _log.info("printer-print-start: %s on %s", filename, printer.id)
        return jsonify({"ok": True, "filename": filename})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502


@app.route("/printer-history")
def printer_history():
    """Get print job history from Klipper/Moonraker.

    Query: ?printer_id=sv08_max&limit=20
    """
    printer_id = request.args.get("printer_id")
    limit = request.args.get("limit", "20")
    printer, err = _resolve_printer(printer_id, "klipper")
    if err:
        return err

    try:
        url = f"{printer.moonraker_url}/server/history/list?limit={int(limit)}&order=desc"
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read())
        jobs = data.get("result", {}).get("jobs", [])
        return jsonify({"ok": True, "jobs": jobs})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "jobs": []}), 502


# ── Printer status + images (so native app doesn't need wrapper) ──

@app.route("/printers")
def list_printers():
    """List all configured printers from registry."""
    return jsonify({"printers": _printer_registry.to_dict()})


@app.route("/printer-status")
def printer_status():
    """Serve cached printer status from daemon output."""
    status_file = os.path.join(_printer_registry.status_dir, "status.json")
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

    # Use registry to iterate printers, build response with legacy keys
    for printer in _printer_registry.list_enabled():
        legacy_key = _printer_registry.legacy_key(printer.id)
        printer_data = data.get("printers", {}).get(legacy_key, {})

        if printer_data.get("online"):
            pd = dict(printer_data)
            if printer.is_klipper and "state" in pd:
                pd["state"] = (pd["state"] or "unknown").capitalize()
            elif printer.is_bambu:
                pd.setdefault("layer", pd.pop("layer_num", None))
                pd.setdefault("total_layers", pd.pop("total_layer_num", None))
                speed_names = printer.speed_levels
                sl = pd.get("speed_level")
                pd["speed"] = speed_names.get(str(sl), "?") if sl else "?"
            # Use legacy response keys for backward compat
            resp_key = "sv08" if legacy_key == "sovol" else "a1" if legacy_key == "bambu" else printer.id
            result[resp_key] = pd
        else:
            resp_key = "sv08" if legacy_key == "sovol" else "a1" if legacy_key == "bambu" else printer.id
            result[resp_key] = {"error": printer_data.get("error", "Offline")}

    result["daemon_timestamp"] = data.get("timestamp", "")

    # Overlay live auto_speed state (daemon cache may be stale between polls)
    try:
        auto_speed_file = os.path.join(_printer_registry.status_dir, "auto_speed.json")
        with open(auto_speed_file) as asf:
            auto_cfg = json.load(asf)
        if "sv08" in result and isinstance(result["sv08"], dict) and "error" not in result["sv08"]:
            result["sv08"]["auto_speed_enabled"] = auto_cfg.get("enabled", False)
            result["sv08"]["auto_speed_mode"] = auto_cfg.get("mode", "optimal")
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    return jsonify(result)


@app.route("/printer-speed-auto", methods=["POST"])
def printer_speed_auto():
    """Toggle auto-speed on/off for the Sovol SV08.

    Accepts JSON: {"enabled": true/false}
    Writes to /tmp/printer_status/auto_speed.json (same file gcode_profile.py uses).
    """
    body = request.get_json(silent=True) or {}
    enabled = body.get("enabled")
    if enabled is None:
        return jsonify({"ok": False, "error": "Missing 'enabled' field"}), 400

    auto_speed_file = "/tmp/printer_status/auto_speed.json"
    try:
        with open(auto_speed_file) as f:
            cfg = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        cfg = {"enabled": False, "mode": "optimal", "min_speed_pct": 80,
               "max_speed_pct": 200, "skip_first_layers": 2}

    cfg["enabled"] = bool(enabled)
    with open(auto_speed_file, "w") as f:
        json.dump(cfg, f, indent=2)

    _log.info("printer-speed-auto: set enabled=%s", enabled)
    return jsonify({"ok": True, "enabled": cfg["enabled"]})


@app.route("/printer-speed-set", methods=["POST"])
def printer_speed_set():
    """Set manual speed factor on the Sovol SV08 via Moonraker M220 gcode.

    Accepts JSON: {"speed_pct": 150}
    Sends M220 S<pct> to Moonraker API.
    """
    import urllib.request
    import urllib.parse

    body = request.get_json(silent=True) or {}
    speed_pct = body.get("speed_pct")
    if speed_pct is None:
        return jsonify({"ok": False, "error": "Missing 'speed_pct' field"}), 400

    speed_pct = int(speed_pct)
    if not (10 <= speed_pct <= 300):
        return jsonify({"ok": False, "error": "speed_pct must be 10-300"}), 400

    # Get Klipper printer from registry (default to first klipper printer)
    printer_id = (request.get_json(silent=True) or {}).get("printer_id")
    try:
        printer = _printer_registry.get(printer_id) if printer_id else _printer_registry.list_klipper()[0]
        moonraker = printer.moonraker_url
    except (KeyError, IndexError):
        return jsonify({"ok": False, "error": "No klipper printer found"}), 404
    cmd = f"M220 S{speed_pct}"
    enc = urllib.parse.quote(cmd)
    url = f"{moonraker}/printer/gcode/script?script={enc}"

    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            result = json.loads(r.read())
        ok = result.get("result") == "ok"
    except Exception as e:
        _log.error("printer-speed-set: Moonraker error: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 502

    _log.info("printer-speed-set: M220 S%d -> ok=%s", speed_pct, ok)
    return jsonify({"ok": ok, "speed_pct": speed_pct})


@app.route("/printer-image/<name>")
def printer_image(name):
    """Serve camera snapshots and thumbnails."""
    if name not in _printer_registry.image_whitelist():
        return "Not found", 404
    img_dir = _printer_registry.status_dir
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

_prev_printer_state = {p.id: None for p in _printer_registry.list_enabled()}
_ai_check_counter = [0]  # Mutable so nested function can modify
_last_obico_alert_ts = [None]  # Track last Obico alert to avoid duplicates
_last_bambu_ai_alert = [0]  # Timestamp of last Bambu AI alert (cooldown)
_last_obico_check = [0]  # Timestamp of last Obico check
_last_bambu_ai_check = [0]  # Timestamp of last Bambu AI check

# Configurable AI check intervals (seconds, 0 = off)
_ai_config = {
    "bambu_interval": 0,     # Disabled — too many false positives (2026-03-01)
    "sv08_interval": 300,    # Default 5 min
}
_ai_config_lock = threading.Lock()


def _check_obico_alerts():
    """Poll Obico API for failure detection alerts on printers with obico feature."""
    # Find printers with Obico configured
    obico_printers = [p for p in _printer_registry.list_enabled() if p.has_feature("obico")]
    if not obico_printers:
        return

    for printer in obico_printers:
        _check_obico_for_printer(printer)


def _check_obico_for_printer(printer):
    """Check Obico for a single printer."""
    obico = printer.get_obico_config()
    if not obico.get("api_token") or not obico.get("printer_id"):
        return

    try:
        import urllib.request
        url = f"{obico['base_url']}/api/v1/printers/{obico['printer_id']}/"
        req = urllib.request.Request(url)
        req.add_header("Authorization", f"Token {obico['api_token']}")
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
                "printer": printer.id,
                "event": "failure_detected",
                "message": f"Obico AI detected possible print failure on {printer.name} "
                           f"(confidence: {confidence_pct}%). Check camera immediately.",
            })
            _log.warning("Obico alert: %s failure p=%.3f", printer.name, failure_p)

    except Exception as e:
        # Don't spam logs for connection errors when Obico isn't set up
        if "URLError" not in type(e).__name__:
            _log.warning("Obico check error: %s", e)


def _check_bambu_camera_ai(printer_data):
    """Analyse Bambu camera image with Claude Haiku for failure detection."""
    bambu_printers = _printer_registry.list_bambu()
    if not bambu_printers:
        return

    for printer in bambu_printers:
        legacy_key = _printer_registry.legacy_key(printer.id)
        bambu = printer_data.get(printer.id) or printer_data.get("printers", {}).get(legacy_key, {})
        state = (bambu.get("state") or "").upper()
        if state != "RUNNING":
            continue

        # Cooldown: don't alert more than once per check interval
        with _ai_config_lock:
            interval = _ai_config.get(f"{legacy_key}_interval", _ai_config.get("bambu_interval", 0))
        if interval <= 0:
            continue
        if time.time() - _last_bambu_ai_alert[0] < interval:
            continue

        # Check camera image exists and is fresh
        freshness_map = {60: 120, 300: 300, 900: 600, 3600: 1800}
        max_age = freshness_map.get(interval, max(120, interval // 2))
        cam_path = os.path.join(_printer_registry.status_dir, printer.camera_snapshot_file)
        if not os.path.exists(cam_path):
            continue
        age = time.time() - os.path.getmtime(cam_path)
        if age > max_age:
            continue

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
                    "printer": printer.id,
                    "event": "ai_failure_detected",
                    "message": f"AI print analysis on {printer.name} ({severity}): {result}",
                })
                _log.warning("%s AI alert: %s", printer.name, result)
            else:
                _log.info("%s AI check: %s", printer.name, result)

        except Exception as e:
            _log.warning("%s AI check error: %s", printer.name, e)
_printer_watches = []  # [{printer, field, op, value, message, id}]
_printer_watches_lock = threading.Lock()


def _notify_slack(message):
    """Send a Slack DM to Tim for critical printer alerts."""
    try:
        from credentials import SLACK_BOT_TOKEN
    except ImportError:
        return
    if not SLACK_BOT_TOKEN:
        return
    slack_user_id = "U03H1AN51MZ"  # Tim Trailor
    try:
        data = json.dumps({
            "channel": slack_user_id,
            "text": f":warning: *Printer Alert*\n{message}",
        }).encode()
        req = urllib.request.Request(
            "https://slack.com/api/chat.postMessage",
            data=data, method="POST",
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
            },
        )
        resp = json.loads(urllib.request.urlopen(req, timeout=10).read())
        if resp.get("ok"):
            _log.info("Slack DM sent for printer alert")
        else:
            _log.warning("Slack API error: %s", resp.get("error", "unknown"))
    except Exception as exc:
        _log.warning("Slack notification failed: %s", exc)


def _notify_email(subject, message):
    """Send email via Gmail SMTP for critical printer alerts."""
    try:
        from credentials import SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS
    except ImportError:
        return
    if not SMTP_USER:
        return
    notify_email = "timtrailor@gmail.com"
    try:
        import smtplib
        from email.mime.text import MIMEText
        msg = MIMEText(message)
        msg["Subject"] = subject
        msg["From"] = SMTP_USER
        msg["To"] = notify_email
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)
        _log.info("Email sent to %s for printer alert", notify_email)
    except Exception as exc:
        _log.warning("Email notification failed: %s", exc)


# Events that should trigger multi-channel alerts (Slack + email + push)
_CRITICAL_EVENTS = {
    "firmware_error", "state_change",
    "config_corruption", "ups_warning", "ups_critical",
}
# Only certain state changes are critical
_CRITICAL_STATE_MESSAGES = {"error", "cancelled", "complete", "paused"}


def _broadcast_ws(event_dict):
    """Send event to all connected WebSocket clients.

    Critical alerts (errors, failures, UPS) are also sent via Slack + email.
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
    _log.info("Broadcast: %s", event_dict.get('message', ''))

    # Multi-channel alerting for critical events
    event_type = event_dict.get("event", "")
    alert_msg = event_dict.get("message", "")
    is_critical = event_type in _CRITICAL_EVENTS

    # For state_change events, only alert on critical transitions
    if event_type == "state_change":
        new_state = event_dict.get("new_state", "").lower()
        is_critical = any(kw in new_state for kw in _CRITICAL_STATE_MESSAGES)

    if is_critical and alert_msg:
        # Run Slack + email in background threads so broadcasting doesn't block
        threading.Thread(
            target=_notify_slack, args=(alert_msg,), daemon=True
        ).start()
        threading.Thread(
            target=_notify_email,
            args=(f"[Printer] {alert_msg[:60]}", alert_msg),
            daemon=True,
        ).start()


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

            # Check state changes for all registered printers
            printers_data = data.get("printers", {})
            for printer in _printer_registry.list_enabled():
                legacy_key = _printer_registry.legacy_key(printer.id)
                pdata = printers_data.get(legacy_key, {})
                current_state = (pdata.get("state") or "unknown").lower()
                old_state = _prev_printer_state.get(printer.id)
                if old_state and old_state != current_state:
                    msg = _describe_state_change(printer.name, old_state, current_state, pdata)
                    if msg:
                        _broadcast_ws({
                            "type": "printer_alert",
                            "printer": printer.id,
                            "event": "state_change",
                            "old_state": old_state,
                            "new_state": current_state,
                            "message": msg,
                        })
                _prev_printer_state[printer.id] = current_state

            # Check custom watches
            with _printer_watches_lock:
                triggered = []
                for watch in _printer_watches:
                    # Resolve watch printer to registry data
                    watch_printer_id = watch["printer"]
                    try:
                        wp = _printer_registry.get(watch_printer_id)
                        wkey = _printer_registry.legacy_key(wp.id)
                    except KeyError:
                        wkey = watch_printer_id
                    printer_data = printers_data.get(wkey, {})
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
_last_alert_pos = [None]  # File offset — None means "skip to end on first read"


def _check_printer_alert_file():
    """Read new alerts written by printer_status_fetch and broadcast them.

    The fetch process appends alerts (method changes, connection lost/restored)
    to printer_alerts.jsonl. We track our read position and only broadcast
    new entries, then truncate the file to prevent unbounded growth.

    On first call after server start, we seek to the end of the file so we
    only broadcast alerts that arrive AFTER the server starts — not old ones.
    """
    if not os.path.exists(_PRINTER_ALERT_FILE):
        return

    try:
        with open(_PRINTER_ALERT_FILE, "r") as f:
            # On first read after server start, skip to end — don't replay old alerts
            if _last_alert_pos[0] is None:
                f.seek(0, 2)  # seek to end
                _last_alert_pos[0] = f.tell()
                return

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
    printer_id = request.args.get("printer_id")
    try:
        printer = _printer_registry.get(printer_id) if printer_id else _printer_registry.list_klipper()[0]
        moonraker = printer.moonraker_url
    except (KeyError, IndexError):
        return jsonify({"error": "No klipper printer found", "objects": []}), 404
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
    printer_id = (request.get_json(force=True) or {}).get("printer_id")
    try:
        printer = _printer_registry.get(printer_id) if printer_id else _printer_registry.list_klipper()[0]
        moonraker = printer.moonraker_url
    except (KeyError, IndexError):
        return jsonify({"error": "No klipper printer found"}), 404
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

    result = {"emails": [], "events": [], "accounts": []}

    for account in active_accounts:
        acct_label, acct_color = account["label"], account["color"]
        try:
            creds = Credentials.from_authorized_user_file(account["token"], GOOGLE_ALL_SCOPES)
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
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

    # Base path: ~/code/ (canonical location since 2026-03-20 migration)
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

    # 4. School docs backup status
    school_docs = Path.home() / "Desktop" / "school docs"
    if school_docs.exists():
        # Use subprocess to count files (avoids TCC issues with rglob in launchd)
        try:
            import subprocess
            out = subprocess.check_output(
                ["find", str(school_docs), "-type", "f", "-not", "-name", ".*"],
                text=True, timeout=10, stderr=subprocess.DEVNULL,
            )
            doc_count = len(out.strip().split("\n")) if out.strip() else 0
        except Exception:
            doc_count = 0
        # Check backup manifest for school docs
        manifest = _find(".backup_manifest.json")
        school_in_backup = False
        if manifest.exists():
            try:
                with open(manifest) as mf:
                    mdata = json.load(mf)
                school_in_backup = any("school" in k.lower() for k in mdata.get("files", {}))
            except Exception:
                pass
        detail = f"{doc_count} files, {'backed up' if school_in_backup else 'NOT backed up'}"
        items.append({
            "name": "School Docs",
            "timestamp": datetime.fromtimestamp(school_docs.stat().st_mtime).isoformat(),
            "detail": detail,
        })
    else:
        items.append({"name": "School Docs", "timestamp": None, "detail": "folder not found"})

    # 5. Memory search DB freshness
    chroma_dir = _find("memory_server_data/chroma")
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
    # Git repo checks — _base resolves via __file__ to ~/code/
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


# ---------------------------------------------------------------------------
# Session Logs API — list and read Claude conversation JSONL logs
# ---------------------------------------------------------------------------

_SESSION_LOGS_DIR = os.path.expanduser(
    "~/.claude/projects/-Users-timtrailor-Documents-Claude-code"
)

@app.route("/session-logs")
def session_logs_list():
    """Return a list of session logs sorted newest-first.

    Each entry: {id, filename, date, size_bytes, first_message, message_count}
    Query params: limit (default 50), offset (default 0)
    """
    from pathlib import Path

    log_dir = Path(_SESSION_LOGS_DIR)
    if not log_dir.is_dir():
        return jsonify({"sessions": [], "error": "logs directory not found"})

    limit = min(int(request.args.get("limit", 50)), 200)
    offset = int(request.args.get("offset", 0))

    # Collect all .jsonl files with metadata
    sessions = []
    for p in log_dir.glob("*.jsonl"):
        stat = p.stat()
        session_id = p.stem

        # Quick scan: read first user message and count messages
        first_msg = ""
        msg_count = 0
        last_ts = None
        try:
            with open(p, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if obj.get("type") == "user":
                        msg_count += 1
                        if not first_msg:
                            # Extract text from message content
                            msg = obj.get("message", {})
                            if isinstance(msg, dict):
                                content = msg.get("content", "")
                                if isinstance(content, list):
                                    for part in content:
                                        if isinstance(part, dict) and part.get("type") == "text":
                                            first_msg = part.get("text", "")[:200]
                                            break
                                elif isinstance(content, str):
                                    first_msg = content[:200]
                            elif isinstance(msg, str):
                                first_msg = msg[:200]
                    elif obj.get("type") == "assistant":
                        msg_count += 1
        except Exception:
            pass

        sessions.append({
            "id": session_id,
            "filename": p.name,
            "date": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            "size_bytes": stat.st_size,
            "first_message": first_msg or "(no user message found)",
            "message_count": msg_count,
        })

    # Sort newest first
    sessions.sort(key=lambda s: s["date"], reverse=True)
    total = len(sessions)
    sessions = sessions[offset : offset + limit]

    return jsonify({"sessions": sessions, "total": total})


@app.route("/session-logs/<session_id>")
def session_logs_read(session_id):
    """Read a specific session log. Returns user/assistant messages.

    Query params: summary (bool, default true) — if true, return only
    user messages and abbreviated assistant messages for quick review.
    """
    from pathlib import Path
    import re

    # Sanitise session_id to prevent path traversal
    if not re.match(r"^[a-f0-9\-]{36}$", session_id):
        return jsonify({"error": "invalid session id"}), 400

    log_path = Path(_SESSION_LOGS_DIR) / f"{session_id}.jsonl"
    if not log_path.exists():
        return jsonify({"error": "session not found"}), 404

    summary = request.args.get("summary", "true").lower() == "true"
    messages = []

    try:
        with open(log_path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                msg_type = obj.get("type")
                if msg_type not in ("user", "assistant"):
                    continue

                # Extract text content
                text = ""
                msg = obj.get("message", {})
                if isinstance(msg, dict):
                    content = msg.get("content", "")
                    if isinstance(content, list):
                        parts = []
                        for part in content:
                            if isinstance(part, dict) and part.get("type") == "text":
                                parts.append(part.get("text", ""))
                        text = "\n".join(parts)
                    elif isinstance(content, str):
                        text = content
                elif isinstance(msg, str):
                    text = msg

                if not text.strip():
                    continue

                if summary and msg_type == "assistant":
                    # Truncate long assistant messages in summary mode
                    text = text[:500] + ("..." if len(text) > 500 else "")

                messages.append({
                    "type": msg_type,
                    "text": text,
                })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return jsonify({
        "session_id": session_id,
        "message_count": len(messages),
        "messages": messages,
    })


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
    for account in active_accounts:
        try:
            creds = Credentials.from_authorized_user_file(account["token"], GOOGLE_ALL_SCOPES)
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


def _keychain_keepalive():
    """Background thread: unlocks macOS keychain every 5 minutes.

    The Mac Mini's keychain re-locks unpredictably (sleep, idle, reboot)
    even with set-keychain-settings no-timeout. When locked, Claude CLI
    can't read its OAuth token → auth errors in the terminal tab.

    This thread keeps the keychain unlocked as long as the server is running.
    """
    try:
        from credentials import KEYCHAIN_PASSWORD
    except (ImportError, AttributeError):
        _log.warning("keychain-keepalive: no KEYCHAIN_PASSWORD in credentials.py — disabled")
        return

    kc_path = os.path.expanduser("~/Library/Keychains/login.keychain-db")
    while True:
        try:
            result = subprocess.run(
                ["security", "unlock-keychain", "-p", KEYCHAIN_PASSWORD, kc_path],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode != 0:
                _log.warning("keychain-keepalive: unlock failed: %s", result.stderr.strip())
        except Exception as e:
            _log.warning("keychain-keepalive: error: %s", e)
        time.sleep(300)  # Every 5 minutes


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

    # Start keychain keepalive (prevents auth failures from locked keychain)
    kc_thread = threading.Thread(target=_keychain_keepalive, daemon=True, name="keychain-keepalive")
    kc_thread.start()
    _log.info("Keychain keepalive started (unlock every 5 min)")

    # Start session/upload cleanup thread (hourly)
    cleaner = threading.Thread(target=_cleanup_thread, daemon=True)
    cleaner.start()
    _log.info("Cleanup thread started (sessions >24h, uploads >1h)")

    # Start work status cache thread (Gmail + Calendar + Slack)
    work_t = threading.Thread(target=_work_cache_thread, daemon=True, name="work-cache")
    work_t.start()
    _log.info("Work cache thread started (60s refresh)")

    app.run(host="0.0.0.0", port=8081, threaded=True)
