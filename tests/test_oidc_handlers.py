"""Integration tests for OIDC HTTP handlers (authorize, callback, admin endpoints).

Uses Starlette TestClient with real SQLiteBackend storage.  External OIDC
functions (exchange_code, validate_id_token, etc.) are mocked — the focus is
on the HTTP handler logic, request/response wiring, and storage side-effects.
"""

from __future__ import annotations

import urllib.parse
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.routing import Mount, Route
from starlette.testclient import TestClient

from tests.conftest import make_oidc_test_config as _make_oidc_config
from turnstone.console.server import (
    admin_delete_oidc_identity,
    admin_list_oidc_identities,
)
from turnstone.core.auth import (
    AUTH_COOKIE_CONSOLE,
    AUTH_COOKIE_SERVER,
    AuthResult,
    LoginRateLimiter,
    handle_oidc_authorize,
    handle_oidc_callback,
)
from turnstone.core.oidc import OIDCConfig, OIDCError, OIDCKeyNotFoundError
from turnstone.core.storage._sqlite import SQLiteBackend

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response

# ---------------------------------------------------------------------------
# Thin handler wrappers — match the pattern used in server.py / console
# ---------------------------------------------------------------------------


async def _oidc_authorize(request: Request) -> Response:
    return await handle_oidc_authorize(request, "test-audience")


async def _oidc_callback(request: Request) -> Response:
    return await handle_oidc_callback(request, "test-audience", cookie_name=AUTH_COOKIE_SERVER)


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

    def test_disabled_retryable_triggers_login_self_heal(self, storage: SQLiteBackend) -> None:
        """Review finding: the LOGIN path must trigger runtime rediscovery too,
        not just the obo mint path — otherwise a single-node install whose node
        booted during a transient IdP outage stays login-dark forever. A
        disabled+retryable config makes authorize call maybe_rediscover_oidc."""
        from unittest.mock import AsyncMock, patch

        app = Starlette(
            routes=[Mount("/v1", routes=[Route("/api/auth/oidc/authorize", _oidc_authorize)])]
        )
        app.state.oidc_config = _make_oidc_config(enabled=False, discovery_retryable=True)
        app.state.auth_storage = storage
        client = TestClient(app, raise_server_exceptions=False)
        with patch("turnstone.core.auth.maybe_rediscover_oidc", new=AsyncMock()) as heal:
            client.get("/v1/api/auth/oidc/authorize")
        heal.assert_awaited_once()

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

    @patch("turnstone.core.auth.provision_oidc_user")
    @patch("turnstone.core.auth.validate_id_token")
    @patch("turnstone.core.auth.exchange_code", new_callable=AsyncMock)
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
        set_cookie = resp.headers["set-cookie"]
        assert set_cookie.split(";", 1)[0].partition("=")[0] == AUTH_COOKIE_SERVER

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

    @patch("turnstone.core.auth.exchange_code", new_callable=AsyncMock)
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

    @patch("turnstone.core.auth.validate_id_token")
    @patch("turnstone.core.auth.exchange_code", new_callable=AsyncMock)
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

    @patch("turnstone.core.auth.provision_oidc_user")
    @patch("turnstone.core.auth.validate_id_token")
    @patch("turnstone.core.auth.fetch_jwks", new_callable=AsyncMock)
    @patch("turnstone.core.auth.exchange_code", new_callable=AsyncMock)
    def test_jwks_key_rotation_retry(
        self,
        mock_exchange: AsyncMock,
        mock_fetch_jwks: AsyncMock,
        mock_validate: Any,
        mock_provision: Any,
        authorize_client: TestClient,
        storage: SQLiteBackend,
    ) -> None:
        """First validate raises kid-not-found, fetch_jwks retried, second validate succeeds."""
        self._seed_pending_state(storage)
        mock_exchange.return_value = {"id_token": "fake.jwt.token"}

        # First call raises kid-not-found; second call (after JWKS refresh) succeeds
        mock_validate.side_effect = [
            OIDCKeyNotFoundError("Signing key 'new-kid' not found in JWKS"),
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

    @patch("turnstone.core.auth.provision_oidc_user")
    @patch("turnstone.core.auth.validate_id_token")
    @patch("turnstone.core.auth.fetch_jwks", new_callable=AsyncMock)
    @patch("turnstone.core.auth.exchange_code", new_callable=AsyncMock)
    def test_callback_uses_keynotfound_for_jwks_retry(
        self,
        mock_exchange: AsyncMock,
        mock_fetch_jwks: AsyncMock,
        mock_validate: Any,
        mock_provision: Any,
        authorize_client: TestClient,
        storage: SQLiteBackend,
    ) -> None:
        """Retry path keys off the OIDCKeyNotFoundError type, not message substring."""
        self._seed_pending_state(storage)
        mock_exchange.return_value = {"id_token": "fake.jwt.token"}

        # First raises subclass; rephrased message must not affect retry behaviour.
        mock_validate.side_effect = [
            OIDCKeyNotFoundError("rotated key absent from cached set"),
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

    @patch("turnstone.core.auth.exchange_code", new_callable=AsyncMock)
    def test_callback_returns_authentication_failed_on_missing_id_token(
        self,
        mock_exchange: AsyncMock,
        authorize_client: TestClient,
        storage: SQLiteBackend,
    ) -> None:
        """A token endpoint response without id_token must redirect with auth-failed."""
        self._seed_pending_state(storage)
        mock_exchange.return_value = {"access_token": "x"}

        resp = authorize_client.get(
            "/v1/api/auth/oidc/callback?code=authcode&state=valid-state",
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert "oidc_error=Authentication+failed" in resp.headers["location"]

    @patch("turnstone.core.auth.provision_oidc_user")
    @patch("turnstone.core.auth.validate_id_token")
    @patch("turnstone.core.auth.exchange_code", new_callable=AsyncMock)
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

    @patch("turnstone.core.auth.provision_oidc_user")
    @patch("turnstone.core.auth.validate_id_token")
    @patch("turnstone.core.auth.exchange_code", new_callable=AsyncMock)
    def test_setup_gate_uses_count_users_not_full_scan(
        self,
        mock_exchange: AsyncMock,
        mock_validate: Any,
        mock_provision: Any,
        authorize_client: TestClient,
        storage: SQLiteBackend,
    ) -> None:
        """Callback's setup-complete gate must call count_users, not list_users."""
        from unittest.mock import patch as obj_patch

        self._seed_pending_state(storage)
        mock_exchange.return_value = {"id_token": "fake.jwt.token"}
        mock_validate.return_value = {
            "sub": "u1",
            "email": "u@example.com",
            "nonce": "test-nonce",
        }
        mock_provision.return_value = {"user_id": "test-admin", "username": "testadmin"}

        with (
            obj_patch.object(storage, "count_users", wraps=storage.count_users) as count_spy,
            obj_patch.object(storage, "list_users", wraps=storage.list_users) as list_spy,
        ):
            resp = authorize_client.get(
                "/v1/api/auth/oidc/callback?code=authcode&state=valid-state",
                follow_redirects=False,
            )

        assert resp.status_code == 302
        assert "oidc_success=1" in resp.headers["location"]
        count_spy.assert_called_once_with()
        list_spy.assert_not_called()

    @patch("turnstone.core.auth.provision_oidc_user")
    @patch("turnstone.core.auth.validate_id_token")
    @patch("turnstone.core.auth.exchange_code", new_callable=AsyncMock)
    def test_state_cleanup_is_gated(
        self,
        mock_exchange: AsyncMock,
        mock_validate: Any,
        mock_provision: Any,
        authorize_client: TestClient,
        storage: SQLiteBackend,
    ) -> None:
        """Cleanup runs once per cleanup-interval window, not every callback."""
        from unittest.mock import patch as obj_patch

        # First call seeds the cleanup timestamp; subsequent calls within
        # _OIDC_STATE_CLEANUP_INTERVAL_S must NOT trigger cleanup again.
        mock_exchange.return_value = {"id_token": "fake.jwt.token"}
        mock_validate.return_value = {
            "sub": "u1",
            "email": "u@example.com",
            "nonce": "test-nonce",
        }
        mock_provision.return_value = {"user_id": "test-admin", "username": "testadmin"}

        with obj_patch.object(
            storage, "cleanup_expired_oidc_states", wraps=storage.cleanup_expired_oidc_states
        ) as cleanup_spy:
            for state in ("s1", "s2", "s3"):
                self._seed_pending_state(storage, state=state, nonce="test-nonce")
                authorize_client.get(
                    f"/v1/api/auth/oidc/callback?code=c&state={state}",
                    follow_redirects=False,
                )

        assert cleanup_spy.call_count == 1

    @patch("turnstone.core.auth.provision_oidc_user")
    @patch("turnstone.core.auth.validate_id_token")
    @patch("turnstone.core.auth.exchange_code", new_callable=AsyncMock)
    def test_callback_uses_pending_audience_not_handler_audience(
        self,
        mock_exchange: AsyncMock,
        mock_validate: Any,
        mock_provision: Any,
        storage: SQLiteBackend,
    ) -> None:
        """JWT ``aud`` claim must come from the audience stored at /authorize,
        not the audience the callback handler was invoked with.

        Regression for the cross-service audience-confusion concern: a
        login flow opened against the server (audience ``"turnstone-server"``)
        must not be silently re-targeted to ``"turnstone-console"`` when
        the callback runs through the console's handler wrapper.
        """
        import jwt as pyjwt

        # Seed pending state with the SERVER audience.
        storage.create_oidc_pending_state(
            "audience-state",
            "audience-nonce",
            "audience-verifier",
            "turnstone-server",
        )
        mock_exchange.return_value = {"id_token": "fake.jwt.token"}
        mock_validate.return_value = {
            "sub": "user-aud",
            "email": "u@example.com",
            "nonce": "audience-nonce",
        }
        mock_provision.return_value = {"user_id": "test-admin", "username": "testadmin"}

        # Wire a callback bound to the CONSOLE audience.  After bug-3 the
        # stored audience must take precedence.
        async def _console_callback(request: Request) -> Response:
            return await handle_oidc_callback(
                request, "turnstone-console", cookie_name=AUTH_COOKIE_CONSOLE
            )

        jwt_secret = "test-jwt-secret-key-padded-32b!!"
        app = Starlette(
            routes=[Mount("/v1", routes=[Route("/api/auth/oidc/callback", _console_callback)])]
        )
        app.state.oidc_config = _make_oidc_config()
        app.state.auth_storage = storage
        app.state.jwt_secret = jwt_secret
        app.state.jwks_data = {"keys": []}
        app.state.login_limiter = None
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.get(
            "/v1/api/auth/oidc/callback?code=authcode&state=audience-state",
            follow_redirects=False,
        )

        assert resp.status_code == 302
        assert "oidc_success=1" in resp.headers["location"]

        # Extract the JWT from the Set-Cookie header and decode it.
        set_cookie = resp.headers["set-cookie"]
        cookie_kv = set_cookie.split(";", 1)[0]
        name, _, token = cookie_kv.partition("=")
        assert name == AUTH_COOKIE_CONSOLE
        assert token

        # Decoding without audience verification first to inspect the claim.
        claims = pyjwt.decode(
            token, jwt_secret, algorithms=["HS256"], options={"verify_aud": False}
        )
        assert claims["aud"] == "turnstone-server"
        assert claims["aud"] != "turnstone-console"

    @patch("turnstone.core.auth.provision_oidc_user")
    @patch("turnstone.core.auth.validate_id_token")
    @patch("turnstone.core.auth.fetch_jwks", new_callable=AsyncMock)
    @patch("turnstone.core.auth.exchange_code", new_callable=AsyncMock)
    def test_jwks_refetch_dedup_when_kid_appears(
        self,
        mock_exchange: AsyncMock,
        mock_fetch_jwks: AsyncMock,
        mock_validate: Any,
        mock_provision: Any,
        authorize_client: TestClient,
        storage: SQLiteBackend,
    ) -> None:
        """If a concurrent caller already refreshed JWKS, second caller skips fetch."""
        from unittest.mock import patch as obj_patch

        self._seed_pending_state(storage)
        mock_exchange.return_value = {"id_token": "fake.jwt.token"}
        mock_provision.return_value = {"user_id": "test-admin", "username": "testadmin"}

        # First validate raises kid-not-found; second succeeds.
        mock_validate.side_effect = [
            OIDCKeyNotFoundError("Signing key 'k-rotated' not found"),
            {"sub": "u1", "email": "u@example.com", "nonce": "test-nonce"},
        ]

        # Pre-populate the JWKS cache so the rotated kid is already
        # present — analog of a concurrent caller having won the lock.
        # The retry path must short-circuit and skip the network fetch.
        authorize_client.app.state.jwks_data = {"keys": [{"kid": "k-rotated", "kty": "RSA"}]}

        with obj_patch("jwt.get_unverified_header", return_value={"kid": "k-rotated"}):
            resp = authorize_client.get(
                "/v1/api/auth/oidc/callback?code=authcode&state=valid-state",
                follow_redirects=False,
            )

        assert resp.status_code == 302
        assert "oidc_success=1" in resp.headers["location"]
        mock_fetch_jwks.assert_not_called()


# ---------------------------------------------------------------------------
# Single-credential capture tests (issue #551)
# ---------------------------------------------------------------------------


class TestOIDCCallbackCapture:
    """Capture of the IdP refresh token at login (``capture_user_credential``)."""

    def _capture_client(
        self,
        storage: SQLiteBackend,
        oidc_config: OIDCConfig,
        *,
        capture: bool = True,
        with_store: bool = True,
    ) -> tuple[TestClient, Any, OIDCConfig]:
        """Client wired like ``authorize_client`` plus a real MCPTokenStore."""
        import dataclasses

        from tests.conftest import make_mcp_token_cipher
        from turnstone.core.mcp_crypto import MCPTokenStore

        cfg = dataclasses.replace(oidc_config, capture_user_credential=capture)
        app = Starlette(
            routes=[
                Mount("/v1", routes=[Route("/api/auth/oidc/callback", _oidc_callback)]),
            ],
        )
        app.state.oidc_config = cfg
        app.state.auth_storage = storage
        app.state.jwt_secret = "test-jwt-secret-key-padded-32b!!"
        app.state.jwks_data = {"keys": []}
        app.state.login_limiter = None
        store = MCPTokenStore(storage, make_mcp_token_cipher()) if with_store else None
        app.state.mcp_token_store = store
        return TestClient(app, raise_server_exceptions=False), store, cfg

    def _login(
        self,
        client: TestClient,
        storage: SQLiteBackend,
        mock_exchange: AsyncMock,
        mock_validate: Any,
        mock_provision: Any,
        *,
        tokens: dict[str, Any],
        state: str = "valid-state",
    ) -> Any:
        storage.create_oidc_pending_state(state, "test-nonce", "test-verifier", "test-audience")
        mock_exchange.return_value = tokens
        mock_validate.return_value = {
            "sub": "user123",
            "email": "u@example.com",
            "nonce": "test-nonce",
        }
        mock_provision.return_value = {"user_id": "test-admin", "username": "testadmin"}
        return client.get(
            f"/v1/api/auth/oidc/callback?code=authcode&state={state}",
            follow_redirects=False,
        )

    @patch("turnstone.core.auth.provision_oidc_user")
    @patch("turnstone.core.auth.validate_id_token")
    @patch("turnstone.core.auth.exchange_code", new_callable=AsyncMock)
    def test_capture_persists_credential(
        self,
        mock_exchange: AsyncMock,
        mock_validate: Any,
        mock_provision: Any,
        storage: SQLiteBackend,
        oidc_config: OIDCConfig,
    ) -> None:
        client, store, cfg = self._capture_client(storage, oidc_config)
        resp = self._login(
            client,
            storage,
            mock_exchange,
            mock_validate,
            mock_provision,
            tokens={"id_token": "fake.jwt.token", "access_token": "at", "refresh_token": "rt-1"},
        )
        assert resp.status_code == 302
        assert "oidc_success=1" in resp.headers["location"]
        assert store is not None
        plain = store.get_oidc_credential("test-admin", cfg.issuer)
        assert plain is not None
        assert plain["refresh_token"] == "rt-1"

    @patch("turnstone.core.auth.provision_oidc_user")
    @patch("turnstone.core.auth.validate_id_token")
    @patch("turnstone.core.auth.exchange_code", new_callable=AsyncMock)
    def test_capture_success_primes_user_pools(
        self,
        mock_exchange: AsyncMock,
        mock_validate: Any,
        mock_provision: Any,
        storage: SQLiteBackend,
        oidc_config: OIDCConfig,
    ) -> None:
        """Re-login is the OBO restore moment (#836): a successful
        credential capture for a user with a LIVE session schedules a
        pool prime so a previously dropped obo catalog returns to their
        open workstreams — obo has no consent flow, so nothing else
        re-primes them after re-login."""
        client, store, cfg = self._capture_client(storage, oidc_config)
        primed: list[str] = []
        client.app.state.mcp_client = SimpleNamespace(  # type: ignore[attr-defined]
            prime_user_pools=primed.append,
            has_live_session_listener=lambda _uid: True,
        )
        resp = self._login(
            client,
            storage,
            mock_exchange,
            mock_validate,
            mock_provision,
            tokens={"id_token": "fake.jwt.token", "access_token": "at", "refresh_token": "rt-1"},
        )
        assert resp.status_code == 302
        assert primed == ["test-admin"]

    @patch("turnstone.core.auth.provision_oidc_user")
    @patch("turnstone.core.auth.validate_id_token")
    @patch("turnstone.core.auth.exchange_code", new_callable=AsyncMock)
    def test_no_capture_no_prime(
        self,
        mock_exchange: AsyncMock,
        mock_validate: Any,
        mock_provision: Any,
        storage: SQLiteBackend,
        oidc_config: OIDCConfig,
    ) -> None:
        """No refresh token in the response → no capture → no prime
        (the prime is gated on a persisted credential, not on login)."""
        client, store, cfg = self._capture_client(storage, oidc_config)
        primed: list[str] = []
        client.app.state.mcp_client = SimpleNamespace(  # type: ignore[attr-defined]
            prime_user_pools=primed.append,
            has_live_session_listener=lambda _uid: True,
        )
        resp = self._login(
            client,
            storage,
            mock_exchange,
            mock_validate,
            mock_provision,
            tokens={"id_token": "fake.jwt.token", "access_token": "at"},
        )
        assert resp.status_code == 302
        assert primed == []

    @patch("turnstone.core.auth.provision_oidc_user")
    @patch("turnstone.core.auth.validate_id_token")
    @patch("turnstone.core.auth.exchange_code", new_callable=AsyncMock)
    def test_capture_without_live_session_does_not_prime(
        self,
        mock_exchange: AsyncMock,
        mock_validate: Any,
        mock_provision: Any,
        storage: SQLiteBackend,
        oidc_config: OIDCConfig,
    ) -> None:
        """Routine SSO re-login with nothing open must not fan out pool
        warms — the prime exists to heal LIVE sessions only."""
        client, store, cfg = self._capture_client(storage, oidc_config)
        primed: list[str] = []
        client.app.state.mcp_client = SimpleNamespace(  # type: ignore[attr-defined]
            prime_user_pools=primed.append,
            has_live_session_listener=lambda _uid: False,
        )
        resp = self._login(
            client,
            storage,
            mock_exchange,
            mock_validate,
            mock_provision,
            tokens={"id_token": "fake.jwt.token", "access_token": "at", "refresh_token": "rt-1"},
        )
        assert resp.status_code == 302
        # Credential captured, but no live session → no prime.
        assert store is not None
        assert store.get_oidc_credential("test-admin", cfg.issuer) is not None
        assert primed == []

    @patch("turnstone.core.auth.provision_oidc_user")
    @patch("turnstone.core.auth.validate_id_token")
    @patch("turnstone.core.auth.exchange_code", new_callable=AsyncMock)
    def test_second_login_replaces_credential(
        self,
        mock_exchange: AsyncMock,
        mock_validate: Any,
        mock_provision: Any,
        storage: SQLiteBackend,
        oidc_config: OIDCConfig,
    ) -> None:
        client, store, cfg = self._capture_client(storage, oidc_config)
        self._login(
            client,
            storage,
            mock_exchange,
            mock_validate,
            mock_provision,
            tokens={"id_token": "t", "refresh_token": "rt-old"},
            state="s1",
        )
        self._login(
            client,
            storage,
            mock_exchange,
            mock_validate,
            mock_provision,
            tokens={"id_token": "t", "refresh_token": "rt-new"},
            state="s2",
        )
        assert store is not None
        plain = store.get_oidc_credential("test-admin", cfg.issuer)
        assert plain is not None
        assert plain["refresh_token"] == "rt-new"

    @patch("turnstone.core.auth.provision_oidc_user")
    @patch("turnstone.core.auth.validate_id_token")
    @patch("turnstone.core.auth.exchange_code", new_callable=AsyncMock)
    def test_no_refresh_token_logs_and_login_succeeds(
        self,
        mock_exchange: AsyncMock,
        mock_validate: Any,
        mock_provision: Any,
        storage: SQLiteBackend,
        oidc_config: OIDCConfig,
    ) -> None:
        client, store, cfg = self._capture_client(storage, oidc_config)
        resp = self._login(
            client,
            storage,
            mock_exchange,
            mock_validate,
            mock_provision,
            tokens={"id_token": "fake.jwt.token", "access_token": "at"},
        )
        assert resp.status_code == 302
        assert "oidc_success=1" in resp.headers["location"]
        assert store is not None
        assert store.get_oidc_credential("test-admin", cfg.issuer) is None

    @patch("turnstone.core.auth.provision_oidc_user")
    @patch("turnstone.core.auth.validate_id_token")
    @patch("turnstone.core.auth.exchange_code", new_callable=AsyncMock)
    def test_capture_disabled_persists_nothing(
        self,
        mock_exchange: AsyncMock,
        mock_validate: Any,
        mock_provision: Any,
        storage: SQLiteBackend,
        oidc_config: OIDCConfig,
    ) -> None:
        client, store, cfg = self._capture_client(storage, oidc_config, capture=False)
        resp = self._login(
            client,
            storage,
            mock_exchange,
            mock_validate,
            mock_provision,
            tokens={"id_token": "t", "refresh_token": "rt-present"},
        )
        assert resp.status_code == 302
        assert store is not None
        assert store.get_oidc_credential("test-admin", cfg.issuer) is None

    @patch("turnstone.core.auth.provision_oidc_user")
    @patch("turnstone.core.auth.validate_id_token")
    @patch("turnstone.core.auth.exchange_code", new_callable=AsyncMock)
    def test_missing_store_login_still_succeeds(
        self,
        mock_exchange: AsyncMock,
        mock_validate: Any,
        mock_provision: Any,
        storage: SQLiteBackend,
        oidc_config: OIDCConfig,
    ) -> None:
        client, _store, _cfg = self._capture_client(storage, oidc_config, with_store=False)
        resp = self._login(
            client,
            storage,
            mock_exchange,
            mock_validate,
            mock_provision,
            tokens={"id_token": "t", "refresh_token": "rt-1"},
        )
        assert resp.status_code == 302
        assert "oidc_success=1" in resp.headers["location"]

    @patch("turnstone.core.auth.provision_oidc_user")
    @patch("turnstone.core.auth.validate_id_token")
    @patch("turnstone.core.auth.exchange_code", new_callable=AsyncMock)
    def test_store_failure_does_not_block_login(
        self,
        mock_exchange: AsyncMock,
        mock_validate: Any,
        mock_provision: Any,
        storage: SQLiteBackend,
        oidc_config: OIDCConfig,
    ) -> None:
        client, store, _cfg = self._capture_client(storage, oidc_config)
        assert store is not None
        with patch.object(store, "upsert_oidc_credential", side_effect=RuntimeError("boom")):
            resp = self._login(
                client,
                storage,
                mock_exchange,
                mock_validate,
                mock_provision,
                tokens={"id_token": "t", "refresh_token": "rt-1"},
            )
        assert resp.status_code == 302
        assert "oidc_success=1" in resp.headers["location"]


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

    def test_delete_identity_revokes_credential_and_purges_obo_cache(
        self, storage: SQLiteBackend
    ) -> None:
        """#551 follow-up: unlinking an identity must revoke the captured
        credential AND purge the user's already-minted oauth_obo cache rows —
        deleting only the credential leaves live cached bearers authorizing
        dispatch until TTL. The response/audit report what was actually cut."""
        from tests.conftest import make_mcp_token_cipher
        from turnstone.core.mcp_crypto import MCPTokenStore

        issuer = "https://idp.example.com"
        store = MCPTokenStore(storage, make_mcp_token_cipher(), node_id="test")
        app = Starlette(
            routes=[
                Mount(
                    "/v1",
                    routes=[
                        Route(
                            "/api/admin/oidc-identities",
                            admin_delete_oidc_identity,
                            methods=["DELETE"],
                        )
                    ],
                )
            ],
            middleware=[Middleware(_InjectAuthMiddleware)],
        )
        app.state.auth_storage = storage
        app.state.mcp_token_store = store
        client = TestClient(app, raise_server_exceptions=False)

        storage.create_oidc_identity(issuer, "sub-1", "user-x", "x@example.com")
        store.upsert_oidc_credential("user-x", issuer, refresh_token="rt-live")
        storage.create_mcp_server(
            server_id="srv-obo",
            name="obo-srv",
            transport="streamable-http",
            url="https://mcp.example.com/sse",
            auth_type="oauth_obo",
            oauth_audience="api://mcp-a",
        )
        store.create_user_token(
            "user-x",
            "obo-srv",
            access_token="minted-at",
            refresh_token=None,
            expires_at="2026-12-31T00:00:00",
            scopes=None,
            as_issuer=issuer,
            audience="api://mcp-a",
        )

        resp = client.delete(f"/v1/api/admin/oidc-identities?issuer={issuer}&subject=sub-1")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["obo_credential_revoked"] is True
        assert body["obo_cache_rows_purged"] == 1
        # Credential gone → no future mints; cache row gone → no live cached bearer.
        assert store.get_oidc_credential("user-x", issuer) is None
        assert storage.get_mcp_user_token("user-x", "obo-srv") is None

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
