"""The shared OAuth HTTP client posture: the environment settings every OAuth and OIDC client
keeps, and a JSON-preferring ``Accept`` on the OAuth clients.

Some token endpoints content-negotiate and answer a bare ``Accept: */*`` with
a form-encoded body the token parsers cannot read. The preference is a client
default set by one factory rather than a per-call literal, so these tests pin
the factory and that every client construction on the OAuth paths goes through
it. The OAuth suites drive the flows with ``MagicMock(spec=httpx2.AsyncClient)``
clients, so asserting a ``headers=`` kwarg on a mocked call would be vacuous;
the merge is exercised on real clients through ``build_request``.
"""

from __future__ import annotations

import asyncio
import gzip
from types import SimpleNamespace
from typing import TYPE_CHECKING

import httpx2
import pytest

from turnstone.core.mcp_oauth import close_mcp_oauth_state, initialize_mcp_oauth_state
from turnstone.core.oauth.context import OAuthContext
from turnstone.core.oauth.http import (
    _MAX_RESPONSE_BODY_BYTES,
    OAuthRefreshError,
    RefreshFailureClass,
    _BoundedClient,
    _enter_mint_client,
    _hardened_token_post,
    json_http_client,
    make_http_client,
)
from turnstone.core.oauth.runtime import OAuthRuntime
from turnstone.core.oauth.work import OAuthUnavailableError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


def _accept(client: httpx2.AsyncClient, headers: dict[str, str] | None = None) -> str:
    request = client.build_request(
        "POST", "https://as.example.com/token", data={"grant_type": "x"}, headers=headers
    )
    return request.headers["accept"]


class TestJsonHttpClient:
    def test_default_accept_is_json_on_the_wire(self) -> None:
        async def _run() -> str:
            async with json_http_client() as client:
                return _accept(client)

        assert asyncio.run(_run()) == "application/json"

    def test_per_request_header_still_overrides(self) -> None:
        async def _run() -> str:
            async with json_http_client() as client:
                return _accept(client, {"Accept": "text/plain"})

        assert asyncio.run(_run()) == "text/plain"

    def test_timeout_is_applied(self) -> None:
        async def _run() -> float | None:
            async with json_http_client(3.5) as client:
                return client.timeout.connect

        assert asyncio.run(_run()) == 3.5


class TestSharedConstructor:
    def test_keeps_the_environment_and_sends_no_json_preference(self) -> None:
        """Proxy variables and ``SSL_CERT_*`` apply (``trust_env``): a deployment may reach its
        identity provider only through an egress proxy. The OIDC sign-in clients come from here
        too, so their wire keeps the default ``Accept``."""

        async def _run() -> tuple[bool, str]:
            async with make_http_client() as client:
                return client.trust_env, _accept(client)

        assert asyncio.run(_run()) == (True, "*/*")


class _Streamed(httpx2.AsyncByteStream):
    """A body delivered piece by piece, as a connection delivers it, counting the pieces pulled.

    A MockTransport response built with ``content=`` is decoded when it is built, so a test of
    how far the client reads has to stream its body."""

    def __init__(self, pieces: list[bytes], *, delay: float = 0.0) -> None:
        self._pieces = pieces
        self._delay = delay
        self.pulled = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for piece in self._pieces:
            self.pulled += 1
            if self._delay:
                await asyncio.sleep(self._delay)
            yield piece


def _gzip_bomb() -> list[bytes]:
    """16 MiB of zeros, gzip-compressed, in 1 KiB pieces: each piece decodes to about 1 MiB."""
    compressed = gzip.compress(bytes(16 << 20))
    return [compressed[start : start + 1024] for start in range(0, len(compressed), 1024)]


class TestBoundedReads:
    """The OAuth client stops reading a response body past its cap, so a small compressed body
    can't decode to gigabytes before a request site's size check runs."""

    def test_the_json_client_is_bounded(self) -> None:
        async def _run() -> tuple[bool, int | None]:
            async with json_http_client() as client:
                return isinstance(client, _BoundedClient), getattr(client, "max_body", None)

        assert asyncio.run(_run()) == (True, _MAX_RESPONSE_BODY_BYTES)

    def test_a_compressed_body_is_decoded_only_past_the_cap(self) -> None:
        stream = _Streamed(_gzip_bomb())

        def respond(request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(200, headers={"content-encoding": "gzip"}, stream=stream)

        async def _run() -> httpx2.Response:
            transport = httpx2.MockTransport(respond)
            async with _BoundedClient(max_body=_MAX_RESPONSE_BODY_BYTES, transport=transport) as c:
                return await c.get("https://as.example.com/.well-known/oauth-authorization-server")

        resp = asyncio.run(_run())

        assert resp.content == bytes(_MAX_RESPONSE_BODY_BYTES + 1)
        # Each 1 KiB piece decodes to about 1 MiB, so the read stops within the first two.
        assert stream.pulled <= 2
        # Already decoded: the rebuilt response must not decode it again.
        assert "content-encoding" not in resp.headers

    def test_bytes_after_a_gzip_stream_stop_at_the_wire_limit(self) -> None:
        """Bytes after the end of a gzip stream decode to nothing, so the decoded cap never sees
        them; twice the cap on the wire stops the read."""
        stream = _Streamed([gzip.compress(b"{}")] + [bytes(64 * 1024)] * 64)

        def respond(request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(200, headers={"content-encoding": "gzip"}, stream=stream)

        async def _run() -> None:
            transport = httpx2.MockTransport(respond)
            async with _BoundedClient(max_body=_MAX_RESPONSE_BODY_BYTES, transport=transport) as c:
                await c.post("https://as.example.com/register", json={})

        with pytest.raises(httpx2.DecodingError, match="on the wire"):
            asyncio.run(_run())
        # 512 KiB on the wire is the gzip stream and eight 64 KiB pieces, plus the one past it.
        assert stream.pulled <= 10

    def test_a_body_that_trickles_in_is_cut_off_at_the_read_timeout(self) -> None:
        """A byte every 50 ms never trips the per-read timeout; the body read as a whole has one."""
        stream = _Streamed([b"x"] * 100, delay=0.05)

        def respond(request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(200, stream=stream)

        async def _run() -> None:
            transport = httpx2.MockTransport(respond)
            async with _BoundedClient(max_body=_MAX_RESPONSE_BODY_BYTES, transport=transport) as c:
                await c.post("https://as.example.com/token", data={}, timeout=0.3)

        with pytest.raises(httpx2.ReadTimeout, match="not read within"):
            asyncio.run(_run())
        assert stream.pulled <= 20

    def test_redirects_are_refused(self) -> None:
        def respond(request: httpx2.Request) -> httpx2.Response:
            raise AssertionError(f"a request was sent to {request.url}")

        async def _run() -> None:
            transport = httpx2.MockTransport(respond)
            async with _BoundedClient(max_body=_MAX_RESPONSE_BODY_BYTES, transport=transport) as c:
                await c.get("https://as.example.com/start", follow_redirects=True)

        with pytest.raises(ValueError, match="redirects"):
            asyncio.run(_run())

    def test_status_and_raw_header_bytes_survive(self) -> None:
        challenge = (
            'Bearer resource_metadata="https://' + chr(0x2603) + '.example.com/x"'
        ).encode()

        def respond(request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(401, headers=[(b"www-authenticate", challenge)])

        async def _run() -> httpx2.Response:
            transport = httpx2.MockTransport(respond)
            async with _BoundedClient(max_body=_MAX_RESPONSE_BODY_BYTES, transport=transport) as c:
                return await c.get("https://mcp.example.com/mcp")

        resp = asyncio.run(_run())

        assert resp.status_code == 401
        assert resp.headers.raw == [(b"www-authenticate", challenge)]

    def test_an_oversized_token_rejection_keeps_its_classification(self) -> None:
        """The token POST still sees an over-cap body as oversized, with the status-based class the
        OBO legs ask for, after reading only past the cap."""
        stream = _Streamed(_gzip_bomb())

        def respond(request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(400, headers={"content-encoding": "gzip"}, stream=stream)

        async def _run() -> None:
            transport = httpx2.MockTransport(respond)
            async with _BoundedClient(max_body=_MAX_RESPONSE_BODY_BYTES, transport=transport) as c:
                await _hardened_token_post(
                    token_endpoint="https://idp.example.com/token",
                    data={"grant_type": "refresh_token"},
                    http_client=c,
                    request_label="mint",
                    endpoint_label="token endpoint",
                    classify_oversized_by_status=True,
                )

        with pytest.raises(OAuthRefreshError, match="exceeds size limit") as raised:
            asyncio.run(_run())
        assert raised.value.failure_class is RefreshFailureClass.AMBIGUOUS
        assert stream.pulled <= 2


class TestEveryClientGoesThroughTheFactory:
    def test_initialize_state_installs_json_client(self) -> None:
        state = SimpleNamespace()

        async def _run() -> tuple[str, bool]:
            await initialize_mcp_oauth_state(state)
            try:
                assert state.mcp_oauth_metadata_cache is state.oauth_context.metadata_cache
                client = state.mcp_oauth_http_client
                return _accept(client), client.trust_env
            finally:
                await close_mcp_oauth_state(state)

        assert asyncio.run(_run()) == ("application/json", True)

    def test_runtime_owns_json_client(self) -> None:
        context = OAuthContext()
        runtime = OAuthRuntime(context)
        context.runtime = runtime
        runtime.start()

        async def inspect() -> tuple[str, bool, bool]:
            async with _enter_mint_client(context) as client:
                on_owner_loop = asyncio.get_running_loop() is runtime._loop
                return _accept(client), on_owner_loop, client.trust_env

        assert runtime.call_sync(inspect) == ("application/json", True, True)
        client = context.http_client
        runtime.shutdown()
        assert client is not None and client.is_closed

    def test_unconfigured_runtime_client_is_unavailable(self) -> None:
        async def run() -> None:
            async with _enter_mint_client(OAuthContext()):
                raise AssertionError("an unconfigured runtime supplied a client")

        with pytest.raises(OAuthUnavailableError):
            asyncio.run(run())
