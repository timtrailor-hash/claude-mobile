"""Pane-button transition logging — extracted from conversation_server.py
2026-04-18 to keep the monolith ratchet green while Tim's logging request
is fulfilled.

This module owns:
 - the last-seen LA options per session_label (state + lock)
 - the transition classifier and logger
 - the with-reason wrapper that conversation_server calls

It does NOT own the tmux capture subprocess or the target-label resolver —
those stay in the monolith as `_capture_pane_options_for_label_with_reason`
calling this module's `log_transition`.
"""
from __future__ import annotations

import json
import logging
import threading
import time

from conv.pane_parser import detect_pane_prompt_options_with_reason

log = logging.getLogger("conversation_server")

_state_lock = threading.Lock()
_last_options: dict = {}
_last_reason: dict = {}

DEBUG_LOG_PATH = "/tmp/terminal_button_debug.log"


def detect_with_reason(pane_text: str):
    """Thin passthrough so callers can import from one module."""
    return detect_pane_prompt_options_with_reason(pane_text)


def log_transition(session_label: str, prev: list, new: list, reason: str, evidence: str) -> None:
    """Classify transition, dedupe on reason+tab, write to device log + file."""
    prev_empty = not prev
    new_empty = not new
    if prev_empty and new_empty:
        if _last_reason.get(session_label) == reason:
            return
        kind = "NOOP"
    elif prev_empty and not new_empty:
        kind = "APPEAR"
    elif not prev_empty and new_empty:
        kind = "DISAPPEAR"
    elif prev != new:
        kind = "CHANGE"
    else:
        return
    _last_reason[session_label] = reason
    line = (
        f"[la-button] {kind} session={session_label} reason={reason} "
        f"prev={prev!r} new={new!r} evidence={{{evidence}}}"
    )
    log.info(line[:1800])
    try:
        with open(DEBUG_LOG_PATH, "a") as f:
            f.write(json.dumps({
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "source": "conversation_server._attach_prompt_options",
                "session": session_label,
                "kind": kind,
                "reason": reason,
                "prev": prev,
                "new": new,
                "evidence": evidence[:500],
            }) + "\n")
    except OSError:
        pass


def record_and_log(session_label: str, new_opts: list, reason: str, evidence: str) -> None:
    """Compare against last-seen, log the transition, update state."""
    with _state_lock:
        prev = _last_options.get(session_label, [])
        _last_options[session_label] = new_opts
        log_transition(session_label, prev, new_opts, reason, evidence)
