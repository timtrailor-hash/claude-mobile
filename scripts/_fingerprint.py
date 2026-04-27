#!/usr/bin/env python3
"""Capture read-only behaviour fingerprint of conversation_server.

For each GET endpoint, record HTTP status + structural shape:
  - application/json -> sorted top-level keys
  - other -> bucketed byte size

Output: stable JSON suitable for diffing across slice extractions.

Usage: python3 scripts/_fingerprint.py [--port 8081]
Necessary but insufficient on its own — pair with scripts/synthetic_suite.py
(slice 1b) for POST + WebSocket coverage. See plan §6.4.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

ENDPOINTS = [
    "/health",
    "/status",
    "/permission-level",
    "/last-response",
    "/printer-resume-info",
    "/task-status",
    "/app",
    "/manifest.json",
    "/",
]


def bucket(n: int) -> str:
    if n < 100:
        return "size<100"
    if n < 1024:
        return "size<1k"
    if n < 10240:
        return "size<10k"
    if n < 102400:
        return "size<100k"
    return "size>=100k"


def shape_of(ctype: str, body: bytes) -> str:
    if ctype.startswith("application/json"):
        try:
            d = json.loads(body)
        except json.JSONDecodeError as e:
            return f"__INVALID_JSON__: {e}"
        if isinstance(d, dict):
            return "object keys: " + ",".join(sorted(d.keys()))
        if isinstance(d, list):
            return f"array length-bucket={'empty' if not d else 'nonempty'}"
        return f"scalar type={type(d).__name__}"
    return bucket(len(body))


def fingerprint_one(base: str, ep: str) -> dict:
    url = f"{base}{ep}"
    try:
        with urllib.request.urlopen(  # nosemgrep: dynamic-urllib-use-detected
            url, timeout=5
        ) as r:
            status = r.status
            ctype = r.headers.get("Content-Type", "")
            body = r.read()
    except urllib.error.HTTPError as e:
        status = e.code
        ctype = e.headers.get("Content-Type", "") if e.headers else ""
        body = e.read() if e.fp else b""
    except (urllib.error.URLError, TimeoutError) as e:
        return {
            "endpoint": ep,
            "method": "GET",
            "status": "ERR",
            "content_type": "",
            "shape": f"unreachable: {e}",
        }
    return {
        "endpoint": ep,
        "method": "GET",
        "status": str(status),
        "content_type": ctype.split(";")[0].strip(),
        "shape": shape_of(ctype, body),
    }


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8081)
    args = p.parse_args(argv[1:])
    base = f"http://localhost:{args.port}"
    out = [fingerprint_one(base, ep) for ep in ENDPOINTS]
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
