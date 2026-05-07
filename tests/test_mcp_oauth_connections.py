"""Integration tests for the MCP OAuth ``/connections`` endpoints.

Covers the list and revoke handlers that surface user-owned MCP server
consents to the settings UI:

* ``GET /v1/api/mcp/oauth/connections`` — non-secret projection only.
* ``DELETE /v1/api/mcp/oauth/connections/{server_name}`` — best-effort
  upstream revoke (RFC 7009) followed by the authoritative local
  delete; cross-user attempts return 404 with the exact same body
  shape as a never-existed row to avoid leaking tenant existence.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.routing import Mount, Route
from starlette.testclient import TestClient

from tests.conftest import make_mcp_token_cipher
from turnstone.core.auth import AuthResult
from turnstone.core.mcp_crypto import MCPTokenStore
from turnstone.core.mcp_oauth import (
    handle_mcp_oauth_list_connections,
    handle_mcp_oauth_revoke_connection,
)
from turnstone.core.oidc import OIDCConfig
from turnstone.core.storage._sqlite import SQLiteBackend

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response


# ---------------------------------------------------------------------------
# Fixtures + helpers (mirror tests/test_mcp_oauth_handlers.py)
# ---------------------------------------------------------------------------


class _InjectAuthMiddleware(BaseHTTPMiddleware):
    """Stamp a fixed authenticated user on every request."""

    def __init__(self, app: Any, user_id: str = "user-1") -> None:
        super().__init__(app)
        self._user_id = user_id

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        request.state.auth_result = AuthResult(
            user_id=self._user_id,
            scopes=frozenset({"write"}),
            token_source="config",
            permissions=frozenset({"read", "write"}),
        )
        return await call_next(request)


class _NoAuthMiddleware(BaseHTTPMiddleware):
    """Leave ``request.state.auth_result`` unset so handlers see anon."""

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        return await call_next(request)


async def _list_handler(request: Request) -> Response:
    return await handle_mcp_oauth_list_connections(request)


async def _revoke_handler(request: Request) -> Response:
    return await handle_mcp_oauth_revoke_connection(request)


def _build_app(
    *,
    storage: SQLiteBackend,
    http_client: httpx.AsyncClient | MagicMock,
    token_store: MCPTokenStore | None,
    user_id: str = "user-1",
    mcp_client: Any = None,
    authenticated: bool = True,
) -> Starlette:
    middleware: list[Middleware]
    if authenticated:
        middleware = [Middleware(_InjectAuthMiddleware, user_id=user_id)]
    else:
        middleware = [Middleware(_NoAuthMiddleware)]
    app = Starlette(
        routes=[
            Mount(
                "/v1",
                routes=[
                    Route("/api/mcp/oauth/connections", _list_handler),
                    Route(
                        "/api/mcp/oauth/connections/{server_name}",
                        _revoke_handler,
                        methods=["DELETE"],
                    ),
                ],
            ),
        ],
        middleware=middleware,
    )
    app.state.auth_storage = storage
    app.state.mcp_token_store = token_store
    app.state.mcp_oauth_http_client = http_client
    app.state.mcp_oauth_refresh_locks = {}
    app.state.mcp_oauth_dcr_locks = {}
    app.state.mcp_oauth_metadata_cache = {}
    app.state.mcp_oauth_last_cleanup_monotonic = 0.0
    app.state.oidc_config = OIDCConfig(enabled=False, redirect_base="https://testserver")
    if mcp_client is not None:
        app.state.mcp_client = mcp_client
    return app


def _make_token_store(backend: SQLiteBackend) -> MCPTokenStore:
    return MCPTokenStore(backend, make_mcp_token_cipher(), node_id="test")


def _seed_oauth_user_server(
    backend: SQLiteBackend,
    *,
    name: str = "srv-oauth",
    server_id: str = "srv-id-1",
    cached_issuer: str | None = "https://as.example.com",
) -> str:
    backend.create_mcp_server(
        server_id=server_id,
        name=name,
        transport="streamable-http",
        url="https://mcp.example.com/sse",
        auth_type="oauth_user",
        oauth_client_id="client-abc",
        oauth_scopes="openid profile",
        oauth_audience="https://mcp.example.com",
        oauth_authorization_server_url=None,
    )
    if cached_issuer is not None:
        backend.update_mcp_server(server_id, oauth_as_issuer_cached=cached_issuer)
    return server_id


def _seed_user_token(
    token_store: MCPTokenStore,
    *,
    user_id: str = "user-1",
    server_name: str = "srv-oauth",
    refresh_token: str | None = "refresh-secret",
) -> None:
    token_store.create_user_token(
        user_id,
        server_name,
        access_token="access-secret",
        refresh_token=refresh_token,
        expires_at="2099-12-31T00:00:00",
        scopes="openid profile",
        as_issuer="https://as.example.com",
        audience="https://mcp.example.com",
    )


def _good_as_metadata_doc(
    *, revocation_endpoint: str | None = "https://as.example.com/revoke"
) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "issuer": "https://as.example.com",
        "authorization_endpoint": "https://as.example.com/authorize",
        "token_endpoint": "https://as.example.com/token",
        "registration_endpoint": "https://as.example.com/register",
        "jwks_uri": "https://as.example.com/jwks",
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none", "client_secret_basic"],
    }
    if revocation_endpoint is not None:
        doc["revocation_endpoint"] = revocation_endpoint
    return doc


def _mk_response(
    status_code: int = 200,
    json_body: Any = None,
    headers: dict[str, str] | None = None,
) -> MagicMock:
    import json as _json

    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.headers = headers or {}
    body_str = _json.dumps(json_body) if json_body is not None else ""
    resp.content = body_str.encode("utf-8")
    if json_body is not None:
        resp.json.return_value = json_body
    else:
        resp.json.side_effect = ValueError("no body")
    resp.text = body_str
    return resp


def _public_addr_patch():
    return patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 0))])


def _drain_revoke_upstream_tasks(client: TestClient, timeout: float = 2.0) -> None:
    """Block until all in-flight upstream-revoke tasks complete.

    Phase 8 perf-1 made the RFC 7009 AS round-trip a fire-and-forget
    task so the user-visible 204 isn't gated on the AS. The tasks were
    scheduled on the TestClient's portal loop; we re-enter that loop
    via :attr:`TestClient.portal` to await them. Tests that assert
    against the upstream POST must call this helper before the
    assertion.
    """
    from turnstone.core.mcp_oauth import _revoke_upstream_tasks

    portal = getattr(client, "portal", None)
    if portal is None:
        return

    async def _drain() -> None:
        pending = list(_revoke_upstream_tasks)
        if pending:
            async with asyncio.timeout(timeout):
                await asyncio.gather(*pending, return_exceptions=True)

    portal.call(_drain)


@pytest.fixture
def storage(tmp_path: Any) -> SQLiteBackend:
    backend = SQLiteBackend(str(tmp_path / "test.db"))
    backend.create_user("user-1", "user1", "User One", "hash")
    backend.create_user("user-2", "user2", "User Two", "hash")
    return backend


@pytest.fixture
def http_client_mock() -> MagicMock:
    client = MagicMock(spec=httpx.AsyncClient)
    client.get = AsyncMock()
    client.post = AsyncMock()
    return client


# ---------------------------------------------------------------------------
# GET /connections
# ---------------------------------------------------------------------------


class TestListConnections:
    def test_list_connections_unauthenticated_401(
        self, storage: SQLiteBackend, http_client_mock: MagicMock
    ) -> None:
        token_store = _make_token_store(storage)
        app = _build_app(
            storage=storage,
            http_client=http_client_mock,
            token_store=token_store,
            authenticated=False,
        )
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/v1/api/mcp/oauth/connections")
        assert resp.status_code == 401
        assert resp.json() == {"error": "Authentication required"}

    def test_list_connections_no_token_store_503(
        self, storage: SQLiteBackend, http_client_mock: MagicMock
    ) -> None:
        app = _build_app(storage=storage, http_client=http_client_mock, token_store=None)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/v1/api/mcp/oauth/connections")
        assert resp.status_code == 503

    def test_list_connections_empty_user_returns_empty_list(
        self, storage: SQLiteBackend, http_client_mock: MagicMock
    ) -> None:
        token_store = _make_token_store(storage)
        app = _build_app(storage=storage, http_client=http_client_mock, token_store=token_store)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/v1/api/mcp/oauth/connections")
        assert resp.status_code == 200
        assert resp.json() == {"connections": []}

    def test_list_connections_returns_users_consents(
        self, storage: SQLiteBackend, http_client_mock: MagicMock
    ) -> None:
        _seed_oauth_user_server(storage, name="srv-a", server_id="srv-id-a")
        _seed_oauth_user_server(storage, name="srv-b", server_id="srv-id-b")
        token_store = _make_token_store(storage)
        _seed_user_token(token_store, server_name="srv-a")
        _seed_user_token(token_store, server_name="srv-b")

        app = _build_app(storage=storage, http_client=http_client_mock, token_store=token_store)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/v1/api/mcp/oauth/connections")
        assert resp.status_code == 200
        body = resp.json()
        assert "connections" in body
        servers = sorted(row["server_name"] for row in body["connections"])
        assert servers == ["srv-a", "srv-b"]

    def test_list_connections_isolates_by_user(
        self, storage: SQLiteBackend, http_client_mock: MagicMock
    ) -> None:
        _seed_oauth_user_server(storage)
        token_store = _make_token_store(storage)
        _seed_user_token(token_store, user_id="user-1", server_name="srv-oauth")
        _seed_user_token(token_store, user_id="user-2", server_name="srv-oauth")

        # User-1 sees only user-1's row.
        app = _build_app(
            storage=storage, http_client=http_client_mock, token_store=token_store, user_id="user-1"
        )
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/v1/api/mcp/oauth/connections")
        rows = resp.json()["connections"]
        assert all(row["user_id"] == "user-1" for row in rows)
        assert len(rows) == 1

        # User-2 sees only user-2's row.
        app2 = _build_app(
            storage=storage, http_client=http_client_mock, token_store=token_store, user_id="user-2"
        )
        client2 = TestClient(app2, raise_server_exceptions=False)
        resp2 = client2.get("/v1/api/mcp/oauth/connections")
        rows2 = resp2.json()["connections"]
        assert all(row["user_id"] == "user-2" for row in rows2)
        assert len(rows2) == 1

    def test_list_connections_does_not_leak_secret_fields(
        self, storage: SQLiteBackend, http_client_mock: MagicMock
    ) -> None:
        _seed_oauth_user_server(storage)
        token_store = _make_token_store(storage)
        _seed_user_token(token_store)

        app = _build_app(storage=storage, http_client=http_client_mock, token_store=token_store)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/v1/api/mcp/oauth/connections")
        rows = resp.json()["connections"]
        assert rows
        for row in rows:
            for forbidden in (
                "access_token",
                "refresh_token",
                "access_token_ct",
                "refresh_token_ct",
            ):
                assert forbidden not in row, f"secret field {forbidden!r} leaked in {row!r}"


# ---------------------------------------------------------------------------
# DELETE /connections/{server_name}
# ---------------------------------------------------------------------------


class TestRevokeConnection:
    def test_revoke_connection_unauthenticated_401(
        self, storage: SQLiteBackend, http_client_mock: MagicMock
    ) -> None:
        _seed_oauth_user_server(storage)
        token_store = _make_token_store(storage)
        _seed_user_token(token_store)
        app = _build_app(
            storage=storage,
            http_client=http_client_mock,
            token_store=token_store,
            authenticated=False,
        )
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.delete("/v1/api/mcp/oauth/connections/srv-oauth")
        assert resp.status_code == 401

    def test_revoke_connection_missing_row_404(
        self, storage: SQLiteBackend, http_client_mock: MagicMock
    ) -> None:
        token_store = _make_token_store(storage)
        app = _build_app(storage=storage, http_client=http_client_mock, token_store=token_store)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.delete("/v1/api/mcp/oauth/connections/srv-nonexistent")
        assert resp.status_code == 404
        assert resp.json() == {"error": "No such connection"}

    def test_revoke_connection_local_delete_succeeds_204(
        self, storage: SQLiteBackend, http_client_mock: MagicMock
    ) -> None:
        _seed_oauth_user_server(storage)
        token_store = _make_token_store(storage)
        # No refresh token → upstream revoke is skipped entirely.
        _seed_user_token(token_store, refresh_token=None)

        app = _build_app(storage=storage, http_client=http_client_mock, token_store=token_store)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.delete("/v1/api/mcp/oauth/connections/srv-oauth")
        assert resp.status_code == 204
        # Local row is gone.
        assert token_store.get_user_token("user-1", "srv-oauth") is None
        # Upstream not contacted.
        http_client_mock.post.assert_not_called()

    def test_revoke_connection_with_revocation_endpoint_calls_upstream(
        self, storage: SQLiteBackend, http_client_mock: MagicMock
    ) -> None:
        _seed_oauth_user_server(storage)
        token_store = _make_token_store(storage)
        _seed_user_token(token_store, refresh_token="refresh-secret")

        http_client_mock.get.return_value = _mk_response(200, _good_as_metadata_doc())
        http_client_mock.post.return_value = _mk_response(200)

        app = _build_app(storage=storage, http_client=http_client_mock, token_store=token_store)
        # ``with TestClient(...)`` keeps a persistent portal so the
        # fire-and-forget upstream-revoke task isn't cancelled when
        # the request handler returns. See ``_drain_revoke_upstream_tasks``.
        # The SSRF-validator's ``socket.getaddrinfo`` patch must wrap
        # the drain too — the discovery call now runs on the background
        # task and resolves the AS hostname after the request returns.
        with (
            TestClient(app, raise_server_exceptions=False) as client,
            _public_addr_patch(),
        ):
            resp = client.delete("/v1/api/mcp/oauth/connections/srv-oauth")

            assert resp.status_code == 204
            # Local row is gone.
            assert token_store.get_user_token("user-1", "srv-oauth") is None
            # The upstream RFC 7009 POST is fire-and-forget post-Phase-8 perf-1
            # so the test must drain the in-flight task set before asserting.
            _drain_revoke_upstream_tasks(client)
            # Upstream POSTed to revocation_endpoint with refresh-token grant.
            assert http_client_mock.post.await_count == 1
            call = http_client_mock.post.await_args
            assert call.args[0] == "https://as.example.com/revoke"
            data = call.kwargs.get("data") or {}
            assert data.get("token") == "refresh-secret"
            assert data.get("token_type_hint") == "refresh_token"
            assert data.get("client_id") == "client-abc"

    def test_revoke_connection_without_revocation_endpoint_skips_upstream(
        self, storage: SQLiteBackend, http_client_mock: MagicMock
    ) -> None:
        _seed_oauth_user_server(storage)
        token_store = _make_token_store(storage)
        _seed_user_token(token_store, refresh_token="refresh-secret")

        http_client_mock.get.return_value = _mk_response(
            200, _good_as_metadata_doc(revocation_endpoint=None)
        )

        app = _build_app(storage=storage, http_client=http_client_mock, token_store=token_store)
        # ``with TestClient(...)`` keeps the portal alive for the
        # background task drain.
        with (
            TestClient(app, raise_server_exceptions=False) as client,
            _public_addr_patch(),
        ):
            resp = client.delete("/v1/api/mcp/oauth/connections/srv-oauth")

            assert resp.status_code == 204
            # Local row gone, upstream POST never made.
            assert token_store.get_user_token("user-1", "srv-oauth") is None
            # Drain the fire-and-forget discovery task before asserting on
            # the AS POST — the task runs ``discover_authorization_server``
            # but does NOT proceed to POST because revocation_endpoint is
            # absent.
            _drain_revoke_upstream_tasks(client)
            http_client_mock.post.assert_not_called()

    def test_revoke_connection_upstream_failure_still_204(
        self, storage: SQLiteBackend, http_client_mock: MagicMock
    ) -> None:
        _seed_oauth_user_server(storage)
        token_store = _make_token_store(storage)
        _seed_user_token(token_store, refresh_token="refresh-secret")

        http_client_mock.get.return_value = _mk_response(200, _good_as_metadata_doc())
        # AS returns 500 — local delete must still succeed.
        http_client_mock.post.return_value = _mk_response(500)

        app = _build_app(storage=storage, http_client=http_client_mock, token_store=token_store)
        client = TestClient(app, raise_server_exceptions=False)

        with _public_addr_patch():
            resp = client.delete("/v1/api/mcp/oauth/connections/srv-oauth")

        assert resp.status_code == 204
        assert token_store.get_user_token("user-1", "srv-oauth") is None

    def test_revoke_connection_audit_event_emitted_with_user_revoked_reason(
        self, storage: SQLiteBackend, http_client_mock: MagicMock
    ) -> None:
        _seed_oauth_user_server(storage)
        token_store = _make_token_store(storage)
        _seed_user_token(token_store, refresh_token=None)

        app = _build_app(storage=storage, http_client=http_client_mock, token_store=token_store)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.delete("/v1/api/mcp/oauth/connections/srv-oauth")
        assert resp.status_code == 204

        # Audit row was written via the storage API (tests don't poke at
        # the SQLite schema directly — the table name is an internal
        # detail).
        events = storage.list_audit_events(action="mcp_server.oauth.token_revoked")
        assert len(events) == 1
        ev = events[0]
        assert ev["user_id"] == "user-1"
        # resource_id is the immutable server_id PK, not the name.
        assert ev["resource_id"] == "srv-id-1"
        import json as _json

        detail = _json.loads(ev["detail"]) if isinstance(ev["detail"], str) else ev["detail"]
        assert detail["reason"] == "user_revoked"
        assert detail["upstream_revoke_outcome"] == "no_refresh_token"
        assert detail["server_name"] == "srv-oauth"

    def test_revoke_connection_cross_user_attempt_404(
        self, storage: SQLiteBackend, http_client_mock: MagicMock
    ) -> None:
        _seed_oauth_user_server(storage)
        token_store = _make_token_store(storage)
        # Owned by user-2, not user-1.
        _seed_user_token(token_store, user_id="user-2", server_name="srv-oauth")

        app = _build_app(
            storage=storage, http_client=http_client_mock, token_store=token_store, user_id="user-1"
        )
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.delete("/v1/api/mcp/oauth/connections/srv-oauth")
        # Cross-user attempt MUST surface as a generic 404, byte-identical
        # body to the never-existed case (no tenant existence leak).
        assert resp.status_code == 404
        assert resp.json() == {"error": "No such connection"}
        # Drain pending tasks defensively, then confirm the upstream
        # endpoint was NEVER contacted on the 404-cross-user path. A
        # bug that scheduled the AS round-trip before the cross-user
        # check would leak existence via the AS-side 200/4xx response.
        _drain_revoke_upstream_tasks(client)
        http_client_mock.post.assert_not_called()
        # User-2's row is untouched.
        assert token_store.get_user_token("user-2", "srv-oauth") is not None

    def test_revoke_connection_evicts_pool_session(
        self, storage: SQLiteBackend, http_client_mock: MagicMock
    ) -> None:
        _seed_oauth_user_server(storage)
        token_store = _make_token_store(storage)
        _seed_user_token(token_store, refresh_token=None)

        mcp_client_mock = MagicMock()
        # ``evict_user_session`` is the public sync surface on
        # MCPClientManager; mirror its signature here so the handler's
        # ``hasattr`` gate triggers.
        mcp_client_mock.evict_user_session = MagicMock(return_value=None)

        app = _build_app(
            storage=storage,
            http_client=http_client_mock,
            token_store=token_store,
            mcp_client=mcp_client_mock,
        )
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.delete("/v1/api/mcp/oauth/connections/srv-oauth")
        assert resp.status_code == 204

        mcp_client_mock.evict_user_session.assert_called_once_with("user-1", "srv-oauth")

    def test_revoke_connection_pool_eviction_failure_does_not_block_204(
        self, storage: SQLiteBackend, http_client_mock: MagicMock
    ) -> None:
        _seed_oauth_user_server(storage)
        token_store = _make_token_store(storage)
        _seed_user_token(token_store, refresh_token=None)

        mcp_client_mock = MagicMock()
        mcp_client_mock.evict_user_session = MagicMock(side_effect=RuntimeError("loop closed"))

        app = _build_app(
            storage=storage,
            http_client=http_client_mock,
            token_store=token_store,
            mcp_client=mcp_client_mock,
        )
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.delete("/v1/api/mcp/oauth/connections/srv-oauth")
        assert resp.status_code == 204
        # Local delete still happened.
        assert token_store.get_user_token("user-1", "srv-oauth") is None

    def test_revoke_connection_204_not_gated_on_slow_upstream(
        self, storage: SQLiteBackend, http_client_mock: MagicMock
    ) -> None:
        """The user-visible 204 must return promptly even when the
        upstream AS round-trip is slow / hanging. Pre-perf-1 the
        handler awaited ``revoke_token_at_as`` synchronously, so a
        stuck AS could block the user's revoke confirmation. The
        fire-and-forget refactor moves the call onto a background task
        so the 204 returns in well under 1s regardless of AS latency.
        Bound is conservative for CI runner jitter.
        """
        import time

        _seed_oauth_user_server(storage)
        token_store = _make_token_store(storage)
        _seed_user_token(token_store, refresh_token="refresh-secret")

        http_client_mock.get.return_value = _mk_response(200, _good_as_metadata_doc())

        async def _slow_post(*_args: Any, **_kwargs: Any) -> Any:
            # Simulate a slow / unreachable AS — must NOT gate the
            # user-visible 204 on this round-trip.
            await asyncio.sleep(5.0)
            return _mk_response(200)

        http_client_mock.post = AsyncMock(side_effect=_slow_post)

        app = _build_app(storage=storage, http_client=http_client_mock, token_store=token_store)
        client = TestClient(app, raise_server_exceptions=False)

        with _public_addr_patch():
            start = time.monotonic()
            resp = client.delete("/v1/api/mcp/oauth/connections/srv-oauth")
            elapsed = time.monotonic() - start

        assert resp.status_code == 204
        # 1s ceiling — the 204 must return on the local-delete path
        # without waiting on the AS POST (which sleeps 5s above). Bound
        # is intentionally generous for CI runner jitter; the actual
        # path is on the order of milliseconds.
        assert elapsed < 1.0, (
            f"204 returned in {elapsed:.3f}s — should be <1s; the "
            "fire-and-forget upstream revoke isn't decoupled from the "
            "response."
        )
        # The local row IS gone — the authoritative delete ran before
        # the 204 returned, even though the AS round-trip is still
        # in flight.
        assert token_store.get_user_token("user-1", "srv-oauth") is None
        # Cancel any in-flight tasks so the test client can exit cleanly.
        from turnstone.core.mcp_oauth import _revoke_upstream_tasks

        portal = getattr(client, "portal", None)
        if portal is not None:
            for task in list(_revoke_upstream_tasks):
                portal.call(task.cancel)

    def test_revoke_connection_sheds_upstream_when_task_set_full(
        self, storage: SQLiteBackend, http_client_mock: MagicMock
    ) -> None:
        """Round-2 q-2 regression: the soft cap on ``_revoke_upstream_tasks``
        is the only protection against unbounded background-task pile-up
        under a coordinated mass-revoke. When the set is full, the local
        delete still runs but no upstream task is scheduled; the audit
        detail records ``upstream_revoke_outcome="shed_by_cap"`` and
        the AS endpoint is never contacted.
        """
        from turnstone.core.mcp_oauth import (
            _REVOKE_UPSTREAM_TASKS_MAX,
            _revoke_upstream_tasks,
        )

        _seed_oauth_user_server(storage)
        token_store = _make_token_store(storage)
        _seed_user_token(token_store, refresh_token="refresh-secret")

        app = _build_app(storage=storage, http_client=http_client_mock, token_store=token_store)
        sentinel_event_holder: dict[str, asyncio.Event] = {}

        # Use ``with TestClient(...)`` so the portal stays alive — we
        # need to schedule sentinel tasks on the portal's loop and the
        # tasks must outlive the request to actually fill the set.
        with (
            TestClient(app, raise_server_exceptions=False) as client,
            _public_addr_patch(),
        ):
            portal = client.portal
            assert portal is not None

            async def _create_sentinel_event() -> asyncio.Event:
                event = asyncio.Event()
                sentinel_event_holder["event"] = event
                return event

            sentinel_event = portal.call(_create_sentinel_event)

            async def _wait_on_event() -> None:
                await sentinel_event.wait()

            async def _fill_task_set() -> list[asyncio.Task[None]]:
                tasks: list[asyncio.Task[None]] = []
                for _ in range(_REVOKE_UPSTREAM_TASKS_MAX):
                    t = asyncio.create_task(_wait_on_event())
                    _revoke_upstream_tasks.add(t)
                    tasks.append(t)
                return tasks

            sentinels = portal.call(_fill_task_set)
            assert len(_revoke_upstream_tasks) >= _REVOKE_UPSTREAM_TASKS_MAX

            try:
                resp = client.delete("/v1/api/mcp/oauth/connections/srv-oauth")
                assert resp.status_code == 204
                # Local row is still gone — authoritative delete ran.
                assert token_store.get_user_token("user-1", "srv-oauth") is None
                # AS endpoint MUST NOT have been contacted.
                http_client_mock.post.assert_not_called()
                # Audit detail records the categorical shed outcome.
                events = storage.list_audit_events(action="mcp_server.oauth.token_revoked")
                assert len(events) == 1
                detail = events[0]["detail"]
                if isinstance(detail, str):
                    import json as _json

                    detail = _json.loads(detail)
                assert detail["upstream_revoke_outcome"] == "shed_by_cap"
            finally:
                # Release sentinels so the portal can shut down cleanly.
                async def _release() -> None:
                    sentinel_event.set()
                    for t in sentinels:
                        t.cancel()
                    await asyncio.gather(*sentinels, return_exceptions=True)

                portal.call(_release)


# ---------------------------------------------------------------------------
# evict_user_session helper sanity checks
# ---------------------------------------------------------------------------


class TestEvictUserSession:
    def test_evict_user_session_no_loop_is_silent_noop(self) -> None:
        from turnstone.core.mcp_client import MCPClientManager

        mgr = MCPClientManager.__new__(MCPClientManager)
        mgr._loop = None  # type: ignore[attr-defined]
        # Must not raise.
        mgr.evict_user_session("user-1", "srv-oauth")

    def test_evict_user_session_dispatches_to_loop(self) -> None:
        from turnstone.core.mcp_client import MCPClientManager

        mgr = MCPClientManager.__new__(MCPClientManager)
        loop = asyncio.new_event_loop()
        try:
            mgr._loop = loop  # type: ignore[attr-defined]
            mgr._user_pool_entries = {}  # type: ignore[attr-defined]
            mgr._last_pool_notification_refresh = {}  # type: ignore[attr-defined]
            evicted: list[tuple[str, str]] = []

            def _fake_evict(key: tuple[str, str]) -> None:
                evicted.append(key)

            mgr._evict_session = _fake_evict  # type: ignore[method-assign]

            # Run the dispatch on a separate thread so the loop can drain.
            import threading

            done = threading.Event()

            def _run_loop() -> None:
                loop.call_later(0.05, loop.stop)
                loop.run_forever()
                done.set()

            t = threading.Thread(target=_run_loop, daemon=True)
            t.start()
            mgr.evict_user_session("user-1", "srv-oauth")
            done.wait(timeout=1.0)

            assert evicted == [("user-1", "srv-oauth")]
        finally:
            if not loop.is_closed():
                loop.close()
