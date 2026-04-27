"""PWA static-file routes.

Routes:
  GET /app            — serve static/app.html
  GET /manifest.json  — serve static/manifest.json
  GET /sw.js          — serve static/sw.js
  GET /offline        — inline offline HTML
  GET /icon-192.png   — generated SVG icon (192x192)
  GET /icon-512.png   — generated SVG icon (512x512)

NOTE: /app and /manifest.json currently 500 in production because
static/app.html and static/manifest.json don't exist in the repo. This
slice does not fix that — the bug pre-dates the decomposition. Tracked
as a separate follow-up; eventual fix is either to add the static files
or to inline minimal HTML/JSON.

STATIC_DIR is env-overridable via CONV_STATIC_DIR for staging.
"""

from __future__ import annotations

import os

from flask import Blueprint, Response

bp = Blueprint("pwa", __name__)


STATIC_DIR = os.environ.get("CONV_STATIC_DIR") or os.path.join(
    os.path.dirname(os.path.realpath(__file__)),
    os.pardir,
    os.pardir,
    "static",
)


@bp.route("/app")
def pwa_app():
    with open(os.path.join(STATIC_DIR, "app.html")) as f:
        return Response(f.read(), content_type="text/html")


@bp.route("/manifest.json")
def pwa_manifest():
    with open(os.path.join(STATIC_DIR, "manifest.json")) as f:
        return Response(f.read(), content_type="application/manifest+json")


@bp.route("/sw.js")
def pwa_sw():
    with open(os.path.join(STATIC_DIR, "sw.js")) as f:
        return Response(f.read(), content_type="application/javascript")


@bp.route("/offline")
def pwa_offline():
    return Response(
        "<html><body style='background:#1a1a2e;color:#e0e0e0;text-align:center;"
        "padding:40px;font-family:-apple-system,sans-serif'>"
        "<h2>Offline</h2><p>Reconnecting...</p></body></html>",
        content_type="text/html",
    )


@bp.route("/icon-192.png")
def pwa_icon_192():
    return _generate_icon(192)


@bp.route("/icon-512.png")
def pwa_icon_512():
    return _generate_icon(512)


def _generate_icon(size: int):
    """Generate an SVG icon (browsers accept SVG for icon-192/512)."""
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" '
        f'viewBox="0 0 {size} {size}">\n'
        f'  <rect width="{size}" height="{size}" rx="{size // 6}" fill="#16213e"/>\n'
        f'  <text x="50%" y="54%" dominant-baseline="middle" text-anchor="middle"\n'
        f'        font-family="-apple-system,sans-serif" font-size="{size // 3}" '
        f'font-weight="600"\n'
        f'        fill="#c9a96e">CC</text>\n'
        f"</svg>"
    )
    return Response(svg, content_type="image/svg+xml")
