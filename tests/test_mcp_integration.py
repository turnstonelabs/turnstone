"""Integration tests for MCPClientManager data flow.

Uses real storage (SQLite) and real MCPClientManager state manipulation,
but mock MCP sessions instead of wire-protocol connections. This validates
the full data pipeline: per-server data -> rebuild -> merged state ->
query methods -> storage sync -> shutdown cleanup.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from turnstone.core.mcp_client import MCPClientManager
from turnstone.core.storage._sqlite import SQLiteBackend

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_resource(
    uri: str, name: str, server: str, description: str = "", mime: str = "text/plain"
) -> dict[str, Any]:
    return {
        "uri": uri,
        "name": name,
        "description": description,
        "mimeType": mime,
        "server": server,
    }


def _make_prompt(
    prefixed_name: str,
    original_name: str,
    server: str,
    description: str = "",
    arguments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "name": prefixed_name,
        "original_name": original_name,
        "server": server,
        "description": description,
        "arguments": arguments or [],
    }


def _make_mock_session(
    read_resource_result: Any = None,
    get_prompt_result: Any = None,
) -> AsyncMock:
    """Build a mock ClientSession with configurable async return values."""
    session = AsyncMock()

    if read_resource_result is not None:
        session.read_resource.return_value = read_resource_result
    else:
        # Default: single text content
        content_item = MagicMock()
        content_item.text = "resource content"
        result = MagicMock()
        result.contents = [content_item]
        session.read_resource.return_value = result

    if get_prompt_result is not None:
        session.get_prompt.return_value = get_prompt_result
    else:
        msg = MagicMock()
        msg.role = "user"
        msg.content = MagicMock()
        msg.content.text = "Hello, World!"
        result = MagicMock()
        result.messages = [msg]
        session.get_prompt.return_value = result

    return session


# ---------------------------------------------------------------------------
# Integration test class
# ---------------------------------------------------------------------------


class TestFullLifecycleResourcesPrompts:
    """Integration test exercising real code paths with real SQLite storage
    but mock MCP sessions.

    Validates the complete data flow: per-server data population, rebuild
    merging, query methods, resource/prompt dispatch through asyncio, storage
    sync, and shutdown cleanup.
    """

    @pytest.fixture()
    def mgr(self) -> MCPClientManager:
        """Create an MCPClientManager with no server configs (no start())."""
        return MCPClientManager({})

    @pytest.fixture()
    def db(self, tmp_path) -> SQLiteBackend:
        """Create a fresh SQLite backend for each test."""
        backend = SQLiteBackend(str(tmp_path / "test.db"))
        yield backend
        backend.close()

    def test_rebuild_resources_produces_merged_state(self, mgr: MCPClientManager) -> None:
        """_rebuild_resources merges per-server resources into a unified list."""
        mgr._per_server_resources["alpha"] = [
            _make_resource("file:///a.txt", "a", "alpha"),
            _make_resource("file:///b.txt", "b", "alpha"),
        ]
        mgr._per_server_resources["beta"] = [
            _make_resource("file:///c.txt", "c", "beta"),
        ]

        mgr._rebuild_resources()

        resources = mgr.get_resources()
        assert len(resources) == 3
        uris = {r["uri"] for r in resources}
        assert uris == {"file:///a.txt", "file:///b.txt", "file:///c.txt"}
        # resource_map should have entries for all non-template resources
        assert "file:///a.txt" in mgr._resource_map
        assert "file:///c.txt" in mgr._resource_map
        assert mgr.resource_count == 3

    def test_rebuild_prompts_produces_merged_state(self, mgr: MCPClientManager) -> None:
        """_rebuild_prompts merges per-server prompts into a unified list."""
        mgr._per_server_prompts["alpha"] = [
            _make_prompt("mcp__alpha__greet", "greet", "alpha", "Say hello"),
        ]
        mgr._per_server_prompts["beta"] = [
            _make_prompt("mcp__beta__summarize", "summarize", "beta", "Summarize text"),
            _make_prompt("mcp__beta__translate", "translate", "beta", "Translate text"),
        ]

        mgr._rebuild_prompts()

        prompts = mgr.get_prompts()
        assert len(prompts) == 3
        names = {p["name"] for p in prompts}
        assert names == {"mcp__alpha__greet", "mcp__beta__summarize", "mcp__beta__translate"}
        # prompt_map should map prefixed -> (server, original)
        assert mgr._prompt_map["mcp__alpha__greet"] == ("alpha", "greet")
        assert mgr._prompt_map["mcp__beta__summarize"] == ("beta", "summarize")
        assert mgr.prompt_count == 3
        assert mgr.is_mcp_prompt("mcp__alpha__greet") is True
        assert mgr.is_mcp_prompt("nonexistent") is False

    def test_read_resource_sync_dispatches_correctly(self, mgr: MCPClientManager) -> None:
        """read_resource_sync dispatches to the correct session via a real asyncio loop."""
        # Set up a real event loop in a thread (simulating start())
        loop = asyncio.new_event_loop()
        import threading

        thread = threading.Thread(target=loop.run_forever, daemon=True)
        thread.start()
        mgr._loop = loop

        try:
            # Populate session and resource map
            session = _make_mock_session()
            mgr._sessions["alpha"] = session
            mgr._per_server_resources["alpha"] = [
                _make_resource("file:///readme.md", "readme", "alpha"),
            ]
            mgr._rebuild_resources()

            result = mgr.read_resource_sync("file:///readme.md", timeout=5)
            assert result == "resource content"
            session.read_resource.assert_awaited_once_with("file:///readme.md")
        finally:
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=5)
            loop.close()

    def test_read_resource_sync_unknown_uri_raises(self, mgr: MCPClientManager) -> None:
        """read_resource_sync raises ValueError for an unknown URI."""
        with pytest.raises(ValueError, match="Unknown MCP resource"):
            mgr.read_resource_sync("file:///nonexistent")

    def test_get_prompt_sync_dispatches_correctly(self, mgr: MCPClientManager) -> None:
        """get_prompt_sync dispatches to the correct session via a real asyncio loop."""
        loop = asyncio.new_event_loop()
        import threading

        thread = threading.Thread(target=loop.run_forever, daemon=True)
        thread.start()
        mgr._loop = loop

        try:
            session = _make_mock_session()
            mgr._sessions["alpha"] = session
            mgr._per_server_prompts["alpha"] = [
                _make_prompt("mcp__alpha__greet", "greet", "alpha", "Say hello"),
            ]
            mgr._rebuild_prompts()

            messages = mgr.get_prompt_sync(
                "mcp__alpha__greet", arguments={"name": "World"}, timeout=5
            )
            assert len(messages) == 1
            assert messages[0]["role"] == "user"
            assert messages[0]["content"] == "Hello, World!"
            session.get_prompt.assert_awaited_once_with("greet", arguments={"name": "World"})
        finally:
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=5)
            loop.close()

    def test_get_prompt_sync_unknown_name_raises(self, mgr: MCPClientManager) -> None:
        """get_prompt_sync raises ValueError for an unknown prompt name."""
        with pytest.raises(ValueError, match="Unknown MCP prompt"):
            mgr.get_prompt_sync("mcp__nosrv__nope")

    def test_sync_prompts_to_storage_creates_templates(
        self, mgr: MCPClientManager, db: SQLiteBackend
    ) -> None:
        """sync_prompts_to_storage creates governance templates in real SQLite."""
        mgr.set_storage(db)
        mgr._prompts = [
            _make_prompt(
                "mcp__alpha__greet",
                "greet",
                "alpha",
                "Say hello",
                [{"name": "user", "description": "Who to greet", "required": True}],
            ),
            _make_prompt(
                "mcp__beta__summarize",
                "summarize",
                "beta",
                "Summarize text",
            ),
        ]
        # Mark connected so set_storage triggers sync
        mgr._connected.set()
        # Re-set storage to trigger auto-sync
        mgr.set_storage(db)

        templates = db.list_prompt_templates()
        assert len(templates) == 2
        names = {t["name"] for t in templates}
        assert names == {"mcp__alpha__greet", "mcp__beta__summarize"}

        # Verify details on first template
        tpl = db.get_prompt_template_by_name("mcp__alpha__greet")
        assert tpl is not None
        assert tpl["origin"] == "mcp"
        assert tpl["mcp_server"] == "alpha"
        assert tpl["readonly"] is True
        assert tpl["category"] == "mcp"
        assert "user" in tpl["variables"]

    def test_sync_prompts_removes_stale_templates(
        self, mgr: MCPClientManager, db: SQLiteBackend
    ) -> None:
        """sync_prompts_to_storage removes templates whose MCP prompts are gone."""
        mgr.set_storage(db)

        # Create an initial template via sync
        mgr._prompts = [
            _make_prompt("mcp__alpha__old", "old", "alpha", "Old prompt"),
        ]
        mgr.sync_prompts_to_storage()
        assert len(db.list_prompt_templates()) == 1

        # Now the prompt is gone
        mgr._prompts = []
        result = mgr.sync_prompts_to_storage()
        assert result["removed"] == ["mcp__alpha__old"]
        assert len(db.list_prompt_templates()) == 0

    def test_shutdown_clears_all_state(self, mgr: MCPClientManager) -> None:
        """shutdown() clears sessions, tools, resources, prompts, and listeners."""
        # Populate state
        mgr._sessions["alpha"] = MagicMock()
        mgr._per_server_tools["alpha"] = [
            {
                "type": "function",
                "function": {
                    "name": "mcp__alpha__search",
                    "description": "Search",
                    "parameters": {},
                },
            }
        ]
        mgr._rebuild_tools()
        mgr._per_server_resources["alpha"] = [
            _make_resource("file:///a.txt", "a", "alpha"),
        ]
        mgr._rebuild_resources()
        mgr._per_server_prompts["alpha"] = [
            _make_prompt("mcp__alpha__greet", "greet", "alpha"),
        ]
        mgr._rebuild_prompts()
        mgr._listeners.append(lambda: None)
        mgr._resource_listeners.append(lambda: None)
        mgr._prompt_listeners.append(lambda: None)

        # Verify populated
        assert len(mgr._sessions) == 1
        assert len(mgr._tools) == 1
        assert len(mgr._resources) == 1
        assert len(mgr._prompts) == 1

        mgr.shutdown()

        assert len(mgr._sessions) == 0
        assert len(mgr._tools) == 0
        assert len(mgr._tool_map) == 0
        assert len(mgr._resources) == 0
        assert len(mgr._resource_map) == 0
        assert len(mgr._prompts) == 0
        assert len(mgr._prompt_map) == 0
        assert len(mgr._listeners) == 0
        assert len(mgr._resource_listeners) == 0
        assert len(mgr._prompt_listeners) == 0

    def test_listener_notifications_fire_on_rebuild(self, mgr: MCPClientManager) -> None:
        """Rebuild methods fire the appropriate listener callbacks."""
        tool_fired = []
        resource_fired = []
        prompt_fired = []
        mgr.add_listener(lambda: tool_fired.append(1))
        mgr.add_resource_listener(lambda: resource_fired.append(1))
        mgr.add_prompt_listener(lambda: prompt_fired.append(1))

        mgr._per_server_tools["alpha"] = []
        mgr._rebuild_tools()
        assert len(tool_fired) == 1

        mgr._per_server_resources["alpha"] = [
            _make_resource("file:///x.txt", "x", "alpha"),
        ]
        mgr._rebuild_resources()
        assert len(resource_fired) == 1

        mgr._per_server_prompts["alpha"] = [
            _make_prompt("mcp__alpha__p1", "p1", "alpha"),
        ]
        mgr._rebuild_prompts()
        assert len(prompt_fired) == 1

        # Tool and resource listeners should not have been fired again
        assert len(tool_fired) == 1
        assert len(resource_fired) == 1
