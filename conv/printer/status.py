"""Printer status + image routes — third printer blueprint (`bp_status`).

Routes (6):
  GET  /printers              — list all configured printers from registry
  GET  /printer-status        — cached daemon status + overlays auto_speed
  POST /printer-speed-auto    — toggle auto-speed config flag
  POST /printer-speed-set     — set M220 speed factor (10–300%)
  GET  /printer-image/<name>  — serve camera snapshots + thumbnails
  GET  /file                  — serve user-readable images from /tmp or ~/Documents

NOTE: /printer-speed-set previously bypassed `_send_moonraker_gcode` and
hit Moonraker via raw urllib, which meant it ignored `CONV_PRINTER_DRY_RUN`.
This slice routes it through the canonical sender so dry-run is honored.
"""

from __future__ import annotations

import json
import mimetypes
import os

from flask import Blueprint, Response, jsonify, request

from conv.app import _log, _printer_registry, is_dry_run

from .moonraker import _send_moonraker_gcode

bp_status = Blueprint("printer_status", __name__)


@bp_status.route("/printers")
def list_printers():
    """List all configured printers from registry."""
    return jsonify({"printers": _printer_registry.to_dict()})


@bp_status.route("/printer-status")
def printer_status():
    """Serve cached printer status from daemon output."""
    status_file = os.path.join(_printer_registry.status_dir, "status.json")
    try:
        with open(status_file) as f:
            data = json.load(f)
    except FileNotFoundError:
        return jsonify(
            {
                "sv08": {"error": "Daemon not running"},
                "a1": {"error": "Daemon not running"},
            }
        )
    except Exception as e:
        return jsonify({"sv08": {"error": str(e)}, "a1": {"error": str(e)}})

    result = {"sv08": {}, "a1": {}}

    # Use registry to iterate printers, build response with legacy keys.
    for printer in _printer_registry.list_enabled():
        legacy_key = _printer_registry.legacy_key(printer.id)
        printer_data = data.get("printers", {}).get(legacy_key, {})

        if printer_data.get("online"):
            pd = dict(printer_data)
            if printer.is_klipper and "state" in pd:
                pd["state"] = (pd["state"] or "unknown").capitalize()
            elif printer.is_bambu:
                pd.setdefault("layer", pd.pop("layer_num", None))
                pd.setdefault("total_layers", pd.pop("total_layer_num", None))
                speed_names = printer.speed_levels
                sl = pd.get("speed_level")
                pd["speed"] = speed_names.get(str(sl), "?") if sl else "?"
            # Use legacy response keys for backward compat.
            resp_key = (
                "sv08"
                if legacy_key == "sovol"
                else "a1"
                if legacy_key == "bambu"
                else printer.id
            )
            result[resp_key] = pd
        else:
            resp_key = (
                "sv08"
                if legacy_key == "sovol"
                else "a1"
                if legacy_key == "bambu"
                else printer.id
            )
            result[resp_key] = {"error": printer_data.get("error", "Offline")}

    result["daemon_timestamp"] = data.get("timestamp", "")

    # Overlay live auto_speed state (daemon cache may be stale between polls).
    try:
        auto_speed_file = os.path.join(_printer_registry.status_dir, "auto_speed.json")
        with open(auto_speed_file) as asf:
            auto_cfg = json.load(asf)
        if (
            "sv08" in result
            and isinstance(result["sv08"], dict)
            and "error" not in result["sv08"]
        ):
            result["sv08"]["auto_speed_enabled"] = auto_cfg.get("enabled", False)
            result["sv08"]["auto_speed_mode"] = auto_cfg.get("mode", "optimal")
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    return jsonify(result)


@bp_status.route("/printer-speed-auto", methods=["POST"])
def printer_speed_auto():
    """Toggle auto-speed on/off for the Sovol SV08.

    Accepts JSON: {"enabled": true/false}
    Writes to /tmp/printer_status/auto_speed.json (same file gcode_profile.py uses).
    """
    body = request.get_json(silent=True) or {}
    enabled = body.get("enabled")
    if enabled is None:
        return jsonify({"ok": False, "error": "Missing 'enabled' field"}), 400

    auto_speed_file = "/tmp/printer_status/auto_speed.json"
    try:
        with open(auto_speed_file) as f:
            cfg = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        cfg = {
            "enabled": False,
            "mode": "optimal",
            "min_speed_pct": 80,
            "max_speed_pct": 200,
            "skip_first_layers": 2,
        }

    cfg["enabled"] = bool(enabled)
    with open(auto_speed_file, "w") as f:
        json.dump(cfg, f, indent=2)

    _log.info("printer-speed-auto: set enabled=%s", enabled)
    return jsonify({"ok": True, "enabled": cfg["enabled"]})


@bp_status.route("/printer-speed-set", methods=["POST"])
def printer_speed_set():
    """Set manual speed factor on the Sovol SV08 via Moonraker M220 gcode.

    Accepts JSON: {"speed_pct": 150, "printer_id": "sv08_max"}.

    Note: this endpoint previously bypassed _send_moonraker_gcode and called
    urllib directly, which meant it ignored CONV_PRINTER_DRY_RUN. It now uses
    the canonical sender so dry-run is honored.
    """
    body = request.get_json(silent=True) or {}
    speed_pct = body.get("speed_pct")
    if speed_pct is None:
        return jsonify({"ok": False, "error": "Missing 'speed_pct' field"}), 400

    speed_pct = int(speed_pct)
    if not (10 <= speed_pct <= 300):
        return jsonify({"ok": False, "error": "speed_pct must be 10-300"}), 400

    printer_id = body.get("printer_id")
    try:
        printer = (
            _printer_registry.get(printer_id)
            if printer_id
            else _printer_registry.list_klipper()[0]
        )
    except (KeyError, IndexError):
        return jsonify({"ok": False, "error": "No klipper printer found"}), 404

    gcode = f"M220 S{speed_pct}"
    ok, result = _send_moonraker_gcode(printer, gcode)
    _log.info("printer-speed-set: M220 S%d -> ok=%s", speed_pct, ok)
    if ok:
        return jsonify({"ok": True, "speed_pct": speed_pct})
    return jsonify({"ok": False, "error": str(result)}), 502


@bp_status.route("/printer-image/<name>")
def printer_image(name):
    """Serve camera snapshots and thumbnails."""
    if name not in _printer_registry.image_whitelist():
        return "Not found", 404
    img_dir = _printer_registry.status_dir
    path = os.path.join(img_dir, name)
    if not os.path.exists(path):
        return "Not found", 404
    mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
    with open(path, "rb") as f:
        return Response(f.read(), content_type=mime)


@bp_status.route("/file")
def serve_file():
    """Serve files that Claude has read (images only, restricted directories)."""
    fpath = request.args.get("path", "")
    if not fpath:
        return "Missing path", 400
    # Resolve to absolute, block path traversal.
    fpath = os.path.realpath(fpath)
    # Only allow image files.
    if not any(
        fpath.lower().endswith(ext)
        for ext in (".png", ".jpg", ".jpeg", ".gif", ".webp")
    ):
        return "Not an image file", 403
    # Only allow files under known safe directories.
    # Resolve prefixes too — macOS /tmp is a symlink to /private/tmp.
    safe_prefixes = [
        os.path.realpath("/tmp/") + "/",
        os.path.realpath(os.path.expanduser("~/Documents/")) + "/",
    ]
    if not any(fpath.startswith(p) for p in safe_prefixes):
        return "Access denied", 403
    if not os.path.exists(fpath):
        return "Not found", 404
    mime = mimetypes.guess_type(fpath)[0] or "application/octet-stream"
    with open(fpath, "rb") as f:
        return Response(f.read(), content_type=mime)


# Suppress unused-import linter: is_dry_run is referenced indirectly via
# _send_moonraker_gcode (which honors the flag itself). Future direct dry-run
# guards in this module would use it.
_ = is_dry_run
