"""Tests for turnstone.core.auth — bearer token authentication and cookies."""

import os
import queue
import threading
from unittest.mock import MagicMock, patch

import pytest

from turnstone.core.auth import (
    WRITE_PATHS,
    _extract_bearer,
    _extract_cookie,
    check_request,
    create_jwt,
    is_public_path,
    load_jwt_secret,
    make_clear_cookie,
    make_set_cookie,
    required_scope,
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

    def test_shared_css_public(self):
        assert is_public_path("/shared/base.css") is True

    def test_shared_js_public(self):
        assert is_public_path("/shared/utils.js") is True

    def test_api_workstreams_not_public(self):
        assert is_public_path("/api/workstreams") is False

    def test_api_send_not_public(self):
        assert is_public_path("/api/send") is False

    def test_api_cluster_overview_not_public(self):
        assert is_public_path("/api/cluster/overview") is False

    def test_api_events_not_public(self):
        assert is_public_path("/api/events") is False

    def test_v1_api_login_public(self):
        assert is_public_path("/v1/api/auth/login") is True

    def test_v1_api_logout_public(self):
        assert is_public_path("/v1/api/auth/logout") is True

    def test_v1_api_workstreams_not_public(self):
        assert is_public_path("/v1/api/workstreams") is False

    def test_v1_api_send_not_public(self):
        assert is_public_path("/v1/api/send") is False

    def test_openapi_json_public(self):
        assert is_public_path("/openapi.json") is True

    def test_docs_public(self):
        assert is_public_path("/docs") is True

    def test_shared_static_public(self):
        assert is_public_path("/shared/base.css") is True


# ---------------------------------------------------------------------------
# TestRequiredRole
# ---------------------------------------------------------------------------


class TestRequiredScope:
    def test_get_api_needs_read(self):
        assert required_scope("GET", "/api/workstreams") == "read"

    def test_get_events_needs_read(self):
        assert required_scope("GET", "/api/events") == "read"

    def test_post_send_needs_write(self):
        assert required_scope("POST", "/api/send") == "write"

    def test_post_approve_needs_approve(self):
        assert required_scope("POST", "/api/approve") == "approve"

    def test_post_plan_needs_write(self):
        assert required_scope("POST", "/api/plan") == "write"

    def test_post_command_needs_write(self):
        assert required_scope("POST", "/api/command") == "write"

    def test_post_workstreams_new_needs_write(self):
        assert required_scope("POST", "/api/workstreams/new") == "write"

    def test_post_workstreams_close_needs_write(self):
        assert required_scope("POST", "/api/workstreams/close") == "write"

    def test_all_write_paths_need_write(self):
        for path in WRITE_PATHS:
            scope = required_scope("POST", path)
            assert scope in ("write", "approve"), f"{path} should need write or approve"

    def test_post_unknown_path_needs_read(self):
        assert required_scope("POST", "/api/unknown") == "read"

    def test_v1_post_send_needs_write(self):
        assert required_scope("POST", "/v1/api/send") == "write"

    def test_v1_post_approve_needs_approve(self):
        assert required_scope("POST", "/v1/api/approve") == "approve"

    def test_v1_get_workstreams_needs_read(self):
        assert required_scope("GET", "/v1/api/workstreams") == "read"

    def test_v1_post_cluster_ws_new_needs_write(self):
        assert required_scope("POST", "/v1/api/cluster/workstreams/new") == "write"

    def test_proxy_v1_send_needs_write(self):
        assert required_scope("POST", "/node/node-a/v1/api/send") == "write"

    def test_proxy_v1_approve_needs_approve(self):
        assert required_scope("POST", "/node/node-a/v1/api/approve") == "approve"

    def test_proxy_v1_read_endpoint_needs_read(self):
        assert required_scope("GET", "/node/node-a/v1/api/workstreams") == "read"

    # Memory endpoints
    def test_get_memories_needs_read(self):
        assert required_scope("GET", "/api/memories") == "read"

    def test_post_memories_needs_write(self):
        assert required_scope("POST", "/api/memories") == "write"

    def test_post_memories_search_needs_read(self):
        """Search via POST is non-mutating — requires only read scope."""
        assert required_scope("POST", "/api/memories/search") == "read"

    def test_delete_memory_needs_write(self):
        assert required_scope("DELETE", "/api/memories/my_key") == "write"

    def test_v1_post_memories_needs_write(self):
        assert required_scope("POST", "/v1/api/memories") == "write"

    def test_v1_delete_memory_needs_write(self):
        assert required_scope("DELETE", "/v1/api/memories/test_key") == "write"

    def test_admin_memories_needs_approve(self):
        assert required_scope("GET", "/api/admin/memories") == "approve"

    def test_admin_memory_delete_needs_approve(self):
        assert required_scope("DELETE", "/api/admin/memories/some-id") == "approve"

    # Internal endpoints
    def test_internal_mcp_reload_needs_approve(self):
        assert required_scope("POST", "/api/_internal/mcp-reload") == "approve"

    def test_v1_internal_mcp_reload_needs_approve(self):
        assert required_scope("POST", "/v1/api/_internal/mcp-reload") == "approve"

    def test_internal_config_reload_needs_approve(self):
        assert required_scope("POST", "/api/_internal/config-reload") == "approve"

    def test_v1_internal_config_reload_needs_approve(self):
        assert required_scope("POST", "/v1/api/_internal/config-reload") == "approve"

    def test_proxy_internal_config_reload_needs_approve(self):
        assert required_scope("POST", "/node/n1/v1/api/_internal/config-reload") == "approve"

    def test_proxy_no_v1_internal_config_reload_needs_approve(self):
        assert required_scope("POST", "/node/n1/api/_internal/config-reload") == "approve"

    def test_proxy_internal_mcp_reload_needs_approve(self):
        assert required_scope("POST", "/node/n1/v1/api/_internal/mcp-reload") == "approve"

    def test_proxy_no_v1_internal_mcp_reload_needs_approve(self):
        assert required_scope("POST", "/node/n1/api/_internal/mcp-reload") == "approve"

    def test_get_internal_mcp_reload_needs_read(self):
        """Only POST is elevated — GET falls through to read."""
        assert required_scope("GET", "/api/_internal/mcp-reload") == "read"

    # Workstream sub-resource mutations (parametric paths)
    def test_ws_delete_needs_write(self):
        assert required_scope("POST", "/api/workstreams/abc123/delete") == "write"

    def test_ws_open_needs_write(self):
        assert required_scope("POST", "/api/workstreams/abc123/open") == "write"

    def test_ws_refresh_title_needs_write(self):
        assert required_scope("POST", "/api/workstreams/abc123/refresh-title") == "write"

    def test_ws_title_needs_write(self):
        assert required_scope("POST", "/api/workstreams/abc123/title") == "write"

    def test_v1_ws_delete_needs_write(self):
        assert required_scope("POST", "/v1/api/workstreams/abc123/delete") == "write"

    def test_proxy_ws_delete_needs_write(self):
        assert required_scope("POST", "/node/n1/v1/api/workstreams/abc123/delete") == "write"

    def test_proxy_ws_open_needs_write(self):
        assert required_scope("POST", "/node/n1/v1/api/workstreams/abc123/open") == "write"

    def test_proxy_ws_title_needs_write(self):
        assert required_scope("POST", "/node/n1/v1/api/workstreams/abc123/title") == "write"

    def test_ws_get_is_still_read(self):
        """GET on workstream sub-resource is not elevated."""
        assert required_scope("GET", "/api/workstreams/abc123/delete") == "read"


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
        assert "Max-Age=86400" in val  # 24 hours (matches JWT expiry)

    def test_max_age_custom(self):
        val = make_set_cookie("tok_abc", max_age=3600)
        assert "Max-Age=3600" in val

    def test_secure_default(self):
        val = make_set_cookie("tok_abc")
        assert "; Secure" in val

    def test_secure_false(self):
        val = make_set_cookie("tok_abc", secure=False)
        assert "; Secure" not in val

    def test_secure_true(self):
        val = make_set_cookie("tok_abc", secure=True)
        assert "; Secure" in val


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

    _SECRET = "test-jwt-secret-minimum-32-chars!"

    @pytest.fixture()
    def read_jwt(self):
        return f"Bearer {create_jwt('u1', frozenset({'read'}), 'test', self._SECRET)}"

    @pytest.fixture()
    def full_jwt(self):
        return f"Bearer {create_jwt('u1', frozenset({'read', 'write', 'approve'}), 'test', self._SECRET)}"

    def test_public_path_no_token_ok(self):
        allowed, status, msg, _result = check_request("GET", "/health", None)
        assert allowed is True
        assert status == 200

    def test_public_root_no_token_ok(self):
        allowed, status, msg, _result = check_request("GET", "/", None)
        assert allowed is True

    def test_public_static_no_token_ok(self):
        allowed, status, msg, _result = check_request("GET", "/static/style.css", None)
        assert allowed is True

    def test_api_no_token_401(self):
        allowed, status, msg, _result = check_request("GET", "/api/workstreams", None)
        assert allowed is False
        assert status == 401
        assert "Unauthorized" in msg

    def test_api_invalid_token_401(self):
        allowed, status, msg, _result = check_request(
            "GET", "/api/workstreams", "Bearer wrong_token"
        )
        assert allowed is False
        assert status == 401

    def test_api_read_token_ok(self, read_jwt):
        allowed, status, msg, _result = check_request(
            "GET", "/api/workstreams", read_jwt, jwt_secret=self._SECRET
        )
        assert allowed is True
        assert status == 200

    def test_api_full_token_ok(self, full_jwt):
        allowed, status, msg, _result = check_request(
            "GET", "/api/workstreams", full_jwt, jwt_secret=self._SECRET
        )
        assert allowed is True

    def test_write_read_token_403(self, read_jwt):
        allowed, status, msg, _result = check_request(
            "POST", "/api/send", read_jwt, jwt_secret=self._SECRET
        )
        assert allowed is False
        assert status == 403
        assert "Forbidden" in msg

    def test_write_full_token_ok(self, full_jwt):
        allowed, status, msg, _result = check_request(
            "POST", "/api/send", full_jwt, jwt_secret=self._SECRET
        )
        assert allowed is True
        assert status == 200

    def test_approve_read_token_403(self, read_jwt):
        allowed, status, msg, _result = check_request(
            "POST", "/api/approve", read_jwt, jwt_secret=self._SECRET
        )
        assert allowed is False
        assert status == 403

    def test_proxy_write_read_token_403(self, read_jwt):
        """Read tokens cannot escalate to write ops via proxy routes."""
        allowed, status, msg, _result = check_request(
            "POST", "/node/node-a/api/send", read_jwt, jwt_secret=self._SECRET
        )
        assert allowed is False
        assert status == 403

    def test_proxy_write_trailing_slash_read_token_403(self, read_jwt):
        """Trailing slash must not bypass write-role check on proxy routes."""
        allowed, status, msg, _result = check_request(
            "POST", "/node/node-a/api/send/", read_jwt, jwt_secret=self._SECRET
        )
        assert allowed is False
        assert status == 403

    def test_direct_write_trailing_slash_read_token_403(self, read_jwt):
        """Trailing slash must not bypass write-role check on direct routes."""
        allowed, status, msg, _result = check_request(
            "POST", "/api/send/", read_jwt, jwt_secret=self._SECRET
        )
        assert allowed is False
        assert status == 403

    def test_proxy_write_full_token_ok(self, full_jwt):
        """Full tokens pass through proxy write routes."""
        allowed, status, msg, _result = check_request(
            "POST", "/node/node-a/api/send", full_jwt, jwt_secret=self._SECRET
        )
        assert allowed is True

    def test_proxy_v1_write_read_token_403(self, read_jwt):
        """Read tokens cannot escalate to write ops via v1 proxy routes."""
        allowed, status, msg, _result = check_request(
            "POST", "/node/node-a/v1/api/send", read_jwt, jwt_secret=self._SECRET
        )
        assert allowed is False
        assert status == 403

    def test_proxy_v1_write_full_token_ok(self, full_jwt):
        """Full tokens pass through v1 proxy write routes."""
        allowed, status, msg, _result = check_request(
            "POST", "/node/node-a/v1/api/send", full_jwt, jwt_secret=self._SECRET
        )
        assert allowed is True

    def test_proxy_v1_cluster_ws_new_read_403(self, read_jwt):
        """Read tokens cannot create workstreams via v1 proxy."""
        allowed, status, msg, _result = check_request(
            "POST",
            "/node/node-a/v1/api/cluster/workstreams/new",
            read_jwt,
            jwt_secret=self._SECRET,
        )
        assert allowed is False
        assert status == 403

    def test_proxy_read_endpoint_read_token_ok(self, read_jwt):
        """Read tokens can access proxy read endpoints."""
        allowed, status, msg, _result = check_request(
            "GET", "/node/node-a/api/workstreams", read_jwt, jwt_secret=self._SECRET
        )
        assert allowed is True

    def test_console_create_ws_read_token_403(self, read_jwt):
        """Read tokens cannot create workstreams."""
        allowed, status, msg, _result = check_request(
            "POST", "/api/cluster/workstreams/new", read_jwt, jwt_secret=self._SECRET
        )
        assert allowed is False
        assert status == 403

    def test_approve_full_token_ok(self, full_jwt):
        allowed, status, msg, _result = check_request(
            "POST", "/api/approve", full_jwt, jwt_secret=self._SECRET
        )
        assert allowed is True

    def test_no_auth_header_string(self):
        allowed, status, msg, _result = check_request("GET", "/api/dashboard", "")
        assert allowed is False
        assert status == 401


# ---------------------------------------------------------------------------
# TestCheckRequestWithCookie
# ---------------------------------------------------------------------------


class TestCheckRequestWithCookie:
    """Tests for cookie-based auth fallback in check_request."""

    _SECRET = "test-jwt-secret-minimum-32-chars!"

    @pytest.fixture()
    def read_jwt(self):
        return create_jwt("u1", frozenset({"read"}), "test", self._SECRET)

    @pytest.fixture()
    def full_jwt(self):
        return create_jwt("u1", frozenset({"read", "write", "approve"}), "test", self._SECRET)

    def test_cookie_fallback_when_no_bearer(self, read_jwt):
        allowed, status, _, _r = check_request(
            "GET",
            "/api/workstreams",
            None,
            cookie_header=f"turnstone_auth={read_jwt}",
            jwt_secret=self._SECRET,
        )
        assert allowed is True
        assert status == 200

    def test_bearer_takes_precedence_over_cookie(self, read_jwt, full_jwt):
        allowed, status, _, _r = check_request(
            "POST",
            "/api/send",
            f"Bearer {full_jwt}",
            cookie_header=f"turnstone_auth={read_jwt}",
            jwt_secret=self._SECRET,
        )
        assert allowed is True

    def test_invalid_cookie_401(self):
        allowed, status, _, _r = check_request(
            "GET",
            "/api/workstreams",
            None,
            cookie_header="turnstone_auth=wrong_token",
            jwt_secret=self._SECRET,
        )
        assert allowed is False
        assert status == 401

    def test_cookie_read_on_write_403(self, read_jwt):
        allowed, status, _, _r = check_request(
            "POST",
            "/api/send",
            None,
            cookie_header=f"turnstone_auth={read_jwt}",
            jwt_secret=self._SECRET,
        )
        assert allowed is False
        assert status == 403

    def test_cookie_full_on_write_ok(self, full_jwt):
        allowed, status, _, _r = check_request(
            "POST",
            "/api/send",
            None,
            cookie_header=f"turnstone_auth={full_jwt}",
            jwt_secret=self._SECRET,
        )
        assert allowed is True

    def test_no_cookie_no_bearer_401(self):
        allowed, status, _, _r = check_request(
            "GET",
            "/api/workstreams",
            None,
            cookie_header=None,
        )
        assert allowed is False
        assert status == 401

    def test_login_path_public(self):
        allowed, status, _, _r = check_request(
            "POST",
            "/api/auth/login",
            None,
        )
        assert allowed is True

    def test_logout_path_public(self):
        allowed, status, _, _r = check_request(
            "POST",
            "/api/auth/logout",
            None,
        )
        assert allowed is True


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
        mock_session.ws_id = "test-session-id"

        mock_ws = MagicMock()
        mock_ws.id = "test-ws"
        mock_ws.name = "test"
        mock_ws.state = WorkstreamState.IDLE
        mock_ws.session = mock_session
        mock_mgr = MagicMock()
        mock_mgr.list_all.return_value = [mock_ws]
        mock_mgr.max_workstreams = 10

        from turnstone.core.auth import JWT_AUD_SERVER

        cls._jwt_secret = "test-jwt-secret-minimum-32-chars!"
        cls._read_hdr = {
            "Authorization": f"Bearer {create_jwt('u1', frozenset({'read'}), 'test', cls._jwt_secret, audience=JWT_AUD_SERVER)}"
        }
        cls._full_hdr = {
            "Authorization": f"Bearer {create_jwt('u1', frozenset({'read', 'write', 'approve'}), 'test', cls._jwt_secret, audience=JWT_AUD_SERVER)}"
        }
        app = srv_mod.create_app(
            workstreams=mock_mgr,
            global_queue=queue.Queue(),
            global_listeners=[],
            global_listeners_lock=threading.Lock(),
            skip_permissions=False,
            jwt_secret=cls._jwt_secret,
            cors_origins=["*"],
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
        assert resp.status_code not in (401, 403)

    def test_root_no_token_200(self):
        resp = self.client.get("/")
        assert resp.status_code == 200

    def test_static_css_no_token_200(self):
        resp = self.client.get("/static/style.css")
        assert resp.status_code == 200

    def test_api_workstreams_no_token_401(self):
        resp = self.client.get("/v1/api/workstreams")
        assert resp.status_code == 401
        assert "Unauthorized" in resp.json().get("error", "")

    def test_api_workstreams_read_token_200(self):
        resp = self.client.get("/v1/api/workstreams", headers=self._read_hdr)
        assert resp.status_code == 200

    def test_api_workstreams_full_token_200(self):
        resp = self.client.get("/v1/api/workstreams", headers=self._full_hdr)
        assert resp.status_code == 200

    def test_api_send_read_token_403(self):
        resp = self.client.post(
            "/v1/api/send",
            headers=self._read_hdr,
            json={"message": "hello", "ws_id": "x"},
        )
        assert resp.status_code == 403
        assert "Forbidden" in resp.json().get("error", "")

    def test_api_send_full_token_passes_auth(self):
        resp = self.client.post(
            "/v1/api/send",
            headers=self._full_hdr,
            json={"message": "hello", "ws_id": "nonexistent"},
        )
        assert resp.status_code not in (401, 403)

    def test_api_send_no_token_401(self):
        resp = self.client.post(
            "/v1/api/send",
            json={"message": "hello", "ws_id": "x"},
        )
        assert resp.status_code == 401

    def test_invalid_token_401(self):
        resp = self.client.get(
            "/v1/api/workstreams",
            headers={"Authorization": "Bearer wrong_token"},
        )
        assert resp.status_code == 401

    def test_options_no_auth_required(self):
        resp = self.client.options(
            "/v1/api/send",
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
            "/v1/api/workstreams",
            headers={
                "Origin": "http://example.com",
                "Access-Control-Request-Method": "GET",
            },
        )
        allowed = resp.headers.get("access-control-allow-headers", "")
        assert "authorization" in allowed.lower()

    def test_shared_css_no_token_200(self):
        resp = self.client.get("/shared/base.css")
        assert resp.status_code == 200


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

        from turnstone.core.auth import JWT_AUD_CONSOLE

        cls._jwt_secret = "test-jwt-secret-minimum-32-chars!"
        cls._read_hdr = {
            "Authorization": f"Bearer {create_jwt('u1', frozenset({'read'}), 'test', cls._jwt_secret, audience=JWT_AUD_CONSOLE)}"
        }
        cls._full_hdr = {
            "Authorization": f"Bearer {create_jwt('u1', frozenset({'read', 'write', 'approve'}), 'test', cls._jwt_secret, audience=JWT_AUD_CONSOLE)}"
        }
        app = create_app(
            collector=mock_collector,
            jwt_secret=cls._jwt_secret,
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
        resp = self.test_client.get("/v1/api/cluster/overview")
        assert resp.status_code == 401

    def test_api_overview_read_token_200(self):
        resp = self.test_client.get("/v1/api/cluster/overview", headers=self._read_hdr)
        assert resp.status_code == 200

    def test_api_overview_full_token_200(self):
        resp = self.test_client.get("/v1/api/cluster/overview", headers=self._full_hdr)
        assert resp.status_code == 200

    def test_invalid_token_401(self):
        resp = self.test_client.get(
            "/v1/api/cluster/overview",
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
        mock_session.ws_id = "test-session-id"

        mock_ws = MagicMock()
        mock_ws.id = "test-ws"
        mock_ws.name = "test"
        mock_ws.state = WorkstreamState.IDLE
        mock_ws.session = mock_session
        mock_mgr = MagicMock()
        mock_mgr.list_all.return_value = [mock_ws]
        mock_mgr.max_workstreams = 10

        # Mock storage with a test user for password login
        from turnstone.core.auth import hash_password

        mock_storage = MagicMock()
        mock_storage.get_user_by_username.side_effect = lambda u: (
            {
                "user_id": "uid_test",
                "username": "testuser",
                "password_hash": hash_password("testpass"),
                "display_name": "Test",
            }
            if u == "testuser"
            else None
        )
        mock_storage.list_user_roles.return_value = [
            {"role_id": "builtin-admin", "scopes": "read,write,approve"}
        ]

        cls._jwt_secret = "test-jwt-secret-minimum-32-chars!"
        app = srv_mod.create_app(
            workstreams=mock_mgr,
            global_queue=queue.Queue(),
            global_listeners=[],
            global_listeners_lock=threading.Lock(),
            skip_permissions=False,
            jwt_secret=cls._jwt_secret,
            auth_storage=mock_storage,
        )
        cls.test_client = TestClient(app, raise_server_exceptions=False)

    @classmethod
    def teardown_class(cls):
        cls.test_client.close()

    def test_login_config_token_rejected(self):
        """Config token exchange is no longer allowed."""
        resp = self.test_client.post(
            "/v1/api/auth/login",
            json={"token": "tok_full"},
        )
        assert resp.status_code == 401

    def test_login_invalid_credentials_401(self):
        resp = self.test_client.post(
            "/v1/api/auth/login",
            json={"username": "testuser", "password": "wrong"},
        )
        assert resp.status_code == 401

    def test_login_password_ok(self):
        resp = self.test_client.post(
            "/v1/api/auth/login",
            json={"username": "testuser", "password": "testpass"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "jwt" in data

    def test_cookie_auth_on_api(self):
        # Login to get cookie (TestClient tracks cookies automatically)
        login_resp = self.test_client.post(
            "/v1/api/auth/login",
            json={"username": "testuser", "password": "testpass"},
        )
        assert login_resp.status_code == 200

        # Use cookie to access API — TestClient forwards cookies
        resp = self.test_client.get("/v1/api/workstreams")
        assert resp.status_code == 200

    def test_logout_clears_cookie(self):
        self.test_client.post(
            "/v1/api/auth/login",
            json={"username": "testuser", "password": "testpass"},
        )

        # Logout
        logout_resp = self.test_client.post("/v1/api/auth/logout")
        assert logout_resp.status_code == 200
        cookie = logout_resp.headers.get("set-cookie", "")
        assert "Max-Age=0" in cookie

        # API should now fail (cookie cleared)
        resp = self.test_client.get("/v1/api/workstreams")
        assert resp.status_code == 401


class TestConsoleLogin:
    """Test login/logout cookie flow on turnstone-console."""

    @classmethod
    def setup_class(cls):
        from unittest.mock import MagicMock

        from starlette.testclient import TestClient

        from turnstone.console.collector import ClusterCollector
        from turnstone.console.server import _load_static, create_app
        from turnstone.core.auth import hash_password

        _load_static()

        mock_collector = MagicMock(spec=ClusterCollector)
        mock_collector.get_overview.return_value = {
            "nodes": 1,
            "workstreams": 2,
            "states": {"running": 1, "idle": 1},
            "aggregate": {"total_tokens": 100},
        }

        mock_storage = MagicMock()
        mock_storage.get_user_by_username.side_effect = lambda u: (
            {
                "user_id": "uid_test",
                "username": "testuser",
                "password_hash": hash_password("testpass"),
                "display_name": "Test",
            }
            if u == "testuser"
            else None
        )
        mock_storage.list_user_roles.return_value = [
            {"role_id": "builtin-admin", "scopes": "read,write,approve"}
        ]

        cls._jwt_secret = "test-jwt-secret-minimum-32-chars!"
        app = create_app(
            collector=mock_collector,
            jwt_secret=cls._jwt_secret,
            auth_storage=mock_storage,
        )
        cls.test_client = TestClient(app, raise_server_exceptions=False)

    @classmethod
    def teardown_class(cls):
        cls.test_client.close()

    def test_login_config_token_rejected(self):
        resp = self.test_client.post(
            "/v1/api/auth/login",
            json={"token": "tok_read"},
        )
        assert resp.status_code == 401

    def test_login_password_ok(self):
        resp = self.test_client.post(
            "/v1/api/auth/login",
            json={"username": "testuser", "password": "testpass"},
        )
        assert resp.status_code == 200
        assert "turnstone_auth" in resp.headers.get("set-cookie", "")

    def test_cookie_auth_on_api(self):
        self.test_client.post(
            "/v1/api/auth/login",
            json={"username": "testuser", "password": "testpass"},
        )
        resp = self.test_client.get("/v1/api/cluster/overview")
        assert resp.status_code == 200

    def test_logout_then_api_fails(self):
        self.test_client.post(
            "/v1/api/auth/login",
            json={"username": "testuser", "password": "testpass"},
        )
        self.test_client.post("/v1/api/auth/logout")
        resp = self.test_client.get("/v1/api/cluster/overview")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Security hardening tests
# ---------------------------------------------------------------------------


class TestLoginRateLimiter:
    def test_allows_under_limit(self):
        from turnstone.core.auth import LoginRateLimiter

        limiter = LoginRateLimiter(max_attempts=3, window_seconds=60)
        for _ in range(3):
            ok, _ = limiter.check("ip:1.2.3.4")
            assert ok
            limiter.record("ip:1.2.3.4")
        # 4th should be blocked (3 recorded)
        ok, retry = limiter.check("ip:1.2.3.4")
        assert not ok
        assert retry > 0

    def test_different_keys_independent(self):
        from turnstone.core.auth import LoginRateLimiter

        limiter = LoginRateLimiter(max_attempts=2, window_seconds=60)
        limiter.record("ip:a")
        limiter.record("ip:a")
        ok_a, _ = limiter.check("ip:a")
        ok_b, _ = limiter.check("ip:b")
        assert not ok_a
        assert ok_b

    def test_cleanup(self):
        from turnstone.core.auth import LoginRateLimiter

        limiter = LoginRateLimiter(max_attempts=1, window_seconds=60)
        limiter.record("ip:old")
        removed = limiter.cleanup(max_age=0.0)
        assert removed == 1
        ok, _ = limiter.check("ip:old")
        assert ok

    def test_max_keys_protection(self):
        from turnstone.core.auth import LoginRateLimiter

        limiter = LoginRateLimiter(max_attempts=5, window_seconds=60)
        limiter.MAX_KEYS = 2
        limiter.record("a")
        limiter.record("b")
        limiter.record("c")  # should be silently dropped (at capacity)
        assert "c" not in limiter._attempts


class TestJWTAudienceIssuer:
    SECRET = "test-secret-that-is-at-least-32-chars"

    def test_create_jwt_includes_iss(self):
        import jwt as pyjwt

        from turnstone.core.auth import JWT_ISSUER, create_jwt

        token = create_jwt("user1", frozenset({"read"}), "test", self.SECRET)
        payload = pyjwt.decode(
            token, self.SECRET, algorithms=["HS256"], options={"verify_aud": False}
        )
        assert payload["iss"] == JWT_ISSUER

    def test_create_jwt_with_audience(self):
        import jwt as pyjwt

        from turnstone.core.auth import JWT_AUD_SERVER, create_jwt

        token = create_jwt(
            "user1", frozenset({"read"}), "test", self.SECRET, audience=JWT_AUD_SERVER
        )
        payload = pyjwt.decode(token, self.SECRET, algorithms=["HS256"], audience=JWT_AUD_SERVER)
        assert payload["aud"] == JWT_AUD_SERVER

    def test_validate_jwt_wrong_audience_rejected(self):
        from turnstone.core.auth import JWT_AUD_CONSOLE, JWT_AUD_SERVER, create_jwt, validate_jwt

        token = create_jwt(
            "user1", frozenset({"read"}), "test", self.SECRET, audience=JWT_AUD_SERVER
        )
        result = validate_jwt(token, self.SECRET, audience=JWT_AUD_CONSOLE)
        assert result is None

    def test_validate_jwt_correct_audience_accepted(self):
        from turnstone.core.auth import JWT_AUD_SERVER, create_jwt, validate_jwt

        token = create_jwt(
            "user1", frozenset({"read"}), "test", self.SECRET, audience=JWT_AUD_SERVER
        )
        result = validate_jwt(token, self.SECRET, audience=JWT_AUD_SERVER)
        assert result is not None
        assert result.user_id == "user1"

    def test_validate_jwt_no_audience_backward_compat(self):
        from turnstone.core.auth import create_jwt, validate_jwt

        # Token without aud claim should be accepted when audience="" (backward compat)
        token = create_jwt("user1", frozenset({"read"}), "test", self.SECRET)
        result = validate_jwt(token, self.SECRET, audience="")
        assert result is not None

    def test_create_jwt_expiry_seconds(self):
        import jwt as pyjwt

        from turnstone.core.auth import create_jwt

        token = create_jwt("user1", frozenset({"read"}), "test", self.SECRET, expiry_seconds=300)
        payload = pyjwt.decode(
            token, self.SECRET, algorithms=["HS256"], options={"verify_aud": False}
        )
        assert payload["exp"] - payload["iat"] == 300

    def test_create_jwt_expiry_seconds_overrides_hours(self):
        import jwt as pyjwt

        from turnstone.core.auth import create_jwt

        token = create_jwt(
            "user1",
            frozenset({"read"}),
            "test",
            self.SECRET,
            expiry_hours=24,
            expiry_seconds=60,
        )
        payload = pyjwt.decode(
            token, self.SECRET, algorithms=["HS256"], options={"verify_aud": False}
        )
        # expiry_seconds takes precedence over expiry_hours
        assert payload["exp"] - payload["iat"] == 60

    def test_create_jwt_expiry_seconds_rejects_zero(self):
        import pytest

        from turnstone.core.auth import create_jwt

        with pytest.raises(ValueError, match="expiry_seconds must be positive"):
            create_jwt("user1", frozenset({"read"}), "test", self.SECRET, expiry_seconds=0)

    def test_create_jwt_expiry_seconds_rejects_negative(self):
        import pytest

        from turnstone.core.auth import create_jwt

        with pytest.raises(ValueError, match="expiry_seconds must be positive"):
            create_jwt("user1", frozenset({"read"}), "test", self.SECRET, expiry_seconds=-1)


class TestJWTVersionClaim:
    SECRET = "test-secret-that-is-at-least-32-chars"

    def test_create_jwt_with_version(self):
        import jwt as pyjwt

        from turnstone.core.auth import create_jwt

        token = create_jwt("user1", frozenset({"read"}), "test", self.SECRET, version="1.2")
        payload = pyjwt.decode(
            token, self.SECRET, algorithms=["HS256"], options={"verify_aud": False}
        )
        assert payload["ver"] == "1.2"

    def test_create_jwt_without_version(self):
        import jwt as pyjwt

        from turnstone.core.auth import create_jwt

        token = create_jwt("user1", frozenset({"read"}), "test", self.SECRET)
        payload = pyjwt.decode(
            token, self.SECRET, algorithms=["HS256"], options={"verify_aud": False}
        )
        assert "ver" not in payload

    def test_validate_jwt_carries_token_version(self):
        from turnstone.core.auth import create_jwt, validate_jwt

        token = create_jwt("user1", frozenset({"read"}), "test", self.SECRET, version="1.2")
        result = validate_jwt(token, self.SECRET)
        assert result is not None
        assert result.user_id == "user1"
        assert result.token_version == "1.2"

    def test_validate_jwt_no_ver_returns_empty_token_version(self):
        from turnstone.core.auth import create_jwt, validate_jwt

        token = create_jwt("user1", frozenset({"read"}), "test", self.SECRET)
        result = validate_jwt(token, self.SECRET)
        assert result is not None
        assert result.token_version == ""

    def test_check_request_accepts_matching_version(self):
        from turnstone.core.auth import JWT_AUD_SERVER, check_request, create_jwt

        token = create_jwt(
            "user1",
            frozenset({"read"}),
            "test",
            self.SECRET,
            audience=JWT_AUD_SERVER,
            version="1.2",
        )
        allowed, _status, _msg, result = check_request(
            "GET",
            "/v1/api/workstreams",
            f"Bearer {token}",
            jwt_secret=self.SECRET,
            jwt_audience=JWT_AUD_SERVER,
            jwt_version="1.2",
        )
        assert allowed
        assert result is not None

    def test_check_request_accepts_no_ver_backward_compat(self):
        from turnstone.core.auth import JWT_AUD_SERVER, check_request, create_jwt

        # Token without ver claim should be accepted (backward compat)
        token = create_jwt(
            "user1",
            frozenset({"read"}),
            "test",
            self.SECRET,
            audience=JWT_AUD_SERVER,
        )
        allowed, _status, _msg, _result = check_request(
            "GET",
            "/v1/api/workstreams",
            f"Bearer {token}",
            jwt_secret=self.SECRET,
            jwt_audience=JWT_AUD_SERVER,
            jwt_version="1.2",
        )
        assert allowed

    def test_check_request_rejects_old_version_jwt(self):
        from turnstone.core.auth import JWT_AUD_SERVER, check_request, create_jwt

        token = create_jwt(
            "user1",
            frozenset({"read"}),
            "test",
            self.SECRET,
            audience=JWT_AUD_SERVER,
            version="1.1",
        )
        allowed, status, msg, _result = check_request(
            "GET",
            "/v1/api/workstreams",
            f"Bearer {token}",
            jwt_secret=self.SECRET,
            jwt_audience=JWT_AUD_SERVER,
            jwt_version="1.2",
        )
        assert not allowed
        assert status == 401
        assert msg == "version_mismatch"


class TestVersionSlot:
    def test_returns_major_minor(self):
        from turnstone.core.auth import jwt_version_slot

        slot = jwt_version_slot()
        parts = slot.split(".")
        assert len(parts) == 2

    def test_strips_patch_and_prerelease(self):
        from unittest.mock import patch

        with patch("turnstone.__version__", "2.3.1a5"):
            from turnstone.core.auth import jwt_version_slot

            assert jwt_version_slot() == "2.3"


class TestServiceTokenManager:
    SECRET = "test-secret-that-is-at-least-32-chars"

    def test_auto_mints_on_first_access(self):
        from turnstone.core.auth import ServiceTokenManager

        mgr = ServiceTokenManager(
            user_id="svc",
            scopes=frozenset({"read"}),
            source="test",
            secret=self.SECRET,
        )
        token = mgr.token
        assert token  # non-empty
        assert isinstance(token, str)

    def test_bearer_header_format(self):
        from turnstone.core.auth import ServiceTokenManager

        mgr = ServiceTokenManager(
            user_id="svc",
            scopes=frozenset({"read"}),
            source="test",
            secret=self.SECRET,
        )
        header = mgr.bearer_header
        assert "Authorization" in header
        assert header["Authorization"].startswith("Bearer ")

    def test_token_stable_within_window(self):
        from turnstone.core.auth import ServiceTokenManager

        mgr = ServiceTokenManager(
            user_id="svc",
            scopes=frozenset({"read"}),
            source="test",
            secret=self.SECRET,
            expiry_hours=1,
        )
        t1 = mgr.token
        t2 = mgr.token
        assert t1 == t2

    def test_token_rotates_near_expiry(self):
        from turnstone.core.auth import ServiceTokenManager

        mgr = ServiceTokenManager(
            user_id="svc",
            scopes=frozenset({"read"}),
            source="test",
            secret=self.SECRET,
            expiry_hours=1,
        )
        _ = mgr.token  # initial mint
        # Simulate expiry by backdating _expires_at
        mgr._expires_at = 0.0
        t2 = mgr.token
        # Token was re-minted (even if payload matches within same second,
        # the internal state was refreshed)
        assert t2  # non-empty, valid token
        assert mgr._expires_at > 0.0  # was refreshed

    def test_audience_included(self):
        import jwt as pyjwt

        from turnstone.core.auth import JWT_AUD_SERVER, ServiceTokenManager

        mgr = ServiceTokenManager(
            user_id="svc",
            scopes=frozenset({"read"}),
            source="test",
            secret=self.SECRET,
            audience=JWT_AUD_SERVER,
        )
        payload = pyjwt.decode(
            mgr.token, self.SECRET, algorithms=["HS256"], audience=JWT_AUD_SERVER
        )
        assert payload["aud"] == JWT_AUD_SERVER

    def test_service_token_no_version_claim(self):
        import jwt as pyjwt

        from turnstone.core.auth import ServiceTokenManager

        mgr = ServiceTokenManager(
            user_id="svc",
            scopes=frozenset({"read"}),
            source="test",
            secret=self.SECRET,
        )
        payload = pyjwt.decode(
            mgr.token, self.SECRET, algorithms=["HS256"], options={"verify_aud": False}
        )
        assert "ver" not in payload


class TestIsSecureRequest:
    def test_https_scheme(self):
        from turnstone.core.auth import is_secure_request

        assert is_secure_request({}, scheme="https") is True

    def test_http_scheme(self):
        from turnstone.core.auth import is_secure_request

        assert is_secure_request({}, scheme="http") is False

    def test_x_forwarded_proto_https(self):
        from turnstone.core.auth import is_secure_request

        assert is_secure_request({"x-forwarded-proto": "https"}, scheme="http") is True

    def test_x_forwarded_proto_http(self):
        from turnstone.core.auth import is_secure_request

        assert is_secure_request({"x-forwarded-proto": "http"}, scheme="http") is False


class TestSecretStrength:
    def test_short_secret_exits(self):
        old = os.environ.get("TURNSTONE_JWT_SECRET", "")
        os.environ["TURNSTONE_JWT_SECRET"] = "short"
        try:
            with pytest.raises(SystemExit):
                load_jwt_secret()
        finally:
            if old:
                os.environ["TURNSTONE_JWT_SECRET"] = old
            else:
                os.environ.pop("TURNSTONE_JWT_SECRET", None)

    def test_missing_secret_exits(self):
        with (
            patch("turnstone.core.config.load_config", return_value={}),
            patch.dict(os.environ, {}, clear=True),
            pytest.raises(SystemExit),
        ):
            load_jwt_secret()


class TestCorsConfigurable:
    """Verify CORS middleware is only added when origins are configured."""

    def test_no_cors_origins_no_cors_headers(self):
        """Without cors_origins, no Access-Control headers."""
        from starlette.testclient import TestClient

        import turnstone.server as srv_mod

        mgr = MagicMock()
        mgr.list_all.return_value = []
        mgr.max_workstreams = 10
        app = srv_mod.create_app(
            workstreams=mgr,
            global_queue=queue.Queue(),
            global_listeners=[],
            global_listeners_lock=threading.Lock(),
            skip_permissions=False,
        )
        client = TestClient(app)
        resp = client.get("/health", headers={"Origin": "http://evil.com"})
        assert "Access-Control-Allow-Origin" not in resp.headers
        client.close()

    def test_cors_origins_set(self):
        """With cors_origins, CORS headers are present."""
        from starlette.testclient import TestClient

        import turnstone.server as srv_mod

        mgr = MagicMock()
        mgr.list_all.return_value = []
        mgr.max_workstreams = 10
        app = srv_mod.create_app(
            workstreams=mgr,
            global_queue=queue.Queue(),
            global_listeners=[],
            global_listeners_lock=threading.Lock(),
            skip_permissions=False,
            cors_origins=["http://example.com"],
        )
        client = TestClient(app)
        resp = client.get(
            "/health",
            headers={"Origin": "http://example.com"},
        )
        assert resp.headers.get("Access-Control-Allow-Origin") == "http://example.com"
        client.close()


# ---------------------------------------------------------------------------
# TestVerifyPassword — OIDC sentinel handling
# ---------------------------------------------------------------------------


class TestVerifyPassword:
    def test_valid_bcrypt_hash(self):
        from turnstone.core.auth import hash_password, verify_password

        hashed = hash_password("mypassword")
        assert verify_password("mypassword", hashed) is True
        assert verify_password("wrongpassword", hashed) is False

    def test_oidc_sentinel_rejected(self):
        from turnstone.core.auth import verify_password

        # OIDC sentinel must return False, not crash with ValueError
        assert verify_password("anypassword", "!oidc") is False

    def test_non_bcrypt_hash_rejected(self):
        from turnstone.core.auth import verify_password

        assert verify_password("password", "not_a_hash") is False
        assert verify_password("password", "") is False

    def test_empty_password_against_oidc_sentinel(self):
        from turnstone.core.auth import verify_password

        assert verify_password("", "!oidc") is False


# ---------------------------------------------------------------------------
# TestOIDCPublicPaths — OIDC endpoints are public
# ---------------------------------------------------------------------------


class TestOIDCPublicPaths:
    def test_oidc_authorize_is_public(self):
        assert is_public_path("/api/auth/oidc/authorize") is True
        assert is_public_path("/v1/api/auth/oidc/authorize") is True

    def test_oidc_callback_is_public(self):
        assert is_public_path("/api/auth/oidc/callback") is True
        assert is_public_path("/v1/api/auth/oidc/callback") is True


# ---------------------------------------------------------------------------
# TestRequirePermissionServiceScope — service scope bypasses permission checks
# ---------------------------------------------------------------------------


class TestRequirePermissionServiceScope:
    """Verify require_permission() behaviour with the service scope."""

    def _make_request(self, auth_result):
        """Build a mock Starlette request with the given AuthResult on state."""
        request = MagicMock()
        request.state.auth_result = auth_result
        return request

    def test_service_scope_bypasses_permission(self):
        """Service-scoped tokens bypass all permission checks (returns None)."""
        from turnstone.core.auth import AuthResult, require_permission

        auth = AuthResult(
            user_id="svc-agent",
            scopes=frozenset({"service"}),
            token_source="jwt",
        )
        request = self._make_request(auth)
        result = require_permission(request, "admin.users")
        assert result is None  # bypass — no 403

    def test_without_service_scope_and_without_permission_returns_403(self):
        """Non-service tokens without the required permission get 403."""
        from turnstone.core.auth import AuthResult, require_permission

        auth = AuthResult(
            user_id="regular-user",
            scopes=frozenset({"read", "write"}),
            token_source="jwt",
        )
        request = self._make_request(auth)
        result = require_permission(request, "admin.users")
        assert result is not None
        assert result.status_code == 403

    def test_without_service_scope_with_permission_returns_none(self):
        """Non-service tokens with the required permission pass."""
        from turnstone.core.auth import AuthResult, require_permission

        auth = AuthResult(
            user_id="admin-user",
            scopes=frozenset({"read", "write", "approve"}),
            token_source="jwt",
            permissions=frozenset({"admin.users"}),
        )
        request = self._make_request(auth)
        result = require_permission(request, "admin.users")
        assert result is None  # granted — no 403

    def test_no_auth_result_returns_401(self):
        """Missing auth_result on request state returns 401."""
        from turnstone.core.auth import require_permission

        request = MagicMock()
        del request.state.auth_result  # ensure attribute is absent
        result = require_permission(request, "admin.users")
        assert result is not None
        assert result.status_code == 401
