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


def _fake_mcp_resource(
    uri: str = "file:///README.md",
    name: str = "readme",
    description: str = "Project readme",
    mime_type: str = "text/plain",
) -> MagicMock:
    """Create a mock MCP Resource object matching the SDK's Resource type."""
    res = MagicMock()
    res.uri = uri
    res.name = name
    res.description = description
    res.mimeType = mime_type
    return res


def _fake_resource_dict(
    uri: str = "file:///README.md",
    name: str = "readme",
    description: str = "Project readme",
    mime_type: str = "text/plain",
    server: str = "test",
) -> dict[str, Any]:
    """Create a fake resource dict as stored in per-server state."""
    return {
        "uri": uri,
        "name": name,
        "description": description,
        "mimeType": mime_type,
        "server": server,
    }


def _fake_mcp_prompt(
    name: str = "code_review",
    description: str = "Generate a code review",
    arguments: list[dict[str, Any]] | None = None,
) -> MagicMock:
    """Create a mock MCP Prompt object matching the SDK's Prompt type."""
    prompt = MagicMock()
    prompt.name = name
    prompt.description = description
    if arguments is None:
        arg = MagicMock()
        arg.name = "language"
        arg.description = "Programming language"
        arg.required = True
        prompt.arguments = [arg]
    else:
        mock_args = []
        for a in arguments:
            arg = MagicMock()
            arg.name = a["name"]
            arg.description = a.get("description", "")
            arg.required = a.get("required", False)
            mock_args.append(arg)
        prompt.arguments = mock_args
    return prompt


def _fake_prompt_dict(
    name: str = "mcp__test__code_review",
    original_name: str = "code_review",
    server: str = "test",
    description: str = "Generate a code review",
) -> dict[str, Any]:
    """Create a fake prompt dict as stored in per-server state."""
    return {
        "name": name,
        "original_name": original_name,
        "server": server,
        "description": description,
        "arguments": [
            {"name": "language", "description": "Programming language", "required": True}
        ],
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
    @staticmethod
    def _add_empty_resource_prompt_mocks(
        mgr: MCPClientManager, server_name: str, mock_session: MagicMock
    ) -> None:
        """Add empty list_resources/list_prompts mocks so _refresh_server works."""
        mgr._supports_resources[server_name] = True
        mgr._supports_prompts[server_name] = True
        empty_res = MagicMock()
        empty_res.resources = []
        mock_session.list_resources = AsyncMock(return_value=empty_res)
        empty_tmpl = MagicMock()
        empty_tmpl.resourceTemplates = []
        mock_session.list_resource_templates = AsyncMock(return_value=empty_tmpl)
        empty_prompts = MagicMock()
        empty_prompts.prompts = []
        mock_session.list_prompts = AsyncMock(return_value=empty_prompts)

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
            self._add_empty_resource_prompt_mocks(mgr, "github", mock_session)
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
            self._add_empty_resource_prompt_mocks(mgr, "github", mock_session)
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
            self._add_empty_resource_prompt_mocks(mgr, "github", mock_session)
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
                await mgr._refresh_server_tools("ghost")

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


# ---------------------------------------------------------------------------
# MCP Resources
# ---------------------------------------------------------------------------


class TestMCPResources:
    def test_resource_discovery(self):
        """Mock list_resources() returning 2 resources, verify get_resources()."""
        mgr = MCPClientManager({})
        mgr._per_server_resources = {
            "fs": [
                _fake_resource_dict("file:///a.txt", "a", "File A", "text/plain", "fs"),
                _fake_resource_dict("file:///b.txt", "b", "File B", "text/plain", "fs"),
            ],
        }
        mgr._rebuild_resources()
        resources = mgr.get_resources()
        assert len(resources) == 2
        uris = {r["uri"] for r in resources}
        assert uris == {"file:///a.txt", "file:///b.txt"}
        assert all(r["server"] == "fs" for r in resources)

    def test_rebuild_resources_copy_on_write(self):
        """Verify mutation safety — get_resources() returns independent copy."""
        mgr = MCPClientManager({})
        mgr._per_server_resources = {
            "a": [_fake_resource_dict("file:///x", "x", "", "", "a")],
        }
        mgr._rebuild_resources()
        old_resources = mgr._resources
        old_map = mgr._resource_map
        mgr._per_server_resources["b"] = [_fake_resource_dict("file:///y", "y", "", "", "b")]
        mgr._rebuild_resources()
        assert mgr._resources is not old_resources
        assert mgr._resource_map is not old_map

    def test_get_resources_returns_copy(self):
        mgr = MCPClientManager({})
        mgr._per_server_resources = {
            "a": [_fake_resource_dict("file:///x", "x", "", "", "a")],
        }
        mgr._rebuild_resources()
        resources = mgr.get_resources()
        assert len(resources) == 1
        resources.clear()
        assert len(mgr.get_resources()) == 1

    def test_read_resource_sync(self):
        """Mock session.read_resource(), verify text extraction."""
        mgr = MCPClientManager({})
        mgr._resource_map = {"file:///readme": ("fs", "file:///readme")}
        mock_session = MagicMock()
        mgr._sessions["fs"] = mock_session
        mgr._loop = asyncio.new_event_loop()

        # Mock the read_resource result
        text_content = MagicMock(spec=["text"])
        text_content.text = "Hello, world!"
        mock_result = MagicMock()
        mock_result.contents = [text_content]
        mock_session.read_resource = AsyncMock(return_value=mock_result)

        thread = None
        try:
            thread = __import__("threading").Thread(target=mgr._loop.run_forever, daemon=True)
            thread.start()
            output = mgr.read_resource_sync("file:///readme", timeout=5)
            assert output == "Hello, world!"
            mock_session.read_resource.assert_awaited_once_with("file:///readme")
        finally:
            mgr._loop.call_soon_threadsafe(mgr._loop.stop)
            if thread:
                thread.join(timeout=5)
            mgr._loop.close()

    def test_read_resource_sync_blob(self):
        """Verify base64 blob extraction."""
        mgr = MCPClientManager({})
        mgr._resource_map = {"file:///img.png": ("fs", "file:///img.png")}
        mock_session = MagicMock()
        mgr._sessions["fs"] = mock_session
        mgr._loop = asyncio.new_event_loop()

        blob_content = MagicMock(spec=["blob"])
        blob_content.blob = "aGVsbG8="
        mock_result = MagicMock()
        mock_result.contents = [blob_content]
        mock_session.read_resource = AsyncMock(return_value=mock_result)

        thread = None
        try:
            thread = __import__("threading").Thread(target=mgr._loop.run_forever, daemon=True)
            thread.start()
            output = mgr.read_resource_sync("file:///img.png", timeout=5)
            assert output == "aGVsbG8="
        finally:
            mgr._loop.call_soon_threadsafe(mgr._loop.stop)
            if thread:
                thread.join(timeout=5)
            mgr._loop.close()

    def test_read_resource_sync_unknown_uri(self):
        mgr = MCPClientManager({})
        with pytest.raises(ValueError, match="Unknown MCP resource"):
            mgr.read_resource_sync("file:///nonexistent")

    def test_read_resource_sync_disconnected(self):
        mgr = MCPClientManager({})
        mgr._resource_map = {"file:///x": ("dead", "file:///x")}
        with pytest.raises(RuntimeError, match="not connected"):
            mgr.read_resource_sync("file:///x")

    def test_read_resource_sync_timeout(self):
        """Verify timeout handling."""
        mgr = MCPClientManager({})
        mgr._resource_map = {"file:///x": ("fs", "file:///x")}
        mock_session = MagicMock()
        mgr._sessions["fs"] = mock_session
        mgr._loop = asyncio.new_event_loop()

        async def _slow_read(_uri: str) -> None:
            await asyncio.sleep(10)

        mock_session.read_resource = _slow_read

        thread = None
        try:
            thread = __import__("threading").Thread(target=mgr._loop.run_forever, daemon=True)
            thread.start()
            with pytest.raises(TimeoutError):
                mgr.read_resource_sync("file:///x", timeout=1)
        finally:
            mgr._loop.call_soon_threadsafe(mgr._loop.stop)
            if thread:
                thread.join(timeout=5)
            mgr._loop.close()

    def test_resource_listener_notification(self):
        """Verify callback fires on rebuild."""
        mgr = MCPClientManager({})
        calls: list[int] = []
        mgr.add_resource_listener(lambda: calls.append(1))
        mgr._per_server_resources = {"a": [_fake_resource_dict()]}
        mgr._rebuild_resources()
        assert len(calls) == 1

    def test_resource_listener_remove(self):
        mgr = MCPClientManager({})
        calls: list[int] = []
        cb = lambda: calls.append(1)  # noqa: E731
        mgr.add_resource_listener(cb)
        mgr.remove_resource_listener(cb)
        mgr._rebuild_resources()
        assert calls == []

    def test_resource_listener_error_does_not_propagate(self):
        mgr = MCPClientManager({})
        mgr.add_resource_listener(lambda: 1 / 0)
        mgr._rebuild_resources()  # should not raise

    def test_resource_refresh_on_notification(self):
        """Mock notification, verify re-fetch of resources."""

        async def _run() -> None:
            mgr = MCPClientManager({})
            mock_session = MagicMock()
            mgr._sessions["fs"] = mock_session
            mgr._supports_resources["fs"] = True

            # Initial state
            mgr._per_server_resources["fs"] = [
                _fake_resource_dict("file:///old", server="fs"),
            ]
            mgr._rebuild_resources()
            assert len(mgr.get_resources()) == 1

            # Mock the re-fetch returning a new resource
            new_res = _fake_mcp_resource("file:///new", "new")
            mock_res_result = MagicMock()
            mock_res_result.resources = [new_res]
            mock_session.list_resources = AsyncMock(return_value=mock_res_result)
            mock_tmpl_result = MagicMock()
            mock_tmpl_result.resourceTemplates = []
            mock_session.list_resource_templates = AsyncMock(return_value=mock_tmpl_result)

            await mgr._refresh_server_resources("fs")
            resources = mgr.get_resources()
            assert len(resources) == 1
            assert resources[0]["uri"] == "file:///new"

        asyncio.run(_run())

    def test_rebuild_resources_empty(self):
        mgr = MCPClientManager({})
        mgr._per_server_resources = {}
        mgr._rebuild_resources()
        assert mgr._resources == []
        assert mgr._resource_map == {}

    def test_rebuild_resources_multi_server(self):
        mgr = MCPClientManager({})
        mgr._per_server_resources = {
            "fs": [_fake_resource_dict("file:///a", server="fs")],
            "db": [_fake_resource_dict("db://table", name="table", server="db")],
        }
        mgr._rebuild_resources()
        assert len(mgr._resources) == 2
        assert mgr._resource_map["file:///a"] == ("fs", "file:///a")
        assert mgr._resource_map["db://table"] == ("db", "db://table")

    def test_template_prefix_matching(self):
        """Expanded URI matches template by prefix."""
        mgr = MCPClientManager({})
        mgr._per_server_resources = {
            "db": [
                {
                    "uri": "db://tables/{table}/rows/{id}",
                    "name": "row",
                    "description": "A row",
                    "mimeType": "application/json",
                    "server": "db",
                    "template": True,
                },
            ],
        }
        mgr._rebuild_resources()
        # Template should not be in resource_map
        assert "db://tables/{table}/rows/{id}" not in mgr._resource_map
        # But prefix matching should find it
        result = mgr._match_template("db://tables/users/rows/1")
        assert result is not None
        server, template_uri = result
        assert server == "db"
        assert template_uri == "db://tables/{table}/rows/{id}"

    def test_template_longest_prefix_wins(self):
        """When two templates have overlapping prefixes, the longer one wins."""
        mgr = MCPClientManager({})
        # Use templates with genuinely different prefix lengths:
        # "db://data/" (6 chars after scheme) vs "db://data/tables/" (13 chars after scheme)
        mgr._per_server_resources = {
            "short": [
                {
                    "uri": "db://data/{collection}",
                    "name": "collection",
                    "description": "",
                    "mimeType": "",
                    "server": "short",
                    "template": True,
                },
            ],
            "long": [
                {
                    "uri": "db://data/tables/{table}",
                    "name": "table",
                    "description": "",
                    "mimeType": "",
                    "server": "long",
                    "template": True,
                },
            ],
        }
        mgr._rebuild_resources()
        # "db://data/tables/users" matches both prefixes ("db://data/" and
        # "db://data/tables/") — the longer one should win
        result = mgr._match_template("db://data/tables/users")
        assert result is not None
        server, template_uri = result
        assert server == "long"
        assert template_uri == "db://data/tables/{table}"
        # URI that only matches the short prefix
        result2 = mgr._match_template("db://data/views/active")
        assert result2 is not None
        assert result2[0] == "short"

    def test_template_no_match_raises(self):
        """Completely unrelated URI still raises ValueError."""
        mgr = MCPClientManager({})
        mgr._per_server_resources = {
            "db": [
                {
                    "uri": "db://tables/{table}",
                    "name": "table",
                    "description": "",
                    "mimeType": "",
                    "server": "db",
                    "template": True,
                },
            ],
        }
        mgr._rebuild_resources()
        assert mgr._match_template("file:///something") is None
        with pytest.raises(ValueError, match="Unknown MCP resource"):
            mgr.read_resource_sync("file:///something")

    def test_read_resource_sync_with_template_uri(self):
        """End-to-end: template discovered, expanded URI dispatched to correct server."""
        mgr = MCPClientManager({})
        mgr._per_server_resources = {
            "db": [
                {
                    "uri": "db://tables/{table}/rows/{id}",
                    "name": "row",
                    "description": "A row",
                    "mimeType": "application/json",
                    "server": "db",
                    "template": True,
                },
            ],
        }
        mgr._rebuild_resources()

        mock_session = MagicMock()
        mgr._sessions["db"] = mock_session
        mgr._loop = asyncio.new_event_loop()

        text_content = MagicMock(spec=["text"])
        text_content.text = '{"name": "Alice"}'
        mock_result = MagicMock()
        mock_result.contents = [text_content]
        mock_session.read_resource = AsyncMock(return_value=mock_result)

        thread = None
        try:
            thread = __import__("threading").Thread(target=mgr._loop.run_forever, daemon=True)
            thread.start()
            output = mgr.read_resource_sync("db://tables/users/rows/1", timeout=5)
            assert output == '{"name": "Alice"}'
            mock_session.read_resource.assert_awaited_once_with("db://tables/users/rows/1")
        finally:
            mgr._loop.call_soon_threadsafe(mgr._loop.stop)
            if thread:
                thread.join(timeout=5)
            mgr._loop.close()


# ---------------------------------------------------------------------------
# MCP Prompts
# ---------------------------------------------------------------------------


class TestMCPPrompts:
    def test_prompt_discovery(self):
        """Mock list_prompts(), verify get_prompts() with correct prefixed names."""
        mgr = MCPClientManager({})
        mgr._per_server_prompts = {
            "tmpl": [
                _fake_prompt_dict("mcp__tmpl__code_review", "code_review", "tmpl"),
                _fake_prompt_dict("mcp__tmpl__summarize", "summarize", "tmpl"),
            ],
        }
        mgr._rebuild_prompts()
        prompts = mgr.get_prompts()
        assert len(prompts) == 2
        names = {p["name"] for p in prompts}
        assert names == {"mcp__tmpl__code_review", "mcp__tmpl__summarize"}
        # Verify map entries
        assert mgr._prompt_map["mcp__tmpl__code_review"] == ("tmpl", "code_review")
        assert mgr._prompt_map["mcp__tmpl__summarize"] == ("tmpl", "summarize")

    def test_rebuild_prompts_copy_on_write(self):
        """Verify mutation safety."""
        mgr = MCPClientManager({})
        mgr._per_server_prompts = {
            "a": [_fake_prompt_dict("mcp__a__p1", "p1", "a")],
        }
        mgr._rebuild_prompts()
        old_prompts = mgr._prompts
        old_map = mgr._prompt_map
        mgr._per_server_prompts["b"] = [_fake_prompt_dict("mcp__b__p2", "p2", "b")]
        mgr._rebuild_prompts()
        assert mgr._prompts is not old_prompts
        assert mgr._prompt_map is not old_map

    def test_get_prompts_returns_copy(self):
        mgr = MCPClientManager({})
        mgr._per_server_prompts = {
            "a": [_fake_prompt_dict("mcp__a__p1", "p1", "a")],
        }
        mgr._rebuild_prompts()
        prompts = mgr.get_prompts()
        assert len(prompts) == 1
        prompts.clear()
        assert len(mgr.get_prompts()) == 1

    def test_get_prompt_sync(self):
        """Mock session.get_prompt(), verify message conversion."""
        mgr = MCPClientManager({})
        mgr._prompt_map = {"mcp__tmpl__review": ("tmpl", "review")}
        mock_session = MagicMock()
        mgr._sessions["tmpl"] = mock_session
        mgr._loop = asyncio.new_event_loop()

        # Build mock PromptMessage
        msg1 = MagicMock()
        msg1.role = "user"
        msg1.content = MagicMock()
        msg1.content.text = "Review this code"
        msg2 = MagicMock()
        msg2.role = "assistant"
        msg2.content = MagicMock()
        msg2.content.text = "Looks good!"
        mock_result = MagicMock()
        mock_result.messages = [msg1, msg2]
        mock_session.get_prompt = AsyncMock(return_value=mock_result)

        thread = None
        try:
            thread = __import__("threading").Thread(target=mgr._loop.run_forever, daemon=True)
            thread.start()
            messages = mgr.get_prompt_sync(
                "mcp__tmpl__review", arguments={"language": "python"}, timeout=5
            )
            assert len(messages) == 2
            assert messages[0] == {"role": "user", "content": "Review this code"}
            assert messages[1] == {"role": "assistant", "content": "Looks good!"}
            mock_session.get_prompt.assert_awaited_once_with(
                "review", arguments={"language": "python"}
            )
        finally:
            mgr._loop.call_soon_threadsafe(mgr._loop.stop)
            if thread:
                thread.join(timeout=5)
            mgr._loop.close()

    def test_get_prompt_sync_unknown(self):
        mgr = MCPClientManager({})
        with pytest.raises(ValueError, match="Unknown MCP prompt"):
            mgr.get_prompt_sync("mcp__no__such")

    def test_get_prompt_sync_disconnected(self):
        mgr = MCPClientManager({})
        mgr._prompt_map = {"mcp__dead__p": ("dead", "p")}
        with pytest.raises(RuntimeError, match="not connected"):
            mgr.get_prompt_sync("mcp__dead__p")

    def test_get_prompt_sync_timeout(self):
        """Verify timeout handling."""
        mgr = MCPClientManager({})
        mgr._prompt_map = {"mcp__tmpl__slow": ("tmpl", "slow")}
        mock_session = MagicMock()
        mgr._sessions["tmpl"] = mock_session
        mgr._loop = asyncio.new_event_loop()

        async def _slow_prompt(_name: str, *, arguments: dict[str, str] | None = None) -> None:
            await asyncio.sleep(10)

        mock_session.get_prompt = _slow_prompt

        thread = None
        try:
            thread = __import__("threading").Thread(target=mgr._loop.run_forever, daemon=True)
            thread.start()
            with pytest.raises(TimeoutError):
                mgr.get_prompt_sync("mcp__tmpl__slow", timeout=1)
        finally:
            mgr._loop.call_soon_threadsafe(mgr._loop.stop)
            if thread:
                thread.join(timeout=5)
            mgr._loop.close()

    def test_prompt_listener_notification(self):
        """Verify callback fires on rebuild."""
        mgr = MCPClientManager({})
        calls: list[int] = []
        mgr.add_prompt_listener(lambda: calls.append(1))
        mgr._per_server_prompts = {"a": [_fake_prompt_dict()]}
        mgr._rebuild_prompts()
        assert len(calls) == 1

    def test_prompt_listener_remove(self):
        mgr = MCPClientManager({})
        calls: list[int] = []
        cb = lambda: calls.append(1)  # noqa: E731
        mgr.add_prompt_listener(cb)
        mgr.remove_prompt_listener(cb)
        mgr._rebuild_prompts()
        assert calls == []

    def test_prompt_listener_error_does_not_propagate(self):
        mgr = MCPClientManager({})
        mgr.add_prompt_listener(lambda: 1 / 0)
        mgr._rebuild_prompts()  # should not raise

    def test_is_mcp_prompt(self):
        """Verify name lookup."""
        mgr = MCPClientManager({})
        mgr._prompt_map["mcp__tmpl__review"] = ("tmpl", "review")
        assert mgr.is_mcp_prompt("mcp__tmpl__review") is True
        assert mgr.is_mcp_prompt("nonexistent") is False

    def test_prompt_refresh_on_notification(self):
        """Mock notification, verify re-fetch of prompts."""

        async def _run() -> None:
            mgr = MCPClientManager({})
            mock_session = MagicMock()
            mgr._sessions["tmpl"] = mock_session
            mgr._supports_prompts["tmpl"] = True

            # Initial state
            mgr._per_server_prompts["tmpl"] = [
                _fake_prompt_dict("mcp__tmpl__old", "old", "tmpl"),
            ]
            mgr._rebuild_prompts()
            assert len(mgr.get_prompts()) == 1

            # Mock re-fetch returning a new prompt
            new_prompt = _fake_mcp_prompt("new_prompt", "A new prompt")
            mock_prompt_result = MagicMock()
            mock_prompt_result.prompts = [new_prompt]
            mock_session.list_prompts = AsyncMock(return_value=mock_prompt_result)

            await mgr._refresh_server_prompts("tmpl")
            prompts = mgr.get_prompts()
            assert len(prompts) == 1
            assert prompts[0]["name"] == "mcp__tmpl__new_prompt"
            assert prompts[0]["original_name"] == "new_prompt"

        asyncio.run(_run())

    def test_rebuild_prompts_empty(self):
        mgr = MCPClientManager({})
        mgr._per_server_prompts = {}
        mgr._rebuild_prompts()
        assert mgr._prompts == []
        assert mgr._prompt_map == {}

    def test_rebuild_prompts_multi_server(self):
        mgr = MCPClientManager({})
        mgr._per_server_prompts = {
            "a": [_fake_prompt_dict("mcp__a__p1", "p1", "a")],
            "b": [_fake_prompt_dict("mcp__b__p2", "p2", "b")],
        }
        mgr._rebuild_prompts()
        assert len(mgr._prompts) == 2
        assert mgr._prompt_map["mcp__a__p1"] == ("a", "p1")
        assert mgr._prompt_map["mcp__b__p2"] == ("b", "p2")


# ---------------------------------------------------------------------------
# Shutdown cleans up new state
# ---------------------------------------------------------------------------


class TestShutdownCleanup:
    def test_shutdown_clears_resources_and_prompts(self):
        mgr = MCPClientManager({})
        mgr._per_server_resources = {"a": [_fake_resource_dict()]}
        mgr._rebuild_resources()
        mgr._per_server_prompts = {"a": [_fake_prompt_dict()]}
        mgr._rebuild_prompts()
        assert mgr.get_resources() != []
        assert mgr.get_prompts() != []

        mgr.shutdown()
        assert mgr.get_resources() == []
        assert mgr.get_prompts() == []
        assert mgr._resource_map == {}
        assert mgr._prompt_map == {}


# ---------------------------------------------------------------------------
# TCP probe and unreachable server handling
# ---------------------------------------------------------------------------


class TestTCPProbe:
    """MCPClientManager._tcp_probe should fail fast on unreachable servers."""

    def test_tcp_probe_unreachable_raises_connection_error(self):
        """Unreachable host raises ConnectionError, not TimeoutError."""
        mgr = MCPClientManager({})

        async def _run():
            with pytest.raises(ConnectionError, match="unreachable"):
                await mgr._tcp_probe("test-server", "http://127.0.0.1:1")

        asyncio.run(_run())

    def test_tcp_probe_parses_url_correctly(self):
        """Port and host are extracted from the URL."""
        mgr = MCPClientManager({})

        async def _run():
            # Non-routable port — should fail with ConnectionError
            with pytest.raises(ConnectionError):
                await mgr._tcp_probe("srv", "https://127.0.0.1:1/mcp")

        asyncio.run(_run())

    def test_tcp_probe_default_port_http(self):
        """Default port 80 used for http:// URLs without explicit port."""
        mgr = MCPClientManager({})

        async def _run():
            # Will fail (nothing on port 80), but should not crash on parsing
            with pytest.raises(ConnectionError):
                await mgr._tcp_probe("srv", "http://127.0.0.1")

        asyncio.run(_run())

    def test_tcp_probe_dns_failure(self):
        """Unresolvable hostname raises ConnectionError."""
        mgr = MCPClientManager({})

        async def _run():
            with pytest.raises(ConnectionError):
                await mgr._tcp_probe("srv", "http://this.host.does.not.exist.invalid:8080/mcp")

        asyncio.run(_run())


class TestConnectOneUnreachable:
    """_connect_one should handle unreachable HTTP servers gracefully."""

    def test_unreachable_http_server_raises_connection_error(self):
        """Unreachable HTTP MCP server raises ConnectionError without spinning."""
        mgr = MCPClientManager({})
        mgr._loop = asyncio.new_event_loop()

        async def _run():
            with pytest.raises(ConnectionError, match="unreachable"):
                await mgr._connect_one(
                    "bad-server",
                    {
                        "type": "http",
                        "url": "http://127.0.0.1:1/mcp",
                    },
                )

        mgr._loop.run_until_complete(_run())
        mgr._loop.close()

        # Server should NOT be in sessions (connection failed)
        assert "bad-server" not in mgr._sessions

    def test_connect_all_continues_after_unreachable_server(self):
        """_connect_all logs error and continues to next server."""
        mgr = MCPClientManager(
            {
                "bad": {"type": "http", "url": "http://127.0.0.1:1/mcp"},
            }
        )

        loop = asyncio.new_event_loop()
        loop.run_until_complete(mgr._connect_all())
        loop.close()

        assert "bad" not in mgr._sessions
        assert "bad" in mgr._last_error


class TestSafeCloseStack:
    """_safe_close_stack should suppress errors from broken anyio scopes."""

    def test_suppresses_runtime_error(self):
        """RuntimeError from broken cancel scope is suppressed."""

        async def _run():
            stack = AsyncExitStack()
            await stack.__aenter__()

            # Simulate a broken close that raises RuntimeError
            async def _broken_close():
                raise RuntimeError("Attempted to exit cancel scope in a different task")

            stack.aclose = _broken_close
            # Should not raise
            await MCPClientManager._safe_close_stack(stack)

        asyncio.run(_run())

    def test_suppresses_cancelled_error(self):
        """CancelledError during close is suppressed."""

        async def _run():
            stack = AsyncExitStack()
            await stack.__aenter__()

            async def _cancel_close():
                raise asyncio.CancelledError()

            stack.aclose = _cancel_close
            await MCPClientManager._safe_close_stack(stack)

        asyncio.run(_run())
