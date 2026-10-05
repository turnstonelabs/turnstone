"""Node HTTP surfaces answer 409 ``workstream_lease_held`` for a workstream
another node owns, so the console and browser retry at the owner."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route

from tests.test_server_authz import _auth
from tests.test_server_authz import app_client as app_client
from turnstone.core.session_manager import SessionFactoryLeaseError
from turnstone.core.storage import WorkstreamLeaseHeldError, WorkstreamLeaseLostError, get_storage
from turnstone.core.workstream import WorkstreamHistoryUnavailableError


def _owned_elsewhere(client: Any, manager: Any) -> str:
    """Create a workstream here, close it, and let another node reopen it."""
    ws_id = client.post("/v1/api/workstreams/new", json={}, headers=_auth("owner")).json()["ws_id"]
    assert manager.close(ws_id)
    storage = get_storage()
    token = storage.get_workstream_reservation_token(ws_id)
    grant = storage.acquire_workstream_lease(
        ws_id,
        incarnation_token=token,
        holder="node-elsewhere/1",
        node_id="node-elsewhere",
        ttl_seconds=30.0,
    )
    assert grant is not None
    return ws_id


def test_open_refuses_a_workstream_owned_elsewhere(app_client: Any, monkeypatch: Any) -> None:
    client, manager = app_client
    ws_id = _owned_elsewhere(client, manager)
    # This fixture runs on SQLite, which takes a live lease over by contract;
    # model PostgreSQL's refusal (covered against a real PostgreSQL backend by
    # the storage and integration suites) to pin the HTTP mapping.
    storage = get_storage()

    def _refuse(ws: str, **_kwargs: Any) -> Any:
        raise WorkstreamLeaseHeldError(ws, holder_node_id="node-elsewhere", retry_after_ms=900)

    monkeypatch.setattr(storage, "acquire_workstream_lease", _refuse)

    response = client.post(f"/v1/api/workstreams/{ws_id}/open", headers=_auth("owner"))

    assert response.status_code == 409, response.text
    body = response.json()
    assert body["code"] == "workstream_lease_held"
    assert body["holder_node_id"] == "node-elsewhere"
    assert manager.get(ws_id) is None


def test_delete_refuses_a_workstream_owned_elsewhere(app_client: Any) -> None:
    client, manager = app_client
    ws_id = _owned_elsewhere(client, manager)

    response = client.post(f"/v1/api/workstreams/{ws_id}/delete", headers=_auth("owner"))

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "workstream_lease_held"
    assert get_storage().get_workstream(ws_id) is not None


def test_holder_delete_releases_the_lease(app_client: Any) -> None:
    client, manager = app_client
    ws_id = client.post("/v1/api/workstreams/new", json={}, headers=_auth("owner")).json()["ws_id"]
    assert get_storage().get_workstream(ws_id)["lease_node_id"] == manager._node_id

    response = client.post(f"/v1/api/workstreams/{ws_id}/delete", headers=_auth("owner"))

    assert response.status_code == 200, response.text
    assert get_storage().get_workstream(ws_id) is None
    assert manager.lease_fence(ws_id) is None


def test_detail_refuses_to_reopen_a_workstream_owned_elsewhere(
    app_client: Any, monkeypatch: Any
) -> None:
    client, manager = app_client
    ws_id = _owned_elsewhere(client, manager)
    storage = get_storage()

    def _refuse(ws: str, **_kwargs: Any) -> Any:
        raise WorkstreamLeaseHeldError(ws, holder_node_id="node-elsewhere", retry_after_ms=900)

    # SQLite takes a live lease over by contract; model PostgreSQL's refusal.
    monkeypatch.setattr(storage, "acquire_workstream_lease", _refuse)

    response = client.get(f"/v1/api/workstreams/{ws_id}", headers=_auth("owner"))

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "workstream_lease_held"
    assert manager.get(ws_id) is None


def test_title_answers_409_when_the_lease_refuses_the_alias(
    app_client: Any, monkeypatch: Any
) -> None:
    client, manager = app_client
    ws_id = client.post("/v1/api/workstreams/new", json={}, headers=_auth("owner")).json()["ws_id"]
    storage = get_storage()

    def _refuse(ws: str, alias: str, **_kwargs: Any) -> bool:
        raise WorkstreamLeaseHeldError(ws, holder_node_id="node-elsewhere")

    monkeypatch.setattr(storage, "set_workstream_alias", _refuse)

    response = client.post(
        f"/v1/api/workstreams/{ws_id}/title", json={"title": "Renamed"}, headers=_auth("owner")
    )

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "workstream_lease_held"
    manager.close(ws_id)


def test_close_releases_the_lease(app_client: Any) -> None:
    client, manager = app_client
    ws_id = client.post("/v1/api/workstreams/new", json={}, headers=_auth("owner")).json()["ws_id"]
    assert get_storage().get_workstream(ws_id)["lease_node_id"] == manager._node_id

    response = client.post(f"/v1/api/workstreams/{ws_id}/close", json={}, headers=_auth("owner"))

    assert response.status_code == 200, response.text
    assert get_storage().get_workstream(ws_id)["lease_node_id"] is None
    assert manager.lease_fence(ws_id) is None


def test_concurrent_opens_run_post_load_and_audit_once() -> None:
    """The open that loses the race answers ``already_loaded`` instead of replaying."""
    from tests.test_session_manager import FakeAdapter, FakeStorage, _make_manager
    from tests.test_workstream_endpoints import _InjectAuthMiddleware
    from turnstone.core.session_routes import SessionEndpointConfig, make_open_handler

    storage = FakeStorage()
    adapter = FakeAdapter()
    mgr, _, _ = _make_manager(adapter, storage=storage)
    ws = mgr.create(user_id="u1")
    assert mgr.close(ws.id) is True
    acquire = storage.acquire_workstream_lease

    def slow_acquire(*args: Any, **kwargs: Any) -> Any:
        # Database latency before the slot is installed widens the race.
        time.sleep(0.3)
        return acquire(*args, **kwargs)

    storage.acquire_workstream_lease = slow_acquire  # type: ignore[method-assign]
    post_loads: list[str] = []
    audits: list[str] = []
    cfg = SessionEndpointConfig(
        permission_gate=None,
        manager_lookup=lambda _request: (mgr, None),
        tenant_check=None,
        not_found_label="Workstream not found",
        audit_action_prefix="workstream",
        open_resolve_alias=None,
        open_post_load=lambda _request, loaded: post_loads.append(loaded.id),
    )
    handler = make_open_handler(cfg, audit_emit=lambda _request, loaded: audits.append(loaded.id))
    app = Starlette(
        routes=[Route("/v1/api/workstreams/{ws_id}/open", handler, methods=["POST"])],
        middleware=[Middleware(_InjectAuthMiddleware)],
    )

    async def both() -> list[Any]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            return await asyncio.gather(
                client.post(f"/v1/api/workstreams/{ws.id}/open"),
                client.post(f"/v1/api/workstreams/{ws.id}/open"),
            )

    responses = asyncio.run(both())

    assert [r.status_code for r in responses] == [200, 200]
    assert sum(1 for r in responses if r.json().get("already_loaded")) == 1
    assert post_loads == [ws.id]
    assert audits == [ws.id]


def test_every_host_starts_its_lease_keeper() -> None:
    """A host that never starts its keeper loses every lease after the TTL."""
    import inspect

    from turnstone import cli, server
    from turnstone.console import server as console_server

    assert "app.state.workstreams.start_lease_keeper()" in inspect.getsource(server._lifespan)
    # Before a startup --resume takes leases, long before the lifespan runs.
    assert "manager.start_lease_keeper()" in inspect.getsource(server.main)
    assert "coord_mgr.start_lease_keeper()" in inspect.getsource(console_server)
    assert "manager.start_lease_keeper()" in inspect.getsource(cli.main)


def test_a_lease_refusal_answers_409_and_names_the_holder_only_when_known() -> None:
    import json

    from turnstone.core.web_helpers import lease_refusal_response

    held = lease_refusal_response(
        WorkstreamLeaseHeldError("ws-1", holder_node_id="node-b", retry_after_ms=1500)
    )
    lost = lease_refusal_response(WorkstreamLeaseLostError("ws-1"))

    assert (held.status_code, lost.status_code) == (409, 409)
    held_body, lost_body = json.loads(held.body), json.loads(lost.body)
    assert held_body["code"] == lost_body["code"] == "workstream_lease_held"
    assert (held_body["holder_node_id"], held_body["retry_after_ms"]) == ("node-b", 1500)
    assert (lost_body["holder_node_id"], lost_body["retry_after_ms"]) == ("", 0)
    assert lost_body["error"]


@pytest.mark.parametrize(
    ("raised", "status", "code"),
    [
        (lambda: WorkstreamLeaseLostError("ws"), 409, "workstream_lease_held"),
        (
            lambda: WorkstreamLeaseHeldError("ws", holder_node_id="node-b"),
            409,
            "workstream_lease_held",
        ),
        (lambda: RuntimeError("All 1 slots are active"), 429, None),
        (lambda: SessionFactoryLeaseError(), 503, None),
        (lambda: WorkstreamHistoryUnavailableError("ws"), 503, None),
    ],
    ids=["lease-lost", "lease-held", "capacity", "factory-dropped-lease", "settings-unreadable"],
)
def test_create_answers_each_failure_with_its_own_status(
    raised: Any, status: int, code: str | None
) -> None:
    """Lease refusals and unreadable settings are RuntimeErrors too: their arms stay above the
    capacity arm."""
    from unittest.mock import MagicMock

    from starlette.testclient import TestClient

    from turnstone.core.session_routes import SessionEndpointConfig, make_create_handler

    mgr = MagicMock()
    mgr.create.side_effect = raised()
    cfg = SessionEndpointConfig(
        permission_gate=None,
        manager_lookup=lambda _request: (mgr, None),
        tenant_check=None,
        not_found_label="Workstream not found",
        audit_action_prefix="workstream",
        create_build_kwargs=lambda *_args: {"user_id": "u1"},
    )
    app = Starlette(routes=[Route("/new", make_create_handler(cfg), methods=["POST"])])

    r = TestClient(app).post("/new", json={})

    assert r.status_code == status
    if code is not None:
        assert r.json()["code"] == code


def test_closing_a_copy_another_process_took_answers_404_without_an_audit() -> None:
    """The console re-routes a 404 to the holder, where the workstream really closes."""
    from unittest.mock import MagicMock

    from starlette.testclient import TestClient

    from turnstone.core.session_manager import CloseOutcome
    from turnstone.core.session_routes import SessionEndpointConfig, make_close_handler

    mgr = MagicMock()
    mgr.close_with_outcome.return_value = CloseOutcome.OWNED_ELSEWHERE
    audit = MagicMock()
    cfg = SessionEndpointConfig(
        permission_gate=None,
        manager_lookup=lambda _request: (mgr, None),
        tenant_check=None,
        not_found_label="Workstream not found",
        audit_action_prefix="workstream",
    )
    app = Starlette(
        routes=[
            Route(
                "/ws/{ws_id}/close",
                make_close_handler(cfg, audit_emit=audit),
                methods=["POST"],
            )
        ]
    )
    app.state.auth_storage = MagicMock()

    r = TestClient(app).post("/ws/ws-taken/close", json={})

    assert r.status_code == 404
    audit.assert_not_called()
