"""
Printer control safety helpers — extracted from conversation_server.py

Handles:
- Print state queries via Moonraker API
- G-code safety validation against allowlist
- Command dispatch to Moonraker printers
"""

import json
import logging
import urllib.request

log = logging.getLogger("printer_helpers")

SAFE_COMMANDS = {
    "M117", "SET_GCODE_OFFSET", "M220", "M221",
    "SET_FAN_SPEED", "PAUSE", "RESUME", "CANCEL_PRINT_CONFIRMED",
}
ALWAYS_BLOCKED = {"FIRMWARE_RESTART", "RESTART", "SAVE_CONFIG"}
DANGEROUS_DURING_PRINT = {
    "G28", "PROBE", "QUAD_GANTRY_LEVEL", "BED_MESH_CALIBRATE",
}


def get_print_state(printer_ip, port=7125):
    """Query Moonraker for current print state."""
    try:
        url = f"http://{printer_ip}:{port}/printer/objects/query?print_stats"
        with urllib.request.urlopen(url, timeout=3) as resp:
            data = json.loads(resp.read())
            return data["result"]["status"]["print_stats"]["state"]
    except Exception as e:
        log.warning("Moonraker unreachable at %s: %s", printer_ip, e)
        return "unknown"


def validate_gcode_safety(gcode, state):
    """Validate G-code safety. Returns (safe: bool, reason: str)."""
    cmd = gcode.strip().split()[0].upper() if gcode.strip() else ""

    if cmd in ALWAYS_BLOCKED:
        return False, f"'{cmd}' is always blocked"

    if state in ("printing", "paused"):
        if cmd in SAFE_COMMANDS:
            return True, f"'{cmd}' allowed during '{state}'"
        if cmd in DANGEROUS_DURING_PRINT:
            return False, f"'{cmd}' dangerous during '{state}'"
        return False, f"'{cmd}' not in allowlist for '{state}'"

    if state == "unknown":
        if cmd in SAFE_COMMANDS:
            return True, f"'{cmd}' safe even with unknown state"
        return False, f"State unknown, '{cmd}' not in safe list"

    return True, f"State '{state}' — allowed"


def send_moonraker_gcode(printer_ip, gcode, port=7125):
    """Send G-code with safety validation. Returns dict with ok/message."""
    state = get_print_state(printer_ip, port)
    safe, reason = validate_gcode_safety(gcode, state)

    if not safe:
        return {"ok": False, "message": f"BLOCKED: {reason}", "state": state}

    try:
        url = f"http://{printer_ip}:{port}/printer/gcode/script"
        data = f"script={gcode}".encode()
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return {"ok": True, "message": f"Sent '{gcode}'", "state": state}
    except Exception as e:
        return {"ok": False, "message": f"Error: {e}", "state": state}
