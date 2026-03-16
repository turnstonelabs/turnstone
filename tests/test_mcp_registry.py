"""Tests for MCP Registry client module."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from turnstone.core.mcp_registry import (
    MCPRegistryClient,
    MCPRegistryError,
    RegistryPackage,
    RegistryRemote,
    RegistryRemoteHeader,
    RegistryRemoteVariable,
    RegistryServer,
    resolve_install_config,
    sanitize_registry_name,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _registry_response(
    servers: list[dict[str, Any]],
    count: int | None = None,
    next_cursor: str | None = None,
) -> dict[str, Any]:
    """Build a registry API response dict."""
    return {
        "servers": servers,
        "metadata": {
            "count": count if count is not None else len(servers),
            "nextCursor": next_cursor,
        },
    }


def _server_entry(
    name: str = "io.example/test-server",
    description: str = "A test server",
    version: str = "1.0.0",
    remotes: list[dict[str, Any]] | None = None,
    packages: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a single server entry for the registry response."""
    entry: dict[str, Any] = {
        "server": {
            "name": name,
            "description": description,
            "version": version,
        },
        "_meta": {
            "io.modelcontextprotocol.registry/official": {
                "status": "active",
                "publishedAt": "2026-01-01T00:00:00Z",
                "updatedAt": "2026-01-01T00:00:00Z",
                "isLatest": True,
            }
        },
    }
    if remotes is not None:
        entry["server"]["remotes"] = remotes
    if packages is not None:
        entry["packages"] = packages
    return entry


# ---------------------------------------------------------------------------
# MCPRegistryClient.search
# ---------------------------------------------------------------------------


class TestMCPRegistryClientSearch:
    @pytest.mark.anyio
    async def test_basic_search(self) -> None:
        resp_data = _registry_response(
            [
                _server_entry(name="io.example/foo", description="Foo server"),
            ]
        )

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = resp_data

        async with MCPRegistryClient() as client:
            with patch.object(
                client._client, "get", new=AsyncMock(return_value=mock_response)
            ) as mock_get:
                result = await client.search(q="foo", limit=5)

        mock_get.assert_called_once()
        call_kwargs = mock_get.call_args
        assert call_kwargs[1]["params"]["search"] == "foo"
        assert call_kwargs[1]["params"]["limit"] == "5"
        assert call_kwargs[1]["params"]["latest"] == "true"

        assert len(result.servers) == 1
        assert result.servers[0].name == "io.example/foo"
        assert result.servers[0].description == "Foo server"

    @pytest.mark.anyio
    async def test_search_with_cursor(self) -> None:
        resp_data = _registry_response([], next_cursor="abc123")

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = resp_data

        async with MCPRegistryClient() as client:
            with patch.object(
                client._client, "get", new=AsyncMock(return_value=mock_response)
            ) as mock_get:
                result = await client.search(cursor="prev_cursor")

        assert mock_get.call_args[1]["params"]["cursor"] == "prev_cursor"
        assert result.next_cursor == "abc123"

    @pytest.mark.anyio
    async def test_search_parses_remotes(self) -> None:
        resp_data = _registry_response(
            [
                _server_entry(
                    remotes=[
                        {
                            "type": "streamable-http",
                            "url": "https://api.example.com/mcp",
                            "headers": [
                                {
                                    "name": "Authorization",
                                    "description": "Bearer token",
                                    "isRequired": True,
                                    "isSecret": True,
                                }
                            ],
                        }
                    ],
                ),
            ]
        )

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = resp_data

        async with MCPRegistryClient() as client:
            with patch.object(client._client, "get", new=AsyncMock(return_value=mock_response)):
                result = await client.search()

        srv = result.servers[0]
        assert len(srv.remotes) == 1
        assert srv.remotes[0].type == "streamable-http"
        assert srv.remotes[0].url == "https://api.example.com/mcp"
        assert srv.remotes[0].headers[0].name == "Authorization"
        assert srv.remotes[0].headers[0].is_secret is True

    @pytest.mark.anyio
    async def test_search_parses_packages(self) -> None:
        resp_data = _registry_response(
            [
                _server_entry(
                    packages=[
                        {
                            "registryType": "npm",
                            "identifier": "@example/server",
                            "version": "2.0.0",
                            "transport": {"type": "stdio"},
                            "environmentVariables": [
                                {
                                    "name": "API_KEY",
                                    "description": "Key",
                                    "isRequired": True,
                                    "isSecret": True,
                                }
                            ],
                        }
                    ],
                ),
            ]
        )

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = resp_data

        async with MCPRegistryClient() as client:
            with patch.object(client._client, "get", new=AsyncMock(return_value=mock_response)):
                result = await client.search()

        srv = result.servers[0]
        assert len(srv.packages) == 1
        assert srv.packages[0].registry_type == "npm"
        assert srv.packages[0].identifier == "@example/server"
        assert srv.packages[0].version == "2.0.0"
        assert srv.packages[0].environment_variables[0].name == "API_KEY"
        assert srv.packages[0].environment_variables[0].is_secret is True

    @pytest.mark.anyio
    async def test_search_parses_meta(self) -> None:
        resp_data = _registry_response([_server_entry()])

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = resp_data

        async with MCPRegistryClient() as client:
            with patch.object(client._client, "get", new=AsyncMock(return_value=mock_response)):
                result = await client.search()

        assert result.servers[0].meta is not None
        assert result.servers[0].meta.status == "active"
        assert result.servers[0].meta.is_latest is True

    @pytest.mark.anyio
    async def test_search_empty_response(self) -> None:
        resp_data = _registry_response([])

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = resp_data

        async with MCPRegistryClient() as client:
            with patch.object(client._client, "get", new=AsyncMock(return_value=mock_response)):
                result = await client.search(q="nonexistent")

        assert result.servers == []
        assert result.total_count == 0
        assert result.next_cursor is None

    @pytest.mark.anyio
    async def test_search_http_error(self) -> None:
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"

        async with MCPRegistryClient() as client:
            with patch.object(client._client, "get", new=AsyncMock(return_value=mock_response)):
                with pytest.raises(MCPRegistryError, match="500"):
                    await client.search()

    @pytest.mark.anyio
    async def test_search_limit_clamped(self) -> None:
        resp_data = _registry_response([])

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = resp_data

        async with MCPRegistryClient() as client:
            with patch.object(
                client._client, "get", new=AsyncMock(return_value=mock_response)
            ) as mock_get:
                await client.search(limit=200)

        assert mock_get.call_args[1]["params"]["limit"] == "100"

    @pytest.mark.anyio
    async def test_search_custom_base_url(self) -> None:
        resp_data = _registry_response([])

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = resp_data

        async with MCPRegistryClient(base_url="https://custom.registry.example.com") as client:
            with patch.object(
                client._client, "get", new=AsyncMock(return_value=mock_response)
            ) as mock_get:
                await client.search()

        call_url = mock_get.call_args[0][0]
        assert call_url.startswith("https://custom.registry.example.com")

    @pytest.mark.anyio
    async def test_search_defensive_parsing(self) -> None:
        """Handle unexpected shapes gracefully."""
        resp_data = {
            "servers": [
                {"server": {"name": "valid"}, "_meta": {}},
                "not-a-dict",  # should be skipped
                {"server": {}, "_meta": {}},  # missing name → empty string
            ],
            "metadata": {},
        }

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = resp_data

        async with MCPRegistryClient() as client:
            with patch.object(client._client, "get", new=AsyncMock(return_value=mock_response)):
                result = await client.search()

        # Non-dict entries should be filtered out
        assert len(result.servers) == 2
        assert result.servers[0].name == "valid"
        assert result.servers[1].name == ""


# ---------------------------------------------------------------------------
# resolve_install_config
# ---------------------------------------------------------------------------


class TestResolveInstallConfig:
    def test_remote_basic(self) -> None:
        server = RegistryServer(
            name="io.example/test",
            version="1.0.0",
            remotes=[
                RegistryRemote(
                    type="streamable-http",
                    url="https://api.example.com/mcp",
                )
            ],
        )
        config = resolve_install_config(server, "remote", 0)
        assert config["transport"] == "streamable-http"
        assert config["url"] == "https://api.example.com/mcp"
        assert config["registry_name"] == "io.example/test"
        assert config["registry_version"] == "1.0.0"

    def test_remote_with_headers(self) -> None:
        server = RegistryServer(
            name="io.example/test",
            version="1.0.0",
            remotes=[
                RegistryRemote(
                    type="streamable-http",
                    url="https://api.example.com/mcp",
                    headers=[
                        RegistryRemoteHeader(
                            name="Authorization", is_required=True, is_secret=True
                        ),
                    ],
                )
            ],
        )
        config = resolve_install_config(server, "remote", 0)
        assert "Authorization" in config["headers"]

    def test_remote_with_variable_substitution(self) -> None:
        server = RegistryServer(
            name="io.example/test",
            version="1.0.0",
            remotes=[
                RegistryRemote(
                    type="streamable-http",
                    url="https://{region}.api.example.com/mcp",
                    variables={
                        "region": RegistryRemoteVariable(
                            description="Region",
                            is_required=True,
                            choices=["us-east", "eu-west"],
                        ),
                    },
                )
            ],
        )
        config = resolve_install_config(server, "remote", 0, variables={"region": "us-east"})
        assert config["url"] == "https://us-east.api.example.com/mcp"

    def test_remote_missing_required_variable(self) -> None:
        server = RegistryServer(
            name="io.example/test",
            version="1.0.0",
            remotes=[
                RegistryRemote(
                    type="streamable-http",
                    url="https://{tenant}.example.com/mcp",
                    variables={
                        "tenant": RegistryRemoteVariable(is_required=True),
                    },
                )
            ],
        )
        with pytest.raises(MCPRegistryError, match="tenant"):
            resolve_install_config(server, "remote", 0)

    def test_remote_variable_default(self) -> None:
        server = RegistryServer(
            name="io.example/test",
            version="1.0.0",
            remotes=[
                RegistryRemote(
                    type="streamable-http",
                    url="https://{region}.example.com/mcp",
                    variables={
                        "region": RegistryRemoteVariable(is_required=True, default="us-east"),
                    },
                )
            ],
        )
        config = resolve_install_config(server, "remote", 0)
        assert config["url"] == "https://us-east.example.com/mcp"

    def test_remote_no_remotes(self) -> None:
        server = RegistryServer(name="io.example/test", version="1.0.0")
        with pytest.raises(MCPRegistryError, match="no remote"):
            resolve_install_config(server, "remote", 0)

    def test_remote_index_out_of_range(self) -> None:
        server = RegistryServer(
            name="io.example/test",
            version="1.0.0",
            remotes=[RegistryRemote(type="streamable-http", url="https://example.com")],
        )
        with pytest.raises(IndexError):
            resolve_install_config(server, "remote", 5)

    def test_package_npm(self) -> None:
        server = RegistryServer(
            name="io.example/test",
            version="1.0.0",
            packages=[
                RegistryPackage(
                    registry_type="npm",
                    identifier="@example/mcp-server",
                    version="2.0.0",
                )
            ],
        )
        config = resolve_install_config(server, "package", 0)
        assert config["transport"] == "stdio"
        assert config["command"] == "npx"
        assert config["args"] == ["-y", "@example/mcp-server@2.0.0"]

    def test_package_pypi(self) -> None:
        server = RegistryServer(
            name="io.example/test",
            version="1.0.0",
            packages=[
                RegistryPackage(
                    registry_type="pypi",
                    identifier="mcp-server-example",
                    version="1.5.0",
                )
            ],
        )
        config = resolve_install_config(server, "package", 0)
        assert config["transport"] == "stdio"
        assert config["command"] == "uvx"
        assert config["args"] == ["mcp-server-example==1.5.0"]

    def test_package_with_env_vars(self) -> None:
        from turnstone.core.mcp_registry import RegistryEnvVar

        server = RegistryServer(
            name="io.example/test",
            version="1.0.0",
            packages=[
                RegistryPackage(
                    registry_type="npm",
                    identifier="@example/server",
                    environment_variables=[
                        RegistryEnvVar(name="API_KEY", is_required=True, is_secret=True),
                        RegistryEnvVar(name="REGION", default="us-east"),
                    ],
                )
            ],
        )
        config = resolve_install_config(server, "package", 0)
        assert config["env"]["API_KEY"] == ""
        assert config["env"]["REGION"] == "us-east"

    def test_package_unsupported_type(self) -> None:
        server = RegistryServer(
            name="io.example/test",
            version="1.0.0",
            packages=[
                RegistryPackage(registry_type="oci", identifier="docker.io/example/server:1.0")
            ],
        )
        with pytest.raises(MCPRegistryError, match="Unsupported package type"):
            resolve_install_config(server, "package", 0)

    def test_package_no_packages(self) -> None:
        server = RegistryServer(name="io.example/test", version="1.0.0")
        with pytest.raises(MCPRegistryError, match="no installable"):
            resolve_install_config(server, "package", 0)

    def test_invalid_source(self) -> None:
        server = RegistryServer(name="io.example/test", version="1.0.0")
        with pytest.raises(MCPRegistryError, match="Invalid source"):
            resolve_install_config(server, "invalid", 0)

    def test_registry_meta_included(self) -> None:
        server = RegistryServer(
            name="io.example/test",
            description="A test server",
            title="Test Server",
            version="1.0.0",
            website_url="https://example.com",
            remotes=[RegistryRemote(type="streamable-http", url="https://example.com/mcp")],
        )
        config = resolve_install_config(server, "remote", 0)
        meta = config["registry_meta"]
        assert meta["description"] == "A test server"
        assert meta["title"] == "Test Server"
        assert meta["website_url"] == "https://example.com"

    def test_npm_no_duplicate_version(self) -> None:
        """Don't add @version if identifier already has it."""
        server = RegistryServer(
            name="io.example/test",
            version="1.0.0",
            packages=[
                RegistryPackage(
                    registry_type="npm",
                    identifier="@example/mcp-server@2.0.0",
                    version="2.0.0",
                )
            ],
        )
        config = resolve_install_config(server, "package", 0)
        assert config["args"] == ["-y", "@example/mcp-server@2.0.0"]

    def test_pypi_no_duplicate_version(self) -> None:
        """Don't add ==version if identifier already has it."""
        server = RegistryServer(
            name="io.example/test",
            version="1.0.0",
            packages=[
                RegistryPackage(
                    registry_type="pypi",
                    identifier="mcp-example==1.5.0",
                    version="1.5.0",
                )
            ],
        )
        config = resolve_install_config(server, "package", 0)
        assert config["args"] == ["mcp-example==1.5.0"]


# ---------------------------------------------------------------------------
# sanitize_registry_name
# ---------------------------------------------------------------------------


class TestSanitizeRegistryName:
    def test_basic_conversion(self) -> None:
        assert sanitize_registry_name("ai.example/mcp-server") == "ai.example.mcp-server"

    def test_strips_invalid_chars(self) -> None:
        assert sanitize_registry_name("ai.example/mcp server!") == "ai.example.mcpserver"

    def test_truncates_to_64(self) -> None:
        long_name = "a" * 100
        assert len(sanitize_registry_name(long_name)) <= 64

    def test_strips_leading_trailing(self) -> None:
        assert sanitize_registry_name(".leading-dot") == "leading-dot"
        assert sanitize_registry_name("trailing-dot.") == "trailing-dot"

    def test_empty_after_sanitization(self) -> None:
        with pytest.raises(MCPRegistryError):
            sanitize_registry_name("///")

    def test_reserved_double_underscore(self) -> None:
        with pytest.raises(MCPRegistryError, match="__"):
            sanitize_registry_name("foo__bar")
