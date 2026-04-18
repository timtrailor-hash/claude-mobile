"""APNs authentication — extracted from conversation_server.py 2026-04-18.

Phase 3 decomposition step 1: pure JWT generation, zero session state.
Previously at conversation_server.py:1852-1868 (lines 17).

Callers should import as:
    from conv.apns import apns_jwt
"""
import time

import jwt


def apns_jwt() -> str:
    """Build a short-lived APNs JWT using credentials.py settings.

    Shared by alert-push and Live Activity code paths. Raises on any
    config error — callers must not swallow.
    """
    # Late import so credentials is only needed when JWT is actually requested.
    from credentials import APNS_KEY_ID, APNS_KEY_PATH, APNS_TEAM_ID

    with open(APNS_KEY_PATH) as f:
        key = f.read()

    return jwt.encode(
        {"iss": APNS_TEAM_ID, "iat": int(time.time())},
        key,
        algorithm="ES256",
        headers={"kid": APNS_KEY_ID},
    )
