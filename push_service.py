"""
APNs Push Notification Service — extracted from conversation_server.py

Handles:
- Multi-app device token persistence (~/code/apns_state/device_tokens.json)
- JWT-based APNs push via HTTP/2 + curl
- Smart notification routing (push only for now)

This module is independently testable and importable.
"""

import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path

log = logging.getLogger("push_service")

# ── Config ──
APNS_STATE_DIR = Path.home() / "code" / "apns_state"
APNS_TOKENS_FILE = APNS_STATE_DIR / "device_tokens.json"

# These are read from environment or credentials at import time
APNS_KEY_ID = os.environ.get("APNS_KEY_ID", "")
APNS_TEAM_ID = os.environ.get("APNS_TEAM_ID", "")
APNS_KEY_PATH = os.environ.get("APNS_KEY_PATH", "")

# ── Token persistence ──
_apns_device_tokens: dict = {}
_apns_device_tokens_lock = threading.Lock()


def load_tokens() -> dict:
    """Load device tokens from persistent storage."""
    global _apns_device_tokens
    APNS_STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        if APNS_TOKENS_FILE.exists():
            with open(APNS_TOKENS_FILE) as f:
                _apns_device_tokens = json.load(f)
    except Exception as e:
        log.warning("Failed to load APNs tokens: %s", e)
        _apns_device_tokens = {}
    return _apns_device_tokens


def save_tokens():
    """Save device tokens to persistent storage."""
    APNS_STATE_DIR.mkdir(parents=True, exist_ok=True)
    with _apns_device_tokens_lock:
        try:
            with open(APNS_TOKENS_FILE, "w") as f:
                json.dump(_apns_device_tokens, f)
        except Exception as e:
            log.warning("Failed to save APNs tokens: %s", e)


def register_device(bundle_id: str, device_token: str):
    """Register a device token for a specific app bundle."""
    with _apns_device_tokens_lock:
        _apns_device_tokens[bundle_id] = device_token
    save_tokens()
    log.info("Registered device token for %s", bundle_id)


def send_push(
    title: str,
    body: str,
    bundle_id: str = "com.timtrailor.terminal",
    user_info: dict | None = None,
    category: str | None = None,
) -> bool:
    """Send a push notification via APNs HTTP/2 + curl.

    Returns True if the push was sent successfully, False otherwise.
    Uses JWT authentication with the configured team/key IDs.

    user_info: optional dict merged into the top-level payload (outside
    the aps key). The iOS notification delegate reads keys from this
    dict to implement tap-opens-URL behaviour.

    category: optional APNs category identifier. Set when the payload
    should trigger a specific iOS handler (e.g. "CLAUDE_URL" for the
    tap-opens-Safari flow).
    """
    token = _apns_device_tokens.get(bundle_id)
    if not token:
        log.warning("No device token registered for %s", bundle_id)
        return False

    if not all([APNS_KEY_ID, APNS_TEAM_ID, APNS_KEY_PATH]):
        log.warning("APNs credentials not configured (KEY_ID=%s, TEAM_ID=%s, KEY_PATH=%s)",
                     bool(APNS_KEY_ID), bool(APNS_TEAM_ID), bool(APNS_KEY_PATH))
        return False

    try:
        # Generate JWT
        jwt_result = subprocess.run(
            ["python3", "-c", f"""
import jwt, time
key = open("{APNS_KEY_PATH}").read()
payload = {{"iss": "{APNS_TEAM_ID}", "iat": int(time.time())}}
token = jwt.encode(payload, key, algorithm="ES256", headers={{"kid": "{APNS_KEY_ID}"}})
print(token)
"""],
            capture_output=True, text=True, timeout=10,
        )
        if jwt_result.returncode != 0:
            log.error("JWT generation failed: %s", jwt_result.stderr)
            return False

        jwt_token = jwt_result.stdout.strip()

        aps: dict = {
            "alert": {"title": title, "body": body[:200]},
            "sound": "default",
        }
        if category:
            aps["category"] = category

        payload_dict: dict = {"aps": aps}
        if user_info:
            for k, v in user_info.items():
                if k == "aps":
                    continue  # never allow override of reserved key
                payload_dict[k] = v

        payload = json.dumps(payload_dict)

        curl_result = subprocess.run([
            "curl", "-s", "--http2",
            "-H", f"authorization: bearer {jwt_token}",
            "-H", f"apns-topic: {bundle_id}",
            "-H", "apns-push-type: alert",
            "-d", payload,
            f"https://api.push.apple.com/3/device/{token}",
        ], capture_output=True, text=True, timeout=15)

        if curl_result.returncode == 0 and not curl_result.stdout.strip():
            log.info("Push sent to %s: %s", bundle_id, title)
            return True
        else:
            log.warning("Push may have failed for %s: %s", bundle_id, curl_result.stdout)
            return False

    except Exception as e:
        log.error("Push notification error: %s", e)
        return False


def send_push_with_userinfo(
    title: str,
    body: str,
    bundle_id: str,
    user_info: dict,
    category: str = "CLAUDE_URL",
) -> bool:
    """Convenience wrapper — send a push carrying a tappable userInfo
    payload. The iOS notification delegate reads `url` from user_info
    and opens it in Safari on tap."""
    return send_push(title, body, bundle_id, user_info=user_info, category=category)


def notify_smart(title: str, message: str):
    """Send push notification to all registered devices."""
    for bundle_id in list(_apns_device_tokens.keys()):
        send_push(title, message, bundle_id)


# Initialize on import
load_tokens()
