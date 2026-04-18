"""Pure pane-text parsing — extracted from conversation_server.py 2026-04-18.

Phase 3 step 3. No session state, no tmux calls, no logger; callers
supply the text. Kept pure so the Python side and the Swift iOS mirror
can have identical semantics (see
TerminalApp/TerminalApp/Views/SplitTerminalView.swift).
"""


def is_prompt_chrome_line(raw: str) -> bool:
    """Lines that are OK to sit between the option block and the pane bottom
    without invalidating prompt detection (box borders, hints, empty lines).
    """
    t = raw.strip()
    if not t:
        return True
    if t.startswith("╭") or t.startswith("╰") or t.startswith("─"):
        return True
    if t.startswith("│") and t.endswith("│"):
        inner = t[1:-1].strip()
        return not inner
    for hint in ("esc to interrupt", "Press up to edit",
                 "ctrl+t to hide tasks", "shift+tab to"):
        if hint in t:
            return True
    return t in ("❯", ">")


def is_working_indicator(raw: str) -> bool:
    """Signals that Claude is actively processing, not awaiting input.

    Any of these visible in the pane tail means a prompt option block in
    scrollback is stale and must NOT be resurfaced as live buttons.
    """
    t = raw.strip()
    if not t:
        return False
    lower = t.lower()
    if "esc to interrupt" in lower:
        return True
    if "crafting" in lower:
        return True
    if "thinking" in lower and ("token" in lower or "thought for" in lower):
        return True
    for glyph in ("✢", "✶", "✽", "✳", "⚒", "✻"):
        if glyph in t and ("token" in lower or "s ·" in lower or "s |" in lower):
            return True
    return False


def parse_prompt_option_line(raw: str):
    """Parse a single `N. label` prompt-option line. Returns (num, label) or None.

    Note: the caller should separately check `has_active_selector(raw)` if
    it needs to know whether THIS option was the highlighted one. Claude
    Code puts `❯` on exactly one option when the prompt is awaiting input
    and leaves all options bare when the prompt has been dismissed. This
    function strips those markers before parsing."""
    t = raw.strip()
    while t and t[0] in "│❯>•·":
        t = t[1:].strip()
    while t and t[-1] == "│":
        t = t[:-1].strip()
    if "." not in t:
        return None
    dot = t.index(".")
    try:
        num = int(t[:dot])
    except ValueError:
        return None
    if not (1 <= num <= 9):
        return None
    rest = t[dot + 1:].strip()
    if not rest:
        return None
    if len(rest) > 40:
        rest = rest[:37] + "…"
    return (num, rest)


def has_active_selector(raw: str) -> bool:
    """True iff the line starts (after optional box-drawing) with `❯` or `>`.
    Claude Code uses this marker on the currently-highlighted option while
    a prompt awaits input; dismissed prompts show every option bare."""
    t = raw.strip()
    # Strip one box-draw border char if present.
    if t.startswith("│"):
        t = t[1:].strip()
    if not t:
        return False
    return t[0] in ("❯", ">")


def detect_pane_prompt_options_with_reason(pane_text: str):
    """Like detect_pane_prompt_options but returns (options, reason, evidence)
    so callers can log every decision. Tim 2026-04-18: "add logging so you
    can see every time they appear and disappear and why"."""
    if not pane_text:
        return ([], "empty-pane", "")
    lines = pane_text.split("\n")
    tail = lines[-25:]
    tail_sample = " | ".join(tail[-6:])[:400]

    for line in tail:
        if is_working_indicator(line):
            return ([], "working-indicator", f"matched='{line.strip()[:60]}' tail=[{tail_sample}]")

    last_opt_idx = None
    for i in range(len(tail) - 1, -1, -1):
        raw = tail[i]
        if parse_prompt_option_line(raw) is not None:
            last_opt_idx = i
            break
        if not is_prompt_chrome_line(raw):
            return ([], "substantive-line-below", f"aborted-at='{raw.strip()[:60]}' tail=[{tail_sample}]")
    if last_opt_idx is None:
        return ([], "no-option-lines-in-tail", f"tail=[{tail_sample}]")

    start_idx = last_opt_idx
    while start_idx > 0 and parse_prompt_option_line(tail[start_idx - 1]) is not None:
        start_idx -= 1

    collected = []
    any_active = False
    active_line = ""
    for i in range(start_idx, last_opt_idx + 1):
        parsed = parse_prompt_option_line(tail[i])
        if parsed is None:
            return ([], "unparseable-option-in-block", f"failed='{tail[i].strip()[:60]}'")
        collected.append(parsed)
        if has_active_selector(tail[i]):
            any_active = True
            active_line = tail[i]

    if len(collected) < 2:
        return ([], f"too-few-options-{len(collected)}", "")
    for idx, (num, _) in enumerate(collected):
        if num != idx + 1:
            return ([], "non-contiguous-numbering",
                    "collected=" + ", ".join(f"{n}.{l}" for n, l in collected))
    if not any_active:
        return ([], "no-active-selector",
                "collected=" + ", ".join(f"{n}.{l}" for n, l in collected)
                + f" tail=[{tail_sample}]")

    opts = [{"number": n, "label": lbl} for n, lbl in collected]
    return (opts, "active-prompt",
            f"selector-on='{active_line.strip()[:60]}' options="
            + ", ".join(f"{n}.{l}" for n, l in collected))


def detect_pane_prompt_options(pane_text: str):
    """Backward-compat wrapper. Returns just the options list."""
    opts, _, _ = detect_pane_prompt_options_with_reason(pane_text)
    return opts
