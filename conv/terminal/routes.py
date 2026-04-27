"""Terminal route handlers — Flask blueprint.

Routes:
  POST /terminal-new-window      — open a new tmux window in claude-terminal
  POST /terminal-reset           — kill ttyd + tmux and restart fresh
  POST /terminal-auth-start      — begin `claude auth login` via PTY, return URL
  POST /terminal-auth-complete   — feed the OAuth code into the waiting PTY
"""

from __future__ import annotations

import os
import re
import select
import subprocess
import threading
import time
import uuid

from flask import Blueprint, jsonify, request

from conv.app import _log, is_dry_run

from .auth import _auth_session, _cleanup_auth_session, _terminal_claude_cmd

bp = Blueprint("terminal", __name__)


@bp.route("/terminal-new-window", methods=["POST"])
def terminal_new_window():
    """Create a new tmux window in the claude-terminal session."""
    if is_dry_run("TERMINAL"):
        _log.info("[DRY RUN] terminal_new_window")
        return jsonify({"ok": True, "dry_run": True})
    try:
        result = subprocess.run(
            [
                "/opt/homebrew/bin/tmux",
                "new-window",
                "-t",
                "claude-terminal",
                "-c",
                os.path.expanduser("~/Documents/Claude code"),
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            time.sleep(0.5)
            subprocess.run(
                [
                    "/opt/homebrew/bin/tmux",
                    "send-keys",
                    "-t",
                    "claude-terminal",
                    _terminal_claude_cmd(),
                    "Enter",
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            _log.info(
                "Created new tmux window in claude-terminal (claude auto-started)"
            )
            return jsonify({"ok": True})
        return jsonify({"ok": False, "error": result.stderr.strip()}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/terminal-reset", methods=["POST"])
def terminal_reset():
    """Kill ttyd + tmux and restart fresh. Used by iOS 'Reset Terminal' button."""
    if is_dry_run("TERMINAL"):
        _log.info("[DRY RUN] terminal_reset")
        return jsonify({"ok": True, "dry_run": True})
    try:
        tmux = "/opt/homebrew/bin/tmux"
        # 1. Kill ttyd
        subprocess.run(["pkill", "-f", "ttyd"], capture_output=True, timeout=5)
        # 2. Kill tmux session
        subprocess.run(
            [tmux, "kill-session", "-t", "claude-terminal"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        time.sleep(2)
        # 3. Restart via wrapper script
        result = subprocess.run(
            [
                "/bin/zsh",
                "-l",
                "-c",
                "export PATH=/opt/homebrew/bin:/usr/local/bin:$PATH && "
                "~/.local/bin/ttyd_wrapper.sh start",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if "Listening on port" in result.stderr or "ttyd started" in result.stdout:
            _log.info("terminal-reset: restarted successfully")
            return jsonify({"ok": True})
        # Wrapper runs ttyd in background, so check if it's actually listening
        time.sleep(2)
        check = subprocess.run(
            ["lsof", "-i", ":7681"], capture_output=True, text=True, timeout=5
        )
        if "ttyd" in check.stdout:
            _log.info("terminal-reset: restarted (confirmed via lsof)")
            return jsonify({"ok": True})
        _log.warning(
            "terminal-reset: wrapper output: stdout=%s stderr=%s",
            result.stdout[:300],
            result.stderr[:300],
        )
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "ttyd may not have started",
                    "stdout": result.stdout[:300],
                    "stderr": result.stderr[:300],
                }
            ),
            500,
        )
    except Exception as e:
        _log.error("terminal-reset failed: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/terminal-auth-start", methods=["POST"])
def terminal_auth_start():
    """Start `claude auth login` via PTY and return the OAuth URL."""
    if is_dry_run("TERMINAL"):
        # Pattern 2: guard before action. Without this, staging would spawn
        # a real claude auth login PTY against production auth state even
        # if downstream tmux/ttyd restart is dry-run-guarded.
        _log.info("[DRY RUN] terminal_auth_start")
        return jsonify(
            {
                "ok": True,
                "dry_run": True,
                "url": "https://claude.ai/oauth/authorize?fake=staging",
                "session_id": "staging-fake-session",
            }
        )
    import pty

    # Clean up any previous session
    _cleanup_auth_session()

    try:
        # Unlock keychain (may already be unlocked — ignore errors)
        try:
            from credentials import KEYCHAIN_PASSWORD

            subprocess.run(
                [
                    "security",
                    "unlock-keychain",
                    "-p",
                    KEYCHAIN_PASSWORD,
                    os.path.expanduser("~/Library/Keychains/login.keychain-db"),
                ],
                capture_output=True,
                timeout=5,
            )
        except (ImportError, Exception):
            pass  # No password configured or unlock failed — proceed anyway

        # Create PTY pair
        master_fd, slave_fd = pty.openpty()

        # Start claude auth login with PTY
        env = os.environ.copy()
        env["TERM"] = "dumb"
        # Ensure node and brew binaries are in PATH (needed by claude CLI)
        env["PATH"] = (
            "/Users/timtrailor/.local/bin:/opt/homebrew/bin:/usr/local/bin:"
            + env.get("PATH", "")
        )
        proc = subprocess.Popen(
            ["/Users/timtrailor/.local/bin/claude", "auth", "login"],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            env=env,
            close_fds=True,
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
                    match = re.search(
                        r"(https://claude\.ai/oauth/authorize[^\s\x1b]*)", output
                    )
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
            _log.warning(
                "terminal-auth-start: no OAuth URL found. Output: %s", output[:500]
            )
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": "No OAuth URL received",
                        "output": output[:500],
                    }
                ),
                500,
            )

        # Store session for completion
        session_id = str(uuid.uuid4())
        _auth_session.update(
            {"process": proc, "master_fd": master_fd, "session_id": session_id}
        )

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


@bp.route("/terminal-auth-complete", methods=["POST"])
def terminal_auth_complete():
    """Feed the OAuth code to the waiting `claude auth login` process."""
    data = request.get_json(force=True)
    code = data.get("code", "").strip()
    if not code:
        return jsonify({"ok": False, "error": "No code provided"}), 400

    if is_dry_run("TERMINAL"):
        # Pattern 2: guard before action. Without this, staging would write
        # the code to a real PTY and run real `claude auth status` against
        # production auth state.
        _log.info("[DRY RUN] terminal_auth_complete code_len=%d", len(code))
        return jsonify({"ok": True, "dry_run": True, "status": "staging dry-run"})

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
        auth_env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + auth_env.get(
            "PATH", ""
        )
        auth_check = subprocess.run(
            ["/Users/timtrailor/.local/bin/claude", "auth", "status"],
            capture_output=True,
            text=True,
            timeout=10,
            env=auth_env,
        )
        auth_ok = auth_check.returncode == 0

        _cleanup_auth_session()

        if auth_ok:
            # Kill and recreate tmux session so it picks up the new auth.
            # Claude Code TUI ignores "exit" via send-keys, so we nuke the session.
            # Also restart ttyd so it reconnects to the new session.
            tmux = "/opt/homebrew/bin/tmux"
            try:
                # Kill ttyd first (it's attached to the tmux session)
                subprocess.run(["pkill", "-f", "ttyd"], capture_output=True, timeout=5)
                time.sleep(0.5)
                subprocess.run(
                    [tmux, "kill-session", "-t", "claude-terminal"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                time.sleep(1)
                # Recreate tmux session
                subprocess.run(
                    [
                        tmux,
                        "new-session",
                        "-d",
                        "-s",
                        "claude-terminal",
                        "-x",
                        "120",
                        "-y",
                        "40",
                        "-c",
                        os.path.expanduser("~/Documents/Claude code"),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                subprocess.run(
                    [tmux, "set", "-t", "claude-terminal", "history-limit", "50000"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                time.sleep(0.5)
                subprocess.run(
                    [
                        tmux,
                        "send-keys",
                        "-t",
                        "claude-terminal",
                        _terminal_claude_cmd(),
                        "Enter",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                _log.info("terminal-auth-complete: tmux session recreated")
                # Restart ttyd attached to new session
                ttyd_wrapper = os.path.expanduser("~/.local/bin/ttyd_wrapper.sh")
                subprocess.Popen(
                    [ttyd_wrapper, "start"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                _log.info("terminal-auth-complete: ttyd restarted")
            except Exception as restart_err:
                _log.warning(
                    "terminal-auth-complete: tmux/ttyd restart failed: %s", restart_err
                )

        _log.info("terminal-auth-complete: auth_ok=%s", auth_ok)
        return jsonify(
            {
                "ok": auth_ok,
                "status": auth_check.stdout.strip()
                if auth_ok
                else auth_check.stderr.strip(),
            }
        )

    except Exception as e:
        _cleanup_auth_session()
        _log.error("terminal-auth-complete failed: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500
