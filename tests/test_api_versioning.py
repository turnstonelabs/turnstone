"""Integration tests for API versioning and OpenAPI/docs endpoints."""

import queue
import threading
from unittest.mock import MagicMock

import pytest

# Shared test auth — JWT-based
_TEST_JWT_SECRET = "test-jwt-secret-minimum-32-chars!"


def _server_jwt() -> str:
    from turnstone.core.auth import JWT_AUD_SERVER, create_jwt

    return create_jwt(
        user_id="test-versioning",
        scopes=frozenset({"read", "write", "approve", "service"}),
        source="test",
        secret=_TEST_JWT_SECRET,
        audience=JWT_AUD_SERVER,
    )


def _console_jwt() -> str:
    from turnstone.core.auth import JWT_AUD_CONSOLE, create_jwt

    return create_jwt(
        user_id="test-versioning",
        scopes=frozenset({"read", "write", "approve", "service"}),
        source="test",
        secret=_TEST_JWT_SECRET,
        audience=JWT_AUD_CONSOLE,
    )


_SERVER_AUTH_HEADERS = {"Authorization": f"Bearer {_server_jwt()}"}
_CONSOLE_AUTH_HEADERS = {"Authorization": f"Bearer {_console_jwt()}"}


class TestServerVersioning:
    """Test /v1/ routes and OpenAPI endpoints on the server."""

    @pytest.fixture()
    def client(self):
        from starlette.testclient import TestClient

        from turnstone.server import create_app

        mock_mgr = MagicMock()
        mock_mgr.list_all.return_value = []
        mock_mgr.max_active = 10
        app = create_app(
            workstreams=mock_mgr,
            global_queue=queue.Queue(),
            global_listeners=[],
            global_listeners_lock=threading.Lock(),
            skip_permissions=False,
            jwt_secret=_TEST_JWT_SECRET,
        )
        client = TestClient(app, raise_server_exceptions=False)
        yield client
        client.close()

    def test_v1_workstreams(self, client):
        resp = client.get("/v1/api/workstreams", headers=_SERVER_AUTH_HEADERS)
        assert resp.status_code == 200
        assert "workstreams" in resp.json()

    def test_unversioned_api_404(self, client):
        resp = client.get("/api/workstreams", headers=_SERVER_AUTH_HEADERS)
        assert resp.status_code == 404

    def test_openapi_json(self, client):
        resp = client.get("/openapi.json")
        assert resp.status_code == 200
        spec = resp.json()
        assert spec["openapi"] == "3.1.0"
        assert "/v1/api/send" in spec["paths"]

    def test_docs_page(self, client):
        resp = client.get("/docs")
        assert resp.status_code == 200
        assert "swagger-ui" in resp.text.lower()

    def test_health_unversioned(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert "status" in resp.json()

    def test_shared_static_unversioned(self, client):
        resp = client.get("/shared/base.css")
        assert resp.status_code == 200


class TestConsoleVersioning:
    """Test /v1/ routes and OpenAPI endpoints on the console."""

    @pytest.fixture()
    def client(self):
        from starlette.testclient import TestClient

        from turnstone.console.collector import ClusterCollector
        from turnstone.console.server import _load_static, create_app

        _load_static()
        collector = MagicMock(spec=ClusterCollector)
        collector.get_overview.return_value = {
            "nodes": 0,
            "workstreams": 0,
            "states": {},
            "aggregate": {},
        }
        app = create_app(
            collector=collector,
            jwt_secret=_TEST_JWT_SECRET,
        )
        client = TestClient(app, raise_server_exceptions=False)
        yield client
        client.close()

    def test_v1_cluster_overview(self, client):
        resp = client.get("/v1/api/cluster/overview", headers=_CONSOLE_AUTH_HEADERS)
        assert resp.status_code == 200

    def test_unversioned_api_404(self, client):
        resp = client.get("/api/cluster/overview", headers=_CONSOLE_AUTH_HEADERS)
        assert resp.status_code == 404

    def test_openapi_json(self, client):
        resp = client.get("/openapi.json")
        assert resp.status_code == 200
        spec = resp.json()
        assert spec["openapi"] == "3.1.0"
        assert "/v1/api/cluster/overview" in spec["paths"]

    def test_docs_page(self, client):
        resp = client.get("/docs")
        assert resp.status_code == 200
        assert "swagger-ui" in resp.text.lower()

    def test_health_unversioned(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200

    def test_console_app_js_uses_v1_paths(self, client):
        resp = client.get("/static/app.js")
        body = resp.text
        assert "/v1/api/cluster" in body
