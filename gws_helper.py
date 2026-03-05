"""
Shared helper for Google Workspace operations via the gws CLI.

Setup (one-time, interactive):
    gws auth setup
    # Or for service-account auth (headless daemons):
    # export GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE=/path/to/service-account.json

Install:
    npm install -g @googleworkspace/cli
"""

import json
import logging
import subprocess
import os

_log = logging.getLogger(__name__)

# gws binary location (npm global install on Apple Silicon / Intel Mac)
_GWS_CANDIDATES = [
    "/opt/homebrew/bin/gws",
    "/usr/local/bin/gws",
]


def _gws_bin():
    for p in _GWS_CANDIDATES:
        if os.path.isfile(p):
            return p
    return "gws"  # fall back to PATH


def _gws(*args, timeout=30):
    """Run a gws CLI command. Returns parsed JSON dict/list, or None on failure."""
    cmd = [_gws_bin()] + list(args)
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
        if result.returncode != 0:
            _log.warning("gws returned %d: %s", result.returncode, result.stderr.strip()[:200])
            return None
        return json.loads(result.stdout) if result.stdout.strip() else {}
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError) as exc:
        _log.warning("gws error: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Sheets
# ---------------------------------------------------------------------------

def create_sheet(title):
    """Create a new Google Sheet. Returns (spreadsheetId, url) or (None, None)."""
    resp = _gws(
        "sheets", "spreadsheets", "create",
        "--json", json.dumps({"properties": {"title": title}}),
    )
    if resp and "spreadsheetId" in resp:
        sid = resp["spreadsheetId"]
        return sid, f"https://docs.google.com/spreadsheets/d/{sid}"
    return None, None


def append_rows(sheet_id, range_name, rows, value_input="USER_ENTERED"):
    """Append rows to a sheet. `rows` is a list of lists.

    Args:
        sheet_id:    Google Sheets spreadsheet ID
        range_name:  e.g. "Sheet1!A1" or "Events!A:Z"
        rows:        [[cell, cell, ...], ...]
        value_input: "USER_ENTERED" (parse formulas) or "RAW"

    Returns True on success, False on failure.
    """
    params = {
        "spreadsheetId": sheet_id,
        "range": range_name,
        "valueInputOption": value_input,
    }
    resp = _gws(
        "sheets", "spreadsheets", "values", "append",
        "--params", json.dumps(params),
        "--json", json.dumps({"values": rows}),
        timeout=30,
    )
    return resp is not None


def set_header_row(sheet_id, tab_name, headers):
    """Write a header row to A1 (useful when creating a new sheet)."""
    params = {
        "spreadsheetId": sheet_id,
        "range": f"{tab_name}!A1",
        "valueInputOption": "USER_ENTERED",
    }
    resp = _gws(
        "sheets", "spreadsheets", "values", "update",
        "--params", json.dumps(params),
        "--json", json.dumps({"values": [headers]}),
        timeout=30,
    )
    return resp is not None


def sheet_url(sheet_id):
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}"


# ---------------------------------------------------------------------------
# Gmail
# ---------------------------------------------------------------------------

def send_email(to, subject, body_text, sender="me"):
    """Send a plain-text email via Gmail (requires gmail scope).

    Uses base64url-encoded RFC 2822 format required by Gmail API.
    Returns True on success.
    """
    import base64
    import email.mime.text

    msg = email.mime.text.MIMEText(body_text)
    msg["to"] = to
    msg["from"] = sender
    msg["subject"] = subject
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()

    resp = _gws(
        "gmail", "users", "messages", "send",
        "--params", json.dumps({"userId": "me"}),
        "--json", json.dumps({"raw": raw}),
        timeout=30,
    )
    return resp is not None
