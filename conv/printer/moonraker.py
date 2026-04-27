"""Moonraker (Klipper) gcode sender.

Honors CONV_PRINTER_DRY_RUN — short-circuits before any HTTP call to the
printer when the flag is set. Required for staging safety (slice 1b).
"""

from __future__ import annotations

import json
import urllib.request

from conv.app import _log, is_dry_run


def _send_moonraker_gcode(printer, script: str):
    """Send a gcode script to Moonraker. Returns (ok, result_or_error)."""
    if is_dry_run("PRINTER"):
        _log.info(
            "[DRY RUN] _send_moonraker_gcode printer=%s script=%s",
            printer.id,
            script[:80],
        )
        return True, {
            "dry_run": True,
            "printer": printer.id,
            "script_preview": script[:80],
        }
    try:
        payload = json.dumps({"script": script}).encode()
        req = urllib.request.Request(
            f"{printer.moonraker_url}/printer/gcode/script",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(
            req, timeout=10
        ) as r:  # nosemgrep: dynamic-urllib-use-detected
            result = json.loads(r.read())
        return True, result
    except Exception as e:
        _log.error(
            "Moonraker gcode failed for %s: %s (script=%s)",
            printer.id,
            e,
            script[:80],
        )
        return False, str(e)
