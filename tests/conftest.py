"""Shared pytest fixtures for the conversation_server.py decomposition test suite.

Slice 1a — the basic harness. More fixtures (mocked APNs, mocked Moonraker,
fake Claude subprocess, temp Google token) come in slice 1b once the daemon
gains the dry-run env var support those fixtures depend on.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def conv_server_path(repo_root: Path) -> Path:
    return repo_root / "conversation_server.py"


@pytest.fixture
def restore_env():
    """Snapshot os.environ; restore on teardown. For tests that twiddle env vars."""
    snapshot = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(snapshot)
