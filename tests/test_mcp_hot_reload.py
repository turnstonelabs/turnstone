"""Tests for MCPClientManager hot-reload methods."""

from __future__ import annotations

from typing import Any

from turnstone.core.mcp_client import MCPClientManager

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _fake_resource_dict(
    uri: str = "file:///README.md",
    name: str = "readme",
    server: str = "test",
) -> dict[str, Any]:
    """Create a fake resource dict as stored in per-server state."""
    return {
        "uri": uri,
        "name": name,
        "description": "A resource",
        "mimeType": "text/plain",
        "server": server,
    }


def _fake_prompt_dict(
    name: str = "mcp__test__code_review",
    original_name: str = "code_review",
    server: str = "test",
) -> dict[str, Any]:
    """Create a fake prompt dict as stored in per-server state."""
    return {
        "name": name,
        "original_name": original_name,
        "server": server,
        "description": "Generate a code review",
        "arguments": [
            {"name": "language", "description": "Programming language", "required": True}
        ],
    }


# ---------------------------------------------------------------------------
# add_server_sync
# ---------------------------------------------------------------------------


class TestAddServerSync:
    def test_rejects_double_underscore_name(self) -> None:
        """Names containing __ should be rejected."""
        mgr = MCPClientManager({})
        result = mgr.add_server_sync("bad__name", {"command": "echo"})
        assert result["connected"] is False
        assert "__" in result["error"]
        assert result["tools"] == 0
        assert result["resources"] == 0
        assert result["prompts"] == 0

    def test_fails_without_event_loop(self) -> None:
        """Adding a server without starting the event loop should fail gracefully."""
        mgr = MCPClientManager({})
        result = mgr.add_server_sync("test", {"command": "echo"})
        assert result["connected"] is False
        assert "loop" in result["error"].lower()

    def test_config_removed_on_failure(self) -> None:
        """add_server_sync removes the config entry when connection fails."""
        mgr = MCPClientManager({})
        mgr.add_server_sync("new-srv", {"command": "echo"})
        # Since the loop isn't running, it fails and config is cleaned up
        assert "new-srv" not in mgr._server_configs


# ---------------------------------------------------------------------------
# remove_server_sync
# ---------------------------------------------------------------------------


class TestRemoveServerSync:
    def test_returns_false_for_nonexistent(self) -> None:
        """Removing a non-connected server returns False."""
        mgr = MCPClientManager({})
        assert mgr.remove_server_sync("nonexistent") is False

    def test_cleans_up_per_server_state(self) -> None:
        """remove_server_sync cleans up all per-server state dicts."""
        mgr = MCPClientManager({"test": {"command": "echo"}})
        # Simulate state as if the server was connected
        mgr._per_server_tools["test"] = [_fake_openai_tool()]
        mgr._per_server_resources["test"] = [_fake_resource_dict()]
        mgr._per_server_prompts["test"] = [_fake_prompt_dict()]
        mgr._supports_list_changed["test"] = True
        mgr._supports_resources["test"] = True
        mgr._supports_resource_list_changed["test"] = True
        mgr._supports_prompts["test"] = True
        mgr._supports_prompt_list_changed["test"] = True
        mgr._rebuild_tools()
        mgr._rebuild_resources()
        mgr._rebuild_prompts()

        # Verify preconditions
        assert len(mgr.get_tools()) == 1
        assert mgr.resource_count == 1
        assert mgr.prompt_count == 1

        mgr.remove_server_sync("test")

        assert len(mgr.get_tools()) == 0
        assert mgr.resource_count == 0
        assert mgr.prompt_count == 0
        assert "test" not in mgr._per_server_tools
        assert "test" not in mgr._per_server_resources
        assert "test" not in mgr._per_server_prompts
        assert "test" not in mgr._supports_list_changed
        assert "test" not in mgr._supports_resources
        assert "test" not in mgr._supports_resource_list_changed
        assert "test" not in mgr._supports_prompts
        assert "test" not in mgr._supports_prompt_list_changed

    def test_removes_config_to_prevent_reconnect(self) -> None:
        """remove_server_sync removes from _server_configs to prevent reconnect."""
        mgr = MCPClientManager({"test": {"command": "echo"}})
        assert "test" in mgr._server_configs
        mgr.remove_server_sync("test")
        assert "test" not in mgr._server_configs

    def test_preserves_other_servers(self) -> None:
        """Removing one server does not affect another server's state."""
        mgr = MCPClientManager({"srv_a": {}, "srv_b": {}})
        mgr._per_server_tools["srv_a"] = [_fake_openai_tool("mcp__srv_a__foo")]
        mgr._per_server_tools["srv_b"] = [_fake_openai_tool("mcp__srv_b__bar")]
        mgr._rebuild_tools()

        assert len(mgr.get_tools()) == 2

        mgr.remove_server_sync("srv_a")

        assert len(mgr.get_tools()) == 1
        assert mgr.get_tools()[0]["function"]["name"] == "mcp__srv_b__bar"
        assert "srv_b" in mgr._server_configs


# ---------------------------------------------------------------------------
# get_server_status
# ---------------------------------------------------------------------------


class TestGetServerStatus:
    def test_disconnected_server_in_config(self) -> None:
        """Status of a configured but not connected server shows disconnected."""
        mgr = MCPClientManager({"test": {"command": "echo"}})
        status = mgr.get_server_status("test")
        assert status["connected"] is False
        assert status["tools"] == 0
        assert status["resources"] == 0
        assert status["prompts"] == 0
        assert status["error"] == ""

    def test_connected_server_with_tools(self) -> None:
        """Status of a connected server reports correct tool/resource/prompt counts."""
        mgr = MCPClientManager({"test": {}})
        # Simulate connected state
        mgr._sessions["test"] = object()  # any truthy value
        mgr._per_server_tools["test"] = [
            _fake_openai_tool("mcp__test__a"),
            _fake_openai_tool("mcp__test__b"),
        ]
        mgr._per_server_resources["test"] = [_fake_resource_dict()]
        mgr._per_server_prompts["test"] = [_fake_prompt_dict()]

        status = mgr.get_server_status("test")
        assert status["connected"] is True
        assert status["tools"] == 2
        assert status["resources"] == 1
        assert status["prompts"] == 1

    def test_unknown_server(self) -> None:
        """Status of a server not in config or sessions shows disconnected."""
        mgr = MCPClientManager({})
        status = mgr.get_server_status("unknown")
        assert status["connected"] is False
        assert status["tools"] == 0


# ---------------------------------------------------------------------------
# get_all_server_status
# ---------------------------------------------------------------------------


class TestGetAllServerStatus:
    def test_empty_manager(self) -> None:
        """Empty manager returns empty status dict."""
        mgr = MCPClientManager({})
        assert mgr.get_all_server_status() == {}

    def test_multiple_servers(self) -> None:
        """Manager with configs but no connections returns status for each."""
        mgr = MCPClientManager({"alpha": {}, "bravo": {}})
        statuses = mgr.get_all_server_status()
        assert len(statuses) == 2
        assert "alpha" in statuses
        assert "bravo" in statuses
        assert statuses["alpha"]["connected"] is False
        assert statuses["bravo"]["connected"] is False

    def test_mixed_connected_and_disconnected(self) -> None:
        """Status correctly reflects a mix of connected and disconnected servers."""
        mgr = MCPClientManager({"up": {}, "down": {}})
        mgr._sessions["up"] = object()
        mgr._per_server_tools["up"] = [_fake_openai_tool("mcp__up__x")]

        statuses = mgr.get_all_server_status()
        assert statuses["up"]["connected"] is True
        assert statuses["up"]["tools"] == 1
        assert statuses["down"]["connected"] is False
        assert statuses["down"]["tools"] == 0


# ---------------------------------------------------------------------------
# Error tracking (_last_error)
# ---------------------------------------------------------------------------


class TestErrorTracking:
    def test_get_server_status_returns_error(self) -> None:
        """Error stored in _last_error flows through get_server_status."""
        mgr = MCPClientManager({"test": {"command": "echo"}})
        mgr._last_error["test"] = "Connection refused"
        status = mgr.get_server_status("test")
        assert status["error"] == "Connection refused"
        assert status["connected"] is False

    def test_no_error_by_default(self) -> None:
        """Default error is empty string."""
        mgr = MCPClientManager({"test": {"command": "echo"}})
        status = mgr.get_server_status("test")
        assert status["error"] == ""

    def test_error_cleared_after_pop(self) -> None:
        """Clearing _last_error makes get_server_status return empty."""
        mgr = MCPClientManager({"test": {"command": "echo"}})
        mgr._last_error["test"] = "Connection refused"
        mgr._last_error.pop("test", None)
        status = mgr.get_server_status("test")
        assert status["error"] == ""

    def test_error_cleared_on_remove(self) -> None:
        """remove_server_sync cleans up _last_error entry."""
        mgr = MCPClientManager({"test": {"command": "echo"}})
        mgr._last_error["test"] = "Connection refused"
        mgr.remove_server_sync("test")
        assert "test" not in mgr._last_error

    def test_all_server_status_includes_errors(self) -> None:
        """get_all_server_status propagates per-server errors."""
        mgr = MCPClientManager({"alpha": {}, "bravo": {}})
        mgr._last_error["alpha"] = "Timeout"
        statuses = mgr.get_all_server_status()
        assert statuses["alpha"]["error"] == "Timeout"
        assert statuses["bravo"]["error"] == ""

    def test_error_does_not_leak_across_servers(self) -> None:
        """Error on one server does not affect another."""
        mgr = MCPClientManager({"a": {}, "b": {}})
        mgr._last_error["a"] = "Failed"
        assert mgr.get_server_status("b")["error"] == ""


# ---------------------------------------------------------------------------
# reconcile_sync
# ---------------------------------------------------------------------------


class _FakeStorage:
    """Minimal mock storage for reconcile tests."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def list_mcp_servers(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        if enabled_only:
            return [r for r in self._rows if r.get("enabled", True)]
        return list(self._rows)


def _db_row(
    name: str,
    transport: str = "stdio",
    command: str = "echo",
    args: str = "[]",
    url: str = "",
    headers: str = "{}",
    env: str = "{}",
    enabled: bool = True,
) -> dict[str, Any]:
    return {
        "name": name,
        "transport": transport,
        "command": command,
        "args": args,
        "url": url,
        "headers": headers,
        "env": env,
        "enabled": enabled,
    }


class TestReconcileSync:
    def test_adds_new_servers(self) -> None:
        mgr = MCPClientManager({})
        storage = _FakeStorage([_db_row("new-srv")])
        # Can't actually connect (no loop), but config should be attempted
        result = mgr.reconcile_sync(storage)
        # add_server_sync fails without a loop, but the method shouldn't crash
        assert "new-srv" not in result["added"]  # fails gracefully
        assert result["removed"] == []
        assert result["updated"] == []

    def test_removes_stale_db_servers(self) -> None:
        mgr = MCPClientManager({"old-srv": {"command": "echo"}})
        mgr._db_managed.add("old-srv")  # mark as DB-managed
        storage = _FakeStorage([])  # DB is empty
        result = mgr.reconcile_sync(storage)
        assert "old-srv" in result["removed"]
        assert "old-srv" not in mgr._server_configs

    def test_preserves_config_file_servers(self) -> None:
        """Config-file servers (not in _db_managed) survive reconcile."""
        mgr = MCPClientManager({"env-srv": {"command": "echo"}})
        # NOT in _db_managed — loaded from MCP_CONFIG env
        storage = _FakeStorage([])  # DB is empty
        result = mgr.reconcile_sync(storage)
        assert result["removed"] == []
        assert "env-srv" in mgr._server_configs  # still there

    def test_config_server_not_overwritten_by_db_name_collision(self) -> None:
        """DB server with same name as config-file server does not replace it."""
        original_cfg = {"type": "stdio", "command": "config-echo", "args": [], "env": {}}
        mgr = MCPClientManager({"shared-name": dict(original_cfg)})
        # NOT in _db_managed — this is a config-file server
        # DB has a server with the same name but different config
        storage = _FakeStorage([_db_row("shared-name", command="db-echo")])
        result = mgr.reconcile_sync(storage)
        # Config-file server should NOT be updated
        assert result["updated"] == []
        assert "shared-name" in mgr._server_configs
        assert mgr._server_configs["shared-name"]["command"] == "config-echo"

    def test_updates_changed_config(self) -> None:
        original_cfg = {"type": "stdio", "command": "echo", "args": [], "env": {}}
        mgr = MCPClientManager({"srv": dict(original_cfg)})
        mgr._db_managed.add("srv")  # mark as DB-managed
        # DB has updated command — config differs
        storage = _FakeStorage([_db_row("srv", command="cat")])
        result = mgr.reconcile_sync(storage)
        # remove_server_sync ran (old config cleared), add_server_sync attempted
        # but fails without a running event loop — that's expected in unit tests.
        # The key assertion: the old config was evicted (not left stale).
        assert "srv" not in mgr._server_configs
        # Not in "removed" (that's for servers absent from DB)
        assert "srv" not in result["removed"]

    def test_no_change_is_noop(self) -> None:
        cfg = {"type": "stdio", "command": "echo", "args": [], "env": {}}
        mgr = MCPClientManager({"srv": dict(cfg)})
        storage = _FakeStorage([_db_row("srv", command="echo")])
        result = mgr.reconcile_sync(storage)
        assert result["added"] == []
        assert result["removed"] == []
        assert result["updated"] == []
        # Config unchanged
        assert "srv" in mgr._server_configs

    def test_storage_failure_graceful(self) -> None:
        mgr = MCPClientManager({"srv": {}})

        class _BrokenStorage:
            def list_mcp_servers(self, **kw: Any) -> list[dict[str, Any]]:
                raise RuntimeError("DB down")

        result = mgr.reconcile_sync(_BrokenStorage())
        assert result == {"added": [], "removed": [], "updated": []}
        # Existing server untouched
        assert "srv" in mgr._server_configs
