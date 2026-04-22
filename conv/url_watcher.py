"""URL watcher — detect URLs in Claude's pane output and push a
tappable notification to the phone.

Design (see memory/topics/feedback_plain_english.md → Plan 2):
  Tim's phone can't easily open URLs printed in the terminal because
  the line-wrap splits them. This module spots URLs in captured pane
  text and fires an APNs push with the URL in the payload. The iOS
  notification delegate opens it directly in Safari.

Isolation:
  This module is side-effect-free apart from (1) the in-memory dedupe
  cache and (2) the push_fn call. Disabled by default via the
  URL_PUSH_ENABLED feature flag; the pane-capture caller must
  explicitly opt in. See process_pane() for the entry point.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from typing import Callable, Iterable

log = logging.getLogger("url_watcher")

# Feature flag. Default off until end-to-end verified on device.
URL_PUSH_ENABLED = os.environ.get("CLAUDE_URL_PUSH", "0") == "1"

# URL regex — stop at whitespace, quotes, brackets, angle brackets, backticks.
_URL_RE = re.compile(r'https?://[^\s\)\]\}"\'<>`]+')

# Trailing punctuation that should not be part of the URL even though the
# regex would grab it (common when a sentence ends with the URL).
_TRAILING_TRIM = ".,;:!?)]}"

# Per-(session_label, url) dedupe window. 10 min matches Plan 2 spec.
_DEDUPE_TTL_SECONDS = 600
_RATE_LIMIT_WINDOW = 60
_RATE_LIMIT_MAX = 3

_dedupe_lock = threading.Lock()
_dedupe: dict[tuple[str, str], float] = {}
_rate_log: list[float] = []


def _now() -> float:
    return time.monotonic()


def _strip_code_fences(text: str) -> str:
    """Remove content inside ``` triple-backtick fences.

    We don't want to push URLs that Claude echoes back from logs or
    source files — only URLs Claude is actively presenting in prose.
    """
    out: list[str] = []
    in_fence = False
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence:
            out.append(line)
    return "\n".join(out)


def detect_urls(pane_text: str) -> list[str]:
    """Return URLs in pane_text, outside of fenced code blocks, with
    trailing sentence punctuation trimmed. Order preserved, duplicates
    within the same text removed."""
    if not pane_text:
        return []
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in _URL_RE.findall(_strip_code_fences(pane_text)):
        url = raw
        while url and url[-1] in _TRAILING_TRIM:
            url = url[:-1]
        if not url or url in seen:
            continue
        seen.add(url)
        cleaned.append(url)
    return cleaned


def label_for_url(url: str) -> str:
    """Short human-friendly title for the notification."""
    lc = url.lower()
    if "github.com/login/device" in lc or "github.com/device" in lc:
        return "GitHub login"
    if "accounts.google.com/o/oauth2" in lc or "google.com/device" in lc:
        return "Google sign-in"
    if "github.com" in lc and "/pull/" in lc:
        return "GitHub pull request"
    if "github.com" in lc and "/issues/" in lc:
        return "GitHub issue"
    if "github.com" in lc:
        return "GitHub link"
    if "docs.google.com" in lc:
        return "Google Docs"
    if "drive.google.com" in lc:
        return "Google Drive"
    if "console.anthropic.com" in lc:
        return "Anthropic Console"
    return "Claude link"


def _is_deduped(session_label: str, url: str, now: float) -> bool:
    """True if this (session_label, url) was pushed within the TTL."""
    key = (session_label, url)
    with _dedupe_lock:
        last = _dedupe.get(key)
        if last is not None and (now - last) < _DEDUPE_TTL_SECONDS:
            return True
        _dedupe[key] = now
        cutoff = now - _DEDUPE_TTL_SECONDS
        stale = [k for k, v in _dedupe.items() if v < cutoff]
        for k in stale:
            _dedupe.pop(k, None)
        return False


def _rate_limit_ok(now: float) -> bool:
    """Allow at most _RATE_LIMIT_MAX pushes per _RATE_LIMIT_WINDOW."""
    cutoff = now - _RATE_LIMIT_WINDOW
    with _dedupe_lock:
        recent = [t for t in _rate_log if t >= cutoff]
        _rate_log[:] = recent
        if len(recent) >= _RATE_LIMIT_MAX:
            return False
        _rate_log.append(now)
        return True


def process_pane(
    session_label: str,
    pane_text: str,
    push_fn: Callable[[str, str, dict | None], None] | None = None,
) -> list[str]:
    """Scan pane_text for new URLs and push each via push_fn.

    Returns the list of URLs actually pushed (after dedupe + rate limit).
    When URL_PUSH_ENABLED is false, detection still runs (so tests pass)
    but no pushes are sent and an empty list is returned.
    """
    if not pane_text:
        return []
    urls = detect_urls(pane_text)
    if not urls:
        return []
    if not URL_PUSH_ENABLED:
        log.debug("URL push disabled; detected=%d", len(urls))
        return []

    if push_fn is None:
        push_fn = _default_push_fn

    pushed: list[str] = []
    for url in urls:
        now = _now()
        if _is_deduped(session_label, url, now):
            continue
        if not _rate_limit_ok(now):
            log.info("URL push rate limit hit; dropping url=%s", url)
            continue
        title = label_for_url(url)
        try:
            push_fn(title, url, {"url": url, "session_label": session_label})
            pushed.append(url)
        except Exception as exc:
            log.warning("URL push failed url=%s err=%s", url, exc)
    return pushed


def _default_push_fn(title: str, body: str, user_info: dict) -> None:
    """Default push path — uses push_service.send_push_with_userinfo if
    available, else falls back to notify_smart."""
    try:
        from push_service import send_push_with_userinfo  # type: ignore
        for bundle_id in _registered_bundles():
            send_push_with_userinfo(title, body, bundle_id, user_info)
        return
    except Exception:
        pass
    try:
        from push_service import notify_smart
        notify_smart(title, body)
    except Exception as exc:
        log.warning("fallback notify_smart failed: %s", exc)


def _registered_bundles() -> Iterable[str]:
    try:
        from push_service import _apns_device_tokens  # type: ignore
        return list(_apns_device_tokens.keys())
    except Exception:
        return ["com.timtrailor.terminal"]
