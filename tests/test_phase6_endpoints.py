"""Tests for the phase-6 polish endpoints (#q-1).

Covers:

- GET /v1/api/cluster/ws/live — bulk live-block fetch (admin.cluster.inspect).
- GET /v1/api/workstreams/{ws_id}/metrics — per-coordinator health snapshot.

Both endpoints ride on the same test harness as
``test_coordinator_endpoints.py`` — a minimal Starlette app with an
auth-injecting middleware, TestClient + MockTransport for the
upstream node fetches.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.routing import Route
from starlette.testclient import TestClient

from turnstone.console.collector import ClusterCollector
from turnstone.console.coordinator_adapter import CoordinatorAdapter
from turnstone.console.coordinator_ui import ConsoleCoordinatorUI
from turnstone.console.server import (
    cluster_ws_live_bulk,
    coordinator_metrics,
)
from turnstone.core.auth import AuthResult
from turnstone.core.session_manager import SessionManager
from turnstone.core.storage._sqlite import SQLiteBackend


class _AuthMiddleware(BaseHTTPMiddleware):
    """Inject a configurable AuthResult from header-based contract."""

    async def dispatch(self, request, call_next):
        perms = request.headers.get("X-Test-Perms", "")
        user_id = request.headers.get("X-Test-User", "")
        if perms or user_id:
            request.state.auth_result = AuthResult(
                user_id=user_id,
                scopes=frozenset({"approve"}),
                token_source="test",
                permissions=frozenset(p for p in perms.split(",") if p),
            )
        return await call_next(request)


class _FakeConfigStore:
    def __init__(self, values: dict[str, Any]) -> None:
        self._values = values

    def get(self, key: str, default: Any = None) -> Any:
        return self._values.get(key, default)


@pytest.fixture
def storage(tmp_path):
    return SQLiteBackend(str(tmp_path / "phase6.db"))


def _build_mgr(storage) -> SessionManager:
    def _sf(ui, model_alias=None, ws_id=None, **kw):
        return MagicMock()

    adapter = CoordinatorAdapter(
        collector=MagicMock(),
        ui_factory=lambda ws: ConsoleCoordinatorUI(ws_id=ws.id, user_id=ws.user_id or ""),
        session_factory=_sf,
    )
    mgr = SessionManager(
        adapter,
        storage=storage,
        max_active=3,
        node_id=ClusterCollector.CONSOLE_PSEUDO_NODE_ID,
    )
    adapter.attach(mgr)
    return mgr


def _fake_registry() -> MagicMock:
    reg = MagicMock()
    reg.resolve.return_value = (MagicMock(), "gpt-4", MagicMock())
    return reg


def _make_client(storage, *, coord_mgr=None) -> TestClient:
    app = Starlette(
        routes=[
            Route("/v1/api/cluster/ws/live", cluster_ws_live_bulk, methods=["GET"]),
            Route(
                "/v1/api/workstreams/{ws_id}/metrics",
                coordinator_metrics,
                methods=["GET"],
            ),
        ],
        middleware=[Middleware(_AuthMiddleware)],
    )
    app.state.coord_mgr = coord_mgr
    app.state.coord_adapter = coord_mgr._adapter if coord_mgr is not None else None
    app.state.config_store = _FakeConfigStore({"coordinator.model_alias": "gpt-4"})
    app.state.coord_registry = _fake_registry() if coord_mgr is not None else None
    app.state.coord_registry_error = "" if coord_mgr else "registry missing"
    app.state.auth_storage = storage
    app.state.jwt_secret = "x" * 64
    return TestClient(app)


def _seed_workstream(
    storage: SQLiteBackend,
    *,
    ws_id: str,
    node_id: str,
    user_id: str = "user-1",
    kind: str = "interactive",
    state: str = "idle",
    parent_ws_id: str | None = None,
    created: str | None = None,
) -> None:
    storage.register_workstream(
        ws_id,
        node_id=node_id,
        user_id=user_id,
        name=f"ws-{ws_id[:4]}",
        state=state,
        kind=kind,
        parent_ws_id=parent_ws_id,
    )
    if created is not None:
        # Override the created timestamp directly — register_workstream
        # stamps "now", so we need a second write to test the
        # spawns_last_hour boundary.
        import sqlalchemy as sa

        from turnstone.core.storage._sqlite import workstreams

        with storage._conn() as conn:
            conn.execute(
                sa.update(workstreams).where(workstreams.c.ws_id == ws_id).values(created=created)
            )
            conn.commit()


# ---------------------------------------------------------------------------
# GET /v1/api/cluster/ws/live — bulk live-block fetch
# ---------------------------------------------------------------------------


_ADMIN_HEADERS = {"X-Test-User": "user-1", "X-Test-Perms": "admin.cluster.inspect"}
_OWNER_HEADERS = _ADMIN_HEADERS  # same caller; permission grants inspect


def test_bulk_live_requires_permission(storage):
    client = _make_client(storage, coord_mgr=_build_mgr(storage))
    resp = client.get(
        "/v1/api/cluster/ws/live?ids=" + "a" * 32,
        headers={"X-Test-User": "u", "X-Test-Perms": "read"},
    )
    assert resp.status_code == 403


def test_bulk_live_empty_ids_returns_empty_body(storage):
    client = _make_client(storage, coord_mgr=_build_mgr(storage))
    resp = client.get("/v1/api/cluster/ws/live?ids=", headers=_ADMIN_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"results": {}, "denied": [], "truncated": False}


def test_bulk_live_strips_invalid_ids(storage):
    """IDs failing the hex-regex are silently dropped; duplicates
    collapse."""
    client = _make_client(storage, coord_mgr=_build_mgr(storage))
    # NOT-HEX is invalid; the valid id is 32 chars hex but unknown to
    # storage → shows up as denied.
    resp = client.get(
        "/v1/api/cluster/ws/live?ids=NOT-HEX,NOT-HEX,," + ("a" * 32) + "," + ("a" * 32),
        headers=_ADMIN_HEADERS,
    )
    assert resp.status_code == 200
    body = resp.json()
    # Invalid / empty / duplicate ids trimmed; only the one valid-but-
    # missing id is reported as denied.
    assert body["denied"] == ["a" * 32]
    assert body["results"] == {}


def test_bulk_live_caps_ids_at_50(storage):
    """Ids past the server-side cap truncate with truncated=true."""
    client = _make_client(storage, coord_mgr=_build_mgr(storage))
    # 60 fake ids → cap=50 keeps the first 50 (dedup preserves order).
    ids = ",".join(f"{i:064x}" for i in range(60))
    resp = client.get(
        "/v1/api/cluster/ws/live?ids=" + ids,
        headers=_ADMIN_HEADERS,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["truncated"] is True
    # All 50 kept ids resolve to 'denied' (no storage rows) — their
    # inclusion in the response proves the cap took the head 50.
    assert len(body["denied"]) == 50


def test_bulk_live_admin_bypass_returns_live(storage):
    """An admin user (holds admin.users or admin.roles, not just
    admin.cluster.inspect) bypasses tenancy and sees non-owned rows'
    live blocks.  Coordinator live-block synthesis is in-process, so
    results is populated without any upstream node fetch."""
    mgr = _build_mgr(storage)
    ws = mgr.create(user_id="other-user")
    client = _make_client(storage, coord_mgr=mgr)
    resp = client.get(
        "/v1/api/cluster/ws/live?ids=" + ws.id,
        headers={
            "X-Test-User": "user-1",
            # admin.users grants the _is_admin bypass in addition to
            # admin.cluster.inspect for the endpoint itself.
            "X-Test-Perms": "admin.cluster.inspect,admin.users",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert ws.id in body["results"]
    assert body["denied"] == []


def test_bulk_live_cluster_wide_visibility(storage):
    """Trusted-team visibility: any ``admin.cluster.inspect`` caller
    sees every row in ``results``.  ``denied`` is reserved for ids
    that don't correspond to a persisted workstream (no existence
    oracle for unknown ids)."""
    ws_id = "b" * 32
    _seed_workstream(storage, ws_id=ws_id, node_id="node-a", user_id="stranger")
    client = _make_client(storage, coord_mgr=_build_mgr(storage))
    resp = client.get(
        f"/v1/api/cluster/ws/live?ids={ws_id}",
        headers={"X-Test-User": "user-1", "X-Test-Perms": "admin.cluster.inspect"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert ws_id in body["results"]
    assert body["denied"] == []


def test_bulk_live_unknown_ids_route_to_denied(storage):
    """Unknown ids (not in storage) land in ``denied`` so the endpoint
    can't be used as an existence oracle."""
    ws_id = "c" * 32  # not seeded
    client = _make_client(storage, coord_mgr=_build_mgr(storage))
    resp = client.get(
        f"/v1/api/cluster/ws/live?ids={ws_id}",
        headers={"X-Test-User": "user-1", "X-Test-Perms": "admin.cluster.inspect"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["denied"] == [ws_id]
    assert body["results"] == {}


def test_bulk_live_coordinator_row_uses_manager_snapshot(storage):
    """A coordinator ws_id routes through _fetch_live_block's
    coordinator branch — live is populated from the in-process manager
    even though the pseudo-node has no /dashboard endpoint."""
    mgr = _build_mgr(storage)
    ws = mgr.create(user_id="user-1")
    client = _make_client(storage, coord_mgr=mgr)
    resp = client.get(
        f"/v1/api/cluster/ws/live?ids={ws.id}",
        headers=_OWNER_HEADERS,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert ws.id in body["results"]
    live = body["results"][ws.id]
    assert live is not None
    assert "pending_approval" in live


# ---------------------------------------------------------------------------
# GET /v1/api/workstreams/{ws_id}/metrics — per-coordinator health snapshot
# ---------------------------------------------------------------------------


_METRICS_HEADERS = {"X-Test-User": "user-1", "X-Test-Perms": "admin.coordinator"}


def test_metrics_requires_permission(storage):
    mgr = _build_mgr(storage)
    ws = mgr.create(user_id="user-1")
    client = _make_client(storage, coord_mgr=mgr)
    resp = client.get(
        f"/v1/api/workstreams/{ws.id}/metrics",
        headers={"X-Test-User": "user-1", "X-Test-Perms": "read"},
    )
    assert resp.status_code == 403


def test_metrics_invalid_ws_id_400(storage):
    mgr = _build_mgr(storage)
    client = _make_client(storage, coord_mgr=mgr)
    resp = client.get(
        "/v1/api/workstreams/NOT-HEX/metrics",
        headers=_METRICS_HEADERS,
    )
    assert resp.status_code == 400


def test_metrics_any_admin_coordinator_caller_can_read(storage):
    """Trusted-team visibility: metrics are readable by any caller
    with ``admin.coordinator`` regardless of the coordinator owner."""
    mgr = _build_mgr(storage)
    ws = mgr.create(user_id="stranger")
    client = _make_client(storage, coord_mgr=mgr)
    resp = client.get(
        f"/v1/api/workstreams/{ws.id}/metrics",
        headers=_METRICS_HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json()["ws_id"] == ws.id


def test_metrics_empty_coordinator_defaults(storage):
    """A freshly created coordinator with no spawns / no verdicts
    returns zero / empty defaults."""
    mgr = _build_mgr(storage)
    ws = mgr.create(user_id="user-1")
    client = _make_client(storage, coord_mgr=mgr)
    resp = client.get(
        f"/v1/api/workstreams/{ws.id}/metrics",
        headers=_METRICS_HEADERS,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ws_id"] == ws.id
    assert body["spawns_total"] == 0
    assert body["spawns_last_hour"] == 0
    assert body["child_state_counts"] == {}
    assert body["judge_fallback_rate"] == 0.0
    assert body["wait_completions"] == 0
    assert body["wait_timeouts"] == 0
    assert body["wait_avg_elapsed"] == 0.0


def test_metrics_spawns_and_state_counts(storage):
    """spawns_total counts ALL children (including closed); state
    histogram groups by current state.  All children share the
    coordinator's owner so the non-admin tenant filter on the
    aggregate queries counts them all (see next test for the
    cross-tenant filter behaviour)."""
    mgr = _build_mgr(storage)
    ws = mgr.create(user_id="user-1")
    _seed_workstream(
        storage,
        ws_id="aa" * 16,
        node_id="node-a",
        user_id="user-1",
        parent_ws_id=ws.id,
        state="idle",
    )
    _seed_workstream(
        storage,
        ws_id="bb" * 16,
        node_id="node-a",
        user_id="user-1",
        parent_ws_id=ws.id,
        state="running",
    )
    _seed_workstream(
        storage,
        ws_id="cc" * 16,
        node_id="node-a",
        user_id="user-1",
        parent_ws_id=ws.id,
        state="closed",
    )
    client = _make_client(storage, coord_mgr=mgr)
    resp = client.get(
        f"/v1/api/workstreams/{ws.id}/metrics",
        headers=_METRICS_HEADERS,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["spawns_total"] == 3
    assert body["child_state_counts"] == {"idle": 1, "running": 1, "closed": 1}


def test_metrics_cluster_wide_aggregates(storage):
    """Trusted-team model: aggregates are cluster-wide across every
    caller with ``admin.coordinator``.  Every child under the
    coordinator counts, regardless of the ``user_id`` on the row.
    """
    mgr = _build_mgr(storage)
    ws = mgr.create(user_id="alice")
    _seed_workstream(
        storage,
        ws_id="aa" * 16,
        node_id="node-a",
        user_id="alice",
        parent_ws_id=ws.id,
        state="idle",
    )
    _seed_workstream(
        storage,
        ws_id="bb" * 16,
        node_id="node-a",
        user_id="bob",
        parent_ws_id=ws.id,
        state="running",
    )
    client = _make_client(storage, coord_mgr=mgr)

    # Every admin.coordinator caller sees both children.
    for caller in ("alice", "bob", "admin-1"):
        resp = client.get(
            f"/v1/api/workstreams/{ws.id}/metrics",
            headers={"X-Test-User": caller, "X-Test-Perms": "admin.coordinator"},
        )
        assert resp.status_code == 200, caller
        body = resp.json()
        assert body["spawns_total"] == 2, caller
        assert body["child_state_counts"] == {"idle": 1, "running": 1}, caller


def test_metrics_judge_fallback_rate_substring_match(storage):
    """judge_fallback_rate is computed from any verdict whose ``tier``
    field contains 'fallback' (case-insensitive).  Supports tiers like
    'llm_fallback', 'LLM_FALLBACK', 'fallback_deterministic'."""
    import uuid

    mgr = _build_mgr(storage)
    ws = mgr.create(user_id="user-1")
    # Three verdicts, two marked fallback (one LLM_FALLBACK, one
    # llm_fallback → both match case-insensitive substring).
    for tier in ("llm_primary", "LLM_FALLBACK", "llm_fallback"):
        storage.create_intent_verdict(
            verdict_id=uuid.uuid4().hex,
            ws_id=ws.id,
            call_id="c-" + tier,
            func_name="f",
            func_args="{}",
            intent_summary="",
            risk_level="low",
            confidence=0.9,
            recommendation="allow",
            reasoning="",
            evidence="",
            tier=tier,
            judge_model="j",
            latency_ms=1,
        )
    client = _make_client(storage, coord_mgr=mgr)
    resp = client.get(
        f"/v1/api/workstreams/{ws.id}/metrics",
        headers=_METRICS_HEADERS,
    )
    assert resp.status_code == 200
    body = resp.json()
    # 2 / 3 verdicts matched → 0.667 (rounded to 3 places).
    assert body["judge_fallback_rate"] == pytest.approx(0.667, abs=1e-3)
    assert body["intent_verdicts_sample"] == 3


def test_metrics_spawns_last_hour_boundary(storage):
    """Only children whose created timestamp is within the last 3600s
    count toward spawns_last_hour; older children count toward
    spawns_total but not the hour bucket."""
    import time
    from datetime import UTC, datetime, timedelta

    mgr = _build_mgr(storage)
    ws = mgr.create(user_id="user-1")
    # One child created "now" (within the window); one created 2
    # hours ago (outside the window).
    recent_iso = datetime.fromtimestamp(time.time(), tz=UTC).strftime("%Y-%m-%dT%H:%M:%S")
    old_iso = (datetime.fromtimestamp(time.time(), tz=UTC) - timedelta(hours=2)).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )
    _seed_workstream(
        storage,
        ws_id="aa" * 16,
        node_id="node-a",
        parent_ws_id=ws.id,
        state="idle",
        created=recent_iso,
    )
    _seed_workstream(
        storage,
        ws_id="bb" * 16,
        node_id="node-a",
        parent_ws_id=ws.id,
        state="closed",
        created=old_iso,
    )
    client = _make_client(storage, coord_mgr=mgr)
    resp = client.get(
        f"/v1/api/workstreams/{ws.id}/metrics",
        headers=_METRICS_HEADERS,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["spawns_total"] == 2
    assert body["spawns_last_hour"] == 1
