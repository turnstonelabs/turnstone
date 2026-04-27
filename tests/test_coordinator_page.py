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
    """Smoke guard for the Chunk 3 frontend wiring — the new helper
    function names must remain reachable in the served JS so a refactor
    accidentally renaming/removing them surfaces here instead of in
    production where the children-tree's inline approve/deny buttons
    silently stop rendering. Asserts string presence only — no DOM
    parsing — since coord.js has no JS test framework today (per the
    plan's testing notes)."""
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
    # Reconnect parity (chunk 4): the SSE re-open handler must clear
    # the live-badge cache so a stale pending_approval_detail (left
    # from before the disconnect) can't render zombie approve/deny
    # buttons on a row whose approval was resolved during the gap.
    assert "liveBadgeCache.clear()" in body
