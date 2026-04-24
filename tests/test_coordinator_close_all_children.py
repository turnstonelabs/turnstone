"""Tests for the coordinator ``close_all_children`` endpoint.

Near-twin of the ``stop_cascade`` tests in
``test_coordinator_governance.py``.  Keeps the close-cascade surface in
its own file so PR A's review surface stays tight.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route
from starlette.testclient import TestClient

from tests._coord_test_helpers import (
    _AuthMiddleware,
    _build_mgr,
    _fake_registry,
    _FakeConfigStore,
    _seed_children,
)
from turnstone.console.server import coordinator_close_all_children
from turnstone.core.storage._sqlite import SQLiteBackend


@pytest.fixture
def storage(tmp_path):
    return SQLiteBackend(str(tmp_path / "coord.db"))


_COORD_HEADERS = {"X-Test-User": "user-1", "X-Test-Perms": "admin.coordinator"}


def _make_client(storage, *, coord_mgr, alias="my-model", registry=None) -> TestClient:
    app = Starlette(
        routes=[
            Route(
                "/v1/api/coordinator/{ws_id}/close_all_children",
                coordinator_close_all_children,
                methods=["POST"],
            ),
        ],
        middleware=[Middleware(_AuthMiddleware)],
    )
    app.state.coord_mgr = coord_mgr
    app.state.coord_adapter = coord_mgr._adapter if coord_mgr is not None else None
    app.state.config_store = _FakeConfigStore({"coordinator.model_alias": alias})
    app.state.coord_registry = registry
    app.state.coord_registry_error = "" if coord_mgr else "registry missing"
    app.state.auth_storage = storage
    app.state.jwt_secret = "x" * 64
    return TestClient(app)


def test_close_all_children_closes_each_child_and_audits(storage):
    mgr = _build_mgr(storage)
    coord = mgr.create(user_id="user-1", name="coord-a")
    _seed_children(mgr._adapter, coord.id, ["child-1", "child-2", "child-3"])

    def _close(wid, reason):
        if wid == "child-2":
            return {"error": "gateway_timeout", "status": 502}
        return {"status": "ok"}

    coord_client = MagicMock()
    coord_client.close_workstream.side_effect = _close
    coord.session = MagicMock()
    coord.session._coord_client = coord_client

    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.post(
        f"/v1/api/coordinator/{coord.id}/close_all_children",
        json={"reason": "tests done"},
        headers=_COORD_HEADERS,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert set(body["closed"] + body["failed"] + body["skipped"]) == {
        "child-1",
        "child-2",
        "child-3",
    }
    assert body["failed"] == ["child-2"]
    assert set(body["closed"]) == {"child-1", "child-3"}
    assert body["skipped"] == []
    assert coord_client.close_workstream.call_count == 3
    # Reason must propagate to each per-child close call.
    for call in coord_client.close_workstream.call_args_list:
        assert call.args[1] == "tests done"

    events = [
        e for e in storage.list_audit_events() if e["action"] == "coordinator.closed_all_children"
    ]
    assert len(events) == 1
    detail = json.loads(events[0]["detail"])
    assert detail["reason"] == "tests done"
    assert set(detail["closed"] + detail["failed"] + detail["skipped"]) == {
        "child-1",
        "child-2",
        "child-3",
    }


def test_close_all_children_routes_404_to_skipped_bucket(storage):
    """An upstream 404 (child row already deleted, stale registry entry)
    is 'already gone', not a dispatch failure.  Route to skipped."""
    mgr = _build_mgr(storage)
    coord = mgr.create(user_id="user-1", name="coord-a")
    _seed_children(mgr._adapter, coord.id, ["stale-child"])

    coord_client = MagicMock()
    coord_client.close_workstream.return_value = {
        "error": "workstream not in coordinator subtree: stale-child",
        "status": 404,
    }
    coord.session = MagicMock()
    coord.session._coord_client = coord_client

    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.post(
        f"/v1/api/coordinator/{coord.id}/close_all_children",
        json={},
        headers=_COORD_HEADERS,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["closed"] == []
    assert body["failed"] == []
    assert body["skipped"] == ["stale-child"]


def test_close_all_children_empty_children_still_audits(storage):
    mgr = _build_mgr(storage)
    coord = mgr.create(user_id="user-1", name="coord-a")
    coord.session = MagicMock()
    coord.session._coord_client = MagicMock()

    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.post(
        f"/v1/api/coordinator/{coord.id}/close_all_children",
        json={},
        headers=_COORD_HEADERS,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"status": "ok", "closed": [], "failed": [], "skipped": []}
    assert [
        e for e in storage.list_audit_events() if e["action"] == "coordinator.closed_all_children"
    ]


def test_close_all_children_without_coord_client_marks_all_failed(storage):
    mgr = _build_mgr(storage)
    coord = mgr.create(user_id="user-1", name="coord-a")
    _seed_children(mgr._adapter, coord.id, ["child-a", "child-b"])
    coord.session = MagicMock()
    coord.session._coord_client = None

    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.post(
        f"/v1/api/coordinator/{coord.id}/close_all_children",
        json={},
        headers=_COORD_HEADERS,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["closed"] == []
    assert body["skipped"] == []
    assert set(body["failed"]) == {"child-a", "child-b"}


def test_close_all_children_rejects_non_string_reason(storage):
    mgr = _build_mgr(storage)
    coord = mgr.create(user_id="user-1", name="coord-a")
    coord.session = MagicMock()
    coord.session._coord_client = MagicMock()

    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.post(
        f"/v1/api/coordinator/{coord.id}/close_all_children",
        json={"reason": 123},
        headers=_COORD_HEADERS,
    )
    assert resp.status_code == 400


def test_close_all_children_rejects_overlong_reason(storage):
    mgr = _build_mgr(storage)
    coord = mgr.create(user_id="user-1", name="coord-a")
    coord.session = MagicMock()
    coord.session._coord_client = MagicMock()

    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.post(
        f"/v1/api/coordinator/{coord.id}/close_all_children",
        json={"reason": "x" * 600},
        headers=_COORD_HEADERS,
    )
    assert resp.status_code == 400


def test_close_all_children_404_when_session_not_loaded(storage):
    mgr = _build_mgr(storage)
    coord = mgr.create(user_id="user-1", name="coord-a")
    coord.session = None
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.post(
        f"/v1/api/coordinator/{coord.id}/close_all_children",
        json={},
        headers=_COORD_HEADERS,
    )
    assert resp.status_code == 404


def test_close_all_children_service_token_cannot_bypass_admin_coordinator(storage):
    """Destructive endpoint — a service token matching the coord owner
    still needs the explicit ``admin.coordinator`` grant.  Mirrors the
    stop_cascade treatment."""
    mgr = _build_mgr(storage)
    coord = mgr.create(user_id="user-1", name="coord-a")
    coord.session = MagicMock()
    coord.session._coord_client = MagicMock()

    # Service token without admin.coordinator should be rejected.
    headers = {"X-Test-User": "user-1", "X-Test-Perms": ""}
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.post(
        f"/v1/api/coordinator/{coord.id}/close_all_children",
        json={},
        headers=headers,
    )
    assert resp.status_code in (401, 403)
