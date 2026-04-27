"""Static smoke guards for ``turnstone/ui/static/app.js``.

The interactive WebUI's app.js has no JS test framework on the
project side. This file holds Python-side string-presence assertions
that catch regressions on critical paths — the kind of one-line
deletion or rename that breaks the UI silently and only surfaces in
manual testing.
"""

from __future__ import annotations

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
