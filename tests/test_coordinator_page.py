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
    # The urgent live-bulk fetch option that fires on activity_state
    # transitions in/out of "approval"
    assert "{ urgent: true }" in body or "urgent: true" in body
    # Server-side payload field — drift here means the JS reads stale keys
    assert "pending_approval_detail" in body
    # Reconnect parity (chunk 4): the SSE re-open handler must drop
    # non-permanent entries from the live-badge cache so a stale
    # pending_approval_detail (left from before the disconnect)
    # can't render zombie approve/deny buttons on a row whose
    # approval was resolved during the gap. The implementation
    # iterates the cache and deletes only !permanent entries —
    # asserting the literal Map iteration form keeps a refactor
    # back to liveBadgeCache.clear() (which would re-pay 403s on
    # every reconnect for denied ids) from sneaking in.
    assert "liveBadgeCache.delete" in body
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
    # Late-judge polling — the LLM judge runs async on the child
    # node and never pushes a signal that reaches the coord, so
    # the row's pending_approval_detail with judge_pending=true
    # would freeze on heuristic verdicts forever without this
    # poll loop. The poller is GLOBAL (not per-row) so off-screen
    # rows still refresh — a per-row poller's scheduleLiveFetch
    # call short-circuits on non-visible rows, leaving them stuck.
    assert "_maybeStartJudgePoll" in body
    assert "_judgePollTick" in body
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
    assert "Denied by user" in body
    assert "callOutcomes" in body


def test_coordinator_js_handle_child_state_reads_sse_pending_approval_detail():
    """Lock the Shape A behavior change: child_ws_state SSE events now
    carry ``pending_approval_detail`` directly so the browser mutates
    ``liveBadgeCache`` without firing an urgent live-bulk fetch on
    every activity_state transition into/out of approval.  A refactor
    that re-introduces the urgent-fetch path on routine transitions
    (or drops the SSE-source merge guard in flushLiveFetches) would
    re-open the load-storm pattern this PR is fixing.

    Structural assertions (regex against multi-line source) — symbol-
    presence alone wouldn't catch a guard that keeps the names but
    inverts the comparison or drops the ``prev.live`` check.  This
    codebase has no JS test framework, so locking the guard's shape
    here is the next-best thing to a behavioral test."""
    import re
    from pathlib import Path

    coord_js = Path(__file__).resolve().parent.parent / (
        "turnstone/console/static/coordinator/coordinator.js"
    )
    body = coord_js.read_text(encoding="utf-8")

    # handleChildState now reads the SSE-supplied detail.
    assert "ev.pending_approval_detail" in body
    # The pre-fix urgent-fetch on activity_state transitions is
    # gone (the 409 retry path keeps its own ``{ urgent: true }``
    # for stale-call_id refresh — that's a different scenario).
    assert "enteredApproval" not in body
    assert "leftApproval" not in body

    # SSE-authoritative window constant is defined and used.
    assert re.search(r"\bconst\s+SSE_AUTHORITATIVE_MS\s*=\s*\d+", body), (
        "SSE_AUTHORITATIVE_MS constant must be defined as a numeric literal"
    )

    # handleChildState writes sseUpdatedAt = Date.now() into the cache
    # entry it sets.  This is the SSE-source tag; without it, the
    # merge guard in flushLiveFetches has nothing to gate on.
    assert re.search(
        r"sseUpdatedAt:\s*Date\.now\(\)",
        body,
    ), "handleChildState must write sseUpdatedAt: Date.now() onto liveBadgeCache entries"

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
