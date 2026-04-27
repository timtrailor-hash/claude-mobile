"""Bambu Lab MQTT command sender.

Honors CONV_PRINTER_DRY_RUN — short-circuits before any MQTT connect when
the flag is set. Required for staging safety (slice 1b).
"""

from __future__ import annotations

import json
import ssl
import time

from conv.app import _log, is_dry_run


def _send_bambu_mqtt(printer, payload_dict):
    """Send a command to Bambu printer via MQTT. Returns (ok, error_or_none)."""
    if is_dry_run("PRINTER"):
        _log.info(
            "[DRY RUN] _send_bambu_mqtt printer=%s payload=%s",
            printer.id,
            str(payload_dict)[:120],
        )
        return True, None
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        return False, "paho-mqtt not installed"

    creds = printer.get_bambu_credentials()
    serial = creds.get("serial", "")
    access_code = creds.get("access_code", "")
    if not serial or not access_code:
        return False, "Bambu credentials not configured"

    result = {"ok": False, "error": None}
    done = [False]

    def on_connect(client, userdata, flags, rc, properties=None):
        if rc == 0:
            client.publish(f"device/{serial}/request", json.dumps(payload_dict))
            result["ok"] = True
            done[0] = True
        else:
            result["error"] = f"MQTT connect failed: rc={rc}"
            done[0] = True

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.username_pw_set("bblp", access_code)
    client.tls_set_context(ctx)
    client.on_connect = on_connect

    try:
        client.connect(printer.host, printer.mqtt_port or 8883, 60)
        client.loop_start()
        timeout_t = time.time() + 5
        while not done[0] and time.time() < timeout_t:
            time.sleep(0.1)
        client.loop_stop()
        client.disconnect()
    except Exception as e:
        return False, str(e)

    return result["ok"], result.get("error")
