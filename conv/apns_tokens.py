"""APNs device-token persistence — extracted from conversation_server.py 2026-04-18.

Phase 3 step 2: multi-app device token storage. Previously at
conversation_server.py:1743-1770.

The `store` dict is the canonical in-memory token map. Callers mutate it
directly (same shape as before). Call save() after mutation.
"""
import json
import logging
import threading
from pathlib import Path

log = logging.getLogger("conversation_server")

TOKENS_FILE = Path("/Users/timtrailor/code/apns_state/device_tokens.json")
LEGACY_TOKEN_FILE = Path("/Users/timtrailor/code/apns_state/device_token.txt")
LEGACY_BUNDLE = "com.timtrailor.terminal"

_lock = threading.Lock()


def load() -> dict:
    """Load tokens dict from disk. Returns empty dict if absent or corrupt."""
    try:
        with TOKENS_FILE.open() as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save(tokens: dict) -> None:
    """Persist tokens dict to disk. Non-fatal on error (logs warning)."""
    try:
        TOKENS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with TOKENS_FILE.open("w") as f:
            json.dump(tokens, f)
    except OSError as e:
        log.warning("Failed to save APNs tokens: %s", e)


def migrate_legacy(tokens: dict) -> bool:
    """If ~/apns_state/device_token.txt exists and the modern file doesn't
    hold a token for the legacy bundle, migrate. Returns True if migrated."""
    if LEGACY_BUNDLE in tokens:
        return False
    try:
        with LEGACY_TOKEN_FILE.open() as f:
            legacy = f.read().strip()
    except FileNotFoundError:
        return False
    if not legacy:
        return False
    tokens[LEGACY_BUNDLE] = legacy
    save(tokens)
    return True


# The canonical in-memory store. Callers mutate directly; call save(store)
# afterwards. This preserves the existing module-global pattern the
# monolith relied on.
store: dict = load()
migrate_legacy(store)
