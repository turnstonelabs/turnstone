"""Discovery tests for the per-(user, server) MCP OAuth flow.

Covers PRM (RFC 9728) and AS metadata (RFC 8414) discovery, including:
- override URL takes precedence
- PRM happy path: server URL -> .well-known/oauth-protected-resource
  -> ``authorization_servers[0]``
- PRM 401 + ``WWW-Authenticate: Bearer resource_metadata="..."`` follows
  the URL.
- AS metadata without S256 -> :class:`MCPOAuthDiscoveryError`.
- SSRF rejection on AS issuer URL.
- In-memory cache hit/miss + persistent cache write to
  ``mcp_servers.oauth_as_issuer_cached``.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from turnstone.core.mcp_oauth import (
    ASMetadata,
    MCPOAuthDiscoveryError,
    _parse_prm_url_from_www_authenticate,
    discover_authorization_server,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mk_response(
    status_code: int = 200,
    json_body: Any = None,
    headers: dict[str, str] | None = None,
) -> MagicMock:
    """Build a MagicMock that quacks like ``httpx.Response``."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.headers = headers or {}
    resp.content = (str(json_body) if json_body is not None else "").encode("utf-8")
    if json_body is not None:
        resp.json.return_value = json_body
    else:
        resp.json.side_effect = ValueError("no body")
    resp.text = str(json_body) if json_body is not None else ""
    return resp


def _good_as_metadata_doc() -> dict[str, Any]:
    return {
        "issuer": "https://as.example.com",
        "authorization_endpoint": "https://as.example.com/authorize",
        "token_endpoint": "https://as.example.com/token",
        "jwks_uri": "https://as.example.com/jwks",
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none", "client_secret_basic"],
        "registration_endpoint": "https://as.example.com/register",
    }


def _public_addr_patch():
    return patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 0))])


def _mk_storage_mock(server_id: str = "srv-id") -> MagicMock:
    storage = MagicMock()
    storage.update_mcp_server.return_value = True
    return storage


# ---------------------------------------------------------------------------
# PRM parser
# ---------------------------------------------------------------------------


class TestParsePRMUrl:
    def test_extracts_resource_metadata_url(self) -> None:
        header = (
            'Bearer error="invalid_token", '
            'resource_metadata="https://srv.example.com/.well-known/oauth-protected-resource"'
        )
        url = _parse_prm_url_from_www_authenticate(header)
        assert url == "https://srv.example.com/.well-known/oauth-protected-resource"

    def test_returns_none_when_absent(self) -> None:
        assert _parse_prm_url_from_www_authenticate('Bearer realm="x"') is None

    def test_handles_empty_header(self) -> None:
        assert _parse_prm_url_from_www_authenticate("") is None

    def test_handles_escaped_quote_in_value(self) -> None:
        """RFC 7230 quoted-string allows ``\\"`` — naive ``[^"]+`` truncates.

        A malicious or buggy resource server could send an embedded
        escaped quote; the parser must yield the unescaped value, not
        the prefix up to the escaped quote.
        """
        header = 'Bearer resource_metadata="https://srv.example.com/with\\"quote"'
        url = _parse_prm_url_from_www_authenticate(header)
        assert url == 'https://srv.example.com/with"quote'

    def test_handles_escaped_backslash(self) -> None:
        header = 'Bearer resource_metadata="https://srv.example.com/back\\\\slash"'
        url = _parse_prm_url_from_www_authenticate(header)
        assert url == "https://srv.example.com/back\\slash"

    def test_unterminated_quoted_string_returns_none(self) -> None:
        # Closing quote missing — naive regex would still match, but
        # the proper parser should reject malformed input.
        header = 'Bearer resource_metadata="https://srv.example.com/no-close'
        assert _parse_prm_url_from_www_authenticate(header) is None


# ---------------------------------------------------------------------------
# discover_authorization_server happy paths
# ---------------------------------------------------------------------------


class TestDiscoveryOverride:
    def test_override_url_skips_prm(self) -> None:
        client = MagicMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(return_value=_mk_response(200, _good_as_metadata_doc()))
        storage = _mk_storage_mock()

        async def _run():
            with _public_addr_patch():
                return await discover_authorization_server(
                    server_name="srv-x",
                    server_url="https://mcp.example.com/sse",
                    override_url="https://as.example.com",
                    cached_issuer=None,
                    http_client=client,
                    storage=storage,
                    server_id="srv-id",
                    trusted_hosts=frozenset(),
                )

        meta = asyncio.run(_run())
        assert isinstance(meta, ASMetadata)
        assert meta.token_endpoint == "https://as.example.com/token"
        # Only the AS metadata URL was hit, not PRM.
        called_urls = [c.args[0] for c in client.get.call_args_list]
        assert all("oauth-authorization-server" in u for u in called_urls)


class TestDiscoveryPRM:
    def test_prm_happy_path(self) -> None:
        async def _get(url, *args, **kwargs):
            if url.endswith("/oauth-protected-resource"):
                return _mk_response(
                    200,
                    {
                        "resource": "https://mcp.example.com",
                        "authorization_servers": ["https://as.example.com"],
                    },
                )
            if url.endswith("/oauth-authorization-server"):
                return _mk_response(200, _good_as_metadata_doc())
            raise AssertionError(f"unexpected URL: {url}")

        client = MagicMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(side_effect=_get)
        storage = _mk_storage_mock()

        async def _run():
            with _public_addr_patch():
                return await discover_authorization_server(
                    server_name="srv-x",
                    server_url="https://mcp.example.com/sse",
                    override_url=None,
                    cached_issuer=None,
                    http_client=client,
                    storage=storage,
                    server_id="srv-id",
                    trusted_hosts=frozenset(),
                )

        meta = asyncio.run(_run())
        assert meta.issuer == "https://as.example.com"

    def test_prm_401_follows_www_authenticate(self) -> None:
        async def _get(url, *args, **kwargs):
            if url == "https://mcp.example.com/.well-known/oauth-protected-resource":
                return _mk_response(
                    401,
                    headers={
                        "www-authenticate": (
                            'Bearer error="invalid_token", '
                            "resource_metadata="
                            '"https://meta.example.com/prm"'
                        )
                    },
                    json_body=None,
                )
            if url == "https://meta.example.com/prm":
                return _mk_response(
                    200,
                    {
                        "authorization_servers": ["https://as.example.com"],
                    },
                )
            if url.endswith("/oauth-authorization-server"):
                return _mk_response(200, _good_as_metadata_doc())
            raise AssertionError(f"unexpected URL: {url}")

        client = MagicMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(side_effect=_get)
        storage = _mk_storage_mock()

        async def _run():
            with _public_addr_patch():
                return await discover_authorization_server(
                    server_name="srv-x",
                    server_url="https://mcp.example.com/sse",
                    override_url=None,
                    cached_issuer=None,
                    http_client=client,
                    storage=storage,
                    server_id="srv-id",
                    trusted_hosts=frozenset(),
                )

        meta = asyncio.run(_run())
        assert meta.token_endpoint == "https://as.example.com/token"

    def test_prm_401_without_resource_metadata_raises(self) -> None:
        async def _get(url, *args, **kwargs):
            return _mk_response(401, headers={"www-authenticate": "Basic realm=x"})

        client = MagicMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(side_effect=_get)
        storage = _mk_storage_mock()

        async def _run():
            with _public_addr_patch():
                await discover_authorization_server(
                    server_name="srv-x",
                    server_url="https://mcp.example.com/sse",
                    override_url=None,
                    cached_issuer=None,
                    http_client=client,
                    storage=storage,
                    server_id="srv-id",
                    trusted_hosts=frozenset(),
                )

        with pytest.raises(MCPOAuthDiscoveryError, match="resource_metadata"):
            asyncio.run(_run())


# ---------------------------------------------------------------------------
# AS metadata validation
# ---------------------------------------------------------------------------


class TestASMetadataValidation:
    def test_no_s256_raises(self) -> None:
        doc = _good_as_metadata_doc()
        doc["code_challenge_methods_supported"] = ["plain"]

        client = MagicMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(return_value=_mk_response(200, doc))
        storage = _mk_storage_mock()

        async def _run():
            with _public_addr_patch():
                await discover_authorization_server(
                    server_name="srv-x",
                    server_url="https://mcp.example.com/sse",
                    override_url="https://as.example.com",
                    cached_issuer=None,
                    http_client=client,
                    storage=storage,
                    server_id="srv-id",
                    trusted_hosts=frozenset(),
                )

        with pytest.raises(MCPOAuthDiscoveryError, match="S256"):
            asyncio.run(_run())

    def test_missing_endpoints_raises(self) -> None:
        doc = _good_as_metadata_doc()
        del doc["token_endpoint"]

        client = MagicMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(return_value=_mk_response(200, doc))
        storage = _mk_storage_mock()

        async def _run():
            with _public_addr_patch():
                await discover_authorization_server(
                    server_name="srv-x",
                    server_url="https://mcp.example.com/sse",
                    override_url="https://as.example.com",
                    cached_issuer=None,
                    http_client=client,
                    storage=storage,
                    server_id="srv-id",
                    trusted_hosts=frozenset(),
                )

        with pytest.raises(MCPOAuthDiscoveryError, match="missing required"):
            asyncio.run(_run())

    def test_third_party_endpoint_rejected(self) -> None:
        doc = _good_as_metadata_doc()
        doc["token_endpoint"] = "https://attacker.example.com/token"

        client = MagicMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(return_value=_mk_response(200, doc))
        storage = _mk_storage_mock()

        async def _run():
            with _public_addr_patch():
                await discover_authorization_server(
                    server_name="srv-x",
                    server_url="https://mcp.example.com/sse",
                    override_url="https://as.example.com",
                    cached_issuer=None,
                    http_client=client,
                    storage=storage,
                    server_id="srv-id",
                    trusted_hosts=frozenset(),
                )

        with pytest.raises(MCPOAuthDiscoveryError, match="token_endpoint"):
            asyncio.run(_run())

    def test_ssrf_on_override_rejected(self) -> None:
        client = MagicMock(spec=httpx.AsyncClient)
        client.get = AsyncMock()
        storage = _mk_storage_mock()

        async def _run():
            # Resolve to private 10.x — SSRF guard fires before any HTTP call.
            with patch(
                "socket.getaddrinfo",
                return_value=[(2, 1, 6, "", ("10.0.0.1", 0))],
            ):
                await discover_authorization_server(
                    server_name="srv-x",
                    server_url="https://mcp.example.com/sse",
                    override_url="https://internal.corp.example.com",
                    cached_issuer=None,
                    http_client=client,
                    storage=storage,
                    server_id="srv-id",
                    trusted_hosts=frozenset(),
                )

        with pytest.raises(MCPOAuthDiscoveryError):
            asyncio.run(_run())
        client.get.assert_not_called()


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------


class TestMetadataCache:
    def test_cache_miss_then_hit(self) -> None:
        client = MagicMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(return_value=_mk_response(200, _good_as_metadata_doc()))
        storage = _mk_storage_mock()
        cache: dict[str, tuple[ASMetadata, float]] = {}

        async def _run():
            with _public_addr_patch():
                first = await discover_authorization_server(
                    server_name="srv-x",
                    server_url="https://mcp.example.com/sse",
                    override_url="https://as.example.com",
                    cached_issuer=None,
                    http_client=client,
                    storage=storage,
                    server_id="srv-id",
                    trusted_hosts=frozenset(),
                    metadata_cache=cache,
                )
                second = await discover_authorization_server(
                    server_name="srv-x",
                    server_url="https://mcp.example.com/sse",
                    override_url="https://as.example.com",
                    cached_issuer="https://as.example.com",
                    http_client=client,
                    storage=storage,
                    server_id="srv-id",
                    trusted_hosts=frozenset(),
                    metadata_cache=cache,
                )
                return first, second

        first, second = asyncio.run(_run())
        assert first.token_endpoint == second.token_endpoint
        # First call hit AS metadata; second call hit the cache.
        assert client.get.call_count == 1

    def test_cache_expiry_refetches(self) -> None:
        client = MagicMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(return_value=_mk_response(200, _good_as_metadata_doc()))
        storage = _mk_storage_mock()
        # Pre-populate cache with a very stale entry.
        stale_meta = ASMetadata(
            issuer="https://as.example.com",
            authorization_endpoint="https://as.example.com/authorize",
            token_endpoint="https://as.example.com/token",
            registration_endpoint=None,
            revocation_endpoint=None,
            jwks_uri=None,
            code_challenge_methods_supported=("S256",),
            token_endpoint_auth_methods_supported=(),
        )
        cache = {"https://as.example.com": (stale_meta, time.monotonic() - 10**6)}

        async def _run():
            with _public_addr_patch():
                return await discover_authorization_server(
                    server_name="srv-x",
                    server_url="https://mcp.example.com/sse",
                    override_url="https://as.example.com",
                    cached_issuer=None,
                    http_client=client,
                    storage=storage,
                    server_id="srv-id",
                    trusted_hosts=frozenset(),
                    metadata_cache=cache,
                )

        meta = asyncio.run(_run())
        # Stale entry was bypassed -> we hit the network.
        assert client.get.call_count == 1
        assert meta.token_endpoint == "https://as.example.com/token"

    def test_persistent_cache_write_on_first_resolution(self) -> None:
        async def _get(url, *args, **kwargs):
            if url.endswith("/oauth-protected-resource"):
                return _mk_response(200, {"authorization_servers": ["https://as.example.com"]})
            return _mk_response(200, _good_as_metadata_doc())

        client = MagicMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(side_effect=_get)
        storage = _mk_storage_mock()

        async def _run():
            with _public_addr_patch():
                return await discover_authorization_server(
                    server_name="srv-x",
                    server_url="https://mcp.example.com/sse",
                    override_url=None,
                    cached_issuer=None,
                    http_client=client,
                    storage=storage,
                    server_id="srv-id",
                    trusted_hosts=frozenset(),
                )

        asyncio.run(_run())
        # update_mcp_server was called once with the cached issuer.
        storage.update_mcp_server.assert_called_once_with(
            "srv-id", oauth_as_issuer_cached="https://as.example.com"
        )

    def test_persistent_cache_skip_when_already_cached(self) -> None:
        client = MagicMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(return_value=_mk_response(200, _good_as_metadata_doc()))
        storage = _mk_storage_mock()

        async def _run():
            with _public_addr_patch():
                return await discover_authorization_server(
                    server_name="srv-x",
                    server_url="https://mcp.example.com/sse",
                    override_url=None,
                    cached_issuer="https://as.example.com",
                    http_client=client,
                    storage=storage,
                    server_id="srv-id",
                    trusted_hosts=frozenset(),
                )

        asyncio.run(_run())
        storage.update_mcp_server.assert_not_called()


# ---------------------------------------------------------------------------
# sec-3 — cached_issuer re-validated on read
# ---------------------------------------------------------------------------


class TestCachedIssuerSSRFRevalidation:
    """A cached issuer URL must still pass SSRF validation on every read.

    Defense-in-depth: an admin who points ``oauth_as_issuer_cached`` at a
    private address (or a hostname that has rebound to one) should not
    bypass the guard just because the value was already in the row.
    """

    def test_cached_issuer_rejected_clears_row_and_falls_through_to_prm(self) -> None:
        async def _get(url: str, *args: Any, **kwargs: Any) -> MagicMock:
            if url.endswith("/oauth-protected-resource"):
                return _mk_response(200, {"authorization_servers": ["https://as.example.com"]})
            if url.endswith("/oauth-authorization-server"):
                return _mk_response(200, _good_as_metadata_doc())
            raise AssertionError(f"unexpected URL: {url}")

        client = MagicMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(side_effect=_get)
        storage = _mk_storage_mock()

        # cached_issuer points at a private host. SSRF guard fires on
        # the cached value first, the row is cleared, and PRM
        # discovery runs as a fallback.
        async def _run() -> Any:
            with patch(
                "socket.getaddrinfo",
                # Private resolution for "internal.corp", public for everything else.
                side_effect=lambda host, *a, **kw: [
                    (2, 1, 6, "", ("10.0.0.1" if "internal" in host else "93.184.216.34", 0))
                ],
            ):
                return await discover_authorization_server(
                    server_name="srv-x",
                    server_url="https://mcp.example.com/sse",
                    override_url=None,
                    cached_issuer="https://internal.corp.example.com",
                    http_client=client,
                    storage=storage,
                    server_id="srv-id",
                    trusted_hosts=frozenset(),
                )

        meta = asyncio.run(_run())
        assert meta.token_endpoint == "https://as.example.com/token"
        # The bad cached_issuer was cleared from the row.
        clear_calls = [
            c
            for c in storage.update_mcp_server.call_args_list
            if c.kwargs.get("oauth_as_issuer_cached") is None
        ]
        assert clear_calls, "cached_issuer should have been cleared"


# ---------------------------------------------------------------------------
# revocation_endpoint parsing (RFC 8414)
# ---------------------------------------------------------------------------


class TestASMetadataRevocationEndpoint:
    def test_as_metadata_parses_revocation_endpoint(self) -> None:
        doc = _good_as_metadata_doc()
        doc["revocation_endpoint"] = "https://as.example.com/revoke"

        client = MagicMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(return_value=_mk_response(200, doc))
        storage = _mk_storage_mock()

        async def _run() -> ASMetadata:
            with _public_addr_patch():
                return await discover_authorization_server(
                    server_name="srv-x",
                    server_url="https://mcp.example.com/sse",
                    override_url="https://as.example.com",
                    cached_issuer=None,
                    http_client=client,
                    storage=storage,
                    server_id="srv-id",
                    trusted_hosts=frozenset(),
                )

        meta = asyncio.run(_run())
        assert meta.revocation_endpoint == "https://as.example.com/revoke"

    def test_as_metadata_revocation_endpoint_absent(self) -> None:
        doc = _good_as_metadata_doc()
        doc.pop("revocation_endpoint", None)

        client = MagicMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(return_value=_mk_response(200, doc))
        storage = _mk_storage_mock()

        async def _run() -> ASMetadata:
            with _public_addr_patch():
                return await discover_authorization_server(
                    server_name="srv-x",
                    server_url="https://mcp.example.com/sse",
                    override_url="https://as.example.com",
                    cached_issuer=None,
                    http_client=client,
                    storage=storage,
                    server_id="srv-id",
                    trusted_hosts=frozenset(),
                )

        meta = asyncio.run(_run())
        assert meta.revocation_endpoint is None

    def test_as_metadata_revocation_endpoint_rejected_when_cross_origin(self) -> None:
        doc = _good_as_metadata_doc()
        doc["revocation_endpoint"] = "https://attacker.example.com/revoke"

        client = MagicMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(return_value=_mk_response(200, doc))
        storage = _mk_storage_mock()

        async def _run() -> ASMetadata:
            with _public_addr_patch():
                return await discover_authorization_server(
                    server_name="srv-x",
                    server_url="https://mcp.example.com/sse",
                    override_url="https://as.example.com",
                    cached_issuer=None,
                    http_client=client,
                    storage=storage,
                    server_id="srv-id",
                    trusted_hosts=frozenset(),
                )

        with pytest.raises(MCPOAuthDiscoveryError, match="revocation_endpoint"):
            asyncio.run(_run())
