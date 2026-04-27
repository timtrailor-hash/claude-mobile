"""Printer route handlers — Flask blueprint.

Routes:
  GET  /printer-resume-info        — describe last checkpoint + resume viability
  POST /printer-resume-from-layer  — generate resume gcode + optionally start

NOT in this slice (deferred):
  /printer-alert            — uses _broadcast_ws from monolith;
                              extracts when websocket slice lands.
  /printer-print-control,
  /printer-estop, /printer-temp-set, /printer-fan-set, /printer-move,
  /printer-extrude, /printer-home, /printer-gcode, /printer-calibrate,
  /printer-led, /printer-files, /printer-file-metadata,
  /printer-file-thumbnail, /printer-print-start
                            — extracted in slice 1f.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request

from flask import Blueprint, jsonify, request

from conv.app import _log, _printer_registry, is_dry_run

bp = Blueprint("printer", __name__)


@bp.route("/printer-resume-info")
def printer_resume_info():
    """Show last checkpoint data and whether a resume is possible."""
    checkpoint_file = "/tmp/printer_status/print_checkpoint.json"
    try:
        with open(checkpoint_file) as f:
            cp = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return jsonify({"available": False, "reason": "No checkpoint found"})

    # Check if the file still exists on the printer.
    filename = cp.get("filename", "")
    file_exists = False
    if filename:
        try:
            klipper = _printer_registry.list_klipper()[0]
            encoded = urllib.parse.quote(filename, safe="")
            url = f"{klipper.moonraker_url}/server/files/metadata?filename={encoded}"
            with urllib.request.urlopen(
                url, timeout=5
            ) as r:  # nosemgrep: dynamic-urllib-use-detected
                meta = json.loads(r.read())
            file_exists = meta.get("result", {}).get("filename") is not None
        except Exception:
            pass

    # Check checkpoint age (only valid for ~24h).
    ts = cp.get("timestamp", "")
    age_hours = 999
    try:
        from datetime import datetime as dt

        cp_time = dt.fromisoformat(ts)
        age_hours = (dt.now() - cp_time).total_seconds() / 3600
    except Exception:
        pass

    # Check if printer is currently idle (print finished successfully).
    printer_idle_after_complete = False
    try:
        with open("/tmp/printer_status/status.json") as sf:
            status = json.load(sf)
        sovol = status.get("printers", {}).get("sovol", {})
        cur_state = (sovol.get("state") or "").lower()
        cur_progress = sovol.get("progress", 0) or 0
        # If printer is idle/standby with 0% progress, print is done — no resume needed.
        if cur_state in ("standby", "ready", "idle", "complete") and cur_progress < 1:
            printer_idle_after_complete = True
    except Exception:
        pass

    available = (
        file_exists
        and age_hours < 24
        and cp.get("progress_pct", 0) > 0
        and not printer_idle_after_complete
    )
    safe_layer = max(1, cp.get("current_layer", 0) - 2)

    # Estimate time saved.
    total_layers = cp.get("total_layers", 1) or 1
    duration_s = cp.get("print_duration_s", 0) or 0
    estimated_total_s = duration_s / max(0.01, cp.get("progress_pct", 1) / 100)
    skip_fraction = safe_layer / total_layers
    time_saved_h = (estimated_total_s * skip_fraction) / 3600

    return jsonify(
        {
            "available": available,
            "checkpoint": cp,
            "file_exists": file_exists,
            "age_hours": round(age_hours, 1),
            "suggested_layer": safe_layer,
            "estimated_time_saved_h": round(time_saved_h, 1),
        }
    )


@bp.route("/printer-resume-from-layer", methods=["POST"])
def printer_resume_from_layer():
    """Generate a resume gcode file and optionally start the print.

    Accepts JSON: {"filename": "...", "layer": 52} or {"layer": "auto"}
    """
    body = request.get_json(silent=True) or {}
    layer = body.get("layer", "auto")
    auto_start = body.get("start", False)
    if is_dry_run("PRINTER"):
        _log.info(
            "[DRY RUN] printer_resume_from_layer layer=%s auto_start=%s",
            layer,
            auto_start,
        )
        return jsonify({"ok": True, "dry_run": True, "planned_layer": layer})

    # Get filename from body or checkpoint.
    filename = body.get("filename", "")
    if not filename:
        try:
            with open("/tmp/printer_status/print_checkpoint.json") as f:
                cp = json.load(f)
            filename = cp.get("filename", "")
        except Exception:
            return jsonify({"ok": False, "error": "No filename and no checkpoint"}), 400

    if not filename:
        return jsonify({"ok": False, "error": "No filename found"}), 400

    # Pattern 1: state check BEFORE auto_start. Without this, gcode_resume.py
    # --start would call Moonraker /printer/print/start regardless of whether
    # the printer is already printing — a second start command mid-print.
    if auto_start:
        from .state import _get_print_state, _resolve_printer

        klipper, _err = _resolve_printer(printer_type="klipper")
        if klipper:
            cur = _get_print_state(klipper)
            if cur not in (
                "ready",
                "standby",
                "complete",
                "cancelled",
                "error",
                "unknown",
            ):
                _log.warning(
                    "printer-resume blocked: auto_start refused while printer is %s",
                    cur,
                )
                return jsonify(
                    {
                        "ok": False,
                        "error": f"Cannot start resume: printer is {cur}",
                    }
                ), 409

    # Run gcode_resume.py as subprocess.
    script = os.path.expanduser("~/code/sv08-print-tools/gcode_resume.py")
    cmd = [sys.executable, script, "--file", filename, "--layer", str(layer)]
    if auto_start:
        cmd.append("--start")

    _log.info("printer-resume: running %s", " ".join(cmd))

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,
            cwd=os.path.dirname(script),
        )
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "Resume script timed out (>10min)"}), 504

    if result.returncode != 0:
        _log.error("printer-resume failed: %s", result.stderr[:500])
        return (
            jsonify(
                {
                    "ok": False,
                    "error": result.stderr[:300] or "Unknown error",
                    "stdout": result.stdout[-500:],
                }
            ),
            500,
        )

    # Parse summary JSON from stdout.
    summary = {}
    for line in result.stdout.split("\n"):
        if line.startswith("__SUMMARY_JSON__:"):
            try:
                summary = json.loads(line.split(":", 1)[1])
            except json.JSONDecodeError:
                pass

    _log.info("printer-resume: success — %s", summary.get("resume_file", "?"))
    return jsonify(
        {
            "ok": True,
            **summary,
            "auto_started": auto_start,
        }
    )
