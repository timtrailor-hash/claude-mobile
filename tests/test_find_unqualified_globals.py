"""Tests for scripts/find_unqualified_globals.py — the §4.2 grep gate.

Reviewers (Gemini, ChatGPT) flagged that this script must have its own tests
because it is the safety story for every slice. Without tests, a buggy gate
gives false confidence.

Each test writes a tiny module to a tmp file and runs the gate's analyze()
function directly, asserting the findings.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import find_unqualified_globals as gate  # noqa: E402


def _analyze(tmp_path: Path, src: str):
    f = tmp_path / "mod.py"
    f.write_text(src)
    return gate.analyze(f)


# ---- known-good (must produce zero findings) ----


def test_comprehension_targets_not_flagged(tmp_path):
    src = """
def f(items):
    return {k: v for k, v in items.items() if k.startswith("_")}
"""
    assert _analyze(tmp_path, src) == []


def test_with_as_binding_not_flagged(tmp_path):
    src = """
def f(path):
    with open(path) as fh:
        return fh.read()
"""
    assert _analyze(tmp_path, src) == []


def test_for_loop_target_not_flagged(tmp_path):
    src = """
def f(items):
    out = []
    for x in items:
        out.append(x)
    return out
"""
    assert _analyze(tmp_path, src) == []


def test_try_except_module_top_collected(tmp_path):
    src = """
try:
    from credentials import AUTH_TOKEN
except ImportError:
    AUTH_TOKEN = None

def f():
    return AUTH_TOKEN
"""
    assert _analyze(tmp_path, src) == []


def test_lambda_params_not_flagged(tmp_path):
    src = """
def f(items):
    return sorted(items, key=lambda x: x.priority)
"""
    assert _analyze(tmp_path, src) == []


def test_walrus_target_local(tmp_path):
    src = """
def f(items):
    if (n := len(items)) > 0:
        return n
    return 0
"""
    assert _analyze(tmp_path, src) == []


def test_explicit_import_resolves(tmp_path):
    src = """
from conv.app import AUTH_TOKEN, _session

def check():
    if not AUTH_TOKEN:
        return False
    return _session.alive
"""
    assert _analyze(tmp_path, src) == []


def test_nested_function_name_visible_in_outer(tmp_path):
    src = """
def outer():
    def inner():
        return 42
    return inner()
"""
    assert _analyze(tmp_path, src) == []


def test_nested_for_in_with_in_function(tmp_path):
    src = """
def f(paths):
    for p in paths:
        with open(p) as fh:
            for line in fh:
                yield line.strip()
"""
    assert _analyze(tmp_path, src) == []


# ---- known-bad (must produce findings) ----


def test_unqualified_module_global_flagged(tmp_path):
    # _session is referenced but never imported or defined locally
    src = """
def check():
    return _session.alive
"""
    findings = _analyze(tmp_path, src)
    assert len(findings) == 1
    assert findings[0][3] == "_session"
    assert findings[0][2] == "check"


def test_typo_caught(tmp_path):
    src = """
from conv.app import _session

def check():
    return _sesion.alive  # typo
"""
    findings = _analyze(tmp_path, src)
    names = {f[3] for f in findings}
    assert "_sesion" in names


def test_used_in_nested_function(tmp_path):
    src = """
def outer():
    def inner():
        return _foreign  # not defined anywhere
    return inner
"""
    findings = _analyze(tmp_path, src)
    names = {f[3] for f in findings}
    assert "_foreign" in names


# ---- false-positive guards (regression tests for fixed bugs) ----


def test_dict_comp_with_two_targets(tmp_path):
    # alert_responder.py:120 pattern
    src = """
def load(ns):
    return {k: v for k, v in ns.items() if not k.startswith("__")}
"""
    assert _analyze(tmp_path, src) == []


def test_list_comp_inside_call(tmp_path):
    # alert_responder.py:243 pattern
    src = """
def f(fails, warns):
    return sorted(
        [f.get("name", "") if isinstance(f, dict) else str(f) for f in fails]
        + [w.get("name", "") if isinstance(w, dict) else str(w) for w in warns]
    )
"""
    assert _analyze(tmp_path, src) == []


# ---- post-review additions ----


def test_parse_error_raises_typed_exception(tmp_path):
    """Library function MUST NOT call sys.exit. It raises ParseError; main() handles exit."""
    f = tmp_path / "bad.py"
    f.write_text("def f(:\n    pass\n")  # syntax error
    import pytest as _pt

    with _pt.raises(gate.ParseError):
        gate.analyze(f)


def test_globals_get_pattern_still_flagged(tmp_path):
    """Defensive `globals().get(name)` callable check does NOT excuse the name from
    the gate. The unqualified call site is still flagged.

    This pattern exists in conversation_server.py:6023
    (_liveactivity_start_tokens_by_bundle). Slice 8 owns the fix.
    """
    src = """
def f():
    if callable(globals().get("_thing")):
        return _thing()
    return None
"""
    findings = _analyze(tmp_path, src)
    names = {finding[3] for finding in findings}
    assert "_thing" in names


def test_return_annotation_unimported_flagged(tmp_path):
    """Type annotations on parameters and return types are evaluated at function
    definition time in Python <3.12 (without `from __future__ import annotations`).
    The gate correctly flags un-imported annotation types. Documented in the
    script docstring.
    """
    src = """
def handler() -> JsonResponse:
    return {}
"""
    findings = _analyze(tmp_path, src)
    names = {finding[3] for finding in findings}
    assert "JsonResponse" in names


def test_param_annotation_unimported_flagged(tmp_path):
    src = """
def f(x: SomeType) -> None:
    pass
"""
    findings = _analyze(tmp_path, src)
    names = {finding[3] for finding in findings}
    assert "SomeType" in names
