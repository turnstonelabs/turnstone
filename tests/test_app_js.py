"""Static smoke guards for ``turnstone/ui/static/app.js``.

The interactive WebUI's app.js has no JS test framework on the
project side. This file holds Python-side string-presence assertions
that catch regressions on critical paths — the kind of one-line
deletion or rename that breaks the UI silently and only surfaces in
manual testing.
"""

from __future__ import annotations

import re
from pathlib import Path

_APP_JS = Path(__file__).resolve().parent.parent / "turnstone/ui/static/app.js"


def test_switch_tab_bootstraps_pane_when_none_exists() -> None:
    """``switchTab`` must create a pane when none exists. A fresh-
    loaded interactive UI with no workstreams shows the dashboard
    and creates no panes (per ``initWorkstreams``); the user's first
    ``create`` or ``open`` then calls ``switchTab(newWsId)``. Pre-fix,
    the early ``if (!pane) return;`` left switchTab with nowhere to
    attach — the chat UI never connected SSE for the freshly-created
    workstream, and only a page refresh fixed it. This test guards
    against accidentally re-introducing the early-return."""
    body = _APP_JS.read_text(encoding="utf-8")
    start = body.index("function switchTab(wsId) {")
    # Bound the search to the function body — switchTab is short.
    fn = body[start : start + 2000]
    assert "if (!pane) return;" not in fn, (
        "switchTab must not early-return when no pane exists — that's "
        "the no-chat-after-first-create bug. Bootstrap a pane instead."
    )
    # Affirmatively check the bootstrap path exists.
    assert "createPane(wsId)" in fn, (
        "switchTab must call createPane(wsId) to bootstrap the first "
        "pane when getFocusedPane returns null"
    )


def test_tool_error_does_not_overwrite_approval_badge() -> None:
    """When an approved tool subsequently errors, the existing
    ``✓ approved`` (or ``✓ auto-approved``) pill must remain visible —
    the error indicator is appended as a sibling pill, not by mutating
    the approval pill in place. Pre-fix, both ``appendToolOutput``
    (live) and ``replayHistory`` (history reconstruction) located the
    existing approval badge via ``querySelector(".ts-approval-badge")``
    and overwrote its className + textContent with the ``--error``
    state, so the user lost the record that they had approved the
    call. This test pins the new append-sibling behaviour."""
    body = _APP_JS.read_text(encoding="utf-8")
    # Affirmatively check that an idempotency guard exists somewhere:
    # a ``querySelector(".ts-approval-badge--error")`` lookup is the
    # structural marker of the fix. Pre-fix the modifier never appeared
    # in app.js at all. Loose on quote style and surrounding form (the
    # guard might be a negated ``if (!q) {build...}`` block at a call
    # site, or a positive ``if (q) return;`` early-exit inside an
    # extracted helper) so a later refactor doesn't trip CI on
    # cosmetics.
    error_guard_re = re.compile(
        r"""querySelector\(\s*['"]\.ts-approval-badge--error['"]\s*\)""",
    )
    assert error_guard_re.search(body), (
        "The error-badge code path must guard creation with a "
        "querySelector for .ts-approval-badge--error so duplicate fires "
        "(live + history re-render) do not stack badges."
    )
    # Forbid the mutate-existing-badge sequence: a generic
    # ``.ts-approval-badge`` lookup followed within a handful of lines
    # by mutating that same handle into the ``--error`` state. Two
    # unrelated call sites (history rendering + live tool-output
    # insertion) legitimately query ``.ts-approval-badge`` to position
    # output above it, so the bare query alone is not the anti-pattern;
    # the close pairing with an ``--error`` class mutation is. Accept
    # either quote style and catch both ``className = "..."`` and
    # ``classList.add("ts-approval-badge--error")`` forms.
    overwrite_re = re.compile(
        r"""(\w+)\s*=\s*\w+\.querySelector\(\s*(["'])\.ts-approval-badge\2\s*\)\s*;"""
        r""".{0,200}?"""
        r"""(?:"""
        r"""\1\.className\s*=\s*(["'])[^"']*\bts-approval-badge--error\b[^"']*\3"""
        r"""|"""
        r"""\1\.classList\.add\([^)]*(["'])ts-approval-badge--error\4[^)]*\)"""
        r""")""",
        re.DOTALL,
    )
    assert not overwrite_re.search(body), (
        "Found the badge-overwrite anti-pattern: a queried "
        ".ts-approval-badge handle is mutated into the --error variant "
        "(via className overwrite or classList.add). Append a sibling "
        "badge instead so the approval verdict stays visible alongside "
        "the error."
    )


def test_replay_history_renders_content_before_tool_block() -> None:
    """In ``replayHistory``'s ``role === "assistant"`` branch, the
    ``msg.content`` render must precede the ``msg.tool_calls`` render.

    Two reasons, both load-bearing:

    1. **Structural** — the next loop iteration's ``role === "tool"``
       message anchors to ``lastToolBlock``. The tool-block branch sets
       that anchor; the content branch clears it. If content runs after
       the tool block, the clear silently drops the upcoming tool
       result. Pre-fix, every interactive tool result was missing from
       saved-workstream replays whenever the assistant turn carried
       both narration and tool calls (very common output shape).

    2. **Visual** — the live SSE path renders content first
       (``stream_text`` streams before ``tool_info`` /
       ``approve_request``), so replay should match.

    The test pins the order via the offsets of the ``msg.content`` and
    ``msg.tool_calls`` branch headers inside the function body."""
    body = _APP_JS.read_text(encoding="utf-8")
    start = body.index("Pane.prototype.replayHistory = function")
    end = body.index("Pane.prototype._attachRetryToLastAssistant", start)
    fn = body[start:end]
    # Locate the assistant branch and bound the search to its body —
    # the function also handles user / tool roles which would otherwise
    # confuse the offset comparison.
    asst_start = fn.index('msg.role === "assistant"')
    asst_end = fn.index('msg.role === "tool"', asst_start)
    asst = fn[asst_start:asst_end]
    content_idx = asst.index("if (msg.content)")
    tool_calls_idx = asst.index("if (msg.tool_calls && msg.tool_calls.length)")
    assert content_idx < tool_calls_idx, (
        "replayHistory must render msg.content BEFORE msg.tool_calls "
        "inside the assistant branch — otherwise the lastToolBlock "
        "anchor is clobbered before the next iteration's tool result "
        "can attach to it (and the visual order also drifts from the "
        "live SSE flow)."
    )


def test_replay_history_renders_persisted_verdict_badge() -> None:
    """Saved-workstream replays must paint the persisted intent verdict
    next to each tool div, using the same ``renderVerdictBadge`` helper
    the live ``showInlineToolBlock`` path uses. Pre-fix the audit trail
    was complete in storage (``intent_verdicts`` table) but never
    surfaced on replay — operators reviewing a saved workstream
    couldn't see what the heuristic / LLM judge thought of any tool
    call. This test pins the call site so a refactor that drops the
    decoration regresses the audit surface."""
    body = _APP_JS.read_text(encoding="utf-8")
    start = body.index("Pane.prototype.replayHistory = function")
    end = body.index("Pane.prototype._attachRetryToLastAssistant", start)
    fn = body[start:end]
    # Match a `renderVerdictBadge(<something>.verdict, ...)` call inside
    # the replay loop.  Loose on whitespace + identifier so a future
    # rename of the iteration variable doesn't trip CI.
    badge_call_re = re.compile(
        r"renderVerdictBadge\(\s*\w+\.verdict\b",
    )
    assert badge_call_re.search(fn), (
        "replayHistory must call renderVerdictBadge(tc.verdict, ...) "
        "when a persisted verdict is attached to a tool_call entry — "
        "otherwise the audit-trail data persisted to intent_verdicts "
        "doesn't surface on saved-workstream replays."
    )
