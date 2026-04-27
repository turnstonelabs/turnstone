"""Tests for routing-proxy audit middleware.

Every successful ``/v1/api/route/*`` hop emits an ``audit_events`` row
with action ``route.workstream.{create,send,close,delete}`` /
``route.{approve,cancel,command,plan}`` and ``detail`` carrying
``{src, node_id, coord_ws_id?}``.  Failure paths (4xx/5xx) MUST NOT
emit, and audit-emission failure MUST NOT break the proxied call.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from starlette.testclient import TestClient

from turnstone.console.collector import ClusterCollector
from turnstone.console.router import ConsoleRouter, NodeRef
from turnstone.core.auth import JWT_AUD_CONSOLE, create_jwt

_TEST_JWT_SECRET = "test-jwt-secret-minimum-32-chars!"


def _coordinator_jwt(coord_ws_id: str = "coord-42") -> str:
    """Mint a JWT shaped like CoordinatorTokenManager would produce."""
    return create_jwt(
        user_id="user-real-creator",
        scopes=frozenset({"read", "write", "approve"}),
        source="coordinator",
        secret=_TEST_JWT_SECRET,
        audience=JWT_AUD_CONSOLE,
        permissions=frozenset({"admin.coordinator"}),
        extra_claims={"coord_ws_id": coord_ws_id},
    )


def _plain_jwt() -> str:
    """A normal JWT — not coordinator-origin."""
    return create_jwt(
        user_id="user-human",
        scopes=frozenset({"read", "write", "approve"}),
        source="jwt",
        secret=_TEST_JWT_SECRET,
        audience=JWT_AUD_CONSOLE,
    )


_COORD_HEADERS: dict[str, str] = {"Authorization": f"Bearer {_coordinator_jwt()}"}
_PLAIN_HEADERS: dict[str, str] = {"Authorization": f"Bearer {_plain_jwt()}"}


# ---------------------------------------------------------------------------
# Mock plumbing
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


def _make_mock_router(node_id: str = "node-a", url: str = "http://a:8080") -> MagicMock:
    router = MagicMock(spec=ConsoleRouter)
    router.is_ready.return_value = True
    router.route.return_value = NodeRef(node_id, url)
    router.generate_ws_id_for_node.return_value = "00ff" + "0" * 28
    return router


def _make_app(router: Any = None) -> Any:
    from turnstone.console.server import _load_static, create_app

    _load_static()
    return create_app(
        collector=_make_mock_collector(),
        jwt_secret=_TEST_JWT_SECRET,
        router=router,
    )


def _make_proxy(status_code: int = 200, body: dict[str, Any] | None = None) -> MagicMock:
    payload = body or {"ws_id": "abc123", "name": "test"}

    async def _post(*args: Any, **kwargs: Any) -> httpx.Response:
        return httpx.Response(
            status_code,
            json=payload,
            request=httpx.Request("POST", args[0] if args else "http://test"),
        )

    async def _request(method: str, *args: Any, **kwargs: Any) -> httpx.Response:
        return await _post(*args, **kwargs)

    proxy = MagicMock(spec=httpx.AsyncClient)
    proxy.post = MagicMock(side_effect=_post)
    proxy.request = MagicMock(side_effect=_request)
    return proxy


def _capture_storage() -> tuple[MagicMock, list[dict[str, Any]]]:
    """Return a mock storage that captures record_audit_event call kwargs."""
    captured: list[dict[str, Any]] = []

    def _record(**kwargs: Any) -> None:
        captured.append(kwargs)

    storage = MagicMock()
    storage.record_audit_event = MagicMock(side_effect=_record)
    return storage, captured


def _wire(app: Any, proxy: MagicMock, storage: MagicMock | None = None) -> None:
    app.state.proxy_client = proxy
    if storage is not None:
        app.state.auth_storage = storage


# ---------------------------------------------------------------------------
# route_create
# ---------------------------------------------------------------------------


class TestRouteCreateAudit:
    def test_emits_route_workstream_create_on_200_with_coordinator_origin(self):
        router = _make_mock_router("node-a", "http://a:8080")
        app = _make_app(router=router)
        storage, captured = _capture_storage()
        _wire(app, _make_proxy(200, {"ws_id": "child123", "name": "child"}), storage)
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.post(
            "/v1/api/route/workstreams/new",
            json={"name": "child"},
            headers=_COORD_HEADERS,
        )
        assert resp.status_code == 200
        assert len(captured) == 1, captured
        row = captured[0]
        assert row["action"] == "route.workstream.create"
        assert row["resource_type"] == "workstream"
        assert row["user_id"] == "user-real-creator"
        # body["ws_id"] is set by the handler to a fresh secrets.token_hex(16)
        # before forwarding upstream — assert it's a 32-char hex string.
        assert len(row["resource_id"]) == 32
        detail = json.loads(row["detail"])
        assert detail["src"] == "coordinator"
        assert detail["coord_ws_id"] == "coord-42"
        assert detail["node_id"] == "node-a"

        client.close()

    def test_does_not_emit_on_502(self):
        router = _make_mock_router()
        app = _make_app(router=router)
        storage, captured = _capture_storage()
        _wire(app, _make_proxy(502, {"error": "upstream"}), storage)
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.post(
            "/v1/api/route/workstreams/new",
            json={"name": "child"},
            headers=_COORD_HEADERS,
        )
        assert resp.status_code == 502
        assert captured == []
        client.close()

    def test_does_not_emit_on_400(self):
        router = _make_mock_router()
        app = _make_app(router=router)
        storage, captured = _capture_storage()
        # No proxy needed — handler returns 400 before any upstream call.
        proxy = MagicMock(spec=httpx.AsyncClient)
        _wire(app, proxy, storage)
        client = TestClient(app, raise_server_exceptions=False)

        # Send invalid JSON (raw body, content-type json) — handler returns 400.
        resp = client.post(
            "/v1/api/route/workstreams/new",
            content=b"not json",
            headers={**_COORD_HEADERS, "Content-Type": "application/json"},
        )
        assert resp.status_code == 400
        assert captured == []
        client.close()

    def test_503_retry_records_final_node_id(self):
        """Audit row must reflect the node that actually served 200, not the failed first node."""
        router = _make_mock_router()

        call_count = 0

        def _route(_ws_id: str) -> NodeRef:
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                return NodeRef("node-a-failed", "http://a:8080")
            return NodeRef("node-b-retry", "http://b:8080")

        router.route.side_effect = _route
        app = _make_app(router=router)
        storage, captured = _capture_storage()

        post_count = 0

        async def _post(*args: Any, **kwargs: Any) -> httpx.Response:
            nonlocal post_count
            post_count += 1
            url = args[0] if args else "http://test"
            if post_count == 1:
                return httpx.Response(
                    503, json={"error": "overloaded"}, request=httpx.Request("POST", url)
                )
            return httpx.Response(
                200, json={"ws_id": "x", "name": "n"}, request=httpx.Request("POST", url)
            )

        proxy = MagicMock(spec=httpx.AsyncClient)
        proxy.post = MagicMock(side_effect=_post)
        _wire(app, proxy, storage)
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.post(
            "/v1/api/route/workstreams/new",
            json={"name": "child"},
            headers=_COORD_HEADERS,
        )
        assert resp.status_code == 200
        assert len(captured) == 1
        detail = json.loads(captured[0]["detail"])
        assert detail["node_id"] == "node-b-retry"
        client.close()

    def test_no_storage_means_no_emission_no_crash(self):
        """When auth_storage is not installed (e.g. pre-config-store tests), the new code is a no-op."""
        router = _make_mock_router()
        app = _make_app(router=router)
        _wire(app, _make_proxy(200, {"ws_id": "x", "name": "n"}))  # NO storage
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.post(
            "/v1/api/route/workstreams/new",
            json={"name": "child"},
            headers=_COORD_HEADERS,
        )
        assert resp.status_code == 200  # no crash
        client.close()


# ---------------------------------------------------------------------------
# route_proxy
# ---------------------------------------------------------------------------


class TestRouteProxyAudit:
    @pytest.mark.parametrize(
        "path,expected_action",
        [
            ("/v1/api/route/workstreams/abc123/send", "route.workstream.send"),
            ("/v1/api/route/workstreams/abc123/approve", "route.approve"),
            ("/v1/api/route/workstreams/abc123/cancel", "route.cancel"),
            ("/v1/api/route/command", "route.command"),
            ("/v1/api/route/plan", "route.plan"),
            ("/v1/api/route/workstreams/abc123/close", "route.workstream.close"),
        ],
    )
    def test_method_to_action_mapping(self, path: str, expected_action: str):
        router = _make_mock_router("node-x", "http://x:8080")
        app = _make_app(router=router)
        storage, captured = _capture_storage()
        _wire(app, _make_proxy(200, {"status": "ok"}), storage)
        client = TestClient(app, raise_server_exceptions=False)

        # ws_id in body is still required by the surviving body-keyed
        # mounts (/route/plan, /route/command); for the path-keyed
        # workstreams routes the proxy reads ws_id from path_params.
        resp = client.post(
            path,
            json={"ws_id": "abc123", "message": "hi"},
            headers=_COORD_HEADERS,
        )
        assert resp.status_code == 200
        assert len(captured) == 1
        row = captured[0]
        assert row["action"] == expected_action
        assert row["resource_id"] == "abc123"
        assert row["user_id"] == "user-real-creator"
        detail = json.loads(row["detail"])
        assert detail["src"] == "coordinator"
        assert detail["coord_ws_id"] == "coord-42"
        assert detail["node_id"] == "node-x"
        client.close()

    def test_does_not_emit_on_4xx(self):
        # Use 403 — 404 triggers the route_proxy refresh-and-retry path
        # which reaches into ConsoleRouter internals our MagicMock
        # doesn't model.  403 exercises the same "non-2xx, no audit"
        # invariant without the side effect.
        router = _make_mock_router()
        app = _make_app(router=router)
        storage, captured = _capture_storage()
        _wire(app, _make_proxy(403, {"error": "forbidden"}), storage)
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.post(
            "/v1/api/route/workstreams/abc/send",
            json={"message": "hi"},
            headers=_COORD_HEADERS,
        )
        assert resp.status_code == 403
        assert captured == []
        client.close()

    def test_emits_with_plain_jwt_origin_no_coord_ws_id_in_detail(self):
        """Non-coordinator inbound: src='jwt', no coord_ws_id key in detail."""
        router = _make_mock_router("node-y", "http://y:8080")
        app = _make_app(router=router)
        storage, captured = _capture_storage()
        _wire(app, _make_proxy(200, {"status": "ok"}), storage)
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.post(
            "/v1/api/route/workstreams/abc/send",
            json={"message": "hi"},
            headers=_PLAIN_HEADERS,
        )
        assert resp.status_code == 200
        assert len(captured) == 1
        row = captured[0]
        assert row["action"] == "route.workstream.send"
        assert row["user_id"] == "user-human"
        detail = json.loads(row["detail"])
        assert detail["src"] == "jwt"
        assert "coord_ws_id" not in detail
        assert detail["node_id"] == "node-y"
        client.close()


# ---------------------------------------------------------------------------
# route_workstream_delete
# ---------------------------------------------------------------------------


class TestRouteWorkstreamDeleteAudit:
    def test_emits_route_workstream_delete_on_200(self):
        router = _make_mock_router("node-d", "http://d:8080")
        app = _make_app(router=router)
        storage, captured = _capture_storage()
        _wire(app, _make_proxy(200, {"status": "deleted"}), storage)
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.post(
            "/v1/api/route/workstreams/delete",
            json={"ws_id": "doomed-ws"},
            headers=_COORD_HEADERS,
        )
        assert resp.status_code == 200
        assert len(captured) == 1
        row = captured[0]
        assert row["action"] == "route.workstream.delete"
        assert row["resource_id"] == "doomed-ws"
        detail = json.loads(row["detail"])
        assert detail["node_id"] == "node-d"
        assert detail["src"] == "coordinator"
        assert detail["coord_ws_id"] == "coord-42"
        client.close()

    def test_does_not_emit_on_502(self):
        router = _make_mock_router()
        app = _make_app(router=router)
        storage, captured = _capture_storage()
        _wire(app, _make_proxy(502, {"error": "down"}), storage)
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.post(
            "/v1/api/route/workstreams/delete",
            json={"ws_id": "doomed-ws"},
            headers=_COORD_HEADERS,
        )
        assert resp.status_code == 502
        assert captured == []
        client.close()


# ---------------------------------------------------------------------------
# Resilience
# ---------------------------------------------------------------------------


class TestAuditResilience:
    def test_emit_swallows_storage_exception(self):
        """If record_audit_event raises, the proxied response must still come back unchanged."""
        router = _make_mock_router()
        app = _make_app(router=router)

        storage = MagicMock()
        storage.record_audit_event = MagicMock(side_effect=RuntimeError("DB down"))

        _wire(app, _make_proxy(200, {"status": "ok"}), storage)
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.post(
            "/v1/api/route/workstreams/abc/send",
            json={"message": "hi"},
            headers=_COORD_HEADERS,
        )
        # Audit failure is swallowed — proxied response still 200.
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}
        client.close()
