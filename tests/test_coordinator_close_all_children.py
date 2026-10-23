"""Tests for the coordinator ``close_all_children`` endpoint.

Keeps the close-cascade surface in its own file so the review surface
stays tight.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import httpx2
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
    _rehydrate_with_children_down,
    _seed_children,
)
from turnstone.console.coordinator_client import CoordinatorClient
from turnstone.console.server import coordinator_close_all_children
from turnstone.core.child_event_bus import ChildEventBus

_COORD_HEADERS = {"X-Test-User": "user-1", "X-Test-Perms": "admin.coordinator"}


def _make_client(storage, *, coord_mgr, alias="my-model", registry=None) -> TestClient:
    app = Starlette(
        routes=[
            Route(
                "/v1/api/workstreams/{ws_id}/close_all_children",
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
        f"/v1/api/workstreams/{coord.id}/close_all_children",
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
        f"/v1/api/workstreams/{coord.id}/close_all_children",
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
        f"/v1/api/workstreams/{coord.id}/close_all_children",
        json={},
        headers=_COORD_HEADERS,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"status": "ok", "closed": [], "failed": [], "skipped": []}
    assert [
        e for e in storage.list_audit_events() if e["action"] == "coordinator.closed_all_children"
    ]


def test_close_all_children_answers_503_until_a_failed_children_read_succeeds(storage, monkeypatch):
    """A coordinator rehydrated while its children query fails doesn't know its
    children: close-all answers 503 and closes nothing, where it reported
    "closed 0", and once the query works again it closes them all."""
    children = ["c" * 32, "d" * 32]
    mgr, ws, coord_id, down = _rehydrate_with_children_down(storage, monkeypatch, children)
    coord_client = MagicMock()
    coord_client.close_workstream.return_value = {"status": "ok"}
    ws.session = MagicMock()
    ws.session._coord_client = coord_client
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    url = f"/v1/api/workstreams/{coord_id}/close_all_children"

    resp = client.post(url, json={}, headers=_COORD_HEADERS)
    assert resp.status_code == 503
    assert "children could not be read" in resp.json()["error"]
    coord_client.close_workstream.assert_not_called()

    down["on"] = False
    resp = client.post(url, json={}, headers=_COORD_HEADERS)
    assert resp.status_code == 200
    assert sorted(resp.json()["closed"]) == children


def test_close_all_children_without_coord_client_marks_all_failed(storage):
    mgr = _build_mgr(storage)
    coord = mgr.create(user_id="user-1", name="coord-a")
    _seed_children(mgr._adapter, coord.id, ["child-a", "child-b"])
    coord.session = MagicMock()
    coord.session._coord_client = None

    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.post(
        f"/v1/api/workstreams/{coord.id}/close_all_children",
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
        f"/v1/api/workstreams/{coord.id}/close_all_children",
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
        f"/v1/api/workstreams/{coord.id}/close_all_children",
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
        f"/v1/api/workstreams/{coord.id}/close_all_children",
        json={},
        headers=_COORD_HEADERS,
    )
    assert resp.status_code == 404


def test_close_all_children_service_token_cannot_bypass_admin_coordinator(storage):
    """Destructive endpoint — a service token matching the coord owner
    still needs the explicit ``admin.coordinator`` grant.  Mirrors the
    ``restrict`` treatment."""
    mgr = _build_mgr(storage)
    coord = mgr.create(user_id="user-1", name="coord-a")
    coord.session = MagicMock()
    coord.session._coord_client = MagicMock()

    # Service token without admin.coordinator should be rejected.
    headers = {"X-Test-User": "user-1", "X-Test-Perms": ""}
    client = _make_client(storage, coord_mgr=mgr, registry=_fake_registry())
    resp = client.post(
        f"/v1/api/workstreams/{coord.id}/close_all_children",
        json={},
        headers=headers,
    )
    assert resp.status_code in (401, 403)


@pytest.fixture
def discovery(storage):
    mgr = _build_mgr(storage)
    coord = mgr.create(user_id="owner", name="parent")
    closed = []
    reasons = []
    statuses = {}

    def upstream(request):
        assert request.method == "POST"
        assert request.url.path.endswith("/close")
        child_id = request.url.path.split("/")[-2]
        closed.append(child_id)
        reasons.append(json.loads(request.content).get("reason", ""))
        status = statuses.get(child_id, 200)
        body = {"status": "ok"} if status == 200 else {"error": "child close failed"}
        return httpx2.Response(status, json=body)

    with httpx2.Client(transport=httpx2.MockTransport(upstream)) as http:
        coord.session._coord_client = CoordinatorClient(
            console_base_url="http://console.example.com",
            storage=storage,
            token_factory=lambda: "test-only",
            coord_ws_id=coord.id,
            user_id="owner",
            http_client=http,
            child_event_bus=ChildEventBus(),
        )
        try:
            with _make_client(storage, coord_mgr=mgr, registry=_fake_registry()) as client:
                client.headers.update(
                    {"X-Test-User": "operator", "X-Test-Perms": "admin.coordinator"}
                )
                yield SimpleNamespace(
                    storage=storage,
                    coord=coord,
                    adapter=mgr._adapter,
                    client=client,
                    url=f"/v1/api/workstreams/{coord.id}/close_all_children",
                    closed=closed,
                    reasons=reasons,
                    statuses=statuses,
                )
        finally:
            mgr.close(coord.id)


def _stored_child(discovery, number, *, state="running", owner="owner", parent=None):
    ws_id = f"{number:032x}"
    discovery.storage.register_workstream(
        ws_id,
        kind="interactive",
        parent_ws_id=discovery.coord.id if parent is None else parent,
        user_id=owner,
        state=state,
    )
    return ws_id


def _discovery_audit(discovery):
    events = [
        event
        for event in discovery.storage.list_audit_events()
        if event["action"] == "coordinator.closed_all_children"
    ]
    assert len(events) == 1
    assert events[0]["user_id"] == "operator"
    return json.loads(events[0]["detail"])


def test_close_discovers_published_children_without_consuming_creation_events(discovery):
    states = ["idle", "running", "attention", "error", "closed"]
    children = [_stored_child(discovery, n, state=state) for n, state in enumerate(states, start=1)]
    listener = discovery.coord.ui._register_listener()

    response = discovery.client.post(discovery.url, json={"reason": "round complete"})

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "closed": children, "failed": [], "skipped": []}
    assert sorted(discovery.closed) == children
    assert discovery.reasons == ["round complete"] * len(children)
    assert _discovery_audit(discovery) == {
        "src": "coordinator",
        "reason": "round complete",
        "closed": children,
        "failed": [],
        "skipped": [],
    }
    assert discovery.adapter.registered_children_snapshot(discovery.coord.id) == []
    discovery.adapter._dispatch_child_event(
        {"type": "ws_created", "ws_id": children[0], "parent_ws_id": discovery.coord.id}
    )
    assert listener.get_nowait()["child_ws_id"] == children[0]


def test_close_unions_registered_and_stored_children_with_per_child_ownership(discovery):
    stored = _stored_child(discovery, 1)
    both = _stored_child(discovery, 2)
    deleted = _stored_child(discovery, 3)
    foreign = _stored_child(discovery, 4, owner="operator")
    _seed_children(discovery.adapter, discovery.coord.id, [both, deleted, foreign])
    assert discovery.storage.delete_workstream(deleted)

    response = discovery.client.post(discovery.url, json={})

    assert response.status_code == 200
    assert sorted(discovery.closed) == [stored, both]
    assert response.json() == {
        "status": "ok",
        "closed": [stored, both],
        "failed": [],
        "skipped": [deleted, foreign],
    }
    assert _discovery_audit(discovery)["skipped"] == [deleted, foreign]


def test_close_filters_request_storage_by_coordinator_owner_parent_and_publication(discovery):
    child = _stored_child(discovery, 1)
    _stored_child(discovery, 2, owner="operator")
    _stored_child(discovery, 3, parent="another-parent")
    _stored_child(discovery, 4, state="creating")
    request_storage = Mock(wraps=discovery.storage)
    discovery.client.app.state.auth_storage = request_storage

    assert discovery.client.post(discovery.url, json={}).status_code == 200

    assert discovery.closed == [child]
    request_storage.list_workstreams.assert_called_once_with(
        parent_ws_id=discovery.coord.id, user_id="owner", limit=None
    )


def test_close_discovery_has_no_listing_page_limit(discovery):
    children = [_stored_child(discovery, n) for n in range(1, 102)]

    response = discovery.client.post(discovery.url, json={})

    assert response.status_code == 200
    assert response.json()["closed"] == children
    assert sorted(discovery.closed) == children


@pytest.mark.parametrize("known_child", [False, True])
def test_close_failed_discovery_closes_nothing_even_with_known_children(
    discovery, monkeypatch, known_child
):
    child = _stored_child(discovery, 1)
    if known_child:
        _seed_children(discovery.adapter, discovery.coord.id, [child])

    def unreadable(**kwargs):
        raise RuntimeError("children query unavailable")

    monkeypatch.setattr(discovery.storage, "list_workstreams", unreadable)

    response = discovery.client.post(discovery.url, json={})

    assert response.status_code == 503
    assert "children could not be read" in response.json()["error"]
    assert discovery.closed == []
    assert not any(
        event["action"] == "coordinator.closed_all_children"
        for event in discovery.storage.list_audit_events()
    )


@pytest.mark.parametrize("storage_recovers", [False, True])
def test_close_after_failed_seed_preserves_delayed_creation_events(
    discovery, monkeypatch, storage_recovers
):
    known = _stored_child(discovery, 1)
    delayed = _stored_child(discovery, 2)
    read = discovery.storage.list_workstreams
    calls = 0

    def recovering(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1 or not storage_recovers:
            raise RuntimeError("children query unavailable")
        return read(**kwargs)

    monkeypatch.setattr(discovery.storage, "list_workstreams", recovering)
    discovery.adapter._rebuild_children_registry(discovery.coord.id)
    _seed_children(discovery.adapter, discovery.coord.id, [known])
    listener = discovery.coord.ui._register_listener()

    response = discovery.client.post(discovery.url, json={})

    assert response.status_code == (200 if storage_recovers else 503)
    assert sorted(discovery.closed) == ([known, delayed] if storage_recovers else [])
    assert discovery.adapter.registered_children_snapshot(discovery.coord.id) == [known]
    discovery.adapter._dispatch_child_event(
        {"type": "ws_created", "ws_id": delayed, "parent_ws_id": discovery.coord.id}
    )
    assert listener.get_nowait()["child_ws_id"] == delayed
    assert calls == 2


def test_close_empty_owner_never_queries_without_an_owner_filter(discovery, monkeypatch):
    child = _stored_child(discovery, 1)
    _seed_children(discovery.adapter, discovery.coord.id, [child])
    discovery.coord.user_id = ""
    read = Mock(wraps=discovery.storage.list_workstreams)
    monkeypatch.setattr(discovery.storage, "list_workstreams", read)

    response = discovery.client.post(discovery.url, json={})

    assert response.status_code == 503
    assert discovery.closed == []
    read.assert_not_called()


def test_close_missing_adapter_does_not_report_empty_success(discovery):
    _stored_child(discovery, 1)
    discovery.client.app.state.coord_adapter = None

    response = discovery.client.post(discovery.url, json={})

    assert response.status_code == 503
    assert discovery.closed == []


def test_close_stored_children_reports_actual_dispatch_outcomes(discovery):
    closed = _stored_child(discovery, 1)
    failed = _stored_child(discovery, 2)
    skipped = _stored_child(discovery, 3)
    discovery.statuses.update({failed: 502, skipped: 404})

    response = discovery.client.post(discovery.url, json={})

    assert response.status_code == 200
    assert sorted(discovery.closed) == [closed, failed, skipped]
    assert response.json() == {
        "status": "ok",
        "closed": [closed],
        "failed": [failed],
        "skipped": [skipped],
    }
    detail = _discovery_audit(discovery)
    assert detail["closed"] == [closed]
    assert detail["failed"] == [failed]
    assert detail["skipped"] == [skipped]
