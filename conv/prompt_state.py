"""Prompt state machine — the structured-signals foundation for the
iOS approval bar rebuild.

Today the iOS Terminal app infers "is there a live prompt?" by parsing
pane text. That's brittle and causes stale buttons (see Tim's
feedback, 2026-04-22). Plan 1 replaces inference with explicit
opened / answered / cancelled events driven from pane-capture ticks.

This module is side-effect-free apart from its per-session state
dict. It does NOT push WebSocket events or notifications itself — it
exposes a pure function `observe(session_label, pane_text, now)` that
returns state transitions. The caller (conversation_server) decides
whether to emit those transitions over WebSocket.

Lifecycle:
    NO_PROMPT  -- parser returns options --> PROMPT_OPEN
    PROMPT_OPEN -- parser returns empty   --> NO_PROMPT (emit CLOSED)
    PROMPT_OPEN -- parser returns NEW set --> PROMPT_OPEN (emit OPENED again)

The iOS side treats OPENED as "show bar with these choices" and
CLOSED as "hide bar now". Simple. No stale buttons possible —
the bar is tied directly to the most recent event, not to pane text.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from conv.pane_parser import detect_pane_prompt_options

# ── Event types (string constants, easy to serialise) ──
EVENT_OPENED = "prompt_opened"
EVENT_CLOSED = "prompt_closed"


@dataclass
class _SessionState:
    last_options: list[dict] = field(default_factory=list)
    opened_at: Optional[float] = None
    prompt_id: int = 0


@dataclass
class PromptEvent:
    """Structured event the caller can serialise over WebSocket."""
    type: str
    session_label: str
    prompt_id: int
    options: list[dict]  # [{"number": 1, "label": "Yes"}, ...]
    timestamp: float

    def to_dict(self) -> dict:
        return {
            "type": self.type,
            "session_label": self.session_label,
            "prompt_id": self.prompt_id,
            "options": self.options,
            "timestamp": self.timestamp,
        }


_state_lock = threading.Lock()
_states: dict[str, _SessionState] = {}


def _get_state(session_label: str) -> _SessionState:
    with _state_lock:
        st = _states.get(session_label)
        if st is None:
            st = _SessionState()
            _states[session_label] = st
        return st


def _options_equal(a: list[dict], b: list[dict]) -> bool:
    if len(a) != len(b):
        return False
    for x, y in zip(a, b):
        if x.get("number") != y.get("number") or x.get("label") != y.get("label"):
            return False
    return True


def observe(session_label: str, pane_text: str, now: Optional[float] = None) -> list[PromptEvent]:
    """Feed a new pane capture and return any emitted events.

    Returns a list of 0, 1, or 2 PromptEvents:
    - empty if state is unchanged
    - one CLOSED if previously-open prompt is now gone
    - one OPENED if a fresh prompt appeared
    - CLOSED + OPENED if the prompt options changed (prompt replaced)
    """
    if now is None:
        now = time.time()

    options = detect_pane_prompt_options(pane_text or "")
    st = _get_state(session_label)
    events: list[PromptEvent] = []

    had_prompt = bool(st.last_options)
    have_prompt = bool(options)

    if not had_prompt and not have_prompt:
        return events

    if had_prompt and not have_prompt:
        # closed
        ev = PromptEvent(
            type=EVENT_CLOSED,
            session_label=session_label,
            prompt_id=st.prompt_id,
            options=[],
            timestamp=now,
        )
        events.append(ev)
        st.last_options = []
        st.opened_at = None
        return events

    if not had_prompt and have_prompt:
        st.prompt_id += 1
        st.last_options = options
        st.opened_at = now
        events.append(PromptEvent(
            type=EVENT_OPENED,
            session_label=session_label,
            prompt_id=st.prompt_id,
            options=options,
            timestamp=now,
        ))
        return events

    # Both present — check if replaced.
    if not _options_equal(st.last_options, options):
        events.append(PromptEvent(
            type=EVENT_CLOSED,
            session_label=session_label,
            prompt_id=st.prompt_id,
            options=[],
            timestamp=now,
        ))
        st.prompt_id += 1
        st.last_options = options
        st.opened_at = now
        events.append(PromptEvent(
            type=EVENT_OPENED,
            session_label=session_label,
            prompt_id=st.prompt_id,
            options=options,
            timestamp=now,
        ))
    return events


def current_prompt(session_label: str) -> Optional[dict]:
    """Return the currently-open prompt's event payload, or None.

    Used by the WebSocket connect path to replay the current state to
    a reconnecting client so it can resurrect the bar without waiting
    for the next tick.
    """
    st = _get_state(session_label)
    if not st.last_options:
        return None
    return {
        "type": EVENT_OPENED,
        "session_label": session_label,
        "prompt_id": st.prompt_id,
        "options": st.last_options,
        "timestamp": st.opened_at or time.time(),
    }


def reset(session_label: Optional[str] = None) -> None:
    """Clear state — useful for tests and for session-end cleanup."""
    with _state_lock:
        if session_label is None:
            _states.clear()
        else:
            _states.pop(session_label, None)


# ── Window-keyed JSONL-based prompt tracker ──────────────────────────────────
#
# Separate lightweight tracker for the iOS Terminal app's HTTP-polling
# path (/tmux-windows). Unlike observe()/current_prompt() above (which
# consume pane text), this one is driven by the JSONL-based pendingApproval
# signal in conversation_server._detect_pending_approval. Key = tmux
# window index (int).

import threading as _threading
import time as _time

_window_prompt_ids: dict[int, dict] = {}
_window_prompt_lock = _threading.Lock()


def bump_window_prompt_id(window_idx: int, pending: bool) -> tuple[int, bool]:
    """Bump the id on a false→true pendingApproval transition.
    Returns (current_id, transitioned). Caller decides how to use the id
    (e.g. attach to the /tmux-windows response for the iOS approval bar)."""
    with _window_prompt_lock:
        entry = _window_prompt_ids.get(window_idx)
        if entry is None:
            entry = {"id": 0, "opened_at": 0.0, "pending": False}
            _window_prompt_ids[window_idx] = entry
        transitioned = pending and not entry["pending"]
        if transitioned:
            entry["id"] += 1
            entry["opened_at"] = _time.time()
        entry["pending"] = pending
        return entry["id"], transitioned


def current_window_prompt_id(window_idx: int) -> int:
    """The id of the currently-open prompt for this window, or 0 if
    no prompt is active. Used as the stale-guard key on /tmux-send-key."""
    with _window_prompt_lock:
        entry = _window_prompt_ids.get(window_idx)
        if not entry or not entry["pending"]:
            return 0
        return int(entry["id"])


def replay_active_prompts(send_fn) -> int:
    """For each session with a live pane-text prompt, call send_fn with
    a ws-ready dict. Returns the number of replays sent. Used on
    WebSocket connect so a reconnecting client can resurrect its bar
    without waiting for the next pane-capture tick."""
    sent = 0
    with _state_lock:
        labels = [lbl for lbl, st in _states.items() if st.last_options]
    for session_label in labels:
        active = current_prompt(session_label)
        if active is None:
            continue
        try:
            send_fn({
                "type": "prompt_state",
                "event": active["type"],
                "session_label": active["session_label"],
                "prompt_id": active["prompt_id"],
                "options": active["options"],
                "timestamp": active["timestamp"],
            })
            sent += 1
        except Exception:
            pass
    return sent


def validate_tap(window_idx: int, key: str, client_prompt_id) -> tuple[bool, dict]:
    """Stale-guard for /tmux-send-key. Caller passes the raw request
    fields; returns (ok, response_dict). When ok is False the response
    dict carries the error payload ready for jsonify+409 return.
    Single-digit keys validate against current_window_prompt_id; other
    keystrokes pass through unchecked. Callers without a promptId
    opt out of the guard entirely (legacy behaviour preserved)."""
    if client_prompt_id is None:
        return True, {}
    if not (len(key) == 1 and key.isdigit()):
        return True, {}
    current_id = current_window_prompt_id(window_idx)
    if current_id == 0:
        return False, {
            "ok": False,
            "error": "no pending prompt for this window",
            "reason": "stale",
        }
    try:
        client_id = int(client_prompt_id)
    except (TypeError, ValueError):
        return False, {"ok": False, "error": "promptId must be int"}
    if client_id != current_id:
        return False, {
            "ok": False,
            "error": f"stale prompt: client={client_id} current={current_id}",
            "reason": "stale",
            "currentPromptId": current_id,
        }
    return True, {}
