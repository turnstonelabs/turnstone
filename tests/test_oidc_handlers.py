"""Integration tests for OIDC HTTP handlers (authorize, callback, admin endpoints).

Uses Starlette TestClient with real SQLiteBackend storage.  External OIDC
functions (exchange_code, validate_id_token, etc.) are mocked — the focus is
on the HTTP handler logic, request/response wiring, and storage side-effects.
"""

from __future__ import annotations

import urllib.parse
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.routing import Mount, Route
from starlette.testclient import TestClient

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response

from turnstone.console.server import (
    admin_delete_oidc_identity,
    admin_list_oidc_identities,
)
from turnstone.core.auth import (
    AuthResult,
    LoginRateLimiter,
    handle_oidc_authorize,
    handle_oidc_callback,
)
from turnstone.core.oidc import OIDCConfig, OIDCError
from turnstone.core.storage._sqlite import SQLiteBackend

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_oidc_config(**overrides: Any) -> OIDCConfig:
    """Build a test OIDCConfig with sensible defaults."""
    defaults: dict[str, Any] = {
        "enabled": True,
        "issuer": "https://idp.example.com",
        "client_id": "my-client",
        "client_secret": "my-secret",
        "scopes": "openid email profile",
        "provider_name": "TestIDP",
        "role_claim": "",
        "role_map": {},
        "password_enabled": True,
        "authorization_endpoint": "https://idp.example.com/authorize",
        "token_endpoint": "https://idp.example.com/token",
        "userinfo_endpoint": "https://idp.example.com/userinfo",
        "jwks_uri": "https://idp.example.com/.well-known/jwks.json",
    }
    defaults.update(overrides)
    return OIDCConfig(**defaults)


# ---------------------------------------------------------------------------
# Thin handler wrappers — match the pattern used in server.py / console
# ---------------------------------------------------------------------------


async def _oidc_authorize(request: Request) -> Response:
    return await handle_oidc_authorize(request, "test-audience")


async def _oidc_callback(request: Request) -> Response:
    return await handle_oidc_callback(request, "test-audience")


# ---------------------------------------------------------------------------
# Auth bypass middleware for admin endpoints
# ---------------------------------------------------------------------------


class _InjectAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Any) -> Response:
        request.state.auth_result = AuthResult(
            user_id="test-admin",
            scopes=frozenset({"approve"}),
            token_source="config",
            permissions=frozenset(
                {
                    "read",
                    "write",
                    "approve",
                    "admin.users",
                }
            ),
        )
        return await call_next(request)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def storage(tmp_path: Any) -> SQLiteBackend:
    """Fresh SQLite backend with a seeded admin user."""
    backend = SQLiteBackend(str(tmp_path / "test.db"))
    backend.create_user("test-admin", "testadmin", "Test Admin", "hash")
    return backend


@pytest.fixture
def oidc_config() -> OIDCConfig:
    return _make_oidc_config()


@pytest.fixture
def authorize_client(storage: SQLiteBackend, oidc_config: OIDCConfig) -> TestClient:
    """TestClient wired to the OIDC authorize + callback handlers."""
    app = Starlette(
        routes=[
            Mount(
                "/v1",
                routes=[
                    Route("/api/auth/oidc/authorize", _oidc_authorize),
                    Route("/api/auth/oidc/callback", _oidc_callback),
                ],
            ),
        ],
    )
    app.state.oidc_config = oidc_config
    app.state.auth_storage = storage
    app.state.jwt_secret = "test-jwt-secret-key-padded-32b!!"
    app.state.jwks_data = {"keys": []}
    app.state.login_limiter = None
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def admin_client(storage: SQLiteBackend) -> TestClient:
    """TestClient wired to the admin OIDC identity endpoints."""
    app = Starlette(
        routes=[
            Mount(
                "/v1",
                routes=[
                    Route(
                        "/api/admin/users/{user_id}/oidc-identities",
                        admin_list_oidc_identities,
                    ),
                    Route(
                        "/api/admin/oidc-identities",
                        admin_delete_oidc_identity,
                        methods=["DELETE"],
                    ),
                ],
            ),
        ],
        middleware=[Middleware(_InjectAuthMiddleware)],
    )
    app.state.auth_storage = storage
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# /authorize tests
# ---------------------------------------------------------------------------


class TestOIDCAuthorize:
    """Tests for GET /v1/api/auth/oidc/authorize."""

    def test_happy_path_redirects_to_idp(self, authorize_client: TestClient) -> None:
        resp = authorize_client.get("/v1/api/auth/oidc/authorize", follow_redirects=False)
        assert resp.status_code == 302
        location = resp.headers["location"]
        assert location.startswith("https://idp.example.com/authorize?")
        parsed = urllib.parse.urlparse(location)
        params = urllib.parse.parse_qs(parsed.query)
        assert params["response_type"] == ["code"]
        assert params["client_id"] == ["my-client"]
        assert params["scope"] == ["openid email profile"]
        assert "state" in params
        assert "nonce" in params
        assert "code_challenge" in params
        assert params["code_challenge_method"] == ["S256"]

    def test_oidc_not_configured_returns_404(
        self,
        storage: SQLiteBackend,
    ) -> None:
        app = Starlette(
            routes=[Mount("/v1", routes=[Route("/api/auth/oidc/authorize", _oidc_authorize)])]
        )
        app.state.auth_storage = storage
        # No oidc_config at all
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/v1/api/auth/oidc/authorize")
        assert resp.status_code == 404
        assert resp.json()["error"] == "OIDC not configured"

    def test_oidc_not_enabled_returns_404(
        self,
        storage: SQLiteBackend,
    ) -> None:
        app = Starlette(
            routes=[Mount("/v1", routes=[Route("/api/auth/oidc/authorize", _oidc_authorize)])]
        )
        app.state.oidc_config = _make_oidc_config(enabled=False)
        app.state.auth_storage = storage
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/v1/api/auth/oidc/authorize")
        assert resp.status_code == 404

    def test_no_storage_returns_503(self) -> None:
        app = Starlette(
            routes=[Mount("/v1", routes=[Route("/api/auth/oidc/authorize", _oidc_authorize)])]
        )
        app.state.oidc_config = _make_oidc_config()
        app.state.login_limiter = None
        # No auth_storage
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/v1/api/auth/oidc/authorize")
        assert resp.status_code == 503

    def test_no_users_returns_403(self, tmp_path: Any) -> None:
        backend = SQLiteBackend(str(tmp_path / "empty.db"))
        app = Starlette(
            routes=[Mount("/v1", routes=[Route("/api/auth/oidc/authorize", _oidc_authorize)])]
        )
        app.state.oidc_config = _make_oidc_config()
        app.state.auth_storage = backend
        app.state.login_limiter = None
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/v1/api/auth/oidc/authorize")
        assert resp.status_code == 403
        assert "setup" in resp.json()["error"].lower()

    def test_pending_state_persisted(
        self,
        authorize_client: TestClient,
        storage: SQLiteBackend,
    ) -> None:
        resp = authorize_client.get("/v1/api/auth/oidc/authorize", follow_redirects=False)
        assert resp.status_code == 302
        location = resp.headers["location"]
        parsed = urllib.parse.urlparse(location)
        params = urllib.parse.parse_qs(parsed.query)
        state = params["state"][0]
        # The pending state should be retrievable from storage
        pending = storage.pop_oidc_pending_state(state, max_age_seconds=300)
        assert pending is not None
        assert pending["audience"] == "test-audience"
        assert pending["nonce"] != ""
        assert pending["code_verifier"] != ""

    def test_rate_limited_redirects_with_error(self, storage: SQLiteBackend) -> None:
        app = Starlette(
            routes=[Mount("/v1", routes=[Route("/api/auth/oidc/authorize", _oidc_authorize)])]
        )
        app.state.oidc_config = _make_oidc_config()
        app.state.auth_storage = storage
        limiter = LoginRateLimiter(max_attempts=1, window_seconds=300)
        # Exhaust the rate limit
        limiter.record("ip:testclient")
        app.state.login_limiter = limiter
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/v1/api/auth/oidc/authorize", follow_redirects=False)
        assert resp.status_code == 302
        assert "oidc_error" in resp.headers["location"]
        assert "Too+many" in resp.headers["location"]


# ---------------------------------------------------------------------------
# /callback tests
# ---------------------------------------------------------------------------


class TestOIDCCallback:
    """Tests for GET /v1/api/auth/oidc/callback."""

    def _seed_pending_state(
        self,
        storage: SQLiteBackend,
        state: str = "valid-state",
        nonce: str = "test-nonce",
        code_verifier: str = "test-verifier",
        audience: str = "test-audience",
    ) -> None:
        storage.create_oidc_pending_state(state, nonce, code_verifier, audience)

    @patch("turnstone.core.oidc.provision_oidc_user")
    @patch("turnstone.core.oidc.validate_id_token")
    @patch("turnstone.core.oidc.exchange_code", new_callable=AsyncMock)
    def test_happy_path(
        self,
        mock_exchange: AsyncMock,
        mock_validate: Any,
        mock_provision: Any,
        authorize_client: TestClient,
        storage: SQLiteBackend,
    ) -> None:
        self._seed_pending_state(storage)
        mock_exchange.return_value = {"id_token": "fake.jwt.token", "access_token": "at"}
        mock_validate.return_value = {
            "sub": "user123",
            "email": "u@example.com",
            "nonce": "test-nonce",
        }
        mock_provision.return_value = {"user_id": "test-admin", "username": "testadmin"}

        resp = authorize_client.get(
            "/v1/api/auth/oidc/callback?code=authcode&state=valid-state",
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert "oidc_success=1" in resp.headers["location"]
        assert "set-cookie" in resp.headers
        assert "turnstone_auth=" in resp.headers["set-cookie"]

    def test_oidc_not_configured_returns_404(self, storage: SQLiteBackend) -> None:
        app = Starlette(
            routes=[Mount("/v1", routes=[Route("/api/auth/oidc/callback", _oidc_callback)])]
        )
        app.state.auth_storage = storage
        # No oidc_config
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/v1/api/auth/oidc/callback?code=x&state=y")
        assert resp.status_code == 404

    def test_idp_error_param_redirects(
        self,
        authorize_client: TestClient,
    ) -> None:
        resp = authorize_client.get(
            "/v1/api/auth/oidc/callback?error=access_denied&error_description=User+cancelled",
            follow_redirects=False,
        )
        assert resp.status_code == 302
        location = resp.headers["location"]
        assert "oidc_error" in location
        assert "User" in urllib.parse.unquote(location)

    def test_invalid_state_redirects_expired(
        self,
        authorize_client: TestClient,
    ) -> None:
        resp = authorize_client.get(
            "/v1/api/auth/oidc/callback?code=authcode&state=nonexistent",
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert "Login+session+expired" in resp.headers["location"]

    def test_expired_state_redirects(
        self,
        authorize_client: TestClient,
        storage: SQLiteBackend,
    ) -> None:
        # Insert state then backdate created_at via raw SQL so that
        # pop_oidc_pending_state's max_age_seconds=300 check rejects it.
        self._seed_pending_state(storage, state="old-state")
        import sqlalchemy as sa

        with storage._engine.connect() as conn:
            conn.execute(
                sa.text(
                    "UPDATE oidc_pending_states SET created_at = '2020-01-01T00:00:00' "
                    "WHERE state = 'old-state'"
                )
            )
            conn.commit()

        resp = authorize_client.get(
            "/v1/api/auth/oidc/callback?code=authcode&state=old-state",
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert "Login+session+expired" in resp.headers["location"]

    @patch("turnstone.core.oidc.exchange_code", new_callable=AsyncMock)
    def test_code_exchange_failure(
        self,
        mock_exchange: AsyncMock,
        authorize_client: TestClient,
        storage: SQLiteBackend,
    ) -> None:
        self._seed_pending_state(storage)
        mock_exchange.side_effect = OIDCError("Token endpoint error")

        resp = authorize_client.get(
            "/v1/api/auth/oidc/callback?code=badcode&state=valid-state",
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert "Authentication+failed" in resp.headers["location"]

    @patch("turnstone.core.oidc.validate_id_token")
    @patch("turnstone.core.oidc.exchange_code", new_callable=AsyncMock)
    def test_token_validation_failure(
        self,
        mock_exchange: AsyncMock,
        mock_validate: Any,
        authorize_client: TestClient,
        storage: SQLiteBackend,
    ) -> None:
        self._seed_pending_state(storage)
        mock_exchange.return_value = {"id_token": "fake.jwt.token"}
        mock_validate.side_effect = OIDCError("Signature invalid")

        resp = authorize_client.get(
            "/v1/api/auth/oidc/callback?code=authcode&state=valid-state",
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert "Authentication+failed" in resp.headers["location"]

    @patch("turnstone.core.oidc.provision_oidc_user")
    @patch("turnstone.core.oidc.validate_id_token")
    @patch("turnstone.core.oidc.fetch_jwks", new_callable=AsyncMock)
    @patch("turnstone.core.oidc.exchange_code", new_callable=AsyncMock)
    def test_jwks_key_rotation_retry(
        self,
        mock_exchange: AsyncMock,
        mock_fetch_jwks: AsyncMock,
        mock_validate: Any,
        mock_provision: Any,
        authorize_client: TestClient,
        storage: SQLiteBackend,
    ) -> None:
        """First validate raises 'kid not found in JWKS', fetch_jwks retried, second validate succeeds."""
        self._seed_pending_state(storage)
        mock_exchange.return_value = {"id_token": "fake.jwt.token"}

        # First call raises kid-not-found; second call (after JWKS refresh) succeeds
        mock_validate.side_effect = [
            OIDCError("Signing key 'new-kid' not found in JWKS"),
            {"sub": "user123", "email": "u@example.com", "nonce": "test-nonce"},
        ]
        mock_fetch_jwks.return_value = {"keys": [{"kid": "new-kid", "kty": "RSA"}]}
        mock_provision.return_value = {"user_id": "test-admin", "username": "testadmin"}

        resp = authorize_client.get(
            "/v1/api/auth/oidc/callback?code=authcode&state=valid-state",
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert "oidc_success=1" in resp.headers["location"]
        mock_fetch_jwks.assert_called_once()
        assert mock_validate.call_count == 2

    @patch("turnstone.core.oidc.provision_oidc_user")
    @patch("turnstone.core.oidc.validate_id_token")
    @patch("turnstone.core.oidc.exchange_code", new_callable=AsyncMock)
    def test_no_users_after_oidc_success_redirects_setup(
        self,
        mock_exchange: AsyncMock,
        mock_validate: Any,
        mock_provision: Any,
        tmp_path: Any,
    ) -> None:
        """When OIDC succeeds but no users exist (edge case), redirect with setup error."""
        # Use a fresh empty-user storage
        backend = SQLiteBackend(str(tmp_path / "empty.db"))
        app = Starlette(
            routes=[Mount("/v1", routes=[Route("/api/auth/oidc/callback", _oidc_callback)])]
        )
        app.state.oidc_config = _make_oidc_config()
        app.state.auth_storage = backend
        app.state.jwt_secret = "test-jwt-secret-key-padded-32b!!"
        app.state.jwks_data = {"keys": []}
        app.state.login_limiter = None

        # Seed a pending state in the empty database
        backend.create_oidc_pending_state("state1", "nonce1", "verifier1", "test-audience")

        mock_exchange.return_value = {"id_token": "fake.jwt.token"}
        mock_validate.return_value = {"sub": "u1", "email": "u@example.com", "nonce": "nonce1"}

        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(
            "/v1/api/auth/oidc/callback?code=authcode&state=state1",
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert "Initial+setup+required" in resp.headers["location"]

    def test_rate_limited_redirects_with_error(self, storage: SQLiteBackend) -> None:
        app = Starlette(
            routes=[Mount("/v1", routes=[Route("/api/auth/oidc/callback", _oidc_callback)])]
        )
        app.state.oidc_config = _make_oidc_config()
        app.state.auth_storage = storage
        app.state.jwt_secret = "test-jwt-secret-key-padded-32b!!"
        app.state.jwks_data = {"keys": []}
        limiter = LoginRateLimiter(max_attempts=1, window_seconds=300)
        limiter.record("ip:testclient")
        app.state.login_limiter = limiter

        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(
            "/v1/api/auth/oidc/callback?code=authcode&state=x",
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert "oidc_error" in resp.headers["location"]
        assert "Too+many" in resp.headers["location"]


# ---------------------------------------------------------------------------
# Admin OIDC identity endpoint tests
# ---------------------------------------------------------------------------


class TestAdminOIDCIdentities:
    """Tests for admin OIDC identity management endpoints."""

    def test_list_identities(
        self,
        admin_client: TestClient,
        storage: SQLiteBackend,
    ) -> None:
        storage.create_oidc_identity(
            "https://idp.example.com",
            "sub-123",
            "test-admin",
            "admin@example.com",
        )
        resp = admin_client.get("/v1/api/admin/users/test-admin/oidc-identities")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["oidc_identities"]) == 1
        identity = data["oidc_identities"][0]
        assert identity["issuer"] == "https://idp.example.com"
        assert identity["subject"] == "sub-123"
        assert identity["user_id"] == "test-admin"
        assert identity["email"] == "admin@example.com"

    def test_list_empty(self, admin_client: TestClient) -> None:
        resp = admin_client.get("/v1/api/admin/users/test-admin/oidc-identities")
        assert resp.status_code == 200
        assert resp.json()["oidc_identities"] == []

    def test_delete_identity(
        self,
        admin_client: TestClient,
        storage: SQLiteBackend,
    ) -> None:
        storage.create_oidc_identity(
            "https://idp.example.com",
            "sub-456",
            "test-admin",
            "admin@example.com",
        )
        resp = admin_client.delete(
            "/v1/api/admin/oidc-identities?issuer=https://idp.example.com&subject=sub-456",
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        # Verify it's gone
        assert storage.get_oidc_identity("https://idp.example.com", "sub-456") is None

    def test_delete_nonexistent_returns_404(self, admin_client: TestClient) -> None:
        resp = admin_client.delete(
            "/v1/api/admin/oidc-identities?issuer=https://no.such&subject=nope",
        )
        assert resp.status_code == 404
        assert "not found" in resp.json()["error"].lower()

    def test_delete_missing_params_returns_400(self, admin_client: TestClient) -> None:
        # Missing subject
        resp = admin_client.delete(
            "/v1/api/admin/oidc-identities?issuer=https://idp.example.com",
        )
        assert resp.status_code == 400
        assert "required" in resp.json()["error"].lower()

        # Missing both
        resp = admin_client.delete("/v1/api/admin/oidc-identities")
        assert resp.status_code == 400
