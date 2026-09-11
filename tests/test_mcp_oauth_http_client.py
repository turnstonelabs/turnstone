"""The shared OAuth HTTP client posture: a JSON-preferring ``Accept`` on every client.

Some token endpoints content-negotiate and answer a bare ``Accept: */*`` with
a form-encoded body the token parsers cannot read. The preference is a client
default set by one factory rather than a per-call literal, so these tests pin
the factory and that every client construction on the OAuth paths goes through
it. The OAuth suites drive the flows with ``MagicMock(spec=httpx.AsyncClient)``
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
from turnstone.core.oauth.http import _enter_mint_client, json_http_client
from turnstone.core.oauth.runtime import OAuthRuntime
from turnstone.core.oauth.work import OAuthUnavailableError

if TYPE_CHECKING:
    import httpx


def _accept(client: httpx.AsyncClient, headers: dict[str, str] | None = None) -> str:
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


class TestEveryClientGoesThroughTheFactory:
    def test_initialize_state_installs_json_client(self) -> None:
        state = SimpleNamespace()

        async def _run() -> str:
            await initialize_mcp_oauth_state(state)
            try:
                assert state.mcp_oauth_metadata_cache is state.oauth_context.metadata_cache
                return _accept(state.mcp_oauth_http_client)
            finally:
                await close_mcp_oauth_state(state)

        assert asyncio.run(_run()) == "application/json"

    def test_runtime_owns_json_client(self) -> None:
        context = OAuthContext()
        runtime = OAuthRuntime(context)
        context.runtime = runtime
        runtime.start()

        async def inspect() -> tuple[str, bool]:
            async with _enter_mint_client(context) as client:
                return _accept(client), asyncio.get_running_loop() is runtime._loop

        assert runtime.call_sync(inspect) == ("application/json", True)
        client = context.http_client
        runtime.shutdown()
        assert client is not None and client.is_closed

    def test_unconfigured_runtime_client_is_unavailable(self) -> None:
        async def run() -> None:
            async with _enter_mint_client(OAuthContext()):
                raise AssertionError("an unconfigured runtime supplied a client")

        with pytest.raises(OAuthUnavailableError):
            asyncio.run(run())
