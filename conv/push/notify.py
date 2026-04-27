"""APNs push and Haiku-backed summariser.

_send_push_notification: send a single APNs push via curl HTTP/2 + JWT.
_summarise_for_push: feed the tail of Claude's response to Haiku 4.5 and
                     return a ~100-word summary suitable for a phone
                     lock-screen notification body.

CONV_APNS_DRY_RUN=1 short-circuits _send_push_notification before any
APNs network call. Required for staging safety (slice 1b).
"""

from __future__ import annotations

import json
import os
import subprocess

from conv.app import _log, is_dry_run
from conv.apns import apns_jwt
from conv.apns_tokens import _lock as _apns_device_tokens_lock
from conv.apns_tokens import save as _save_apns_tokens_inner
from conv.apns_tokens import store as _apns_device_tokens

# Endpoint URL — env-overridable so a staging instance pointed at the
# Apple sandbox doesn't need a code change.
_APNS_HOST = os.environ.get("CONV_APNS_HOST", "https://api.sandbox.push.apple.com")


def _save_apns_tokens() -> None:
    """Thin shim mirroring the monolith's helper."""
    _save_apns_tokens_inner(_apns_device_tokens)


def _send_push_notification(
    title: str,
    body: str,
    bundle_id: str = "com.timtrailor.terminal",
    window_index=None,
    content_available: bool = False,
    category=None,
    user_info=None,
) -> None:
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
    if is_dry_run("APNS"):
        _log.info(
            "[DRY RUN] _send_push_notification bundle=%s title=%s win=%s silent=%s",
            bundle_id,
            (title or "")[:40],
            window_index,
            content_available,
        )
        return

    with _apns_device_tokens_lock:
        token = _apns_device_tokens.get(bundle_id)
    if not token:
        _log.debug("No APNs token for %s — skipping push", bundle_id)
        return
    kind = "silent" if content_available else "alert"
    _log.info(
        "APNs: sending %s push to %s (token=%s... win=%s)",
        kind,
        bundle_id,
        token[:8],
        window_index,
    )
    try:
        jwt_token = apns_jwt()

        if content_available:
            aps = {"content-available": 1}
        else:
            aps = {
                "alert": {"title": title, "body": body},
                "sound": "default",
            }
            # Category drives iOS-side actionable buttons on the lock-screen
            # banner. The TerminalApp registers "PROPOSAL_ACTIONS" with
            # Accept/Reject/Discuss; the alert-responder pipeline sets it.
            if category:
                aps["category"] = str(category)
        payload_obj = {"aps": aps}
        if window_index is not None:
            payload_obj["window"] = int(window_index)
        # user_info is merged at the top level of the APNs payload so the
        # iOS-side notification handlers can read keys like "alert_id" to
        # dispatch actionable-button taps back to the server.
        if user_info and isinstance(user_info, dict):
            for k, v in user_info.items():
                if k in ("aps", "window"):
                    continue
                payload_obj[str(k)] = v
        payload = json.dumps(payload_obj)

        url = f"{_APNS_HOST}/3/device/{token}"

        push_type = "background" if content_available else "alert"
        priority = "5" if content_available else "10"
        result = subprocess.run(
            [
                "curl",
                "-s",
                "--http2",
                "-H",
                f"authorization: bearer {jwt_token}",
                "-H",
                f"apns-topic: {bundle_id}",
                "-H",
                f"apns-push-type: {push_type}",
                "-H",
                f"apns-priority: {priority}",
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

        lines = result.stdout.strip().split("\n")
        status_code = lines[-1] if lines else "?"
        response_body = "\n".join(lines[:-1])

        if status_code == "200":
            _log.info("APNs push sent OK to %s: %s", bundle_id, title)
        elif status_code == "410":
            # Token no longer valid — remove it.
            _log.info("APNs token for %s no longer valid, removing", bundle_id)
            with _apns_device_tokens_lock:
                _apns_device_tokens.pop(bundle_id, None)
                _save_apns_tokens()
        else:
            _log.warning(
                "APNs push to %s failed (HTTP %s): %s",
                bundle_id,
                status_code,
                response_body,
            )
    except Exception as exc:
        _log.error("APNs push error (%s): %s", bundle_id, exc, exc_info=True)


def _summarise_for_push(
    raw_text: str,
    max_lines: int = 100,
    target_words: int = 100,
    timeout: float = 20.0,
):
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
