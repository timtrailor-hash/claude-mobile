#!/bin/zsh
# Conversation server daemon — runs via launchd as KeepAlive service.
cd ~/.local/lib/conversation
mkdir -p /tmp/claude_sessions
exec /usr/bin/python3 conversation_server.py
