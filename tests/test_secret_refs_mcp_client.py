"""MCP client: static header references resolve at connect; stored text stays in memory."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from tests._secret_refs_helpers import FakeBackend, install_fake_resolver
from turnstone.core.mcp_client import (
    MCPClientManager,
    MCPSecretReferenceError,
    _db_servers_to_config,
    _pool_cfg_from_row,
)


class _FakeStorage:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def list_mcp_servers(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        return list(self._rows)


@pytest.fixture
def fake_store(monkeypatch: pytest.MonkeyPatch) -> FakeBackend:
    return install_fake_resolver(monkeypatch, {"/mcp/token": "Bearer live-token"})


HEADERS = {"Authorization": "secret://fake/mcp/token", "X-Plain": "v"}


class TestStoredFormStaysInMemory:
    def test_db_rows_keep_the_reference_text(self) -> None:
        row = {
            "name": "srv",
            "transport": "streamable-http",
            "url": "https://mcp.example.com/mcp",
            "headers": json.dumps(HEADERS),
            "env": "{}",
            "auth_type": "static",
        }
        assert _db_servers_to_config([row])["srv"]["headers"] == HEADERS
        assert _pool_cfg_from_row(row)["headers"] == HEADERS


class TestResolvedCopyAtConnect:
    def test_resolves_a_copy_and_leaves_the_stored_cfg_alone(self, fake_store: FakeBackend) -> None:
        mgr = MCPClientManager({})
        cfg = {
            "type": "streamable-http",
            "url": "https://mcp.example.com/mcp",
            "headers": dict(HEADERS),
        }
        resolved = asyncio.run(mgr._with_resolved_headers("srv", cfg))
        assert resolved["headers"] == {"Authorization": "Bearer live-token", "X-Plain": "v"}
        assert cfg["headers"] == HEADERS  # the stored config keeps the reference text

    def test_cfg_without_references_is_unchanged(self, fake_store: FakeBackend) -> None:
        mgr = MCPClientManager({})
        cfg = {"type": "stdio", "command": "echo"}
        assert asyncio.run(mgr._with_resolved_headers("srv", cfg)) == cfg
        cfg2 = {"type": "streamable-http", "url": "u", "headers": {"X": "y"}}
        assert asyncio.run(mgr._with_resolved_headers("srv", cfg2)) == cfg2
        assert fake_store.calls == 0

    def test_failure_names_the_reference_not_the_value(self, fake_store: FakeBackend) -> None:
        mgr = MCPClientManager({})
        cfg = {
            "type": "streamable-http",
            "url": "u",
            "headers": {"Authorization": "secret://fake/nope"},
        }
        with pytest.raises(MCPSecretReferenceError) as info:
            asyncio.run(mgr._with_resolved_headers("srv", cfg))
        message = str(info.value)
        assert "MCP server 'srv'" in message and "secret://fake/nope" in message
        assert "live-token" not in message


class TestOperatorTriggeredReloadsAskTheStoreAgain:
    def test_reconcile_picks_up_a_rotated_header_value(self, fake_store: FakeBackend) -> None:
        mgr = MCPClientManager({})
        cfg = {"transport": "streamable-http", "url": "http://x", "headers": dict(HEADERS)}
        assert asyncio.run(mgr._with_resolved_headers("srv", cfg))["headers"]["Authorization"] == (
            "Bearer live-token"
        )
        fake_store.values["/mcp/token"] = "Bearer rotated"
        # Inside the cache TTL the old value is still served...
        assert asyncio.run(mgr._with_resolved_headers("srv", cfg))["headers"]["Authorization"] == (
            "Bearer live-token"
        )
        # ...until an operator-triggered reconcile asks the store again.
        mgr.reconcile_sync(_FakeStorage([]))
        assert asyncio.run(mgr._with_resolved_headers("srv", cfg))["headers"]["Authorization"] == (
            "Bearer rotated"
        )
