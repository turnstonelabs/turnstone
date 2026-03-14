"""Tests for turnstone.core.auth_oidc — OIDC authentication provider."""

import base64
import json
import time
from unittest.mock import MagicMock, patch

import pytest

from turnstone.core.auth_oidc import (
    OIDCConfig,
    _create_state,
    _decode_jwt_claims,
    _email_to_username,
    _extract_identity,
    _validate_state,
)


# ---------------------------------------------------------------------------
# Mock OIDC discovery document
# ---------------------------------------------------------------------------

MOCK_DISCOVERY = {
    "issuer": "https://oauth.example.com",
    "authorization_endpoint": "https://oauth.example.com/oauth2/auth",
    "token_endpoint": "https://oauth.example.com/oauth2/token",
    "userinfo_endpoint": "https://oauth.example.com/userinfo",
    "jwks_uri": "https://oauth.example.com/.well-known/jwks.json",
}


def _make_id_token(claims: dict) -> str:
    """Build a fake JWT with the given claims (no signature verification)."""
    header = base64.urlsafe_b64encode(json.dumps({"alg": "RS256"}).encode()).rstrip(b"=")
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=")
    sig = base64.urlsafe_b64encode(b"fakesig").rstrip(b"=")
    return f"{header.decode()}.{payload.decode()}.{sig.decode()}"


# ---------------------------------------------------------------------------
# OIDCConfig
# ---------------------------------------------------------------------------


class TestOIDCConfig:
    def test_enabled_when_all_fields_set(self):
        cfg = OIDCConfig(
            provider_url="https://oauth.example.com/.well-known/openid-configuration",
            client_id="myapp",
            redirect_uri="https://myapp.example.com/callback",
        )
        assert cfg.enabled is True

    def test_disabled_when_missing_provider_url(self):
        cfg = OIDCConfig(client_id="myapp", redirect_uri="https://myapp.example.com/callback")
        assert cfg.enabled is False

    def test_disabled_when_missing_client_id(self):
        cfg = OIDCConfig(
            provider_url="https://oauth.example.com",
            redirect_uri="https://myapp.example.com/callback",
        )
        assert cfg.enabled is False

    def test_disabled_when_missing_redirect_uri(self):
        cfg = OIDCConfig(
            provider_url="https://oauth.example.com",
            client_id="myapp",
        )
        assert cfg.enabled is False

    def test_disabled_by_default(self):
        cfg = OIDCConfig()
        assert cfg.enabled is False

    def test_default_scopes(self):
        cfg = OIDCConfig()
        assert cfg.scopes == "openid email profile"

    def test_default_role(self):
        cfg = OIDCConfig()
        assert cfg.default_role == "builtin-operator"

    def test_from_env(self):
        env = {
            "TURNSTONE_OIDC_PROVIDER_URL": "https://oauth.example.com",
            "TURNSTONE_OIDC_CLIENT_ID": "testapp",
            "TURNSTONE_OIDC_CLIENT_SECRET": "secret123",
            "TURNSTONE_OIDC_REDIRECT_URI": "https://testapp.example.com/cb",
            "TURNSTONE_OIDC_SCOPES": "openid email",
            "TURNSTONE_OIDC_DEFAULT_ROLE": "builtin-admin",
        }
        with patch.dict("os.environ", env, clear=False):
            cfg = OIDCConfig.from_env()
        assert cfg.provider_url == "https://oauth.example.com"
        assert cfg.client_id == "testapp"
        assert cfg.client_secret == "secret123"
        assert cfg.redirect_uri == "https://testapp.example.com/cb"
        assert cfg.scopes == "openid email"
        assert cfg.default_role == "builtin-admin"

    def test_from_env_defaults(self):
        with patch.dict("os.environ", {}, clear=False):
            cfg = OIDCConfig.from_env()
        assert cfg.enabled is False
        assert cfg.scopes == "openid email profile"


# ---------------------------------------------------------------------------
# State management (CSRF)
# ---------------------------------------------------------------------------


class TestStateManagement:
    def test_create_and_validate(self):
        state = _create_state()
        assert isinstance(state, str)
        assert len(state) > 20
        assert _validate_state(state) is True

    def test_state_consumed_on_validate(self):
        state = _create_state()
        assert _validate_state(state) is True
        assert _validate_state(state) is False  # Already consumed

    def test_invalid_state(self):
        assert _validate_state("nonexistent") is False

    def test_empty_state(self):
        assert _validate_state("") is False

    def test_expired_state(self):
        from turnstone.core import auth_oidc

        state = _create_state()
        # Manually expire the state
        with auth_oidc._states_lock:
            auth_oidc._pending_states[state] = time.time() - 700  # Expired (>600s TTL)
        assert _validate_state(state) is False


# ---------------------------------------------------------------------------
# JWT claims decoding
# ---------------------------------------------------------------------------


class TestDecodeJwtClaims:
    def test_valid_token(self):
        claims = {"sub": "user123", "email": "user@example.com", "name": "Test User"}
        token = _make_id_token(claims)
        result = _decode_jwt_claims(token)
        assert result is not None
        assert result["email"] == "user@example.com"
        assert result["sub"] == "user123"

    def test_invalid_token_no_dots(self):
        assert _decode_jwt_claims("notajwt") is None

    def test_invalid_token_bad_base64(self):
        assert _decode_jwt_claims("a.!!!invalid!!!.c") is None

    def test_empty_token(self):
        assert _decode_jwt_claims("") is None


# ---------------------------------------------------------------------------
# Email to username conversion
# ---------------------------------------------------------------------------


class TestEmailToUsername:
    def test_simple_email(self):
        assert _email_to_username("john@example.com") == "john"

    def test_email_with_dots(self):
        assert _email_to_username("john.doe@example.com") == "john.doe"

    def test_email_with_plus(self):
        assert _email_to_username("john+tag@example.com") == "john_tag"

    def test_email_with_underscores(self):
        assert _email_to_username("john_doe@example.com") == "john_doe"

    def test_email_uppercase(self):
        assert _email_to_username("John.Doe@example.com") == "john.doe"

    def test_plain_string(self):
        assert _email_to_username("johndoe") == "johndoe"

    def test_empty_string(self):
        assert _email_to_username("") == "oidc-user"


# ---------------------------------------------------------------------------
# Identity extraction
# ---------------------------------------------------------------------------


class TestExtractIdentity:
    def test_from_id_token(self):
        claims = {"email": "user@example.com", "name": "Test User"}
        token_data = {"id_token": _make_id_token(claims), "access_token": "at_xxx"}
        result = _extract_identity(token_data, MOCK_DISCOVERY, OIDCConfig())
        assert result is not None
        assert result["email"] == "user@example.com"

    def test_fallback_to_userinfo(self):
        token_data = {"access_token": "at_xxx"}  # No id_token

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"email": "userinfo@example.com", "name": "UI User"}

        with patch("turnstone.core.auth_oidc.httpx.get", return_value=mock_resp):
            result = _extract_identity(token_data, MOCK_DISCOVERY, OIDCConfig())
        assert result is not None
        assert result["email"] == "userinfo@example.com"

    def test_no_identity_available(self):
        token_data = {}  # No id_token, no access_token
        result = _extract_identity(token_data, {}, OIDCConfig())
        assert result is None


# ---------------------------------------------------------------------------
# OIDC login handler
# ---------------------------------------------------------------------------


class TestOIDCLoginHandler:
    @pytest.fixture
    def mock_app(self):
        app = MagicMock()
        app.state.oidc_config = OIDCConfig(
            provider_url="https://oauth.example.com",
            client_id="testapp",
            client_secret="secret",
            redirect_uri="https://testapp.example.com/api/auth/oidc/callback",
        )
        return app

    @pytest.mark.asyncio
    async def test_login_redirects(self, mock_app):
        from turnstone.core.auth_oidc import handle_oidc_login

        request = MagicMock()
        request.app = mock_app

        with patch("turnstone.core.auth_oidc._discover", return_value=MOCK_DISCOVERY):
            response = await handle_oidc_login(request)

        assert response.status_code == 302
        location = response.headers.get("location", "")
        assert "oauth.example.com/oauth2/auth" in location
        assert "client_id=testapp" in location
        assert "response_type=code" in location

    @pytest.mark.asyncio
    async def test_login_disabled(self):
        from turnstone.core.auth_oidc import handle_oidc_login

        request = MagicMock()
        request.app.state.oidc_config = OIDCConfig()  # Disabled

        response = await handle_oidc_login(request)
        assert response.status_code == 503


# ---------------------------------------------------------------------------
# OIDC callback handler
# ---------------------------------------------------------------------------


class TestOIDCCallbackHandler:
    @pytest.fixture
    def mock_app(self, tmp_db):
        from turnstone.core.storage import get_storage

        app = MagicMock()
        app.state.oidc_config = OIDCConfig(
            provider_url="https://oauth.example.com",
            client_id="testapp",
            client_secret="secret",
            redirect_uri="https://testapp.example.com/api/auth/oidc/callback",
        )
        app.state.auth_storage = get_storage()
        app.state.jwt_secret = "test-secret-at-least-32-chars-long-for-hmac"
        return app

    @pytest.mark.asyncio
    async def test_callback_missing_state(self, mock_app):
        from turnstone.core.auth_oidc import handle_oidc_callback

        request = MagicMock()
        request.app = mock_app
        request.query_params = {"code": "abc123"}  # No state

        response = await handle_oidc_callback(request)
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_callback_invalid_state(self, mock_app):
        from turnstone.core.auth_oidc import handle_oidc_callback

        request = MagicMock()
        request.app = mock_app
        request.query_params = {"code": "abc123", "state": "bogus"}

        response = await handle_oidc_callback(request)
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_callback_provider_error(self, mock_app):
        from turnstone.core.auth_oidc import handle_oidc_callback

        state = _create_state()
        request = MagicMock()
        request.app = mock_app
        request.query_params = {
            "error": "access_denied",
            "error_description": "User denied consent",
            "state": state,
        }

        response = await handle_oidc_callback(request)
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_callback_success_creates_user(self, mock_app):
        from turnstone.core.auth_oidc import handle_oidc_callback

        state = _create_state()
        claims = {"email": "newuser@example.com", "name": "New User"}
        token_response = {
            "access_token": "at_xxx",
            "id_token": _make_id_token(claims),
            "token_type": "bearer",
        }

        mock_token_resp = MagicMock()
        mock_token_resp.status_code = 200
        mock_token_resp.json.return_value = token_response
        mock_token_resp.raise_for_status = MagicMock()

        request = MagicMock()
        request.app = mock_app
        request.query_params = {"code": "authcode123", "state": state}
        request.headers = {}
        request.url.scheme = "https"

        with (
            patch("turnstone.core.auth_oidc._discover", return_value=MOCK_DISCOVERY),
            patch("turnstone.core.auth_oidc.httpx.post", return_value=mock_token_resp),
        ):
            response = await handle_oidc_callback(request)

        # Should redirect to / with auth cookie
        assert response.status_code == 302
        cookie = response.headers.get("set-cookie", "")
        assert "turnstone_auth=" in cookie

        # User should be created in storage
        storage = mock_app.state.auth_storage
        user = storage.get_user_by_username("newuser")
        assert user is not None
        assert user["display_name"] == "New User"

    @pytest.mark.asyncio
    async def test_callback_existing_user(self, mock_app):
        from turnstone.core.auth_oidc import handle_oidc_callback

        # Pre-create user
        storage = mock_app.state.auth_storage
        storage.create_user("existing123", "existinguser", "Existing User", "hash")

        state = _create_state()
        claims = {"email": "existinguser@example.com", "name": "Existing User"}
        token_response = {
            "access_token": "at_xxx",
            "id_token": _make_id_token(claims),
        }

        mock_token_resp = MagicMock()
        mock_token_resp.json.return_value = token_response
        mock_token_resp.raise_for_status = MagicMock()

        request = MagicMock()
        request.app = mock_app
        request.query_params = {"code": "authcode456", "state": state}
        request.headers = {}
        request.url.scheme = "https"

        with (
            patch("turnstone.core.auth_oidc._discover", return_value=MOCK_DISCOVERY),
            patch("turnstone.core.auth_oidc.httpx.post", return_value=mock_token_resp),
        ):
            response = await handle_oidc_callback(request)

        assert response.status_code == 302
        cookie = response.headers.get("set-cookie", "")
        assert "turnstone_auth=" in cookie

    @pytest.mark.asyncio
    async def test_callback_missing_code(self, mock_app):
        from turnstone.core.auth_oidc import handle_oidc_callback

        state = _create_state()
        request = MagicMock()
        request.app = mock_app
        request.query_params = {"state": state}  # No code

        response = await handle_oidc_callback(request)
        assert response.status_code == 400


# ---------------------------------------------------------------------------
# Public paths
# ---------------------------------------------------------------------------


class TestOIDCPublicPaths:
    def test_oidc_login_is_public(self):
        from turnstone.core.auth import is_public_path

        assert is_public_path("/api/auth/oidc/login") is True

    def test_oidc_callback_is_public(self):
        from turnstone.core.auth import is_public_path

        assert is_public_path("/api/auth/oidc/callback") is True


# ---------------------------------------------------------------------------
# Auth status includes OIDC info
# ---------------------------------------------------------------------------


class TestAuthStatusOIDC:
    @pytest.mark.asyncio
    async def test_status_includes_oidc_enabled(self):
        from turnstone.core.auth import handle_auth_status

        request = MagicMock()
        request.app.state.auth_config = MagicMock(enabled=True)
        request.app.state.auth_storage = None
        request.app.state.oidc_config = OIDCConfig(
            provider_url="https://oauth.example.com",
            client_id="testapp",
            redirect_uri="https://testapp.example.com/cb",
        )

        response = await handle_auth_status(request)
        body = json.loads(response.body)
        assert body["oidc_enabled"] is True
        assert body["oidc_login_url"] == "/api/auth/oidc/login"

    @pytest.mark.asyncio
    async def test_status_oidc_disabled(self):
        from turnstone.core.auth import handle_auth_status

        request = MagicMock()
        request.app.state.auth_config = MagicMock(enabled=False)
        request.app.state.auth_storage = None
        request.app.state.oidc_config = OIDCConfig()  # Disabled

        response = await handle_auth_status(request)
        body = json.loads(response.body)
        assert body["oidc_enabled"] is False
        assert body["oidc_login_url"] is None
