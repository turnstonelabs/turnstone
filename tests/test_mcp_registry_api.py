"""Tests for MCP Registry admin API endpoints."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

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
    _get_registry_url,
    admin_registry_install,
    admin_registry_search,
)
from turnstone.core.auth import AuthResult
from turnstone.core.mcp_registry import (
    DEFAULT_REGISTRY_URL,
    MCPRegistryError,
    RegistryPackage,
    RegistryRemote,
    RegistryRemoteHeader,
    RegistrySearchResult,
    RegistryServer,
    RegistryServerMeta,
)
from turnstone.core.storage._sqlite import SQLiteBackend

# ---------------------------------------------------------------------------
# Auth middleware
# ---------------------------------------------------------------------------


class _InjectAuthMiddleware(BaseHTTPMiddleware):
    """Inject an admin auth result with admin.mcp permission."""

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        request.state.auth_result = AuthResult(
            user_id="test-user",
            scopes=frozenset({"approve"}),
            token_source="config",
            permissions=frozenset({"read", "write", "approve", "admin.mcp"}),
        )
        return await call_next(request)


class _InjectAuthNoMcpMiddleware(BaseHTTPMiddleware):
    """Inject an auth result WITHOUT admin.mcp permission."""

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        request.state.auth_result = AuthResult(
            user_id="test-user",
            scopes=frozenset({"approve"}),
            token_source="jwt",
            permissions=frozenset({"read", "write", "approve"}),
        )
        return await call_next(request)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_ROUTES = [
    Mount(
        "/v1",
        routes=[
            Route("/api/admin/mcp-registry/search", admin_registry_search),
            Route(
                "/api/admin/mcp-registry/install",
                admin_registry_install,
                methods=["POST"],
            ),
        ],
    ),
]


@pytest.fixture
def storage(tmp_path):
    return SQLiteBackend(str(tmp_path / "test.db"))


@pytest.fixture
def client(storage):
    app = Starlette(
        routes=_ROUTES,
        middleware=[Middleware(_InjectAuthMiddleware)],
    )
    app.state.auth_storage = storage
    return TestClient(app)


@pytest.fixture
def client_no_perm(storage):
    app = Starlette(
        routes=_ROUTES,
        middleware=[Middleware(_InjectAuthNoMcpMiddleware)],
    )
    app.state.auth_storage = storage
    return TestClient(app)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_search_result(
    servers: list[RegistryServer] | None = None,
    next_cursor: str | None = None,
) -> RegistrySearchResult:
    return RegistrySearchResult(
        servers=servers or [],
        total_count=len(servers or []),
        next_cursor=next_cursor,
    )


def _sample_remote_server(
    name: str = "io.example/test-server",
    version: str = "1.0.0",
) -> RegistryServer:
    return RegistryServer(
        name=name,
        description="A test server",
        title="Test Server",
        version=version,
        remotes=[
            RegistryRemote(
                type="streamable-http",
                url="https://api.example.com/mcp",
                headers=[
                    RegistryRemoteHeader(
                        name="Authorization",
                        description="Bearer token",
                        is_required=True,
                        is_secret=True,
                    )
                ],
            )
        ],
        meta=RegistryServerMeta(status="active", is_latest=True),
    )


def _sample_package_server(
    name: str = "io.example/npm-server",
    version: str = "2.0.0",
) -> RegistryServer:
    return RegistryServer(
        name=name,
        description="An npm package server",
        version=version,
        packages=[
            RegistryPackage(
                registry_type="npm",
                identifier="@example/mcp-server",
                version="2.0.0",
            )
        ],
        meta=RegistryServerMeta(status="active", is_latest=True),
    )


# ---------------------------------------------------------------------------
# Search endpoint tests
# ---------------------------------------------------------------------------


class TestRegistrySearch:
    def test_search_basic(self, client: TestClient) -> None:
        srv = _sample_remote_server()
        mock_result = _mock_search_result([srv])

        with patch("turnstone.core.mcp_registry.MCPRegistryClient") as mock_client:
            instance = AsyncMock()
            instance.search.return_value = mock_result
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            mock_client.return_value = instance

            resp = client.get("/v1/api/admin/mcp-registry/search?search=test")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data["servers"]) == 1
        assert data["servers"][0]["name"] == "io.example/test-server"
        assert data["servers"][0]["installed"] is False

    def test_search_with_installed_server(self, client: TestClient, storage: SQLiteBackend) -> None:
        """Servers already installed should be flagged."""
        import uuid

        storage.create_mcp_server(
            server_id=uuid.uuid4().hex,
            name="test-server",
            transport="streamable-http",
            url="https://api.example.com/mcp",
            registry_name="io.example/test-server",
            registry_version="0.9.0",
        )

        srv = _sample_remote_server(version="1.0.0")
        mock_result = _mock_search_result([srv])

        with patch("turnstone.core.mcp_registry.MCPRegistryClient") as mock_client:
            instance = AsyncMock()
            instance.search.return_value = mock_result
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            mock_client.return_value = instance

            resp = client.get("/v1/api/admin/mcp-registry/search?search=test")

        data = resp.json()
        s = data["servers"][0]
        assert s["installed"] is True
        assert s["installed_version"] == "0.9.0"
        assert s["update_available"] is True

    def test_search_permission_denied(self, client_no_perm: TestClient) -> None:
        resp = client_no_perm.get("/v1/api/admin/mcp-registry/search")
        assert resp.status_code == 403

    def test_search_registry_error(self, client: TestClient) -> None:
        with patch("turnstone.core.mcp_registry.MCPRegistryClient") as mock_client:
            instance = AsyncMock()
            instance.search.side_effect = MCPRegistryError("Connection failed")
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            mock_client.return_value = instance

            resp = client.get("/v1/api/admin/mcp-registry/search?search=test")

        assert resp.status_code == 502
        assert "Registry error" in resp.json()["error"]

    def test_search_pagination(self, client: TestClient) -> None:
        mock_result = _mock_search_result([], next_cursor="cursor123")

        with patch("turnstone.core.mcp_registry.MCPRegistryClient") as mock_client:
            instance = AsyncMock()
            instance.search.return_value = mock_result
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            mock_client.return_value = instance

            resp = client.get("/v1/api/admin/mcp-registry/search?search=test&limit=5&cursor=prev")

        assert resp.status_code == 200
        assert resp.json()["next_cursor"] == "cursor123"
        instance.search.assert_called_once_with(q="test", limit=5, cursor="prev")


# ---------------------------------------------------------------------------
# Install endpoint tests
# ---------------------------------------------------------------------------


class TestRegistryInstall:
    def test_install_remote_server(self, client: TestClient, storage: SQLiteBackend) -> None:
        srv = _sample_remote_server()
        mock_result = _mock_search_result([srv])

        with (
            patch("turnstone.core.mcp_registry.MCPRegistryClient") as mock_client,
            patch(
                "turnstone.console.server._notify_nodes_mcp_reload",
                new_callable=AsyncMock,
                return_value={},
            ),
        ):
            instance = AsyncMock()
            instance.search.return_value = mock_result
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            mock_client.return_value = instance

            resp = client.post(
                "/v1/api/admin/mcp-registry/install",
                json={
                    "registry_name": "io.example/test-server",
                    "source": "remote",
                    "headers": {"Authorization": "Bearer sk-123"},
                },
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["transport"] == "streamable-http"
        assert data["url"] == "https://api.example.com/mcp"
        assert data["registry_name"] == "io.example/test-server"
        assert data["registry_version"] == "1.0.0"

        # Verify in storage
        s = storage.get_mcp_server_by_registry_name("io.example/test-server")
        assert s is not None
        assert s["transport"] == "streamable-http"

    def test_install_package_server(self, client: TestClient, storage: SQLiteBackend) -> None:
        srv = _sample_package_server()
        mock_result = _mock_search_result([srv])

        with (
            patch("turnstone.core.mcp_registry.MCPRegistryClient") as mock_client,
            patch(
                "turnstone.console.server._notify_nodes_mcp_reload",
                new_callable=AsyncMock,
                return_value={},
            ),
        ):
            instance = AsyncMock()
            instance.search.return_value = mock_result
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            mock_client.return_value = instance

            resp = client.post(
                "/v1/api/admin/mcp-registry/install",
                json={
                    "registry_name": "io.example/npm-server",
                    "source": "package",
                },
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["transport"] == "stdio"
        assert data["command"] == "npx"

    def test_install_duplicate_registry_name(
        self, client: TestClient, storage: SQLiteBackend
    ) -> None:
        import uuid

        storage.create_mcp_server(
            server_id=uuid.uuid4().hex,
            name="existing-server",
            transport="streamable-http",
            url="https://example.com",
            registry_name="io.example/test-server",
        )

        resp = client.post(
            "/v1/api/admin/mcp-registry/install",
            json={
                "registry_name": "io.example/test-server",
                "source": "remote",
            },
        )
        assert resp.status_code == 409
        assert "already installed" in resp.json()["error"]

    def test_install_max_servers(self, client: TestClient, storage: SQLiteBackend) -> None:
        import uuid

        for i in range(200):
            storage.create_mcp_server(
                server_id=uuid.uuid4().hex,
                name=f"server-{i}",
                transport="stdio",
                command="echo",
            )

        resp = client.post(
            "/v1/api/admin/mcp-registry/install",
            json={
                "registry_name": "io.example/new-server",
                "source": "remote",
            },
        )
        assert resp.status_code == 400
        assert "Maximum" in resp.json()["error"]

    def test_install_missing_registry_name(self, client: TestClient) -> None:
        resp = client.post(
            "/v1/api/admin/mcp-registry/install",
            json={"source": "remote"},
        )
        assert resp.status_code == 400

    def test_install_invalid_source(self, client: TestClient) -> None:
        resp = client.post(
            "/v1/api/admin/mcp-registry/install",
            json={"registry_name": "io.example/test", "source": "invalid"},
        )
        assert resp.status_code == 400

    def test_install_not_found_in_registry(self, client: TestClient) -> None:
        mock_result = _mock_search_result([])

        with patch("turnstone.core.mcp_registry.MCPRegistryClient") as mock_client:
            instance = AsyncMock()
            instance.search.return_value = mock_result
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            mock_client.return_value = instance

            resp = client.post(
                "/v1/api/admin/mcp-registry/install",
                json={
                    "registry_name": "io.example/nonexistent",
                    "source": "remote",
                },
            )

        assert resp.status_code == 404
        assert "not found" in resp.json()["error"]

    def test_install_custom_name(self, client: TestClient, storage: SQLiteBackend) -> None:
        srv = _sample_remote_server()
        mock_result = _mock_search_result([srv])

        with (
            patch("turnstone.core.mcp_registry.MCPRegistryClient") as mock_client,
            patch(
                "turnstone.console.server._notify_nodes_mcp_reload",
                new_callable=AsyncMock,
                return_value={},
            ),
        ):
            instance = AsyncMock()
            instance.search.return_value = mock_result
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            mock_client.return_value = instance

            resp = client.post(
                "/v1/api/admin/mcp-registry/install",
                json={
                    "registry_name": "io.example/test-server",
                    "source": "remote",
                    "name": "my-custom-name",
                },
            )

        assert resp.status_code == 200
        assert resp.json()["name"] == "my-custom-name"

    def test_install_with_env_values(self, client: TestClient, storage: SQLiteBackend) -> None:
        srv = _sample_package_server()
        mock_result = _mock_search_result([srv])

        with (
            patch("turnstone.core.mcp_registry.MCPRegistryClient") as mock_client,
            patch(
                "turnstone.console.server._notify_nodes_mcp_reload",
                new_callable=AsyncMock,
                return_value={},
            ),
        ):
            instance = AsyncMock()
            instance.search.return_value = mock_result
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            mock_client.return_value = instance

            resp = client.post(
                "/v1/api/admin/mcp-registry/install",
                json={
                    "registry_name": "io.example/npm-server",
                    "source": "package",
                    "env": {"API_KEY": "my-secret-key"},
                },
            )

        assert resp.status_code == 200
        s = storage.get_mcp_server_by_registry_name("io.example/npm-server")
        assert s is not None
        env = json.loads(s["env"])
        assert env["API_KEY"] == "my-secret-key"

    def test_install_permission_denied(self, client_no_perm: TestClient) -> None:
        resp = client_no_perm.post(
            "/v1/api/admin/mcp-registry/install",
            json={
                "registry_name": "io.example/test",
                "source": "remote",
            },
        )
        assert resp.status_code == 403

    def test_install_auto_reloads_nodes(self, client: TestClient, storage: SQLiteBackend) -> None:
        """Verify _notify_nodes_mcp_reload is called on install."""
        srv = _sample_remote_server()
        mock_result = _mock_search_result([srv])

        with (
            patch("turnstone.core.mcp_registry.MCPRegistryClient") as mock_client,
            patch(
                "turnstone.console.server._notify_nodes_mcp_reload",
                new_callable=AsyncMock,
                return_value={},
            ) as mock_reload,
        ):
            instance = AsyncMock()
            instance.search.return_value = mock_result
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            mock_client.return_value = instance

            resp = client.post(
                "/v1/api/admin/mcp-registry/install",
                json={
                    "registry_name": "io.example/test-server",
                    "source": "remote",
                },
            )

        assert resp.status_code == 200
        mock_reload.assert_called_once()

    def test_install_name_collision(self, client: TestClient, storage: SQLiteBackend) -> None:
        """If sanitized name collides with existing server, suggest custom name."""
        import uuid

        storage.create_mcp_server(
            server_id=uuid.uuid4().hex,
            name="io.example.test-server",
            transport="stdio",
            command="echo",
        )

        srv = _sample_remote_server()
        mock_result = _mock_search_result([srv])

        with patch("turnstone.core.mcp_registry.MCPRegistryClient") as mock_client:
            instance = AsyncMock()
            instance.search.return_value = mock_result
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            mock_client.return_value = instance

            resp = client.post(
                "/v1/api/admin/mcp-registry/install",
                json={
                    "registry_name": "io.example/test-server",
                    "source": "remote",
                },
            )

        assert resp.status_code == 409
        assert "custom 'name'" in resp.json()["error"]


# ---------------------------------------------------------------------------
# _get_registry_url fallback chain tests
# ---------------------------------------------------------------------------


def _mock_request(storage: Any = None, config_store: Any = None) -> MagicMock:
    """Build a mock Request with app.state.auth_storage and app.state.config_store."""
    request = MagicMock()
    request.app.state.auth_storage = storage
    request.app.state.config_store = config_store
    return request


class TestGetRegistryUrl:
    """Verify three-tier URL resolution: DB setting -> config.toml -> default."""

    def test_returns_db_setting_when_available(self) -> None:
        config_store = MagicMock()
        config_store.get.return_value = "https://custom.registry.example.com"
        request = _mock_request(config_store=config_store)

        with patch("turnstone.core.config.load_config", return_value={}):
            url = _get_registry_url(request)

        assert url == "https://custom.registry.example.com"
        config_store.get.assert_called_once_with("mcp.registry_url")

    def test_falls_back_to_config_when_config_store_returns_empty(self) -> None:
        config_store = MagicMock()
        config_store.get.return_value = ""
        request = _mock_request(config_store=config_store)

        with patch(
            "turnstone.core.config.load_config",
            return_value={"registry_url": "https://config.registry.example.com"},
        ):
            url = _get_registry_url(request)

        assert url == "https://config.registry.example.com"

    def test_falls_back_to_config_when_no_config_store(self) -> None:
        request = _mock_request()

        with patch(
            "turnstone.core.config.load_config",
            return_value={"registry_url": "https://config.registry.example.com"},
        ):
            url = _get_registry_url(request)

        assert url == "https://config.registry.example.com"

    def test_falls_back_to_default_when_both_unavailable(self) -> None:
        config_store = MagicMock()
        config_store.get.return_value = ""
        request = _mock_request(config_store=config_store)

        with patch("turnstone.core.config.load_config", return_value={}):
            url = _get_registry_url(request)

        assert url == DEFAULT_REGISTRY_URL

    def test_falls_back_to_default_when_no_config_store_or_config(self) -> None:
        request = _mock_request()

        with patch("turnstone.core.config.load_config", return_value={}):
            url = _get_registry_url(request)

        assert url == DEFAULT_REGISTRY_URL
