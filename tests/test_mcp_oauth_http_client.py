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
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from turnstone.core.mcp_oauth import close_mcp_oauth_state, initialize_mcp_oauth_state
from turnstone.core.oauth.context import OAuthContext
from turnstone.core.oauth.http import _enter_mint_client, json_http_client, make_http_client
from turnstone.core.oauth.runtime import OAuthRuntime
from turnstone.core.oauth.work import OAuthUnavailableError

if TYPE_CHECKING:
    import httpx2


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
