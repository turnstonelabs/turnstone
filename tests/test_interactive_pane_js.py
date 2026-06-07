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


def test_interactive_is_esm_with_window_bridge() -> None:
    """The module is a real ES module (the first legacy pane lifted into one):
    it ``export``s the factory for the console shell's ``import`` AND publishes
    ``window.InteractivePane`` / ``window.createInteractivePane`` so the still
    classic standalone ``app.js`` can read the class.  Both halves are
    load-bearing — drop the export and the console shell can't import it; drop
    the window bridge and the standalone shell's ``createPane`` goes undefined."""
    body = _INTERACTIVE.read_text(encoding="utf-8")
    assert "export { Pane as InteractivePane, createInteractivePane };" in body
    assert "window.InteractivePane = Pane;" in body
    assert "window.createInteractivePane = createInteractivePane;" in body
    # And the standalone HTML must load it as a module (not a classic script).
    html = _UI_INDEX.read_text(encoding="utf-8")
    assert '<script type="module" src="/shared/interactive.js"></script>' in html, (
        "ui/static/index.html must load interactive.js as an ES module — a "
        "classic <script src> would choke on the top-level export."
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


def test_standalone_shell_constructs_via_window_bridge() -> None:
    """The standalone ``app.js`` shell builds panes through
    ``window.InteractivePane`` with its ``STANDALONE_HOST`` adapter, and keeps
    the focused-pane stream-error recovery (``refetchWorkstreamsAndReassign``)
    that moved out of the class."""
    app = _APP.read_text(encoding="utf-8")
    assert "new window.InteractivePane(wsId, { host: STANDALONE_HOST })" in app
    assert "const STANDALONE_HOST = {" in app
    assert "function refetchWorkstreamsAndReassign(focusedPane) {" in app
    # The host adapter must implement every seam the Pane calls.
    for method in (
        "getWsName(wsId)",
        "isFocused(pane)",
        "onStreamError(pane)",
        "warningTarget()",
        "onConsentDetected(server)",
    ):
        assert method in app, f"STANDALONE_HOST missing {method!r}"
    # The class itself no longer carries the split-pane reassign method.
    inter = _INTERACTIVE.read_text(encoding="utf-8")
    assert "_refetchWorkstreamsAndReassign" not in inter
