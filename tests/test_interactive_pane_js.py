"""Static smoke guards for the shared interactive pane module.

``turnstone/shared_static/interactive.js`` is the per-workstream conversational
``Pane`` lifted out of ``ui/static/app.js`` (L-shell step 5a) so BOTH the
standalone ``turnstone-server`` UI and the console L-shell can mount it.  The
load-bearing invariants of that extraction are pinned here — like the rest of
the WebUI, the module has no JS test framework, so these are Python-side
string-presence assertions that catch the silent one-line regression.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_INTERACTIVE = _ROOT / "turnstone/shared_static/interactive.js"
_APP = _ROOT / "turnstone/ui/static/app.js"
_UI_INDEX = _ROOT / "turnstone/ui/static/index.html"


def _strip_comments(js: str) -> str:
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    js = re.sub(r"//[^\n]*", "", js)
    return js


def test_interactive_is_esm_imported_by_the_shell() -> None:
    """Real ES module: it ``export``s the factory the shell imports in BOTH
    deployments.  Step 6 retired the window bridge (no window.InteractivePane)
    and the standalone HTML no longer script-tags interactive.js — shell.js
    pulls it via ``import``."""
    body = _INTERACTIVE.read_text(encoding="utf-8")
    assert "export { Pane as InteractivePane, createInteractivePane };" in body
    assert "window.InteractivePane = Pane" not in body, (
        "the window bridge is retired — the shell imports the factory (ESM)."
    )
    html = _UI_INDEX.read_text(encoding="utf-8")
    assert "/shared/interactive.js" not in html, (
        "the standalone HTML must NOT script-tag interactive.js — shell.js imports it."
    )


def test_pane_constructor_takes_transport_and_host_seam() -> None:
    """The constructor grew the ``(wsId, opts)`` seam: a transport ``base`` (the
    node-proxy prefix), an ``embedded`` flag (drop the standalone split-pane
    chrome), and a ``host`` adapter for the few things only the surrounding
    shell knows."""
    body = _INTERACTIVE.read_text(encoding="utf-8")
    assert "constructor(wsId, opts) {" in body
    for field in (
        "this._base = opts.base",
        "this._embedded = !!opts.embedded",
        "this._host = opts.host || INTERACTIVE_DEFAULT_HOST",
    ):
        assert field in body, f"missing constructor seam: {field!r}"


def test_transport_urls_are_base_prefixed() -> None:
    """Every per-ws request is prefixed with ``this._base`` so a console pane
    proxies through ``/node/{id}`` (the LOCALITY invariant: an interactive
    session lives on a cluster node).  A bare ``/v1/api/workstreams/`` URL would
    hit the console instead of the node and silently 404 / cross-talk."""
    body = _strip_comments(_INTERACTIVE.read_text(encoding="utf-8"))
    # Collapse whitespace so a prettier line-wrap (``this._base +`` on the line
    # ABOVE the URL string) doesn't read as a bare URL: every workstream URL
    # must be preceded by ``this._base +``.
    collapsed = re.sub(r"\s+", " ", body)
    bad = re.findall(r'(?<!this\._base \+ )"/v1/api/workstreams/"', collapsed)
    assert not bad, (
        "found a bare '/v1/api/workstreams/' URL not prefixed by this._base — "
        "a console pane would route it to the console, not the owning node."
    )
    # The EventSource + history + send all go through the base.
    assert 'this._base + "/v1/api/workstreams/"' in collapsed
    assert "new EventSource(evtUrl" in body


def test_embedded_chrome_is_gated() -> None:
    """The standalone split-pane affordances (focus tracking, context menu,
    split/close buttons) AND the pane header are gated behind ``!this._embedded``:
    the console (embedded) pane has NO header — name / persona / state live in
    the tab + rail, and the conversation reclaims the full pane height."""
    body = _INTERACTIVE.read_text(encoding="utf-8")
    assert "if (!this._embedded) {" in body, "standalone chrome must be gated"
    assert 'this.el.classList.add("pane--embedded")' in body
    # The embedded pane builds NO header — the persona tag is gone (the rail's
    # INT/COORD vocabulary shows it instead).
    assert '"pane-persona-tag"' not in body
    assert '"INTERACTIVE"' not in body
    # The header (workstream name + split/close actions) builds for standalone
    # only — gated, not unconditional.
    assert 'this.headerEl = document.createElement("div")' in body


def test_factory_returns_lifecycle_over_node_proxy() -> None:
    """``createInteractivePane`` is the console factory (mirrors
    ``createCoordinatorPane``): it derives the node-proxy base from ``nodeId``
    and returns the lifecycle controller the shell drives."""
    body = _INTERACTIVE.read_text(encoding="utf-8")
    assert "function createInteractivePane(root, wsId, opts) {" in body
    assert '"/node/" + encodeURIComponent(opts.nodeId)' in body
    for hook in ("connect()", "deactivate()", "onLogin()", "destroy()"):
        assert hook in body, f"factory controller missing lifecycle hook {hook!r}"
    # Teardown must close the stream so a backgrounded pane can't leak an
    # upstream node connection.
    assert "pane.disconnectSSE();" in body


def test_host_seam_routes_shell_couplings() -> None:
    """Every coupling to the surrounding shell goes through ``this._host`` — so
    the same Pane works standalone (real adapter) and console-embedded (no-op /
    Tier-1 adapter).  No direct ``focusedPaneId`` / ``workstreams`` / consent
    badge reference survives in the module."""
    body = _INTERACTIVE.read_text(encoding="utf-8")
    for call in (
        "this._host.getWsName(",
        "this._host.isFocused(this)",
        "this._host.onStreamError(this)",
        "this._host.warningTarget(this)",
        "this._host.onConsentDetected(",
    ):
        assert call in body, f"missing host seam call {call!r}"
    code = _strip_comments(body)
    # The classic split-pane shell globals must not leak into the module as
    # bare code references (URL path strings excepted, handled above).
    assert not re.search(r"(?<![\w$./\"])focusedPaneId(?![\w$])", code), (
        "focusedPaneId leaked into the shared module — route it through "
        "host.isFocused so the console (which has no such global) still works."
    )
    assert "_pendingConsentServers" not in code, (
        "the consent-badge state must stay in the standalone shell; the pane "
        "only notifies via host.onConsentDetected."
    )


def test_standalone_opens_sessions_via_the_shell_pane_manager() -> None:
    """Step 6 retired the standalone's local split-pane construction: app.js no
    longer builds panes via window.InteractivePane / STANDALONE_HOST.  Sessions
    open through the shared shell's PaneManager — openSessionPane delegates to
    openPane('interactive', wsId)."""
    app = _APP.read_text(encoding="utf-8")
    assert "STANDALONE_HOST" not in app, "the standalone host adapter is retired."
    assert "new window.InteractivePane(" not in app, (
        "the standalone no longer constructs panes locally."
    )
    start = app.index("function openSessionPane(wsId)")
    fn = app[start : start + 300]
    assert 'openPane("interactive", wsId)' in fn, (
        "openSessionPane must open the session as a pane via the shell PaneManager."
    )


def test_approval_keyboard_shortcuts_wired() -> None:
    """The converged card advertises y/n/a (+Enter/Esc) kbd hints, so the pane
    must route those keys to resolveApproval when a pending approval is up —
    pane-owned on this.el (the fork collapse retired the old app.js global
    handler + getFocusedPane).  Guards against the chips over-promising."""
    body = _INTERACTIVE.read_text(encoding="utf-8")
    assert "if (!this.pendingApproval || !this.approvalBlockEl) return;" in body, (
        "approval keydown must early-return unless a pending approval is up"
    )
    assert "e.key.toLowerCase()" in body, "the y/n/a shortcut branch"
    assert ".conv-feedback" in body, (
        "the feedback field uses the converged .conv-feedback, not the retired "
        ".ts-approval-feedback"
    )
