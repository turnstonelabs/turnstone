"""Live flaky-server smoke test: SIGKILL-flap a real MCP server, no CPU spin.

End-to-end regression for the flaky-server 100%-CPU incident: a real
streamable-http MCP server (FastMCP, subprocess) is SIGKILLed and restarted
several times underneath a real ``MCPClientManager`` with the health loop
running on compressed timings. The production failure signature was armed
anyio ``CancelScope``s — each one re-delivers cancellation via ``call_soon``
every event-loop iteration, forever (~10^5+ callbacks/s), one more per flap
cycle — so the pass criterion is structural: after the flaps settle, ZERO
armed scopes exist on the mcp-loop, exactly one transport owner is alive, the
health loop still runs, and a real tool call round-trips.

Self-contained (spawns its own server; no LLM backend, no network beyond
127.0.0.1) — deliberately NOT marked ``live``. Wall clock ~10-15s.
"""

from __future__ import annotations

import asyncio
import gc
import signal
import textwrap
import time
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from tests.conftest import _free_port, _popen_mcp_server, _wait_session_live, _wait_tcp_ready
from turnstone.core.mcp_client import MCPClientManager

if TYPE_CHECKING:
    import subprocess
    from pathlib import Path

SERVER_SRC = textwrap.dedent(
    '''
    """Healthy streamable-http MCP server; the test SIGKILLs it to flap."""
    import sys

    from mcp.server.fastmcp import FastMCP

    port = int(sys.argv[1])
    mcp = FastMCP("flaky-victim", host="127.0.0.1", port=port)


    @mcp.tool()
    def ping_me(x: int) -> int:
        """Return x + 1."""
        return x + 1


    if __name__ == "__main__":
        mcp.run(transport="streamable-http")
    '''
).lstrip()


async def _armed_scope_count() -> int:
    """Armed scopes hosted on THIS (the mcp) loop — mirrors the production
    disarm sweep's scoping, and keeps an unrelated scope on another loop that
    is momentarily mid-cancellation from flaking the assertion."""
    import asyncio as _asyncio

    from anyio._backends._asyncio import CancelScope

    this_loop = _asyncio.get_running_loop()
    armed = 0
    for obj in gc.get_objects():
        if not isinstance(obj, CancelScope):
            continue
        if getattr(obj, "_cancel_handle", None) is None:
            continue
        host = getattr(obj, "_host_task", None)
        if host is not None and host.get_loop() is not this_loop:
            continue
        armed += 1
    return armed


async def _live_owner_count() -> int:
    return sum(
        1
        for t in asyncio.all_tasks()
        if t.get_name().startswith("mcp-transport-owner:") and not t.done()
    )


class TestFlakyServerNoSpin:
    def test_sigkill_flap_cycle_no_armed_scopes_and_recovers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The subprocess runs sys.executable, so importability HERE is a
        # faithful proxy for the server side. Environment gaps skip, not fail.
        pytest.importorskip("mcp.server.fastmcp")
        script = tmp_path / "flaky_srv.py"
        script.write_text(SERVER_SRC)
        port = _free_port()

        # Compress recovery timings so 3 flap cycles fit a unit-test budget.
        monkeypatch.setattr(MCPClientManager, "_CONNECT_TIMEOUT", 3)
        monkeypatch.setattr(MCPClientManager, "_TCP_PROBE_TIMEOUT", 1)
        monkeypatch.setattr(MCPClientManager, "_STATIC_RECONNECT_ATTEMPT_TIMEOUT_S", 5.0)
        monkeypatch.setattr(MCPClientManager, "_STATIC_RECONNECT_CALLER_TIMEOUT_S", 6.0)
        monkeypatch.setattr(MCPClientManager, "_STATIC_RECONNECT_BASE_S", 0.2)
        monkeypatch.setattr(MCPClientManager, "_STATIC_RECONNECT_MAX_S", 0.8)
        monkeypatch.setattr(MCPClientManager, "_STATIC_HEALTH_PING_TIMEOUT_S", 1.5)

        def _spawn_server(*, initial: bool = False) -> subprocess.Popen[bytes]:
            proc = _popen_mcp_server(script, port)
            if not _wait_tcp_ready(port, 10.0):
                proc.kill()
                proc.wait(timeout=5)
                if initial:
                    # Environment gap (loaded CI runner, sandboxed sockets) —
                    # not a regression signal. Mid-test respawns DO fail: the
                    # server already bound once, so a vanishing rebind is real.
                    pytest.skip("flaky-server subprocess did not come up")
                raise AssertionError("flaky server did not come back up mid-test")
            return proc

        proc: subprocess.Popen[bytes] | None = None
        mgr: MCPClientManager | None = None
        try:
            proc = _spawn_server(initial=True)
            with patch(
                "turnstone.core.mcp_client.load_config",
                return_value={"static_health_check_seconds": 0.4},
            ):
                mgr = MCPClientManager(
                    {"flaky": {"type": "http", "url": f"http://127.0.0.1:{port}/mcp"}}
                )
            mgr.start()
            assert _wait_session_live(mgr, "flaky", 8.0), "initial connect failed"

            for _cycle in range(3):
                proc.send_signal(signal.SIGKILL)
                proc.wait()
                time.sleep(0.6)  # dead window: health loop sees the corpse
                proc = _spawn_server()
                assert _wait_session_live(mgr, "flaky", 10.0), (
                    f"no reconnect after flap cycle {_cycle}"
                )

            # Let in-flight teardown/backoff machinery fully settle.
            time.sleep(1.5)

            assert mgr._loop is not None
            armed = asyncio.run_coroutine_threadsafe(_armed_scope_count(), mgr._loop).result(
                timeout=10
            )
            owners = asyncio.run_coroutine_threadsafe(_live_owner_count(), mgr._loop).result(
                timeout=10
            )
            health = mgr._static_health_task

            # The production failure signature: one armed scope per flap cycle.
            assert armed == 0, f"{armed} armed cancel scope(s) — the CPU-spin signature"
            # Exactly the current session's owner is alive; the flapped ones
            # all unwound instead of leaking.
            assert owners == 1
            # The recovery machinery itself survived every flap.
            assert health is not None and not health.done()
            # The structural fix did the work — the disarm backstop never ran.
            assert mgr._last_scope_disarm == 0.0

            # And the recovered session actually dispatches.
            out = mgr.call_tool_sync("mcp__flaky__ping_me", {"x": 41}, timeout=10)
            assert "42" in out
        finally:
            if mgr is not None:
                mgr.shutdown()
            if proc is not None:
                proc.send_signal(signal.SIGKILL)
                proc.wait(timeout=5)
