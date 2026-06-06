"""Tests for the /coordinator/{ws_id} HTML page handler.

The handler serves the shared template with the ws_id injected as a
``data-ws-id`` attribute.  It does NOT enforce auth on the page itself —
auth gating happens on the API endpoints the page calls (an unauthenticated
visitor lands on the page but all API calls fail).
"""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from turnstone.console.server import coordinator_page


@pytest.fixture
def client():
    app = Starlette(routes=[Route("/coordinator/{ws_id}", coordinator_page, methods=["GET"])])
    return TestClient(app)


def test_valid_ws_id_injects_data_attr(client):
    ws_id = "a" * 32
    resp = client.get(f"/coordinator/{ws_id}")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    body = resp.text
    # ws_id is injected into the html data-ws-id attribute.
    assert f'data-ws-id="{ws_id}"' in body
    # Template placeholder is fully substituted.
    assert "{{WS_ID}}" not in body
    # Sanity: the shared static imports are wired.
    assert "/shared/base.css" in body
    assert "/static/coordinator/coordinator.js" in body


def test_non_hex_ws_id_returns_400(client):
    """Only hex chars are allowed to avoid HTML injection."""
    resp = client.get("/coordinator/not-hex-chars-here")
    assert resp.status_code == 400


def test_ws_id_too_long_returns_400(client):
    resp = client.get("/coordinator/" + "a" * 65)
    assert resp.status_code == 400


def test_uppercase_hex_rejected(client):
    # Our ws_ids are lowercase hex; reject mixed/upper to avoid surprises.
    resp = client.get("/coordinator/" + "A" * 32)
    assert resp.status_code == 400


def test_coordinator_js_exposes_inline_approval_helpers():
    """Smoke guard for two layers of the coord chat frontend: the
    children-tree inline approve/deny block (the original Chunk 3
    landing) and the PR #447 tool-batch construct that replaced the
    pinned approval dock for the coord-self surface.  Both layers'
    helper symbols must remain reachable in the served JS so a
    refactor that accidentally renames or removes them surfaces here
    instead of in production where the affected gates silently stop
    rendering.  Asserts string presence only — no DOM parsing —
    since coord.js has no JS test framework today (per the plan's
    testing notes)."""
    from pathlib import Path

    coord_js = Path(__file__).resolve().parent.parent / (
        "turnstone/console/static/coordinator/coordinator.js"
    )
    body = coord_js.read_text(encoding="utf-8")
    # Approval-block rendering helpers
    assert "function renderApprovalBlock" in body
    assert "function _maxSeverityItem" in body
    assert "function _renderSubItem" in body
    # The submit + 409 race-handling path
    assert "function submitChildApproval" in body or "submitChildApproval(" in body
    # The shared approve POST helper (parameterized for child ws_ids)
    assert "function approveWorkstream" in body or "approveWorkstream(" in body
    # The 409 stale-call_id retry path uses invalidateLiveBadge +
    # scheduleLiveFetch (Stage 3 cleanup removed the urgent flag —
    # cache invalidation makes the TTL gate fall through naturally).
    assert "invalidateLiveBadge(targetWsId)" in body
    # Server-side payload field — drift here means the JS reads stale keys
    assert "pending_approval_detail" in body
    # Reconnect parity (chunk 4): the SSE re-open handler must drop
    # non-permanent entries from the live-badge cache so a stale
    # pending_approval_detail (left from before the disconnect)
    # can't render zombie approve/deny buttons on a row whose
    # approval was resolved during the gap. The implementation
    # iterates the cache and deletes only !permanent entries —
    # asserting the literal helper call keeps a refactor back to
    # _liveBadgeCacheClear() (which would re-pay 403s on every
    # reconnect for denied ids) from sneaking in.
    assert "_liveBadgeCacheDelete" in body
    # Edge-case matrix sentinel labels — POLICY-BLOCKED renders when
    # an item has error set + needs_approval=False (server-side
    # tool policy already blocked the call); "(judge unavailable)"
    # renders when no verdict (judge or heuristic) and no
    # judge_pending. Refactors that drop either branch silently
    # regress to a buttoned approve UI on the wrong state.
    assert "POLICY-BLOCKED" in body
    assert "judge unavailable" in body
    # Critical-risk handling — bug-1 was that risk_level='critical'
    # rendered as low because RISK_SEVERITY only mapped 'crit'.
    # Both aliases must remain in the table so a 'critical' verdict
    # ranks at 3 and renders with the .risk.crit pill.
    assert "critical: 3" in body
    # Child approves must round-trip through the routing proxy at
    # /v1/api/route/workstreams/{ws_id}/approve — the bare
    # /v1/api/workstreams/.../approve path only works for the
    # coord-self ws_id (the coord lives on the console process).
    # Children live on cluster nodes and 404 without the prefix.
    assert "/v1/api/route/workstreams/" in body
    # Late-arriving LLM judge verdicts — Stage 3 Step 5 promoted
    # ``intent_verdict`` and ``approval_resolved`` to first-class
    # cluster-bus event types, so the coord adapter dispatches them
    # as ``child_ws_intent_verdict`` / ``child_ws_approval_resolved``
    # on the parent's SSE stream. The browser handlers write
    # directly to liveBadgeCache (bypassing scheduleLiveFetch's
    # visibility gate cleanly) so off-screen rows pick up verdicts
    # without polling. Replaced the old ``_judgePollTick`` 90-second
    # global poll loop and its visibility-gate-bypass workaround.
    assert "handleChildIntentVerdict" in body
    assert "handleChildApprovalResolved" in body
    assert "child_ws_intent_verdict" in body
    assert "child_ws_approval_resolved" in body
    # Reload parity for the coord-self approval gate: init() must
    # consume the authoritative GET /workstreams snapshot's
    # pending_approval_detail so a freshly opened tab can render
    # Approve/Deny before SSE replay arrives.
    assert "wsSnapshot.pending_approval_detail" in body
    assert "appendToolBatch(pendingDetail.items" in body
    # Tool-batch construct (PR #447) — the inline replacement for the
    # pinned approval-dock pattern.  These helpers carry the
    # state-machine that pairs each tool call with its result and
    # embeds the approval flow.  Refactors that rename or drop them
    # silently regress the entire coord-self approval surface — the
    # most novel and risky behavior in the PR.
    assert "function appendToolBatch" in body
    assert "function _morphBatchResolved" in body
    assert "function _resolveBatchAction" in body
    assert "function _refreshBatchTier" in body
    assert "function _refreshRowStatus" in body
    # State modifiers driven by the upgrade-in-place path
    # (--running orphan promoted to --pending or --auto when SSE
    # arrives with the authoritative shape).  Both class names must
    # remain reachable from JS — dropping either breaks the reload
    # state machine that PR #447's review pass surfaced.
    assert "coord-tool-batch--running" in body
    assert "coord-tool-batch--pending" in body
    # History replay's outcome classifier — denied / errored tool
    # turns must render with the correct batch state on reload, not
    # the contradictory "✓ approved" pill that pre-fix showed for
    # any prior denial.  bug-1 / bug-3 from the second /review pass.
    # Post wire-shape unification the deny/error classification moved
    # server-side into ``project_history_messages``; coord reads the
    # derived ``m.denied`` / ``m.is_error`` flags (pin the live read,
    # not the comment prose the old content-prefix sniffing left behind).
    assert "m.denied" in body
    assert "m.is_error" in body
    assert "callOutcomes" in body
    # User-message attachment pills — both live send (coordSend) and
    # history replay route through appendUserMessageWithAttachments.
    # Renaming or dropping the helper would silently regress the
    # attachment affordance to the pre-fix plain-text bubble, which
    # would only surface in manual testing of an attached-file flow.
    # The CSS class is the visual anchor (coordinator.css) — keeping
    # both literals in the smoke layer covers JS↔CSS drift in either
    # direction.
    assert "function appendUserMessageWithAttachments" in body
    assert "msg-user-attach" in body
    # PR #487 — whitespace-only assistant content (Qwen3 with vLLM
    # ``--reasoning-parser`` strips ``<think>…</think>`` and emits only
    # ``"\n\n"`` as content before a tool call) must be skipped on
    # history replay or the empty ``.msg.assistant`` card surfaces as
    # a phantom row.  The literal substring ``content.trim()`` is the
    # single-line guard the rendering branch uses; a refactor that
    # drops the trim() (e.g. simplifies to ``if (!content)``) silently
    # regresses the phantom-card fix on the multi-node coord path.
    # Mirrors ``test_app_js.py``'s same-shape pin on ``app.js``.
    assert "content.trim()" in body
    # PR #487 — coord history replay must render the assistant content
    # card BEFORE the tool batch, not after, so DOM order matches the
    # chronological order the model emitted (text → dispatch → results).
    # Pre-fix the tool_calls branch sat at the role-agnostic top of the
    # loop and rendered ahead of the assistant text that announced the
    # batch, putting parallel fan-outs visually above their narrating
    # message.  The fix hoisted the synthesis into ``renderAssistantToolBatch``
    # called from inside the assistant branch AFTER the content card —
    # asserting the helper name lets a refactor that re-inlines or
    # renames it surface here instead of via manual reload testing.
    assert "function renderAssistantToolBatch" in body
    assert "renderAssistantToolBatch(m)" in body


def test_coordinator_js_handle_child_state_no_longer_reads_sse_pending_approval_detail():
    """Stage 3 cleanup — ``pending_approval_detail`` is no longer
    piggybacked on child_ws_state events. Approval items now arrive
    via bulk fetch on the activity_state="approval" transition;
    verdicts via the explicit ``child_ws_intent_verdict`` event class;
    resolution via ``child_ws_approval_resolved``. A refactor that
    re-introduces the piggyback would silently re-open the
    duplicate-path race the dedicated event classes were added to
    eliminate.

    Structural assertions (regex against multi-line source) — symbol-
    presence alone wouldn't catch a guard that keeps the names but
    inverts the comparison or drops the ``prev.live`` check. This
    codebase has no JS test framework, so locking the guard's shape
    here is the next-best thing to a behavioral test."""
    import re
    from pathlib import Path

    coord_js = Path(__file__).resolve().parent.parent / (
        "turnstone/console/static/coordinator/coordinator.js"
    )
    body = coord_js.read_text(encoding="utf-8")

    # The piggyback read is gone from handleChildState. (The string
    # may still appear elsewhere — e.g. handleChildIntentVerdict
    # reading from cache, or comments — but never as ``ev.pending_approval_detail``.)
    assert "ev.pending_approval_detail" not in body
    # The pre-fix urgent-fetch on activity_state transitions is gone.
    assert "enteredApproval" not in body
    assert "leftApproval" not in body
    # ``pendingApproval`` flag derivation must check BOTH state and
    # activity_state. The worker thread can fire the state transition
    # to "attention" before approve_tools updates activity_state, so
    # checking only activity_state misses children that legitimately
    # need approval. Pin the disjunction so the regression doesn't
    # silently re-introduce.
    assert re.search(
        r'existing\.state\s*===\s*"attention"\s*\|\|\s*'
        r'existing\.activity_state\s*===\s*"approval"',
        body,
    ), (
        "handleChildState must derive pendingApproval from "
        "(state==='attention' || activity_state==='approval')"
    )

    # SSE-authoritative window constant is defined and used.
    assert re.search(r"\bconst\s+SSE_AUTHORITATIVE_MS\s*=\s*\d+", body), (
        "SSE_AUTHORITATIVE_MS constant must be defined as a numeric literal"
    )

    # SSE writers tag entries with sseUpdatedAt: Date.now() so the
    # merge guard in flushLiveFetches preserves them against stale
    # bulk-fetch responses. handleChildState only stamps when it
    # AUTHORITATIVELY clears the detail (off-approval transition);
    # writers that stamp unconditionally are intent_verdict (verdict
    # stamp), approval_resolved (clear), and the optimistic-clear
    # path in submitChildApproval. Pinning the literal Date.now()
    # call keeps a refactor that drops the SSE-source tag entirely
    # from sneaking in.
    assert re.search(
        r"sseUpdatedAt:\s*Date\.now\(\)",
        body,
    ), "Critical SSE writers must stamp sseUpdatedAt: Date.now()"

    # flushLiveFetches' merge guard structure: SSE-set pending_approval
    # / _detail wins over a stale bulk-poll snapshot when (live) AND
    # (prev exists) AND (prev.sseUpdatedAt set) AND (within window)
    # AND (prev.live exists).  Inverting the comparison or dropping
    # any of these guards reopens the clobber bug.
    merge_guard = re.search(
        r"if\s*\(\s*live\s*&&\s*prev\s*&&\s*prev\.sseUpdatedAt\s*&&\s*"
        r"now\s*-\s*prev\.sseUpdatedAt\s*<\s*SSE_AUTHORITATIVE_MS\s*&&\s*"
        r"prev\.live\s*\)",
        body,
    )
    assert merge_guard is not None, (
        "flushLiveFetches merge guard must be the conjunction "
        "(live && prev && prev.sseUpdatedAt && now - prev.sseUpdatedAt < "
        "SSE_AUTHORITATIVE_MS && prev.live).  An inverted comparison or "
        "missing prev.live check would let a stale bulk-poll clobber a "
        "fresh SSE-set approval."
    )

    # The merge body must preserve BOTH pending_approval and
    # pending_approval_detail from prev — preserving only one would
    # render a row with a phantom badge but no buttons (or vice versa).
    merge_body = re.search(
        r"mergedLive\s*=\s*Object\.assign\(\s*\{\}\s*,\s*live\s*,\s*\{"
        r"[^}]*pending_approval:\s*prev\.live\.pending_approval[^}]*"
        r"pending_approval_detail:\s*prev\.live\.pending_approval_detail",
        body,
    )
    assert merge_body is not None, (
        "Merge body must preserve both pending_approval AND "
        "pending_approval_detail from prev.live — preserving only one "
        "creates a half-rendered approval row."
    )

    # flushLiveFetches must forward sseUpdatedAt onto the new cache
    # entry so the SSE-source tag survives the bulk-poll write back —
    # without this, every bulk-poll resets the window and the next
    # late-arriving poll silently clobbers.
    assert re.search(
        r"sseUpdatedAt:\s*prev\s*\?\s*prev\.sseUpdatedAt",
        body,
    ), (
        "flushLiveFetches must forward prev.sseUpdatedAt onto the new "
        "cache entry (preserving the SSE-source window across bulk-poll "
        "cycles) — without this, the second bulk-poll after an SSE "
        "transition silently clobbers."
    )


def test_coord_history_renders_system_turn_via_msg_variants():
    """First-class operator-context ``system`` turns (output-guard findings,
    user interjections, metacognitive nudges) replay through the coord
    history loop's ``system``-role branch, labelled with the turn's
    ``source`` and styled via the ``system`` ``_MSG_VARIANTS`` entry.  The
    legacy ``replayAdvisoriesAfterTool`` envelope path is gone.

    Mirrors ``test_app_js.py``'s same-shape pin on interactive's
    ``replayHistory``."""
    from pathlib import Path

    coord_js = Path(__file__).resolve().parent.parent / (
        "turnstone/console/static/coordinator/coordinator.js"
    )
    body = coord_js.read_text(encoding="utf-8")

    # The advisory-envelope replay helper is gone.
    assert "replayAdvisoriesAfterTool" not in body, (
        "replayAdvisoriesAfterTool should be deleted — operator context now "
        "rides first-class system rows, not the tool envelope."
    )
    # The coord history loop has an explicit system-role branch labelling
    # the bubble with the turn's source kind.
    assert 'role === "system"' in body, (
        "coord history loop must have a system-role branch for first-class operator-context turns."
    )
    # The ``system`` _MSG_VARIANTS entry gives the bubble operator styling and
    # tags it with the shared ``operator-context`` marker (so the retry-skip
    # walk steps over it — see test_coord_retry_walk_skips_operator_context_cards).
    assert 'system: "system-context operator-context"' in body, (
        "coordinator.js must map the system role to the "
        "'system-context operator-context' variant so operator-context turns "
        "get the operator styling AND carry the retry-skip marker."
    )


def test_coord_dedups_system_turn_against_history_by_event_id():
    """The coord live ``system_turn`` handler skips an event already painted
    from ``/history`` (matched by ``_event_id``) so an SSE replay redelivering
    it past the resume cursor doesn't double-render the operator bubble.
    Symmetric with ``test_app_js.py``'s interactive dedup and the row/event
    id-alignment backend fix — both panes share the seam."""
    from pathlib import Path

    coord_js = Path(__file__).resolve().parent.parent / (
        "turnstone/console/static/coordinator/coordinator.js"
    )
    body = coord_js.read_text(encoding="utf-8")

    assert "renderedSystemEventIds.has(" in body, (
        "the coord system_turn handler must skip an event whose id was already "
        "rendered from /history."
    )
    assert "renderedSystemEventIds.add(" in body, (
        "the coord history loop (and live handler) must record system-turn ids."
    )
    assert "renderedSystemEventIds.clear(" in body, (
        "refetchHistory must reset the dedup set so a re-render doesn't "
        "false-skip after clear_ui / replay_truncated."
    )


def test_coord_retry_walk_skips_operator_context_cards():
    """Retry must NOT regenerate a stale assistant turn when the last DOM row is
    a tool batch trailed by an operator-context row.  ``_refreshRetryButton``
    walks back past ``.operator-context`` rows before testing for
    ``.coord-tool-batch`` — which only works if EVERY operator row carries the
    shared marker.  Pin the walk predicate AND the marker on each structured
    card so a new card kind (or a walk keyed on a single class) can't silently
    re-introduce the wrong-turn retry regression."""
    from pathlib import Path

    coord_js = Path(__file__).resolve().parent.parent / (
        "turnstone/console/static/coordinator/coordinator.js"
    )
    body = coord_js.read_text(encoding="utf-8")

    assert 'classList.contains("operator-context")' in body, (
        "_refreshRetryButton must walk back past .operator-context rows so the "
        "tool-only retry skip fires even when a card trails the tool batch."
    )
    for builder, cls in (
        ("appendWatchResult", '"msg watch-result operator-context"'),
        ("appendGuardFinding", '"msg guard-finding operator-context"'),
        ("appendIdleChildren", '"msg idle-children operator-context"'),
    ):
        assert cls in body, (
            f"{builder} must tag its card with the shared operator-context "
            f"marker ({cls}) or the retry walk won't skip it."
        )


def test_coordinator_js_seeds_resume_cursor_only_on_initial_connect():
    """coordinator.js must consume the /history resume cursor the same way
    ui/static/app.js does: the shared make_history_handler trims the
    executing in-flight orphan turn and returns a cursor, so the coord
    client MUST open its initial SSE with that cursor (?last_event_id=) or
    the trimmed turn is neither in /history nor delta-replayed — it vanishes
    from the dashboard (a regression vs the prior #610 in-flight render).

    Pins three invariants mirroring the app.js guards:
      1. ``refetchHistory`` takes a ``seedCursor`` flag (default false) and
         seeds ``lastEventId`` from ``hist.cursor`` only when set + non-null,
         so the clear_ui / replay_truncated re-render callers (live stream,
         no reconnect) don't rewind the live cursor.
      2. the initial-connect path opts in via ``refetchHistory(true)``.
      3. ``connectSSE`` gates ``?last_event_id=`` on ``!= null`` so a cursor
         of 0 (a brand-new ws's first-turn boundary) isn't dropped.
    """
    import re
    from pathlib import Path

    coord_js = Path(__file__).resolve().parent.parent / (
        "turnstone/console/static/coordinator/coordinator.js"
    )
    body = coord_js.read_text(encoding="utf-8")
    assert "async function refetchHistory(seedCursor = false)" in body, (
        "refetchHistory must take a seedCursor flag (default false) so only "
        "the initial-connect caller seeds the resume cursor."
    )
    assert re.search(
        r"if\s*\(\s*seedCursor\s*&&\s*hist\.cursor\s*!=\s*null\s*\)\s*"
        r"lastEventId\s*=\s*hist\.cursor",
        body,
    ), "refetchHistory must seed lastEventId from hist.cursor only when seedCursor && != null."
    assert "await refetchHistory(true)" in body, (
        "the initial-connect path must call refetchHistory(true) to seed the cursor."
    )
    assert re.search(
        r"if\s*\(\s*lastEventId\s*!=\s*null\s*\)\s*\{\s*url\s*\+=\s*\"\?last_event_id=\"",
        body,
    ), "connectSSE must gate ?last_event_id= on lastEventId != null (so cursor 0 isn't dropped)."


def test_coordinator_js_early_paints_pending_tool_calls():
    """The coord chat frontend must render a committed tool call on
    ``tool_pending`` — before the intent judge verdict + approval gate
    resolve — reusing the idempotent ``appendToolBatch`` upgrade path so the
    authoritative ``approve_request`` / ``tool_info`` morphs the same
    construct in place.  Guards the early-paint wiring (the #621 block) so a
    refactor that drops the handler or the ``announce`` kicker branch surfaces
    here instead of in production.  String presence only — coord.js has no JS
    test framework today."""
    from pathlib import Path

    coord_js = Path(__file__).resolve().parent.parent / (
        "turnstone/console/static/coordinator/coordinator.js"
    )
    body = coord_js.read_text(encoding="utf-8")
    assert 'case "tool_pending":' in body
    assert "announce: true" in body
    # Distinct "Evaluating" placeholder kicker for the pre-verdict shell.
    assert "opts.announce" in body
    assert '"Evaluating"' in body


def test_coordinator_js_early_paint_screen_reader_announce():
    """Coord screen-reader parity for the early paint: a committed tool call
    routes to a POLITE off-screen announcer (not the assertive gate region,
    not the messages log which is aria-live="off" mid-stream), and the
    announced batch carries aria-busy until upgraded.  Silent failures, so
    pin both the JS wiring and the index.html region."""
    from pathlib import Path

    base = Path(__file__).resolve().parent.parent / "turnstone/console/static/coordinator"
    coord_js = (base / "coordinator.js").read_text(encoding="utf-8")

    # Dedicated polite announcer + helper, distinct from the assertive one.  The
    # markup is built by buildCoordChrome now (the standalone page went thin).
    assert '"coord-sr-announcer-polite"' in coord_js
    pos = coord_js.index('"coord-sr-announcer-polite"')
    assert '"aria-live": "polite"' in coord_js[pos : pos + 200]
    assert "function _announcePolite(" in coord_js
    # Root-scoped now (de-globalized pane factory): the polite announcer is
    # resolved off the pane root, not document.getElementById.
    assert 'querySelector("#coord-sr-announcer-polite")' in coord_js
    # tool_pending announces politely; the announce shell is marked busy.
    assert "_announcePolite(_toolAnnounceText(ev.items" in coord_js
    assert 'if (opts.announce) batch.setAttribute("aria-busy", "true")' in coord_js


def test_coordinator_de_globalized_to_pane_factory():
    """Step 4a: coordinator.js is a multi-instantiable pane factory, not a
    page-global IIFE.  ``createCoordinatorPane(root, wsId)`` root-scopes every
    lookup, owns its lifecycle (connect / destroy / onLogin), and exposes no
    page-global ``window.coord*`` / ``onLoginSuccess`` collision point; the
    standalone page bootstraps one pane filling the body."""
    from pathlib import Path

    base = Path(__file__).resolve().parent.parent / "turnstone/console/static/coordinator"
    coord_js = (base / "coordinator.js").read_text(encoding="utf-8")
    index_html = (base / "index.html").read_text(encoding="utf-8")

    assert "function createCoordinatorPane(root, wsId, opts) {" in coord_js
    assert "window.createCoordinatorPane = createCoordinatorPane;" in coord_js
    assert "function destroy() {" in coord_js, "a pane must have a teardown path"
    # ws_id is a constructor arg now, not read off <html>; lookups are root-scoped.
    assert "document.documentElement.dataset.wsId" not in coord_js
    assert "document.getElementById(" not in coord_js, (
        "pane code must root-scope, not getElementById"
    )
    # No page-global collision points (multi-instance safe).
    for gone in ("window.coordSend", "window.coordCloseSession", "window.onLoginSuccess"):
        assert gone not in coord_js, f"de-globalized: {gone} must be gone"
    # Standalone page = one pane filling the body; the inline close onclick is gone.
    assert "createCoordinatorPane(document.body" in index_html
    assert 'onclick="coordCloseSession()"' not in index_html


def test_coordinator_chrome_builder_and_thin_page():
    """Step 4b: the coordinator chrome is built programmatically (createElement,
    no innerHTML) by buildCoordChrome, so the SAME factory serves the standalone
    page and a console pane.  The standalone page is now a thin bootstrap passing
    {standalone:true}; its static chrome + inline <style> are gone (CSS migrated)."""
    from pathlib import Path

    base = Path(__file__).resolve().parent.parent / "turnstone/console/static/coordinator"
    coord_js = (base / "coordinator.js").read_text(encoding="utf-8")
    index_html = (base / "index.html").read_text(encoding="utf-8")

    assert "function buildCoordChrome(root, opts)" in coord_js
    assert "buildCoordChrome(root, opts);" in coord_js, "the factory must build its own chrome"
    assert ".innerHTML" not in coord_js, "the chrome builder must stay innerHTML-free"
    # Pane-hosted close routes through opts.onClose (close the pane), not a redirect.
    assert "opts.onClose" in coord_js, "coordCloseSession must close the pane when pane-hosted"
    # Standalone page is thin: static chrome gone, links the migrated stylesheet,
    # bootstraps with the standalone flag (adds back-link / theme / toast).
    assert 'id="coord-header"' not in index_html, (
        "static chrome must be gone (the factory builds it)"
    )
    assert "coord-chrome.css" in index_html, "standalone must link the migrated chrome CSS"
    assert "standalone: true" in index_html
    assert (base / "coord-chrome.css").exists(), "the migrated chrome stylesheet must exist"


def test_coord_child_links_open_interactive_pane():
    """Step 5c: a coordinator child ws link (children tree + linkified tool
    output) opens the child as a node-proxied interactive pane in the console
    L-shell.  A delegated handler on the pane root reads data-ws-id/data-node-id
    and calls openPane('interactive', ...) with the CHILD's node; the link's
    href stays the standalone fallback (the standalone coordinator page has no
    PaneManager, so the new-tab nav stands)."""
    from pathlib import Path

    coord_js = (
        Path(__file__).resolve().parent.parent
        / "turnstone/console/static/coordinator/coordinator.js"
    ).read_text(encoding="utf-8")
    # Delegated handler, gated on the pane host so standalone keeps the href nav.
    assert '.closest(".ws-link, .coord-ws-link")' in coord_js
    assert "window.TS_SHELL && window.TS_SHELL.panes" in coord_js
    assert 'pm.openPane("interactive", childWs, { nodeId: childNode })' in coord_js
    # Both link sites carry the ids the handler reads.
    assert "a.dataset.wsId = safeWs;" in coord_js  # renderChildRow (DOM)
    assert "a.dataset.nodeId = safeNode;" in coord_js
    assert 'data-ws-id="' in coord_js  # renderToolOutput (string)
    assert 'data-node-id="' in coord_js
    # The /node/{id}/?ws_id= href fallback must remain for the standalone page.
    assert '"/node/"' in coord_js
