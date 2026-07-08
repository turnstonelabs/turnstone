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
_COMPOSER = _ROOT / "turnstone/shared_static/composer.js"
_AUTH = _ROOT / "turnstone/shared_static/auth.js"
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


# -- Shared-workstream cross-user send gate -----------------------------------
#
# The UX complement to the server-side CrossUserInterjectionError (a 409): while
# another participant's turn is in flight, this viewer's send button is disabled
# so they can't interject under the initiator's credentials / be misattributed.
# The wiring spans three modules; these string-presence guards catch the silent
# one-line regression the way the rest of this file does (no JS test framework).


def test_composer_exposes_hard_send_block() -> None:
    """The composer has an independent hard-block axis, reconciled with busy,
    so a caller can disable send even in queueWhileBusy (queue) mode."""
    body = _COMPOSER.read_text(encoding="utf-8")
    assert "Composer.prototype.setSendBlocked = function" in body
    assert "Composer.prototype._reconcileDisabled = function" in body
    assert "this._sendBlocked = false;" in body
    # setBusy must route the disabled write through the reconciler (not clobber
    # the block with a direct sendBtn.disabled assignment).
    stripped = _strip_comments(body)
    setbusy = stripped.index("Composer.prototype.setBusy = function")
    setbusy_end = stripped.index("Composer.prototype._reconcileDisabled")
    assert "this._reconcileDisabled();" in stripped[setbusy:setbusy_end]
    assert "this.sendBtn.disabled =" not in stripped[setbusy:setbusy_end], (
        "setBusy must not write sendBtn.disabled directly — reconcile owns it"
    )


def test_auth_retains_user_id_for_gate() -> None:
    """whoami's opaque user_id is retained (separately from the display
    username) so the pane can compare it against the acting-user id."""
    body = _AUTH.read_text(encoding="utf-8")
    assert 'sessionStorage.setItem("ts.user_id", data.user_id);' in body
    assert 'sessionStorage.removeItem("ts.user_id");' in body


def test_pane_gates_send_on_cross_user_busy() -> None:
    """The pane tracks the acting user from state_change, compares it against
    the viewer's own id, and blocks send while another participant is busy."""
    body = _INTERACTIVE.read_text(encoding="utf-8")
    assert "_reconcileSendBlock() {" in body
    # tracks the acting user from the state_change event...
    assert "this._actingUserId = evt.acting_user_id;" in body
    assert "this._actingUserId = null;" in body  # cleared when the turn settles
    # ...compares against the viewer's own id from /whoami...
    assert 'sessionStorage.getItem("ts.user_id")' in body
    assert "this._actingUserId !== me" in body
    # ...and drives the composer's hard block, re-run on every busy edge.
    assert "this.composer.setSendBlocked(" in body
    stripped = _strip_comments(body)
    setbusy = stripped.index("setBusy(b) {")
    assert "this._reconcileSendBlock();" in stripped[setbusy : setbusy + 600]


def test_pane_handles_cross_user_409() -> None:
    """The reactive fallback: a 409 (button not yet disabled) surfaces a clean
    message, not the generic 'Connection error' catch."""
    body = _INTERACTIVE.read_text(encoding="utf-8")
    assert "r.status === 409" in body
    assert 'status: "cross_user_interjection"' in body
    assert 'data.status === "cross_user_interjection"' in body


def test_sync_approval_state_prunes_orphan_cycles() -> None:
    """``_syncApprovalState`` prunes cycles whose block elements are no longer
    in the living DOM (``.isConnected === false``).  This covers the rare case
    where an ``approve_request`` event is processed between a DOM wipe
    (``clear_ui`` / ``replay_truncated`` / ``replaceChildren``) and the
    refetch-restore — the cycle card lives in a detached subtree, the matching
    ``approval_resolved`` never arrives, and the send button stays disabled
    forever without this guard.  The pin guards against a future refactor that
    drops the orphan prune but doesn't otherwise break ``_syncApprovalState``."""
    body = _INTERACTIVE.read_text(encoding="utf-8")
    fn_start = body.index("_syncApprovalState() {")
    assert "entry.blockEls && !entry.blockEls.some((el) => el.isConnected)" in body, (
        "orphan pruning must check .isConnected on block elements"
    )
    tail = body[fn_start : body.index("_oldestCycleId()", fn_start)]
    assert "this.approvalCycles.delete(cid);" in tail, (
        "orphan pruning must delete the cycle from the Map"
    )


# ---------------------------------------------------------------------------
# SSE overflow recovery + close-on-hide (fast-stream corruption fixes)
# ---------------------------------------------------------------------------


def test_stream_overflow_case_counts_and_rate_limits() -> None:
    """The server closes an overflowed stream after an id-less
    ``stream_overflow`` frame; the pane must count it (field
    instrumentation for the drop-vs-render-wedge diagnosis) and route it
    through the reconnect limiter so a persistently slow consumer trips
    the degraded catch-up instead of churning reconnect/replay cycles."""
    body = _INTERACTIVE.read_text(encoding="utf-8")
    assert 'case "stream_overflow":' in body
    assert "this._noteStreamOverflow();" in body
    assert "_streamHealth = { overflows: 0, renderThrows: 0, malformedFrames: 0 }" in body
    # Both wedge-class catch sites increment the render-throw counter,
    # and the malformed-frame drop counts too — the C-OVERDETERMINED
    # instrumentation that tells drops apart from wedges in the field.
    assert body.count("this._streamHealth.renderThrows += 1;") == 2
    assert "this._streamHealth.malformedFrames += 1;" in body
    assert "this._streamHealth.overflows += 1;" in body


def test_degraded_catchup_stops_live_stream_and_retries() -> None:
    """Degraded catch-up contract: close the stream FIRST (which also
    clears any earlier degraded timer — disconnectSSE owns that), show a
    plain-language status, then arm the retry timer with a doubling
    cooldown.  The retry must defer to the show edge when the tab is
    hidden (reopening into a throttled tab would overflow again)."""
    body = _INTERACTIVE.read_text(encoding="utf-8")
    m = re.search(r"_enterDegradedCatchup\(\)\s*\{(.*?)\n  \}", body, re.S)
    assert m is not None, "_enterDegradedCatchup method not found"
    method = m.group(1)
    # Order matters: disconnect before arming the timer, or the fresh
    # timer would be cancelled by its own disconnect.
    assert method.index("this.disconnectSSE()") < method.index("this._degradedTimer = setTimeout")
    assert "Connection is slow" in method, "degraded state must use plain language"
    assert "DEGRADED_COOLDOWN_MAX_MS" in method
    assert "document.hidden" in method
    # disconnectSSE owns the timer teardown (ws-switch / giveUp / destroy
    # all supersede a pending degraded retry through it).
    dis = re.search(r"disconnectSSE\(\)\s*\{(.*?)\n  \}", body, re.S)
    assert dis is not None
    assert "clearTimeout(this._degradedTimer)" in dis.group(1)


def test_visibilitychange_closes_on_hide_reconnects_on_show() -> None:
    """Close-on-hide / replay-on-show: a hidden tab's throttled drain is
    the likeliest slow consumer behind server-side overflow (the old
    "PR-G closes those connections on hide" comment described a handler
    that never existed).  The pane installs one visibilitychange
    listener, marks ITS OWN hide-closes via ``_hiddenDisconnect`` so a
    show edge never resurrects a deliberately-closed stream, and the
    factory's destroy removes the listener (it strongly references the
    pane)."""
    body = _INTERACTIVE.read_text(encoding="utf-8")
    assert 'document.addEventListener("visibilitychange", this._visHandler);' in body
    assert 'document.removeEventListener("visibilitychange", this._visHandler);' in body
    vis = re.search(r"_onVisibilityChange\(\)\s*\{(.*?)\n  \}", body, re.S)
    assert vis is not None, "_onVisibilityChange method not found"
    method = vis.group(1)
    assert "this.disconnectSSE();" in method
    assert "this._hiddenDisconnect = true;" in method
    assert "this.connectSSE(this.wsId);" in method
    # Reconnect only consumes OUR hide-close marker.
    assert "else if (this._hiddenDisconnect)" in method
    # Teardown: the factory controller removes the listener on destroy.
    assert "pane._removeVisibilityHandler();" in body
    # The streaming buffers survive a hide-close: disconnectSSE stays
    # transport-only (no contentBuffer wipe) so the visible tail is
    # intact when the tab returns.
    dis = re.search(r"disconnectSSE\(\)\s*\{(.*?)\n  \}", body, re.S)
    assert dis is not None
    assert "contentBuffer" not in dis.group(1)


def test_no_global_sse_gap_detector() -> None:
    """Live event ids are NOT strictly monotonic across concurrent
    tool+content emit (the fan-out runs outside the listeners lock), so
    a naive ``id !== lastEventId + 1`` gap check would false-positive.
    Recovery is server-signalled (``stream_overflow``) + reconnect
    replay instead.  This tripwire pins the absence of the naive
    arithmetic — if gap detection is ever added, it must be scoped to
    the content stream only (content-vs-content never reorders)."""
    code = _strip_comments(_INTERACTIVE.read_text(encoding="utf-8"))
    assert not re.search(r"_lastEventId\s*[+\-]\s*1", code), (
        "found lastEventId +/- 1 arithmetic — a global gap detector "
        "false-positives on legal concurrent tool/content id inversion"
    )


def test_overflow_helpers_extracted_to_shared_module() -> None:
    """The storm-guard constants + the two pure helpers were extracted to the
    shared ``sse_overflow.js`` module (its own runtime probes live in
    ``test_sse_overflow_js.py``) so the interactive and coordinator panes can't
    drift.  Pin that the pane IMPORTS them rather than re-declaring a local
    copy: a stray local ``function overflowWindowTripped`` / ``const
    OVERFLOW_TRIP_COUNT`` would silently fork the trip math again."""
    body = _INTERACTIVE.read_text(encoding="utf-8")
    m = re.search(
        r"import \{([^}]*)\} from \"\./sse_overflow\.js\";",
        body,
        re.S,
    )
    assert m is not None, "interactive pane must import the shared overflow helpers"
    imported = m.group(1)
    for name in (
        "OVERFLOW_TRIP_COUNT",
        "OVERFLOW_TRIP_WINDOW_MS",
        "DEGRADED_COOLDOWN_BASE_MS",
        "DEGRADED_COOLDOWN_MAX_MS",
        "DEGRADED_COOLDOWN_RESET_MS",
        "overflowWindowTripped",
        "degradedCooldownStep",
    ):
        assert name in imported, f"{name} must be imported from sse_overflow.js"
    # No local fork of the extracted definitions.
    assert not re.search(r"^function overflowWindowTripped\(", body, re.M), (
        "overflowWindowTripped must be imported, not re-declared locally"
    )
    assert not re.search(r"^function degradedCooldownStep\(", body, re.M), (
        "degradedCooldownStep must be imported, not re-declared locally"
    )
    assert not re.search(r"^const OVERFLOW_TRIP_COUNT\s*=", body, re.M), (
        "the trip constants must be imported, not re-declared locally"
    )


def test_note_stream_overflow_does_not_reset_cooldown() -> None:
    """The exact finding [0] bug shape must not regress: _noteStreamOverflow
    only counts + trips; it must NOT touch _degradedCooldownMs (the reset
    that defeated the ladder lived here).  The ladder decision lives solely
    in _enterDegradedCatchup, keyed off _lastDegradedAt via
    degradedCooldownStep."""
    body = _INTERACTIVE.read_text(encoding="utf-8")
    note = re.search(r"_noteStreamOverflow\(\)\s*\{(.*?)\n  \}", body, re.S)
    assert note is not None, "_noteStreamOverflow not found"
    assert "_degradedCooldownMs" not in note.group(1), (
        "_noteStreamOverflow must not write _degradedCooldownMs — that reset "
        "was the bug that stopped the ladder escalating"
    )
    enter = re.search(r"_enterDegradedCatchup\(\)\s*\{(.*?)\n  \}", body, re.S)
    assert enter is not None
    assert "degradedCooldownStep(" in enter.group(1)
    assert "this._lastDegradedAt = now" in enter.group(1)


def test_recover_beat_defers_reconnect_when_tab_hidden() -> None:
    """Review round-2 finding [1]: the factory's transient-error recovery
    beat (recoverTimer) must NOT reopen an EventSource into a hidden tab —
    that re-creates the throttled slow-consumer overflow that close-on-hide
    exists to prevent.  It guards on document.hidden and defers to the
    visibilitychange show edge (marking _hiddenDisconnect)."""
    body = _INTERACTIVE.read_text(encoding="utf-8")
    beat = re.search(r"recoverTimer = setTimeout\(\(\) => \{(.*?)\n      \}, 5000\);", body, re.S)
    assert beat is not None, "recoverTimer setTimeout body not found"
    b = beat.group(1)
    assert "document.hidden" in b, "recovery beat must guard on document.hidden"
    assert "pane._hiddenDisconnect = true" in b, (
        "recovery beat must defer to the show edge when hidden"
    )
    # The hidden guard must precede the reconnect (connectSSE) so it can't fall
    # through to reopening the stream.
    assert b.index("document.hidden") < b.index("pane.connectSSE(pane.wsId)")


def test_giveup_removes_visibility_handler() -> None:
    """Review round-2 finding [3]: giveUp() (markDead) must detach the
    visibility handler and clear _hiddenDisconnect, or a tab hidden before
    the give-up resurrects the dead controller's stream on return (the show
    edge would connectSSE the closed ws and 404-reconnect it forever)."""
    body = _INTERACTIVE.read_text(encoding="utf-8")
    give = re.search(r"const giveUp = function \(\) \{(.*?)\n  \};", body, re.S)
    assert give is not None, "giveUp function body not found"
    g = give.group(1)
    assert "pane._removeVisibilityHandler();" in g, (
        "giveUp must remove the visibility handler so a show edge can't resurrect a dead controller"
    )
    # _removeVisibilityHandler also clears _hiddenDisconnect (pinned in its body).
    rvh = re.search(r"_removeVisibilityHandler\(\)\s*\{(.*?)\n  \}", body, re.S)
    assert rvh is not None
    assert "this._hiddenDisconnect = false" in rvh.group(1)


def test_connectsse_defers_open_when_tab_hidden() -> None:
    """PR #805 review (Copilot + R3): connectSSE is the single connect
    chokepoint and must not open an EventSource into a hidden tab.  The
    fresh-connect path (_loadHistoryThenConnect) has no timer guard, so a
    first load in a background tab would otherwise open a throttled stream —
    the slow-consumer overflow this PR exists to prevent.  The guard sits
    AFTER the visibilitychange-handler install (so the show edge can
    reconnect) and AFTER the wsId assignment (so it targets the right ws),
    and BEFORE `new EventSource` (so nothing opens)."""
    body = _INTERACTIVE.read_text(encoding="utf-8")
    start = body.index("connectSSE(wsId) {")
    open_at = body.index("new EventSource(evtUrl)", start)
    head = body[start:open_at]  # connectSSE up to the EventSource open
    assert "if (document.hidden) {" in head, (
        "connectSSE must guard on document.hidden BEFORE opening the stream"
    )
    assert "this._hiddenDisconnect = true;" in head, (
        "the deferred connect must mark _hiddenDisconnect so the show edge reconnects"
    )
    assert head.index("this.wsId = wsId;") < head.index("if (document.hidden) {")
    assert head.index('addEventListener("visibilitychange"') < head.index("if (document.hidden) {")
