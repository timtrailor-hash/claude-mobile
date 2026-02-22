#!/usr/bin/env python3
"""Claude Code Mobile — full Claude Code experience in a scrollable mobile web UI."""

import os
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

STREAMLIT_URL = 'http://192.168.87.37:8501'

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


CHAT_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
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
.tool-indicator {
    margin-bottom: 6px; padding: 5px 12px; font-size: 12px; color: #c9a96e;
    background: #1a1a3a; border-left: 2px solid #c9a96e;
    border-radius: 0 8px 8px 0; max-width: 92%;
    font-family: 'SF Mono', Menlo, monospace;
}
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
    flex: 1; overflow-y: auto; padding: 12px;
    -webkit-overflow-scrolling: touch;
}
.work-section { margin-bottom: 16px; }
.work-section h3 {
    font-size: 13px; color: #c9a96e; text-transform: uppercase;
    letter-spacing: 0.5px; margin-bottom: 8px; padding-bottom: 4px;
    border-bottom: 1px solid #2a2a4a;
}
.work-card {
    background: #16213e; border-radius: 10px; padding: 12px;
    margin-bottom: 8px; border: 1px solid #2a2a4a;
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
        <button onclick="window.location.href='/?t='+Date.now()">&#8635;</button>
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
    <div class="work-dash" id="workDash">
        <div class="printer-loading" id="workLoading">Loading work status...</div>
    </div>
</div>

</div><!-- /tab-wrapper -->

<!-- ====== CHAT JS (isolated) ====== -->
<script>
(function() {
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

    function startWorkingIndicator() {
        workingStart = Date.now();
        lastActivityTime = workingStart;
        workingDiv = addMsg('msg system', '<span style="color:#c9a96e;">Working...</span>');
        workingTimer = setInterval(function() {
            if (!workingDiv) return;
            var elapsed = Math.floor((Date.now() - workingStart) / 1000);
            var idle = Math.floor((Date.now() - lastActivityTime) / 1000);
            var mins = Math.floor(elapsed / 60);
            var secs = elapsed % 60;
            var timeStr = mins > 0 ? mins + 'm ' + secs + 's' : secs + 's';
            var status = idle > 15 ? 'Still working' : 'Working';
            workingDiv.innerHTML = '<span style="color:#c9a96e;">' + status + '... ' + timeStr + '</span>';
            scrollToBottom();
        }, 10000);
    }

    function touchWorkingIndicator() {
        lastActivityTime = Date.now();
    }

    function stopWorkingIndicator() {
        if (workingTimer) { clearInterval(workingTimer); workingTimer = null; }
        if (workingDiv) { workingDiv.remove(); workingDiv = null; }
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

    function scrollToBottom() { messagesEl.scrollTop = messagesEl.scrollHeight; }

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

    function addMsg(cls, html) {
        var div = document.createElement('div');
        div.className = cls;
        div.innerHTML = html;
        messagesEl.appendChild(div);
        scrollToBottom();
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
            messageQueue.push({text: text, images: pendingImages.slice()});
            addMsg('msg user', escapeHtml(text) + ' <span style="color:#c9a96e;font-size:11px;">[queued]</span>');
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
    }

    function sendMessage(text, images) {
        sending = true;
        wasCancelled = false;
        setButtonMode('cancel');
        startWorkingIndicator();

        var displayText = text;
        if (images && images.length) displayText += ' [' + images.length + ' image(s)]';
        addMsg('msg user', escapeHtml(displayText));

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
                    if (result.done) return;
                    buffer += decoder.decode(result.value, {stream: true});
                    var lines = buffer.split('\\n');
                    buffer = lines.pop();
                    for (var i = 0; i < lines.length; i++) {
                        if (!lines[i].startsWith('data: ')) continue;
                        try {
                            var data = JSON.parse(lines[i].slice(6));
                            gotData = true;
                            touchWorkingIndicator();
                            if (data.type === 'init') {
                                sessionId = data.session_id;
                                if (!sessionInited) {
                                    sessionInited = true;
                                    addMsg('msg system', 'Connected (' + escapeHtml(data.model) + ')');
                                }
                            } else if (data.type === 'tool') {
                                hadToolOutput = true;
                                addMsg('tool-indicator', escapeHtml(data.content));
                            } else if (data.type === 'text') {
                                if (!assistantDiv) assistantDiv = addMsg('msg assistant', '');
                                fullText += data.content;
                                assistantDiv.innerHTML = formatOutput(fullText);
                                scrollToBottom();
                            } else if (data.type === 'cost') {
                                addMsg('cost', data.content);
                            } else if (data.type === 'error') {
                                addMsg('msg system', '<span style="color:#e55">' + escapeHtml(data.content) + '</span>');
                            }
                        } catch(e) {}
                    }
                    return readChunk();
                });
            }
            return readChunk();
        }).catch(function(e) {
            if (!wasCancelled && e.name !== 'AbortError' && !gotData) {
                addMsg('msg system', '<span style="color:#e55">Connection error</span>');
            }
        }).finally(function() {
            sending = false;
            stopWorkingIndicator();
            currentAbortController = null;
            setButtonMode('send');
            inputEl.focus();
            if (messageQueue.length > 0) {
                var next = messageQueue.shift();
                setTimeout(function() { sendMessage(next.text, next.images); }, 300);
            }
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

    addMsg('msg system', 'Claude Code Mobile ready.');
    setButtonMode('send');
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

        // Speed analysis (SV08)
        if (p.speed_factor && p.speed_factor > 1.0) {
            var spd_color = p.speed_warning ? '#e55' : '#4a7';
            h += '<div style="margin:6px 0;padding:6px 8px;background:#1a1a2e;border-radius:8px;border-left:3px solid ' + spd_color + ';">';
            h += '<div style="font-size:11px;color:#888;">SPEED</div>';
            h += '<div style="font-size:13px;color:#ddd;">Set: <b>' + Math.round(p.speed_factor * 100) + '%</b>';
            if (p.effective_speed) h += ' &middot; Effective: <b style="color:' + spd_color + ';">' + p.effective_speed + 'x</b>';
            if (p.max_useful_pct) h += ' &middot; Max useful: <b>' + p.max_useful_pct + '%</b>';
            h += '</div>';
            if (p.speed_warning) {
                h += '<div style="font-size:11px;color:#e55;margin-top:3px;">' + esc(p.speed_warning) + '</div>';
            }
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
        if (p.eta_str) h += '<span class="lbl">ETA</span><span class="val-good">' + esc(p.eta_str) + '</span>';
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
        fetch('/printer-status').then(function(r) { return r.json(); }).then(function(data) {
            var now = new Date();
            lastUpdate = now.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
            var html = '<div class="refresh-bar">';
            html += '<span>Updated: ' + lastUpdate + '</span>';
            html += '<button onclick="window.refreshPrinters()">Refresh</button>';
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

    function esc(text) {
        if (!text && text !== 0) return '';
        return String(text).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    }

    function renderWork(data) {
        if (data.setup_required) {
            dashEl.innerHTML = '<div class="work-setup">' + data.setup_message + '</div>';
            return;
        }

        var html = '<div class="refresh-bar" style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">';
        html += '<span style="font-size:11px;color:#666;">Updated: ' + new Date().toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'}) + '</span>';
        html += '<button onclick="window.refreshWork()" style="background:#2a2a4a;color:#c9a96e;border:1px solid #3a3a5a;padding:6px 14px;border-radius:16px;font-size:12px;cursor:pointer;">Refresh</button>';
        html += '</div>';

        // Emails
        html += '<div class="work-section"><h3>Unread Emails (' + (data.emails ? data.emails.length : 0) + ')</h3>';
        if (data.emails && data.emails.length) {
            for (var i = 0; i < data.emails.length; i++) {
                var e = data.emails[i];
                html += '<div class="work-card">';
                html += '<div class="wc-from">' + esc(e.from) + '</div>';
                html += '<div class="wc-subject">' + esc(e.subject) + '</div>';
                if (e.snippet) html += '<div class="wc-snippet">' + esc(e.snippet) + '</div>';
                html += '<div class="wc-time">' + esc(e.time) + '</div>';
                html += '</div>';
            }
        } else {
            html += '<div style="color:#666;font-size:13px;padding:10px;">No unread emails</div>';
        }
        html += '</div>';

        // Calendar
        html += '<div class="work-section"><h3>Upcoming Events</h3>';
        if (data.events && data.events.length) {
            for (var j = 0; j < data.events.length; j++) {
                var ev = data.events[j];
                html += '<div class="work-card cal-card">';
                html += '<div class="wc-title">' + esc(ev.summary) + '</div>';
                html += '<div class="wc-when">' + esc(ev.when) + '</div>';
                if (ev.location) html += '<div class="wc-location">' + esc(ev.location) + '</div>';
                if (ev.attendees) html += '<div class="wc-attendees">' + esc(ev.attendees) + '</div>';
                html += '</div>';
            }
        } else {
            html += '<div style="color:#666;font-size:13px;padding:10px;">No upcoming events</div>';
        }
        html += '</div>';

        dashEl.innerHTML = html;
    }

    function loadWork() {
        dashEl.innerHTML = '<div class="printer-loading">Loading work status...</div>';
        fetch('/work-status').then(function(r) { return r.json(); }).then(function(data) {
            renderWork(data);
        }).catch(function(e) {
            dashEl.innerHTML = '<div class="printer-loading" style="color:#e55;">Failed to load. <button onclick="window.refreshWork()" style="color:#c9a96e;background:#2a2a4a;border:1px solid #3a3a5a;padding:4px 12px;border-radius:12px;margin-top:8px;">Retry</button></div>';
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
            frame.src = '/governors/?embed=true&embed_options=light_theme';
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
        global current_session_id

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

        # Send immediate keepalive
        yield ": keepalive\n\n"

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=WORK_DIR,
                env=env,
                preexec_fn=os.setsid
            )
            with current_proc_lock:
                global current_proc
                current_proc = proc

            # Set stdout to non-blocking
            fd = proc.stdout.fileno()
            flags = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

            raw_buffer = b""
            while proc.poll() is None:
                try:
                    chunk = proc.stdout.read(8192)
                    if chunk:
                        raw_buffer += chunk
                        # Process complete lines
                        while b'\n' in raw_buffer:
                            line_bytes, raw_buffer = raw_buffer.split(b'\n', 1)
                            line = line_bytes.decode('utf-8', errors='replace').strip()
                            if not line:
                                continue
                            try:
                                event = json.loads(line)
                                for sse in process_event(event):
                                    yield sse
                            except json.JSONDecodeError:
                                pass
                    else:
                        yield ": keepalive\n\n"
                        time.sleep(0.5)
                except (BlockingIOError, OSError):
                    yield ": keepalive\n\n"
                    time.sleep(0.5)

            # Read remaining output
            try:
                remaining = proc.stdout.read()
                if remaining:
                    raw_buffer += remaining
                    for line in raw_buffer.decode('utf-8', errors='replace').strip().split('\n'):
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            event = json.loads(line)
                            for sse in process_event(event):
                                yield sse
                        except json.JSONDecodeError:
                            pass
            except Exception:
                pass

            proc.wait()
            with current_proc_lock:
                current_proc = None

        except FileNotFoundError:
            yield f"data: {json.dumps({'type': 'error', 'content': 'Claude Code CLI not found'})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'content': str(e)})}\n\n"

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

    else:
        # Log unhandled event types for debugging
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

    # --- SV08 via Moonraker ---
    try:
        url = 'http://192.168.87.38:7125/printer/objects/query?print_stats&display_status&heater_bed&extruder&fan&gcode_move&motion_report'
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
            'print_duration': ps.get('print_duration', 0),
            'filament_used_mm': ps.get('filament_used', 0),
            'nozzle_temp': round(ext.get('temperature', 0), 1),
            'nozzle_target': round(ext.get('target', 0), 1),
            'bed_temp': round(bed.get('temperature', 0), 1),
            'bed_target': round(bed.get('target', 0), 1),
            'speed_factor': gm.get('speed_factor', 1.0),
            'live_velocity': round(mr.get('live_velocity', 0), 1),
        }

        # Layer progress
        info = ps.get('info', {})
        sv['current_layer'] = info.get('current_layer', 0)
        sv['total_layers'] = info.get('total_layer', 0)
        if sv['total_layers'] > 0:
            sv['layer_progress'] = round(sv['current_layer'] / sv['total_layers'] * 100, 1)

        # Metadata for current file
        if sv['filename']:
            enc = urlquote(sv['filename'])
            try:
                with urllib.request.urlopen(f'http://192.168.87.38:7125/server/files/metadata?filename={enc}', timeout=4) as r:
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
                        thumb_url = f'http://192.168.87.38:7125/server/files/gcodes/{urlquote(tp)}'
                        thumb_path = os.path.join(PRINTER_IMG_DIR, 'sovol_thumbnail.png')
                        urllib.request.urlretrieve(thumb_url, thumb_path)
                        sv['has_thumbnail'] = True
            except Exception:
                pass

            # Print start time from history
            try:
                with urllib.request.urlopen('http://192.168.87.38:7125/server/history/list?limit=1&order=desc', timeout=3) as r:
                    hist = json.loads(r.read())['result']
                if hist.get('jobs'):
                    sv['start_time'] = hist['jobs'][0].get('start_time', 0)
            except Exception:
                pass

        # Camera snapshot
        try:
            cam_path = os.path.join(PRINTER_IMG_DIR, 'sovol_camera.jpg')
            urllib.request.urlretrieve('http://192.168.87.38/webcam/?action=snapshot', cam_path)
            sv['has_camera'] = True
        except Exception:
            pass

        # Calculate times
        dur = sv.get('print_duration', 0)
        sv['duration_str'] = f"{int(dur // 3600)}h {int((dur % 3600) // 60)}m"
        if sv.get('start_time'):
            sv['start_str'] = datetime.fromtimestamp(sv['start_time']).strftime('%a %d %b %H:%M')
        if sv['progress'] > 0:
            actual_total = dur / (sv['progress'] / 100)
            remaining = actual_total - dur
            eta = datetime.now() + timedelta(seconds=remaining)
            sv['remaining_str'] = f"{int(remaining // 3600)}h {int((remaining % 3600) // 60)}m"
            sv['eta_str'] = eta.strftime('%a %d %b %H:%M')

        # Effective speed analysis
        est = sv.get('estimated_time', 0)
        prog = sv.get('progress', 0)
        spd = sv.get('speed_factor', 1.0)
        if est > 0 and dur > 0 and prog > 1:
            effective = (est * (prog / 100)) / dur
            sv['effective_speed'] = round(effective, 2)
            sv['speed_efficiency'] = round((effective / spd) * 100) if spd > 0 else 0
            max_useful = min(spd, effective * 1.1)
            sv['max_useful_pct'] = round(max_useful * 100)
            if spd > 1.2 and effective < spd * 0.6:
                sv['speed_warning'] = f"Speed set to {int(spd * 100)}% but only achieving {effective:.1f}x — max useful: {sv['max_useful_pct']}%"

        # Filament calculations
        fil_used_m = sv.get('filament_used_mm', 0) / 1000
        fil_total_m = sv.get('filament_total_mm', 0) / 1000
        if fil_total_m > 0:
            sv['filament_used_m'] = round(fil_used_m, 1)
            sv['filament_total_m'] = round(fil_total_m, 1)
            sv['filament_pct'] = round(fil_used_m / fil_total_m * 100)
            wt = sv.get('filament_weight_g', 0)
            if wt:
                sv['filament_used_g'] = round(wt * (fil_used_m / fil_total_m))
                sv['filament_total_g'] = round(wt)

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
        serial = '03919D591203833'

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
        c.username_pw_set('bblp', '27346688')
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        c.tls_set_context(ctx)
        c.on_connect = _on_connect
        c.on_message = _on_message
        c.connect('192.168.87.47', 8883, keepalive=60)
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
                with JPEGFrameStream('192.168.87.47', '27346688') as stream:
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


@app.route('/work-status')
def work_status():
    """Fetch unread Gmail and upcoming calendar events through end of next working day."""
    DIR = os.path.dirname(os.path.abspath(__file__))
    TOKEN_FILE = os.path.join(DIR, 'google_token.json')

    if not os.path.exists(TOKEN_FILE):
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

    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build

        SCOPES = [
            'https://www.googleapis.com/auth/gmail.readonly',
            'https://www.googleapis.com/auth/calendar.readonly',
        ]
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            with open(TOKEN_FILE, 'w') as f:
                f.write(creds.to_json())

        result = {'emails': [], 'events': []}

        # --- Gmail: unread emails (max 15) ---
        try:
            gmail = build('gmail', 'v1', credentials=creds, cache_discovery=False)
            msgs = gmail.users().messages().list(
                userId='me', q='is:unread', maxResults=15
            ).execute()

            for msg_meta in msgs.get('messages', []):
                msg = gmail.users().messages().get(
                    userId='me', id=msg_meta['id'], format='metadata',
                    metadataHeaders=['From', 'Subject', 'Date']
                ).execute()
                headers = {h['name']: h['value'] for h in msg.get('payload', {}).get('headers', [])}
                # Parse sender name
                from_raw = headers.get('From', '')
                if '<' in from_raw:
                    from_name = from_raw.split('<')[0].strip().strip('"')
                else:
                    from_name = from_raw

                # Parse date
                date_str = headers.get('Date', '')
                time_display = ''
                try:
                    from email.utils import parsedate_to_datetime
                    dt = parsedate_to_datetime(date_str)
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
                })
        except Exception as e:
            result['email_error'] = str(e)

        # --- Calendar: events through end of next working day ---
        try:
            cal = build('calendar', 'v3', credentials=creds, cache_discovery=False)
            now = datetime.now(timezone.utc)
            # Find next working day end (skip weekends)
            today = datetime.now()
            target = today + timedelta(days=1)
            while target.weekday() >= 5:  # 5=Sat, 6=Sun
                target += timedelta(days=1)
            # End of that working day (23:59)
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
                end = ev.get('end', {})
                # Format start time
                if 'dateTime' in start:
                    try:
                        from dateutil.parser import parse as dt_parse
                        st = dt_parse(start['dateTime'])
                        en = dt_parse(end.get('dateTime', start['dateTime']))
                        when = st.strftime('%a %d %b %H:%M') + ' - ' + en.strftime('%H:%M')
                    except Exception:
                        when = start['dateTime'][:16]
                elif 'date' in start:
                    when = 'All day - ' + start['date']
                else:
                    when = ''

                # Attendees
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
                })
        except Exception as e:
            result['calendar_error'] = str(e)

        return jsonify(result)

    except Exception as e:
        return jsonify({
            'setup_required': True,
            'setup_message': f'Auth error: {str(e)}<br><br>Try running <code>python3 google_auth_setup.py</code> again.'
        })


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
    headers['Host'] = '192.168.87.37:8501'

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
    upstream_url = f'ws://192.168.87.37:8501/_stcore/stream'
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


if __name__ == '__main__':
    topic_count = len(glob.glob(os.path.join(TOPICS_DIR, "*.md")))
    print(f"\n  Claude Code Mobile")
    print(f"  Local:     http://localhost:8080")
    print(f"  iPhone:    http://100.112.125.42:8080")
    print(f"  Memory:    {topic_count} topic files")
    print(f"  Work dir:  {WORK_DIR}")
    print(f"  Features:  Full CLI, streaming, tool visibility, session continuity\n")
    app.run(host='0.0.0.0', port=8080, debug=False, threaded=True)
