"""Tests for MCP server storage CRUD operations."""

from __future__ import annotations

import uuid

import pytest

from turnstone.core.storage._sqlite import SQLiteBackend


@pytest.fixture
def db(tmp_path):
    """Fresh SQLite backend for each test."""
    return SQLiteBackend(str(tmp_path / "test.db"))


def _make_id() -> str:
    return uuid.uuid4().hex


class TestMcpServerStorage:
    def test_create_and_get(self, db: SQLiteBackend) -> None:
        sid = _make_id()
        db.create_mcp_server(
            server_id=sid,
            name="test-server",
            transport="stdio",
            command="echo",
            args='["hello"]',
        )
        s = db.get_mcp_server(sid)
        assert s is not None
        assert s["name"] == "test-server"
        assert s["transport"] == "stdio"
        assert s["command"] == "echo"
        assert s["args"] == '["hello"]'
        assert s["enabled"] is True
        assert s["auto_approve"] is False

    def test_get_by_name(self, db: SQLiteBackend) -> None:
        sid = _make_id()
        db.create_mcp_server(server_id=sid, name="named-srv", transport="stdio")
        s = db.get_mcp_server_by_name("named-srv")
        assert s is not None
        assert s["server_id"] == sid

    def test_get_by_name_not_found(self, db: SQLiteBackend) -> None:
        assert db.get_mcp_server_by_name("nope") is None

    def test_get_not_found(self, db: SQLiteBackend) -> None:
        assert db.get_mcp_server("nonexistent") is None

    def test_list_empty(self, db: SQLiteBackend) -> None:
        assert db.list_mcp_servers() == []

    def test_list_all(self, db: SQLiteBackend) -> None:
        db.create_mcp_server(server_id=_make_id(), name="alpha", transport="stdio")
        db.create_mcp_server(
            server_id=_make_id(), name="beta", transport="streamable-http", url="http://x"
        )
        servers = db.list_mcp_servers()
        assert len(servers) == 2
        assert servers[0]["name"] == "alpha"  # ordered by name
        assert servers[1]["name"] == "beta"

    def test_list_enabled_only(self, db: SQLiteBackend) -> None:
        sid1 = _make_id()
        sid2 = _make_id()
        db.create_mcp_server(server_id=sid1, name="enabled-srv", transport="stdio", enabled=True)
        db.create_mcp_server(server_id=sid2, name="disabled-srv", transport="stdio", enabled=False)
        enabled = db.list_mcp_servers(enabled_only=True)
        assert len(enabled) == 1
        assert enabled[0]["name"] == "enabled-srv"

    def test_update_basic_fields(self, db: SQLiteBackend) -> None:
        sid = _make_id()
        db.create_mcp_server(server_id=sid, name="orig", transport="stdio", command="echo")
        ok = db.update_mcp_server(sid, name="renamed", command="cat")
        assert ok is True
        s = db.get_mcp_server(sid)
        assert s is not None
        assert s["name"] == "renamed"
        assert s["command"] == "cat"

    def test_update_boolean_conversion(self, db: SQLiteBackend) -> None:
        sid = _make_id()
        db.create_mcp_server(server_id=sid, name="booltest", transport="stdio")
        db.update_mcp_server(sid, auto_approve=True, enabled=False)
        s = db.get_mcp_server(sid)
        assert s is not None
        assert s["auto_approve"] is True
        assert s["enabled"] is False

    def test_update_not_found(self, db: SQLiteBackend) -> None:
        ok = db.update_mcp_server("nonexistent", name="x")
        assert ok is False

    def test_update_ignores_disallowed_fields(self, db: SQLiteBackend) -> None:
        sid = _make_id()
        db.create_mcp_server(server_id=sid, name="guard", transport="stdio", created_by="admin")
        original = db.get_mcp_server(sid)
        assert original is not None
        original_created = original["created"]
        # created_by and created are not in the mutable allowlist
        db.update_mcp_server(sid, created_by="evil", created="2000-01-01T00:00:00")
        s = db.get_mcp_server(sid)
        assert s is not None
        assert s["created_by"] == "admin"  # unchanged
        assert s["created"] == original_created  # unchanged

    def test_delete(self, db: SQLiteBackend) -> None:
        sid = _make_id()
        db.create_mcp_server(server_id=sid, name="delme", transport="stdio")
        ok = db.delete_mcp_server(sid)
        assert ok is True
        assert db.get_mcp_server(sid) is None

    def test_delete_not_found(self, db: SQLiteBackend) -> None:
        ok = db.delete_mcp_server("nonexistent")
        assert ok is False

    def test_create_duplicate_name(self, db: SQLiteBackend) -> None:
        db.create_mcp_server(server_id=_make_id(), name="unique", transport="stdio")
        # Second create with same name but different ID should be no-op (OR IGNORE)
        sid2 = _make_id()
        db.create_mcp_server(server_id=sid2, name="unique", transport="stdio")
        # OR IGNORE silently drops the conflicting insert
        assert db.get_mcp_server(sid2) is None

    def test_create_idempotent_same_id(self, db: SQLiteBackend) -> None:
        sid = _make_id()
        db.create_mcp_server(server_id=sid, name="idem", transport="stdio", command="v1")
        db.create_mcp_server(server_id=sid, name="idem", transport="stdio", command="v2")
        s = db.get_mcp_server(sid)
        assert s is not None
        assert s["command"] == "v1"  # original preserved, second ignored

    def test_http_transport_fields(self, db: SQLiteBackend) -> None:
        sid = _make_id()
        db.create_mcp_server(
            server_id=sid,
            name="http-srv",
            transport="streamable-http",
            url="https://example.com/mcp",
            headers='{"Authorization":"Bearer xyz"}',
        )
        s = db.get_mcp_server(sid)
        assert s is not None
        assert s["transport"] == "streamable-http"
        assert s["url"] == "https://example.com/mcp"
        assert "Authorization" in s["headers"]

    # -- Registry columns -------------------------------------------------------

    def test_create_with_registry_columns(self, db: SQLiteBackend) -> None:
        sid = _make_id()
        db.create_mcp_server(
            server_id=sid,
            name="reg-server",
            transport="streamable-http",
            url="https://example.com/mcp",
            registry_name="io.example/mcp-server",
            registry_version="1.0.0",
            registry_meta='{"description":"test"}',
        )
        s = db.get_mcp_server(sid)
        assert s is not None
        assert s["registry_name"] == "io.example/mcp-server"
        assert s["registry_version"] == "1.0.0"
        assert s["registry_meta"] == '{"description":"test"}'

    def test_create_without_registry_columns(self, db: SQLiteBackend) -> None:
        """Non-registry servers should have None/empty defaults."""
        sid = _make_id()
        db.create_mcp_server(server_id=sid, name="plain", transport="stdio")
        s = db.get_mcp_server(sid)
        assert s is not None
        assert s["registry_name"] is None
        assert s["registry_version"] == ""
        assert s["registry_meta"] == "{}"

    def test_get_by_registry_name(self, db: SQLiteBackend) -> None:
        sid = _make_id()
        db.create_mcp_server(
            server_id=sid,
            name="reg-test",
            transport="stdio",
            registry_name="io.example/test",
        )
        s = db.get_mcp_server_by_registry_name("io.example/test")
        assert s is not None
        assert s["server_id"] == sid

    def test_get_by_registry_name_not_found(self, db: SQLiteBackend) -> None:
        assert db.get_mcp_server_by_registry_name("nonexistent") is None

    def test_null_registry_name_no_conflict(self, db: SQLiteBackend) -> None:
        """Multiple servers with NULL registry_name should coexist."""
        db.create_mcp_server(server_id=_make_id(), name="a", transport="stdio")
        db.create_mcp_server(server_id=_make_id(), name="b", transport="stdio")
        servers = db.list_mcp_servers()
        assert len(servers) == 2

    def test_update_registry_columns(self, db: SQLiteBackend) -> None:
        sid = _make_id()
        db.create_mcp_server(
            server_id=sid,
            name="upgradable",
            transport="stdio",
            registry_name="io.example/up",
            registry_version="1.0.0",
        )
        ok = db.update_mcp_server(sid, registry_version="2.0.0")
        assert ok is True
        s = db.get_mcp_server(sid)
        assert s is not None
        assert s["registry_version"] == "2.0.0"
