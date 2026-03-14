"""Tests for MCP server admin API endpoints."""

from __future__ import annotations

import json
import uuid
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
    admin_create_mcp_server,
    admin_delete_mcp_server,
    admin_get_mcp_server,
    admin_import_mcp_config,
    admin_list_mcp_servers,
    admin_update_mcp_server,
)
from turnstone.core.auth import AuthResult
from turnstone.core.storage._sqlite import SQLiteBackend

# ---------------------------------------------------------------------------
# Auth middleware variants
# ---------------------------------------------------------------------------


class _InjectAuthMiddleware(BaseHTTPMiddleware):
    """Inject an admin auth result with admin.mcp permission."""

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        request.state.auth_result = AuthResult(
            user_id="test-user",
            scopes=frozenset({"approve"}),
            token_source="config",
            permissions=frozenset(
                {
                    "read",
                    "write",
                    "approve",
                    "admin.mcp",
                }
            ),
        )
        resp: Response = await call_next(request)
        return resp


class _InjectAuthNoMcpMiddleware(BaseHTTPMiddleware):
    """Inject an auth result WITHOUT admin.mcp permission."""

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        request.state.auth_result = AuthResult(
            user_id="test-user",
            scopes=frozenset({"approve"}),
            token_source="jwt",
            permissions=frozenset(
                {
                    "read",
                    "write",
                    "approve",
                }
            ),
        )
        resp: Response = await call_next(request)
        return resp


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_ROUTES = [
    Mount(
        "/v1",
        routes=[
            Route("/api/admin/mcp-servers", admin_list_mcp_servers),
            Route(
                "/api/admin/mcp-servers",
                admin_create_mcp_server,
                methods=["POST"],
            ),
            Route(
                "/api/admin/mcp-servers/import",
                admin_import_mcp_config,
                methods=["POST"],
            ),
            Route(
                "/api/admin/mcp-servers/{server_id}",
                admin_get_mcp_server,
            ),
            Route(
                "/api/admin/mcp-servers/{server_id}",
                admin_update_mcp_server,
                methods=["PUT"],
            ),
            Route(
                "/api/admin/mcp-servers/{server_id}",
                admin_delete_mcp_server,
                methods=["DELETE"],
            ),
        ],
    ),
]


@pytest.fixture
def storage(tmp_path):
    return SQLiteBackend(str(tmp_path / "test.db"))


@pytest.fixture
def client(storage):
    """TestClient wired to console admin MCP endpoints with full permissions."""
    app = Starlette(
        routes=_ROUTES,
        middleware=[Middleware(_InjectAuthMiddleware)],
    )
    app.state.auth_storage = storage
    return TestClient(app)


@pytest.fixture
def client_no_perm(storage):
    """TestClient without admin.mcp permission."""
    app = Starlette(
        routes=_ROUTES,
        middleware=[Middleware(_InjectAuthNoMcpMiddleware)],
    )
    app.state.auth_storage = storage
    return TestClient(app)


def _create_server(
    client: TestClient,
    *,
    name: str = "test-server",
    transport: str = "stdio",
    command: str = "npx",
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    url: str = "",
) -> dict[str, Any]:
    """Helper to create a server via the API and return the response dict."""
    body: dict[str, Any] = {"name": name, "transport": transport}
    if transport == "stdio":
        body["command"] = command
        body["args"] = args or ["-y", "@modelcontextprotocol/server-test"]
    else:
        body["url"] = url or "http://localhost:8080/mcp"
    if env is not None:
        body["env"] = env
    if headers is not None:
        body["headers"] = headers
    r = client.post("/v1/api/admin/mcp-servers", json=body)
    assert r.status_code == 200
    data: dict[str, Any] = r.json()
    return data


# ---------------------------------------------------------------------------
# Mock _collect_mcp_status to avoid real HTTP calls
# ---------------------------------------------------------------------------

_PATCH_MCP_STATUS = patch(
    "turnstone.console.server._collect_mcp_status",
    new_callable=AsyncMock,
    return_value={},
)


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


class TestListMcpServers:
    def test_list_empty(self, client):
        with _PATCH_MCP_STATUS:
            r = client.get("/v1/api/admin/mcp-servers")
        assert r.status_code == 200
        assert r.json()["servers"] == []

    def test_list_returns_created_servers(self, client):
        _create_server(client, name="server-a")
        _create_server(client, name="server-b")
        with _PATCH_MCP_STATUS:
            r = client.get("/v1/api/admin/mcp-servers")
        assert r.status_code == 200
        names = [s["name"] for s in r.json()["servers"]]
        assert "server-a" in names
        assert "server-b" in names


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


class TestCreateMcpServer:
    def test_create_stdio_server(self, client):
        data = _create_server(client, name="my-mcp", transport="stdio", command="node")
        assert data["name"] == "my-mcp"
        assert data["transport"] == "stdio"
        assert data["command"] == "node"
        assert data["server_id"]
        assert data["enabled"] is True

    def test_create_http_server(self, client):
        data = _create_server(
            client,
            name="remote-mcp",
            transport="streamable-http",
            url="http://mcp.example.com/sse",
        )
        assert data["name"] == "remote-mcp"
        assert data["transport"] == "streamable-http"
        assert data["url"] == "http://mcp.example.com/sse"

    def test_create_invalid_name_spaces(self, client):
        r = client.post(
            "/v1/api/admin/mcp-servers",
            json={"name": "bad name!", "transport": "stdio", "command": "x"},
        )
        assert r.status_code == 400
        assert "name" in r.json()["error"].lower()

    def test_create_invalid_name_double_underscore(self, client):
        r = client.post(
            "/v1/api/admin/mcp-servers",
            json={"name": "bad__name", "transport": "stdio", "command": "x"},
        )
        assert r.status_code == 400
        assert "__" in r.json()["error"]

    def test_create_invalid_transport(self, client):
        r = client.post(
            "/v1/api/admin/mcp-servers",
            json={"name": "ok-name", "transport": "grpc"},
        )
        assert r.status_code == 400
        assert "transport" in r.json()["error"].lower()

    def test_create_duplicate_name(self, client):
        _create_server(client, name="dup-test")
        r = client.post(
            "/v1/api/admin/mcp-servers",
            json={"name": "dup-test", "transport": "stdio", "command": "x"},
        )
        assert r.status_code == 409
        assert "already exists" in r.json()["error"]

    def test_create_missing_name(self, client):
        r = client.post(
            "/v1/api/admin/mcp-servers",
            json={"transport": "stdio", "command": "x"},
        )
        assert r.status_code == 400
        assert "name" in r.json()["error"].lower()


# ---------------------------------------------------------------------------
# Get single
# ---------------------------------------------------------------------------


class TestGetMcpServer:
    def test_get_existing(self, client):
        created = _create_server(client, name="get-test")
        sid = created["server_id"]
        with _PATCH_MCP_STATUS:
            r = client.get(f"/v1/api/admin/mcp-servers/{sid}")
        assert r.status_code == 200
        assert r.json()["name"] == "get-test"

    def test_get_not_found(self, client):
        fake_id = uuid.uuid4().hex
        with _PATCH_MCP_STATUS:
            r = client.get(f"/v1/api/admin/mcp-servers/{fake_id}")
        assert r.status_code == 404
        assert "not found" in r.json()["error"].lower()


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------


class TestUpdateMcpServer:
    def test_update_name(self, client):
        created = _create_server(client, name="old-name")
        sid = created["server_id"]
        r = client.put(
            f"/v1/api/admin/mcp-servers/{sid}",
            json={"name": "new-name"},
        )
        assert r.status_code == 200
        assert r.json()["name"] == "new-name"

    def test_update_transport(self, client):
        created = _create_server(
            client,
            name="update-transport",
            transport="streamable-http",
            url="http://localhost/mcp",
        )
        sid = created["server_id"]
        r = client.put(
            f"/v1/api/admin/mcp-servers/{sid}",
            json={"transport": "stdio", "command": "node"},
        )
        assert r.status_code == 200
        assert r.json()["transport"] == "stdio"

    def test_update_enabled(self, client):
        created = _create_server(client, name="toggle-enabled")
        sid = created["server_id"]
        r = client.put(
            f"/v1/api/admin/mcp-servers/{sid}",
            json={"enabled": False},
        )
        assert r.status_code == 200
        assert r.json()["enabled"] is False

    def test_update_not_found(self, client):
        fake_id = uuid.uuid4().hex
        r = client.put(
            f"/v1/api/admin/mcp-servers/{fake_id}",
            json={"name": "x"},
        )
        assert r.status_code == 404

    def test_update_invalid_transport(self, client):
        created = _create_server(client, name="bad-transport-update")
        sid = created["server_id"]
        r = client.put(
            f"/v1/api/admin/mcp-servers/{sid}",
            json={"transport": "websocket"},
        )
        assert r.status_code == 400
        assert "transport" in r.json()["error"].lower()


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


class TestDeleteMcpServer:
    def test_delete_existing(self, client):
        created = _create_server(client, name="del-test")
        sid = created["server_id"]
        r = client.delete(f"/v1/api/admin/mcp-servers/{sid}")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

        # Confirm it's gone
        with _PATCH_MCP_STATUS:
            r2 = client.get(f"/v1/api/admin/mcp-servers/{sid}")
        assert r2.status_code == 404

    def test_delete_not_found(self, client):
        fake_id = uuid.uuid4().hex
        r = client.delete(f"/v1/api/admin/mcp-servers/{fake_id}")
        assert r.status_code == 404
        assert "not found" in r.json()["error"].lower()


# ---------------------------------------------------------------------------
# Secret masking
# ---------------------------------------------------------------------------


class TestSecretMasking:
    def test_list_masks_secrets(self, client):
        _create_server(
            client,
            name="secret-test",
            env={"API_KEY": "sk-real-secret-123"},
            headers={"Authorization": "Bearer tok-xyz"},
            transport="streamable-http",
            url="http://localhost/mcp",
        )
        with _PATCH_MCP_STATUS:
            r = client.get("/v1/api/admin/mcp-servers")
        assert r.status_code == 200
        server = r.json()["servers"][0]
        env = json.loads(server["env"])
        headers = json.loads(server["headers"])
        assert env["API_KEY"] == "***"
        assert headers["Authorization"] == "***"

    def test_list_reveals_secrets(self, client):
        _create_server(
            client,
            name="reveal-test",
            env={"API_KEY": "sk-real-secret-123"},
            headers={"Authorization": "Bearer tok-xyz"},
            transport="streamable-http",
            url="http://localhost/mcp",
        )
        with _PATCH_MCP_STATUS:
            r = client.get("/v1/api/admin/mcp-servers?reveal=true")
        assert r.status_code == 200
        server = r.json()["servers"][0]
        env = json.loads(server["env"])
        headers = json.loads(server["headers"])
        assert env["API_KEY"] == "sk-real-secret-123"
        assert headers["Authorization"] == "Bearer tok-xyz"

    def test_get_masks_secrets_by_default(self, client):
        created = _create_server(
            client,
            name="mask-get-test",
            env={"SECRET": "value"},
        )
        sid = created["server_id"]
        with _PATCH_MCP_STATUS:
            r = client.get(f"/v1/api/admin/mcp-servers/{sid}")
        assert r.status_code == 200
        env = json.loads(r.json()["env"])
        assert env["SECRET"] == "***"

    def test_get_reveals_secrets(self, client):
        created = _create_server(
            client,
            name="reveal-get-test",
            env={"SECRET": "real-value"},
        )
        sid = created["server_id"]
        with _PATCH_MCP_STATUS:
            r = client.get(f"/v1/api/admin/mcp-servers/{sid}?reveal=true")
        assert r.status_code == 200
        env = json.loads(r.json()["env"])
        assert env["SECRET"] == "real-value"


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


class TestImportMcpConfig:
    def test_import_inline_config(self, client):
        config = {
            "mcpServers": {
                "filesystem": {
                    "command": "npx",
                    "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
                },
                "remote": {
                    "url": "http://remote.example.com/mcp",
                },
            },
        }
        r = client.post(
            "/v1/api/admin/mcp-servers/import",
            json={"config": config},
        )
        assert r.status_code == 200
        data = r.json()
        assert "filesystem" in data["imported"]
        assert "remote" in data["imported"]
        assert data["skipped"] == []
        assert data["errors"] == []

    def test_import_not_a_dict(self, client):
        r = client.post(
            "/v1/api/admin/mcp-servers/import",
            json={"config": "not-a-dict"},
        )
        assert r.status_code == 400

    def test_import_skips_duplicates(self, client):
        _create_server(client, name="existing-srv")
        config = {
            "mcpServers": {
                "existing-srv": {"command": "node", "args": []},
                "new-srv": {"command": "node", "args": []},
            },
        }
        r = client.post(
            "/v1/api/admin/mcp-servers/import",
            json={"config": config},
        )
        assert r.status_code == 200
        data = r.json()
        assert "new-srv" in data["imported"]
        assert "existing-srv" in data["skipped"]

    def test_import_empty_body(self, client):
        r = client.post(
            "/v1/api/admin/mcp-servers/import",
            json={},
        )
        assert r.status_code == 400
        assert "config" in r.json()["error"].lower()

    def test_import_no_mcp_servers_key(self, client):
        r = client.post(
            "/v1/api/admin/mcp-servers/import",
            json={"config": {"other": "data"}},
        )
        assert r.status_code == 400
        assert "mcpServers" in r.json()["error"] or "No" in r.json()["error"]


# ---------------------------------------------------------------------------
# Permission check
# ---------------------------------------------------------------------------


class TestPermission:
    def test_list_without_permission(self, client_no_perm):
        with _PATCH_MCP_STATUS:
            r = client_no_perm.get("/v1/api/admin/mcp-servers")
        assert r.status_code == 403
        assert "admin.mcp" in r.json()["error"]

    def test_create_without_permission(self, client_no_perm):
        r = client_no_perm.post(
            "/v1/api/admin/mcp-servers",
            json={"name": "test", "transport": "stdio", "command": "x"},
        )
        assert r.status_code == 403

    def test_delete_without_permission(self, client_no_perm):
        r = client_no_perm.delete(f"/v1/api/admin/mcp-servers/{uuid.uuid4().hex}")
        assert r.status_code == 403
