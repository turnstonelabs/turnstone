"""Tests for MCP server admin API endpoints."""

from __future__ import annotations

import base64
import json
import logging
import uuid
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
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
        internal_mcp_status,
    )

    return [
        Mount(
            "/v1",
            routes=[
                *_ROUTES[0].routes,  # type: ignore[union-attr]
                Route("/api/_internal/mcp-reload", internal_mcp_reload, methods=["POST"]),
                Route("/api/_internal/mcp-status", internal_mcp_status),
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


def _enabled_oidc(profile: str = "entra") -> SimpleNamespace:
    """An OIDC config that satisfies the oauth_obo write-time gate.

    oauth_obo mints from the user's captured sign-in, so the write choke point
    requires OIDC enabled + a valid ``obo_grant_profile``. Tests exercising obo
    writes install one of these; the finding-C tests install a disabled /
    bad-profile config instead to assert the rejection.
    """
    return SimpleNamespace(
        enabled=True,
        issuer="https://idp.example.com",
        obo_grant_profile=profile,
        capture_user_credential=True,
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
    # Default: OIDC enabled under the entra profile so oauth_obo writes pass the
    # requirement gate. Per-test overrides install rfc8693 / disabled / bad
    # profile as needed.
    app.state.oidc_config = _enabled_oidc("entra")
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
        """Admin can POST a server with auth_type=oauth_user; the seven
        OAuth text fields round-trip via GET and the plaintext client
        secret is encrypted-at-rest via the dedicated writer."""
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
        # Ciphertext is persisted; the response masks it to "***".
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

    def test_admin_create_oauth_user_rejects_http(self, client, storage):
        """sec-1: an oauth_user row with a plaintext (non-loopback) URL
        must 400 — pool dispatch would transmit the per-user bearer in
        the clear."""
        r = client.post(
            "/v1/api/admin/mcp-servers",
            json={
                "name": "insecure-oauth",
                "transport": "streamable-http",
                "url": "http://mcp.example.com/sse",
                "auth_type": "oauth_user",
                "oauth_client_id": "cli_abc",
                "oauth_client_secret": "secret-value",
            },
        )
        assert r.status_code == 400, r.text
        assert "https://" in r.json()["error"]
        # No partial row was persisted.
        assert storage.get_mcp_server_by_name("insecure-oauth") is None

    def test_admin_create_oauth_user_accepts_loopback_http(self, client):
        """``http://localhost`` and ``http://127.0.0.1`` must remain
        usable for dev/test convenience."""
        r = client.post(
            "/v1/api/admin/mcp-servers",
            json={
                "name": "dev-oauth",
                "transport": "streamable-http",
                "url": "http://127.0.0.1:9000/sse",
                "auth_type": "oauth_user",
                "oauth_client_id": "cli_abc",
                "oauth_client_secret": "secret-value",
            },
        )
        assert r.status_code == 200, r.text


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
        """An existing static row can be flipped to oauth_user with
        OAuth fields supplied alongside. The row must already use
        https:// — sec-1 enforces this on update too."""
        created = _create_server(
            client,
            name="flip-to-oauth",
            transport="streamable-http",
            url="https://mcp.example.com/sse",
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

    def test_admin_update_oauth_url_to_http_rejected(self, client):
        """sec-1: flipping the URL on an existing oauth_user row to
        plaintext http must 400."""
        # Create with proper https.
        r0 = client.post(
            "/v1/api/admin/mcp-servers",
            json={
                "name": "oauth-flip-url",
                "transport": "streamable-http",
                "url": "https://mcp.example.com/sse",
                "auth_type": "oauth_user",
                "oauth_client_id": "cli_abc",
                "oauth_client_secret": "secret-value",
            },
        )
        assert r0.status_code == 200, r0.text
        sid = r0.json()["server_id"]

        r = client.put(
            f"/v1/api/admin/mcp-servers/{sid}",
            json={"url": "http://insecure.example.com/sse"},
        )
        assert r.status_code == 400, r.text
        assert "https://" in r.json()["error"]

    def test_admin_update_oauth_url_change_purges_user_tokens(self, client, storage):
        """sec-1 (pre-push): URL change on an oauth_user row must purge
        per-user tokens. Bearer tokens are bound (via OAuth resource /
        audience) to the URL active at consent time; sending them to a
        new URL is a token-binding violation. A compromised admin who
        flips the URL to an attacker endpoint would otherwise replay
        every user's bearer there silently. Re-consent must be forced.
        """
        import sqlalchemy as sa

        from turnstone.core.storage._schema import mcp_user_tokens

        # Seed an oauth_user row at URL_A.
        r = client.post(
            "/v1/api/admin/mcp-servers",
            json={
                "name": "url-change-purge",
                "transport": "streamable-http",
                "url": "https://orig.example.com/sse",
                "auth_type": "oauth_user",
                "oauth_client_id": "cli_seed",
            },
        )
        assert r.status_code == 200, r.text

        # Plant a per-user token row keyed on the server name.
        with storage._engine.connect() as conn:
            conn.execute(
                sa.insert(mcp_user_tokens),
                {
                    "user_id": "u1",
                    "server_name": "url-change-purge",
                    "access_token_ct": b"\x00ciphertext-a",
                    "refresh_token_ct": b"\x00ciphertext-r",
                    "expires_at": "2026-12-31T00:00:00",
                    "scopes": "openid",
                    "as_issuer": "https://auth.orig.example.com",
                    "audience": "https://orig.example.com",
                    "created": "2026-05-04T11:00:00",
                    "last_refreshed": None,
                },
            )
            conn.commit()
            count_before = conn.execute(
                sa.select(sa.func.count())
                .select_from(mcp_user_tokens)
                .where(mcp_user_tokens.c.server_name == "url-change-purge")
            ).scalar()
        assert count_before == 1

        # Flip the URL to a different (still https) endpoint.
        sid = r.json()["server_id"]
        r2 = client.put(
            f"/v1/api/admin/mcp-servers/{sid}",
            json={"url": "https://new.example.com/sse"},
        )
        assert r2.status_code == 200, r2.text
        assert r2.json()["url"] == "https://new.example.com/sse"

        # Token rows keyed on the OLD server name must be gone — the new
        # URL is a different OAuth resource, so the bearer is no longer
        # valid there. Force re-consent.
        with storage._engine.connect() as conn:
            count_after = conn.execute(
                sa.select(sa.func.count())
                .select_from(mcp_user_tokens)
                .where(mcp_user_tokens.c.server_name == "url-change-purge")
            ).scalar()
        assert count_after == 0, "URL change must purge per-user tokens"

    def test_admin_create_oauth_obo_requires_audience(self, client):
        """#551: an oauth_obo row without oauth_audience is rejected at the
        write choke point (the mint engine hard-requires it)."""
        r = client.post(
            "/v1/api/admin/mcp-servers",
            json={
                "name": "obo-no-aud",
                "transport": "streamable-http",
                "url": "https://mcp.example.com/sse",
                "auth_type": "oauth_obo",
            },
        )
        assert r.status_code == 400, r.text
        assert "oauth_audience" in r.json()["error"]

    def test_admin_create_oauth_obo_without_token_store_returns_503(self, client_no_token_store):
        """#551: creating an oauth_obo row with no encryption key is rejected —
        accepting it would SystemExit the whole cluster at the next boot."""
        r = client_no_token_store.post(
            "/v1/api/admin/mcp-servers",
            json={
                "name": "obo-no-key",
                "transport": "streamable-http",
                "url": "https://mcp.example.com/sse",
                "auth_type": "oauth_obo",
                "oauth_audience": "api://mcp-a",
            },
        )
        assert r.status_code == 503, r.text
        assert "mcp_token_encryption_key" in r.json()["error"]

    def test_admin_create_oauth_obo_happy_path(self, client):
        """A well-formed oauth_obo row persists with its audience intact."""
        r = client.post(
            "/v1/api/admin/mcp-servers",
            json={
                "name": "obo-ok",
                "transport": "streamable-http",
                "url": "https://mcp.example.com/sse",
                "auth_type": "oauth_obo",
                "oauth_audience": "api://mcp-a",
            },
        )
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["auth_type"] == "oauth_obo"
        assert data["oauth_audience"] == "api://mcp-a"

    def test_same_type_static_edit_cannot_inject_oauth_columns(self, client, storage):
        """Review finding (SECURITY): the OAuth columns must be a pure function
        of the target auth_type on EVERY write, not just a flip. A same-type
        static edit that injects oauth_authorization_server_url must be scrubbed
        to NULL — otherwise a later flip to oauth_user (which legitimately uses
        that column) would inherit the attacker AS URL and redirect every
        consenting user's OAuth traffic."""
        r = client.post(
            "/v1/api/admin/mcp-servers",
            json={
                "name": "static-inject",
                "transport": "streamable-http",
                "url": "https://mcp.example.com/sse",
                "auth_type": "static",
            },
        )
        sid = r.json()["server_id"]
        # Same-type static edit trying to smuggle an oauth_user-only column.
        r2 = client.put(
            f"/v1/api/admin/mcp-servers/{sid}",
            json={
                "auth_type": "static",
                "oauth_authorization_server_url": "https://attacker.example",
            },
        )
        assert r2.status_code == 200, r2.text
        row = storage.get_mcp_server(sid)
        assert (row.get("oauth_authorization_server_url") or None) is None

    def test_flip_to_oauth_user_does_not_inherit_stale_as_url(self, client, storage):
        """Review finding (SECURITY): flipping a non-oauth_user row to oauth_user
        must recompute the oauth_user-only columns from the request, never
        inherit a stale/injected authorization_server_url left on the pre-flip
        row (defence-in-depth for a value that predates the unconditional
        scrub)."""
        # Plant a static row that already carries a stale AS URL directly in DB.
        storage.create_mcp_server(
            server_id="stale-asurl-id",
            name="stale-asurl",
            transport="streamable-http",
            url="https://mcp.example.com/sse",
            auth_type="static",
            oauth_authorization_server_url="https://attacker.example",
        )
        # Flip to oauth_user WITHOUT supplying an AS URL in the body.
        r = client.put(
            "/v1/api/admin/mcp-servers/stale-asurl-id",
            json={"auth_type": "oauth_user", "oauth_client_id": "cli_x"},
        )
        assert r.status_code == 200, r.text
        row = storage.get_mcp_server("stale-asurl-id")
        assert (row.get("oauth_authorization_server_url") or None) is None
        assert row.get("oauth_client_id") == "cli_x"

    def test_create_obo_rejected_when_capture_disabled(self, client):
        """Review finding: oauth_obo mints from the user's CAPTURED sign-in
        credential, so with capture_user_credential off, login persists nothing
        and every dispatch returns kind='missing' with an unsatisfiable remedy.
        Reject at write time."""
        client.app.state.oidc_config = SimpleNamespace(
            enabled=True,
            issuer="https://idp.example.com",
            obo_grant_profile="entra",
            capture_user_credential=False,
        )
        r = client.post(
            "/v1/api/admin/mcp-servers",
            json={
                "name": "obo-no-capture",
                "transport": "streamable-http",
                "url": "https://mcp.example.com/sse",
                "auth_type": "oauth_obo",
                "oauth_audience": "api://mcp-a",
            },
        )
        assert r.status_code == 400, r.text
        assert "capture_user_credential" in r.json()["error"]

    def test_create_obo_rejected_when_oidc_disabled(self, client):
        """Review finding: oauth_obo mints from the user's OIDC sign-in, so an
        install with OIDC disabled can NEVER mint. Reject at write time (a
        permanent misconfig otherwise surfaces per-dispatch as a retryable
        transient that never heals)."""
        client.app.state.oidc_config = SimpleNamespace(enabled=False, obo_grant_profile="entra")
        r = client.post(
            "/v1/api/admin/mcp-servers",
            json={
                "name": "obo-no-oidc",
                "transport": "streamable-http",
                "url": "https://mcp.example.com/sse",
                "auth_type": "oauth_obo",
                "oauth_audience": "api://mcp-a",
            },
        )
        assert r.status_code == 400, r.text
        assert "OIDC" in r.json()["error"]

    def test_obo_editable_when_oidc_discovery_transiently_failed(self, client, storage):
        """Review finding: the console never runs runtime OIDC rediscovery, so a
        transient discovery failure at console boot (enabled=False,
        discovery_retryable=True) must NOT make oauth_obo servers un-editable /
        un-disable-able. OIDC is still CONFIGURED (issuer set) — the write gate
        accepts a discovery_retryable config; it rejects only a genuinely absent
        OIDC (neither flag set)."""
        # Seed an obo row (created while OIDC was healthy).
        storage.create_mcp_server(
            server_id="obo-retry-id",
            name="obo-retry",
            transport="streamable-http",
            url="https://mcp.example.com/sse",
            auth_type="oauth_obo",
            oauth_audience="api://mcp-a",
        )
        # Console process booted while the IdP was briefly unreachable.
        client.app.state.oidc_config = SimpleNamespace(
            enabled=False,
            issuer="https://idp.example.com",
            obo_grant_profile="entra",
            capture_user_credential=True,
            discovery_retryable=True,
        )
        # Disabling the misbehaving obo server must succeed, not 400.
        r = client.put(
            "/v1/api/admin/mcp-servers/obo-retry-id",
            json={"enabled": False},
        )
        assert r.status_code == 200, r.text
        assert r.json()["enabled"] is False

        # Even with OIDC fully operator-disabled (neither flag set), a same-type
        # edit of the EXISTING obo server is still allowed — the deployment
        # checks only fire on create / flip-into-obo, so an operator is never
        # locked out of disabling or editing a server (review finding R8-1).
        client.app.state.oidc_config = SimpleNamespace(
            enabled=False,
            issuer="",
            obo_grant_profile="entra",
            capture_user_credential=True,
            discovery_retryable=False,
        )
        r2 = client.put(
            "/v1/api/admin/mcp-servers/obo-retry-id",
            json={"enabled": True},
        )
        assert r2.status_code == 200, r2.text

    def test_create_new_obo_still_rejected_when_oidc_operator_disabled(self, client):
        """The deployment gate still fires for a NEW obo enablement: creating a
        fresh oauth_obo server (or flipping one into obo) while OIDC is fully
        operator-disabled is rejected — only same-type edits of an existing obo
        server skip the deployment checks."""
        client.app.state.oidc_config = SimpleNamespace(
            enabled=False,
            issuer="",
            obo_grant_profile="entra",
            capture_user_credential=True,
            discovery_retryable=False,
        )
        r = client.post(
            "/v1/api/admin/mcp-servers",
            json={
                "name": "obo-new-nooidc",
                "transport": "streamable-http",
                "url": "https://mcp.example.com/sse",
                "auth_type": "oauth_obo",
                "oauth_audience": "api://mcp-a",
            },
        )
        assert r.status_code == 400, r.text
        assert "OIDC" in r.json()["error"]

    def test_create_obo_rejected_on_invalid_grant_profile(self, client):
        """Review finding: a typo'd deployment obo_grant_profile leaves the mint
        leg unresolved (obo_misconfigured per dispatch), so reject it at the
        write choke point rather than as a runtime transient."""
        client.app.state.oidc_config = _enabled_oidc("bogus-profile")
        r = client.post(
            "/v1/api/admin/mcp-servers",
            json={
                "name": "obo-bad-profile",
                "transport": "streamable-http",
                "url": "https://mcp.example.com/sse",
                "auth_type": "oauth_obo",
                "oauth_audience": "api://mcp-a",
            },
        )
        assert r.status_code == 400, r.text
        assert "obo_grant_profile" in r.json()["error"]

    def test_flip_user_to_obo_via_api_without_audience_is_rejected(self, client, storage):
        """Review finding: a flip into obo must NOT carry the oauth_user-era
        oauth_audience (a resource indicator, conventionally the MCP URL) — it
        would pass the audience-required check and then fail every mint. An API
        PUT of just {auth_type: oauth_obo} recomputes audience from the body
        (absent → NULL) and is rejected loudly, not saved with the stale value."""
        r = client.post(
            "/v1/api/admin/mcp-servers",
            json={
                "name": "flip-api-noaud",
                "transport": "streamable-http",
                "url": "https://mcp.example.com/sse",
                "auth_type": "oauth_user",
                "oauth_client_id": "cli_x",
                "oauth_audience": "https://mcp.example.com/sse",  # resource indicator
            },
        )
        sid = r.json()["server_id"]
        r2 = client.put(
            f"/v1/api/admin/mcp-servers/{sid}",
            json={"auth_type": "oauth_obo"},  # no audience in body
        )
        assert r2.status_code == 400, r2.text
        assert "oauth_audience" in r2.json()["error"]

    def test_update_flip_oauth_user_to_obo_keeps_audience_and_purges_tokens(self, client, storage):
        """#551 (findings 10344 + 10326): flipping oauth_user→oauth_obo must NOT
        null oauth_audience (the mint engine needs it), and MUST purge the old
        per-user consent-token rows (they carry per-server-AS refresh tokens that
        the mint cache invariant forbids)."""
        import sqlalchemy as sa

        from turnstone.core.storage._schema import mcp_user_tokens

        r = client.post(
            "/v1/api/admin/mcp-servers",
            json={
                "name": "flip-to-obo",
                "transport": "streamable-http",
                "url": "https://mcp.example.com/sse",
                "auth_type": "oauth_user",
                "oauth_client_id": "cli_x",
                "oauth_audience": "api://mcp-a",
            },
        )
        assert r.status_code == 200, r.text
        sid = r.json()["server_id"]

        with storage._engine.connect() as conn:
            conn.execute(
                sa.insert(mcp_user_tokens),
                {
                    "user_id": "u1",
                    "server_name": "flip-to-obo",
                    "access_token_ct": b"\x00ct-a",
                    "refresh_token_ct": b"\x00ct-r",
                    "expires_at": "2026-12-31T00:00:00",
                    "scopes": "openid",
                    "as_issuer": "https://auth.example.com",
                    "audience": "api://mcp-a",
                    "created": "2026-05-04T11:00:00",
                    "last_refreshed": None,
                },
            )
            conn.commit()

        r2 = client.put(
            f"/v1/api/admin/mcp-servers/{sid}",
            json={"auth_type": "oauth_obo", "oauth_audience": "api://mcp-a"},
        )
        assert r2.status_code == 200, r2.text
        data = r2.json()
        assert data["auth_type"] == "oauth_obo"
        assert data["oauth_audience"] == "api://mcp-a"  # NOT nulled
        # The oauth_user-only client_id is cleared.
        assert data["oauth_client_id"] in (None, "")
        # Old consent-token rows purged.
        with storage._engine.connect() as conn:
            remaining = conn.execute(
                sa.select(sa.func.count())
                .select_from(mcp_user_tokens)
                .where(mcp_user_tokens.c.server_name == "flip-to-obo")
            ).scalar()
        assert remaining == 0, "oauth_user→oauth_obo flip must purge stale per-user rows"

    def test_flip_to_obo_clears_stale_oauth_user_scopes(self, client):
        """#551 follow-up: flipping oauth_user→oauth_obo without supplying new
        scopes must CLEAR the old AS-consent scopes — otherwise the rfc8693 mint
        leg would send them and loop on invalid_scope."""
        r = client.post(
            "/v1/api/admin/mcp-servers",
            json={
                "name": "flip-scopes",
                "transport": "streamable-http",
                "url": "https://mcp.example.com/sse",
                "auth_type": "oauth_user",
                "oauth_scopes": "openid profile offline_access",
                "oauth_audience": "api://mcp-a",
            },
        )
        sid = r.json()["server_id"]
        r2 = client.put(
            f"/v1/api/admin/mcp-servers/{sid}",
            json={"auth_type": "oauth_obo", "oauth_audience": "api://mcp-a"},
        )
        assert r2.status_code == 200, r2.text
        assert r2.json()["oauth_scopes"] in (None, "")  # stale scopes cleared

    def _create_oauth_user_row_with_scopes(self, client, name: str) -> str:
        r = client.post(
            "/v1/api/admin/mcp-servers",
            json={
                "name": name,
                "transport": "streamable-http",
                "url": "https://mcp.example.com/sse",
                "auth_type": "oauth_user",
                "oauth_scopes": "openid profile offline_access",
                "oauth_audience": "api://mcp-a",
            },
        )
        sid: str = r.json()["server_id"]
        return sid

    def test_flip_to_obo_under_entra_rejects_explicit_scopes_but_omit_clears(self, client):
        """Redesign: a flip into obo recomputes scopes from the body (never
        carries the old row's value across the semantic boundary). Under entra,
        an EXPLICIT non-empty scopes value is rejected 400 — an honest visible
        snap rather than a silent drop — while the console-realistic flip (the
        form clears the semantic field on the auth-type switch, so scopes is
        omitted/empty) succeeds with scopes NULL."""
        client.app.state.oidc_config = _enabled_oidc("entra")
        # Explicit non-empty scopes on the flip → 400 (they can't apply on entra).
        sid = self._create_oauth_user_row_with_scopes(client, "flip-resend-entra")
        rejected = client.put(
            f"/v1/api/admin/mcp-servers/{sid}",
            json={
                "auth_type": "oauth_obo",
                "oauth_audience": "api://mcp-a",
                "oauth_scopes": "openid profile offline_access",
            },
        )
        assert rejected.status_code == 400
        assert "entra" in rejected.json()["error"]
        # The realistic flip (scopes field cleared → omitted) succeeds, NULL scopes.
        sid2 = self._create_oauth_user_row_with_scopes(client, "flip-omit-entra")
        ok = client.put(
            f"/v1/api/admin/mcp-servers/{sid2}",
            json={"auth_type": "oauth_obo", "oauth_audience": "api://mcp-a"},
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["oauth_scopes"] in (None, "")  # not carried across the flip

    def test_flip_to_obo_resent_scopes_kept_under_rfc8693(self, client):
        """Review finding: under rfc8693 oauth_scopes IS the token-exchange
        scope — an operator flipping to obo and keeping the same value (the
        Keycloak optional-audience scope can legitimately equal the old
        consent scope string) must NOT have it silently nulled; only an
        omitted field clears (previous test)."""
        client.app.state.oidc_config = _enabled_oidc("rfc8693")
        sid = self._create_oauth_user_row_with_scopes(client, "flip-resend-rfc")
        r2 = client.put(
            f"/v1/api/admin/mcp-servers/{sid}",
            json={
                "auth_type": "oauth_obo",
                "oauth_audience": "api://mcp-a",
                "oauth_scopes": "openid profile offline_access",
            },
        )
        assert r2.status_code == 200, r2.text
        assert r2.json()["oauth_scopes"] == "openid profile offline_access"

    def test_entra_obo_row_with_scopes_stays_editable(self, client, storage):
        """Review finding: a pre-existing oauth_obo row carrying scopes under the
        entra profile must stay editable — an unrelated PUT that doesn't touch
        scopes must NOT be rejected (the entra-scope reject fires only on a real
        scopes write)."""
        # Seed an obo row that already has scopes (e.g. created under rfc8693).
        storage.create_mcp_server(
            server_id="entra-edit-id",
            name="entra-edit",
            transport="streamable-http",
            url="https://mcp.example.com/sse",
            auth_type="oauth_obo",
            oauth_audience="api://mcp-a",
            oauth_scopes="custom.scope",
        )
        # Now the deployment is on the entra profile.
        client.app.state.oidc_config = _enabled_oidc("entra")

        # An unrelated maintenance edit (disable) — does NOT touch scopes.
        r = client.put(
            "/v1/api/admin/mcp-servers/entra-edit-id",
            json={"enabled": False},
        )
        assert r.status_code == 200, r.text  # NOT a 400 lockout

        # But actively SETTING scopes under entra is still rejected.
        r2 = client.put(
            "/v1/api/admin/mcp-servers/entra-edit-id",
            json={"oauth_scopes": "another.scope"},
        )
        assert r2.status_code == 400, r2.text
        assert "oauth_scopes" in r2.json()["error"]

    def test_obo_server_reports_consented_users_count_for_flush_button(self, client, storage):
        """Review finding: obo rows must report consented_users_count (users with a
        minted cache row) so the console flush-cache action (gated on count>0)
        renders — previously only oauth_user rows got the count."""
        storage.create_mcp_server(
            server_id="obo-count-id",
            name="obo-count",
            transport="streamable-http",
            url="https://mcp.example.com/sse",
            auth_type="oauth_obo",
            oauth_audience="api://mcp-a",
        )
        for i in range(2):
            storage.create_mcp_user_token(
                f"u{i}",
                "obo-count",
                access_token_ct=b"\x00ct",
                refresh_token_ct=None,
                expires_at="2026-12-31T00:00:00",
                scopes=None,
                as_issuer="https://idp.test",
                audience="api://mcp-a",
            )

        # The list handler fans out node status; no cluster nodes in this test.
        client.app.state.collector = SimpleNamespace(get_all_nodes=lambda: [])
        client.app.state.proxy_client = MagicMock()
        r = client.get("/v1/api/admin/mcp-servers")
        assert r.status_code == 200, r.text
        row = next(s for s in r.json()["servers"] if s["name"] == "obo-count")
        assert row["consented_users_count"] == 2

    def test_obo_audience_change_purges_cached_tokens(self, client, storage):
        """#551 follow-up: changing an obo row's oauth_audience purges cached
        tokens minted for the OLD audience (they are audience-bound)."""
        import sqlalchemy as sa

        from turnstone.core.storage._schema import mcp_user_tokens

        r = client.post(
            "/v1/api/admin/mcp-servers",
            json={
                "name": "aud-change",
                "transport": "streamable-http",
                "url": "https://mcp.example.com/sse",
                "auth_type": "oauth_obo",
                "oauth_audience": "api://old-aud",
            },
        )
        sid = r.json()["server_id"]
        with storage._engine.connect() as conn:
            conn.execute(
                sa.insert(mcp_user_tokens),
                {
                    "user_id": "u1",
                    "server_name": "aud-change",
                    "access_token_ct": b"\x00ct",
                    "refresh_token_ct": None,
                    "expires_at": "2026-12-31T00:00:00",
                    "scopes": None,
                    "as_issuer": "https://idp.test",
                    "audience": "api://old-aud",
                    "created": "2026-05-04T11:00:00",
                    "last_refreshed": None,
                },
            )
            conn.commit()

        r2 = client.put(
            f"/v1/api/admin/mcp-servers/{sid}",
            json={"oauth_audience": "api://new-aud"},
        )
        assert r2.status_code == 200, r2.text
        with storage._engine.connect() as conn:
            remaining = conn.execute(
                sa.select(sa.func.count())
                .select_from(mcp_user_tokens)
                .where(mcp_user_tokens.c.server_name == "aud-change")
            ).scalar()
        assert remaining == 0, "audience change must purge old-audience cache rows"

    def _seed_obo_row_with_cache(self, client, storage, *, name: str, scopes: str | None) -> str:
        import sqlalchemy as sa

        from turnstone.core.storage._schema import mcp_user_tokens

        r = client.post(
            "/v1/api/admin/mcp-servers",
            json={
                "name": name,
                "transport": "streamable-http",
                "url": "https://mcp.example.com/sse",
                "auth_type": "oauth_obo",
                "oauth_audience": "api://aud",
                **({"oauth_scopes": scopes} if scopes else {}),
            },
        )
        sid: str = r.json()["server_id"]
        with storage._engine.connect() as conn:
            conn.execute(
                sa.insert(mcp_user_tokens),
                {
                    "user_id": "u1",
                    "server_name": name,
                    "access_token_ct": b"\x00ct",
                    "refresh_token_ct": None,
                    "expires_at": "2026-12-31T00:00:00",
                    "scopes": scopes,
                    "as_issuer": "https://idp.test",
                    "audience": "api://aud",
                    "created": "2026-05-04T11:00:00",
                    "last_refreshed": None,
                },
            )
            conn.commit()
        return sid

    def _count_cache_rows(self, storage, name: str) -> int:
        import sqlalchemy as sa

        from turnstone.core.storage._schema import mcp_user_tokens

        with storage._engine.connect() as conn:
            count = conn.execute(
                sa.select(sa.func.count())
                .select_from(mcp_user_tokens)
                .where(mcp_user_tokens.c.server_name == name)
            ).scalar()
        return int(count or 0)

    def test_obo_scope_change_purges_cached_tokens(self, client, storage):
        """Review finding: under rfc8693 the exchange scope shapes the minted
        bearer's privileges exactly like the audience does — narrowing
        oauth_scopes must purge cached rows or the reduction silently waits
        out the token TTL (inconsistent with the audience purge)."""
        client.app.state.oidc_config = _enabled_oidc("rfc8693")
        sid = self._seed_obo_row_with_cache(
            client, storage, name="scope-change", scopes="api.read api.write"
        )
        r2 = client.put(
            f"/v1/api/admin/mcp-servers/{sid}",
            json={"oauth_scopes": "api.read"},
        )
        assert r2.status_code == 200, r2.text
        assert self._count_cache_rows(storage, "scope-change") == 0, (
            "scope change must purge cache rows minted with the old scopes"
        )

    def test_obo_scope_noop_resend_does_not_purge(self, client, storage):
        """Review finding companion: the admin form re-submits the pre-filled
        scopes on every save — an EQUAL value is normalized out of the update
        and must not flush every user's minted tokens."""
        client.app.state.oidc_config = _enabled_oidc("rfc8693")
        sid = self._seed_obo_row_with_cache(client, storage, name="scope-noop", scopes="api.read")
        r2 = client.put(
            f"/v1/api/admin/mcp-servers/{sid}",
            json={"oauth_scopes": "api.read", "enabled": True},
        )
        assert r2.status_code == 200, r2.text
        assert self._count_cache_rows(storage, "scope-noop") == 1, (
            "a no-op scopes re-send must not purge the mint cache"
        )

    def test_flip_obo_to_oauth_user_clears_obo_audience_and_scopes(self, client, storage):
        """Review finding: the obo-era oauth_audience is an IdP-side app
        identifier, not the resource indicator oauth_user sends to its AS —
        carried over, every consent yields a wrong-resource token that 401s
        with no visible cause. The flip must clear it (and the rfc8693
        exchange scopes) unless the request explicitly sets new values."""
        client.app.state.oidc_config = _enabled_oidc("rfc8693")
        sid = self._seed_obo_row_with_cache(client, storage, name="flip-back", scopes="api.read")
        r2 = client.put(
            f"/v1/api/admin/mcp-servers/{sid}",
            json={"auth_type": "oauth_user", "oauth_client_id": "client-xyz"},
        )
        assert r2.status_code == 200, r2.text
        data = r2.json()
        assert data["auth_type"] == "oauth_user"
        assert data["oauth_audience"] in (None, ""), "obo app-id audience must not carry over"
        assert data["oauth_scopes"] in (None, ""), "rfc8693 exchange scopes must not carry over"
        # The flip is an auth-model change → mint-cache rows purged too.
        assert self._count_cache_rows(storage, "flip-back") == 0

    def test_entra_obo_equal_scope_resend_is_accepted(self, client, storage):
        """Review finding: the admin form always re-submits the pre-filled
        oauth_scopes, so a same-type edit of an entra-profile obo row carrying
        legacy scopes must accept an EQUAL value (normalized to a no-op)
        instead of 400ing — only a genuine scope CHANGE is rejected."""
        # The legacy-scoped entra row arises from a deployment profile switch:
        # the row is created while the profile is rfc8693 (scopes accepted),
        # then the deployment flips to entra.
        client.app.state.oidc_config = _enabled_oidc("rfc8693")
        sid = self._seed_obo_row_with_cache(
            client, storage, name="entra-resend", scopes="legacy.scope"
        )
        client.app.state.oidc_config = _enabled_oidc("entra")
        # Equal re-send + unrelated change → accepted, scopes untouched.
        r2 = client.put(
            f"/v1/api/admin/mcp-servers/{sid}",
            json={"oauth_scopes": "legacy.scope", "enabled": False},
        )
        assert r2.status_code == 200, r2.text
        assert r2.json()["oauth_scopes"] == "legacy.scope"
        # A genuine CHANGE to non-empty scopes still 400s under entra.
        r3 = client.put(
            f"/v1/api/admin/mcp-servers/{sid}",
            json={"oauth_scopes": "new.scope"},
        )
        assert r3.status_code == 400
        assert "entra" in r3.json()["error"]

    def test_create_obo_rejects_scopes_under_entra_profile(self, client):
        """#551 follow-up: oauth_scopes is meaningless for the entra grant leg
        (it mints <audience>/.default), so the write path rejects it rather than
        silently ignoring it at mint time."""
        client.app.state.oidc_config = _enabled_oidc("entra")
        r = client.post(
            "/v1/api/admin/mcp-servers",
            json={
                "name": "obo-entra-scopes",
                "transport": "streamable-http",
                "url": "https://mcp.example.com/sse",
                "auth_type": "oauth_obo",
                "oauth_audience": "api://mcp-a",
                "oauth_scopes": "custom.scope",
            },
        )
        assert r.status_code == 400, r.text
        assert "oauth_scopes" in r.json()["error"]

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
    async def test_records_error_on_non_2xx(self):
        """A node replying non-2xx (e.g. 503) is recorded as an error, not
        counted as a reached node — raise_for_status() routes the status into
        the error path so a stale node trips the 'did not reach' WARNING, and
        the (unused) response body is never consulted."""
        http_req = httpx.Request("POST", "http://n1:8000/x")
        resp = MagicMock()
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "503", request=http_req, response=httpx.Response(503, request=http_req)
        )
        client = AsyncMock()
        client.post.return_value = resp
        req = _fake_request(
            {"node_id": "n1", "server_url": "http://n1:8000"},
            proxy_client=client,
        )
        result = await _notify_nodes_mcp_reload(req)
        assert "n1" in result
        assert "error" in result["n1"]
        resp.json.assert_not_called()

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

    def test_reload_fails_loud_without_fanout_infra(self, storage: SQLiteBackend) -> None:
        """F4 guard: the operator reload drains + reports, so with storage and
        admin.mcp permission but no collector/proxy_client on app.state it must
        fail loudly (500) — never silently 200 with empty results (which a
        re-introduced None-guard would do)."""
        app = Starlette(
            routes=_ROUTES,
            middleware=[Middleware(_InjectAuthMiddleware)],
        )
        app.state.auth_storage = storage
        # Deliberately omit app.state.collector / proxy_client.
        c = TestClient(app, raise_server_exceptions=False)
        r = c.post("/v1/api/admin/mcp-servers/reload")
        assert r.status_code == 500


class TestMcpWriteAutoReload:
    """create / update / delete schedule a node reload (after the 200) so a
    write reaches nodes — and active per-user pools re-prime — without a
    separate /reload. The fan-out rides only the success response; an error
    return schedules nothing. (The error paths tested here return before the
    row is written; a post-write secret-apply failure is a separate pre-existing
    partial-write path, not exercised here.)"""

    def test_create_notifies_nodes(self, client: TestClient) -> None:
        with patch(
            "turnstone.console.server._notify_nodes_mcp_reload",
            new_callable=AsyncMock,
            return_value={},
        ) as notify:
            _create_server(client, name="auto-reload-create")
        notify.assert_awaited_once()

    def test_update_notifies_nodes(self, client: TestClient) -> None:
        sid = _create_server(client, name="auto-reload-update")["server_id"]
        with patch(
            "turnstone.console.server._notify_nodes_mcp_reload",
            new_callable=AsyncMock,
            return_value={},
        ) as notify:
            r = client.put(f"/v1/api/admin/mcp-servers/{sid}", json={"enabled": False})
        assert r.status_code == 200
        notify.assert_awaited_once()

    def test_delete_notifies_nodes(self, client: TestClient) -> None:
        sid = _create_server(client, name="auto-reload-delete")["server_id"]
        with patch(
            "turnstone.console.server._notify_nodes_mcp_reload",
            new_callable=AsyncMock,
            return_value={},
        ) as notify:
            r = client.delete(f"/v1/api/admin/mcp-servers/{sid}")
        assert r.status_code == 200
        notify.assert_awaited_once()

    def test_delete_does_not_notify_on_missing_server(self, client: TestClient) -> None:
        """A 404 (server not found) returns before the success response, so no
        node reload is scheduled — the fan-out rides only the success path."""
        with patch(
            "turnstone.console.server._notify_nodes_mcp_reload",
            new_callable=AsyncMock,
            return_value={},
        ) as notify:
            r = client.delete("/v1/api/admin/mcp-servers/does-not-exist")
        assert r.status_code == 404
        notify.assert_not_awaited()

    def test_update_does_not_notify_on_missing_server(self, client: TestClient) -> None:
        """A 404 on update likewise schedules no reload."""
        with patch(
            "turnstone.console.server._notify_nodes_mcp_reload",
            new_callable=AsyncMock,
            return_value={},
        ) as notify:
            r = client.put("/v1/api/admin/mcp-servers/does-not-exist", json={"enabled": False})
        assert r.status_code == 404
        notify.assert_not_awaited()

    def test_create_does_not_notify_on_secret_store_503(
        self, client_no_token_store: TestClient
    ) -> None:
        """A create that 503s on the OAuth-secret token-store gate returns an
        error before any write — so no reload is scheduled."""
        with patch(
            "turnstone.console.server._notify_nodes_mcp_reload",
            new_callable=AsyncMock,
            return_value={},
        ) as notify:
            r = client_no_token_store.post(
                "/v1/api/admin/mcp-servers",
                json={
                    "name": "no-notify-503",
                    "transport": "streamable-http",
                    "url": "https://mcp.example.com/sse",
                    "auth_type": "oauth_user",
                    "oauth_client_id": "cli_abc",
                    "oauth_client_secret": "secret-value",
                },
            )
        assert r.status_code == 503, r.text
        notify.assert_not_awaited()

    def test_write_warns_when_reload_reaches_no_node(
        self, client: TestClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A background fan-out that leaves nodes unreached is surfaced at
        WARNING (not swallowed at debug) — operators need a signal the cluster
        catalog may be stale, since there is no periodic node reconcile."""
        with (
            patch(
                "turnstone.console.server._notify_nodes_mcp_reload",
                new_callable=AsyncMock,
                return_value={"n1": {"error": "Connection refused"}},
            ),
            caplog.at_level(logging.WARNING),
        ):
            _create_server(client, name="warn-on-stale")
        assert any("did not reach" in r.getMessage() for r in caplog.records)

    def test_write_warns_when_reload_fan_out_raises(
        self, client: TestClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A systemic fan-out fault (the whole reload raises) is logged at
        WARNING rather than lost, for the same reason."""
        with (
            patch(
                "turnstone.console.server._notify_nodes_mcp_reload",
                new_callable=AsyncMock,
                side_effect=RuntimeError("collector exploded"),
            ),
            caplog.at_level(logging.WARNING),
        ):
            _create_server(client, name="warn-on-fault")
        assert any("fan-out failed after admin write" in r.getMessage() for r in caplog.records)


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

    def test_refresh_one_skipped_returns_202(self, node_app_factory) -> None:
        # A busy-lock skip never ran the refresh — it must NOT be reported
        # as 200 "ok" (the caller would believe the catalog is current).
        # 202 Accepted + status "skipped": the health-tick retry will run it.
        mgr = MagicMock()
        mgr.refresh_sync.return_value = {"srv": None}
        # The endpoint reads the outcome from the manager accessor, not the
        # stripped status (the public projection whitelists it out).
        mgr.last_refresh_outcome.return_value = "skipped"
        mgr.get_server_status.return_value = {
            "connected": True,
            "tools": 3,
            "resources": 0,
            "prompts": 1,
            "error": "",
            "transport": "stdio",
            "command": "secret",
            "url": "",
            "circuit_open": False,
            "consecutive_failures": 0,
        }
        c = node_app_factory(mgr)
        r = c.post("/v1/api/_internal/mcp-refresh/srv")
        assert r.status_code == 202
        data = r.json()
        assert data["status"] == "skipped"
        assert "command" not in data["server"]  # stripped
        mgr.last_refresh_outcome.assert_called_with("srv")

    def test_refresh_one_error_beats_skip_returns_500(self, node_app_factory) -> None:
        # A skip on a server that ALSO carries a live error pill must
        # surface as 500, not a benign 202 — a status-code-keyed caller
        # would otherwise treat a genuinely erroring server as healthy.
        mgr = MagicMock()
        mgr.refresh_sync.return_value = {"srv": None}
        mgr.last_refresh_outcome.return_value = "skipped"
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
        assert r.status_code == 500, "a live error must win over the skip"
        assert r.json()["status"] == "error"

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


# ---------------------------------------------------------------------------
# Node fan-out status endpoint: GET /v1/api/_internal/mcp-status
# ---------------------------------------------------------------------------


class TestInternalMcpStatusEndpoint:
    """HTTP-level tests for the node-side aggregate status endpoint.

    The endpoint falls through to ``read`` scope (a deliberate choice
    so dashboards can render status indicators for non-admin
    operators). Because of that, the response must strip ``command``
    (stdio argv) and ``url`` (remote MCP endpoint) — both admin-only
    context — before it leaves the process.
    """

    @pytest.fixture()
    def node_app_factory(self, storage: SQLiteBackend):
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

    def test_status_strips_command_url_and_error(self, node_app_factory) -> None:
        mgr = MagicMock()
        mgr.get_all_server_status.return_value = {
            "srv-stdio": {
                "connected": False,
                "tools": 0,
                "resources": 0,
                "prompts": 0,
                # Error text would carry the binary path even after
                # ``command`` is stripped — read scope must not see it.
                "error": "FileNotFoundError: [Errno 2] No such file or "
                "directory: '/usr/local/bin/secret-mcp-bin'",
                "transport": "stdio",
                "command": ["/usr/local/bin/secret-mcp-bin", "--token", "abc"],
                "url": "",
                "circuit_open": True,
                "consecutive_failures": 5,
            },
            "srv-http": {
                "connected": True,
                "tools": 1,
                "resources": 1,
                "prompts": 0,
                "error": "",
                "transport": "streamable-http",
                "command": "",
                "url": "https://internal-mcp.example/mcp",
                "circuit_open": False,
                "consecutive_failures": 0,
            },
        }
        c = node_app_factory(mgr)
        r = c.get("/v1/api/_internal/mcp-status")
        assert r.status_code == 200
        servers = r.json()["servers"]
        assert set(servers) == {"srv-stdio", "srv-http"}
        for entry in servers.values():
            assert "command" not in entry
            assert "url" not in entry
            # ``error`` text is replaced by ``has_error`` boolean so a
            # FileNotFoundError binary path or httpx URL cannot leak
            # through verbose exception messages at read scope.
            assert "error" not in entry
            assert "has_error" in entry
        # Coarse error indicator preserved.
        assert servers["srv-stdio"]["has_error"] is True
        assert servers["srv-http"]["has_error"] is False
        # Operational fields preserved.
        assert servers["srv-http"]["tools"] == 1
        assert servers["srv-http"]["transport"] == "streamable-http"
        assert servers["srv-stdio"]["circuit_open"] is True
        # No leaked binary path anywhere in the rendered response.
        assert "secret-mcp-bin" not in r.text

    def test_status_no_mcp_client_returns_empty_servers(self, node_app_factory) -> None:
        c = node_app_factory(None)
        r = c.get("/v1/api/_internal/mcp-status")
        assert r.status_code == 200
        assert r.json() == {"servers": {}}

    def test_status_aggregate_gated_on_admin_mcp_permission(self, storage: SQLiteBackend) -> None:
        """oauth_user status is cross-user-aggregated ONLY for callers holding
        admin.mcp (the console cluster-health view). A read/approve user without
        it gets aggregate=False — strictly their own pool, the leak guard."""

        def _aggregate_arg(middleware_cls: type) -> Any:
            mgr = MagicMock()
            mgr.get_all_server_status.return_value = {}
            app = Starlette(
                routes=_routes_with_internal(),
                middleware=[Middleware(middleware_cls)],
            )
            app.state.auth_storage = storage
            app.state.mcp_client = mgr
            client = TestClient(app, raise_server_exceptions=False)
            assert client.get("/v1/api/_internal/mcp-status").status_code == 200
            return mgr.get_all_server_status.call_args

        admin_call = _aggregate_arg(_InjectAuthMiddleware)
        assert admin_call.kwargs.get("aggregate") is True

        user_call = _aggregate_arg(_InjectAuthNoMcpMiddleware)
        assert user_call.kwargs.get("aggregate") is False
