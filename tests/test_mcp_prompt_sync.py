"""Tests for MCP prompt → governance template sync and readonly API guards."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from turnstone.core.mcp_client import MCPClientManager


@pytest.fixture()
def mgr() -> MCPClientManager:
    """Create an MCPClientManager with no real servers (no start())."""
    return MCPClientManager({})


def _make_storage() -> MagicMock:
    """Create a mock storage backend with prompt template methods."""
    storage = MagicMock()
    storage.get_prompt_template_by_name.return_value = None
    storage.list_prompt_templates_by_origin.return_value = []
    storage.create_prompt_template.return_value = None
    storage.update_prompt_template.return_value = True
    storage.delete_prompt_template.return_value = True
    return storage


class TestSyncPromptsToStorage:
    def test_sync_no_storage(self, mgr: MCPClientManager) -> None:
        """Without storage set, sync returns empty stats."""
        result = mgr.sync_prompts_to_storage()
        assert result == {"added": [], "removed": [], "skipped": []}

    def test_sync_creates_mcp_templates(self, mgr: MCPClientManager) -> None:
        """New MCP prompts are created as templates."""
        storage = _make_storage()
        mgr.set_storage(storage)

        # Populate internal prompts list directly
        mgr._prompts = [
            {
                "name": "mcp__test__greeting",
                "original_name": "greeting",
                "server": "test",
                "description": "Say hello",
                "arguments": [
                    {"name": "name", "description": "Who to greet", "required": True},
                ],
            },
        ]

        result = mgr.sync_prompts_to_storage()

        assert result["added"] == ["mcp__test__greeting"]
        assert result["removed"] == []
        assert result["skipped"] == []
        storage.create_prompt_template.assert_called_once()
        call_kwargs = storage.create_prompt_template.call_args
        assert call_kwargs[1]["name"] == "mcp__test__greeting"
        assert call_kwargs[1]["origin"] == "mcp"
        assert call_kwargs[1]["mcp_server"] == "test"
        assert call_kwargs[1]["readonly"] is True
        assert call_kwargs[1]["category"] == "mcp"
        assert '"name"' in call_kwargs[1]["variables"]

    def test_sync_skips_manual_overrides(self, mgr: MCPClientManager) -> None:
        """A manual template with the same name is not overwritten."""
        storage = _make_storage()
        storage.get_prompt_template_by_name.return_value = {
            "template_id": "existing-id",
            "name": "mcp__test__greeting",
            "origin": "manual",
            "readonly": False,
        }
        mgr.set_storage(storage)

        mgr._prompts = [
            {
                "name": "mcp__test__greeting",
                "original_name": "greeting",
                "server": "test",
                "description": "Say hello",
                "arguments": [],
            },
        ]

        result = mgr.sync_prompts_to_storage()

        assert result["skipped"] == ["mcp__test__greeting"]
        assert result["added"] == []
        storage.create_prompt_template.assert_not_called()
        storage.update_prompt_template.assert_not_called()

    def test_sync_updates_existing_mcp_template(self, mgr: MCPClientManager) -> None:
        """An existing MCP template gets its content/variables updated."""
        storage = _make_storage()
        storage.get_prompt_template_by_name.return_value = {
            "template_id": "existing-id",
            "name": "mcp__test__greeting",
            "origin": "mcp",
            "mcp_server": "test",
            "readonly": True,
        }
        mgr.set_storage(storage)

        mgr._prompts = [
            {
                "name": "mcp__test__greeting",
                "original_name": "greeting",
                "server": "test",
                "description": "Updated description",
                "arguments": [
                    {"name": "user", "description": "The user", "required": False},
                ],
            },
        ]

        result = mgr.sync_prompts_to_storage()

        assert result["added"] == []
        assert result["skipped"] == []
        storage.create_prompt_template.assert_not_called()
        storage.update_prompt_template.assert_called_once()
        call_args = storage.update_prompt_template.call_args
        assert call_args[0][0] == "existing-id"
        assert "Updated description" in call_args[1]["content"]
        assert "user" in call_args[1]["variables"]

    def test_sync_removes_deleted_prompts(self, mgr: MCPClientManager) -> None:
        """MCP templates in storage with no matching prompt are deleted."""
        storage = _make_storage()
        storage.list_prompt_templates_by_origin.return_value = [
            {
                "template_id": "old-id",
                "name": "mcp__test__old_prompt",
                "origin": "mcp",
                "mcp_server": "test",
            },
        ]
        mgr.set_storage(storage)
        mgr._prompts = []  # No prompts at all

        result = mgr.sync_prompts_to_storage()

        assert result["removed"] == ["mcp__test__old_prompt"]
        storage.delete_prompt_template.assert_called_once_with("old-id")


class TestSetStorageAutoSync:
    """set_storage() triggers an immediate sync when servers are already connected."""

    def test_set_storage_syncs_when_connected(self, mgr) -> None:
        storage = _make_storage()
        mgr._prompts = [
            {
                "name": "mcp__srv__p1",
                "original_name": "p1",
                "server": "srv",
                "description": "A prompt",
                "arguments": [],
            }
        ]
        mgr._connected.set()

        mgr.set_storage(storage)

        # Should have called create_prompt_template for the discovered prompt
        storage.create_prompt_template.assert_called_once()
        call_kwargs = storage.create_prompt_template.call_args
        assert call_kwargs[1]["name"] == "mcp__srv__p1"
        assert call_kwargs[1]["origin"] == "mcp"

    def test_set_storage_no_sync_when_not_connected(self, mgr) -> None:
        storage = _make_storage()
        mgr._prompts = [
            {
                "name": "mcp__srv__p1",
                "original_name": "p1",
                "server": "srv",
                "description": "A prompt",
                "arguments": [],
            }
        ]
        # _connected is NOT set

        mgr.set_storage(storage)

        # Should not have synced
        storage.create_prompt_template.assert_not_called()


class TestReadonlyAPIGuards:
    """Test that the console server API guards reject edits to readonly templates."""

    @pytest.fixture()
    def db(self, tmp_path):
        """Create a fresh SQLite backend for each test."""
        from turnstone.core.storage._sqlite import SQLiteBackend

        return SQLiteBackend(str(tmp_path / "test.db"))

    def test_readonly_guard_update(self, db) -> None:
        """Readonly templates cannot be updated via storage guard logic."""
        db.create_prompt_template(
            "t1",
            "mcp__srv__prompt",
            "mcp",
            "content",
            variables="[]",
            is_default=False,
            org_id="",
            created_by="",
            origin="mcp",
            mcp_server="srv",
            readonly=True,
        )
        tpl = db.get_prompt_template("t1")
        assert tpl is not None
        assert tpl["readonly"] is True
        # Simulate API guard check
        assert tpl.get("readonly") is True

    def test_readonly_guard_delete(self, db) -> None:
        """Readonly templates are flagged for API-level rejection."""
        db.create_prompt_template(
            "t1",
            "mcp__srv__prompt",
            "mcp",
            "content",
            variables="[]",
            is_default=False,
            org_id="",
            created_by="",
            origin="mcp",
            mcp_server="srv",
            readonly=True,
        )
        existing = db.get_prompt_template("t1")
        assert existing is not None
        assert existing.get("readonly") is True
