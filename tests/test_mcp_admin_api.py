"""Tests for MCP server admin API endpoints."""

from __future__ import annotations

import base64
import json
import uuid
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
    _collect_mcp_status,
    _notify_nodes_mcp_reconnect_one,
    _notify_nodes_mcp_refresh_one,
    _notify_nodes_mcp_reload,
    admin_create_mcp_server,
    admin_delete_mcp_server,
    admin_get_mcp_server,
    admin_import_mcp_config,
    admin_list_mcp_servers,
    admin_mcp_reconnect_one,
    admin_mcp_refresh_one,
    admin_mcp_reload,
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
                "/api/admin/mcp-servers/reload",
                admin_mcp_reload,
                methods=["POST"],
            ),
            Route(
                "/api/admin/mcp-servers/{name}/refresh",
                admin_mcp_refresh_one,
                methods=["POST"],
            ),
            Route(
                "/api/admin/mcp-servers/{name}/reconnect",
                admin_mcp_reconnect_one,
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


def _routes_with_internal() -> list[Mount]:
    """Routes including the node-side internal endpoints (lazy-imported)."""
    from turnstone.server import (
        internal_mcp_reconnect_one,
        internal_mcp_refresh_one,
        internal_mcp_reload,
    )

    return [
        Mount(
            "/v1",
            routes=[
                *_ROUTES[0].routes,  # type: ignore[union-attr]
                Route("/api/_internal/mcp-reload", internal_mcp_reload, methods=["POST"]),
                Route(
                    "/api/_internal/mcp-refresh/{name}",
                    internal_mcp_refresh_one,
                    methods=["POST"],
                ),
                Route(
                    "/api/_internal/mcp-reconnect/{name}",
                    internal_mcp_reconnect_one,
                    methods=["POST"],
                ),
            ],
        ),
    ]


@pytest.fixture
def storage(tmp_path):
    return SQLiteBackend(str(tmp_path / "test.db"))


def _install_token_store(app, storage) -> None:
    """Install an MCPTokenStore on ``app.state`` for tests that exercise
    the OAuth client-secret write path.  Uses a deterministic test key."""
    from cryptography.fernet import Fernet

    from turnstone.core.mcp_crypto import (
        MCPTokenCipher,
        MCPTokenCipherConfig,
        MCPTokenStore,
    )

    raw_key = base64.urlsafe_b64decode(Fernet.generate_key())
    cipher = MCPTokenCipher(MCPTokenCipherConfig(keys=(raw_key,)))
    app.state.mcp_token_cipher = cipher
    app.state.mcp_token_store = MCPTokenStore(
        storage, cipher, node_id="test", audit_storage=storage
    )


@pytest.fixture
def client(storage):
    """TestClient wired to console admin MCP endpoints with full permissions."""
    app = Starlette(
        routes=_ROUTES,
        middleware=[Middleware(_InjectAuthMiddleware)],
    )
    app.state.auth_storage = storage
    _install_token_store(app, storage)
    return TestClient(app)


@pytest.fixture
def client_no_perm(storage):
    """TestClient without admin.mcp permission."""
    app = Starlette(
        routes=_ROUTES,
        middleware=[Middleware(_InjectAuthNoMcpMiddleware)],
    )
    app.state.auth_storage = storage
    _install_token_store(app, storage)
    return TestClient(app)


@pytest.fixture
def client_no_token_store(storage):
    """TestClient WITHOUT MCPTokenStore — the 503 path for OAuth secret writes."""
    app = Starlette(
        routes=_ROUTES,
        middleware=[Middleware(_InjectAuthMiddleware)],
    )
    app.state.auth_storage = storage
    app.state.mcp_token_store = None
    app.state.mcp_token_cipher = None
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

    def test_admin_create_oauth_server(self, client):
        """Phase 3: admin can POST a server with auth_type=oauth_user;
        the seven OAuth text fields round-trip via GET and the plaintext
        client secret is encrypted-at-rest via the dedicated writer."""
        r = client.post(
            "/v1/api/admin/mcp-servers",
            json={
                "name": "oauth-srv",
                "transport": "streamable-http",
                "url": "https://mcp.example.com/sse",
                "auth_type": "oauth_user",
                "oauth_client_id": "cli_abc",
                "oauth_client_secret": "secret-value",
                "oauth_scopes": "openid profile",
                "oauth_audience": "https://mcp.example.com",
                "oauth_registration_mode": "preregistered",
                "oauth_authorization_server_url": "https://auth.example.com",
            },
        )
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["auth_type"] == "oauth_user"
        assert data["oauth_client_id"] == "cli_abc"
        assert data["oauth_scopes"] == "openid profile"
        assert data["oauth_audience"] == "https://mcp.example.com"
        assert data["oauth_registration_mode"] == "preregistered"
        assert data["oauth_authorization_server_url"] == "https://auth.example.com"
        # Phase 3: ciphertext is persisted; the response masks it to "***".
        assert data["oauth_client_secret_ct"] == "***"

    def test_admin_create_oauth_server_without_token_store_returns_503(self, client_no_token_store):
        """When MCPTokenStore is unconfigured (no Fernet key), the admin
        form returns 503 with an operator-actionable hint rather than
        silently dropping the secret."""
        r = client_no_token_store.post(
            "/v1/api/admin/mcp-servers",
            json={
                "name": "oauth-srv",
                "transport": "streamable-http",
                "url": "https://mcp.example.com/sse",
                "auth_type": "oauth_user",
                "oauth_client_id": "cli_abc",
                "oauth_client_secret": "secret-value",
            },
        )
        assert r.status_code == 503, r.text
        assert "mcp_token_encryption_key" in r.json()["error"]

    def test_admin_create_oauth_server_503_does_not_create_orphan_row(
        self, client_no_token_store, storage
    ):
        """bug-1: a 503 from the token-store gate must not leave an orphan
        ``oauth_user`` row behind.  The validation is pre-mutation."""
        r = client_no_token_store.post(
            "/v1/api/admin/mcp-servers",
            json={
                "name": "would-be-orphan",
                "transport": "streamable-http",
                "url": "https://mcp.example.com/sse",
                "auth_type": "oauth_user",
                "oauth_client_id": "cli_abc",
                "oauth_client_secret": "secret-value",
            },
        )
        assert r.status_code == 503
        # No row was created — the storage write was gated on the token store.
        assert storage.get_mcp_server_by_name("would-be-orphan") is None
        assert storage.list_mcp_servers() == []

    def test_admin_create_oauth_server_rejects_non_string_secret(self, client):
        """bug-3: ``oauth_client_secret`` must be a string or null in JSON.
        A boolean / number / list payload should 400 cleanly, not be coerced
        via ``str(...)``."""
        for bad in (False, 0, [], {}):
            r = client.post(
                "/v1/api/admin/mcp-servers",
                json={
                    "name": f"bad-secret-{type(bad).__name__}",
                    "transport": "streamable-http",
                    "url": "https://mcp.example.com/sse",
                    "auth_type": "oauth_user",
                    "oauth_client_secret": bad,
                },
            )
            assert r.status_code == 400, f"payload={bad!r} got {r.status_code}: {r.text}"
            assert "oauth_client_secret" in r.json()["error"]

    def test_create_invalid_auth_type(self, client):
        r = client.post(
            "/v1/api/admin/mcp-servers",
            json={
                "name": "bad-auth",
                "transport": "stdio",
                "command": "x",
                "auth_type": "magic",
            },
        )
        assert r.status_code == 400
        assert "auth_type" in r.json()["error"].lower()


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

    def test_admin_update_auth_type_static_to_oauth(self, client):
        """Phase 2: an existing static row can be flipped to oauth_user
        with OAuth fields supplied alongside."""
        created = _create_server(
            client,
            name="flip-to-oauth",
            transport="streamable-http",
            url="http://mcp.example.com/sse",
        )
        sid = created["server_id"]
        assert created["auth_type"] == "static"

        r = client.put(
            f"/v1/api/admin/mcp-servers/{sid}",
            json={
                "auth_type": "oauth_user",
                "oauth_client_id": "cli_xyz",
                "oauth_audience": "https://mcp.example.com",
                "oauth_registration_mode": "dcr",
            },
        )
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["auth_type"] == "oauth_user"
        assert data["oauth_client_id"] == "cli_xyz"
        assert data["oauth_audience"] == "https://mcp.example.com"
        assert data["oauth_registration_mode"] == "dcr"

    def test_update_invalid_auth_type(self, client):
        created = _create_server(client, name="bad-auth-update")
        sid = created["server_id"]
        r = client.put(
            f"/v1/api/admin/mcp-servers/{sid}",
            json={"auth_type": "wat"},
        )
        assert r.status_code == 400
        assert "auth_type" in r.json()["error"].lower()

    def test_update_empty_auth_type_rejected(self, client):
        """Empty-string auth_type is rejected (no silent coercion to 'static')."""
        created = _create_server(client, name="empty-auth-update")
        sid = created["server_id"]
        r = client.put(
            f"/v1/api/admin/mcp-servers/{sid}",
            json={"auth_type": ""},
        )
        assert r.status_code == 400
        assert "auth_type" in r.json()["error"].lower()

    def test_create_empty_auth_type_rejected(self, client):
        """Empty-string auth_type on create is rejected too."""
        r = client.post(
            "/v1/api/admin/mcp-servers",
            json={
                "name": "empty-auth-create",
                "transport": "stdio",
                "command": "x",
                "auth_type": "",
            },
        )
        assert r.status_code == 400
        assert "auth_type" in r.json()["error"].lower()

    def test_update_auth_type_oauth_to_static_clears_oauth_fields(self, client):
        """Flipping auth_type away from oauth_user clears the oauth_* text
        columns so a stale client_id / audience can't leak back."""
        # Seed an oauth_user row with all fields populated.
        r = client.post(
            "/v1/api/admin/mcp-servers",
            json={
                "name": "flip-away",
                "transport": "streamable-http",
                "url": "https://mcp.example.com/sse",
                "auth_type": "oauth_user",
                "oauth_client_id": "cli_seed",
                "oauth_scopes": "openid",
                "oauth_audience": "https://mcp.example.com",
                "oauth_registration_mode": "preregistered",
                "oauth_authorization_server_url": "https://auth.example.com",
            },
        )
        assert r.status_code == 200, r.text
        sid = r.json()["server_id"]

        # Flip to static — server should clear all oauth_* text fields.
        r = client.put(
            f"/v1/api/admin/mcp-servers/{sid}",
            json={"auth_type": "static"},
        )
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["auth_type"] == "static"
        assert data["oauth_client_id"] is None
        assert data["oauth_scopes"] is None
        assert data["oauth_audience"] is None
        assert data["oauth_registration_mode"] is None
        assert data["oauth_authorization_server_url"] is None

    def test_update_auth_type_oauth_to_none_clears_oauth_fields(self, client):
        """Same clear behavior when flipping to 'none'."""
        r = client.post(
            "/v1/api/admin/mcp-servers",
            json={
                "name": "flip-to-none",
                "transport": "streamable-http",
                "url": "https://mcp.example.com/sse",
                "auth_type": "oauth_user",
                "oauth_client_id": "cli_seed2",
                "oauth_audience": "https://mcp.example.com",
            },
        )
        assert r.status_code == 200, r.text
        sid = r.json()["server_id"]

        r = client.put(
            f"/v1/api/admin/mcp-servers/{sid}",
            json={"auth_type": "none"},
        )
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["auth_type"] == "none"
        assert data["oauth_client_id"] is None
        assert data["oauth_audience"] is None

    def test_admin_update_oauth_server_503_does_not_partial_write(
        self, client_no_token_store, storage
    ):
        """bug-2: a 503 from the token-store gate during PUT must leave the
        existing row unchanged — no partial column rewrites persist."""
        # Seed a static row directly via storage so no token store is needed.
        storage.create_mcp_server(
            server_id="srv-pre",
            name="pre-existing",
            transport="streamable-http",
            url="https://orig.example.com/sse",
            auth_type="static",
        )
        before = storage.get_mcp_server("srv-pre")
        assert before is not None

        # Try to flip to oauth_user with a secret while token store is None.
        r = client_no_token_store.put(
            "/v1/api/admin/mcp-servers/srv-pre",
            json={
                "auth_type": "oauth_user",
                "oauth_client_id": "cli_partial",
                "oauth_client_secret": "would-be-written",
                "url": "https://changed.example.com/sse",
            },
        )
        assert r.status_code == 503, r.text

        # Row must be unchanged — no partial column rewrites.
        after = storage.get_mcp_server("srv-pre")
        assert after is not None
        assert after["auth_type"] == "static"
        assert after["url"] == "https://orig.example.com/sse"
        assert after.get("oauth_client_id") in (None, "")

    def test_admin_update_rejects_non_string_secret(self, client):
        """bug-3 (update path): non-string ``oauth_client_secret`` -> 400."""
        # Seed an oauth_user row.
        r = client.post(
            "/v1/api/admin/mcp-servers",
            json={
                "name": "oauth-update-bad-secret",
                "transport": "streamable-http",
                "url": "https://mcp.example.com/sse",
                "auth_type": "oauth_user",
                "oauth_client_id": "cli",
            },
        )
        assert r.status_code == 200, r.text
        sid = r.json()["server_id"]

        for bad in (False, 0, [], {}):
            r = client.put(
                f"/v1/api/admin/mcp-servers/{sid}",
                json={"oauth_client_secret": bad},
            )
            assert r.status_code == 400, f"payload={bad!r} got {r.status_code}: {r.text}"
            assert "oauth_client_secret" in r.json()["error"]

    def test_auth_type_transition_clears_oauth_client_secret_ct(self, client, storage):
        """sec-2: flipping auth_type from oauth_user away (to static or none)
        must clear the encrypted client secret column.  Otherwise the stale
        ciphertext would resurface if the row were flipped back."""
        # Seed an oauth_user row WITH a client secret persisted.
        r = client.post(
            "/v1/api/admin/mcp-servers",
            json={
                "name": "transition-clears-secret",
                "transport": "streamable-http",
                "url": "https://mcp.example.com/sse",
                "auth_type": "oauth_user",
                "oauth_client_id": "cli_xx",
                "oauth_client_secret": "stays-until-transition",
            },
        )
        assert r.status_code == 200, r.text
        sid = r.json()["server_id"]
        # Confirm the ciphertext column is populated before the transition.
        seeded = storage.get_mcp_server(sid)
        assert seeded is not None
        assert seeded.get("oauth_client_secret_ct") is not None

        # Flip to static — column must be cleared.
        r = client.put(
            f"/v1/api/admin/mcp-servers/{sid}",
            json={"auth_type": "static"},
        )
        assert r.status_code == 200, r.text
        cleared = storage.get_mcp_server(sid)
        assert cleared is not None
        assert cleared["auth_type"] == "static"
        assert cleared.get("oauth_client_secret_ct") is None

    def test_auth_type_transition_clears_secret_without_token_store(
        self, client_no_token_store, storage
    ):
        """sec-2: when no encryption key is configured the transition still
        clears the column via a direct storage call — the operator's
        mental model holds even with the cipher disabled at runtime."""
        # Seed an oauth_user row with raw ciphertext bytes via storage so the
        # transition has something to clear.
        storage.create_mcp_server(
            server_id="srv-direct-clear",
            name="direct-clear",
            transport="streamable-http",
            url="https://mcp.example.com/sse",
            auth_type="oauth_user",
            oauth_client_id="cli_direct",
        )
        # Plant ciphertext via the dedicated writer (no cipher needed).
        storage.set_mcp_oauth_client_secret_ct("srv-direct-clear", b"opaque-bytes")
        seeded = storage.get_mcp_server("srv-direct-clear")
        assert seeded is not None
        assert seeded.get("oauth_client_secret_ct") is not None

        r = client_no_token_store.put(
            "/v1/api/admin/mcp-servers/srv-direct-clear",
            json={"auth_type": "none"},
        )
        assert r.status_code == 200, r.text
        cleared = storage.get_mcp_server("srv-direct-clear")
        assert cleared is not None
        assert cleared["auth_type"] == "none"
        assert cleared.get("oauth_client_secret_ct") is None


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


# ---------------------------------------------------------------------------
# Unit tests for _collect_mcp_status / _notify_nodes_mcp_reload
# ---------------------------------------------------------------------------


def _fake_request(*nodes: dict[str, Any], proxy_client: Any = None) -> MagicMock:
    """Build a minimal mock request with collector and proxy_client."""
    collector = MagicMock()
    collector.get_nodes.return_value = (list(nodes), len(nodes))
    collector.get_all_nodes.side_effect = lambda: collector.get_nodes.return_value[0]
    req = MagicMock()
    req.state.auth_result = None
    req.app.state.collector = collector
    req.app.state.jwt_secret = ""
    req.app.state.proxy_client = proxy_client or AsyncMock()
    req.app.state.proxy_token_mgr = None
    req.app.state.proxy_auth_token = "tok"
    return req


def _mock_resp(status_code: int = 200, json_data: Any = None) -> MagicMock:
    """Build a mock httpx response (sync .json(), like the real thing)."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    return resp


class TestCollectMcpStatus:
    @pytest.mark.anyio
    async def test_returns_servers_on_200(self):
        resp = _mock_resp(200, {"servers": {"s1": {"status": "ok"}}})
        client = AsyncMock()
        client.get.return_value = resp
        req = _fake_request(
            {"node_id": "n1", "server_url": "http://n1:8000"},
            proxy_client=client,
        )
        result = await _collect_mcp_status(req)
        assert result == {"n1": {"s1": {"status": "ok"}}}

    @pytest.mark.anyio
    async def test_skips_non_200(self):
        client = AsyncMock()
        client.get.return_value = _mock_resp(503)
        req = _fake_request(
            {"node_id": "n1", "server_url": "http://n1:8000"},
            proxy_client=client,
        )
        result = await _collect_mcp_status(req)
        assert result == {}

    @pytest.mark.anyio
    async def test_skips_nodes_without_url(self):
        client = AsyncMock()
        req = _fake_request(
            {"node_id": "n1", "server_url": ""},
            {"node_id": "n2"},
            proxy_client=client,
        )
        result = await _collect_mcp_status(req)
        assert result == {}
        client.get.assert_not_called()

    @pytest.mark.anyio
    async def test_handles_exception(self):
        client = AsyncMock()
        client.get.side_effect = ConnectionError("refused")
        req = _fake_request(
            {"node_id": "n1", "server_url": "http://n1:8000"},
            proxy_client=client,
        )
        result = await _collect_mcp_status(req)
        assert result == {}

    @pytest.mark.anyio
    async def test_empty_cluster(self):
        req = _fake_request()
        result = await _collect_mcp_status(req)
        assert result == {}

    @pytest.mark.anyio
    async def test_multiple_nodes_mixed(self):
        ok_resp = _mock_resp(200, {"servers": {"s1": {"status": "ok"}}})
        err_resp = _mock_resp(500)

        client = AsyncMock()
        client.get.side_effect = [ok_resp, ConnectionError("down"), err_resp]
        req = _fake_request(
            {"node_id": "n1", "server_url": "http://n1:8000"},
            {"node_id": "n2", "server_url": "http://n2:8000"},
            {"node_id": "n3", "server_url": "http://n3:8000"},
            proxy_client=client,
        )
        result = await _collect_mcp_status(req)
        assert result == {"n1": {"s1": {"status": "ok"}}}


class TestNotifyNodesMcpReload:
    @pytest.mark.anyio
    async def test_returns_json_on_success(self):
        client = AsyncMock()
        client.post.return_value = _mock_resp(200, {"reloaded": 3})
        req = _fake_request(
            {"node_id": "n1", "server_url": "http://n1:8000"},
            proxy_client=client,
        )
        result = await _notify_nodes_mcp_reload(req)
        assert result == {"n1": {"reloaded": 3}}

    @pytest.mark.anyio
    async def test_skips_nodes_without_url(self):
        client = AsyncMock()
        req = _fake_request(
            {"node_id": "n1", "server_url": ""},
            proxy_client=client,
        )
        result = await _notify_nodes_mcp_reload(req)
        assert result == {}
        client.post.assert_not_called()

    @pytest.mark.anyio
    async def test_records_error_on_exception(self):
        client = AsyncMock()
        client.post.side_effect = ConnectionError("refused")
        req = _fake_request(
            {"node_id": "n1", "server_url": "http://n1:8000"},
            proxy_client=client,
        )
        result = await _notify_nodes_mcp_reload(req)
        assert "n1" in result
        assert "error" in result["n1"]
        assert "refused" in result["n1"]["error"]

    @pytest.mark.anyio
    async def test_empty_cluster(self):
        req = _fake_request()
        result = await _notify_nodes_mcp_reload(req)
        assert result == {}

    @pytest.mark.anyio
    async def test_multiple_nodes_mixed(self):
        client = AsyncMock()
        client.post.side_effect = [
            _mock_resp(200, {"reloaded": 2}),
            TimeoutError("timeout"),
        ]
        req = _fake_request(
            {"node_id": "n1", "server_url": "http://n1:8000"},
            {"node_id": "n2", "server_url": "http://n2:8000"},
            proxy_client=client,
        )
        result = await _notify_nodes_mcp_reload(req)
        assert result["n1"] == {"reloaded": 2}
        assert "error" in result["n2"]


# ---------------------------------------------------------------------------
# Console reload endpoint: POST /v1/api/admin/mcp-servers/reload
# ---------------------------------------------------------------------------


class TestAdminMcpReloadEndpoint:
    """HTTP-level tests for the console reload endpoint."""

    def test_reload_success(self, client: TestClient) -> None:
        """Reload endpoint returns status ok and fan-out results."""
        with patch(
            "turnstone.console.server._notify_nodes_mcp_reload",
            new_callable=AsyncMock,
            return_value={"n1": {"reloaded": 3}},
        ):
            r = client.post("/v1/api/admin/mcp-servers/reload")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"
        assert data["results"] == {"n1": {"reloaded": 3}}

    def test_reload_empty_cluster(self, client: TestClient) -> None:
        """Reload with no nodes returns empty results."""
        with patch(
            "turnstone.console.server._notify_nodes_mcp_reload",
            new_callable=AsyncMock,
            return_value={},
        ):
            r = client.post("/v1/api/admin/mcp-servers/reload")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"
        assert data["results"] == {}

    def test_reload_permission_denied(self, client_no_perm: TestClient) -> None:
        """Reload without admin.mcp permission is rejected."""
        r = client_no_perm.post("/v1/api/admin/mcp-servers/reload")
        assert r.status_code == 403
        assert "admin.mcp" in r.json()["error"]

    def test_reload_no_storage(self) -> None:
        """Reload returns 503 when auth_storage is not available."""
        app = Starlette(
            routes=_ROUTES,
            middleware=[Middleware(_InjectAuthMiddleware)],
        )
        # Deliberately omit app.state.auth_storage
        no_storage_client = TestClient(app, raise_server_exceptions=False)
        r = no_storage_client.post("/v1/api/admin/mcp-servers/reload")
        assert r.status_code == 503

    def test_reload_mixed_node_results(self, client: TestClient) -> None:
        """Reload propagates per-node errors in results."""
        with patch(
            "turnstone.console.server._notify_nodes_mcp_reload",
            new_callable=AsyncMock,
            return_value={
                "n1": {"reloaded": 2},
                "n2": {"error": "Connection refused"},
            },
        ):
            r = client.post("/v1/api/admin/mcp-servers/reload")
        assert r.status_code == 200
        data = r.json()
        assert data["results"]["n1"] == {"reloaded": 2}
        assert "error" in data["results"]["n2"]


# ---------------------------------------------------------------------------
# Node reload endpoint: POST /v1/api/_internal/mcp-reload
# ---------------------------------------------------------------------------


class TestInternalMcpReloadEndpoint:
    """HTTP-level tests for the node-side MCP reload endpoint."""

    @pytest.fixture()
    def node_client(self, storage: SQLiteBackend) -> TestClient:
        """TestClient with an MCP client manager on app.state."""
        app = Starlette(
            routes=_routes_with_internal(),
            middleware=[Middleware(_InjectAuthMiddleware)],
        )
        app.state.auth_storage = storage
        mgr = MagicMock()
        mgr.reconcile_sync.return_value = {
            "added": ["new-srv"],
            "removed": [],
            "updated": [],
        }
        app.state.mcp_client = mgr
        return TestClient(app, raise_server_exceptions=False)

    def test_reload_calls_reconcile(self, node_client: TestClient, storage: SQLiteBackend) -> None:
        """Reload endpoint calls reconcile_sync and returns its result."""
        with patch("turnstone.core.storage._registry.get_storage", return_value=storage):
            r = node_client.post("/v1/api/_internal/mcp-reload")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"
        assert data["added"] == ["new-srv"]
        assert data["removed"] == []
        assert data["updated"] == []

    def test_reload_passes_storage_to_reconcile(
        self,
        storage: SQLiteBackend,
    ) -> None:
        """Verify reconcile_sync receives the storage backend."""
        app = Starlette(
            routes=_routes_with_internal(),
            middleware=[Middleware(_InjectAuthMiddleware)],
        )
        app.state.auth_storage = storage
        mgr = MagicMock()
        mgr.reconcile_sync.return_value = {"added": [], "removed": [], "updated": []}
        app.state.mcp_client = mgr
        c = TestClient(app, raise_server_exceptions=False)
        with patch("turnstone.core.storage._registry.get_storage", return_value=storage):
            r = c.post("/v1/api/_internal/mcp-reload")
        assert r.status_code == 200
        mgr.reconcile_sync.assert_called_once_with(storage)

    def test_reload_creates_manager_when_missing(self, storage: SQLiteBackend) -> None:
        """When mcp_client is absent, a new MCPClientManager is created."""
        app = Starlette(
            routes=_routes_with_internal(),
            middleware=[Middleware(_InjectAuthMiddleware)],
        )
        app.state.auth_storage = storage
        # No mcp_client on app.state
        c = TestClient(app, raise_server_exceptions=False)
        with (
            patch("turnstone.core.storage._registry.get_storage", return_value=storage),
            patch("turnstone.core.mcp_client.MCPClientManager") as mock_cls,
        ):
            mock_mgr = MagicMock()
            mock_mgr.reconcile_sync.return_value = {
                "added": [],
                "removed": [],
                "updated": [],
            }
            mock_cls.return_value = mock_mgr
            r = c.post("/v1/api/_internal/mcp-reload")
        assert r.status_code == 200
        mock_cls.assert_called_once_with({})
        mock_mgr.start.assert_called_once()
        mock_mgr.reconcile_sync.assert_called_once_with(storage)

    def test_reload_reconcile_result_in_response(self, storage: SQLiteBackend) -> None:
        """Full reconcile result fields (added/removed/updated) appear in JSON."""
        app = Starlette(
            routes=_routes_with_internal(),
            middleware=[Middleware(_InjectAuthMiddleware)],
        )
        app.state.auth_storage = storage
        mgr = MagicMock()
        mgr.reconcile_sync.return_value = {
            "added": ["a"],
            "removed": ["b"],
            "updated": ["c"],
        }
        app.state.mcp_client = mgr
        c = TestClient(app, raise_server_exceptions=False)
        with patch("turnstone.core.storage._registry.get_storage", return_value=storage):
            r = c.post("/v1/api/_internal/mcp-reload")
        data = r.json()
        assert data["added"] == ["a"]
        assert data["removed"] == ["b"]
        assert data["updated"] == ["c"]


# ---------------------------------------------------------------------------
# _notify_nodes_mcp_refresh_one / _notify_nodes_mcp_reconnect_one
# ---------------------------------------------------------------------------


class TestNotifyNodesMcpRefreshOne:
    @pytest.mark.anyio
    async def test_returns_json_on_success(self):
        client = AsyncMock()
        client.post.return_value = _mock_resp(200, {"status": "ok"})
        req = _fake_request(
            {"node_id": "n1", "server_url": "http://n1:8000"},
            proxy_client=client,
        )
        result = await _notify_nodes_mcp_refresh_one(req, "srv")
        assert result == {"n1": {"status": "ok"}}
        # Verify the URL used the safe-encoded name segment
        call_args = client.post.call_args
        assert call_args[0][0].endswith("/v1/api/_internal/mcp-refresh/srv")

    @pytest.mark.anyio
    async def test_skips_nodes_without_url(self):
        client = AsyncMock()
        req = _fake_request(
            {"node_id": "n1", "server_url": ""},
            proxy_client=client,
        )
        result = await _notify_nodes_mcp_refresh_one(req, "srv")
        assert result == {}
        client.post.assert_not_called()

    @pytest.mark.anyio
    async def test_records_error_on_exception(self):
        client = AsyncMock()
        client.post.side_effect = ConnectionError("refused")
        req = _fake_request(
            {"node_id": "n1", "server_url": "http://n1:8000"},
            proxy_client=client,
        )
        result = await _notify_nodes_mcp_refresh_one(req, "srv")
        assert "n1" in result
        assert "error" in result["n1"]
        assert "refused" in result["n1"]["error"]

    @pytest.mark.anyio
    async def test_multiple_nodes_mixed(self):
        client = AsyncMock()
        client.post.side_effect = [
            _mock_resp(200, {"status": "ok"}),
            TimeoutError("timeout"),
        ]
        req = _fake_request(
            {"node_id": "n1", "server_url": "http://n1:8000"},
            {"node_id": "n2", "server_url": "http://n2:8000"},
            proxy_client=client,
        )
        result = await _notify_nodes_mcp_refresh_one(req, "srv")
        assert result["n1"] == {"status": "ok"}
        assert "error" in result["n2"]

    @pytest.mark.anyio
    async def test_empty_cluster(self):
        req = _fake_request()
        result = await _notify_nodes_mcp_refresh_one(req, "srv")
        assert result == {}


class TestNotifyNodesMcpReconnectOne:
    @pytest.mark.anyio
    async def test_returns_json_on_success(self):
        client = AsyncMock()
        client.post.return_value = _mock_resp(200, {"status": "ok"})
        req = _fake_request(
            {"node_id": "n1", "server_url": "http://n1:8000"},
            proxy_client=client,
        )
        result = await _notify_nodes_mcp_reconnect_one(req, "srv")
        assert result == {"n1": {"status": "ok"}}
        call_args = client.post.call_args
        assert call_args[0][0].endswith("/v1/api/_internal/mcp-reconnect/srv")

    @pytest.mark.anyio
    async def test_skips_nodes_without_url(self):
        client = AsyncMock()
        req = _fake_request(
            {"node_id": "n1", "server_url": ""},
            proxy_client=client,
        )
        result = await _notify_nodes_mcp_reconnect_one(req, "srv")
        assert result == {}
        client.post.assert_not_called()

    @pytest.mark.anyio
    async def test_records_error_on_exception(self):
        client = AsyncMock()
        client.post.side_effect = ConnectionError("refused")
        req = _fake_request(
            {"node_id": "n1", "server_url": "http://n1:8000"},
            proxy_client=client,
        )
        result = await _notify_nodes_mcp_reconnect_one(req, "srv")
        assert "n1" in result
        assert "error" in result["n1"]
        assert "refused" in result["n1"]["error"]

    @pytest.mark.anyio
    async def test_multiple_nodes_mixed(self):
        client = AsyncMock()
        client.post.side_effect = [
            _mock_resp(200, {"status": "ok"}),
            TimeoutError("timeout"),
        ]
        req = _fake_request(
            {"node_id": "n1", "server_url": "http://n1:8000"},
            {"node_id": "n2", "server_url": "http://n2:8000"},
            proxy_client=client,
        )
        result = await _notify_nodes_mcp_reconnect_one(req, "srv")
        assert result["n1"] == {"status": "ok"}
        assert "error" in result["n2"]

    @pytest.mark.anyio
    async def test_empty_cluster(self):
        req = _fake_request()
        result = await _notify_nodes_mcp_reconnect_one(req, "srv")
        assert result == {}


# ---------------------------------------------------------------------------
# Console refresh / reconnect endpoints
# ---------------------------------------------------------------------------


class TestAdminMcpRefreshOneEndpoint:
    """HTTP-level tests for the console refresh-one endpoint."""

    def test_refresh_one_success(self, client: TestClient) -> None:
        with patch(
            "turnstone.console.server._notify_nodes_mcp_action",
            new_callable=AsyncMock,
            return_value={"n1": {"status": "ok"}},
        ) as mock_notify:
            r = client.post("/v1/api/admin/mcp-servers/srv/refresh")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"
        assert data["results"] == {"n1": {"status": "ok"}}
        # The shared helper is called with the action verb.
        mock_notify.assert_awaited_once()
        args = mock_notify.await_args.args
        assert args[1] == "refresh"
        assert args[2] == "srv"

    def test_refresh_one_permission_denied(self, client_no_perm: TestClient) -> None:
        r = client_no_perm.post("/v1/api/admin/mcp-servers/srv/refresh")
        assert r.status_code == 403
        assert "admin.mcp" in r.json()["error"]

    def test_refresh_one_invalid_name(self, client: TestClient) -> None:
        # Names with '__' (reserved delimiter) are rejected.
        r = client.post("/v1/api/admin/mcp-servers/bad__name/refresh")
        assert r.status_code == 400
        assert "invalid" in r.json()["error"].lower()


class TestAdminMcpReconnectOneEndpoint:
    """HTTP-level tests for the console reconnect-one endpoint."""

    def test_reconnect_one_success(self, client: TestClient) -> None:
        with patch(
            "turnstone.console.server._notify_nodes_mcp_action",
            new_callable=AsyncMock,
            return_value={"n1": {"status": "ok"}},
        ) as mock_notify:
            r = client.post("/v1/api/admin/mcp-servers/srv/reconnect")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"
        assert data["results"] == {"n1": {"status": "ok"}}
        mock_notify.assert_awaited_once()
        args = mock_notify.await_args.args
        assert args[1] == "reconnect"
        assert args[2] == "srv"

    def test_reconnect_one_permission_denied(self, client_no_perm: TestClient) -> None:
        r = client_no_perm.post("/v1/api/admin/mcp-servers/srv/reconnect")
        assert r.status_code == 403
        assert "admin.mcp" in r.json()["error"]

    def test_reconnect_one_invalid_name(self, client: TestClient) -> None:
        r = client.post("/v1/api/admin/mcp-servers/bad__name/reconnect")
        assert r.status_code == 400
        assert "invalid" in r.json()["error"].lower()


# ---------------------------------------------------------------------------
# Node refresh-one endpoint: POST /v1/api/_internal/mcp-refresh/{name}
# ---------------------------------------------------------------------------


class TestInternalMcpRefreshOneEndpoint:
    """HTTP-level tests for the node-side per-server refresh endpoint."""

    @pytest.fixture()
    def node_app_factory(self, storage: SQLiteBackend):
        """Build a TestClient with an MCP client manager on app.state."""

        def _make(mgr: Any) -> TestClient:
            app = Starlette(
                routes=_routes_with_internal(),
                middleware=[Middleware(_InjectAuthMiddleware)],
            )
            app.state.auth_storage = storage
            if mgr is not None:
                app.state.mcp_client = mgr
            return TestClient(app, raise_server_exceptions=False)

        return _make

    def test_refresh_one_success(self, node_app_factory) -> None:
        mgr = MagicMock()
        mgr.refresh_sync.return_value = None
        mgr.get_server_status.return_value = {
            "connected": True,
            "tools": 3,
            "resources": 0,
            "prompts": 1,
            "error": "",
            "transport": "stdio",
            "command": "/usr/bin/secret-stdio",
            "url": "",
            "circuit_open": False,
            "consecutive_failures": 0,
        }
        c = node_app_factory(mgr)
        r = c.post("/v1/api/_internal/mcp-refresh/srv")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"
        # sec-3: command/url stripped from response.
        assert "command" not in data["server"]
        assert "url" not in data["server"]
        assert data["server"]["tools"] == 3
        mgr.refresh_sync.assert_called_once_with(server_name="srv")

    def test_refresh_one_no_mcp_client_returns_503(self, node_app_factory) -> None:
        c = node_app_factory(None)
        r = c.post("/v1/api/_internal/mcp-refresh/srv")
        assert r.status_code == 503
        assert r.json()["status"] == "error"

    def test_refresh_one_raises_returns_500(self, node_app_factory) -> None:
        mgr = MagicMock()
        mgr.refresh_sync.side_effect = RuntimeError("internal stdio path /etc/shadow blew up")
        c = node_app_factory(mgr)
        r = c.post("/v1/api/_internal/mcp-refresh/srv")
        assert r.status_code == 500
        # sec-2: raw exception detail must not leak to the caller.
        body = r.json()
        assert body["error"] == "refresh failed"
        assert "shadow" not in body["error"]

    def test_refresh_one_per_server_error_returns_500(self, node_app_factory) -> None:
        # q-3 / bug-3: refresh_sync swallows per-server errors into _last_error,
        # so a 200 from refresh_sync is not enough — get_server_status reports.
        mgr = MagicMock()
        mgr.refresh_sync.return_value = None
        mgr.get_server_status.return_value = {
            "connected": False,
            "tools": 0,
            "resources": 0,
            "prompts": 0,
            "error": "Refresh failed: connection refused",
            "transport": "stdio",
            "command": "secret",
            "url": "",
            "circuit_open": True,
            "consecutive_failures": 5,
        }
        c = node_app_factory(mgr)
        r = c.post("/v1/api/_internal/mcp-refresh/srv")
        assert r.status_code == 500
        data = r.json()
        assert data["status"] == "error"
        assert data["error"] == "refresh failed"
        # Public status echoed but command/url stripped.
        assert "command" not in data["server"]
        assert "url" not in data["server"]
        assert data["server"]["circuit_open"] is True

    def test_refresh_one_invalid_name_returns_400(self, node_app_factory) -> None:
        # sec-4: name validation symmetric with console side.
        mgr = MagicMock()
        c = node_app_factory(mgr)
        r = c.post("/v1/api/_internal/mcp-refresh/bad__name")
        assert r.status_code == 400
        assert "invalid" in r.json()["error"].lower()
        mgr.refresh_sync.assert_not_called()


# ---------------------------------------------------------------------------
# Node reconnect-one endpoint: POST /v1/api/_internal/mcp-reconnect/{name}
# ---------------------------------------------------------------------------


class TestInternalMcpReconnectOneEndpoint:
    """HTTP-level tests for the node-side per-server reconnect endpoint."""

    @pytest.fixture()
    def node_app_factory(self, storage: SQLiteBackend):
        """Build a TestClient with an MCP client manager on app.state."""

        def _make(mgr: Any) -> TestClient:
            app = Starlette(
                routes=_routes_with_internal(),
                middleware=[Middleware(_InjectAuthMiddleware)],
            )
            app.state.auth_storage = storage
            if mgr is not None:
                app.state.mcp_client = mgr
            return TestClient(app, raise_server_exceptions=False)

        return _make

    def test_reconnect_one_success(self, node_app_factory) -> None:
        mgr = MagicMock()
        mgr.reconnect_sync.return_value = {
            "connected": True,
            "tools": 2,
            "resources": 0,
            "prompts": 0,
            "error": "",
        }
        mgr.get_server_status.return_value = {
            "connected": True,
            "tools": 2,
            "resources": 0,
            "prompts": 0,
            "error": "",
            "transport": "stdio",
            "command": "secret-cmd",
            "url": "",
            "circuit_open": False,
            "consecutive_failures": 0,
        }
        c = node_app_factory(mgr)
        r = c.post("/v1/api/_internal/mcp-reconnect/srv")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"
        # sec-3: command/url stripped from response.
        assert "command" not in data["server"]
        assert "url" not in data["server"]
        mgr.reconnect_sync.assert_called_once_with("srv")

    def test_reconnect_one_no_mcp_client_returns_503(self, node_app_factory) -> None:
        c = node_app_factory(None)
        r = c.post("/v1/api/_internal/mcp-reconnect/srv")
        assert r.status_code == 503

    def test_reconnect_one_returns_error_dict_500(self, node_app_factory) -> None:
        mgr = MagicMock()
        mgr.reconnect_sync.return_value = {
            "connected": False,
            "tools": 0,
            "resources": 0,
            "prompts": 0,
            "error": "secret stdio at /etc/shadow timed out",
        }
        mgr.get_server_status.return_value = {
            "connected": False,
            "tools": 0,
            "resources": 0,
            "prompts": 0,
            "error": "secret stdio at /etc/shadow timed out",
            "transport": "stdio",
            "command": "secret",
            "url": "",
            "circuit_open": False,
            "consecutive_failures": 1,
        }
        c = node_app_factory(mgr)
        r = c.post("/v1/api/_internal/mcp-reconnect/srv")
        assert r.status_code == 500
        body = r.json()
        # The top-level `error` is generic; the inner `server.error` echoes
        # whatever ``get_server_status`` returned (still admin-facing).
        assert body["error"] == "reconnect failed"
        assert "command" not in body["server"]
        assert "url" not in body["server"]

    def test_reconnect_one_raises_returns_500(self, node_app_factory) -> None:
        mgr = MagicMock()
        mgr.reconnect_sync.side_effect = RuntimeError("internal stdio /etc/shadow blew up")
        c = node_app_factory(mgr)
        r = c.post("/v1/api/_internal/mcp-reconnect/srv")
        assert r.status_code == 500
        body = r.json()
        assert body["error"] == "reconnect failed"
        assert "shadow" not in body["error"]

    def test_reconnect_one_invalid_name_returns_400(self, node_app_factory) -> None:
        # sec-4: name validation symmetric with console side.
        mgr = MagicMock()
        c = node_app_factory(mgr)
        r = c.post("/v1/api/_internal/mcp-reconnect/bad__name")
        assert r.status_code == 400
        assert "invalid" in r.json()["error"].lower()
        mgr.reconnect_sync.assert_not_called()
