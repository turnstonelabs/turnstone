"""Coordinator Stop discovers children independently of creation-event delivery."""

from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import httpx2
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.routing import Route
from starlette.testclient import TestClient

from tests._coord_test_helpers import _AuthMiddleware, _build_mgr, _seed_children
from turnstone.console.coordinator_client import CoordinatorClient
from turnstone.console.server import (
    _cascade_cancel_to_children,
    _coordinator_tenant_check,
    _require_admin_coordinator,
)
from turnstone.core.auth import AuthResult
from turnstone.core.child_event_bus import ChildEventBus
from turnstone.core.session_routes import SessionEndpointConfig, make_cancel_handler


@pytest.fixture
def cascade(storage_backend):
    mgr = _build_mgr(storage_backend)
    coord = mgr.create(user_id="owner", name="parent")
    cancelled = []

    def upstream(request):
        coord.session.cancel.assert_called()
        assert request.method == "POST"
        assert request.url.path.endswith("/cancel")
        cancelled.append(request.url.path.split("/")[-2])
        return httpx2.Response(200, json={"status": "ok"})

    with httpx2.Client(transport=httpx2.MockTransport(upstream)) as http:
        coord_client = CoordinatorClient(
            console_base_url="http://console.example.com",
            storage=storage_backend,
            token_factory=lambda: "test-only",
            coord_ws_id=coord.id,
            user_id="owner",
            http_client=http,
            child_event_bus=ChildEventBus(),
        )
        coord.session._coord_client = coord_client
        cfg = SessionEndpointConfig(
            permission_gate=_require_admin_coordinator,
            manager_lookup=lambda request: (mgr, None),
            tenant_check=_coordinator_tenant_check,
            not_found_label="coordinator not found",
            audit_action_prefix="coordinator",
        )
        handler = make_cancel_handler(cfg, post_cancel=_cascade_cancel_to_children)
        app = Starlette(
            routes=[Route("/v1/api/workstreams/{ws_id}/cancel", handler, methods=["POST"])]
        )
        app.state.coord_adapter = mgr._adapter
        app.state.auth_storage = storage_backend
        app.add_middleware(_AuthMiddleware)
        try:
            with TestClient(app) as client:
                # An authorized operator can stop another owner's coordinator.
                client.headers.update(
                    {"X-Test-User": "operator", "X-Test-Perms": "admin.coordinator"}
                )
                yield SimpleNamespace(
                    storage=storage_backend,
                    coord=coord,
                    adapter=mgr._adapter,
                    client=client,
                    app=app,
                    handler=handler,
                    url=f"/v1/api/workstreams/{coord.id}/cancel",
                    cancelled=cancelled,
                    coord_client=coord_client,
                )
        finally:
            mgr.close(coord.id)


def _child(cascade, number, *, state="running", owner="owner", parent=None):
    ws_id = f"{number:032x}"
    cascade.storage.register_workstream(
        ws_id,
        kind="interactive",
        parent_ws_id=cascade.coord.id if parent is None else parent,
        user_id=owner,
        state=state,
    )
    return ws_id


def _audit(cascade):
    events = [
        event
        for event in cascade.storage.list_audit_events()
        if event["action"] == "coordinator.cancel_cascaded"
    ]
    assert len(events) == 1
    assert events[0]["user_id"] == "operator"
    return json.loads(events[0]["detail"])


@pytest.mark.parametrize("force", [False, True])
def test_stop_reaches_stored_children_without_consuming_their_creation_events(cascade, force):
    states = ["idle", "running", "attention", "error", "closed"]
    children = [_child(cascade, n, state=state) for n, state in enumerate(states, start=1)]
    listener = cascade.coord.ui._register_listener()
    assert cascade.adapter.registered_children_snapshot(cascade.coord.id) == []

    response = cascade.client.post(cascade.url, json={"force": force})

    assert response.status_code == 200
    assert sorted(cascade.cancelled) == children
    assert _audit(cascade) == {
        "src": "coordinator",
        "cancelled": children,
        "failed": [],
        "skipped": [],
    }
    assert cascade.adapter.registered_children_snapshot(cascade.coord.id) == []
    cascade.adapter._dispatch_child_event(
        {"type": "ws_created", "ws_id": children[0], "parent_ws_id": cascade.coord.id}
    )
    events = []
    while not listener.empty():
        events.append(listener.get_nowait())
    assert [event["child_ws_id"] for event in events if event["type"] == "child_ws_created"] == [
        children[0]
    ]


def test_stop_unions_stored_and_registered_children_once(cascade):
    stored = _child(cascade, 1)
    both = _child(cascade, 2)
    removed = _child(cascade, 3)
    _seed_children(cascade.adapter, cascade.coord.id, [both, removed])
    assert cascade.storage.delete_workstream(removed)

    assert cascade.client.post(cascade.url).status_code == 200

    assert sorted(cascade.cancelled) == [stored, both]
    assert _audit(cascade) == {
        "src": "coordinator",
        "cancelled": [stored, both],
        "failed": [],
        "skipped": [removed],
    }


def test_stop_filters_storage_by_coordinator_owner_and_parent(cascade, monkeypatch):
    child = _child(cascade, 1)
    _child(cascade, 2, owner="operator")
    _child(cascade, 3, parent="another-parent")
    _child(cascade, 4, state="creating")
    read = Mock(wraps=cascade.storage.list_workstreams)
    monkeypatch.setattr(cascade.storage, "list_workstreams", read)

    assert cascade.client.post(cascade.url).status_code == 200

    assert cascade.cancelled == [child]
    assert _audit(cascade) == {
        "src": "coordinator",
        "cancelled": [child],
        "failed": [],
        "skipped": [],
    }
    read.assert_called_once_with(parent_ws_id=cascade.coord.id, user_id="owner", limit=None)


def test_stop_has_no_listing_page_limit(cascade):
    children = [_child(cascade, n) for n in range(1, 102)]

    assert cascade.client.post(cascade.url).status_code == 200

    assert sorted(cascade.cancelled) == children
    assert sorted(_audit(cascade)["cancelled"]) == children


@pytest.mark.parametrize("known_child", [False, True])
def test_stop_audits_failed_discovery_and_still_attempts_known_children(
    cascade, monkeypatch, known_child
):
    child = _child(cascade, 1)
    if known_child:
        _seed_children(cascade.adapter, cascade.coord.id, [child])

    def unreadable(*args, **kwargs):
        raise RuntimeError("children query unavailable")

    monkeypatch.setattr(cascade.storage, "list_workstreams", unreadable)

    assert cascade.client.post(cascade.url).status_code == 200

    expected = [child] if known_child else []
    assert cascade.cancelled == expected
    assert _audit(cascade) == {
        "src": "coordinator",
        "error": "the coordinator's children could not be read",
        "cancelled": expected,
        "failed": [],
        "skipped": [],
    }


@pytest.mark.parametrize("storage_recovers", [False, True])
def test_stop_keeps_known_children_and_delayed_events_after_a_failed_seed(
    cascade, monkeypatch, storage_recovers
):
    known = _child(cascade, 1)
    delayed = _child(cascade, 2)
    read = cascade.storage.list_workstreams
    calls = 0

    def recovering(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1 or not storage_recovers:
            raise RuntimeError("children query unavailable")
        return read(*args, **kwargs)

    monkeypatch.setattr(cascade.storage, "list_workstreams", recovering)
    cascade.adapter._rebuild_children_registry(cascade.coord.id)
    _seed_children(cascade.adapter, cascade.coord.id, [known])
    listener = cascade.coord.ui._register_listener()

    assert cascade.client.post(cascade.url).status_code == 200

    expected = [known, delayed] if storage_recovers else [known]
    assert sorted(cascade.cancelled) == expected
    detail = _audit(cascade)
    assert detail["cancelled"] == expected
    assert ("error" in detail) is not storage_recovers
    # Stop must not retry a seed that would consume the delayed creation event.
    assert cascade.adapter.registered_children_snapshot(cascade.coord.id) == [known]
    cascade.adapter._dispatch_child_event(
        {"type": "ws_created", "ws_id": delayed, "parent_ws_id": cascade.coord.id}
    )
    assert listener.get_nowait()["child_ws_id"] == delayed
    assert calls == 2  # the failed seed, then one authoritative cancellation read


def test_stop_never_discovers_unowned_children_with_an_unfiltered_read(cascade, monkeypatch):
    _child(cascade, 1)
    cascade.coord.user_id = ""
    read = Mock(wraps=cascade.storage.list_workstreams)
    monkeypatch.setattr(cascade.storage, "list_workstreams", read)

    assert cascade.client.post(cascade.url).status_code == 200

    read.assert_not_called()
    assert cascade.cancelled == []
    assert _audit(cascade)["error"] == "the coordinator's children could not be read"


def test_stop_response_is_sent_before_child_http_finishes(cascade, monkeypatch):
    child = _child(cascade, 1)
    response_sent = threading.Event()
    cancel_started = threading.Event()
    release_cancel = threading.Event()
    cancel = cascade.coord_client.cancel

    def blocked_cancel(ws_id):
        cancel_started.set()
        assert response_sent.is_set()
        assert release_cancel.wait(5)
        return cancel(ws_id)

    monkeypatch.setattr(cascade.coord_client, "cancel", blocked_cancel)

    async def run():
        scope = {
            "type": "http",
            "method": "POST",
            "path": cascade.url,
            "headers": [],
            "path_params": {"ws_id": cascade.coord.id},
            "app": cascade.app,
            "state": {
                "auth_result": AuthResult(
                    user_id="operator",
                    scopes=frozenset({"read", "write", "approve"}),
                    permissions=frozenset({"admin.coordinator"}),
                    token_source="test",
                )
            },
        }

        async def receive():
            return {"type": "http.request", "body": b"{}", "more_body": False}

        sent = []

        async def send(message):
            sent.append(message)
            if message["type"] == "http.response.body" and not message.get("more_body"):
                response_sent.set()

        response = await cascade.handler(Request(scope, receive))
        assert not cancel_started.is_set()
        task = asyncio.create_task(response(scope, receive, send))
        try:
            assert await asyncio.to_thread(cancel_started.wait, 5)
            assert response_sent.is_set()
            assert sent[0]["status"] == 200
            assert not task.done()
        finally:
            release_cancel.set()
            await task

    asyncio.run(run())
    assert cascade.cancelled == [child]
    assert _audit(cascade)["cancelled"] == [child]
