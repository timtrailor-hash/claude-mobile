# claude-mobile — Conversation Server Subsystem

## What this is
Mobile conversation server: persistent Claude CLI subprocess broker, WebSocket/SSE
real-time bridge, tmux terminal proxy, APNs push + Live Activity, printer control
API, and supporting infrastructure. Runs as a launchd KeepAlive daemon on port 8081.

**Canonical file:** `conversation_server.py` (7,195 lines — decomposition in progress).
Pre-push hook blocks non-Mac-Mini pushes. Mac Mini is the only authoritative host.

## Target module decomposition

`conversation_server.py` is being split into a `conv/` package. Until the split
is complete, use this map to scope edits — load only the section you need:

| Module (target) | Lines (approx) | Responsibility |
|-----------------|----------------|----------------|
| `conv/auth.py` | 1–120 | Flask app, auth middleware, config, session state globals |
| `conv/sessions.py` | 120–700 | Subprocess lifecycle, reader thread, keepalive, permission bridge |
| `conv/routes_core.py` | 700–1025 | /send, /stream, /status, /cancel, /new-session, /last-response, /health |
| `conv/printer.py` | 1025–1370 + 3129–4372 | Printer safety, Moonraker/Bambu commands, all /printer-* routes, state monitor |
| `conv/terminal.py` | 1370–1766 + 6029–6360 | Terminal window mgmt, OAuth bridge, Google Docs, all /tmux-* routes |
| `conv/notifications.py` | 1766–2062 | APNs multi-app push, base Live Activity JWT/push primitives |
| `conv/liveactivity.py` | 2062–5500 | LA arc refresher, autonomous heartbeat, pane-session map, prompt options, /claude-hook |
| `conv/health.py` | 6360–7195 | /system-health, /infrastructure-health, session logs, work-search, cleanup thread |

## Editing protocol

**When editing a specific area**, load only the relevant section from the map above
plus `conv/auth.py` (shared state). Do NOT read the entire 7,195-line file.

- Printer changes: read lines 1025–1370 + 3129–4372 only.
- Tmux/terminal changes: read lines 1370–1766 + 6029–6360 only.
- Live Activity changes: read lines 1766–2062 (APNs base) + 2062–5500 (LA logic).
- Session/subprocess changes: read lines 1–700.
- Health/monitoring changes: read lines 6360–7195.

After the `conv/` split is done, each module is its own file and this line-range
map becomes obsolete — load the module file directly.

## Seams and invariants

- **Auth owns the Flask app and `_lock`.** All route handlers are registered on `app`.
- **Sessions owns `_session` dict and subprocess lifecycle.** Only sessions may call `_kill_subprocess()` or `_ensure_subprocess()`.
- **Persistence: event files live in `SESSIONS_DIR`.** `_append_event()` is the single write path.
- **Notifications owns APNs tokens and Live Activity state.** Other modules call `_send_push_notification()` but never touch the token store directly.
- **Printer safety helpers are self-contained.** They read printer state via HTTP, never touch session state.
- **The cleanup thread and signal handler live in health.** They may call into sessions for shutdown.

## Action defaults

**Email sends are pre-authorized.** When instructed to email Tim, execute the send
immediately via SMTP (`credentials.SMTP_USER` / `credentials.SMTP_PASS`,
`smtp.gmail.com:465` SSL). Do NOT use `mcp__claude_ai_Gmail__gmail_create_draft` —
that tool creates drafts, not sends. Do NOT ask for confirmation. Do NOT write
the message to a file for review. The task is complete only when the SMTP server
returns 250 OK and the send is logged.

**Notification sends are pre-authorized.** ntfy, Slack, and APNs push are all
fire-and-forget. Use them when the task calls for notification.

## Context budget

- No single file in this directory should exceed 2,000 lines (enforced by scenario test).
- When `conv/` split is complete, the entry point (`conversation_server.py`) should be <200 lines.
- Load ≤3 files from this subsystem per edit. If you need more, the module boundary is wrong.

## Tests

- 17 behavioral tests in `~/code/tim-claude-controlplane/scenarios/test_conversation_server_behavioral.py`
- 4 canonicity tests in `~/code/tim-claude-controlplane/scenarios/drift/test_conversation_server_canonicity.py`
- Run after every code change: `cd ~/code/tim-claude-controlplane && python3.11 -m pytest scenarios/ -q`
