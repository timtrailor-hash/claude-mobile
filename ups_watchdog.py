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
import ssl
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
import urllib.parse
from logging.handlers import RotatingFileHandler

# ---------------------------------------------------------------------------
# Printer config — import from shared config, fall back to defaults
# ---------------------------------------------------------------------------
try:
    sys.path.insert(0, os.path.expanduser("~/Documents/Claude code"))
    sys.path.insert(0, os.path.expanduser("~/projects/claude"))
    from printer_config import (SOVOL_IP, MOONRAKER_PORT,
                                BAMBU_IP, BAMBU_SERIAL,
                                BAMBU_ACCESS_CODE, BAMBU_MQTT_PORT)
except ImportError:
    SOVOL_IP = "192.168.87.52"
    MOONRAKER_PORT = 7125
    BAMBU_IP = "192.168.87.47"
    BAMBU_SERIAL = ""
    BAMBU_ACCESS_CODE = ""
    BAMBU_MQTT_PORT = 8883

# Notification credentials — Slack DM + email + push
try:
    from credentials import (SLACK_BOT_TOKEN, SMTP_HOST, SMTP_PORT,
                              SMTP_USER, SMTP_PASS)
except ImportError:
    SLACK_BOT_TOKEN = ""
    SMTP_HOST = ""
    SMTP_PORT = 587
    SMTP_USER = ""
    SMTP_PASS = ""

SLACK_USER_ID = "U03H1AN51MZ"       # Tim Trailor
NOTIFY_EMAIL = "timtrailor@gmail.com"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
POLL_INTERVAL = 30          # seconds between power checks
EMERGENCY_BATTERY_PCT = 10  # below this → kill all beds, emergency stop Sovol
MOONRAKER_BASE = f"http://{SOVOL_IP}:{MOONRAKER_PORT}"
# macOS haltlevel should be set to 5% (sudo pmset -u haltlevel 5)
# so the watchdog has 10%→5% to act before the Mac shuts down

# Bed heater power scheduling — limit at night to avoid UPS overload beeping
NIGHT_START_HOUR = 22       # 10 PM — reduce bed max_power
NIGHT_END_HOUR = 7          # 7 AM — restore full bed max_power
BED_POWER_DAY = 1.0         # full power during the day
BED_POWER_NIGHT = 0.7       # 70% at night (keeps total load under UPS 900W)
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


# ---------------------------------------------------------------------------
# Bed heater power scheduling (night mode)
# ---------------------------------------------------------------------------
def _is_night_hours():
    """Return True if current time is in the night window (10pm-7am)."""
    hour = time.localtime().tm_hour
    return hour >= NIGHT_START_HOUR or hour < NIGHT_END_HOUR


def _sovol_ssh(cmd):
    """Run a command on the Sovol via SSH (uses sshpass)."""
    try:
        result = subprocess.run(
            ["/opt/homebrew/bin/sshpass", "-p", "sovol",
             "ssh", "-o", "StrictHostKeyChecking=no",
             f"sovol@{SOVOL_IP}", cmd],
            capture_output=True, text=True, timeout=15,
        )
        return result.returncode == 0, result.stdout.strip()
    except Exception as exc:
        return False, str(exc)


def _get_current_bed_max_power():
    """Read the current heater_bed max_power from Klipper's live config."""
    url = f"{MOONRAKER_BASE}/printer/objects/query?configfile=settings"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        return data["result"]["status"]["configfile"]["settings"]["heater_bed"]["max_power"]
    except Exception:
        return None


def set_bed_max_power(power):
    """Change heater_bed max_power in printer.cfg and restart firmware.

    Only applies when the printer is NOT actively printing and the current
    value differs from the target.
    """
    # Check if already at the desired value
    current = _get_current_bed_max_power()
    if current is not None and abs(current - power) < 0.01:
        logger.debug("Bed max_power already at %s — no change needed", power)
        return True

    state = get_sovol_print_state()
    if state == "printing":
        logger.info("Skipping bed power change — printer is actively printing")
        return False

    if state == "unreachable":
        logger.warning("Sovol unreachable — cannot change bed power")
        return False

    # sed: replace max_power on line 346 (heater_bed section)
    ok, out = _sovol_ssh(
        f'sed -i "346s/max_power: .*/max_power: {power}/" '
        '/home/sovol/printer_data/config/printer.cfg'
    )
    if not ok:
        logger.error("Failed to update printer.cfg: %s", out)
        return False

    # Firmware restart to apply
    moonraker_request("/printer/firmware_restart")
    logger.info("Bed max_power changed from %s to %s — firmware restarting",
                current, power)
    return True


def _notify_push(message, level="warning"):
    """Send alert to the iOS app via conversation server."""
    try:
        data = json.dumps({"message": message, "level": level}).encode()
        req = urllib.request.Request(
            "http://localhost:8081/printer-alert",
            data=data, method="POST",
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=5)
        logger.info("Push notification sent")
    except Exception as exc:
        logger.warning("Failed to send push notification: %s", exc)


def _notify_slack(message):
    """Send a Slack DM to Tim."""
    if not SLACK_BOT_TOKEN:
        logger.warning("No Slack token — skipping Slack notification")
        return
    try:
        data = json.dumps({
            "channel": SLACK_USER_ID,
            "text": f":rotating_light: *UPS ALERT* :rotating_light:\n{message}",
        }).encode()
        req = urllib.request.Request(
            "https://slack.com/api/chat.postMessage",
            data=data, method="POST",
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
            },
        )
        resp = json.loads(urllib.request.urlopen(req, timeout=10).read())
        if resp.get("ok"):
            logger.info("Slack DM sent")
        else:
            logger.warning("Slack API error: %s", resp.get("error", "unknown"))
    except Exception as exc:
        logger.warning("Failed to send Slack DM: %s", exc)


def _notify_email(subject, message):
    """Send email via SMTP (Gmail)."""
    if not SMTP_USER:
        logger.warning("No SMTP credentials — skipping email notification")
        return
    try:
        import smtplib
        from email.mime.text import MIMEText
        msg = MIMEText(message)
        msg["Subject"] = subject
        msg["From"] = SMTP_USER
        msg["To"] = NOTIFY_EMAIL
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)
        logger.info("Email sent to %s", NOTIFY_EMAIL)
    except Exception as exc:
        logger.warning("Failed to send email: %s", exc)


def notify_all(message, level="warning"):
    """Blast notification across ALL channels: push, Slack DM, and email.

    Each channel is independent — failure of one doesn't block the others.
    """
    subject = "UPS POWER FAILURE" if level == "warning" else "UPS Alert"
    _notify_push(message, level)
    _notify_slack(message)
    _notify_email(f"[Mac Mini] {subject}", message)


def _gcode(cmd):
    """Send a gcode command to the Sovol via Moonraker."""
    encoded = urllib.parse.quote(cmd)
    ok, msg = moonraker_request(f"/printer/gcode/script?script={encoded}")
    if ok:
        logger.info("  gcode OK: %s", cmd)
    else:
        logger.error("  gcode FAILED: %s — %s", cmd, msg)
    return ok


def _save_plr_state():
    """Save print position and state via PLR variables for later resume.

    Queries all current print parameters from Moonraker and saves them
    to Klipper's saved_variables.cfg via SAVE_VARIABLE commands.
    After power restore, POWER_RESUME macro reads these to continue.
    """
    url = (f"{MOONRAKER_BASE}/printer/objects/query?"
           "gcode_move=gcode_position,speed_factor,extrude_factor"
           "&virtual_sdcard=file_position,file_path,progress"
           "&extruder=temperature,target"
           "&heater_bed=temperature,target"
           "&fan_generic%20fan0=speed")
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        status = data["result"]["status"]
    except Exception as exc:
        logger.error("Failed to query print state for PLR save: %s", exc)
        return False

    gm = status["gcode_move"]
    vs = status["virtual_sdcard"]
    ext = status["extruder"]
    bed = status["heater_bed"]
    fan = status.get("fan_generic fan0", {})

    z_pos = gm["gcode_position"][2]
    speed_pct = int(gm["speed_factor"] * 100)
    flow_pct = int(gm["extrude_factor"] * 100)
    fan_speed = int(fan.get("speed", 1.0) * 255)
    nozzle_target = int(ext["target"])
    bed_target = int(bed["target"])
    file_pos = vs["file_position"]
    progress = vs.get("progress", 0)

    logger.info("Saving PLR state: Z=%.2f, speed=%d%%, flow=%d%%, fan=%d, "
                "nozzle=%d°C, bed=%d°C, file_pos=%d, progress=%.1f%%",
                z_pos, speed_pct, flow_pct, fan_speed,
                nozzle_target, bed_target, file_pos, progress * 100)

    variables = [
        ("power_resume_z", z_pos),
        ("plr_speed_pct", speed_pct),
        ("plr_flow_pct", flow_pct),
        ("plr_fan_speed", fan_speed),
        ("plr_nozzle_target", nozzle_target),
        ("plr_bed_target", bed_target),
        ("plr_file_position", file_pos),
        ("was_interrupted", "True"),
    ]
    for var, val in variables:
        _gcode(f"SAVE_VARIABLE VARIABLE={var} VALUE={val}")
    _gcode("save_last_file")

    logger.info("PLR state saved — can resume with POWER_RESUME after power restore")
    return True


def protect_sovol():
    """Full power-loss protection sequence for Sovol SV08.

    Goal: minimise power draw to maximise UPS runtime while preserving
    the ability to resume the print after power restore.

    Sequence:
      1. Save print position + state to PLR variables (for POWER_RESUME)
      2. Pause the print (stops movement, retracts filament)
      3. Turn off hotend (saves ~50W)
      4. Drop bed to 45°C (saves ~400W but maintains adhesion)
      5. Turn off part cooling fan (saves ~5-10W)
      6. Turn off LED strip (saves ~5W)
      7. Disable X/Y/E steppers (saves ~30-60W; Z stays locked to hold height)

    After power restore: run POWER_RESUME from Mainsail to re-heat, re-home,
    and continue from saved position.
    """
    state = get_sovol_print_state()

    if state == "unreachable":
        logger.warning("Sovol is unreachable — cannot protect")
        return

    if state == "printing":
        # 1. Save PLR state BEFORE pausing (captures exact position)
        logger.warning("Sovol is printing — saving state and pausing")
        _save_plr_state()

        # 2. Pause print (retracts filament, lifts Z, parks head)
        ok, msg = moonraker_request("/printer/print/pause")
        if ok:
            logger.info("Sovol paused successfully")
        else:
            logger.error("Failed to pause Sovol: %s", msg)
    elif state == "paused":
        logger.info("Sovol already paused — saving state and reducing power")
        _save_plr_state()
    else:
        logger.info("Sovol print state is '%s' — no print to save, reducing power", state)

    # 3. Turn off hotend (~50W saved)
    _gcode("SET_HEATER_TEMPERATURE HEATER=extruder TARGET=0")

    # 4. Drop bed to 45°C (~400W saved while maintaining adhesion)
    _gcode("SET_HEATER_TEMPERATURE HEATER=heater_bed TARGET=45")

    # 5. Turn off part cooling fan
    _gcode("M106 S0")

    # 6. Turn off LED strip (if configured)
    _gcode("SET_PIN PIN=caselight VALUE=0")

    # 7. Disable X/Y/E steppers (Z stays locked to hold print height)
    #    Saves ~30-60W. Safe because PLR state is already saved and
    #    POWER_RESUME will re-home before continuing.
    _gcode("SET_STEPPER_ENABLE STEPPER=stepper_x ENABLE=0")
    _gcode("SET_STEPPER_ENABLE STEPPER=stepper_y ENABLE=0")
    _gcode("SET_STEPPER_ENABLE STEPPER=extruder ENABLE=0")

    logger.info("Sovol protected: paused, PLR saved, heaters/fans/steppers reduced")


def emergency_stop_sovol():
    """Send emergency stop to the Sovol via Moonraker."""
    logger.critical("EMERGENCY STOP — battery below %d%%", EMERGENCY_BATTERY_PCT)
    ok, msg = moonraker_request("/printer/emergency_stop")
    if ok:
        logger.info("Sovol emergency stop sent successfully")
    else:
        logger.error("Failed to send emergency stop to Sovol: %s", msg)


# ---------------------------------------------------------------------------
# Bambu A1 MQTT control
# ---------------------------------------------------------------------------

def _bambu_mqtt_command(payload):
    """Send a command to the Bambu A1 via MQTT (one-shot connection)."""
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        logger.warning("paho-mqtt not installed — cannot control Bambu")
        return False

    if not BAMBU_SERIAL or not BAMBU_ACCESS_CODE:
        logger.warning("Bambu credentials not configured — skipping")
        return False

    topic = f"device/{BAMBU_SERIAL}/request"
    success = [False]

    def on_connect(client, userdata, flags, reason_code, properties):
        if reason_code == 0:
            client.publish(topic, json.dumps(payload))
            success[0] = True
            logger.info("  Bambu MQTT command sent: %s", list(payload.keys()))
        else:
            logger.error("  Bambu MQTT connect failed: %s", reason_code)
        client.disconnect()

    try:
        client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id="ups_watchdog_bambu",
            protocol=mqtt.MQTTv311,
        )
        client.username_pw_set("bblp", BAMBU_ACCESS_CODE)
        tls_ctx = ssl.create_default_context()
        tls_ctx.check_hostname = False
        tls_ctx.verify_mode = ssl.CERT_NONE
        client.tls_set_context(tls_ctx)
        client.on_connect = on_connect
        client.connect(BAMBU_IP, BAMBU_MQTT_PORT, keepalive=10)
        client.loop(timeout=5)
        return success[0]
    except Exception as exc:
        logger.error("Bambu MQTT error: %s", exc)
        return False


def protect_bambu():
    """Pause Bambu A1 and minimise power draw immediately.

    Pauses print, turns off hotend/fans/light/camera, drops bed to 45°C.
    Bed stays warm until battery hits EMERGENCY_BATTERY_PCT (managed by
    the main loop calling shutdown_bambu_bed()).
    """
    logger.warning("Bambu A1: pausing print, reducing power")

    # 1. Pause print immediately
    _bambu_mqtt_command({"print": {"command": "pause", "sequence_id": "0"}})
    time.sleep(1)

    # 2. Turn off hotend (~40W saved)
    _bambu_mqtt_command({"print": {"command": "gcode_line",
                                   "sequence_id": "0",
                                   "param": "M104 S0\n"}})
    # 3. Drop bed to 45°C (maintains adhesion, saves ~80W vs full temp)
    _bambu_mqtt_command({"print": {"command": "gcode_line",
                                   "sequence_id": "0",
                                   "param": "M140 S45\n"}})
    # 4. Turn off part cooling fan
    _bambu_mqtt_command({"print": {"command": "gcode_line",
                                   "sequence_id": "0",
                                   "param": "M106 P1 S0\n"}})
    # 5. Turn off aux fan
    _bambu_mqtt_command({"print": {"command": "gcode_line",
                                   "sequence_id": "0",
                                   "param": "M106 P2 S0\n"}})
    # 6. Turn off chamber light
    _bambu_mqtt_command({"system": {"sequence_id": "0",
                                    "command": "ledctrl",
                                    "led_node": "chamber_light",
                                    "led_mode": "off"}})
    # 7. Turn off camera recording
    _bambu_mqtt_command({"camera": {"sequence_id": "0",
                                    "command": "ipcam_record_set",
                                    "ipcam_record": "disable"}})

    logger.info("Bambu A1: paused, hotend off, bed→45°C, fans/light/camera off")


def shutdown_bambu_bed():
    """Kill Bambu bed heater and stop print — called when battery is critical."""
    logger.warning("Bambu A1: battery critical — bed OFF, stopping print")
    _bambu_mqtt_command({"print": {"command": "gcode_line",
                                   "sequence_id": "0",
                                   "param": "M140 S0\n"}})
    _bambu_mqtt_command({"print": {"command": "stop", "sequence_id": "0"}})


def reduce_mac_power():
    """Reduce Mac Mini power draw by stopping non-essential services."""
    logger.info("Reducing Mac Mini power draw")
    cmds = [
        # Stop non-essential services
        ["launchctl", "bootout", f"gui/{os.getuid()}",
         os.path.expanduser("~/Library/LaunchAgents/com.timtrailor.printer-snapshots.plist")],
        # Kill non-essential processes
        ["pkill", "-f", "streamlit"],
        ["pkill", "-f", "streamlit_https_proxy"],
        # Dim display (already set to 2 min on battery, but force it now)
        ["pmset", "displaysleepnow"],
    ]
    for cmd in cmds:
        try:
            subprocess.run(cmd, capture_output=True, timeout=5)
        except Exception:
            pass
    logger.info("Mac Mini: stopped printer daemon, Streamlit, HTTPS proxy, display off")


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
    mac_reduced = False
    current_bed_mode = None  # "day" or "night" — tracks what we last set
    notified_levels = set()  # track which % thresholds we've already notified

    while True:
        state = get_power_state()
        source = state["source"]
        percent = state["percent"]
        remaining = state["remaining"]

        # --- Transition: AC → Battery (power cut) ---
        if previous_source is not None and source != previous_source:
            if source == "Battery Power":
                logger.warning(
                    "POWER LOST — now on battery (%s%%, %s remaining)",
                    percent, remaining or "unknown"
                )
                # Immediate protection for both printers
                protect_sovol()
                protect_bambu()
                # Reduce Mac Mini draw (kill non-essential services)
                reduce_mac_power()
                mac_reduced = True

                notify_all(
                    f"POWER FAILURE — both printers paused, beds at 45°C, "
                    f"UPS at {percent}% ({remaining or 'unknown'} remaining). "
                    f"Beds stay warm until {EMERGENCY_BATTERY_PCT}%."
                )
                emergency_sent = False
                notified_levels.clear()

            # --- Transition: Battery → AC (power restored) ---
            elif source == "AC Power":
                logger.info(
                    "POWER RESTORED — back on AC (%s%%)", percent
                )
                # Re-warm beds to 45°C to preserve adhesion for resume
                logger.info("Re-warming beds to 45°C")
                _gcode("M140 S45")  # Sovol bed → 45°C
                _bambu_mqtt_command({"print": {"command": "gcode_line",
                                               "sequence_id": "0",
                                               "param": "M140 S45\n"}})
                notify_all(
                    "AC power restored — beds re-warming to 45°C, "
                    "printers still paused. Check and resume manually.",
                    level="info"
                )
                emergency_sent = False
                mac_reduced = False

        # --- Battery level monitoring (while on battery) ---
        if source == "Battery Power" and percent is not None:
            # Log battery level at INFO when on battery (important to track)
            logger.info("On battery: %s%% (%s remaining)",
                        percent, remaining or "unknown")

            # Milestone notifications at 75%, 50%, 25%
            for threshold in (75, 50, 25):
                if percent <= threshold and threshold not in notified_levels:
                    notified_levels.add(threshold)
                    notify_all(
                        f"UPS at {percent}% ({remaining or 'unknown'} remaining). "
                        f"Still on battery — beds warm at 45°C.",
                        level="warning" if threshold > 25 else "critical"
                    )

            # At EMERGENCY_BATTERY_PCT: kill all beds, stop Bambu, e-stop Sovol
            # Leaves remaining battery for Mac Mini clean shutdown
            if percent <= EMERGENCY_BATTERY_PCT and not emergency_sent:
                logger.critical(
                    "BATTERY CRITICAL (%s%%) — killing all heaters, "
                    "stopping printers, reserving power for Mac shutdown",
                    percent
                )
                # Kill Sovol bed + emergency stop
                _gcode("M140 S0")  # Sovol bed OFF
                emergency_stop_sovol()
                # Kill Bambu bed + stop print
                shutdown_bambu_bed()

                notify_all(
                    f"CRITICAL: UPS at {percent}% — all heaters OFF, "
                    f"printers stopped. Mac Mini will shut down at 5%."
                )
                emergency_sent = True

        # --- Bed heater power scheduling (night mode, AC only) ---
        if source == "AC Power":
            night = _is_night_hours()
            wanted = "night" if night else "day"
            if wanted != current_bed_mode:
                power = BED_POWER_NIGHT if night else BED_POWER_DAY
                logger.info("Switching bed power to %s mode (max_power=%s)",
                            wanted, power)
                if set_bed_max_power(power):
                    current_bed_mode = wanted

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
