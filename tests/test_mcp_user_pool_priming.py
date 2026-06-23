"""Tests for the per-user MCP pool priming APIs.

Covers:
- ``prime_user_server`` best-effort warm of a (user, server) pool.
- ``prime_user_pools`` fire-and-forget scheduling onto the mcp-loop.
- Guards against RuntimeError when the loop is closed/stopping.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.conftest import stop_loop_thread
from turnstone.core.mcp_client import MCPClientManager

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def running_loop_mgr():
    """Background-loop fixture for priming tests."""
    cfg: dict[str, Any] = {}
    mgr = MCPClientManager(cfg)
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True, name="mcp-prime-test-loop")
    thread.start()
    mgr._loop = loop
    try:
        yield mgr, loop, thread
    finally:

        async def _drain(m: MCPClientManager) -> None:
            task = m._user_pool_eviction_task
            if task is not None:
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task
                m._user_pool_eviction_task = None

        with contextlib.suppress(Exception):
            asyncio.run_coroutine_threadsafe(_drain(mgr), loop).result(timeout=2)
        stop_loop_thread(loop, thread)


def _run_on_loop(loop: asyncio.AbstractEventLoop, coro: Any) -> Any:
    """Submit *coro* to *loop*, wait for the result with a 5s timeout."""
    fut = asyncio.run_coroutine_threadsafe(coro, loop)
    return fut.result(timeout=5)


# ---------------------------------------------------------------------------
# prime_user_server
# ---------------------------------------------------------------------------


class TestPrimeUserServer:
    def test_noop_for_non_oauth_user_server(self, running_loop_mgr: Any) -> None:
        """prime_user_server returns False for servers not in _oauth_user_server_names."""
        mgr, loop, _ = running_loop_mgr
        mgr._oauth_user_server_names = set()

        result = _run_on_loop(
            loop,
            mgr.prime_user_server(
                user_id="u1",
                server_name="not-oauth",
                access_token="tok",
                server_row={"url": "https://example.com/sse"},
            ),
        )
        assert result is False

    def test_noop_without_loop(self) -> None:
        """prime_user_server returns False when _loop is None."""
        mgr = MCPClientManager({})
        mgr._oauth_user_server_names = {"srv"}
        mgr._loop = None

        result = asyncio.run(
            mgr.prime_user_server(
                user_id="u1",
                server_name="srv",
                access_token="tok",
                server_row={"url": "https://example.com/sse"},
            )
        )
        assert result is False

    def test_noop_without_token(self, running_loop_mgr: Any) -> None:
        """prime_user_server returns False when access_token is empty."""
        mgr, loop, _ = running_loop_mgr
        mgr._oauth_user_server_names = {"srv"}

        result = _run_on_loop(
            loop,
            mgr.prime_user_server(
                user_id="u1",
                server_name="srv",
                access_token="",
                server_row={"url": "https://example.com/sse"},
            ),
        )
        assert result is False

    def test_prime_failure_returns_false(self, running_loop_mgr: Any) -> None:
        """prime_user_server catches exceptions and returns False."""
        mgr, loop, _ = running_loop_mgr
        mgr._oauth_user_server_names = {"srv"}

        with patch.object(
            mgr,
            "_prime_user_server",
            new_callable=AsyncMock,
            side_effect=RuntimeError("connect failed"),
        ):
            result = _run_on_loop(
                loop,
                mgr.prime_user_server(
                    user_id="u1",
                    server_name="srv",
                    access_token="tok",
                    server_row={"url": "https://example.com/sse", "name": "srv"},
                    timeout=2.0,
                ),
            )
            assert result is False

    def test_prime_success_returns_true(self, running_loop_mgr: Any) -> None:
        """prime_user_server returns True when _prime_user_server succeeds."""
        mgr, loop, _ = running_loop_mgr
        mgr._oauth_user_server_names = {"srv"}

        with patch.object(
            mgr,
            "_prime_user_server",
            new_callable=AsyncMock,
            return_value=3,  # 3 tools discovered
        ):
            result = _run_on_loop(
                loop,
                mgr.prime_user_server(
                    user_id="u1",
                    server_name="srv",
                    access_token="tok",
                    server_row={"url": "https://example.com/sse", "name": "srv"},
                    timeout=2.0,
                ),
            )
            assert result is True


# ---------------------------------------------------------------------------
# prime_user_pools
# ---------------------------------------------------------------------------


class TestPrimeUserPools:
    def test_noop_without_user_id(self) -> None:
        """prime_user_pools is a no-op if user_id is empty."""
        mgr = MCPClientManager({})
        mgr._loop = MagicMock()
        mgr._oauth_user_server_names = {"srv"}
        mgr._app_state = SimpleNamespace()
        mgr._storage = MagicMock()

        # Should not raise and should not schedule anything
        mgr.prime_user_pools("")

    def test_noop_without_loop(self) -> None:
        """prime_user_pools is a no-op if _loop is None."""
        mgr = MCPClientManager({})
        mgr._loop = None
        mgr._oauth_user_server_names = {"srv"}
        mgr._app_state = SimpleNamespace()
        mgr._storage = MagicMock()

        mgr.prime_user_pools("user-1")

    def test_noop_without_oauth_servers(self) -> None:
        """prime_user_pools is a no-op if no oauth_user servers configured."""
        mgr = MCPClientManager({})
        mgr._loop = MagicMock()
        mgr._oauth_user_server_names = set()
        mgr._app_state = SimpleNamespace()
        mgr._storage = MagicMock()

        mgr.prime_user_pools("user-1")

    def test_noop_without_app_state(self) -> None:
        """prime_user_pools is a no-op if _app_state is None."""
        mgr = MCPClientManager({})
        mgr._loop = MagicMock()
        mgr._oauth_user_server_names = {"srv"}
        mgr._app_state = None
        mgr._storage = MagicMock()

        mgr.prime_user_pools("user-1")

    def test_runtime_error_on_closed_loop_is_caught(self) -> None:
        """prime_user_pools catches RuntimeError if the loop is closed."""
        mgr = MCPClientManager({})
        closed_loop = asyncio.new_event_loop()
        closed_loop.close()
        mgr._loop = closed_loop
        mgr._oauth_user_server_names = {"srv"}
        mgr._app_state = SimpleNamespace()
        mgr._storage = MagicMock()

        # Should not raise despite the closed loop
        mgr.prime_user_pools("user-1")

    def test_schedules_on_loop(self, running_loop_mgr: Any) -> None:
        """prime_user_pools schedules _prime_user_pools onto the mcp-loop."""
        mgr, loop, _ = running_loop_mgr
        mgr._oauth_user_server_names = {"srv"}
        mgr._app_state = SimpleNamespace()
        mgr._storage = MagicMock()

        called = threading.Event()

        async def _mock_prime(user_id: str) -> None:
            called.set()

        with patch.object(mgr, "_prime_user_pools", side_effect=_mock_prime):
            mgr.prime_user_pools("user-1")
            assert called.wait(timeout=3), "_prime_user_pools was not scheduled"
