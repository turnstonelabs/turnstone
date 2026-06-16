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

    # route_proxy uses ``client.request(method, url, ...)`` for path-keyed
    # routes (so DELETE on /send proxies through correctly). Wire a
    # request-shim that drops the leading method positional and forwards
    # to the same mock_post for compatibility.
    async def _request_shim(method: str, *args: Any, **kwargs: Any) -> httpx.Response:
        return await mock_post(*args, **kwargs)

    mock_proxy.request = MagicMock(side_effect=_request_shim)
    app.state.proxy_client = mock_proxy


# ---------------------------------------------------------------------------
# Tests — route_create
# ---------------------------------------------------------------------------


class TestRouteCreate:
    """POST /v1/api/route/workstreams/new — create via rendezvous routing."""

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
