"""Discovery tests for the per-(user, server) MCP OAuth flow.

Covers PRM (RFC 9728) and AS metadata (RFC 8414) discovery, including:
- override URL takes precedence
- PRM happy path: the server URL path is preserved in .well-known discovery
  -> ``authorization_servers[0]``
- A missing path-specific PRM falls back to the origin-level well-known URL.
- PRM ``resource`` identity is validated before its metadata is trusted.
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
    canonical_resource,
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


_SERVER = "https://mcp.example.com/sse"
_PATH_PRM = "https://mcp.example.com/.well-known/oauth-protected-resource/sse"
_ROOT_PRM = "https://mcp.example.com/.well-known/oauth-protected-resource"
_AS_META = "https://as.example.com/.well-known/oauth-authorization-server"


def _prm(resource: Any, issuer: str = "https://as.example.com") -> MagicMock:
    """A 200 protected-resource metadata response declaring *resource*."""
    return _mk_response(200, {"resource": resource, "authorization_servers": [issuer]})


def _challenge(resource_metadata_url: str) -> MagicMock:
    """A 401 whose ``WWW-Authenticate`` names *resource_metadata_url*."""
    return _mk_response(
        401,
        headers={"www-authenticate": f'Bearer resource_metadata="{resource_metadata_url}"'},
    )


def _router(routes: dict[str, Any]) -> Any:
    """``client.get`` side effect: exact-URL lookup, AssertionError for anything else."""

    async def _get(url: str, *args: Any, **kwargs: Any) -> Any:
        if url in routes:
            return routes[url]
        raise AssertionError(f"unexpected URL: {url}")

    return _get


def _urls(client: MagicMock) -> list[str]:
    return [call.args[0] for call in client.get.call_args_list]


def _discover(
    server_url: str,
    get: Any,
    *,
    override_url: str | None = None,
    cached_issuer: str | None = None,
) -> tuple[ASMetadata, MagicMock]:
    """Run discovery with a mocked client whose ``get`` is *get*; return (metadata, client)."""
    client = MagicMock(spec=httpx.AsyncClient)
    client.get = AsyncMock(side_effect=get)
    storage = _mk_storage_mock()

    async def _run() -> ASMetadata:
        with _public_addr_patch():
            return await discover_authorization_server(
                server_name="srv-x",
                server_url=server_url,
                override_url=override_url,
                cached_issuer=cached_issuer,
                http_client=client,
                storage=storage,
                server_id="srv-id",
                trusted_hosts=frozenset(),
            )

    return asyncio.run(_run()), client


def _discover_error(
    server_url: str,
    get: Any,
    match: str,
    *,
    override_url: str | None = None,
) -> MagicMock:
    """Run discovery expecting ``MCPOAuthDiscoveryError`` matching *match*; return the client."""
    client = MagicMock(spec=httpx.AsyncClient)
    client.get = AsyncMock(side_effect=get)
    storage = _mk_storage_mock()

    async def _run() -> None:
        with _public_addr_patch():
            await discover_authorization_server(
                server_name="srv-x",
                server_url=server_url,
                override_url=override_url,
                cached_issuer=None,
                http_client=client,
                storage=storage,
                server_id="srv-id",
                trusted_hosts=frozenset(),
            )

    with pytest.raises(MCPOAuthDiscoveryError, match=match):
        asyncio.run(_run())
    return client


def _private_addr_patch(addr: str = "192.168.1.50"):
    """Resolve every hostname to a private address."""
    return patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", (addr, 0))])


def _addr_map_patch(mapping: dict[str, list[str]], default: str = "93.184.216.34"):
    """Resolve each hostname to the addresses named for it.

    A single ``return_value`` cannot express "the server is private but its
    authorization server is not", which is the shape the opt-in exists to
    allow, nor a name answering with both a public and a private record.
    """

    def _getaddrinfo(host, *args, **kwargs):
        addrs = mapping.get(host, [default])
        return [(2, 1, 6, "", (a, 0)) for a in addrs]

    return patch("socket.getaddrinfo", side_effect=_getaddrinfo)


def _discover_private(
    server_url: str,
    get: Any,
    *,
    allow_private_network: bool,
    override_url: str | None = None,
    addr: str = "192.168.1.50",
) -> tuple[ASMetadata, MagicMock]:
    """Run discovery against private-resolving hosts; return (metadata, client)."""
    client = MagicMock(spec=httpx.AsyncClient)
    client.get = AsyncMock(side_effect=get)
    storage = _mk_storage_mock()

    async def _run() -> ASMetadata:
        with _private_addr_patch(addr):
            return await discover_authorization_server(
                server_name="srv-x",
                server_url=server_url,
                override_url=override_url,
                cached_issuer=None,
                http_client=client,
                storage=storage,
                server_id="srv-id",
                trusted_hosts=frozenset(),
                allow_private_network=allow_private_network,
            )

    return asyncio.run(_run()), client


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
            if url.endswith("/oauth-protected-resource/sse"):
                return _mk_response(
                    200,
                    {
                        "resource": "https://mcp.example.com/sse",
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

    def test_path_and_trailing_slash_are_preserved(self) -> None:
        # The shape of a hosted MCP server: the resource lives under a path
        # with a significant trailing slash, and its issuer lives under a path
        # on another host. Both well-known URLs insert their segment before
        # the path and keep the slash.
        server_url = "https://mcp.example.com/mcp/"
        issuer = "https://as.example.com/login/oauth"
        expected_prm_url = "https://mcp.example.com/.well-known/oauth-protected-resource/mcp/"
        expected_as_url = (
            "https://as.example.com/.well-known/oauth-authorization-server/login/oauth"
        )

        path_issuer_as_doc = {
            "issuer": issuer,
            "authorization_endpoint": "https://as.example.com/login/oauth/authorize",
            "token_endpoint": "https://as.example.com/login/oauth/access_token",
            "code_challenge_methods_supported": ["S256"],
        }

        async def _get(url, *args, **kwargs):
            if url == expected_prm_url:
                return _mk_response(
                    200,
                    {
                        "resource": server_url,
                        "authorization_servers": [issuer],
                    },
                )
            if url == expected_as_url:
                return _mk_response(200, path_issuer_as_doc)
            raise AssertionError(f"unexpected URL: {url}")

        client = MagicMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(side_effect=_get)
        storage = _mk_storage_mock()

        async def _run():
            with _public_addr_patch():
                return await discover_authorization_server(
                    server_name="srv-hosted",
                    server_url=server_url,
                    override_url=None,
                    cached_issuer=None,
                    http_client=client,
                    storage=storage,
                    server_id="srv-id",
                    trusted_hosts=frozenset(),
                )

        meta = asyncio.run(_run())
        assert meta.issuer == issuer
        assert [call.args[0] for call in client.get.call_args_list] == [
            expected_prm_url,
            expected_as_url,
        ]

    def test_path_404_falls_back_to_root_prm(self) -> None:
        server_url = "https://mcp.example.com/sse"
        path_prm_url = "https://mcp.example.com/.well-known/oauth-protected-resource/sse"
        root_prm_url = "https://mcp.example.com/.well-known/oauth-protected-resource"

        async def _get(url, *args, **kwargs):
            if url == path_prm_url:
                return _mk_response(404)
            if url == root_prm_url:
                return _mk_response(
                    200,
                    {
                        "resource": "https://mcp.example.com",
                        "authorization_servers": ["https://as.example.com"],
                    },
                )
            if url == "https://as.example.com/.well-known/oauth-authorization-server":
                return _mk_response(200, _good_as_metadata_doc())
            raise AssertionError(f"unexpected URL: {url}")

        client = MagicMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(side_effect=_get)
        storage = _mk_storage_mock()

        async def _run():
            with _public_addr_patch():
                return await discover_authorization_server(
                    server_name="srv-x",
                    server_url=server_url,
                    override_url=None,
                    cached_issuer=None,
                    http_client=client,
                    storage=storage,
                    server_id="srv-id",
                    trusted_hosts=frozenset(),
                )

        meta = asyncio.run(_run())
        assert meta.issuer == "https://as.example.com"
        assert [call.args[0] for call in client.get.call_args_list] == [
            path_prm_url,
            root_prm_url,
            "https://as.example.com/.well-known/oauth-authorization-server",
        ]

    @pytest.mark.parametrize(
        "declared",
        [
            "https://MCP.example.com/sse",
            "https://mcp.example.com:443/sse",
            "HTTPS://mcp.example.com/sse",
        ],
    )
    def test_path_document_accepts_case_and_port_variants(self, declared: str) -> None:
        # RFC 3986 §6.2.2.1 / §6.2.3 equivalents of the server URL are the
        # same resource; MCP tells clients to accept them.
        meta, client = _discover(
            _SERVER,
            _router(
                {_PATH_PRM: _prm(declared), _AS_META: _mk_response(200, _good_as_metadata_doc())}
            ),
        )
        assert meta.issuer == "https://as.example.com"
        assert _urls(client) == [_PATH_PRM, _AS_META]

    def test_root_only_server_url_accepts_bare_origin_document(self) -> None:
        # ``https://host/`` derives the origin-level PRM URL, whose document
        # naturally declares ``https://host``; the two must compare equal.
        meta, client = _discover(
            "https://mcp.example.com/",
            _router(
                {
                    _ROOT_PRM: _prm("https://mcp.example.com"),
                    _AS_META: _mk_response(200, _good_as_metadata_doc()),
                }
            ),
        )
        assert meta.issuer == "https://as.example.com"
        assert _urls(client) == [_ROOT_PRM, _AS_META]

    @pytest.mark.parametrize(
        "declared",
        [
            "https://mcp.example.com/",
            "https://mcp.example.com",
            "https://MCP.example.com:443",
        ],
    )
    def test_origin_fallback_accepts_origin_declarations(self, declared: str) -> None:
        # This URL was derived from the origin, so the origin is what the
        # document may declare (RFC 9728 §3.3) — in any spelling that
        # canonicalizes to it.
        meta, client = _discover(
            _SERVER,
            _router(
                {
                    _PATH_PRM: _mk_response(404),
                    _ROOT_PRM: _prm(declared),
                    _AS_META: _mk_response(200, _good_as_metadata_doc()),
                }
            ),
        )
        assert meta.issuer == "https://as.example.com"
        assert _urls(client) == [_PATH_PRM, _ROOT_PRM, _AS_META]

    @pytest.mark.parametrize(
        "declared", ["https://other.example.com/sse", "https://mcp.example.com/sse"]
    )
    def test_origin_fallback_rejects_non_origin_resource(self, declared: str) -> None:
        # Including this server's own path-bearing URL: the document was
        # retrieved from the origin location, so that is the only identifier
        # it may claim. A server that serves only here and declares its path
        # form predates the path-specific location; the remedy is the
        # Authorization Server URL override, which skips PRM entirely.
        _discover_error(
            _SERVER,
            _router({_PATH_PRM: _mk_response(404), _ROOT_PRM: _prm(declared)}),
            "resource identifier",
        )

    @pytest.mark.parametrize(
        "path_response",
        [
            _mk_response(403),
            _mk_response(405),
            _mk_response(400),
            _mk_response(200, json_body=None),
            _mk_response(401, headers={"www-authenticate": "Basic realm=x"}),
        ],
        ids=["403", "405", "400", "200-not-json", "401-no-challenge"],
    )
    def test_unusable_path_response_falls_back_to_origin(self, path_response: MagicMock) -> None:
        # A server that publishes only the origin-level document may answer
        # the path-specific probe with anything a gateway or framework emits;
        # none of those is a reason to skip the origin document.
        meta, client = _discover(
            _SERVER,
            _router(
                {
                    _PATH_PRM: path_response,
                    _ROOT_PRM: _prm("https://mcp.example.com"),
                    _AS_META: _mk_response(200, _good_as_metadata_doc()),
                }
            ),
        )
        assert meta.issuer == "https://as.example.com"
        assert _urls(client) == [_PATH_PRM, _ROOT_PRM, _AS_META]

    def test_path_document_declaring_origin_is_rejected(self) -> None:
        # RFC 9728 §3.3 binds the identifier to the URL it was derived from,
        # so the path-specific location must declare the path identifier. A
        # catch-all that serves the origin document from every path is not
        # accepted there — it is accepted at the origin location, one GET
        # later, which is where that document belongs.
        meta, client = _discover(
            _SERVER,
            _router(
                {
                    _PATH_PRM: _prm("https://mcp.example.com"),
                    _ROOT_PRM: _prm("https://mcp.example.com"),
                    _AS_META: _mk_response(200, _good_as_metadata_doc()),
                }
            ),
        )
        assert meta.issuer == "https://as.example.com"
        assert _urls(client) == [_PATH_PRM, _ROOT_PRM, _AS_META]

    def test_path_document_declaring_origin_with_no_origin_document_fails(self) -> None:
        _discover_error(
            _SERVER,
            _router({_PATH_PRM: _prm("https://mcp.example.com"), _ROOT_PRM: _mk_response(404)}),
            "resource identifier",
        )

    @pytest.mark.parametrize(
        "resource",
        [
            "  https://mcp.example.com/sse",
            "https://mcp.example.com/sse  ",
            "https://u:p@mcp.example.com/sse",
            "https://mcp.example.com/sse#other",
            "https://mcp.example.com/sse#",
            "https://mcp.example.com/s\nse",
            "https://mcp.exa\tmple.com/sse",
        ],
    )
    def test_declared_resource_is_parsed_strictly(self, resource: str) -> None:
        # The leniency that heals a row written before this validation existed
        # must not extend to an identifier a remote server just handed us:
        # each of these canonicalizes to the server URL, and none of them is
        # the identifier RFC 9728 §3.3 asks the document to name.
        _discover_error(
            _SERVER,
            _router({_PATH_PRM: _prm(resource), _ROOT_PRM: _mk_response(404)}),
            "not a valid resource URL",
        )

    @pytest.mark.parametrize("resource", [None, "https://mcp.example.com/other", 42])
    def test_path_document_with_wrong_resource_reports_document_error(self, resource: Any) -> None:
        # The origin candidate is still tried (and 404s here), but the error
        # an operator sees is the document-level one, not the later 404.
        with pytest.raises(MCPOAuthDiscoveryError, match="resource identifier"):
            _discover(
                _SERVER,
                _router({_PATH_PRM: _prm(resource), _ROOT_PRM: _mk_response(404)}),
            )

    def test_transport_error_on_path_probe_falls_back_to_origin(self) -> None:
        # A gateway that stalls or refuses the path-specific probe must not
        # hide an origin document that answers.
        routes = _router(
            {
                _ROOT_PRM: _prm("https://mcp.example.com"),
                _AS_META: _mk_response(200, _good_as_metadata_doc()),
            }
        )

        async def _get(url: str, *args: Any, **kwargs: Any) -> Any:
            if url == _PATH_PRM:
                raise httpx.ConnectError("refused")
            return await routes(url, *args, **kwargs)

        meta, client = _discover(_SERVER, _get)
        assert meta.issuer == "https://as.example.com"
        assert _urls(client) == [_PATH_PRM, _ROOT_PRM, _AS_META]

    def test_transport_error_on_every_candidate_reports_the_first(self) -> None:
        async def _get(url: str, *args: Any, **kwargs: Any) -> Any:
            raise httpx.ConnectError("refused")

        client = _discover_error(_SERVER, _get, "PRM fetch failed")
        assert _urls(client) == [_PATH_PRM, _ROOT_PRM]

    def test_missing_resource_names_the_field(self) -> None:
        _discover_error(
            _SERVER,
            _router({_PATH_PRM: _prm(None), _ROOT_PRM: _mk_response(404)}),
            "missing its resource identifier",
        )

    def test_first_status_error_is_reported(self) -> None:
        # The path probe's 401-without-challenge is the informative error; the
        # origin's plain 404 must not overwrite it.
        _discover_error(
            _SERVER,
            _router(
                {
                    _PATH_PRM: _mk_response(401, headers={"www-authenticate": "Basic realm=x"}),
                    _ROOT_PRM: _mk_response(404),
                }
            ),
            "without resource_metadata",
        )

    def test_prm_401_challenge_carries_the_challenged_expectation(self) -> None:
        # RFC 9728 §3.3: a document reached through a challenge names the
        # resource the client asked for, so a challenge off the path-specific
        # location must still declare the canonical server URL.
        meta, client = _discover(
            _SERVER,
            _router(
                {
                    _PATH_PRM: _challenge("https://meta.example.com/prm"),
                    "https://meta.example.com/prm": _prm(_SERVER),
                    _AS_META: _mk_response(200, _good_as_metadata_doc()),
                }
            ),
        )
        assert meta.issuer == "https://as.example.com"
        assert _urls(client) == [_PATH_PRM, "https://meta.example.com/prm", _AS_META]

    def test_challenge_document_declaring_origin_is_rejected(self) -> None:
        _discover_error(
            _SERVER,
            _router(
                {
                    _PATH_PRM: _challenge("https://meta.example.com/prm"),
                    "https://meta.example.com/prm": _prm("https://mcp.example.com"),
                    _ROOT_PRM: _mk_response(404),
                }
            ),
            "resource identifier",
        )

    def test_challenge_to_origin_is_rejudged_for_the_origin_candidate(self) -> None:
        # The challenge sends us to the origin URL carrying the PATH
        # location's expectation, which the origin document does not meet.
        # The origin candidate then judges that same response on its own
        # terms and accepts it — without a second request.
        meta, client = _discover(
            _SERVER,
            _router(
                {
                    _PATH_PRM: _challenge(_ROOT_PRM),
                    _ROOT_PRM: _prm("https://mcp.example.com"),
                    _AS_META: _mk_response(200, _good_as_metadata_doc()),
                }
            ),
        )
        assert meta.issuer == "https://as.example.com"
        assert _urls(client) == [_PATH_PRM, _ROOT_PRM, _AS_META]

    def test_challenge_url_already_fetched_is_not_refetched(self) -> None:
        # A challenge naming a location still queued (or already tried) must
        # not cost a second request.
        client = _discover_error(
            _SERVER,
            _router({_PATH_PRM: _challenge(_ROOT_PRM), _ROOT_PRM: _mk_response(404)}),
            "HTTP 404",
        )
        assert _urls(client) == [_PATH_PRM, _ROOT_PRM]

    def test_each_derived_location_keeps_its_own_challenge(self) -> None:
        # The path probe's challenge points at a stale document; the origin
        # probe's challenge points at the right one and is still followed.
        stale = "https://meta.example.com/stale"
        good = "https://meta.example.com/good"
        meta, client = _discover(
            _SERVER,
            _router(
                {
                    _PATH_PRM: _challenge(stale),
                    stale: _mk_response(404),
                    _ROOT_PRM: _challenge(good),
                    # Challenged from the origin location, so the origin is
                    # what this document may declare.
                    good: _prm("https://mcp.example.com"),
                    _AS_META: _mk_response(200, _good_as_metadata_doc()),
                }
            ),
        )
        assert meta.issuer == "https://as.example.com"
        assert _urls(client) == [_PATH_PRM, stale, _ROOT_PRM, good, _AS_META]

    def test_challenge_chain_is_bounded_per_location(self) -> None:
        # One hop per derived location: a chain off the path probe cannot
        # spend the origin probe's hop, and the origin's own challenge is
        # still followed.
        good = "https://meta.example.com/good"
        meta, client = _discover(
            _SERVER,
            _router(
                {
                    _PATH_PRM: _challenge("https://meta.example.com/a"),
                    "https://meta.example.com/a": _challenge("https://meta.example.com/b"),
                    _ROOT_PRM: _challenge(good),
                    good: _prm("https://mcp.example.com"),  # origin: the challenged location
                    _AS_META: _mk_response(200, _good_as_metadata_doc()),
                }
            ),
        )
        assert meta.issuer == "https://as.example.com"
        assert _urls(client) == [
            _PATH_PRM,
            "https://meta.example.com/a",
            _ROOT_PRM,
            good,
            _AS_META,
        ]

    def test_server_url_fragment_is_stripped_not_refused(self) -> None:
        # The console refuses a fragment at the write boundary. A row stored
        # before that validation existed must still resolve: the fragment is
        # not part of the resource identifier (RFC 8707 §2), so discovery
        # probes and compares the identifier without it.
        meta, client = _discover(
            "https://mcp.example.com/sse#fragment",
            _router(
                {_PATH_PRM: _prm(_SERVER), _AS_META: _mk_response(200, _good_as_metadata_doc())}
            ),
        )
        assert meta.issuer == "https://as.example.com"
        assert _urls(client) == [_PATH_PRM, _AS_META]

    def test_prm_401_follows_www_authenticate(self) -> None:
        async def _get(url, *args, **kwargs):
            if url == "https://mcp.example.com/.well-known/oauth-protected-resource/sse":
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
                        "resource": "https://mcp.example.com/sse",
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


class TestS256PerDocumentAndOIDCFallback:
    """PKCE S256 defaulting is per-discovery-document, and OIDC discovery is a
    fallback to RFC 8414 (PR #706 follow-up).

    The client always sends ``code_challenge_method=S256``, so the AS-metadata
    check is the only PKCE-enforcement pre-flight. An ABSENT
    ``code_challenge_methods_supported`` is treated as "S256 supported" ONLY for
    the OIDC ``openid-configuration`` document (where the field is optional and
    Entra omits it); for the RFC 8414 ``oauth-authorization-server`` document an
    absent field fails closed.
    """

    @staticmethod
    def _doc_without_code_challenge() -> dict[str, Any]:
        doc = _good_as_metadata_doc()
        del doc["code_challenge_methods_supported"]
        return doc

    def test_absent_field_on_oidc_doc_assumes_s256(self) -> None:
        # RFC 8414 path 404s; the OIDC doc omits code_challenge_methods_supported
        # -> assume S256 (Entra's shape) and discovery succeeds.
        async def _get(url, *args, **kwargs):
            if url.endswith("/oauth-authorization-server"):
                return _mk_response(404, json_body=None)
            if url.endswith("/openid-configuration"):
                return _mk_response(200, self._doc_without_code_challenge())
            raise AssertionError(f"unexpected URL: {url}")

        client = MagicMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(side_effect=_get)
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

    def test_absent_field_on_rfc8414_doc_fails_closed(self) -> None:
        # The RFC 8414 doc is served (200) but omits the field — must NOT assume
        # S256. Per RFC 8414 an omitted field means "no PKCE advertised", so
        # discovery fails closed rather than silently downgrading.
        async def _get(url, *args, **kwargs):
            if url.endswith("/oauth-authorization-server"):
                return _mk_response(200, self._doc_without_code_challenge())
            raise AssertionError(f"unexpected URL: {url}")

        client = MagicMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(side_effect=_get)
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

    def test_rfc8414_404_falls_back_to_openid_configuration(self) -> None:
        issuer = "https://as.example.com/tenant/"
        rfc8414_url = "https://as.example.com/.well-known/oauth-authorization-server/tenant"
        legacy_oidc_url = "https://as.example.com/tenant/.well-known/openid-configuration"
        oidc_doc = _good_as_metadata_doc()
        oidc_doc["issuer"] = issuer

        # Every RFC 8414 form and the OIDC insert form 404; the legacy OIDC
        # append form used by providers such as Microsoft Entra answers last.
        oidc_insert_url = "https://as.example.com/.well-known/openid-configuration/tenant"

        async def _get(url, *args, **kwargs):
            if url in (rfc8414_url, oidc_insert_url):
                return _mk_response(404, json_body=None)
            if url == legacy_oidc_url:
                return _mk_response(200, oidc_doc)
            raise AssertionError(f"unexpected URL: {url}")

        client = MagicMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(side_effect=_get)
        storage = _mk_storage_mock()

        async def _run():
            with _public_addr_patch():
                return await discover_authorization_server(
                    server_name="srv-x",
                    server_url="https://mcp.example.com/sse",
                    override_url=issuer,
                    cached_issuer=None,
                    http_client=client,
                    storage=storage,
                    server_id="srv-id",
                    trusted_hosts=frozenset(),
                )

        meta = asyncio.run(_run())
        assert meta.issuer == issuer
        assert meta.token_endpoint == "https://as.example.com/token"
        assert [call.args[0] for call in client.get.call_args_list] == [
            rfc8414_url,
            oidc_insert_url,
            legacy_oidc_url,
        ]


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
            if url.endswith("/oauth-protected-resource/sse"):
                return _mk_response(
                    200,
                    {
                        "resource": "https://mcp.example.com/sse",
                        "authorization_servers": ["https://as.example.com"],
                    },
                )
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
            if url.endswith("/oauth-protected-resource/sse"):
                return _mk_response(
                    200,
                    {
                        "resource": "https://mcp.example.com/sse",
                        "authorization_servers": ["https://as.example.com"],
                    },
                )
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


# ---------------------------------------------------------------------------
# canonical_resource
# ---------------------------------------------------------------------------


class TestCanonicalResource:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("https://MCP.Example.com/sse", "https://mcp.example.com/sse"),
            ("HTTPS://mcp.example.com/sse", "https://mcp.example.com/sse"),
            ("https://mcp.example.com:443/sse", "https://mcp.example.com/sse"),
            ("http://localhost:80/sse", "http://localhost/sse"),
            ("https://mcp.example.com:8443/sse", "https://mcp.example.com:8443/sse"),
            ("https://mcp.example.com/", "https://mcp.example.com"),
            ("https://mcp.example.com", "https://mcp.example.com"),
            ("https://mcp.example.com/mcp/", "https://mcp.example.com/mcp/"),
            ("https://mcp.example.com/MCP", "https://mcp.example.com/MCP"),
            ("https://mcp.example.com/sse?tenant=a", "https://mcp.example.com/sse?tenant=a"),
            ("http://[::1]/sse", "http://[::1]/sse"),
            ("https://[2001:DB8::1]:8443/mcp", "https://[2001:db8::1]:8443/mcp"),
            ("  https://mcp.example.com/sse  ", "https://mcp.example.com/sse"),
        ],
    )
    def test_normalizes(self, raw: str, expected: str) -> None:
        assert canonical_resource(raw) == expected
        # Canonical input is a fixed point, which is what lets every read
        # site re-run it on an already-canonical row for free.
        assert canonical_resource(expected) == expected

    @pytest.mark.parametrize(
        "raw",
        [
            "mcp.example.com/sse",
            # ``urlsplit`` deletes these outright, so the parsed value would
            # name a host the bytes never did.
            "https://mcp.exa\nmple.com/sse",
            "https://mcp.exa\tmple.com/sse",
            "https://mcp.example.com/s\rse",
            "https://mcp.example.com/\x00sse",
            "https://mcp.example.com/s se",
            "https://mcp.example.com/s\x7fse",
            "https:///sse",
            "https://mcp.example.com:99999/sse",
            "",
        ],
    )
    def test_rejects(self, raw: str) -> None:
        with pytest.raises(ValueError):
            canonical_resource(raw)
        with pytest.raises(ValueError):
            canonical_resource(raw, strict=True)

    def test_bare_fragment_delimiter_is_a_fragment(self) -> None:
        # A trailing ``#`` parses to an EMPTY fragment, so a truthiness check
        # lets it through; RFC 8707 §2 forbids the component, not just a
        # non-empty one.
        with pytest.raises(ValueError):
            canonical_resource("https://mcp.example.com/sse#", strict=True)
        assert canonical_resource("https://mcp.example.com/sse#") == "https://mcp.example.com/sse"

    @pytest.mark.parametrize(
        ("raw", "stripped"),
        [
            ("https://mcp.example.com/sse#v2", "https://mcp.example.com/sse"),
            ("https://user:pw@mcp.example.com/sse", "https://mcp.example.com/sse"),
            ("https://@mcp.example.com/sse", "https://mcp.example.com/sse"),
        ],
    )
    def test_fragment_and_userinfo_strict_only(self, raw: str, stripped: str) -> None:
        # Refused where an operator can fix the input, stripped where the
        # value merely comes back off a row written before the check existed.
        with pytest.raises(ValueError):
            canonical_resource(raw, strict=True)
        assert canonical_resource(raw) == stripped


# ---------------------------------------------------------------------------
# AS metadata candidate locations
# ---------------------------------------------------------------------------


class TestASMetadataCandidates:
    _ISSUER = "https://as.example.com/tenant/"
    # MCP order first (RFC 8414 insert, OIDC insert, OIDC append), then the
    # non-spec RFC 8414 append form as a compatibility probe that cannot
    # preempt or delay a standard location.
    _CANDIDATES = (
        "https://as.example.com/.well-known/oauth-authorization-server/tenant",
        "https://as.example.com/.well-known/openid-configuration/tenant",
        "https://as.example.com/tenant/.well-known/openid-configuration",
        "https://as.example.com/tenant/.well-known/oauth-authorization-server",
    )
    _RFC8414_INDEXES = (0, 3)

    def _doc_without_code_challenge(self) -> dict[str, Any]:
        doc = _good_as_metadata_doc()
        doc["issuer"] = self._ISSUER
        del doc["code_challenge_methods_supported"]
        return doc

    @pytest.mark.parametrize("winner", [0, 1, 2, 3])
    def test_path_issuer_tries_four_locations_in_order(self, winner: int) -> None:
        # Both RFC 8414 forms precede both OIDC forms. The document omits
        # ``code_challenge_methods_supported`` so the per-document S256 rule
        # also identifies WHICH profile answered: the RFC 8414 profile fails
        # closed on the omission, the OIDC profile assumes S256.
        routes: dict[str, Any] = {
            url: _mk_response(404, json_body=None) for url in self._CANDIDATES[:winner]
        }
        routes[self._CANDIDATES[winner]] = _mk_response(200, self._doc_without_code_challenge())
        if winner in self._RFC8414_INDEXES:
            client = _discover_error(_SERVER, _router(routes), "S256", override_url=self._ISSUER)
        else:
            meta, client = _discover(_SERVER, _router(routes), override_url=self._ISSUER)
            assert meta.token_endpoint == "https://as.example.com/token"
        assert _urls(client) == list(self._CANDIDATES[: winner + 1])

    def test_root_issuer_request_list(self) -> None:
        # Insert and append forms coincide for a root issuer and are deduplicated.
        async def _get(url: str, *args: Any, **kwargs: Any) -> Any:
            return _mk_response(404, json_body=None)

        client = _discover_error(_SERVER, _get, "HTTP 404", override_url="https://as.example.com")
        assert _urls(client) == [
            "https://as.example.com/.well-known/oauth-authorization-server",
            "https://as.example.com/.well-known/openid-configuration",
        ]

    @pytest.mark.parametrize(
        ("override", "match"),
        [
            ("https://as.example.com/tenant?realm=x", "query or fragment"),
            # Bare delimiters parse to empty components, so a truthiness
            # check lets them through; RFC 8414 §2 forbids the components.
            ("https://as.example.com/tenant?", "query or fragment"),
            ("https://as.example.com/tenant#", "query or fragment"),
            ("https://as.example.com/ten\nant", "control characters"),
            ("https://as.exa\tmple.com/tenant", "control characters"),
        ],
    )
    def test_malformed_issuer_rejected_before_any_request(self, override: str, match: str) -> None:
        client = _discover_error(_SERVER, _router({}), match, override_url=override)
        assert _urls(client) == []

    @pytest.mark.parametrize(
        "declared",
        [
            "https://as.example.com/{tenantid}\n",
            "https://as.example.com/{tenantid}\t",
            # Bare delimiters parse to empty components, which a truthiness
            # check reads as absent; the template exception tolerates a
            # placeholder, not a query or a fragment.
            "https://as.example.com/{tenantid}?",
            "https://as.example.com/{tenantid}#",
            "https://as.example.com/{tenantid}?x=1",
        ],
    )
    def test_template_does_not_launder_malformed_issuers(self, declared: str) -> None:
        # The exact comparison already fails on the raw bytes; the template
        # path must not rescue them through ``urlsplit`` either.
        doc = _good_as_metadata_doc()
        doc["issuer"] = declared

        async def _get(url: str, *args: Any, **kwargs: Any) -> Any:
            return _mk_response(200, doc)

        _discover_error(
            _SERVER, _get, "does not match", override_url="https://as.example.com/common"
        )

    def _doc(self) -> dict[str, Any]:
        doc = _good_as_metadata_doc()
        doc["issuer"] = self._ISSUER
        return doc

    def test_unusable_200_on_insert_form_falls_through_to_append_form(self) -> None:
        # A front end that answers unknown well-known paths with an HTML shell.
        meta, client = _discover(
            _SERVER,
            _router(
                {
                    self._CANDIDATES[0]: _mk_response(200, json_body=None),
                    self._CANDIDATES[1]: _mk_response(200, self._doc()),
                }
            ),
            override_url=self._ISSUER,
        )
        assert meta.token_endpoint == "https://as.example.com/token"
        assert _urls(client) == list(self._CANDIDATES[:2])

    def test_transport_error_on_insert_form_falls_through(self) -> None:
        routes = _router({self._CANDIDATES[1]: _mk_response(200, self._doc())})

        async def _get(url: str, *args: Any, **kwargs: Any) -> Any:
            if url == self._CANDIDATES[0]:
                raise httpx.ReadTimeout("stalled")
            return await routes(url, *args, **kwargs)

        meta, client = _discover(_SERVER, _get, override_url=self._ISSUER)
        assert meta.token_endpoint == "https://as.example.com/token"
        assert _urls(client) == list(self._CANDIDATES[:2])

    def test_probes_stop_when_the_shared_budget_is_spent(self) -> None:
        # The budget is a wall-clock ceiling, not a between-requests check:
        # each probe is given only what is left, and once that reaches zero
        # no further location is tried. The clock is faked on the module's
        # own ``time`` reference so the event loop keeps the real one.
        timeouts: list[float] = []

        async def _get(url: str, *args: Any, **kwargs: Any) -> Any:
            timeouts.append(kwargs["timeout"])
            return _mk_response(404, json_body=None)

        fake_time = MagicMock()
        fake_time.monotonic.side_effect = [0.0, 8.0, 16.0, 24.0, 32.0, 40.0]
        fake_time.time.return_value = 1000.0
        with patch("turnstone.core.mcp_oauth.time", fake_time):
            # The surfaced error stays the first candidate's 404: it is what
            # an operator can act on, and running out of budget afterwards
            # says nothing more.
            _discover_error(_SERVER, _get, "HTTP 404", override_url="https://as.example.com/tenant")
        # Budget 20s from t=0: probe 1 gets min(10, 20-8)=10, probe 2 gets
        # min(10, 20-16)=4, and at t=24 nothing is left, so the third and
        # fourth candidates are never tried.
        assert timeouts == [10.0, 4.0]

    def test_outer_timeout_bounds_work_outside_the_request_timeouts(self) -> None:
        # httpx timeouts bound per-phase inactivity, and DNS, SSRF and
        # endpoint validation run outside them entirely, so the budget is
        # enforced by an outer deadline over the whole resolution.
        async def _slow_validate(url: str, **kwargs: Any) -> Any:
            await asyncio.sleep(0.05)
            raise AssertionError("should have been cancelled by the outer deadline")

        client = MagicMock(spec=httpx.AsyncClient)
        client.get = AsyncMock()
        storage = _mk_storage_mock()

        async def _run() -> None:
            await discover_authorization_server(
                server_name="srv-x",
                server_url=_SERVER,
                override_url="https://as.example.com",
                cached_issuer=None,
                http_client=client,
                storage=storage,
                server_id="srv-id",
                trusted_hosts=frozenset(),
            )

        with (
            patch("turnstone.core.mcp_oauth._DISCOVERY_TOTAL_BUDGET", 0.01),
            patch("turnstone.core.mcp_oauth.validate_url_no_ssrf_async", _slow_validate),
            pytest.raises(MCPOAuthDiscoveryError, match="exceeded its time budget"),
        ):
            asyncio.run(_run())
        client.get.assert_not_called()

    def test_document_error_preferred_over_later_404(self) -> None:
        routes: dict[str, Any] = {
            url: _mk_response(404, json_body=None) for url in self._CANDIDATES
        }
        routes[self._CANDIDATES[0]] = _mk_response(200, json_body=None)
        client = _discover_error(
            _SERVER, _router(routes), "not valid JSON", override_url=self._ISSUER
        )
        assert _urls(client) == list(self._CANDIDATES)

    def test_document_without_issuer_is_skipped(self) -> None:
        # RFC 8414 §3.3 makes ``issuer`` the field that binds a document to
        # the issuer that was asked for; a document without it is not this
        # issuer's metadata.
        doc = _good_as_metadata_doc()
        doc["issuer"] = None
        _discover_error(
            _SERVER,
            _router(
                {
                    "https://as.example.com/.well-known/oauth-authorization-server": (
                        _mk_response(200, doc)
                    ),
                    "https://as.example.com/.well-known/openid-configuration": _mk_response(404),
                }
            ),
            "no issuer",
            override_url="https://as.example.com",
        )

    def test_mismatched_issuer_advances_to_the_right_document(self) -> None:
        # A catch-all document at a neighbouring location declares someone
        # else's issuer; it must not decide this issuer's endpoints, and the
        # correct document one candidate later must still be reached.
        catch_all = _good_as_metadata_doc()
        catch_all["issuer"] = "https://other.example.com"
        meta, client = _discover(
            _SERVER,
            _router(
                {
                    self._CANDIDATES[0]: _mk_response(200, catch_all),
                    self._CANDIDATES[1]: _mk_response(200, self._doc()),
                }
            ),
            override_url=self._ISSUER,
        )
        assert meta.issuer == self._ISSUER
        assert _urls(client) == list(self._CANDIDATES[:2])

    @pytest.mark.parametrize(
        "declared",
        [
            "https://other.example.com/{tenantid}",
            "https://as.example.com/{tenantid}/v2.0",
            "https://as.example.com/{a}/{b}",
            "https://as.example.com/prefix-{tenantid}",
            # A placeholder may never stand in for the authority or the
            # scheme — that is precisely the mix-up the equality rule exists
            # to prevent — nor be empty or malformed.
            "https://{host}/tenant",
            "{scheme}://as.example.com/tenant",
            "https://as.{domain}.com/tenant",
            "https://as.example.com/{}",
            "https://as.example.com/{",
            "https://as.example.com/{a{b}}",
        ],
    )
    def test_template_must_expand_to_the_requested_issuer(self, declared: str) -> None:
        # A placeholder is not a wildcard: the literal runs around it must
        # match and it stands for one path segment, so a template for
        # another host or another path shape is a mismatch like any other.
        doc = _good_as_metadata_doc()
        doc["issuer"] = declared
        _discover_error(
            _SERVER,
            _router(
                {
                    "https://as.example.com/.well-known/oauth-authorization-server/tenant": (
                        _mk_response(200, doc)
                    ),
                    "https://as.example.com/.well-known/openid-configuration/tenant": (
                        _mk_response(404)
                    ),
                    "https://as.example.com/tenant/.well-known/openid-configuration": (
                        _mk_response(404)
                    ),
                    "https://as.example.com/tenant/.well-known/oauth-authorization-server": (
                        _mk_response(404)
                    ),
                }
            ),
            "does not match the requested issuer",
            override_url="https://as.example.com/tenant",
        )

    def test_template_matching_is_linear_in_placeholder_count(self) -> None:
        # Metadata is externally supplied and this runs on the event loop, so
        # matching walks path segments rather than building a pattern: a
        # document packed with placeholders must not blow up.
        from turnstone.core.mcp_oauth import _issuer_template_matches

        declared = "https://as.example.com/" + "/".join("{p}" for _ in range(40))
        requested = "https://as.example.com/" + "/".join("seg" for _ in range(40))
        started = time.monotonic()
        assert _issuer_template_matches(declared, requested) is True
        assert _issuer_template_matches(declared, requested + "/extra") is False
        assert time.monotonic() - started < 1.0

    @pytest.mark.parametrize(
        ("requested", "declared"),
        [
            ("https://as.example.com/tenant", "https://as.example.com/tenant/"),
            ("https://as.example.com/tenant", "https://as.example.com/tenant///"),
            ("https://as.example.com/tenant/", "https://as.example.com/tenant"),
            ("https://as.example.com", "https://as.example.com/"),
        ],
    )
    def test_trailing_slash_is_not_the_same_issuer(self, requested: str, declared: str) -> None:
        # RFC 8414 §3.3 means "identical" literally, and the comparison is
        # against the issuer as requested rather than the slash-stripped form
        # used to build the well-known URL — otherwise every document at a
        # neighbouring spelling would be accepted for this one.
        doc = _good_as_metadata_doc()
        doc["issuer"] = declared

        async def _get(url: str, *args: Any, **kwargs: Any) -> Any:
            # Every location serves the same document, so the only thing that
            # can refuse it is the issuer comparison.
            return _mk_response(200, doc)

        _discover_error(
            _SERVER, _get, "does not match the requested issuer", override_url=requested
        )

    def test_issuer_matching_the_requested_spelling_is_accepted(self) -> None:
        # The mirror of the case above: the AS declares exactly what was
        # asked for, trailing slash included.
        requested = "https://as.example.com/tenant/"
        doc = _good_as_metadata_doc()
        doc["issuer"] = requested
        meta, _client = _discover(
            _SERVER,
            _router(
                {
                    "https://as.example.com/.well-known/oauth-authorization-server/tenant": (
                        _mk_response(200, doc)
                    )
                }
            ),
            override_url=requested,
        )
        assert meta.issuer == requested

    def test_templated_issuer_is_accepted_and_logged(self) -> None:
        # A tenant-agnostic document declares a templated issuer; a strict
        # RFC 8414 §3.3 equality check would lock out multi-tenant IdPs, so
        # this one deviation is tolerated, logged, and the requested issuer
        # is what the metadata carries forward.
        doc = _good_as_metadata_doc()
        doc["issuer"] = "https://as.example.com/{tenantid}"
        with patch("turnstone.core.mcp_oauth.log") as log_mock:
            meta, _client = _discover(
                _SERVER,
                _router(
                    {
                        "https://as.example.com/.well-known/oauth-authorization-server/common": (
                            _mk_response(200, doc)
                        )
                    }
                ),
                override_url="https://as.example.com/common",
            )
        assert meta.token_endpoint == "https://as.example.com/token"
        assert meta.issuer == "https://as.example.com/common"
        events = [c.args[0] for c in log_mock.info.call_args_list]
        assert "mcp_server.oauth.as_metadata_templated_issuer" in events


# ---------------------------------------------------------------------------
# Private-network opt-in (``mcp.oauth_allow_private_network``)
# ---------------------------------------------------------------------------


class TestPrivateNetworkOptIn:
    """The opt-in reaches what the operator typed, and nothing a peer named.

    A deployment whose MCP server lives on an internal network is the case
    this exists for. The line it must not cross: a document Turnstone
    fetched naming a private authorization server would let a remote server
    steer the deployment into its operator's own network, so that stays
    refused whatever the setting says.
    """

    _AS_INSERT = "https://as.internal.example/.well-known/oauth-authorization-server"

    def _as_doc(self, issuer: str = "https://as.internal.example") -> dict[str, Any]:
        doc = _good_as_metadata_doc()
        doc["issuer"] = issuer
        for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            doc[key] = doc[key].replace("https://as.example.com", issuer)
        doc["registration_endpoint"] = f"{issuer}/register"
        return doc

    def test_setting_off_refuses_the_typed_server_with_a_hint(self) -> None:
        client = MagicMock(spec=httpx.AsyncClient)
        client.get = AsyncMock()
        storage = _mk_storage_mock()

        async def _run() -> None:
            with _private_addr_patch():
                await discover_authorization_server(
                    server_name="srv-x",
                    server_url=_SERVER,
                    override_url=None,
                    cached_issuer=None,
                    http_client=client,
                    storage=storage,
                    server_id="srv-id",
                    trusted_hosts=frozenset(),
                    allow_private_network=False,
                )

        with pytest.raises(MCPOAuthDiscoveryError, match="mcp.oauth_allow_private_network"):
            asyncio.run(_run())
        client.get.assert_not_called()

    def test_setting_on_reaches_the_typed_server_and_override(self) -> None:
        meta, client = _discover_private(
            _SERVER,
            _router({self._AS_INSERT: _mk_response(200, self._as_doc())}),
            allow_private_network=True,
            override_url="https://as.internal.example",
        )
        assert meta.token_endpoint == "https://as.internal.example/token"
        assert _urls(client) == [self._AS_INSERT]

    def _private_server_public_issuer(self, *, allow_private_network: bool):
        """Private MCP server, public issuer — the feature's headline lane."""
        client = MagicMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(
            side_effect=_router(
                {
                    _PATH_PRM: _prm(_SERVER, issuer="https://as.example.com"),
                    _AS_META: _mk_response(200, _good_as_metadata_doc()),
                }
            )
        )
        storage = _mk_storage_mock()

        async def _run() -> ASMetadata:
            with _addr_map_patch({"mcp.example.com": ["192.168.1.50"]}):
                return await discover_authorization_server(
                    server_name="srv-x",
                    server_url=_SERVER,
                    override_url=None,
                    cached_issuer=None,
                    http_client=client,
                    storage=storage,
                    server_id="srv-id",
                    trusted_hosts=frozenset(),
                    allow_private_network=allow_private_network,
                )

        return _run, client

    def test_setting_on_reaches_a_private_prm_location(self) -> None:
        # The PRM URL is derived from the operator's own server URL, so it
        # carries the opt-in; the issuer it names is public and needs none.
        run, client = self._private_server_public_issuer(allow_private_network=True)
        meta = asyncio.run(run())
        assert meta.issuer == "https://as.example.com"
        assert _urls(client) == [_PATH_PRM, _AS_META]

    def test_the_same_lane_is_refused_with_the_setting_off(self) -> None:
        # The negative half of the test above: without it, flipping the flag
        # in the positive test would change nothing and prove nothing.
        run, client = self._private_server_public_issuer(allow_private_network=False)
        with pytest.raises(MCPOAuthDiscoveryError, match="mcp.oauth_allow_private_network"):
            asyncio.run(run())
        client.get.assert_not_called()

    def test_mixed_public_and_private_records_are_refused(self) -> None:
        # The name answers with both, so the request may land on either and
        # the opt-in would not describe where it goes.
        client = MagicMock(spec=httpx.AsyncClient)
        client.get = AsyncMock()
        storage = _mk_storage_mock()

        async def _run() -> None:
            with _addr_map_patch({"mcp.example.com": ["93.184.216.34", "10.0.0.5"]}):
                await discover_authorization_server(
                    server_name="srv-x",
                    server_url=_SERVER,
                    override_url=None,
                    cached_issuer=None,
                    http_client=client,
                    storage=storage,
                    server_id="srv-id",
                    trusted_hosts=frozenset(),
                    allow_private_network=True,
                )

        with pytest.raises(MCPOAuthDiscoveryError, match="private network"):
            asyncio.run(_run())
        client.get.assert_not_called()

    def test_same_origin_challenge_keeps_the_opt_in(self) -> None:
        # A challenge back to another location on the host the operator
        # typed adds no reach, so refusing it would strand an internal
        # server that points at its own alternate metadata location.
        challenge_target = "https://mcp.example.com/custom/prm"
        client = MagicMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(
            side_effect=_router(
                {
                    _PATH_PRM: _challenge(challenge_target),
                    challenge_target: _prm(_SERVER, issuer="https://as.example.com"),
                    _AS_META: _mk_response(200, _good_as_metadata_doc()),
                }
            )
        )
        storage = _mk_storage_mock()

        async def _run() -> ASMetadata:
            with _addr_map_patch({"mcp.example.com": ["192.168.1.50"]}):
                return await discover_authorization_server(
                    server_name="srv-x",
                    server_url=_SERVER,
                    override_url=None,
                    cached_issuer=None,
                    http_client=client,
                    storage=storage,
                    server_id="srv-id",
                    trusted_hosts=frozenset(),
                    allow_private_network=True,
                )

        meta = asyncio.run(_run())
        assert meta.issuer == "https://as.example.com"
        assert challenge_target in _urls(client)

    def test_issuer_named_by_a_document_never_inherits_the_opt_in(self) -> None:
        # The load-bearing boundary: the resource server is private and
        # typed by the operator, but the issuer it names is not, so it is
        # validated strictly even with the setting on.
        client = MagicMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(
            side_effect=_router(
                {
                    _PATH_PRM: _prm(_SERVER, issuer="https://as.internal.example"),
                    # The refused issuer is a document-level rejection, so the
                    # loop still tries the origin location before giving up.
                    _ROOT_PRM: _mk_response(404),
                }
            )
        )
        storage = _mk_storage_mock()

        async def _run() -> None:
            with _private_addr_patch():
                await discover_authorization_server(
                    server_name="srv-x",
                    server_url=_SERVER,
                    override_url=None,
                    cached_issuer=None,
                    http_client=client,
                    storage=storage,
                    server_id="srv-id",
                    trusted_hosts=frozenset(),
                    allow_private_network=True,
                )

        with pytest.raises(MCPOAuthDiscoveryError, match="Authorization Server URL"):
            asyncio.run(_run())

    def test_challenge_url_never_inherits_the_opt_in(self) -> None:
        # Same rule one layer down: the challenge target is named by the
        # server, so it is validated strictly and the loop moves on.
        client = MagicMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(
            side_effect=_router(
                {
                    _PATH_PRM: _challenge("https://meta.internal.example/prm"),
                    _ROOT_PRM: _mk_response(404),
                }
            )
        )
        storage = _mk_storage_mock()

        async def _run() -> None:
            with _private_addr_patch():
                await discover_authorization_server(
                    server_name="srv-x",
                    server_url=_SERVER,
                    override_url=None,
                    cached_issuer=None,
                    http_client=client,
                    storage=storage,
                    server_id="srv-id",
                    trusted_hosts=frozenset(),
                    allow_private_network=True,
                )

        # The remedy must not be "enable the setting" — it is already on and
        # could never have applied to a URL the server chose.
        with pytest.raises(MCPOAuthDiscoveryError, match="never followed"):
            asyncio.run(_run())
        assert "https://meta.internal.example/prm" not in _urls(client)

    @pytest.mark.parametrize("addr", ["169.254.169.254", "224.0.0.1", "0.0.0.0"])
    def test_always_refused_lanes_stay_refused_under_the_opt_in(self, addr: str) -> None:
        client = MagicMock(spec=httpx.AsyncClient)
        client.get = AsyncMock()
        storage = _mk_storage_mock()

        async def _run() -> None:
            with _private_addr_patch(addr):
                await discover_authorization_server(
                    server_name="srv-x",
                    server_url=_SERVER,
                    override_url=None,
                    cached_issuer=None,
                    http_client=client,
                    storage=storage,
                    server_id="srv-id",
                    trusted_hosts=frozenset(),
                    allow_private_network=True,
                )

        with pytest.raises(MCPOAuthDiscoveryError, match="refused"):
            asyncio.run(_run())
        client.get.assert_not_called()

    def test_reader_defaults_strict_without_a_config_store(self) -> None:
        from types import SimpleNamespace

        from turnstone.core.mcp_oauth import oauth_allow_private_network

        assert oauth_allow_private_network(SimpleNamespace()) is False
        assert oauth_allow_private_network(SimpleNamespace(config_store=None)) is False

        store = MagicMock()
        store.get.return_value = True
        assert oauth_allow_private_network(SimpleNamespace(config_store=store)) is True
        store.get.assert_called_once_with("mcp.oauth_allow_private_network")

    def test_reader_survives_a_failing_config_store(self) -> None:
        from types import SimpleNamespace

        from turnstone.core.mcp_oauth import oauth_allow_private_network

        store = MagicMock()
        store.get.side_effect = RuntimeError("db down")
        assert oauth_allow_private_network(SimpleNamespace(config_store=store)) is False

    def test_every_discovery_call_site_passes_the_setting(self) -> None:
        """No production call to discovery may omit the opt-in.

        Each site defaults to ``False`` at every level, so dropping the
        argument is type-clean and silent: connect and callback keep working
        against an internal server and only the refresh hours later fails,
        surfacing as a dead grant rather than a test failure. Checked at the
        source because the alternative is four handler harnesses that would
        still miss the fifth site someone adds.
        """
        import inspect
        import re

        from turnstone.core import mcp_oauth

        source = inspect.getsource(mcp_oauth)
        calls = re.findall(
            r"await discover_authorization_server\((.*?)\n(\s*)\)", source, re.DOTALL
        )
        assert len(calls) == 4, f"expected 4 discovery call sites, found {len(calls)}"
        for body, _indent in calls:
            assert "allow_private_network=" in body, (
                "a discovery call site does not pass allow_private_network; it would "
                f"silently fall back to strict:\n{body}"
            )

    def test_refresh_reads_the_setting_from_app_state(self) -> None:
        # The site that fails latest and loudest: a refresh runs long after
        # consent, so a dropped read shows up as a dead grant.
        from types import SimpleNamespace

        from turnstone.core import mcp_oauth

        seen: dict[str, Any] = {}

        async def _fake_discover(**kwargs: Any) -> ASMetadata:
            seen.update(kwargs)
            raise MCPOAuthDiscoveryError("stop here")

        store = MagicMock()
        store.get.return_value = True
        state = SimpleNamespace(
            mcp_oauth_http_client=MagicMock(spec=httpx.AsyncClient),
            mcp_oauth_metadata_cache=None,
            config_store=store,
        )

        async def _run() -> None:
            await mcp_oauth._refresh_and_persist(
                app_state=state,
                storage=MagicMock(),
                token_store=MagicMock(),
                user_id="u1",
                server_name="srv-x",
                server_row={
                    "server_id": "srv-id",
                    "url": _SERVER,
                    "oauth_client_id": "cid",
                },
                refresh_value="rt",
                existing_scopes="",
            )

        with (
            patch.object(mcp_oauth, "discover_authorization_server", _fake_discover),
            pytest.raises(mcp_oauth.MCPOAuthRefreshFailed),
        ):
            asyncio.run(_run())
        assert seen["allow_private_network"] is True
        store.get.assert_called_with("mcp.oauth_allow_private_network")

    @pytest.mark.parametrize(
        ("one", "other", "same"),
        [
            ("https://mcp.example.com/a", "https://MCP.Example.com/b", True),
            ("https://mcp.example.com/a", "https://mcp.example.com:443/b", True),
            ("http://mcp.example.com/a", "http://mcp.example.com:80/b", True),
            ("https://[2001:db8::1]/a", "https://[2001:0DB8:0:0:0:0:0:1]/b", True),
            ("https://mcp.example.com/a", "http://mcp.example.com/b", False),
            ("https://mcp.example.com/a", "https://mcp.example.com:8443/b", False),
            ("https://mcp.example.com/a", "https://other.example.com/b", False),
            # Total by construction: these arrive from remote headers, and a
            # malformed literal must refuse rather than escape as ValueError.
            ("https://[::1", "https://mcp.example.com/b", False),
            ("", "https://mcp.example.com/b", False),
            ("not-a-url", "https://mcp.example.com/b", False),
        ],
    )
    def test_origin_comparison_is_canonical_and_total(
        self, one: str, other: str, same: bool
    ) -> None:
        from turnstone.core.mcp_oauth import _same_origin

        assert _same_origin(one, other) is same

    def test_malformed_challenge_refuses_without_escaping(self) -> None:
        # A malformed address literal in a challenge header must not abort
        # discovery before the origin location is tried.
        meta, client = _discover_private(
            _SERVER,
            _router(
                {
                    _PATH_PRM: _challenge("https://[::1"),
                    _ROOT_PRM: _prm("https://mcp.example.com", issuer="https://as.example.com"),
                    _AS_META: _mk_response(200, _good_as_metadata_doc()),
                }
            ),
            allow_private_network=True,
            addr="93.184.216.34",
        )
        assert meta.issuer == "https://as.example.com"

    def test_public_plus_metadata_address_keeps_the_absolute_refusal(self) -> None:
        # A metadata address is refused absolutely; reporting it as a mixed
        # private answer would offer a remedy that is already taken and could
        # never apply.
        client = MagicMock(spec=httpx.AsyncClient)
        client.get = AsyncMock()
        storage = _mk_storage_mock()

        async def _run() -> None:
            with _addr_map_patch({"mcp.example.com": ["93.184.216.34", "169.254.169.254"]}):
                await discover_authorization_server(
                    server_name="srv-x",
                    server_url=_SERVER,
                    override_url=None,
                    cached_issuer=None,
                    http_client=client,
                    storage=storage,
                    server_id="srv-id",
                    trusted_hosts=frozenset(),
                    allow_private_network=True,
                )

        with pytest.raises(MCPOAuthDiscoveryError, match="refused"):
            asyncio.run(_run())
        client.get.assert_not_called()
