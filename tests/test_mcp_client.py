"""Tests for turnstone.core.mcp_client — MCP client manager and config loading."""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import inspect
import json
import threading
import time
from contextlib import AsyncExitStack, suppress
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import anyio
import mcp.types as mcp_types
import pytest

from tests.conftest import _drain_background, _run_on_loop, _seed_static_state
from turnstone.core.mcp_client import (
    _MAX_RESOURCES_PER_SERVER,
    MCPClientManager,
    _db_servers_to_config,
    _is_dead_transport,
    _mcp_to_openai,
    load_mcp_config,
)
from turnstone.core.tools import INTERACTIVE_TOOLS, TOOLS, merge_mcp_tools

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _dispatch_stub(mock_future: MagicMock) -> Any:
    """Stand-in for ``asyncio.run_coroutine_threadsafe`` in sync-bridge tests.

    Closes the never-scheduled coroutine before handing back the canned
    future — a mocked dispatch never awaits it, and an unawaited coroutine
    GC-fires "coroutine ... was never awaited" inside whatever unrelated
    test happens to be running when collection finally occurs (cross-test
    bleed that per-test filterwarnings markers cannot catch).
    """

    def _rct(coro: Any, _loop: Any) -> MagicMock:
        # Only real coroutines need (or survive) closing — several tests
        # dispatch a plain MagicMock return value through this seam.
        if inspect.iscoroutine(coro):
            coro.close()
        return mock_future

    return _rct


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


@pytest.fixture
def running_loop_mgr():
    """Yield a (mgr, loop, thread) triple with a background loop already running.

    Spawns an MCPClientManager with a default config of {"srv": stdio echo}
    and a fresh asyncio loop driven by a daemon thread.  Tests that need a
    different config can mutate ``mgr._server_configs`` directly.  The
    fixture stops the loop and joins the thread on teardown so each test
    leaves a clean slate.
    """
    import threading as _threading

    cfg = {"srv": {"type": "stdio", "command": "echo"}}
    mgr = MCPClientManager(cfg)
    loop = asyncio.new_event_loop()
    thread = _threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    mgr._loop = loop
    try:
        yield mgr, loop, thread
    finally:
        # Drain BEFORE stopping: a task left pending (or finished-but-
        # unretrieved) on a stopped loop becomes cross-test global state —
        # asyncio reports it at GC time, mid-suite, onto whatever stream
        # pytest has attached THEN (the "I/O operation on closed file"
        # spew), and a silently-abandoned loop thread keeps running
        # manager code against torn-down mocks.
        async def _cancel_pending() -> None:
            tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        with suppress(Exception):
            asyncio.run_coroutine_threadsafe(_cancel_pending(), loop).result(timeout=5)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        assert not thread.is_alive(), "mcp test loop thread failed to stop within 5s"
        loop.close()


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
        assert func["description"] == "Search GitHub repos"
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
        assert result["function"]["description"] == ""


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


class TestDBServersToConfig:
    """``_db_servers_to_config`` shapes DB rows for the MCP client."""

    def test_static_streamable_http_row_passes_through(self) -> None:
        rows = [
            {
                "name": "static-srv",
                "transport": "streamable-http",
                "url": "https://mcp.example.com",
                "headers": '{"Authorization": "Bearer token"}',
                "auth_type": "static",
            }
        ]
        result = _db_servers_to_config(rows)
        assert "static-srv" in result
        assert result["static-srv"]["url"] == "https://mcp.example.com"
        assert result["static-srv"]["headers"] == {"Authorization": "Bearer token"}

    def test_db_servers_to_config_skips_oauth_user_rows(self) -> None:
        """Rows with auth_type=oauth_user must be invisible to the static
        auto-connect path.

        Auto-connecting these with empty headers fails the AS check and
        trips the circuit breaker on startup. Per-user OAuth servers
        come online lazily once the user has consented.
        """
        rows = [
            {
                "name": "static-srv",
                "transport": "streamable-http",
                "url": "https://static.example.com",
                "headers": "{}",
                "auth_type": "static",
            },
            {
                "name": "oauth-srv",
                "transport": "streamable-http",
                "url": "https://oauth.example.com",
                "headers": "{}",
                "auth_type": "oauth_user",
            },
            {
                "name": "stdio-srv",
                "transport": "stdio",
                "command": "echo",
                "args": "[]",
                "env": "{}",
                "auth_type": "none",
            },
        ]
        result = _db_servers_to_config(rows)
        assert set(result) == {"static-srv", "stdio-srv"}
        assert "oauth-srv" not in result


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
        _seed_static_state(mgr, "a", session=MagicMock())
        _seed_static_state(mgr, "b", session=MagicMock())
        assert mgr.server_count == 2

    def test_call_tool_sync_unknown_tool(self):
        mgr = MCPClientManager({})
        with pytest.raises(ValueError, match="Unknown MCP tool"):
            mgr.call_tool_sync("mcp__no__such", {})

    def test_call_tool_sync_disconnected_server(self):
        mgr = MCPClientManager({})
        mgr._tool_map["mcp__dead__ping"] = ("dead", "ping")
        # No session registered for "dead", no config/loop → reconnect fails
        with pytest.raises(RuntimeError, match="not connected"):
            mgr.call_tool_sync("mcp__dead__ping", {})

    def test_shutdown_on_unstarted_manager(self):
        """shutdown() should not raise when called on a manager that was never started."""
        mgr = MCPClientManager({})
        mgr.shutdown()  # should be a no-op

    # -- Phase 7: per-user catalog scoping ---------------------------------

    def test_is_mcp_tool_user_id_default_none_unchanged(self):
        """Sanity: default ``user_id=None`` answers static-only.

        The legacy single-arg call still works, and unknown names still
        return False — Phase 7 adds an optional keyword without
        rewriting the static-path semantics.
        """
        mgr = MCPClientManager({})
        mgr._tool_map["mcp__static__list"] = ("static", "list")
        # Legacy single-arg call still works.
        assert mgr.is_mcp_tool("mcp__static__list") is True
        assert mgr.is_mcp_tool("mcp__static__list", user_id=None) is True
        assert mgr.is_mcp_tool("nonexistent") is False
        assert mgr.is_mcp_tool("nonexistent", user_id=None) is False

    def test_is_mcp_tool_user_keyed_pool_tool(self):
        """A name visible only via ``_user_tool_map`` resolves only for
        the matching ``user_id``.

        Verifies the new branch: ``_tool_map`` miss + ``user_id`` hit.
        """
        mgr = MCPClientManager({})
        mgr._user_tool_map["user-1"] = {
            "mcp__pool-srv__do": ("pool-srv", "do"),
        }
        # Visible to user-1.
        assert mgr.is_mcp_tool("mcp__pool-srv__do", user_id="user-1") is True
        # Invisible to None caller (admin / web-search backend resolution).
        assert mgr.is_mcp_tool("mcp__pool-srv__do", user_id=None) is False
        # Invisible to a different user.
        assert mgr.is_mcp_tool("mcp__pool-srv__do", user_id="user-2") is False

    def test_is_mcp_tool_static_wins_for_any_user(self):
        """Static-path tools are visible regardless of ``user_id`` —
        the merged view is ``static ∪ user-pool``."""
        mgr = MCPClientManager({})
        mgr._tool_map["mcp__static__list"] = ("static", "list")
        # Even for an unknown user, a static tool is still reachable —
        # static-path is process-global.
        assert mgr.is_mcp_tool("mcp__static__list", user_id="user-1") is True
        assert mgr.is_mcp_tool("mcp__static__list", user_id="anybody") is True

    def test_get_tools_user_id_none_returns_static_only(self):
        """``get_tools(user_id=None)`` returns the global static catalog.

        Pool tools are NEVER included in the default-arg view — that's
        the legacy contract every pre-Phase-7 caller relies on.
        """
        mgr = MCPClientManager({})
        mgr._tools = [_fake_openai_tool("mcp__static__list")]
        # Seed a pool entry that should NOT appear in the default view.
        from turnstone.core.mcp_client import PoolEntryState

        entry = PoolEntryState(key=("user-1", "pool-srv"), open_lock=MagicMock())
        entry.tools = [_fake_openai_tool("mcp__pool-srv__do")]
        mgr._user_pool_entries[("user-1", "pool-srv")] = entry

        tools = mgr.get_tools()
        names = [t["function"]["name"] for t in tools]
        assert names == ["mcp__static__list"]

    def test_get_tools_user_id_merges_pool(self):
        """``get_tools(user_id='user-1')`` merges static + that user's pool tools.

        Other users' pool entries MUST NOT leak into the result —
        privacy / RBAC invariant.
        """
        from turnstone.core.mcp_client import PoolEntryState

        mgr = MCPClientManager({})
        mgr._tools = [_fake_openai_tool("mcp__static__list")]
        e1 = PoolEntryState(key=("user-1", "pool-srv"), open_lock=MagicMock())
        e1.tools = [_fake_openai_tool("mcp__pool-srv__do")]
        e2 = PoolEntryState(key=("user-2", "pool-srv"), open_lock=MagicMock())
        e2.tools = [_fake_openai_tool("mcp__pool-srv__other")]
        mgr._user_pool_entries[("user-1", "pool-srv")] = e1
        mgr._user_pool_entries[("user-2", "pool-srv")] = e2
        # Production invariant: ``_connect_one_pool`` /
        # ``_refresh_pool_server_tools`` / ``_evict_session`` /
        # ``_close_pool_entry_if_idle`` all call ``_rebuild_user_tool_map``
        # immediately after mutating ``_user_pool_entries``. Tests that
        # seed pool entries directly must mirror that invariant —
        # ``get_tools(user_id=...)`` reads from the ``_user_tools``
        # snapshot (built by ``_rebuild_user_tool_map``), never iterating
        # ``_user_pool_entries`` directly.
        mgr._rebuild_user_tool_map("user-1")
        mgr._rebuild_user_tool_map("user-2")

        u1_names = [t["function"]["name"] for t in mgr.get_tools(user_id="user-1")]
        assert sorted(u1_names) == ["mcp__pool-srv__do", "mcp__static__list"]

        u2_names = [t["function"]["name"] for t in mgr.get_tools(user_id="user-2")]
        assert sorted(u2_names) == ["mcp__pool-srv__other", "mcp__static__list"]

        # Default still global-only — unaffected by either user's entries.
        default_names = [t["function"]["name"] for t in mgr.get_tools()]
        assert default_names == ["mcp__static__list"]

    def test_get_tools_user_id_returns_copies(self):
        """Mirror existing ``test_get_tools_returns_copy``: caller mutation
        of the returned list MUST NOT affect the manager's catalog."""
        from turnstone.core.mcp_client import PoolEntryState

        mgr = MCPClientManager({})
        mgr._tools = [_fake_openai_tool("mcp__static__a")]
        entry = PoolEntryState(key=("user-1", "pool-srv"), open_lock=MagicMock())
        entry.tools = [_fake_openai_tool("mcp__pool-srv__b")]
        mgr._user_pool_entries[("user-1", "pool-srv")] = entry
        # See note in ``test_get_tools_user_id_merges_pool``.
        mgr._rebuild_user_tool_map("user-1")

        tools = mgr.get_tools(user_id="user-1")
        assert len(tools) == 2
        tools.clear()
        # Re-fetch — original catalog unchanged.
        assert len(mgr.get_tools(user_id="user-1")) == 2

    def test_get_tools_user_with_none_tools_skipped(self):
        """A pool entry that hasn't completed discovery (``entry.tools is None``)
        contributes no tools — the merged view skips it cleanly."""
        from turnstone.core.mcp_client import PoolEntryState

        mgr = MCPClientManager({})
        mgr._tools = [_fake_openai_tool("mcp__static__a")]
        # Brand-new pool entry, discovery not yet run.
        entry = PoolEntryState(key=("user-1", "pool-srv"), open_lock=MagicMock())
        assert entry.tools is None
        mgr._user_pool_entries[("user-1", "pool-srv")] = entry
        # Rebuild observes ``entry.tools is None`` and skips this entry.
        mgr._rebuild_user_tool_map("user-1")

        names = [t["function"]["name"] for t in mgr.get_tools(user_id="user-1")]
        assert names == ["mcp__static__a"]

    def test_rebuild_user_tool_map_populates(self):
        """``_rebuild_user_tool_map`` materializes the per-user index from
        pool entries owned by that user."""
        from turnstone.core.mcp_client import PoolEntryState

        mgr = MCPClientManager({})
        entry = PoolEntryState(key=("user-1", "pool-srv"), open_lock=MagicMock())
        entry.tools = [_fake_openai_tool("mcp__pool-srv__do")]
        mgr._user_pool_entries[("user-1", "pool-srv")] = entry

        mgr._rebuild_user_tool_map("user-1")
        assert mgr._user_tool_map["user-1"] == {"mcp__pool-srv__do": ("pool-srv", "do")}
        # Sibling _user_tools cache (bug-1 fix) MUST be populated alongside
        # the map — otherwise get_tools(user_id="user-1") would silently
        # return the static-only view despite is_mcp_tool returning True.
        assert mgr._user_tools["user-1"] == [_fake_openai_tool("mcp__pool-srv__do")]

    def test_rebuild_user_tool_map_drops_empty_user(self):
        """Rebuilding for a user with no pool entries removes the key
        rather than retaining an empty-dict sentinel."""
        from turnstone.core.mcp_client import PoolEntryState

        mgr = MCPClientManager({})
        entry = PoolEntryState(key=("user-1", "pool-srv"), open_lock=MagicMock())
        entry.tools = [_fake_openai_tool("mcp__pool-srv__do")]
        mgr._user_pool_entries[("user-1", "pool-srv")] = entry
        mgr._rebuild_user_tool_map("user-1")
        assert "user-1" in mgr._user_tool_map
        assert "user-1" in mgr._user_tools

        # Drop the entry, rebuild — user_id key should be removed from BOTH
        # the map and the sibling tool list (bug-1 fix). A drop in only one
        # would leave get_tools and is_mcp_tool out of sync.
        mgr._user_pool_entries.clear()
        mgr._rebuild_user_tool_map("user-1")
        assert "user-1" not in mgr._user_tool_map
        assert "user-1" not in mgr._user_tools

    def test_rebuild_user_tool_map_isolates_users(self):
        """Rebuilding for ``user-1`` MUST NOT touch ``user-2``'s entry."""
        from turnstone.core.mcp_client import PoolEntryState

        mgr = MCPClientManager({})
        e1 = PoolEntryState(key=("user-1", "pool-srv"), open_lock=MagicMock())
        e1.tools = [_fake_openai_tool("mcp__pool-srv__one")]
        e2 = PoolEntryState(key=("user-2", "pool-srv"), open_lock=MagicMock())
        e2.tools = [_fake_openai_tool("mcp__pool-srv__two")]
        mgr._user_pool_entries[("user-1", "pool-srv")] = e1
        mgr._user_pool_entries[("user-2", "pool-srv")] = e2

        mgr._rebuild_user_tool_map("user-1")
        mgr._rebuild_user_tool_map("user-2")
        assert mgr._user_tool_map["user-1"] == {"mcp__pool-srv__one": ("pool-srv", "one")}
        assert mgr._user_tool_map["user-2"] == {"mcp__pool-srv__two": ("pool-srv", "two")}

        # Clear user-1's entry only; rebuild user-1; user-2 must remain.
        mgr._user_pool_entries.pop(("user-1", "pool-srv"))
        mgr._rebuild_user_tool_map("user-1")
        assert "user-1" not in mgr._user_tool_map
        assert mgr._user_tool_map["user-2"] == {"mcp__pool-srv__two": ("pool-srv", "two")}

    def test_rebuild_user_tool_map_does_not_touch_static(self):
        """Invariant 1: per-user rebuild must NOT mutate ``_tool_map``."""
        from turnstone.core.mcp_client import PoolEntryState

        mgr = MCPClientManager({})
        mgr._tool_map["mcp__static__list"] = ("static", "list")
        entry = PoolEntryState(key=("user-1", "pool-srv"), open_lock=MagicMock())
        entry.tools = [_fake_openai_tool("mcp__pool-srv__do")]
        mgr._user_pool_entries[("user-1", "pool-srv")] = entry

        before = dict(mgr._tool_map)
        mgr._rebuild_user_tool_map("user-1")
        assert mgr._tool_map == before


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
        # Interactive session surface — coordinator tools excluded.
        assert session._tools is INTERACTIVE_TOOLS
        assert session._mcp_client is None

    def test_session_with_mcp(self, tmp_db):
        mock_mcp = MagicMock()
        mock_mcp.get_tools.return_value = [_fake_openai_tool()]
        session = self._make_session(mcp_client=mock_mcp)
        assert len(session._tools) == len(INTERACTIVE_TOOLS) + 1
        assert session._tools[-1]["function"]["name"] == "mcp__test__search"

    def test_task_tools_include_mcp(self, tmp_db):
        from turnstone.core.tools import TASK_AGENT_TOOLS

        mock_mcp = MagicMock()
        mock_mcp.get_tools.return_value = [_fake_openai_tool()]
        session = self._make_session(mcp_client=mock_mcp)
        assert len(session._task_tools) == len(TASK_AGENT_TOOLS) + 1

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
        # Error lists available tools so the model can self-correct
        assert "bash" in prepared["error"]
        # Surfaces warning to user
        session.ui.on_error.assert_called_once()
        assert "nonexistent" in session.ui.on_error.call_args[0][0]

    def test_prepare_tool_strips_whitespace_from_name(self, tmp_db):
        """Local models may produce tool names with leading/trailing whitespace."""
        session = self._make_session(mcp_client=None)
        tc = {
            "id": "call_strip",
            "function": {"name": "  bash\n", "arguments": '{"command": "echo hi"}'},
        }
        prepared = session._prepare_tool(tc)
        assert prepared["func_name"] == "bash"
        assert "error" not in prepared

    def test_prepare_tool_malformed_json_surfaces_error(self, tmp_db):
        """Malformed JSON args should surface a warning to the user and
        give the model a hint about expected format."""
        session = self._make_session(mcp_client=None)
        tc = {
            "id": "call_bad",
            "function": {"name": "bash", "arguments": "{command: echo hi}"},
        }
        prepared = session._prepare_tool(tc)
        assert "error" in prepared
        assert "JSON parse error" in prepared["error"]
        assert "command" in prepared["error"]  # hint about expected key
        assert "Please retry" in prepared["error"]
        # User-facing warning
        session.ui.on_error.assert_called_once()
        assert "Malformed tool call" in session.ui.on_error.call_args[0][0]

    def test_ensure_tool_call_ids_dict(self, tmp_db):
        """_ensure_tool_call_ids fills empty IDs on streaming-style dict."""
        from turnstone.core.session import ChatSession

        tool_calls_acc = {
            0: {"id": "", "function": {"name": "bash", "arguments": "{}"}},
            1: {"id": "", "function": {"name": "read_file", "arguments": "{}"}},
        }
        ChatSession._ensure_tool_call_ids(tool_calls_acc)
        ids = [tc["id"] for tc in tool_calls_acc.values()]
        assert all(id_.startswith("call_") for id_ in ids)
        assert len(set(ids)) == 2  # unique

    def test_ensure_tool_call_ids_list(self, tmp_db):
        """_ensure_tool_call_ids fills empty IDs on list (agent path)."""
        from turnstone.core.session import ChatSession

        tool_calls = [
            {"id": None, "function": {"name": "bash", "arguments": "{}"}},
            {"id": "call_existing", "function": {"name": "bash", "arguments": "{}"}},
        ]
        ChatSession._ensure_tool_call_ids(tool_calls)
        assert tool_calls[0]["id"].startswith("call_")
        assert tool_calls[1]["id"] == "call_existing"  # preserved

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
            "mcp__test__search",
            {"query": "hello"},
            user_id=None,
            timeout=30,
            is_interactive_for_consent=True,
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

    # -- Phase 7: per-user catalog scoping ---------------------------------

    def test_session_passes_user_id_to_get_tools(self, tmp_db):
        """ChatSession threads its ``user_id`` into ``get_tools`` so the
        merged static + pool view is scoped to the session's user."""
        mock_mcp = MagicMock()
        mock_mcp.get_tools.return_value = [_fake_openai_tool()]
        self._make_session(mcp_client=mock_mcp, user_id="user-7")
        mock_mcp.get_tools.assert_called_with(user_id="user-7")

    def test_session_get_tools_empty_user_id_passes_none(self, tmp_db):
        """Sentinel ``user_id=""`` (CLI / service / unknown) collapses to
        ``user_id=None`` so the static-only view is returned."""
        mock_mcp = MagicMock()
        mock_mcp.get_tools.return_value = [_fake_openai_tool()]
        self._make_session(mcp_client=mock_mcp, user_id="")
        mock_mcp.get_tools.assert_called_with(user_id=None)

    def test_session_passes_user_id_to_add_listener(self, tmp_db):
        """ChatSession registers its tool-change listener under its own
        ``user_id`` so pool-only changes for OTHER users do not fire it."""
        mock_mcp = MagicMock()
        mock_mcp.get_tools.return_value = [_fake_openai_tool()]
        self._make_session(mcp_client=mock_mcp, user_id="user-7")
        # ``add_listener`` was called with ``user_id="user-7"``.
        listener_calls = mock_mcp.add_listener.call_args_list
        assert listener_calls, "ChatSession did not register a tool listener"
        first_call = listener_calls[0]
        assert first_call.kwargs.get("user_id") == "user-7"

    def test_session_close_removes_listener_with_same_user_id(self, tmp_db):
        """R4 critical: register and remove MUST agree on ``user_id`` —
        the listener identity is ``(user_id, callback)``."""
        mock_mcp = MagicMock()
        mock_mcp.get_tools.return_value = [_fake_openai_tool()]
        session = self._make_session(mcp_client=mock_mcp, user_id="user-7")

        session.close()
        # ``remove_listener`` must be called with the same ``user_id``.
        remove_calls = mock_mcp.remove_listener.call_args_list
        assert remove_calls, "ChatSession.close did not unregister a tool listener"
        first_remove = remove_calls[0]
        assert first_remove.kwargs.get("user_id") == "user-7"
        # And the callback identity must match what was registered.
        registered_cb = mock_mcp.add_listener.call_args_list[0].args[0]
        removed_cb = first_remove.args[0]
        assert registered_cb is removed_cb

    def test_session_unknown_tool_lists_user_scoped_catalog(self, tmp_db):
        """The "Unknown tool" error message lists tools the session can
        actually invoke — drawn from the merged user-scoped catalog,
        not the manager's private static-only ``_tool_map``."""
        mock_mcp = MagicMock()
        # Pretend the user's merged view contains a static + pool entry.
        mock_mcp.get_tools.return_value = [
            _fake_openai_tool("mcp__static__list"),
            _fake_openai_tool("mcp__pool-srv__do"),
        ]
        mock_mcp.is_mcp_tool.return_value = False
        session = self._make_session(mcp_client=mock_mcp, user_id="user-7")
        # Reset the call counter so we observe only the _prepare_tool call.
        mock_mcp.get_tools.reset_mock()

        tc = {
            "id": "call_unknown",
            "function": {"name": "no_such_tool", "arguments": "{}"},
        }
        prepared = session._prepare_tool(tc)
        assert "error" in prepared
        # The error mentions both static and pool tools — proves we're
        # consulting the merged catalog rather than ``_tool_map``.
        assert "mcp__static__list" in prepared["error"]
        assert "mcp__pool-srv__do" in prepared["error"]
        # And the catalog request was scoped to this session's user.
        assert any(
            call.kwargs.get("user_id") == "user-7" for call in mock_mcp.get_tools.call_args_list
        )


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
            assert "my__bad" not in mgr._static_servers
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
        _seed_static_state(mgr, "github", tools=[_fake_openai_tool("mcp__github__search")])
        _seed_static_state(mgr, "slack", tools=[_fake_openai_tool("mcp__slack__send")])
        mgr._rebuild_tools()
        assert len(mgr._tools) == 2
        names = {t["function"]["name"] for t in mgr._tools}
        assert names == {"mcp__github__search", "mcp__slack__send"}
        assert mgr._tool_map["mcp__github__search"] == ("github", "search")
        assert mgr._tool_map["mcp__slack__send"] == ("slack", "send")

    def test_rebuild_copy_on_write(self):
        mgr = MCPClientManager({})
        _seed_static_state(mgr, "a", tools=[_fake_openai_tool("mcp__a__x")])
        mgr._rebuild_tools()
        old_tools = mgr._tools
        old_map = mgr._tool_map
        _seed_static_state(mgr, "b", tools=[_fake_openai_tool("mcp__b__y")])
        mgr._rebuild_tools()
        assert mgr._tools is not old_tools
        assert mgr._tool_map is not old_map

    def test_rebuild_empty(self):
        mgr = MCPClientManager({})
        mgr._static_servers = {}
        mgr._rebuild_tools()
        assert mgr._tools == []
        assert mgr._tool_map == {}


class TestRefreshServer:
    @staticmethod
    def _add_empty_resource_prompt_mocks(
        mgr: MCPClientManager, server_name: str, mock_session: MagicMock
    ) -> None:
        """Add empty list_resources/list_prompts mocks so _refresh_server works."""
        _seed_static_state(mgr, server_name, supports_resources=True, supports_prompts=True)
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
            _seed_static_state(
                mgr,
                "github",
                session=mock_session,
                tools=[_fake_openai_tool("mcp__github__search")],
            )
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
            _seed_static_state(
                mgr,
                "github",
                session=mock_session,
                tools=[_fake_openai_tool("mcp__github__search")],
            )
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
            _seed_static_state(
                mgr,
                "github",
                session=mock_session,
                tools=[_fake_openai_tool("mcp__github__search")],
            )
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


class TestLastRefreshTracking:
    """Phase 9 admin status pill — ``_last_refresh`` is written on every
    refresh path so the admin UI reflects manual-refresh AND auto-
    reconnect outcomes uniformly.  This test class pins the contract.
    """

    @staticmethod
    def _seed_minimal(mgr: MCPClientManager, name: str = "srv") -> MagicMock:
        mock_session = MagicMock()
        mock_session.list_tools = AsyncMock(return_value=MagicMock(tools=[]))
        mock_session.list_resources = AsyncMock(return_value=MagicMock(resources=[]))
        mock_session.list_resource_templates = AsyncMock(
            return_value=MagicMock(resourceTemplates=[])
        )
        mock_session.list_prompts = AsyncMock(return_value=MagicMock(prompts=[]))
        # Config present: outcome writes are config-gated (a removed server
        # must leave no stale row), so the tracked server must be configured.
        mgr._server_configs[name] = {"type": "stdio", "command": "x"}
        _seed_static_state(
            mgr,
            name,
            session=mock_session,
            tools=[],
            supports_resources=True,
            supports_prompts=True,
        )
        return mock_session

    def test_last_refresh_written_on_success(self) -> None:
        async def _run() -> None:
            mgr = MCPClientManager({})
            self._seed_minimal(mgr)
            assert "srv" not in mgr._last_refresh

            await mgr._refresh_server("srv")

            entry = mgr._last_refresh.get("srv")
            assert entry is not None
            ts, outcome = entry
            assert outcome == "ok"
            assert isinstance(ts, float) and ts > 0

        asyncio.run(_run())

    def test_last_refresh_written_on_tool_refresh_failure(self) -> None:
        """When ``_refresh_server_tools`` raises, ``_refresh_server``
        propagates the exception; the OUTCOME row is written by the
        caller's ``_record_refresh_failure`` (one config-gated place),
        which the spawned ``_refresh_server_logged`` wrapper routes
        through."""

        async def _run() -> None:
            mgr = MCPClientManager({})
            mock_session = self._seed_minimal(mgr)
            mock_session.list_tools = AsyncMock(side_effect=RuntimeError("upstream down"))

            # _refresh_server itself propagates (the caller records).
            with pytest.raises(RuntimeError, match="upstream down"):
                await mgr._refresh_server("srv")
            # The caller (here the logged wrapper) records the outcome.
            await mgr._refresh_server_logged("srv")

            entry = mgr._last_refresh.get("srv")
            assert entry is not None
            _, outcome = entry
            assert outcome == "error:RuntimeError"

        asyncio.run(_run())

    def test_last_refresh_records_first_exception_when_multiple_fail(
        self,
    ) -> None:
        """``return_exceptions=True`` lets sibling tasks complete; the
        outcome reflects the FIRST exception encountered."""

        async def _run() -> None:
            mgr = MCPClientManager({})
            mock_session = self._seed_minimal(mgr)
            # Tools succeeds; resources raises first (gather preserves
            # argument order in its results list, so resources is the
            # first failure regardless of which awaitable finished first
            # in wall-clock terms).
            mock_session.list_resources = AsyncMock(side_effect=ValueError("res boom"))
            mock_session.list_prompts = AsyncMock(side_effect=KeyError("prompts boom"))

            # _refresh_server raises the first exception (positional gather
            # order: resources arg #2 before prompts arg #3); the caller
            # records that class as the outcome.
            with pytest.raises((ValueError, KeyError)):
                await mgr._refresh_server("srv")
            await mgr._refresh_server_logged("srv")

            entry = mgr._last_refresh.get("srv")
            assert entry is not None
            _, outcome = entry
            assert outcome == "error:ValueError"

        asyncio.run(_run())

    def test_refresh_all_overwrites_stale_ok_on_reconnect_failure(
        self,
    ) -> None:
        """The chokepoint bug-1 fix: a prior successful refresh's ``'ok'``
        entry MUST be overwritten when a subsequent reconnect fails —
        otherwise the admin pill shows misleading "ok" while the server
        is in fact broken."""

        async def _run() -> None:
            mgr = MCPClientManager({})
            # Server is configured but has no live session — _refresh_all
            # routes to the reconnect branch.
            mgr._server_configs["srv"] = {"type": "stdio", "command": "x"}
            # Pre-seed a stale "ok" from an earlier successful refresh.
            mgr._last_refresh["srv"] = (1000.0, "ok")

            async def _raise(*_a: object, **_kw: object) -> None:
                raise ConnectionError("reconnect failed")

            # The reconnect branch routes through _ensure_static_connected,
            # which calls the LOCKED connect body.
            mgr._connect_one_locked = _raise  # type: ignore[assignment]

            await mgr._refresh_all("srv")

            entry = mgr._last_refresh.get("srv")
            assert entry is not None
            ts, outcome = entry
            # Outcome reflects the new failure, not the stale ok.
            assert outcome == "error:ConnectionError"
            assert ts > 1000.0

        asyncio.run(_run())

    def test_get_server_status_surfaces_last_refresh_fields(self) -> None:
        """``get_server_status`` surfaces ``last_refresh_at`` and
        ``last_refresh_outcome`` for the admin pill — null when no
        refresh has occurred yet, populated after one."""
        mgr = MCPClientManager({})
        mgr._server_configs["srv"] = {"type": "stdio", "command": "x"}

        # No refresh yet — fields must be present and null so the JS
        # renderer can branch on absence cleanly.
        status = mgr.get_server_status("srv")
        assert status["last_refresh_at"] is None
        assert status["last_refresh_outcome"] is None

        # Populate the tuple directly and re-read.
        mgr._last_refresh["srv"] = (12345.5, "ok")
        status = mgr.get_server_status("srv")
        assert status["last_refresh_at"] == 12345.5
        assert status["last_refresh_outcome"] == "ok"


class TestListeners:
    def test_add_and_notify(self):
        mgr = MCPClientManager({})
        calls: list[int] = []
        mgr.add_listener(lambda: calls.append(1))
        _seed_static_state(mgr, "a", tools=[_fake_openai_tool("mcp__a__x")])
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

    # -- Phase 7: user-keyed listener fan-out ------------------------------

    def test_add_listener_records_user_id(self):
        """``add_listener`` stores ``(user_id, callback)`` tuples — the
        listener identity carries the user_id."""
        mgr = MCPClientManager({})
        cb_admin = lambda: None  # noqa: E731
        cb_user = lambda: None  # noqa: E731
        mgr.add_listener(cb_admin)  # default: user_id=None (admin)
        mgr.add_listener(cb_user, user_id="user-1")
        assert (None, cb_admin) in mgr._listeners
        assert ("user-1", cb_user) in mgr._listeners

    def test_remove_listener_requires_matching_user_id(self):
        """Removing with a different ``user_id`` must NOT remove the
        original registration — listener identity is the pair."""
        mgr = MCPClientManager({})
        calls: list[int] = []
        cb = lambda: calls.append(1)  # noqa: E731
        mgr.add_listener(cb, user_id="user-1")

        # Try to remove with the wrong user_id — should be a no-op.
        mgr.remove_listener(cb, user_id="user-2")
        # The user-1 listener should still be live.
        mgr._notify_user_tool_listeners("user-1")
        assert calls == [1]

        # Now remove with the right user_id.
        mgr.remove_listener(cb, user_id="user-1")
        mgr._notify_user_tool_listeners("user-1")
        assert calls == [1]  # not invoked again

    def test_static_change_fires_all_listeners(self):
        """``_rebuild_tools`` (static-path change) fires ALL registered
        listeners — admin + every user. RFC §3.3."""
        mgr = MCPClientManager({})
        admin_calls: list[int] = []
        u1_calls: list[int] = []
        u2_calls: list[int] = []
        mgr.add_listener(lambda: admin_calls.append(1))
        mgr.add_listener(lambda: u1_calls.append(1), user_id="user-1")
        mgr.add_listener(lambda: u2_calls.append(1), user_id="user-2")
        _seed_static_state(mgr, "a", tools=[_fake_openai_tool("mcp__a__x")])
        mgr._rebuild_tools()
        assert admin_calls == [1]
        assert u1_calls == [1]
        assert u2_calls == [1]

    def test_user_tool_listeners_only_fire_for_matching_user(self):
        """``_notify_user_tool_listeners('user-1')`` fires admin (None)
        and user-1 listeners; user-2's listener is silent."""
        mgr = MCPClientManager({})
        admin_calls: list[int] = []
        u1_calls: list[int] = []
        u2_calls: list[int] = []
        mgr.add_listener(lambda: admin_calls.append(1))
        mgr.add_listener(lambda: u1_calls.append(1), user_id="user-1")
        mgr.add_listener(lambda: u2_calls.append(1), user_id="user-2")

        mgr._notify_user_tool_listeners("user-1")
        assert admin_calls == [1]
        assert u1_calls == [1]
        assert u2_calls == []

        mgr._notify_user_tool_listeners("user-2")
        assert admin_calls == [1, 1]
        assert u1_calls == [1]
        assert u2_calls == [1]


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

    def test_mcp_refresh_renders_skip_distinctly(self, tmp_db):
        # A None result (busy-skip) with a "skipped" outcome must NOT render
        # as "no changes" — the operator would think the server is current.
        mock_mcp = MagicMock()
        mock_mcp.get_tools.return_value = [_fake_openai_tool()]
        mock_mcp.server_names = ["srv"]
        mock_mcp.refresh_sync.return_value = {"srv": None}
        mock_mcp.last_refresh_outcome.return_value = "skipped"
        session = self._make_session(mcp_client=mock_mcp)

        session.handle_command("/mcp refresh")
        rendered = session.ui.on_info.call_args[0][0]
        assert "skipped" in rendered
        assert "no changes" not in rendered

    def test_mcp_refresh_renders_failure_distinctly(self, tmp_db):
        # A None result whose outcome is an error must render as a failure,
        # NOT as "no changes" (the pre-#839 lie the None sentinel closes).
        mock_mcp = MagicMock()
        mock_mcp.get_tools.return_value = [_fake_openai_tool()]
        mock_mcp.server_names = ["srv"]
        mock_mcp.refresh_sync.return_value = {"srv": None}
        mock_mcp.last_refresh_outcome.return_value = "error:ConnectionError"
        session = self._make_session(mcp_client=mock_mcp)

        session.handle_command("/mcp refresh")
        rendered = session.ui.on_info.call_args[0][0]
        assert "failed" in rendered
        assert "no changes" not in rendered


# ---------------------------------------------------------------------------
# MCP Resources
# ---------------------------------------------------------------------------


class TestMCPResources:
    def test_resource_discovery(self):
        """Mock list_resources() returning 2 resources, verify get_resources()."""
        mgr = MCPClientManager({})
        _seed_static_state(
            mgr,
            "fs",
            resources=[
                _fake_resource_dict("file:///a.txt", "a", "File A", "text/plain", "fs"),
                _fake_resource_dict("file:///b.txt", "b", "File B", "text/plain", "fs"),
            ],
        )
        mgr._rebuild_resources()
        resources = mgr.get_resources()
        assert len(resources) == 2
        uris = {r["uri"] for r in resources}
        assert uris == {"file:///a.txt", "file:///b.txt"}
        assert all(r["server"] == "fs" for r in resources)

    def test_rebuild_resources_copy_on_write(self):
        """Verify mutation safety — get_resources() returns independent copy."""
        mgr = MCPClientManager({})
        _seed_static_state(mgr, "a", resources=[_fake_resource_dict("file:///x", "x", "", "", "a")])
        mgr._rebuild_resources()
        old_resources = mgr._resources
        old_map = mgr._resource_map
        _seed_static_state(mgr, "b", resources=[_fake_resource_dict("file:///y", "y", "", "", "b")])
        mgr._rebuild_resources()
        assert mgr._resources is not old_resources
        assert mgr._resource_map is not old_map

    def test_get_resources_returns_copy(self):
        mgr = MCPClientManager({})
        _seed_static_state(mgr, "a", resources=[_fake_resource_dict("file:///x", "x", "", "", "a")])
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
        _seed_static_state(mgr, "fs", session=mock_session)
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
        _seed_static_state(mgr, "fs", session=mock_session)
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
        _seed_static_state(mgr, "fs", session=mock_session)
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
        _seed_static_state(mgr, "a", resources=[_fake_resource_dict()])
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
            _seed_static_state(
                mgr,
                "fs",
                session=mock_session,
                supports_resources=True,
                resources=[_fake_resource_dict("file:///old", server="fs")],
            )
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
        mgr._static_servers = {}
        mgr._rebuild_resources()
        assert mgr._resources == []
        assert mgr._resource_map == {}

    def test_rebuild_resources_multi_server(self):
        mgr = MCPClientManager({})
        _seed_static_state(mgr, "fs", resources=[_fake_resource_dict("file:///a", server="fs")])
        _seed_static_state(
            mgr,
            "db",
            resources=[_fake_resource_dict("db://table", name="table", server="db")],
        )
        mgr._rebuild_resources()
        assert len(mgr._resources) == 2
        assert mgr._resource_map["file:///a"] == ("fs", "file:///a")
        assert mgr._resource_map["db://table"] == ("db", "db://table")

    def test_template_prefix_matching(self):
        """Expanded URI matches template by prefix."""
        mgr = MCPClientManager({})
        _seed_static_state(
            mgr,
            "db",
            resources=[
                {
                    "uri": "db://tables/{table}/rows/{id}",
                    "name": "row",
                    "description": "A row",
                    "mimeType": "application/json",
                    "server": "db",
                    "template": True,
                },
            ],
        )
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
        _seed_static_state(
            mgr,
            "short",
            resources=[
                {
                    "uri": "db://data/{collection}",
                    "name": "collection",
                    "description": "",
                    "mimeType": "",
                    "server": "short",
                    "template": True,
                },
            ],
        )
        _seed_static_state(
            mgr,
            "long",
            resources=[
                {
                    "uri": "db://data/tables/{table}",
                    "name": "table",
                    "description": "",
                    "mimeType": "",
                    "server": "long",
                    "template": True,
                },
            ],
        )
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
        _seed_static_state(
            mgr,
            "db",
            resources=[
                {
                    "uri": "db://tables/{table}",
                    "name": "table",
                    "description": "",
                    "mimeType": "",
                    "server": "db",
                    "template": True,
                },
            ],
        )
        mgr._rebuild_resources()
        assert mgr._match_template("file:///something") is None
        with pytest.raises(ValueError, match="Unknown MCP resource"):
            mgr.read_resource_sync("file:///something")

    def test_read_resource_sync_with_template_uri(self):
        """End-to-end: template discovered, expanded URI dispatched to correct server."""
        mgr = MCPClientManager({})
        _seed_static_state(
            mgr,
            "db",
            resources=[
                {
                    "uri": "db://tables/{table}/rows/{id}",
                    "name": "row",
                    "description": "A row",
                    "mimeType": "application/json",
                    "server": "db",
                    "template": True,
                },
            ],
        )
        mgr._rebuild_resources()

        mock_session = MagicMock()
        _seed_static_state(mgr, "db", session=mock_session)
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
        _seed_static_state(
            mgr,
            "tmpl",
            prompts=[
                _fake_prompt_dict("mcp__tmpl__code_review", "code_review", "tmpl"),
                _fake_prompt_dict("mcp__tmpl__summarize", "summarize", "tmpl"),
            ],
        )
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
        _seed_static_state(mgr, "a", prompts=[_fake_prompt_dict("mcp__a__p1", "p1", "a")])
        mgr._rebuild_prompts()
        old_prompts = mgr._prompts
        old_map = mgr._prompt_map
        _seed_static_state(mgr, "b", prompts=[_fake_prompt_dict("mcp__b__p2", "p2", "b")])
        mgr._rebuild_prompts()
        assert mgr._prompts is not old_prompts
        assert mgr._prompt_map is not old_map

    def test_get_prompts_returns_copy(self):
        mgr = MCPClientManager({})
        _seed_static_state(mgr, "a", prompts=[_fake_prompt_dict("mcp__a__p1", "p1", "a")])
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
        _seed_static_state(mgr, "tmpl", session=mock_session)
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
        _seed_static_state(mgr, "tmpl", session=mock_session)
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
        _seed_static_state(mgr, "a", prompts=[_fake_prompt_dict()])
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
            _seed_static_state(
                mgr,
                "tmpl",
                session=mock_session,
                supports_prompts=True,
                prompts=[_fake_prompt_dict("mcp__tmpl__old", "old", "tmpl")],
            )
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
        mgr._static_servers = {}
        mgr._rebuild_prompts()
        assert mgr._prompts == []
        assert mgr._prompt_map == {}

    def test_rebuild_prompts_multi_server(self):
        mgr = MCPClientManager({})
        _seed_static_state(mgr, "a", prompts=[_fake_prompt_dict("mcp__a__p1", "p1", "a")])
        _seed_static_state(mgr, "b", prompts=[_fake_prompt_dict("mcp__b__p2", "p2", "b")])
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
        _seed_static_state(mgr, "a", resources=[_fake_resource_dict()])
        mgr._rebuild_resources()
        _seed_static_state(mgr, "a", prompts=[_fake_prompt_dict()])
        mgr._rebuild_prompts()
        assert mgr.get_resources() != []
        assert mgr.get_prompts() != []

        mgr.shutdown()
        assert mgr.get_resources() == []
        assert mgr.get_prompts() == []
        assert mgr._resource_map == {}
        assert mgr._prompt_map == {}

    def test_shutdown_closes_owned_loop_and_clears_refs(self):
        """When the manager owns the loop thread, shutdown must close the loop
        (selector resources leak otherwise) and drop both refs; a second
        shutdown is then a clean no-op."""
        import threading as _threading

        mgr = MCPClientManager({})
        loop = asyncio.new_event_loop()
        thread = _threading.Thread(target=loop.run_forever, daemon=True)
        thread.start()
        mgr._loop = loop
        mgr._thread = thread

        mgr.shutdown()
        assert loop.is_closed()
        assert mgr._loop is None
        assert mgr._thread is None
        mgr.shutdown()  # idempotent

    def test_shutdown_leaves_unowned_loop_open(self):
        """Tests (and any embedder) that wire ``_loop`` directly without a
        thread own the loop's lifecycle — shutdown must not close it."""
        mgr = MCPClientManager({})
        loop = asyncio.new_event_loop()
        mgr._loop = loop
        try:
            mgr.shutdown()
            assert not loop.is_closed()
        finally:
            loop.close()


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

        # Server should NOT have a live session (connection failed)
        bad_state = mgr._static_servers.get("bad-server")
        assert bad_state is None or bad_state.session is None

    def test_connect_all_continues_after_unreachable_server(self):
        """_connect_all logs error and continues to next server."""
        mgr = MCPClientManager(
            {
                "bad": {"type": "http", "url": "http://127.0.0.1:1/mcp"},
            }
        )

        loop = asyncio.new_event_loop()
        loop.run_until_complete(mgr._connect_all())
        # _connect_all spawns the long-lived token-freshness sweep task; cancel
        # it before closing the loop so it isn't destroyed while pending.
        sweep = mgr._user_token_sweep_task
        if sweep is not None:
            sweep.cancel()
            with contextlib.suppress(BaseException):
                loop.run_until_complete(sweep)
        # _connect_all spawns the long-lived static health task; cancel it before
        # closing the loop so it isn't destroyed while pending.
        health = mgr._static_health_task
        if health is not None:
            health.cancel()
            with suppress(BaseException):
                loop.run_until_complete(health)
        loop.close()

        bad_state = mgr._static_servers.get("bad")
        assert bad_state is None or bad_state.session is None
        assert "bad" in mgr._last_error


# ---------------------------------------------------------------------------
# Fix 1: Cancel orphaned futures on timeout
# ---------------------------------------------------------------------------


class TestFutureCancellation:
    """Verify future.cancel() is called when sync bridge methods time out."""

    def _make_manager_with_session(self) -> MCPClientManager:
        mgr = MCPClientManager({"test": {"type": "stdio", "command": "echo"}})
        mock_session = MagicMock()
        # Prevent auto-spec from creating async coroutines that trigger warnings
        mock_session.call_tool = MagicMock(return_value="sentinel")
        mock_session.read_resource = MagicMock(return_value="sentinel")
        mock_session.get_prompt = MagicMock(return_value="sentinel")
        _seed_static_state(mgr, "test", session=mock_session)
        mgr._loop = MagicMock()
        mgr._tool_map["mcp__test__search"] = ("test", "search")
        mgr._resource_map["file:///a.txt"] = ("test", "file:///a.txt")
        mgr._prompt_map["mcp__test__review"] = ("test", "review")
        return mgr

    def test_call_tool_sync_cancels_future_on_timeout(self):
        mgr = self._make_manager_with_session()
        mock_future = MagicMock()
        mock_future.result.side_effect = concurrent.futures.TimeoutError()
        with (
            patch("asyncio.run_coroutine_threadsafe", new=_dispatch_stub(mock_future)),
            pytest.raises(TimeoutError, match="timed out"),
        ):
            mgr.call_tool_sync("mcp__test__search", {"query": "x"}, timeout=1)
        mock_future.cancel.assert_called_once()

    def test_read_resource_sync_cancels_future_on_timeout(self):
        mgr = self._make_manager_with_session()
        mock_future = MagicMock()
        mock_future.result.side_effect = concurrent.futures.TimeoutError()
        with (
            patch("asyncio.run_coroutine_threadsafe", new=_dispatch_stub(mock_future)),
            pytest.raises(TimeoutError, match="timed out"),
        ):
            mgr.read_resource_sync("file:///a.txt", timeout=1)
        mock_future.cancel.assert_called_once()

    def test_get_prompt_sync_cancels_future_on_timeout(self):
        mgr = self._make_manager_with_session()
        mock_future = MagicMock()
        mock_future.result.side_effect = concurrent.futures.TimeoutError()
        with (
            patch("asyncio.run_coroutine_threadsafe", new=_dispatch_stub(mock_future)),
            pytest.raises(TimeoutError, match="timed out"),
        ):
            mgr.get_prompt_sync("mcp__test__review", timeout=1)
        mock_future.cancel.assert_called_once()

    def test_refresh_sync_cancels_future_on_timeout(self):
        mgr = MCPClientManager({})
        mgr._loop = MagicMock()
        mock_future = MagicMock()
        mock_future.result.side_effect = concurrent.futures.TimeoutError()
        with (
            patch.object(mgr, "_refresh_all", return_value=MagicMock()),
            patch("asyncio.run_coroutine_threadsafe", new=_dispatch_stub(mock_future)),
            pytest.raises(TimeoutError, match="timed out"),
        ):
            mgr.refresh_sync(timeout=1)
        mock_future.cancel.assert_called_once()


# ---------------------------------------------------------------------------
# Fix 2: Per-server circuit breaker
# ---------------------------------------------------------------------------


class TestCircuitBreaker:
    """Verify per-server circuit breaker behavior."""

    def test_circuit_stays_closed_below_threshold(self):
        mgr = MCPClientManager({})
        mgr._cb_record_failure("srv")
        mgr._cb_record_failure("srv")
        is_open, _ = mgr._cb_check("srv")
        assert not is_open

    def test_circuit_opens_at_threshold(self):
        mgr = MCPClientManager({})
        for _ in range(3):
            mgr._cb_record_failure("srv")
        is_open, cooldown_expired = mgr._cb_check("srv")
        assert is_open
        assert not cooldown_expired  # just opened, cooldown not expired

    def test_circuit_half_open_after_cooldown(self):
        mgr = MCPClientManager({})
        for _ in range(3):
            mgr._cb_record_failure("srv")
        # Simulate cooldown expiry
        mgr._circuit_open_until["srv"] = time.monotonic() - 1
        is_open, cooldown_expired = mgr._cb_check("srv")
        assert is_open
        assert cooldown_expired

    def test_circuit_resets_on_success(self):
        mgr = MCPClientManager({})
        for _ in range(3):
            mgr._cb_record_failure("srv")
        assert "srv" in mgr._circuit_open_until
        mgr._cb_record_success("srv")
        is_open, _ = mgr._cb_check("srv")
        assert not is_open
        assert mgr._consecutive_failures.get("srv") is None

    def test_success_decays_trip_count(self):
        """Success decays trip_count by 1 so flapping servers escalate backoff."""
        mgr = MCPClientManager({})
        mgr._circuit_trip_count["srv"] = 3
        mgr._cb_record_success("srv")
        assert mgr._circuit_trip_count["srv"] == 2
        mgr._cb_record_success("srv")
        assert mgr._circuit_trip_count["srv"] == 1
        mgr._cb_record_success("srv")
        assert "srv" not in mgr._circuit_trip_count

    def test_cooldown_is_exponential(self):
        mgr = MCPClientManager({})
        # First trip (trip_count starts at 0)
        for _ in range(3):
            mgr._cb_record_failure("srv")
        deadline1 = mgr._circuit_open_until["srv"]
        base1 = deadline1 - time.monotonic()
        # Reset circuit but keep trip_count at 1 (set by first trip)
        mgr._cb_record_success("srv")
        # trip_count decayed from 1 to 0 — manually set to 1 for test
        mgr._circuit_trip_count["srv"] = 1
        for _ in range(3):
            mgr._cb_record_failure("srv")
        deadline2 = mgr._circuit_open_until["srv"]
        base2 = deadline2 - time.monotonic()
        # Second trip should have longer cooldown (roughly 2x, within jitter)
        assert base2 > base1 * 1.5

    def test_cooldown_capped_at_max(self):
        mgr = MCPClientManager({})
        mgr._circuit_trip_count["srv"] = 100  # very high trip count
        for _ in range(3):
            mgr._cb_record_failure("srv")
        deadline = mgr._circuit_open_until["srv"]
        cooldown = deadline - time.monotonic()
        # Should not exceed max (300s) + 10% jitter = 330s
        assert cooldown <= mgr._CB_MAX_COOLDOWN * 1.11

    def test_cb_gate_rejects_when_open(self):
        mgr = MCPClientManager({})
        for _ in range(3):
            mgr._cb_record_failure("srv")
        with pytest.raises(RuntimeError, match="circuit open"):
            mgr._cb_gate("srv")

    def test_cb_gate_allows_after_cooldown(self):
        mgr = MCPClientManager({})
        for _ in range(3):
            mgr._cb_record_failure("srv")
        mgr._circuit_open_until["srv"] = time.monotonic() - 1
        # Should not raise
        mgr._cb_gate("srv")
        # Deadline should be removed (half-open probe allowed)
        assert "srv" not in mgr._circuit_open_until

    def test_cb_clear_removes_all_state(self):
        mgr = MCPClientManager({})
        for _ in range(3):
            mgr._cb_record_failure("srv")
        mgr._cb_clear("srv")
        assert "srv" not in mgr._consecutive_failures
        assert "srv" not in mgr._circuit_open_until
        assert "srv" not in mgr._circuit_trip_count

    def test_call_tool_sync_records_failure_on_timeout(self):
        mgr = MCPClientManager({"test": {"type": "stdio", "command": "echo"}})
        mock_session = MagicMock()
        mock_session.call_tool = MagicMock(return_value="sentinel")
        _seed_static_state(mgr, "test", session=mock_session)
        mgr._loop = MagicMock()
        mgr._tool_map["mcp__test__ping"] = ("test", "ping")
        mock_future = MagicMock()
        mock_future.result.side_effect = concurrent.futures.TimeoutError()
        with (
            patch("asyncio.run_coroutine_threadsafe", new=_dispatch_stub(mock_future)),
            pytest.raises(TimeoutError),
        ):
            mgr.call_tool_sync("mcp__test__ping", {}, timeout=1)
        assert mgr._consecutive_failures.get("test", 0) == 1

    def test_call_tool_sync_records_success(self):
        mgr = MCPClientManager({"test": {"type": "stdio", "command": "echo"}})
        mock_session = MagicMock()
        mock_session.call_tool = MagicMock(return_value="sentinel")
        _seed_static_state(mgr, "test", session=mock_session)
        mgr._loop = MagicMock()
        mgr._tool_map["mcp__test__ping"] = ("test", "ping")
        # Pre-set a failure
        mgr._consecutive_failures["test"] = 2
        mock_result = MagicMock()
        mock_result.content = []
        mock_result.isError = False
        mock_future = MagicMock()
        mock_future.result.return_value = mock_result
        with patch("asyncio.run_coroutine_threadsafe", new=_dispatch_stub(mock_future)):
            mgr.call_tool_sync("mcp__test__ping", {}, timeout=5)
        assert mgr._consecutive_failures.get("test") is None

    def test_connection_error_evicts_session(self):
        mgr = MCPClientManager({"test": {"type": "stdio", "command": "echo"}})
        mock_session = MagicMock()
        mock_session.call_tool = MagicMock(return_value="sentinel")
        # Seed session + owner so the test can verify the owner survives.
        old_owner = MagicMock()
        old_streams = (MagicMock(), MagicMock())
        _seed_static_state(
            mgr, "test", session=mock_session, owner_task=old_owner, streams=old_streams
        )
        mgr._loop = MagicMock()
        mgr._tool_map["mcp__test__ping"] = ("test", "ping")
        mock_future = MagicMock()
        mock_future.result.side_effect = BrokenPipeError("dead")
        with (
            patch("asyncio.run_coroutine_threadsafe", new=_dispatch_stub(mock_future)),
            pytest.raises(BrokenPipeError),
        ):
            mgr.call_tool_sync("mcp__test__ping", {}, timeout=5)
        # Session evicted, but the owner/streams remain for the stale guard in
        # _connect_one_locked to close on the next reconnect attempt.
        state = mgr._static_servers["test"]
        assert state.session is None
        assert state.owner_task is old_owner
        assert state.streams is old_streams

    def test_independent_circuits_per_server(self):
        mgr = MCPClientManager({})
        for _ in range(3):
            mgr._cb_record_failure("a")
        is_open_a, _ = mgr._cb_check("a")
        is_open_b, _ = mgr._cb_check("b")
        assert is_open_a
        assert not is_open_b

    def test_mcp_error_does_not_trip_circuit(self):
        """Protocol errors (McpError) should not count as transport failures."""
        from mcp import McpError
        from mcp.types import ErrorData

        mgr = MCPClientManager({"test": {"type": "stdio", "command": "echo"}})
        mock_session = MagicMock()
        mock_session.call_tool = MagicMock(return_value="sentinel")
        _seed_static_state(mgr, "test", session=mock_session)
        mgr._loop = MagicMock()
        mgr._tool_map["mcp__test__ping"] = ("test", "ping")
        mock_future = MagicMock()
        mock_future.result.side_effect = McpError(ErrorData(code=-32601, message="tool not found"))
        with (
            patch("asyncio.run_coroutine_threadsafe", new=_dispatch_stub(mock_future)),
            pytest.raises(McpError),
        ):
            mgr.call_tool_sync("mcp__test__ping", {}, timeout=5)
        # Circuit should NOT have recorded a failure
        assert mgr._consecutive_failures.get("test", 0) == 0

    def test_closed_resource_error_evicts_session_and_trips_circuit(self):
        """Regression: the MCP SDK's streamable-http transport raises
        ``anyio.ClosedResourceError`` (NOT BrokenPipeError) when its write
        stream is dead. That must evict the session AND trip the breaker —
        otherwise the corpse session is re-used on every call forever and
        only a full process restart recovers it."""
        import anyio

        mgr = MCPClientManager({"test": {"type": "stdio", "command": "echo"}})
        mock_session = MagicMock()
        mock_session.call_tool = MagicMock(return_value="sentinel")
        _seed_static_state(mgr, "test", session=mock_session)
        mgr._loop = MagicMock()
        mgr._tool_map["mcp__test__ping"] = ("test", "ping")
        mock_future = MagicMock()
        mock_future.result.side_effect = anyio.ClosedResourceError()
        with (
            patch("asyncio.run_coroutine_threadsafe", new=_dispatch_stub(mock_future)),
            pytest.raises(anyio.ClosedResourceError),
        ):
            mgr.call_tool_sync("mcp__test__ping", {}, timeout=5)
        assert mgr._static_servers["test"].session is None
        assert mgr._consecutive_failures.get("test", 0) == 1

    def test_session_terminated_mcperror_evicts_and_trips_circuit(self):
        """Regression: when the MCP SERVER restarts and loses its session map, our
        held mcp-session-id is stale; the server returns HTTP 404 and the SDK
        surfaces McpError(code=32600, 'Session terminated'). That is NOT a healthy
        protocol rejection — the session must be evicted so the next dispatch
        reconnects with a fresh initialize; reusing it 404s forever (restart-hang)."""
        from mcp import McpError
        from mcp.types import ErrorData

        mgr = MCPClientManager({"test": {"type": "stdio", "command": "echo"}})
        mock_session = MagicMock()
        mock_session.call_tool = MagicMock(return_value="sentinel")
        _seed_static_state(mgr, "test", session=mock_session)
        mgr._loop = MagicMock()
        mgr._tool_map["mcp__test__ping"] = ("test", "ping")
        mock_future = MagicMock()
        # Exactly what the streamable-http SDK injects on a 404 stale session.
        mock_future.result.side_effect = McpError(
            ErrorData(code=32600, message="Session terminated")
        )
        with (
            patch("asyncio.run_coroutine_threadsafe", new=_dispatch_stub(mock_future)),
            pytest.raises(McpError),
        ):
            mgr.call_tool_sync("mcp__test__ping", {}, timeout=5)
        assert mgr._static_servers["test"].session is None
        assert mgr._consecutive_failures.get("test", 0) == 1

    def test_httpx_connect_error_evicts_session(self):
        """A dead underlying httpx connection (server down mid-call) is transport
        death, not a protocol rejection — evict so the next call reconnects."""
        import httpx

        mgr = MCPClientManager({"test": {"type": "stdio", "command": "echo"}})
        mock_session = MagicMock()
        mock_session.call_tool = MagicMock(return_value="sentinel")
        _seed_static_state(mgr, "test", session=mock_session)
        mgr._loop = MagicMock()
        mgr._tool_map["mcp__test__ping"] = ("test", "ping")
        mock_future = MagicMock()
        mock_future.result.side_effect = httpx.ConnectError("connection refused")
        with (
            patch("asyncio.run_coroutine_threadsafe", new=_dispatch_stub(mock_future)),
            pytest.raises(httpx.ConnectError),
        ):
            mgr.call_tool_sync("mcp__test__ping", {}, timeout=5)
        assert mgr._static_servers["test"].session is None
        assert mgr._consecutive_failures.get("test", 0) == 1

    def test_connection_closed_mcperror_evicts_and_trips_circuit(self):
        """Regression: when the SDK's ``post_writer`` swallows the transport
        error, a dead connection surfaces as ``McpError(CONNECTION_CLOSED)``.
        Unlike a genuine protocol rejection, this MUST evict + trip the
        breaker so the next dispatch reconnects instead of looping."""
        from mcp import McpError
        from mcp.types import CONNECTION_CLOSED, ErrorData

        mgr = MCPClientManager({"test": {"type": "stdio", "command": "echo"}})
        mock_session = MagicMock()
        mock_session.call_tool = MagicMock(return_value="sentinel")
        _seed_static_state(mgr, "test", session=mock_session)
        mgr._loop = MagicMock()
        mgr._tool_map["mcp__test__ping"] = ("test", "ping")
        mock_future = MagicMock()
        mock_future.result.side_effect = McpError(
            ErrorData(code=CONNECTION_CLOSED, message="connection closed")
        )
        with (
            patch("asyncio.run_coroutine_threadsafe", new=_dispatch_stub(mock_future)),
            pytest.raises(McpError),
        ):
            mgr.call_tool_sync("mcp__test__ping", {}, timeout=5)
        assert mgr._static_servers["test"].session is None
        assert mgr._consecutive_failures.get("test", 0) == 1

    def test_refresh_all_evicts_dead_session_so_next_tick_reconnects(self):
        """Regression: a periodic refresh that hits a dead-but-non-None
        session must null the session so the reconnect branch (gated on
        ``session is None``) fires on the NEXT tick. Without this the
        refresh re-probes the corpse forever — the bug that required a
        full restart."""
        import anyio

        async def _run() -> None:
            mgr = MCPClientManager({})
            mgr._server_configs["test"] = {"type": "stdio", "command": "x"}
            dead = anyio.ClosedResourceError()
            mock_session = MagicMock()
            mock_session.list_tools = AsyncMock(side_effect=dead)
            mock_session.list_resources = AsyncMock(side_effect=dead)
            mock_session.list_resource_templates = AsyncMock(side_effect=dead)
            mock_session.list_prompts = AsyncMock(side_effect=dead)
            _seed_static_state(mgr, "test", session=mock_session)

            results = await mgr._refresh_all("test")

            # Dead session evicted → next refresh tick / dispatch reconnects.
            assert mgr._static_servers["test"].session is None
            ts, outcome = mgr._last_refresh["test"]
            assert outcome == "error:ClosedResourceError"
            # A FAILURE reports None, NOT ([], []) — rendering a failed
            # refresh as "no changes" is the operator lie the sentinel
            # closes; the outcome disambiguates None (error vs skipped).
            assert results["test"] is None
            assert mgr.last_refresh_outcome("test") == "error:ClosedResourceError"

        asyncio.run(_run())

    def test_read_resource_sync_dead_transport_evicts_and_trips_circuit(self):
        """Regression (follow-up): read_resource_sync kept the old
        BrokenPipe/ConnectionReset/EOF-only guard, so a dead streamable-http
        transport surfacing as McpError(CONNECTION_CLOSED) reused the corpse
        session forever — the exact restart-hang call_tool_sync already fixes.
        It must now evict the session AND trip the breaker."""
        from mcp import McpError
        from mcp.types import CONNECTION_CLOSED, ErrorData

        mgr = MCPClientManager({"test": {"type": "stdio", "command": "echo"}})
        mock_session = MagicMock()
        _seed_static_state(mgr, "test", session=mock_session)
        mgr._loop = MagicMock()
        mgr._resource_map = {"file:///x": ("test", "file:///x")}
        mock_future = MagicMock()
        mock_future.result.side_effect = McpError(
            ErrorData(code=CONNECTION_CLOSED, message="connection closed")
        )
        with (
            patch("asyncio.run_coroutine_threadsafe", new=_dispatch_stub(mock_future)),
            pytest.raises(McpError),
        ):
            mgr.read_resource_sync("file:///x", timeout=5)
        assert mgr._static_servers["test"].session is None
        assert mgr._consecutive_failures.get("test", 0) == 1

    def test_read_resource_sync_protocol_mcperror_does_not_evict(self):
        """A healthy protocol rejection (resource not found) must NOT evict the
        session or trip the breaker on the resource path."""
        from mcp import McpError
        from mcp.types import ErrorData

        mgr = MCPClientManager({"test": {"type": "stdio", "command": "echo"}})
        mock_session = MagicMock()
        _seed_static_state(mgr, "test", session=mock_session)
        mgr._loop = MagicMock()
        mgr._resource_map = {"file:///x": ("test", "file:///x")}
        mock_future = MagicMock()
        mock_future.result.side_effect = McpError(
            ErrorData(code=-32602, message="resource not found")
        )
        with (
            patch("asyncio.run_coroutine_threadsafe", new=_dispatch_stub(mock_future)),
            pytest.raises(McpError),
        ):
            mgr.read_resource_sync("file:///x", timeout=5)
        assert mgr._static_servers["test"].session is mock_session
        assert mgr._consecutive_failures.get("test", 0) == 0

    def test_get_prompt_sync_dead_transport_evicts_and_trips_circuit(self):
        """Regression (follow-up): get_prompt_sync had the same corpse-reuse
        bug as read_resource_sync. A dead transport (anyio.ClosedResourceError)
        must evict the session AND trip the breaker."""
        import anyio

        mgr = MCPClientManager({"test": {"type": "stdio", "command": "echo"}})
        mock_session = MagicMock()
        _seed_static_state(mgr, "test", session=mock_session)
        mgr._loop = MagicMock()
        mgr._prompt_map = {"mcp__test__p": ("test", "p")}
        mock_future = MagicMock()
        mock_future.result.side_effect = anyio.ClosedResourceError()
        with (
            patch("asyncio.run_coroutine_threadsafe", new=_dispatch_stub(mock_future)),
            pytest.raises(anyio.ClosedResourceError),
        ):
            mgr.get_prompt_sync("mcp__test__p", timeout=5)
        assert mgr._static_servers["test"].session is None
        assert mgr._consecutive_failures.get("test", 0) == 1

    def test_get_prompt_sync_protocol_mcperror_does_not_evict(self):
        """A healthy protocol rejection must NOT evict on the prompt path."""
        from mcp import McpError
        from mcp.types import ErrorData

        mgr = MCPClientManager({"test": {"type": "stdio", "command": "echo"}})
        mock_session = MagicMock()
        _seed_static_state(mgr, "test", session=mock_session)
        mgr._loop = MagicMock()
        mgr._prompt_map = {"mcp__test__p": ("test", "p")}
        mock_future = MagicMock()
        mock_future.result.side_effect = McpError(
            ErrorData(code=-32602, message="prompt not found")
        )
        with (
            patch("asyncio.run_coroutine_threadsafe", new=_dispatch_stub(mock_future)),
            pytest.raises(McpError),
        ):
            mgr.get_prompt_sync("mcp__test__p", timeout=5)
        assert mgr._static_servers["test"].session is mock_session
        assert mgr._consecutive_failures.get("test", 0) == 0


class TestIsDeadTransport:
    """Direct unit tests for ``_is_dead_transport`` — the single shared gate
    that decides 'tear down and rebuild the session' vs 'healthy protocol
    rejection' across every session-use site."""

    def test_connection_closed_is_dead(self):
        from mcp import McpError
        from mcp.types import CONNECTION_CLOSED, ErrorData

        assert _is_dead_transport(
            McpError(ErrorData(code=CONNECTION_CLOSED, message="connection closed"))
        )

    def test_sdk_session_terminated_is_dead(self):
        """The streamable-http SDK synthesizes EXACTLY code=32600 /
        'Session terminated' when a held mcp-session-id 404s after a server
        restart — keyed off the code so it survives a message reword."""
        from mcp import McpError
        from mcp.types import ErrorData

        assert _is_dead_transport(McpError(ErrorData(code=32600, message="Session terminated")))

    def test_app_session_not_found_is_not_dead(self):
        """#2 regression: a HEALTHY session-owning MCP server (game/shell)
        rejecting a stale id with 'session not found' is a protocol error, NOT
        transport death. The old bare-substring match wrongly evicted the live
        session and tripped the shared breaker for every user."""
        from mcp import McpError
        from mcp.types import ErrorData

        assert not _is_dead_transport(
            McpError(ErrorData(code=-32603, message="Backend session not found"))
        )

    def test_app_session_terminated_message_is_not_dead(self):
        """#8 regression: the message is application-controlled and is NOT matched
        — only the SDK's synthesized code 32600 is. A healthy session-owning
        server that returns a protocol error whose message is EXACTLY 'Session
        terminated' (or a superstring) with a normal code stays breaker-safe."""
        from mcp import McpError
        from mcp.types import ErrorData

        # Exact SDK message but an app protocol code (not 32600) — must NOT be dead.
        assert not _is_dead_transport(
            McpError(ErrorData(code=-32603, message="Session terminated"))
        )
        # Superstring likewise.
        assert not _is_dead_transport(
            McpError(ErrorData(code=-32603, message="Player session terminated by host"))
        )

    def test_plain_protocol_mcperror_is_not_dead(self):
        from mcp import McpError
        from mcp.types import ErrorData

        assert not _is_dead_transport(McpError(ErrorData(code=-32601, message="method not found")))

    def test_httpx_read_timeout_is_dead(self):
        """#7: an idle read timeout on a long-lived streamable-http stream is
        the dominant idle-death mode — and is NOT a builtin TimeoutError, so it
        must be caught here or it falls through to a healthy 'other'."""
        import httpx

        assert not issubclass(httpx.ReadTimeout, TimeoutError)  # premise guard
        assert _is_dead_transport(httpx.ReadTimeout("read timed out"))

    def test_httpx_pool_timeout_is_not_dead(self):
        """PoolTimeout is connection-pool saturation, NOT a dead connection:
        evicting the session can't relieve pool pressure and would trip the
        shared breaker for all users under transient load. The Connect/Read/Write
        timeouts (a dead/hung connection) stay dead."""
        import httpx

        assert not _is_dead_transport(httpx.PoolTimeout("pool exhausted"))
        assert _is_dead_transport(httpx.WriteTimeout("write timed out"))

    def test_httpx_read_error_is_dead(self):
        """#8: a connection that dies mid-read surfaces as httpx.ReadError (a
        NetworkError sibling of the already-handled ConnectError)."""
        import httpx

        assert _is_dead_transport(httpx.ReadError("peer reset"))

    def test_httpx_write_error_is_dead(self):
        import httpx

        assert _is_dead_transport(httpx.WriteError("broken pipe"))

    def test_httpx_local_protocol_error_is_not_dead(self):
        """LocalProtocolError is OUR bug (a malformed request we built), not a
        dead peer — it must NOT be mistaken for transport death."""
        import httpx

        assert not _is_dead_transport(httpx.LocalProtocolError("bad header"))

    def test_anyio_closed_resource_is_dead(self):
        import anyio

        assert _is_dead_transport(anyio.ClosedResourceError())


# ---------------------------------------------------------------------------
# Fix 3: Safe transport stream pre-close
# ---------------------------------------------------------------------------


class TestSafeTransportStreams:
    """Verify stream references are stored and pre-closed."""

    def test_pre_close_streams_closes_both(self):
        mgr = MCPClientManager({})
        stream_a = MagicMock()
        stream_b = MagicMock()
        _seed_static_state(mgr, "srv", streams=(stream_a, stream_b))

        async def _run():
            await mgr._pre_close_streams("srv")

        asyncio.run(_run())
        stream_a.aclose.assert_called_once()
        stream_b.aclose.assert_called_once()
        # Streams cleared, but the state entry itself can remain.
        assert mgr._static_servers["srv"].streams is None

    def test_pre_close_streams_ignores_missing(self):
        mgr = MCPClientManager({})

        async def _run():
            await mgr._pre_close_streams("nonexistent")

        asyncio.run(_run())  # should not raise

    def test_pre_close_streams_suppresses_errors(self):
        mgr = MCPClientManager({})
        stream_a = MagicMock()
        stream_a.aclose.side_effect = RuntimeError("boom")
        stream_b = MagicMock()
        _seed_static_state(mgr, "srv", streams=(stream_a, stream_b))

        async def _run():
            await mgr._pre_close_streams("srv")

        asyncio.run(_run())  # should not raise despite stream_a error
        stream_b.aclose.assert_called_once()

    def test_shutdown_clears_stream_refs(self):
        mgr = MCPClientManager({})
        _seed_static_state(mgr, "srv", streams=(MagicMock(), MagicMock()))
        mgr.shutdown()
        assert len(mgr._static_servers) == 0


# ---------------------------------------------------------------------------
# Notification debounce is covered BEHAVIORALLY (through the real handler) by
# TestStaticNotificationRefresh — per-kind independence, coalescing, and the
# debounce-drop retry arm. The old stamp-arithmetic TestNotificationDebounce
# was deleted: it seeded a dict and asserted time math on the same dict,
# exercising no product code path.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Static-path list_changed refresh — spawned runner protocol (#839)
# ---------------------------------------------------------------------------


class TestStaticNotificationRefresh:
    """The static ``*/list_changed`` protocol: spawn / serialize / coalesce.

    Static twin of ``test_mcp_user_pool.py``'s notification-runner suite.
    The SDK awaits message handlers inline in its receive loop, so a
    handler that awaits a request on the SAME session self-deadlocks the
    loop (#839) — and static sessions are shared per node, so every
    user's in-flight calls on that server stall with it. The refresh must
    be spawned, serialized on the per-name connect lock, coalesced per
    (server, kind), and keep bearer-carrying exception chains out of the
    logs on failure.
    """

    def test_list_changed_handler_spawns_refresh_off_receive_loop(self, running_loop_mgr) -> None:
        """The notification handler must SPAWN the refresh, not await it:
        an in-handler request on the same session can never receive its
        response (only the parked receive loop could route it), so
        push-driven static refreshes structurally never completed."""
        mgr, loop, _thread = running_loop_mgr
        refreshed: list[str] = []

        async def _fake_refresh(name: str) -> tuple[list[str], list[str]]:
            refreshed.append(name)
            return [], []

        mgr._refresh_server_tools = _fake_refresh  # type: ignore[method-assign]
        handler = mgr._make_static_notification_handler("srv")

        async def _fire() -> bool:
            mgr._static_connect_lock_for("srv")
            _seed_static_state(mgr, "srv", session=MagicMock())
            note = mcp_types.ServerNotification(
                mcp_types.ToolListChangedNotification(method="notifications/tools/list_changed")
            )
            await handler(note)
            # The handler returned WITHOUT running the refresh inline —
            # it must complete even though the refresh hasn't run yet.
            return len(refreshed) == 0

        returned_before_refresh = _run_on_loop(loop, _fire())
        assert returned_before_refresh, "handler must not await the refresh inline"
        _drain_background(mgr, loop)
        assert refreshed == ["srv"]
        # Debounce stamp consumed at schedule time (storm dedupe); the
        # runner released its coalesce marker at lock acquire.
        assert ("srv", "tools") in mgr._last_notification_refresh
        assert not mgr._static_refresh_pending

    def test_handler_ignores_non_list_changed_messages(self, running_loop_mgr) -> None:
        """Non-ServerNotification messages (RequestResponder / Exception)
        must schedule nothing — no stamp, no marker, no task."""
        mgr, loop, _thread = running_loop_mgr
        handler = mgr._make_static_notification_handler("srv")

        async def _fire() -> int:
            before = len(mgr._background_tasks)
            await handler(MagicMock())
            return len(mgr._background_tasks) - before

        assert _run_on_loop(loop, _fire()) == 0
        assert ("srv", "tools") not in mgr._last_notification_refresh
        assert not mgr._static_refresh_pending

    def test_notification_coalesces_when_refresh_already_queued(self, running_loop_mgr) -> None:
        """While a runner is queued for a server+kind (coalesce marker
        set), further notifications spawn NOTHING — the parked runner's
        fresh list observes their change when it acquires the lock. This
        bounds the connect lock's waiter queue at one parked runner per
        server+kind: without it a notifying-but-slow server accretes
        waiters (admitted 1/5s, drained 1/30s) that starve the reconnect
        drivers sharing the lock, while ``_background_tasks`` grows
        without bound."""
        mgr, loop, _thread = running_loop_mgr
        refreshed: list[str] = []

        async def _fake_refresh(name: str) -> tuple[list[str], list[str]]:
            refreshed.append(name)
            return [], []

        mgr._refresh_server_tools = _fake_refresh  # type: ignore[method-assign]
        handler = mgr._make_static_notification_handler("srv")

        async def _fire_twice() -> int:
            lock = mgr._static_connect_lock_for("srv")
            _seed_static_state(mgr, "srv", session=MagicMock())
            await lock.acquire()  # park the first runner
            note = mcp_types.ServerNotification(
                mcp_types.ToolListChangedNotification(method="notifications/tools/list_changed")
            )
            before = len(mgr._background_tasks)
            await handler(note)
            # Age the stamp past the debounce window: the MARKER (not
            # the stamp) must do the suppression for the second fire.
            mgr._last_notification_refresh[("srv", "tools")] = time.monotonic() - 10.0
            await handler(note)
            spawned = len(mgr._background_tasks) - before
            lock.release()
            return spawned

        spawned = _run_on_loop(loop, _fire_twice())
        _drain_background(mgr, loop)
        assert spawned == 1, "second notification must coalesce into the parked runner"
        assert refreshed == ["srv"]
        # The runner released its own marker at lock acquire.
        assert not mgr._static_refresh_pending

    def test_notification_refresh_failure_keeps_debounce_stamp(self, running_loop_mgr) -> None:
        """A failed spawned refresh must KEEP the debounce stamp: popping
        it re-arms the handler on every notification, so a fast-failing
        server spawns unthrottled refresh tasks at its notification rate.
        The bounded cost — a change announced in the remainder of the
        failed window waits for the health tick's retry (armed by this
        failure), the server's next ``list_changed``, or the next
        reconnect — is the lesser failure (every teardown pops the
        stamp, so a reconnect's first notification refreshes immediately).
        The recorded operator error is ``type: message`` — never the
        serialized exception chain, which can carry the configured
        bearer for ``auth_type=static``."""
        mgr, loop, _thread = running_loop_mgr

        async def _seed() -> None:
            mgr._static_connect_lock_for("srv")
            _seed_static_state(mgr, "srv", session=MagicMock())

        _run_on_loop(loop, _seed())
        mgr._last_notification_refresh[("srv", "tools")] = 123.0

        async def _boom(_name: str) -> tuple[list[str], list[str]]:
            raise TimeoutError("slow server")

        _run_on_loop(loop, mgr._run_static_notification_refresh("srv", "tools", _boom))
        assert mgr._last_notification_refresh[("srv", "tools")] == 123.0
        # type + message, never the serialized chain: the message text is
        # diagnostic and header-free; exc_info's chained request carries
        # the configured bearer.
        assert mgr._last_error["srv"] == "Refresh failed: TimeoutError: slow server"
        # The failure armed the health-tick retry — the only automatic
        # driver left (no periodic pass; the server may never re-push).
        assert "srv" in mgr._static_refresh_retry
        mgr._static_refresh_retry.discard("srv")

        async def _ok(_name: str) -> tuple[list[str], list[str]]:
            return [], []

        mgr._last_notification_refresh[("srv", "tools")] = 456.0
        _run_on_loop(loop, mgr._run_static_notification_refresh("srv", "tools", _ok))
        assert mgr._last_notification_refresh[("srv", "tools")] == 456.0
        # A single-kind push SUCCESS does NOT clear the server error pill:
        # it refreshed one kind, not the whole server, so it can't declare
        # health (a different kind may still be broken). The prior failure's
        # error stands until the full-pass retry clears it.
        assert "srv" in mgr._last_error

    def test_notification_refresh_catches_exception_group(self, running_loop_mgr) -> None:
        """A wedged anyio transport surfaces session-op failures as
        ``BaseExceptionGroup`` — which ``except Exception`` misses. An
        escaping group reaches ``_spawn_background``'s failure log, whose
        ``exc_info`` serializes the chained httpx request carrying the
        CONFIGURED bearer for ``auth_type=static`` servers. The runner
        must swallow the group (and keep the stamp) like any other
        refresh failure. The member below is a BaseException so the group
        does NOT collapse to ``ExceptionGroup`` (which ``Exception``
        would catch) — the anyio stray-cancel shape."""
        mgr, loop, _thread = running_loop_mgr

        async def _seed() -> None:
            mgr._static_connect_lock_for("srv")
            _seed_static_state(mgr, "srv", session=MagicMock())

        _run_on_loop(loop, _seed())
        mgr._last_notification_refresh[("srv", "tools")] = 123.0

        async def _wedge(_name: str) -> tuple[list[str], list[str]]:
            raise BaseExceptionGroup("wedged transport", [asyncio.CancelledError()])

        _run_on_loop(loop, mgr._run_static_notification_refresh("srv", "tools", _wedge))
        assert mgr._last_notification_refresh[("srv", "tools")] == 123.0
        assert mgr._last_error["srv"] == (
            "Refresh failed: BaseExceptionGroup: wedged transport (1 sub-exception)"
        )

    def test_notification_refresh_serializes_on_connect_lock(self, running_loop_mgr) -> None:
        """The runner must take the per-name connect lock before
        refreshing: an unserialized refresh races an in-flight
        ``_connect_one_locked``'s discovery wiring (which would overwrite
        the refresh's newer catalog with its older snapshot) and sibling
        same-server refreshes (the slower list call publishing the older
        catalog last)."""
        mgr, loop, _thread = running_loop_mgr
        refreshed: list[str] = []

        async def _rec(name: str) -> tuple[list[str], list[str]]:
            refreshed.append(name)
            return [], []

        async def _scenario() -> bool:
            lock = mgr._static_connect_lock_for("srv")
            _seed_static_state(mgr, "srv", session=MagicMock())
            await lock.acquire()
            task = asyncio.ensure_future(mgr._run_static_notification_refresh("srv", "tools", _rec))
            for _ in range(3):
                await asyncio.sleep(0)
            held_back = len(refreshed) == 0
            lock.release()
            await task
            return held_back

        held_back = _run_on_loop(loop, _scenario())
        assert held_back, "refresh must wait for the connect lock"
        assert refreshed == ["srv"]

    def test_notification_refresh_discards_when_server_removed_while_waiting(
        self, running_loop_mgr
    ) -> None:
        """``remove_server_sync`` retires the lock object after teardown;
        a runner that parked on the OLD lock must not touch state now
        owned by a NEW-lock holder (remove → re-add): the notification
        belonged to the old transport and the re-add publishes its own
        discovery under the new lock."""
        mgr, loop, _thread = running_loop_mgr
        refreshed: list[str] = []

        async def _rec(name: str) -> tuple[list[str], list[str]]:
            refreshed.append(name)
            return [], []

        async def _scenario() -> None:
            old_lock = mgr._static_connect_lock_for("srv")
            _seed_static_state(mgr, "srv", session=MagicMock())
            await old_lock.acquire()
            task = asyncio.ensure_future(mgr._run_static_notification_refresh("srv", "tools", _rec))
            for _ in range(3):
                await asyncio.sleep(0)
            # Simulate remove → re-add while the runner is parked: the
            # lock object is retired and a fresh one minted.
            mgr._static_connect_locks.pop("srv", None)
            mgr._static_connect_lock_for("srv")
            old_lock.release()
            await task

        _run_on_loop(loop, _scenario())
        assert refreshed == []

    def test_notification_refresh_skips_when_session_evicted_while_parked(
        self, running_loop_mgr
    ) -> None:
        """A session evicted while the runner was parked returns quietly:
        the reconnect's rediscovery republishes, and every teardown pops
        the debounce stamp, so the reconnected transport's first
        notification refreshes immediately."""
        mgr, loop, _thread = running_loop_mgr
        refreshed: list[str] = []

        async def _rec(name: str) -> tuple[list[str], list[str]]:
            refreshed.append(name)
            return [], []

        async def _scenario() -> None:
            lock = mgr._static_connect_lock_for("srv")
            state = _seed_static_state(mgr, "srv", session=MagicMock())
            await lock.acquire()
            task = asyncio.ensure_future(mgr._run_static_notification_refresh("srv", "tools", _rec))
            for _ in range(3):
                await asyncio.sleep(0)
            state.session = None  # evicted while parked
            lock.release()
            await task

        _run_on_loop(loop, _scenario())
        assert refreshed == []

    def test_finally_does_not_clobber_successor_marker(self, running_loop_mgr) -> None:
        """After the at-acquire discard, a marker present at the runner's
        exit belongs to the SUCCESSOR spawned during its in-flight list
        call — the finally must not discard it, or the handler mints
        runners past the one-parked-runner bound."""
        mgr, loop, _thread = running_loop_mgr
        marker = ("srv", "tools")

        async def _refresh_readding(_name: str) -> tuple[list[str], list[str]]:
            # Our own marker was discarded at lock-acquire; a successor
            # spawned mid-refresh re-adds the same (server, kind) marker.
            assert marker not in mgr._static_refresh_pending
            mgr._static_refresh_pending.add(marker)
            return [], []

        async def _scenario() -> None:
            mgr._static_connect_lock_for("srv")
            _seed_static_state(mgr, "srv", session=MagicMock())
            mgr._static_refresh_pending.add(marker)  # our spawn's marker
            await mgr._run_static_notification_refresh("srv", "tools", _refresh_readding)

        _run_on_loop(loop, _scenario())
        assert marker in mgr._static_refresh_pending, "successor's marker must survive our exit"
        mgr._static_refresh_pending.discard(marker)

    def test_cancel_while_parked_releases_marker(self, running_loop_mgr) -> None:
        """A runner cancelled while PARKED on the connect lock never
        reached the at-acquire discard — the finally must release its
        marker, or the server+kind never refreshes again."""
        mgr, loop, _thread = running_loop_mgr
        marker = ("srv", "tools")

        async def _rec(_name: str) -> tuple[list[str], list[str]]:
            return [], []

        async def _scenario() -> None:
            lock = mgr._static_connect_lock_for("srv")
            _seed_static_state(mgr, "srv", session=MagicMock())
            await lock.acquire()
            mgr._static_refresh_pending.add(marker)
            task = asyncio.ensure_future(mgr._run_static_notification_refresh("srv", "tools", _rec))
            for _ in range(3):
                await asyncio.sleep(0)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            lock.release()

        _run_on_loop(loop, _scenario())
        assert marker not in mgr._static_refresh_pending

    def test_runner_releases_marker_when_lock_already_retired(self, running_loop_mgr) -> None:
        """Spawn → remove completes (lock retired) → runner starts: it
        must release its marker and must NOT mint a fresh lock for the
        removed server."""
        mgr, loop, _thread = running_loop_mgr
        marker = ("gone", "tools")
        mgr._static_refresh_pending.add(marker)

        async def _rec(_name: str) -> tuple[list[str], list[str]]:
            raise AssertionError("must not refresh a removed server")

        _run_on_loop(loop, mgr._run_static_notification_refresh("gone", "tools", _rec))
        assert marker not in mgr._static_refresh_pending
        assert "gone" not in mgr._static_connect_locks

    def test_teardown_static_session_pops_debounce_stamp(self, running_loop_mgr) -> None:
        """Every teardown path must pop the debounce stamp — the
        keep-stamp-on-failure design leans on it: a reconnect's first
        ``list_changed`` refreshes immediately, so a change announced in
        a failed window converges at the next reconnect."""
        mgr, loop, _thread = running_loop_mgr

        async def _scenario() -> None:
            _seed_static_state(mgr, "srv", session=MagicMock())
            mgr._last_notification_refresh[("srv", "tools")] = 123.0
            mgr._last_notification_refresh[("srv", "prompts")] = 123.0
            await mgr._teardown_static_session("srv")

        _run_on_loop(loop, _scenario())
        assert ("srv", "tools") not in mgr._last_notification_refresh
        assert ("srv", "prompts") not in mgr._last_notification_refresh

    def test_owner_death_pops_debounce_stamp(self, running_loop_mgr) -> None:
        """The unrequested-collapse path (owner done-callback) is a
        teardown too: the stamp must not outlive the transport."""
        mgr, loop, _thread = running_loop_mgr

        async def _scenario() -> None:
            state = _seed_static_state(mgr, "srv", session=MagicMock())

            async def _noop() -> None:
                pass

            task = asyncio.ensure_future(_noop())
            await task
            state.owner_task = task
            mgr._last_notification_refresh[("srv", "tools")] = 123.0
            mgr._on_static_owner_death("srv", task)

        _run_on_loop(loop, _scenario())
        assert ("srv", "tools") not in mgr._last_notification_refresh

    def test_dead_transport_eviction_pops_debounce_stamp(self) -> None:
        """The dispatch-observed transport-failure eviction is a teardown
        too — the stamp must not survive the session it throttled."""
        mgr = MCPClientManager({})
        _seed_static_state(mgr, "srv", session=MagicMock())
        mgr._last_notification_refresh[("srv", "tools")] = 123.0
        mgr._record_and_evict_on_dead_transport("srv", anyio.ClosedResourceError())
        assert mgr._static_servers["srv"].session is None
        assert ("srv", "tools") not in mgr._last_notification_refresh

    def test_refresh_server_tools_bounded_by_timeout(self) -> None:
        """A wedged server's list call must not hang a spawned refresh
        (and the connect lock it holds) forever — #839's unbounded
        ``list_tools`` is what turned the receive-loop park into a
        permanent wedge on main."""
        mgr = MCPClientManager({})
        mgr._CONNECT_TIMEOUT = 0.05  # instance override of the class constant

        async def _hang() -> Any:
            await asyncio.sleep(30)

        session = MagicMock()
        session.list_tools = _hang
        _seed_static_state(mgr, "srv", session=session)

        async def _run() -> None:
            await mgr._refresh_server_tools("srv")

        with pytest.raises(TimeoutError):
            asyncio.run(_run())

    def test_refresh_server_skips_when_session_evicted(self, running_loop_mgr) -> None:
        """A spawned retry/post-reconnect pass racing an eviction must
        SKIP (None), not run the gather into a RuntimeError that
        manufactures a false operator error pill for a self-healing
        condition — the reconnect's rediscovery owns convergence."""
        mgr, loop, _thread = running_loop_mgr
        refreshed: list[str] = []

        async def _rec(_name: str) -> tuple[list[str], list[str]]:
            refreshed.append("ran")
            return [], []

        mgr._refresh_server_tools = _rec  # type: ignore[method-assign]
        mgr._refresh_server_resources = _rec  # type: ignore[method-assign]
        mgr._refresh_server_prompts = _rec  # type: ignore[method-assign]

        async def _scenario() -> Any:
            mgr._static_connect_lock_for("srv")
            _seed_static_state(mgr, "srv", session=None)  # evicted before the pass ran
            return await mgr._refresh_server("srv")

        result = _run_on_loop(loop, _scenario())
        assert result is None
        assert refreshed == []
        assert "srv" not in mgr._last_refresh
        assert "srv" not in mgr._last_error

    def test_static_resources_refresh_caps_published_list(self) -> None:
        """The static push path must cap resource publication like the
        pool twin — a misbehaving server's push must not balloon the
        shared node's merged catalogs."""
        mgr = MCPClientManager({})

        def _resource(i: int) -> MagicMock:
            r = MagicMock()
            r.uri = f"file:///r/{i}"
            r.name = f"r{i}"
            r.description = ""
            r.mimeType = "text/plain"
            return r

        res_result = MagicMock()
        res_result.resources = [_resource(i) for i in range(_MAX_RESOURCES_PER_SERVER + 50)]
        tmpl_result = MagicMock()
        tmpl_result.resourceTemplates = []
        session = MagicMock()
        session.list_resources = AsyncMock(return_value=res_result)
        session.list_resource_templates = AsyncMock(return_value=tmpl_result)
        _seed_static_state(mgr, "srv", session=session, supports_resources=True)

        async def _run() -> None:
            await mgr._refresh_server_resources("srv")

        asyncio.run(_run())
        assert len(mgr._static_servers["srv"].resources) == _MAX_RESOURCES_PER_SERVER

    def test_different_kind_notification_not_debounced(self, running_loop_mgr) -> None:
        """A tools push must not swallow a prompts push arriving inside
        the same debounce window: refreshes are kind-scoped, so a
        server-scoped stamp would drop the prompts notification outright
        — no runner exists or is spawned for prompts, and the new prompt
        never appears until the server pushes again (the same staleness
        class #839 was opened to fix)."""
        mgr, loop, _thread = running_loop_mgr
        refreshed: list[str] = []

        async def _rec_tools(_name: str) -> tuple[list[str], list[str]]:
            refreshed.append("tools")
            return [], []

        async def _rec_prompts(_name: str) -> None:
            refreshed.append("prompts")

        mgr._refresh_server_tools = _rec_tools  # type: ignore[method-assign]
        mgr._refresh_server_prompts = _rec_prompts  # type: ignore[method-assign]
        handler = mgr._make_static_notification_handler("srv")

        async def _fire_both() -> None:
            mgr._static_connect_lock_for("srv")
            _seed_static_state(mgr, "srv", session=MagicMock())
            tools_note = mcp_types.ServerNotification(
                mcp_types.ToolListChangedNotification(method="notifications/tools/list_changed")
            )
            prompts_note = mcp_types.ServerNotification(
                mcp_types.PromptListChangedNotification(method="notifications/prompts/list_changed")
            )
            await handler(tools_note)
            # Immediately inside the tools stamp's window.
            await handler(prompts_note)

        _run_on_loop(loop, _fire_both())
        _drain_background(mgr, loop)
        assert sorted(refreshed) == ["prompts", "tools"], (
            "a same-window different-kind notification must spawn its own refresh"
        )

    def test_scheduling_failure_rolls_back_stamp_and_marker(self, running_loop_mgr) -> None:
        """If _spawn_background raises (loop tearing down), BOTH the coalesce
        marker and the debounce stamp we just wrote must be rolled back —
        else a same-kind push landing in the window afterward is debounced
        against a stamp for a refresh that never spawned, and (on the pool
        path, no on_debounce_drop) never recovers."""
        mgr, loop, _thread = running_loop_mgr
        handler = mgr._make_static_notification_handler("srv")

        def _boom(coro: Any, _label: str) -> None:
            coro.close()  # avoid "coroutine was never awaited" — prod is loop-teardown
            raise RuntimeError("loop closing")

        async def _fire() -> None:
            mgr._static_connect_lock_for("srv")
            _seed_static_state(mgr, "srv", session=MagicMock())
            note = mcp_types.ServerNotification(
                mcp_types.ToolListChangedNotification(method="notifications/tools/list_changed")
            )
            with patch.object(mgr, "_spawn_background", side_effect=_boom):
                await handler(note)  # must swallow the scheduling failure

        _run_on_loop(loop, _fire())
        assert ("srv", "tools") not in mgr._last_notification_refresh, "stamp must roll back"
        assert ("srv", "tools") not in mgr._static_refresh_pending, "marker must roll back"

    def test_first_notification_not_debounced_near_boot(self, running_loop_mgr) -> None:
        """The FIRST (server, kind) notification must never be debounced —
        even when ``time.monotonic()`` is < the debounce window (a node
        whose process started < 5s after boot). An absent stamp (None), not
        a 0.0 default, means 'never refreshed → always admit'."""
        mgr, loop, _thread = running_loop_mgr
        refreshed: list[str] = []

        async def _rec(name: str) -> tuple[list[str], list[str]]:
            refreshed.append(name)
            return [], []

        mgr._refresh_server_tools = _rec  # type: ignore[method-assign]
        handler = mgr._make_static_notification_handler("srv")

        async def _fire() -> None:
            mgr._static_connect_lock_for("srv")
            _seed_static_state(mgr, "srv", session=MagicMock())
            note = mcp_types.ServerNotification(
                mcp_types.ToolListChangedNotification(method="notifications/tools/list_changed")
            )
            # monotonic() < _NOTIFICATION_DEBOUNCE: a 0.0-default compare
            # would debounce this first push; the None sentinel admits it.
            with patch("turnstone.core.mcp_client.time.monotonic", return_value=2.0):
                await handler(note)

        _run_on_loop(loop, _fire())
        _drain_background(mgr, loop)
        assert refreshed == ["srv"], "the first notification must not be debounced near boot"

    def test_same_kind_debounce_drop_arms_retry(self, running_loop_mgr) -> None:
        """A SAME-kind push landing in the debounce window AFTER the
        previous runner completed (no runner queued) is genuinely lost to
        the window — MCP servers announce a change once. It must arm the
        health-tick retry so the change still converges; otherwise the
        catalog stays stale for every user until an unrelated push or a
        reconnect."""
        mgr, loop, _thread = running_loop_mgr

        async def _rec(_name: str) -> tuple[list[str], list[str]]:
            return [], []

        mgr._refresh_server_tools = _rec  # type: ignore[method-assign]
        mgr._server_configs["srv"] = {"type": "http", "url": "http://x/mcp"}
        handler = mgr._make_static_notification_handler("srv")

        async def _fire_then_redrive() -> int:
            mgr._static_connect_lock_for("srv")
            _seed_static_state(mgr, "srv", session=MagicMock())
            note = mcp_types.ServerNotification(
                mcp_types.ToolListChangedNotification(method="notifications/tools/list_changed")
            )
            await handler(note)  # admitted, runner spawns
            return len(mgr._background_tasks)

        _run_on_loop(loop, _fire_then_redrive())
        _drain_background(mgr, loop)  # first runner completes, clears marker
        # Second same-kind push, still inside the 5s window, NO runner queued.
        mgr._static_refresh_retry.discard("srv")

        async def _fire_again() -> int:
            note = mcp_types.ServerNotification(
                mcp_types.ToolListChangedNotification(method="notifications/tools/list_changed")
            )
            before = len(mgr._background_tasks)
            await handler(note)
            return len(mgr._background_tasks) - before

        spawned = _run_on_loop(loop, _fire_again())
        assert spawned == 0, "debounce must still suppress the spawn (throttle intact)"
        assert "srv" in mgr._static_refresh_retry, "but the lost change must arm the retry"

    def test_debounce_drop_behind_running_runner_does_not_arm(self, running_loop_mgr) -> None:
        """A same-kind push arriving while a runner is STILL queued (marker
        present) is already covered — that runner spawns a successor — so
        the debounce drop must NOT arm the retry (no lost change to
        recover). Guards against the arm firing on every chatty push."""
        mgr, loop, _thread = running_loop_mgr
        mgr._server_configs["srv"] = {"type": "http", "url": "http://x/mcp"}
        handler = mgr._make_static_notification_handler("srv")

        async def _fire_with_marker_present() -> None:
            mgr._static_connect_lock_for("srv")
            _seed_static_state(mgr, "srv", session=MagicMock())
            # Stamp fresh (inside window) AND a runner marker present.
            mgr._last_notification_refresh[("srv", "tools")] = time.monotonic()
            mgr._static_refresh_pending.add(("srv", "tools"))
            note = mcp_types.ServerNotification(
                mcp_types.ToolListChangedNotification(method="notifications/tools/list_changed")
            )
            await handler(note)

        _run_on_loop(loop, _fire_with_marker_present())
        assert "srv" not in mgr._static_refresh_retry, (
            "a push covered by a queued runner must not arm a redundant retry"
        )

    def test_refresh_server_superseded_by_remove_returns_none(self, running_loop_mgr) -> None:
        """A ``_refresh_server`` pass whose server was removed before it
        ran must publish nothing and write NO status: the removal
        cleaned the status maps, and running the list calls would
        resurrect ``_last_refresh`` / ``_last_error`` rows for a server
        that no longer exists (or stamp a false ``ok`` over a re-added
        generation this pass never refreshed)."""
        mgr, loop, _thread = running_loop_mgr
        refreshed: list[str] = []

        async def _rec(_name: str) -> tuple[list[str], list[str]]:
            refreshed.append("ran")
            return [], []

        mgr._refresh_server_tools = _rec  # type: ignore[method-assign]
        mgr._refresh_server_resources = _rec  # type: ignore[method-assign]
        mgr._refresh_server_prompts = _rec  # type: ignore[method-assign]

        async def _scenario() -> Any:
            mgr._static_connect_lock_for("srv")
            _seed_static_state(mgr, "srv", session=MagicMock())
            # Remove completed before the pass ran: state gone, lock free.
            mgr._static_servers.pop("srv")
            return await mgr._refresh_server("srv")

        result = _run_on_loop(loop, _scenario())
        assert result is None
        assert refreshed == []
        assert "srv" not in mgr._last_refresh, "superseded pass must not write a status row"
        assert "srv" not in mgr._last_error

    def test_refresh_server_skips_when_lock_busy(self, running_loop_mgr) -> None:
        """A manual/periodic-style pass must NOT park on a held connect
        lock: the holder is itself a catalog publisher whose publish
        supersedes the pass, and parking would burn ``refresh_sync``'s
        whole 30s budget on one busy server (a reconnect attempt holds
        the lock up to 45s), failing the pass for every healthy server
        queued behind it. Skip → ``None``, no publish, no status row."""
        mgr, loop, _thread = running_loop_mgr
        refreshed: list[str] = []

        async def _rec(_name: str) -> tuple[list[str], list[str]]:
            refreshed.append("ran")
            return [], []

        mgr._refresh_server_tools = _rec  # type: ignore[method-assign]
        mgr._refresh_server_resources = _rec  # type: ignore[method-assign]
        mgr._refresh_server_prompts = _rec  # type: ignore[method-assign]

        async def _scenario() -> Any:
            lock = mgr._static_connect_lock_for("srv")
            _seed_static_state(mgr, "srv", session=MagicMock())
            await lock.acquire()
            try:
                # Returns IMMEDIATELY (no parking) even though the lock
                # is held — a parked pass would deadlock this scenario.
                return await mgr._refresh_server("srv")
            finally:
                lock.release()

        result = _run_on_loop(loop, _scenario())
        assert result is None
        assert refreshed == []
        assert "srv" not in mgr._last_refresh
        assert "srv" not in mgr._last_error

    def test_refresh_server_logged_swallows_failure(self, running_loop_mgr) -> None:
        """The spawned post-reconnect pass has no caller to observe a
        re-raise; an escaping exception would reach ``_spawn_background``'s
        ``exc_info`` failure log, which serializes the bearer-carrying
        request chain for ``auth_type=static`` servers. The wrapper must
        swallow, record the pill, leave the error row written by
        ``_refresh_server`` — and ARM the health-tick retry, the only
        automatic driver that can converge the catalog afterwards."""
        mgr, loop, _thread = running_loop_mgr

        async def _boom(_name: str) -> tuple[list[str], list[str]]:
            raise TimeoutError("slow server")

        mgr._refresh_server_tools = _boom  # type: ignore[method-assign]
        mgr._refresh_server_resources = _boom  # type: ignore[method-assign]
        mgr._refresh_server_prompts = _boom  # type: ignore[method-assign]

        async def _scenario() -> None:
            mgr._static_connect_lock_for("srv")
            _seed_static_state(mgr, "srv", session=MagicMock())
            await mgr._refresh_server_logged("srv")  # must not raise

        _run_on_loop(loop, _scenario())
        assert mgr._last_refresh["srv"][1] == "error:TimeoutError"
        assert mgr._last_error["srv"] == "Refresh failed: TimeoutError: slow server"
        assert "srv" in mgr._static_refresh_retry

    def test_refresh_server_logged_rearms_retry_on_busy_skip(self, running_loop_mgr) -> None:
        """A skipped pass (connect lock busy) must RE-ARM the health-tick
        retry: the lock holder may be a single-kind notification refresh,
        not the full pass the retry wanted, so a skip is not convergence."""
        mgr, loop, _thread = running_loop_mgr

        async def _scenario() -> None:
            lock = mgr._static_connect_lock_for("srv")
            _seed_static_state(mgr, "srv", session=MagicMock())
            await lock.acquire()
            try:
                await mgr._refresh_server_logged("srv")
            finally:
                lock.release()

        _run_on_loop(loop, _scenario())
        assert "srv" in mgr._static_refresh_retry

    def test_health_tick_drains_refresh_retry(self, running_loop_mgr) -> None:
        """An armed retry flag + a live session → the health pass spawns
        one full lock-serialized refresh and clears the flag (a failure
        inside that refresh re-arms it for the next tick)."""
        mgr, loop, _thread = running_loop_mgr
        refreshed: list[str] = []

        async def _rec(name: str) -> tuple[list[str], list[str]]:
            refreshed.append(name)
            return [], []

        mgr._refresh_server_tools = _rec  # type: ignore[method-assign]
        mgr._refresh_server_resources = _rec  # type: ignore[method-assign]
        mgr._refresh_server_prompts = _rec  # type: ignore[method-assign]

        async def _scenario() -> None:
            mgr._static_connect_lock_for("srv")
            session = MagicMock()
            session.send_ping = AsyncMock()
            _seed_static_state(mgr, "srv", session=session)
            mgr._static_refresh_retry.add("srv")
            await mgr._static_health_one("srv", time.monotonic())

        _run_on_loop(loop, _scenario())
        _drain_background(mgr, loop)
        assert refreshed == ["srv", "srv", "srv"], "retry must run the FULL three-kind pass"
        assert "srv" not in mgr._static_refresh_retry

    def test_push_refresh_failure_writes_outcome(self, running_loop_mgr) -> None:
        """A push-driven refresh failure must update last_refresh_outcome
        (not just the error pill) — the pill and outcome are ONE source of
        truth, so a green 'ok' outcome persisting under a red error row is
        a contradictory signal."""
        mgr, loop, _thread = running_loop_mgr
        mgr._server_configs["srv"] = {"type": "http", "url": "http://x/mcp"}
        mgr._last_refresh["srv"] = (1.0, "ok")  # stale prior success

        async def _boom(_name: str) -> tuple[list[str], list[str]]:
            raise TimeoutError("slow")

        async def _scenario() -> None:
            mgr._static_connect_lock_for("srv")
            _seed_static_state(mgr, "srv", session=MagicMock())
            await mgr._run_static_notification_refresh("srv", "tools", _boom)

        _run_on_loop(loop, _scenario())
        assert mgr.last_refresh_outcome("srv") == "error:TimeoutError"

    def test_push_refresh_success_does_not_declare_server_healthy(self, running_loop_mgr) -> None:
        """A single-kind push SUCCESS must NOT clear the server error pill
        or stamp 'ok': ``_last_error`` / ``_last_refresh`` are server-scoped
        but the push refreshed only ONE kind, so a tools-still-broken server
        must not go green because its prompts push succeeded. The full-pass
        health-tick retry (armed by the tools failure) is the authority on
        'ok'."""
        mgr, loop, _thread = running_loop_mgr
        mgr._server_configs["srv"] = {"type": "http", "url": "http://x/mcp"}
        mgr._last_refresh["srv"] = (1.0, "error:TimeoutError")
        mgr._last_error["srv"] = "Refresh failed: TimeoutError"  # tools broken

        async def _ok(_name: str) -> tuple[list[str], list[str]]:
            return [], []  # prompts push succeeds

        async def _scenario() -> None:
            mgr._static_connect_lock_for("srv")
            _seed_static_state(mgr, "srv", session=MagicMock())
            await mgr._run_static_notification_refresh("srv", "prompts", _ok)

        _run_on_loop(loop, _scenario())
        # The tools error stands — the prompts success did not paint the
        # whole server healthy.
        assert mgr.last_refresh_outcome("srv") == "error:TimeoutError"
        assert "srv" in mgr._last_error

    def test_remove_server_pops_last_refresh_outcome(self) -> None:
        """Removal must drop the outcome row — a stale row would make
        last_refresh_outcome report a departed server's last result, and a
        re-add before its first refresh would read it."""
        mgr = MCPClientManager({})  # no loop → direct-mutation branch
        _seed_static_state(mgr, "srv", session=None)
        mgr._server_configs["srv"] = {"type": "http", "url": "http://x/mcp"}
        mgr._last_refresh["srv"] = (1.0, "ok")
        mgr.remove_server_sync("srv")
        assert mgr.last_refresh_outcome("srv") is None

    def test_refresh_failure_for_removed_server_leaves_no_stale_row(self) -> None:
        """A failure observed for a just-removed server must NOT stamp a
        stale error outcome (removal doesn't resurrect the row): the write
        is config-gated."""
        mgr = MCPClientManager({})
        # NOT configured (removed).
        mgr._record_refresh_failure("gone", RuntimeError("x"), context="Refresh")
        assert mgr.last_refresh_outcome("gone") is None
        assert "gone" not in mgr._static_refresh_retry

    def test_session_drop_clears_refresh_retry(self, running_loop_mgr) -> None:
        """Every session drop clears the retry flag — the reconnect's
        full rediscovery supersedes the pending retry. But the refresh
        OUTCOME persists across a drop (only REMOVAL clears it): a
        transient reconnect must not erase the last-known status."""
        mgr, loop, _thread = running_loop_mgr

        async def _scenario() -> None:
            state = _seed_static_state(mgr, "srv", session=MagicMock())
            mgr._static_refresh_retry.add("srv")
            mgr._last_refresh["srv"] = (1.0, "ok")
            mgr._drop_static_session_and_stamp("srv", state)

        _run_on_loop(loop, _scenario())
        assert "srv" not in mgr._static_refresh_retry
        assert mgr.last_refresh_outcome("srv") == "ok", "outcome persists across a session drop"

    def test_refresh_all_arms_retry_on_busy_skip(self, running_loop_mgr) -> None:
        """An operator-requested pass that busy-skips a server must report
        None (distinct from a real ``([], [])`` no-change) AND arm the
        health-tick retry: the lock holder may be a single-kind push
        runner, not the full pass the operator asked for. A ``None`` +
        ``skipped`` status is what keeps the operator from being told a
        stale server is current."""
        mgr, loop, _thread = running_loop_mgr

        async def _scenario() -> dict[str, tuple[list[str], list[str]] | None]:
            lock = mgr._static_connect_lock_for("srv")
            _seed_static_state(mgr, "srv", session=MagicMock())
            await lock.acquire()
            try:
                return await mgr._refresh_all("srv")
            finally:
                lock.release()

        results = _run_on_loop(loop, _scenario())
        assert results == {"srv": None}, "a busy-skip must be None, not a fake no-change"
        assert "srv" in mgr._static_refresh_retry
        assert mgr._last_refresh["srv"][1] == "skipped"

    def test_refresh_all_disconnected_reconnect_deferral_stamps_skipped(
        self, running_loop_mgr
    ) -> None:
        """A disconnected server whose reconnect DEFERS (``_ensure_static_connected``
        returns None: sibling call in flight on the old stack) must stamp
        ``skipped``, not leave a STALE prior ``ok`` — else the endpoint /
        pill report a never-run refresh as current. Lock NOT held here (an
        in_flight defer, not a busy lock), so it reaches the reconnect
        branch."""
        mgr, loop, _thread = running_loop_mgr
        mgr._server_configs["srv"] = {"type": "http", "url": "http://x/mcp"}
        mgr._last_refresh["srv"] = (1.0, "ok")  # stale prior success

        async def _defer(_name: str, _cfg: dict[str, Any], **_kw: Any) -> Any:
            return None  # reconnect deferred

        mgr._ensure_static_connected = _defer  # type: ignore[method-assign]

        async def _scenario() -> dict[str, tuple[list[str], list[str]] | None]:
            _seed_static_state(mgr, "srv", session=None)  # disconnected
            return await mgr._refresh_all("srv")

        results = _run_on_loop(loop, _scenario())
        assert results == {"srv": None}
        assert mgr.last_refresh_outcome("srv") == "skipped", "stale 'ok' must be overwritten"

    def test_refresh_all_reports_server_removed_mid_pass(self, running_loop_mgr) -> None:
        """A server removed from config between the session check and the
        cfg lookup must still appear in the result dict (as None), not be
        silently omitted — an operator refreshing that one server got a
        bare 'refresh complete' with no line at all."""
        mgr, loop, _thread = running_loop_mgr

        async def _scenario() -> dict[str, tuple[list[str], list[str]] | None]:
            # Target present at the top (state exists, disconnected) but
            # NO config → the cfg lookup finds nothing (removed mid-pass).
            _seed_static_state(mgr, "srv", session=None)
            return await mgr._refresh_all("srv")

        results = _run_on_loop(loop, _scenario())
        assert "srv" in results, "a removed-mid-pass server must not vanish from results"
        assert results["srv"] is None

    def test_refresh_all_skips_disconnected_server_with_reconnect_in_flight(
        self, running_loop_mgr
    ) -> None:
        """The DISCONNECTED branch must busy-skip too: a reconnect
        attempt holds the per-name lock for up to 45s, and parking there
        burned the whole 30s ``refresh_sync`` budget on one server,
        starving every healthy server behind it. The in-flight reconnect
        finishes the job (full rediscovery); the pass reports the skip
        as None with a ``skipped`` pill."""
        mgr, loop, _thread = running_loop_mgr
        ensure_calls: list[str] = []

        async def _ensure(name: str, _cfg: dict[str, Any], **_kw: Any) -> Any:
            ensure_calls.append(name)
            return MagicMock()

        mgr._ensure_static_connected = _ensure  # type: ignore[method-assign]
        mgr._server_configs["srv"] = {"type": "http", "url": "http://x/mcp"}

        async def _scenario() -> dict[str, tuple[list[str], list[str]] | None]:
            lock = mgr._static_connect_lock_for("srv")
            _seed_static_state(mgr, "srv", session=None)  # disconnected
            await lock.acquire()  # a reconnect driver holds the lock
            try:
                return await mgr._refresh_all("srv")
            finally:
                lock.release()

        results = _run_on_loop(loop, _scenario())
        assert results == {"srv": None}
        assert ensure_calls == [], "the pass must not park inside _ensure_static_connected"
        assert mgr._last_refresh["srv"][1] == "skipped"

    def test_remove_server_timeout_leaves_retryable_state(self, running_loop_mgr) -> None:
        """A removal cancelled while PARKED behind a lock holder must
        leave RETRYABLE state — config still present (pre-fix it popped
        the config up front, so a timeout stranded a live session and its
        published catalog with no config behind them). The FORCE session
        pre-drop is recoverable precisely because the config survives: the
        health loop reconnects it. The config pop and all cleanup now sit
        under the lock, so a re-remove works."""
        mgr, loop, _thread = running_loop_mgr
        mgr._server_configs["srv"] = {"type": "http", "url": "http://x/mcp"}

        async def _hold() -> None:
            lock = mgr._static_connect_lock_for("srv")
            _seed_static_state(mgr, "srv", session=MagicMock())
            await lock.acquire()

        _run_on_loop(loop, _hold())
        assert mgr.remove_server_sync("srv", timeout=0.3) is False
        # Config survives (the pop is under the lock the timeout never
        # reached), so the health loop can reconnect and a retry works —
        # no config-gone ghost. The session pre-drop is the one recoverable
        # mutation.
        assert "srv" in mgr._server_configs

        async def _release() -> None:
            mgr._static_connect_locks["srv"].release()

        _run_on_loop(loop, _release())
        # The retry completes the removal. Its bool return is ``was_connected``
        # (connected AND removed) — False here because the first attempt's
        # FORCE pre-drop already disconnected the session; the EFFECT is what
        # matters, and every trace of the server is now gone.
        mgr.remove_server_sync("srv", timeout=5)
        assert "srv" not in mgr._server_configs
        assert "srv" not in mgr._static_servers
        assert "srv" not in mgr._static_connect_locks

    def test_remove_cancel_mid_teardown_completes_cleanup(self, running_loop_mgr) -> None:
        """If the caller timeout cancels _remove AFTER the config pop,
        while it is awaiting inside _teardown_static_session, the SYNC
        cleanup in the finally must still run — else config-gone +
        published ghost catalogs strand with no driver to reach them.
        We drive the exact interleave: teardown blocks, the caller times
        out and cancels mid-await, and we assert full cleanup ran."""
        mgr, loop, _thread = running_loop_mgr
        mgr._server_configs["srv"] = {"type": "http", "url": "http://x/mcp"}
        _seed_static_state(
            mgr, "srv", session=MagicMock(), tools=[_fake_openai_tool("mcp__srv__t")]
        )
        mgr._rebuild_tools()

        release = threading.Event()

        async def _blocking_teardown(_name: str) -> None:
            # Simulate a teardown that blocks past the caller budget, then
            # gets cancelled at this await.
            await asyncio.get_running_loop().run_in_executor(None, release.wait)

        mgr._teardown_static_session = _blocking_teardown  # type: ignore[method-assign]

        # Short caller timeout: cancels _remove while it's parked in the
        # blocking teardown (config already popped).
        result = mgr.remove_server_sync("srv", timeout=0.4)
        release.set()  # let the executor wait return so the loop drains
        assert result is False
        # The finally completed the removal despite the mid-teardown cancel:
        # no ghost catalog, no orphaned state.
        deadline = time.time() + 5
        while "srv" in mgr._static_servers and time.time() < deadline:
            time.sleep(0.02)
        assert "srv" not in mgr._static_servers, "finally must complete cleanup on cancel"
        assert "srv" not in mgr._server_configs
        assert "mcp__srv__t" not in mgr._tool_map, "ghost tool must not survive"

    def test_reconcile_keeps_managed_when_removal_times_out(self, running_loop_mgr) -> None:
        """reconcile_sync must NOT discard a name from _db_managed when
        remove_server_sync times out (returns False having mutated
        nothing): discarding it made a DB-driven removal a permanent
        no-op — the removal loop iterates ``_db_managed - desired``, so a
        discarded name can never be retried while the health loop keeps
        the server alive off ``_server_configs``."""
        mgr, _loop, _thread = running_loop_mgr
        mgr._db_managed = {"gone"}
        mgr._server_configs["gone"] = {"type": "http", "url": "http://x/mcp"}

        # remove_server_sync reports timeout (False) WITHOUT clearing config
        # — the mutated-nothing path.
        def _timeout_remove(name: str, timeout: float = 30) -> bool:
            return False

        mgr.remove_server_sync = _timeout_remove  # type: ignore[method-assign]
        # DB now has NO servers, so 'gone' is in the removal set.
        storage = MagicMock()
        storage.list_mcp_servers.return_value = []
        mgr.reconcile_sync(storage)
        # Name RETAINED for the next reconcile to retry (config still present,
        # so the False means "timed out", not "removed").
        assert "gone" in mgr._db_managed, "a timed-out removal must be retried, not abandoned"

    def test_reconnect_sync_drops_session_before_queueing(self, running_loop_mgr) -> None:
        """Force-reconnect drops the session up front so parked
        push-refresh runners bail at their session check instead of
        serializing their 30s list calls ahead of the operator's
        recovery action."""
        mgr, loop, _thread = running_loop_mgr
        mgr._server_configs["srv"] = {"type": "http", "url": "http://x/mcp"}
        session_at_connect: list[Any] = []

        async def _fake_connect(name: str, _cfg: dict[str, Any]) -> None:
            session_at_connect.append(mgr._static_servers[name].session)
            _seed_static_state(mgr, name, session=MagicMock())

        async def _seed() -> None:
            mgr._static_connect_lock_for("srv")
            _seed_static_state(mgr, "srv", session=MagicMock())

        _run_on_loop(loop, _seed())
        with patch.object(mgr, "_connect_one_locked", side_effect=_fake_connect):
            result = mgr.reconnect_sync("srv", timeout=5)
        assert result["connected"] is True
        assert session_at_connect == [None], "session must be dropped before the rebuild"

    def test_resources_refresh_fails_fast_and_reaps_sibling(self) -> None:
        """A fast real error must surface as ITSELF — not be masked
        behind a hung sibling's eventual 30s ``TimeoutError`` — and the
        surviving sibling must be CANCELLED and REAPED inside the scope,
        never left running detached (outside the timeout scope and the
        lock serialization) on the shared session."""
        mgr = MCPClientManager({})
        sibling_events: list[str] = []

        async def _hanging_resources() -> Any:
            try:
                await asyncio.sleep(30)  # would mask the real error as TimeoutError
            except asyncio.CancelledError:
                sibling_events.append("cancelled")
                raise
            sibling_events.append("completed")

        async def _fast_fail_templates() -> Any:
            raise RuntimeError("method not found")

        session = MagicMock()
        session.list_resources = _hanging_resources
        session.list_resource_templates = _fast_fail_templates
        _seed_static_state(mgr, "srv", session=session, supports_resources=True)

        async def _run() -> None:
            await mgr._refresh_server_resources("srv")

        start = time.monotonic()
        with pytest.raises(RuntimeError, match="method not found"):
            asyncio.run(_run())
        assert time.monotonic() - start < 5, "real error must surface fast, not at timeout"
        assert sibling_events == ["cancelled"], (
            "the hung sibling must be cancelled and reaped inside the scope"
        )

    def test_reap_bounded_reraises_external_cancel(self) -> None:
        """An EXTERNAL cancel delivered during the reap window must be
        HONOURED (re-raised), not swallowed — else a shutdown/cancel of
        the refresh runner is silently dropped and the frame completes via
        its original error path instead."""
        mgr = MCPClientManager({})

        async def _run() -> None:
            async def _quick() -> None:
                return None

            t1 = asyncio.ensure_future(_quick())
            t2 = asyncio.ensure_future(_quick())
            await asyncio.gather(t1, t2)  # both done before the reap
            # asyncio.wait raising CancelledError models an external cancel
            # landing on the reap await.
            with (
                patch("asyncio.wait", side_effect=asyncio.CancelledError()),
                pytest.raises(asyncio.CancelledError),
            ):
                await mgr._reap_bounded((t1, t2))

        asyncio.run(_run())

    def test_remove_server_clears_markers_and_stamps(self) -> None:
        """``remove_server_sync`` must clear the server's coalesce
        markers: a parked old-generation runner's marker would otherwise
        coalesce AWAY a re-added server's first push, and that runner
        bails at its lock-identity check without refreshing — the pushed
        change would be silently dropped."""
        mgr = MCPClientManager({})  # no loop → direct-mutation branch
        _seed_static_state(mgr, "srv", session=None)
        mgr._server_configs["srv"] = {"type": "http", "url": "http://x/mcp"}
        mgr._static_refresh_pending.add(("srv", "tools"))
        mgr._static_refresh_pending.add(("srv", "prompts"))
        mgr._last_notification_refresh[("srv", "tools")] = 123.0
        mgr.remove_server_sync("srv")
        assert not mgr._static_refresh_pending
        assert ("srv", "tools") not in mgr._last_notification_refresh

    def test_refresh_server_runs_and_publishes_when_lock_free(self, running_loop_mgr) -> None:
        """The happy path: lock free → the pass acquires it, runs all
        three kind refreshes under it, and records the ``ok`` row.
        (Serialization against a BUSY lock is the skip test above — the
        pass never runs concurrently with another publisher.)"""
        mgr, loop, _thread = running_loop_mgr
        refreshed: list[str] = []

        async def _rec_tools(name: str) -> tuple[list[str], list[str]]:
            refreshed.append(name)
            return [], []

        async def _rec_none(_name: str) -> None:
            return None

        mgr._refresh_server_tools = _rec_tools  # type: ignore[method-assign]
        mgr._refresh_server_resources = _rec_none  # type: ignore[method-assign]
        mgr._refresh_server_prompts = _rec_none  # type: ignore[method-assign]

        async def _scenario() -> Any:
            mgr._static_connect_lock_for("srv")
            _seed_static_state(mgr, "srv", session=MagicMock())
            return await mgr._refresh_server("srv")

        result = _run_on_loop(loop, _scenario())
        assert result == ([], [])
        assert refreshed == ["srv"]
        assert mgr._last_refresh["srv"][1] == "ok"


# ---------------------------------------------------------------------------
# reconnect_sync — operator-driven full reconnect
# ---------------------------------------------------------------------------


class TestReconnectSync:
    """Verify reconnect_sync tears down old session, clears CB, calls _connect_one."""

    def test_reconnect_unknown_server_returns_error(self):
        mgr = MCPClientManager({})
        result = mgr.reconnect_sync("missing")
        assert result == {
            "connected": False,
            "tools": 0,
            "resources": 0,
            "prompts": 0,
            "error": "unknown server",
        }

    def test_reconnect_clears_circuit_breaker(self, running_loop_mgr):
        mgr, _loop, _thread = running_loop_mgr

        async def _fake_connect_one(name: str, _cfg: dict[str, Any]) -> None:
            _seed_static_state(mgr, name, session=MagicMock())

        # Pre-trip the breaker
        for _ in range(3):
            mgr._cb_record_failure("srv")
        assert "srv" in mgr._circuit_open_until

        with (
            # reconnect_sync holds the per-name lock and calls the LOCKED body.
            patch.object(mgr, "_connect_one_locked", side_effect=_fake_connect_one),
            patch.object(mgr, "_pre_close_streams", new=AsyncMock()),
        ):
            result = mgr.reconnect_sync("srv")
        assert result["connected"] is True
        assert result["error"] == ""
        assert "srv" not in mgr._circuit_open_until
        assert "srv" not in mgr._consecutive_failures

    def test_reconnect_closes_old_session_then_calls_connect_one(self, running_loop_mgr):
        """The FORCE-rebuild teardown lives in ``_connect_one_locked``'s
        stale-guard (the one canonical ``_teardown_static_session`` sequence) —
        ``reconnect_sync`` no longer carries its own copy. Drive the REAL locked
        body via a no-command stdio cfg: the stale-guard runs, then the connect
        early-returns, so the ordering is observable without a live server."""
        mgr, loop, _thread = running_loop_mgr
        mgr._server_configs["srv"] = {"type": "stdio"}  # no command → early return

        order: list[str] = []

        async def _make_owner() -> tuple[asyncio.Event, asyncio.Task[None]]:
            ev = asyncio.Event()

            async def _parked_owner() -> None:
                await ev.wait()
                order.append("owner_exit")

            task = asyncio.create_task(_parked_owner())
            await asyncio.sleep(0)
            return ev, task

        ev, old_owner = _run_on_loop(loop, _make_owner())

        async def _pre_close(name: str) -> None:
            order.append("pre_close")
            # Session must already be nulled when streams close (canonical order).
            assert mgr._static_servers["srv"].session is None

        # Seed the old session/owner/streams that the stale-guard should close.
        _seed_static_state(
            mgr,
            "srv",
            session=MagicMock(),
            owner_task=old_owner,
            close_requested=ev,
            streams=(MagicMock(), MagicMock()),
        )

        with patch.object(mgr, "_pre_close_streams", side_effect=_pre_close):
            result = mgr.reconnect_sync("srv")
        assert order == ["pre_close", "owner_exit"]  # teardown ran, in order
        assert result["connected"] is False  # no command — nothing to rebuild
        state = mgr._static_servers["srv"]
        assert state.session is None
        assert state.owner_task is None  # old owner cleared from state
        assert old_owner.done() and not old_owner.cancelled()

    def test_reconnect_failure_returns_error_dict(self, running_loop_mgr):
        mgr, _loop, _thread = running_loop_mgr

        async def _connect_one_locked(name: str, _cfg: dict[str, Any]) -> None:
            raise RuntimeError("handshake failed")

        with (
            patch.object(mgr, "_connect_one_locked", side_effect=_connect_one_locked),
            patch.object(mgr, "_pre_close_streams", new=AsyncMock()),
        ):
            result = mgr.reconnect_sync("srv")
        assert result["connected"] is False
        assert "handshake failed" in result["error"]

    def test_reconnect_failure_clears_stale_catalog(self, running_loop_mgr):
        # bug-2: when _connect_one fails mid-reconnect, the per-server
        # catalog must be dropped so the merged tool/resource/prompt maps
        # don't keep advertising entries with no live session.
        mgr, _loop, _thread = running_loop_mgr

        # Seed catalog state from a previous successful connect.
        _seed_static_state(
            mgr,
            "srv",
            tools=[_fake_openai_tool("mcp__srv__t")],
            resources=[_fake_resource_dict(server="srv")],
            prompts=[_fake_prompt_dict(server="srv")],
        )
        mgr._rebuild_tools()
        mgr._rebuild_resources()
        mgr._rebuild_prompts()

        async def _connect_one_locked(name: str, _cfg: dict[str, Any]) -> None:
            raise RuntimeError("handshake failed")

        with (
            patch.object(mgr, "_connect_one_locked", side_effect=_connect_one_locked),
            patch.object(mgr, "_pre_close_streams", new=AsyncMock()),
        ):
            result = mgr.reconnect_sync("srv")
        assert result["connected"] is False
        # Per-server catalog should be cleared and merged maps drained.
        srv_state = mgr._static_servers.get("srv")
        assert srv_state is not None
        assert srv_state.tools == []
        assert srv_state.resources == []
        assert srv_state.prompts == []
        assert "mcp__srv__t" not in mgr._tool_map

    def test_reconnect_preserves_static_state_identity(self, running_loop_mgr):
        # q-3: PR #296 invariant 5 — _static_servers[name] must be the SAME
        # object across a connect → transient-failure → reconnect cycle.
        # Guards against future refactors that pop-and-repopulate the entry,
        # which would invalidate any references held by concurrent readers.
        mgr, _loop, _thread = running_loop_mgr

        # First connect: seed an initial entry as if _connect_one succeeded.
        async def _first_connect(name: str, _cfg: dict[str, Any]) -> None:
            _seed_static_state(mgr, name, session=MagicMock())

        with (
            patch.object(mgr, "_connect_one_locked", side_effect=_first_connect),
            patch.object(mgr, "_pre_close_streams", new=AsyncMock()),
        ):
            mgr.reconnect_sync("srv")

        state_before = mgr._static_servers["srv"]
        id_before = id(state_before)

        # Simulate a transient transport failure: evict the session (as
        # call_tool_sync would on BrokenPipeError) but keep the entry.
        state_before.session = None

        # Reconnect.
        async def _reconnect(name: str, _cfg: dict[str, Any]) -> None:
            _seed_static_state(mgr, name, session=MagicMock())

        with (
            patch.object(mgr, "_connect_one_locked", side_effect=_reconnect),
            patch.object(mgr, "_pre_close_streams", new=AsyncMock()),
        ):
            result = mgr.reconnect_sync("srv")
        assert result["connected"] is True

        state_after = mgr._static_servers["srv"]
        assert id(state_after) == id_before
        assert state_after is state_before


# ---------------------------------------------------------------------------
# _cb_auto_reconnect — refresh-on-reconnect
# ---------------------------------------------------------------------------


class TestCBAutoReconnectRefresh:
    """Verify _cb_auto_reconnect schedules catalog refresh after a successful reconnect.

    The refresh runs as a fire-and-forget background task on the loop so it
    doesn't block the caller (perf-1).  Tests wait briefly for the scheduled
    task to run and observe its effect.
    """

    def test_auto_reconnect_schedules_refresh_server_on_success(self, running_loop_mgr):
        import threading as _threading

        mgr, _loop, _thread = running_loop_mgr

        new_session = MagicMock()
        refresh_event = _threading.Event()

        async def _connect_one(name: str, _cfg: dict[str, Any]) -> None:
            _seed_static_state(mgr, name, session=new_session)

        async def _refresh(name: str) -> tuple[list[str], list[str]]:
            refresh_event.set()
            return [], []

        with (
            # _cb_auto_reconnect coordinates via the per-name lock and calls the
            # LOCKED connect body directly.
            patch.object(mgr, "_connect_one_locked", side_effect=_connect_one),
            patch.object(mgr, "_refresh_server", side_effect=_refresh),
        ):
            session = mgr._cb_auto_reconnect("srv")
            # Wait for the scheduled refresh task to actually run on the loop,
            # then for the tracked task to DRAIN — exiting the patch context
            # while the task is still in flight would hand the un-patched
            # method to its tail.
            assert refresh_event.wait(timeout=5), "refresh task was not scheduled"
            deadline = time.time() + 5
            while mgr._background_tasks and time.time() < deadline:
                time.sleep(0.02)
            assert not mgr._background_tasks, "background refresh task never drained"
        assert session is new_session

    def test_auto_reconnect_retrieves_and_logs_refresh_failure(self, running_loop_mgr):
        """A refresh failure must be logged INSIDE ``_refresh_server_logged``
        (type + message, no ``exc_info``) — not by the done-callback, whose
        ``exc_info`` log serializes the chained ``httpx.Request`` carrying the
        configured bearer for ``auth_type=static`` servers, and not abandoned
        for asyncio to report as "Task exception was never retrieved" at GC
        time (the closed-file CI spew)."""
        import threading as _threading

        mgr, _loop, _thread = running_loop_mgr

        new_session = MagicMock()
        refresh_started = _threading.Event()

        async def _connect_one(name: str, _cfg: dict[str, Any]) -> None:
            _seed_static_state(mgr, name, session=new_session)

        async def _refresh_failing(name: str) -> tuple[list[str], list[str]]:
            refresh_started.set()
            raise RuntimeError("catalog fetch broke")

        with (
            patch.object(mgr, "_connect_one_locked", side_effect=_connect_one),
            patch.object(mgr, "_refresh_server", side_effect=_refresh_failing),
            patch("turnstone.core.mcp_client.log") as mock_log,
        ):
            # Must not raise — refresh failures are non-fatal to the caller.
            session = mgr._cb_auto_reconnect("srv")
            assert refresh_started.wait(timeout=5), "refresh task was not scheduled"
            # Poll for the WARNING while the patch is still active — gating on
            # set-emptiness alone would race the un-patch (review-caught: the
            # warning could land on the restored real logger).
            deadline = time.time() + 5
            warn_calls = []
            while not warn_calls and time.time() < deadline:
                warn_calls = [
                    c
                    for c in mock_log.warning.call_args_list
                    if "Background catalog refresh" in str(c.args)
                ]
                time.sleep(0.02)
            # The tracked task must also fully drain (emptiness now implies
            # "done AND reported" — discard is the callback's LAST step).
            deadline = time.time() + 5
            while mgr._background_tasks and time.time() < deadline:
                time.sleep(0.02)
            assert not mgr._background_tasks, "background refresh task never drained"
            # The done-callback's exc_info channel must have stayed silent:
            # the wrapper swallowed the failure before it could escape.
            assert not [
                c for c in mock_log.warning.call_args_list if "MCP background" in str(c.args[0])
            ], "failure escaped to _spawn_background's exc_info log (bearer-leak channel)"
        assert session is new_session
        assert warn_calls, (
            "the refresh failure must be logged by _refresh_server_logged, not "
            "left for GC-time reporting"
        )
        # type + message as structured args (via _record_refresh_failure:
        # fmt, context, name, type-name, message); exc_info must NOT be
        # passed.
        assert warn_calls[0].kwargs.get("exc_info") is None
        assert warn_calls[0].args[3] == "RuntimeError"
        assert "catalog fetch broke" in str(warn_calls[0].args[4])


class TestStaticHealthLoop:
    """Autonomous static-server reconnect + liveness.

    The SDK gives up reconnecting after 2 attempts on one transport and never on
    the others (verified: mcp 1.28.1), and every other Turnstone reconnect path
    is dispatch- or operator-driven — so a static server that dies while idle
    stays dead. This loop is the missing autonomous trigger: it reconnects
    disconnected servers on a capped, jittered, forever backoff and pings
    connected ones, evicting a dead-but-idle session (which nothing else would
    notice) so it is reconnected.
    """

    # -- backoff policy ------------------------------------------------------

    def test_reconnect_delay_capped_and_jittered(self) -> None:
        mgr = MCPClientManager({})
        # attempt 0: ceiling = base (1s); every draw within [0, base].
        assert all(
            0.0 <= mgr._static_reconnect_delay(0) <= mgr._STATIC_RECONNECT_BASE_S
            for _ in range(200)
        )
        # large / unbounded attempt: never exceeds the cap (no overflow, forever).
        assert all(
            0.0 <= mgr._static_reconnect_delay(a) <= mgr._STATIC_RECONNECT_MAX_S
            for a in (10, 100, 10_000)
        )
        # jitter actually varies (not a fixed value).
        assert len({round(mgr._static_reconnect_delay(8), 6) for _ in range(50)}) > 1

    # -- reconnect (layer 1) -------------------------------------------------

    def test_reconnect_one_success_closes_open_circuit_but_keeps_failure_count(
        self, running_loop_mgr
    ) -> None:
        mgr, loop, _ = running_loop_mgr
        mgr._server_configs["down"] = {"type": "stdio", "command": "echo"}
        _seed_static_state(mgr, "down", session=None)
        mgr._static_reconnect_attempt["down"] = 4  # pretend we'd been failing
        # Trip the breaker OPEN (3 failures) so there is a deadline to clear.
        for _ in range(3):
            mgr._cb_record_failure("down")
        assert "down" in mgr._circuit_open_until

        async def _fake_connect(name: str, _cfg: dict[str, Any]) -> None:
            _seed_static_state(mgr, name, session=MagicMock())

        with (
            # The driver routes through _ensure_static_connected, which calls
            # the LOCKED connect body under the per-name lock.
            patch.object(mgr, "_connect_one_locked", side_effect=_fake_connect),
            patch.object(mgr, "_refresh_server", new=AsyncMock()),
        ):
            _run_on_loop(loop, mgr._static_reconnect_one("down"))

        assert mgr._static_servers["down"].session is not None
        assert "down" not in mgr._static_reconnect_attempt  # health backoff reset
        # A transport reconnect closes the OPEN CIRCUIT (dispatch can flow) ...
        assert "down" not in mgr._circuit_open_until
        # ... but leaves the failure COUNT for a real dispatch to confirm/reset,
        # so a connect-ok / calls-fail server still escalates to a trip.
        assert mgr._consecutive_failures.get("down", 0) >= 1

    def test_reconnect_one_retries_forever_with_growing_backoff(self, running_loop_mgr) -> None:
        mgr, loop, _ = running_loop_mgr
        mgr._server_configs["down"] = {"type": "stdio", "command": "echo"}
        _seed_static_state(mgr, "down", session=None)

        async def _boom(name: str, _cfg: dict[str, Any]) -> None:
            raise RuntimeError("still down")

        with patch.object(mgr, "_connect_one_locked", side_effect=_boom):
            for expected in range(1, 7):
                mgr._static_reconnect_next.pop("down", None)  # force it due
                due = _run_on_loop(loop, mgr._static_reconnect_one("down"))
                assert mgr._static_reconnect_attempt["down"] == expected  # no cap
                assert due > time.monotonic() - 1  # next attempt scheduled

    def test_reconnect_one_queued_behind_connect_reuses_its_session(self, running_loop_mgr) -> None:
        """While another driver holds the per-name connect lock, a health
        reconnect QUEUES on it (no ``.locked()`` skip anymore) and then REUSES
        the session the holder installed — never a second ``_connect_one_locked``
        pile-on tearing down the fresh session."""
        mgr, loop, _ = running_loop_mgr
        mgr._server_configs["down"] = {"type": "stdio", "command": "echo"}
        _seed_static_state(mgr, "down", session=None)
        sess = MagicMock()

        async def _scenario() -> float:
            lock = mgr._static_connect_lock_for("down")
            await lock.acquire()  # a dispatch reconnect in progress
            recon = asyncio.ensure_future(mgr._static_reconnect_one("down"))
            await asyncio.sleep(0.05)
            assert not recon.done()  # queued on the lock, not skipped/failed
            mgr._static_servers["down"].session = sess  # the holder's connect lands
            lock.release()
            return await asyncio.wait_for(recon, timeout=5)

        with (
            patch.object(mgr, "_connect_one_locked", new=AsyncMock()) as cm,
            patch.object(mgr, "_refresh_server", new=AsyncMock()),
        ):
            due = _run_on_loop(loop, _scenario())
        cm.assert_not_awaited()  # reused the holder's session; no second connect
        assert mgr._static_servers["down"].session is sess  # never torn down
        assert due > time.monotonic() - 1  # success cadence scheduled

    def test_connect_one_serializes_concurrent_reconnects(self, running_loop_mgr) -> None:
        """The per-name lock prevents two concurrent ``_connect_one`` for one
        server from interleaving teardown/rebuild on the shared state."""
        mgr, loop, _ = running_loop_mgr
        active = 0
        max_active = 0

        async def _inner(name: str, _cfg: dict[str, Any]) -> None:
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.02)
            active -= 1

        async def _two() -> None:
            with patch.object(mgr, "_connect_one_locked", side_effect=_inner):
                await asyncio.gather(mgr._connect_one("x", {}), mgr._connect_one("x", {}))

        _run_on_loop(loop, _two())
        assert max_active == 1  # serialized, never overlapping

    # -- liveness ping (layer 2 — the idle-dead detection) -------------------

    def test_ping_one_healthy_keeps_session(self, running_loop_mgr) -> None:
        mgr, loop, _ = running_loop_mgr
        sess = MagicMock()
        sess.send_ping = AsyncMock()
        _seed_static_state(mgr, "up", session=sess)
        due = _run_on_loop(loop, mgr._static_ping_one("up", time.monotonic()))
        sess.send_ping.assert_awaited_once()
        assert mgr._static_servers["up"].session is sess  # kept
        assert due > time.monotonic()  # next ping scheduled

    def test_ping_one_dead_session_evicts_for_reconnect(self, running_loop_mgr) -> None:
        """The Turnstone case: a connected-but-dead session that nothing else
        would notice is detected by the ping and evicted so the next tick
        reconnects it."""
        mgr, loop, _ = running_loop_mgr
        sess = MagicMock()
        sess.send_ping = AsyncMock(side_effect=ConnectionResetError("peer gone"))
        _seed_static_state(mgr, "up", session=sess)
        now = time.monotonic()
        _run_on_loop(loop, mgr._static_ping_one("up", now))
        assert mgr._static_servers["up"].session is None  # evicted
        # "Reconnect asap": the (fresh-clock) deadline is already due.
        assert now <= mgr._static_reconnect_next["up"] <= time.monotonic()
        assert "up" in mgr._consecutive_failures  # breaker recorded a failure

    def test_ping_one_timeout_does_not_evict(self, running_loop_mgr) -> None:
        """A ping TIMEOUT means 'slow', not 'dead': the session is kept and the
        ping rescheduled. A strict ping timeout must not churn a heavy-but-
        working server every cycle (only a ``_is_dead_transport`` failure evicts).
        """
        mgr, loop, _ = running_loop_mgr
        mgr._STATIC_HEALTH_PING_TIMEOUT_S = 0.05  # instance shadow for a fast test

        async def _hang() -> None:
            await asyncio.sleep(10)

        sess = MagicMock()
        sess.send_ping = _hang
        _seed_static_state(mgr, "up", session=sess)
        now = time.monotonic()
        due = _run_on_loop(loop, mgr._static_ping_one("up", now))
        assert mgr._static_servers["up"].session is sess  # kept — timeout != dead
        assert "up" not in mgr._consecutive_failures  # breaker NOT tripped
        assert due > now  # next ping rescheduled, not "reconnect asap"

    def test_ping_one_mcp_error_does_not_evict(self, running_loop_mgr) -> None:
        """A protocol ``McpError`` from a healthy connection (server gates/omits
        ``ping``) must NOT evict or trip the breaker — only a dead transport does
        (mirrors ``_record_and_evict_on_dead_transport``)."""
        from mcp import McpError
        from mcp.types import ErrorData

        mgr, loop, _ = running_loop_mgr
        sess = MagicMock()
        sess.send_ping = AsyncMock(
            side_effect=McpError(ErrorData(code=-32601, message="method not found"))
        )
        _seed_static_state(mgr, "up", session=sess)
        now = time.monotonic()
        due = _run_on_loop(loop, mgr._static_ping_one("up", now))
        assert mgr._static_servers["up"].session is sess  # kept — protocol != dead
        assert "up" not in mgr._consecutive_failures  # breaker untouched
        assert due > now  # rescheduled, not "reconnect asap"

    def test_ping_one_pool_timeout_does_not_evict(self, running_loop_mgr) -> None:
        """``httpx.PoolTimeout`` is pool saturation, not a dead session — evicting
        wouldn't relieve it and would trip the shared breaker under load; the ping
        rescheduled instead (``_is_dead_transport`` deliberately excludes it)."""
        import httpx

        mgr, loop, _ = running_loop_mgr
        sess = MagicMock()
        sess.send_ping = AsyncMock(side_effect=httpx.PoolTimeout("pool saturated"))
        _seed_static_state(mgr, "up", session=sess)
        now = time.monotonic()
        due = _run_on_loop(loop, mgr._static_ping_one("up", now))
        assert mgr._static_servers["up"].session is sess  # kept — pool != dead
        assert "up" not in mgr._consecutive_failures
        assert due > now

    def test_ping_one_skips_busy_server(self, running_loop_mgr) -> None:
        """A server with an in-flight dispatch (``in_flight`` > 0) is demonstrably
        alive; the ping is skipped and it is NEVER evicted — the interlock that
        keeps a long-running ``call_tool`` from being torn down under itself."""
        mgr, loop, _ = running_loop_mgr
        sess = MagicMock()
        sess.send_ping = AsyncMock(side_effect=AssertionError("busy server pinged"))
        state = _seed_static_state(mgr, "up", session=sess)
        state.in_flight = 1  # a call_tool is in flight
        now = time.monotonic()
        due = _run_on_loop(loop, mgr._static_ping_one("up", now))
        sess.send_ping.assert_not_awaited()  # skipped entirely
        assert mgr._static_servers["up"].session is sess  # not evicted
        assert "up" not in mgr._consecutive_failures
        assert due > now  # rescheduled

    def test_ping_one_does_not_clobber_concurrent_reconnect(self, running_loop_mgr) -> None:
        """A reconnect that installs a FRESH session during the ping's await
        window must not be undone: the failure handler only nulls the session it
        actually pinged (session-identity check)."""
        mgr, loop, _ = running_loop_mgr
        fresh = MagicMock()  # S2, installed mid-ping by a concurrent reconnect

        async def _die_after_swap() -> None:
            # Model the race: a concurrent reconnect swaps in a fresh session,
            # THEN this stale (S1) ping fails with a dead transport.
            mgr._static_servers["up"].session = fresh
            raise ConnectionResetError("S1 transport died")

        stale = MagicMock()  # S1
        stale.send_ping = _die_after_swap
        _seed_static_state(mgr, "up", session=stale)
        _run_on_loop(loop, mgr._static_ping_one("up", time.monotonic()))
        # S1's failure handler must NOT have nulled the freshly-installed S2.
        assert mgr._static_servers["up"].session is fresh

    # -- in-flight interlock (dispatch side) --------------------------------

    def test_session_op_increments_in_flight_around_op(self, running_loop_mgr) -> None:
        mgr, loop, _ = running_loop_mgr
        state = _seed_static_state(mgr, "srv", session=MagicMock())
        seen: list[int] = []

        async def _op() -> str:
            seen.append(state.in_flight)  # observed WHILE in flight
            return "ok"

        assert _run_on_loop(loop, mgr._static_session_op("srv", _op())) == "ok"
        assert seen == [1]  # bumped for the duration of the op
        assert state.in_flight == 0  # decremented in finally

    def test_session_op_decrements_in_flight_on_error(self, running_loop_mgr) -> None:
        mgr, loop, _ = running_loop_mgr
        state = _seed_static_state(mgr, "srv", session=MagicMock())

        async def _boom() -> None:
            raise RuntimeError("dead transport")

        with pytest.raises(RuntimeError, match="dead transport"):
            _run_on_loop(loop, mgr._static_session_op("srv", _boom()))
        assert state.in_flight == 0  # finally ran even on failure

    def test_ping_skips_while_session_op_in_flight(self, running_loop_mgr) -> None:
        """End-to-end interlock: while a ``_static_session_op`` is mid-flight the
        concurrent liveness ping skips and leaves the session intact."""
        mgr, loop, _ = running_loop_mgr
        sess = MagicMock()
        sess.send_ping = AsyncMock(side_effect=AssertionError("busy server pinged"))
        _seed_static_state(mgr, "up", session=sess)

        async def _scenario() -> None:
            release = asyncio.Event()

            async def _op() -> str:
                await release.wait()
                return "done"

            op_task = asyncio.ensure_future(mgr._static_session_op("up", _op()))
            await asyncio.sleep(0)  # let the op start and bump in_flight
            assert mgr._static_servers["up"].in_flight == 1
            due = await mgr._static_ping_one("up", time.monotonic())
            assert mgr._static_servers["up"].session is sess  # not evicted
            assert due > time.monotonic()  # rescheduled
            release.set()
            assert await op_task == "done"
            assert mgr._static_servers["up"].in_flight == 0  # decremented

        _run_on_loop(loop, _scenario())
        sess.send_ping.assert_not_awaited()

    # -- loop robustness (bounded / concurrent / fresh clock) ---------------

    def test_reconnect_one_bounded_when_connect_wedges(self, running_loop_mgr) -> None:
        """A connect that handshakes then stalls its (unbounded) discovery must
        not wedge the single loop coroutine or hold the per-name lock forever: the
        caller-side timeout bounds it, it counts as a failed attempt (backoff),
        and the ``async with`` lock is released on the cancellation."""
        mgr, loop, _ = running_loop_mgr
        mgr._STATIC_RECONNECT_ATTEMPT_TIMEOUT_S = 0.05  # shrink for a fast test
        mgr._server_configs["wedge"] = {"type": "stdio", "command": "echo"}
        _seed_static_state(mgr, "wedge", session=None)

        async def _wedge_locked(name: str, _cfg: dict[str, Any]) -> None:
            await asyncio.sleep(3600)  # handshake ok, then hangs forever

        # Patch the LOCKED body so the real ``_connect_one`` still takes the lock.
        with patch.object(mgr, "_connect_one_locked", side_effect=_wedge_locked):
            due = _run_on_loop(loop, mgr._static_reconnect_one("wedge"))
        assert mgr._static_reconnect_attempt["wedge"] == 1  # failed attempt
        assert not mgr._static_connect_lock_for("wedge").locked()  # lock released
        assert due > time.monotonic() - 1  # backoff scheduled

    def test_health_tick_processes_servers_concurrently(self, running_loop_mgr) -> None:
        """One server stalling its reconnect must not block another server's ping
        in the same tick — per-server work runs concurrently."""
        mgr, loop, _ = running_loop_mgr
        mgr._server_configs = {
            "slow": {"type": "stdio", "command": "echo"},
            "fast": {"type": "stdio", "command": "echo"},
        }
        _seed_static_state(mgr, "slow", session=None)  # disconnected → reconnect
        fast_sess = MagicMock()
        _seed_static_state(mgr, "fast", session=fast_sess)  # connected → ping

        async def _scenario() -> None:
            slow_in_connect = asyncio.Event()
            fast_pinged = asyncio.Event()

            async def _slow_connect(name: str, _cfg: dict[str, Any]) -> None:
                slow_in_connect.set()
                await asyncio.sleep(3600)  # would block the WHOLE tick if serial

            async def _fast_ping() -> None:
                fast_pinged.set()

            fast_sess.send_ping = _fast_ping
            with patch.object(mgr, "_connect_one_locked", side_effect=_slow_connect):
                tick = asyncio.ensure_future(mgr._static_health_tick())
                # Sequential-with-slow-first would never reach 'fast'; concurrency
                # means 'fast' is pinged while 'slow' is still stuck connecting.
                await asyncio.wait_for(slow_in_connect.wait(), timeout=2)
                await asyncio.wait_for(fast_pinged.wait(), timeout=2)
                tick.cancel()
                with suppress(BaseException):
                    await tick

        _run_on_loop(loop, _scenario())

    def test_health_tick_sleep_uses_fresh_clock(self, running_loop_mgr) -> None:
        """The returned sleep is computed against a FRESH clock, so a tick whose
        per-server work burned real time doesn't over-sleep by that elapsed time."""
        mgr, loop, _ = running_loop_mgr
        mgr._server_configs = {"srv": {"type": "stdio", "command": "echo"}}
        _seed_static_state(mgr, "srv", session=None)

        async def _slow_reconnect(name: str) -> float:
            await asyncio.sleep(0.3)  # burn real time during the pass
            return time.monotonic() + 1.0  # due in ~1s from now

        with patch.object(mgr, "_static_reconnect_one", side_effect=_slow_reconnect):
            sleep_s = _run_on_loop(loop, mgr._static_health_tick())
        # The reconnect returns "due ~1s from post-sleep clock", and the tick
        # subtracts its own fresh after-clock — the result should be near 1.0.
        assert 0.7 <= sleep_s <= 1.3

    def test_tick_skips_double_underscore_names(self, running_loop_mgr) -> None:
        """A ``__``-containing server can never connect (reserved delimiter); the
        tick skips it entirely rather than retrying forever + spamming log.error."""
        mgr, loop, _ = running_loop_mgr
        mgr._server_configs = {"bad__name": {"type": "stdio", "command": "echo"}}
        with (
            patch.object(mgr, "_static_reconnect_one", new=AsyncMock()) as recon,
            patch.object(mgr, "_static_ping_one", new=AsyncMock()) as ping,
        ):
            sleep_s = _run_on_loop(loop, mgr._static_health_tick())
        recon.assert_not_awaited()  # never even attempted
        ping.assert_not_awaited()
        assert sleep_s == mgr._static_health_check_s  # nothing due → full cadence

    # -- cross-path coordination (operator / dispatch) ----------------------

    def test_reconnect_sync_holds_connect_lock(self, running_loop_mgr) -> None:
        """Operator ``reconnect_sync`` must hold the per-name connect lock across
        teardown+rebuild so an autonomous health reconnect can't interleave."""
        mgr, loop, _ = running_loop_mgr
        held: list[bool] = []

        async def _connect_locked(name: str, _cfg: dict[str, Any]) -> None:
            held.append(mgr._static_connect_lock_for(name).locked())
            _seed_static_state(mgr, name, session=MagicMock())

        with (
            patch.object(mgr, "_connect_one_locked", side_effect=_connect_locked),
            patch.object(mgr, "_pre_close_streams", new=AsyncMock()),
        ):
            result = mgr.reconnect_sync("srv")
        assert result["connected"] is True
        assert held == [True]  # the lock was held while rebuilding

    def test_reconnect_sync_timeout_cleans_catalog(self, running_loop_mgr) -> None:
        """The inner ``asyncio.timeout`` in ``reconnect_sync``'s ``_reconnect``
        fires before the caller-side ``future.result(timeout=...)``, triggers the
        catalog cleanup (empty tools/resources/prompts so the merged maps don't
        advertise entries with no live session), nulls the stale session, and
        returns an error dict."""
        mgr, loop, _ = running_loop_mgr
        mgr._server_configs["srv"] = {"type": "stdio", "command": "echo"}
        mgr._STATIC_RECONNECT_ATTEMPT_TIMEOUT_S = 0.1  # fire quickly
        _seed_static_state(mgr, "srv", session=MagicMock())
        state = mgr._static_servers["srv"]
        state.tools = [{"name": "ghost_tool"}]
        state.resources = [{"uri": "ghost://resource"}]
        state.prompts = [{"name": "ghost_prompt"}]

        async def _stall(name: str, _cfg: dict[str, Any]) -> None:
            await asyncio.sleep(3600)  # never completes — timeout fires first

        with (
            patch.object(mgr, "_connect_one_locked", side_effect=_stall),
            patch.object(mgr, "_pre_close_streams", new=AsyncMock()),
        ):
            result = mgr.reconnect_sync("srv")

        assert result["connected"] is False
        assert "timed out" in result["error"].lower()
        assert result["tools"] == 0
        assert result["resources"] == 0
        assert result["prompts"] == 0
        # Stale session was nulled after the timeout
        assert mgr._static_servers["srv"].session is None
        # Lock was released after the timeout + cleanup
        assert not mgr._static_connect_lock_for("srv").locked()

    def test_cb_auto_reconnect_reuses_existing_session(self, running_loop_mgr) -> None:
        """If a session is already live (a health reconnect established it), a
        dispatch's ``_cb_auto_reconnect`` REUSES it — no second reconnect, no
        spurious breaker failure."""
        mgr, loop, _ = running_loop_mgr
        existing = MagicMock()
        _seed_static_state(mgr, "srv", session=existing)  # already up

        with (
            patch.object(mgr, "_connect_one_locked", new=AsyncMock()) as connect,
            patch.object(mgr, "_refresh_server", new=AsyncMock()),
        ):
            session = mgr._cb_auto_reconnect("srv")
        assert session is existing  # reused
        connect.assert_not_awaited()  # did NOT reconnect
        assert "srv" not in mgr._consecutive_failures  # no spurious failure

    def test_cb_auto_reconnect_reuses_session_established_under_lock(
        self, running_loop_mgr
    ) -> None:
        """A health reconnect that finishes while the dispatch waits for the lock
        is reused: ``_cb_auto_reconnect`` re-checks the session AFTER acquiring
        the lock and does not reconnect again or record a spurious failure."""
        import threading as _threading

        mgr, loop, _ = running_loop_mgr
        mgr._server_configs["srv"] = {"type": "stdio", "command": "echo"}
        _seed_static_state(mgr, "srv", session=None)  # down at the first check
        fresh = MagicMock()

        async def _acquire_hold() -> asyncio.Lock:
            lock = mgr._static_connect_lock_for("srv")
            await lock.acquire()  # stand in for an in-progress health reconnect
            return lock

        lock = asyncio.run_coroutine_threadsafe(_acquire_hold(), loop).result(timeout=5)

        result: dict[str, Any] = {}
        error: dict[str, Exception] = {}

        def _dispatch() -> None:
            try:
                result["session"] = mgr._cb_auto_reconnect("srv")
            except Exception as exc:
                error["exc"] = exc

        with (
            patch.object(mgr, "_connect_one_locked", new=AsyncMock()) as connect,
            patch.object(mgr, "_refresh_server", new=AsyncMock()),
        ):
            worker = _threading.Thread(target=_dispatch)
            worker.start()
            try:
                time.sleep(0.2)  # let the dispatch reach the lock wait

                async def _finish() -> None:
                    mgr._static_servers["srv"].session = fresh  # reconnect finished
                    lock.release()

                asyncio.run_coroutine_threadsafe(_finish(), loop).result(timeout=5)
                worker.join(timeout=5)
            finally:
                # Never leak the worker (conftest thread guard): if it is still
                # blocked, release the lock on the loop and let it drain.
                if worker.is_alive():

                    async def _emergency() -> None:
                        if lock.locked():
                            lock.release()

                    asyncio.run_coroutine_threadsafe(_emergency(), loop).result(timeout=5)
                    worker.join(timeout=5)

        assert not worker.is_alive()
        assert "exc" not in error, error.get("exc")
        assert result["session"] is fresh  # reused the under-lock session
        connect.assert_not_awaited()  # did NOT reconnect again
        assert "srv" not in mgr._consecutive_failures  # no spurious failure

    # -- tick scope + lifecycle ---------------------------------------------

    def test_tick_skips_oauth_user_servers(self, running_loop_mgr) -> None:
        mgr, loop, _ = running_loop_mgr
        mgr._server_configs["pool"] = {"type": "http", "url": "https://x/mcp"}
        mgr._oauth_user_server_names = {"pool"}
        now = time.monotonic()
        with (
            patch.object(mgr, "_static_ping_one", new=AsyncMock(return_value=now)) as ping,
            patch.object(mgr, "_static_reconnect_one", new=AsyncMock(return_value=now)) as recon,
        ):
            _run_on_loop(loop, mgr._static_health_tick())
        for call in ping.await_args_list + recon.await_args_list:
            assert call.args[0] != "pool"  # oauth_user pools are managed separately

    def test_connect_all_starts_health_task_and_disable_gates_it(self, running_loop_mgr) -> None:
        mgr, loop, _ = running_loop_mgr
        mgr._server_configs = {}
        _run_on_loop(loop, mgr._connect_all())
        assert mgr._static_health_task is not None  # started

        mgr2 = MCPClientManager({})
        mgr2._loop = loop
        mgr2._server_configs = {}
        mgr2._static_health_check_s = 0  # disabled
        _run_on_loop(loop, mgr2._connect_all())
        assert mgr2._static_health_task is None  # not started

    def test_health_loop_cancel_returns_cleanly(self, running_loop_mgr) -> None:
        mgr, loop, _ = running_loop_mgr
        mgr._server_configs = {}  # nothing to do → parks in the sleep

        task = _run_on_loop(loop, _spawn_task(mgr._static_health_loop()))

        async def _cancel() -> None:
            task.cancel()
            with suppress(BaseException):
                await task

        _run_on_loop(loop, _cancel())
        assert task.cancelled() or task.done()

    # -- unified reconnect coordination (round 3) ----------------------------

    def test_reconnect_one_defers_while_sibling_in_flight(self, running_loop_mgr) -> None:
        """The health loop DEFERS (short recheck; no backoff bump, no breaker)
        while a sibling dispatch still runs on the old evicted stack — tearing
        down now would abort that call mid-flight (review finding [0])."""
        mgr, loop, _ = running_loop_mgr
        mgr._server_configs["down"] = {"type": "stdio", "command": "echo"}
        state = _seed_static_state(mgr, "down", session=None)
        state.in_flight = 1  # a call_tool still draining on the evicted stack
        before = time.monotonic()
        with patch.object(mgr, "_connect_one_locked", new=AsyncMock()) as cm:
            due = _run_on_loop(loop, mgr._static_reconnect_one("down"))
        cm.assert_not_awaited()  # no teardown-under-dispatch
        assert before < due <= time.monotonic() + 1.5  # ~1s recheck, not backoff
        assert "down" not in mgr._static_reconnect_attempt  # not a failed attempt
        assert "down" not in mgr._consecutive_failures  # breaker untouched

    def test_reconnect_one_failure_deadline_uses_fresh_clock(self, running_loop_mgr) -> None:
        """A failed attempt's next-due is written from a FRESH clock — scheduling
        from a stale tick-start ``now`` (a slow sibling op ran first in the same
        gather) lands the deadline in the past and collapses the backoff into an
        every-tick retry storm (review finding [3])."""
        mgr, loop, _ = running_loop_mgr
        mgr._server_configs["down"] = {"type": "stdio", "command": "echo"}
        _seed_static_state(mgr, "down", session=None)

        async def _boom(name: str, _cfg: dict[str, Any]) -> None:
            raise RuntimeError("still down")

        with patch.object(mgr, "_connect_one_locked", side_effect=_boom):
            before = time.monotonic()
            due = _run_on_loop(loop, mgr._static_reconnect_one("down"))
        assert due >= before  # future-dated, not tick-start-relative
        assert mgr._static_reconnect_next["down"] == due

    def test_ping_one_deadline_uses_fresh_clock(self, running_loop_mgr) -> None:
        """The next-ping deadline is written from a FRESH clock — a stale
        tick-start ``now`` would schedule the next ping in the past and re-ping
        the server on every tick (review finding [3])."""
        mgr, loop, _ = running_loop_mgr
        sess = MagicMock()
        sess.send_ping = AsyncMock()
        _seed_static_state(mgr, "up", session=sess)
        stale_now = time.monotonic() - 45.0
        before = time.monotonic()
        due = _run_on_loop(loop, mgr._static_ping_one("up", stale_now))
        assert due >= before + mgr._static_health_check_s - 1.0
        assert mgr._static_next_ping["up"] == due

    def test_health_tick_isolates_per_server_cancelled_error(self, running_loop_mgr) -> None:
        """A CancelledError in the gather RESULTS is per-server fallout, not
        shutdown — a genuine shutdown cancels the ``await gather`` itself and
        never lands in the results list. Re-raising it killed the whole loop."""
        mgr, loop, _ = running_loop_mgr
        mgr._server_configs = {"srv": {"type": "stdio", "command": "echo"}}
        _seed_static_state(mgr, "srv", session=None)

        async def _stray(name: str) -> float:
            raise asyncio.CancelledError

        with patch.object(mgr, "_static_reconnect_one", side_effect=_stray):
            sleep_s = _run_on_loop(loop, mgr._static_health_tick())
        # Returned a sleep instead of re-raising; the server was rescheduled
        # on the normal cadence.
        assert 0.5 <= sleep_s <= mgr._static_health_check_s + 1.0

    def test_health_loop_absorbs_stray_cancel_and_stops_on_real_cancel(
        self, running_loop_mgr
    ) -> None:
        """A stray CancelledError escaping the tick (no pending cancel request
        on the task) must not kill the loop; a genuine ``task.cancel()`` still
        stops it promptly."""
        import threading as _threading

        mgr, loop, _ = running_loop_mgr
        mgr._static_health_check_s = 0.05  # instance shadow for a fast test
        survived = _threading.Event()
        calls = 0

        async def _tick() -> float:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise asyncio.CancelledError  # stray — the task was NOT cancelled
            survived.set()
            return 3600.0  # park until the genuine cancel below

        with patch.object(mgr, "_static_health_tick", side_effect=_tick):
            task = _run_on_loop(loop, _spawn_task(mgr._static_health_loop()))
            assert survived.wait(timeout=5), "loop died on a stray per-server cancel"
            assert not task.done()

            async def _cancel() -> None:
                task.cancel()
                with suppress(BaseException):
                    await task

            _run_on_loop(loop, _cancel())
        assert task.done()

    def test_dispatch_reconnect_lock_contention_records_no_breaker_failure(
        self, running_loop_mgr
    ) -> None:
        """Review finding [1]: a dispatch reconnect that times out while merely
        QUEUED on the per-name lock (held by a longer-bounded health-loop
        attempt) must not advance the breaker — the server was never proven
        unreachable. The breaker is owned by _ensure_static_connected."""
        mgr, loop, _ = running_loop_mgr
        # Instance-shadow the sync-boundary wait (now the caller-timeout constant,
        # not _CONNECT_TIMEOUT) so the held-lock contention path resolves fast.
        mgr._STATIC_RECONNECT_CALLER_TIMEOUT_S = 1.0
        _seed_static_state(mgr, "srv", session=None)

        async def _hold() -> asyncio.Lock:
            lock = mgr._static_connect_lock_for("srv")
            await lock.acquire()  # stand in for a health-loop reconnect
            return lock

        lock = asyncio.run_coroutine_threadsafe(_hold(), loop).result(timeout=5)
        try:
            with (
                patch.object(mgr, "_connect_one_locked", new=AsyncMock()) as cm,
                pytest.raises(RuntimeError, match="reconnect timed out"),
            ):
                mgr._cb_auto_reconnect("srv")
        finally:

            async def _release() -> None:
                if lock.locked():
                    lock.release()

            asyncio.run_coroutine_threadsafe(_release(), loop).result(timeout=5)
        cm.assert_not_awaited()
        assert "srv" not in mgr._consecutive_failures  # lock wait != failure

    def test_dispatch_reconnect_real_failure_records_breaker_once(self, running_loop_mgr) -> None:
        """A REAL connect failure through the dispatch path advances the breaker
        exactly ONCE — recorded inside _ensure_static_connected; the sync
        boundary must not double-record the same outcome."""
        mgr, loop, _ = running_loop_mgr
        _seed_static_state(mgr, "srv", session=None)

        async def _boom(name: str, _cfg: dict[str, Any]) -> None:
            raise ConnectionError("refused")

        with (
            patch.object(mgr, "_connect_one_locked", side_effect=_boom),
            pytest.raises(RuntimeError, match="reconnect failed"),
        ):
            mgr._cb_auto_reconnect("srv")
        assert mgr._consecutive_failures.get("srv") == 1

    def test_dispatch_reconnect_does_not_resurrect_removed_server(self, running_loop_mgr) -> None:
        """Review finding [2]: a dispatch racing remove_server_sync must not
        rebuild the server from its pre-lock cfg snapshot once the config is
        gone — _ensure_static_connected re-checks under the lock."""
        import threading as _threading

        mgr, loop, _ = running_loop_mgr
        _seed_static_state(mgr, "srv", session=None)

        async def _hold() -> asyncio.Lock:
            lock = mgr._static_connect_lock_for("srv")
            await lock.acquire()
            return lock

        lock = asyncio.run_coroutine_threadsafe(_hold(), loop).result(timeout=5)
        error: dict[str, Exception] = {}

        def _dispatch() -> None:
            try:
                mgr._cb_auto_reconnect("srv")
            except Exception as exc:
                error["exc"] = exc

        with patch.object(mgr, "_connect_one_locked", new=AsyncMock()) as cm:
            worker = _threading.Thread(target=_dispatch)
            worker.start()
            try:
                time.sleep(0.2)  # let the dispatch queue on the lock

                async def _remove_and_release() -> None:
                    mgr._server_configs.pop("srv", None)  # the remove wins the race
                    lock.release()

                asyncio.run_coroutine_threadsafe(_remove_and_release(), loop).result(timeout=5)
                worker.join(timeout=5)
            finally:
                # Never leak the worker (conftest thread guard).
                if worker.is_alive():

                    async def _emergency() -> None:
                        if lock.locked():
                            lock.release()

                    asyncio.run_coroutine_threadsafe(_emergency(), loop).result(timeout=5)
                    worker.join(timeout=5)

        assert not worker.is_alive()
        cm.assert_not_awaited()  # no rebuild from the stale cfg
        assert isinstance(error.get("exc"), RuntimeError)  # failed cleanly
        assert "unavailable" in str(error["exc"])
        assert "srv" not in mgr._consecutive_failures  # not a breaker failure


class TestEnsureStaticConnected:
    """The ONE lazy-connect primitive every autonomous driver routes through
    (health loop, dispatch _cb_auto_reconnect, _refresh_all). Operator
    reconnect_sync deliberately stays a force rebuild outside it."""

    def test_concurrent_drivers_collapse_to_single_connect(self, running_loop_mgr) -> None:
        """The reconnect STORM fix: N concurrent callers for one server queue on
        the per-name lock and collapse to a SINGLE _connect_one_locked; everyone
        else reuses the installed session (never tears it down to rebuild)."""
        mgr, loop, _ = running_loop_mgr
        _seed_static_state(mgr, "srv", session=None)
        sess = MagicMock()
        connects = 0

        async def _connect(name: str, _cfg: dict[str, Any]) -> None:
            nonlocal connects
            connects += 1
            await asyncio.sleep(0.05)  # hold the lock so siblings queue
            _seed_static_state(mgr, name, session=sess)

        async def _storm() -> list[Any]:
            cfg = mgr._server_configs["srv"]
            return await asyncio.gather(
                *(mgr._ensure_static_connected("srv", cfg) for _ in range(5))
            )

        with patch.object(mgr, "_connect_one_locked", side_effect=_connect):
            sessions = _run_on_loop(loop, _storm())
        assert connects == 1  # queued callers reused, not rebuilt
        assert all(s is sess for s in sessions)

    def test_reuses_live_session_without_teardown(self, running_loop_mgr) -> None:
        mgr, loop, _ = running_loop_mgr
        sess = MagicMock()
        _seed_static_state(mgr, "srv", session=sess)
        with patch.object(mgr, "_connect_one_locked", new=AsyncMock()) as cm:
            out = _run_on_loop(
                loop, mgr._ensure_static_connected("srv", mgr._server_configs["srv"])
            )
        assert out is sess
        cm.assert_not_awaited()

    def test_returns_none_for_removed_server(self, running_loop_mgr) -> None:
        """Config re-check under the lock: a concurrently-removed server is not
        resurrected from the caller's pre-lock cfg snapshot."""
        mgr, loop, _ = running_loop_mgr
        cfg = {"type": "stdio", "command": "echo"}  # caller's stale snapshot
        assert "gone" not in mgr._server_configs
        with patch.object(mgr, "_connect_one_locked", new=AsyncMock()) as cm:
            out = _run_on_loop(loop, mgr._ensure_static_connected("gone", cfg))
        assert out is None
        cm.assert_not_awaited()
        assert "gone" not in mgr._static_servers  # nothing rebuilt

    def test_defers_when_sibling_call_in_flight(self, running_loop_mgr) -> None:
        """session None + in_flight > 0 → defer (None) without teardown; once
        the sibling call drains, the next call reconnects."""
        mgr, loop, _ = running_loop_mgr
        state = _seed_static_state(mgr, "srv", session=None)
        state.in_flight = 1
        sess = MagicMock()

        async def _connect(name: str, _cfg: dict[str, Any]) -> None:
            _seed_static_state(mgr, name, session=sess)

        cfg = mgr._server_configs["srv"]
        with patch.object(mgr, "_connect_one_locked", side_effect=_connect) as cm:
            out = _run_on_loop(loop, mgr._ensure_static_connected("srv", cfg))
            assert out is None
            assert cm.await_count == 0  # no teardown-under-dispatch
            state.in_flight = 0  # the sibling call finished
            out2 = _run_on_loop(loop, mgr._ensure_static_connected("srv", cfg))
        assert out2 is sess
        assert cm.await_count == 1

    def test_dispatch_does_not_defer_when_busy(self, running_loop_mgr) -> None:
        """Round-4 [3]: a DISPATCH (defer_if_busy=False) reconnects even with an
        in-flight sibling — it needs the session now — rather than hard-failing a
        reachable server. The autonomous default still defers (test above)."""
        mgr, loop, _ = running_loop_mgr
        state = _seed_static_state(mgr, "srv", session=None)
        state.in_flight = 1
        sess = MagicMock()

        async def _connect(name: str, _cfg: dict[str, Any]) -> None:
            _seed_static_state(mgr, name, session=sess)

        with patch.object(mgr, "_connect_one_locked", side_effect=_connect) as cm:
            out = _run_on_loop(
                loop,
                mgr._ensure_static_connected(
                    "srv", mgr._server_configs["srv"], defer_if_busy=False
                ),
            )
        assert out is sess  # reconnected despite in_flight > 0
        assert cm.await_count == 1

    def test_cancelled_attempt_cleans_up_partial_session_and_no_breaker_record(
        self, running_loop_mgr
    ) -> None:
        """A bare CancelledError delivered mid-connect (a caller
        sync-boundary giving up, or shutdown) must NOT leave a half-discovered
        session installed but must NOT record a breaker failure — CancelledError
        proves nothing about the server."""
        mgr, loop, _ = running_loop_mgr
        state = _seed_static_state(mgr, "srv", session=None)

        async def _handshake_then_cancel(name: str, _cfg: dict[str, Any]) -> None:
            state.session = MagicMock()  # handshake OK, session installed
            raise asyncio.CancelledError()  # discovery cancelled by a caller giving up

        with (
            patch.object(mgr, "_connect_one_locked", side_effect=_handshake_then_cancel),
            # run_coroutine_threadsafe surfaces a re-raised asyncio.CancelledError
            # as concurrent.futures.CancelledError at the .result() boundary.
            pytest.raises((asyncio.CancelledError, concurrent.futures.CancelledError)),
        ):
            _run_on_loop(loop, mgr._ensure_static_connected("srv", mgr._server_configs["srv"]))
        assert mgr._static_servers["srv"].session is None  # partial session dropped
        assert "srv" not in mgr._consecutive_failures  # CancelledError is NOT a server failure

    def test_timeout_hierarchy_caller_exceeds_attempt(self) -> None:
        """The systemic round-4 fix: the caller wait must exceed the inner attempt
        bound so the inner asyncio.timeout fires first (clean TimeoutError), never
        a bare cancel escaping — and the attempt must cover a full handshake."""
        assert (
            MCPClientManager._STATIC_RECONNECT_CALLER_TIMEOUT_S
            > MCPClientManager._STATIC_RECONNECT_ATTEMPT_TIMEOUT_S
            > MCPClientManager._CONNECT_TIMEOUT
        )

    def test_failure_records_one_breaker_failure_and_raises(self, running_loop_mgr) -> None:
        mgr, loop, _ = running_loop_mgr
        _seed_static_state(mgr, "srv", session=None)

        async def _boom(name: str, _cfg: dict[str, Any]) -> None:
            raise ConnectionError("refused")

        with (
            patch.object(mgr, "_connect_one_locked", side_effect=_boom),
            pytest.raises(ConnectionError),
        ):
            _run_on_loop(loop, mgr._ensure_static_connected("srv", mgr._server_configs["srv"]))
        assert mgr._consecutive_failures.get("srv") == 1

    def test_success_clears_only_open_circuit_deadline(self, running_loop_mgr) -> None:
        """Finding-13 semantics, now owned by the primitive: success re-opens
        dispatch (deadline cleared) but keeps _consecutive_failures so a
        connect-ok / calls-fail server still escalates to a trip."""
        mgr, loop, _ = running_loop_mgr
        _seed_static_state(mgr, "srv", session=None)
        for _ in range(3):
            mgr._cb_record_failure("srv")
        assert "srv" in mgr._circuit_open_until
        sess = MagicMock()

        async def _connect(name: str, _cfg: dict[str, Any]) -> None:
            _seed_static_state(mgr, name, session=sess)

        with patch.object(mgr, "_connect_one_locked", side_effect=_connect):
            out = _run_on_loop(
                loop, mgr._ensure_static_connected("srv", mgr._server_configs["srv"])
            )
        assert out is sess
        assert "srv" not in mgr._circuit_open_until  # dispatch flows again
        assert mgr._consecutive_failures.get("srv", 0) >= 3  # count kept

    def test_refresh_all_reconnect_keeps_breaker_failure_count(self) -> None:
        """_refresh_all's reconnect branch routes through the primitive: only
        the open-circuit deadline clears — the old full _cb_record_success
        reset let a connect-ok / calls-fail server oscillate 0->1->0 below the
        breaker threshold forever."""

        async def _run() -> None:
            mgr = MCPClientManager({})
            mgr._server_configs["srv"] = {"type": "stdio", "command": "x"}
            mgr._consecutive_failures["srv"] = 2
            sess = MagicMock()

            async def _connect(name: str, _cfg: dict[str, Any]) -> None:
                _seed_static_state(mgr, name, session=sess)

            with patch.object(mgr, "_connect_one_locked", side_effect=_connect):
                results = await mgr._refresh_all("srv")

            assert results["srv"] == ([], [])  # reconnected; no tools seeded
            assert mgr._last_refresh["srv"][1] == "ok"
            assert mgr._consecutive_failures.get("srv") == 2  # NOT reset
            assert mgr._static_servers["srv"].session is sess

        asyncio.run(_run())

    def test_remove_server_sync_cancels_on_timeout_and_reports_failure(
        self, running_loop_mgr
    ) -> None:
        """Round-4 [2]: if teardown can't finish within the caller timeout (a slow
        reconnect holding the per-name lock), remove_server_sync CANCELS the
        pending _remove — so it can't later pop a re-added entry — and returns
        False rather than a false 'removed'."""
        mgr, loop, _ = running_loop_mgr
        _seed_static_state(mgr, "srv", session=MagicMock())

        async def _hang(_name: str) -> None:
            await asyncio.sleep(10)  # teardown wedged (stands in for lock contention)

        with patch.object(mgr, "_teardown_static_session", side_effect=_hang):
            result = mgr.remove_server_sync("srv", timeout=0.2)
        assert result is False  # not a false success
        assert "srv" not in mgr._server_configs  # config still popped (won't reconnect)

    def test_schedule_next_ping_sets_and_returns_fresh_deadline(self) -> None:
        """Round-4 [5]: the extracted ping-scheduling helper stores and returns
        the same fresh deadline (was a copy-pasted triplet in 3 branches)."""
        mgr = MCPClientManager({})
        mgr._static_health_check_s = 30.0
        before = time.monotonic()
        due = mgr._schedule_next_ping("srv")
        assert mgr._static_next_ping["srv"] == due
        assert before + 30.0 <= due <= time.monotonic() + 30.0


class TestTeardownStaticSession:
    """The one canonical teardown sequence (shared by _connect_one_locked's
    stale-guard and remove_server_sync)."""

    def test_teardown_order_and_state_cleared(self, running_loop_mgr) -> None:
        """Close protocol: session nulled, close event set BEFORE the first
        await (a teardown cancelled mid-flight must still have delivered the
        owner's marching orders), streams pre-closed, then the parked owner
        exits GRACEFULLY — no cancel."""
        mgr, loop, _ = running_loop_mgr
        order: list[str] = []

        async def _make_owner() -> tuple[asyncio.Event, asyncio.Task[None]]:
            ev = asyncio.Event()

            async def _parked_owner() -> None:
                await ev.wait()
                order.append("owner_exit")

            task = asyncio.create_task(_parked_owner())
            await asyncio.sleep(0)  # let the owner park
            return ev, task

        ev, owner = _run_on_loop(loop, _make_owner())

        async def _pre_close(name: str) -> None:
            order.append("pre_close")
            # Session nulled FIRST so concurrent dispatch reads see
            # "disconnected", not a corpse.
            assert mgr._static_servers["srv"].session is None
            # The close signal precedes the first await of the teardown.
            assert ev.is_set()

        _seed_static_state(mgr, "srv", session=MagicMock(), owner_task=owner, close_requested=ev)
        with patch.object(mgr, "_pre_close_streams", side_effect=_pre_close):
            _run_on_loop(loop, mgr._teardown_static_session("srv"))
        assert order == ["pre_close", "owner_exit"]
        state = mgr._static_servers["srv"]
        assert state.session is None
        assert state.owner_task is None
        assert state.close_requested is None
        assert owner.done() and not owner.cancelled()  # graceful, no escalation

    def test_teardown_escalates_to_single_cancel(self, running_loop_mgr) -> None:
        """An owner that ignores the close event gets EXACTLY one cancel — a
        second cancel is the zombie-minting mistake the protocol forbids, so
        the count is pinned, not just the final cancelled state."""
        mgr, loop, _ = running_loop_mgr
        mgr._OWNER_CLOSE_GRACE_S = 0.05  # keep the graceful window short
        cancel_calls: list[Any] = []

        async def _make_owner() -> asyncio.Task[None]:
            async def _stubborn_owner() -> None:
                await asyncio.sleep(3600)  # never watches the event

            task = asyncio.create_task(_stubborn_owner())
            await asyncio.sleep(0)
            real_cancel = task.cancel

            def _counting_cancel(*args: Any, **kwargs: Any) -> bool:
                cancel_calls.append(args)
                return real_cancel(*args, **kwargs)

            task.cancel = _counting_cancel  # type: ignore[method-assign]
            return task

        owner = _run_on_loop(loop, _make_owner())
        _seed_static_state(
            mgr, "srv", session=MagicMock(), owner_task=owner, close_requested=asyncio.Event()
        )
        _run_on_loop(loop, mgr._teardown_static_session("srv"))
        assert owner.cancelled()
        assert len(cancel_calls) == 1  # one cancel, never a second
        assert mgr._static_servers["srv"].owner_task is None

    def test_teardown_missing_server_is_noop(self, running_loop_mgr) -> None:
        mgr, loop, _ = running_loop_mgr
        _run_on_loop(loop, mgr._teardown_static_session("nope"))  # must not raise


async def _spawn_task(coro: Any) -> asyncio.Task[Any]:
    return asyncio.ensure_future(coro)
