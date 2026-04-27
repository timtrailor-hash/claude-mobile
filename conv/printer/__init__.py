"""Printer slice — Moonraker (Klipper) and Bambu MQTT command paths.

Extracted from conversation_server.py 2026-04-27.

Public surface re-exported for back-compat:
  _get_print_state, _resolve_printer
  _send_moonraker_gcode, _send_bambu_mqtt
  _validate_gcode_safety, _DANGEROUS_WHILE_PRINTING, _ALWAYS_ALLOWED
  bp                       — Flask blueprint with /printer-resume-info,
                             /printer-resume-from-layer

NOT in this slice (deferred):
  printer_alert_endpoint   — uses _broadcast_ws from the monolith;
                             extracted when websocket slice lands.
"""

from .bambu import _send_bambu_mqtt  # noqa: F401
from .moonraker import _send_moonraker_gcode  # noqa: F401
from .routes import bp  # noqa: F401
from .control import bp as bp_control  # noqa: F401
from .status import bp_status  # noqa: F401
from .safety import (  # noqa: F401
    _ALWAYS_ALLOWED,
    _DANGEROUS_WHILE_PRINTING,
    _validate_gcode_safety,
)
from .state import _get_print_state, _resolve_printer  # noqa: F401
