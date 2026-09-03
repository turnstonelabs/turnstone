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
import contextlib
from types import SimpleNamespace

import httpx

from turnstone.core.mcp_client import MCPClientManager
from turnstone.core.mcp_oauth import (
    _enter_mint_client,
    close_mcp_oauth_state,
    initialize_mcp_oauth_state,
    json_http_client,
)


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
                return _accept(state.mcp_oauth_http_client)
            finally:
                await close_mcp_oauth_state(state)

        assert asyncio.run(_run()) == "application/json"

    def test_enter_mint_client_yields_injected_client(self) -> None:
        async def _run() -> bool:
            async with httpx.AsyncClient() as injected:
                state = SimpleNamespace(obo_http_client=injected)
                async with _enter_mint_client(state) as client:
                    return client is injected

        assert asyncio.run(_run())

    def test_enter_mint_client_transient_is_json_client(self) -> None:
        async def _run() -> str:
            async with _enter_mint_client(SimpleNamespace()) as client:
                return _accept(client)

        assert asyncio.run(_run()) == "application/json"

    def test_mcp_client_connect_installs_json_client(self) -> None:
        # The node's loop-owned client is what production OBO mints use, via
        # ``app_state.obo_http_client``; it must carry the same posture.
        mgr = MCPClientManager({})
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(mgr._connect_all())
            client = mgr._model_auth_http_client
            assert client is not None
            assert _accept(client) == "application/json"
            for task in (mgr._user_token_sweep_task, mgr._static_health_task):
                if task is not None:
                    task.cancel()
                    with contextlib.suppress(BaseException):
                        loop.run_until_complete(task)
            loop.run_until_complete(client.aclose())
        finally:
            loop.close()
