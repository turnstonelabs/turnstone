"""MCP client: static header references resolve at connect; stored text stays in memory."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from turnstone.core.mcp_client import (
    MCPClientManager,
    MCPSecretReferenceError,
    _db_servers_to_config,
    _pool_cfg_from_row,
)
from turnstone.core.secret_refs import SecretResolver, reset_for_tests
from turnstone.core.secret_refs import _resolver as resolver_module
from turnstone.core.secret_refs._config import parse_secrets_config
from turnstone.core.secret_refs._errors import SecretBackendError


class FakeBackend:
    name = "fake"

    def __init__(self) -> None:
        self.values: dict[str, str] = {"/mcp/token": "Bearer live-token"}
        self.fail: SecretBackendError | None = None

    def fetch(self, path: str, key: str | None) -> str:
        if self.fail is not None:
            raise self.fail
        if path not in self.values:
            raise SecretBackendError(f"{path.strip('/')!r} not found", retryable=False)
        return self.values[path]

    def close(self) -> None:
        pass


class _FakeStorage:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def list_mcp_servers(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        return list(self._rows)


@pytest.fixture
def fake_store(monkeypatch: pytest.MonkeyPatch) -> Any:
    backend = FakeBackend()
    resolver = SecretResolver(parse_secrets_config({}), backends={"fake": backend})
    monkeypatch.setattr(resolver_module, "_instance", resolver)
    yield backend
    reset_for_tests()


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
        assert cfg["headers"] == HEADERS
        assert resolved is not cfg

    def test_cfg_without_references_is_returned_as_is(self, fake_store: FakeBackend) -> None:
        mgr = MCPClientManager({})
        cfg = {"type": "stdio", "command": "echo"}
        assert asyncio.run(mgr._with_resolved_headers("srv", cfg)) is cfg
        cfg2 = {"type": "streamable-http", "url": "u", "headers": {"X": "y"}}
        assert asyncio.run(mgr._with_resolved_headers("srv", cfg2)) is cfg2

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
    def test_reconcile_invalidates_the_cache(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import turnstone.core.secret_refs as pkg

        calls: list[str] = []
        monkeypatch.setattr(pkg, "invalidate_cache", lambda: calls.append("invalidate"))
        mgr = MCPClientManager({})
        mgr.reconcile_sync(_FakeStorage([]))
        assert calls == ["invalidate"]
