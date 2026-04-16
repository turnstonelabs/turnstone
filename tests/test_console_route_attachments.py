"""Tests for console routing of attachment endpoints + multipart route_create.

Covers the cluster-routing surface added alongside the workstream
attachment-on-create feature: the multipart variant of route_create and
the four ws-id-keyed attachment proxies under /v1/api/route/.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import httpx
from starlette.testclient import TestClient

from turnstone.console.collector import ClusterCollector
from turnstone.console.router import ConsoleRouter, NodeRef

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


_AUTH: dict[str, str] = {"Authorization": f"Bearer {_test_jwt()}"}


def _make_app(router: Any) -> Any:
    from turnstone.console.server import _load_static, create_app

    _load_static()
    collector = MagicMock(spec=ClusterCollector)
    return create_app(
        collector=collector,
        jwt_secret=_TEST_JWT_SECRET,
        router=router,
    )


def _make_router() -> MagicMock:
    router = MagicMock(spec=ConsoleRouter)
    router.is_ready.return_value = True
    router.route.return_value = NodeRef("node-a", "http://a:8080")
    return router


# ---------------------------------------------------------------------------
# route_create multipart
# ---------------------------------------------------------------------------


class TestRouteCreateMultipart:
    def test_multipart_requires_ws_id_query(self):
        router = _make_router()
        app = _make_app(router=router)
        app.state.proxy_client = MagicMock(spec=httpx.AsyncClient)
        client = TestClient(app, raise_server_exceptions=False)
        try:
            resp = client.post(
                "/v1/api/route/workstreams/new",
                files=[("file", ("a.txt", b"hello", "text/plain"))],
                data={"meta": "{}"},
                headers=_AUTH,
            )
            assert resp.status_code == 400
            assert "ws_id" in resp.json()["error"]
        finally:
            client.close()

    def test_multipart_forwards_raw_body_to_routed_node(self):
        router = _make_router()
        app = _make_app(router=router)

        captured: dict[str, Any] = {}

        async def _mock_post(*args: Any, **kwargs: Any) -> httpx.Response:
            captured["url"] = args[0] if args else ""
            captured["headers"] = kwargs.get("headers") or {}
            captured["content"] = kwargs.get("content")
            return httpx.Response(
                200,
                json={"ws_id": "00ff" + "0" * 28, "name": "demo"},
                request=httpx.Request("POST", args[0] if args else "http://test"),
            )

        mock_proxy = MagicMock(spec=httpx.AsyncClient)
        mock_proxy.post = MagicMock(side_effect=_mock_post)
        app.state.proxy_client = mock_proxy
        client = TestClient(app, raise_server_exceptions=False)
        try:
            ws_id = "00ff" + "0" * 28
            resp = client.post(
                f"/v1/api/route/workstreams/new?ws_id={ws_id}",
                files=[("file", ("a.txt", b"hello", "text/plain"))],
                data={"meta": '{"name":"demo"}'},
                headers=_AUTH,
            )
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data["node_id"] == "node-a"
            # Forwarded multipart Content-Type
            assert captured["headers"].get("Content-Type", "").startswith("multipart/form-data")
            # Body bytes were forwarded raw
            assert isinstance(captured["content"], (bytes, bytearray))
            assert b"hello" in bytes(captured["content"])
            router.route.assert_called_with(ws_id)
        finally:
            client.close()

    def test_multipart_preserves_mixed_case_boundary(self):
        """The boundary= param is case-sensitive — must match body bytes verbatim.

        Regression for an earlier bug where route_create lowercased the
        whole Content-Type header before forwarding, mangling boundaries
        like ``WebKitFormBoundary7MA4YWxkTrZu0gW``.
        """
        router = _make_router()
        app = _make_app(router=router)

        captured: dict[str, Any] = {}

        async def _mock_post(*args: Any, **kwargs: Any) -> httpx.Response:
            captured["headers"] = kwargs.get("headers") or {}
            captured["content"] = kwargs.get("content")
            return httpx.Response(
                200,
                json={"ws_id": "00ff" + "0" * 28, "name": "ok"},
                request=httpx.Request("POST", args[0] if args else "http://test"),
            )

        mock_proxy = MagicMock(spec=httpx.AsyncClient)
        mock_proxy.post = MagicMock(side_effect=_mock_post)
        app.state.proxy_client = mock_proxy
        client = TestClient(app, raise_server_exceptions=False)
        try:
            ws_id = "00ff" + "0" * 28
            boundary = "WebKitFormBoundary7MA4YWxkTrZu0gW"  # mixed-case
            body = (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="meta"\r\n\r\n'
                f'{{"name":"demo"}}\r\n'
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="file"; filename="a.txt"\r\n'
                f"Content-Type: text/plain\r\n\r\n"
                f"hello\r\n"
                f"--{boundary}--\r\n"
            ).encode()
            resp = client.post(
                f"/v1/api/route/workstreams/new?ws_id={ws_id}",
                content=body,
                headers={
                    **_AUTH,
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                },
            )
            assert resp.status_code == 200, resp.text
            forwarded = captured["headers"].get("Content-Type", "")
            assert boundary in forwarded, (
                f"boundary mangled in upstream Content-Type: {forwarded!r}"
            )
            # Body bytes still contain the mixed-case boundary
            assert boundary.encode() in bytes(captured["content"])
        finally:
            client.close()

    def test_json_path_unchanged(self):
        """Existing JSON callers should continue to work as before."""
        router = _make_router()
        app = _make_app(router=router)

        async def _mock_post(*args: Any, **kwargs: Any) -> httpx.Response:
            return httpx.Response(
                200,
                json={"ws_id": "abc123", "name": "json"},
                request=httpx.Request("POST", args[0] if args else "http://test"),
            )

        mock_proxy = MagicMock(spec=httpx.AsyncClient)
        mock_proxy.post = MagicMock(side_effect=_mock_post)
        app.state.proxy_client = mock_proxy
        client = TestClient(app, raise_server_exceptions=False)
        try:
            resp = client.post(
                "/v1/api/route/workstreams/new",
                json={"name": "json"},
                headers=_AUTH,
            )
            assert resp.status_code == 200
            assert resp.json()["ws_id"] == "abc123"
            # JSON path uses json= kwarg, not content=
            call_kwargs = mock_proxy.post.call_args.kwargs
            assert "json" in call_kwargs
            assert "content" not in call_kwargs
        finally:
            client.close()


# ---------------------------------------------------------------------------
# route_attachment_proxy
# ---------------------------------------------------------------------------


class TestRouteAttachmentProxy:
    def _wire(self, mock_request_fn) -> tuple[Any, MagicMock]:
        router = _make_router()
        app = _make_app(router=router)
        mock_proxy = MagicMock(spec=httpx.AsyncClient)
        mock_proxy.request = MagicMock(side_effect=mock_request_fn)
        mock_proxy.get = MagicMock(side_effect=mock_request_fn)
        mock_proxy.post = MagicMock(side_effect=mock_request_fn)
        app.state.proxy_client = mock_proxy
        return app, mock_proxy

    def test_upload_proxies_multipart(self):
        captured: dict[str, Any] = {}

        async def _mock(*args: Any, **kwargs: Any) -> httpx.Response:
            captured["method"] = args[0] if args else kwargs.get("method")
            captured["url"] = args[1] if len(args) > 1 else kwargs.get("url", "")
            captured["headers"] = kwargs.get("headers") or {}
            captured["content"] = kwargs.get("content")
            return httpx.Response(
                200,
                json={
                    "attachment_id": "att-1",
                    "filename": "a.txt",
                    "mime_type": "text/plain",
                    "size_bytes": 5,
                    "kind": "text",
                },
                request=httpx.Request("POST", "http://a:8080/x"),
            )

        app, _ = self._wire(_mock)
        client = TestClient(app, raise_server_exceptions=False)
        try:
            resp = client.post(
                "/v1/api/route/workstreams/ws-X/attachments",
                files=[("file", ("a.txt", b"hello", "text/plain"))],
                headers=_AUTH,
            )
            assert resp.status_code == 200
            assert resp.json()["attachment_id"] == "att-1"
            assert "/v1/api/workstreams/ws-X/attachments" in captured["url"]
            assert "/route/" not in captured["url"]
            assert captured["headers"].get("Content-Type", "").startswith("multipart/form-data")
        finally:
            client.close()

    def test_list_proxies_get(self):
        async def _mock(*args: Any, **kwargs: Any) -> httpx.Response:
            return httpx.Response(
                200,
                json={"attachments": []},
                request=httpx.Request("GET", "http://a:8080/x"),
            )

        app, mock_proxy = self._wire(_mock)
        client = TestClient(app, raise_server_exceptions=False)
        try:
            resp = client.get(
                "/v1/api/route/workstreams/ws-X/attachments",
                headers=_AUTH,
            )
            assert resp.status_code == 200
            assert resp.json() == {"attachments": []}
            mock_proxy.get.assert_called()
        finally:
            client.close()

    def test_get_content_preserves_upstream_headers(self):
        async def _mock(*args: Any, **kwargs: Any) -> httpx.Response:
            return httpx.Response(
                200,
                content=b"hello world",
                headers={
                    "Content-Type": "text/plain; charset=utf-8",
                    "Content-Disposition": 'inline; filename="notes.md"',
                    "X-Content-Type-Options": "nosniff",
                },
                request=httpx.Request("GET", "http://a:8080/x"),
            )

        app, _ = self._wire(_mock)
        client = TestClient(app, raise_server_exceptions=False)
        try:
            resp = client.get(
                "/v1/api/route/workstreams/ws-X/attachments/att-1/content",
                headers=_AUTH,
            )
            assert resp.status_code == 200
            assert resp.content == b"hello world"
            assert resp.headers.get("X-Content-Type-Options") == "nosniff"
            assert "filename" in resp.headers.get("Content-Disposition", "")
        finally:
            client.close()

    def test_delete_proxies_method(self):
        captured: dict[str, Any] = {}

        async def _mock(*args: Any, **kwargs: Any) -> httpx.Response:
            captured["method"] = args[0] if args else ""
            captured["url"] = args[1] if len(args) > 1 else ""
            return httpx.Response(
                200,
                json={"status": "deleted"},
                request=httpx.Request("DELETE", "http://a:8080/x"),
            )

        app, _ = self._wire(_mock)
        client = TestClient(app, raise_server_exceptions=False)
        try:
            resp = client.delete(
                "/v1/api/route/workstreams/ws-X/attachments/att-1",
                headers=_AUTH,
            )
            assert resp.status_code == 200
            assert resp.json() == {"status": "deleted"}
            assert captured["method"] == "DELETE"
        finally:
            client.close()


# ---------------------------------------------------------------------------
# Routing-failure paths
# ---------------------------------------------------------------------------


class TestRoutingFailures:
    def test_router_not_ready_returns_503(self):
        router = MagicMock(spec=ConsoleRouter)
        router.is_ready.return_value = False
        router.refresh_cache.return_value = None
        app = _make_app(router=router)
        app.state.proxy_client = MagicMock(spec=httpx.AsyncClient)
        client = TestClient(app, raise_server_exceptions=False)
        try:
            resp = client.get(
                "/v1/api/route/workstreams/ws-X/attachments",
                headers=_AUTH,
            )
            assert resp.status_code == 503
        finally:
            client.close()
