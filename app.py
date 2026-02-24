#!/usr/bin/env python3
"""Claude Code Mobile — full Claude Code experience in a scrollable mobile web UI."""

import os
import sys
import json
import glob
import ssl
import subprocess
import fcntl
import time
import uuid
import threading
from datetime import datetime, timedelta, timezone
import requests as http_requests
import websocket as ws_client
from flask import Flask, request, Response, render_template_string, jsonify
from flask_sock import Sock
from werkzeug.utils import secure_filename

app = Flask(__name__)
sock = Sock(app)

# --- Request logging to file ---
import logging as _logging
_log_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'wrapper.log')
_fh = _logging.FileHandler(_log_file)
_fh.setFormatter(_logging.Formatter('%(asctime)s %(message)s', datefmt='%H:%M:%S'))
_req_log = _logging.getLogger('wrapper_requests')
_req_log.addHandler(_fh)
_req_log.setLevel(_logging.INFO)

@app.after_request
def _log_request(response):
    if request.path not in ('/', '/favicon.ico') and not request.path.startswith('/static'):
        _req_log.info(f'{request.method} {request.path} -> {response.status_code} ({request.remote_addr})')
    return response

try:
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
    from printer_config import (SOVOL_IP, SOVOL_WIFI_IP, SOVOL_CAMERA_PORT,
                                MOONRAKER_PORT, BAMBU_IP, BAMBU_SERIAL,
                                BAMBU_ACCESS_CODE, BAMBU_MQTT_PORT,
                                STREAMLIT_IP, STREAMLIT_PORT)
except ImportError:
    SOVOL_IP = "[REDACTED — see printer_config.example.py]"
    SOVOL_WIFI_IP = "[REDACTED — see printer_config.example.py]"
    SOVOL_CAMERA_PORT = 8081
    MOONRAKER_PORT = 7125
    BAMBU_IP = "[REDACTED — see printer_config.example.py]"
    BAMBU_SERIAL = "[REDACTED — see printer_config.example.py]"
    BAMBU_ACCESS_CODE = "[REDACTED — see printer_config.example.py]"
    BAMBU_MQTT_PORT = 8883
    STREAMLIT_IP = "[REDACTED — see printer_config.example.py]"
    STREAMLIT_PORT = 8501
STREAMLIT_URL = f'http://{STREAMLIT_IP}:{STREAMLIT_PORT}'

MEMORY_DIR = os.path.expanduser("~/.claude/projects/-Users-timtrailor-Documents-Claude-code/memory")
TOPICS_DIR = os.path.join(MEMORY_DIR, "topics")
WORK_DIR = os.path.expanduser("~/Documents/Claude code")
UPLOAD_DIR = os.path.expanduser("~/Documents/Claude code/claude-mobile/uploads")
# API_KEY removed — wrapper uses Claude Code subscription auth, not API billing
os.makedirs(UPLOAD_DIR, exist_ok=True)

# Store session ID for conversation continuity
current_session_id = None

# Track the current running subprocess so we can cancel it
current_proc = None
current_proc_lock = __import__('threading').Lock()

# Server-side response buffer — survives client disconnects (iOS Safari backgrounding)
# Stores the last completed response so the client can recover it on reconnect
last_response = {"text": "", "cost": "", "session_id": None, "seq": 0}
last_response_lock = threading.Lock()

# --- Restart-resilient work tracking ---
# State file persists across restarts so the new instance can reattach
WORK_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.work_state.json')

def _save_work_state(pid, output_file, session_id):
    """Save active work state to disk for recovery after restart."""
    import time as _t
    state = {'pid': pid, 'output_file': output_file,
             'session_id': session_id or '', 'started': _t.time()}
    try:
        with open(WORK_STATE_FILE, 'w') as f:
            json.dump(state, f)
    except Exception:
        pass

def _clear_work_state():
    """Remove work state file when processing completes."""
    try:
        if os.path.exists(WORK_STATE_FILE):
            os.unlink(WORK_STATE_FILE)
    except Exception:
        pass

def _load_work_state():
    """Load work state and check if the process is still alive."""
    if not os.path.exists(WORK_STATE_FILE):
        return None
    try:
        with open(WORK_STATE_FILE) as f:
            state = json.load(f)
        pid = state.get('pid')
        if not pid:
            return None
        # Check if process is still alive
        os.kill(pid, 0)
        return state
    except (OSError, json.JSONDecodeError, KeyError):
        # Process dead or bad state file — clean up
        _clear_work_state()
        return None


CHAT_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<meta http-equiv="Pragma" content="no-cache">
<meta http-equiv="Expires" content="0">
<title>Claude Code</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
    font-family: -apple-system, BlinkMacSystemFont, sans-serif;
    background: #1a1a2e;
    color: #e0e0e0;
    height: 100dvh;
    display: flex;
    flex-direction: column;
    overflow: hidden;
}
.header {
    background: #16213e;
    padding: 8px 16px;
    border-bottom: 1px solid #2a2a4a;
    display: flex;
    justify-content: space-between;
    align-items: center;
    flex-shrink: 0;
}
.header h1 { font-size: 17px; color: #c9a96e; }
.header-buttons { display: flex; gap: 6px; }
.header button {
    background: #2a2a4a; color: #aaa; border: none;
    padding: 5px 10px; border-radius: 6px; font-size: 12px;
}
/* --- Tabs --- */
.tab-bar {
    display: flex; background: #16213e; flex-shrink: 0;
    border-bottom: 1px solid #2a2a4a;
}
.tab-bar button {
    flex: 1; padding: 10px; font-size: 14px; font-weight: 600;
    background: none; color: #666; border: none;
    border-bottom: 2px solid transparent; cursor: pointer;
}
.tab-bar button.active { color: #c9a96e; border-bottom-color: #c9a96e; }
.tab-wrapper { position: relative; flex: 1; overflow: hidden; }
.tab-content { display: none !important; position: absolute; top: 0; left: 0; right: 0; bottom: 0; flex-direction: column; overflow: hidden; }
.tab-content.active { display: flex !important; }
#governorsTab.active { }
/* --- Chat tab --- */
.messages {
    flex: 1; overflow-y: auto; padding: 10px;
    -webkit-overflow-scrolling: touch;
}
.msg {
    margin-bottom: 10px; max-width: 92%; padding: 10px 14px;
    border-radius: 16px; line-height: 1.5; font-size: 14px; word-wrap: break-word;
}
.msg.user {
    background: #0a3d62; margin-left: auto;
    border-bottom-right-radius: 4px; white-space: pre-wrap;
}
.msg.assistant { background: #2a2a4a; margin-right: auto; border-bottom-left-radius: 4px; }
.msg.assistant code {
    background: #1a1a2e; padding: 1px 4px; border-radius: 3px;
    font-size: 12px; font-family: 'SF Mono', Menlo, monospace;
}
.msg.assistant pre {
    background: #1a1a2e; padding: 8px; border-radius: 6px; overflow-x: auto;
    margin: 6px 0; font-size: 12px; white-space: pre-wrap; word-wrap: break-word;
}
.msg.assistant pre code { background: none; padding: 0; }
.msg.system {
    background: #1a3a2a; margin: 0 auto; text-align: center;
    font-size: 12px; color: #7a7; padding: 6px 12px;
}
#workingBar {
    display: none; background: #1a2a1a; border-top: 1px solid #2a4a2a;
    padding: 6px 14px; font-size: 12px; color: #c9a96e;
    text-align: center; flex-shrink: 0;
    animation: fadeInBar 0.2s ease;
}
@keyframes fadeInBar { from { opacity: 0; } to { opacity: 1; } }
.tool-indicator {
    margin-bottom: 6px; padding: 5px 12px; font-size: 12px; color: #c9a96e;
    background: #1a1a3a; border-left: 2px solid #c9a96e;
    border-radius: 0 8px 8px 0; max-width: 92%;
    font-family: 'SF Mono', Menlo, monospace;
}
.queue-remove {
    background: #e55 !important; color: #fff !important; border: none !important;
    border-radius: 50% !important; width: 22px !important; height: 22px !important;
    font-size: 14px !important; cursor: pointer; vertical-align: middle;
    margin-left: 4px; padding: 0 !important; line-height: 22px !important;
    display: inline-block !important;
}
.queue-text { cursor: pointer; text-decoration: underline; text-decoration-style: dotted;
    text-underline-offset: 3px; text-decoration-color: #c9a96e; }
.input-area {
    padding: 8px 12px; padding-bottom: max(8px, env(safe-area-inset-bottom));
    background: #16213e; border-top: 1px solid #2a2a4a;
    display: flex; gap: 8px; flex-shrink: 0;
}
.input-area textarea {
    flex: 1; background: #2a2a4a; color: #e0e0e0;
    border: 1px solid #3a3a5a; border-radius: 20px;
    padding: 10px 16px; font-size: 16px; font-family: inherit;
    resize: none; max-height: 120px; line-height: 1.4;
}
.input-area textarea:focus { outline: none; border-color: #c9a96e; }
.input-area button {
    background: #c9a96e; color: #1a1a2e; border: none;
    border-radius: 50%; width: 42px; height: 42px;
    font-size: 20px; font-weight: bold; flex-shrink: 0; cursor: pointer;
}
.input-area button:disabled { opacity: 0.4; }
.input-area button.cancel-btn {
    background: #e55 !important; color: #fff !important; font-size: 16px !important;
}
.attach-btn {
    background: #2a2a4a !important; color: #c9a96e !important;
    font-size: 22px !important; border: 1px solid #3a3a5a !important;
}
.image-preview {
    padding: 4px 12px; background: #16213e; flex-shrink: 0; display: none;
}
.image-preview img { max-height: 80px; border-radius: 8px; border: 1px solid #3a3a5a; }
.image-preview .remove-img {
    background: #e55; color: #fff; border: none; border-radius: 50%;
    width: 20px; height: 20px; font-size: 12px; cursor: pointer;
    position: relative; top: -34px; left: -12px; line-height: 20px; text-align: center;
}
.quick-actions {
    display: flex; gap: 6px; padding: 6px 12px;
    background: #16213e; overflow-x: auto; flex-shrink: 0;
}
.quick-actions button {
    background: #2a2a4a; color: #c9a96e;
    border: 1px solid #3a3a5a; padding: 5px 10px;
    border-radius: 16px; font-size: 12px; white-space: nowrap; cursor: pointer;
}
.cost { font-size: 10px; color: #666; text-align: center; margin-top: 4px; }
/* --- Printers tab --- */
.printer-dash {
    flex: 1; overflow-y: auto; padding: 12px;
    -webkit-overflow-scrolling: touch;
}
.printer-dash .refresh-bar {
    display: flex; justify-content: space-between; align-items: center;
    margin-bottom: 10px;
}
.printer-dash .refresh-bar span { font-size: 11px; color: #666; }
.printer-dash .refresh-bar button {
    background: #2a2a4a; color: #c9a96e; border: 1px solid #3a3a5a;
    padding: 6px 14px; border-radius: 16px; font-size: 12px; cursor: pointer;
}
.printer-card {
    background: #16213e; border-radius: 12px; padding: 14px;
    margin-bottom: 12px; border: 1px solid #2a2a4a;
}
.printer-card .card-header {
    display: flex; align-items: center; gap: 8px; margin-bottom: 8px;
}
.printer-card .card-header h2 { font-size: 15px; font-weight: 600; }
.printer-card .state-badge {
    font-size: 11px; padding: 2px 8px; border-radius: 10px;
}
.printer-card .model-name {
    font-size: 14px; font-weight: 600; color: #e0e0e0; margin-bottom: 6px;
}
.printer-card .media-row {
    display: flex; gap: 8px; margin: 8px 0; overflow-x: auto;
}
.printer-card .media-row img {
    border-radius: 8px; border: 1px solid #3a3a5a;
}
.printer-card .progress-wrap { margin: 8px 0; }
.printer-card .progress-label {
    display: flex; justify-content: space-between;
    font-size: 11px; color: #888; margin-bottom: 2px;
}
.printer-card .progress-bar {
    background: #1a1a2e; border-radius: 4px; height: 14px; overflow: hidden;
}
.printer-card .progress-fill {
    height: 100%; border-radius: 4px; transition: width 0.3s;
}
.printer-card .detail-grid {
    display: grid; grid-template-columns: 1fr 1fr;
    gap: 3px 12px; font-size: 12px; margin-top: 6px;
}
.printer-card .detail-grid .lbl { color: #888; }
.printer-card .detail-grid .val-warn { color: #c9a96e; }
.printer-card .detail-grid .val-good { color: #4a7; }
.printer-card .ams-row {
    margin-top: 8px; font-size: 11px; color: #888;
}
.printer-card .ams-dot {
    display: inline-block; width: 16px; height: 16px; border-radius: 50%;
    border: 1px solid #555; vertical-align: middle; margin: 0 2px;
}
.printer-card .offline {
    color: #888; font-style: italic; padding: 20px 0; text-align: center;
}
.printer-loading {
    text-align: center; padding: 40px; color: #888; font-size: 14px;
}
/* --- Work tab --- */
.work-dash {
    flex: 1; overflow-y: auto; padding: 0;
    -webkit-overflow-scrolling: touch;
    display: flex; flex-direction: column;
}
.work-subtabs {
    display: flex; padding: 8px 12px 0; gap: 0; flex-shrink: 0;
    border-bottom: 1px solid #2a2a4a;
}
.work-subtab {
    flex: 1; padding: 8px 0; text-align: center; font-size: 13px; font-weight: 600;
    color: #666; background: none; border: none; border-bottom: 2px solid transparent;
    cursor: pointer; transition: color 0.2s, border-color 0.2s;
}
.work-subtab.active { color: #c9a96e; border-bottom-color: #c9a96e; }
.work-subcontent { flex: 1; overflow-y: auto; padding: 12px; -webkit-overflow-scrolling: touch; }
.work-section { margin-bottom: 16px; }
.work-section h3 {
    font-size: 13px; color: #c9a96e; text-transform: uppercase;
    letter-spacing: 0.5px; margin-bottom: 8px; padding-bottom: 4px;
    border-bottom: 1px solid #2a2a4a;
}
.work-card {
    background: #16213e; border-radius: 10px; padding: 12px;
    margin-bottom: 8px; border: 1px solid #2a2a4a;
    cursor: pointer; transition: background 0.15s, border-color 0.15s;
    -webkit-tap-highlight-color: rgba(201,169,110,0.15);
}
.work-card:active { background: #1e2d50; border-color: #3a3a5a; }
.work-card .wc-acct {
    display: inline-block; font-size: 10px; font-weight: 600; padding: 1px 6px;
    border-radius: 8px; margin-bottom: 4px; text-transform: uppercase; letter-spacing: 0.3px;
}
.work-card .wc-from {
    font-size: 13px; font-weight: 600; color: #e0e0e0; margin-bottom: 2px;
}
.work-card .wc-subject {
    font-size: 14px; color: #fff; font-weight: 500; margin-bottom: 4px;
}
.work-card .wc-snippet {
    font-size: 12px; color: #888; line-height: 1.4;
    display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
}
.work-card .wc-time {
    font-size: 11px; color: #666; margin-top: 4px;
}
.work-card.cal-card .wc-title {
    font-size: 14px; font-weight: 600; color: #e0e0e0; margin-bottom: 2px;
}
.work-card.cal-card .wc-when {
    font-size: 13px; color: #52b788; margin-bottom: 2px;
}
.work-card.cal-card .wc-location {
    font-size: 12px; color: #888;
}
.work-card.cal-card .wc-attendees {
    font-size: 11px; color: #666; margin-top: 4px;
}
.work-card.slack-card .wc-channel {
    font-size: 12px; color: #a78bfa; font-weight: 600; margin-bottom: 2px;
}
.work-card.slack-card .wc-user {
    font-size: 13px; font-weight: 600; color: #e0e0e0; margin-bottom: 2px;
}
.work-card.slack-card .wc-text {
    font-size: 13px; color: #ccc; line-height: 1.4;
    display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden;
}
.work-card.slack-card .wc-replies {
    font-size: 11px; color: #52b788; margin-top: 4px;
}
.work-search-bar {
    display: flex; gap: 8px; padding: 10px 12px; flex-shrink: 0;
    background: #16213e; border-bottom: 1px solid #2a2a4a;
}
.work-search-bar input {
    flex: 1; background: #2a2a4a; color: #e0e0e0;
    border: 1px solid #3a3a5a; border-radius: 20px;
    padding: 8px 16px; font-size: 14px; font-family: inherit;
}
.work-search-bar input:focus { outline: none; border-color: #c9a96e; }
.work-search-bar input::placeholder { color: #555; }
.work-search-bar button {
    background: #c9a96e; color: #1a1a2e; border: none;
    border-radius: 20px; padding: 8px 16px; font-size: 13px; font-weight: 600;
    cursor: pointer; flex-shrink: 0;
}
.work-search-results { padding: 12px; }
.work-search-results .search-group { margin-bottom: 16px; }
.work-search-results .search-group h3 {
    font-size: 12px; color: #c9a96e; text-transform: uppercase;
    letter-spacing: 0.5px; margin-bottom: 8px;
}
.work-setup {
    text-align: center; padding: 40px 20px; color: #888; font-size: 13px; line-height: 1.6;
}
.work-setup code {
    background: #1a1a2e; padding: 2px 6px; border-radius: 4px;
    font-size: 12px; font-family: 'SF Mono', Menlo, monospace; color: #c9a96e;
}
</style>
</head>
<body>
<div class="header">
    <h1>Claude Code</h1>
    <div class="header-buttons">
        <button onclick="fetch('/restart',{method:'POST'}).then(()=>setTimeout(()=>window.location.reload(true),2000))">&#8635;</button>
        <button onclick="newSession()">New</button>
    </div>
</div>

<!-- Tab bar -->
<div class="tab-bar">
    <button id="tabChat" class="active" onclick="switchTab('chat')">Chat</button>
    <button id="tabPrinters" onclick="switchTab('printers')">Printers</button>
    <button id="tabGovernors" onclick="switchTab('governors')">Governors</button>
    <button id="tabWork" onclick="switchTab('work')">Work</button>
</div>

<div class="tab-wrapper">
<!-- Chat tab -->
<div id="chatTab" class="tab-content active">
    <div class="quick-actions"></div>
    <div class="messages" id="messages"></div>
    <div class="image-preview" id="imagePreview"></div>
    <input type="file" id="imageInput" accept="image/*" multiple style="display:none" onchange="handleImageSelect(this)">
    <div id="workingBar"></div>
    <div class="input-area">
        <button class="attach-btn" onclick="document.getElementById('imageInput').click()">&#128247;</button>
        <textarea id="input" rows="1" placeholder="Ask anything..."
            oninput="autoResize(this)" onkeydown="handleKey(event)"></textarea>
        <button id="sendBtn" onclick="handleSendOrQueue()">&#8593;</button>
    </div>
</div>

<!-- Printers tab -->
<div id="printersTab" class="tab-content">
    <div class="printer-dash" id="printerDash">
        <div class="printer-loading" id="printerLoading">Loading printer status...</div>
    </div>
</div>

<!-- Governors tab -->
<div id="governorsTab" class="tab-content">
    <iframe id="governorsFrame" style="width:100%;height:100%;border:none;background:#1a1a2e;"
            allow="microphone"></iframe>
</div>

<!-- Work tab -->
<div id="workTab" class="tab-content">
    <div style="font-size:10px;color:#555;text-align:right;padding:2px 8px;" id="workDebug">v3</div>
    <div class="work-dash" id="workDash">
        <div class="printer-loading" id="workLoading">Loading work status...</div>
    </div>
    <div class="work-dash" id="workDashReady" style="display:none;">
        <div class="work-search-bar">
            <input type="text" id="workSearchInput" placeholder="Search emails, calendar, slack..." onkeydown="if(event.key==='Enter')window.workSearch()">
            <button onclick="window.workSearch()">Search</button>
        </div>
        <div class="work-search-results" id="workSearchResults" style="display:none;overflow-y:auto;flex:1;-webkit-overflow-scrolling:touch;"></div>
        <div class="work-subtabs" id="workSubtabBar">
            <button class="work-subtab active" id="wsEmails" onclick="window.switchWorkSubtab('emails')">Emails</button>
            <button class="work-subtab" id="wsDiary" onclick="window.switchWorkSubtab('diary')">Diary</button>
            <button class="work-subtab" id="wsSlack" onclick="window.switchWorkSubtab('slack')">Slack</button>
        </div>
        <div class="work-subcontent" id="workEmails"></div>
        <div class="work-subcontent" id="workDiary" style="display:none;"></div>
        <div class="work-subcontent" id="workSlack" style="display:none;"></div>
        <div id="workAiChat" style="display:none;border-top:1px solid #3a3a5a;margin-top:8px;padding-top:8px;">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;">
                <span style="font-size:12px;color:#a78bfa;font-weight:600;">Claude</span>
                <button onclick="document.getElementById('workAiChat').style.display='none'" style="background:none;border:none;color:#666;font-size:16px;cursor:pointer;padding:0 4px;">&times;</button>
            </div>
            <div id="workAiContent" style="font-size:13px;color:#ddd;line-height:1.5;max-height:300px;overflow-y:auto;-webkit-overflow-scrolling:touch;"></div>
        </div>
    </div>
</div>

</div><!-- /tab-wrapper -->

<!-- ====== CHAT JS (isolated) ====== -->
<script>
(function() {
    var _tabHidden = false; // Track tab visibility for recovery logic
    var messagesEl = document.getElementById('messages');
    var inputEl = document.getElementById('input');
    var sendBtn = document.getElementById('sendBtn');
    var sending = false;
    var sessionId = null;
    var sessionInited = false;
    var pendingImages = [];
    var messageQueue = [];
    var currentAbortController = null;
    var wasCancelled = false;
    var workingTimer = null;
    var workingDiv = null;
    var workingStart = 0;
    var lastActivityTime = 0;
    var lastToolTime = 0;
    var lastToolLabel = '';

    var workingBarEl = document.getElementById('workingBar');

    function startWorkingIndicator() {
        workingStart = Date.now();
        lastActivityTime = workingStart;
        lastToolTime = workingStart;
        lastToolLabel = '';
        workingBarEl.textContent = 'Working...';
        workingBarEl.style.display = 'block';
        workingDiv = true; // flag for compat
        workingTimer = setInterval(function() {
            if (!workingDiv) return;
            var elapsed = Math.floor((Date.now() - workingStart) / 1000);
            var toolIdle = Math.floor((Date.now() - lastToolTime) / 1000);
            var connIdle = Math.floor((Date.now() - lastActivityTime) / 1000);
            var mins = Math.floor(elapsed / 60);
            var secs = elapsed % 60;
            var timeStr = mins > 0 ? mins + 'm ' + secs + 's' : secs + 's';
            var dots = ['', '.', '..', '...'][Math.floor(Date.now() / 600) % 4];
            var label;
            if (connIdle > 25) {
                label = 'Reconnecting';
            } else if (lastToolLabel && toolIdle < 15) {
                label = lastToolLabel;
            } else {
                label = elapsed > 10 ? 'Processing' : 'Working';
            }
            workingBarEl.innerHTML = '<span style="color:#c9a96e;">' + escapeHtml(label) + dots + ' <span style="opacity:0.5">' + timeStr + '</span></span>';
        }, 2000);
    }

    function touchWorkingIndicator(toolLabel) {
        lastActivityTime = Date.now();
        if (toolLabel) {
            lastToolLabel = toolLabel;
            lastToolTime = Date.now();
        }
        if (workingDiv && toolLabel) {
            var elapsed = Math.floor((Date.now() - workingStart) / 1000);
            var mins = Math.floor(elapsed / 60);
            var secs = elapsed % 60;
            var timeStr = mins > 0 ? mins + 'm ' + secs + 's' : secs + 's';
            workingBarEl.innerHTML = '<span style="color:#c9a96e;">' + escapeHtml(toolLabel) + ' <span style="opacity:0.5">' + timeStr + '</span></span>';
        }
    }

    function stopWorkingIndicator() {
        if (workingTimer) { clearInterval(workingTimer); workingTimer = null; }
        workingBarEl.style.display = 'none';
        workingBarEl.textContent = '';
        workingDiv = null;
        lastToolLabel = '';
    }

    function autoResize(el) {
        el.style.height = 'auto';
        el.style.height = Math.min(el.scrollHeight, 120) + 'px';
    }
    window.autoResize = autoResize;

    function handleKey(e) {
        if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSendOrQueue(); }
    }
    window.handleKey = handleKey;

    var userScrolledUp = false;
    messagesEl.addEventListener('scroll', function() {
        var atBottom = messagesEl.scrollHeight - messagesEl.scrollTop - messagesEl.clientHeight < 80;
        userScrolledUp = !atBottom;
    });
    function scrollToBottom(force) {
        if (!force && userScrolledUp) return;
        messagesEl.scrollTop = messagesEl.scrollHeight;
    }

    function escapeHtml(text) {
        return text.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    }
    window.escapeHtml = escapeHtml;

    function formatOutput(text) {
        var html = escapeHtml(text);
        html = html.replace(/```(\\w*)\\n([\\s\\S]*?)```/g, '<pre><code>$2</code></pre>');
        html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
        html = html.replace(/\\*\\*(.+?)\\*\\*/g, '<strong>$1</strong>');
        html = html.replace(/\\n/g, '<br>');
        return html;
    }

    function addMsg(cls, html, forceScroll) {
        var div = document.createElement('div');
        div.className = cls;
        div.innerHTML = html;
        messagesEl.appendChild(div);
        scrollToBottom(forceScroll);
        return div;
    }

    function setButtonMode(mode) {
        if (mode === 'cancel') {
            sendBtn.innerHTML = '&#9632;';
            sendBtn.className = 'cancel-btn';
            sendBtn.disabled = false;
            sendBtn.onclick = cancelRequest;
        } else {
            sendBtn.innerHTML = '&#8593;';
            sendBtn.className = '';
            sendBtn.disabled = false;
            sendBtn.onclick = handleSendOrQueue;
        }
    }

    function handleSendOrQueue() {
        var text = inputEl.value.trim();
        if (!text) return;
        if (sending) {
            var qIdx = messageQueue.length;
            var queuedDiv = addMsg('msg user',
                '<span class="queue-text" data-queue-idx="' + qIdx + '">' +
                escapeHtml(text) + '</span>' +
                ' <span class="queue-tag" style="color:#c9a96e;font-size:11px;">[queued]</span>' +
                ' <button class="queue-remove" data-queue-idx="' + qIdx + '">&times;</button>');
            messageQueue.push({text: text, images: pendingImages.slice(), div: queuedDiv, idx: qIdx});
            inputEl.value = '';
            autoResize(inputEl);
            clearAllImages();
            return;
        }
        inputEl.value = '';
        autoResize(inputEl);
        sendMessage(text, pendingImages.slice());
        clearAllImages();
    }
    window.handleSendOrQueue = handleSendOrQueue;

    function cancelRequest() {
        if (!sending) return;
        wasCancelled = true;
        if (currentAbortController) currentAbortController.abort();
        try { fetch('/cancel', {method: 'POST'}); } catch(e) {}
        addMsg('msg system', '<span style="color:#c9a96e;">Cancelled</span>');
        // Force reset — Safari may not fire .finally() after abort
        setTimeout(finishSend, 300);
    }

    var staleTimer = null;
    function finishSend() {
        if (!sending) return;
        sending = false;
        stopWorkingIndicator();
        currentAbortController = null;
        setButtonMode('send');
        inputEl.focus();
        if (staleTimer) { clearInterval(staleTimer); staleTimer = null; }
        if (messageQueue.length > 0) {
            var next = messageQueue.shift();
            var tag = next.div && next.div.querySelector('.queue-tag');
            if (tag) tag.remove();
            setTimeout(function() { sendMessage(next.text, next.images, true); }, 300);
        }
    }

    function sendMessage(text, images, fromQueue) {
        sending = true;
        wasCancelled = false;
        setButtonMode('cancel');
        startWorkingIndicator();
        // Safety: if no data (not even keepalives) for 120s, recover from stuck state.
        // Server sends data keepalives every 2s, but Safari batches/throttles SSE delivery
        // so pings may not arrive for extended periods. 120s is safe — if truly no data
        // for 2 full minutes, the connection is dead. Skip while tab is hidden.
        if (staleTimer) clearInterval(staleTimer);
        staleTimer = setInterval(function() {
            if (!sending) { clearInterval(staleTimer); staleTimer = null; return; }
            if (_tabHidden) return; // Don't fire while backgrounded — visibilitychange handles it
            var idle = Date.now() - lastActivityTime;
            if (idle > 120000) {
                // Try to recover missed response before giving up
                fetch('/last-response').then(function(r) { return r.json(); }).then(function(data) {
                    if (data.text && data.seq > lastSeenSeq) {
                        lastSeenSeq = data.seq;
                        var div = addMsg('msg assistant', '', true);
                        div.innerHTML = formatOutput(data.text);
                        if (data.cost) addMsg('cost', data.cost);
                        scrollToBottom(true);
                    }
                }).catch(function() {});
                finishSend();
            }
        }, 10000);

        if (!fromQueue) {
            var displayText = text;
            if (images && images.length) displayText += ' [' + images.length + ' image(s)]';
            addMsg('msg user', escapeHtml(displayText), true);
        }

        var assistantDiv = null;
        var fullText = '';
        var gotData = false;
        var hadToolOutput = false;
        currentAbortController = new AbortController();

        var body = {message: text};
        if (sessionId) body.session_id = sessionId;
        if (images && images.length) body.image_paths = images.map(function(img) { return img.path; });

        fetch('/chat', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(body),
            signal: currentAbortController.signal
        }).then(function(response) {
            var reader = response.body.getReader();
            var decoder = new TextDecoder();
            var buffer = '';

            function readChunk() {
                return reader.read().then(function(result) {
                    if (result.done) { finishSend(); return; }
                    // Any incoming data = connection alive, reset stale timer
                    lastActivityTime = Date.now();
                    buffer += decoder.decode(result.value, {stream: true});
                    var lines = buffer.split('\\n');
                    buffer = lines.pop();
                    for (var i = 0; i < lines.length; i++) {
                        if (!lines[i].startsWith('data: ')) continue;
                        try {
                            var data = JSON.parse(lines[i].slice(6));
                            gotData = true;
                            if (data.type === 'init') {
                                touchWorkingIndicator();
                                sessionId = data.session_id;
                                if (!sessionInited) {
                                    sessionInited = true;
                                    addMsg('msg system', 'Connected (' + escapeHtml(data.model) + ')');
                                }
                            } else if (data.type === 'tool') {
                                hadToolOutput = true;
                                touchWorkingIndicator(data.content);
                            } else if (data.type === 'activity') {
                                touchWorkingIndicator();
                            } else if (data.type === 'text') {
                                if (!assistantDiv) assistantDiv = addMsg('msg assistant', '');
                                fullText += data.content;
                                assistantDiv.innerHTML = formatOutput(fullText);
                                scrollToBottom();
                            } else if (data.type === 'cost') {
                                addMsg('cost', data.content);
                                // Cost is always the final event — call directly
                                // AND via timeout (belt + suspenders for Safari)
                                finishSend();
                                setTimeout(finishSend, 500);
                            } else if (data.type === 'ping') {
                                touchWorkingIndicator();
                            } else if (data.type === 'error') {
                                addMsg('msg system', '<span style="color:#e55">' + escapeHtml(data.content) + '</span>');
                            }
                        } catch(e) {}
                    }
                    return readChunk();
                }).catch(function(readErr) {
                    // Stream read failed — Safari drops streams under memory pressure
                    // or background throttling. Don't kill the session — try to reattach.
                    if (wasCancelled || (readErr && readErr.name === 'AbortError')) {
                        finishSend();
                        return;
                    }
                    // Check if Claude is still working before giving up
                    fetch('/pending-work').then(function(r) { return r.json(); }).then(function(pw) {
                        if (pw.active) {
                            reattachToActiveWork();
                        } else {
                            // Process finished while stream was broken — recover final response
                            fetch('/last-response').then(function(r) { return r.json(); }).then(function(lr) {
                                if (lr.text && lr.seq > lastSeenSeq) {
                                    lastSeenSeq = lr.seq;
                                    var div = addMsg('msg assistant', '', true);
                                    div.innerHTML = formatOutput(lr.text);
                                    if (lr.cost) addMsg('cost', lr.cost);
                                    scrollToBottom(true);
                                }
                            }).catch(function() {});
                            finishSend();
                        }
                    }).catch(function() { finishSend(); });
                });
            }
            return readChunk();
        }).catch(function(e) {
            // Fetch itself failed (not stream read) — network error
            if (wasCancelled || (e && e.name === 'AbortError')) return;
            if (!gotData) {
                addMsg('msg system', '<span style="color:#e55">Connection error</span>');
            }
            // Still try to reattach in case Claude is running
            fetch('/pending-work').then(function(r) { return r.json(); }).then(function(pw) {
                if (pw.active) { reattachToActiveWork(); }
                else { finishSend(); }
            }).catch(function() { finishSend(); });
        });
    }

    function sendQuick(text) { inputEl.value = text; handleSendOrQueue(); }
    window.sendQuick = sendQuick;

    function clearChat() {
        messagesEl.innerHTML = '';
        addMsg('msg system', 'Display cleared. Session continues.');
    }
    window.clearChat = clearChat;

    function newSession() {
        sessionId = null;
        sessionInited = false;
        messageQueue = [];
        messagesEl.innerHTML = '';
        fetch('/new-session', {method: 'POST'});
        addMsg('msg system', 'New session started.');
    }
    window.newSession = newSession;

    function handleImageSelect(input) {
        var files = Array.from(input.files);
        if (!files.length) return;
        for (var i = 0; i < files.length; i++) {
            (function(file) {
                var formData = new FormData();
                formData.append('image', file);
                fetch('/upload', { method: 'POST', body: formData })
                    .then(function(r) { return r.json(); })
                    .then(function(data) {
                        if (data.path) {
                            pendingImages.push({path: data.path, name: file.name, dataUrl: URL.createObjectURL(file)});
                            renderPreviews();
                        }
                    })
                    .catch(function() { addMsg('msg system', '<span style="color:#e55">Upload failed</span>'); });
            })(files[i]);
        }
        input.value = '';
    }
    window.handleImageSelect = handleImageSelect;

    function renderPreviews() {
        var previewEl = document.getElementById('imagePreview');
        if (!pendingImages.length) { previewEl.innerHTML = ''; previewEl.style.display = 'none'; return; }
        var html = '';
        for (var i = 0; i < pendingImages.length; i++) {
            html += '<span style="display:inline-block;position:relative;margin-right:6px;">';
            html += '<img src="' + pendingImages[i].dataUrl + '">';
            html += '<button class="remove-img" onclick="removeImage(' + i + ')">X</button></span>';
        }
        previewEl.innerHTML = html;
        previewEl.style.display = 'block';
    }

    function removeImage(idx) { pendingImages.splice(idx, 1); renderPreviews(); }
    window.removeImage = removeImage;

    function clearAllImages() { pendingImages = []; renderPreviews(); }

    // Queue management: tap text to edit, tap X to remove
    messagesEl.addEventListener('click', function(e) {
        var removeBtn = e.target.closest('.queue-remove');
        var queueText = e.target.closest('.queue-text');
        if (removeBtn) {
            var idx = parseInt(removeBtn.getAttribute('data-queue-idx'));
            for (var i = 0; i < messageQueue.length; i++) {
                if (messageQueue[i].idx === idx) {
                    if (messageQueue[i].div) messageQueue[i].div.remove();
                    messageQueue.splice(i, 1);
                    break;
                }
            }
        } else if (queueText) {
            var idx2 = parseInt(queueText.getAttribute('data-queue-idx'));
            for (var j = 0; j < messageQueue.length; j++) {
                if (messageQueue[j].idx === idx2) {
                    inputEl.value = messageQueue[j].text;
                    autoResize(inputEl);
                    if (messageQueue[j].div) messageQueue[j].div.remove();
                    messageQueue.splice(j, 1);
                    inputEl.focus();
                    break;
                }
            }
        }
    });

    addMsg('msg system', 'Claude Code Mobile ready.');
    setButtonMode('send');

    // Recovery: if user taps input area while cancel is stuck, reset after 60s idle.
    // Uses touchstart on the whole input-area div (more reliable than focus on
    // iOS — focus won't re-fire if textarea is already focused).
    // 60s threshold ensures this only fires when the stream is genuinely dead,
    // not during normal pauses between SSE deliveries.
    function recoverIfStuck() {
        if (sending && Date.now() - lastActivityTime > 60000) {
            finishSend();
        }
    }
    inputEl.addEventListener('focus', recoverIfStuck);
    document.querySelector('.input-area').addEventListener('touchstart', recoverIfStuck);
    inputEl.addEventListener('input', recoverIfStuck);

    // --- Recovery: reconnect to active stream when returning from background ---
    // ALWAYS check /pending-work on tab return, regardless of sending state.
    // The stale timer may have already fired (setting sending=false) before
    // visibilitychange runs — so we can't rely on sending to decide.
    var lastSeenSeq = 0;

    document.addEventListener('visibilitychange', function() {
        if (document.visibilityState !== 'visible') {
            _tabHidden = true;
            return;
        }
        if (!_tabHidden) return; // Only recover after genuine background
        _tabHidden = false;

        // Always check if Claude is still working — stale timer may have
        // already called finishSend() while we were away
        fetch('/pending-work').then(function(r) { return r.json(); }).then(function(pw) {
            if (pw.active) {
                // Process still running — kill any dead stream and reattach
                if (currentAbortController) currentAbortController.abort();
                reattachToActiveWork();
            } else {
                // Process finished while we were away — grab the result
                fetch('/last-response').then(function(r) { return r.json(); }).then(function(data) {
                    if (!data.text || data.seq <= lastSeenSeq) return;
                    var msgs = document.querySelectorAll('.msg.assistant');
                    var lastMsg = msgs.length ? msgs[msgs.length - 1].textContent : '';
                    if (lastMsg && data.text.substring(0, 80) === lastMsg.substring(0, 80)) {
                        lastSeenSeq = data.seq;
                        return;
                    }
                    lastSeenSeq = data.seq;
                    var div = addMsg('msg assistant', '', true);
                    div.innerHTML = formatOutput(data.text);
                    if (data.cost) addMsg('cost', data.cost);
                    scrollToBottom(true);
                    finishSend();
                }).catch(function() {});
            }
        }).catch(function() {});
    });
    // Also track seq when we receive responses normally
    var origFinish = finishSend;
    // Update lastSeenSeq periodically when actively receiving
    setInterval(function() {
        fetch('/last-response').then(function(r) { return r.json(); }).then(function(data) {
            if (data.seq > lastSeenSeq) lastSeenSeq = data.seq;
        }).catch(function() {});
    }, 30000);

    // --- Restart recovery: reattach to active Claude process after wrapper restart ---
    function reattachToActiveWork() {
        fetch('/pending-work').then(function(r) { return r.json(); }).then(function(data) {
            if (!data.active) {
                // Process not running — recover last response if we missed it
                if (sending) {
                    fetch('/last-response').then(function(r) { return r.json(); }).then(function(lr) {
                        if (lr.text && lr.seq > lastSeenSeq) {
                            lastSeenSeq = lr.seq;
                            var div = addMsg('msg assistant', '', true);
                            div.innerHTML = formatOutput(lr.text);
                            if (lr.cost) addMsg('cost', lr.cost);
                            scrollToBottom(true);
                        }
                    }).catch(function() {}).finally(function() { finishSend(); });
                }
                return;
            }

            // There's a Claude process still running — reattach to it
            sending = true;
            setButtonMode('cancel');
            startWorkingIndicator();
            if (data.session_id) sessionId = data.session_id;

            var assistantDiv = null;
            var fullText = '';
            currentAbortController = new AbortController();

            if (staleTimer) clearInterval(staleTimer);
            staleTimer = setInterval(function() {
                if (!sending) { clearInterval(staleTimer); staleTimer = null; return; }
                if (_tabHidden) return;
                var idle = Date.now() - lastActivityTime;
                if (idle > 120000) { finishSend(); }
            }, 10000);

            fetch('/reattach', {signal: currentAbortController.signal}).then(function(response) {
                var reader = response.body.getReader();
                var decoder = new TextDecoder();
                var buffer = '';

                function readChunk() {
                    return reader.read().then(function(result) {
                        if (result.done) { finishSend(); return; }
                        lastActivityTime = Date.now();
                        buffer += decoder.decode(result.value, {stream: true});
                        var lines = buffer.split('\\n');
                        buffer = lines.pop();
                        for (var i = 0; i < lines.length; i++) {
                            if (!lines[i].startsWith('data: ')) continue;
                            try {
                                var d = JSON.parse(lines[i].slice(6));
                                if (d.type === 'init') {
                                    touchWorkingIndicator();
                                    if (d.session_id) sessionId = d.session_id;
                                } else if (d.type === 'tool') {
                                    touchWorkingIndicator(d.content);
                                } else if (d.type === 'activity' || d.type === 'ping') {
                                    touchWorkingIndicator();
                                } else if (d.type === 'snapshot') {
                                    // Snapshot = accumulated text so far — replace, don't append
                                    // Find existing assistant msg or create one
                                    var msgs = document.querySelectorAll('.msg.assistant');
                                    var lastMsg = msgs.length ? msgs[msgs.length - 1] : null;
                                    if (lastMsg && lastMsg.textContent.substring(0, 80) === d.content.substring(0, 80)) {
                                        // Already showing this — just update in place
                                        assistantDiv = lastMsg;
                                    } else {
                                        assistantDiv = addMsg('msg assistant', '');
                                    }
                                    fullText = d.content;
                                    assistantDiv.innerHTML = formatOutput(fullText);
                                    scrollToBottom();
                                } else if (d.type === 'text') {
                                    if (!assistantDiv) assistantDiv = addMsg('msg assistant', '');
                                    fullText += d.content;
                                    assistantDiv.innerHTML = formatOutput(fullText);
                                    scrollToBottom();
                                } else if (d.type === 'cost') {
                                    addMsg('cost', d.content);
                                    finishSend();
                                } else if (d.type === 'error') {
                                    addMsg('msg system', '<span style="color:#e55">' + escapeHtml(d.content) + '</span>');
                                }
                            } catch(e) {}
                        }
                        return readChunk();
                    }).catch(function(readErr) {
                        // Reattach stream broke — check if process is still alive
                        if (readErr && readErr.name === 'AbortError') { finishSend(); return; }
                        fetch('/pending-work').then(function(r) { return r.json(); }).then(function(pw) {
                            if (pw.active) {
                                // Still running — one more reattach attempt
                                setTimeout(function() { reattachToActiveWork(); }, 1000);
                            } else {
                                // Done — grab final response
                                fetch('/last-response').then(function(r) { return r.json(); }).then(function(lr) {
                                    if (lr.text && lr.seq > lastSeenSeq) {
                                        lastSeenSeq = lr.seq;
                                        var div = addMsg('msg assistant', '', true);
                                        div.innerHTML = formatOutput(lr.text);
                                        if (lr.cost) addMsg('cost', lr.cost);
                                        scrollToBottom(true);
                                    }
                                }).catch(function() {});
                                finishSend();
                            }
                        }).catch(function() { finishSend(); });
                    });
                }
                return readChunk();
            }).catch(function(e) {
                if (e && e.name === 'AbortError') { finishSend(); return; }
                // Fetch to /reattach failed — try recovering
                fetch('/pending-work').then(function(r) { return r.json(); }).then(function(pw) {
                    if (pw.active) { setTimeout(function() { reattachToActiveWork(); }, 1000); }
                    else { finishSend(); }
                }).catch(function() { finishSend(); });
            });
        }).catch(function() {});
    }
    // Run on page load
    reattachToActiveWork();
})();
</script>

<!-- ====== PRINTERS JS (isolated — errors here won't break chat) ====== -->
<script>
(function() {
    var dashEl = document.getElementById('printerDash');
    var autoTimer = null;
    var lastUpdate = null;

    function esc(text) {
        if (!text && text !== 0) return '';
        return String(text).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    }

    function stateColor(state) {
        var s = (state || '').toLowerCase();
        if (s === 'printing' || s === 'running') return '#4a7';
        if (s === 'paused') return '#c9a96e';
        if (s === 'error' || s === 'failed') return '#e55';
        return '#888';
    }

    window.toggleAutoSpeed = function(enabled) {
        fetch('/printer-auto-speed', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({enabled: enabled})
        }).then(function(r) { return r.json(); }).then(function(data) {
            if (data.ok) loadStatus();
        });
    };

    function renderSpeedGraph(graph, currentLayer, currentPct) {
        var W = 280, H = 100, PAD_L = 30, PAD_R = 8, PAD_T = 14, PAD_B = 20;
        var gW = W - PAD_L - PAD_R;
        var gH = H - PAD_T - PAD_B;
        var n = graph.length;
        if (n < 2) return '';

        // Cap optimal at 200% and find y range
        var minY = 200, maxY = 50;
        for (var i = 0; i < n; i++) {
            if (graph[i].optimal_pct > 200) graph[i].optimal_pct = 200;
            var v = graph[i].optimal_pct;
            if (v < minY) minY = v;
            if (v > maxY) maxY = v;
        }
        minY = Math.max(50, Math.floor(minY / 10) * 10 - 10);
        maxY = Math.ceil(maxY / 10) * 10 + 10;
        var yRange = maxY - minY || 1;

        function xPos(idx) { return PAD_L + (idx / (n - 1)) * gW; }
        function yPos(val) { return PAD_T + gH - ((val - minY) / yRange) * gH; }

        var svg = '<div style="margin:6px 0;padding:6px 8px;background:#1a1a2e;border-radius:8px;">';
        svg += '<div style="font-size:10px;color:#888;margin-bottom:4px;">SPEED PROFILE (optimal % per layer)</div>';
        svg += '<svg width="100%" viewBox="0 0 ' + W + ' ' + H + '" style="display:block;">';

        // Grid lines
        var gridSteps = [minY, Math.round(minY + yRange * 0.33), Math.round(minY + yRange * 0.66), maxY];
        for (var g = 0; g < gridSteps.length; g++) {
            var gy = yPos(gridSteps[g]);
            svg += '<line x1="' + PAD_L + '" y1="' + gy + '" x2="' + (W - PAD_R) + '" y2="' + gy + '" stroke="#333" stroke-width="0.5"/>';
            svg += '<text x="' + (PAD_L - 3) + '" y="' + (gy + 3) + '" fill="#666" font-size="8" text-anchor="end">' + gridSteps[g] + '</text>';
        }

        // 100% reference line
        if (minY <= 100 && maxY >= 100) {
            var y100 = yPos(100);
            svg += '<line x1="' + PAD_L + '" y1="' + y100 + '" x2="' + (W - PAD_R) + '" y2="' + y100 + '" stroke="#555" stroke-width="0.5" stroke-dasharray="3,3"/>';
        }

        // Current layer vertical line
        var curIdx = 0;
        for (var i = 0; i < n; i++) {
            if (graph[i].layer <= currentLayer) curIdx = i;
        }
        var cx = xPos(curIdx);
        svg += '<line x1="' + cx + '" y1="' + PAD_T + '" x2="' + cx + '" y2="' + (H - PAD_B) + '" stroke="#88f" stroke-width="1" stroke-dasharray="2,2"/>';

        // Past layers path (solid green)
        var pastPath = '';
        var futurePath = '';
        for (var i = 0; i < n; i++) {
            var px = xPos(i);
            var py = yPos(graph[i].optimal_pct);
            if (i <= curIdx) {
                pastPath += (pastPath ? 'L' : 'M') + px.toFixed(1) + ',' + py.toFixed(1);
            }
            if (i >= curIdx) {
                futurePath += (futurePath ? 'L' : 'M') + px.toFixed(1) + ',' + py.toFixed(1);
            }
        }
        if (pastPath) svg += '<path d="' + pastPath + '" fill="none" stroke="#4a7" stroke-width="1.5"/>';
        if (futurePath) svg += '<path d="' + futurePath + '" fill="none" stroke="#4a7" stroke-width="1" stroke-dasharray="3,2" opacity="0.6"/>';

        // Current position dot
        var cy = yPos(graph[curIdx] ? graph[curIdx].optimal_pct : 100);
        svg += '<circle cx="' + cx + '" cy="' + cy + '" r="3" fill="#88f" stroke="#fff" stroke-width="0.5"/>';

        // Actual current speed dot (if different)
        if (currentPct) {
            var ay = yPos(currentPct);
            svg += '<circle cx="' + cx + '" cy="' + ay + '" r="2.5" fill="#c9a96e" stroke="#fff" stroke-width="0.5"/>';
        }

        // X-axis labels
        var xLabels = [0, Math.round(n * 0.25), Math.round(n * 0.5), Math.round(n * 0.75), n - 1];
        for (var i = 0; i < xLabels.length; i++) {
            var li = xLabels[i];
            if (li >= n) li = n - 1;
            svg += '<text x="' + xPos(li) + '" y="' + (H - 4) + '" fill="#666" font-size="7" text-anchor="middle">' + graph[li].layer + '</text>';
        }

        svg += '</svg>';
        svg += '<div style="font-size:9px;color:#666;display:flex;gap:10px;margin-top:2px;">';
        svg += '<span><span style="color:#4a7;">&#9644;</span> Optimal</span>';
        svg += '<span><span style="color:#88f;">&#9679;</span> Current layer</span>';
        svg += '<span><span style="color:#c9a96e;">&#9679;</span> Actual speed</span>';
        svg += '</div></div>';
        return svg;
    }

    function renderPrinterCard(p, name, accent, camImg, thumbImg) {
        var h = '<div class="printer-card">';
        h += '<div class="card-header">';
        h += '<h2 style="color:' + accent + ';">' + esc(name) + '</h2>';

        if (p.error) {
            h += '<span class="state-badge" style="color:#e55;background:#e5522;font-size:11px;">OFFLINE</span>';
            h += '</div>';
            h += '<div class="offline">' + esc(p.error) + '</div>';
            h += '</div>';
            return h;
        }

        var sc = stateColor(p.state);
        h += '<span class="state-badge" style="color:' + sc + ';background:' + sc + '22;">' + esc(p.state) + '</span>';
        h += '</div>';

        if (p.model_name) h += '<div class="model-name">' + esc(p.model_name) + '</div>';

        // Media row
        var ts = '?' + Date.now();
        if (p.has_camera || p.has_thumbnail) {
            h += '<div class="media-row">';
            if (p.has_camera) {
                h += '<div style="flex:1;min-width:0;">';
                h += '<div style="color:#888;font-size:10px;margin-bottom:2px;">CAMERA</div>';
                h += '<img src="/printer-image/' + camImg + ts + '" style="width:100%;" onerror="this.hidden=true">';
                h += '</div>';
            }
            if (p.has_thumbnail) {
                h += '<div style="width:90px;flex-shrink:0;">';
                h += '<div style="color:#888;font-size:10px;margin-bottom:2px;">PREVIEW</div>';
                h += '<img src="/printer-image/' + thumbImg + ts + '" style="width:100%;background:#222;" onerror="this.hidden=true">';
                h += '</div>';
            }
            h += '</div>';
        }

        // Progress bar
        var prog = p.progress || 0;
        if (prog > 0) {
            h += '<div class="progress-wrap">';
            h += '<div class="progress-label"><span>Progress</span><span style="color:' + accent + ';font-weight:600;">' + (typeof prog === 'number' && prog % 1 !== 0 ? prog.toFixed(1) : prog) + '%</span></div>';
            h += '<div class="progress-bar"><div class="progress-fill" style="background:linear-gradient(90deg,#2d6a4f,' + accent + ');width:' + prog + '%;"></div></div>';
            if (p.current_layer && p.total_layers) {
                h += '<div style="font-size:10px;color:#888;margin-top:2px;">Layer ' + p.current_layer + ' / ' + p.total_layers + ' (' + (p.layer_progress || 0) + '%)</div>';
            }
            h += '</div>';
        }

        // Speed analysis + auto-speed toggle (SV08)
        if (p.speed_factor) {
            var spd_color = p.speed_warning ? '#e55' : '#4a7';
            h += '<div style="margin:6px 0;padding:6px 8px;background:#1a1a2e;border-radius:8px;border-left:3px solid ' + spd_color + ';">';
            h += '<div style="display:flex;justify-content:space-between;align-items:center;">';
            h += '<div style="font-size:11px;color:#888;">SPEED</div>';
            // Auto-speed toggle
            if (p.auto_speed_enabled !== undefined) {
                var togOn = p.auto_speed_enabled;
                var togColor = togOn ? '#4a7' : '#666';
                h += '<div style="display:flex;align-items:center;gap:6px;">';
                h += '<span style="font-size:10px;color:#888;">Auto</span>';
                h += '<div onclick="window.toggleAutoSpeed(' + (togOn ? 'false' : 'true') + ')" style="cursor:pointer;width:36px;height:20px;border-radius:10px;background:' + togColor + ';position:relative;transition:background .2s;">';
                h += '<div style="width:16px;height:16px;border-radius:50%;background:#fff;position:absolute;top:2px;' + (togOn ? 'right:2px;' : 'left:2px;') + 'transition:all .2s;"></div>';
                h += '</div>';
                h += '</div>';
            }
            h += '</div>';
            h += '<div style="font-size:13px;color:#ddd;margin-top:3px;">Set: <b>' + Math.round(p.speed_factor * 100) + '%</b>';
            if (p.effective_speed) h += ' &middot; Effective: <b style="color:' + spd_color + ';">' + p.effective_speed + 'x</b>';
            if (p.layer_optimal_speed) h += ' &middot; Optimal: <b style="color:#88f;">' + p.layer_optimal_speed + '%</b>';
            else if (p.max_useful_pct) h += ' &middot; Max useful: <b>' + p.max_useful_pct + '%</b>';
            h += '</div>';
            if (p.layer_alpha !== undefined) {
                h += '<div style="font-size:11px;color:#888;margin-top:2px;">\u03b1=' + p.layer_alpha.toFixed(3) + ' (layer ' + (p.current_layer || '?') + ')</div>';
            }
            if (p.speed_warning) {
                h += '<div style="font-size:11px;color:#e55;margin-top:3px;">' + esc(p.speed_warning) + '</div>';
            }
            if (p.speed_adjusted) {
                h += '<div style="font-size:11px;color:#4a7;margin-top:3px;">Auto-adjusted to ' + p.speed_adjusted_to + '%</div>';
            }
            h += '</div>';
        }

        // Speed graph (per-layer optimal speeds)
        if (p.speed_graph && p.speed_graph.length > 1) {
            h += renderSpeedGraph(p.speed_graph, p.current_layer || 0, p.current_speed_pct || 100);
        }

        // Filament feed warning
        if (p.filament_warning) {
            h += '<div style="background:#e5522;border:1px solid #e55;border-radius:8px;padding:8px 12px;margin:8px 0;">';
            h += '<div style="font-size:13px;color:#e55;font-weight:600;">⚠ ' + esc(p.filament_warning) + '</div>';
            h += '</div>';
        }

        // Details grid
        h += '<div class="detail-grid">';
        if (p.filament_name || p.filament_type) {
            h += '<span class="lbl">Filament</span><span>' + esc(p.filament_name || p.filament_type) + '</span>';
        }
        if (p.slicer) h += '<span class="lbl">Slicer</span><span>' + esc(p.slicer) + '</span>';
        if (p.start_str) h += '<span class="lbl">Started</span><span>' + esc(p.start_str) + '</span>';
        if (p.duration_str) h += '<span class="lbl">Duration</span><span>' + esc(p.duration_str) + '</span>';
        if (p.remaining_str) h += '<span class="lbl">Remaining</span><span class="val-warn">' + esc(p.remaining_str) + '</span>';
        if (p.eta_str) {
            var conf = p.eta_confidence || '';
            var confDot = conf === 'high' ? ' \u2705' : conf === 'medium' ? ' \u26A0' : conf === 'low' ? ' ~' : '';
            var method = (p.eta_method || '').indexOf('profile') >= 0 ? ' <span style="font-size:9px;color:#4a7;">[profile]</span>' : '';
            h += '<span class="lbl">ETA</span><span class="val-good">' + esc(p.eta_str) + confDot + method + '</span>';
        }
        if (p.filament_used_m) {
            var fil = p.filament_used_m + 'm / ' + p.filament_total_m + 'm';
            if (p.filament_used_g) fil += ' (' + p.filament_used_g + 'g / ' + p.filament_total_g + 'g)';
            h += '<span class="lbl">Filament used</span><span>' + fil + '</span>';
        }
        if (p.nozzle_temp !== undefined) {
            var noz = p.nozzle_temp + ' / ' + p.nozzle_target + String.fromCharCode(176) + 'C';
            if (p.nozzle_diameter) noz += ' (' + p.nozzle_diameter + 'mm)';
            h += '<span class="lbl">Nozzle</span><span>' + noz + '</span>';
        }
        if (p.bed_temp !== undefined) {
            h += '<span class="lbl">Bed</span><span>' + p.bed_temp + ' / ' + p.bed_target + String.fromCharCode(176) + 'C</span>';
        }
        if (p.layer_height) h += '<span class="lbl">Layer height</span><span>' + p.layer_height + 'mm</span>';
        if (p.speed) h += '<span class="lbl">Speed</span><span>' + esc(p.speed) + '</span>';
        h += '</div>';

        // AMS trays
        if (p.ams_trays && p.ams_trays.length) {
            h += '<div class="ams-row">AMS: ';
            for (var i = 0; i < p.ams_trays.length; i++) {
                var c = '#' + (p.ams_trays[i].color || '888888').substring(0, 6);
                h += '<span class="ams-dot" style="background:' + c + ';" title="' + esc(p.ams_trays[i].type) + '"></span>';
            }
            h += '</div>';
        }

        h += '</div>';
        return h;
    }

    function loadStatus() {
        fetch('/printer-status', {cache: 'no-store'}).then(function(r) { return r.json(); }).then(function(data) {
            var now = new Date();
            lastUpdate = now.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
            var html = '<div class="refresh-bar">';
            html += '<span>Updated: ' + lastUpdate + '</span>';
            html += '</div>';
            html += renderPrinterCard(data.sv08, 'Sovol SV08 Max', '#88f', 'sovol_camera.jpg', 'sovol_thumbnail.png');
            html += renderPrinterCard(data.a1, 'Bambu A1', '#52b788', 'a1_camera.jpg', '');
            dashEl.innerHTML = html;
        }).catch(function(e) {
            dashEl.innerHTML = '<div class="printer-loading" style="color:#e55;">Failed to load status. <button onclick="window.refreshPrinters()" style="color:#c9a96e;background:#2a2a4a;border:1px solid #3a3a5a;padding:4px 12px;border-radius:12px;margin-top:8px;">Retry</button></div>';
        });
    }

    function refreshPrinters() { loadStatus(); }
    window.refreshPrinters = refreshPrinters;
    window.hardRefresh = function() { window.location.reload(true); };

    function startAutoRefresh() {
        if (autoTimer) clearInterval(autoTimer);
        autoTimer = setInterval(loadStatus, 30000);
    }
    function stopAutoRefresh() {
        if (autoTimer) { clearInterval(autoTimer); autoTimer = null; }
    }

    window.onPrintersTabActive = function() { loadStatus(); startAutoRefresh(); };
    window.onPrintersTabInactive = function() { stopAutoRefresh(); };
})();
</script>

<!-- ====== WORK JS (isolated — errors here won't break other tabs) ====== -->
<script>
(function() {
    var dashEl = document.getElementById('workDash');
    var autoTimer = null;
    var currentSubtab = 'emails';

    function esc(text) {
        if (!text && text !== 0) return '';
        return String(text).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    }

    var readyEl = document.getElementById('workDashReady');
    var emailsEl = document.getElementById('workEmails');
    var diaryEl = document.getElementById('workDiary');
    var slackEl = document.getElementById('workSlack');
    var searchResultsEl = document.getElementById('workSearchResults');
    var searchInput = document.getElementById('workSearchInput');

    function acctBadge(item) {
        if (!item.account) return '';
        var bg = (item.account_color || '#888') + '22';
        var fg = item.account_color || '#888';
        return '<span class="wc-acct" style="background:' + bg + ';color:' + fg + ';">' + esc(item.account) + '</span> ';
    }

    function refreshBar() {
        return '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">' +
            '<span style="font-size:11px;color:#666;">Updated: ' + new Date().toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'}) + '</span>' +
            '<button onclick="window.refreshWork()" style="background:#2a2a4a;color:#c9a96e;border:1px solid #3a3a5a;padding:6px 14px;border-radius:16px;font-size:12px;cursor:pointer;">Refresh</button>' +
            '</div>';
    }

    function gmailUrl(e) {
        if (!e.message_id) return '';
        var idx = (e.account === 'Work') ? '1' : '0';
        return 'https://mail.google.com/mail/u/' + idx + '/#inbox/' + e.message_id;
    }

    function emailCard(e) {
        var url = gmailUrl(e);
        var tag = url ? 'a' : 'div';
        var extra = url ? ' href="' + esc(url) + '" style="text-decoration:none;color:inherit;display:block;"' : '';
        var html = '<' + tag + ' class="work-card"' + extra + '>';
        html += '<div>' + acctBadge(e) + '<span class="wc-from">' + esc(e.from) + '</span></div>';
        html += '<div class="wc-subject">' + esc(e.subject) + '</div>';
        if (e.snippet) html += '<div class="wc-snippet">' + esc(e.snippet) + '</div>';
        html += '<div class="wc-time">' + esc(e.time) + '</div>';
        html += '</' + tag + '>';
        return html;
    }

    function calCard(ev) {
        var url = ev.html_link || '';
        var tag = url ? 'a' : 'div';
        var extra = url ? ' href="' + esc(url) + '" style="text-decoration:none;color:inherit;display:block;"' : '';
        var html = '<' + tag + ' class="work-card cal-card"' + extra + '>';
        html += '<div>' + acctBadge(ev) + '<span class="wc-title">' + esc(ev.summary) + '</span></div>';
        html += '<div class="wc-when">' + esc(ev.when) + '</div>';
        if (ev.location) html += '<div class="wc-location">' + esc(ev.location) + '</div>';
        if (ev.attendees) html += '<div class="wc-attendees">' + esc(ev.attendees) + '</div>';
        html += '</' + tag + '>';
        return html;
    }

    function slackCard(m) {
        var url = m.link || '';
        var tag = url ? 'a' : 'div';
        var extra = url ? ' href="' + esc(url) + '" style="text-decoration:none;color:inherit;display:block;"' : '';
        var html = '<' + tag + ' class="work-card slack-card"' + extra + '>';
        html += '<div class="wc-channel">#' + esc(m.channel) + '</div>';
        html += '<div class="wc-user">' + esc(m.user) + '</div>';
        html += '<div class="wc-text">' + esc(m.text) + '</div>';
        var meta = esc(m.time);
        if (m.reply_count) meta += ' &middot; ' + m.reply_count + ' replies';
        html += '<div class="wc-time">' + meta + '</div>';
        html += '</' + tag + '>';
        return html;
    }

    function renderEmails(data) {
        var html = refreshBar();
        var count = data.emails ? data.emails.length : 0;
        html += '<div class="work-section"><h3>Unread (' + count + ')</h3>';
        if (data.emails && data.emails.length) {
            for (var i = 0; i < data.emails.length; i++) {
                html += emailCard(data.emails[i]);
            }
        } else {
            html += '<div style="color:#666;font-size:13px;padding:10px;">No unread emails</div>';
        }
        html += '</div>';
        emailsEl.innerHTML = html;
    }

    function renderDiary(data) {
        var html = refreshBar();
        html += '<div class="work-section"><h3>Upcoming Events</h3>';
        if (data.events && data.events.length) {
            for (var j = 0; j < data.events.length; j++) {
                html += calCard(data.events[j]);
            }
        } else {
            html += '<div style="color:#666;font-size:13px;padding:10px;">No upcoming events</div>';
        }
        html += '</div>';
        diaryEl.innerHTML = html;
    }

    function renderSlack(data) {
        if (data.setup_required) {
            slackEl.innerHTML = '<div class="work-setup">' + data.setup_message + '</div>';
            return;
        }
        if (data.error) {
            slackEl.innerHTML = '<div class="work-setup" style="color:#e55;">' + esc(data.error) + '</div>';
            return;
        }
        if (data.messages) window._workData.slack = data.messages;
        var html = refreshBar();
        var count = data.messages ? data.messages.length : 0;
        html += '<div class="work-section"><h3>Recent Messages (' + count + ')</h3>';
        if (data.messages && data.messages.length) {
            for (var i = 0; i < data.messages.length; i++) {
                html += slackCard(data.messages[i]);
            }
        } else {
            html += '<div style="color:#666;font-size:13px;padding:10px;">No recent messages</div>';
        }
        html += '</div>';
        slackEl.innerHTML = html;
    }

    // Global store for AI search context
    window._workData = {emails: [], events: [], slack: []};

    function renderWork(data) {
        if (data.setup_required) {
            dashEl.style.display = '';
            readyEl.style.display = 'none';
            dashEl.innerHTML = '<div class="work-setup">' + data.setup_message + '</div>';
            return;
        }
        dashEl.style.display = 'none';
        readyEl.style.display = '';
        if (data.emails) window._workData.emails = data.emails;
        if (data.events) window._workData.events = data.events;
        renderEmails(data);
        renderDiary(data);
    }

    function showSubtabContent(tab) {
        emailsEl.style.display = tab === 'emails' ? '' : 'none';
        diaryEl.style.display = tab === 'diary' ? '' : 'none';
        slackEl.style.display = tab === 'slack' ? '' : 'none';
        document.getElementById('wsEmails').classList.toggle('active', tab === 'emails');
        document.getElementById('wsDiary').classList.toggle('active', tab === 'diary');
        document.getElementById('wsSlack').classList.toggle('active', tab === 'slack');
        document.getElementById('workSubtabBar').style.display = '';
        searchResultsEl.style.display = 'none';
    }

    window.switchWorkSubtab = function(tab) {
        currentSubtab = tab;
        showSubtabContent(tab);
        if (tab === 'slack') loadSlack();
    };

    function loadSlack() {
        slackEl.innerHTML = '<div class="printer-loading">Loading Slack...</div>';
        var controller = new AbortController();
        var timer = setTimeout(function() { controller.abort(); }, 15000);
        fetch('/slack-messages', {signal: controller.signal}).then(function(r) { clearTimeout(timer); return r.json(); }).then(function(data) {
            renderSlack(data);
        }).catch(function(e) {
            clearTimeout(timer);
            var msg = e.name === 'AbortError' ? 'Slack timed out.' : 'Failed to load Slack.';
            slackEl.innerHTML = '<div class="printer-loading" style="color:#e55;">' + msg + ' <button onclick="window.switchWorkSubtab(\\'slack\\')" style="color:#c9a96e;background:#2a2a4a;border:1px solid #3a3a5a;padding:4px 12px;border-radius:12px;margin-top:8px;">Retry</button></div>';
        });
    }

    var aiChatEl = document.getElementById('workAiChat');
    var aiContentEl = document.getElementById('workAiContent');
    var _aiAbort = null;

    function startAiSearch(query) {
        aiChatEl.style.display = '';
        aiContentEl.innerHTML = '<span style="color:#888;">Thinking...</span>';
        if (_aiAbort) _aiAbort.abort();
        _aiAbort = new AbortController();

        fetch('/work-ai-search', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                query: query,
                emails: window._workData.emails || [],
                events: window._workData.events || [],
                slack: window._workData.slack || []
            }),
            signal: _aiAbort.signal
        }).then(function(resp) {
            var reader = resp.body.getReader();
            var decoder = new TextDecoder();
            var buf = '';
            var fullText = '';
            aiContentEl.innerHTML = '';

            function pump() {
                return reader.read().then(function(result) {
                    if (result.done) return;
                    buf += decoder.decode(result.value, {stream: true});
                    var lines = buf.split('\\n');
                    buf = lines.pop();
                    for (var i = 0; i < lines.length; i++) {
                        var line = lines[i].trim();
                        if (!line.startsWith('data: ')) continue;
                        try {
                            var ev = JSON.parse(line.substring(6));
                            if (ev.type === 'text') {
                                fullText += ev.content;
                                aiContentEl.innerHTML = '<div style="white-space:pre-wrap;word-break:break-word;">' + esc(fullText) + '</div>';
                                aiContentEl.scrollTop = aiContentEl.scrollHeight;
                            } else if (ev.type === 'cost') {
                                aiContentEl.innerHTML += '<div style="font-size:10px;color:#666;margin-top:6px;">' + esc(ev.content) + '</div>';
                            } else if (ev.type === 'error') {
                                aiContentEl.innerHTML += '<div style="color:#e55;margin-top:4px;">' + esc(ev.content) + '</div>';
                            }
                        } catch(e) {}
                    }
                    return pump();
                });
            }
            return pump();
        }).catch(function(e) {
            if (e.name !== 'AbortError') {
                aiContentEl.innerHTML = '<div style="color:#e55;">AI search failed: ' + esc(e.message) + '</div>';
            }
        });
    }

    window.workSearch = function() {
        var q = searchInput.value.trim();
        if (!q) {
            searchResultsEl.style.display = 'none';
            aiChatEl.style.display = 'none';
            showSubtabContent(currentSubtab);
            return;
        }
        // Hide subtabs and content, show search results
        document.getElementById('workSubtabBar').style.display = 'none';
        emailsEl.style.display = 'none';
        diaryEl.style.display = 'none';
        slackEl.style.display = 'none';
        searchResultsEl.style.display = '';
        searchResultsEl.innerHTML = '<div class="printer-loading">Searching...</div>';

        // Launch AI search in parallel
        startAiSearch(q);

        fetch('/work-search?q=' + encodeURIComponent(q)).then(function(r) { return r.json(); }).then(function(data) {
            var html = '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">' +
                '<span style="font-size:12px;color:#888;">Results for "' + esc(q) + '"</span>' +
                '<button onclick="window.clearWorkSearch()" style="background:#2a2a4a;color:#c9a96e;border:1px solid #3a3a5a;padding:6px 14px;border-radius:16px;font-size:12px;cursor:pointer;">Clear</button></div>';

            // Emails
            if (data.emails && data.emails.length) {
                html += '<div class="search-group"><h3>Emails (' + data.emails.length + ')</h3>';
                for (var i = 0; i < data.emails.length; i++) html += emailCard(data.emails[i]);
                html += '</div>';
            }
            // Calendar
            if (data.events && data.events.length) {
                html += '<div class="search-group"><h3>Calendar (' + data.events.length + ')</h3>';
                for (var j = 0; j < data.events.length; j++) html += calCard(data.events[j]);
                html += '</div>';
            }
            // Slack
            if (data.slack && data.slack.length) {
                html += '<div class="search-group"><h3>Slack (' + data.slack.length + ')</h3>';
                for (var k = 0; k < data.slack.length; k++) html += slackCard(data.slack[k]);
                html += '</div>';
            }

            var total = (data.emails ? data.emails.length : 0) + (data.events ? data.events.length : 0) + (data.slack ? data.slack.length : 0);
            if (total === 0) {
                html += '<div style="color:#666;font-size:13px;padding:20px;text-align:center;">No results found</div>';
            }
            searchResultsEl.innerHTML = html;
        }).catch(function(e) {
            searchResultsEl.innerHTML = '<div class="printer-loading" style="color:#e55;">Search failed. <button onclick="window.workSearch()" style="color:#c9a96e;background:#2a2a4a;border:1px solid #3a3a5a;padding:4px 12px;border-radius:12px;margin-top:8px;">Retry</button></div>';
        });
    };

    window.clearWorkSearch = function() {
        searchInput.value = '';
        searchResultsEl.style.display = 'none';
        aiChatEl.style.display = 'none';
        if (_aiAbort) { _aiAbort.abort(); _aiAbort = null; }
        showSubtabContent(currentSubtab);
    };

    var dbg = document.getElementById('workDebug');
    function wdbg(msg) { if (dbg) dbg.textContent = 'v3 | ' + msg; }

    function loadWork() {
        wdbg('loadWork called');
        dashEl.style.display = '';
        readyEl.style.display = 'none';
        dashEl.innerHTML = '<div class="printer-loading">Loading work status...</div>';
        var controller = new AbortController();
        var timer = setTimeout(function() { controller.abort(); }, 10000);
        var t0 = Date.now();
        wdbg('fetching /work-status...');
        fetch('/work-status', {signal: controller.signal, cache: 'no-store', headers: {'Cache-Control': 'no-cache'}}).then(function(r) {
            clearTimeout(timer);
            wdbg('got response ' + r.status + ' in ' + ((Date.now()-t0)/1000).toFixed(1) + 's');
            return r.json();
        }).then(function(data) {
            wdbg('rendering ' + (data.emails ? data.emails.length : 0) + ' emails, ' + (data.events ? data.events.length : 0) + ' events');
            renderWork(data);
            wdbg('done in ' + ((Date.now()-t0)/1000).toFixed(1) + 's');
        }).catch(function(e) {
            clearTimeout(timer);
            dashEl.style.display = '';
            readyEl.style.display = 'none';
            var msg = e.name === 'AbortError' ? 'Timed out after 10s.' : 'Failed: ' + e.message;
            wdbg('ERROR: ' + msg);
            dashEl.innerHTML = '<div class="printer-loading" style="color:#e55;">' + msg + ' <button onclick="window.refreshWork()" style="color:#c9a96e;background:#2a2a4a;border:1px solid #3a3a5a;padding:4px 12px;border-radius:12px;margin-top:8px;">Retry</button></div>';
        });
    }

    window.refreshWork = function() { loadWork(); };
    window.onWorkTabActive = function() {
        loadWork();
        if (autoTimer) clearInterval(autoTimer);
        autoTimer = setInterval(loadWork, 300000);
    };
    window.onWorkTabInactive = function() {
        if (autoTimer) { clearInterval(autoTimer); autoTimer = null; }
    };
})();
</script>

<!-- ====== TAB SWITCHING ====== -->
<script>
function switchTab(tab) {
    var tabs = ['chat', 'printers', 'governors', 'work'];
    var contentIds = {chat: 'chatTab', printers: 'printersTab', governors: 'governorsTab', work: 'workTab'};
    var buttonIds = {chat: 'tabChat', printers: 'tabPrinters', governors: 'tabGovernors', work: 'tabWork'};
    for (var i = 0; i < tabs.length; i++) {
        var t = tabs[i];
        document.getElementById(contentIds[t]).classList.toggle('active', t === tab);
        document.getElementById(buttonIds[t]).classList.toggle('active', t === tab);
    }
    if (tab === 'printers') {
        if (window.onPrintersTabActive) window.onPrintersTabActive();
    } else {
        if (window.onPrintersTabInactive) window.onPrintersTabInactive();
    }
    if (tab === 'governors') {
        var frame = document.getElementById('governorsFrame');
        if (frame && !frame.src) {
            frame.src = '/governors/';
        }
    }
    if (tab === 'work') {
        if (window.onWorkTabActive) window.onWorkTabActive();
    } else {
        if (window.onWorkTabInactive) window.onWorkTabInactive();
    }
}
</script>
</body>
</html>"""


@app.route('/')
def index():
    resp = Response(render_template_string(CHAT_HTML), content_type='text/html')
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp


@app.route('/work-test')
def work_test():
    """Minimal diagnostic page for Work tab debugging."""
    return Response("""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Work Tab Diagnostic</title>
<style>body{background:#1a1a2e;color:#e0e0e0;font-family:monospace;padding:16px;font-size:13px;}
pre{white-space:pre-wrap;word-break:break-all;background:#0d0d1a;padding:12px;border-radius:8px;max-height:60vh;overflow-y:auto;}
.ok{color:#52b788;} .err{color:#e55;} .warn{color:#c9a96e;} h2{color:#c9a96e;margin:8px 0;}
button{background:#2a2a4a;color:#c9a96e;border:1px solid #3a3a5a;padding:8px 16px;border-radius:8px;margin:4px;font-size:13px;}</style></head>
<body>
<h2>Work Tab Diagnostic</h2>
<div id="log"></div>
<pre id="out"></pre>
<button onclick="runTest()">Run Again</button>
<script>
var log = document.getElementById('log');
var out = document.getElementById('out');
function L(cls, msg) { log.innerHTML += '<div class="'+cls+'">' + new Date().toLocaleTimeString() + ' ' + msg + '</div>'; }
function runTest() {
    log.innerHTML = '';
    out.textContent = '';
    L('warn', 'Step 1: JS executing OK');
    L('warn', 'Step 2: Starting fetch to /work-status...');
    var t0 = Date.now();
    var controller = new AbortController();
    var timer = setTimeout(function() { controller.abort(); }, 15000);
    fetch('/work-status', {signal: controller.signal, cache: 'no-store'})
        .then(function(r) {
            clearTimeout(timer);
            var ms = Date.now() - t0;
            L('ok', 'Step 3: Response received — HTTP ' + r.status + ' in ' + ms + 'ms');
            return r.text();
        })
        .then(function(text) {
            L('ok', 'Step 4: Body received — ' + text.length + ' bytes');
            try {
                var data = JSON.parse(text);
                L('ok', 'Step 5: JSON parsed OK — ' + (data.emails ? data.emails.length : 0) + ' emails, ' + (data.events ? data.events.length : 0) + ' events');
                if (data.errors) L('err', 'Server errors: ' + JSON.stringify(data.errors));
                out.textContent = JSON.stringify(data, null, 2).substring(0, 2000);
            } catch(e) {
                L('err', 'Step 5: JSON parse FAILED — ' + e.message);
                out.textContent = text.substring(0, 1000);
            }
        })
        .catch(function(e) {
            clearTimeout(timer);
            var ms = Date.now() - t0;
            L('err', 'FAILED after ' + ms + 'ms — ' + e.name + ': ' + e.message);
        });
    L('warn', 'Step 2b: Fetch initiated (async), waiting for response...');

    // Also test /slack-messages
    L('warn', 'Step 6: Fetching /slack-messages...');
    var t1 = Date.now();
    fetch('/slack-messages', {cache: 'no-store'})
        .then(function(r) { return r.json(); })
        .then(function(d) { L('ok', 'Step 7: Slack OK in ' + (Date.now()-t1) + 'ms — ' + (d.messages ? d.messages.length : 0) + ' messages'); })
        .catch(function(e) { L('err', 'Slack FAILED: ' + e.name + ': ' + e.message); });
}
runTest();
</script></body></html>""", content_type='text/html')


@app.route('/chat', methods=['POST'])
def chat():
    global current_session_id
    data = request.json
    user_message = data.get('message', '')
    client_session_id = data.get('session_id', None)
    image_paths = data.get('image_paths', [])

    # If images attached, tell Claude to read them
    valid_paths = [p for p in image_paths if os.path.exists(p)]
    if valid_paths:
        paths_str = ", ".join(valid_paths)
        user_message = f"I've attached {len(valid_paths)} image(s) at these paths: {paths_str} — please read/view them first. {user_message}"

    def generate():
        global current_session_id, last_response
        import queue as _queue

        response_buf = {"text": "", "cost": ""}
        sse_queue = _queue.Queue()

        env = os.environ.copy()
        for key in ['CLAUDE_CODE_ENTRYPOINT', 'CLAUDECODE', 'CLAUDE_CODE_SESSION']:
            env.pop(key, None)
        env['HOME'] = os.path.expanduser('~')
        env['PATH'] = '/Users/timtrailor/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin'
        # Don't set ANTHROPIC_API_KEY — let Claude Code use subscription auth instead of API billing
        env.pop('ANTHROPIC_API_KEY', None)

        cmd = [
            'claude', '-p', user_message,
            '--model', 'claude-opus-4-6',
            '--max-turns', '20',
            '--output-format', 'stream-json',
            '--verbose'
        ]

        # Resume existing session for conversation continuity
        if client_session_id or current_session_id:
            sid = client_session_id or current_session_id
            cmd.extend(['--resume', sid])

        # Send 2KB padding to prime Safari's ReadableStream buffer.
        # iOS Safari won't start delivering chunks until ~1KB is received.
        yield ": " + "x" * 2048 + "\n\n"

        try:
            # Write stdout to a temp file so the subprocess survives wrapper restarts.
            # The subprocess runs in its own session group (os.setsid) and writes to
            # a file, not a pipe — so it continues even if Flask restarts.
            import tempfile
            output_fd, output_path = tempfile.mkstemp(
                suffix='.jsonl', prefix='claude_out_', dir='/tmp')
            output_handle = os.fdopen(output_fd, 'wb', buffering=0)
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdout=output_handle,
                    stderr=subprocess.PIPE,
                    cwd=WORK_DIR,
                    env=env,
                    preexec_fn=os.setsid
                )
            finally:
                output_handle.close()  # Parent doesn't need it; child has the fd

            with current_proc_lock:
                global current_proc
                current_proc = proc

            # Persist state so the new wrapper instance can reattach after restart
            _save_work_state(proc.pid, output_path, current_session_id)

            # Background reader thread — tails the output file and feeds SSE events.
            # If Safari suspends the tab, this thread keeps running and buffers.
            # If the wrapper restarts, /reattach can resume from the same file.
            def _reader_thread():
                raw_buffer = b""
                try:
                    with open(output_path, 'rb') as f:
                        while True:
                            chunk = f.read(8192)
                            if chunk:
                                raw_buffer += chunk
                                while b'\n' in raw_buffer:
                                    line_bytes, raw_buffer = raw_buffer.split(b'\n', 1)
                                    line = line_bytes.decode('utf-8', errors='replace').strip()
                                    if not line:
                                        continue
                                    try:
                                        event = json.loads(line)
                                        for sse in process_event(event):
                                            _buffer_sse(sse, response_buf)
                                            sse_queue.put(sse)
                                    except json.JSONDecodeError:
                                        pass
                            else:
                                # No new data — check if process finished
                                if proc.poll() is not None:
                                    time.sleep(0.3)  # Brief pause for final flush
                                    final = f.read()
                                    if final:
                                        raw_buffer += final
                                        while b'\n' in raw_buffer:
                                            line_bytes, raw_buffer = raw_buffer.split(b'\n', 1)
                                            line = line_bytes.decode('utf-8', errors='replace').strip()
                                            if not line:
                                                continue
                                            try:
                                                event = json.loads(line)
                                                for sse in process_event(event):
                                                    _buffer_sse(sse, response_buf)
                                                    sse_queue.put(sse)
                                            except json.JSONDecodeError:
                                                pass
                                    break
                                time.sleep(0.2)  # Poll interval for new file data
                except Exception:
                    pass
                finally:
                    proc.wait()
                    with current_proc_lock:
                        global current_proc
                        current_proc = None
                    # Save completed response — runs even if client disconnected
                    if response_buf["text"]:
                        with last_response_lock:
                            last_response["text"] = response_buf["text"]
                            last_response["cost"] = response_buf["cost"]
                            last_response["session_id"] = current_session_id
                            last_response["seq"] += 1
                    _clear_work_state()
                    # Clean up output file now that we're done
                    try:
                        os.unlink(output_path)
                    except Exception:
                        pass
                    sse_queue.put(None)  # Sentinel to signal completion

            reader = threading.Thread(target=_reader_thread, daemon=True)
            reader.start()

            # Safari requires ~1KB per chunk to trigger ReadableStream delivery.
            SAFARI_PAD = ": " + "x" * 256 + "\n\n"
            while True:
                try:
                    item = sse_queue.get(timeout=2.0)
                    if item is None:
                        break  # Reader thread finished
                    yield item + SAFARI_PAD
                except _queue.Empty:
                    # No data yet — send keepalive to keep connection alive
                    yield f"data: {json.dumps({'type': 'ping'})}\n\n" + SAFARI_PAD

        except FileNotFoundError:
            yield f"data: {json.dumps({'type': 'error', 'content': 'Claude Code CLI not found'})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'content': str(e)})}\n\n"

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache',
                             'X-Accel-Buffering': 'no',
                             'Connection': 'keep-alive'})


def _buffer_sse(sse_line, buf):
    """Extract text/cost from an SSE line and accumulate into buf."""
    if not sse_line.startswith('data: '):
        return
    try:
        data = json.loads(sse_line[6:].strip())
        if data.get('type') == 'text':
            buf["text"] += data.get('content', '')
        elif data.get('type') == 'cost':
            buf["cost"] = data.get('content', '')
    except (json.JSONDecodeError, KeyError):
        pass


@app.route('/last-response')
def get_last_response():
    """Return the last completed response for client-side recovery."""
    with last_response_lock:
        return jsonify(last_response)


@app.route('/pending-work')
def pending_work():
    """Check if a Claude process is still running from before a restart."""
    state = _load_work_state()
    if state:
        return jsonify({'active': True, 'session_id': state.get('session_id', ''),
                        'pid': state.get('pid'), 'output_file': state.get('output_file')})
    return jsonify({'active': False})


@app.route('/reattach')
def reattach():
    """Re-attach to a running Claude process after wrapper restart.

    Reads the output file from the beginning.  Historical text is accumulated
    and sent as a single 'snapshot' event so the client can replace (not
    duplicate) the assistant message.  After the snapshot, new events are
    streamed live in the same SSE format as /chat.
    """
    state = _load_work_state()
    if not state:
        return jsonify({'error': 'No active work to reattach to'}), 404

    pid = state['pid']
    output_path = state['output_file']
    session_id = state.get('session_id', '')

    if not os.path.exists(output_path):
        _clear_work_state()
        return jsonify({'error': 'Output file not found'}), 404

    def generate():
        global current_session_id
        if session_id:
            current_session_id = session_id

        SAFARI_PAD = ": " + "x" * 256 + "\n\n"
        yield ": " + "x" * 2048 + "\n\n"  # Safari prime

        response_buf = {"text": "", "cost": ""}
        raw_buffer = b""
        caught_up = False  # True once we've read all existing data

        try:
            with open(output_path, 'rb') as f:
                while True:
                    chunk = f.read(8192)
                    if chunk:
                        raw_buffer += chunk
                        while b'\n' in raw_buffer:
                            line_bytes, raw_buffer = raw_buffer.split(b'\n', 1)
                            line = line_bytes.decode('utf-8', errors='replace').strip()
                            if not line:
                                continue
                            try:
                                event = json.loads(line)
                                for sse in process_event(event):
                                    _buffer_sse(sse, response_buf)
                                    if caught_up:
                                        # After snapshot, stream new events live
                                        yield sse + SAFARI_PAD
                            except json.JSONDecodeError:
                                pass
                    else:
                        # No more data in file right now.
                        # If we haven't sent the snapshot yet, send it now —
                        # this marks the boundary between history and live.
                        if not caught_up:
                            caught_up = True
                            if response_buf["text"]:
                                yield f"data: {json.dumps({'type': 'snapshot', 'content': response_buf['text']})}\n\n" + SAFARI_PAD

                        # Check if process is still alive
                        alive = False
                        try:
                            os.kill(pid, 0)
                            alive = True
                        except OSError:
                            pass

                        if not alive:
                            # Process finished — final read
                            time.sleep(0.3)
                            final = f.read()
                            if final:
                                raw_buffer += final
                                while b'\n' in raw_buffer:
                                    line_bytes, raw_buffer = raw_buffer.split(b'\n', 1)
                                    line = line_bytes.decode('utf-8', errors='replace').strip()
                                    if not line:
                                        continue
                                    try:
                                        event = json.loads(line)
                                        for sse in process_event(event):
                                            _buffer_sse(sse, response_buf)
                                            yield sse + SAFARI_PAD
                                    except json.JSONDecodeError:
                                        pass
                            break

                        # Still alive — keepalive + wait
                        yield f"data: {json.dumps({'type': 'ping'})}\n\n" + SAFARI_PAD
                        time.sleep(1)

        except Exception:
            pass

        # Save completed response for /last-response
        if response_buf["text"]:
            with last_response_lock:
                last_response["text"] = response_buf["text"]
                last_response["cost"] = response_buf["cost"]
                last_response["session_id"] = session_id
                last_response["seq"] += 1

        # Clean up state + output file
        _clear_work_state()
        try:
            os.unlink(output_path)
        except Exception:
            pass

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache',
                             'X-Accel-Buffering': 'no',
                             'Connection': 'keep-alive'})


def process_event(event):
    """Convert Claude Code stream-json events to SSE messages for the frontend."""
    global current_session_id
    etype = event.get('type', '')

    if etype == 'system' and event.get('subtype') == 'init':
        current_session_id = event.get('session_id', '')
        model = event.get('model', 'unknown')
        yield f"data: {json.dumps({'type': 'init', 'session_id': current_session_id, 'model': model})}\n\n"

    elif etype == 'assistant':
        msg = event.get('message', {})
        content_blocks = msg.get('content', [])
        for block in content_blocks:
            if block.get('type') == 'text':
                text = block.get('text', '')
                if text:
                    yield f"data: {json.dumps({'type': 'text', 'content': text})}\n\n"
            elif block.get('type') == 'tool_use':
                tool_name = block.get('name', 'unknown')
                tool_input = block.get('input', {})
                # Create a readable summary
                summary = format_tool_summary(tool_name, tool_input)
                yield f"data: {json.dumps({'type': 'tool', 'content': summary})}\n\n"

    elif etype == 'tool':
        # Tool result
        tool_name = event.get('tool', '')
        content = event.get('content', '')
        if tool_name:
            summary = f"Result from {tool_name}"
            yield f"data: {json.dumps({'type': 'tool', 'content': summary})}\n\n"

    elif etype == 'result':
        cost = event.get('total_cost_usd', 0)
        duration = event.get('duration_ms', 0)
        turns = event.get('num_turns', 0)
        if cost > 0:
            cost_str = f"${cost:.4f} | {duration/1000:.1f}s | {turns} turn(s)"
            yield f"data: {json.dumps({'type': 'cost', 'content': cost_str})}\n\n"

    elif etype == 'user':
        # Tool results, permission approvals, synthetic messages — send activity pulse
        # so frontend knows Claude is still working during long tool sequences
        msg = event.get('message', {})
        tool_result = event.get('tool_use_result')
        if tool_result:
            # Completed tool — extract name if available
            yield f"data: {json.dumps({'type': 'activity', 'content': 'tool_result'})}\n\n"
        else:
            yield f"data: {json.dumps({'type': 'activity', 'content': 'processing'})}\n\n"

    elif etype == 'system':
        # Background task notifications, permission mode changes, etc.
        subtype = event.get('subtype', '')
        if subtype and subtype != 'init':
            yield f"data: {json.dumps({'type': 'activity', 'content': subtype})}\n\n"

    elif etype == 'rate_limit_event':
        # Rate limit info — just keep the connection alive
        yield f"data: {json.dumps({'type': 'activity', 'content': 'rate_limited'})}\n\n"

    else:
        import sys
        print(f"[UNHANDLED EVENT] type={etype} keys={list(event.keys())}", file=sys.stderr, flush=True)


def format_tool_summary(name, input_data):
    """Create a readable one-line summary of a tool call."""
    if name == 'Bash':
        cmd = input_data.get('command', '')
        desc = input_data.get('description', '')
        if desc:
            return f"Running: {desc}"
        if len(cmd) > 80:
            cmd = cmd[:77] + "..."
        return f"$ {cmd}"
    elif name == 'Read':
        path = input_data.get('file_path', '')
        return f"Reading: {os.path.basename(path)}"
    elif name == 'Write':
        path = input_data.get('file_path', '')
        return f"Writing: {os.path.basename(path)}"
    elif name == 'Edit':
        path = input_data.get('file_path', '')
        return f"Editing: {os.path.basename(path)}"
    elif name == 'Glob':
        pattern = input_data.get('pattern', '')
        return f"Searching: {pattern}"
    elif name == 'Grep':
        pattern = input_data.get('pattern', '')
        return f"Grep: {pattern}"
    elif name == 'Task':
        desc = input_data.get('description', '')
        return f"Agent: {desc}"
    elif name == 'WebFetch':
        url = input_data.get('url', '')
        return f"Fetching: {url[:60]}"
    elif name == 'WebSearch':
        query = input_data.get('query', '')
        return f"Searching web: {query}"
    else:
        return f"Using {name}..."


@app.route('/upload', methods=['POST'])
def upload():
    if 'image' not in request.files:
        return jsonify({'error': 'No image'}), 400
    f = request.files['image']
    if not f.filename:
        return jsonify({'error': 'No filename'}), 400
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in ('.jpg', '.jpeg', '.png', '.gif', '.webp', '.heic', '.heif'):
        return jsonify({'error': 'Unsupported image type'}), 400
    filename = f"{uuid.uuid4().hex}{ext}"
    filepath = os.path.join(UPLOAD_DIR, filename)
    f.save(filepath)
    return jsonify({'path': filepath, 'filename': filename})


@app.route('/printer-status')
def printer_status():
    """Full printer status with metadata, times, filament details, camera & thumbnail."""
    from datetime import datetime, timedelta
    from urllib.parse import quote as urlquote
    import urllib.request

    PRINTER_IMG_DIR = '/tmp/printer_status'
    os.makedirs(PRINTER_IMG_DIR, exist_ok=True)

    result = {'sv08': {}, 'a1': {}}

    # --- SV08 via Moonraker (Ethernet .52, fallback WiFi .38) ---
    sv08_base = None
    for sv08_ip in [SOVOL_IP, SOVOL_WIFI_IP]:
        try:
            with urllib.request.urlopen(f'http://{sv08_ip}:{MOONRAKER_PORT}/printer/info', timeout=3) as r:
                json.loads(r.read())
            sv08_base = f'http://{sv08_ip}:{MOONRAKER_PORT}'
            break
        except Exception:
            continue
    try:
        if not sv08_base:
            raise ConnectionError('Could not connect')
        url = f'{sv08_base}/printer/objects/query?print_stats&display_status&heater_bed&extruder&fan&gcode_move&motion_report'
        with urllib.request.urlopen(url, timeout=4) as r:
            data = json.loads(r.read())['result']['status']
        ps = data.get('print_stats', {})
        ds = data.get('display_status', {})
        bed = data.get('heater_bed', {})
        ext = data.get('extruder', {})
        gm = data.get('gcode_move', {})
        mr = data.get('motion_report', {})

        sv = {
            'state': (ps.get('state', 'unknown') or 'unknown').capitalize(),
            'filename': ps.get('filename', ''),
            'progress': round((ds.get('progress', 0) or 0) * 100, 1),
            'print_duration': ps.get('print_duration', 0) or 0,
            'filament_used_mm': ps.get('filament_used', 0) or 0,
            'nozzle_temp': round(ext.get('temperature', 0) or 0, 1),
            'nozzle_target': round(ext.get('target', 0) or 0, 1),
            'bed_temp': round(bed.get('temperature', 0) or 0, 1),
            'bed_target': round(bed.get('target', 0) or 0, 1),
            'speed_factor': gm.get('speed_factor', 1.0) or 1.0,
            'live_velocity': round(mr.get('live_velocity', 0) or 0, 1),
        }

        # Layer progress
        info = ps.get('info', {})
        sv['current_layer'] = info.get('current_layer', 0) or 0
        sv['total_layers'] = info.get('total_layer', 0) or 0
        if sv['total_layers'] > 0:
            sv['layer_progress'] = round(sv['current_layer'] / sv['total_layers'] * 100, 1)

        # Metadata for current file
        if sv['filename']:
            enc = urlquote(sv['filename'])
            try:
                with urllib.request.urlopen(f'{sv08_base}/server/files/metadata?filename={enc}', timeout=4) as r:
                    meta = json.loads(r.read())['result']
                sv['estimated_time'] = meta.get('estimated_time', 0)
                sv['filament_total_mm'] = meta.get('filament_total', 0)
                sv['filament_weight_g'] = meta.get('filament_weight_total', 0)
                sv['slicer'] = meta.get('slicer', '')
                sv['layer_height'] = meta.get('layer_height', 0)
                sv['nozzle_diameter'] = meta.get('nozzle_diameter', 0)
                sv['filament_name'] = meta.get('filament_name', '')
                sv['filament_type'] = meta.get('filament_type', '')
                sv['object_height'] = meta.get('object_height', 0)

                # Thumbnail
                thumbs = meta.get('thumbnails', [])
                if thumbs:
                    biggest = max(thumbs, key=lambda t: t.get('width', 0) * t.get('height', 0))
                    tp = biggest.get('relative_path', '')
                    if tp:
                        thumb_url = f'{sv08_base}/server/files/gcodes/{urlquote(tp)}'
                        thumb_path = os.path.join(PRINTER_IMG_DIR, 'sovol_thumbnail.png')
                        with urllib.request.urlopen(thumb_url, timeout=4) as thumb_r:
                            with open(thumb_path, 'wb') as f:
                                f.write(thumb_r.read())
                        sv['has_thumbnail'] = True
            except Exception:
                pass

            # Print start time from history
            try:
                with urllib.request.urlopen(f'{sv08_base}/server/history/list?limit=1&order=desc', timeout=3) as r:
                    hist = json.loads(r.read())['result']
                if hist.get('jobs'):
                    sv['start_time'] = hist['jobs'][0].get('start_time', 0)
            except Exception:
                pass

        # Camera snapshot (with timeout to avoid hanging)
        try:
            cam_path = os.path.join(PRINTER_IMG_DIR, 'sovol_camera.jpg')
            with urllib.request.urlopen(f'http://{SOVOL_IP}:{SOVOL_CAMERA_PORT}/webcam/?action=snapshot', timeout=5) as cam_r:
                with open(cam_path, 'wb') as f:
                    f.write(cam_r.read())
            sv['has_camera'] = True
        except Exception:
            pass

        # Calculate times — smart ETA
        dur = sv.get('print_duration', 0) or 0
        sv['duration_str'] = f"{int(dur // 3600)}h {int((dur % 3600) // 60)}m"
        if sv.get('start_time'):
            sv['start_str'] = datetime.fromtimestamp(sv['start_time']).strftime('%a %d %b %H:%M')

        try:
            sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
            from print_eta import calculate_eta, log_snapshot
            eta_data = calculate_eta(
                progress_pct=sv.get('progress', 0),
                print_duration_s=dur,
                estimated_time_s=sv.get('estimated_time', 0),
                speed_factor=sv.get('speed_factor', 1.0),
                live_velocity=sv.get('live_velocity', 0),
                commanded_speed=sv.get('commanded_speed', 0),
                current_layer=sv.get('current_layer', 0),
                total_layers=sv.get('total_layers', 0),
                filename=sv.get('filename', ''),
            )
            if 'error' not in eta_data:
                sv['remaining_str'] = eta_data['remaining_str']
                sv['eta_str'] = eta_data['eta_str']
                sv['eta_confidence'] = eta_data['confidence']
                sv['eta_method'] = eta_data['method']
                if 'effective_speed' in eta_data:
                    sv['effective_speed'] = eta_data['effective_speed']
            if sv.get('state', '').lower() == 'printing':
                log_snapshot(sv)
        except Exception:
            # Fallback to naive calculation
            if sv.get('progress', 0) > 0:
                actual_total = dur / (sv['progress'] / 100)
                remaining = actual_total - dur
                eta = datetime.now() + timedelta(seconds=remaining)
                sv['remaining_str'] = f"{int(remaining // 3600)}h {int((remaining % 3600) // 60)}m"
                sv['eta_str'] = eta.strftime('%a %d %b %H:%M')

        # ── Gcode profile integration (auto-speed, per-layer data, calibrated ETA) ──
        try:
            from gcode_profile import (load_profile, load_auto_speed,
                                       calibrate_profile, calibrated_eta_remaining,
                                       get_layer_info, set_printer_speed,
                                       save_auto_speed)
            profile = load_profile(sv.get('filename'))
            auto_cfg = load_auto_speed()
            sv['auto_speed_enabled'] = auto_cfg.get('enabled', False)
            sv['auto_speed_mode'] = auto_cfg.get('mode', 'optimal')

            if profile and sv.get('state', '').lower() == 'printing':
                sv['profile_available'] = True
                cur_layer = sv.get('current_layer', 0)
                total_layers_p = sv.get('total_layers', 0)
                spd = sv.get('speed_factor', 1.0)

                # Get measured alpha from persistence
                measured_alpha = None
                alpha_file = '/tmp/printer_status/current_alpha.json'
                try:
                    if os.path.exists(alpha_file):
                        with open(alpha_file) as af:
                            alpha_data = json.load(af)
                        stored = alpha_data.get('alpha')
                        if stored and stored > 0.01:
                            measured_alpha = stored
                except Exception:
                    pass

                # Calibrate profile
                if measured_alpha and measured_alpha > 0.01 and cur_layer > 2:
                    cal = calibrate_profile(profile, measured_alpha, cur_layer,
                                            spd, elapsed_time_s=dur)
                    if cal:
                        sv['profile_calibrated'] = True

                        # Current layer info
                        for cl in cal:
                            if cl['layer'] == cur_layer:
                                sv['layer_alpha'] = cl['calibrated_alpha']
                                sv['layer_optimal_speed'] = min(cl['optimal_speed_pct'], 200)
                                break

                        # Profile-based ETA (calibrated) — promote as primary
                        progress_in_layer = 0.5
                        if total_layers_p > 0 and cur_layer > 0:
                            progress_in_layer = max(0.1, min(0.9,
                                (sv.get('progress', 0) / 100 - cur_layer / total_layers_p)
                                / (1.0 / total_layers_p) if total_layers_p > 0 else 0.5
                            ))
                        cal_remaining = calibrated_eta_remaining(
                            cal, cur_layer, progress_in_layer)
                        if cal_remaining > 0:
                            cal_eta = datetime.now() + timedelta(seconds=cal_remaining)
                            sv['remaining_str'] = f"{int(cal_remaining // 3600)}h {int((cal_remaining % 3600) // 60)}m"
                            sv['eta_str'] = cal_eta.strftime('%a %d %b %H:%M')
                            sv['eta_method'] = f"profile(cal,\u03b1={measured_alpha:.2f})"
                            sv['eta_confidence'] = 'high'

                        # Auto-speed adjustment
                        if auto_cfg.get('enabled') and cur_layer >= auto_cfg.get('skip_first_layers', 2):
                            target_pct = None
                            for cl in cal:
                                if cl['layer'] == cur_layer:
                                    target_pct = cl['optimal_speed_pct']
                                    break
                            if target_pct:
                                if auto_cfg.get('mode') == 'conservative':
                                    target_pct = max(round(target_pct * 0.95),
                                                     auto_cfg.get('min_speed_pct', 80))
                                # Global optimal cap: per-layer cal can underestimate
                                # complexity; the measured alpha is ground truth
                                try:
                                    from print_eta import optimal_speed_factor
                                    global_opt_pct = round(optimal_speed_factor(measured_alpha) * 100)
                                    target_pct = min(target_pct, global_opt_pct)
                                except Exception:
                                    pass
                                target_pct = max(auto_cfg.get('min_speed_pct', 80),
                                                 min(target_pct, auto_cfg.get('max_speed_pct', 200)))
                                current_pct = round(spd * 100)
                                last = auto_cfg.get('last_adjustment', {})
                                # If current speed is hurting the print (making it
                                # slower), drop immediately — don't wait for layer change
                                hurting = False
                                try:
                                    from print_eta import speed_time_ratio
                                    hurting = speed_time_ratio(spd, measured_alpha) > 1.0
                                except Exception:
                                    pass
                                should_adjust = False
                                if hurting and current_pct > target_pct:
                                    should_adjust = True
                                elif (last.get('layer') != cur_layer and
                                        abs(target_pct - current_pct) > 5):
                                    should_adjust = True
                                if should_adjust:
                                    if set_printer_speed(target_pct):
                                        auto_cfg['last_adjustment'] = {
                                            'layer': cur_layer, 'speed': target_pct,
                                            'from_speed': current_pct,
                                            'forced': hurting,
                                            'timestamp': datetime.now().isoformat()
                                        }
                                        save_auto_speed(auto_cfg)
                                        sv['speed_adjusted'] = True
                                        sv['speed_adjusted_to'] = target_pct

                # Build speed graph data: per-layer optimal speeds from profile
                speed_graph = []
                for l in profile.get('layers', []):
                    entry = {
                        'layer': l['layer'],
                        'optimal_pct': l.get('optimal_speed_pct', 100),
                        'alpha': round(l.get('alpha', 1.0), 3),
                    }
                    if l['layer'] <= cur_layer:
                        entry['status'] = 'past'
                    elif l['layer'] == cur_layer:
                        entry['status'] = 'current'
                    else:
                        entry['status'] = 'future'
                    speed_graph.append(entry)

                # Overlay actual speed adjustments from auto_speed history
                last_adj = auto_cfg.get('last_adjustment', {})
                if last_adj.get('layer') is not None:
                    sv['last_speed_layer'] = last_adj['layer']
                    sv['last_speed_pct'] = last_adj.get('speed', 100)

                sv['speed_graph'] = speed_graph
                sv['current_speed_pct'] = round(spd * 100)
        except Exception:
            pass

        # Effective speed analysis
        est = sv.get('estimated_time', 0) or 0
        prog = sv.get('progress', 0) or 0
        spd = sv.get('speed_factor', 1.0) or 1.0
        if est > 0 and dur > 0 and prog > 1 and 'effective_speed' not in sv:
            effective = (est * (prog / 100)) / dur
            sv['effective_speed'] = round(effective, 2)
            sv['speed_efficiency'] = round((effective / spd) * 100) if spd > 0 else 0
            max_useful = min(spd, effective * 1.1)
            sv['max_useful_pct'] = round(max_useful * 100)
            if spd > 1.2 and effective < spd * 0.6:
                sv['speed_warning'] = f"Speed set to {int(spd * 100)}% but only achieving {effective:.1f}x — max useful: {sv['max_useful_pct']}%"

        # Filament calculations
        fil_used_m = (sv.get('filament_used_mm', 0) or 0) / 1000
        fil_total_m = (sv.get('filament_total_mm', 0) or 0) / 1000
        if fil_total_m > 0:
            sv['filament_used_m'] = round(fil_used_m, 1)
            sv['filament_total_m'] = round(fil_total_m, 1)
            sv['filament_pct'] = round(fil_used_m / fil_total_m * 100)
            wt = sv.get('filament_weight_g', 0)
            if wt:
                sv['filament_used_g'] = round(wt * (fil_used_m / fil_total_m))
                sv['filament_total_g'] = round(wt)

        # Filament feed monitor — detect stalled extrusion
        if fil_total_m > 0 and sv.get('progress', 0) > 5 and sv.get('state', '').lower() == 'printing':
            expected_m = fil_total_m * (sv['progress'] / 100)
            if expected_m > 0:
                feed_ratio = fil_used_m / expected_m
                sv['feed_ratio'] = round(feed_ratio, 2)
                if feed_ratio < 0.5:
                    sv['filament_warning'] = (
                        f"Filament feed issue! Used {fil_used_m:.1f}m but expected ~{expected_m:.1f}m. Check filament path."
                    )

        # Extract model name
        fn = sv.get('filename', '')
        name = fn.replace('.gcode', '').replace('.3mf', '')
        scale = ''
        if 'pct_' in name:
            parts = name.split('pct_', 1)
            scale = parts[0].replace('_', '').strip() + '%'
            name = parts[1] if len(parts) > 1 else name
        for prefix in ['Sovol SV08 MAX_', 'Sovol SV08_']:
            if prefix in name:
                name = name[name.index(prefix) + len(prefix):]
        for sep in ['__PLA', '__ABS', '__PETG', '__TPU']:
            if sep in name:
                name = name[:name.index(sep)]
        name = name.replace('_', ' ').strip()
        if scale:
            name = f"{name} ({scale})"
        sv['model_name'] = name

        result['sv08'] = sv

    except Exception as e:
        result['sv08'] = {'error': str(e)}

    # --- A1 via MQTT ---
    try:
        import paho.mqtt.client as mqtt
        a1_data = {}
        got_data = threading.Event()
        serial = BAMBU_SERIAL

        def _on_connect(client, ud, flags, rc, props):
            if rc == 0:
                client.subscribe(f'device/{serial}/report')
                client.publish(f'device/{serial}/request',
                               json.dumps({'pushing': {'sequence_id': '0', 'command': 'pushall'}}))

        def _on_message(client, ud, msg):
            try:
                d = json.loads(msg.payload)
                if 'print' in d:
                    a1_data.update(d['print'])
                    got_data.set()
            except Exception:
                pass

        c = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                        client_id='status_check', protocol=mqtt.MQTTv311)
        c.username_pw_set('bblp', BAMBU_ACCESS_CODE)
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        c.tls_set_context(ctx)
        c.on_connect = _on_connect
        c.on_message = _on_message
        c.connect(BAMBU_IP, BAMBU_MQTT_PORT, keepalive=60)
        c.loop_start()
        got_data.wait(timeout=5)
        c.loop_stop()
        c.disconnect()

        if a1_data:
            speed_names = {1: 'Silent', 2: 'Standard', 3: 'Sport', 4: 'Ludicrous'}
            rem = a1_data.get('mc_remaining_time', 0)
            prog = a1_data.get('mc_percent', 0)

            a1 = {
                'state': a1_data.get('gcode_state', 'Unknown'),
                'progress': prog,
                'remaining': rem,
                'nozzle_temp': round(a1_data.get('nozzle_temper', 0), 1),
                'nozzle_target': round(a1_data.get('nozzle_target_temper', 0), 1),
                'bed_temp': round(a1_data.get('bed_temper', 0), 1),
                'bed_target': round(a1_data.get('bed_target_temper', 0), 1),
                'layer': a1_data.get('layer_num'),
                'total_layers': a1_data.get('total_layer_num'),
                'speed': speed_names.get(a1_data.get('spd_lvl'), '?'),
                'filename': a1_data.get('gcode_file', ''),
                'subtask_name': a1_data.get('subtask_name', ''),
            }

            # Calculate times
            if rem < 60:
                a1['remaining_str'] = f"{rem}m"
            else:
                a1['remaining_str'] = f"{rem // 60}h {rem % 60}m"
            if prog and prog > 0 and prog < 100:
                total_min = rem / (1 - prog / 100)
                elapsed_min = total_min - rem
                a1['duration_str'] = f"{int(elapsed_min // 60)}h {int(elapsed_min % 60)}m"
            eta = datetime.now() + timedelta(minutes=rem)
            a1['eta_str'] = eta.strftime('%a %d %b %H:%M')

            # Model name
            name = a1.get('subtask_name', '') or a1.get('filename', '')
            a1['model_name'] = name.replace('.gcode', '').replace('.3mf', '')

            # AMS trays
            ams = a1_data.get('ams', {})
            if ams and 'ams' in ams:
                trays = []
                for unit in ams['ams']:
                    for tray in unit.get('tray', []):
                        if tray.get('tray_type'):
                            trays.append({
                                'type': tray.get('tray_type', ''),
                                'color': tray.get('tray_color', ''),
                            })
                a1['ams_trays'] = trays

            # Camera snapshot via JPEG frame stream (port 6000)
            try:
                from bambulab import JPEGFrameStream
                with JPEGFrameStream(BAMBU_IP, BAMBU_ACCESS_CODE) as stream:
                    frame = stream.get_frame()
                    if frame:
                        cam_path = os.path.join(PRINTER_IMG_DIR, 'a1_camera.jpg')
                        with open(cam_path, 'wb') as f:
                            f.write(frame)
                        a1['has_camera'] = True
            except Exception:
                pass

            result['a1'] = a1
        else:
            result['a1'] = {'error': 'No response from A1'}
    except Exception as e:
        result['a1'] = {'error': str(e)}

    return jsonify(result)


@app.route('/printer-image/<name>')
def printer_image(name):
    """Serve camera snapshots and thumbnails."""
    from flask import send_from_directory
    safe_names = {'sovol_camera.jpg', 'sovol_thumbnail.png', 'a1_camera.jpg'}
    if name not in safe_names:
        return 'Not found', 404
    img_dir = '/tmp/printer_status'
    path = os.path.join(img_dir, name)
    if not os.path.exists(path):
        return 'Not found', 404
    return send_from_directory(img_dir, name)


@app.route('/printer-auto-speed', methods=['POST'])
def printer_auto_speed():
    """Toggle auto-speed on/off or set mode."""
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
    try:
        from gcode_profile import load_auto_speed, save_auto_speed
        data = request.get_json(force=True) if request.data else {}
        cfg = load_auto_speed()
        if 'enabled' in data:
            cfg['enabled'] = bool(data['enabled'])
        if 'mode' in data:
            cfg['mode'] = data['mode'] if data['mode'] in ('optimal', 'conservative') else 'optimal'
        save_auto_speed(cfg)
        return jsonify({'ok': True, 'enabled': cfg['enabled'], 'mode': cfg['mode']})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/work-status')
def work_status():
    """Fetch unread Gmail and upcoming calendar events from all configured Google accounts."""
    DIR = os.path.dirname(os.path.abspath(__file__))

    GOOGLE_ACCOUNTS = [
        {'token': os.path.join(DIR, 'google_token.json'), 'label': 'Personal', 'color': '#52b788'},
        {'token': os.path.join(DIR, 'google_token_work.json'), 'label': 'Work', 'color': '#c9a96e'},
    ]

    active_accounts = [a for a in GOOGLE_ACCOUNTS if os.path.exists(a['token'])]

    if not active_accounts:
        return jsonify({
            'setup_required': True,
            'setup_message': (
                'Google auth not configured yet.<br><br>'
                '1. Create OAuth credentials at<br>'
                '<code>console.cloud.google.com/apis/credentials</code><br><br>'
                '2. Save as <code>google_credentials.json</code> in the claude-mobile folder<br><br>'
                '3. Run: <code>python3 google_auth_setup.py</code>'
            )
        })

    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    SCOPES = [
        'https://www.googleapis.com/auth/gmail.readonly',
        'https://www.googleapis.com/auth/calendar.readonly',
    ]

    result = {'emails': [], 'events': [], 'accounts': []}

    for account in active_accounts:
        acct_label = account['label']
        acct_color = account['color']
        try:
            creds = Credentials.from_authorized_user_file(account['token'], SCOPES)
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
                with open(account['token'], 'w') as f:
                    f.write(creds.to_json())
        except Exception as e:
            result.setdefault('errors', []).append(f'{acct_label}: auth error — {e}')
            continue

        result['accounts'].append({'label': acct_label, 'color': acct_color})

        # --- Gmail: unread emails (max 10 per account) ---
        try:
            gmail = build('gmail', 'v1', credentials=creds, cache_discovery=False)
            msgs = gmail.users().messages().list(
                userId='me', q='is:unread', maxResults=10
            ).execute()

            for msg_meta in msgs.get('messages', []):
                msg = gmail.users().messages().get(
                    userId='me', id=msg_meta['id'], format='metadata',
                    metadataHeaders=['From', 'Subject', 'Date']
                ).execute()
                headers = {h['name']: h['value'] for h in msg.get('payload', {}).get('headers', [])}
                from_raw = headers.get('From', '')
                if '<' in from_raw:
                    from_name = from_raw.split('<')[0].strip().strip('"')
                else:
                    from_name = from_raw

                date_str = headers.get('Date', '')
                time_display = ''
                sort_ts = 0
                try:
                    from email.utils import parsedate_to_datetime
                    dt = parsedate_to_datetime(date_str)
                    sort_ts = dt.timestamp()
                    now = datetime.now(timezone.utc)
                    diff = now - dt
                    if diff.days == 0:
                        time_display = dt.strftime('%H:%M')
                    elif diff.days == 1:
                        time_display = 'Yesterday ' + dt.strftime('%H:%M')
                    elif diff.days < 7:
                        time_display = dt.strftime('%a %H:%M')
                    else:
                        time_display = dt.strftime('%d %b')
                except Exception:
                    time_display = date_str[:20] if date_str else ''

                result['emails'].append({
                    'from': from_name,
                    'subject': headers.get('Subject', '(no subject)'),
                    'snippet': msg.get('snippet', ''),
                    'time': time_display,
                    'sort_ts': sort_ts,
                    'message_id': msg_meta['id'],
                    'thread_id': msg.get('threadId', ''),
                    'account': acct_label,
                    'account_color': acct_color,
                })
        except Exception as e:
            result.setdefault('errors', []).append(f'{acct_label} Gmail: {e}')

        # --- Calendar: events through end of next working day ---
        try:
            cal = build('calendar', 'v3', credentials=creds, cache_discovery=False)
            now = datetime.now(timezone.utc)
            today = datetime.now()
            target = today + timedelta(days=1)
            while target.weekday() >= 5:
                target += timedelta(days=1)
            end_of_next_workday = target.replace(hour=23, minute=59, second=59)

            time_min = now.isoformat()
            time_max = end_of_next_workday.astimezone(timezone.utc).isoformat() if end_of_next_workday.tzinfo else end_of_next_workday.replace(tzinfo=timezone.utc).isoformat()

            events_result = cal.events().list(
                calendarId='primary',
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
                orderBy='startTime',
                maxResults=20
            ).execute()

            for ev in events_result.get('items', []):
                start = ev.get('start', {})
                end_t = ev.get('end', {})
                sort_ts = 0
                if 'dateTime' in start:
                    try:
                        from dateutil.parser import parse as dt_parse
                        st = dt_parse(start['dateTime'])
                        en = dt_parse(end_t.get('dateTime', start['dateTime']))
                        sort_ts = st.timestamp()
                        when = st.strftime('%a %d %b %H:%M') + ' - ' + en.strftime('%H:%M')
                    except Exception:
                        when = start['dateTime'][:16]
                elif 'date' in start:
                    when = 'All day - ' + start['date']
                else:
                    when = ''

                attendees = ev.get('attendees', [])
                att_names = []
                for a in attendees[:5]:
                    name = a.get('displayName', a.get('email', ''))
                    if a.get('self'):
                        continue
                    att_names.append(name)
                att_str = ', '.join(att_names)
                if len(attendees) > 5:
                    att_str += f' +{len(attendees) - 5} more'

                result['events'].append({
                    'summary': ev.get('summary', '(no title)'),
                    'when': when,
                    'location': ev.get('location', ''),
                    'attendees': att_str,
                    'sort_ts': sort_ts,
                    'html_link': ev.get('htmlLink', ''),
                    'event_id': ev.get('id', ''),
                    'account': acct_label,
                    'account_color': acct_color,
                })
        except Exception as e:
            result.setdefault('errors', []).append(f'{acct_label} Calendar: {e}')

    # --- IMAP accounts (e.g. Microsoft 365) ---
    IMAP_FILE = os.path.join(DIR, 'imap_accounts.json')
    if os.path.exists(IMAP_FILE):
        try:
            import imaplib
            import email as email_lib
            from email.header import decode_header
            from email.utils import parsedate_to_datetime as imap_parse_date

            with open(IMAP_FILE) as f:
                imap_accounts = json.load(f)

            for iacct in imap_accounts:
                acct_label = iacct.get('label', 'IMAP')
                acct_color = iacct.get('color', '#a78bfa')
                try:
                    mail = imaplib.IMAP4_SSL(iacct['server'], iacct.get('port', 993))
                    mail.login(iacct['email'], iacct['password'])
                    mail.select('INBOX', readonly=True)

                    result['accounts'].append({'label': acct_label, 'color': acct_color})

                    status, msg_ids = mail.search(None, 'UNSEEN')
                    ids = msg_ids[0].split() if msg_ids[0] else []
                    # Newest first, max 10
                    ids = ids[-10:][::-1]

                    for mid in ids:
                        status, msg_data = mail.fetch(mid, '(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])')
                        if status != 'OK':
                            continue
                        raw_header = msg_data[0][1]
                        msg_obj = email_lib.message_from_bytes(raw_header)

                        # Decode subject
                        subj_raw = msg_obj.get('Subject', '(no subject)')
                        decoded_parts = decode_header(subj_raw)
                        subj = ''
                        for part, charset in decoded_parts:
                            if isinstance(part, bytes):
                                subj += part.decode(charset or 'utf-8', errors='replace')
                            else:
                                subj += part

                        # Decode from
                        from_raw = msg_obj.get('From', '')
                        decoded_from = decode_header(from_raw)
                        from_str = ''
                        for part, charset in decoded_from:
                            if isinstance(part, bytes):
                                from_str += part.decode(charset or 'utf-8', errors='replace')
                            else:
                                from_str += part
                        if '<' in from_str:
                            from_name = from_str.split('<')[0].strip().strip('"')
                        else:
                            from_name = from_str

                        # Parse date
                        date_str = msg_obj.get('Date', '')
                        time_display = ''
                        sort_ts = 0
                        try:
                            dt = imap_parse_date(date_str)
                            sort_ts = dt.timestamp()
                            now = datetime.now(timezone.utc)
                            diff = now - dt
                            if diff.days == 0:
                                time_display = dt.strftime('%H:%M')
                            elif diff.days == 1:
                                time_display = 'Yesterday ' + dt.strftime('%H:%M')
                            elif diff.days < 7:
                                time_display = dt.strftime('%a %H:%M')
                            else:
                                time_display = dt.strftime('%d %b')
                        except Exception:
                            time_display = date_str[:20] if date_str else ''

                        result['emails'].append({
                            'from': from_name,
                            'subject': subj,
                            'snippet': '',
                            'time': time_display,
                            'sort_ts': sort_ts,
                            'account': acct_label,
                            'account_color': acct_color,
                        })

                    mail.logout()
                except Exception as e:
                    result.setdefault('errors', []).append(f'{acct_label}: {e}')
        except Exception as e:
            result.setdefault('errors', []).append(f'IMAP setup: {e}')

    # Sort: emails newest first, events earliest first
    result['emails'].sort(key=lambda x: x.get('sort_ts', 0), reverse=True)
    result['events'].sort(key=lambda x: x.get('sort_ts', 0))

    resp = jsonify(result)
    resp.headers['Cache-Control'] = 'no-store'
    return resp


# Persistent Slack caches (survive across requests)
_slack_user_cache = {}
_slack_team_id = None

@app.route('/slack-messages')
def slack_messages():
    """Fetch recent Slack messages from all accessible channels, DMs, and group DMs."""
    import re as _re
    DIR = os.path.dirname(os.path.abspath(__file__))
    SLACK_CONFIG = os.path.join(DIR, 'slack_config.json')

    # Load Slack tokens: try slack_config.json first, fall back to credentials.py
    slack_cfg = None
    if os.path.exists(SLACK_CONFIG):
        try:
            with open(SLACK_CONFIG) as f:
                slack_cfg = json.load(f)
        except Exception as e:
            return jsonify({'error': f'Failed to read slack_config.json: {e}'}), 500
    else:
        try:
            import sys as _sys
            _sys.path.insert(0, os.path.dirname(DIR))
            from credentials import SLACK_USER_TOKEN, SLACK_BOT_TOKEN
            slack_cfg = {'token': SLACK_USER_TOKEN, 'bot_token': SLACK_BOT_TOKEN, 'channels': [], 'max_channels': 12}
        except ImportError:
            pass

    if not slack_cfg:
        return jsonify({
            'setup_required': True,
            'setup_message': (
                'Slack not configured yet.<br><br>'
                '1. Create a Slack app at <code>api.slack.com/apps</code><br><br>'
                '2. Add User Token Scopes: <code>channels:history</code>, <code>channels:read</code>, '
                '<code>groups:history</code>, <code>groups:read</code>, <code>im:history</code>, '
                '<code>im:read</code>, <code>mpim:history</code>, <code>mpim:read</code>, '
                '<code>users:read</code>, <code>search:read</code><br><br>'
                '3. Install to workspace and copy the User OAuth Token (xoxp-...)<br><br>'
                '4. Add tokens to <code>credentials.py</code> or create <code>slack_config.json</code>'
            )
        })

    # User token for conversations/history, bot token for user lookups
    token = slack_cfg.get('token', '') or slack_cfg.get('bot_token', '')
    bot_token = slack_cfg.get('bot_token', '') or token
    max_channels = slack_cfg.get('max_channels', 20)
    channel_names = slack_cfg.get('channels', [])  # optional filter
    slack_headers = {'Authorization': f'Bearer {token}'}
    bot_headers = {'Authorization': f'Bearer {bot_token}'}

    result = {'messages': [], 'channels': []}
    import time as _time
    DEADLINE = _time.monotonic() + 12  # hard 12-second budget

    def _over_budget():
        return _time.monotonic() >= DEADLINE

    try:
        # Use persistent cache — survives across requests
        global _slack_user_cache, _slack_team_id

        # Bulk-fetch ALL workspace users on first request (one API call vs N individual calls)
        if not _slack_user_cache:
            try:
                cursor = ''
                for _ in range(5):  # max 5 pages (5000 users)
                    params = {'limit': 1000}
                    if cursor:
                        params['cursor'] = cursor
                    users_resp = http_requests.get(
                        'https://slack.com/api/users.list',
                        headers=bot_headers,
                        params=params,
                        timeout=10
                    ).json()
                    if users_resp.get('ok'):
                        for u in users_resp.get('members', []):
                            uid = u.get('id', '')
                            if not uid:
                                continue
                            profile = u.get('profile', {})
                            name = (profile.get('display_name', '').strip()
                                    or u.get('real_name', '').strip()
                                    or profile.get('real_name', '').strip()
                                    or u.get('name', uid))
                            _slack_user_cache[uid] = name
                    cursor = users_resp.get('response_metadata', {}).get('next_cursor', '')
                    if not cursor:
                        break
            except Exception:
                pass  # fall through to individual lookups if bulk fails

        def get_username(user_id):
            if not user_id or user_id in _slack_user_cache:
                return _slack_user_cache.get(user_id, user_id)
            if _over_budget():
                return user_id
            try:
                user_resp = http_requests.get(
                    'https://slack.com/api/users.info',
                    headers=bot_headers,
                    params={'user': user_id},
                    timeout=3
                ).json()
                if user_resp.get('ok'):
                    profile = user_resp['user'].get('profile', {})
                    name = (profile.get('display_name', '').strip()
                            or user_resp['user'].get('real_name', '').strip()
                            or profile.get('real_name', '').strip()
                            or user_resp['user'].get('name', user_id))
                    _slack_user_cache[user_id] = name
                    return name
            except Exception:
                pass
            _slack_user_cache[user_id] = user_id
            return user_id

        # Get conversations (single page only — fastest path)
        conv_types = 'public_channel,private_channel,im,mpim'
        convos_resp = http_requests.get(
            'https://slack.com/api/users.conversations',
            headers=slack_headers,
            params={'types': conv_types, 'limit': 200, 'exclude_archived': 'true'},
            timeout=8
        ).json()
        if not convos_resp.get('ok'):
            return jsonify({'error': f'Slack API error: {convos_resp.get("error", "unknown")}'}), 500
        all_convos = convos_resp.get('channels', [])

        # Optional filter: if channels specified, only show those
        if channel_names:
            name_set = set(channel_names)
            all_convos = [c for c in all_convos if c.get('name', '') in name_set]

        # Build channel list — DM names resolved from bulk cache
        target_channels = []
        for c in all_convos:
            ch_id = c['id']
            if c.get('is_im'):
                dm_uid = c.get('user', ch_id)
                ch_name = 'DM: ' + get_username(dm_uid)
            elif c.get('is_mpim'):
                ch_name = c.get('name', ch_id).replace('mpdm-', '').replace('--', ', ').rstrip('-')
            else:
                ch_name = c.get('name', ch_id)
            target_channels.append((ch_name, ch_id))

        # Limit to max_channels
        target_channels = target_channels[:max_channels]
        result['channels'] = [{'name': n, 'id': cid} for n, cid in target_channels]

        # Get team ID for slack:// deep links (cached persistently, via auth.test)
        if not _slack_team_id and not _over_budget():
            try:
                auth_resp = http_requests.get(
                    'https://slack.com/api/auth.test',
                    headers=bot_headers,
                    timeout=3
                ).json()
                if auth_resp.get('ok'):
                    _slack_team_id = auth_resp.get('team_id', '')
            except Exception:
                pass

        for ch_name, ch_id in target_channels:
            if _over_budget():
                result.setdefault('warnings', []).append('Time limit reached — showing partial results')
                break
            try:
                history = http_requests.get(
                    'https://slack.com/api/conversations.history',
                    headers=slack_headers,
                    params={'channel': ch_id, 'limit': 5},
                    timeout=5
                ).json()

                if not history.get('ok'):
                    continue

                for msg in history.get('messages', []):
                    if msg.get('subtype') in ('channel_join', 'channel_leave', 'channel_topic',
                                               'channel_purpose', 'bot_add', 'bot_remove'):
                        continue
                    ts = float(msg.get('ts', 0))
                    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
                    now = datetime.now(timezone.utc)
                    diff = now - dt
                    if diff.days == 0:
                        time_display = dt.strftime('%H:%M')
                    elif diff.days == 1:
                        time_display = 'Yesterday ' + dt.strftime('%H:%M')
                    elif diff.days < 7:
                        time_display = dt.strftime('%a %H:%M')
                    else:
                        time_display = dt.strftime('%d %b')

                    user_name = get_username(msg.get('user', ''))
                    text = msg.get('text', '')
                    # Replace Slack user mentions
                    for uid_match in _re.findall(r'<@(U[A-Z0-9]+)>', text):
                        text = text.replace(f'<@{uid_match}>', f'@{get_username(uid_match)}')
                    # Strip Slack URL formatting <http://...|label> -> label or url
                    text = _re.sub(r'<(https?://[^|>]+)\|([^>]+)>', r'\2', text)
                    text = _re.sub(r'<(https?://[^>]+)>', r'\1', text)

                    # Build slack:// deep link (opens in Slack app)
                    msg_link = ''
                    if _slack_team_id:
                        msg_link = f"slack://channel?team={_slack_team_id}&id={ch_id}&message={msg.get('ts', '')}"

                    result['messages'].append({
                        'channel': ch_name,
                        'channel_id': ch_id,
                        'user': user_name,
                        'text': text[:300],
                        'time': time_display,
                        'sort_ts': ts,
                        'link': msg_link,
                        'thread_ts': msg.get('thread_ts', ''),
                        'reply_count': msg.get('reply_count', 0),
                    })
            except Exception as e:
                result.setdefault('errors', []).append(f'#{ch_name}: {e}')

        result['messages'].sort(key=lambda x: x.get('sort_ts', 0), reverse=True)

    except Exception as e:
        return jsonify({'error': str(e)}), 500

    return jsonify(result)


@app.route('/work-search')
def work_search():
    """Search across Gmail, Calendar, and Slack."""
    query = request.args.get('q', '').strip()
    if not query:
        return jsonify({'emails': [], 'events': [], 'slack': []})

    DIR = os.path.dirname(os.path.abspath(__file__))
    result = {'emails': [], 'events': [], 'slack': []}

    # --- Gmail search ---
    GOOGLE_ACCOUNTS = [
        {'token': os.path.join(DIR, 'google_token.json'), 'label': 'Personal', 'color': '#52b788'},
        {'token': os.path.join(DIR, 'google_token_work.json'), 'label': 'Work', 'color': '#c9a96e'},
    ]
    active_accounts = [a for a in GOOGLE_ACCOUNTS if os.path.exists(a['token'])]

    if active_accounts:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build

        SCOPES = [
            'https://www.googleapis.com/auth/gmail.readonly',
            'https://www.googleapis.com/auth/calendar.readonly',
        ]

        for account in active_accounts:
            acct_label = account['label']
            acct_color = account['color']
            try:
                creds = Credentials.from_authorized_user_file(account['token'], SCOPES)
                if creds.expired and creds.refresh_token:
                    creds.refresh(Request())
                    with open(account['token'], 'w') as f:
                        f.write(creds.to_json())
            except Exception:
                continue

            # Search Gmail
            try:
                gmail = build('gmail', 'v1', credentials=creds, cache_discovery=False)
                msgs = gmail.users().messages().list(
                    userId='me', q=query, maxResults=10
                ).execute()

                for msg_meta in msgs.get('messages', []):
                    msg = gmail.users().messages().get(
                        userId='me', id=msg_meta['id'], format='metadata',
                        metadataHeaders=['From', 'Subject', 'Date']
                    ).execute()
                    headers = {h['name']: h['value'] for h in msg.get('payload', {}).get('headers', [])}
                    from_raw = headers.get('From', '')
                    from_name = from_raw.split('<')[0].strip().strip('"') if '<' in from_raw else from_raw

                    date_str = headers.get('Date', '')
                    time_display = ''
                    sort_ts = 0
                    try:
                        from email.utils import parsedate_to_datetime
                        dt = parsedate_to_datetime(date_str)
                        sort_ts = dt.timestamp()
                        time_display = dt.strftime('%d %b %H:%M')
                    except Exception:
                        time_display = date_str[:20] if date_str else ''

                    result['emails'].append({
                        'from': from_name,
                        'subject': headers.get('Subject', '(no subject)'),
                        'snippet': msg.get('snippet', ''),
                        'time': time_display,
                        'sort_ts': sort_ts,
                        'message_id': msg_meta['id'],
                        'thread_id': msg.get('threadId', ''),
                        'account': acct_label,
                        'account_color': acct_color,
                    })
            except Exception:
                pass

            # Search Calendar
            try:
                cal = build('calendar', 'v3', credentials=creds, cache_discovery=False)
                events_result = cal.events().list(
                    calendarId='primary', q=query,
                    singleEvents=True, orderBy='startTime',
                    maxResults=10
                ).execute()

                for ev in events_result.get('items', []):
                    start = ev.get('start', {})
                    end_t = ev.get('end', {})
                    sort_ts = 0
                    if 'dateTime' in start:
                        try:
                            from dateutil.parser import parse as dt_parse
                            st = dt_parse(start['dateTime'])
                            en = dt_parse(end_t.get('dateTime', start['dateTime']))
                            sort_ts = st.timestamp()
                            when = st.strftime('%a %d %b %H:%M') + ' - ' + en.strftime('%H:%M')
                        except Exception:
                            when = start['dateTime'][:16]
                    elif 'date' in start:
                        when = 'All day - ' + start['date']
                    else:
                        when = ''

                    result['events'].append({
                        'summary': ev.get('summary', '(no title)'),
                        'when': when,
                        'location': ev.get('location', ''),
                        'sort_ts': sort_ts,
                        'html_link': ev.get('htmlLink', ''),
                        'event_id': ev.get('id', ''),
                        'account': acct_label,
                        'account_color': acct_color,
                    })
            except Exception:
                pass

    # --- Slack search ---
    SLACK_CONFIG = os.path.join(DIR, 'slack_config.json')
    slack_cfg_search = None
    if os.path.exists(SLACK_CONFIG):
        try:
            with open(SLACK_CONFIG) as f:
                slack_cfg_search = json.load(f)
        except Exception:
            pass
    if not slack_cfg_search:
        try:
            import sys as _sys
            _sys.path.insert(0, os.path.dirname(DIR))
            from credentials import SLACK_USER_TOKEN, SLACK_BOT_TOKEN
            slack_cfg_search = {'token': SLACK_USER_TOKEN, 'bot_token': SLACK_BOT_TOKEN}
        except ImportError:
            pass
    if slack_cfg_search:
        try:
            # User token for search (requires search:read), bot token for user lookups
            s_user_token = slack_cfg_search.get('token', '') or slack_cfg_search.get('bot_token', '')
            s_bot_token = slack_cfg_search.get('bot_token', '') or s_user_token
            s_user_headers = {'Authorization': f'Bearer {s_user_token}'}
            s_bot_headers = {'Authorization': f'Bearer {s_bot_token}'}

            search_resp = http_requests.get(
                'https://slack.com/api/search.messages',
                headers=s_user_headers,
                params={'query': query, 'count': 10, 'sort': 'timestamp'},
                timeout=10
            ).json()

            if search_resp.get('ok'):
                matches = search_resp.get('messages', {}).get('matches', [])
                # Resolve user IDs to display names (use persistent cache + bot token)
                unique_uids = set(m.get('user', '') for m in matches if m.get('user'))
                for uid in list(unique_uids)[:10]:
                    if uid in _slack_user_cache:
                        continue
                    try:
                        uresp = http_requests.get(
                            'https://slack.com/api/users.info',
                            headers=s_bot_headers,
                            params={'user': uid},
                            timeout=3
                        ).json()
                        if uresp.get('ok'):
                            p = uresp['user'].get('profile', {})
                            _slack_user_cache[uid] = (p.get('display_name', '').strip()
                                                     or uresp['user'].get('real_name', '').strip()
                                                     or p.get('real_name', '').strip()
                                                     or uresp['user'].get('name', uid))
                    except Exception:
                        pass
                for match in matches:
                    ts = float(match.get('ts', 0))
                    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
                    time_display = dt.strftime('%d %b %H:%M')
                    ch_name = match.get('channel', {}).get('name', '')
                    ch_id = match.get('channel', {}).get('id', '')
                    uid = match.get('user', '')
                    display_name = _slack_user_cache.get(uid, match.get('username', uid))
                    # Build slack:// deep link for Slack app
                    msg_link = ''
                    if _slack_team_id and ch_id:
                        msg_link = f"slack://channel?team={_slack_team_id}&id={ch_id}&message={match.get('ts', '')}"
                    else:
                        msg_link = match.get('permalink', '')
                    result['slack'].append({
                        'channel': ch_name,
                        'user': display_name,
                        'text': match.get('text', '')[:300],
                        'time': time_display,
                        'sort_ts': ts,
                        'link': msg_link,
                    })
        except Exception:
            pass

    result['emails'].sort(key=lambda x: x.get('sort_ts', 0), reverse=True)
    result['events'].sort(key=lambda x: x.get('sort_ts', 0))
    result['slack'].sort(key=lambda x: x.get('sort_ts', 0), reverse=True)

    return jsonify(result)


@app.route('/work-ai-search', methods=['POST'])
def work_ai_search():
    """AI-powered search over work data using Claude Haiku."""
    import queue as _queue

    data = request.json or {}
    query = data.get('query', '').strip()
    if not query:
        return jsonify({'error': 'No query provided'}), 400

    emails = data.get('emails', [])
    events = data.get('events', [])
    slack = data.get('slack', [])

    # Build context for Claude
    parts = []
    if emails:
        lines = []
        for e in emails[:30]:
            lines.append(f"- From: {e.get('from','')} | Subject: {e.get('subject','')} | {e.get('time','')}\n  {e.get('snippet','')}")
        parts.append(f"EMAILS ({len(emails)}):\n" + "\n".join(lines))
    if events:
        lines = []
        for ev in events[:20]:
            loc = f" | Location: {ev['location']}" if ev.get('location') else ''
            att = f" | With: {ev['attendees']}" if ev.get('attendees') else ''
            lines.append(f"- {ev.get('summary','')} | {ev.get('when','')}{loc}{att}")
        parts.append(f"CALENDAR ({len(events)}):\n" + "\n".join(lines))
    if slack:
        lines = []
        for m in slack[:40]:
            lines.append(f"- #{m.get('channel','')} | {m.get('user','')}: {m.get('text','')} ({m.get('time','')})")
        parts.append(f"SLACK ({len(slack)}):\n" + "\n".join(lines))

    context = "\n\n".join(parts) if parts else "(No work data loaded yet)"

    prompt = f"""You are a helpful work assistant. The user has the following emails, calendar events and Slack messages loaded. Answer their question based on this data. Be concise and direct. If you reference specific items, mention enough detail to identify them (sender, subject, channel, time etc).

{context}

USER QUESTION: {query}"""

    env = dict(os.environ)
    for key in ['CLAUDE_CODE_ENTRYPOINT', 'CLAUDECODE', 'CLAUDE_CODE_SESSION']:
        env.pop(key, None)
    env['HOME'] = os.path.expanduser('~')
    env['PATH'] = '/Users/timtrailor/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin'
    env.pop('ANTHROPIC_API_KEY', None)

    cmd = [
        'claude', '-p', prompt,
        '--model', 'claude-haiku-4-5-20251001',
        '--max-turns', '1',
        '--output-format', 'stream-json',
        '--verbose'
    ]

    def generate():
        sse_queue = _queue.Queue()
        SAFARI_PAD = ": " + "x" * 256 + "\n\n"
        yield ": " + "x" * 2048 + "\n\n"

        try:
            import tempfile
            output_fd, output_path = tempfile.mkstemp(
                suffix='.jsonl', prefix='claude_ai_search_', dir='/tmp')
            output_handle = os.fdopen(output_fd, 'wb', buffering=0)
            try:
                proc = subprocess.Popen(
                    cmd, stdout=output_handle, stderr=subprocess.PIPE,
                    cwd=os.path.expanduser('~'), env=env
                )
            finally:
                output_handle.close()

            def _reader():
                raw_buffer = b""
                try:
                    with open(output_path, 'rb') as f:
                        while True:
                            chunk = f.read(8192)
                            if chunk:
                                raw_buffer += chunk
                                while b'\n' in raw_buffer:
                                    line_bytes, raw_buffer = raw_buffer.split(b'\n', 1)
                                    line = line_bytes.decode('utf-8', errors='replace').strip()
                                    if not line:
                                        continue
                                    try:
                                        event = json.loads(line)
                                        etype = event.get('type', '')
                                        if etype == 'assistant':
                                            for block in event.get('message', {}).get('content', []):
                                                if block.get('type') == 'text' and block.get('text'):
                                                    sse_queue.put(f"data: {json.dumps({'type': 'text', 'content': block['text']})}\n\n")
                                        elif etype == 'result':
                                            cost = event.get('total_cost_usd', 0)
                                            dur = event.get('duration_ms', 0)
                                            if cost > 0:
                                                sse_queue.put(f"data: {json.dumps({'type': 'cost', 'content': f'${cost:.4f} | {dur/1000:.1f}s'})}\n\n")
                                    except json.JSONDecodeError:
                                        pass
                            else:
                                if proc.poll() is not None:
                                    time.sleep(0.3)
                                    final = f.read()
                                    if final:
                                        raw_buffer += final
                                        while b'\n' in raw_buffer:
                                            line_bytes, raw_buffer = raw_buffer.split(b'\n', 1)
                                            line = line_bytes.decode('utf-8', errors='replace').strip()
                                            if line:
                                                try:
                                                    event = json.loads(line)
                                                    if event.get('type') == 'assistant':
                                                        for block in event.get('message', {}).get('content', []):
                                                            if block.get('type') == 'text' and block.get('text'):
                                                                sse_queue.put(f"data: {json.dumps({'type': 'text', 'content': block['text']})}\n\n")
                                                except json.JSONDecodeError:
                                                    pass
                                    break
                                time.sleep(0.2)
                except Exception:
                    pass
                finally:
                    proc.wait()
                    try:
                        os.unlink(output_path)
                    except Exception:
                        pass
                    sse_queue.put(None)

            reader = threading.Thread(target=_reader, daemon=True)
            reader.start()

            while True:
                try:
                    item = sse_queue.get(timeout=2.0)
                    if item is None:
                        break
                    yield item + SAFARI_PAD
                except _queue.Empty:
                    yield f"data: {json.dumps({'type': 'ping'})}\n\n" + SAFARI_PAD

        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'content': str(e)})}\n\n"

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache',
                             'X-Accel-Buffering': 'no',
                             'Connection': 'keep-alive'})


@app.route('/cancel', methods=['POST'])
def cancel():
    """Kill the current running Claude process."""
    global current_proc
    with current_proc_lock:
        if current_proc and current_proc.poll() is None:
            import signal
            try:
                os.killpg(os.getpgid(current_proc.pid), signal.SIGTERM)
            except (ProcessLookupError, OSError):
                try:
                    current_proc.terminate()
                except Exception:
                    pass
            current_proc = None
            _clear_work_state()
            return jsonify({'status': 'cancelled'})
    return jsonify({'status': 'nothing_running'})


@app.route('/new-session', methods=['POST'])
def new_session():
    global current_session_id
    current_session_id = None
    return 'ok'


@app.route('/clear', methods=['POST'])
def clear():
    return 'ok'


# --- Governors: Reverse proxy to Streamlit ---

@app.route('/governors/')
@app.route('/governors/<path:path>', methods=['GET', 'POST', 'PUT', 'DELETE'])
def proxy_governors(path=''):
    """Reverse proxy HTTP requests to Streamlit, making it same-origin for cookies."""
    target = f'{STREAMLIT_URL}/{path}'
    if request.query_string:
        target += f'?{request.query_string.decode()}'

    # Forward headers, replacing Host
    headers = {k: v for k, v in request.headers if k.lower() not in ('host', 'content-length')}
    headers['Host'] = f'{STREAMLIT_IP}:{STREAMLIT_PORT}'

    try:
        resp = http_requests.request(
            method=request.method,
            url=target,
            headers=headers,
            data=request.get_data(),
            cookies=request.cookies,
            allow_redirects=False,
            stream=True,
            timeout=15
        )

        # Build response, rewriting any Location headers
        excluded = {'content-encoding', 'transfer-encoding', 'connection', 'content-length'}
        resp_headers = [(k, v) for k, v in resp.raw.headers.items() if k.lower() not in excluded]

        # Rewrite absolute URLs in Location header
        resp_headers = [
            (k, v.replace(STREAMLIT_URL, '/governors')) if k.lower() == 'location' else (k, v)
            for k, v in resp_headers
        ]

        content = resp.content

        # For HTML responses, rewrite asset paths to go through proxy
        ct = resp.headers.get('content-type', '')
        if 'text/html' in ct:
            content = content.replace(
                b'/_stcore/', b'/governors/_stcore/'
            ).replace(
                b'"/_stcore/', b'"/governors/_stcore/'
            )

        return Response(content, status=resp.status_code, headers=resp_headers,
                        content_type=resp.headers.get('content-type'))
    except http_requests.exceptions.ConnectionError:
        return Response('<h3 style="color:#e55;text-align:center;margin-top:40px;">'
                        'Governors app not running.<br>Start it with: '
                        '<code>streamlit run app.py</code></h3>',
                        status=502, content_type='text/html')


@sock.route('/governors/_stcore/stream')
def proxy_governors_ws(ws):
    """Reverse proxy WebSocket to Streamlit's _stcore/stream."""
    # Build upstream WebSocket URL
    qs = request.query_string.decode()
    upstream_url = f'ws://{STREAMLIT_IP}:{STREAMLIT_PORT}/_stcore/stream'
    if qs:
        upstream_url += f'?{qs}'

    # Forward cookies as header
    cookie_header = request.headers.get('Cookie', '')

    try:
        upstream = ws_client.create_connection(
            upstream_url,
            header=[f'Cookie: {cookie_header}'] if cookie_header else [],
            timeout=10
        )
    except Exception:
        return

    closed = threading.Event()

    def upstream_to_client():
        """Forward messages from Streamlit to the browser."""
        while not closed.is_set():
            try:
                opcode, data = upstream.recv_data(control_frame=False)
                if opcode == 1:  # text
                    ws.send(data.decode('utf-8', errors='replace'))
                elif opcode == 2:  # binary
                    ws.send(data)
                else:
                    break
            except Exception:
                break
        closed.set()

    t = threading.Thread(target=upstream_to_client, daemon=True)
    t.start()

    # Forward messages from browser to Streamlit
    while not closed.is_set():
        try:
            data = ws.receive(timeout=30)
            if data is None:
                break
            if isinstance(data, bytes):
                upstream.send_binary(data)
            else:
                upstream.send(data)
        except Exception:
            break

    closed.set()
    try:
        upstream.close()
    except Exception:
        pass


@app.route('/restart', methods=['POST'])
def restart_server():
    """Restart the Flask server — shut down cleanly before re-exec to avoid port conflict.

    If a Claude subprocess is active, it keeps running (stdout goes to a temp file,
    not a pipe) and the new wrapper instance reattaches via /pending-work + /reattach.
    """
    import signal
    # Detach reader thread from subprocess — it will die with the old process,
    # but the Claude subprocess survives because its stdout goes to a file.
    with current_proc_lock:
        active = current_proc is not None and current_proc.poll() is None
    def _restart():
        time.sleep(0.3)
        os.kill(os.getpid(), signal.SIGINT)
        time.sleep(1)
        os.execv(sys.executable, [sys.executable] + sys.argv)
    threading.Thread(target=_restart, daemon=True).start()
    msg = 'Restarting... (active work will resume automatically)' if active else 'Restarting...'
    return msg, 200


if __name__ == '__main__':
    import socket
    from werkzeug.serving import make_server

    topic_count = len(glob.glob(os.path.join(TOPICS_DIR, "*.md")))
    print(f"\n  Claude Code Mobile")
    print(f"  Local:     http://localhost:8080")
    print(f"  iPhone:    http://100.112.125.42:8080")
    print(f"  Memory:    {topic_count} topic files")
    print(f"  Work dir:  {WORK_DIR}")
    print(f"  Features:  Full CLI, streaming, tool visibility, session continuity\n")

    server = make_server('0.0.0.0', 8080, app, threaded=True)
    server.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
