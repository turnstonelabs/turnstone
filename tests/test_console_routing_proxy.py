"""Tests for console routing proxy endpoints (route_create, route_proxy, route_lookup)."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from starlette.testclient import TestClient

from turnstone.console.collector import ClusterCollector
from turnstone.console.router import ConsoleRouter, NodeRef
from turnstone.core.hash_ring import NoAvailableNodeError

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
    router.route.return_value = NodeRef("node-a", "http://a:8080")
    router.generate_ws_id_for_node.return_value = "00ff" + "0" * 28
    return router


def _make_app(
    collector: Any = None,
    router: Any = None,
) -> Any:
    from turnstone.console.server import _load_static, create_app

    _load_static()
    return create_app(
        collector=collector or _make_mock_collector(),
        jwt_secret=_TEST_JWT_SECRET,
        router=router,
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
    app.state.proxy_client = mock_proxy


# ---------------------------------------------------------------------------
# Tests — route_create
# ---------------------------------------------------------------------------


class TestRouteCreate:
    """POST /v1/api/route/workstreams/new — create via hash-ring routing."""

    @pytest.fixture()
    def client(self):
        router = _make_mock_router()
        app = _make_app(router=router)
        _wire_proxy(app, _make_proxy_post(json_data={"ws_id": "abc123", "name": "test"}))
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
        assert data["ws_id"] == "abc123"

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
        app = _make_app(router=router)
        _wire_proxy(app, _make_proxy_post(json_data={"ws_id": "old_ws_resumed", "name": "resumed"}))
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.post(
            "/v1/api/route/workstreams/new",
            json={"resume_ws": "old_ws_id"},
            headers=_TEST_AUTH_HEADERS,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["node_url"] == "http://b:8080"
        assert data["node_id"] == "node-b"
        # route() should have been called with the old ws_id
        router.route.assert_called_with("old_ws_id")
        client.close()

    def test_route_create_target_node(self):
        """target_node should generate a ws_id that hashes to that node."""
        router = _make_mock_router()
        router.generate_ws_id_for_node.return_value = "00ff" + "0" * 28
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
        router.generate_ws_id_for_node.assert_called_with("node-c")
        client.close()

    def test_route_create_routing_strategy_hash_ring(self, client):
        """Default fan-out (no resume_ws / no target_node) reports
        routing_strategy='hash_ring' so the coordinator's spawn tool can
        explain why the node was chosen."""
        resp = client.post(
            "/v1/api/route/workstreams/new",
            json={"name": "test-ws"},
            headers=_TEST_AUTH_HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["routing_strategy"] == "hash_ring"

    def test_route_create_routing_strategy_target_node(self):
        router = _make_mock_router()
        router.generate_ws_id_for_node.return_value = "00ff" + "0" * 28
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
        app = _make_app(router=router)
        _wire_proxy(app, _make_proxy_post(json_data={"ws_id": "old_ws_resumed", "name": "resumed"}))
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/v1/api/route/workstreams/new",
            json={"resume_ws": "old_ws_id"},
            headers=_TEST_AUTH_HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["routing_strategy"] == "resume"
        client.close()


class TestRouteCreate503Retry:
    """503 retry logic in route_create."""

    def test_route_create_503_retries_on_different_node(self):
        """If the first node returns 503, retry with a new ws_id targeting a different node."""
        router = _make_mock_router()
        call_count = 0

        def side_effect_route(ws_id: str) -> NodeRef:
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
                json={"ws_id": "retry_ws", "name": "retry"},
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
        assert data["ws_id"] == "retry_ws"
        assert data["node_id"] == "node-b"
        assert post_count == 2
        client.close()


# ---------------------------------------------------------------------------
# Tests — route_proxy
# ---------------------------------------------------------------------------


class TestRouteProxy:
    """POST /v1/api/route/send (and other routed endpoints)."""

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
            "/v1/api/route/send",
            json={"ws_id": "abc123", "message": "hello"},
            headers=_TEST_AUTH_HEADERS,
        )
        assert resp.status_code == 200
        # Verify upstream URL was /v1/api/send (not /v1/api/route/send)
        mock_post = client.app.state.proxy_client.post
        call_args = mock_post.call_args
        assert "/v1/api/send" in call_args[0][0]
        assert "/route/" not in call_args[0][0]

    def test_route_proxy_approve(self, client):
        resp = client.post(
            "/v1/api/route/approve",
            json={"ws_id": "abc123", "approved": True},
            headers=_TEST_AUTH_HEADERS,
        )
        assert resp.status_code == 200

    def test_route_proxy_cancel(self, client):
        resp = client.post(
            "/v1/api/route/cancel",
            json={"ws_id": "abc123"},
            headers=_TEST_AUTH_HEADERS,
        )
        assert resp.status_code == 200

    def test_route_proxy_command(self, client):
        resp = client.post(
            "/v1/api/route/command",
            json={"ws_id": "abc123", "command": "status"},
            headers=_TEST_AUTH_HEADERS,
        )
        assert resp.status_code == 200

    def test_route_proxy_close(self, client):
        resp = client.post(
            "/v1/api/route/workstreams/close",
            json={"ws_id": "abc123"},
            headers=_TEST_AUTH_HEADERS,
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
            "/v1/api/route/send",
            json={"ws_id": "abc", "message": "hello"},
            headers=_TEST_AUTH_HEADERS,
        )
        assert resp.status_code == 503

    def test_route_lookup_no_router_503(self, client_no_router):
        resp = client_no_router.get("/v1/api/route?ws_id=abc", headers=_TEST_AUTH_HEADERS)
        assert resp.status_code == 503

    def test_route_proxy_empty_cache_503(self, client_empty_cache):
        resp = client_empty_cache.post(
            "/v1/api/route/send",
            json={"ws_id": "abc", "message": "hello"},
            headers=_TEST_AUTH_HEADERS,
        )
        assert resp.status_code == 503

    def test_route_lookup_empty_cache_503(self, client_empty_cache):
        resp = client_empty_cache.get("/v1/api/route?ws_id=abc", headers=_TEST_AUTH_HEADERS)
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
            "/v1/api/route/send",
            json={"ws_id": "abc", "message": "hello"},
            headers=_TEST_AUTH_HEADERS,
        )
        assert resp.status_code == 503

    def test_route_lookup_no_node_503(self, client):
        resp = client.get("/v1/api/route?ws_id=abc", headers=_TEST_AUTH_HEADERS)
        assert resp.status_code == 503
