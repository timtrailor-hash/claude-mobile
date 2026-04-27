"""Printer-safety constants and gcode-validation helper.

These constants and the validator are referenced from MULTIPLE places in
the monolith (printer route handlers, the printer state monitor thread,
G-code submission paths). Extracting them here gives every slice that
later moves a printer route a single canonical import target.

Hard-coded incidents this protects against:
- 2026-03-05: SAVE_CONFIG sent during a print restarted Klipper and
  destroyed the print. SAVE_CONFIG is now in _DANGEROUS_WHILE_PRINTING.
"""

from __future__ import annotations


# Commands that are ALWAYS blocked during printing/paused.
_DANGEROUS_WHILE_PRINTING: set[str] = {
    "G28",
    "QUAD_GANTRY_LEVEL",
    "BED_MESH_CALIBRATE",
    "PROBE",
    "FIRMWARE_RESTART",
    "RESTART",  # Restarts Klipper — kills print.
    "SAVE_CONFIG",  # Restarts Klipper after writing config — destroyed print 2026-03-05.
    "SET_KINEMATIC_POSITION",
}

# Commands that are always allowed regardless of state.
_ALWAYS_ALLOWED: set[str] = {"M112", "EMERGENCY_STOP"}


def _validate_gcode_safety(script: str, state: str) -> tuple[bool, str]:
    """Check if a gcode command is safe given the current printer state.

    Returns (safe, reason).
    """
    if not script or not script.strip():
        return False, "Empty command"

    cmd = script.strip().split()[0].upper()

    if cmd in _ALWAYS_ALLOWED:
        return True, ""

    if state in ("printing", "paused"):
        if cmd in _DANGEROUS_WHILE_PRINTING:
            return False, f"{cmd} is blocked while printer is {state}"
        # Block movement during printing (except speed/temp changes).
        if cmd in ("G1", "G0") and state == "printing":
            return False, "Movement commands blocked while printing"

    return True, ""
