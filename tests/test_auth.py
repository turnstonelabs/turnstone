"""Tests for turnstone.core.auth — bearer token authentication and cookies."""

import os
from unittest.mock import patch

import pytest

from turnstone.core.auth import (
    WRITE_PATHS,
    AuthConfig,
    _extract_bearer,
    _extract_cookie,
    check_request,
    is_public_path,
    load_auth_config,
    make_clear_cookie,
    make_set_cookie,
    required_role,
)

# ---------------------------------------------------------------------------
# TestIsPublicPath
# ---------------------------------------------------------------------------


class TestIsPublicPath:
    def test_root(self):
        assert is_public_path("/") is True

    def test_health(self):
        assert is_public_path("/health") is True

    def test_metrics(self):
        assert is_public_path("/metrics") is True

    def test_static_css(self):
        assert is_public_path("/static/style.css") is True

    def test_static_js(self):
        assert is_public_path("/static/app.js") is True

    def test_static_subdir(self):
        assert is_public_path("/static/fonts/mono.woff2") is True

    def test_api_workstreams_not_public(self):
        assert is_public_path("/api/workstreams") is False

    def test_api_send_not_public(self):
        assert is_public_path("/api/send") is False

    def test_api_cluster_overview_not_public(self):
        assert is_public_path("/api/cluster/overview") is False

    def test_api_events_not_public(self):
        assert is_public_path("/api/events") is False


# ---------------------------------------------------------------------------
# TestRequiredRole
# ---------------------------------------------------------------------------


class TestRequiredRole:
    def test_get_api_needs_read(self):
        assert required_role("GET", "/api/workstreams") == "read"

    def test_get_events_needs_read(self):
        assert required_role("GET", "/api/events") == "read"

    def test_get_dashboard_needs_read(self):
        assert required_role("GET", "/api/dashboard") == "read"

    def test_post_send_needs_full(self):
        assert required_role("POST", "/api/send") == "full"

    def test_post_approve_needs_full(self):
        assert required_role("POST", "/api/approve") == "full"

    def test_post_plan_needs_full(self):
        assert required_role("POST", "/api/plan") == "full"

    def test_post_command_needs_full(self):
        assert required_role("POST", "/api/command") == "full"

    def test_post_workstreams_new_needs_full(self):
        assert required_role("POST", "/api/workstreams/new") == "full"

    def test_post_workstreams_close_needs_full(self):
        assert required_role("POST", "/api/workstreams/close") == "full"

    def test_all_write_paths_need_full(self):
        for path in WRITE_PATHS:
            assert required_role("POST", path) == "full"

    def test_post_unknown_path_needs_read(self):
        assert required_role("POST", "/api/unknown") == "read"


# ---------------------------------------------------------------------------
# TestAuthConfig
# ---------------------------------------------------------------------------


class TestAuthConfig:
    def test_check_valid_full_token(self):
        cfg = AuthConfig(enabled=True, tokens={"tok_full": "full", "tok_read": "read"})
        assert cfg.check("tok_full") == "full"

    def test_check_valid_read_token(self):
        cfg = AuthConfig(enabled=True, tokens={"tok_full": "full", "tok_read": "read"})
        assert cfg.check("tok_read") == "read"

    def test_check_invalid_token(self):
        cfg = AuthConfig(enabled=True, tokens={"tok_full": "full"})
        assert cfg.check("wrong") is None

    def test_check_none_token(self):
        cfg = AuthConfig(enabled=True, tokens={"tok_full": "full"})
        assert cfg.check(None) is None

    def test_check_empty_token(self):
        cfg = AuthConfig(enabled=True, tokens={"tok_full": "full"})
        assert cfg.check("") is None

    def test_check_no_tokens(self):
        cfg = AuthConfig(enabled=True, tokens={})
        assert cfg.check("anything") is None


# ---------------------------------------------------------------------------
# TestExtractBearer
# ---------------------------------------------------------------------------


class TestExtractBearer:
    def test_valid_bearer(self):
        assert _extract_bearer("Bearer tok_abc123") == "tok_abc123"

    def test_case_insensitive(self):
        assert _extract_bearer("bearer tok_abc123") == "tok_abc123"

    def test_mixed_case(self):
        assert _extract_bearer("BEARER tok_abc123") == "tok_abc123"

    def test_no_bearer_prefix(self):
        assert _extract_bearer("tok_abc123") is None

    def test_basic_auth_ignored(self):
        assert _extract_bearer("Basic dXNlcjpwYXNz") is None

    def test_none(self):
        assert _extract_bearer(None) is None

    def test_empty(self):
        assert _extract_bearer("") is None

    def test_bearer_only_no_token(self):
        assert _extract_bearer("Bearer") is None

    def test_token_with_spaces(self):
        # Only the first space separates scheme from token
        assert _extract_bearer("Bearer tok with spaces") == "tok with spaces"


# ---------------------------------------------------------------------------
# TestExtractCookie
# ---------------------------------------------------------------------------


class TestExtractCookie:
    def test_single_cookie(self):
        assert _extract_cookie("turnstone_auth=tok_abc", "turnstone_auth") == "tok_abc"

    def test_multiple_cookies(self):
        header = "theme=dark; turnstone_auth=tok_abc; other=val"
        assert _extract_cookie(header, "turnstone_auth") == "tok_abc"

    def test_missing_cookie(self):
        assert _extract_cookie("theme=dark; other=val", "turnstone_auth") is None

    def test_none_header(self):
        assert _extract_cookie(None, "turnstone_auth") is None

    def test_empty_header(self):
        assert _extract_cookie("", "turnstone_auth") is None

    def test_spaces_around_value(self):
        assert _extract_cookie("turnstone_auth = tok_abc ", "turnstone_auth") == "tok_abc"

    def test_no_equals(self):
        assert _extract_cookie("malformed", "turnstone_auth") is None


# ---------------------------------------------------------------------------
# TestMakeSetCookie / TestMakeClearCookie
# ---------------------------------------------------------------------------


class TestMakeSetCookie:
    def test_contains_token(self):
        val = make_set_cookie("tok_abc")
        assert "turnstone_auth=tok_abc" in val

    def test_httponly(self):
        assert "HttpOnly" in make_set_cookie("tok_abc")

    def test_samesite_lax(self):
        assert "SameSite=Lax" in make_set_cookie("tok_abc")

    def test_path(self):
        assert "Path=/" in make_set_cookie("tok_abc")

    def test_max_age_default(self):
        val = make_set_cookie("tok_abc")
        assert "Max-Age=2592000" in val  # 30 days

    def test_max_age_custom(self):
        val = make_set_cookie("tok_abc", max_age=3600)
        assert "Max-Age=3600" in val


class TestMakeClearCookie:
    def test_max_age_zero(self):
        assert "Max-Age=0" in make_clear_cookie()

    def test_empty_value(self):
        assert "turnstone_auth=;" in make_clear_cookie()

    def test_httponly(self):
        assert "HttpOnly" in make_clear_cookie()


# ---------------------------------------------------------------------------
# TestCheckRequest
# ---------------------------------------------------------------------------


class TestCheckRequest:
    """Tests for the main check_request() entry point."""

    @pytest.fixture()
    def disabled(self):
        return AuthConfig(enabled=False)

    @pytest.fixture()
    def enabled(self):
        return AuthConfig(
            enabled=True,
            tokens={"tok_full": "full", "tok_read": "read"},
        )

    def test_disabled_allows_all(self, disabled):
        allowed, status, msg = check_request(disabled, "POST", "/api/send", None)
        assert allowed is True
        assert status == 200

    def test_disabled_allows_no_header(self, disabled):
        allowed, status, msg = check_request(disabled, "GET", "/api/workstreams", None)
        assert allowed is True

    def test_public_path_no_token_ok(self, enabled):
        allowed, status, msg = check_request(enabled, "GET", "/health", None)
        assert allowed is True
        assert status == 200

    def test_public_root_no_token_ok(self, enabled):
        allowed, status, msg = check_request(enabled, "GET", "/", None)
        assert allowed is True

    def test_public_static_no_token_ok(self, enabled):
        allowed, status, msg = check_request(enabled, "GET", "/static/style.css", None)
        assert allowed is True

    def test_api_no_token_401(self, enabled):
        allowed, status, msg = check_request(enabled, "GET", "/api/workstreams", None)
        assert allowed is False
        assert status == 401
        assert "Unauthorized" in msg

    def test_api_invalid_token_401(self, enabled):
        allowed, status, msg = check_request(
            enabled, "GET", "/api/workstreams", "Bearer wrong_token"
        )
        assert allowed is False
        assert status == 401

    def test_api_read_token_ok(self, enabled):
        allowed, status, msg = check_request(enabled, "GET", "/api/workstreams", "Bearer tok_read")
        assert allowed is True
        assert status == 200

    def test_api_full_token_ok(self, enabled):
        allowed, status, msg = check_request(enabled, "GET", "/api/workstreams", "Bearer tok_full")
        assert allowed is True

    def test_write_read_token_403(self, enabled):
        allowed, status, msg = check_request(enabled, "POST", "/api/send", "Bearer tok_read")
        assert allowed is False
        assert status == 403
        assert "Forbidden" in msg

    def test_write_full_token_ok(self, enabled):
        allowed, status, msg = check_request(enabled, "POST", "/api/send", "Bearer tok_full")
        assert allowed is True
        assert status == 200

    def test_approve_read_token_403(self, enabled):
        allowed, status, msg = check_request(enabled, "POST", "/api/approve", "Bearer tok_read")
        assert allowed is False
        assert status == 403

    def test_approve_full_token_ok(self, enabled):
        allowed, status, msg = check_request(enabled, "POST", "/api/approve", "Bearer tok_full")
        assert allowed is True

    def test_no_auth_header_string(self, enabled):
        allowed, status, msg = check_request(enabled, "GET", "/api/dashboard", "")
        assert allowed is False
        assert status == 401


# ---------------------------------------------------------------------------
# TestCheckRequestWithCookie
# ---------------------------------------------------------------------------


class TestCheckRequestWithCookie:
    """Tests for cookie-based auth fallback in check_request."""

    @pytest.fixture()
    def enabled(self):
        return AuthConfig(
            enabled=True,
            tokens={"tok_full": "full", "tok_read": "read"},
        )

    def test_cookie_fallback_when_no_bearer(self, enabled):
        allowed, status, _ = check_request(
            enabled,
            "GET",
            "/api/workstreams",
            None,
            cookie_header="turnstone_auth=tok_read",
        )
        assert allowed is True
        assert status == 200

    def test_bearer_takes_precedence_over_cookie(self, enabled):
        # Bearer is full, cookie is read — Bearer should win
        allowed, status, _ = check_request(
            enabled,
            "POST",
            "/api/send",
            "Bearer tok_full",
            cookie_header="turnstone_auth=tok_read",
        )
        assert allowed is True

    def test_invalid_cookie_401(self, enabled):
        allowed, status, _ = check_request(
            enabled,
            "GET",
            "/api/workstreams",
            None,
            cookie_header="turnstone_auth=wrong_token",
        )
        assert allowed is False
        assert status == 401

    def test_cookie_read_on_write_403(self, enabled):
        allowed, status, _ = check_request(
            enabled,
            "POST",
            "/api/send",
            None,
            cookie_header="turnstone_auth=tok_read",
        )
        assert allowed is False
        assert status == 403

    def test_cookie_full_on_write_ok(self, enabled):
        allowed, status, _ = check_request(
            enabled,
            "POST",
            "/api/send",
            None,
            cookie_header="turnstone_auth=tok_full",
        )
        assert allowed is True

    def test_no_cookie_no_bearer_401(self, enabled):
        allowed, status, _ = check_request(
            enabled,
            "GET",
            "/api/workstreams",
            None,
            cookie_header=None,
        )
        assert allowed is False
        assert status == 401

    def test_login_path_public(self, enabled):
        allowed, status, _ = check_request(
            enabled,
            "POST",
            "/api/auth/login",
            None,
        )
        assert allowed is True

    def test_logout_path_public(self, enabled):
        allowed, status, _ = check_request(
            enabled,
            "POST",
            "/api/auth/logout",
            None,
        )
        assert allowed is True


# ---------------------------------------------------------------------------
# TestLoadAuthConfig
# ---------------------------------------------------------------------------


class TestLoadAuthConfig:
    """Tests for load_auth_config with mocked config + env vars."""

    def test_default_disabled(self):
        with patch("turnstone.core.config.load_config", return_value={}):
            cfg = load_auth_config()
        assert cfg.enabled is False
        assert cfg.tokens == {}

    def test_config_file_tokens(self):
        mock_cfg = {
            "enabled": True,
            "tokens": [
                {"value": "tok_a", "role": "full"},
                {"value": "tok_b", "role": "read"},
            ],
        }
        with (
            patch("turnstone.core.config.load_config", return_value=mock_cfg),
            patch.dict(os.environ, {}, clear=True),
        ):
            cfg = load_auth_config()
        assert cfg.enabled is True
        assert cfg.tokens == {"tok_a": "full", "tok_b": "read"}

    def test_env_var_enabled(self):
        with (
            patch("turnstone.core.config.load_config", return_value={}),
            patch.dict(os.environ, {"TURNSTONE_AUTH_ENABLED": "1"}, clear=False),
        ):
            cfg = load_auth_config()
        assert cfg.enabled is True

    def test_env_var_token(self):
        with (
            patch("turnstone.core.config.load_config", return_value={}),
            patch.dict(os.environ, {"TURNSTONE_AUTH_TOKEN": "tok_env"}, clear=False),
        ):
            cfg = load_auth_config()
        assert "tok_env" in cfg.tokens
        assert cfg.tokens["tok_env"] == "full"

    def test_config_plus_env_merge(self):
        mock_cfg = {
            "enabled": True,
            "tokens": [{"value": "tok_cfg", "role": "read"}],
        }
        with (
            patch("turnstone.core.config.load_config", return_value=mock_cfg),
            patch.dict(os.environ, {"TURNSTONE_AUTH_TOKEN": "tok_env"}, clear=False),
        ):
            cfg = load_auth_config()
        assert cfg.tokens["tok_cfg"] == "read"
        assert cfg.tokens["tok_env"] == "full"

    def test_invalid_role_skipped(self):
        mock_cfg = {
            "enabled": True,
            "tokens": [
                {"value": "tok_ok", "role": "full"},
                {"value": "tok_bad", "role": "admin"},
            ],
        }
        with (
            patch("turnstone.core.config.load_config", return_value=mock_cfg),
            patch.dict(os.environ, {}, clear=True),
        ):
            cfg = load_auth_config()
        assert "tok_ok" in cfg.tokens
        assert "tok_bad" not in cfg.tokens

    def test_empty_value_skipped(self):
        mock_cfg = {
            "enabled": True,
            "tokens": [{"value": "", "role": "full"}],
        }
        with (
            patch("turnstone.core.config.load_config", return_value=mock_cfg),
            patch.dict(os.environ, {}, clear=True),
        ):
            cfg = load_auth_config()
        assert len(cfg.tokens) == 0

    def test_non_dict_token_entry_skipped(self):
        mock_cfg = {
            "enabled": True,
            "tokens": ["not_a_dict", {"value": "tok_ok", "role": "full"}],
        }
        with (
            patch("turnstone.core.config.load_config", return_value=mock_cfg),
            patch.dict(os.environ, {}, clear=True),
        ):
            cfg = load_auth_config()
        assert cfg.tokens == {"tok_ok": "full"}

    def test_env_enabled_true(self):
        with (
            patch("turnstone.core.config.load_config", return_value={}),
            patch.dict(os.environ, {"TURNSTONE_AUTH_ENABLED": "true"}, clear=False),
        ):
            cfg = load_auth_config()
        assert cfg.enabled is True

    def test_env_enabled_yes(self):
        with (
            patch("turnstone.core.config.load_config", return_value={}),
            patch.dict(os.environ, {"TURNSTONE_AUTH_ENABLED": "yes"}, clear=False),
        ):
            cfg = load_auth_config()
        assert cfg.enabled is True


# ---------------------------------------------------------------------------
# Integration tests — actual HTTP server with auth enabled
# ---------------------------------------------------------------------------


class TestServerAuth:
    """Test turnstone-server with auth enabled using Starlette TestClient."""

    @classmethod
    def setup_class(cls):
        import queue
        import threading
        from unittest.mock import MagicMock

        from starlette.testclient import TestClient

        import turnstone.server as srv_mod
        from turnstone.core.metrics import MetricsCollector
        from turnstone.core.workstream import WorkstreamState

        srv_mod._metrics = MetricsCollector()
        srv_mod._metrics.model = "test-model"

        mock_session = MagicMock()
        mock_session.session_id = "test-session-id"

        mock_ws = MagicMock()
        mock_ws.id = "test-ws"
        mock_ws.name = "test"
        mock_ws.state = WorkstreamState.IDLE
        mock_ws.session = mock_session
        mock_mgr = MagicMock()
        mock_mgr.list_all.return_value = [mock_ws]

        app = srv_mod.create_app(
            workstreams=mock_mgr,
            global_queue=queue.Queue(),
            global_listeners=[],
            global_listeners_lock=threading.Lock(),
            skip_permissions=False,
            auth_config=AuthConfig(
                enabled=True,
                tokens={"tok_full": "full", "tok_read": "read"},
            ),
        )
        cls.client = TestClient(app, raise_server_exceptions=False)

    @classmethod
    def teardown_class(cls):
        cls.client.close()

    def test_health_no_token_200(self):
        resp = self.client.get("/health")
        assert resp.status_code == 200

    def test_metrics_no_token_passes_auth(self):
        resp = self.client.get("/metrics")
        # Public path — should never be 401/403
        assert resp.status_code not in (401, 403)

    def test_root_no_token_200(self):
        resp = self.client.get("/")
        assert resp.status_code == 200

    def test_static_css_no_token_200(self):
        resp = self.client.get("/static/style.css")
        assert resp.status_code == 200

    def test_api_workstreams_no_token_401(self):
        resp = self.client.get("/api/workstreams")
        assert resp.status_code == 401
        assert "Unauthorized" in resp.json().get("error", "")

    def test_api_workstreams_read_token_200(self):
        resp = self.client.get(
            "/api/workstreams",
            headers={"Authorization": "Bearer tok_read"},
        )
        assert resp.status_code == 200

    def test_api_workstreams_full_token_200(self):
        resp = self.client.get(
            "/api/workstreams",
            headers={"Authorization": "Bearer tok_full"},
        )
        assert resp.status_code == 200

    def test_api_send_read_token_403(self):
        resp = self.client.post(
            "/api/send",
            headers={"Authorization": "Bearer tok_read"},
            json={"message": "hello", "ws_id": "x"},
        )
        assert resp.status_code == 403
        assert "Forbidden" in resp.json().get("error", "")

    def test_api_send_full_token_passes_auth(self):
        resp = self.client.post(
            "/api/send",
            headers={"Authorization": "Bearer tok_full"},
            json={"message": "hello", "ws_id": "nonexistent"},
        )
        # Should get 404 (unknown workstream), not 401/403
        assert resp.status_code not in (401, 403)

    def test_api_send_no_token_401(self):
        resp = self.client.post(
            "/api/send",
            json={"message": "hello", "ws_id": "x"},
        )
        assert resp.status_code == 401

    def test_invalid_token_401(self):
        resp = self.client.get(
            "/api/workstreams",
            headers={"Authorization": "Bearer wrong_token"},
        )
        assert resp.status_code == 401

    def test_options_no_auth_required(self):
        resp = self.client.options(
            "/api/send",
            headers={
                "Origin": "http://example.com",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert resp.status_code == 200
        allowed = resp.headers.get("access-control-allow-headers", "")
        assert "authorization" in allowed.lower()

    def test_cors_includes_authorization(self):
        resp = self.client.options(
            "/api/workstreams",
            headers={
                "Origin": "http://example.com",
                "Access-Control-Request-Method": "GET",
            },
        )
        allowed = resp.headers.get("access-control-allow-headers", "")
        assert "authorization" in allowed.lower()


class TestConsoleAuth:
    """Test console server with auth enabled using TestClient."""

    @classmethod
    def setup_class(cls):
        from unittest.mock import MagicMock

        from starlette.testclient import TestClient

        from turnstone.console.collector import ClusterCollector
        from turnstone.console.server import _load_static, create_app

        _load_static()

        mock_collector = MagicMock(spec=ClusterCollector)
        mock_collector.get_overview.return_value = {
            "nodes": 1,
            "workstreams": 2,
            "states": {"running": 1, "idle": 1},
            "aggregate": {"total_tokens": 100},
        }

        app = create_app(
            collector=mock_collector,
            broker=MagicMock(),
            auth_config=AuthConfig(
                enabled=True,
                tokens={"tok_full": "full", "tok_read": "read"},
            ),
        )
        cls.test_client = TestClient(app, raise_server_exceptions=False)

    @classmethod
    def teardown_class(cls):
        cls.test_client.close()

    def test_health_no_token_200(self):
        resp = self.test_client.get("/health")
        assert resp.status_code == 200

    def test_root_no_token_200(self):
        resp = self.test_client.get("/")
        assert resp.status_code == 200

    def test_api_overview_no_token_401(self):
        resp = self.test_client.get("/api/cluster/overview")
        assert resp.status_code == 401

    def test_api_overview_read_token_200(self):
        resp = self.test_client.get(
            "/api/cluster/overview",
            headers={"Authorization": "Bearer tok_read"},
        )
        assert resp.status_code == 200

    def test_api_overview_full_token_200(self):
        resp = self.test_client.get(
            "/api/cluster/overview",
            headers={"Authorization": "Bearer tok_full"},
        )
        assert resp.status_code == 200

    def test_invalid_token_401(self):
        resp = self.test_client.get(
            "/api/cluster/overview",
            headers={"Authorization": "Bearer wrong"},
        )
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Login / Logout integration tests
# ---------------------------------------------------------------------------


class TestServerLogin:
    """Test login/logout cookie flow on turnstone-server."""

    @classmethod
    def setup_class(cls):
        import queue
        import threading
        from unittest.mock import MagicMock

        from starlette.testclient import TestClient

        import turnstone.server as srv_mod
        from turnstone.core.metrics import MetricsCollector
        from turnstone.core.workstream import WorkstreamState

        srv_mod._metrics = MetricsCollector()
        srv_mod._metrics.model = "test-model"

        mock_session = MagicMock()
        mock_session.session_id = "test-session-id"

        mock_ws = MagicMock()
        mock_ws.id = "test-ws"
        mock_ws.name = "test"
        mock_ws.state = WorkstreamState.IDLE
        mock_ws.session = mock_session
        mock_mgr = MagicMock()
        mock_mgr.list_all.return_value = [mock_ws]

        app = srv_mod.create_app(
            workstreams=mock_mgr,
            global_queue=queue.Queue(),
            global_listeners=[],
            global_listeners_lock=threading.Lock(),
            skip_permissions=False,
            auth_config=AuthConfig(
                enabled=True,
                tokens={"tok_full": "full", "tok_read": "read"},
            ),
        )
        cls.test_client = TestClient(app, raise_server_exceptions=False)

    @classmethod
    def teardown_class(cls):
        cls.test_client.close()

    def test_login_valid_token_sets_cookie(self):
        resp = self.test_client.post(
            "/api/auth/login",
            json={"token": "tok_full"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["role"] == "full"
        cookie = resp.headers.get("set-cookie", "")
        assert "turnstone_auth=tok_full" in cookie
        assert "HttpOnly" in cookie

    def test_login_invalid_token_401(self):
        resp = self.test_client.post(
            "/api/auth/login",
            json={"token": "wrong"},
        )
        assert resp.status_code == 401

    def test_login_no_auth_required(self):
        # /api/auth/login is public — shouldn't require auth itself
        resp = self.test_client.post(
            "/api/auth/login",
            json={"token": "tok_read"},
        )
        assert resp.status_code == 200

    def test_cookie_auth_on_api(self):
        # Login to get cookie (TestClient tracks cookies automatically)
        login_resp = self.test_client.post("/api/auth/login", json={"token": "tok_read"})
        assert login_resp.status_code == 200

        # Use cookie to access API — TestClient forwards cookies
        resp = self.test_client.get("/api/workstreams")
        assert resp.status_code == 200

    def test_logout_clears_cookie(self):
        self.test_client.post("/api/auth/login", json={"token": "tok_read"})

        # Logout
        logout_resp = self.test_client.post("/api/auth/logout")
        assert logout_resp.status_code == 200
        cookie = logout_resp.headers.get("set-cookie", "")
        assert "Max-Age=0" in cookie

        # API should now fail (cookie cleared)
        resp = self.test_client.get("/api/workstreams")
        assert resp.status_code == 401


class TestConsoleLogin:
    """Test login/logout cookie flow on turnstone-console."""

    @classmethod
    def setup_class(cls):
        from unittest.mock import MagicMock

        from starlette.testclient import TestClient

        from turnstone.console.collector import ClusterCollector
        from turnstone.console.server import _load_static, create_app

        _load_static()

        mock_collector = MagicMock(spec=ClusterCollector)
        mock_collector.get_overview.return_value = {
            "nodes": 1,
            "workstreams": 2,
            "states": {"running": 1, "idle": 1},
            "aggregate": {"total_tokens": 100},
        }

        app = create_app(
            collector=mock_collector,
            broker=MagicMock(),
            auth_config=AuthConfig(
                enabled=True,
                tokens={"tok_full": "full", "tok_read": "read"},
            ),
        )
        cls.test_client = TestClient(app, raise_server_exceptions=False)

    @classmethod
    def teardown_class(cls):
        cls.test_client.close()

    def test_login_valid_token(self):
        resp = self.test_client.post(
            "/api/auth/login",
            json={"token": "tok_read"},
        )
        assert resp.status_code == 200
        assert "turnstone_auth" in resp.headers.get("set-cookie", "")

    def test_login_invalid_token(self):
        resp = self.test_client.post(
            "/api/auth/login",
            json={"token": "wrong"},
        )
        assert resp.status_code == 401

    def test_cookie_auth_on_api(self):
        self.test_client.post("/api/auth/login", json={"token": "tok_read"})
        resp = self.test_client.get("/api/cluster/overview")
        assert resp.status_code == 200

    def test_logout_then_api_fails(self):
        self.test_client.post("/api/auth/login", json={"token": "tok_read"})
        self.test_client.post("/api/auth/logout")
        resp = self.test_client.get("/api/cluster/overview")
        assert resp.status_code == 401
