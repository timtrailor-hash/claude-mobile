"""
Live Activity stale-reaper.

Problem (2026-04-18): when a Claude tab is killed mid-turn — or the Stop
hook fails to reach /internal/claude-hook for any reason — the Live
Activity on Tim's phone never ends. The server keeps pushing
`event=update` every minute, the Dynamic Island keeps ticking "still
working", and only a manual POST /debug/la-end-all clears it.

Solution: background thread that wakes every 60s and ends any LA whose
turn is no longer plausibly active. Two cases, matching the two failure
modes we've actually seen:

1. **Stale turn** — `_liveactivity_turn_state[label].started_at` is more
   than `_STALE_THRESHOLD_SEC` old. Legitimate Claude turns rarely
   exceed 30 min; if we're still ticking beyond that, the Stop hook has
   almost certainly been missed.

2. **Orphan LA** — `_liveactivity_tokens` has an entry whose
   `sessionLabel` has NO `_liveactivity_turn_state` record at all, AND
   the token's `last_update` is older than `_STALE_THRESHOLD_SEC`. This
   catches LAs that outlived their turn state (e.g. the turn-state dict
   was cleared by a reset but the tokens persisted).

The reaper is deliberately conservative (30 min) — it only targets
indisputable orphans, not the long-tail of legitimate long turns. Tim
can lower the threshold if false positives remain low.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Mapping, MutableMapping

_REAPER_INTERVAL_SEC = 60
_STALE_THRESHOLD_SEC = 30 * 60  # 30 min


def start_la_reaper(
    *,
    tokens_lock: threading.Lock,
    tokens_dict: MutableMapping[str, Mapping[str, Any]],
    turn_state_lock: threading.Lock,
    turn_state: MutableMapping[str, Mapping[str, Any]],
    end_fn: Callable[[str, str], int],
    logger,
) -> threading.Thread:
    """Start the reaper loop as a daemon thread. Returns the thread handle
    so the caller can name it in diagnostics.

    Dependencies are passed in rather than imported from conversation_server
    so this module doesn't introduce a circular import at load time.

    `end_fn` must be `_end_live_activity_for_label(label, reason)` from
    conversation_server.py.
    """

    def _loop() -> None:
        logger.info(
            "la-reaper: loop started (interval=%ds threshold=%ds)",
            _REAPER_INTERVAL_SEC, _STALE_THRESHOLD_SEC,
        )
        while True:
            try:
                time.sleep(_REAPER_INTERVAL_SEC)
                _run_once(
                    tokens_lock=tokens_lock,
                    tokens_dict=tokens_dict,
                    turn_state_lock=turn_state_lock,
                    turn_state=turn_state,
                    end_fn=end_fn,
                    logger=logger,
                )
            except Exception:
                # Daemon thread — never let an exception kill the loop.
                logger.exception("la-reaper: run_once crashed")

    t = threading.Thread(target=_loop, name="la-reaper", daemon=True)
    t.start()
    return t


def _run_once(
    *,
    tokens_lock: threading.Lock,
    tokens_dict: MutableMapping[str, Mapping[str, Any]],
    turn_state_lock: threading.Lock,
    turn_state: MutableMapping[str, Mapping[str, Any]],
    end_fn: Callable[[str, str], int],
    logger,
) -> None:
    """Single sweep. Collect labels to reap, then call end_fn outside any
    lock (end_fn acquires both locks internally)."""
    now = time.time()
    labels_to_end: set[str] = set()
    reasons: dict[str, str] = {}

    # Case 1: turn state started_at is stale.
    with turn_state_lock:
        for label, state in list(turn_state.items()):
            started = state.get("started_at", 0) or 0
            if not started:
                continue
            age = now - started
            if age > _STALE_THRESHOLD_SEC:
                labels_to_end.add(label)
                reasons[label] = f"turn-stale-{int(age)}s"

    # Case 2: LA registered but NO turn_state entry, and last_update is stale.
    with tokens_lock:
        for rec in tokens_dict.values():
            attrs = rec.get("attributes") or {}
            label = attrs.get("sessionLabel")
            if not label or label in labels_to_end:
                continue
            # Lock order: tokens → turn_state (matches _end_live_activity_for_label)
            with turn_state_lock:
                has_turn = label in turn_state
            if has_turn:
                continue
            last_update = rec.get("last_update", 0) or 0
            if last_update and (now - last_update) > _STALE_THRESHOLD_SEC:
                labels_to_end.add(label)
                reasons[label] = f"orphan-{int(now - last_update)}s"

    if not labels_to_end:
        return

    for label in sorted(labels_to_end):
        try:
            ended = end_fn(label, reasons.get(label, "la-reaper"))
            if ended:
                logger.warning(
                    "la-reaper: ended %d LA(s) for label=%s reason=%s",
                    ended, label, reasons.get(label, "la-reaper"),
                )
        except Exception:
            logger.exception("la-reaper: end_fn failed for label=%s", label)
