"""End-to-end MCP integration test — the only test that touches the network.

Boots the real Understone FastMCP app (backed by a temp DB) in a uvicorn
thread, then drives it over the real streamable-HTTP wire with the real MCP
client: initialize, list_tools (all nine door_* names), join, look. A second
client session joins a second adventurer in the SAME process and world, and
the first player's view then shows the '&' other-player marker — proving the
shared-world, single-process contract over a real wire.

A second test drives the read-only Watch routes that ride inside the same app:
GET /watch (the HTML page), /watch/world.json (the static map), and
/watch/state.json (the live snapshot) — confirming the spectator endpoints
serve real world data alongside a working /mcp without breaking either.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from typing import TYPE_CHECKING, Any

import httpx
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from understone import server as understone_server

if TYPE_CHECKING:
    from pathlib import Path

PACK = str(understone_server._PACKAGED_WORLD)


def _find_free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return int(port)


def _build_server(port: int, db_path: str) -> uvicorn.Server:
    app = understone_server.create_app(db_path, PACK)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)
    return uvicorn.Server(config)


def _wait_ready(port: int, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.05)
    raise TimeoutError(f"understone server at 127.0.0.1:{port} not ready after {timeout}s")


@pytest.fixture
def live_server(tmp_path: Path) -> Any:
    """Boot the real Understone app in a background uvicorn thread."""
    port = _find_free_port()
    db_path = str(tmp_path / "wire.db")
    server = _build_server(port, db_path)

    def _run() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(server.serve())

    thread = threading.Thread(target=_run, daemon=True, name="understone-itest")
    thread.start()
    try:
        _wait_ready(port)
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        # create_app installed a module-level game whose Store holds an open
        # SQLite connection; close it and clear the singleton so the next test
        # builds its own rather than inheriting this temp DB.
        if understone_server._GAME is not None:
            understone_server._GAME.store.close()
            understone_server._GAME = None
        # FastMCP caches a StreamableHTTPSessionManager on the module-level mcp
        # singleton and refuses a second lifespan .run() on the same instance.
        # Reset it so each fixture instance boots a fresh session manager (the
        # production server only ever runs one). Without this, a second
        # fixture-using test fails on "run() can only be called once".
        understone_server.mcp._session_manager = None


async def _call_text(session: ClientSession, name: str, arguments: dict[str, Any]) -> str:
    result = await session.call_tool(name, arguments)
    chunks = [block.text for block in result.content if getattr(block, "type", None) == "text"]
    return "\n".join(chunks)


async def _drive(url: str) -> dict[str, Any]:
    """Run the full client conversation and return observations."""
    observations: dict[str, Any] = {}
    async with (
        streamable_http_client(url) as (read, write, _get_session_id),
        ClientSession(read, write) as session,
    ):
        await session.initialize()

        tools = await session.list_tools()
        observations["tool_names"] = sorted(t.name for t in tools.tools)

        observations["join_one"] = await _call_text(session, "door_join", {"player": "Brandr"})
        observations["look_one_before"] = await _call_text(
            session, "door_look", {"player": "Brandr"}
        )

    # A SECOND, independent session joins a second adventurer in the same world.
    async with (
        streamable_http_client(url) as (read, write, _get_session_id),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        # Place player two adjacent to player one so they share the view.
        await _call_text(session, "door_join", {"player": "Sigrun"})
        await _call_text(
            session, "door_move", {"player": "Sigrun", "heading": "east", "distance": 1}
        )

    # Back as player one: the shared world now shows the other adventurer.
    async with (
        streamable_http_client(url) as (read, write, _get_session_id),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        observations["look_one_after"] = await _call_text(
            session, "door_look", {"player": "Brandr"}
        )
        observations["rank"] = await _call_text(session, "door_rank", {"player": "Brandr"})

    return observations


def test_mcp_end_to_end(live_server: str) -> None:
    obs = asyncio.run(_drive(live_server))

    # All nine tools are advertised over the wire.
    expected = {
        "door_help",
        "door_join",
        "door_status",
        "door_look",
        "door_move",
        "door_action",
        "door_log",
        "door_rank",
        "door_bestow",
    }
    assert set(obs["tool_names"]) == expected

    # The join + look frames are real ASCII map frames.
    assert "@" in obs["join_one"]
    look_before = obs["look_one_before"]
    assert "@" in look_before
    assert "┌" in look_before and "┐" in look_before

    # Shared-world proof: after player two joins next door, player one sees '&'.
    assert "&" in obs["look_one_after"]
    # And the leaderboard lists both adventurers (one process, one world).
    assert "Brandr" in obs["rank"]
    assert "Sigrun" in obs["rank"]


def _watch_base(mcp_url: str) -> str:
    """Derive the app root (where /watch lives) from the /mcp endpoint URL."""
    return mcp_url[: -len("/mcp")] if mcp_url.endswith("/mcp") else mcp_url


async def _join_over_mcp(mcp_url: str, name: str) -> None:
    """Sign one adventurer in over the real MCP wire (so state.json sees them)."""
    async with (
        streamable_http_client(mcp_url) as (read, write, _get_session_id),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        await _call_text(session, "door_join", {"player": name})


def test_watch_routes_serve_world_state(live_server: str) -> None:
    base = _watch_base(live_server)

    # The MCP join writes the player into the shared world the routes read.
    asyncio.run(_join_over_mcp(live_server, "Watcher"))

    with httpx.Client(timeout=5.0) as client:
        page = client.get(f"{base}/watch")
        world = client.get(f"{base}/watch/world.json")
        state = client.get(f"{base}/watch/state.json")

    # The page is real HTML carrying the static masthead.
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert "Understone — Live Watch" in page.text

    # The static world payload matches the loaded world.
    assert world.status_code == 200
    world_body = world.json()
    assert world_body["width"] == 96
    assert world_body["height"] == 48
    assert len(world_body["glyph_rows"]) == world_body["height"]
    assert all(len(row) == world_body["width"] for row in world_body["glyph_rows"])

    # The live snapshot lists the adventurer who joined over MCP.
    assert state.status_code == 200
    state_body = state.json()
    names = {p["name"] for p in state_body["players"]}
    assert "Watcher" in names


def test_watch_routes_coexist_with_mcp(live_server: str) -> None:
    """The custom routes don't shadow /mcp: tool calls still work alongside them."""
    base = _watch_base(live_server)

    async def _drive_both() -> tuple[str, int]:
        async with (
            streamable_http_client(live_server) as (read, write, _get_session_id),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            joined = await _call_text(session, "door_join", {"player": "Coexist"})
        with httpx.Client(timeout=5.0) as client:
            status = client.get(f"{base}/watch/state.json").status_code
        return joined, status

    joined, watch_status = asyncio.run(_drive_both())
    assert "@" in joined  # the MCP tool still returns a real frame
    assert watch_status == 200  # and the watch route still answers
