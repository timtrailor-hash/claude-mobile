"""Google Docs route handlers — Flask blueprint.

Routes:
  POST /terminal-to-doc            — create a Google Doc from terminal text
  POST /google-docs-auth-start     — begin OAuth flow, return auth URL
  POST /google-docs-auth-complete  — finish OAuth flow with auth code

All three are mounted on the shared Flask app via blueprint registration
in conversation_server.py.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime

from flask import Blueprint, jsonify, request

from conv.app import _log

from .auth import (
    GOOGLE_ALL_SCOPES,
    GOOGLE_TOKEN_FILE,
    _google_auth_flow,
    _google_creds,
)

bp = Blueprint("google_docs", __name__)


@bp.route("/terminal-to-doc", methods=["POST"])
def terminal_to_doc():
    """Create a Google Doc from terminal content.

    Accepts JSON: {"text": "...", "title": "optional title"}
    Also captures tmux pane as fallback.
    Returns: {"ok": true, "doc_url": "https://docs.google.com/..."} or error.
    """
    data = request.get_json(force=True)
    text = data.get("text", "").strip()

    # Fallback: capture tmux pane content if no text provided
    if not text:
        try:
            result = subprocess.run(
                [
                    "/opt/homebrew/bin/tmux",
                    "capture-pane",
                    "-t",
                    "claude-terminal",
                    "-p",
                    "-S",
                    "-",
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            text = result.stdout.strip()
        except Exception:
            pass

    if not text:
        return jsonify({"ok": False, "error": "No text to export"}), 400

    title = (
        data.get("title")
        or f"Terminal Export {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    )

    creds = _google_creds()
    if not creds:
        # Save text locally as fallback
        export_path = f"/tmp/terminal_export_{int(time.time())}.txt"
        with open(export_path, "w") as f:
            f.write(text)
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "Google Docs auth not configured. Run /google-docs-setup first.",
                    "local_file": export_path,
                }
            ),
            503,
        )

    try:
        from googleapiclient.discovery import build

        # Create empty doc
        docs_service = build("docs", "v1", credentials=creds)
        doc = docs_service.documents().create(body={"title": title}).execute()
        doc_id = doc["documentId"]

        # Insert text content
        requests_body = [{"insertText": {"location": {"index": 1}, "text": text}}]
        docs_service.documents().batchUpdate(
            documentId=doc_id, body={"requests": requests_body}
        ).execute()

        doc_url = f"https://docs.google.com/document/d/{doc_id}/edit"
        _log.info("Created Google Doc: %s (%d chars)", doc_url, len(text))
        return jsonify({"ok": True, "doc_url": doc_url, "doc_id": doc_id})

    except Exception as e:
        _log.error("Google Docs creation failed: %s", e)
        export_path = f"/tmp/terminal_export_{int(time.time())}.txt"
        with open(export_path, "w") as f:
            f.write(text)
        return jsonify({"ok": False, "error": str(e), "local_file": export_path}), 500


@bp.route("/google-docs-auth-start", methods=["POST"])
def google_docs_auth_start():
    """Start Google Docs OAuth flow. Returns authorization URL."""
    try:
        from google_auth_oauthlib.flow import Flow

        # Read client credentials from existing token file
        with open(GOOGLE_TOKEN_FILE) as f:
            token_data = json.load(f)

        client_config = {
            "installed": {
                "client_id": token_data["client_id"],
                "client_secret": token_data["client_secret"],
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["http://localhost:1"],
            }
        }

        flow = Flow.from_client_config(client_config, scopes=GOOGLE_ALL_SCOPES)
        flow.redirect_uri = "http://localhost:1"

        auth_url, state = flow.authorization_url(
            access_type="offline",
            prompt="consent",
            include_granted_scopes="true",
        )

        # Store flow state for completion
        _google_auth_flow.update({"flow": flow, "state": state})

        return jsonify({"ok": True, "url": auth_url})

    except Exception as e:
        _log.error("google-docs-auth-start failed: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/google-docs-auth-complete", methods=["POST"])
def google_docs_auth_complete():
    """Complete Google Docs OAuth flow with authorization code."""
    data = request.get_json(force=True)
    code = data.get("code", "").strip()
    if not code:
        return jsonify({"ok": False, "error": "No code provided"}), 400

    flow = _google_auth_flow.get("flow")
    if not flow:
        return jsonify({"ok": False, "error": "No auth flow in progress"}), 400

    try:
        flow.fetch_token(code=code)
        creds = flow.credentials

        # Merge with existing token data (keep client_id, client_secret)
        try:
            with open(GOOGLE_TOKEN_FILE) as f:
                existing = json.load(f)
        except Exception:
            existing = {}

        token_data = json.loads(creds.to_json())
        token_data["client_id"] = existing.get("client_id", token_data.get("client_id"))
        token_data["client_secret"] = existing.get(
            "client_secret", token_data.get("client_secret")
        )

        # Backup old token to /tmp (security rule: backups live in /tmp,
        # never alongside the live token). Past incident 2026-04-21: a
        # Claude-authored helper wrote google_token.json.pre-rescope into
        # the repo dir and a live refresh token went untracked for 12h
        # before being caught. Fixed here for the same reason.
        if os.path.exists(GOOGLE_TOKEN_FILE):
            from pathlib import Path as _Path

            _bak = _Path("/tmp") / f"google_token.json.pre-reauth.{int(time.time())}"
            _bak.write_bytes(_Path(GOOGLE_TOKEN_FILE).read_bytes())

        with open(GOOGLE_TOKEN_FILE, "w") as f:
            json.dump(token_data, f, indent=2)

        _google_auth_flow.update({"flow": None, "state": None})
        _log.info("Google Docs auth complete — scopes: %s", token_data.get("scopes"))
        return jsonify({"ok": True, "scopes": token_data.get("scopes", [])})

    except Exception as e:
        _log.error("google-docs-auth-complete failed: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500
