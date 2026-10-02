"""Processes started by a stdio MCP server do not outlive it (#1226).

The SDK starts each server in its own process group but kills the group only
when the server ignores its closed stdin, so a server that exits cleanly used
to leave its helpers running. These tests drive a real stdio MCP server that
starts a helper through ``MCPClientManager``. Self-contained, no network.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest
from mcp import StdioServerParameters
from mcp.client.stdio import stdio_client

from tests.conftest import _poll_until, _wait_session_live
from turnstone.core.mcp_client import (
    MCPClientManager,
    _stdio_server_pids,
    _stop_stdio_server_group,
)

# Starts a helper that inherits its stdio (the issue's reproduction), serves,
# and after its stdin closes takes a moment to shut down before marking that
# its shutdown completed.
SERVER_SRC = textwrap.dedent(
    '''
    """stdio MCP server that starts a helper process."""
    import subprocess, sys, time

    from mcp.server.fastmcp import FastMCP

    pidfile, marker, helper = sys.argv[1], sys.argv[2], sys.argv[3]
    sleep = "import time; time.sleep(120)"
    if helper == "ignores-sigterm":
        sleep = "import signal; signal.signal(signal.SIGTERM, signal.SIG_IGN); " + sleep
    proc = subprocess.Popen([sys.executable, "-c", sleep])
    with open(pidfile, "a") as f:
        print(proc.pid, file=f)

    mcp = FastMCP("helper-spawner")


    @mcp.tool()
    def ping_me(x: int) -> int:
        """Return x + 1."""
        return x + 1


    mcp.run(transport="stdio")
    time.sleep(0.3)
    open(marker, "a").close()
    '''
)


def _gone(pid: int) -> bool:
    """True once *pid* has exited; a zombie awaiting its reaper counts as gone."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat.rsplit(")", 1)[1].split()[0] == "Z"


def _read_pids(path: Path, count: int, timeout: float = 10.0) -> list[int]:
    pids: list[int] = []

    def _ready() -> bool:
        nonlocal pids
        try:
            pids = [int(p) for p in path.read_text().split()]
        except (OSError, ValueError):
            return False
        return len(pids) >= count

    assert _poll_until(_ready, timeout), f"no pids in {path.name}"
    return pids


class _Spawner:
    """A manager running one real stdio server; kills stray helpers on exit."""

    def __init__(self, tmp_path: Path, helper: str = "plain") -> None:
        pytest.importorskip("mcp.server.fastmcp")
        script = tmp_path / "server.py"
        script.write_text(SERVER_SRC)
        self.pidfile, self.marker = tmp_path / "pids", tmp_path / "clean-exit"
        cfg = {
            "type": "stdio",
            "command": sys.executable,
            "args": [str(script), str(self.pidfile), str(self.marker), helper],
        }
        with patch("turnstone.core.mcp_client.load_config", return_value={}):
            self.mgr = MCPClientManager({"spawner": cfg})
        self.shut_down = False

    def __enter__(self) -> _Spawner:
        self.mgr.start()
        assert _wait_session_live(self.mgr, "spawner", 20.0), "initial connect failed"
        assert "42" in self.mgr.call_tool_sync("mcp__spawner__ping_me", {"x": 41}, timeout=10)
        return self

    def shutdown(self) -> None:
        if not self.shut_down:
            self.shut_down = True
            self.mgr.shutdown()

    def __exit__(self, *_exc: object) -> None:
        self.shutdown()
        if self.pidfile.exists():
            for pid in (int(p) for p in self.pidfile.read_text().split()):
                if not _gone(pid):
                    with contextlib.suppress(ProcessLookupError):
                        os.kill(pid, signal.SIGKILL)


@pytest.mark.parametrize("teardown", ["shutdown", "reconnect", "remove"])
def test_no_helper_survives_manager_teardown(tmp_path: Path, teardown: str) -> None:
    with _Spawner(tmp_path) as spawner:
        (first,) = _read_pids(spawner.pidfile, 1)
        if teardown == "shutdown":
            spawner.shutdown()
        elif teardown == "reconnect":
            assert spawner.mgr.reconnect_sync("spawner")["connected"]
            _, second = _read_pids(spawner.pidfile, 2)
            assert not _gone(second)  # the live server's helper is left alone
        else:
            assert spawner.mgr.remove_server_sync("spawner")
        assert _poll_until(lambda: _gone(first), 5.0), f"helper survived {teardown}"
        # The server finished its own shutdown before its group was stopped.
        assert spawner.marker.exists()
        spawner.shutdown()
        pids = _read_pids(spawner.pidfile, 1)
        assert _poll_until(lambda: all(_gone(p) for p in pids), 5.0)


def test_helper_ignoring_sigterm_is_killed_after_the_grace(tmp_path: Path) -> None:
    with _Spawner(tmp_path, helper="ignores-sigterm") as spawner:
        (helper,) = _read_pids(spawner.pidfile, 1)
        assert spawner.mgr.remove_server_sync("spawner")
        assert _poll_until(lambda: _gone(helper), 5.0)


def test_cancelled_owner_still_stops_the_group(tmp_path: Path) -> None:
    """The teardown protocol's cancel escalation: anyio kills only the server."""
    with _Spawner(tmp_path) as spawner:
        (helper,) = _read_pids(spawner.pidfile, 1)
        mgr = spawner.mgr
        assert mgr._loop is not None

        async def _cancel_owner() -> None:
            owner = mgr._static_servers["spawner"].owner_task
            assert owner is not None
            owner.cancel()
            await asyncio.wait({owner}, timeout=10)

        asyncio.run_coroutine_threadsafe(_cancel_owner(), mgr._loop).result(timeout=15)
        assert _poll_until(lambda: _gone(helper), 5.0)


def test_a_live_process_holding_the_group_id_is_never_signalled() -> None:
    """The server is reaped before its group is stopped, so a live process with
    its pid means the id was reused by an unrelated group leader."""
    sleeper = [sys.executable, "-c", "import time; time.sleep(30)"]
    with subprocess.Popen(sleeper, start_new_session=True) as stranger:
        try:
            asyncio.run(_stop_stdio_server_group(stranger.pid))
            assert stranger.poll() is None
        finally:
            stranger.kill()


def test_spawn_hook_records_the_server_pid() -> None:
    """Pins the private SDK spawn function the group stop depends on."""
    params = StdioServerParameters(command=sys.executable, args=["-c", "input()"])

    async def _spawn() -> list[int]:
        sink: list[int] = []
        _stdio_server_pids.set(sink)
        async with stdio_client(params):
            pass
        return sink

    assert len(asyncio.run(_spawn())) == 1
