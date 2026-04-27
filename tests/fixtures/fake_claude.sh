#!/usr/bin/env bash
# tests/fixtures/fake_claude.sh — stub Claude CLI binary for staging.
#
# Used when CONV_CLAUDE_BIN is set to this path (typically with CONV_MODE=staging).
# Reads stream-json messages from stdin, emits a single canned assistant turn
# on stdout, then exits cleanly. NEVER hits any external service.
#
# Conversation-server invokes:
#   claude -p --input-format stream-json --output-format stream-json [args...]
#
# Stream-json protocol (Anthropic): one JSON message per line.
# We emit a "system: init", "assistant: text", and "result" so the
# server reader sees a complete turn.

# Drain stdin in the background (server sends user message)
cat > /dev/null &
DRAIN_PID=$!

SESSION_ID="staging-$(date +%s)-$$"

# system init
printf '{"type":"system","subtype":"init","model":"fake-claude","session_id":"%s"}\n' "$SESSION_ID"

# assistant text
printf '{"type":"assistant","message":{"id":"msg_fake","model":"fake-claude","role":"assistant","content":[{"type":"text","text":"OK (staging dry-run, no real Claude call)."}]},"session_id":"%s"}\n' "$SESSION_ID"

# result
printf '{"type":"result","subtype":"success","is_error":false,"duration_ms":10,"duration_api_ms":0,"num_turns":1,"result":"OK","session_id":"%s","total_cost_usd":0.0}\n' "$SESSION_ID"

# Wait briefly for stdin drain so we exit cleanly
sleep 0.1
kill $DRAIN_PID 2>/dev/null
wait 2>/dev/null
exit 0
