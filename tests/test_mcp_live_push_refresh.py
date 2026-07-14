"""Live push-refresh smoke test: a real ``tools/list_changed`` lands, no wedge.

End-to-end regression for #839: the static-path notification handler used to
await its catalog refresh inline in the SDK's receive loop, but the refresh
issues a request on the SAME session — a request whose response only that
(now parked) receive loop could route. The refresh never completed, the
receive loop wedged permanently, and every call on the shared per-node
session stalled behind it; the only "recovery" was the health loop's ping
timeout tearing the transport down.

A real streamable-http MCP server (FastMCP, subprocess) registers an extra
tool at runtime inside a tool call and pushes ``notifications/tools/
list_changed`` on the live session — so the notification and the call result
are multiplexed on the real SDK receive loop, exactly the production shape.
Pass criteria are discriminating on purpose:

* the TRIGGERING ``call_tool`` returns promptly — on the pre-fix code its
  result could never route past the parked receive loop, so this call is
  itself the deadlock repro;
* the pushed catalog change lands on the SAME session object (health loop
  slowed to keep teardown/reconnect out of the picture) — the push did the
  work, not a rebuild;
* the newly pushed tool DISPATCHES — the merged tool map was rebuilt, and
  the receive loop is still routing responses afterwards.

Self-contained (spawns its own server; no LLM backend, no network beyond
127.0.0.1) — deliberately NOT marked ``live``, mirroring
``test_mcp_live_flaky_server.py``. Wall clock ~5s.
"""

from __future__ import annotations

import textwrap
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from tests.conftest import (
    _free_port,
    _poll_until,
    _popen_mcp_server,
    _wait_session_live,
    _wait_tcp_ready,
)
from turnstone.core.mcp_client import MCPClientManager

if TYPE_CHECKING:
    import subprocess
    from pathlib import Path

SERVER_SRC = textwrap.dedent(
    '''
    """Streamable-http MCP server that grows a tool at runtime (#839 repro)."""
    import sys

    from mcp.server.fastmcp import Context, FastMCP

    port = int(sys.argv[1])
    mcp = FastMCP("push-victim", host="127.0.0.1", port=port)


    @mcp.tool()
    def ping_me(x: int) -> int:
        """Return x + 1."""
        return x + 1


    def extra_tool(y: int) -> int:
        """Return y * 2."""
        return y * 2


    @mcp.tool()
    async def register_extra(ctx: Context) -> str:
        """Register extra_tool, then push tools/list_changed on this session."""
        mcp.add_tool(extra_tool)
        await ctx.session.send_tool_list_changed()
        return "registered"


    if __name__ == "__main__":
        mcp.run(transport="streamable-http")
    '''
).lstrip()


def _wait_tool_visible(mgr: MCPClientManager, server: str, tool: str, timeout: float) -> bool:
    """Poll the per-server catalog for *tool* — the push refresh landing."""

    def _visible() -> bool:
        state = mgr._static_servers.get(server)
        return state is not None and any(t["function"]["name"] == tool for t in state.tools)

    return _poll_until(_visible, timeout)


class TestPushRefreshNoDeadlock:
    def test_list_changed_push_refreshes_without_teardown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The subprocess runs sys.executable, so importability HERE is a
        # faithful proxy for the server side. Environment gaps skip, not fail.
        pytest.importorskip("mcp.server.fastmcp")
        script = tmp_path / "push_srv.py"
        script.write_text(SERVER_SRC)
        port = _free_port()

        # Bound connect/discovery/refresh phases for a unit-test budget, but
        # SLOW the health loop right down: pre-fix, its ping-timeout teardown
        # was the accidental recovery path, and this test must prove the push
        # itself does the work on the ORIGINAL session.
        monkeypatch.setattr(MCPClientManager, "_CONNECT_TIMEOUT", 5)
        monkeypatch.setattr(MCPClientManager, "_TCP_PROBE_TIMEOUT", 1)

        proc: subprocess.Popen[bytes] | None = None
        mgr: MCPClientManager | None = None
        try:
            proc = _popen_mcp_server(script, port)
            if not _wait_tcp_ready(port, 10.0):
                pytest.skip("push-refresh server subprocess did not come up")
            with patch(
                "turnstone.core.mcp_client.load_config",
                return_value={"static_health_check_seconds": 30},
            ):
                mgr = MCPClientManager(
                    {"push": {"type": "http", "url": f"http://127.0.0.1:{port}/mcp"}}
                )
            mgr.start()
            assert _wait_session_live(mgr, "push", 8.0), "initial connect failed"
            state = mgr._static_servers["push"]
            session_before = state.session
            assert not any(t["function"]["name"] == "mcp__push__extra_tool" for t in state.tools), (
                "extra_tool must not exist before the push"
            )

            # THE repro: the server pushes list_changed while this call is in
            # flight, so its result and the notification share the receive
            # loop. Pre-fix, the inline-await handler parked that loop and
            # this call never returned.
            out = mgr.call_tool_sync("mcp__push__register_extra", {}, timeout=10)
            assert "registered" in out

            # The push-driven refresh completes on its own — no teardown.
            assert _wait_tool_visible(mgr, "push", "mcp__push__extra_tool", 8.0), (
                "pushed tools/list_changed never refreshed the catalog"
            )
            assert mgr._static_servers["push"].session is session_before, (
                "catalog arrived via teardown/reconnect, not via the push refresh"
            )

            # The merged map rebuilt AND the receive loop still routes:
            # the brand-new tool dispatches end-to-end.
            out = mgr.call_tool_sync("mcp__push__extra_tool", {"y": 21}, timeout=10)
            assert "42" in out
        finally:
            if mgr is not None:
                mgr.shutdown()
            if proc is not None:
                proc.kill()
                proc.wait(timeout=5)
