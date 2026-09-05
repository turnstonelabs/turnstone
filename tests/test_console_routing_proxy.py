"""Tests for console routing proxy endpoints (route_create, route_proxy, route_lookup)."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from starlette.testclient import TestClient

from turnstone.console.collector import ClusterCollector
from turnstone.console.router import ConsoleRouter, NodeRef
from turnstone.core.rendezvous import NoAvailableNodeError

# Shared test auth — JWT-based
_TEST_JWT_SECRET = "test-jwt-secret-minimum-32-chars!"


def _test_jwt() -> str:
    from turnstone.core.auth import JWT_AUD_CONSOLE, create_jwt

    return create_jwt(
        user_id="test-routing",
        scopes=frozenset({"read", "write", "approve", "service"}),
        source="test",
        secret=_TEST_JWT_SECRET,
        audience=JWT_AUD_CONSOLE,
    )


_TEST_AUTH_HEADERS: dict[str, str] = {"Authorization": f"Bearer {_test_jwt()}"}
_DEST_WS_ID = "a" * 32
_FORK_DEST_WS_ID = "b" * 32
_RETRY_DEST_WS_ID = "c" * 32

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_collector() -> MagicMock:
    collector = MagicMock(spec=ClusterCollector)
    collector.get_overview.return_value = {
        "nodes": 1,
        "workstreams": 0,
        "states": {"running": 0, "thinking": 0, "attention": 0, "idle": 0, "error": 0},
        "aggregate": {"total_tokens": 0, "total_tool_calls": 0},
    }
    return collector


def _make_mock_router(ready: bool = True) -> MagicMock:
    router = MagicMock(spec=ConsoleRouter)
    router.is_ready.return_value = ready
    if not ready:
        router.route.side_effect = NoAvailableNodeError("no live nodes")
    router.route.return_value = NodeRef("node-a", "http://a:8080")
    router.node_count.return_value = 2
    router.rendezvous_node.return_value = NodeRef("node-b", "http://b:8080")
    router.required_node.side_effect = lambda node_id: NodeRef(
        node_id, f"http://{node_id.removeprefix('node-')}:8080"
    )
    return router


def _make_app(
    collector: Any = None,
    router: Any = None,
    auth_storage: Any = None,
) -> Any:
    from turnstone.console.server import _load_static, create_app

    _load_static()
    return create_app(
        collector=collector or _make_mock_collector(),
        jwt_secret=_TEST_JWT_SECRET,
        router=router,
        auth_storage=auth_storage,
    )


def _make_proxy_post(
    status_code: int = 200,
    json_data: dict[str, Any] | None = None,
) -> MagicMock:
    """Create a mock for httpx.AsyncClient.post that returns a fixed response."""
    data = json_data or {"ws_id": "abc123", "name": "test"}

    async def _mock_post(*args: Any, **kwargs: Any) -> httpx.Response:
        return httpx.Response(
            status_code,
            json=data,
            request=httpx.Request("POST", args[0] if args else "http://test"),
        )

    mock_post = MagicMock(side_effect=_mock_post)
    return mock_post


def _wire_proxy(app: Any, mock_post: MagicMock | None = None) -> None:
    """Attach a mock proxy_client to the app (lifespan doesn't run in TestClient)."""
    if mock_post is None:
        mock_post = _make_proxy_post()
    mock_proxy = MagicMock(spec=httpx.AsyncClient)
    mock_proxy.post = mock_post

    # route_proxy uses ``client.request(method, url, ...)`` for path-keyed
    # routes (so DELETE on /send proxies through correctly). Wire a
    # request-shim that drops the leading method positional and forwards
    # to the same mock_post for compatibility.
    async def _request_shim(method: str, *args: Any, **kwargs: Any) -> httpx.Response:
        return await mock_post(*args, **kwargs)

    mock_proxy.request = MagicMock(side_effect=_request_shim)
    app.state.proxy_client = mock_proxy


def _wire_proxy_get(
    app: Any,
    *,
    status_code: int = 200,
    json_data: dict[str, Any] | None = None,
    raw_content: bytes | None = None,
) -> MagicMock:
    """Attach a proxy client whose GET returns one deterministic response."""

    async def _mock_get(*args: Any, **kwargs: Any) -> httpx.Response:
        request = httpx.Request("GET", args[0] if args else "http://test")
        if raw_content is not None:
            return httpx.Response(status_code, content=raw_content, request=request)
        return httpx.Response(status_code, json=json_data or {}, request=request)

    mock_get = MagicMock(side_effect=_mock_get)
    mock_proxy = MagicMock(spec=httpx.AsyncClient)
    mock_proxy.get = mock_get
    app.state.proxy_client = mock_proxy
    return mock_get


# ---------------------------------------------------------------------------
# Tests — route_create
# ---------------------------------------------------------------------------


class TestRouteCreate:
    """POST /v1/api/route/workstreams/new — create via rendezvous routing."""

    @pytest.fixture()
    def client(self):
        router = _make_mock_router()
        app = _make_app(router=router)
        _wire_proxy(app, _make_proxy_post(json_data={"ws_id": _DEST_WS_ID, "name": "test"}))
        client = TestClient(app, raise_server_exceptions=False)
        yield client
        client.close()

    def test_route_create_proxies_to_node(self, client):
        resp = client.post(
            "/v1/api/route/workstreams/new",
            json={"name": "test-ws"},
            headers=_TEST_AUTH_HEADERS,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ws_id"] == _DEST_WS_ID

    def test_route_create_injects_node_url(self, client):
        resp = client.post(
            "/v1/api/route/workstreams/new",
            json={"name": "test-ws"},
            headers=_TEST_AUTH_HEADERS,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["node_url"] == "http://a:8080"
        assert data["node_id"] == "node-a"

    def test_route_create_resume_ws(self):
        """resume_ws should route to the node that owns the old workstream."""
        router = _make_mock_router()
        router.route.return_value = NodeRef("node-b", "http://b:8080")
        storage = MagicMock()
        storage.resolve_workstream.return_value = "d" * 32
        storage.get_workstream.return_value = {"ws_id": "d" * 32, "state": "idle"}
        app = _make_app(router=router, auth_storage=storage)
        _wire_proxy(
            app,
            _make_proxy_post(json_data={"ws_id": _FORK_DEST_WS_ID, "name": "resumed"}),
        )
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.post(
            "/v1/api/route/workstreams/new",
            json={"resume_ws": "saved-alias"},
            headers=_TEST_AUTH_HEADERS,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["node_url"] == "http://b:8080"
        assert data["node_id"] == "node-b"
        storage.resolve_workstream.assert_called_once_with("saved-alias")
        assert router.route.call_args.args == ("d" * 32,)
        router.remember_override.assert_called_once_with(
            _FORK_DEST_WS_ID,
            NodeRef("node-b", "http://b:8080"),
        )
        assert app.state.proxy_client.post.call_args.kwargs["json"]["resume_ws"] == "d" * 32
        assert app.state.proxy_client.post.call_args.kwargs["json"]["resume_ws_exact"] is True
        client.close()

    def test_route_create_target_node(self):
        """An explicit target is persisted without constraining the ID hash."""
        router = _make_mock_router()
        router.route.return_value = NodeRef("node-c", "http://c:8080")
        app = _make_app(router=router)
        _wire_proxy(
            app,
            _make_proxy_post(json_data={"ws_id": "00ff" + "0" * 28, "name": "pinned"}),
        )
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.post(
            "/v1/api/route/workstreams/new",
            json={"target_node": "node-c"},
            headers=_TEST_AUTH_HEADERS,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["node_id"] == "node-c"
        router.required_node.assert_called_with("node-c")
        assert app.state.proxy_client.post.call_args.kwargs["json"]["required_node_id"] == "node-c"
        client.close()

    def test_route_create_routing_strategy_rendezvous(self, client):
        """Default fan-out (no resume_ws / no target_node) reports
        routing_strategy='rendezvous' so the coordinator's spawn tool
        can explain why the node was chosen."""
        resp = client.post(
            "/v1/api/route/workstreams/new",
            json={"name": "test-ws"},
            headers=_TEST_AUTH_HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["routing_strategy"] == "rendezvous"

    def test_route_create_routing_strategy_target_node(self):
        router = _make_mock_router()
        router.route.return_value = NodeRef("node-c", "http://c:8080")
        app = _make_app(router=router)
        _wire_proxy(app, _make_proxy_post(json_data={"ws_id": "00ff" + "0" * 28, "name": "pinned"}))
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/v1/api/route/workstreams/new",
            json={"target_node": "node-c"},
            headers=_TEST_AUTH_HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["routing_strategy"] == "target_node"
        client.close()

    def test_route_create_routing_strategy_resume(self):
        router = _make_mock_router()
        router.route.return_value = NodeRef("node-b", "http://b:8080")
        storage = MagicMock()
        storage.resolve_workstream.return_value = "d" * 32
        storage.get_workstream.return_value = {"ws_id": "d" * 32, "state": "idle"}
        app = _make_app(router=router, auth_storage=storage)
        _wire_proxy(
            app,
            _make_proxy_post(json_data={"ws_id": _FORK_DEST_WS_ID, "name": "resumed"}),
        )
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/v1/api/route/workstreams/new",
            json={"resume_ws": "source-alias"},
            headers=_TEST_AUTH_HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["routing_strategy"] == "resume"
        client.close()

    @pytest.mark.anyio
    async def test_python_sdk_decodes_live_route_create_response(self):
        """Exercise the SDK against the actual ASGI route, not a mock transport."""
        from turnstone.sdk.console import AsyncTurnstoneConsole

        router = _make_mock_router()
        app = _make_app(router=router)
        _wire_proxy(app, _make_proxy_post(json_data={"ws_id": _DEST_WS_ID, "name": "sdk"}))
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            headers=_TEST_AUTH_HEADERS,
        ) as http_client:
            sdk = AsyncTurnstoneConsole(httpx_client=http_client)
            result = await sdk.route_create_workstream(name="sdk")

        assert result.ws_id == _DEST_WS_ID
        assert result.node_id == "node-a"
        assert result.node_url == "http://a:8080"
        assert result.routing_strategy == "rendezvous"

    @pytest.mark.parametrize("payload", [[], "text", 7])
    def test_route_create_rejects_non_object_json(self, client, payload):
        resp = client.post(
            "/v1/api/route/workstreams/new",
            json=payload,
            headers=_TEST_AUTH_HEADERS,
        )
        assert resp.status_code == 400
        assert resp.json() == {"error": "Request body must be a JSON object"}

    def test_route_create_rejects_json_null(self, client):
        resp = client.post(
            "/v1/api/route/workstreams/new",
            content=b"null",
            headers={**_TEST_AUTH_HEADERS, "Content-Type": "application/json"},
        )
        assert resp.status_code == 400
        assert resp.json() == {"error": "Request body must be a JSON object"}

    @pytest.mark.parametrize(
        ("field", "value", "error"),
        [
            ("resume_ws", 3, "resume_ws must be a string"),
            ("resume_ws_exact", "true", "resume_ws_exact must be a boolean"),
            ("resume_ws_exact", True, "resume_ws_exact requires resume_ws"),
            ("resume_ws", "x" * 257, "resume_ws must be at most 256 characters"),
            ("target_node", ["node-a"], "target_node must be a string"),
            ("target_node", "bad/node", "invalid target_node format"),
            ("ws_id", None, "ws_id must be a string"),
            ("ws_id", "abc", "invalid ws_id format"),
        ],
    )
    def test_route_create_validates_placement_field_shapes(self, client, field, value, error):
        resp = client.post(
            "/v1/api/route/workstreams/new",
            json={field: value},
            headers=_TEST_AUTH_HEADERS,
        )
        assert resp.status_code == 400
        assert resp.json() == {"error": error}

    def test_explicit_json_ws_id_is_preserved_with_required_node(self):
        router = _make_mock_router()
        app = _make_app(router=router)
        post = _make_proxy_post(json_data={"ws_id": _DEST_WS_ID, "name": "fixed"})
        _wire_proxy(app, post)
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.post(
            "/v1/api/route/workstreams/new",
            json={"ws_id": _DEST_WS_ID, "target_node": "node-a"},
            headers=_TEST_AUTH_HEADERS,
        )
        client.close()

        assert resp.status_code == 200
        assert resp.json()["routing_strategy"] == "target_node"
        router.required_node.assert_called_once_with("node-a")
        router.route.assert_not_called()
        assert post.call_args.kwargs["json"]["ws_id"] == _DEST_WS_ID

    def test_resume_alias_missing_and_storage_uncertainty_are_bounded(self):
        router = _make_mock_router()
        storage = MagicMock()
        storage.resolve_workstream.return_value = None
        app = _make_app(router=router, auth_storage=storage)
        _wire_proxy(app)
        client = TestClient(app, raise_server_exceptions=False)

        missing = client.post(
            "/v1/api/route/workstreams/new",
            json={"resume_ws": "missing-alias"},
            headers=_TEST_AUTH_HEADERS,
        )
        assert missing.status_code == 404
        assert missing.json() == {"error": "Workstream not found"}

        storage.resolve_workstream.side_effect = RuntimeError("database details")
        unavailable = client.post(
            "/v1/api/route/workstreams/new",
            json={"resume_ws": "source-alias"},
            headers=_TEST_AUTH_HEADERS,
        )
        client.close()
        assert unavailable.status_code == 503
        assert unavailable.json() == {"error": "Storage not available"}

    def test_full_resume_id_wins_over_alias_shadow(self):
        source_id = "a" * 32
        shadow_id = "b" * 32
        router = _make_mock_router()
        storage = MagicMock()
        storage.get_workstream.return_value = {"ws_id": source_id, "state": "idle"}
        storage.resolve_workstream.return_value = shadow_id
        app = _make_app(router=router, auth_storage=storage)
        post = _make_proxy_post(json_data={"ws_id": _FORK_DEST_WS_ID, "name": "fork"})
        _wire_proxy(app, post)
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.post(
            "/v1/api/route/workstreams/new",
            json={"resume_ws": source_id},
            headers=_TEST_AUTH_HEADERS,
        )
        client.close()

        assert resp.status_code == 200, resp.text
        storage.get_workstream.assert_any_call(source_id)
        storage.resolve_workstream.assert_not_called()
        assert router.route.call_count == 1
        assert router.route.call_args.args == (source_id,)
        assert post.call_args.kwargs["json"]["resume_ws"] == source_id

    @pytest.mark.anyio
    async def test_sdk_exact_resume_does_not_resolve_a_missing_id_as_an_alias(self):
        from turnstone.sdk._types import TurnstoneAPIError
        from turnstone.sdk.console import AsyncTurnstoneConsole

        storage = MagicMock()
        storage.get_workstream.return_value = None
        storage.resolve_workstream.return_value = "e" * 32
        app = _make_app(router=_make_mock_router(), auth_storage=storage)
        post = _make_proxy_post(json_data={"ws_id": _FORK_DEST_WS_ID})
        _wire_proxy(app, post)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers=_TEST_AUTH_HEADERS,
        ) as http_client:
            sdk = AsyncTurnstoneConsole(httpx_client=http_client)
            with pytest.raises(TurnstoneAPIError) as error:
                await sdk.route_create_workstream(resume_ws="d" * 32, resume_ws_exact=True)
        assert error.value.status_code == 404
        storage.resolve_workstream.assert_not_called()
        post.assert_not_called()

    @pytest.mark.parametrize(
        "upstream",
        [
            httpx.Response(200, content=b"not json"),
            httpx.Response(200, json=[]),
            httpx.Response(200, json={"name": "missing id"}),
            httpx.Response(200, json={"ws_id": "not-a-workstream-id"}),
            httpx.Response(200, json={"ws_id": _DEST_WS_ID}),
            httpx.Response(200, json={"ws_id": _DEST_WS_ID, "name": 42}),
        ],
    )
    def test_malformed_upstream_success_returns_bounded_502(self, upstream):
        router = _make_mock_router()
        app = _make_app(router=router)

        async def _post(*args: Any, **kwargs: Any) -> httpx.Response:
            upstream.request = httpx.Request("POST", args[0])
            return upstream

        mock_proxy = MagicMock(spec=httpx.AsyncClient)
        mock_proxy.post = MagicMock(side_effect=_post)
        app.state.proxy_client = mock_proxy
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/v1/api/route/workstreams/new",
            json={},
            headers=_TEST_AUTH_HEADERS,
        )
        client.close()

        assert resp.status_code == 502
        assert resp.json() == {"error": "Dispatch to node node-a failed"}

    def test_returned_destination_is_binding_and_audit_authority(self, monkeypatch):
        router = _make_mock_router()
        storage = MagicMock()
        storage.resolve_workstream.return_value = "d" * 32
        storage.get_workstream.return_value = {"ws_id": "d" * 32, "state": "idle"}
        storage.get_workstream.side_effect = lambda ws_id: {
            "ws_id": ws_id,
            "state": "idle",
            "node_id": "stored-node",
        }
        app = _make_app(router=router, auth_storage=storage)
        _wire_proxy(
            app,
            _make_proxy_post(json_data={"ws_id": _FORK_DEST_WS_ID, "name": "fork"}),
        )
        audit = MagicMock()
        monkeypatch.setattr("turnstone.console.server._emit_route_audit", audit)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/v1/api/route/workstreams/new",
            json={"resume_ws": "source-alias"},
            headers=_TEST_AUTH_HEADERS,
        )
        client.close()

        assert resp.status_code == 200
        assert resp.json()["node_id"] == "stored-node"
        storage.get_workstream.assert_any_call(_FORK_DEST_WS_ID)
        audit.assert_called_once()
        assert audit.call_args.args[2] == _FORK_DEST_WS_ID

    def test_multipart_preallocated_id_reports_rendezvous(self):
        router = _make_mock_router()
        app = _make_app(router=router)
        _wire_proxy(app, _make_proxy_post(json_data={"ws_id": _DEST_WS_ID, "name": "upload"}))
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            f"/v1/api/route/workstreams/new?ws_id={_DEST_WS_ID}",
            data={"meta": json.dumps({"ws_id": _DEST_WS_ID})},
            files={"file": ("a.txt", b"hello", "text/plain")},
            headers=_TEST_AUTH_HEADERS,
        )
        client.close()

        assert resp.status_code == 200
        assert resp.json()["routing_strategy"] == "rendezvous"
        assert router.route.call_count == 1
        assert router.route.call_args.args == (_DEST_WS_ID,)


class TestRouteCreate503Retry:
    """503 retry logic in route_create."""

    def test_route_create_503_retries_on_different_node(self):
        """If the first node returns 503, retry with a new ws_id targeting a different node."""
        router = _make_mock_router()
        call_count = 0

        def side_effect_route(ws_id: str, **kwargs: Any) -> NodeRef:
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                # First call returns node-a
                return NodeRef("node-a", "http://a:8080")
            # Subsequent calls return node-b (different node for retry)
            return NodeRef("node-b", "http://b:8080")

        router.route.side_effect = side_effect_route
        app = _make_app(router=router)

        post_count = 0

        async def _mock_post(*args: Any, **kwargs: Any) -> httpx.Response:
            nonlocal post_count
            post_count += 1
            if post_count == 1:
                return httpx.Response(
                    503,
                    json={"error": "overloaded"},
                    request=httpx.Request("POST", args[0] if args else "http://test"),
                )
            return httpx.Response(
                200,
                json={"ws_id": _RETRY_DEST_WS_ID, "name": "retry"},
                request=httpx.Request("POST", args[0] if args else "http://test"),
            )

        mock_proxy = MagicMock(spec=httpx.AsyncClient)
        mock_proxy.post = MagicMock(side_effect=_mock_post)
        app.state.proxy_client = mock_proxy
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.post(
            "/v1/api/route/workstreams/new",
            json={"name": "test-ws"},
            headers=_TEST_AUTH_HEADERS,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ws_id"] == _RETRY_DEST_WS_ID
        assert data["node_id"] == "node-b"
        assert post_count == 2
        client.close()

    def test_explicit_ws_id_is_not_replaced_or_retried_on_503(self):
        router = _make_mock_router()
        app = _make_app(router=router)
        post = _make_proxy_post(status_code=503, json_data={"error": "overloaded"})
        _wire_proxy(app, post)
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.post(
            "/v1/api/route/workstreams/new",
            json={"ws_id": _DEST_WS_ID},
            headers=_TEST_AUTH_HEADERS,
        )
        client.close()

        assert resp.status_code == 503
        assert post.call_count == 1
        assert post.call_args.kwargs["json"]["ws_id"] == _DEST_WS_ID
        assert router.route.call_count == 1
        assert router.route.call_args.args == (_DEST_WS_ID,)


class TestRouteCreate409Retry:
    """Generated destination ids retry live collisions at the router."""

    def test_wrong_executor_conflict_is_not_an_id_collision(self):
        router = _make_mock_router()
        app = _make_app(router=router)
        post = _make_proxy_post(
            status_code=409,
            json_data={
                "error": "This workstream must run on node 'host-1'.",
                "code": "wrong_execution_node",
                "required_node_id": "host-1",
            },
        )
        _wire_proxy(app, post)
        client = TestClient(app, raise_server_exceptions=False)
        try:
            response = client.post(
                "/v1/api/route/workstreams/new", json={}, headers=_TEST_AUTH_HEADERS
            )
        finally:
            client.close()
        assert response.status_code == 409
        assert response.json()["code"] == "wrong_execution_node"
        assert post.call_count == 1

    def test_generated_ws_id_collision_draws_another_id(self, monkeypatch):
        first_id = "1" * 32
        second_id = "2" * 32
        generated = MagicMock(side_effect=[first_id, second_id])
        monkeypatch.setattr("turnstone.console.server.secrets.token_hex", generated)
        router = _make_mock_router()
        app = _make_app(router=router)
        posted_ids: list[str] = []

        async def _mock_post(*args: Any, **kwargs: Any) -> httpx.Response:
            posted_ids.append(kwargs["json"]["ws_id"])
            status = 409 if len(posted_ids) == 1 else 200
            payload = (
                {"error": "Workstream already exists"}
                if status == 409
                else {"ws_id": second_id, "name": "retry"}
            )
            return httpx.Response(
                status,
                json=payload,
                request=httpx.Request("POST", args[0]),
            )

        _wire_proxy(app, MagicMock(side_effect=_mock_post))
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/v1/api/route/workstreams/new",
            json={"name": "generated"},
            headers=_TEST_AUTH_HEADERS,
        )
        client.close()

        assert resp.status_code == 200
        assert resp.json()["ws_id"] == second_id
        assert posted_ids == [first_id, second_id]
        assert generated.call_count == 2

    def test_target_node_collision_keeps_required_node(self, monkeypatch):
        first_id = "1" * 32
        second_id = "2" * 32
        router = _make_mock_router()
        generated = MagicMock(side_effect=[first_id, second_id])
        monkeypatch.setattr("turnstone.console.server.secrets.token_hex", generated)
        router.route.return_value = NodeRef("node-c", "http://c:8080")
        app = _make_app(router=router)
        posted_ids: list[str] = []

        async def _mock_post(*args: Any, **kwargs: Any) -> httpx.Response:
            posted_ids.append(kwargs["json"]["ws_id"])
            status = 409 if len(posted_ids) == 1 else 200
            payload = (
                {"error": "Workstream already exists"}
                if status == 409
                else {"ws_id": second_id, "name": "targeted-retry"}
            )
            return httpx.Response(
                status,
                json=payload,
                request=httpx.Request("POST", args[0]),
            )

        _wire_proxy(app, MagicMock(side_effect=_mock_post))
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/v1/api/route/workstreams/new",
            json={"target_node": "node-c"},
            headers=_TEST_AUTH_HEADERS,
        )
        client.close()

        assert resp.status_code == 200
        assert resp.json()["node_id"] == "node-c"
        assert posted_ids == [first_id, second_id]
        assert generated.call_count == 2
        assert router.required_node.call_count == 2
        assert all(call.args == ("node-c",) for call in router.required_node.call_args_list)

    def test_explicit_ws_id_collision_is_not_retried(self):
        router = _make_mock_router()
        app = _make_app(router=router)
        post = _make_proxy_post(
            status_code=409,
            json_data={"error": "Workstream already exists"},
        )
        _wire_proxy(app, post)
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.post(
            "/v1/api/route/workstreams/new",
            json={"ws_id": _DEST_WS_ID},
            headers=_TEST_AUTH_HEADERS,
        )
        client.close()

        assert resp.status_code == 409
        assert post.call_count == 1
        assert post.call_args.kwargs["json"]["ws_id"] == _DEST_WS_ID

    def test_generated_ws_id_collision_retry_is_bounded(self, monkeypatch):
        generated_ids = [f"{value:x}" * 32 for value in range(1, 5)]
        generated = MagicMock(side_effect=generated_ids)
        monkeypatch.setattr("turnstone.console.server.secrets.token_hex", generated)
        router = _make_mock_router()
        app = _make_app(router=router)
        post = _make_proxy_post(
            status_code=409,
            json_data={"error": "Workstream already exists"},
        )
        _wire_proxy(app, post)
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.post(
            "/v1/api/route/workstreams/new",
            json={"name": "generated"},
            headers=_TEST_AUTH_HEADERS,
        )
        client.close()

        assert resp.status_code == 409
        assert post.call_count == 4
        assert generated.call_count == 4


# ---------------------------------------------------------------------------
# Tests — cluster create (capacity-routed proxy)
# ---------------------------------------------------------------------------


class TestClusterCreate:
    """POST /v1/api/cluster/workstreams/new — the launcher's create proxy.

    Create-with-attachments rides multipart (a ``meta`` JSON field + ``file``
    parts) and must forward to the node AS multipart — not collapse to JSON,
    which would silently drop the files (the pre-fix behaviour, gated in the UI
    as "Attachments aren't supported for interactive sessions yet")."""

    def _app_with_node(self, mock_post: MagicMock) -> Any:
        collector = _make_mock_collector()
        collector.get_node_detail.return_value = {"server_url": "http://a:8080"}
        app = _make_app(collector=collector)
        _wire_proxy(app, mock_post)
        return app

    def test_cluster_create_json_forwards_json(self):
        mock_post = _make_proxy_post(json_data={"ws_id": "abc123"})
        client = TestClient(self._app_with_node(mock_post), raise_server_exceptions=False)
        resp = client.post(
            "/v1/api/cluster/workstreams/new",
            json={"node_id": "node-a", "name": "j", "initial_message": "hi"},
            headers=_TEST_AUTH_HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["correlation_id"] == "abc123"
        kwargs = mock_post.call_args.kwargs
        assert "json" in kwargs and "files" not in kwargs, "no-file create must stay JSON"
        assert kwargs["json"]["initial_message"] == "hi"
        client.close()

    def test_cluster_create_multipart_forwards_files(self):
        mock_post = _make_proxy_post(json_data={"ws_id": "withfile"})
        client = TestClient(self._app_with_node(mock_post), raise_server_exceptions=False)
        meta = {"node_id": "node-a", "name": "i", "initial_message": "describe"}
        resp = client.post(
            "/v1/api/cluster/workstreams/new",
            data={"meta": json.dumps(meta)},
            files={"file": ("a.txt", b"hello world", "text/plain")},
            headers=_TEST_AUTH_HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["correlation_id"] == "withfile"
        kwargs = mock_post.call_args.kwargs
        # Forwarded as multipart: a `meta` JSON field + `file` parts, never json=.
        assert "json" not in kwargs, "a multipart create must not collapse to JSON"
        assert kwargs.get("files"), "the blob must be forwarded to the node"
        forwarded_meta = json.loads(kwargs["data"]["meta"])
        assert forwarded_meta["initial_message"] == "describe"
        assert "user_id" in forwarded_meta, "the proxy must inject the owner uid"
        # The file part carries our blob unchanged: ("file", (name, bytes, ctype)).
        name, payload = kwargs["files"][0]
        assert name == "file"
        assert payload[0] == "a.txt" and payload[1] == b"hello world"
        client.close()

    def test_cluster_create_forwards_project_id(self) -> None:
        # Phase 6: the launcher's project picker sends project_id; the proxy
        # selectively REBUILDS the forwarded body (it doesn't pass it through),
        # so project_id must be explicitly carried or the node never scopes the
        # session to its project.
        mock_post = _make_proxy_post(json_data={"ws_id": "p1ws"})
        client = TestClient(self._app_with_node(mock_post), raise_server_exceptions=False)
        resp = client.post(
            "/v1/api/cluster/workstreams/new",
            json={"node_id": "node-a", "name": "j", "project_id": "proj-42"},
            headers=_TEST_AUTH_HEADERS,
        )
        assert resp.status_code == 200
        assert mock_post.call_args.kwargs["json"]["project_id"] == "proj-42"
        client.close()

    def test_cluster_create_forwards_persona(self) -> None:
        # The launcher's persona picker sends persona; the proxy selectively
        # REBUILDS the forwarded body (it doesn't pass it through), so persona
        # must be explicitly carried or the receiving node stamps its kind
        # default instead of the operator's choice.
        mock_post = _make_proxy_post(json_data={"ws_id": "p1ws"})
        client = TestClient(self._app_with_node(mock_post), raise_server_exceptions=False)
        resp = client.post(
            "/v1/api/cluster/workstreams/new",
            json={"node_id": "node-a", "name": "j", "persona": "scribe"},
            headers=_TEST_AUTH_HEADERS,
        )
        assert resp.status_code == 200
        assert mock_post.call_args.kwargs["json"]["persona"] == "scribe"
        client.close()

    @pytest.mark.anyio
    async def test_python_sdk_forwards_schema_contract_fields(self) -> None:
        """Run the SDK through the live handler and inspect its upstream request."""
        from turnstone.sdk.console import AsyncTurnstoneConsole

        mock_post = _make_proxy_post(json_data={"ws_id": "contract-ws"})
        app = self._app_with_node(mock_post)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            headers=_TEST_AUTH_HEADERS,
        ) as http_client:
            sdk = AsyncTurnstoneConsole(httpx_client=http_client)
            result = await sdk.create_workstream(
                node_id="node-a",
                name="contract",
                project_id="project-42",
                judge_model="judge-fast",
            )

        assert result.correlation_id == "contract-ws"
        forwarded = mock_post.call_args.kwargs["json"]
        assert forwarded["project_id"] == "project-42"
        assert forwarded["judge_model"] == "judge-fast"


# ---------------------------------------------------------------------------
# Tests — route_proxy
# ---------------------------------------------------------------------------


class TestRouteProxy:
    """POST /v1/api/route/workstreams/{ws_id}/<verb> (and the surviving
    body-keyed plan/command routes)."""

    @pytest.fixture()
    def client(self):
        router = _make_mock_router()
        app = _make_app(router=router)
        _wire_proxy(app, _make_proxy_post(json_data={"status": "ok"}))
        client = TestClient(app, raise_server_exceptions=False)
        yield client
        client.close()

    def test_route_proxy_send(self, client):
        resp = client.post(
            "/v1/api/route/workstreams/abc123/send",
            json={"message": "hello"},
            headers=_TEST_AUTH_HEADERS,
        )
        assert resp.status_code == 200
        # Verify upstream URL was /v1/api/workstreams/abc123/send
        # (not /v1/api/route/workstreams/abc123/send).
        mock_request = client.app.state.proxy_client.request
        call_args = mock_request.call_args
        # request is called as ``request(method, url, ...)`` — url is the
        # second positional arg.
        upstream_url = call_args[0][1]
        assert "/v1/api/workstreams/abc123/send" in upstream_url
        assert "/route/" not in upstream_url

    def test_route_proxy_approve(self, client):
        resp = client.post(
            "/v1/api/route/workstreams/abc123/approve",
            json={"approved": True},
            headers=_TEST_AUTH_HEADERS,
        )
        assert resp.status_code == 200

    def test_route_proxy_cancel(self, client):
        resp = client.post(
            "/v1/api/route/workstreams/abc123/cancel",
            json={},
            headers=_TEST_AUTH_HEADERS,
        )
        assert resp.status_code == 200

    @pytest.mark.parametrize(
        ("content", "content_type"),
        [
            (b"", None),
            (b"{", "application/json"),
            (b"[]", "application/json"),
        ],
        ids=["empty", "malformed", "non-object"],
    )
    def test_route_proxy_cancel_normalizes_unusable_body(self, client, content, content_type):
        headers = dict(_TEST_AUTH_HEADERS)
        if content_type is not None:
            headers["Content-Type"] = content_type
        resp = client.post(
            "/v1/api/route/workstreams/abc123/cancel",
            content=content,
            headers=headers,
        )

        assert resp.status_code == 200
        assert client.app.state.proxy_client.request.call_args.kwargs["json"] == {}

    def test_route_proxy_non_cancel_rejects_non_object_json(self, client):
        resp = client.post(
            "/v1/api/route/workstreams/abc123/send",
            json=[],
            headers=_TEST_AUTH_HEADERS,
        )

        assert resp.status_code == 400
        assert resp.json() == {"error": "Request body must be a JSON object"}

    def test_route_proxy_command(self, client):
        resp = client.post(
            "/v1/api/route/command",
            json={"ws_id": "abc123", "command": "status"},
            headers=_TEST_AUTH_HEADERS,
        )
        assert resp.status_code == 200

    def test_route_proxy_close(self, client):
        resp = client.post(
            "/v1/api/route/workstreams/abc123/close",
            json={},
            headers=_TEST_AUTH_HEADERS,
        )
        assert resp.status_code == 200


class TestRouteProxyPermissionGates:
    """``route_proxy`` was pre-existing infra that forwarded blindly —
    any authenticated caller could send/approve/cancel/close.  PR
    adding 057_role_permission_overrides added verb-scoped gates on
    approve + close (the verbs that had vestigial perms in
    ``_VALID_PERMISSIONS`` with no enforcement site).  These tests
    pin the new shape and the OR fallback to ``admin.coordinator``."""

    @pytest.fixture()
    def client(self):
        router = _make_mock_router()
        app = _make_app(router=router)
        _wire_proxy(app, _make_proxy_post(json_data={"status": "ok"}))
        client = TestClient(app, raise_server_exceptions=False)
        yield client
        client.close()

    @staticmethod
    def _hdr(*, perms: frozenset[str] = frozenset()) -> dict[str, str]:
        # Plain user — no service scope, so the bypass doesn't kick in;
        # just the perms passed by the test.
        from turnstone.core.auth import JWT_AUD_CONSOLE, create_jwt

        return {
            "Authorization": (
                "Bearer "
                + create_jwt(
                    user_id="test-user",
                    scopes=frozenset({"read", "write", "approve"}),
                    source="test",
                    secret=_TEST_JWT_SECRET,
                    audience=JWT_AUD_CONSOLE,
                    permissions=perms,
                )
            )
        }

    def test_approve_without_perm_returns_403(self, client):
        resp = client.post(
            "/v1/api/route/workstreams/abc123/approve",
            json={"approved": True},
            headers=self._hdr(),
        )
        assert resp.status_code == 403
        assert "tools.approve" in resp.json()["error"]

    def test_close_without_perm_returns_403(self, client):
        resp = client.post(
            "/v1/api/route/workstreams/abc123/close",
            json={},
            headers=self._hdr(),
        )
        assert resp.status_code == 403
        assert "workstreams.close" in resp.json()["error"]

    def test_approve_with_tools_approve_passes(self, client):
        resp = client.post(
            "/v1/api/route/workstreams/abc123/approve",
            json={"approved": True},
            headers=self._hdr(perms=frozenset({"tools.approve"})),
        )
        assert resp.status_code == 200

    def test_close_with_admin_coordinator_passes(self, client):
        # The OR fallback: coord sessions can drive close on
        # interactive children without holding workstreams.close.
        resp = client.post(
            "/v1/api/route/workstreams/abc123/close",
            json={},
            headers=self._hdr(perms=frozenset({"admin.coordinator"})),
        )
        assert resp.status_code == 200

    def test_send_remains_authenticated_only(self, client):
        # send/cancel/dequeue/command/plan are unchanged — no new gate.
        resp = client.post(
            "/v1/api/route/workstreams/abc123/send",
            json={"message": "hi"},
            headers=self._hdr(),
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Tests — route_lookup
# ---------------------------------------------------------------------------


class TestRouteLookup:
    """GET /v1/api/route — look up which node owns a workstream."""

    @pytest.fixture()
    def client(self):
        router = _make_mock_router()
        app = _make_app(router=router)
        app.state.proxy_client = MagicMock(spec=httpx.AsyncClient)
        client = TestClient(app, raise_server_exceptions=False)
        yield client
        client.close()

    def test_route_lookup(self, client):
        resp = client.get("/v1/api/route?ws_id=abc123", headers=_TEST_AUTH_HEADERS)
        assert resp.status_code == 200
        data = resp.json()
        assert data["node_url"] == "http://a:8080"
        assert data["node_id"] == "node-a"

    def test_route_lookup_missing_ws_id(self, client):
        resp = client.get("/v1/api/route", headers=_TEST_AUTH_HEADERS)
        assert resp.status_code == 400
        assert "ws_id" in resp.json()["error"]


# ---------------------------------------------------------------------------
# Tests — route_workstream_live
# ---------------------------------------------------------------------------


class TestRouteWorkstreamLive:
    """GET routed live probe reads the owner node's active manager list."""

    def test_reports_exact_visible_active_row(self):
        router = _make_mock_router()
        app = _make_app(router=router)
        mock_get = _wire_proxy_get(
            app,
            json_data={
                "workstreams": [
                    {"ws_id": "other", "state": "idle"},
                    {"ws_id": "ws-live", "state": "running"},
                ]
            },
        )
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(
            "/v1/api/route/workstreams/ws-live/live",
            headers=_TEST_AUTH_HEADERS,
        )
        client.close()

        assert resp.status_code == 200
        assert resp.json() == {"ws_id": "ws-live", "live": True}
        assert router.route.call_count == 1
        assert router.route.call_args.args == ("ws-live",)
        assert mock_get.call_args.args[0] == "http://a:8080/v1/api/workstreams"

    def test_false_miss_refreshes_stale_override_and_reprobes_new_owner(self):
        router = _make_mock_router()
        stale_ref = NodeRef("node-b", "http://b:8080")
        owner_ref = NodeRef("node-a", "http://a:8080")
        router.route.side_effect = [stale_ref, owner_ref]
        app = _make_app(router=router)

        async def _get(url: str, **_kwargs: Any) -> httpx.Response:
            payload = (
                {"workstreams": []}
                if url.startswith(stale_ref.url)
                else {"workstreams": [{"ws_id": "ws-live", "state": "idle"}]}
            )
            return httpx.Response(200, json=payload, request=httpx.Request("GET", url))

        proxy = MagicMock(spec=httpx.AsyncClient)
        proxy.get = MagicMock(side_effect=_get)
        app.state.proxy_client = proxy
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(
            "/v1/api/route/workstreams/ws-live/live",
            headers=_TEST_AUTH_HEADERS,
        )
        client.close()

        assert resp.status_code == 200
        assert resp.json() == {"ws_id": "ws-live", "live": True}
        router.force_refresh.assert_called_once_with()
        assert [call.args[0] for call in proxy.get.call_args_list] == [
            "http://b:8080/v1/api/workstreams",
            "http://a:8080/v1/api/workstreams",
        ]

    @pytest.mark.parametrize(
        "rows",
        [
            [],
            [{"ws_id": "other", "state": "idle"}],
            [{"ws_id": "ws-live", "state": "creating"}],
        ],
        ids=["missing-or-private", "different-row", "creating"],
    )
    def test_reports_false_without_exposing_non_live_rows(self, rows):
        app = _make_app(router=_make_mock_router())
        _wire_proxy_get(app, json_data={"workstreams": rows})
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(
            "/v1/api/route/workstreams/ws-live/live",
            headers=_TEST_AUTH_HEADERS,
        )
        client.close()

        assert resp.status_code == 200
        assert resp.json() == {"ws_id": "ws-live", "live": False}

    def test_propagates_upstream_acl_failure(self):
        app = _make_app(router=_make_mock_router())
        _wire_proxy_get(
            app,
            status_code=403,
            json_data={"error": "Forbidden"},
        )
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(
            "/v1/api/route/workstreams/ws-live/live",
            headers=_TEST_AUTH_HEADERS,
        )
        client.close()

        assert resp.status_code == 403
        assert resp.json() == {"error": "Forbidden"}

    def test_malformed_active_list_fails_closed(self):
        app = _make_app(router=_make_mock_router())
        _wire_proxy_get(app, json_data={"unexpected": []})
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(
            "/v1/api/route/workstreams/ws-live/live",
            headers=_TEST_AUTH_HEADERS,
        )
        client.close()

        assert resp.status_code == 502
        assert "invalid active list" in resp.json()["error"]


# ---------------------------------------------------------------------------
# Tests — not ready / no router -> 503
# ---------------------------------------------------------------------------


class TestRouteNotReady:
    """When router is None or empty cache, all routing endpoints return 503."""

    @pytest.fixture()
    def client_no_router(self):
        app = _make_app(router=None)
        app.state.proxy_client = MagicMock(spec=httpx.AsyncClient)
        client = TestClient(app, raise_server_exceptions=False)
        yield client
        client.close()

    @pytest.fixture()
    def client_empty_cache(self):
        router = _make_mock_router(ready=False)
        app = _make_app(router=router)
        app.state.proxy_client = MagicMock(spec=httpx.AsyncClient)
        client = TestClient(app, raise_server_exceptions=False)
        yield client
        client.close()

    def test_route_create_no_router_503(self, client_no_router):
        resp = client_no_router.post(
            "/v1/api/route/workstreams/new",
            json={"name": "test"},
            headers=_TEST_AUTH_HEADERS,
        )
        assert resp.status_code == 503

    def test_route_create_empty_cache_503(self, client_empty_cache):
        resp = client_empty_cache.post(
            "/v1/api/route/workstreams/new",
            json={"name": "test"},
            headers=_TEST_AUTH_HEADERS,
        )
        assert resp.status_code == 503

    def test_route_proxy_no_router_503(self, client_no_router):
        resp = client_no_router.post(
            "/v1/api/route/workstreams/abc/send",
            json={"message": "hello"},
            headers=_TEST_AUTH_HEADERS,
        )
        assert resp.status_code == 503

    def test_route_lookup_no_router_503(self, client_no_router):
        resp = client_no_router.get("/v1/api/route?ws_id=abc", headers=_TEST_AUTH_HEADERS)
        assert resp.status_code == 503

    def test_route_live_no_router_503(self, client_no_router):
        resp = client_no_router.get(
            "/v1/api/route/workstreams/abc/live",
            headers=_TEST_AUTH_HEADERS,
        )
        assert resp.status_code == 503

    def test_route_proxy_empty_cache_503(self, client_empty_cache):
        resp = client_empty_cache.post(
            "/v1/api/route/workstreams/abc/send",
            json={"message": "hello"},
            headers=_TEST_AUTH_HEADERS,
        )
        assert resp.status_code == 503

    def test_route_lookup_empty_cache_503(self, client_empty_cache):
        resp = client_empty_cache.get("/v1/api/route?ws_id=abc", headers=_TEST_AUTH_HEADERS)
        assert resp.status_code == 503

    def test_route_live_empty_cache_503(self, client_empty_cache):
        resp = client_empty_cache.get(
            "/v1/api/route/workstreams/abc/live",
            headers=_TEST_AUTH_HEADERS,
        )
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# Tests — NoAvailableNodeError handling
# ---------------------------------------------------------------------------


class TestRouteNoNode:
    """When router.route() raises NoAvailableNodeError, endpoints return 503."""

    @pytest.fixture()
    def client(self):
        router = _make_mock_router()
        router.route.side_effect = NoAvailableNodeError("bucket 0 not assigned")
        app = _make_app(router=router)
        app.state.proxy_client = MagicMock(spec=httpx.AsyncClient)
        client = TestClient(app, raise_server_exceptions=False)
        yield client
        client.close()

    def test_route_create_no_node_503(self, client):
        resp = client.post(
            "/v1/api/route/workstreams/new",
            json={"name": "test"},
            headers=_TEST_AUTH_HEADERS,
        )
        assert resp.status_code == 503
        assert "No available node" in resp.json()["error"]

    def test_route_proxy_no_node_503(self, client):
        resp = client.post(
            "/v1/api/route/workstreams/abc/send",
            json={"message": "hello"},
            headers=_TEST_AUTH_HEADERS,
        )
        assert resp.status_code == 503

    def test_route_lookup_no_node_503(self, client):
        resp = client.get("/v1/api/route?ws_id=abc", headers=_TEST_AUTH_HEADERS)
        assert resp.status_code == 503

    def test_route_live_no_node_503(self, client):
        resp = client.get(
            "/v1/api/route/workstreams/abc/live",
            headers=_TEST_AUTH_HEADERS,
        )
        assert resp.status_code == 503


def test_route_proxy_refresh_retries_new_url_for_same_node_identity():
    router = _make_mock_router()
    router.route.side_effect = [
        NodeRef("host-1", "http://old:8080"),
        NodeRef("host-1", "http://new:8080"),
    ]
    app = _make_app(router=router)
    requests = []

    async def upstream(method, url, **kwargs):
        requests.append(url)
        return httpx.Response(
            404 if len(requests) == 1 else 200,
            json={"status": "ok"},
            request=httpx.Request(method, url),
        )

    proxy = MagicMock(spec=httpx.AsyncClient)
    proxy.request = MagicMock(side_effect=upstream)
    app.state.proxy_client = proxy
    client = TestClient(app, raise_server_exceptions=False)
    try:
        response = client.post(
            "/v1/api/route/workstreams/saved/send",
            json={"message": "continue"},
            headers=_TEST_AUTH_HEADERS,
        )
    finally:
        client.close()
    assert response.status_code == 200
    assert requests == [
        "http://old:8080/v1/api/workstreams/saved/send",
        "http://new:8080/v1/api/workstreams/saved/send",
    ]
    router.force_refresh.assert_called_once()
