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
    # Affirmatively check that both sites construct a fresh error badge
    # under an idempotency guard keyed off the ``--error`` modifier.
    # The presence of these guards is the structural marker of the fix
    # — pre-fix the code mutated the existing approval badge, so neither
    # selector appeared anywhere in the file.
    assert '!parentBlock.querySelector(".ts-approval-badge--error")' in body, (
        "appendToolOutput must guard error-badge creation with an "
        "existence check on .ts-approval-badge--error so duplicate fires "
        "do not stack badges."
    )
    assert '!lastToolBlock.querySelector(".ts-approval-badge--error")' in body, (
        "replayHistory must guard error-badge creation with an "
        "existence check on .ts-approval-badge--error so re-renders do "
        "not stack badges."
    )
    # Forbid the specific mutate-existing-badge sequence: a generic
    # ``.ts-approval-badge`` lookup followed within a handful of lines
    # by an assignment of the ``--error`` modifier to the same handle.
    # Two unrelated call sites (history rendering + live tool-output
    # insertion) legitimately query ``.ts-approval-badge`` to position
    # output above it, so the bare query alone is not the anti-pattern;
    # the close pairing with an ``--error`` className overwrite is.
    pattern = re.compile(
        r'(\w+)\s*=\s*\w+\.querySelector\("\.ts-approval-badge"\)\s*;'
        r".{0,200}?"
        r'\1\.className\s*=\s*"ts-approval-badge ts-approval-badge--error"',
        re.DOTALL,
    )
    assert not pattern.search(body), (
        "Found the badge-overwrite anti-pattern: a queried "
        ".ts-approval-badge handle whose className is reassigned to "
        "the --error variant. Append a sibling badge instead so the "
        "approval verdict stays visible alongside the error."
    )
