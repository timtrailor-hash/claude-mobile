"""LiveActivity (ActivityKit) push-to-start, update, end + watcher loops.

Extracted from conversation_server.py 2026-04-30 (slice 1N — plan §13's
deferred slice 8). Move-only; the 9-day-outage hardening logic is preserved
verbatim. Slice 1M (conv.transcript) provided the parser/summary helpers
this module imports.
"""

from __future__ import annotations

import json
import os
import threading
import time

from conv.app import _log, is_dry_run
from conv.apns import apns_jwt as _apns_jwt
from conv.pane_logger import (
    record_and_log as _log_la_transition,
    detect_with_reason as _detect_with_reason,
)
from conv.push.notify import _APNS_HOST
from conv.transcript import (
    _clean_user_text,
    _extract_session_summary,
    _find_claude_session_file,
    _prompt_hash,
    _summary_llm_cache,
    _summary_llm_lock,
    _thematic_title_for_session,
    _tmux_target_for_label,
)
from conv.websocket import _broadcast_ws


# ---- Live Activity (ActivityKit push-to-start + update) support ----
# Persisted state for push-to-start tokens (per bundle) and per-activity
# update tokens. State is written to disk so it survives daemon restart.
_LIVEACTIVITY_FILE = os.environ.get("CONV_LIVEACTIVITY_FILE") or os.path.expanduser(
    "~/code/apns_state/liveactivity_tokens.json"
)
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
        _log.error("LiveActivity state file corrupt, resetting: %s", exc, exc_info=True)
        try:
            os.rename(
                _LIVEACTIVITY_FILE, f"{_LIVEACTIVITY_FILE}.corrupt.{int(time.time())}"
            )
        except OSError as rename_exc:
            _log.error(
                "Could not rename corrupt LiveActivity state: %s",
                rename_exc,
                exc_info=True,
            )
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
        _log.error("Failed to persist LiveActivity state: %s", exc, exc_info=True)


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

# APNs endpoint comment retained: sandbox is correct as long as the app is
# signed with aps-environment=development (see TerminalApp.entitlements). If
# the entitlement ever flips to production, update _APNS_HOST in conv.push.notify
# AND the _send_push_notification path in lockstep.


def _push_liveactivity(
    token,
    *,
    event,
    content_state,
    attributes=None,
    dismissal_date=None,
    alert_title=None,
    alert_body=None,
    stale_date=None,
):
    """Send an ActivityKit APNs push. event is start, update or end.

    Returns (status_code, response_body). Does NOT raise on APNs-level
    failure (400/410 are logged and handled); only raises on programmer
    errors (bad args, missing credentials, curl not runnable).
    """
    if event not in ("start", "update", "end"):
        raise ValueError(f"invalid event: {event}")
    if event == "start" and attributes is None:
        raise ValueError("attributes required for start event")
    if is_dry_run("APNS"):
        _log.info(
            "[DRY RUN] _push_liveactivity event=%s token=%s", event, str(token)[:16]
        )
        # Return string '200' to match the real-path contract — _push_liveactivity
        # parses status from curl stdout's last line (always a string). Callers
        # compare status_code == '200' (e.g. lines 1940, 1950, 2584, 2586).
        return "200", {"dry_run": True}

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
    result = _sp.run(
        [
            "curl",
            "-s",
            "--http2",
            "--connect-timeout",
            "5",
            "--max-time",
            "12",
            "-H",
            f"authorization: bearer {jwt_token}",
            "-H",
            f"apns-topic: {_LIVEACTIVITY_TOPIC}",
            "-H",
            "apns-push-type: liveactivity",
            "-H",
            "apns-priority: 10",
            "-d",
            payload,
            "-w",
            "\n%{http_code}",
            url,
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )

    stdout = (result.stdout or "").strip()
    lines = stdout.split("\n") if stdout else []
    status_code = lines[-1] if lines else "?"
    response_body = "\n".join(lines[:-1])
    if result.returncode != 0 and not stdout:
        _log.error(
            "LiveActivity curl exit=%s stderr=%s",
            result.returncode,
            (result.stderr or "").strip()[:500],
        )
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
            stale_aids = [
                aid
                for aid, rec in list(_liveactivity_tokens.items())
                if rec.get("token") == token
            ]
            for aid in stale_aids:
                _liveactivity_tokens.pop(aid, None)
            _save_liveactivity_state()
    else:
        _log.warning(
            "LiveActivity push failed (event=%s HTTP %s): %s",
            event,
            status_code,
            response_body,
        )
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
                _log.info(
                    "Pruned %d stale LiveActivity record(s): %s",
                    len(stale),
                    ", ".join(stale),
                )
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
            [
                "/opt/homebrew/bin/tmux",
                "display-message",
                "-p",
                "-t",
                tmux_target,
                "-F",
                "#{pane_pid}",
            ],
            capture_output=True,
            text=True,
            timeout=3,
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


_mobile_watcher_state = {}  # {pane_pid: last_user_uuid}
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
                [
                    "/opt/homebrew/bin/tmux",
                    "list-panes",
                    "-s",
                    "-t",
                    "mobile",
                    "-F",
                    "#{pane_pid}\t#{window_index}",
                ],
                capture_output=True,
                text=True,
                timeout=3,
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
                    _log.info(
                        "mobile watcher: new user message in pane %s (tab %s) uuid=%s — firing LA turn_start on %s",
                        pane_pid,
                        window_idx,
                        uuid[:8],
                        la_label,
                    )
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
    threading.Thread(
        target=_mobile_session_watcher_loop, daemon=True, name="mobile-session-watcher"
    ).start()


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
                [
                    "/opt/homebrew/bin/tmux",
                    "display-message",
                    "-p",
                    "-t",
                    tmux_target,
                    "-F",
                    "#{pane_pid}",
                ],
                capture_output=True,
                text=True,
                timeout=3,
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

    def _refresh_headline_when_ready(
        label=session_label, snap_started=_LiveActivity_headline_refresh_snapshot
    ):
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
                    _push_liveactivity(
                        tok,
                        event="update",
                        content_state=cs,
                        alert_title="Claude",
                        alert_body=new_headline,
                    )
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
                _log.error(
                    "Failed to cancel prior LiveActivity timer: %s", exc, exc_info=True
                )
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
                _push_liveactivity(
                    tok,
                    event="update",
                    content_state=content_state,
                    alert_title="Claude",
                    alert_body=headline,
                )
            except Exception as exc:
                _log.error(
                    "LiveActivity update (turn_start) error: %s", exc, exc_info=True
                )
        return
    # No live activity yet — try push-to-start using ONLY the most recent
    # start token so we don't spawn duplicate activities on every turn.
    #
    # 2026-04-18 fix: also guard against iOS never registering the
    # activity-token back after our event=start (e.g. app backgrounded /
    # phone locked). Without this guard, every subsequent turn_start
    # would see active_tokens=0 and fire another event=start, creating
    # orphan LAs on iOS that pile up indefinitely because the server
    # has no activity-token to push an event=end to.
    #
    # Track last_start_push_at on the turn_state entry. Suppress event=start
    # if we sent one for this label within the last 30 min — matches the
    # la-reaper threshold so orphans either get reclaimed or the entry
    # ages out naturally.
    with _liveactivity_turn_state_lock:
        ts = _liveactivity_turn_state.setdefault(session_label, {})
        last_start = ts.get("last_start_push_at", 0) or 0
    if last_start and (time.time() - last_start) < 30 * 60:
        _log.info(
            "LiveActivity turn_start: suppressing event=start for "
            "session=%s (last start %ds ago; iOS never registered "
            "activity-token — probably backgrounded)",
            session_label,
            int(time.time() - last_start),
        )
        return
    with _liveactivity_lock:
        tokens = list(_liveactivity_start_tokens)
    if tokens:
        tok = tokens[-1]
        try:
            _push_liveactivity(
                tok,
                event="start",
                content_state=content_state,
                attributes={"sessionLabel": session_label},
                stale_date=started_at + 15 * 60,
                alert_title="Claude",
                alert_body=headline,
            )
            with _liveactivity_turn_state_lock:
                ts = _liveactivity_turn_state.setdefault(session_label, {})
                ts["last_start_push_at"] = time.time()
        except Exception as exc:
            _log.error("LiveActivity start error: %s", exc, exc_info=True)


def _liveactivity_on_turn_complete(session_label, body_text):
    """Push a finished-phase update and schedule auto-dismiss after 10 min."""
    with _liveactivity_turn_state_lock:
        state = dict(_liveactivity_turn_state.get(session_label) or {})
    started_at = state.get("started_at")
    if started_at is None:
        _log.warning(
            "LiveActivity turn_complete: no started_at for session %s "
            "(out-of-order hook?) — elapsed will be 0",
            session_label,
        )
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
        _log.warning(
            "turn_complete(%s): no active Live Activity tokens — "
            "phase=finished push skipped. Island will not flip to "
            "Done; most likely the iOS app didn't register an "
            "update token after push-to-start.",
            session_label,
        )
        return
    _la_arc_last_reset.pop(session_label, None)
    for _aid, tok in active:
        try:
            _push_liveactivity(
                tok,
                event="update",
                content_state=content_state,
                alert_title="Claude finished",
                alert_body=headline,
            )
        except Exception as exc:
            _log.error(
                "LiveActivity update (turn_complete) error: %s", exc, exc_info=True
            )

    captured_gen = state.get("generation", 0)

    def _end_later(
        tokens_snapshot=list(active),
        cs=content_state,
        gen=captured_gen,
        label=session_label,
    ):
        with _liveactivity_turn_state_lock:
            current = int(
                (_liveactivity_turn_state.get(label) or {}).get("generation", 0)
            )
            if current != gen:
                _log.info(
                    "LiveActivity dismiss-timer generation mismatch "
                    "(captured=%s current=%s) — skipping",
                    gen,
                    current,
                )
                return
            # Clear our slot so the record doesn't leak a stale Timer ref.
            rec = _liveactivity_turn_state.get(label) or {}
            if rec.get("dismiss_timer") is not None:
                rec["dismiss_timer"] = None
        dismiss_at = int(time.time())
        for _a, t in tokens_snapshot:
            try:
                _push_liveactivity(
                    t, event="end", content_state=cs, dismissal_date=dismiss_at
                )
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
            _log.info(
                "LiveActivity turn_complete: generation moved on "
                "(captured=%s current=%s) — not arming dismiss timer",
                captured_gen,
                rec.get("generation"),
            )
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
                _log.error(
                    "Failed to cancel prior LiveActivity timer: %s", exc, exc_info=True
                )
        new_gen = int(prior.get("generation", 0)) + 1
        prior["generation"] = new_gen
        prior["dismiss_timer"] = None
        _liveactivity_turn_state[session_label] = prior
        state = dict(prior)
    started_at = state.get("started_at")
    if started_at is None:
        _log.warning(
            "LiveActivity turn_error: no started_at for session %s", session_label
        )
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
            _push_liveactivity(
                tok,
                event="update",
                content_state=content_state,
                alert_title="Claude error",
                alert_body=msg[:60],
            )
        except Exception as exc:
            _log.error("LiveActivity update (turn_error) error: %s", exc, exc_info=True)

    captured_gen = state.get("generation", 0)

    def _end_later(
        tokens_snapshot=list(active),
        cs=content_state,
        gen=captured_gen,
        label=session_label,
    ):
        with _liveactivity_turn_state_lock:
            current = int(
                (_liveactivity_turn_state.get(label) or {}).get("generation", 0)
            )
            if current != gen:
                _log.info(
                    "LiveActivity dismiss-timer (error) generation mismatch "
                    "(captured=%s current=%s) — skipping",
                    gen,
                    current,
                )
                return
            rec = _liveactivity_turn_state.get(label) or {}
            if rec.get("dismiss_timer") is not None:
                rec["dismiss_timer"] = None
        dismiss_at = int(time.time())
        for _a, t in tokens_snapshot:
            try:
                _push_liveactivity(
                    t, event="end", content_state=cs, dismissal_date=dismiss_at
                )
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
            _log.info(
                "LiveActivity turn_error: generation moved on "
                "(captured=%s current=%s) — not arming dismiss timer",
                captured_gen,
                rec.get("generation"),
            )
    if started:
        timer.start()


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
                        _push_liveactivity(
                            tok,
                            event="update",
                            content_state=content_state,
                            stale_date=now + 15 * 60,
                        )
                    except Exception as exc:
                        _log.warning("arc refresher push failed for %s: %s", label, exc)
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
    threading.Thread(
        target=_la_arc_refresher_loop, daemon=True, name="la-arc-refresher"
    ).start()


_SERVER_BASE_URL_FOR_LA = os.environ.get(
    "CONV_SERVER_BASE_URL", "http://100.126.253.40:8081"
)


def _attach_prompt_options(content_state: dict, session_label: str) -> None:
    """Populate content_state["options"]. Transition logging handled inside
    _capture_pane_options_for_label (conv.pane_logger)."""
    content_state["serverBaseURL"] = _SERVER_BASE_URL_FOR_LA
    if content_state.get("phase") == "thinking":
        content_state["options"] = _capture_pane_options_for_label(session_label)
    else:
        content_state["options"] = []


def _capture_pane_options_for_label(session_label: str):
    """Capture current pane for a mobile-<N> label, run detector, log the
    transition. Returns just the options list (legacy callers unchanged)."""
    target = _tmux_target_for_label(session_label)
    if ":" not in target:
        _log_la_transition(session_label, [], "non-mobile-label", f"target={target!r}")
        return []
    import subprocess as _sp

    try:
        r = _sp.run(
            ["/opt/homebrew/bin/tmux", "capture-pane", "-p", "-t", target, "-S", "-40"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if r.returncode != 0:
            _log_la_transition(
                session_label, [], "capture-failed", f"rc={r.returncode}"
            )
            return []
        opts, reason, evidence = _detect_with_reason(r.stdout)
        _log_la_transition(session_label, opts, reason, evidence)
        # URL watcher hook — detect new URLs in pane output and push
        # a tappable notification. Disabled via CLAUDE_URL_PUSH=0 by
        # default so this ships dormant until verified on device.
        try:
            from conv.url_watcher import process_pane as _url_process

            _url_process(session_label, r.stdout)
        except Exception as url_exc:
            _log.debug("url_watcher hook skipped: %s", url_exc)
        # Prompt-state hook — emit structured opened/closed events so
        # the iOS approval bar can track live vs dismissed prompts
        # without parsing pane text. Events broadcast via WebSocket
        # in the {"type": "prompt_state", "event": "prompt_opened" | "prompt_closed"} shape.
        try:
            from conv.prompt_state import observe as _ps_observe

            for ev in _ps_observe(session_label, r.stdout):
                _broadcast_ws(
                    {
                        "type": "prompt_state",
                        "event": ev.type,
                        "session_label": ev.session_label,
                        "prompt_id": ev.prompt_id,
                        "options": ev.options,
                        "timestamp": ev.timestamp,
                    }
                )
        except Exception as ps_exc:
            _log.debug("prompt_state hook skipped: %s", ps_exc)
        return opts
    except Exception as exc:
        _log.warning("capture-pane for %s failed: %s", session_label, exc)
        _log_la_transition(session_label, [], "capture-exception", str(exc)[:120])
        return []
