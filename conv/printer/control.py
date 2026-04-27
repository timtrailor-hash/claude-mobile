"""Operational printer route handlers — second printer blueprint.

Routes (14):
  POST /printer-print-control     — pause / resume / cancel a print
  POST /printer-estop             — emergency stop (always allowed)
  POST /printer-temp-set          — set extruder/bed temperature
  POST /printer-fan-set           — set fan speed (Klipper)
  POST /printer-move              — move toolhead (relative)
  POST /printer-extrude           — extrude or retract filament
  POST /printer-home              — home axes
  POST /printer-gcode             — arbitrary gcode with safety validation
  POST /printer-calibrate         — QGL or bed_mesh
  POST /printer-led               — Bambu chamber LED
  GET  /printer-files             — list gcode files (Moonraker)
  GET  /printer-file-metadata     — file metadata (Moonraker)
  GET  /printer-file-thumbnail    — file thumbnail image (Moonraker)
  POST /printer-print-start       — start printing a file
  GET  /printer-history           — job history (Moonraker)

All routes import the shared helpers from conv.printer (state, safety, senders).
Dry-run safety is centralised in those helpers — when CONV_PRINTER_DRY_RUN=1
is set, _send_moonraker_gcode and _send_bambu_mqtt early-return without
contacting the printers, so every route here is safe in staging.

NOTE: routes that bypass the helpers and call urllib.request directly against
Moonraker (printer-print-control, printer-estop, printer-print-start,
printer-files, printer-file-metadata, printer-file-thumbnail, printer-history)
are NOT yet covered by the dry-run flag. See _direct_dry_run guard below.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request

from flask import Blueprint, Response, jsonify, request

from conv.app import _log, is_dry_run

from .moonraker import _send_moonraker_gcode
from .bambu import _send_bambu_mqtt
from .safety import _validate_gcode_safety
from .state import _get_print_state, _resolve_printer

bp = Blueprint("printer_control", __name__)


def _direct_dry_run(label: str, **detail) -> tuple | None:
    """Helper for routes that hit Moonraker directly (not via _send_moonraker_gcode).

    Returns a Flask response tuple when CONV_PRINTER_DRY_RUN=1 is set; otherwise
    None so the caller proceeds with the real call.
    """
    if is_dry_run("PRINTER"):
        _log.info("[DRY RUN] %s %s", label, detail)
        return jsonify({"ok": True, "dry_run": True, **detail}), 200
    return None


@bp.route("/printer-print-control", methods=["POST"])
def printer_print_control():
    """Pause, resume, or cancel a print.

    Body: {printer_id, action} where action is pause/resume/cancel.
    SV08: Moonraker /printer/print/{action}
    Bambu: MQTT command
    """
    body = request.get_json(force=True) or {}
    action = body.get("action", "").lower()
    printer_id = body.get("printer_id")

    if action not in ("pause", "resume", "cancel"):
        return jsonify(
            {"ok": False, "error": "action must be pause/resume/cancel"}
        ), 400

    # Try to resolve as any printer type
    printer, err = _resolve_printer(printer_id, "klipper")
    if err:
        printer, err = _resolve_printer(printer_id, "bambu")
    if err:
        return err

    if printer.is_klipper:
        state = _get_print_state(printer)
        if action == "pause" and state != "printing":
            return jsonify(
                {"ok": False, "error": f"Cannot pause: state is {state}"}
            ), 409
        if action == "resume" and state != "paused":
            return jsonify(
                {"ok": False, "error": f"Cannot resume: state is {state}"}
            ), 409
        if action == "cancel" and state not in ("printing", "paused"):
            return jsonify(
                {"ok": False, "error": f"Cannot cancel: state is {state}"}
            ), 409

        dr = _direct_dry_run("printer_print_control", action=action, printer=printer.id)
        if dr is not None:
            return dr

        try:
            url = f"{printer.moonraker_url}/printer/print/{action}"
            req = urllib.request.Request(url, data=b"", method="POST")
            with urllib.request.urlopen(
                req, timeout=10
            ) as r:  # nosemgrep: dynamic-urllib-use-detected
                r.read()
            _log.info(
                "printer-print-control: %s on %s (was %s)", action, printer.id, state
            )
            return jsonify({"ok": True, "action": action})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 502

    elif printer.is_bambu:
        bambu_actions = {"pause": "pause", "resume": "resume", "cancel": "stop"}
        payload = {"print": {"sequence_id": "0", "command": bambu_actions[action]}}
        ok, error = _send_bambu_mqtt(printer, payload)
        if ok:
            _log.info("printer-print-control: %s on %s (bambu)", action, printer.id)
            return jsonify({"ok": True, "action": action})
        return jsonify({"ok": False, "error": error or "MQTT failed"}), 502

    return jsonify({"ok": False, "error": "Unknown printer type"}), 400


@bp.route("/printer-estop", methods=["POST"])
def printer_estop():
    """Emergency stop — always allowed, no state check.

    Body: {printer_id}
    """
    body = request.get_json(force=True) or {}
    printer_id = body.get("printer_id")
    printer, err = _resolve_printer(printer_id, "klipper")
    if err:
        return err

    if not printer.is_klipper:
        return jsonify(
            {"ok": False, "error": "E-stop only supported on Klipper printers"}
        ), 400

    dr = _direct_dry_run("printer_estop", printer=printer.id)
    if dr is not None:
        return dr

    try:
        url = f"{printer.moonraker_url}/printer/emergency_stop"
        req = urllib.request.Request(url, data=b"", method="POST")
        with urllib.request.urlopen(
            req, timeout=5
        ) as r:  # nosemgrep: dynamic-urllib-use-detected
            r.read()
        _log.warning("printer-estop: EMERGENCY STOP on %s", printer.id)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502


@bp.route("/printer-temp-set", methods=["POST"])
def printer_temp_set():
    """Set extruder or bed temperature.

    Body: {printer_id, heater, target}
    heater: "extruder" or "bed"
    target: temperature in C (0 = off)
    """
    body = request.get_json(force=True) or {}
    printer_id = body.get("printer_id")
    heater = body.get("heater", "").lower()
    target = body.get("target")

    if heater not in ("extruder", "bed"):
        return jsonify(
            {"ok": False, "error": "heater must be 'extruder' or 'bed'"}
        ), 400
    if target is None:
        return jsonify({"ok": False, "error": "Missing 'target' temperature"}), 400

    target = int(target)
    if target < 0:
        return jsonify({"ok": False, "error": "Temperature cannot be negative"}), 400

    # Try klipper first, then bambu
    printer, err = _resolve_printer(printer_id, "klipper")
    if err:
        printer, err = _resolve_printer(printer_id, "bambu")
    if err:
        return err

    # Pattern 1 / printer-safety.md: M104 and M140 are NOT on the allowlist.
    # Refuse temp changes while a print is in progress.
    if printer.is_klipper:
        cur_state = _get_print_state(printer)
        if cur_state in ("printing", "paused"):
            return jsonify(
                {
                    "ok": False,
                    "error": f"Temperature change blocked: printer is {cur_state}",
                }
            ), 409

    # Enforce temp limits from printers.json
    limits = printer._data.get("temp_limits", {})
    if heater == "extruder" and target > limits.get("extruder_max", 310):
        return jsonify(
            {
                "ok": False,
                "error": f"Extruder max is {limits.get('extruder_max', 310)}C",
            }
        ), 400
    if heater == "bed" and target > limits.get("bed_max", 110):
        return jsonify(
            {"ok": False, "error": f"Bed max is {limits.get('bed_max', 110)}C"}
        ), 400

    if printer.is_klipper:
        gcode = f"M104 S{target}" if heater == "extruder" else f"M140 S{target}"
        ok, result = _send_moonraker_gcode(printer, gcode)
        if ok:
            _log.info("printer-temp-set: %s=%dC on %s", heater, target, printer.id)
            return jsonify({"ok": True, "heater": heater, "target": target})
        return jsonify({"ok": False, "error": str(result)}), 502

    elif printer.is_bambu:
        # Bambu supports temp via gcode_line MQTT command
        gcode = f"M104 S{target}" if heater == "extruder" else f"M140 S{target}"
        payload = {
            "print": {"sequence_id": "0", "command": "gcode_line", "param": gcode}
        }
        ok, error = _send_bambu_mqtt(printer, payload)
        if ok:
            _log.info(
                "printer-temp-set: %s=%dC on %s (bambu)", heater, target, printer.id
            )
            return jsonify({"ok": True, "heater": heater, "target": target})
        return jsonify({"ok": False, "error": error or "MQTT failed"}), 502

    return jsonify({"ok": False, "error": "Unknown printer type"}), 400


@bp.route("/printer-fan-set", methods=["POST"])
def printer_fan_set():
    """Set fan speed on Klipper printer.

    Body: {printer_id, fan, speed}
    fan: fan key from printers.json fans config (e.g. "front_cooling")
    speed: 0-100 (percentage)
    """
    body = request.get_json(force=True) or {}
    printer_id = body.get("printer_id")
    fan_key = body.get("fan", "")
    speed = body.get("speed")

    if speed is None:
        return jsonify({"ok": False, "error": "Missing 'speed'"}), 400
    speed = max(0, min(100, int(speed)))

    printer, err = _resolve_printer(printer_id, "klipper")
    if err:
        return err

    fans_config = printer._data.get("fans", {})
    fan_cfg = fans_config.get(fan_key)
    if not fan_cfg:
        return jsonify({"ok": False, "error": f"Unknown fan: {fan_key}"}), 400
    if not fan_cfg.get("controllable"):
        return jsonify(
            {"ok": False, "error": f"Fan '{fan_cfg['name']}' is auto-controlled"}
        ), 400

    gcode_template = fan_cfg.get("gcode_set", "")
    if not gcode_template:
        return jsonify({"ok": False, "error": "No gcode template for fan"}), 400

    # fan0 uses M106 S0-255, generic fans use SPEED 0.0-1.0
    if "M106" in gcode_template:
        value = int(speed * 255 / 100)
    else:
        value = round(speed / 100, 2)

    gcode = gcode_template.replace("{value}", str(value))

    # Pattern 1 / printer-safety.md: M106 is NOT on the allowlist (only
    # SET_FAN_SPEED is). Validate the resolved gcode against the live state.
    cur_state = _get_print_state(printer)
    safe, reason = _validate_gcode_safety(gcode, cur_state)
    if not safe:
        # Soft fall-through for SET_FAN_SPEED (allowlisted) — _validate_gcode_safety
        # only flags _DANGEROUS_WHILE_PRINTING + raw G0/G1, so SET_FAN_SPEED passes.
        # Anything blocked here is a real fan-config issue.
        _log.warning(
            "printer-fan-set BLOCKED: %s (state=%s, gcode=%s)",
            reason,
            cur_state,
            gcode[:80],
        )
        return jsonify({"ok": False, "error": reason, "blocked": True}), 403

    ok, result = _send_moonraker_gcode(printer, gcode)
    if ok:
        _log.info("printer-fan-set: %s=%d%% on %s", fan_key, speed, printer.id)
        return jsonify({"ok": True, "fan": fan_key, "speed": speed})
    return jsonify({"ok": False, "error": str(result)}), 502


@bp.route("/printer-move", methods=["POST"])
def printer_move():
    """Move toolhead on Klipper printer (relative).

    Body: {printer_id, axis, distance, speed}
    axis: X/Y/Z, distance: mm (signed), speed: mm/s (default 50)
    Requires state == ready.
    """
    body = request.get_json(force=True) or {}
    printer_id = body.get("printer_id")
    axis = body.get("axis", "").upper()
    distance = body.get("distance")
    speed = body.get("speed", 50)

    if axis not in ("X", "Y", "Z"):
        return jsonify({"ok": False, "error": "axis must be X, Y, or Z"}), 400
    if distance is None:
        return jsonify({"ok": False, "error": "Missing 'distance'"}), 400

    printer, err = _resolve_printer(printer_id, "klipper")
    if err:
        return err

    state = _get_print_state(printer)
    if state not in ("ready", "standby", "complete", "cancelled"):
        return jsonify(
            {"ok": False, "error": f"Movement blocked: printer is {state}"}
        ), 409

    feed = int(float(speed) * 60)  # mm/s → mm/min
    gcode = f"G91\nG1 {axis}{float(distance):.2f} F{feed}\nG90"
    ok, result = _send_moonraker_gcode(printer, gcode)
    if ok:
        _log.info(
            "printer-move: %s%s at %smm/s on %s", axis, distance, speed, printer.id
        )
        return jsonify({"ok": True, "axis": axis, "distance": distance})
    return jsonify({"ok": False, "error": str(result)}), 502


@bp.route("/printer-extrude", methods=["POST"])
def printer_extrude():
    """Extrude or retract filament on Klipper printer.

    Body: {printer_id, length, speed}
    length: mm (positive=extrude, negative=retract), speed: mm/s (default 5)
    Requires state == ready AND nozzle >= 170C.
    """
    body = request.get_json(force=True) or {}
    printer_id = body.get("printer_id")
    length = body.get("length")
    speed = body.get("speed", 5)

    if length is None:
        return jsonify({"ok": False, "error": "Missing 'length'"}), 400

    printer, err = _resolve_printer(printer_id, "klipper")
    if err:
        return err

    state = _get_print_state(printer)
    if state not in ("ready", "standby", "complete", "cancelled"):
        return jsonify(
            {"ok": False, "error": f"Extrusion blocked: printer is {state}"}
        ), 409

    # Check nozzle temperature for cold extrusion prevention
    try:
        url = f"{printer.moonraker_url}/printer/objects/query?extruder"
        with urllib.request.urlopen(
            url, timeout=5
        ) as r:  # nosemgrep: dynamic-urllib-use-detected
            data = json.loads(r.read())
        nozzle_temp = data["result"]["status"]["extruder"].get("temperature", 0)
        if nozzle_temp < 170:
            return jsonify(
                {"ok": False, "error": f"Nozzle too cold ({nozzle_temp:.0f}C < 170C)"}
            ), 409
    except Exception as e:
        return jsonify({"ok": False, "error": f"Cannot check nozzle temp: {e}"}), 502

    feed = int(float(speed) * 60)
    gcode = f"M83\nG1 E{float(length):.2f} F{feed}\nM82"
    ok, result = _send_moonraker_gcode(printer, gcode)
    if ok:
        _log.info("printer-extrude: E%s at %smm/s on %s", length, speed, printer.id)
        return jsonify({"ok": True, "length": length})
    return jsonify({"ok": False, "error": str(result)}), 502


@bp.route("/printer-home", methods=["POST"])
def printer_home():
    """Home axes on Klipper printer.

    Body: {printer_id, axes}
    axes: "XY", "Z", "ALL" (default "ALL")
    Requires state == ready.
    """
    body = request.get_json(force=True) or {}
    printer_id = body.get("printer_id")
    axes = body.get("axes", "ALL").upper()

    printer, err = _resolve_printer(printer_id, "klipper")
    if err:
        return err

    state = _get_print_state(printer)
    if state not in ("ready", "standby", "complete", "cancelled"):
        return jsonify(
            {"ok": False, "error": f"Homing blocked: printer is {state}"}
        ), 409

    if axes == "ALL":
        gcode = "G28"
    elif axes == "XY":
        gcode = "G28 X Y"
    elif axes == "Z":
        gcode = "G28 Z"
    else:
        gcode = f"G28 {axes}"

    ok, result = _send_moonraker_gcode(printer, gcode)
    if ok:
        _log.info("printer-home: %s on %s", axes, printer.id)
        return jsonify({"ok": True, "axes": axes})
    return jsonify({"ok": False, "error": str(result)}), 502


@bp.route("/printer-gcode", methods=["POST"])
def printer_gcode():
    """Send arbitrary gcode to Klipper printer with safety validation.

    Body: {printer_id, script}
    """
    body = request.get_json(force=True) or {}
    printer_id = body.get("printer_id")
    script = body.get("script", "").strip()

    if not script:
        return jsonify({"ok": False, "error": "Empty script"}), 400

    printer, err = _resolve_printer(printer_id, "klipper")
    if err:
        return err

    state = _get_print_state(printer)
    safe, reason = _validate_gcode_safety(script, state)
    if not safe:
        _log.warning(
            "printer-gcode BLOCKED: %s (state=%s, script=%s)",
            reason,
            state,
            script[:80],
        )
        return jsonify({"ok": False, "error": reason, "blocked": True}), 403

    ok, result = _send_moonraker_gcode(printer, script)
    if ok:
        _log.info(
            "printer-gcode: '%s' on %s (state=%s)", script[:60], printer.id, state
        )
        return jsonify({"ok": True, "script": script, "result": str(result)})
    return jsonify({"ok": False, "error": str(result)}), 502


@bp.route("/printer-calibrate", methods=["POST"])
def printer_calibrate():
    """Run calibration command on Klipper printer.

    Body: {printer_id, action}
    action: "qgl" (QUAD_GANTRY_LEVEL) or "bed_mesh" (BED_MESH_CALIBRATE)
    Requires state == ready.
    """
    body = request.get_json(force=True) or {}
    printer_id = body.get("printer_id")
    action = body.get("action", "").lower()

    if action not in ("qgl", "bed_mesh"):
        return jsonify(
            {"ok": False, "error": "action must be 'qgl' or 'bed_mesh'"}
        ), 400

    printer, err = _resolve_printer(printer_id, "klipper")
    if err:
        return err

    state = _get_print_state(printer)
    if state not in ("ready", "standby", "complete", "cancelled"):
        return jsonify(
            {"ok": False, "error": f"Calibration blocked: printer is {state}"}
        ), 409

    gcode = "QUAD_GANTRY_LEVEL" if action == "qgl" else "BED_MESH_CALIBRATE"
    ok, result = _send_moonraker_gcode(printer, gcode)
    if ok:
        _log.info("printer-calibrate: %s on %s", gcode, printer.id)
        return jsonify({"ok": True, "action": action})
    return jsonify({"ok": False, "error": str(result)}), 502


@bp.route("/printer-led", methods=["POST"])
def printer_led():
    """Control LED on Bambu printer.

    Body: {printer_id, mode}
    mode: "on", "off", "flash"
    """
    body = request.get_json(force=True) or {}
    printer_id = body.get("printer_id")
    mode = body.get("mode", "").lower()

    if mode not in ("on", "off", "flash"):
        return jsonify(
            {"ok": False, "error": "mode must be 'on', 'off', or 'flash'"}
        ), 400

    printer, err = _resolve_printer(printer_id, "bambu")
    if err:
        return err

    led_modes = {
        "on": {"led_node": "chamber_light", "led_mode": "on"},
        "off": {"led_node": "chamber_light", "led_mode": "off"},
        "flash": {"led_node": "chamber_light", "led_mode": "flashing"},
    }
    payload = {"system": {"sequence_id": "0", "command": "ledctrl", **led_modes[mode]}}
    ok, error = _send_bambu_mqtt(printer, payload)
    if ok:
        _log.info("printer-led: %s on %s", mode, printer.id)
        return jsonify({"ok": True, "mode": mode})
    return jsonify({"ok": False, "error": error or "MQTT failed"}), 502


@bp.route("/printer-files")
def printer_files():
    """List gcode files on Klipper printer.

    Query: ?printer_id=sv08_max
    Returns file list with metadata.
    """
    printer_id = request.args.get("printer_id")
    printer, err = _resolve_printer(printer_id, "klipper")
    if err:
        return err

    try:
        url = f"{printer.moonraker_url}/server/files/list?root=gcodes"
        with urllib.request.urlopen(
            url, timeout=10
        ) as r:  # nosemgrep: dynamic-urllib-use-detected
            data = json.loads(r.read())
        files = data.get("result", [])
        # Sort by modified time, most recent first
        files.sort(key=lambda f: f.get("modified", 0), reverse=True)
        return jsonify({"ok": True, "files": files})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "files": []}), 502


@bp.route("/printer-file-metadata")
def printer_file_metadata():
    """Get metadata for a specific gcode file.

    Query: ?printer_id=sv08_max&filename=my_print.gcode
    """
    printer_id = request.args.get("printer_id")
    filename = request.args.get("filename", "")
    if not filename:
        return jsonify({"ok": False, "error": "Missing filename"}), 400

    printer, err = _resolve_printer(printer_id, "klipper")
    if err:
        return err

    try:
        enc = urllib.parse.quote(filename, safe="")
        url = f"{printer.moonraker_url}/server/files/metadata?filename={enc}"
        with urllib.request.urlopen(
            url, timeout=10
        ) as r:  # nosemgrep: dynamic-urllib-use-detected
            data = json.loads(r.read())
        return jsonify({"ok": True, "metadata": data.get("result", {})})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502


@bp.route("/printer-file-thumbnail")
def printer_file_thumbnail():
    """Serve a gcode file's thumbnail image.

    Query: ?printer_id=sv08_max&path=.thumbs/my_print.png
    """
    printer_id = request.args.get("printer_id")
    thumb_path = request.args.get("path", "")
    if not thumb_path:
        return jsonify({"ok": False, "error": "Missing path"}), 400

    printer, err = _resolve_printer(printer_id, "klipper")
    if err:
        return err

    try:
        enc = urllib.parse.quote(thumb_path, safe="/")
        url = f"{printer.moonraker_url}/server/files/gcodes/{enc}"
        with urllib.request.urlopen(
            url, timeout=10
        ) as r:  # nosemgrep: dynamic-urllib-use-detected
            img_data = r.read()
            content_type = r.headers.get("Content-Type", "image/png")
        return Response(img_data, content_type=content_type)
    except Exception as e:
        return Response(f"Thumbnail not found: {e}", status=404)


@bp.route("/printer-print-start", methods=["POST"])
def printer_print_start():
    """Start printing a file on Klipper printer.

    Body: {printer_id, filename}
    Requires state == ready.
    """
    body = request.get_json(force=True) or {}
    printer_id = body.get("printer_id")
    filename = body.get("filename", "").strip()

    if not filename:
        return jsonify({"ok": False, "error": "Missing filename"}), 400

    printer, err = _resolve_printer(printer_id, "klipper")
    if err:
        return err

    state = _get_print_state(printer)
    if state not in ("ready", "standby", "complete", "cancelled", "error"):
        return jsonify(
            {"ok": False, "error": f"Cannot start print: printer is {state}"}
        ), 409

    dr = _direct_dry_run("printer_print_start", printer=printer.id, filename=filename)
    if dr is not None:
        return dr

    try:
        payload = json.dumps({"filename": filename}).encode()
        req = urllib.request.Request(
            f"{printer.moonraker_url}/printer/print/start",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(
            req, timeout=10
        ) as r:  # nosemgrep: dynamic-urllib-use-detected
            r.read()
        _log.info("printer-print-start: %s on %s", filename, printer.id)
        return jsonify({"ok": True, "filename": filename})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502


@bp.route("/printer-history")
def printer_history():
    """Get print job history from Klipper/Moonraker.

    Query: ?printer_id=sv08_max&limit=20
    """
    printer_id = request.args.get("printer_id")
    limit = request.args.get("limit", "20")
    printer, err = _resolve_printer(printer_id, "klipper")
    if err:
        return err

    try:
        url = (
            f"{printer.moonraker_url}/server/history/list?limit={int(limit)}&order=desc"
        )
        with urllib.request.urlopen(
            url, timeout=10
        ) as r:  # nosemgrep: dynamic-urllib-use-detected
            data = json.loads(r.read())
        jobs = data.get("result", {}).get("jobs", [])
        return jsonify({"ok": True, "jobs": jobs})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "jobs": []}), 502
