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
