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
    """The constructor takes the ``(wsId, opts)`` seam: a transport ``base``
    (the node-proxy prefix) and a ``host`` adapter for the few things only the
    surrounding shell knows.  The old ``embedded`` flag is gone — every pane is
    L-shell-hosted since the step-6 fork collapse."""
    body = _INTERACTIVE.read_text(encoding="utf-8")
    assert "constructor(wsId, opts) {" in body
    for field in (
        "this._base = opts.base",
        "this._host = opts.host || INTERACTIVE_DEFAULT_HOST",
    ):
        assert field in body, f"missing constructor seam: {field!r}"
    assert "opts.embedded" not in body, (
        "the embedded flag is retired — every pane is L-shell-hosted."
    )


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


def test_split_pane_chrome_is_retired() -> None:
    """The standalone split-pane chrome is GONE, not gated: no pane header
    (name / persona / state live in the tab + rail; the conversation owns the
    full pane height), no focus tracking, no split/close buttons.  The dead
    ``!this._embedded`` branches referenced shell globals (setFocusedPane,
    splitPane, splitRoot…) that no longer exist anywhere — reaching them was a
    guaranteed ReferenceError, so their removal is a bugfix too."""
    body = _INTERACTIVE.read_text(encoding="utf-8")
    assert "_embedded" not in body, "the embedded gate is retired (always-on)"
    assert 'className = "pane pane--embedded"' in body, (
        "the pane root must carry pane--embedded unconditionally — "
        "interactive.css scopes the slim-chrome layout to it"
    )
    for gone in (
        "setFocusedPane",
        "showPaneContextMenu",
        "splitPane(",
        "splitRoot",
        "this.headerEl",
        '"pane-header"',
        '"pane-action-btn"',
        "updateWsName",
    ):
        assert gone not in body, f"retired split-pane symbol {gone!r} resurfaced"
    # The persona tag stays gone (the rail's INT/COORD vocabulary shows it).
    assert '"pane-persona-tag"' not in body
    assert '"INTERACTIVE"' not in body


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
        "this._host.isFocused(this)",
        "this._host.onStreamError(this)",
        "this._host.warningTarget(this)",
        "this._host.onConsentDetected(",
    ):
        assert call in body, f"missing host seam call {call!r}"
    assert "getWsName" not in body, (
        "getWsName left the host seam with the pane header — the tab + rail "
        "own the workstream name now."
    )
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


def test_media_playback_lifted_and_pane_owned() -> None:
    """The media Play affordance is rendered by the pane (buildPlayButton /
    buildMediaEmbed), so its activation must live in the pane too — the old
    standalone wired a DOCUMENT-level click/keydown listener in app.js, which
    the console host never loaded (so the button was dead in console-hosted
    panes).  The fix mirrors the approval-keydown pattern: a pane-owned listener
    on this.el, root-scoped via closest(".media-play-btn").  Pin both the
    lifted helpers and the pane wiring so the document-level regression can't
    silently come back."""
    body = _INTERACTIVE.read_text(encoding="utf-8")
    # The lifted activation machinery now lives in the shared module.
    for fn in (
        "function _loadHls(",
        "function _isHlsUrl(",
        "function _activatePlayer(",
        "function activateMediaPlayButton(",
    ):
        assert fn in body, f"media player helper must be lifted into the pane: {fn}"
    # The HLS vendor is fetched by absolute /shared/ URL (resolves in BOTH the
    # standalone server and the console, where /shared is mounted at the root).
    assert 'script.src = "/shared/hls-1.6.16/hls.min.js";' in body
    # Pane-owned + root-scoped — NOT a document-level delegated listener.
    assert 'this.el.addEventListener("click"' in body, (
        "media play must be wired on this.el (pane-owned), not document"
    )
    assert 'e.target.closest(".media-play-btn")' in body, (
        "the play handler must be root-scoped via closest, not a document-wide id"
    )
    assert "activateMediaPlayButton(btn)" in body
    collapsed = _strip_comments(body)
    assert 'document.addEventListener("click"' not in collapsed, (
        "the pane must not register a document-level click delegate — that is "
        "the standalone regression that left console panes dead"
    )


def test_controller_terminal_dead_state() -> None:
    """Lifecycle round 2: the console controller must STOP reconnect-polling a
    session that is gone (closed / evicted / node restarted) — three consecutive
    CLOSED recovery beats → give up: stream closed, status bar terminal,
    ``opts.onDead()`` fired once.  A successful stream open resets the counter
    (the new host.onStreamOpen seam).  ``isDead()`` / ``markDead()`` / ``base``
    are the shell's revive surface; a dead controller also ignores the login
    re-arm (recovery may need a DIFFERENT node — the shell's revive owns it)."""
    body = _INTERACTIVE.read_text(encoding="utf-8")
    # The give-up ladder.
    assert "let dead = false;" in body and "let failCount = 0;" in body
    assert "const giveUp = function () {" in body
    assert "failCount += 1;" in body and "if (failCount >= 3) giveUp();" in body
    assert 'pane._sbTokens.textContent = "Disconnected"' in body, (
        "the terminal state must be worded distinctly from the transient Reconnecting…"
    )
    assert "opts.onDead" in body, "the shell must hear about the give-up"
    # The reset seam: Pane.connectSSE onopen → host.onStreamOpen → failCount = 0.
    assert "this._host.onStreamOpen(this)" in body
    assert "onStreamOpen() {}" in body, "the default host must carry the no-op"
    # The shell-facing surface.
    assert "isDead()" in body and "markDead: giveUp," in body
    assert "base: base," in body, "the controller must expose its transport base"
    # Dead controllers don't reconnect on re-auth.
    assert "if (connected && !dead) pane._loadHistoryThenConnect(wsId);" in body


def test_stream_pipeline_is_wedge_proof() -> None:
    """Long-session hardening (perf audit P0): the SSE pipeline must not be
    able to permanently wedge the pane.  ``onmessage`` guards BOTH the
    ``JSON.parse`` and the ``handleEvent`` dispatch (an exception escaping it
    doesn't close the EventSource, so an unguarded throw left the streaming
    refs poisoned for the rest of the session), and ``stream_end`` resets the
    segment refs BEFORE the finalize render, with a plain-text fallback —
    with the old order a finalize throw skipped the clears and every later
    delta painted into the dead segment."""
    body = _INTERACTIVE.read_text(encoding="utf-8")
    assert "dropping malformed SSE frame" in body
    assert "handleEvent failed for" in body
    case = body.index('case "stream_end"')
    seg = body[case : body.index("break;", case)]
    clears = seg.index("this.currentAssistantBodyEl = null;")
    finalize = seg.index("streamingRenderFinalize(")
    assert clears < finalize, (
        "stream_end must clear segment refs BEFORE finalize — the old "
        "finalize-first order wedged all later assistant output on a throw."
    )
    assert "doneBodyEl.textContent = doneBuffer;" in seg


def test_rebuild_quiesces_live_events_and_releases_agent_tracking() -> None:
    """clear_ui / replay_truncated re-render race (perf audit P0): live SSE
    events painted between the history snapshot and ``replaceChildren()``
    were wiped with no redelivery, and streaming refs kept pointing at
    detached nodes.  Pinned: the quiesce queue sits on the handleEvent hot
    path, both re-render triggers arm it, ``replayHistory`` resets the
    streaming refs and clears the agent-card/orphan maps (the detached-DOM
    retention leak), and the mid-stream guard covers the reasoning bubble."""
    body = _INTERACTIVE.read_text(encoding="utf-8")
    assert "this._replayQueue.events.push(evt);" in body
    assert body.count("this._beginReplayQuiesce(") >= 2, (
        "both clear_ui and replay_truncated must arm the quiesce"
    )
    assert "!this.currentAssistantEl && !this.currentReasoningEl" in body
    replay = body.index("replayHistory(messages) {")
    seg = body[replay : replay + 1600]
    for line in (
        "this._resetStreamingRefs();",
        "this._clearAgentTracking();",
    ):
        assert line in seg, f"replayHistory must reset: {line!r}"
    assert "this._agentCards.clear();" in body
    # Review-hardened lifecycle: the card entry SURVIVES the terminal
    # tool_result (a late child event finding no Map entry would rebuild a
    # duplicate empty card beside the finished one), and transport-only
    # reconnects preserve the maps + any armed quiesce queue — clearing them
    # in disconnectSSE duplicated cards and dropped buffered orphan steps on
    # every transient stream blip.  Full-reload cleanup lives in
    # _loadHistoryThenConnect; terminal cleanup in the factory's destroy().
    assert "this._agentCards.delete(callId);" not in body
    disc = body.index("disconnectSSE() {")
    disc_seg = body[disc : body.index("_loadHistoryThenConnect(wsId) {", disc)]
    assert "this._clearAgentTracking();" not in disc_seg
    assert "this._replayQueue = null;" not in disc_seg
    load = body.index("_loadHistoryThenConnect(wsId) {")
    load_seg = body[load : load + 2200]
    assert "this._clearAgentTracking();" in load_seg
    assert "this._replayQueue = null;" in load_seg
    # A mid-stream replay_truncated DEFERS the re-sync (flag consumed on the
    # idle edge) instead of dropping it — skipping left the lost-event gap
    # unrepaired for the rest of the session.
    assert "this._pendingTruncatedResync = true;" in body
    # The refetch FAILURE branch resets streaming refs too — it never reaches
    # replayHistory, and stale refs there streamed the retried generation's
    # first segment into a detached bubble.
    fail = body.index("Failure path never reaches replayHistory")
    assert "this._resetStreamingRefs();" in body[fail : fail + 400], (
        "the refetch failure branch must reset streaming refs"
    )


def test_per_token_hot_path_avoids_container_scans() -> None:
    """P1 (perf audit): per-token work must stay O(1) in transcript length.
    The thinking indicator is an instance ref (the class-selector miss walked
    the whole transcript on EVERY content/reasoning delta); near-bottom state
    comes from the passive scroll listener instead of a forced-layout
    geometry read per event; the scroll pin is rAF-coalesced; per-tool
    row/stream lookups resolve through the self-healing caches."""
    body = _INTERACTIVE.read_text(encoding="utf-8")
    stripped = _strip_comments(body)
    assert 'querySelector(".thinking-indicator")' not in stripped, (
        "thinking indicator must use the instance ref, not a container scan"
    )
    assert "this._thinkingEl" in body
    near = body.index("isNearBottom() {")
    assert "return this._nearBottom;" in body[near : near + 700]
    assert "passive: true" in body
    # The rAF pin re-checks the flag AT FIRE TIME (a user scroll landing in
    # the schedule→rAF window must win over a stale pin), with force
    # requests latched across the coalescing window; resizes re-derive the
    # flag via ResizeObserver since they move the bottom without a scroll.
    assert "this._scrollPinForce = false;" in body
    assert "ResizeObserver" in body
    for helper in ("_toolRow(callId) {", "_streamEl(callId) {"):
        assert helper in body, f"missing lookup-cache helper: {helper!r}"
