"""Integration tests for the MCP OAuth HTTP handlers.

Mirrors the structure of ``tests/test_oidc_handlers.py``: builds a small
Starlette app with the ``/api/mcp/oauth/start`` and ``/api/mcp/oauth/callback``
routes wired in, mocks the ``httpx.AsyncClient`` calls into the AS, and
exercises both happy and failure paths.
"""

from __future__ import annotations

import asyncio
import base64
import json
import urllib.parse
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
    _validate_return_url,
    handle_mcp_oauth_authorize,
    handle_mcp_oauth_callback,
)
from turnstone.core.oidc import OIDCConfig
from turnstone.core.storage._sqlite import SQLiteBackend

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _InjectAuthMiddleware(BaseHTTPMiddleware):
    """Stamp a fixed authenticated user on every request."""

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        request.state.auth_result = AuthResult(
            user_id="user-1",
            scopes=frozenset({"write"}),
            token_source="config",
            permissions=frozenset({"read", "write"}),
        )
        return await call_next(request)


async def _mcp_authorize(request: Request) -> Response:
    return await handle_mcp_oauth_authorize(request)


async def _mcp_callback(request: Request) -> Response:
    return await handle_mcp_oauth_callback(request)


def _build_app(
    *,
    storage: SQLiteBackend,
    http_client: httpx.AsyncClient,
    token_store: MCPTokenStore | None,
    redirect_base: str = "https://testserver",
) -> Starlette:
    app = Starlette(
        routes=[
            Mount(
                "/v1",
                routes=[
                    Route("/api/mcp/oauth/start", _mcp_authorize),
                    Route("/api/mcp/oauth/callback", _mcp_callback),
                ],
            ),
        ],
        middleware=[Middleware(_InjectAuthMiddleware)],
    )
    app.state.auth_storage = storage
    app.state.mcp_token_store = token_store
    app.state.mcp_oauth_http_client = http_client
    app.state.mcp_oauth_refresh_locks = {}
    app.state.mcp_oauth_dcr_locks = {}
    app.state.mcp_oauth_metadata_cache = {}
    app.state.mcp_oauth_last_cleanup_monotonic = 0.0
    # Mirror the OIDC redirect_base contract — the MCP OAuth handlers
    # reuse it to pin the callback URL against Host-header injection.
    app.state.oidc_config = OIDCConfig(
        enabled=False,
        redirect_base=redirect_base,
    )
    return app


def _make_token_store(backend: SQLiteBackend) -> MCPTokenStore:
    return MCPTokenStore(backend, make_mcp_token_cipher(), node_id="test")


def _seed_oauth_user_server(
    backend: SQLiteBackend,
    *,
    name: str = "srv-oauth",
    server_id: str = "srv-id-1",
    client_id: str | None = "client-abc",
    cached_issuer: str | None = "https://as.example.com",
    registration_mode: str | None = None,
) -> str:
    backend.create_mcp_server(
        server_id=server_id,
        name=name,
        transport="streamable-http",
        url="https://mcp.example.com/sse",
        auth_type="oauth_user",
        oauth_client_id=client_id,
        oauth_scopes="openid profile",
        oauth_audience="https://mcp.example.com",
        oauth_authorization_server_url=None,
        oauth_registration_mode=registration_mode,
    )
    if cached_issuer is not None:
        backend.update_mcp_server(server_id, oauth_as_issuer_cached=cached_issuer)
    return server_id


def _good_as_metadata_doc() -> dict[str, Any]:
    return {
        "issuer": "https://as.example.com",
        "authorization_endpoint": "https://as.example.com/authorize",
        "token_endpoint": "https://as.example.com/token",
        "registration_endpoint": "https://as.example.com/register",
        "jwks_uri": "https://as.example.com/jwks",
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none", "client_secret_basic"],
    }


def _mk_response(
    status_code: int = 200,
    json_body: Any = None,
    headers: dict[str, str] | None = None,
) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.headers = headers or {}
    body_str = (
        json.dumps(json_body) if json_body is not None and not isinstance(json_body, str) else ""
    )
    resp.content = body_str.encode("utf-8")
    if json_body is not None:
        resp.json.return_value = json_body
    else:
        resp.json.side_effect = ValueError("no body")
    resp.text = body_str
    return resp


def _public_addr_patch():
    return patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 0))])


@pytest.fixture
def storage(tmp_path: Any) -> SQLiteBackend:
    backend = SQLiteBackend(str(tmp_path / "test.db"))
    backend.create_user("user-1", "user1", "User One", "hash")
    return backend


@pytest.fixture
def http_client_mock() -> MagicMock:
    client = MagicMock(spec=httpx.AsyncClient)
    client.get = AsyncMock()
    client.post = AsyncMock()
    return client


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------


class TestAuthorize:
    def test_happy_path_redirects_to_as(
        self, storage: SQLiteBackend, http_client_mock: MagicMock
    ) -> None:
        _seed_oauth_user_server(storage)
        token_store = _make_token_store(storage)
        http_client_mock.get.return_value = _mk_response(200, _good_as_metadata_doc())

        app = _build_app(storage=storage, http_client=http_client_mock, token_store=token_store)
        client = TestClient(app, raise_server_exceptions=False)

        with _public_addr_patch():
            resp = client.get(
                "/v1/api/mcp/oauth/start?server=srv-oauth&return_url=/admin/mcp-servers",
                follow_redirects=False,
            )

        assert resp.status_code == 302
        location = resp.headers["location"]
        parsed = urllib.parse.urlparse(location)
        assert parsed.scheme == "https"
        assert parsed.netloc == "as.example.com"
        params = urllib.parse.parse_qs(parsed.query)
        assert params["client_id"] == ["client-abc"]
        assert params["response_type"] == ["code"]
        assert params["code_challenge_method"] == ["S256"]
        assert "code_challenge" in params
        assert "state" in params
        assert params["resource"] == ["https://mcp.example.com/sse"]

    def test_unknown_server_404(self, storage: SQLiteBackend, http_client_mock: MagicMock) -> None:
        token_store = _make_token_store(storage)
        app = _build_app(storage=storage, http_client=http_client_mock, token_store=token_store)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/v1/api/mcp/oauth/start?server=nope")
        assert resp.status_code == 404

    def test_wrong_auth_type_400(self, storage: SQLiteBackend, http_client_mock: MagicMock) -> None:
        storage.create_mcp_server(
            server_id="static-1",
            name="srv-static",
            transport="streamable-http",
            url="https://x.example.com",
            auth_type="static",
        )
        token_store = _make_token_store(storage)
        app = _build_app(storage=storage, http_client=http_client_mock, token_store=token_store)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/v1/api/mcp/oauth/start?server=srv-static")
        assert resp.status_code == 400
        assert "per-user OAuth" in resp.json()["error"]

    def test_missing_server_param_400(
        self, storage: SQLiteBackend, http_client_mock: MagicMock
    ) -> None:
        token_store = _make_token_store(storage)
        app = _build_app(storage=storage, http_client=http_client_mock, token_store=token_store)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/v1/api/mcp/oauth/start")
        assert resp.status_code == 400

    def test_as_without_s256_redirects_with_error(
        self, storage: SQLiteBackend, http_client_mock: MagicMock
    ) -> None:
        _seed_oauth_user_server(storage)
        token_store = _make_token_store(storage)
        bad_doc = _good_as_metadata_doc()
        bad_doc["code_challenge_methods_supported"] = ["plain"]
        http_client_mock.get.return_value = _mk_response(200, bad_doc)

        app = _build_app(storage=storage, http_client=http_client_mock, token_store=token_store)
        client = TestClient(app, raise_server_exceptions=False)

        with _public_addr_patch():
            resp = client.get("/v1/api/mcp/oauth/start?server=srv-oauth", follow_redirects=False)

        assert resp.status_code == 502
        assert "S256" in resp.json()["error"]

    def test_pending_state_persisted(
        self, storage: SQLiteBackend, http_client_mock: MagicMock
    ) -> None:
        _seed_oauth_user_server(storage)
        token_store = _make_token_store(storage)
        http_client_mock.get.return_value = _mk_response(200, _good_as_metadata_doc())

        app = _build_app(storage=storage, http_client=http_client_mock, token_store=token_store)
        client = TestClient(app, raise_server_exceptions=False)

        with _public_addr_patch():
            resp = client.get("/v1/api/mcp/oauth/start?server=srv-oauth", follow_redirects=False)

        location = resp.headers["location"]
        params = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)
        state = params["state"][0]
        pending = storage.pop_mcp_oauth_pending_state(state)
        assert pending is not None
        assert pending["user_id"] == "user-1"
        assert pending["server_name"] == "srv-oauth"

    def test_return_url_cross_origin_falls_back(
        self, storage: SQLiteBackend, http_client_mock: MagicMock
    ) -> None:
        _seed_oauth_user_server(storage)
        token_store = _make_token_store(storage)
        http_client_mock.get.return_value = _mk_response(200, _good_as_metadata_doc())

        app = _build_app(storage=storage, http_client=http_client_mock, token_store=token_store)
        client = TestClient(app, raise_server_exceptions=False)

        with _public_addr_patch():
            resp = client.get(
                "/v1/api/mcp/oauth/start"
                "?server=srv-oauth&return_url=https://attacker.example.com/x",
                follow_redirects=False,
            )

        assert resp.status_code == 302
        location = resp.headers["location"]
        params = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)
        # Pull pending and verify the return_url was sanitised to "/".
        pending = storage.pop_mcp_oauth_pending_state(params["state"][0])
        assert pending is not None
        assert pending["return_url"] == "/"

    def test_authorize_scope_param_unioned_with_configured_scopes(
        self, storage: SQLiteBackend, http_client_mock: MagicMock
    ) -> None:
        _seed_oauth_user_server(storage)
        token_store = _make_token_store(storage)
        http_client_mock.get.return_value = _mk_response(200, _good_as_metadata_doc())

        app = _build_app(storage=storage, http_client=http_client_mock, token_store=token_store)
        client = TestClient(app, raise_server_exceptions=False)

        with _public_addr_patch():
            resp = client.get(
                "/v1/api/mcp/oauth/start?server=srv-oauth&scopes=email%20admin",
                follow_redirects=False,
            )

        assert resp.status_code == 302
        params = urllib.parse.parse_qs(urllib.parse.urlparse(resp.headers["location"]).query)
        # Configured = "openid profile"; requested = "email admin".
        # Union sorted alphabetically.
        scope_param = params["scope"][0]
        assert sorted(scope_param.split(" ")) == ["admin", "email", "openid", "profile"]

    def test_authorize_scope_param_dedupe_and_sort(
        self, storage: SQLiteBackend, http_client_mock: MagicMock
    ) -> None:
        _seed_oauth_user_server(storage)
        token_store = _make_token_store(storage)
        http_client_mock.get.return_value = _mk_response(200, _good_as_metadata_doc())

        app = _build_app(storage=storage, http_client=http_client_mock, token_store=token_store)
        client = TestClient(app, raise_server_exceptions=False)

        # Caller passes scopes that overlap with the configured set + an
        # extra. Ordering is intentionally unsorted to exercise the
        # deterministic-sort property.
        with _public_addr_patch():
            resp = client.get(
                "/v1/api/mcp/oauth/start?server=srv-oauth"
                "&scopes=zzz%20openid%20aaa%20openid%20profile",
                follow_redirects=False,
            )

        assert resp.status_code == 302
        params = urllib.parse.parse_qs(urllib.parse.urlparse(resp.headers["location"]).query)
        # Sorted, deduped union.
        assert params["scope"][0] == "aaa openid profile zzz"

    def test_authorize_scope_param_rejects_invalid_token(
        self, storage: SQLiteBackend, http_client_mock: MagicMock
    ) -> None:
        _seed_oauth_user_server(storage)
        token_store = _make_token_store(storage)
        http_client_mock.get.return_value = _mk_response(200, _good_as_metadata_doc())

        app = _build_app(storage=storage, http_client=http_client_mock, token_store=token_store)
        client = TestClient(app, raise_server_exceptions=False)

        # CR/LF, NUL, backslash, and double-quote each individually fail
        # the RFC 6749 §3.3 scope-token grammar; ``request.query_params``
        # already URL-decodes so we send the encoded form.
        for hostile in ("a%0Db", "a%0Ab", "a%00b", 'a"b', "a\\b"):
            with _public_addr_patch():
                resp = client.get(
                    f"/v1/api/mcp/oauth/start?server=srv-oauth&scopes={hostile}",
                    follow_redirects=False,
                )
            assert resp.status_code == 400, f"hostile={hostile!r} got {resp.status_code}"
            assert resp.json() == {"error": "Invalid scope token"}

    def test_authorize_scope_param_rejects_over_cap(
        self, storage: SQLiteBackend, http_client_mock: MagicMock
    ) -> None:
        _seed_oauth_user_server(storage)
        token_store = _make_token_store(storage)
        http_client_mock.get.return_value = _mk_response(200, _good_as_metadata_doc())

        app = _build_app(storage=storage, http_client=http_client_mock, token_store=token_store)
        client = TestClient(app, raise_server_exceptions=False)

        # 33 tokens — one over the cap (``MAX_INSUFFICIENT_SCOPE_REPORTED = 32``).
        scopes = "%20".join(f"s{i}" for i in range(33))
        with _public_addr_patch():
            resp = client.get(
                f"/v1/api/mcp/oauth/start?server=srv-oauth&scopes={scopes}",
                follow_redirects=False,
            )
        assert resp.status_code == 400
        assert resp.json() == {"error": "Invalid scope token"}

    def test_authorize_no_scope_param_passes_configured_scope_unchanged(
        self, storage: SQLiteBackend, http_client_mock: MagicMock
    ) -> None:
        # Static-path invariant: when no ``scopes`` query param is passed,
        # the AS sees the configured scope string verbatim (not run
        # through sort/dedup). Configured = "openid profile" → AS
        # receives "openid profile" in the same order.
        _seed_oauth_user_server(storage)
        token_store = _make_token_store(storage)
        http_client_mock.get.return_value = _mk_response(200, _good_as_metadata_doc())

        app = _build_app(storage=storage, http_client=http_client_mock, token_store=token_store)
        client = TestClient(app, raise_server_exceptions=False)

        with _public_addr_patch():
            resp = client.get("/v1/api/mcp/oauth/start?server=srv-oauth", follow_redirects=False)

        assert resp.status_code == 302
        params = urllib.parse.parse_qs(urllib.parse.urlparse(resp.headers["location"]).query)
        assert params["scope"] == ["openid profile"]


# ---------------------------------------------------------------------------
# /callback
# ---------------------------------------------------------------------------


class TestCallback:
    def _seed_pending(
        self,
        storage: SQLiteBackend,
        *,
        state: str = "valid-state",
        user_id: str = "user-1",
        server_name: str = "srv-oauth",
        verifier: str = "verifier-blob",
        return_url: str = "/admin/mcp-servers",
    ) -> None:
        storage.create_mcp_oauth_pending_state(state, user_id, server_name, verifier, return_url)

    def test_happy_path_persists_token_and_redirects(
        self, storage: SQLiteBackend, http_client_mock: MagicMock
    ) -> None:
        _seed_oauth_user_server(storage)
        self._seed_pending(storage)
        token_store = _make_token_store(storage)

        http_client_mock.get.return_value = _mk_response(200, _good_as_metadata_doc())
        http_client_mock.post.return_value = _mk_response(
            200,
            {
                "access_token": "opaque-access-aaa",
                "refresh_token": "refresh-bbb",
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": "openid profile",
            },
        )

        app = _build_app(storage=storage, http_client=http_client_mock, token_store=token_store)
        client = TestClient(app, raise_server_exceptions=False)

        with _public_addr_patch():
            resp = client.get(
                "/v1/api/mcp/oauth/callback?code=auth-code&state=valid-state",
                follow_redirects=False,
            )

        assert resp.status_code == 302
        assert resp.headers["location"] == "/admin/mcp-servers"
        # Token row was persisted.
        plain = token_store.get_user_token("user-1", "srv-oauth")
        assert plain is not None
        assert plain["access_token"] == "opaque-access-aaa"
        assert plain["refresh_token"] == "refresh-bbb"

    def test_state_mismatch_redirects(
        self, storage: SQLiteBackend, http_client_mock: MagicMock
    ) -> None:
        _seed_oauth_user_server(storage)
        token_store = _make_token_store(storage)
        app = _build_app(storage=storage, http_client=http_client_mock, token_store=token_store)
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.get(
            "/v1/api/mcp/oauth/callback?code=auth-code&state=does-not-exist",
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert "session+expired" in resp.headers["location"]

    def test_user_id_mismatch_redirects(
        self, storage: SQLiteBackend, http_client_mock: MagicMock
    ) -> None:
        _seed_oauth_user_server(storage)
        # Pending row attributes the flow to a different user.
        self._seed_pending(storage, user_id="other-user")
        token_store = _make_token_store(storage)
        app = _build_app(storage=storage, http_client=http_client_mock, token_store=token_store)
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.get(
            "/v1/api/mcp/oauth/callback?code=c&state=valid-state",
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert "user+mismatch" in resp.headers["location"]

    def test_as_error_redirects_with_message(
        self, storage: SQLiteBackend, http_client_mock: MagicMock
    ) -> None:
        token_store = _make_token_store(storage)
        app = _build_app(storage=storage, http_client=http_client_mock, token_store=token_store)
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.get(
            "/v1/api/mcp/oauth/callback?error=access_denied&error_description=user+cancelled",
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert "mcp_oauth_error" in resp.headers["location"]

    def test_jwt_audience_mismatch_redirects(
        self, storage: SQLiteBackend, http_client_mock: MagicMock
    ) -> None:
        _seed_oauth_user_server(storage)
        self._seed_pending(storage)
        token_store = _make_token_store(storage)

        # Build a JWT with a wrong audience.
        header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
        payload = (
            base64.urlsafe_b64encode(json.dumps({"aud": "https://wrong.example.com"}).encode())
            .rstrip(b"=")
            .decode()
        )
        bad_jwt = f"{header}.{payload}.sig"

        http_client_mock.get.return_value = _mk_response(200, _good_as_metadata_doc())
        http_client_mock.post.return_value = _mk_response(
            200,
            {"access_token": bad_jwt, "expires_in": 3600},
        )

        app = _build_app(storage=storage, http_client=http_client_mock, token_store=token_store)
        client = TestClient(app, raise_server_exceptions=False)

        with _public_addr_patch():
            resp = client.get(
                "/v1/api/mcp/oauth/callback?code=c&state=valid-state",
                follow_redirects=False,
            )

        assert resp.status_code == 302
        assert "audience+mismatch" in resp.headers["location"]
        # And no token row written.
        assert token_store.get_user_token("user-1", "srv-oauth") is None

    def test_opaque_token_logs_and_trusts(
        self, storage: SQLiteBackend, http_client_mock: MagicMock
    ) -> None:
        _seed_oauth_user_server(storage)
        self._seed_pending(storage)
        token_store = _make_token_store(storage)

        http_client_mock.get.return_value = _mk_response(200, _good_as_metadata_doc())
        http_client_mock.post.return_value = _mk_response(
            200, {"access_token": "opaque-no-dots", "expires_in": 3600}
        )

        app = _build_app(storage=storage, http_client=http_client_mock, token_store=token_store)
        client = TestClient(app, raise_server_exceptions=False)

        with _public_addr_patch():
            resp = client.get(
                "/v1/api/mcp/oauth/callback?code=c&state=valid-state",
                follow_redirects=False,
            )

        # Opaque tokens are trusted per the documented contract.
        assert resp.status_code == 302
        assert resp.headers["location"] == "/admin/mcp-servers"
        plain = token_store.get_user_token("user-1", "srv-oauth")
        assert plain is not None
        assert plain["access_token"] == "opaque-no-dots"

    def test_refresh_token_omitted_creates_row_without_refresh(
        self, storage: SQLiteBackend, http_client_mock: MagicMock
    ) -> None:
        _seed_oauth_user_server(storage)
        self._seed_pending(storage)
        token_store = _make_token_store(storage)

        http_client_mock.get.return_value = _mk_response(200, _good_as_metadata_doc())
        http_client_mock.post.return_value = _mk_response(
            200, {"access_token": "opaque-access", "expires_in": 3600}
        )

        app = _build_app(storage=storage, http_client=http_client_mock, token_store=token_store)
        client = TestClient(app, raise_server_exceptions=False)

        with _public_addr_patch():
            client.get(
                "/v1/api/mcp/oauth/callback?code=c&state=valid-state",
                follow_redirects=False,
            )

        plain = token_store.get_user_token("user-1", "srv-oauth")
        assert plain is not None
        assert plain["refresh_token"] is None

    def test_callback_clears_pending_consent_on_success(
        self, storage: SQLiteBackend, http_client_mock: MagicMock
    ) -> None:
        """Successful callback must drop any ``mcp_pending_consent`` rows
        for the just-consented ``(user, server)`` (Phase 9 lifecycle
        contract).  Regression guard for the dashboard-stays-stale-after-
        consent invariant.
        """
        _seed_oauth_user_server(storage)
        self._seed_pending(storage)
        # Seed a deferred-consent record that a prior non-interactive run
        # would have left behind.  Plus a cross-tenant record that must
        # NOT be touched.
        storage.upsert_mcp_pending_consent(
            user_id="user-1",
            server_name="srv-oauth",
            error_code="mcp_consent_required",
            scopes_required=None,
            last_ws_id="ws-1",
            last_tool_call_id="tool-1",
            now_iso="2026-05-11T12:00:00",
        )
        storage.upsert_mcp_pending_consent(
            user_id="other-user",
            server_name="srv-oauth",
            error_code="mcp_consent_required",
            scopes_required=None,
            last_ws_id=None,
            last_tool_call_id=None,
            now_iso="2026-05-11T12:00:00",
        )
        token_store = _make_token_store(storage)
        http_client_mock.get.return_value = _mk_response(200, _good_as_metadata_doc())
        http_client_mock.post.return_value = _mk_response(
            200,
            {"access_token": "opaque-aaa", "expires_in": 3600},
        )
        app = _build_app(storage=storage, http_client=http_client_mock, token_store=token_store)
        client = TestClient(app, raise_server_exceptions=False)

        with _public_addr_patch():
            resp = client.get(
                "/v1/api/mcp/oauth/callback?code=c&state=valid-state",
                follow_redirects=False,
            )
        assert resp.status_code == 302
        # Callback completed → user-1's deferred-consent row was cleared.
        assert storage.list_mcp_pending_consent_by_user("user-1") == []
        # Cross-tenant row survives — clear is per-(user, server).
        other = storage.list_mcp_pending_consent_by_user("other-user")
        assert len(other) == 1
        assert other[0]["server_name"] == "srv-oauth"

    def test_callback_storage_failure_does_not_block_redirect(
        self, storage: SQLiteBackend, http_client_mock: MagicMock
    ) -> None:
        """If the post-persist ``delete_mcp_pending_consent`` raises, the
        callback's redirect still completes (best-effort contract).  The
        stale badge is preferred over a broken consent flow.
        """
        _seed_oauth_user_server(storage)
        self._seed_pending(storage)
        token_store = _make_token_store(storage)
        http_client_mock.get.return_value = _mk_response(200, _good_as_metadata_doc())
        http_client_mock.post.return_value = _mk_response(
            200,
            {"access_token": "opaque-aaa", "expires_in": 3600},
        )
        app = _build_app(storage=storage, http_client=http_client_mock, token_store=token_store)
        client = TestClient(app, raise_server_exceptions=False)

        original_delete = storage.delete_mcp_pending_consent

        def _raise(*_a: Any, **_kw: Any) -> bool:
            raise RuntimeError("storage offline")

        storage.delete_mcp_pending_consent = _raise  # type: ignore[method-assign]
        try:
            with _public_addr_patch():
                resp = client.get(
                    "/v1/api/mcp/oauth/callback?code=c&state=valid-state",
                    follow_redirects=False,
                )
        finally:
            storage.delete_mcp_pending_consent = original_delete  # type: ignore[method-assign]

        assert resp.status_code == 302
        # Token persistence still succeeded — the user-visible contract.
        plain = token_store.get_user_token("user-1", "srv-oauth")
        assert plain is not None


# ---------------------------------------------------------------------------
# 503 paths when mcp_token_store is None
# ---------------------------------------------------------------------------


class TestNoTokenStore:
    def test_start_503(self, storage: SQLiteBackend) -> None:
        client_obj = MagicMock(spec=httpx.AsyncClient)
        app = _build_app(storage=storage, http_client=client_obj, token_store=None)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/v1/api/mcp/oauth/start?server=anything")
        assert resp.status_code == 503
        body = resp.json()
        assert "mcp_token_encryption_key" in body["hint"]

    def test_callback_503(self, storage: SQLiteBackend) -> None:
        client_obj = MagicMock(spec=httpx.AsyncClient)
        app = _build_app(storage=storage, http_client=client_obj, token_store=None)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/v1/api/mcp/oauth/callback?code=x&state=y")
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# DCR
# ---------------------------------------------------------------------------


class TestDCR:
    def test_registration_endpoint_hit_when_dcr_and_no_client_id(
        self, storage: SQLiteBackend, http_client_mock: MagicMock
    ) -> None:
        _seed_oauth_user_server(
            storage,
            client_id=None,
            registration_mode="dcr",
        )
        token_store = _make_token_store(storage)

        async def _post(url, *args, **kwargs):
            if url.endswith("/register"):
                return _mk_response(
                    201, {"client_id": "dcr-client-xyz", "client_secret": "dcr-secret"}
                )
            raise AssertionError(f"unexpected POST {url}")

        http_client_mock.get.return_value = _mk_response(200, _good_as_metadata_doc())
        http_client_mock.post.side_effect = _post

        app = _build_app(storage=storage, http_client=http_client_mock, token_store=token_store)
        client = TestClient(app, raise_server_exceptions=False)

        with _public_addr_patch():
            resp = client.get("/v1/api/mcp/oauth/start?server=srv-oauth", follow_redirects=False)

        assert resp.status_code == 302
        # client_id was persisted on the row.
        row = storage.get_mcp_server_by_name("srv-oauth")
        assert row is not None
        assert row["oauth_client_id"] == "dcr-client-xyz"
        # Client secret was encrypted + stored.
        secret = token_store.get_oauth_client_secret(row["server_id"])
        assert secret == "dcr-secret"

    def test_concurrent_dcr_callers_register_once(
        self, storage: SQLiteBackend, http_client_mock: MagicMock
    ) -> None:
        """Two concurrent /start calls against a DCR server must register exactly once.

        Without the per-server lock + re-fetch, both callers race past
        the NULL ``oauth_client_id`` check and both POST to /register.
        The second registration's client_id then overwrites the first
        in the row, leaving the first user's authorize URL pointing at
        a client_id that never makes it back to /callback (the row has
        a different one), so the AS rejects the code exchange.
        """
        _seed_oauth_user_server(
            storage,
            client_id=None,
            registration_mode="dcr",
        )
        token_store = _make_token_store(storage)
        register_calls = 0
        register_started = asyncio.Event()
        register_release = asyncio.Event()

        async def _post(url, *args, **kwargs):
            nonlocal register_calls
            if url.endswith("/register"):
                register_calls += 1
                register_started.set()
                # Block first caller inside the AS POST so the second
                # caller is forced to take the DCR lock contended.
                await register_release.wait()
                return _mk_response(
                    201,
                    {"client_id": f"dcr-client-{register_calls}", "client_secret": "secret"},
                )
            raise AssertionError(f"unexpected POST {url}")

        http_client_mock.get.return_value = _mk_response(200, _good_as_metadata_doc())
        http_client_mock.post.side_effect = _post

        # Drive both /start handlers directly through ASGI to share the
        # same app.state.mcp_oauth_dcr_locks dict — TestClient spawns its
        # own thread loop per call, so direct invocation is the
        # cleanest way to exercise the lock.
        from starlette.requests import Request

        async def _make_request(app: Starlette) -> Request:
            scope = {
                "type": "http",
                "method": "GET",
                "path": "/v1/api/mcp/oauth/start",
                "raw_path": b"/v1/api/mcp/oauth/start",
                "query_string": b"server=srv-oauth",
                "headers": [(b"host", b"app.example.com")],
                "scheme": "https",
                "server": ("app.example.com", 443),
                "app": app,
            }

            async def _receive() -> dict[str, Any]:
                return {"type": "http.request", "body": b"", "more_body": False}

            req = Request(scope, _receive)
            req.state.auth_result = AuthResult(
                user_id="user-1",
                scopes=frozenset({"write"}),
                token_source="config",
                permissions=frozenset({"read", "write"}),
            )
            return req

        app = _build_app(storage=storage, http_client=http_client_mock, token_store=token_store)

        async def _two_concurrent() -> tuple[Any, Any]:
            with _public_addr_patch():
                req1 = await _make_request(app)
                req2 = await _make_request(app)
                t1 = asyncio.create_task(handle_mcp_oauth_authorize(req1))
                # Wait until the first caller is inside the /register POST
                # so the second caller has to queue on the DCR lock.
                await register_started.wait()
                t2 = asyncio.create_task(handle_mcp_oauth_authorize(req2))
                # Give t2 a chance to enter _register_dynamic_client_if_needed
                # and block on the lock.
                await asyncio.sleep(0.05)
                register_release.set()
                return await asyncio.gather(t1, t2)

        resp1, resp2 = asyncio.run(_two_concurrent())

        # Exactly one /register POST hit the AS.
        assert register_calls == 1
        # Both /start handlers redirected (302) using the SAME client_id.
        assert resp1.status_code == 302
        assert resp2.status_code == 302
        loc1 = urllib.parse.parse_qs(urllib.parse.urlparse(resp1.headers["location"]).query)
        loc2 = urllib.parse.parse_qs(urllib.parse.urlparse(resp2.headers["location"]).query)
        assert loc1["client_id"] == loc2["client_id"]
        # The persisted row shows the single client_id from the first
        # (only) registration.
        row = storage.get_mcp_server_by_name("srv-oauth")
        assert row is not None
        assert row["oauth_client_id"] == "dcr-client-1"


# ---------------------------------------------------------------------------
# JWT audience handling — bug-3 + sec-1 + bug-5
# ---------------------------------------------------------------------------


class TestJWTAudienceHandling:
    """Operator-set ``oauth_audience`` must be honored by JWT aud validation.

    Auth0 (and similar non-RFC-8707 ASes) read the ``audience=`` URL
    parameter and mint tokens with ``aud=<oauth_audience>`` rather than
    the canonical resource URL. Phase 4 used the canonical URL only,
    so legitimate Auth0 tokens were rejected.
    """

    def _seed_pending(
        self,
        storage: SQLiteBackend,
        *,
        state: str = "valid-state",
        verifier: str = "verifier-blob",
    ) -> None:
        storage.create_mcp_oauth_pending_state(
            state, "user-1", "srv-oauth", verifier, "/admin/mcp-servers"
        )

    @staticmethod
    def _make_jwt(aud: Any) -> str:
        header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
        payload = base64.urlsafe_b64encode(json.dumps({"aud": aud}).encode()).rstrip(b"=").decode()
        return f"{header}.{payload}.sig"

    def test_jwt_aud_matches_oauth_audience_when_set(
        self, storage: SQLiteBackend, http_client_mock: MagicMock
    ) -> None:
        """JWT ``aud=oauth_audience`` is accepted (Auth0 form)."""
        _seed_oauth_user_server(storage)
        # ``oauth_audience`` was seeded as ``https://mcp.example.com``;
        # ``server_url`` is ``https://mcp.example.com/sse``.  A JWT whose
        # aud matches the configured oauth_audience must be accepted
        # even though it differs from the canonical resource URL.
        self._seed_pending(storage)
        token_store = _make_token_store(storage)
        good_jwt = self._make_jwt("https://mcp.example.com")
        http_client_mock.get.return_value = _mk_response(200, _good_as_metadata_doc())
        http_client_mock.post.return_value = _mk_response(
            200, {"access_token": good_jwt, "expires_in": 3600}
        )

        app = _build_app(storage=storage, http_client=http_client_mock, token_store=token_store)
        client = TestClient(app, raise_server_exceptions=False)

        with _public_addr_patch():
            resp = client.get(
                "/v1/api/mcp/oauth/callback?code=c&state=valid-state",
                follow_redirects=False,
            )

        # Accepted — redirect to return_url, token row written.
        assert resp.status_code == 302
        assert resp.headers["location"] == "/admin/mcp-servers"
        plain = token_store.get_user_token("user-1", "srv-oauth")
        assert plain is not None

    def test_jwt_aud_matches_canonical_resource_url(
        self, storage: SQLiteBackend, http_client_mock: MagicMock
    ) -> None:
        """JWT ``aud=server_url`` is also accepted (RFC 8707 form)."""
        _seed_oauth_user_server(storage)
        self._seed_pending(storage)
        token_store = _make_token_store(storage)
        good_jwt = self._make_jwt("https://mcp.example.com/sse")
        http_client_mock.get.return_value = _mk_response(200, _good_as_metadata_doc())
        http_client_mock.post.return_value = _mk_response(
            200, {"access_token": good_jwt, "expires_in": 3600}
        )

        app = _build_app(storage=storage, http_client=http_client_mock, token_store=token_store)
        client = TestClient(app, raise_server_exceptions=False)

        with _public_addr_patch():
            resp = client.get(
                "/v1/api/mcp/oauth/callback?code=c&state=valid-state",
                follow_redirects=False,
            )

        assert resp.status_code == 302
        assert resp.headers["location"] == "/admin/mcp-servers"

    def test_jwt_aud_list_with_one_match_accepted(
        self, storage: SQLiteBackend, http_client_mock: MagicMock
    ) -> None:
        """JWT ``aud`` may be a list — accepted when ANY entry matches."""
        _seed_oauth_user_server(storage)
        self._seed_pending(storage)
        token_store = _make_token_store(storage)
        good_jwt = self._make_jwt(["https://other.example.com", "https://mcp.example.com"])
        http_client_mock.get.return_value = _mk_response(200, _good_as_metadata_doc())
        http_client_mock.post.return_value = _mk_response(
            200, {"access_token": good_jwt, "expires_in": 3600}
        )

        app = _build_app(storage=storage, http_client=http_client_mock, token_store=token_store)
        client = TestClient(app, raise_server_exceptions=False)

        with _public_addr_patch():
            resp = client.get(
                "/v1/api/mcp/oauth/callback?code=c&state=valid-state",
                follow_redirects=False,
            )

        assert resp.status_code == 302
        assert resp.headers["location"] == "/admin/mcp-servers"


# ---------------------------------------------------------------------------
# sec-1 — redirect_uri pinned to oidc_config.redirect_base
# ---------------------------------------------------------------------------


class TestRedirectBasePinning:
    def test_start_503_when_redirect_base_unset(
        self, storage: SQLiteBackend, http_client_mock: MagicMock
    ) -> None:
        """``oidc_config.redirect_base`` empty → ``/start`` returns 503."""
        _seed_oauth_user_server(storage)
        token_store = _make_token_store(storage)
        app = _build_app(
            storage=storage,
            http_client=http_client_mock,
            token_store=token_store,
            redirect_base="",
        )
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/v1/api/mcp/oauth/start?server=srv-oauth")
        assert resp.status_code == 503
        body = resp.json()
        assert "redirect" in body["error"].lower()

    def test_redirect_uri_uses_pinned_base_not_host_header(
        self, storage: SQLiteBackend, http_client_mock: MagicMock
    ) -> None:
        """Spoofed Host header MUST NOT influence redirect_uri."""
        _seed_oauth_user_server(storage)
        token_store = _make_token_store(storage)
        http_client_mock.get.return_value = _mk_response(200, _good_as_metadata_doc())

        app = _build_app(
            storage=storage,
            http_client=http_client_mock,
            token_store=token_store,
            redirect_base="https://app.example.com",
        )
        client = TestClient(app, raise_server_exceptions=False)

        with _public_addr_patch():
            resp = client.get(
                "/v1/api/mcp/oauth/start?server=srv-oauth",
                headers={"Host": "attacker.example.com"},
                follow_redirects=False,
            )

        assert resp.status_code == 302
        location = resp.headers["location"]
        params = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)
        assert params["redirect_uri"] == ["https://app.example.com/v1/api/mcp/oauth/callback"]


# ---------------------------------------------------------------------------
# bug-5 — error /callback pops pending state
# ---------------------------------------------------------------------------


class TestCallbackErrorPopsPendingState:
    def test_callback_error_pops_pending_state(
        self, storage: SQLiteBackend, http_client_mock: MagicMock
    ) -> None:
        _seed_oauth_user_server(storage)
        storage.create_mcp_oauth_pending_state(
            "valid-state", "user-1", "srv-oauth", "verifier-blob", "/admin/mcp-servers"
        )
        token_store = _make_token_store(storage)

        app = _build_app(storage=storage, http_client=http_client_mock, token_store=token_store)
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.get(
            "/v1/api/mcp/oauth/callback"
            "?error=access_denied&error_description=user+cancelled&state=valid-state",
            follow_redirects=False,
        )
        assert resp.status_code == 302
        # Pending state was popped — a replay with the same state should
        # now miss and redirect to "session expired".
        replay = client.get(
            "/v1/api/mcp/oauth/callback?code=any&state=valid-state",
            follow_redirects=False,
        )
        assert replay.status_code == 302
        assert "session+expired" in replay.headers["location"]


# ---------------------------------------------------------------------------
# return_url same-origin pinning (Host-header injection defense)
# ---------------------------------------------------------------------------


class TestValidateReturnUrl:
    """Direct unit tests for ``_validate_return_url``.

    The validator pins ``return_url`` against the configured
    ``redirect_base`` rather than the request Host header, so a
    permissive front proxy cannot turn the OAuth callback into an
    open redirect via spoofed ``Host``.
    """

    REDIRECT_BASE = "https://app.example.com"

    def test_empty_returns_none(self) -> None:
        assert _validate_return_url("", self.REDIRECT_BASE) is None

    def test_relative_path_passes(self) -> None:
        assert _validate_return_url("/admin/mcp", self.REDIRECT_BASE) == "/admin/mcp"

    def test_non_root_relative_rejected(self) -> None:
        assert _validate_return_url("admin/mcp", self.REDIRECT_BASE) is None

    def test_same_origin_absolute_passes(self) -> None:
        same_origin = "https://app.example.com/admin/mcp"
        assert _validate_return_url(same_origin, self.REDIRECT_BASE) == same_origin

    def test_cross_origin_absolute_rejected(self) -> None:
        # Even when the request arrives with ``Host: attacker.example``
        # and ``return_url`` matches that host, pinning to the
        # configured redirect_base catches the open-redirect attempt.
        assert _validate_return_url("https://attacker.example/cb", self.REDIRECT_BASE) is None

    def test_scheme_mismatch_rejected(self) -> None:
        assert _validate_return_url("http://app.example.com/admin", self.REDIRECT_BASE) is None

    def test_backslash_in_path_rejected(self) -> None:
        # WHATWG-conformant browsers normalise ``\`` to ``/``, so
        # ``/\evil.example/foo`` becomes the protocol-relative
        # ``//evil.example/foo`` after the 302 — must be rejected up
        # front because urlparse leaves the backslash inside ``path``
        # and the path-only branch would otherwise return it verbatim.
        assert _validate_return_url("/\\evil.example/foo", self.REDIRECT_BASE) is None
        # Trailing-position backslash still rejected (defense-in-depth).
        assert _validate_return_url("/admin\\..", self.REDIRECT_BASE) is None

    def test_protocol_relative_rejected(self) -> None:
        # ``//evil.example/foo`` would urlparse to netloc=evil.example
        # and fail the cross-origin check anyway, but the early
        # ``startswith("//")`` reject is defense-in-depth.
        assert _validate_return_url("//evil.example/foo", self.REDIRECT_BASE) is None

    def test_same_origin_with_default_port_passes(self) -> None:
        # Operator may legitimately type ``:443`` even though
        # ``redirect_base`` was configured without it.
        url = "https://app.example.com:443/admin"
        assert _validate_return_url(url, self.REDIRECT_BASE) == url

    def test_same_origin_with_uppercase_host_passes(self) -> None:
        url = "https://App.Example.COM/admin"
        assert _validate_return_url(url, self.REDIRECT_BASE) == url

    def test_explicit_port_mismatch_rejected(self) -> None:
        assert (
            _validate_return_url("https://app.example.com:8443/admin", self.REDIRECT_BASE) is None
        )
