#!/usr/bin/env python3
"""Drain step before launchctl kickstart.

Polls /status until the daemon reports idle. Exits 0 if it goes idle within
the timeout, 1 if it times out (caller should ABORT kickstart unless Tim
explicitly approves a kill of in-flight work).

The daemon currently exposes:
    GET /status -> {"alive": bool, "status": "idle"|..., ...}

Idle = alive == False OR status == "idle".

Usage:
    python3 scripts/wait_for_idle.py [--port 8081] [--timeout 30] [--interval 1]

This is the §5.4 drain step from the decomposition plan.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request


def is_idle(port: int, timeout_s: float = 2.0) -> tuple[bool, str]:
    url = f"http://localhost:{port}/status"
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as r:
            data = json.loads(r.read())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        return False, f"unreachable: {e}"
    alive = bool(data.get("alive"))
    status = str(data.get("status") or "").lower()
    if status == "idle" or alive is False:
        return True, f"idle (status={status} alive={alive})"
    return False, f"busy (status={status} alive={alive})"


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8081)
    p.add_argument("--timeout", type=float, default=30.0, help="seconds")
    p.add_argument("--interval", type=float, default=1.0, help="poll interval seconds")
    args = p.parse_args(argv[1:])

    deadline = time.monotonic() + args.timeout
    last_msg = ""
    while time.monotonic() < deadline:
        idle, msg = is_idle(args.port)
        if idle:
            print(f"OK: {msg}")
            return 0
        last_msg = msg
        time.sleep(args.interval)
    print(f"TIMEOUT after {args.timeout}s: {last_msg}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
