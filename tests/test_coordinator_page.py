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
