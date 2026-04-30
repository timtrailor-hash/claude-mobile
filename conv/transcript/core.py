"""Transcript / summary / pane-session helpers — extracted from
conversation_server.py 2026-04-30 (slice 1M).

This module owns three related concerns:
1. JSONL transcript parsing (Claude Code session files): _find_claude_session_file,
   _extract_session_summary, _collect_session_user_prompts, _clean_user_text.
2. LLM-backed summarisation: _generate_summary_llm, _generate_thematic_title_llm,
   _thematic_title_for_session, the _summary_llm_cache.
3. Pane-to-session mapping: _pane_session_map, _pane_session_lookup,
   _register_pane_session, _load/_save_pane_session_map.

Plus the small _tmux_target_for_label utility used to resolve LA session
labels back to tmux targets.

The extraction was move-only: every function and constant is preserved
verbatim. Module-level state (_summary_llm_cache, _pane_session_map) is now
defined here; conversation_server.py re-imports them so existing callers work
unchanged.
"""

from __future__ import annotations

import json
import os
import threading
import re
import time

from conv.app import _log

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
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            _log.warning(
                "claude -p summary failed rc=%s stderr=%s",
                result.returncode,
                (result.stderr or "").strip()[:200],
            )
            return ""
        lines = [ln.strip() for ln in (result.stdout or "").splitlines() if ln.strip()]
        if not lines:
            return ""
        title = lines[0].strip("\"'`.:;,")
        for prefix in ("Title:", "Task:", "Summary:"):
            if title.lower().startswith(prefix.lower()):
                title = title[len(prefix) :].strip()
        return title[:60]
    except _sp.TimeoutExpired:
        _log.warning("claude -p summary timed out after %ss", timeout)
        return ""
    except Exception as exc:
        _log.warning("claude -p summary error: %s", exc)
        return ""


_DEFAULT_SHELL_NAMES = {"zsh", "bash", "sh", "fish", "ksh", "tcsh", "dash"}

_THEME_WINDOW = 10  # number of recent user prompts fed to the LLM
_THEME_BUCKET_SIZE = 3  # regenerate thematic title every N new user messages


def _collect_session_user_prompts(
    jsonl_path: str, max_bytes: int = 4 * 1024 * 1024
) -> list:
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
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            _log.warning(
                "thematic title failed rc=%s stderr=%s",
                result.returncode,
                (result.stderr or "").strip()[:200],
            )
            return ""
        lines = [ln.strip() for ln in (result.stdout or "").splitlines() if ln.strip()]
        if not lines:
            return ""
        title = lines[0].strip("\"'`.:;,")
        for prefix in ("Title:", "Task:", "Summary:", "Theme:"):
            if title.lower().startswith(prefix.lower()):
                title = title[len(prefix) :].strip()
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
            stale = [
                k
                for k in list(_summary_llm_cache.keys())
                if k.startswith(f"theme:{sid}:") and k != hash_key
            ]
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

    threading.Thread(
        target=_bg, args=(blob, key, session_id, bucket), daemon=True
    ).start()
    return fallback or recent[-1][:60]


_PANE_SESSION_FILE = "/tmp/claude_sessions/pane_session_map.json"
_pane_session_map = {}  # {pane_pid: {"session_id", "jsonl", "last_seen", "pane_id", "tmux_session"}}
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

        ps = _sp.run(
            ["ps", "-A", "-o", "pid="], capture_output=True, text=True, timeout=3
        )
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
        _log.info(
            "Loaded %d pane→session mappings from %s", len(loaded), _PANE_SESSION_FILE
        )
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


def _register_pane_session(
    pane_pid: int,
    *,
    session_id: str,
    jsonl: str,
    pane_id: str = "",
    tmux_session: str = "",
):
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


def _tmux_target_for_label(label: str) -> str:
    """Translate a Live Activity session label (e.g. "mobile-3") into a
    tmux target string (e.g. "mobile:3"). Non-mobile labels are returned
    unchanged so existing tmux session names still work."""
    if label.startswith("mobile-"):
        suffix = label[len("mobile-") :]
        if suffix.isdigit():
            return f"mobile:{suffix}"
    return label


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
        ps = _sp.run(
            ["ps", "-ax", "-o", "pid=,ppid=,comm="],
            capture_output=True,
            text=True,
            timeout=3,
        )
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
        lsof = _sp.run(
            ["lsof", "-p", str(claude_pid)], capture_output=True, text=True, timeout=3
        )
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
            matches = _glob.glob(
                _os.path.expanduser(f"~/.claude/projects/*/{uuid}.jsonl")
            )
            if matches:
                return matches[0]

        # Fallback: no task-dir UUID visible via lsof. Use claude's CWD to
        # compute the project slug, then pick the most-recently-modified
        # jsonl whose birth time is after the claude process start time.
        try:
            cwd_run = _sp.run(
                ["lsof", "-a", "-p", str(claude_pid), "-d", "cwd", "-Fn"],
                capture_output=True,
                text=True,
                timeout=3,
            )
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

            start_run = _sp.run(
                ["ps", "-o", "etime=", "-p", str(claude_pid)],
                capture_output=True,
                text=True,
                timeout=3,
            )
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
