#!/usr/bin/env bash
# baseline_request_log.sh — capture iOS request lines from the live daemon log
# for use as the wire-contract baseline (plan §11.4).
#
# Usage:
#   bash scripts/baseline_request_log.sh start [--out PATH]
#   bash scripts/baseline_request_log.sh stop
#   bash scripts/baseline_request_log.sh status
#
# Capture has no built-in duration limit (macOS lacks 'timeout'). Run 'stop'
# explicitly when the baseline window is sufficient (typically ~24h).
# Capture survives SSH disconnect via nohup + full stdio detachment.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FIXTURES_DIR="$REPO_ROOT/tests/fixtures"
PID_FILE="$FIXTURES_DIR/baseline_capture.pid"
META_FILE="$FIXTURES_DIR/baseline_capture_meta.log"
SOURCE_LOG="/tmp/claude_sessions/server.log"

mkdir -p "$FIXTURES_DIR"

cmd="${1:-status}"

is_running() {
    [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE" 2>/dev/null)" 2>/dev/null
}

case "$cmd" in
    start)
        shift || true
        OUT="$FIXTURES_DIR/iphone_request_baseline_$(date +%Y%m%d-%H%M%S).log"
        while [[ $# -gt 0 ]]; do
            case "$1" in
                --out) OUT="$2"; shift 2 ;;
                *) echo "unknown arg: $1" >&2; exit 2 ;;
            esac
        done
        if is_running; then
            echo "already running (pid $(cat "$PID_FILE")). Run 'stop' first." >&2
            exit 1
        fi
        if [[ ! -f "$SOURCE_LOG" ]]; then
            echo "source log not found: $SOURCE_LOG" >&2
            exit 2
        fi
        # macOS-friendly daemon launch: nohup + full stdio detach.
        nohup tail -F "$SOURCE_LOG" >"$OUT" 2>>"$META_FILE" </dev/null &
        PID=$!
        echo "$PID" > "$PID_FILE"
        echo "$(date '+%Y-%m-%d %H:%M:%S') start pid=$PID out=$OUT" >> "$META_FILE"
        sleep 1
        if kill -0 "$PID" 2>/dev/null; then
            echo "OK started (pid $PID)"
            echo "output: $OUT"
            echo "stop with: bash scripts/baseline_request_log.sh stop"
        else
            echo "FAILED — process did not survive launch (check $META_FILE)" >&2
            rm -f "$PID_FILE"
            exit 1
        fi
        ;;
    stop)
        if ! is_running; then
            echo "not running"
            rm -f "$PID_FILE"
            exit 0
        fi
        PID="$(cat "$PID_FILE")"
        kill "$PID"
        sleep 1
        kill -0 "$PID" 2>/dev/null && kill -9 "$PID" || true
        rm -f "$PID_FILE"
        echo "$(date '+%Y-%m-%d %H:%M:%S') stop" >> "$META_FILE"
        echo "OK stopped (pid $PID)"
        ;;
    status)
        if is_running; then
            PID="$(cat "$PID_FILE")"
            echo "running (pid $PID)"
            ls -la "$FIXTURES_DIR"/iphone_request_baseline_*.log 2>/dev/null | tail -3
        else
            echo "not running"
        fi
        ;;
    *)
        echo "usage: $0 {start|stop|status}" >&2
        exit 2
        ;;
esac
