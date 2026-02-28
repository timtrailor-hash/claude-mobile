#!/usr/bin/env python3
"""UPS Watchdog — monitors Mac power state and protects 3D printers.

Polls `pmset -g batt` every 30 seconds. When AC power is lost (UPS kicks in),
pauses any active Sovol print via Moonraker API. If battery drops below 20%,
sends an emergency stop to prevent damage from an uncontrolled power loss.

Does NOT auto-resume on power restore — user must inspect and resume manually.

Designed to run as a launchd daemon (see com.timtrailor.ups-watchdog.plist).
"""

import json
import logging
import os
import re
import subprocess
import sys
import time
import urllib.request
import urllib.error
from logging.handlers import RotatingFileHandler

# ---------------------------------------------------------------------------
# Printer config — import from shared config, fall back to defaults
# ---------------------------------------------------------------------------
try:
    sys.path.insert(0, os.path.expanduser("~/Documents/Claude code"))
    from printer_config import SOVOL_IP, MOONRAKER_PORT
except ImportError:
    SOVOL_IP = "192.168.87.52"
    MOONRAKER_PORT = 7125

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
POLL_INTERVAL = 30          # seconds between power checks
EMERGENCY_BATTERY_PCT = 20  # below this → emergency stop
MOONRAKER_BASE = f"http://{SOVOL_IP}:{MOONRAKER_PORT}"
LOG_PATH = "/tmp/ups_watchdog.log"
LOG_MAX_BYTES = 2 * 1024 * 1024  # 2 MB
LOG_BACKUP_COUNT = 3

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logger = logging.getLogger("ups_watchdog")
logger.setLevel(logging.INFO)

handler = RotatingFileHandler(
    LOG_PATH, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT
)
handler.setFormatter(
    logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
)
logger.addHandler(handler)

# Also log to stderr so launchd captures it
console = logging.StreamHandler()
console.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
logger.addHandler(console)


# ---------------------------------------------------------------------------
# Power state parsing
# ---------------------------------------------------------------------------
def get_power_state():
    """Run `pmset -g batt` and parse the output.

    Returns:
        dict with keys:
            source (str): "AC Power" or "Battery Power" or "Unknown"
            percent (int or None): battery percentage
            remaining (str or None): time remaining string e.g. "1:23"
            charging (bool): True if charging/charged
    """
    try:
        result = subprocess.run(
            ["pmset", "-g", "batt"],
            capture_output=True, text=True, timeout=10
        )
        output = result.stdout
    except (subprocess.SubprocessError, OSError) as exc:
        logger.error("Failed to run pmset: %s", exc)
        return {"source": "Unknown", "percent": None, "remaining": None, "charging": False}

    # First line: "Now drawing from 'AC Power'" or "Now drawing from 'Battery Power'"
    source = "Unknown"
    source_match = re.search(r"Now drawing from '(.+?)'", output)
    if source_match:
        source = source_match.group(1)

    # Battery line: " -InternalBattery-0 (id=...)  100%; charged; 0:00 remaining ..."
    percent = None
    remaining = None
    charging = False

    pct_match = re.search(r"(\d+)%", output)
    if pct_match:
        percent = int(pct_match.group(1))

    time_match = re.search(r"(\d+:\d+) remaining", output)
    if time_match:
        remaining = time_match.group(1)

    if "charging" in output.lower() or "charged" in output.lower():
        charging = True

    return {
        "source": source,
        "percent": percent,
        "remaining": remaining,
        "charging": charging,
    }


# ---------------------------------------------------------------------------
# Moonraker API helpers
# ---------------------------------------------------------------------------
def moonraker_request(endpoint, method="POST", timeout=10):
    """Send a request to the Moonraker API.

    Args:
        endpoint: API path, e.g. "/printer/print/pause"
        method: HTTP method
        timeout: request timeout in seconds

    Returns:
        (success: bool, response_text: str)
    """
    url = f"{MOONRAKER_BASE}{endpoint}"
    try:
        req = urllib.request.Request(url, method=method, data=b"")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return True, body
    except urllib.error.URLError as exc:
        return False, str(exc)
    except OSError as exc:
        return False, str(exc)


def get_sovol_print_state():
    """Query Moonraker for the current print state.

    Returns:
        str: one of "standby", "printing", "paused", "complete", "cancelled", "error",
             or "unreachable" if the printer can't be contacted.
    """
    url = f"{MOONRAKER_BASE}/printer/objects/query?print_stats=state"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            return data["result"]["status"]["print_stats"]["state"]
    except Exception:
        return "unreachable"


def notify_app(message, level="warning"):
    """Send alert to the iOS app via conversation server."""
    try:
        data = json.dumps({"message": message, "level": level}).encode()
        req = urllib.request.Request(
            "http://localhost:8081/printer-alert",
            data=data, method="POST",
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception as exc:
        logger.warning("Failed to notify iOS app: %s", exc)


def protect_sovol():
    """Pause print, drop bed to 45°C, turn off hotend."""
    state = get_sovol_print_state()
    if state == "printing":
        logger.warning("Sovol is printing — sending PAUSE command")
        ok, msg = moonraker_request("/printer/print/pause")
        if ok:
            logger.info("Sovol pause command sent successfully")
        else:
            logger.error("Failed to pause Sovol: %s", msg)
    elif state == "unreachable":
        logger.warning("Sovol is unreachable — cannot pause")
    else:
        logger.info("Sovol print state is '%s' — no pause needed", state)

    # Drop bed to 45°C (maintain adhesion but save power)
    moonraker_request(
        "/printer/gcode/script?script=SET_HEATER_TEMPERATURE%20HEATER%3Dheater_bed%20TARGET%3D45"
    )
    # Turn off hotend
    moonraker_request(
        "/printer/gcode/script?script=SET_HEATER_TEMPERATURE%20HEATER%3Dextruder%20TARGET%3D0"
    )
    logger.info("Sovol bed→45°C, hotend→off")


def emergency_stop_sovol():
    """Send emergency stop to the Sovol via Moonraker."""
    logger.critical("EMERGENCY STOP — battery below %d%%", EMERGENCY_BATTERY_PCT)
    ok, msg = moonraker_request("/printer/emergency_stop")
    if ok:
        logger.info("Sovol emergency stop sent successfully")
    else:
        logger.error("Failed to send emergency stop to Sovol: %s", msg)


# ---------------------------------------------------------------------------
# Main watchdog loop
# ---------------------------------------------------------------------------
def run():
    """Main polling loop."""
    logger.info("UPS watchdog starting — polling every %ds", POLL_INTERVAL)
    logger.info("Sovol endpoint: %s", MOONRAKER_BASE)
    logger.info("Emergency stop threshold: %d%%", EMERGENCY_BATTERY_PCT)

    previous_source = None
    emergency_sent = False

    while True:
        state = get_power_state()
        source = state["source"]
        percent = state["percent"]
        remaining = state["remaining"]

        # --- Transition detection ---
        if previous_source is not None and source != previous_source:
            if source == "Battery Power":
                logger.warning(
                    "POWER LOST — now on battery (%s%%, %s remaining)",
                    percent, remaining or "unknown"
                )
                # Attempt to pause and protect printers
                protect_sovol()
                notify_app(
                    f"POWER FAILURE — printers paused, UPS at {percent}%"
                    f" ({remaining or 'unknown'} remaining)"
                )
                logger.warning(
                    "Bambu A1: cannot be paused remotely — "
                    "if printing, it will continue until power fails or user intervenes"
                )
                emergency_sent = False  # reset in case of repeated outages

            elif source == "AC Power":
                logger.info(
                    "POWER RESTORED — back on AC (%s%%)", percent
                )
                notify_app(
                    "AC power restored — printers were paused, check and resume manually",
                    level="info"
                )
                logger.info(
                    "Auto-resume is disabled — inspect printers and resume manually"
                )
                emergency_sent = False

        # --- Emergency stop check (while on battery) ---
        if source == "Battery Power" and percent is not None:
            if percent < EMERGENCY_BATTERY_PCT and not emergency_sent:
                emergency_stop_sovol()
                emergency_sent = True

        # --- Periodic status log (every poll, at DEBUG level) ---
        logger.debug(
            "Power: %s | Battery: %s%% | Remaining: %s | Charging: %s",
            source, percent, remaining or "n/a", state["charging"]
        )

        previous_source = source

        try:
            time.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            logger.info("UPS watchdog stopped by user")
            break


if __name__ == "__main__":
    try:
        run()
    except Exception:
        logger.exception("UPS watchdog crashed")
        raise
