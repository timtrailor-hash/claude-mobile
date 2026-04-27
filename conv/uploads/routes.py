"""File upload route — accepts image/PDF attachments from the native iOS app."""

from __future__ import annotations

import os
import tempfile
import uuid

from flask import Blueprint, jsonify, request

bp = Blueprint("uploads", __name__)


_ALLOWED_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".pdf",
    ".heic",
    ".heif",
}


@bp.route("/upload", methods=["POST"])
def upload_file():
    """Accept file upload from native app, return server path."""
    upload_dir = os.path.join(tempfile.gettempdir(), "claude_uploads")
    os.makedirs(upload_dir, exist_ok=True)

    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "Empty filename"}), 400

    # Save with a unique name to avoid collisions.
    ext = os.path.splitext(f.filename)[1].lower() or ".jpg"
    if ext not in _ALLOWED_EXTENSIONS:
        return jsonify({"error": f"Unsupported file type: {ext}"}), 400
    saved_name = f"{uuid.uuid4().hex}{ext}"
    saved_path = os.path.join(upload_dir, saved_name)
    f.save(saved_path)

    return jsonify({"path": saved_path, "filename": f.filename})
