#!/usr/bin/env bash
# fingerprint.sh — thin wrapper. Logic lives in scripts/_fingerprint.py.
# See plan §6.4. Usage: bash scripts/fingerprint.sh [--port 8081] > /tmp/fp.json
exec /opt/homebrew/bin/python3.11 "$(dirname "$0")/_fingerprint.py" "$@"
