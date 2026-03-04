"""Tests for synchronous SDK wrappers (TurnstoneServer, TurnstoneConsole)."""

from __future__ import annotations

import httpx

from turnstone.sdk._sync import _SyncRunner
from turnstone.sdk.console import AsyncTurnstoneConsole, TurnstoneConsole
from turnstone.sdk.server import AsyncTurnstoneServer, TurnstoneServer


def _json_response(data: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=data)


# ---------------------------------------------------------------------------
# _SyncRunner
# ---------------------------------------------------------------------------


def test_sync_runner_basic():
    """_SyncRunner can execute a simple async coroutine."""
    runner = _SyncRunner()
    try:
        import asyncio

        async def _add(a: int, b: int) -> int:
            await asyncio.sleep(0)
            return a + b

        result = runner.run(_add(1, 2))
        assert result == 3
    finally:
        runner.close()


def test_sync_runner_iter():
    """_SyncRunner.run_iter iterates over an async generator."""
    runner = _SyncRunner()
    try:

        async def _gen():
            for i in range(3):
                yield i

        items = list(runner.run_iter(_gen()))
        assert items == [0, 1, 2]
    finally:
        runner.close()


def test_sync_runner_iter_empty():
    """_SyncRunner.run_iter handles empty async generator via sentinel."""
    runner = _SyncRunner()
    try:

        async def _empty():
            return
            yield  # pragma: no cover  # makes this an async generator

        items = list(runner.run_iter(_empty()))
        assert items == []
    finally:
        runner.close()


# ---------------------------------------------------------------------------
# TurnstoneServer (sync)
# ---------------------------------------------------------------------------


def test_sync_server_list_workstreams():
    """Sync server client delegates to async and returns correct model."""

    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response({"workstreams": [{"id": "ws1", "name": "test", "state": "idle"}]})

    # We need to create the async client with a mock transport,
    # then wrap it in the sync client
    transport = httpx.MockTransport(handler)
    hc = httpx.AsyncClient(transport=transport, base_url="http://test")
    async_client = AsyncTurnstoneServer(httpx_client=hc)

    server = TurnstoneServer.__new__(TurnstoneServer)
    server._runner = _SyncRunner()
    server._async = async_client

    try:
        resp = server.list_workstreams()
        assert len(resp.workstreams) == 1
        assert resp.workstreams[0].id == "ws1"
    finally:
        server.close()


def test_sync_server_context_manager():
    """TurnstoneServer can be used as a context manager."""

    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(
            {"status": "ok", "version": "0.3.0", "uptime_seconds": 1.0, "model": "gpt-5"}
        )

    transport = httpx.MockTransport(handler)
    hc = httpx.AsyncClient(transport=transport, base_url="http://test")
    async_client = AsyncTurnstoneServer(httpx_client=hc)

    server = TurnstoneServer.__new__(TurnstoneServer)
    server._runner = _SyncRunner()
    server._async = async_client

    with server as s:
        resp = s.health()
        assert resp.status == "ok"


# ---------------------------------------------------------------------------
# TurnstoneConsole (sync)
# ---------------------------------------------------------------------------


def test_sync_console_overview():
    """Sync console client delegates to async and returns correct model."""

    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(
            {
                "nodes": 1,
                "workstreams": 3,
                "states": {"idle": 3},
                "aggregate": {"total_tokens": 100, "total_tool_calls": 0},
                "version_drift": False,
                "versions": ["0.3.0"],
            }
        )

    transport = httpx.MockTransport(handler)
    hc = httpx.AsyncClient(transport=transport, base_url="http://test")
    async_client = AsyncTurnstoneConsole(httpx_client=hc)

    console = TurnstoneConsole.__new__(TurnstoneConsole)
    console._runner = _SyncRunner()
    console._async = async_client

    try:
        resp = console.overview()
        assert resp.nodes == 1
        assert resp.workstreams == 3
    finally:
        console.close()
