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
import urllib.request
import urllib.parse
from datetime import datetime, timedelta, timezone
import requests as http_requests
import websocket as ws_client
from flask import Flask, request, Response, render_template_string, jsonify, send_file
from flask_sock import Sock
from werkzeug.utils import secure_filename

app = Flask(__name__)
sock = Sock(app)

# --- Work tab background cache ---
# Fetches Gmail, Calendar, Slack in background every 60s.
# Endpoints serve from cache instantly (<1ms).
_work_cache = {
    'work_status': None,       # cached /work-status response dict
    'slack_messages': None,    # cached /slack-messages response dict
    'last_refresh': 0,         # monotonic timestamp of last successful refresh
    'refreshing': False,       # prevent concurrent refreshes
    'error': None,             # last error (if any)
}
_WORK_CACHE_TTL = 60  # seconds between background refreshes

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
WORK_DIR = os.path.expanduser("~/code/claude-mobile")
UPLOAD_DIR = os.path.expanduser("~/code/claude-mobile/uploads")
# API_KEY removed — wrapper uses Claude Code subscription auth, not API billing
os.makedirs(UPLOAD_DIR, exist_ok=True)

# Conversation state is now managed by the conversation server daemon (port 8081).
# The wrapper is a thin read-only proxy for all chat/conversation routes.


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
<script>if('serviceWorker' in navigator){navigator.serviceWorker.register('/sw.js');}</script>
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
.printer-subtabs {
    display: flex; padding: 8px 12px 0; gap: 0; flex-shrink: 0;
    border-bottom: 1px solid #2a2a4a;
}
.printer-subtab {
    flex: 1; padding: 8px 0; text-align: center; font-size: 13px; font-weight: 600;
    color: #666; background: none; border: none; border-bottom: 2px solid transparent;
    cursor: pointer; transition: color 0.2s, border-color 0.2s;
}
.printer-subtab.active { color: #c9a96e; border-bottom-color: #c9a96e; }
.printer-subtab .subtab-state {
    font-size: 9px; display: block; font-weight: 400; margin-top: 1px;
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
    <div class="printer-subtabs" id="printerSubtabBar">
        <button class="printer-subtab active" id="psSv08" onclick="window.switchPrinterSubtab('sv08')">SV08 Max<span class="subtab-state" id="psSv08State"></span></button>
        <button class="printer-subtab" id="psA1" onclick="window.switchPrinterSubtab('a1')">Bambu A1<span class="subtab-state" id="psA1State"></span></button>
    </div>
    <div class="printer-dash" id="printerSv08" style="display:block;">
        <div class="printer-loading">Loading...</div>
    </div>
    <div class="printer-dash" id="printerA1" style="display:none;">
        <div class="printer-loading">Loading...</div>
    </div>
</div>

<!-- Governors tab -->
<div id="governorsTab" class="tab-content">
    <iframe id="governorsFrame" style="width:100%;height:100%;border:none;background:#1a1a2e;"
            allow="microphone"></iframe>
</div>

<!-- Work tab -->
<div id="workTab" class="tab-content">
    <div style="font-size:10px;color:#555;text-align:right;padding:2px 8px;" id="workDebug">v4-smart</div>
    <div class="work-dash" id="workDash">
        <div class="printer-loading" id="workLoading">Loading work status...</div>
    </div>
    <div class="work-dash" id="workDashReady" style="display:none;">
        <div class="work-search-bar">
            <input type="text" id="workSearchInput" placeholder="Search emails, calendar, slack..." onkeydown="if(event.key==='Enter')window.workSearch()">
            <button onclick="window.workSearch()">Search</button>
        </div>
        <div id="smartSearchArea" style="display:none;overflow-y:auto;flex:1;-webkit-overflow-scrolling:touch;padding:12px;">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">
                <span id="smartSearchQuery" style="font-size:12px;color:#888;"></span>
                <button onclick="window.clearWorkSearch()" style="background:#2a2a4a;color:#c9a96e;border:1px solid #3a3a5a;padding:6px 14px;border-radius:16px;font-size:12px;cursor:pointer;">Clear</button>
            </div>
            <div id="smartSearchStatus" style="font-size:12px;color:#a78bfa;margin-bottom:8px;"></div>
            <div id="smartSearchAnswer" style="display:none;background:#1e1e3a;border:1px solid #3a3a5a;border-radius:12px;padding:12px;margin-bottom:12px;">
                <div style="font-size:11px;color:#a78bfa;font-weight:600;margin-bottom:6px;">Claude</div>
                <div id="smartAnswerText" style="font-size:13px;color:#ddd;line-height:1.6;white-space:pre-wrap;word-break:break-word;"></div>
                <div id="smartSearchCost" style="font-size:10px;color:#555;margin-top:8px;"></div>
            </div>
            <div id="smartSearchResults"></div>
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
    var wasTruncated = false;
    var autoContCount = 0;  // Track consecutive auto-continues to prevent loops
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

        // Auto-continue: if response was truncated by turn limit, automatically
        // send "Continue" (up to 3 times to prevent infinite loops)
        if (wasTruncated && !wasCancelled && autoContCount < 3) {
            wasTruncated = false;
            autoContCount++;
            addMsg('msg system', '<span style="color:#88aacc;">Auto-continuing (' + autoContCount + '/3)...</span>');
            setTimeout(function() { sendMessage('Continue from where you left off.', [], true); }, 500);
            return;
        }
        wasTruncated = false;
        autoContCount = 0;

        if (messageQueue.length > 0) {
            var next = messageQueue.shift();
            var tag = next.div && next.div.querySelector('.queue-tag');
            if (tag) tag.remove();
            var rmBtn = next.div && next.div.querySelector('.queue-remove');
            if (rmBtn) rmBtn.remove();
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
            if (idle > 45000) {
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
        var hadCost = false;
        lastOffset = 0;  // Reset offset for new conversation turn
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
                    if (result.done) {
                        // Stream ended — verify we got everything
                        fetch('/last-response').then(function(r) { return r.json(); }).then(function(lr) {
                            if (lr.text && lr.text.length > fullText.length) {
                                if (!assistantDiv) assistantDiv = addMsg('msg assistant', '');
                                fullText = lr.text;
                                assistantDiv.innerHTML = formatOutput(fullText);
                                scrollToBottom(true);
                            }
                            if (lr.cost && !hadCost) addMsg('cost', lr.cost);
                        }).catch(function() {}).finally(function() { finishSend(); });
                        return;
                    }
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
                            if (data.offset !== undefined) lastOffset = data.offset + 1;
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
                                hadCost = true;
                                addMsg('cost', data.content);
                                // Cost is always the final event — call directly
                                // AND via timeout (belt + suspenders for Safari)
                                finishSend();
                                setTimeout(finishSend, 500);
                            } else if (data.type === 'done') {
                                if (data.truncated) wasTruncated = true;
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
                                    // Reuse existing div if present, else create new
                                    if (!assistantDiv) assistantDiv = addMsg('msg assistant', '', true);
                                    fullText = lr.text;
                                    assistantDiv.innerHTML = formatOutput(fullText);
                                    if (lr.cost) addMsg('cost', lr.cost);
                                    scrollToBottom(true);
                                }
                            }).catch(function() {}).finally(function() { finishSend(); });
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
    var lastOffset = 0;  // Track conversation server event offset for reconnection

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
                if (idle > 45000) { finishSend(); }
            }, 10000);

            fetch('/reattach?offset=' + lastOffset, {signal: currentAbortController.signal}).then(function(response) {
                var reader = response.body.getReader();
                var decoder = new TextDecoder();
                var buffer = '';

                function readChunk() {
                    return reader.read().then(function(result) {
                        if (result.done) {
                            // Stream ended — verify we got everything
                            fetch('/last-response').then(function(r) { return r.json(); }).then(function(lr) {
                                if (lr.text && lr.text.length > fullText.length) {
                                    if (!assistantDiv) assistantDiv = addMsg('msg assistant', '');
                                    fullText = lr.text;
                                    assistantDiv.innerHTML = formatOutput(fullText);
                                    scrollToBottom(true);
                                }
                                if (lr.cost) addMsg('cost', lr.cost);
                            }).catch(function() {}).finally(function() { finishSend(); });
                            return;
                        }
                        lastActivityTime = Date.now();
                        buffer += decoder.decode(result.value, {stream: true});
                        var lines = buffer.split('\\n');
                        buffer = lines.pop();
                        for (var i = 0; i < lines.length; i++) {
                            if (!lines[i].startsWith('data: ')) continue;
                            try {
                                var d = JSON.parse(lines[i].slice(6));
                                if (d.offset !== undefined) lastOffset = d.offset + 1;
                                if (d.type === 'init') {
                                    touchWorkingIndicator();
                                    if (d.session_id) sessionId = d.session_id;
                                } else if (d.type === 'tool') {
                                    touchWorkingIndicator(d.content);
                                } else if (d.type === 'activity' || d.type === 'ping') {
                                    touchWorkingIndicator();
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
                                        if (!assistantDiv) assistantDiv = addMsg('msg assistant', '', true);
                                        fullText = lr.text;
                                        assistantDiv.innerHTML = formatOutput(fullText);
                                        if (lr.cost) addMsg('cost', lr.cost);
                                        scrollToBottom(true);
                                    }
                                }).catch(function() {}).finally(function() { finishSend(); });
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

    function renderEtaGraph(history, currentProgress) {
        if (!history || history.length < 2) return '';
        var W = 280, H = 110, PAD_L = 50, PAD_R = 8, PAD_T = 14, PAD_B = 20;
        var gW = W - PAD_L - PAD_R;
        var gH = H - PAD_T - PAD_B;
        var n = history.length;

        // Find min/max finish timestamps for Y axis
        var minTs = Infinity, maxTs = -Infinity;
        for (var i = 0; i < n; i++) {
            var t = history[i].finish_ts;
            if (t < minTs) minTs = t;
            if (t > maxTs) maxTs = t;
        }
        // Add 5% padding to Y range
        var range = maxTs - minTs;
        if (range < 600) range = 600; // minimum 10 min range
        var pad = range * 0.05;
        var yMin = minTs - pad;
        var yMax = maxTs + pad;
        var yRange = yMax - yMin;

        // X axis: elapsed time (hours) — auto-scales to data available
        var xMin = history[0].elapsed_h;
        var xMax = history[n - 1].elapsed_h;
        if (xMax - xMin < 0.1) xMax = xMin + 0.1;

        function xPos(eh) { return PAD_L + ((eh - xMin) / (xMax - xMin)) * gW; }
        function yPos(ts) { return PAD_T + gH - ((ts - yMin) / yRange) * gH; }

        var svg = '<div style="margin:6px 0;padding:6px 8px;background:#1a1a2e;border-radius:8px;">';
        svg += '<div style="font-size:10px;color:#888;margin-bottom:4px;">ETA HISTORY (predicted finish time over elapsed time)</div>';
        svg += '<svg width="100%" viewBox="0 0 ' + W + ' ' + H + '" style="display:block;">';

        // Y-axis grid lines (time labels)
        var nGrid = 4;
        for (var g = 0; g <= nGrid; g++) {
            var ts = yMin + (g / nGrid) * yRange;
            var gy = yPos(ts);
            svg += '<line x1="' + PAD_L + '" y1="' + gy + '" x2="' + (W - PAD_R) + '" y2="' + gy + '" stroke="#333" stroke-width="0.5"/>';
            var d = new Date(ts * 1000);
            var days = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
            var lbl = days[d.getDay()] + ' ' + ('0' + d.getHours()).slice(-2) + ':' + ('0' + d.getMinutes()).slice(-2);
            svg += '<text x="' + (PAD_L - 3) + '" y="' + (gy + 3) + '" fill="#666" font-size="7" text-anchor="end">' + lbl + '</text>';
        }

        // X-axis baseline
        var xAxisY = PAD_T + gH;
        svg += '<line x1="' + PAD_L + '" y1="' + xAxisY + '" x2="' + (W - PAD_R) + '" y2="' + xAxisY + '" stroke="#444" stroke-width="0.5"/>';

        // X-axis labels (elapsed hours) — pick clean tick values
        var xRange = xMax - xMin;
        var xSteps = [];
        if (xRange <= 1) {
            // Under 1h: show labels in minutes
            var stepMin = xRange * 60 <= 15 ? 5 : xRange * 60 <= 30 ? 10 : 15;
            for (var m = Math.ceil(xMin * 60 / stepMin) * stepMin; m <= xMax * 60; m += stepMin) {
                xSteps.push({val: m / 60, lbl: Math.round(m) + 'm'});
            }
        } else {
            // Over 1h: show labels in hours
            var stepH = xRange <= 4 ? 0.5 : xRange <= 10 ? 1 : xRange <= 24 ? 2 : 4;
            for (var hh = Math.ceil(xMin / stepH) * stepH; hh <= xMax; hh += stepH) {
                xSteps.push({val: hh, lbl: (hh % 1 === 0 ? hh : hh.toFixed(1)) + 'h'});
            }
        }
        // Always include first and last if no ticks generated
        if (xSteps.length === 0) {
            xSteps.push({val: xMin, lbl: xMin < 1 ? Math.round(xMin * 60) + 'm' : xMin.toFixed(1) + 'h'});
            xSteps.push({val: xMax, lbl: xMax < 1 ? Math.round(xMax * 60) + 'm' : xMax.toFixed(1) + 'h'});
        }
        for (var xi = 0; xi < xSteps.length; xi++) {
            var tx = xPos(xSteps[xi].val);
            // Tick mark
            svg += '<line x1="' + tx + '" y1="' + xAxisY + '" x2="' + tx + '" y2="' + (xAxisY + 3) + '" stroke="#444" stroke-width="0.5"/>';
            // Vertical gridline
            svg += '<line x1="' + tx + '" y1="' + PAD_T + '" x2="' + tx + '" y2="' + xAxisY + '" stroke="#333" stroke-width="0.3" stroke-dasharray="2,2"/>';
            // Label
            svg += '<text x="' + tx + '" y="' + (H - 4) + '" fill="#666" font-size="7" text-anchor="middle">' + xSteps[xi].lbl + '</text>';
        }

        // Draw the ETA line
        var path = '';
        for (var i = 0; i < n; i++) {
            var px = xPos(history[i].elapsed_h);
            var py = yPos(history[i].finish_ts);
            path += (i === 0 ? 'M' : 'L') + px.toFixed(1) + ' ' + py.toFixed(1);
        }
        svg += '<path d="' + path + '" fill="none" stroke="#c9a96e" stroke-width="1.5"/>';

        // Dots at start and end
        var first = history[0], last = history[n - 1];
        svg += '<circle cx="' + xPos(first.elapsed_h) + '" cy="' + yPos(first.finish_ts) + '" r="2.5" fill="#c9a96e" stroke="#fff" stroke-width="0.5"/>';
        svg += '<circle cx="' + xPos(last.elapsed_h) + '" cy="' + yPos(last.finish_ts) + '" r="3" fill="#c9a96e" stroke="#fff" stroke-width="0.5"/>';

        // Current ETA label at the end (Day HH:MM format)
        var ld = new Date(last.finish_ts * 1000);
        var lds = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
        var etaLbl = lds[ld.getDay()] + ' ' + ('0' + ld.getHours()).slice(-2) + ':' + ('0' + ld.getMinutes()).slice(-2);
        svg += '<text x="' + (xPos(last.elapsed_h) - 3) + '" y="' + (yPos(last.finish_ts) - 6) + '" fill="#c9a96e" font-size="9" text-anchor="end">' + etaLbl + '</text>';

        // Drift indicator
        var driftMin = Math.round((last.finish_ts - first.finish_ts) / 60);
        var driftColor = Math.abs(driftMin) < 15 ? '#4a7' : driftMin > 0 ? '#e55' : '#4a7';
        var driftLabel = driftMin > 0 ? '+' + driftMin + 'min' : driftMin + 'min';
        if (Math.abs(driftMin) < 2) driftLabel = 'stable';
        svg += '<text x="' + (W - PAD_R) + '" y="' + (PAD_T - 2) + '" fill="' + driftColor + '" font-size="9" text-anchor="end">Drift: ' + driftLabel + '</text>';

        svg += '</svg>';
        svg += '<div style="font-size:9px;color:#666;display:flex;gap:10px;margin-top:2px;">';
        svg += '<span><span style="color:#c9a96e;">&#9644;</span> Predicted finish</span>';
        svg += '<span style="color:#888;">Flat = accurate, rising = slipping</span>';
        svg += '</div></div>';
        return svg;
    }

    function renderAlphaGraph(graph, currentLayer) {
        var W = 280, H = 100, PAD_L = 30, PAD_R = 8, PAD_T = 14, PAD_B = 20;
        var gW = W - PAD_L - PAD_R;
        var gH = H - PAD_T - PAD_B;
        var n = graph.length;
        if (n < 2) return '';

        // Collect alpha values, skip layers with no alpha
        var pts = [];
        for (var i = 0; i < n; i++) {
            if (graph[i].alpha !== undefined && graph[i].alpha !== null) {
                var a = Math.min(graph[i].alpha, 5.0); // cap display at 5
                pts.push({layer: graph[i].layer, alpha: a, idx: i});
            }
        }
        if (pts.length < 2) return '';

        var minA = Infinity, maxA = -Infinity;
        for (var i = 0; i < pts.length; i++) {
            if (pts[i].alpha < minA) minA = pts[i].alpha;
            if (pts[i].alpha > maxA) maxA = pts[i].alpha;
        }
        minA = Math.max(0, Math.floor(minA * 10) / 10 - 0.1);
        maxA = Math.ceil(maxA * 10) / 10 + 0.1;
        var aRange = maxA - minA || 0.1;

        function xPos(idx) { return PAD_L + (idx / (pts.length - 1)) * gW; }
        function yPos(val) { return PAD_T + gH - ((val - minA) / aRange) * gH; }

        var svg = '<div style="margin:6px 0;padding:6px 8px;background:#1a1a2e;border-radius:8px;">';
        svg += '<div style="font-size:10px;color:#888;margin-bottom:4px;">COMPLEXITY PROFILE (\u03b1 per layer \u2014 higher = more complex geometry)</div>';
        svg += '<svg width="100%" viewBox="0 0 ' + W + ' ' + H + '" style="display:block;">';

        // Grid lines
        var gridSteps = [minA, minA + aRange * 0.33, minA + aRange * 0.66, maxA];
        for (var g = 0; g < gridSteps.length; g++) {
            var gy = yPos(gridSteps[g]);
            svg += '<line x1="' + PAD_L + '" y1="' + gy + '" x2="' + (W - PAD_R) + '" y2="' + gy + '" stroke="#333" stroke-width="0.5"/>';
            svg += '<text x="' + (PAD_L - 3) + '" y="' + (gy + 3) + '" fill="#666" font-size="8" text-anchor="end">' + gridSteps[g].toFixed(2) + '</text>';
        }

        // Current layer vertical line
        var curPtIdx = 0;
        for (var i = 0; i < pts.length; i++) {
            if (pts[i].layer <= currentLayer) curPtIdx = i;
        }
        var cx = xPos(curPtIdx);
        svg += '<line x1="' + cx + '" y1="' + PAD_T + '" x2="' + cx + '" y2="' + (H - PAD_B) + '" stroke="#88f" stroke-width="1" stroke-dasharray="2,2"/>';

        // Area fill under curve
        var areaPath = 'M' + xPos(0).toFixed(1) + ',' + (PAD_T + gH);
        for (var i = 0; i < pts.length; i++) {
            areaPath += 'L' + xPos(i).toFixed(1) + ',' + yPos(pts[i].alpha).toFixed(1);
        }
        areaPath += 'L' + xPos(pts.length - 1).toFixed(1) + ',' + (PAD_T + gH) + 'Z';
        svg += '<path d="' + areaPath + '" fill="#e8525522" stroke="none"/>';

        // Past line (solid)
        var pastPath = '';
        var futurePath = '';
        for (var i = 0; i < pts.length; i++) {
            var px = xPos(i);
            var py = yPos(pts[i].alpha);
            if (i <= curPtIdx) {
                pastPath += (pastPath ? 'L' : 'M') + px.toFixed(1) + ',' + py.toFixed(1);
            }
            if (i >= curPtIdx) {
                futurePath += (futurePath ? 'L' : 'M') + px.toFixed(1) + ',' + py.toFixed(1);
            }
        }
        if (pastPath) svg += '<path d="' + pastPath + '" fill="none" stroke="#e85255" stroke-width="1.5"/>';
        if (futurePath) svg += '<path d="' + futurePath + '" fill="none" stroke="#e85255" stroke-width="1" stroke-dasharray="3,2" opacity="0.6"/>';

        // Current position dot
        var cy = yPos(pts[curPtIdx].alpha);
        svg += '<circle cx="' + cx + '" cy="' + cy + '" r="3" fill="#88f" stroke="#fff" stroke-width="0.5"/>';

        // X-axis labels
        var xLabels = [0, Math.round(pts.length * 0.25), Math.round(pts.length * 0.5), Math.round(pts.length * 0.75), pts.length - 1];
        for (var i = 0; i < xLabels.length; i++) {
            var li = Math.min(xLabels[i], pts.length - 1);
            svg += '<text x="' + xPos(li) + '" y="' + (H - 4) + '" fill="#666" font-size="7" text-anchor="middle">' + pts[li].layer + '</text>';
        }

        svg += '</svg>';
        svg += '<div style="font-size:9px;color:#666;display:flex;gap:10px;margin-top:2px;">';
        svg += '<span><span style="color:#e85255;">&#9644;</span> Layer \u03b1</span>';
        svg += '<span><span style="color:#88f;">&#9679;</span> Current</span>';
        svg += '<span style="color:#888;">Low \u03b1 = simple, High \u03b1 = complex</span>';
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

        // Progress bar with layer info
        var prog = p.progress || 0;
        if (prog > 0) {
            h += '<div class="progress-wrap">';
            h += '<div class="progress-label"><span>Progress</span><span style="color:' + accent + ';font-weight:600;">' + (typeof prog === 'number' && prog % 1 !== 0 ? prog.toFixed(1) : prog) + '%</span></div>';
            h += '<div class="progress-bar"><div class="progress-fill" style="background:linear-gradient(90deg,#2d6a4f,' + accent + ');width:' + prog + '%;"></div></div>';
            if (p.total_layers > 0) {
                var cl = (p.current_layer != null) ? p.current_layer : '?';
                var lp = (p.layer_progress != null) ? ' (' + p.layer_progress + '%)' : '';
                h += '<div style="font-size:10px;color:#888;margin-top:2px;">Layer ' + cl + ' / ' + p.total_layers + lp + '</div>';
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

        // Alpha complexity graph (per-layer alpha from speed_graph)
        if (p.speed_graph && p.speed_graph.length > 1) {
            h += renderAlphaGraph(p.speed_graph, p.current_layer || 0);
        }

        // ETA history graph (predicted finish time over print progress)
        if (p.eta_history && p.eta_history.length >= 2) {
            h += renderEtaGraph(p.eta_history, p.progress || 0);
        }

        // Filament feed warning
        if (p.filament_warning) {
            h += '<div style="background:#e5522;border:1px solid #e55;border-radius:8px;padding:8px 12px;margin:8px 0;">';
            h += '<div style="font-size:13px;color:#e55;font-weight:600;">\u26a0 ' + esc(p.filament_warning) + '</div>';
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

    // Sub-tab state
    var currentPrinterTab = 'sv08';
    var sv08El = document.getElementById('printerSv08');
    var a1El = document.getElementById('printerA1');

    window.switchPrinterSubtab = function(tab) {
        currentPrinterTab = tab;
        sv08El.style.display = tab === 'sv08' ? '' : 'none';
        a1El.style.display = tab === 'a1' ? '' : 'none';
        document.getElementById('psSv08').classList.toggle('active', tab === 'sv08');
        document.getElementById('psA1').classList.toggle('active', tab === 'a1');
    };

    function updateSubtabStates(sv08Data, a1Data) {
        var sv08State = document.getElementById('psSv08State');
        var a1State = document.getElementById('psA1State');
        if (sv08State) {
            if (sv08Data.error) {
                sv08State.textContent = 'Offline';
                sv08State.style.color = '#888';
            } else {
                var s = (sv08Data.state || '').toLowerCase();
                sv08State.textContent = sv08Data.state || '';
                sv08State.style.color = stateColor(s);
            }
        }
        if (a1State) {
            if (a1Data.error) {
                a1State.textContent = 'Offline';
                a1State.style.color = '#888';
            } else {
                var s = (a1Data.state || '').toLowerCase();
                a1State.textContent = a1Data.state || '';
                a1State.style.color = stateColor(s);
            }
        }
    }

    function loadStatus() {
        fetch('/printer-status', {cache: 'no-store'}).then(function(r) { return r.json(); }).then(function(data) {
            var now = new Date();
            lastUpdate = now.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});

            var refreshHtml = '<div class="refresh-bar"><span>Updated: ' + lastUpdate + '</span>' +
                '<button onclick="window.refreshPrinters()" style="background:#2a2a4a;color:#c9a96e;border:1px solid #3a3a5a;padding:6px 14px;border-radius:16px;font-size:12px;cursor:pointer;">Refresh</button></div>';

            sv08El.innerHTML = refreshHtml + renderPrinterCard(data.sv08, 'Sovol SV08 Max', '#88f', 'sovol_camera.jpg', 'sovol_thumbnail.png');
            a1El.innerHTML = refreshHtml + renderPrinterCard(data.a1, 'Bambu A1', '#52b788', 'a1_camera.jpg', '');

            updateSubtabStates(data.sv08, data.a1);
        }).catch(function(e) {
            var errHtml = '<div class="printer-loading" style="color:#e55;">Failed to load status. <button onclick="window.refreshPrinters()" style="color:#c9a96e;background:#2a2a4a;border:1px solid #3a3a5a;padding:4px 12px;border-radius:12px;margin-top:8px;">Retry</button></div>';
            sv08El.innerHTML = errHtml;
            a1El.innerHTML = errHtml;
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
        var sa = document.getElementById('smartSearchArea');
        if (sa) sa.style.display = 'none';
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

    var _smartAbort = null;
    var smartArea = document.getElementById('smartSearchArea');
    var smartStatus = document.getElementById('smartSearchStatus');
    var smartAnswer = document.getElementById('smartSearchAnswer');
    var smartAnswerText = document.getElementById('smartAnswerText');
    var smartResults = document.getElementById('smartSearchResults');
    var smartCost = document.getElementById('smartSearchCost');
    var smartQueryEl = document.getElementById('smartSearchQuery');

    function renderSmartResults(results) {
        var html = '';
        if (results.emails && results.emails.length) {
            html += '<div class="search-group"><h3>Emails (' + results.emails.length + ')</h3>';
            for (var i = 0; i < results.emails.length; i++) {
                html += '<div id="smart-email-' + i + '">' + emailCard(results.emails[i]) + '</div>';
            }
            html += '</div>';
        }
        if (results.events && results.events.length) {
            html += '<div class="search-group"><h3>Calendar (' + results.events.length + ')</h3>';
            for (var j = 0; j < results.events.length; j++) {
                html += '<div id="smart-calendar-' + j + '">' + calCard(results.events[j]) + '</div>';
            }
            html += '</div>';
        }
        if (results.slack && results.slack.length) {
            html += '<div class="search-group"><h3>Slack (' + results.slack.length + ')</h3>';
            for (var k = 0; k < results.slack.length; k++) {
                html += '<div id="smart-slack-' + k + '">' + slackCard(results.slack[k]) + '</div>';
            }
            html += '</div>';
        }
        smartResults.innerHTML = html;
    }

    function scrollToSmartRef(evt) {
        evt.preventDefault();
        var el = document.getElementById(evt.target.dataset.ref);
        if (el) {
            el.scrollIntoView({behavior:'smooth',block:'center'});
            el.style.outline = '2px solid #a78bfa';
            setTimeout(function(){ el.style.outline = ''; }, 2000);
        }
    }

    function linkifyRefs(text) {
        // Turn [email:0], [calendar:2], [slack:1] into clickable links that scroll to the card
        return esc(text).replace(/\[(email|calendar|slack):(\d+)\]/g, function(match, type, idx) {
            return '<a href="#" data-ref="smart-' + type + '-' + idx + '" onclick="scrollToSmartRef(event)" style="color:#a78bfa;text-decoration:underline;cursor:pointer;">' + type + ' ' + idx + '</a>';
        });
    }

    window.workSearch = function() {
        var q = searchInput.value.trim();
        if (!q) {
            smartArea.style.display = 'none';
            searchResultsEl.style.display = 'none';
            showSubtabContent(currentSubtab);
            return;
        }
        // Hide subtabs, show smart search area
        document.getElementById('workSubtabBar').style.display = 'none';
        emailsEl.style.display = 'none';
        diaryEl.style.display = 'none';
        slackEl.style.display = 'none';
        searchResultsEl.style.display = 'none';
        smartArea.style.display = '';
        smartQueryEl.innerHTML = 'Searching: "' + esc(q) + '"';
        smartStatus.innerHTML = '<span style="color:#a78bfa;">Starting smart search...</span>';
        smartAnswer.style.display = 'none';
        smartAnswerText.innerHTML = '';
        smartResults.innerHTML = '';
        smartCost.innerHTML = '';

        if (_smartAbort) _smartAbort.abort();
        _smartAbort = new AbortController();

        fetch('/work-smart-search?q=' + encodeURIComponent(q), {signal: _smartAbort.signal}).then(function(resp) {
            var reader = resp.body.getReader();
            var decoder = new TextDecoder();
            var buf = '';

            function pump() {
                return reader.read().then(function(result) {
                    if (result.done) {
                        // If we never got an answer, show what we have
                        if (smartAnswer.style.display === 'none' && smartResults.innerHTML) {
                            smartStatus.innerHTML = '';
                        }
                        return;
                    }
                    buf += decoder.decode(result.value, {stream: true});
                    var lines = buf.split('\\n');
                    buf = lines.pop();
                    for (var i = 0; i < lines.length; i++) {
                        var line = lines[i].trim();
                        if (!line.startsWith('data: ')) continue;
                        try {
                            var ev = JSON.parse(line.substring(6));
                            if (ev.type === 'status') {
                                smartStatus.innerHTML = '<span style="color:#a78bfa;">' + esc(ev.content) + '</span>';
                            } else if (ev.type === 'answer') {
                                smartStatus.innerHTML = '';
                                smartAnswer.style.display = '';
                                smartAnswerText.innerHTML = linkifyRefs(ev.content);
                                if (ev.results) renderSmartResults(ev.results);
                            } else if (ev.type === 'cost') {
                                smartCost.innerHTML = esc(ev.content) + (ev.turns ? ' | ' + ev.turns + ' turn' + (ev.turns > 1 ? 's' : '') : '');
                            } else if (ev.type === 'error') {
                                smartStatus.innerHTML = '<span style="color:#e55;">' + esc(ev.content) + '</span>';
                            }
                        } catch(e) {}
                    }
                    return pump();
                });
            }
            return pump();
        }).catch(function(e) {
            if (e.name !== 'AbortError') {
                smartStatus.innerHTML = '<span style="color:#e55;">Smart search failed: ' + esc(e.message) + '</span>';
            }
        });
    };

    window.clearWorkSearch = function() {
        searchInput.value = '';
        smartArea.style.display = 'none';
        searchResultsEl.style.display = 'none';
        if (_smartAbort) { _smartAbort.abort(); _smartAbort = null; }
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


OFFLINE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<title>Claude Code — Reconnecting</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
    font-family: -apple-system, BlinkMacSystemFont, sans-serif;
    background: #1a1a2e; color: #e0e0e0;
    height: 100dvh; display: flex; flex-direction: column;
    align-items: center; justify-content: center; text-align: center;
    padding: 20px;
}
.spinner {
    width: 40px; height: 40px; border: 3px solid #2a2a4a;
    border-top-color: #c9a96e; border-radius: 50%;
    animation: spin 0.8s linear infinite; margin-bottom: 20px;
}
@keyframes spin { to { transform: rotate(360deg); } }
h1 { font-size: 20px; color: #c9a96e; margin-bottom: 8px; }
.status { font-size: 14px; color: #888; margin-bottom: 4px; }
.hint {
    font-size: 14px; color: #e0e0e0; margin-top: 16px; padding: 12px 18px;
    background: #2a2a4a; border-radius: 10px; border-left: 3px solid #c9a96e;
    display: none;
}
.attempt { font-size: 12px; color: #555; margin-top: 12px; }
</style>
</head>
<body>
<div class="spinner" id="spinner"></div>
<h1>Reconnecting...</h1>
<div class="status" id="status">Trying to reach server</div>
<div class="hint" id="hint">Check Tailscale is connected on your iPhone</div>
<div class="attempt" id="attempt"></div>
<script>
var attempts = 0;
var maxBackoff = 10000;
function tryReconnect() {
    attempts++;
    var el = document.getElementById('status');
    var hint = document.getElementById('hint');
    var att = document.getElementById('attempt');
    el.textContent = 'Attempting to reconnect...';
    att.textContent = 'Attempt ' + attempts;
    if (attempts >= 4) {
        hint.style.display = 'block';
    }
    fetch('/', {cache: 'no-store'}).then(function(r) {
        if (r.ok) {
            el.textContent = 'Connected! Reloading...';
            document.getElementById('spinner').style.borderTopColor = '#4a4';
            window.location.replace('/');
        } else {
            scheduleRetry();
        }
    }).catch(function() {
        el.textContent = 'Server unreachable';
        scheduleRetry();
    });
}
function scheduleRetry() {
    var delay = Math.min(3000 * Math.pow(1.3, attempts - 1), maxBackoff);
    setTimeout(tryReconnect, delay);
}
tryReconnect();
</script>
</body>
</html>"""

SW_JS = """
var CACHE_NAME = 'claude-offline-v1';
var OFFLINE_URL = '/offline';

self.addEventListener('install', function(event) {
    event.waitUntil(
        caches.open(CACHE_NAME).then(function(cache) {
            return cache.add(new Request(OFFLINE_URL, {cache: 'reload'}));
        }).then(function() { self.skipWaiting(); })
    );
});

self.addEventListener('activate', function(event) {
    event.waitUntil(
        caches.keys().then(function(names) {
            return Promise.all(
                names.filter(function(n) { return n !== CACHE_NAME; })
                     .map(function(n) { return caches.delete(n); })
            );
        }).then(function() { return self.clients.claim(); })
    );
});

self.addEventListener('fetch', function(event) {
    if (event.request.mode === 'navigate') {
        event.respondWith(
            fetch(event.request).catch(function() {
                return caches.match(OFFLINE_URL);
            })
        );
    }
});
"""


@app.route('/sw.js')
def service_worker():
    resp = Response(SW_JS, content_type='application/javascript')
    resp.headers['Cache-Control'] = 'no-cache'
    resp.headers['Service-Worker-Allowed'] = '/'
    return resp


@app.route('/offline')
def offline_page():
    resp = Response(OFFLINE_HTML, content_type='text/html')
    resp.headers['Cache-Control'] = 'no-cache'
    return resp


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


# ── Conversation server proxy ──
# All chat/conversation routes proxy to the conversation server daemon
# running on port 8081. The server owns the Claude CLI subprocess and
# persists events to disk, surviving wrapper restarts and connection drops.
CONV_SERVER = 'http://127.0.0.1:8081'


def _conv_proxy_get(path):
    """GET request to conversation server, return parsed JSON."""
    try:
        with urllib.request.urlopen(f'{CONV_SERVER}{path}', timeout=5) as r:
            return json.loads(r.read())
    except Exception as e:
        return {'error': str(e)}


def _conv_proxy_post(path, data=None):
    """POST request to conversation server, return parsed JSON."""
    try:
        body = json.dumps(data or {}).encode()
        req = urllib.request.Request(
            f'{CONV_SERVER}{path}', data=body,
            headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except Exception as e:
        return {'error': str(e)}


@app.route('/chat', methods=['POST'])
def chat():
    """Proxy: send message to conversation server, then stream events back."""
    data = request.json or {}

    # Forward the message to the conversation server
    result = _conv_proxy_post('/send', {
        'message': data.get('message', ''),
        'session_id': data.get('session_id'),
        'image_paths': data.get('image_paths', []),
    })

    if 'error' in result:
        def error_gen():
            yield ": " + "x" * 2048 + "\n\n"
            yield f"data: {json.dumps({'type': 'error', 'content': result['error']})}\n\n"
        return Response(error_gen(), mimetype='text/event-stream',
                        headers={'Cache-Control': 'no-cache',
                                 'X-Accel-Buffering': 'no',
                                 'Connection': 'keep-alive'})

    # Stream events from the conversation server
    return _stream_from_server(offset=0)


@app.route('/last-response')
def get_last_response():
    """Proxy: get last completed response from conversation server."""
    data = _conv_proxy_get('/last-response')
    return jsonify(data)


@app.route('/pending-work')
def pending_work():
    """Proxy: check if conversation server has an active process."""
    status = _conv_proxy_get('/status')
    if status.get('error'):
        return jsonify({'active': False, 'server_error': status['error']})
    active = status.get('status') == 'running' and status.get('alive', False)
    return jsonify({
        'active': active,
        'session_id': status.get('claude_sid', ''),
        'pid': status.get('pid'),
    })


@app.route('/reattach')
def reattach():
    """Proxy: reattach to running conversation via offset-based streaming.

    The conversation server keeps all events on disk.  Streaming from
    offset=0 replays the full history, then continues live — replacing
    the old snapshot-based reattach with a simpler, more robust approach.
    """
    offset = request.args.get('offset', 0, type=int)
    return _stream_from_server(offset=offset)


def _stream_from_server(offset=0):
    """SSE proxy: stream events from conversation server with reconnection support.

    Uses requests with stream=True and iter_content(chunk_size=1) for
    real-time forwarding — urllib.request buffers and delays delivery.
    """
    SAFARI_PAD = ": " + "x" * 256 + "\n\n"

    def generate():
        yield ": " + "x" * 2048 + "\n\n"  # Safari prime

        try:
            url = f'{CONV_SERVER}/stream?offset={offset}'
            resp = http_requests.get(url, stream=True, timeout=(5, 600))
            buffer = ""
            for chunk in resp.iter_content(chunk_size=1, decode_unicode=True):
                if not chunk:
                    continue
                buffer += chunk
                # Forward complete SSE event blocks (terminated by \n\n)
                while "\n\n" in buffer:
                    event_block, buffer = buffer.split("\n\n", 1)
                    line = event_block.strip()
                    if not line or line.startswith(":"):
                        continue  # Skip comments/padding from server
                    yield line + "\n\n" + SAFARI_PAD
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'content': f'Conversation server: {e}'})}\n\n"

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache',
                             'X-Accel-Buffering': 'no',
                             'Connection': 'keep-alive'})


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
    """Read-only: serve cached printer status from daemon output."""
    status_file = '/tmp/printer_status/status.json'
    try:
        with open(status_file) as f:
            data = json.load(f)
    except FileNotFoundError:
        return jsonify({'sv08': {'error': 'Daemon not running'},
                        'a1': {'error': 'Daemon not running'}})
    except Exception as e:
        return jsonify({'sv08': {'error': str(e)},
                        'a1': {'error': str(e)}})

    result = {'sv08': {}, 'a1': {}}

    # SV08 — daemon already computes everything
    sovol = data.get('printers', {}).get('sovol', {})
    if sovol.get('online'):
        # Capitalize state to match frontend expectations
        if 'state' in sovol:
            sovol['state'] = (sovol['state'] or 'unknown').capitalize()
        result['sv08'] = sovol
    else:
        result['sv08'] = {'error': sovol.get('error', 'Offline')}

    # Bambu A1 — reshape daemon keys to match frontend expectations
    bambu = data.get('printers', {}).get('bambu', {})
    if bambu.get('online'):
        a1 = dict(bambu)
        # Map daemon field names to what the frontend expects
        a1.setdefault('layer', a1.pop('layer_num', None))
        a1.setdefault('total_layers', a1.pop('total_layer_num', None))
        speed_names = {1: 'Silent', 2: 'Standard', 3: 'Sport', 4: 'Ludicrous'}
        a1['speed'] = speed_names.get(a1.get('speed_level'), '?')
        result['a1'] = a1
    else:
        result['a1'] = {'error': bambu.get('error', 'Offline')}

    # Add daemon freshness indicator
    result['daemon_timestamp'] = data.get('timestamp', '')

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
    # Don't serve stale camera images (>10 min old) — misleading
    age = time.time() - os.path.getmtime(path)
    if age > 600 and name.endswith('_camera.jpg'):
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


def _fetch_work_status():
    """Background fetch: unread + last 7 days Gmail, 40-day calendar window."""
    DIR = os.path.dirname(os.path.abspath(__file__))

    GOOGLE_ACCOUNTS = [
        {'token': os.path.join(DIR, 'google_token.json'), 'label': 'Personal', 'color': '#52b788'},
        {'token': os.path.join(DIR, 'google_token_work.json'), 'label': 'Work', 'color': '#c9a96e'},
    ]

    active_accounts = [a for a in GOOGLE_ACCOUNTS if os.path.exists(a['token'])]

    if not active_accounts:
        return {
            'setup_required': True,
            'setup_message': (
                'Google auth not configured yet.<br><br>'
                '1. Create OAuth credentials at<br>'
                '<code>console.cloud.google.com/apis/credentials</code><br><br>'
                '2. Save as <code>google_credentials.json</code> in the claude-mobile folder<br><br>'
                '3. Run: <code>python3 google_auth_setup.py</code>'
            )
        }

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

        # --- Gmail: unread + last 7 days (deduplicated) ---
        try:
            gmail = build('gmail', 'v1', credentials=creds, cache_discovery=False)
            # Fetch unread (any age) + recent 7 days (read or unread)
            seven_days_ago = (datetime.now() - timedelta(days=7)).strftime('%Y/%m/%d')
            seen_ids = set()
            all_msg_metas = []
            for query in [f'is:unread', f'after:{seven_days_ago}']:
                try:
                    resp = gmail.users().messages().list(
                        userId='me', q=query, maxResults=50
                    ).execute()
                    for m in resp.get('messages', []):
                        if m['id'] not in seen_ids:
                            seen_ids.add(m['id'])
                            all_msg_metas.append(m)
                except Exception:
                    continue

            for msg_meta in all_msg_metas:
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

        # --- Calendar: 7 days ago to 33 days ahead (40-day window) ---
        try:
            cal = build('calendar', 'v3', credentials=creds, cache_discovery=False)
            now = datetime.now(timezone.utc)
            window_start = now - timedelta(days=7)
            window_end = now + timedelta(days=33)

            time_min = window_start.isoformat()
            time_max = window_end.isoformat()

            events_result = cal.events().list(
                calendarId='primary',
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
                orderBy='startTime',
                maxResults=100
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

    return result


@app.route('/work-status')
def work_status():
    """Serve work status from background cache (instant response)."""
    cached = _work_cache.get('work_status')
    if cached is not None:
        resp = jsonify(cached)
        resp.headers['Cache-Control'] = 'no-store'
        return resp
    # First request before cache is populated — fetch synchronously
    try:
        data = _fetch_work_status()
        _work_cache['work_status'] = data
        return jsonify(data)
    except Exception as e:
        return jsonify({'error': str(e)}), 502


# Persistent Slack caches (survive across requests)
_slack_user_cache = {}
_slack_team_id = None

def _fetch_slack_messages():
    """Background fetch: last 20 messages per channel + unreads."""
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
            return {'error': f'Failed to read slack_config.json: {e}'}
    else:
        try:
            import sys as _sys
            _sys.path.insert(0, os.path.dirname(DIR))
            from credentials import SLACK_USER_TOKEN, SLACK_BOT_TOKEN
            slack_cfg = {'token': SLACK_USER_TOKEN, 'bot_token': SLACK_BOT_TOKEN, 'channels': [], 'max_channels': 12}
        except ImportError:
            pass

    if not slack_cfg:
        return {
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
        }

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
            return {'error': f'Slack API error: {convos_resp.get("error", "unknown")}'}
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
                    params={'channel': ch_id, 'limit': 20},
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
        result['error'] = str(e)

    return result


@app.route('/slack-messages')
def slack_messages():
    """Serve Slack messages from background cache (instant response)."""
    cached = _work_cache.get('slack_messages')
    if cached is not None:
        return jsonify(cached)
    # First request before cache is populated — fetch synchronously
    try:
        data = _fetch_slack_messages()
        _work_cache['slack_messages'] = data
        return jsonify(data)
    except Exception as e:
        return jsonify({'error': str(e)}), 502


# ═══════════════════════════════════════════════════════════════════════════════
# Work Smart Search — helper functions + Haiku agent
# ═══════════════════════════════════════════════════════════════════════════════

def _get_google_creds():
    """Return list of (creds, label, color) for available Google accounts."""
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    DIR = os.path.dirname(os.path.abspath(__file__))
    SCOPES = [
        'https://www.googleapis.com/auth/gmail.readonly',
        'https://www.googleapis.com/auth/calendar.readonly',
    ]
    GOOGLE_ACCOUNTS = [
        {'token': os.path.join(DIR, 'google_token.json'), 'label': 'Personal', 'color': '#52b788'},
        {'token': os.path.join(DIR, 'google_token_work.json'), 'label': 'Work', 'color': '#c9a96e'},
    ]
    result = []
    for account in GOOGLE_ACCOUNTS:
        if not os.path.exists(account['token']):
            continue
        try:
            creds = Credentials.from_authorized_user_file(account['token'], SCOPES)
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
                with open(account['token'], 'w') as f:
                    f.write(creds.to_json())
            result.append((creds, account['label'], account['color']))
        except Exception:
            continue
    return result


def _search_gmail(query, max_results=10):
    """Search Gmail across all configured accounts. Returns list of email dicts."""
    from googleapiclient.discovery import build
    emails = []
    for creds, acct_label, acct_color in _get_google_creds():
        try:
            gmail = build('gmail', 'v1', credentials=creds, cache_discovery=False)
            msgs = gmail.users().messages().list(
                userId='me', q=query, maxResults=max_results
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
                emails.append({
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
    emails.sort(key=lambda x: x.get('sort_ts', 0), reverse=True)
    return emails


def _search_calendar(query, max_results=10):
    """Search Google Calendar across all configured accounts. Returns list of event dicts."""
    from googleapiclient.discovery import build
    events = []
    for creds, acct_label, acct_color in _get_google_creds():
        try:
            cal = build('calendar', 'v3', credentials=creds, cache_discovery=False)
            events_result = cal.events().list(
                calendarId='primary', q=query,
                singleEvents=True, orderBy='startTime',
                maxResults=max_results
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
                events.append({
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
    events.sort(key=lambda x: x.get('sort_ts', 0))
    return events


def _search_slack(query, max_results=10):
    """Search Slack messages. Returns list of message dicts."""
    DIR = os.path.dirname(os.path.abspath(__file__))
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
    if not slack_cfg_search:
        return []

    messages = []
    try:
        s_user_token = slack_cfg_search.get('token', '') or slack_cfg_search.get('bot_token', '')
        s_bot_token = slack_cfg_search.get('bot_token', '') or s_user_token
        s_user_headers = {'Authorization': f'Bearer {s_user_token}'}
        s_bot_headers = {'Authorization': f'Bearer {s_bot_token}'}

        search_resp = http_requests.get(
            'https://slack.com/api/search.messages',
            headers=s_user_headers,
            params={'query': query, 'count': max_results, 'sort': 'timestamp'},
            timeout=10
        ).json()

        if search_resp.get('ok'):
            matches = search_resp.get('messages', {}).get('matches', [])
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
                msg_link = ''
                if _slack_team_id and ch_id:
                    msg_link = f"slack://channel?team={_slack_team_id}&id={ch_id}&message={match.get('ts', '')}"
                else:
                    msg_link = match.get('permalink', '')
                messages.append({
                    'channel': ch_name,
                    'user': display_name,
                    'text': match.get('text', '')[:300],
                    'time': time_display,
                    'sort_ts': ts,
                    'link': msg_link,
                })
    except Exception:
        pass
    messages.sort(key=lambda x: x.get('sort_ts', 0), reverse=True)
    return messages


def _get_anthropic_key():
    """Get Anthropic API key for Haiku calls (direct API, NOT Claude CLI).
    Uses shared_utils.get_api_key() — single source of truth."""
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
        from shared_utils import get_api_key
        return get_api_key()
    except ImportError:
        # Fallback if shared_utils not available
        try:
            from credentials import ANTHROPIC_API_KEY
            return ANTHROPIC_API_KEY
        except ImportError:
            return os.environ.get('ANTHROPIC_API_KEY', '')


_SMART_SEARCH_TOOLS = [
    {
        "name": "search_gmail",
        "description": "Search Gmail across all configured accounts. Use Gmail search syntax (e.g. 'from:rajiv', 'subject:review after:2026/02/01', 'is:unread from:sarah').",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Gmail search query"},
                "max_results": {"type": "integer", "description": "Max results (default 10)", "default": 10}
            },
            "required": ["query"]
        }
    },
    {
        "name": "search_calendar",
        "description": "Search Google Calendar events. Matches event titles, descriptions, locations.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Calendar search query"},
                "max_results": {"type": "integer", "description": "Max results (default 10)", "default": 10}
            },
            "required": ["query"]
        }
    },
    {
        "name": "search_slack",
        "description": "Search Slack messages. Supports modifiers (e.g. 'from:@rajiv', 'in:#general', 'during:today').",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Slack search query"},
                "max_results": {"type": "integer", "description": "Max results (default 10)", "default": 10}
            },
            "required": ["query"]
        }
    }
]


def _call_haiku(messages, tools=None):
    """Call Claude Haiku via the Anthropic Messages API. Returns parsed JSON response."""
    api_key = _get_anthropic_key()
    if not api_key:
        raise RuntimeError("No Anthropic API key configured")
    body = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 1024,
        "messages": messages,
    }
    if tools:
        body["tools"] = tools
    resp = http_requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json=body,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def _run_smart_search(user_query):
    """Agentic smart search: Haiku interprets query, calls search tools,
    evaluates results, optionally refines, then answers. Yields SSE events."""

    today = datetime.now().strftime('%A %d %B %Y')
    SAFARI_PAD = ": " + "x" * 256 + "\n\n"

    system_prompt = (
        f"You are a smart work search assistant. Today is {today}.\n\n"
        "The user will ask a question about their emails, calendar, or Slack messages. "
        "You have tools to search each platform.\n\n"
        "Your job:\n"
        "1. Interpret the question — think about WHO they mean, WHAT timeframe, which platforms.\n"
        "2. Call the appropriate search tools with well-crafted queries. You can call multiple tools.\n"
        "3. Review results. If not good enough, refine and retry (max 2 refinements).\n"
        "4. Write a concise answer (2-4 sentences).\n\n"
        "When writing your final answer:\n"
        "- Be direct and concise\n"
        "- Reference specific messages like [email:0], [calendar:2], [slack:1] so the UI can link them\n"
        "- If nothing relevant found, say so clearly\n"
        "- Do not use markdown formatting\n\n"
        "Be efficient — don't search platforms clearly irrelevant to the question."
    )

    messages = [{"role": "user", "content": system_prompt + "\n\nUser question: " + user_query}]
    all_results = {"emails": [], "events": [], "slack": []}
    total_in = 0
    total_out = 0

    yield ": " + "x" * 2048 + "\n\n"
    yield f"data: {json.dumps({'type': 'status', 'content': 'Analysing your question...'})}\n\n" + SAFARI_PAD

    max_turns = 4
    for turn in range(max_turns):
        try:
            response = _call_haiku(messages, tools=_SMART_SEARCH_TOOLS)
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'content': 'Haiku error: ' + str(e)})}\n\n" + SAFARI_PAD
            return

        usage = response.get("usage", {})
        total_in += usage.get("input_tokens", 0)
        total_out += usage.get("output_tokens", 0)

        stop_reason = response.get("stop_reason", "")
        content_blocks = response.get("content", [])

        text_parts = []
        tool_uses = []
        for block in content_blocks:
            if block.get("type") == "text":
                text_parts.append(block["text"])
            elif block.get("type") == "tool_use":
                tool_uses.append(block)

        # Final answer
        if stop_reason == "end_turn":
            final_text = "\n".join(text_parts)
            yield f"data: {json.dumps({'type': 'answer', 'content': final_text, 'results': all_results})}\n\n" + SAFARI_PAD
            cost = total_in * 0.8 / 1_000_000 + total_out * 4.0 / 1_000_000
            yield f"data: {json.dumps({'type': 'cost', 'content': '${0:.4f} | {1}in/{2}out'.format(cost, total_in, total_out), 'turns': turn + 1})}\n\n" + SAFARI_PAD
            return

        # Tool calls — execute and feed back
        if tool_uses:
            messages.append({"role": "assistant", "content": content_blocks})
            tool_results = []

            for tool_use in tool_uses:
                tool_name = tool_use["name"]
                tool_input = tool_use.get("input", {})
                tool_id = tool_use["id"]

                platform = tool_name.replace("search_", "")
                yield f"data: {json.dumps({'type': 'status', 'content': 'Searching ' + platform + '...'})}\n\n" + SAFARI_PAD

                try:
                    if tool_name == "search_gmail":
                        results = _search_gmail(tool_input["query"], tool_input.get("max_results", 10))
                        base = len(all_results["emails"])
                        all_results["emails"].extend(results)
                        lines = []
                        for i, e in enumerate(results):
                            lines.append(f"[email:{base + i}] From: {e['from']} | Subject: {e['subject']} | {e['time']}\n  {e['snippet'][:150]}")
                        tool_output = f"Found {len(results)} emails:\n" + "\n".join(lines) if results else "No emails found."

                    elif tool_name == "search_calendar":
                        results = _search_calendar(tool_input["query"], tool_input.get("max_results", 10))
                        base = len(all_results["events"])
                        all_results["events"].extend(results)
                        lines = []
                        for i, ev in enumerate(results):
                            loc = f" | Location: {ev['location']}" if ev.get('location') else ''
                            lines.append(f"[calendar:{base + i}] {ev['summary']} | {ev['when']}{loc}")
                        tool_output = f"Found {len(results)} events:\n" + "\n".join(lines) if results else "No calendar events found."

                    elif tool_name == "search_slack":
                        results = _search_slack(tool_input["query"], tool_input.get("max_results", 10))
                        base = len(all_results["slack"])
                        all_results["slack"].extend(results)
                        lines = []
                        for i, m in enumerate(results):
                            lines.append(f"[slack:{base + i}] #{m['channel']} | {m['user']}: {m['text'][:120]} ({m['time']})")
                        tool_output = f"Found {len(results)} Slack messages:\n" + "\n".join(lines) if results else "No Slack messages found."
                    else:
                        tool_output = f"Unknown tool: {tool_name}"
                except Exception as e:
                    tool_output = f"Error: {e}"

                tool_results.append({"type": "tool_result", "tool_use_id": tool_id, "content": tool_output})

            messages.append({"role": "user", "content": tool_results})
            yield f"data: {json.dumps({'type': 'status', 'content': 'Evaluating results...'})}\n\n" + SAFARI_PAD
            continue

        # Fallback
        final_text = "\n".join(text_parts) if text_parts else "No answer generated."
        yield f"data: {json.dumps({'type': 'answer', 'content': final_text, 'results': all_results})}\n\n" + SAFARI_PAD
        return

    yield f"data: {json.dumps({'type': 'answer', 'content': 'Search timed out after max refinements.', 'results': all_results})}\n\n" + SAFARI_PAD


@app.route('/work-search')
def work_search():
    """Search across Gmail, Calendar, and Slack — uses extracted helpers."""
    query = request.args.get('q', '').strip()
    if not query:
        return jsonify({'emails': [], 'events': [], 'slack': []})

    result = {
        'emails': _search_gmail(query),
        'events': _search_calendar(query),
        'slack': _search_slack(query),
    }
    return jsonify(result)


@app.route('/work-smart-search')
def work_smart_search():
    """Smart search: Haiku agent interprets query, searches platforms, evaluates & answers."""
    query = request.args.get('q', '').strip()
    if not query:
        return jsonify({'error': 'No query provided'}), 400

    return Response(
        _run_smart_search(query),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache',
                 'X-Accel-Buffering': 'no',
                 'Connection': 'keep-alive'}
    )


@app.route('/cancel', methods=['POST'])
def cancel():
    """Proxy: kill active process on conversation server."""
    result = _conv_proxy_post('/cancel')
    if result.get('cancelled'):
        return jsonify({'status': 'cancelled'})
    return jsonify({'status': 'nothing_running'})


@app.route('/new-session', methods=['POST'])
def new_session():
    """Proxy: clear conversation context on server."""
    _conv_proxy_post('/new-session')
    return 'ok'


@app.route('/clear', methods=['POST'])
def clear():
    return 'ok'


# --- Governors: Serve school documents for mobile viewing ---

@app.route('/governors/doc/<path:filepath>')
def serve_school_doc(filepath):
    """Serve a school document file for viewing on mobile."""
    import urllib.parse
    from pathlib import Path
    school_docs = Path.home() / "Desktop" / "school docs"
    decoded = urllib.parse.unquote(filepath)
    full_path = (school_docs / decoded).resolve()
    # Security: ensure path stays within school docs directory
    if not str(full_path).startswith(str(school_docs.resolve())):
        return Response("Forbidden", status=403)
    if not full_path.is_file():
        return Response("File not found", status=404)
    return send_file(str(full_path), as_attachment=False)


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
    # Check if conversation server has an active process (it survives wrapper restart)
    status = _conv_proxy_get('/status')
    active = status.get('status') == 'running' and status.get('alive', False)
    def _restart():
        time.sleep(0.3)
        os.kill(os.getpid(), signal.SIGINT)
        time.sleep(1)
        os.execv(sys.executable, [sys.executable] + sys.argv)
    threading.Thread(target=_restart, daemon=True).start()
    msg = 'Restarting... (active work will resume automatically)' if active else 'Restarting...'
    return msg, 200


def _work_cache_thread():
    """Background thread: refreshes work status + Slack every 60s."""
    import time as _t
    # Wait 5s for server to fully start before first fetch
    _t.sleep(5)
    while True:
        try:
            _work_cache['refreshing'] = True
            _work_cache['work_status'] = _fetch_work_status()
        except Exception as e:
            _work_cache['error'] = f'work_status: {e}'
        try:
            _work_cache['slack_messages'] = _fetch_slack_messages()
        except Exception as e:
            _work_cache['error'] = f'slack: {e}'
        _work_cache['refreshing'] = False
        _work_cache['last_refresh'] = _t.monotonic()
        _t.sleep(_WORK_CACHE_TTL)


if __name__ == '__main__':
    import socket
    from werkzeug.serving import make_server

    # Start background cache thread for Work tab data
    _cache_t = threading.Thread(target=_work_cache_thread, daemon=True, name='work-cache')
    _cache_t.start()

    topic_count = len(glob.glob(os.path.join(TOPICS_DIR, "*.md")))
    print(f"\n  Claude Code Mobile")
    print(f"  Local:     http://localhost:8080")
    print(f"  iPhone:    http://100.112.125.42:8080")
    print(f"  Memory:    {topic_count} topic files")
    print(f"  Work dir:  {WORK_DIR}")
    print(f"  Features:  Full CLI, streaming, tool visibility, session continuity")
    print(f"  Work cache: every {_WORK_CACHE_TTL}s (background thread)\n")

    server = make_server('0.0.0.0', 8080, app, threaded=True)
    server.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
