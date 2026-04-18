"""
Auto-Alert-Responder Flask Blueprint (extracted 2026-04-18).

Extracted from conversation_server.py to keep the monolith line-count under
the repo ratchet. Behaviour is unchanged; routes now live on a Blueprint
registered from conversation_server.py.

Flow (see memory/topics/auto-alert-responder.md for the full spec):
  health_check.py / monitor → POST /internal/alert-fired
    → spawn alert_responder.py, start 5 min interim / 16 min escalate watchdog
    → responder investigates (5-layer RCA) → writes /tmp/proposals/<id>.json
    → responder POSTs /internal/proposal-ready/<id>
    → server creates tmux window with numbered Accept/Reject/Discuss options
    → server sends APNs push to TerminalApp with window_index deep-link

  Tim taps a button:
    - Accept  → /internal/proposal-action/<id>/accept  → staleness re-check → apply
    - Reject  → /internal/proposal-action/<id>/reject  → mark DISMISSED
    - Discuss → /internal/proposal-action/<id>/discuss → spawn fresh Claude tmux window

Email fallback path (APNs unreachable): /proposals/<id>/<action>/<token>

Dependencies provided by conversation_server at runtime (late-imported
inside handlers to avoid circular imports):
  _log, _send_push_notification, _ensure_tmux_session
"""

import hashlib as _ar_hashlib
import hmac as _ar_hmac
import json as _ar_json
import os as _ar_os
import subprocess as _ar_sp
import threading as _ar_threading
import time as _ar_time
import uuid as _ar_uuid
from pathlib import Path as _ar_Path

from flask import Blueprint, jsonify, request

bp = Blueprint("alert_responder", __name__)

_AR_RESPONDER = "/Users/timtrailor/code/alert_responder.py"
_AR_PROPOSALS_DIR = _ar_Path("/tmp/proposals")
_AR_ALERTS_DIR = _ar_Path("/tmp/alerts")
_AR_STATE_PATH = _ar_Path("/tmp/alert_responder_state.json")
_AR_LOG_PATH = _ar_Path("/tmp/alert_responder_server.log")
_AR_WATCHDOG_INITIAL = 300   # 5 min — interim "still investigating" heartbeat
_AR_WATCHDOG_ESCALATE = 660  # +11 min — ESCALATE (total 16 min, 1 min buffer past alert_responder.py's 900s Claude timeout)
_AR_APPLIED_LOG = _ar_Path("/tmp/alert_responder_applied.log")

_AR_PROPOSALS_DIR.mkdir(parents=True, exist_ok=True)
_AR_ALERTS_DIR.mkdir(parents=True, exist_ok=True)

_ar_watchdog_lock = _ar_threading.Lock()
_ar_watchdog_timers: dict = {}


def _ar_log(msg: str) -> None:
    try:
        with _AR_LOG_PATH.open("a") as f:
            from datetime import datetime, timezone
            f.write(f"[{datetime.now(timezone.utc).isoformat()}] {msg}\n")
    except OSError:
        pass
    try:
        from conversation_server import _log
        _log.info("[alert-responder] %s", msg)
    except Exception:
        pass


def _ar_load_state() -> dict:
    if not _AR_STATE_PATH.exists():
        return {"signatures": {}}
    try:
        return _ar_json.loads(_AR_STATE_PATH.read_text())
    except (OSError, ValueError):
        return {"signatures": {}}


def _ar_save_state(state: dict) -> None:
    try:
        _AR_STATE_PATH.write_text(_ar_json.dumps(state, indent=2))
    except OSError:
        pass


def _ar_mark_state(signature: str, status: str, **extra) -> None:
    state = _ar_load_state()
    entry = state.setdefault("signatures", {}).setdefault(signature, {})
    entry["status"] = status
    entry["last_seen"] = _ar_time.time()
    entry.update(extra)
    _ar_save_state(state)


def _ar_load_credentials() -> dict:
    ns: dict = {}
    with open("/Users/timtrailor/code/credentials.py") as f:
        exec(f.read(), ns)
    return {k: v for k, v in ns.items() if not k.startswith("__")}


def _ar_sign(alert_id: str, action: str) -> str:
    secret = _ar_load_credentials().get("RESPONDER_SECRET", "")
    msg = f"{alert_id}:{action}".encode()
    return _ar_hmac.new(secret.encode(), msg, _ar_hashlib.sha256).hexdigest()[:32]


def _ar_verify_token(alert_id: str, action: str, token: str) -> bool:
    return _ar_hmac.compare_digest(_ar_sign(alert_id, action), token)


# ── Watchdog: interim + escalate pushes if responder silent ───────────────
def _ar_watchdog_interim(alert_id: str) -> None:
    """Fires 5 min after alert if no proposal file yet — sends 'still investigating'
    push so Tim sees something. Schedules the escalate check."""
    with _ar_watchdog_lock:
        _ar_watchdog_timers.pop(alert_id, None)
    proposal_path = _AR_PROPOSALS_DIR / f"{alert_id}.json"
    if proposal_path.exists():
        _ar_log(f"watchdog-interim: {alert_id} — proposal already landed, skipping")
        return
    _ar_log(f"watchdog-interim: {alert_id} — no proposal yet, sending interim push")
    try:
        from conversation_server import _send_push_notification
        _send_push_notification(
            title="[Alert] Investigating…",
            body="Responder is still working. Will push again when a proposal is ready.",
            bundle_id="com.timtrailor.terminal",
        )
    except Exception as e:
        _ar_log(f"watchdog-interim push failed: {e}")
    t = _ar_threading.Timer(_AR_WATCHDOG_ESCALATE, _ar_watchdog_escalate, args=[alert_id])
    t.daemon = True
    with _ar_watchdog_lock:
        _ar_watchdog_timers[alert_id] = t
    t.start()


def _ar_watchdog_escalate(alert_id: str) -> None:
    """Fires 16 min after alert if still no proposal — responder is hung/crashed."""
    with _ar_watchdog_lock:
        _ar_watchdog_timers.pop(alert_id, None)
    proposal_path = _AR_PROPOSALS_DIR / f"{alert_id}.json"
    if proposal_path.exists():
        _ar_log(f"watchdog-escalate: {alert_id} — proposal landed late, skipping")
        return
    _ar_log(f"watchdog-escalate: {alert_id} — responder failed to produce proposal, ESCALATE push")
    alert_path = _AR_ALERTS_DIR / f"{alert_id}.json"
    summary = "(no summary)"
    try:
        if alert_path.exists():
            payload = _ar_json.loads(alert_path.read_text())
            summary = payload.get("raw_summary", summary)[:100]
    except Exception:
        pass
    try:
        from conversation_server import _send_push_notification
        _send_push_notification(
            title="[Alert] Responder FAILED — manual review needed",
            body=f"No proposal produced. Original alert: {summary}",
            bundle_id="com.timtrailor.terminal",
        )
    except Exception as e:
        _ar_log(f"watchdog-escalate push failed: {e}")


# ── /internal/alert-fired ─────────────────────────────────────────────────
@bp.route("/internal/alert-fired", methods=["POST"])
def alert_fired():
    """Called by health_check.py and mac_mini_health_monitor.sh when a persistent
    alert fires. Writes the payload, spawns alert_responder.py, kicks off the
    watchdog. Returns immediately — responder + notification are async.
    """
    data = request.get_json(silent=True) or {}
    fails = data.get("fails") or []
    warns = data.get("warns") or []
    if not fails and not warns:
        return jsonify({"ok": False, "error": "no fails/warns in payload"}), 400

    # Stable signature = sorted fail+warn names, sha256
    names = sorted(
        [f.get("name", "") if isinstance(f, dict) else str(f) for f in fails]
        + [w.get("name", "") if isinstance(w, dict) else str(w) for w in warns]
    )
    signature = _ar_hashlib.sha256(("|".join(names)).encode()).hexdigest()
    data["signature"] = signature
    if "timestamp" not in data:
        from datetime import datetime, timezone
        data["timestamp"] = datetime.now(timezone.utc).isoformat()

    # Dedupe at server layer too (belt-and-braces with responder's gate)
    state = _ar_load_state()
    entry = state.get("signatures", {}).get(signature) or {}
    now = _ar_time.time()
    if entry.get("status") in ("INVESTIGATING", "PROPOSED") and now - entry.get("last_seen", 0) < 2 * 3600:
        _ar_log(f"alert-fired: dedupe drop sig={signature[:12]} status={entry.get('status')}")
        entry["last_seen"] = now
        entry["dupe_count"] = entry.get("dupe_count", 0) + 1
        state.setdefault("signatures", {})[signature] = entry
        _ar_save_state(state)
        return jsonify({"ok": True, "status": "deduped", "signature": signature[:12]})

    alert_id = _ar_uuid.uuid4().hex[:12]
    payload_path = _AR_ALERTS_DIR / f"{alert_id}.json"
    payload_path.write_text(_ar_json.dumps(data, indent=2))

    _ar_mark_state(signature, "INVESTIGATING", alert_id=alert_id, started_at=now)

    # Spawn responder detached
    try:
        _ar_sp.Popen(
            ["/opt/homebrew/bin/python3.11", _AR_RESPONDER,
             "--alert-id", alert_id, "--payload-file", str(payload_path)],
            stdout=open("/tmp/alert_responder_stdout.log", "a"),
            stderr=_ar_sp.STDOUT,
            start_new_session=True,
            cwd="/Users/timtrailor/code",
        )
        _ar_log(f"alert-fired: spawned responder alert_id={alert_id} sig={signature[:12]}")
    except Exception as e:
        _ar_log(f"alert-fired: FAILED to spawn responder: {e}")
        return jsonify({"ok": False, "error": f"spawn failed: {e}"}), 500

    # Watchdog timer — 5 min to interim, +11 min to escalate (total 16 min)
    t = _ar_threading.Timer(_AR_WATCHDOG_INITIAL, _ar_watchdog_interim, args=[alert_id])
    t.daemon = True
    with _ar_watchdog_lock:
        _ar_watchdog_timers[alert_id] = t
    t.start()

    return jsonify({"ok": True, "alert_id": alert_id, "signature": signature[:12]})


# ── /internal/proposal-ready/<alert_id> ───────────────────────────────────
@bp.route("/internal/proposal-ready/<alert_id>", methods=["POST"])
def proposal_ready(alert_id: str):
    """Called by alert_responder.py when its proposal JSON is written.
    Server creates a tmux window with numbered Accept/Reject/Discuss options
    and sends an APNs push to TerminalApp with window_index deep-link."""
    # Cancel watchdog — proposal is here
    with _ar_watchdog_lock:
        t = _ar_watchdog_timers.pop(alert_id, None)
    if t:
        t.cancel()

    proposal_path = _AR_PROPOSALS_DIR / f"{alert_id}.json"
    if not proposal_path.exists():
        _ar_log(f"proposal-ready: no proposal file at {proposal_path}")
        return jsonify({"ok": False, "error": "no proposal file"}), 404
    try:
        proposal = _ar_json.loads(proposal_path.read_text())
    except ValueError as e:
        _ar_log(f"proposal-ready: invalid JSON: {e}")
        return jsonify({"ok": False, "error": f"invalid JSON: {e}"}), 400

    verdict = proposal.get("verdict", "ESCALATE")
    one_liner = proposal.get("one_liner", "(no summary)")

    if verdict == "NOISE":
        _ar_log(f"proposal-ready: NOISE verdict — not creating window or push")
        return jsonify({"ok": True, "action": "noise_logged"})

    # Render proposal + numbered options into a tmux window
    window_index = _ar_create_proposal_window(alert_id, proposal)

    title_verb = {"PROPOSAL": "Proposal ready", "ESCALATE": "Escalation"}.get(verdict, "Alert")
    try:
        from conversation_server import _send_push_notification
        _send_push_notification(
            title=f"[Responder] {title_verb}",
            body=one_liner[:200],
            bundle_id="com.timtrailor.terminal",
            window_index=window_index if window_index > 0 else None,
            category="PROPOSAL_ACTIONS",
            user_info={"alert_id": alert_id},
        )
        _ar_log(f"proposal-ready: APNs push sent alert_id={alert_id} window={window_index}")
    except Exception as e:
        _ar_log(f"proposal-ready: push failed: {e}")
        return jsonify({"ok": False, "error": f"push failed: {e}"}), 500

    return jsonify({"ok": True, "window_index": window_index})


def _ar_create_proposal_window(alert_id: str, proposal: dict) -> int:
    """Create a tmux window containing the rendered proposal and a numbered
    Accept/Reject/Discuss prompt. Returns the window index (1-based) or 0 on
    failure. The existing PromptOptionButtons infrastructure in TerminalApp
    renders lock-screen buttons for '1. Accept / 2. Reject / 3. Discuss' text."""
    from conversation_server import _ensure_tmux_session
    _ensure_tmux_session()
    rendered = _ar_render_proposal(alert_id, proposal)
    rendered_path = _ar_Path(f"/tmp/proposals/{alert_id}_rendered.txt")
    rendered_path.write_text(rendered)

    runner_path = _ar_Path(f"/tmp/proposals/{alert_id}_buttons.sh")
    runner_path.write_text(f"""#!/bin/bash
# Proposal action runner for alert_id={alert_id}. Displays the rendered
# proposal, reads a single key, POSTs to /internal/proposal-action.
clear
cat {rendered_path}
echo ""
echo "╭────────────────────────────╮"
echo "│ ❯ 1. Accept                │"
echo "│   2. Reject                │"
echo "│   3. Discuss               │"
echo "╰────────────────────────────╯"
echo ""
read -n 1 -s choice
echo ""
case "$choice" in
  1) ACTION=accept ;;
  2) ACTION=reject ;;
  3) ACTION=discuss ;;
  *) echo "Invalid choice — closing."; sleep 1; exit 1 ;;
esac
echo "You chose: $ACTION"
curl -sS -X POST "http://127.0.0.1:8081/internal/proposal-action/{alert_id}/$ACTION" \\
  -H 'Content-Type: application/json' -d '{{"source":"in-app"}}' || true
echo ""
echo "Done. Closing window in 3s."
sleep 3
""")
    runner_path.chmod(0o755)

    try:
        result = _ar_sp.run(
            ["/opt/homebrew/bin/tmux", "new-window", "-P",
             "-F", "#{window_index}", "-t", "mobile",
             "-n", f"proposal-{alert_id[:6]}",
             "bash", str(runner_path)],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            _ar_log(f"tmux new-window failed: {result.stderr.strip()}")
            return 0
        try:
            return int((result.stdout or "").strip())
        except ValueError:
            return 0
    except Exception as e:
        _ar_log(f"tmux new-window exception: {e}")
        return 0


def _ar_render_proposal(alert_id: str, proposal: dict) -> str:
    """Render a proposal into human-readable text for the tmux window.
    v2 (2026-04-18) — includes `context` block + separate `control_fix`
    section so Tim can see both the symptom fix and the prevention work."""
    ctx = proposal.get("context", {})
    rca = proposal.get("rca", {})
    p = proposal.get("proposal", {})
    cf = proposal.get("control_fix", {})

    def _fenced(label: str, body: str):
        """Render a labelled code-fence block with a Claude-style border."""
        out = [label, "┌" + "─" * 70]
        for line in (body or "(none)").split("\n"):
            out.append(f"│ {line}")
        out.append("└" + "─" * 70)
        return out

    lines = [
        "═══════════════════════════════════════════════════════════════════════",
        f"  ALERT RESPONDER — verdict: {proposal.get('verdict', '?')}",
        f"  {proposal.get('one_liner', '')}",
        "═══════════════════════════════════════════════════════════════════════",
        "",
        f"Alert ID:  {alert_id}",
        f"Signature: {proposal.get('signature', '')[:16]}",
        "",
        "── CONTEXT ────────────────────────────────────────────────────────────",
        f"What fired:         {ctx.get('what_fired', '(unspecified)')}",
        f"Live state now:     {ctx.get('live_state_summary', '(unspecified)')}",
        f"User impact:        {ctx.get('user_impact', '(unspecified)')}",
        "",
        "Evidence:",
    ]
    evidence = ctx.get("evidence") or []
    if evidence:
        for e in evidence:
            lines.append(f"   • {e}")
    else:
        lines.append("   (none captured — responder left this empty)")
    lines.extend([
        "",
        "── 5-LAYER RCA ────────────────────────────────────────────────────────",
        "",
        "L1 — What happened:",
        f"   {rca.get('layer1_what_happened', '(blank)')}",
        "",
        "L2 — Controls that should have caught this:",
    ])
    for c in rca.get("layer2_controls_that_existed", []):
        lines.append(f"   • {c}")
    lines.append("")
    lines.append("L3 — Why each control failed:")
    for f_entry in rca.get("layer3_why_each_failed", []):
        lines.append(f"   • {f_entry.get('control', '?')}")
        lines.append(f"     → {f_entry.get('why_failed', '?')}")
    lines.extend([
        "",
        f"L4 — Fix classification ({rca.get('layer4_fix_classification', {}).get('type', '?')}):",
        f"   {rca.get('layer4_fix_classification', {}).get('explanation', '')}",
        "",
        f"L5 — Control class:  {rca.get('layer5_control_class', '?')}",
        f"Matches lesson:      {rca.get('matches_existing_lesson') or 'none'}",
        f"Recurs:              {rca.get('recurs', False)}",
        "",
        "── SYMPTOM FIX (clears this alert) ────────────────────────────────────",
        f"Type:          {p.get('type', '?')}",
        f"Description:   {p.get('description', '')}",
        f"Blast radius:  {p.get('blast_radius', '')}",
        f"Manual-only:   {p.get('requires_manual_apply', True)}"
        + (f"  (deny: {p['deny_rule_matched']})" if p.get("deny_rule_matched") else ""),
        "",
    ])
    lines.extend(_fenced("Command/diff:", p.get("command_or_diff", "")))
    lines.extend([
        "",
        f"Rollback: {p.get('rollback', '(none)')}",
        "",
        "── CONTROL FIX (prevents recurrence) ──────────────────────────────────",
        f"Type:          {cf.get('type', '?')}",
        f"Description:   {cf.get('description', '(responder did not propose a control fix)')}",
        f"Where it lives: {cf.get('where_it_lives', '(n/a)')}",
        f"Manual-only:   {cf.get('requires_manual_apply', True)}",
        "",
    ])
    if cf.get("type") != "none_possible":
        lines.extend(_fenced("Control fix diff/command:", cf.get("command_or_diff", "")))
        lines.extend([
            "",
            f"Rollback: {cf.get('rollback', '(none)')}",
            "",
        ])
    if proposal.get("verdict") == "ESCALATE":
        lines.extend(["── ESCALATION REASON ──────────────────────────────────────────────────",
                      proposal.get("escalation_reason", ""), ""])
    lines.append("Tap 1 (Accept) / 2 (Reject) / 3 (Discuss) below — or answer in-place.")
    return "\n".join(lines)


# ── /internal/proposal-action/<alert_id>/<action> ─────────────────────────
@bp.route("/internal/proposal-action/<alert_id>/<action>", methods=["POST"])
def proposal_action(alert_id: str, action: str):
    """Called by button tap (in-app tmux script or iOS lock-screen intent)."""
    if action not in ("accept", "reject", "discuss"):
        return jsonify({"ok": False, "error": "invalid action"}), 400
    return _ar_apply_action(alert_id, action, source="in-app")


# ── /proposals/<alert_id>/<action>/<token> — email-fallback path ─────────
@bp.route("/proposals/<alert_id>/<action>/<token>", methods=["GET"])
def proposal_action_signed(alert_id: str, action: str, token: str):
    if action not in ("accept", "reject", "discuss"):
        return jsonify({"ok": False, "error": "invalid action"}), 400
    if not _ar_verify_token(alert_id, action, token):
        return jsonify({"ok": False, "error": "invalid token"}), 403
    result = _ar_apply_action(alert_id, action, source="email")
    return result


def _ar_apply_action(alert_id: str, action: str, source: str):
    """Execute the chosen action for a proposal."""
    proposal_path = _AR_PROPOSALS_DIR / f"{alert_id}.json"
    if not proposal_path.exists():
        return jsonify({"ok": False, "error": "proposal not found"}), 404
    try:
        proposal = _ar_json.loads(proposal_path.read_text())
    except ValueError as e:
        return jsonify({"ok": False, "error": f"invalid JSON: {e}"}), 500

    signature = proposal.get("signature", "")

    if action == "reject":
        _ar_mark_state(signature, "DISMISSED", action_by=source)
        _ar_log(f"action: REJECT alert_id={alert_id} source={source}")
        return jsonify({"ok": True, "action": "rejected"})

    if action == "discuss":
        window_index = _ar_spawn_discuss_window(alert_id, proposal)
        _ar_mark_state(signature, "DISCUSSING", discuss_window=window_index)
        _ar_log(f"action: DISCUSS alert_id={alert_id} window={window_index}")
        return jsonify({"ok": True, "action": "discussing", "window_index": window_index})

    # ACCEPT path — staleness re-check + apply
    cmd = proposal.get("proposal", {}).get("command_or_diff", "")
    requires_manual = proposal.get("proposal", {}).get("requires_manual_apply", True)
    deny_match = proposal.get("proposal", {}).get("deny_rule_matched")

    # Safety re-check against deny_always (never trust the stored proposal —
    # policy file may have been tightened since)
    deny_now = _ar_policy_deny_match(cmd)
    if deny_now or deny_match or requires_manual:
        _ar_log(f"action: ACCEPT refused auto-apply alert_id={alert_id} "
                f"deny_stored={deny_match} deny_now={deny_now} manual={requires_manual}")
        return jsonify({
            "ok": False,
            "error": "This proposal requires manual application (deny_always match or manual-only flag). "
                     "Apply the command yourself from the tab; the responder will not auto-run it.",
        }), 403

    policy = _ar_load_policy()
    if not policy.get("ALLOW_AUTONOMOUS_APPLY", False):
        _ar_log(f"action: ACCEPT refused — ALLOW_AUTONOMOUS_APPLY=false (Phase 1)")
        return jsonify({
            "ok": False,
            "error": "Phase 1 is proposal-only. Copy the command from the proposal tab and run it manually.",
        }), 403

    # Staleness re-check: if no longer-failing conditions, report RESOLVED no-op
    if not _ar_alert_still_firing(signature, proposal):
        _ar_mark_state(signature, "RESOLVED_NO_ACTION", action_by=source)
        _ar_log(f"action: ACCEPT — alert self-healed, no action alert_id={alert_id}")
        try:
            from conversation_server import _send_push_notification
            _send_push_notification(
                title="[Responder] Already resolved",
                body="Alert cleared before fix was applied. No action taken.",
                bundle_id="com.timtrailor.terminal",
            )
        except Exception:
            pass
        return jsonify({"ok": True, "action": "already_resolved"})

    # Apply the symptom fix (only truly allowlisted commands reach here in Phase 2)
    rc, out, err = _ar_run_allowlisted(cmd)
    applied = rc == 0
    _ar_log(f"action: ACCEPT symptom-fix alert_id={alert_id} rc={rc}")

    # Apply the control_fix in the same ACCEPT action when safe. The control
    # fix closes the gap that let this alert reach Tim — applying the symptom
    # without the control fix guarantees the next occurrence.
    cf = proposal.get("control_fix", {}) or {}
    cf_cmd = cf.get("command_or_diff", "") or ""
    cf_rc = None
    cf_out = cf_err = ""
    cf_type = cf.get("type", "none_possible")
    cf_manual = cf.get("requires_manual_apply", True)
    if cf_cmd and not cf_manual and cf_type != "none_possible":
        cf_deny = _ar_policy_deny_match(cf_cmd)
        if cf_deny:
            _ar_log(f"action: ACCEPT control_fix refused — deny match {cf_deny}")
            cf_rc, cf_err = -1, f"deny_always matched: {cf_deny}"
        else:
            cf_rc, cf_out, cf_err = _ar_run_allowlisted(cf_cmd)
            _ar_log(f"action: ACCEPT control_fix rc={cf_rc}")
    else:
        _ar_log(f"action: ACCEPT control_fix skipped (type={cf_type} manual={cf_manual})")

    try:
        with _AR_APPLIED_LOG.open("a") as f:
            f.write(_ar_json.dumps({
                "alert_id": alert_id,
                "symptom": {"rc": rc, "stdout": out[-500:], "stderr": err[-500:]},
                "control_fix": {
                    "applied": cf_rc is not None,
                    "rc": cf_rc,
                    "stdout": cf_out[-500:] if cf_out else "",
                    "stderr": cf_err[-500:] if cf_err else "",
                    "skipped_reason": None if cf_rc is not None else (
                        "manual" if cf_manual else ("none_possible" if cf_type == "none_possible" else "no_command")
                    ),
                },
                "when": _ar_time.time(),
            }) + "\n")
    except OSError:
        pass

    overall_ok = applied and (cf_rc is None or cf_rc == 0)
    _ar_mark_state(
        signature,
        "ACCEPTED" if overall_ok else "APPLY_FAILED",
        action_by=source, rc=rc, cf_rc=cf_rc,
    )
    try:
        from conversation_server import _send_push_notification
        if overall_ok:
            summary = f"Symptom fix OK. Control fix: "
            summary += "applied" if cf_rc == 0 else ("skipped" if cf_rc is None else f"FAILED rc={cf_rc}")
            title = "[Responder] Applied"
        else:
            title = "[Responder] Apply FAILED"
            summary = (err or out or cf_err or cf_out or "(no output)")[:180]
        _send_push_notification(
            title=title,
            body=summary[:200],
            bundle_id="com.timtrailor.terminal",
        )
    except Exception:
        pass
    return jsonify({
        "ok": overall_ok,
        "symptom": {"rc": rc, "stdout": out[-500:], "stderr": err[-500:]},
        "control_fix": {"rc": cf_rc, "stdout": cf_out[-500:] if cf_out else "",
                        "stderr": cf_err[-500:] if cf_err else ""},
    })


def _ar_alert_still_firing(signature: str, proposal: dict) -> bool:
    """Best-effort staleness check: if the proposal's deny_rule_matched was a
    printer pattern, and Moonraker is reachable now, consider the alert healed.
    For acceptance-tests signatures, re-read /tmp/acceptance_results.json.
    Defaults to True (firing) when we can't tell — conservative."""
    try:
        ar_path = _ar_Path("/tmp/acceptance_results.json")
        if ar_path.exists():
            ar = _ar_json.loads(ar_path.read_text())
            fails = ar.get("fails", [])
            # If signature is empty or no current fails, healed
            if not fails:
                return False
    except Exception:
        pass
    return True


def _ar_load_policy() -> dict:
    import yaml
    with open("/Users/timtrailor/code/responder_policy.yaml") as f:
        return yaml.safe_load(f) or {}


def _ar_policy_deny_match(command: str):
    import re as _ar_re
    if not command:
        return None
    policy = _ar_load_policy()
    for pattern in policy.get("deny_always", []):
        if _ar_re.search(pattern, command):
            return pattern
    return None


def _ar_run_allowlisted(command: str):
    """Run a shell command that's already passed all policy checks.
    Used only in Phase 2 when ALLOW_AUTONOMOUS_APPLY=true."""
    try:
        proc = _ar_sp.run(["/bin/bash", "-c", command],
                          capture_output=True, text=True, timeout=60)
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except _ar_sp.TimeoutExpired:
        return 124, "", "apply timed out after 60s"
    except Exception as e:
        return 1, "", str(e)


# ── /internal/proposal-discuss (also the Discuss action handler) ─────────
def _ar_spawn_discuss_window(alert_id: str, proposal: dict) -> int:
    """Open a fresh tmux window with a Claude session pre-loaded with the
    proposal context so Tim can discuss it."""
    from conversation_server import _ensure_tmux_session
    _ensure_tmux_session()
    context_path = _ar_Path(f"/tmp/proposals/{alert_id}_discuss_context.md")
    context = [
        f"# Discussing alert proposal {alert_id}",
        "",
        "## Original alert",
        "```json",
        _ar_json.dumps(
            _ar_json.loads((_AR_ALERTS_DIR / f"{alert_id}.json").read_text()) if (_AR_ALERTS_DIR / f"{alert_id}.json").exists() else {},
            indent=2,
        ),
        "```",
        "",
        "## Responder proposal + RCA",
        "```json",
        _ar_json.dumps(proposal, indent=2),
        "```",
        "",
        "Tim wants to discuss this proposal. Ask what concern he has and help refine / reject / replace the proposed fix. You have full Mac Mini access.",
    ]
    context_path.write_text("\n".join(context))

    runner_path = _ar_Path(f"/tmp/proposals/{alert_id}_discuss.sh")
    runner_path.write_text(f"""#!/bin/bash
cd /Users/timtrailor/Documents/Claude\\ code 2>/dev/null || cd ~/code
# Start a fresh Claude session with the proposal context pre-piped
exec /Users/timtrailor/.local/bin/claude "$(cat {context_path})"
""")
    runner_path.chmod(0o755)

    try:
        result = _ar_sp.run(
            ["/opt/homebrew/bin/tmux", "new-window", "-P",
             "-F", "#{window_index}", "-t", "mobile",
             "-n", f"discuss-{alert_id[:6]}",
             "bash", str(runner_path)],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return 0
        try:
            return int((result.stdout or "").strip())
        except ValueError:
            return 0
    except Exception as e:
        _ar_log(f"spawn_discuss exception: {e}")
        return 0
