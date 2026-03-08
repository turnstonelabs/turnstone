"""Tests for turnstone.core.mcp_client — MCP client manager and config loading."""

from __future__ import annotations

import asyncio
import json
from contextlib import AsyncExitStack
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from turnstone.core.mcp_client import (
    MCPClientManager,
    _mcp_to_openai,
    load_mcp_config,
)
from turnstone.core.tools import TOOLS, merge_mcp_tools

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_mcp_tool(name: str = "search", description: str = "Search stuff") -> MagicMock:
    """Create a mock MCP tool object matching the SDK's Tool type."""
    tool = MagicMock()
    tool.name = name
    tool.description = description
    tool.inputSchema = {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    }
    return tool


def _fake_openai_tool(name: str = "mcp__test__search") -> dict[str, Any]:
    """Create a fake OpenAI-format tool dict."""
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "[MCP: test] Search stuff",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    }


# ---------------------------------------------------------------------------
# Schema conversion
# ---------------------------------------------------------------------------


class TestMcpToOpenai:
    def test_basic_conversion(self):
        tool = _fake_mcp_tool("search_repos", "Search GitHub repos")
        result = _mcp_to_openai("github", tool)

        assert result["type"] == "function"
        func = result["function"]
        assert func["name"] == "mcp__github__search_repos"
        assert "[MCP: github]" in func["description"]
        assert func["parameters"]["type"] == "object"
        assert "query" in func["parameters"]["properties"]

    def test_name_prefixing(self):
        tool = _fake_mcp_tool("list_files")
        result = _mcp_to_openai("fs", tool)
        assert result["function"]["name"] == "mcp__fs__list_files"

    def test_missing_input_schema(self):
        tool = MagicMock()
        tool.name = "ping"
        tool.description = "Ping the server"
        tool.inputSchema = None
        result = _mcp_to_openai("test", tool)
        assert result["function"]["parameters"] == {"type": "object", "properties": {}}

    def test_empty_description(self):
        tool = MagicMock()
        tool.name = "noop"
        tool.description = ""
        tool.inputSchema = {"type": "object", "properties": {}}
        result = _mcp_to_openai("test", tool)
        assert result["function"]["description"] == "[MCP: test] "


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


class TestLoadMcpConfig:
    def test_load_from_json_file(self, tmp_path):
        config_file = tmp_path / "mcp.json"
        config_file.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "github": {
                            "command": "npx",
                            "args": ["-y", "@modelcontextprotocol/server-github"],
                            "env": {"GITHUB_TOKEN": "test"},
                        }
                    }
                }
            )
        )
        result = load_mcp_config(str(config_file))
        assert "github" in result
        assert result["github"]["command"] == "npx"
        assert result["github"]["env"]["GITHUB_TOKEN"] == "test"

    def test_load_from_toml(self):
        mock_config = {
            "servers": {
                "postgres": {
                    "type": "http",
                    "url": "https://mcp.example.com/mcp",
                }
            }
        }
        with patch("turnstone.core.mcp_client.load_config", return_value=mock_config):
            result = load_mcp_config(None)
        assert "postgres" in result
        assert result["postgres"]["url"] == "https://mcp.example.com/mcp"

    def test_empty_when_no_config(self):
        with patch("turnstone.core.mcp_client.load_config", return_value={}):
            result = load_mcp_config(None)
        assert result == {}

    def test_json_file_not_found(self, tmp_path):
        with patch("turnstone.core.mcp_client.load_config", return_value={}):
            result = load_mcp_config(str(tmp_path / "nonexistent.json"))
        assert result == {}

    def test_toml_config_path_redirect(self):
        """TOML [mcp] config_path redirects to JSON file."""
        # load_config returns a section with config_path pointing to a nonexistent file
        mock_config = {"config_path": "/tmp/nonexistent_mcp.json"}
        with patch("turnstone.core.mcp_client.load_config", return_value=mock_config):
            result = load_mcp_config(None)
        assert result == {}

    def test_invalid_json(self, tmp_path):
        config_file = tmp_path / "bad.json"
        config_file.write_text("not json")
        with patch("turnstone.core.mcp_client.load_config", return_value={}):
            result = load_mcp_config(str(config_file))
        assert result == {}


# ---------------------------------------------------------------------------
# merge_mcp_tools
# ---------------------------------------------------------------------------


class TestMergeTools:
    def test_merge_preserves_builtin(self):
        mcp_tools = [_fake_openai_tool()]
        merged = merge_mcp_tools(TOOLS, mcp_tools)
        # First N should be built-in
        for i, t in enumerate(TOOLS):
            assert merged[i] is t

    def test_merge_appends_mcp(self):
        mcp_tools = [_fake_openai_tool("mcp__a__x"), _fake_openai_tool("mcp__b__y")]
        merged = merge_mcp_tools(TOOLS, mcp_tools)
        assert len(merged) == len(TOOLS) + 2
        assert merged[-2]["function"]["name"] == "mcp__a__x"
        assert merged[-1]["function"]["name"] == "mcp__b__y"

    def test_merge_empty_mcp(self):
        merged = merge_mcp_tools(TOOLS, [])
        assert merged == TOOLS

    def test_merge_does_not_mutate_input(self):
        mcp_tools = [_fake_openai_tool()]
        original_len = len(TOOLS)
        merge_mcp_tools(TOOLS, mcp_tools)
        assert len(TOOLS) == original_len


# ---------------------------------------------------------------------------
# MCPClientManager unit tests (no real MCP servers)
# ---------------------------------------------------------------------------


class TestMCPClientManager:
    def test_init_state(self):
        mgr = MCPClientManager({"test": {"command": "echo"}})
        assert mgr.get_tools() == []
        assert mgr.is_mcp_tool("anything") is False
        assert mgr.server_count == 0

    def test_get_tools_returns_copy(self):
        mgr = MCPClientManager({})
        mgr._tools = [_fake_openai_tool()]
        tools = mgr.get_tools()
        assert len(tools) == 1
        tools.clear()  # mutate the copy
        assert len(mgr.get_tools()) == 1  # original unchanged

    def test_is_mcp_tool(self):
        mgr = MCPClientManager({})
        mgr._tool_map["mcp__gh__search"] = ("gh", "search")
        assert mgr.is_mcp_tool("mcp__gh__search") is True
        assert mgr.is_mcp_tool("bash") is False

    def test_server_count(self):
        mgr = MCPClientManager({})
        mgr._sessions["a"] = MagicMock()
        mgr._sessions["b"] = MagicMock()
        assert mgr.server_count == 2

    def test_call_tool_sync_unknown_tool(self):
        mgr = MCPClientManager({})
        with pytest.raises(ValueError, match="Unknown MCP tool"):
            mgr.call_tool_sync("mcp__no__such", {})

    def test_call_tool_sync_disconnected_server(self):
        mgr = MCPClientManager({})
        mgr._tool_map["mcp__dead__ping"] = ("dead", "ping")
        # No session registered for "dead"
        with pytest.raises(RuntimeError, match="not connected"):
            mgr.call_tool_sync("mcp__dead__ping", {})

    def test_shutdown_on_unstarted_manager(self):
        """shutdown() should not raise when called on a manager that was never started."""
        mgr = MCPClientManager({})
        mgr.shutdown()  # should be a no-op


# ---------------------------------------------------------------------------
# Session integration (mock MCP client)
# ---------------------------------------------------------------------------


class TestSessionIntegration:
    @pytest.fixture()
    def tmp_db(self, tmp_path):
        from turnstone.core.storage import init_storage, reset_storage

        reset_storage()
        init_storage("sqlite", path=str(tmp_path / "test.db"), run_migrations=False)
        yield
        reset_storage()

    def _make_session(self, mcp_client=None, **kwargs):
        from turnstone.core.session import ChatSession

        defaults: dict[str, Any] = dict(
            client=MagicMock(),
            model="test-model",
            ui=MagicMock(),
            instructions=None,
            temperature=0.5,
            max_tokens=4096,
            tool_timeout=30,
            mcp_client=mcp_client,
        )
        defaults.update(kwargs)
        return ChatSession(**defaults)

    def test_session_without_mcp(self, tmp_db):
        session = self._make_session(mcp_client=None)
        assert session._tools is TOOLS
        assert session._mcp_client is None

    def test_session_with_mcp(self, tmp_db):
        mock_mcp = MagicMock()
        mock_mcp.get_tools.return_value = [_fake_openai_tool()]
        session = self._make_session(mcp_client=mock_mcp)
        assert len(session._tools) == len(TOOLS) + 1
        assert session._tools[-1]["function"]["name"] == "mcp__test__search"

    def test_task_tools_include_mcp(self, tmp_db):
        from turnstone.core.tools import TASK_AGENT_TOOLS

        mock_mcp = MagicMock()
        mock_mcp.get_tools.return_value = [_fake_openai_tool()]
        session = self._make_session(mcp_client=mock_mcp)
        assert len(session._task_tools) == len(TASK_AGENT_TOOLS) + 1

    def test_agent_tools_include_mcp(self, tmp_db):
        from turnstone.core.tools import AGENT_TOOLS

        mock_mcp = MagicMock()
        mock_mcp.get_tools.return_value = [_fake_openai_tool()]
        session = self._make_session(mcp_client=mock_mcp)
        assert len(session._agent_tools) == len(AGENT_TOOLS) + 1

    def test_prepare_mcp_tool(self, tmp_db):
        mock_mcp = MagicMock()
        mock_mcp.get_tools.return_value = [_fake_openai_tool()]
        mock_mcp.is_mcp_tool.return_value = True
        session = self._make_session(mcp_client=mock_mcp)

        tc = {
            "id": "call_123",
            "function": {
                "name": "mcp__test__search",
                "arguments": '{"query": "hello"}',
            },
        }
        prepared = session._prepare_tool(tc)
        assert prepared["func_name"] == "mcp__test__search"
        assert prepared["needs_approval"] is True
        assert "mcp:test/search" in prepared["header"]
        assert callable(prepared["execute"])

    def test_unknown_tool_without_mcp(self, tmp_db):
        session = self._make_session(mcp_client=None)
        tc = {
            "id": "call_456",
            "function": {"name": "nonexistent", "arguments": "{}"},
        }
        prepared = session._prepare_tool(tc)
        assert "error" in prepared
        assert "Unknown tool" in prepared["error"]

    def test_mcp_command_no_client(self, tmp_db):
        session = self._make_session(mcp_client=None)
        session.handle_command("/mcp")
        session.ui.on_info.assert_called_once()
        assert "No MCP servers" in session.ui.on_info.call_args[0][0]

    def test_mcp_command_with_tools(self, tmp_db):
        mock_mcp = MagicMock()
        mock_mcp.get_tools.return_value = [_fake_openai_tool()]
        session = self._make_session(mcp_client=mock_mcp)
        session.handle_command("/mcp")
        session.ui.on_info.assert_called_once()
        output = session.ui.on_info.call_args[0][0]
        assert "MCP tools (1)" in output
        assert "mcp__test__search" in output

    def test_exec_mcp_tool(self, tmp_db):
        mock_mcp = MagicMock()
        mock_mcp.get_tools.return_value = [_fake_openai_tool()]
        mock_mcp.is_mcp_tool.return_value = True
        mock_mcp.call_tool_sync.return_value = "result text"
        session = self._make_session(mcp_client=mock_mcp)

        item = {
            "call_id": "call_789",
            "mcp_func_name": "mcp__test__search",
            "mcp_args": {"query": "hello"},
        }
        call_id, output = session._exec_mcp_tool(item)
        assert call_id == "call_789"
        assert output == "result text"
        mock_mcp.call_tool_sync.assert_called_once_with(
            "mcp__test__search", {"query": "hello"}, timeout=30
        )

    def test_exec_mcp_tool_error(self, tmp_db):
        mock_mcp = MagicMock()
        mock_mcp.get_tools.return_value = [_fake_openai_tool()]
        mock_mcp.is_mcp_tool.return_value = True
        mock_mcp.call_tool_sync.side_effect = RuntimeError("server crashed")
        session = self._make_session(mcp_client=mock_mcp)

        item = {
            "call_id": "call_err",
            "mcp_func_name": "mcp__test__search",
            "mcp_args": {"query": "hello"},
        }
        call_id, output = session._exec_mcp_tool(item)
        assert call_id == "call_err"
        assert "MCP tool error" in output
        assert "server crashed" in output


# ---------------------------------------------------------------------------
# Server name validation
# ---------------------------------------------------------------------------


class TestServerNameValidation:
    def test_double_underscore_in_name(self):
        """Server names with __ should be rejected during _connect_one."""
        import asyncio

        async def _run() -> None:
            mgr = MCPClientManager({"my__bad": {"command": "echo"}})
            async with AsyncExitStack() as stack:
                mgr._exit_stack = stack
                await mgr._connect_one("my__bad", {"command": "echo"})
            # Should not have connected
            assert "my__bad" not in mgr._sessions
            assert mgr.get_tools() == []

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# create_mcp_client guard
# ---------------------------------------------------------------------------


class TestCreateMcpClient:
    def test_returns_none_when_no_config(self):
        with patch("turnstone.core.mcp_client.load_mcp_config", return_value={}):
            from turnstone.core.mcp_client import create_mcp_client

            result = create_mcp_client()
            assert result is None


# ---------------------------------------------------------------------------
# Tool refresh — _rebuild_tools, _refresh_server, listeners
# ---------------------------------------------------------------------------


class TestRebuildTools:
    def test_rebuild_from_per_server(self):
        mgr = MCPClientManager({})
        mgr._per_server_tools = {
            "github": [_fake_openai_tool("mcp__github__search")],
            "slack": [_fake_openai_tool("mcp__slack__send")],
        }
        mgr._rebuild_tools()
        assert len(mgr._tools) == 2
        names = {t["function"]["name"] for t in mgr._tools}
        assert names == {"mcp__github__search", "mcp__slack__send"}
        assert mgr._tool_map["mcp__github__search"] == ("github", "search")
        assert mgr._tool_map["mcp__slack__send"] == ("slack", "send")

    def test_rebuild_copy_on_write(self):
        mgr = MCPClientManager({})
        mgr._per_server_tools = {"a": [_fake_openai_tool("mcp__a__x")]}
        mgr._rebuild_tools()
        old_tools = mgr._tools
        old_map = mgr._tool_map
        mgr._per_server_tools["b"] = [_fake_openai_tool("mcp__b__y")]
        mgr._rebuild_tools()
        assert mgr._tools is not old_tools
        assert mgr._tool_map is not old_map

    def test_rebuild_empty(self):
        mgr = MCPClientManager({})
        mgr._per_server_tools = {}
        mgr._rebuild_tools()
        assert mgr._tools == []
        assert mgr._tool_map == {}


class TestRefreshServer:
    def test_refresh_detects_added_tools(self):
        async def _run() -> None:
            mgr = MCPClientManager({})
            mock_session = MagicMock()
            mock_result = MagicMock()
            mock_result.tools = [
                _fake_mcp_tool("search"),
                _fake_mcp_tool("create"),  # new tool
            ]
            mock_session.list_tools = AsyncMock(return_value=mock_result)
            mgr._sessions["github"] = mock_session
            mgr._per_server_tools["github"] = [_fake_openai_tool("mcp__github__search")]
            mgr._rebuild_tools()

            added, removed = await mgr._refresh_server("github")
            assert "mcp__github__create" in added
            assert removed == []
            assert len(mgr._tools) == 2

        asyncio.run(_run())

    def test_refresh_detects_removed_tools(self):
        async def _run() -> None:
            mgr = MCPClientManager({})
            mock_session = MagicMock()
            mock_result = MagicMock()
            mock_result.tools = []  # all tools removed
            mock_session.list_tools = AsyncMock(return_value=mock_result)
            mgr._sessions["github"] = mock_session
            mgr._per_server_tools["github"] = [_fake_openai_tool("mcp__github__search")]
            mgr._rebuild_tools()

            added, removed = await mgr._refresh_server("github")
            assert added == []
            assert "mcp__github__search" in removed
            assert mgr._tools == []

        asyncio.run(_run())

    def test_refresh_no_changes(self):
        async def _run() -> None:
            mgr = MCPClientManager({})
            mock_session = MagicMock()
            mock_result = MagicMock()
            mock_result.tools = [_fake_mcp_tool("search")]
            mock_session.list_tools = AsyncMock(return_value=mock_result)
            mgr._sessions["github"] = mock_session
            mgr._per_server_tools["github"] = [_fake_openai_tool("mcp__github__search")]
            mgr._rebuild_tools()

            added, removed = await mgr._refresh_server("github")
            assert added == []
            assert removed == []

        asyncio.run(_run())

    def test_refresh_disconnected_raises(self):
        async def _run() -> None:
            mgr = MCPClientManager({})
            with pytest.raises(RuntimeError, match="not connected"):
                await mgr._refresh_server("ghost")

        asyncio.run(_run())


class TestListeners:
    def test_add_and_notify(self):
        mgr = MCPClientManager({})
        calls: list[int] = []
        mgr.add_listener(lambda: calls.append(1))
        mgr._per_server_tools = {"a": [_fake_openai_tool("mcp__a__x")]}
        mgr._rebuild_tools()
        assert len(calls) == 1

    def test_remove_listener(self):
        mgr = MCPClientManager({})
        calls: list[int] = []
        cb = lambda: calls.append(1)  # noqa: E731
        mgr.add_listener(cb)
        mgr.remove_listener(cb)
        mgr._rebuild_tools()
        assert calls == []

    def test_remove_nonexistent_listener(self):
        mgr = MCPClientManager({})
        mgr.remove_listener(lambda: None)  # should not raise

    def test_listener_error_does_not_propagate(self):
        mgr = MCPClientManager({})
        mgr.add_listener(lambda: 1 / 0)  # will raise ZeroDivisionError
        mgr._rebuild_tools()  # should not raise


class TestServerNames:
    def test_server_names_property(self):
        mgr = MCPClientManager({"github": {}, "slack": {}})
        assert sorted(mgr.server_names) == ["github", "slack"]

    def test_server_names_empty(self):
        mgr = MCPClientManager({})
        assert mgr.server_names == []


# ---------------------------------------------------------------------------
# Session integration — tool refresh propagation
# ---------------------------------------------------------------------------


class TestSessionRefresh:
    @pytest.fixture()
    def tmp_db(self, tmp_path):
        from turnstone.core.storage import init_storage, reset_storage

        reset_storage()
        init_storage("sqlite", path=str(tmp_path / "test.db"), run_migrations=False)
        yield
        reset_storage()

    def _make_session(self, mcp_client=None, **kwargs):
        from turnstone.core.session import ChatSession

        defaults: dict[str, Any] = dict(
            client=MagicMock(),
            model="test-model",
            ui=MagicMock(),
            instructions=None,
            temperature=0.5,
            max_tokens=4096,
            tool_timeout=30,
            mcp_client=mcp_client,
        )
        defaults.update(kwargs)
        return ChatSession(**defaults)

    def test_listener_registered_on_init(self, tmp_db):
        mock_mcp = MagicMock()
        mock_mcp.get_tools.return_value = []
        session = self._make_session(mcp_client=mock_mcp)
        mock_mcp.add_listener.assert_called_once()
        assert session._mcp_refresh_cb is not None

    def test_no_listener_without_mcp(self, tmp_db):
        session = self._make_session(mcp_client=None)
        assert session._mcp_refresh_cb is None

    def test_close_removes_listener(self, tmp_db):
        mock_mcp = MagicMock()
        mock_mcp.get_tools.return_value = []
        session = self._make_session(mcp_client=mock_mcp)
        session.close()
        mock_mcp.remove_listener.assert_called_once()
        assert session._mcp_refresh_cb is None

    def test_close_idempotent(self, tmp_db):
        mock_mcp = MagicMock()
        mock_mcp.get_tools.return_value = []
        session = self._make_session(mcp_client=mock_mcp)
        session.close()
        session.close()  # should not raise
        assert mock_mcp.remove_listener.call_count == 1

    def test_on_mcp_tools_changed_rebuilds_tools(self, tmp_db):
        mock_mcp = MagicMock()
        mock_mcp.get_tools.return_value = [_fake_openai_tool("mcp__test__a")]
        session = self._make_session(mcp_client=mock_mcp)
        initial_count = len(session._tools)

        # Simulate a tool refresh — MCP now has 2 tools
        mock_mcp.get_tools.return_value = [
            _fake_openai_tool("mcp__test__a"),
            _fake_openai_tool("mcp__test__b"),
        ]
        session._on_mcp_tools_changed()
        assert len(session._tools) == initial_count + 1

    def test_tool_search_preserved_across_refresh(self, tmp_db):
        # Create enough MCP tools to trigger tool search
        mcp_tools = [_fake_openai_tool(f"mcp__srv__tool{i}") for i in range(25)]
        mock_mcp = MagicMock()
        mock_mcp.get_tools.return_value = mcp_tools
        session = self._make_session(
            mcp_client=mock_mcp,
            tool_search="auto",
            tool_search_threshold=20,
        )
        assert session._tool_search is not None

        # Expand a tool
        session._tool_search.expand_visible(["mcp__srv__tool0"])
        assert "mcp__srv__tool0" in session._tool_search.get_expanded_names()

        # Refresh with same tools
        session._on_mcp_tools_changed()
        assert session._tool_search is not None
        assert "mcp__srv__tool0" in session._tool_search.get_expanded_names()

    def test_tool_search_prunes_removed_from_expanded(self, tmp_db):
        mcp_tools = [_fake_openai_tool(f"mcp__srv__tool{i}") for i in range(25)]
        mock_mcp = MagicMock()
        mock_mcp.get_tools.return_value = mcp_tools
        session = self._make_session(
            mcp_client=mock_mcp,
            tool_search="auto",
            tool_search_threshold=20,
        )
        session._tool_search.expand_visible(["mcp__srv__tool0"])

        # Refresh with tool0 removed
        new_tools = [_fake_openai_tool(f"mcp__srv__tool{i}") for i in range(1, 25)]
        mock_mcp.get_tools.return_value = new_tools
        session._on_mcp_tools_changed()
        # tool0 was removed, so it should no longer be in expanded
        expanded = session._tool_search.get_expanded_names()
        assert "mcp__srv__tool0" not in expanded

    def test_mcp_refresh_command(self, tmp_db):
        mock_mcp = MagicMock()
        mock_mcp.get_tools.return_value = [_fake_openai_tool()]
        mock_mcp.server_names = ["test"]
        mock_mcp.refresh_sync.return_value = {"test": (["mcp__test__new"], [])}
        session = self._make_session(mcp_client=mock_mcp)

        session.handle_command("/mcp refresh")
        mock_mcp.refresh_sync.assert_called_once_with(None)
        session.ui.on_info.assert_called()

    def test_mcp_refresh_specific_server(self, tmp_db):
        mock_mcp = MagicMock()
        mock_mcp.get_tools.return_value = [_fake_openai_tool()]
        mock_mcp.server_names = ["github", "slack"]
        mock_mcp.refresh_sync.return_value = {"github": ([], [])}
        session = self._make_session(mcp_client=mock_mcp)

        session.handle_command("/mcp refresh github")
        mock_mcp.refresh_sync.assert_called_once_with("github")

    def test_mcp_refresh_unknown_server(self, tmp_db):
        mock_mcp = MagicMock()
        mock_mcp.get_tools.return_value = [_fake_openai_tool()]
        mock_mcp.server_names = ["github"]
        session = self._make_session(mcp_client=mock_mcp)

        session.handle_command("/mcp refresh nonexistent")
        session.ui.on_error.assert_called_once()
        assert "Unknown MCP server" in session.ui.on_error.call_args[0][0]

    def test_mcp_refresh_error_handling(self, tmp_db):
        mock_mcp = MagicMock()
        mock_mcp.get_tools.return_value = [_fake_openai_tool()]
        mock_mcp.server_names = ["test"]
        mock_mcp.refresh_sync.side_effect = TimeoutError("timed out")
        session = self._make_session(mcp_client=mock_mcp)

        session.handle_command("/mcp refresh")
        session.ui.on_error.assert_called_once()
        assert "MCP refresh failed" in session.ui.on_error.call_args[0][0]
