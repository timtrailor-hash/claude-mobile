"""Printer state lookup helpers.

_get_print_state — query Moonraker for live print_stats.state.
_resolve_printer — resolve a printer from request body (printer_id or fallback).
"""

from __future__ import annotations

import json
import urllib.request

from flask import jsonify

from conv.app import _log, _printer_registry


def _get_print_state(printer) -> str:
    """Fetch live print_stats.state from Moonraker. Returns state string or 'unknown'."""
    try:
        url = f"{printer.moonraker_url}/printer/objects/query?print_stats"
        with urllib.request.urlopen(
            url, timeout=5
        ) as r:  # nosemgrep: dynamic-urllib-use-detected
            data = json.loads(r.read())
        return data["result"]["status"]["print_stats"]["state"]
    except Exception as e:
        _log.warning("_get_print_state failed for %s: %s", printer.id, e)
        return "unknown"


def _resolve_printer(printer_id=None, printer_type: str = "klipper"):
    """Resolve printer from request. Returns (printer, error_response) — one will be None."""
    try:
        if printer_id:
            return _printer_registry.get(printer_id), None
        if printer_type == "klipper":
            return _printer_registry.list_klipper()[0], None
        if printer_type == "bambu":
            return _printer_registry.list_bambu()[0], None
        return _printer_registry.list_enabled()[0], None
    except (KeyError, IndexError):
        return None, (
            jsonify({"ok": False, "error": f"No {printer_type} printer found"}),
            404,
        )
