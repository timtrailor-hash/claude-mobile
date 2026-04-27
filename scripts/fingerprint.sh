#!/usr/bin/env bash
# fingerprint.sh — thin wrapper. Logic lives in scripts/_fingerprint.py.
# See plan §6.4. Usage: bash scripts/fingerprint.sh [--port 8081] > /tmp/fp.json
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
for p in /opt/homebrew/bin/python3.11 /usr/local/bin/python3.11 python3.11 python3; do
    if command -v "$p" >/dev/null 2>&1; then
        exec "$p" "$DIR/_fingerprint.py" "$@"
    fi
done
echo 'python3.11 not found in /opt/homebrew, /usr/local, or PATH' >&2
exit 1
