"""Console behavior around workstream owner leases.

A node refuses (409 ``workstream_lease_held``) to open or delete a workstream
another node owns; the delete proxy re-routes once, and the router prefers the
owner. The console deletes coordinators itself, because it holds their leases.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route
from starlette.testclient import TestClient

from tests._coord_test_helpers import _AuthMiddleware, _build_mgr
from tests.test_console_routing_proxy import (
    _TEST_AUTH_HEADERS,
    _make_app,
    _make_mock_router,
    _wire_proxy,
)
from turnstone.console.router import NodeRef
from turnstone.console.server import route_workstream_delete
from turnstone.core.storage import WorkstreamLeaseHeldError

_HELD = WorkstreamLeaseHeldError("ws", holder_node_id="node-b", retry_after_ms=1500).as_dict()
_COORD_HEADERS = {"X-Test-User": "user-1", "X-Test-Perms": "admin.coordinator"}


def _scripted_post(*responses: tuple[int, dict[str, Any]]) -> Any:
    from unittest.mock import MagicMock

    remaining = list(responses)

    async def _post(*args: Any, **kwargs: Any) -> httpx.Response:
        status, body = remaining.pop(0)
        return httpx.Response(
            status, json=body, request=httpx.Request("POST", args[0] if args else "http://t")
        )

    return MagicMock(side_effect=_post)


def _post(app: Any, path: str, body: dict[str, Any]) -> Any:
    """POST without running the app lifespan, which would replace the stubbed proxy."""
    client = TestClient(app, raise_server_exceptions=False)
    try:
        return client.post(path, json=body, headers=_TEST_AUTH_HEADERS)
    finally:
        client.close()


def _upstream_urls(app: Any, *, method: str) -> list[str]:
    client = app.state.proxy_client
    if method == "request":
        return [call.args[1] for call in client.request.call_args_list]
    return [call.args[0] for call in client.post.call_args_list]


# -- routing proxy -------------------------------------------------------------


def test_verb_proxy_passes_a_lease_refusal_through() -> None:
    """Proxied verbs act on a loaded workstream and answer 404 off-owner; a 409
    is not something they re-route on."""
    router = _make_mock_router()
    router.route.side_effect = [
        NodeRef("node-a", "http://a:8080"),
        NodeRef("node-b", "http://b:8080"),
    ]
    app = _make_app(router=router)
    _wire_proxy(app, _scripted_post((409, _HELD)))
    resp = _post(app, "/v1/api/route/workstreams/moved/send", {"message": "hi"})

    assert resp.status_code == 409
    assert resp.json()["code"] == "workstream_lease_held"
    assert len(_upstream_urls(app, method="request")) == 1
    router.force_refresh.assert_not_called()


# -- delete proxy ----------------------------------------------------------------


@pytest.mark.parametrize("holder_known", [True, False])
def test_delete_proxy_retries_once_at_the_lease_holder(holder_known: bool) -> None:
    router = _make_mock_router()
    router.knows_node.return_value = holder_known
    router.route.side_effect = [
        NodeRef("node-a", "http://a:8080"),
        NodeRef("node-b", "http://b:8080"),
    ]
    app = _make_app(router=router)
    _wire_proxy(app, _scripted_post((409, _HELD), (200, {"deleted": "moved"})))
    resp = _post(app, "/v1/api/route/workstreams/delete", {"ws_id": "moved"})

    assert resp.status_code == 200
    assert _upstream_urls(app, method="post") == [
        "http://a:8080/v1/api/workstreams/moved/delete",
        "http://b:8080/v1/api/workstreams/moved/delete",
    ]
    # route() re-reads the holder from the row; only an unknown node needs a refresh.
    assert router.force_refresh.call_count == (0 if holder_known else 1)


def test_delete_proxy_keeps_a_refusal_that_names_the_refusing_node() -> None:
    router = _make_mock_router()
    held_here = WorkstreamLeaseHeldError("ws", holder_node_id="node-a").as_dict()
    app = _make_app(router=router)
    _wire_proxy(app, _scripted_post((409, held_here)))
    resp = _post(app, "/v1/api/route/workstreams/delete", {"ws_id": "moved"})

    assert resp.status_code == 409
    assert len(_upstream_urls(app, method="post")) == 1
    router.force_refresh.assert_not_called()
    assert router.route.call_count == 1


def test_delete_proxy_returns_the_refusal_when_the_route_does_not_move() -> None:
    router = _make_mock_router()
    app = _make_app(router=router)
    _wire_proxy(app, _scripted_post((409, _HELD)))
    resp = _post(app, "/v1/api/route/workstreams/delete", {"ws_id": "moved"})

    assert resp.status_code == 409
    assert resp.json()["code"] == "workstream_lease_held"
    assert len(_upstream_urls(app, method="post")) == 1


@pytest.mark.parametrize("ws_id", ["../memories?", "a/b", "..", "x#y", "", 7])
def test_a_delete_id_that_could_change_the_upstream_path_is_refused(ws_id: Any) -> None:
    router = _make_mock_router()
    app = _make_app(router=router)
    _wire_proxy(app, _scripted_post())
    resp = _post(app, "/v1/api/route/workstreams/delete", {"ws_id": ws_id})

    assert resp.status_code == 400
    assert _upstream_urls(app, method="post") == []
    router.route.assert_not_called()


def test_delete_proxy_does_not_retry_other_conflicts() -> None:
    router = _make_mock_router()
    router.route.side_effect = [
        NodeRef("node-a", "http://a:8080"),
        NodeRef("node-b", "http://b:8080"),
    ]
    app = _make_app(router=router)
    _wire_proxy(app, _scripted_post((409, {"error": "busy", "code": "something_else"})))
    resp = _post(app, "/v1/api/route/workstreams/delete", {"ws_id": "moved"})

    assert resp.status_code == 409
    assert len(_upstream_urls(app, method="post")) == 1
    router.force_refresh.assert_not_called()


# -- coordinator delete on the console -------------------------------------------


@pytest.fixture
def coord_storage(tmp_path: Any) -> Any:
    from turnstone.core.storage import init_storage, reset_storage

    reset_storage()
    backend = init_storage("sqlite", path=str(tmp_path / "coord.db"), run_migrations=False)
    yield backend
    reset_storage()


def _coord_delete_client(storage: Any, mgr: Any) -> TestClient:
    app = Starlette(
        routes=[
            Route(
                "/v1/api/route/workstreams/delete",
                route_workstream_delete,
                methods=["POST"],
            )
        ],
        middleware=[Middleware(_AuthMiddleware)],
    )
    app.state.coord_mgr = mgr
    app.state.router = None
    app.state.auth_storage = storage
    return TestClient(app, raise_server_exceptions=False)


def test_console_deletes_a_loaded_coordinator_through_its_manager(coord_storage: Any) -> None:
    mgr = _build_mgr(coord_storage)
    ws = mgr.create(user_id="user-1", name="doomed")
    client = _coord_delete_client(coord_storage, mgr)

    resp = client.post(
        "/v1/api/route/workstreams/delete", json={"ws_id": ws.id}, headers=_COORD_HEADERS
    )

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"deleted": ws.id}
    assert mgr.get(ws.id) is None
    assert coord_storage.get_workstream(ws.id) is None


def test_a_console_coordinator_delete_writes_both_audit_records(coord_storage: Any) -> None:
    mgr = _build_mgr(coord_storage)
    ws = mgr.create(user_id="user-1", name="audited")
    client = _coord_delete_client(coord_storage, mgr)

    resp = client.post(
        "/v1/api/route/workstreams/delete", json={"ws_id": ws.id}, headers=_COORD_HEADERS
    )

    assert resp.status_code == 200, resp.text
    deleted = coord_storage.list_audit_events(action="workstream.deleted", resource_id=ws.id)
    routed = coord_storage.list_audit_events(action="route.workstream.delete", resource_id=ws.id)
    assert len(deleted) == 1
    assert len(routed) == 1
    assert "console" in str(routed[0].get("detail"))


def test_console_deletes_an_unloaded_coordinator(coord_storage: Any) -> None:
    mgr = _build_mgr(coord_storage)
    ws = mgr.create(user_id="user-1", name="saved")
    assert mgr.close(ws.id)
    client = _coord_delete_client(coord_storage, mgr)

    resp = client.post(
        "/v1/api/route/workstreams/delete", json={"ws_id": ws.id}, headers=_COORD_HEADERS
    )

    assert resp.status_code == 200, resp.text
    assert coord_storage.get_workstream(ws.id) is None


def test_console_refuses_a_coordinator_another_console_owns(coord_storage: Any) -> None:
    mgr = _build_mgr(coord_storage)
    other = _build_mgr(coord_storage)
    ws = mgr.create(user_id="user-1", name="busy")
    assert mgr.close(ws.id)
    # Another console reopened it. (SQLite lets it take the lease over; the
    # delete below is unfenced here, so it is refused on both dialects.)
    assert other.open(ws.id) is not None
    client = _coord_delete_client(coord_storage, mgr)

    resp = client.post(
        "/v1/api/route/workstreams/delete", json={"ws_id": ws.id}, headers=_COORD_HEADERS
    )

    assert resp.status_code == 409
    assert resp.json()["code"] == "workstream_lease_held"
    assert coord_storage.get_workstream(ws.id) is not None
    other.release_leases()


@pytest.mark.parametrize("loaded", [True, False])
def test_read_scoped_token_cannot_delete_a_coordinator(coord_storage: Any, loaded: bool) -> None:
    """Through the real console app and AuthMiddleware, not the all-scopes test middleware."""
    from unittest.mock import MagicMock

    from turnstone.console.collector import ClusterCollector
    from turnstone.console.server import _load_static, create_app
    from turnstone.core.auth import JWT_AUD_CONSOLE, create_jwt

    secret = "lease-routing-test-secret-at-least-32-chars"
    mgr = _build_mgr(coord_storage)
    ws = mgr.create(user_id="admin", name="guarded")
    if not loaded:
        assert mgr.close(ws.id)
    _load_static()
    app = create_app(
        collector=MagicMock(spec=ClusterCollector),
        jwt_secret=secret,
        auth_storage=coord_storage,
        router=None,
    )
    app.state.coord_mgr = mgr
    token = create_jwt(
        user_id="admin",
        scopes=frozenset({"read"}),
        permissions=frozenset({"admin.coordinator"}),
        source="test",
        secret=secret,
        audience=JWT_AUD_CONSOLE,
    )
    client = TestClient(app, raise_server_exceptions=False)
    try:
        resp = client.post(
            "/v1/api/route/workstreams/delete",
            json={"ws_id": ws.id},
            headers={"Authorization": f"Bearer {token}"},
        )
    finally:
        client.close()
        mgr.release_leases()

    assert resp.status_code == 403
    assert coord_storage.get_workstream(ws.id) is not None


@pytest.mark.parametrize("perms", ["admin.coordinator", "read"])
def test_an_invisible_coordinator_answers_404_before_the_permission_check(
    coord_storage: Any, monkeypatch: pytest.MonkeyPatch, perms: str
) -> None:
    from turnstone.core.auth import WorkstreamProjectVisibility

    mgr = _build_mgr(coord_storage)
    ws = mgr.create(user_id="user-1", name="private")
    monkeypatch.setattr(WorkstreamProjectVisibility, "ws_visible", lambda *_a, **_k: False)
    client = _coord_delete_client(coord_storage, mgr)

    resp = client.post(
        "/v1/api/route/workstreams/delete",
        json={"ws_id": ws.id},
        headers={"X-Test-User": "user-1", "X-Test-Perms": perms},
    )

    assert resp.status_code == 404
    assert resp.json() == {"error": "Workstream not found"}
    assert coord_storage.get_workstream(ws.id) is not None
    mgr.release_leases()


def test_an_interactive_delete_skips_the_console_snapshot(
    coord_storage: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a coordinator needs the locking snapshot; others go to routing."""
    assert coord_storage.register_workstream("interactive-row", user_id="user-1") is True
    snapshots: list[str] = []
    real_snapshot = coord_storage.ensure_workstream_incarnation_snapshot

    def _spy(ws_id: str) -> Any:
        snapshots.append(ws_id)
        return real_snapshot(ws_id)

    monkeypatch.setattr(coord_storage, "ensure_workstream_incarnation_snapshot", _spy)
    client = _coord_delete_client(coord_storage, _build_mgr(coord_storage))

    resp = client.post(
        "/v1/api/route/workstreams/delete",
        json={"ws_id": "interactive-row"},
        headers=_COORD_HEADERS,
    )

    # No router in this app: the delete reached the routing step.
    assert resp.status_code == 503
    assert snapshots == []
    assert coord_storage.get_workstream("interactive-row") is not None


def test_an_unknown_id_answers_404_on_the_console(coord_storage: Any) -> None:
    """Storage is shared, so there is nothing to delete anywhere; the answer
    matches an invisible coordinator's whether or not any node is live."""
    client = _coord_delete_client(coord_storage, _build_mgr(coord_storage))

    resp = client.post(
        "/v1/api/route/workstreams/delete",
        json={"ws_id": "no-such-row"},
        headers=_COORD_HEADERS,
    )

    assert resp.status_code == 404
    assert resp.json() == {"error": "Workstream not found"}


def test_an_interactive_delete_hands_its_row_to_the_router(coord_storage: Any) -> None:
    assert coord_storage.register_workstream("interactive-row", user_id="user-1") is True
    client = _coord_delete_client(coord_storage, _build_mgr(coord_storage))
    router = _make_mock_router()
    client.app.state.router = router  # type: ignore[attr-defined]
    _wire_proxy(client.app, _scripted_post((200, {"deleted": "interactive-row"})))

    resp = client.post(
        "/v1/api/route/workstreams/delete",
        json={"ws_id": "interactive-row"},
        headers=_COORD_HEADERS,
    )

    assert resp.status_code == 200
    assert router.route.call_args.kwargs["known_row"]["ws_id"] == "interactive-row"


def test_console_coordinator_delete_requires_admin_coordinator(coord_storage: Any) -> None:
    mgr = _build_mgr(coord_storage)
    ws = mgr.create(user_id="user-1", name="guarded")
    client = _coord_delete_client(coord_storage, mgr)

    resp = client.post(
        "/v1/api/route/workstreams/delete",
        json={"ws_id": ws.id},
        headers={"X-Test-User": "user-1", "X-Test-Perms": "read"},
    )

    assert resp.status_code == 403
    assert mgr.get(ws.id) is ws
