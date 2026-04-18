#!/usr/bin/env python3
"""Conversation server — owns Claude CLI subprocess lifecycle.

Runs as a persistent launchd daemon on port 8081.
Split into conv/ package (2026-04-12). See CLAUDE.md for module map.
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

# Shared foundation — Flask app, auth, config, session state
from conv.app import (  # noqa: F401
    app, sock, _log, _lock, _session, _last_response,
    _last_response_lock, _event_condition,
    WORK_DIR, SESSIONS_DIR, _TMP_LOG_DIR, MCP_CONFIG,
    env_for_claude_cli, _printer_registry, AUTH_TOKEN,
    check_auth,  # re-exported for backward compat with existing tests
)
from flask import Response, jsonify, request

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

                # Notify when Claude finishes (smart routing: WS if connected, else push).
                # Body is a Haiku-generated ~100-word summary of the last 100 lines;
                # falls back to raw truncation if the API call fails.
                def _summarise_and_notify(raw=response_text):
                    body = _summarise_for_push(raw)
                    if not body:
                        body = (raw[:100] + "...") if len(raw) > 100 else (raw or "Task complete")
                    _notify_smart("Claude finished", body)
                    try:
                        _liveactivity_on_turn_complete("mobile", body)
                    except Exception as exc:
                        _log.error("LiveActivity turn-complete hook failed: %s", exc, exc_info=True)

                threading.Thread(
                    target=_summarise_and_notify,
                    daemon=True,
                ).start()

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
        try:
            _liveactivity_on_turn_error("mobile", str(e))
        except Exception as exc:
            _log.error("LiveActivity turn-error hook failed: %s", exc, exc_info=True)

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
            stderr_path = os.path.join(_TMP_LOG_DIR, "claude_stderr.log")
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

    # Kick a thinking-phase Live Activity push (push-to-start or update).
    try:
        threading.Thread(
            target=_liveactivity_on_turn_start,
            kwargs={"session_label": "mobile", "headline": "Working…"},
            daemon=True,
        ).start()
    except Exception as exc:
        _log.error("LiveActivity turn-start hook failed: %s", exc, exc_info=True)

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

# --- APNs Push Notification Support (multi-app) ---
# Extracted 2026-04-18 to conv/apns_tokens.py (Phase 3 step 2).
# The `store` dict and load/save helpers are imported as their legacy names
# so callers continue to work without change.
from conv.apns_tokens import (  # noqa: E402
    store as _apns_device_tokens,
    load as _load_apns_tokens,
    TOKENS_FILE as _APNS_TOKENS_FILE,
)
from conv.apns_tokens import _lock as _apns_device_tokens_lock  # noqa: E402

def _save_apns_tokens():
    """Thin shim — delegates to conv.apns_tokens.save(store)."""
    from conv.apns_tokens import save
    save(_apns_device_tokens)


# ---- Live Activity (ActivityKit push-to-start + update) support ----
# Persisted state for push-to-start tokens (per bundle) and per-activity
# update tokens. State is written to disk so it survives daemon restart.
_LIVEACTIVITY_FILE = "/Users/timtrailor/code/apns_state/liveactivity_tokens.json"
_liveactivity_lock = threading.Lock()
_LIVEACTIVITY_BUNDLE = "com.timtrailor.terminal"
_LIVEACTIVITY_TOPIC = "com.timtrailor.terminal.push-type.liveactivity"


def _load_liveactivity_state():
    """Load start-token set and per-activity token map from disk.

    On a corrupt file (truncated write after a crash, partial JSON), the
    old file is renamed to .corrupt.<ts> for post-mortem and an empty
    state is returned so the daemon can still start. This is a push-
    notification cache, not session-critical state — failing closed would
    take down every unrelated feature over a cache problem.
    """
    try:
        with open(_LIVEACTIVITY_FILE) as f:
            data = json.loads(f.read())
    except FileNotFoundError:
        return set(), {}
    except (json.JSONDecodeError, ValueError, OSError) as exc:
        _log.error("LiveActivity state file corrupt, resetting: %s",
                   exc, exc_info=True)
        try:
            os.rename(_LIVEACTIVITY_FILE,
                      f"{_LIVEACTIVITY_FILE}.corrupt.{int(time.time())}")
        except OSError as rename_exc:
            _log.error("Could not rename corrupt LiveActivity state: %s",
                       rename_exc, exc_info=True)
        return set(), {}
    start_tokens = set(data.get("start_tokens", {}).get(_LIVEACTIVITY_BUNDLE, []))
    activities = data.get("activities", {}) or {}
    return start_tokens, activities


def _save_liveactivity_state():
    """Persist Live Activity state to disk. Caller holds _liveactivity_lock.

    Does not re-raise on I/O errors — state is kept in memory and the
    failure is logged with a full traceback. A disk-full or permission
    error must not kill the Flask request handler or the background
    timer that called us.
    """
    payload = {
        "start_tokens": {_LIVEACTIVITY_BUNDLE: sorted(_liveactivity_start_tokens)},
        "activities": _liveactivity_tokens,
    }
    tmp = _LIVEACTIVITY_FILE + ".tmp"
    try:
        with open(tmp, "w") as f:
            f.write(json.dumps(payload))
        os.replace(tmp, _LIVEACTIVITY_FILE)
    except OSError as exc:
        _log.error("Failed to persist LiveActivity state: %s",
                   exc, exc_info=True)


_liveactivity_start_tokens, _liveactivity_tokens = _load_liveactivity_state()
# Per-session-label turn state: started_at, headline, dismiss_timer, generation.
# `generation` increments on every new turn; dismiss-timer closures capture
# the generation they were scheduled under and bail out if the turn has
# advanced. This is belt-and-braces alongside timer.cancel() because
# threading.Timer.cancel() is best-effort — if the worker thread has
# already started running, cancel is a no-op. Guarded by
# _liveactivity_turn_state_lock (NOT _liveactivity_lock — the token state
# and the turn state are independent and we want to avoid holding the
# token lock across APNs I/O).
_liveactivity_turn_state = {}
_liveactivity_turn_state_lock = threading.Lock()

# APNs endpoint — sandbox is correct as long as the app is signed with
# aps-environment=development (see TerminalApp.entitlements). If the
# entitlement ever flips to production, update this URL AND the
# _send_push_notification path in lockstep.
_APNS_HOST = "https://api.sandbox.push.apple.com"


# APNs JWT extracted 2026-04-18 — Phase 3 decomposition step 1.
# Original implementation lived here; now in conv/apns.py.
from conv.apns import apns_jwt as _apns_jwt  # noqa: F401,E402


def _push_liveactivity(token, *, event, content_state,
                       attributes=None, dismissal_date=None,
                       alert_title=None, alert_body=None, stale_date=None):
    """Send an ActivityKit APNs push. event is start, update or end.

    Returns (status_code, response_body). Does NOT raise on APNs-level
    failure (400/410 are logged and handled); only raises on programmer
    errors (bad args, missing credentials, curl not runnable).
    """
    if event not in ("start", "update", "end"):
        raise ValueError(f"invalid event: {event}")
    if event == "start" and attributes is None:
        raise ValueError("attributes required for start event")

    import subprocess as _sp
    import time as _time

    aps = {
        "timestamp": int(_time.time()),
        "event": event,
        "content-state": content_state,
    }
    if event == "start":
        aps["attributes-type"] = "ClaudeActivityAttributes"
        aps["attributes"] = attributes
    if stale_date is not None:
        aps["stale-date"] = int(stale_date)
    if event == "end" and dismissal_date is not None:
        aps["dismissal-date"] = int(dismissal_date)
    if alert_title or alert_body:
        aps["alert"] = {
            "title": alert_title or "",
            "body": alert_body or "",
        }
    payload = json.dumps({"aps": aps})

    jwt_token = _apns_jwt()
    url = f"{_APNS_HOST}/3/device/{token}"

    # apns-topic for Live Activities is "<main-app-bundle-id>.push-type.liveactivity"
    # per Apple docs (developer.apple.com/documentation/activitykit/
    # starting-and-updating-live-activities-with-activitykit-push-notifications).
    # This is NOT the widget extension bundle ID.
    result = _sp.run([
        "curl", "-s", "--http2",
        "--connect-timeout", "5",
        "--max-time", "12",
        "-H", f"authorization: bearer {jwt_token}",
        "-H", f"apns-topic: {_LIVEACTIVITY_TOPIC}",
        "-H", "apns-push-type: liveactivity",
        "-H", "apns-priority: 10",
        "-d", payload,
        "-w", "\n%{http_code}",
        url,
    ], capture_output=True, text=True, timeout=15)

    stdout = (result.stdout or "").strip()
    lines = stdout.split("\n") if stdout else []
    status_code = lines[-1] if lines else "?"
    response_body = "\n".join(lines[:-1])
    if result.returncode != 0 and not stdout:
        _log.error("LiveActivity curl exit=%s stderr=%s",
                   result.returncode, (result.stderr or "").strip()[:500])
        return status_code, response_body

    if status_code == "200":
        _log.info("LiveActivity push ok (event=%s token=%s...)", event, token[:8])
        try:
            now_ts = _time.time()
            with _liveactivity_lock:
                for _aid, rec in _liveactivity_tokens.items():
                    if rec.get("token") == token:
                        rec["last_update"] = now_ts
        except Exception as exc:
            _log.warning("Failed to bump last_update: %s", exc)
    elif status_code == "410":
        _log.info("LiveActivity token %s... invalid — removing", token[:8])
        with _liveactivity_lock:
            _liveactivity_start_tokens.discard(token)
            # list() snapshot — never iterate a dict while popping from it,
            # even under a lock. Pattern 3 (silent failure) hardening.
            stale_aids = [aid for aid, rec in list(_liveactivity_tokens.items())
                          if rec.get("token") == token]
            for aid in stale_aids:
                _liveactivity_tokens.pop(aid, None)
            _save_liveactivity_state()
    else:
        _log.warning("LiveActivity push failed (event=%s HTTP %s): %s",
                     event, status_code, response_body)
    return status_code, response_body


# Activities older than this are assumed dead on the device (iOS may have
# dismissed them but APNs still accepts updates to the stale token). Pruning
# them forces the `_liveactivity_on_turn_start` flow to fall through to
# push-to-start, creating a fresh activity that iOS will actually render.
_LIVEACTIVITY_MAX_AGE_SEC = 600  # 10 minutes


_LIVEACTIVITY_TURN_STATE_MAX_AGE_SEC = 2 * 60 * 60  # 2 hours


def _prune_stale_turn_state():
    """Drop _liveactivity_turn_state entries whose started_at is older
    than 2h. Called from _liveactivity_active_tokens so we don't need a
    separate sweeper thread."""
    now = time.time()
    stale = []
    with _liveactivity_turn_state_lock:
        for label, rec in list(_liveactivity_turn_state.items()):
            started = rec.get("started_at") or 0
            if started and (now - started) > _LIVEACTIVITY_TURN_STATE_MAX_AGE_SEC:
                # Cancel any pending dismiss timer so the GC doesn't leak it.
                timer = rec.get("dismiss_timer")
                if timer is not None:
                    try:
                        timer.cancel()
                    except Exception:
                        pass
                stale.append(label)
        for label in stale:
            _liveactivity_turn_state.pop(label, None)
    if stale:
        _log.info("Pruned %d stale LA turn_state record(s)", len(stale))


def _liveactivity_active_tokens(session_label):
    """Return (activity_id, token) pairs for activities matching the label
    whose last_update is within _LIVEACTIVITY_MAX_AGE_SEC. Stale records
    are pruned inline so the next call can trigger push-to-start.
    Also opportunistically prunes _liveactivity_turn_state."""
    _prune_stale_turn_state()
    out = []
    now = time.time()
    stale = []
    with _liveactivity_lock:
        for aid, rec in list(_liveactivity_tokens.items()):
            attrs = rec.get("attributes") or {}
            tok = rec.get("token")
            if not tok:
                stale.append(aid)
                continue
            if attrs.get("sessionLabel") != session_label:
                continue
            last_update = rec.get("last_update", 0) or 0
            if now - last_update > _LIVEACTIVITY_MAX_AGE_SEC:
                stale.append(aid)
                continue
            out.append((aid, tok))
        if stale:
            for aid in stale:
                _liveactivity_tokens.pop(aid, None)
            try:
                _save_liveactivity_state()
                _log.info("Pruned %d stale LiveActivity record(s): %s",
                          len(stale), ", ".join(stale))
            except Exception as exc:
                _log.warning("Failed to persist pruned LA state: %s", exc)
    return out


# Swift Date uses 2001-01-01 00:00:00 UTC as its reference. JSONEncoder's
# default strategy emits a Double of TimeIntervalSinceReferenceDate, so we
# must subtract the Unix-to-Swift epoch delta before sending to iOS.
_SWIFT_EPOCH_DELTA = 978307200.0


def _swift_date(unix_ts: float) -> float:
    return float(unix_ts) - _SWIFT_EPOCH_DELTA


def _latest_user_prompt_for_session(session_label: str) -> str:
    """Return the active Claude session's headline for a tmux session_label.

    Uses the same ps/lsof/JSONL pipeline as the tab-summary lookup.
    Falls back to empty string if nothing is running or no match is found.
    """
    import subprocess as _sp
    try:
        tmux_target = _tmux_target_for_label(session_label)
        disp = _sp.run(
            ["/opt/homebrew/bin/tmux", "display-message", "-p",
             "-t", tmux_target, "-F", "#{pane_pid}"],
            capture_output=True, text=True, timeout=3,
        )
        if disp.returncode != 0:
            return ""
        raw = (disp.stdout or "").strip()
        if not raw:
            return ""
        pane_pid = int(raw)
        jsonl = _find_claude_session_file(pane_pid)
        if not jsonl:
            return ""
        return _extract_session_summary(jsonl)
    except Exception:
        return ""


_mobile_watcher_state = {}   # {pane_pid: last_user_uuid}
_mobile_watcher_started = False
_mobile_watcher_lock = threading.Lock()


def _latest_real_user_uuid(jsonl_path: str) -> tuple[str, str]:
    """Return (uuid, text) of the newest non-tool-result user message in the
    tail of a Claude session JSONL, or ("", "") if none is found."""
    import os as _os
    import json as _json
    try:
        size = _os.path.getsize(jsonl_path)
        to_read = min(size, 64 * 1024)
        with open(jsonl_path, "rb") as f:
            f.seek(max(0, size - to_read))
            tail = f.read().decode("utf-8", errors="replace")
    except Exception:
        return "", ""
    for line in reversed(tail.strip().split("\n")):
        if not line.strip():
            continue
        try:
            obj = _json.loads(line)
        except Exception:
            continue
        if obj.get("type") != "user":
            continue
        msg = obj.get("message") or {}
        content = msg.get("content")
        txt = ""
        if isinstance(content, str):
            txt = content
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    txt = part.get("text") or ""
                    break
        cleaned = _clean_user_text(txt)
        if cleaned and len(cleaned) >= 3 and not cleaned.startswith("<"):
            uuid = obj.get("uuid") or obj.get("promptId") or ""
            return uuid, cleaned
    return "", ""


def _mobile_session_watcher_loop():
    """Tail every mobile-session Claude JSONL and fire a Live Activity
    turn-start whenever a new real user message appears. This replaces the
    need for the iOS app to explicitly call /watch-tmux — any tab spawned in
    the mobile tmux session is implicitly app-owned, and the watcher makes
    the Dynamic Island track activity that originates outside the app too
    (e.g. messages typed directly at the Mac Mini or via mosh)."""
    import subprocess as _sp
    _log.info("mobile watcher: starting loop")
    while True:
        try:
            r = _sp.run(
                ["/opt/homebrew/bin/tmux", "list-panes", "-s", "-t", "mobile",
                 "-F", "#{pane_pid}\t#{window_index}"],
                capture_output=True, text=True, timeout=3,
            )
            if r.returncode != 0:
                time.sleep(5)
                continue
            live_pids = set()
            for raw in r.stdout.strip().split("\n"):
                raw = raw.strip()
                if not raw:
                    continue
                parts = raw.split("\t")
                try:
                    pane_pid = int(parts[0])
                    window_idx = int(parts[1]) if len(parts) > 1 else 0
                except ValueError:
                    continue
                live_pids.add(pane_pid)
                jsonl = _find_claude_session_file(pane_pid)
                if not jsonl:
                    continue
                uuid, _txt = _latest_real_user_uuid(jsonl)
                if not uuid:
                    continue
                prior = _mobile_watcher_state.get(pane_pid)
                if prior is None:
                    _mobile_watcher_state[pane_pid] = uuid
                    continue
                if uuid != prior:
                    _mobile_watcher_state[pane_pid] = uuid
                    la_label = f"mobile-{window_idx}"
                    _log.info("mobile watcher: new user message in pane %s (tab %s) uuid=%s — firing LA turn_start on %s",
                              pane_pid, window_idx, uuid[:8], la_label)
                    try:
                        threading.Thread(
                            target=_liveactivity_on_turn_start,
                            kwargs={"session_label": la_label},
                            daemon=True,
                        ).start()
                    except Exception as exc:
                        _log.warning("mobile watcher: turn_start spawn failed: %s", exc)
            # GC: drop state for panes that have been closed.
            for pid in list(_mobile_watcher_state.keys()):
                if pid not in live_pids:
                    _mobile_watcher_state.pop(pid, None)
        except Exception as exc:
            _log.warning("mobile watcher loop error: %s", exc)
        time.sleep(5)


def _start_mobile_session_watcher():
    global _mobile_watcher_started
    with _mobile_watcher_lock:
        if _mobile_watcher_started:
            return
        _mobile_watcher_started = True
    threading.Thread(target=_mobile_session_watcher_loop,
                     daemon=True,
                     name="mobile-session-watcher").start()


def _liveactivity_on_turn_start(session_label="mobile", headline="Working…"):
    """Push a thinking-phase update (or start a fresh activity) for this turn.

    Cancels any previously scheduled auto-dismiss timer for this session
    and bumps the generation counter, so a new turn starting within the
    dismiss window cannot be killed mid-flight by a stale timer.
    """
    started_at = time.time()
    # If caller passed the default placeholder, try to resolve a real label
    # from the active Claude session JSONL. Falls back cleanly if no session
    # is running in the pane yet.
    if headline in ("", "Working…", "Working..."):
        import subprocess as _sp
        headline = "Working…"
        try:
            tmux_target = _tmux_target_for_label(session_label)
            disp = _sp.run(
                ["/opt/homebrew/bin/tmux", "display-message", "-p",
                 "-t", tmux_target, "-F", "#{pane_pid}"],
                capture_output=True, text=True, timeout=3,
            )
            if disp.returncode == 0 and disp.stdout.strip():
                pane_pid = int(disp.stdout.strip())
                jsonl_path = _find_claude_session_file(pane_pid)
                if jsonl_path:
                    theme = _thematic_title_for_session(jsonl_path)
                    if theme:
                        headline = theme
        except Exception as exc:
            _log.warning("LA thematic headline lookup failed: %s", exc)
    # If the LLM title wasn't cached yet we just returned raw text; schedule
    # an async follow-up that re-pushes the headline once the background
    # generator populates the cache.
    _LiveActivity_headline_refresh_snapshot = started_at
    def _refresh_headline_when_ready(label=session_label,
                                     snap_started=_LiveActivity_headline_refresh_snapshot):
        for _ in range(6):  # up to ~30s
            time.sleep(5)
            resolved2 = _latest_user_prompt_for_session(label)
            if not resolved2:
                return
            hkey = _prompt_hash(resolved2.strip())
            with _summary_llm_lock:
                rec = _summary_llm_cache.get(hkey)
            if not rec:
                continue
            new_headline = rec[0]
            with _liveactivity_turn_state_lock:
                cur = _liveactivity_turn_state.get(label) or {}
                if cur.get("started_at") != snap_started:
                    return
                cur["headline"] = new_headline
            cs = {
                "phase": "thinking",
                "headline": new_headline,
                "body": "",
                "startedAt": _swift_date(snap_started),
                "arcStartedAt": _swift_date(time.time()),
                "elapsedSeconds": 0,
            }
            _attach_prompt_options(cs, label)
            for _aid, tok in _liveactivity_active_tokens(label):
                try:
                    _push_liveactivity(tok, event="update", content_state=cs,
                                       alert_title="Claude", alert_body=new_headline)
                except Exception as exc:
                    _log.warning("LiveActivity headline refresh push failed: %s", exc)
            return
    try:
        threading.Thread(target=_refresh_headline_when_ready, daemon=True).start()
    except Exception as exc:
        _log.warning("LiveActivity headline refresh scheduling failed: %s", exc)
    with _liveactivity_turn_state_lock:
        prior = _liveactivity_turn_state.get(session_label) or {}
        prior_timer = prior.get("dismiss_timer")
        if prior_timer is not None:
            try:
                prior_timer.cancel()
            except Exception as exc:
                _log.error("Failed to cancel prior LiveActivity timer: %s",
                           exc, exc_info=True)
        new_gen = int(prior.get("generation", 0)) + 1
        _liveactivity_turn_state[session_label] = {
            "started_at": started_at,
            "headline": headline,
            "dismiss_timer": None,
            "generation": new_gen,
        }
    _start_la_arc_refresher()
    _la_arc_last_reset[session_label] = started_at
    content_state = {
        "phase": "thinking",
        "headline": headline,
        "body": "",
        "startedAt": _swift_date(started_at),
        "arcStartedAt": _swift_date(time.time()),
        "elapsedSeconds": 0,
    }
    _attach_prompt_options(content_state, session_label)
    active = _liveactivity_active_tokens(session_label)
    if active:
        for _aid, tok in active:
            try:
                _push_liveactivity(tok, event="update", content_state=content_state,
                                   alert_title="Claude", alert_body=headline)
            except Exception as exc:
                _log.error("LiveActivity update (turn_start) error: %s",
                           exc, exc_info=True)
        return
    # No live activity yet — try push-to-start using ONLY the most recent
    # start token so we don't spawn duplicate activities on every turn.
    with _liveactivity_lock:
        tokens = list(_liveactivity_start_tokens)
    if tokens:
        tok = tokens[-1]
        try:
            _push_liveactivity(
                tok, event="start",
                content_state=content_state,
                attributes={"sessionLabel": session_label},
                stale_date=started_at + 15 * 60,
                alert_title="Claude", alert_body=headline,
            )
        except Exception as exc:
            _log.error("LiveActivity start error: %s", exc, exc_info=True)


def _liveactivity_on_turn_complete(session_label, body_text):
    """Push a finished-phase update and schedule auto-dismiss after 10 min."""
    with _liveactivity_turn_state_lock:
        state = dict(_liveactivity_turn_state.get(session_label) or {})
    started_at = state.get("started_at")
    if started_at is None:
        _log.warning("LiveActivity turn_complete: no started_at for session %s "
                     "(out-of-order hook?) — elapsed will be 0", session_label)
        started_at = time.time()
    elapsed = max(0, int(time.time() - started_at))
    summary = (body_text or "").strip() or "Task complete"
    headline = summary
    if len(headline) > 60:
        cut = summary[:60]
        space = cut.rfind(" ")
        headline = (cut[:space] if space > 20 else cut).rstrip() + "…"
    content_state = {
        "phase": "finished",
        "headline": headline,
        "body": summary,
        "startedAt": _swift_date(started_at),
        "arcStartedAt": _swift_date(time.time()),
        "elapsedSeconds": elapsed,
    }
    _attach_prompt_options(content_state, session_label)
    active = _liveactivity_active_tokens(session_label)
    if not active:
        _log.warning("turn_complete(%s): no active Live Activity tokens — "
                     "phase=finished push skipped. Island will not flip to "
                     "Done; most likely the iOS app didn't register an "
                     "update token after push-to-start.",
                     session_label)
        return
    _la_arc_last_reset.pop(session_label, None)
    for _aid, tok in active:
        try:
            _push_liveactivity(tok, event="update", content_state=content_state,
                               alert_title="Claude finished", alert_body=headline)
        except Exception as exc:
            _log.error("LiveActivity update (turn_complete) error: %s",
                       exc, exc_info=True)

    captured_gen = state.get("generation", 0)

    def _end_later(tokens_snapshot=list(active), cs=content_state,
                   gen=captured_gen, label=session_label):
        with _liveactivity_turn_state_lock:
            current = int((_liveactivity_turn_state.get(label) or {}).get("generation", 0))
            if current != gen:
                _log.info("LiveActivity dismiss-timer generation mismatch "
                          "(captured=%s current=%s) — skipping", gen, current)
                return
            # Clear our slot so the record doesn't leak a stale Timer ref.
            rec = _liveactivity_turn_state.get(label) or {}
            if rec.get("dismiss_timer") is not None:
                rec["dismiss_timer"] = None
        dismiss_at = int(time.time())
        for _a, t in tokens_snapshot:
            try:
                _push_liveactivity(t, event="end", content_state=cs,
                                   dismissal_date=dismiss_at)
            except Exception as exc:
                _log.error("LiveActivity end error: %s", exc, exc_info=True)
    timer = threading.Timer(3.0, _end_later)
    timer.daemon = True
    # Atomic write under lock with generation check — prevents the
    # following race: _on_turn_complete (gen=N) writes its timer into
    # the slot AFTER a concurrent _on_turn_start has bumped the slot to
    # gen=N+1, leaving an N-generation timer attached to the N+1 record.
    # The timer's _end_later closure would still bail correctly via the
    # captured-gen check, but the slot would hold a stale reference and
    # the next _on_turn_start's prior_timer.cancel() would cancel the
    # wrong timer.
    started = False
    with _liveactivity_turn_state_lock:
        rec = _liveactivity_turn_state.setdefault(session_label, {})
        if int(rec.get("generation", 0)) == captured_gen:
            rec["dismiss_timer"] = timer
            started = True
        else:
            _log.info("LiveActivity turn_complete: generation moved on "
                      "(captured=%s current=%s) — not arming dismiss timer",
                      captured_gen, rec.get("generation"))
    if started:
        timer.start()


def _liveactivity_on_turn_error(session_label, error_text):
    """Push an error-phase update and schedule a short auto-dismiss.

    Bumps the generation counter on entry, symmetric with _on_turn_start,
    so that two errors fired in sequence (e.g. retry that also errors)
    each get a distinct generation and stale timers from the prior error
    cannot end the new error's activity.
    """
    with _liveactivity_turn_state_lock:
        prior = _liveactivity_turn_state.get(session_label) or {}
        prior_timer = prior.get("dismiss_timer")
        if prior_timer is not None:
            try:
                prior_timer.cancel()
            except Exception as exc:
                _log.error("Failed to cancel prior LiveActivity timer: %s",
                           exc, exc_info=True)
        new_gen = int(prior.get("generation", 0)) + 1
        prior["generation"] = new_gen
        prior["dismiss_timer"] = None
        _liveactivity_turn_state[session_label] = prior
        state = dict(prior)
    started_at = state.get("started_at")
    if started_at is None:
        _log.warning("LiveActivity turn_error: no started_at for session %s",
                     session_label)
        started_at = time.time()
    elapsed = max(0, int(time.time() - started_at))
    msg = (error_text or "Claude errored")[:200]
    content_state = {
        "phase": "error",
        "headline": msg[:60],
        "body": msg,
        "startedAt": _swift_date(started_at),
        "elapsedSeconds": elapsed,
    }
    active = _liveactivity_active_tokens(session_label)
    if not active:
        return
    for _aid, tok in active:
        try:
            _push_liveactivity(tok, event="update", content_state=content_state,
                               alert_title="Claude error", alert_body=msg[:60])
        except Exception as exc:
            _log.error("LiveActivity update (turn_error) error: %s",
                       exc, exc_info=True)

    captured_gen = state.get("generation", 0)

    def _end_later(tokens_snapshot=list(active), cs=content_state,
                   gen=captured_gen, label=session_label):
        with _liveactivity_turn_state_lock:
            current = int((_liveactivity_turn_state.get(label) or {}).get("generation", 0))
            if current != gen:
                _log.info("LiveActivity dismiss-timer (error) generation mismatch "
                          "(captured=%s current=%s) — skipping", gen, current)
                return
            rec = _liveactivity_turn_state.get(label) or {}
            if rec.get("dismiss_timer") is not None:
                rec["dismiss_timer"] = None
        dismiss_at = int(time.time())
        for _a, t in tokens_snapshot:
            try:
                _push_liveactivity(t, event="end", content_state=cs,
                                   dismissal_date=dismiss_at)
            except Exception as exc:
                _log.error("LiveActivity end error: %s", exc, exc_info=True)
    timer = threading.Timer(30.0, _end_later)
    timer.daemon = True
    started = False
    with _liveactivity_turn_state_lock:
        rec = _liveactivity_turn_state.setdefault(session_label, {})
        if int(rec.get("generation", 0)) == captured_gen:
            rec["dismiss_timer"] = timer
            started = True
        else:
            _log.info("LiveActivity turn_error: generation moved on "
                      "(captured=%s current=%s) — not arming dismiss timer",
                      captured_gen, rec.get("generation"))
    if started:
        timer.start()


def _send_push_notification(title, body, bundle_id="com.timtrailor.terminal",
                            window_index=None, content_available=False):
    """Send APNs push to a specific app via curl (HTTP/2 + JWT).

    bundle_id: which app to push to. Must have a registered device token.
        com.timtrailor.terminal     — Terminal app
        com.timtrailor.claudecontrol — ClaudeControl
        com.timtrailor.printerpilot  — PrinterPilot
    window_index: optional tmux window index — injected as top-level "window"
        so the iOS client can deep-link to the right tab on tap.
    content_available: if True, send a silent background push (no alert, no
        sound) that wakes the app to pre-fetch fresh pane content.
    """
    with _apns_device_tokens_lock:
        token = _apns_device_tokens.get(bundle_id)
    if not token:
        _log.debug("No APNs token for %s — skipping push", bundle_id)
        return
    kind = "silent" if content_available else "alert"
    _log.info("APNs: sending %s push to %s (token=%s... win=%s)",
              kind, bundle_id, token[:8], window_index)
    try:
        import subprocess

        jwt_token = _apns_jwt()

        if content_available:
            aps = {"content-available": 1}
        else:
            aps = {
                "alert": {"title": title, "body": body},
                "sound": "default",
            }
        payload_obj = {"aps": aps}
        if window_index is not None:
            payload_obj["window"] = int(window_index)
        payload = json.dumps(payload_obj)

        url = f"{_APNS_HOST}/3/device/{token}"

        push_type = "background" if content_available else "alert"
        priority = "5" if content_available else "10"
        result = subprocess.run([
            "curl", "-s", "--http2",
            "-H", f"authorization: bearer {jwt_token}",
            "-H", f"apns-topic: {bundle_id}",
            "-H", f"apns-push-type: {push_type}",
            "-H", f"apns-priority: {priority}",
            "-d", payload,
            "-w", "\n%{http_code}",
            url,
        ], capture_output=True, text=True, timeout=15)

        lines = result.stdout.strip().split("\n")
        status_code = lines[-1] if lines else "?"
        response_body = "\n".join(lines[:-1])

        if status_code == "200":
            _log.info("APNs push sent OK to %s: %s", bundle_id, title)
        elif status_code == "410":
            # Token no longer valid — remove it
            _log.info("APNs token for %s no longer valid, removing", bundle_id)
            with _apns_device_tokens_lock:
                _apns_device_tokens.pop(bundle_id, None)
                _save_apns_tokens()
        else:
            _log.warning("APNs push to %s failed (HTTP %s): %s", bundle_id, status_code, response_body)
    except Exception as exc:
        _log.error("APNs push error (%s): %s", bundle_id, exc, exc_info=True)


def _summarise_for_push(raw_text, max_lines=100, target_words=100, timeout=20.0):
    """Summarise the tail of Claude's output into a ~100-word push body.

    Feeds the last `max_lines` lines of `raw_text` to Haiku 4.5 and asks for a
    plain-prose single paragraph suitable for a phone lock-screen. Returns the
    summary string on success, or None on ANY failure so callers can fall back
    to their existing truncation logic — we never want to drop a notification.
    """
    if not raw_text or not raw_text.strip():
        return None

    tail = "\n".join(raw_text.splitlines()[-max_lines:]).strip()
    if not tail:
        return None

    try:
        from shared_utils import get_api_key
        api_key = get_api_key()
        if not api_key:
            _log.warning("push-summary: no ANTHROPIC_API_KEY available, falling back")
            return None

        import anthropic
        client = anthropic.Anthropic(api_key=api_key, timeout=timeout)

        prompt = (
            f"Below is the tail of Claude's most recent response to the user. "
            f"Write a single-paragraph summary of about {target_words} words that "
            f"a human will read on their phone lock screen. Lead with the outcome "
            f"or headline finding. No preamble like 'Claude' or 'The assistant'. "
            f"No markdown, no bullets — plain prose only.\n\n"
            f"---\n{tail}\n---"
        )

        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=260,
            messages=[{"role": "user", "content": prompt}],
        )

        parts = [b.text for b in resp.content if getattr(b, "type", None) == "text"]
        summary = " ".join(p.strip() for p in parts).strip()
        if not summary:
            _log.warning("push-summary: Haiku returned empty content, falling back")
            return None
        _log.info("push-summary: Haiku produced %d-char summary", len(summary))
        return summary
    except Exception as exc:
        _log.warning("push-summary: Haiku call failed (%s), falling back", exc)
        return None


def _notify_smart(title, message):
    """Smart notification: WebSocket if app is connected, else APNs + email + Slack."""
    with _ws_clients_lock:
        has_clients = len(_ws_clients) > 0

    if has_clients:
        # App is connected — it will show a local notification
        _broadcast_ws({"type": "push_message", "content": f"{title}: {message}"})
        _log.info("Smart notify via WebSocket: %s", title)
    else:
        # App is offline — use push + email + Slack
        _log.info("Smart notify via APNs/email/Slack (no WS clients): %s", title)
        threading.Thread(target=_send_push_notification, args=(title, message, "com.timtrailor.terminal"), daemon=True).start()
        # threading.Thread(target=_notify_email, args=(title, message), daemon=True).start()  # disabled — push only
        # threading.Thread(target=_notify_slack, args=(f"{title}: {message}",), daemon=True).start()  # disabled — push only




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
        # FIRST CHOICE: push to PrinterPilot and ClaudeControl (native iOS push)
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
        # BACKUP: Slack + email so alerts still arrive if push fails
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



@app.route("/register-device", methods=["POST"])
def register_device():
    """Register an iOS device token for APNs push notifications.

    Accepts bundle_id to support multiple apps (Terminal, ClaudeControl, PrinterPilot).
    Tokens persisted to /Users/timtrailor/code/apns_state/device_tokens.json.
    """
    data = request.get_json() or {}
    token = data.get("device_token", "").strip()
    bundle_id = data.get("bundle_id", "com.timtrailor.terminal").strip()
    if not token:
        return jsonify({"error": "device_token required"}), 400
    with _apns_device_tokens_lock:
        _apns_device_tokens[bundle_id] = token
        _save_apns_tokens()
    _log.info("APNs token registered for %s: %s...%s", bundle_id, token[:8], token[-4:])
    return jsonify({"ok": True})


@app.route("/register-liveactivity-start-token", methods=["POST"])
def register_liveactivity_start_token():
    """Register an ActivityKit push-to-start token for the Terminal app.

    Called by the iOS app's LiveActivityManager observer whenever a fresh
    push-to-start token is issued. Tokens are deduped in a set and survive
    daemon restart via /Users/timtrailor/code/apns_state/liveactivity_tokens.json.
    """
    data = request.get_json(silent=True) or {}
    token = (data.get("token") or "").strip()
    bundle_id = (data.get("bundle_id") or "com.timtrailor.terminal").strip()
    if not token:
        return jsonify({"error": "token required"}), 400
    if bundle_id != _LIVEACTIVITY_BUNDLE:
        return jsonify({"error": "unsupported bundle_id"}), 400
    with _liveactivity_lock:
        _liveactivity_start_tokens.add(token)
        _save_liveactivity_state()
    _log.info("LiveActivity start-token registered: %s...%s", token[:8], token[-4:])
    return jsonify({"ok": True})


@app.route("/register-liveactivity-token", methods=["POST"])
def register_liveactivity_token():
    """Register a per-activity update token from the iOS app.

    The iOS app posts this when an Activity is created locally or started
    via push-to-start — it carries the activity_id, attributes, and the
    push update token (optional on the first call because it arrives
    asynchronously, filled in on a follow-up).
    """
    data = request.get_json(silent=True) or {}
    activity_id = (data.get("activity_id") or "").strip()
    token = (data.get("token") or "").strip()
    attributes = data.get("attributes") or {}
    if not activity_id:
        return jsonify({"error": "activity_id required"}), 400
    if not token:
        return jsonify({"error": "token required"}), 400
    if not isinstance(attributes, dict):
        return jsonify({"error": "attributes must be an object"}), 400
    session_label = attributes.get("sessionLabel")
    if not isinstance(session_label, str) or not session_label:
        return jsonify({"error": "attributes.sessionLabel required"}), 400
    with _liveactivity_lock:
        rec = _liveactivity_tokens.setdefault(activity_id, {})
        if token:
            rec["token"] = token
        rec["attributes"] = attributes
        rec["last_update"] = int(time.time())
        _save_liveactivity_state()
    _log.info("LiveActivity activity-token registered: aid=%s token=%s",
              activity_id, (token[:8] + "..." if token else "(pending)"))
    return jsonify({"ok": True})





def _ensure_tmux_session(session: str = "mobile") -> None:
    """Ensure a detached tmux session exists. Idempotent."""
    import subprocess as _sp
    try:
        # Check if session exists
        result = _sp.run(
            ["/opt/homebrew/bin/tmux", "has-session", "-t", session],
            capture_output=True, text=True, timeout=3
        )
        if result.returncode == 0:
            return  # already exists
        # Create new detached session with default shell in home directory
        import os as _os
        _sp.run(
            ["/opt/homebrew/bin/tmux", "new-session", "-d", "-s", session, "-c", _os.path.expanduser("~"), "-x", "200", "-y", "50"],
            capture_output=True, text=True, timeout=5
        )
        _log.info("Created tmux session: %s", session)
    except Exception as e:
        _log.warning("Failed to ensure tmux session: %s", e)



@app.route("/tmux-reset", methods=["POST"])
def tmux_reset():
    """Hard reset: kill and recreate the mobile tmux session."""
    import subprocess as _sp
    try:
        _sp.run(["/opt/homebrew/bin/tmux", "kill-session", "-t", "mobile"],
                capture_output=True, text=True, timeout=5)
    except Exception:
        pass
    _ensure_tmux_session("mobile")
    return jsonify({"ok": True})


_CLAUDE_CLI_BIN = "/Users/timtrailor/.local/bin/claude"
_summary_llm_cache: dict = {}
_summary_llm_inflight: set = set()
_summary_llm_lock = threading.Lock()


def _prompt_hash(text: str) -> str:
    import hashlib as _hashlib
    return _hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


def _generate_summary_llm(raw: str, timeout: float = 20.0) -> str:
    """Spawn `claude -p --model haiku` to produce a 3-5 word task title.

    Uses Tim's Claude subscription via OAuth (no --bare), so this does not
    consume API credits. Returns empty string on failure so callers can
    fall back to the raw prompt.
    """
    import subprocess as _sp
    clean = raw.strip()
    if not clean:
        return ""
    prompt = (
        "Summarize this user request as a 3-5 word task title. "
        "Output ONLY the title. No punctuation, no quotes, no prefix, no labels. "
        "Request: " + clean[:800]
    )
    try:
        result = _sp.run(
            [_CLAUDE_CLI_BIN, "-p", "--model", "haiku", prompt],
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode != 0:
            _log.warning("claude -p summary failed rc=%s stderr=%s",
                         result.returncode, (result.stderr or "").strip()[:200])
            return ""
        lines = [ln.strip() for ln in (result.stdout or "").splitlines() if ln.strip()]
        if not lines:
            return ""
        title = lines[0].strip("\"\'`.:;,")
        for prefix in ("Title:", "Task:", "Summary:"):
            if title.lower().startswith(prefix.lower()):
                title = title[len(prefix):].strip()
        return title[:60]
    except _sp.TimeoutExpired:
        _log.warning("claude -p summary timed out after %ss", timeout)
        return ""
    except Exception as exc:
        _log.warning("claude -p summary error: %s", exc)
        return ""


_DEFAULT_SHELL_NAMES = {"zsh", "bash", "sh", "fish", "ksh", "tcsh", "dash"}

_THEME_WINDOW = 10        # number of recent user prompts fed to the LLM
_THEME_BUCKET_SIZE = 3    # regenerate thematic title every N new user messages


def _collect_session_user_prompts(jsonl_path: str, max_bytes: int = 4 * 1024 * 1024) -> list:
    """Return cleaned text of real user messages from a Claude session JSONL.
    Tool_result wrappers, image-only stubs and very short junk are dropped."""
    import os as _os
    import json as _json
    out = []
    try:
        size = _os.path.getsize(jsonl_path)
        to_read = min(size, max_bytes)
        with open(jsonl_path, "rb") as f:
            f.seek(max(0, size - to_read))
            blob = f.read().decode("utf-8", errors="replace")
    except Exception:
        return out
    for line in blob.split("\n"):
        if not line.strip():
            continue
        try:
            obj = _json.loads(line)
        except Exception:
            continue
        if obj.get("type") != "user":
            continue
        msg = obj.get("message") or {}
        content = msg.get("content")
        txt = ""
        if isinstance(content, str):
            txt = content
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    txt = part.get("text") or ""
                    break
        cleaned = _clean_user_text(txt)
        if cleaned and len(cleaned) >= 3 and not cleaned.startswith("<"):
            out.append(cleaned[:400])
    return out


def _generate_thematic_title_llm(prompts_blob: str, timeout: float = 25.0) -> str:
    """Ask Haiku for the overall theme of the last N user messages."""
    import subprocess as _sp
    if not prompts_blob.strip():
        return ""
    prompt = (
        "The lines below are the most recent user messages from an ongoing "
        "coding session. Generate a 3-5 word THEMATIC title describing the "
        "overarching work being done — not the latest message, but the "
        "overall theme across these messages. Output ONLY the title. "
        "No punctuation, no quotes, no prefix, no label.\n\n"
        "Messages:\n" + prompts_blob[:4000]
    )
    try:
        result = _sp.run(
            [_CLAUDE_CLI_BIN, "-p", "--model", "haiku", prompt],
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode != 0:
            _log.warning("thematic title failed rc=%s stderr=%s",
                         result.returncode, (result.stderr or "").strip()[:200])
            return ""
        lines = [ln.strip() for ln in (result.stdout or "").splitlines() if ln.strip()]
        if not lines:
            return ""
        title = lines[0].strip("\"\'`.:;,")
        for prefix in ("Title:", "Task:", "Summary:", "Theme:"):
            if title.lower().startswith(prefix.lower()):
                title = title[len(prefix):].strip()
        return title[:60]
    except _sp.TimeoutExpired:
        _log.warning("thematic title timed out after %ss", timeout)
        return ""
    except Exception as exc:
        _log.warning("thematic title error: %s", exc)
        return ""


def _thematic_title_for_session(jsonl_path: str) -> str:
    """Return a cached thematic title derived from the last _THEME_WINDOW user
    prompts. Regenerates only when a new bucket of _THEME_BUCKET_SIZE messages
    arrives, so the title stays stable across normal turn-taking."""
    import os as _os
    all_prompts = _collect_session_user_prompts(jsonl_path)
    if not all_prompts:
        return ""
    recent = all_prompts[-_THEME_WINDOW:]
    session_id = _os.path.splitext(_os.path.basename(jsonl_path))[0]
    bucket = len(all_prompts) // _THEME_BUCKET_SIZE
    key = f"theme:{session_id}:{bucket}"

    with _summary_llm_lock:
        cached = _summary_llm_cache.get(key)
        if cached:
            return cached[0]
        inflight = key in _summary_llm_inflight
        if not inflight:
            _summary_llm_inflight.add(key)
        fallback = ""
        for b in range(bucket - 1, -1, -1):
            prev = _summary_llm_cache.get(f"theme:{session_id}:{b}")
            if prev:
                fallback = prev[0]
                break

    if inflight:
        return fallback or recent[-1][:60]

    blob = "\n".join(f"- {p}" for p in recent)

    def _bg(messages: str, hash_key: str, sid: str, b: int):
        try:
            title = _generate_thematic_title_llm(messages)
        finally:
            with _summary_llm_lock:
                _summary_llm_inflight.discard(hash_key)
        if not title:
            return
        with _summary_llm_lock:
            _summary_llm_cache[hash_key] = (title, time.time())
            stale = [k for k in list(_summary_llm_cache.keys())
                     if k.startswith(f"theme:{sid}:") and k != hash_key]
            for k in stale:
                try:
                    if int(k.split(":")[-1]) < b:
                        _summary_llm_cache.pop(k, None)
                except Exception:
                    pass
            # Global cap across all session themes + non-thematic titles.
            if len(_summary_llm_cache) > 256:
                oldest = sorted(_summary_llm_cache.items(), key=lambda kv: kv[1][1])
                for k, _ in oldest[:-256]:
                    _summary_llm_cache.pop(k, None)

    threading.Thread(target=_bg, args=(blob, key, session_id, bucket), daemon=True).start()
    return fallback or recent[-1][:60]


_PANE_SESSION_FILE = "/tmp/claude_sessions/pane_session_map.json"
_pane_session_map = {}   # {pane_pid: {"session_id", "jsonl", "last_seen", "pane_id", "tmux_session"}}
_pane_session_lock = threading.Lock()


def _load_pane_session_map():
    """Load persisted pane→session mapping from disk and prune dead PIDs."""
    global _pane_session_map
    try:
        if not os.path.exists(_PANE_SESSION_FILE):
            return
        with open(_PANE_SESSION_FILE) as f:
            raw = json.load(f)
        # PIDs come back as strings from JSON; convert, and drop any whose
        # process is no longer alive so a reboot / stale entries get cleaned.
        import subprocess as _sp
        ps = _sp.run(["ps", "-A", "-o", "pid="], capture_output=True, text=True, timeout=3)
        alive = set()
        for ln in ps.stdout.split("\n"):
            try:
                alive.add(int(ln.strip()))
            except ValueError:
                pass
        loaded = {}
        for k, v in (raw or {}).items():
            try:
                pid = int(k)
            except ValueError:
                continue
            if pid in alive and isinstance(v, dict) and v.get("jsonl"):
                loaded[pid] = v
        _pane_session_map = loaded
        _log.info("Loaded %d pane→session mappings from %s",
                  len(loaded), _PANE_SESSION_FILE)
    except Exception as exc:
        _log.warning("Failed to load pane session map: %s", exc)


def _save_pane_session_map():
    """Persist the map. Called under _pane_session_lock."""
    try:
        os.makedirs(os.path.dirname(_PANE_SESSION_FILE), exist_ok=True)
        tmp = _PANE_SESSION_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({str(k): v for k, v in _pane_session_map.items()}, f)
        os.replace(tmp, _PANE_SESSION_FILE)
    except Exception as exc:
        _log.warning("Failed to persist pane session map: %s", exc)


def _register_pane_session(pane_pid: int, *, session_id: str, jsonl: str,
                           pane_id: str = "", tmux_session: str = ""):
    if not pane_pid:
        return
    with _pane_session_lock:
        _pane_session_map[pane_pid] = {
            "session_id": session_id,
            "jsonl": jsonl,
            "last_seen": time.time(),
            "pane_id": pane_id,
            "tmux_session": tmux_session,
        }
        _save_pane_session_map()


def _pane_session_lookup(pane_pid: int) -> dict:
    with _pane_session_lock:
        return dict(_pane_session_map.get(pane_pid) or {})


_load_pane_session_map()


_autonomous_tasks = {}  # {task_id: {"label", "headline", "started_at", "status", "pid"}}
_autonomous_tasks_lock = threading.Lock()


def _autonomous_label(task_id: str) -> str:
    return f"auto-{task_id}" if task_id else "auto"


def _autonomous_heartbeat_loop():
    """Every ~8 min, push a no-op update to every active autonomous LA so
    iOS doesn't mark it stale (Apple's stale-date cap is 15 min and a
    long-running autonomous task may go that long between real events).

    Also reaps entries whose runner pid has died so the registry doesn't
    accumulate forever on crashes or SIGKILL — picked up by the first
    tick after the process exits."""
    import subprocess as _sp
    while True:
        try:
            # Prune dead runners before heartbeating.
            with _autonomous_tasks_lock:
                snapshot_ids = list(_autonomous_tasks.items())
            alive = set()
            try:
                ps = _sp.run(["ps", "-A", "-o", "pid="], capture_output=True, text=True, timeout=3)
                for ln in ps.stdout.split("\n"):
                    try:
                        alive.add(int(ln.strip()))
                    except ValueError:
                        pass
            except Exception:
                alive = None  # skip pruning this tick if ps failed
            if alive is not None:
                with _autonomous_tasks_lock:
                    for task_id, rec in snapshot_ids:
                        pid = rec.get("pid")
                        if isinstance(pid, int) and pid not in alive:
                            _log.info("autonomous: reaping dead task %s (pid %s)",
                                      task_id, pid)
                            _autonomous_tasks.pop(task_id, None)
                snapshot_ids = [(k, v) for k, v in snapshot_ids if v.get("pid") in alive or not isinstance(v.get("pid"), int)]

            time.sleep(480)  # 8 minutes — do prune-before-sleep so first sleep isn't wasted
            with _autonomous_tasks_lock:
                snapshot = list(_autonomous_tasks.items())
            for task_id, rec in snapshot:
                label = rec.get("label") or _autonomous_label(task_id)
                started_at = rec.get("started_at") or time.time()
                content_state = {
                    "phase": "thinking",
                    "headline": rec.get("headline") or "Autonomous task",
                    "body": rec.get("status") or "",
                    "startedAt": _swift_date(started_at),
                    "arcStartedAt": _swift_date(time.time()),
                    "elapsedSeconds": int(time.time() - started_at),
                }
                _attach_prompt_options(content_state, label)
                active = _liveactivity_active_tokens(label)
                for _aid, tok in active:
                    try:
                        _push_liveactivity(tok, event="update",
                                           content_state=content_state,
                                           stale_date=time.time() + 15 * 60)
                    except Exception as exc:
                        _log.warning("autonomous heartbeat push failed: %s", exc)
        except Exception as exc:
            _log.warning("autonomous heartbeat loop error: %s", exc)


_autonomous_heartbeat_started = False
_autonomous_heartbeat_lock = threading.Lock()


_la_arc_refresher_started = False
_la_arc_refresher_lock = threading.Lock()
_la_arc_last_reset = {}  # {session_label: ts of last arc reset push}


def _la_arc_refresher_loop():
    """Every ~10 seconds, scan _liveactivity_turn_state for activities that
    are still in the thinking phase (no dismiss_timer armed) and push an
    event=update with a fresh arcStartedAt if the previous one was more
    than 55 seconds ago. This is what makes the circular arc on the
    Dynamic Island actually loop — iOS will not fire a widget TimelineView
    often enough in a Live Activity to re-render the ProgressView on its
    own, so the reset has to come from a real push."""
    while True:
        try:
            now = time.time()
            with _liveactivity_turn_state_lock:
                snapshot = [
                    (label, dict(rec))
                    for label, rec in _liveactivity_turn_state.items()
                    if rec.get("dismiss_timer") is None
                ]
            for label, rec in snapshot:
                last = _la_arc_last_reset.get(label, 0)
                if now - last < 55:
                    continue
                started_at = rec.get("started_at") or now
                headline = rec.get("headline") or "Working…"
                content_state = {
                    "phase": "thinking",
                    "headline": headline,
                    "body": "",
                    "startedAt": _swift_date(started_at),
                    "arcStartedAt": _swift_date(now),
                    "elapsedSeconds": int(now - started_at),
                }
                _attach_prompt_options(content_state, label)
                active = _liveactivity_active_tokens(label)
                if not active:
                    continue
                for _aid, tok in active:
                    try:
                        _push_liveactivity(tok, event="update",
                                           content_state=content_state,
                                           stale_date=now + 15 * 60)
                    except Exception as exc:
                        _log.warning("arc refresher push failed for %s: %s",
                                     label, exc)
                _la_arc_last_reset[label] = now
        except Exception as exc:
            _log.warning("arc refresher loop error: %s", exc)
        time.sleep(10)


def _start_la_arc_refresher():
    global _la_arc_refresher_started
    with _la_arc_refresher_lock:
        if _la_arc_refresher_started:
            return
        _la_arc_refresher_started = True
    threading.Thread(target=_la_arc_refresher_loop,
                     daemon=True, name="la-arc-refresher").start()


def _start_autonomous_heartbeat():
    global _autonomous_heartbeat_started
    with _autonomous_heartbeat_lock:
        if _autonomous_heartbeat_started:
            return
        _autonomous_heartbeat_started = True
    threading.Thread(target=_autonomous_heartbeat_loop,
                     daemon=True, name="autonomous-heartbeat").start()


@app.route("/internal/autonomous-event", methods=["POST"])
def autonomous_event():
    """Receive lifecycle events from autonomous_runner.py and drive a
    per-task Live Activity (sessionLabel=auto-<task_id>). The runner is a
    detached nohup process with no iOS/tmux affinity, so this is the only
    channel by which the Island can track it."""
    remote = (request.remote_addr or "")
    if remote not in ("127.0.0.1", "::1", "localhost"):
        return jsonify({"ok": False, "error": "localhost only"}), 403

    try:
        data = request.get_json(silent=True) or {}
    except Exception:
        data = {}
    event = (data.get("event") or "").strip()
    task_id = (data.get("task_id") or "").strip() or "anon"
    label = _autonomous_label(task_id)
    prompt = (data.get("prompt") or "").strip()
    attempt = data.get("attempt")
    max_retries = data.get("max_retries")
    body = (data.get("body") or "").strip()
    preview = (data.get("preview") or "").strip()

    _log.info("[auto] event=%s task=%s attempt=%s/%s",
              event, task_id, attempt, max_retries)

    if event == "task_started":
        headline_raw = prompt or "Autonomous task"
        # Thematic title up front so the Island shows a concise label
        # instead of the full 2 KB autonomous prompt. The generator is
        # synchronous here but cached by prompt hash so subsequent
        # invocations for the same task are fast.
        headline = _generate_summary_llm(headline_raw) or headline_raw[:60]
        started_at = time.time()
        with _autonomous_tasks_lock:
            _autonomous_tasks[task_id] = {
                "label": label,
                "headline": headline,
                "started_at": started_at,
                "status": "starting",
                "pid": data.get("pid"),
            }
        try:
            threading.Thread(
                target=_liveactivity_on_turn_start,
                kwargs={"session_label": label, "headline": headline},
                daemon=True,
            ).start()
        except Exception as exc:
            _log.warning("[auto] turn_start spawn failed: %s", exc)
        _start_autonomous_heartbeat()

    elif event == "attempt_started":
        with _autonomous_tasks_lock:
            rec = _autonomous_tasks.get(task_id) or {}
            rec["status"] = f"attempt {attempt}/{max_retries}"
            _autonomous_tasks[task_id] = rec
        # Push a plain update reflecting the new attempt count.
        started_at = (rec.get("started_at") or time.time()) if rec else time.time()
        content_state = {
            "phase": "thinking",
            "headline": (rec.get("headline") if rec else "") or "Autonomous task",
            "body": f"attempt {attempt}/{max_retries}",
            "startedAt": _swift_date(started_at),
            "arcStartedAt": _swift_date(time.time()),
            "elapsedSeconds": int(time.time() - started_at),
        }
        _attach_prompt_options(content_state, label)
        for _aid, tok in _liveactivity_active_tokens(label):
            try:
                _push_liveactivity(tok, event="update",
                                   content_state=content_state,
                                   stale_date=time.time() + 15 * 60)
            except Exception as exc:
                _log.warning("[auto] attempt update failed: %s", exc)

    elif event == "attempt_failed":
        with _autonomous_tasks_lock:
            rec = _autonomous_tasks.get(task_id) or {}
            rec["status"] = f"retry after attempt {attempt}"
            _autonomous_tasks[task_id] = rec

    elif event == "task_completed":
        try:
            threading.Thread(
                target=_liveactivity_on_turn_complete,
                args=(label, body or "Task complete"),
                daemon=True,
            ).start()
        except Exception as exc:
            _log.warning("[auto] turn_complete spawn failed: %s", exc)
        with _autonomous_tasks_lock:
            _autonomous_tasks.pop(task_id, None)

    elif event == "task_failed":
        # Push phase=error, then let turn_complete's dismiss timer clear it.
        with _autonomous_tasks_lock:
            rec = _autonomous_tasks.get(task_id) or {}
            started_at = rec.get("started_at") or time.time()
            headline = rec.get("headline") or "Autonomous task"
        content_state = {
            "phase": "error",
            "headline": headline,
            "body": (body or "Task failed")[:400],
            "startedAt": _swift_date(started_at),
            "arcStartedAt": _swift_date(time.time()),
            "elapsedSeconds": int(time.time() - started_at),
        }
        _attach_prompt_options(content_state, label)
        for _aid, tok in _liveactivity_active_tokens(label):
            try:
                _push_liveactivity(tok, event="update",
                                   content_state=content_state,
                                   alert_title="Autonomous task failed",
                                   alert_body=headline)
            except Exception as exc:
                _log.warning("[auto] error update failed: %s", exc)
        # Schedule a dismiss in 10s so the error is visible briefly.
        def _end_later(lbl=label):
            time.sleep(10)
            cs = content_state
            for _aid, tok in _liveactivity_active_tokens(lbl):
                try:
                    _push_liveactivity(tok, event="end", content_state=cs,
                                       dismissal_date=int(time.time()))
                except Exception as exc:
                    _log.warning("[auto] end push failed: %s", exc)
        threading.Thread(target=_end_later, daemon=True).start()
        with _autonomous_tasks_lock:
            _autonomous_tasks.pop(task_id, None)

    return jsonify({"ok": True, "label": label})


_SERVER_BASE_URL_FOR_LA = "http://100.126.253.40:8081"


def _tmux_target_for_label(label: str) -> str:
    """Translate a Live Activity session label (e.g. "mobile-3") into a
    tmux target string (e.g. "mobile:3"). Non-mobile labels are returned
    unchanged so existing tmux session names still work."""
    if label.startswith("mobile-"):
        suffix = label[len("mobile-"):]
        if suffix.isdigit():
            return f"mobile:{suffix}"
    return label


def _detect_pending_approval(jsonl_path: str) -> dict:
    """Check if the Claude session is waiting for tool-use approval by
    reading the JSONL tail. If the most recent message record (assistant
    or user) is an assistant with stop_reason='tool_use', the session is
    blocked on a permission prompt. Returns {"pending": True, "tool_name":
    "Edit"} or {"pending": False}. Deterministic — no pane text parsing."""
    import json as _json
    try:
        size = os.path.getsize(jsonl_path)
        to_read = min(size, 64 * 1024)
        with open(jsonl_path, "rb") as f:
            f.seek(max(0, size - to_read))
            blob = f.read().decode("utf-8", errors="replace")
    except Exception:
        return {"pending": False}
    for line in reversed(blob.strip().split("\n")):
        if not line.strip():
            continue
        try:
            obj = _json.loads(line)
        except Exception:
            continue
        t = obj.get("type")
        if t not in ("assistant", "user"):
            continue
        if t == "assistant":
            msg = obj.get("message") or {}
            if msg.get("stop_reason") == "tool_use":
                tool_name = "Tool"
                for part in (msg.get("content") or []):
                    if isinstance(part, dict) and part.get("type") == "tool_use":
                        tool_name = part.get("name") or "Tool"
                        break
                return {"pending": True, "tool_name": tool_name}
            return {"pending": False}
        elif t == "user":
            return {"pending": False}
    return {"pending": False}


def _attach_prompt_options(content_state: dict, session_label: str) -> None:
    """Populate content_state["options"] and ["serverBaseURL"] so the Live
    Activity widget can render PromptChoiceIntent buttons. Only thinking
    phase states receive options — finished/error clear them."""
    content_state["serverBaseURL"] = _SERVER_BASE_URL_FOR_LA
    if content_state.get("phase") == "thinking":
        content_state["options"] = _capture_pane_options_for_label(session_label)
    else:
        content_state["options"] = []


def _is_prompt_chrome_line(raw: str) -> bool:
    t = raw.strip()
    if not t:
        return True
    if t.startswith("╭") or t.startswith("╰") or t.startswith("─"):
        return True
    if t.startswith("│") and t.endswith("│"):
        inner = t[1:-1].strip()
        return not inner
    for hint in ("esc to interrupt", "Press up to edit",
                 "ctrl+t to hide tasks", "shift+tab to"):
        if hint in t:
            return True
    return t in ("❯", ">")


def _parse_prompt_option_line(raw: str):
    t = raw.strip()
    while t and t[0] in "│❯>•·":
        t = t[1:].strip()
    while t and t[-1] == "│":
        t = t[:-1].strip()
    if "." not in t:
        return None
    dot = t.index(".")
    try:
        num = int(t[:dot])
    except ValueError:
        return None
    if not (1 <= num <= 9):
        return None
    rest = t[dot + 1:].strip()
    if not rest:
        return None
    if len(rest) > 40:
        rest = rest[:37] + "…"
    return (num, rest)


def _detect_pane_prompt_options(pane_text: str):
    """Mirror of the iOS detectPromptOptions. Returns the active prompt's
    numbered options as [{number, label}, ...] or [] if no active prompt."""
    if not pane_text:
        return []
    lines = pane_text.split("\n")
    tail = lines[-25:]
    last_opt_idx = None
    for i in range(len(tail) - 1, -1, -1):
        raw = tail[i]
        if _parse_prompt_option_line(raw) is not None:
            last_opt_idx = i
            break
        if not _is_prompt_chrome_line(raw):
            return []
    if last_opt_idx is None:
        return []
    start_idx = last_opt_idx
    while start_idx > 0 and _parse_prompt_option_line(tail[start_idx - 1]) is not None:
        start_idx -= 1
    collected = []
    for i in range(start_idx, last_opt_idx + 1):
        parsed = _parse_prompt_option_line(tail[i])
        if parsed is None:
            return []
        collected.append(parsed)
    if len(collected) < 2:
        return []
    for idx, (num, _) in enumerate(collected):
        if num != idx + 1:
            return []
    return [{"number": n, "label": lbl} for n, lbl in collected]


def _capture_pane_options_for_label(session_label: str):
    """Capture the current pane for a mobile-<N> label and run the option
    detector. Returns [] for any non-mobile label or capture failure."""
    target = _tmux_target_for_label(session_label)
    if ":" not in target:
        return []
    import subprocess as _sp
    try:
        r = _sp.run(
            ["/opt/homebrew/bin/tmux", "capture-pane", "-p",
             "-t", target, "-S", "-40"],
            capture_output=True, text=True, timeout=3,
        )
        if r.returncode != 0:
            return []
        return _detect_pane_prompt_options(r.stdout)
    except Exception as exc:
        _log.warning("capture-pane for %s failed: %s", session_label, exc)
        return []


@app.route("/internal/prompt-choice", methods=["POST"])
def prompt_choice():
    """Receive a numbered-choice tap from the Live Activity's
    PromptChoiceIntent and forward it to the right tmux pane. No
    localhost restriction — widget runs on the phone via Tailscale."""
    try:
        data = request.get_json(silent=True) or {}
    except Exception:
        data = {}
    session_label = (data.get("session_label") or "").strip()
    number = data.get("number")
    if not session_label or number is None:
        return jsonify({"ok": False, "error": "session_label and number required"}), 400
    try:
        number = int(number)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "number must be int"}), 400
    if not (1 <= number <= 9):
        return jsonify({"ok": False, "error": "number out of range"}), 400
    target = _tmux_target_for_label(session_label)
    if ":" not in target:
        return jsonify({"ok": False, "error": "non-mobile label"}), 400
    _log.info("[prompt-choice] label=%s target=%s number=%s",
              session_label, target, number)
    import subprocess as _sp
    try:
        _sp.run(
            ["/opt/homebrew/bin/tmux", "send-keys", "-t", target,
             str(number)],
            check=True, timeout=5,
        )
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


def _end_live_activity_for_label(session_label: str, reason: str = "") -> int:
    """End every Live Activity matching session_label. Pushes an event=end
    APNs message with dismissal_date=now, drops the token records from
    memory + disk, cancels any pending dismiss timer, and also clears the
    label from _la_arc_last_reset so the arc refresher stops pushing to it.
    Returns the number of activities ended."""
    now = int(time.time())
    ended_count = 0
    tokens_to_end = []
    with _liveactivity_lock:
        for aid, rec in list(_liveactivity_tokens.items()):
            attrs = rec.get("attributes") or {}
            tok = rec.get("token")
            if not tok:
                continue
            if attrs.get("sessionLabel") != session_label:
                continue
            tokens_to_end.append((aid, tok))
    if not tokens_to_end:
        return 0
    end_content_state = {
        "phase": "finished",
        "headline": "",
        "body": "",
        "startedAt": _swift_date(time.time()),
        "arcStartedAt": _swift_date(time.time()),
        "elapsedSeconds": 0,
        "options": [],
        "serverBaseURL": _SERVER_BASE_URL_FOR_LA,
    }
    for aid, tok in tokens_to_end:
        try:
            _push_liveactivity(tok, event="end",
                               content_state=end_content_state,
                               dismissal_date=now)
            ended_count += 1
        except Exception as exc:
            _log.warning("end push failed for %s: %s", aid, exc)
    with _liveactivity_lock:
        for aid, _tok in tokens_to_end:
            _liveactivity_tokens.pop(aid, None)
        try:
            _save_liveactivity_state()
        except Exception as exc:
            _log.warning("Failed to persist LA state after end: %s", exc)
    with _liveactivity_turn_state_lock:
        rec = _liveactivity_turn_state.pop(session_label, None)
        if rec and rec.get("dismiss_timer") is not None:
            try:
                rec["dismiss_timer"].cancel()
            except Exception:
                pass
    _la_arc_last_reset.pop(session_label, None)
    _log.info("Ended %d Live Activity record(s) for %s (%s)",
              ended_count, session_label, reason or "manual")
    return ended_count


@app.route("/debug/la-end-all", methods=["POST"])
def la_end_all():
    """Force-end every registered Live Activity, regardless of label.
    Use to clean up stale activities that piled up before the
    close-on-tab-kill wiring. Localhost-only."""
    remote = (request.remote_addr or "")
    if remote not in ("127.0.0.1", "::1", "localhost"):
        return jsonify({"ok": False, "error": "localhost only"}), 403
    labels = set()
    with _liveactivity_lock:
        for rec in _liveactivity_tokens.values():
            lbl = (rec.get("attributes") or {}).get("sessionLabel")
            if lbl:
                labels.add(lbl)
    total = 0
    for lbl in labels:
        total += _end_live_activity_for_label(lbl, reason="debug la-end-all")
    return jsonify({"ok": True, "ended": total, "labels": sorted(labels)})


@app.route("/internal/claude-hook", methods=["POST"])
def claude_hook():
    """Receive UserPromptSubmit / Stop notifications from Claude Code hooks.

    Each call carries the session id, transcript path, tmux pane metadata
    and the hook event name. We record the mapping so tab-summary and
    Live Activity code can resolve pane_pid → jsonl deterministically
    without relying on lsof (Claude Code opens/closes the file per write
    and doesn't keep an fd we can see). UserPromptSubmit fires a fresh
    Live Activity turn_start. Stop fires turn_complete.
    """
    # Security: only accept localhost hits since Claude Code runs on the Mac Mini.
    remote = (request.remote_addr or "")
    if remote not in ("127.0.0.1", "::1", "localhost"):
        _log.warning("[hook] rejected from remote=%s", remote)
        return jsonify({"ok": False, "error": "localhost only"}), 403

    try:
        data = request.get_json(silent=True) or {}
    except Exception:
        data = {}
    event = (data.get("event") or "").strip()
    session_id = (data.get("session_id") or "").strip()
    transcript = (data.get("transcript_path") or "").strip()
    pane_pid_raw = (data.get("pane_pid") or "").strip()
    pane_id = (data.get("pane_id") or "").strip()
    tmux_session = (data.get("tmux_session") or "").strip()

    try:
        pane_pid = int(pane_pid_raw) if pane_pid_raw else 0
    except ValueError:
        pane_pid = 0

    _log.info("[hook] event=%s session=%s pane_pid=%s pane_id=%s tmux=%s "
              "transcript=%s",
              event, session_id[:8] if session_id else "-",
              pane_pid or "-", pane_id or "-", tmux_session or "-",
              transcript or "-")

    if pane_pid and transcript:
        # Reject transcripts that aren't inside Claude Code's projects dir —
        # a local caller shouldn't be able to pin the map to an arbitrary
        # filesystem path we'll later read.
        claude_projects_root = os.path.expanduser("~/.claude/projects/")
        try:
            real = os.path.realpath(transcript)
        except Exception:
            real = ""
        if real and real.startswith(claude_projects_root) and real.endswith(".jsonl"):
            _register_pane_session(
                pane_pid,
                session_id=session_id,
                jsonl=real,
                pane_id=pane_id,
                tmux_session=tmux_session,
            )
        else:
            _log.warning("[hook] rejected transcript path outside projects dir: %s",
                         transcript[:200])

    # Only the mobile tmux session is wired to the Live Activity flow for now.
    if tmux_session and tmux_session != "mobile":
        return jsonify({"ok": True, "ignored": f"non-mobile session {tmux_session}"})

    # Build the per-tab LA label. Falls back to the tmux-session-wide
    # "mobile" label if we don't know the window index (shouldn't happen
    # under the hook path but keeps things robust).
    window_idx_raw = (data.get("window_index") or "").strip()
    try:
        window_idx = int(window_idx_raw) if window_idx_raw else None
    except ValueError:
        window_idx = None
    la_label = f"mobile-{window_idx}" if window_idx is not None else "mobile"

    if event == "UserPromptSubmit":
        try:
            threading.Thread(
                target=_liveactivity_on_turn_start,
                kwargs={"session_label": la_label},
                daemon=True,
            ).start()
        except Exception as exc:
            _log.warning("[hook] turn_start spawn failed: %s", exc)
    elif event == "Stop":
        # Extract the final assistant text from the transcript tail so the
        # banner notification and the Live Activity body have real content.
        body_text = ""
        try:
            if transcript and os.path.exists(transcript):
                import json as _json
                size = os.path.getsize(transcript)
                to_read = min(size, 256 * 1024)
                with open(transcript, "rb") as f:
                    f.seek(max(0, size - to_read))
                    tail = f.read().decode("utf-8", errors="replace")
                for line in reversed(tail.strip().split("\n")):
                    if not line.strip():
                        continue
                    try:
                        obj = _json.loads(line)
                    except Exception:
                        continue
                    if obj.get("type") != "assistant":
                        continue
                    msg = obj.get("message") or {}
                    if (msg.get("stop_reason") or "") in ("tool_use", ""):
                        continue
                    content = msg.get("content")
                    if isinstance(content, list):
                        for part in content:
                            if isinstance(part, dict) and part.get("type") == "text":
                                body_text = (part.get("text") or "").strip()
                                break
                    elif isinstance(content, str):
                        body_text = content.strip()
                    if body_text:
                        break
        except Exception as exc:
            _log.warning("[hook] body extract failed: %s", exc)
        body_text = (body_text or "Done")[:400]
        try:
            threading.Thread(
                target=_liveactivity_on_turn_complete,
                args=(la_label, body_text),
                daemon=True,
            ).start()
        except Exception as exc:
            _log.warning("[hook] turn_complete spawn failed: %s", exc)

    return jsonify({"ok": True})


_tmux_summary_cache = {}  # {pane_pid: (jsonl_path, jsonl_mtime, summary)}


def _find_claude_session_file(pane_pid: int) -> str | None:
    """Resolve the active Claude session JSONL for a tmux pane.

    Fast path: consult `_pane_session_map`, which is populated by the
    Claude Code `UserPromptSubmit`/`Stop` hooks — hooks give us the
    transcript path directly and are the only reliable source because
    Claude Code opens/closes the JSONL per write and leaves nothing
    visible via lsof between turns.

    Slow path: walk descendants for a `claude` process and try lsof on
    it — used when no hook has fired yet for the pane (e.g. the first
    poll right after tab creation)."""
    import subprocess as _sp
    import os as _os
    import glob as _glob
    rec = _pane_session_lookup(pane_pid)
    jsonl = rec.get("jsonl") if rec else ""
    if jsonl and _os.path.exists(jsonl):
        return jsonl
    try:
        ps = _sp.run(["ps", "-ax", "-o", "pid=,ppid=,comm="],
                     capture_output=True, text=True, timeout=3)
        if ps.returncode != 0:
            return None
        children: dict[int, list[int]] = {}
        comms: dict[int, str] = {}
        for line in ps.stdout.split("\n"):
            line = line.strip()
            if not line:
                continue
            toks = line.split(None, 2)
            if len(toks) < 3:
                continue
            try:
                pid_i, ppid_i = int(toks[0]), int(toks[1])
            except ValueError:
                continue
            comms[pid_i] = toks[2]
            children.setdefault(ppid_i, []).append(pid_i)

        # BFS for a claude process under pane_pid
        stack = list(children.get(pane_pid, []))
        claude_pid = None
        seen: set[int] = set()
        while stack:
            pid_i = stack.pop(0)
            if pid_i in seen:
                continue
            seen.add(pid_i)
            base = _os.path.basename(comms.get(pid_i, ""))
            if base == "claude":
                claude_pid = pid_i
                break
            stack.extend(children.get(pid_i, []))
        if claude_pid is None:
            return None

        # Find the session UUID via lsof
        lsof = _sp.run(["lsof", "-p", str(claude_pid)],
                       capture_output=True, text=True, timeout=3)
        uuid = None
        for line in lsof.stdout.split("\n"):
            if "/.claude/tasks/" in line:
                path = line.split()[-1]
                # /Users/.../.claude/tasks/<uuid>
                uuid = _os.path.basename(path.rstrip("/"))
                if re.fullmatch(r"[0-9a-f-]{30,40}", uuid):
                    break
                uuid = None
        if uuid:
            matches = _glob.glob(_os.path.expanduser(f"~/.claude/projects/*/{uuid}.jsonl"))
            if matches:
                return matches[0]

        # Fallback: no task-dir UUID visible via lsof. Use claude's CWD to
        # compute the project slug, then pick the most-recently-modified
        # jsonl whose birth time is after the claude process start time.
        try:
            cwd_run = _sp.run(["lsof", "-a", "-p", str(claude_pid), "-d", "cwd", "-Fn"],
                              capture_output=True, text=True, timeout=3)
            cwd = ""
            for line in cwd_run.stdout.split("\n"):
                if line.startswith("n/"):
                    cwd = line[1:]
                    break
            if not cwd:
                return None
            slug = cwd.replace("/", "-").replace(" ", "-")
            project_dir = _os.path.expanduser(f"~/.claude/projects/{slug}")
            if not _os.path.isdir(project_dir):
                return None
            # Use etime (elapsed seconds since start) — locale-independent
            # and unambiguous. Format: [[DD-]HH:]MM:SS.
            import time as _time
            start_run = _sp.run(["ps", "-o", "etime=", "-p", str(claude_pid)],
                                capture_output=True, text=True, timeout=3)
            claude_start_ts = 0
            try:
                raw = (start_run.stdout or "").strip()
                if raw:
                    days = 0
                    if "-" in raw:
                        d, raw = raw.split("-", 1)
                        days = int(d)
                    hms = raw.split(":")
                    if len(hms) == 3:
                        secs = int(hms[0]) * 3600 + int(hms[1]) * 60 + int(hms[2])
                    elif len(hms) == 2:
                        secs = int(hms[0]) * 60 + int(hms[1])
                    else:
                        secs = int(hms[0])
                    secs += days * 86400
                    claude_start_ts = _time.time() - secs
            except Exception:
                claude_start_ts = 0
            # Pick the jsonl whose birth time is EARLIEST after claude start.
            # Rationale: the primary session jsonl is created the moment the
            # user's first prompt lands. Subprocess jsonls (e.g. autonomous
            # runner spawning `claude -p`) are necessarily born later, so
            # the earliest-birth one post-start is our session of interest.
            # Falls back to most-recent-mtime if no birth info is usable.
            best = None
            best_birth = None
            best_mtime_fallback = None
            best_mtime = 0
            for fn in _os.listdir(project_dir):
                if not fn.endswith(".jsonl"):
                    continue
                path = _os.path.join(project_dir, fn)
                try:
                    st = _os.stat(path)
                except OSError:
                    continue
                if claude_start_ts and st.st_birthtime < claude_start_ts - 60:
                    continue
                if best_birth is None or st.st_birthtime < best_birth:
                    best = path
                    best_birth = st.st_birthtime
                if st.st_mtime > best_mtime:
                    best_mtime_fallback = path
                    best_mtime = st.st_mtime
            return best or best_mtime_fallback
        except Exception:
            return None
    except Exception:
        return None


_IMG_MARKER_RE = re.compile(r"\[Image[^]]*\]")


def _clean_user_text(txt: str) -> str:
    """Strip Claude Code image markers and whitespace from a user message."""
    if not txt:
        return ""
    return _IMG_MARKER_RE.sub("", txt).strip()


def _extract_session_summary(jsonl_path: str) -> str:
    """Read the tail of a claude session JSONL and return a short summary.
    Prefers customTitle > lastPrompt > latest user text (image markers stripped)."""
    import os as _os
    import json as _json
    try:
        size = _os.path.getsize(jsonl_path)
        # Read a generous tail so we reach older last-prompt / custom-title
        # records that Claude Code only emits occasionally.
        to_read = min(size, 2 * 1024 * 1024)
        with open(jsonl_path, "rb") as f:
            f.seek(max(0, size - to_read))
            tail = f.read().decode("utf-8", errors="replace")
        lines = tail.strip().split("\n")
        custom_title = None
        last_prompt = None
        last_user = None
        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                obj = _json.loads(line)
            except Exception:
                continue
            t = obj.get("type")
            if t == "custom-title" and custom_title is None:
                custom_title = (obj.get("customTitle") or "").strip()
                if custom_title:
                    break  # highest-priority match
            elif t == "last-prompt" and last_prompt is None:
                last_prompt = _clean_user_text(obj.get("lastPrompt") or "")
            elif t == "user" and last_user is None:
                msg = obj.get("message") or {}
                content = msg.get("content")
                if isinstance(content, str):
                    candidate = _clean_user_text(content)
                elif isinstance(content, list):
                    candidate = ""
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "text":
                            candidate = _clean_user_text(part.get("text") or "")
                            if candidate:
                                break
                else:
                    candidate = ""
                # Only accept substantive user text; skip empties, tool_result
                # wrappers, and image-only attachment stubs.
                if candidate and len(candidate) >= 3 and not candidate.startswith("<"):
                    last_user = candidate
        return (custom_title or last_prompt or last_user or "").strip()
    except Exception:
        return ""


def _summary_for_window(pane_pid: int) -> str:
    """Cached lookup of the active claude session summary for a tmux pane."""
    import os as _os
    try:
        jsonl_path = _find_claude_session_file(pane_pid)
        if not jsonl_path:
            return ""
        mtime = _os.path.getmtime(jsonl_path)
        cached = _tmux_summary_cache.get(pane_pid)
        if cached and cached[0] == jsonl_path and cached[1] == mtime:
            return cached[2]
        summary = _extract_session_summary(jsonl_path)
        _tmux_summary_cache[pane_pid] = (jsonl_path, mtime, summary)
        return summary
    except Exception:
        return ""


@app.route("/la-diag", methods=["POST"])
def la_diag():
    """Receive a one-line diagnostic message from the iOS LiveActivityManager.
    Each line is logged with a [LA-ios] prefix so the server log has the
    same visibility into Activity/token state that Xcode would show."""
    try:
        data = request.get_json(silent=True) or {}
        msg = (data.get("msg") or "").strip()[:500]
        sid = (data.get("session") or "").strip()[:40]
        if not msg:
            return jsonify({"ok": False, "error": "msg required"}), 400
        _log.info("[LA-ios] session=%s %s", sid or "-", msg)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/debug/la-state")
def la_state_debug():
    """Dump current Live Activity state + recent push history for ad-hoc
    debugging. Localhost-only to avoid leaking token prefixes + session
    labels over Tailscale / LAN."""
    remote = (request.remote_addr or "")
    if remote not in ("127.0.0.1", "::1", "localhost"):
        return jsonify({"error": "localhost only"}), 403
    import os as _os
    try:
        with _liveactivity_lock:
            tokens_snapshot = {
                aid: {
                    "sessionLabel": (rec.get("attributes") or {}).get("sessionLabel"),
                    "token_prefix": (rec.get("token") or "")[:16],
                    "last_update": rec.get("last_update"),
                    "age_sec": int(time.time() - (rec.get("last_update") or 0)),
                }
                for aid, rec in _liveactivity_tokens.items()
            }
            start_tokens = {
                bundle: [t[:16] for t in toks]
                for bundle, toks in _liveactivity_start_tokens_by_bundle().items()
            } if callable(globals().get("_liveactivity_start_tokens_by_bundle")) else {
                "com.timtrailor.terminal": [t[:16] for t in _liveactivity_start_tokens]
            }
        with _liveactivity_turn_state_lock:
            turn_state = {
                k: {kk: vv for kk, vv in v.items() if kk != "dismiss_timer"}
                for k, v in _liveactivity_turn_state.items()
            }
        return jsonify({
            "now": int(time.time()),
            "now_swift": _swift_date(time.time()),
            "active_tokens": tokens_snapshot,
            "start_tokens": start_tokens,
            "turn_state": turn_state,
            "watcher_state": {str(k): v for k, v in _mobile_watcher_state.items()},
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/tmux-windows")
def tmux_windows():
    """List tmux windows for the tab bar in the Terminal iOS app."""
    import subprocess as _sp
    import os as _os
    try:
        result = _sp.run(
            ["tmux", "list-windows", "-F",
             "#{window_index}	#{window_name}	#{window_active}	#{pane_current_command}	#{b:pane_current_path}	#{pane_pid}"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            return jsonify({"windows": [], "error": result.stderr.strip()})
        windows = []
        now = time.time()
        for line in result.stdout.strip().split("\n"):
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) >= 3:
                pane_pid = 0
                if len(parts) > 5:
                    try:
                        pane_pid = int(parts[5])
                    except ValueError:
                        pane_pid = 0
                window_name = parts[1]
                jsonl_path = _find_claude_session_file(pane_pid) if pane_pid else None
                if window_name and window_name.strip() and window_name.strip().lower() not in _DEFAULT_SHELL_NAMES:
                    summary = window_name.strip()
                elif jsonl_path:
                    summary = _thematic_title_for_session(jsonl_path)
                else:
                    summary = ""
                elapsed = -1
                status = ""
                if jsonl_path:
                    try:
                        mtime = _os.path.getmtime(jsonl_path)
                        elapsed = max(0, int(now - mtime))
                        status = "working" if elapsed <= 10 else "idle"
                    except Exception:
                        pass
                approval = _detect_pending_approval(jsonl_path) if jsonl_path else {"pending": False}
                windows.append({
                    "index": int(parts[0]),
                    "name": parts[1],
                    "active": parts[2] == "1",
                    "command": parts[3] if len(parts) > 3 else "",
                    "cwd": parts[4] if len(parts) > 4 else "",
                    "summary": summary,
                    "status": status,
                    "elapsed": elapsed,
                    "pendingApproval": approval.get("pending", False),
                    "pendingToolName": approval.get("tool_name", ""),
                })
        return jsonify({"windows": windows})
    except Exception as e:
        return jsonify({"windows": [], "error": str(e)})



@app.route("/tmux-new-window", methods=["POST"])
def tmux_new_window():
    """Create a new tmux window and return its (1-based) index so the iOS
    client can update its active-tab pointer in the same network round-trip
    instead of racing polling → user-type → wrong-tab-routing.

    Optional body: {cols:int, rows:int} — create the window at the
    client's viewport size so the shell renders its first prompt at the
    right dimensions. Without this, tmux new-window inherits the default
    80x24, the client sends a resize afterward, zsh redraws its prompt
    on SIGWINCH, and the redraw leaves the old prompt in scrollback
    (one extra prompt per resize). Reading scrollback later shows
    duplicated prompt lines."""
    import subprocess as _sp
    import time as _time
    _ensure_tmux_session()
    data = request.get_json(silent=True) or {}
    try:
        cols = int(data.get("cols", 0))
        rows = int(data.get("rows", 0))
    except (TypeError, ValueError):
        cols, rows = 0, 0
    try:
        result = _sp.run(
            ["/opt/homebrew/bin/tmux", "new-window", "-P",
             "-F", "#{window_index}", "-t", "mobile"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            return jsonify({"ok": False, "error": result.stderr.strip()})
        try:
            new_index = int((result.stdout or "").strip())
        except ValueError:
            new_index = 0
        # If the client told us its viewport size, resize the window and
        # then clear-history. zsh redraws its prompt on SIGWINCH and
        # leaves the pre-resize prompt in scrollback; without clearing
        # the pane shows duplicate prompt lines ("% " stacked). A fresh
        # window has no real history worth preserving, so wiping is
        # safe here.
        if 20 <= cols <= 500 and 5 <= rows <= 500 and new_index > 0:
            target = f"mobile:{new_index}"
            _sp.run(
                ["/opt/homebrew/bin/tmux", "resize-window", "-t", target,
                 "-x", str(cols), "-y", str(rows)],
                check=False, timeout=5,
            )
            # Give zsh a beat to finish its SIGWINCH redraw before we
            # wipe scrollback, otherwise the pre-resize prompt is still
            # on the visible screen (not in scrollback yet) and survives.
            _time.sleep(0.12)
            _sp.run(
                ["/opt/homebrew/bin/tmux", "clear-history", "-t", target],
                check=False, timeout=5,
            )
        return jsonify({"ok": True, "index": new_index})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500



@app.route("/tmux-rename-window", methods=["POST"])
def tmux_rename_window():
    """Rename a tmux window in the mobile session. Body: {index:int, name:str}."""
    import subprocess as _sp
    _ensure_tmux_session()
    data = request.get_json() or {}
    index = data.get("index")
    name = (data.get("name") or "").strip()
    if index is None or not name:
        return jsonify({"ok": False, "error": "index and name required"}), 400
    # Keep names short enough to display and strip shell-ish characters.
    safe_name = "".join(c for c in name if c.isalnum() or c in " _-.").strip()
    if not safe_name:
        return jsonify({"ok": False, "error": "name empty after sanitise"}), 400
    safe_name = safe_name[:40]
    try:
        _sp.run(
            ["/opt/homebrew/bin/tmux", "set-window-option",
             "-t", f"mobile:{int(index)}", "automatic-rename", "off"],
            capture_output=True, text=True, timeout=5,
        )
        _sp.run(
            ["/opt/homebrew/bin/tmux", "set-window-option",
             "-t", f"mobile:{int(index)}", "allow-rename", "off"],
            capture_output=True, text=True, timeout=5,
        )
        result = _sp.run(
            ["/opt/homebrew/bin/tmux", "rename-window",
             "-t", f"mobile:{int(index)}", safe_name],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return jsonify({"ok": False, "error": result.stderr.strip()}), 500
        return jsonify({"ok": True, "name": safe_name})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/tmux-kill-window", methods=["POST"])
def tmux_kill_window():
    """Close a tmux window by index. Also ends any Live Activities that
    were scoped to that tab's session label so the lock-screen / Dynamic
    Island card disappears at the same moment the tab does."""
    import subprocess as _sp
    _ensure_tmux_session()
    data = request.get_json() or {}
    index = data.get("index")
    if index is None:
        return jsonify({"error": "index required"}), 400
    try:
        result = _sp.run(
            ["/opt/homebrew/bin/tmux", "kill-window", "-t", f"mobile:{index}"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            return jsonify({"ok": False, "error": result.stderr.strip()})
        # Best-effort: end the corresponding LA. Failures don't block the
        # kill itself — we've already killed the tmux window successfully.
        try:
            ended = _end_live_activity_for_label(
                f"mobile-{int(index)}", reason="tab closed"
            )
        except Exception as exc:
            _log.warning("end-on-tab-kill failed for %s: %s", index, exc)
            ended = 0
        return jsonify({"ok": True, "la_ended": ended})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/tmux-select-window", methods=["POST"])
def tmux_select_window():
    """Switch to a tmux window by index."""
    import subprocess as _sp
    _ensure_tmux_session()
    data = request.get_json() or {}
    index = data.get("index")
    if index is None:
        return jsonify({"error": "index required"}), 400
    try:
        result = _sp.run(
            ["/opt/homebrew/bin/tmux", "select-window", "-t", f"mobile:{index}"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            return jsonify({"ok": False, "error": result.stderr.strip()})
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500




@app.route("/tmux-capture", methods=["GET"])
def tmux_capture():
    """Capture a tmux pane's content."""
    import subprocess as _sp
    _ensure_tmux_session()
    window = request.args.get("window", "1")
    lines = request.args.get("lines", "500")
    session = request.args.get("session", "mobile")
    target = f"{session}:{window}"
    try:
        result = _sp.run(
            ["/opt/homebrew/bin/tmux", "capture-pane", "-p", "-J", "-S", f"-{lines}", "-t", target],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            return jsonify({"ok": False, "error": result.stderr.strip()}), 500
        return jsonify({"ok": True, "content": result.stdout.rstrip()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/tmux-resize", methods=["POST"])
def tmux_resize():
    """Resize every window in the mobile session so TUI apps inside (Claude Code CLI)
    wrap at the phone's actual visual column count. Without this the pane defaults to
    80 cols, Claude Code wraps at ~78, and the iPhone's ~48-col SwiftUI view re-wraps
    every line mid-paragraph."""
    import subprocess as _sp
    _ensure_tmux_session()
    data = request.get_json(silent=True) or {}
    try:
        cols = int(data.get("cols", 0))
        rows = int(data.get("rows", 0))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "cols/rows must be integers"}), 400
    if cols < 20 or cols > 500 or rows < 5 or rows > 500:
        return jsonify({"ok": False, "error": "cols/rows out of range"}), 400
    session = data.get("session", "mobile")
    try:
        # List windows, resize each one individually. resize-window -A adapts the
        # smallest client but detached sessions need explicit -x/-y.
        lw = _sp.run(
            ["/opt/homebrew/bin/tmux", "list-windows", "-t", session, "-F", "#{window_index}"],
            capture_output=True, text=True, timeout=5
        )
        if lw.returncode != 0:
            return jsonify({"ok": False, "error": lw.stderr.strip()}), 500
        indices = [i.strip() for i in lw.stdout.splitlines() if i.strip()]
        resized = []
        for idx in indices:
            r = _sp.run(
                ["/opt/homebrew/bin/tmux", "resize-window", "-t", f"{session}:{idx}", "-x", str(cols), "-y", str(rows)],
                capture_output=True, text=True, timeout=5
            )
            if r.returncode == 0:
                resized.append(idx)
        return jsonify({"ok": True, "cols": cols, "rows": rows, "resized": resized})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/tmux-send-key", methods=["POST"])
def tmux_send_key():
    """Send a single keystroke to a tmux pane. Unlike /tmux-send-text (which
    uses paste-buffer) and /tmux-send-enter (which adds Home+End+Enter),
    this sends a bare key via `tmux send-keys` — exactly what Claude Code's
    single-keystroke prompt handlers (permission prompts + session feedback
    survey) expect. No paste wrapping, no Enter."""
    import subprocess as _sp
    data = request.get_json() or {}
    window = data.get("window", 1)
    key = str(data.get("key", ""))
    session = data.get("session", "mobile")
    if not key:
        return jsonify({"ok": False, "error": "key required"}), 400
    target = f"{session}:{window}"
    try:
        _sp.run(
            ["/opt/homebrew/bin/tmux", "send-keys", "-t", target, key],
            check=True, timeout=5,
        )
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/tmux-send-text", methods=["POST"])
def tmux_send_text():
    """Paste text into a tmux window (no Enter)."""
    import subprocess as _sp, base64 as _b64
    data = request.get_json() or {}
    text = data.get("text", "")
    window = data.get("window", 1)
    session = data.get("session", "mobile")
    target = f"{session}:{window}"
    if not text:
        return jsonify({"error": "text required"}), 400
    try:
        b64_text = _b64.b64encode(text.encode()).decode()
        _sp.run(
            f"echo {b64_text} | base64 -d | /opt/homebrew/bin/tmux load-buffer -",
            shell=True, check=True, timeout=5
        )
        _sp.run(
            ["/opt/homebrew/bin/tmux", "paste-buffer", "-t", target, "-d"],
            check=True, timeout=5
        )
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/tmux-send-enter", methods=["POST"])
def tmux_send_enter():
    """Send Enter key to a tmux window.

    A preceding `/tmux-send-text` call uses tmux `paste-buffer`, which
    injects bracketed-paste escape sequences (ESC[200~ … ESC[201~) into the
    pane. Claude Code's TUI needs a handful of milliseconds to finish
    parsing those markers into its `[Pasted text #N]` placeholder before
    it will treat a subsequent Enter as "submit". If the Enter arrives
    mid-parse the TUI either swallows it as a newline inside the paste or
    drops it — the paste sits in the input box forever and the turn never
    starts.

    Three hardeners:
      1. Small sleep so the paste finishes first.
      2. Send `Home` then `End` before Enter — cursor-only movement that
         forces Claude Code to exit any lingering paste-edit state.
      3. Then the actual Enter.
    """
    import subprocess as _sp
    import time as _time
    data = request.get_json() or {}
    window = data.get("window", 1)
    session = data.get("session", "mobile")
    target = f"{session}:{window}"
    try:
        _time.sleep(0.12)
        _sp.run(
            ["/opt/homebrew/bin/tmux", "send-keys", "-t", target, "Home", "End"],
            check=True, timeout=5,
        )
        _sp.run(
            ["/opt/homebrew/bin/tmux", "send-keys", "-t", target, "Enter"],
            check=True, timeout=5,
        )
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/push-message", methods=["POST"])
def push_message():
    """Push a message to all connected app clients (used by terminal sessions)."""
    data = request.get_json() or {}
    content = data.get("content", "")
    if not content:
        return jsonify({"error": "content required"}), 400
    _notify_smart("Terminal", content)
    return jsonify({"ok": True})





# -- Tmux response watcher for push notifications --
_tmux_watcher_cancel = threading.Event()


def _extract_claude_preview(content):
    """Extract a meaningful preview from Claude Code tmux pane content."""
    import re
    lines = content.split("\n")

    def is_chrome(line):
        s = line.strip()
        if not s:
            return True
        if s.startswith("▶▶") or s.startswith("⏵⏵") or "esc to" in s:
            return True
        if all(c in "─━═│┃║┌┐└┘├┤┬┴┼╭╮╯╰╱╲ " for c in s):
            return True
        if s.startswith("❯") or s == ">":
            return True
        if "Composing" in s or "Cooked for" in s or "tokens" in s or "Cogitated" in s:
            return True
        if re.match(r"^\d+\s+\d+\.\d+\.\d+", s):
            return True
        return False

    last_claude_idx = None
    for i in range(len(lines) - 1, -1, -1):
        s = lines[i].strip()
        if s.startswith("⏺") or s.startswith("●"):
            last_claude_idx = i
            break

    if last_claude_idx is not None:
        response_lines = []
        for i in range(last_claude_idx, len(lines)):
            line = lines[i].strip()
            if is_chrome(line):
                break
            if i == last_claude_idx:
                line = line.lstrip("⏺●").strip()
            if line:
                response_lines.append(line)
        if response_lines:
            text = " ".join(response_lines)
            return text[:200] if len(text) > 200 else text

    meaningful = [l.strip() for l in lines if l.strip() and not is_chrome(l)]
    if meaningful:
        return meaningful[-1][:200]
    return "Response complete"


_TERMINAL_STOP_REASONS = frozenset({"end_turn", "max_tokens", "stop_sequence"})


def _resolve_session_jsonl(session_label: str):
    """Best-effort JSONL lookup for a tmux session label. Preferred path:
    hook-registered pane→session map keyed by the active pane_pid of the
    tmux session. Fallback: walk the active pane and use
    _find_claude_session_file. Returns (jsonl_path, pane_pid) or (None, 0)."""
    import subprocess as _sp
    try:
        r = _sp.run(
            ["/opt/homebrew/bin/tmux", "display-message", "-p",
             "-t", session_label, "-F", "#{pane_pid}"],
            capture_output=True, text=True, timeout=3,
        )
        if r.returncode != 0 or not r.stdout.strip():
            return None, 0
        pane_pid = int(r.stdout.strip())
    except Exception:
        return None, 0
    rec = _pane_session_lookup(pane_pid)
    jsonl = (rec or {}).get("jsonl") or ""
    if jsonl and os.path.exists(jsonl):
        return jsonl, pane_pid
    fallback = _find_claude_session_file(pane_pid)
    return (fallback, pane_pid) if fallback else (None, pane_pid)


def _scan_assistant_tail(jsonl_path: str):
    """Read the tail of a Claude Code JSONL and return a list of
    (uuid, stop_reason, text) for every assistant message found. Newest
    last. `text` is the first text content part, or '' for tool_use-only
    turns."""
    import json as _json
    out = []
    try:
        size = os.path.getsize(jsonl_path)
        to_read = min(size, 512 * 1024)
        with open(jsonl_path, "rb") as f:
            f.seek(max(0, size - to_read))
            blob = f.read().decode("utf-8", errors="replace")
    except Exception as exc:
        _log.warning("jsonl tail read failed for %s: %s", jsonl_path, exc)
        return out
    for line in blob.split("\n"):
        if not line.strip():
            continue
        try:
            obj = _json.loads(line)
        except Exception:
            continue
        if obj.get("type") != "assistant":
            continue
        msg = obj.get("message") or {}
        stop_reason = msg.get("stop_reason") or ""
        uuid = obj.get("uuid") or msg.get("id") or ""
        text = ""
        content = msg.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    text = (part.get("text") or "").strip()
                    if text:
                        break
        elif isinstance(content, str):
            text = content.strip()
        out.append((uuid, stop_reason, text))
    return out


def _watch_tmux_worker(session, timeout_secs=600, window_index=None):
    """Tail the session's Claude JSONL and push-notify on real turn end.

    Replaces the old hash-stabilise worker which fired false positives on
    long Claude pauses. A turn is "done" when a new assistant record
    appears with stop_reason in {end_turn, max_tokens, stop_sequence}.
    tool_use terminates mid-turn so we ignore it.

    Falls back to doing nothing (single 'expired' log) if the JSONL for
    the session cannot be resolved — the Stop hook still drives Live
    Activity completion via /internal/claude-hook, so this worker is the
    backup path for the banner push, not the primary signal.
    """
    start = time.time()
    _tmux_watcher_cancel.clear()
    _log.info("Tmux watcher (JSONL) started for session '%s'", session)

    jsonl_path = None
    pane_pid = 0
    # Give the session a beat to materialise its JSONL file before we
    # snapshot the baseline — new sessions create the file after the
    # first user prompt lands.
    for _ in range(5):
        if _tmux_watcher_cancel.is_set():
            return
        jsonl_path, pane_pid = _resolve_session_jsonl(session)
        if jsonl_path:
            break
        _tmux_watcher_cancel.wait(1)

    if not jsonl_path:
        _log.warning("Tmux watcher: could not resolve JSONL for session %s — "
                     "no banner push will fire; Stop hook still covers LA state.",
                     session)
        return

    # Baseline: the newest assistant uuid at watch-start (may be empty for
    # a brand-new session). We fire only when a NEW assistant uuid appears
    # after this point with a terminal stop_reason.
    baseline_uuid = ""
    try:
        records = _scan_assistant_tail(jsonl_path)
        if records:
            baseline_uuid = records[-1][0]
    except Exception:
        pass

    _log.info("Tmux watcher: baseline uuid=%s jsonl=%s",
              baseline_uuid[:8] if baseline_uuid else "-",
              os.path.basename(jsonl_path))

    last_mtime = 0.0
    while not _tmux_watcher_cancel.is_set() and (time.time() - start) < timeout_secs:
        try:
            try:
                mtime = os.path.getmtime(jsonl_path)
            except OSError:
                mtime = 0
            if mtime == last_mtime:
                _tmux_watcher_cancel.wait(2)
                continue
            last_mtime = mtime

            records = _scan_assistant_tail(jsonl_path)
            if not records:
                _tmux_watcher_cancel.wait(2)
                continue

            # Walk backwards to find the newest assistant record with a
            # terminal stop reason AND a uuid we haven't seen before.
            fired = False
            for uuid, stop_reason, text in reversed(records):
                if uuid == baseline_uuid:
                    break
                if stop_reason not in _TERMINAL_STOP_REASONS:
                    continue
                body = text.strip() or "Task complete"
                if len(body) > 400:
                    body = body[:397].rstrip() + "…"
                baseline_uuid = uuid
                _send_push_notification("Claude finished", body,
                                       window_index=window_index)
                # Silent mirror so the app (if backgrounded) wakes and
                # pre-fetches the fresh pane before the user taps.
                if window_index is not None:
                    _send_push_notification(
                        "", "", window_index=window_index,
                        content_available=True,
                    )
                try:
                    _liveactivity_on_turn_complete(session, body)
                except Exception as exc:
                    _log.error("LiveActivity turn-complete (tmux watcher) failed: %s",
                               exc, exc_info=True)
                _log.info("Tmux watcher: stop_reason=%s uuid=%s push sent: %s",
                          stop_reason, uuid[:8], body[:60])
                fired = True
                break
            if fired:
                return
        except Exception as exc:
            _log.error("Tmux watcher iteration error: %s", exc, exc_info=True)
        _tmux_watcher_cancel.wait(2)

    _log.info("Tmux watcher expired for session '%s'", session)


@app.route("/watch-tmux", methods=["POST"])
def watch_tmux():
    """Start watching a tmux session for response completion. Sends push when done."""
    data = request.get_json() or {}
    session = data.get("session", "mobile")
    cancel = data.get("cancel", False)

    if cancel:
        _tmux_watcher_cancel.set()
        return jsonify({"ok": True, "status": "cancelled"})

    _tmux_watcher_cancel.set()
    import time as _time
    _time.sleep(0.1)

    # Kick off a Live Activity thinking-phase push (or push-to-start).
    # The LA label may be more specific than the tmux session (e.g.
    # "mobile-2" for tab 2) so each tab owns its own Activity.
    la_label = data.get("label") or session
    try:
        threading.Thread(
            target=_liveactivity_on_turn_start,
            kwargs={"session_label": la_label, "headline": "Working…"},
            daemon=True,
        ).start()
    except Exception as exc:
        _log.error("LiveActivity turn-start (watch-tmux) failed: %s",
                   exc, exc_info=True)

    # Parse window index from label (e.g. "mobile-2" → 2) for deep-linkable
    # pushes. Label is iOS-owned so it reliably tracks the tab the user sent
    # the message from; the bare `session` is tmux-level and lacks that.
    window_index = None
    import re as _re
    m = _re.search(r"(\d+)$", la_label or "")
    if m:
        try:
            window_index = int(m.group(1))
        except ValueError:
            window_index = None

    t = threading.Thread(
        target=_watch_tmux_worker,
        args=(session,),
        kwargs={"window_index": window_index},
        daemon=True,
    )
    t.start()
    return jsonify({"ok": True, "status": "watching"})


@app.route("/infrastructure-health")
def infrastructure_health():
    """Return health_check.py results for iOS dashboard."""
    import json as _json
    from pathlib import Path as _Path
    results_file = _Path("/tmp/health_check_results.json")
    if not results_file.exists():
        return jsonify({"error": "no health check results yet", "checks": [], "summary": {}}), 200
    try:
        data = _json.loads(results_file.read_text())
        # Add staleness check
        ts = data.get("timestamp", "")
        if ts:
            from datetime import datetime, timezone
            check_time = datetime.fromisoformat(ts)
            if check_time.tzinfo is None:
                check_time = check_time.replace(tzinfo=timezone.utc)
            age_h = (datetime.now(timezone.utc) - check_time).total_seconds() / 3600
            data["age_hours"] = round(age_h, 1)
            data["stale"] = age_h > 6
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e), "checks": [], "summary": {}}), 200
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
        # Check backup manifest for school docs — use groups_verified timestamp
        manifest = _find(".backup_manifest.json")
        school_in_backup = False
        verified_ts = None
        if manifest.exists():
            try:
                with open(manifest) as mf:
                    mdata = json.load(mf)
                school_files = {k: v for k, v in mdata.get("files", {}).items() if "school" in k.lower()}
                school_in_backup = bool(school_files)
                # Use groups_verified (set every backup run) instead of per-file backed_up_at
                verified_ts = mdata.get("groups_verified", {}).get("school_docs")
                if not verified_ts and school_files:
                    verified_ts = max(v.get("backed_up_at", "") for v in school_files.values())
            except Exception:
                pass
        detail = f"{doc_count} files, {'backed up' if school_in_backup else 'NOT backed up'}"
        if verified_ts:
            ts = verified_ts
        else:
            ts = datetime.fromtimestamp(school_docs.stat().st_mtime).isoformat()
        items.append({
            "name": "School Docs",
            "timestamp": ts,
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

        # Session dirs are now persistent (in project dir) — no cleanup
        # Only server logs in /tmp get cleaned up after 7 days
        try:
            log_path = os.path.join(_TMP_LOG_DIR, 'server.log')
            if os.path.exists(log_path):
                mtime = os.path.getmtime(log_path)
                if now - mtime > 604800:  # 7 days
                    open(log_path, 'w').close()
                    _log.info('Cleanup: truncated old server.log')
        except Exception as e:
            _log.warning('Cleanup error (logs): %s', e)

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

    _start_mobile_session_watcher()
    _log.info("Mobile session watcher thread started")

    app.run(host="0.0.0.0", port=8081, threaded=True)
