"""MCP client: static header references resolve at connect; stored text stays in memory."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import TYPE_CHECKING, Any

import pytest

from tests._secret_refs_helpers import FakeBackend, install_fake_resolver
from tests.conftest import stop_loop_thread
from turnstone.core.mcp_client import (
    ConnectFailure,
    MCPClientManager,
    MCPSecretReferenceError,
    _db_servers_to_config,
    _pool_cfg_from_row,
    classify_connect_failure,
)
from turnstone.core.secret_refs._errors import SecretBackendError

if TYPE_CHECKING:
    from collections.abc import Iterator


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

    def test_a_refused_reference_is_recorded_in_its_own_words(
        self, fake_store: FakeBackend
    ) -> None:
        mgr = MCPClientManager({})
        cfg = {
            "type": "streamable-http",
            "url": "u",
            "headers": {"Authorization": "secret://fake/nope"},
        }
        with pytest.raises(MCPSecretReferenceError) as info:
            asyncio.run(mgr._with_resolved_headers("srv", cfg))
        failure = classify_connect_failure(info.value)
        # Not the type-and-message fallback for an exception the classifier does not know.
        assert failure == ConnectFailure(
            "error", "MCP server 'srv': secret://fake/nope: 'nope' not found"
        )
        assert failure.recognized


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


class TestHealthLoopRetry:
    """The static health loop's next attempt after a header reference fails to resolve."""

    def _reconnect_delay(self, fake_store: FakeBackend, failure: SecretBackendError) -> float:
        fake_store.fail = failure
        mgr = MCPClientManager(
            {
                "srv": {
                    "type": "streamable-http",
                    "url": "https://mcp.example.com/mcp",
                    "headers": {"Authorization": "secret://fake/mcp/token"},
                }
            }
        )

        async def reachable(key: str | tuple[str, str], url: str) -> None:
            return None

        mgr._tcp_probe = reachable  # type: ignore[method-assign]

        async def attempt() -> float:
            return await mgr._static_reconnect_one("srv") - time.monotonic()

        return asyncio.run(attempt())

    def test_a_refused_secret_waits_for_the_slow_retry(self, fake_store: FakeBackend) -> None:
        denied = SecretBackendError("permission denied", retryable=False)
        delay = self._reconnect_delay(fake_store, denied)
        assert delay > MCPClientManager._STATIC_RECONNECT_MAX_S
        assert fake_store.calls == 1

    def test_an_unreachable_store_keeps_the_reconnect_backoff(
        self, fake_store: FakeBackend
    ) -> None:
        outage = SecretBackendError("store unreachable", retryable=True)
        delay = self._reconnect_delay(fake_store, outage)
        assert delay <= MCPClientManager._STATIC_RECONNECT_MAX_S


STATIC_CFG = {
    "type": "streamable-http",
    "url": "https://mcp.example.com/mcp",
    "headers": {"Authorization": "secret://fake/mcp/token", "X-Plain": "v"},
}


def _capture_owner(mgr: MCPClientManager, seen: list[dict[str, Any]]) -> None:
    """Stand in for the transport owner: record the config it is handed, then fail the connect."""

    async def reachable(key: str | tuple[str, str], url: str) -> None:
        return None

    async def owner(
        name: str, cfg: dict[str, Any], ready: asyncio.Future[Any], close: asyncio.Event
    ) -> None:
        seen.append(cfg)
        ready.set_exception(ConnectionError("transport stubbed out"))

    mgr._tcp_probe = reachable  # type: ignore[method-assign]
    mgr._static_transport_owner = owner  # type: ignore[method-assign]


@pytest.fixture
def loop_mgr() -> Iterator[MCPClientManager]:
    """A manager whose loop runs on a background thread, as ``reconnect_sync`` needs."""
    mgr = MCPClientManager({})
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True, name="secret-refs-mcp-loop")
    thread.start()
    mgr._loop = loop
    try:
        yield mgr
    finally:
        stop_loop_thread(loop, thread)


class TestConnectPaths:
    """The real connect paths hand the transport resolved headers and keep the stored text."""

    def test_static_connect_resolves_header_references(self, fake_store: FakeBackend) -> None:
        mgr = MCPClientManager({"srv": json.loads(json.dumps(STATIC_CFG))})
        seen: list[dict[str, Any]] = []
        _capture_owner(mgr, seen)
        with pytest.raises(ConnectionError):
            asyncio.run(mgr._connect_one("srv"))
        assert seen[0]["headers"] == {"Authorization": "Bearer live-token", "X-Plain": "v"}
        assert mgr._server_configs["srv"]["headers"] == STATIC_CFG["headers"]

    def test_pool_connect_resolves_header_references(self, fake_store: FakeBackend) -> None:
        from unittest.mock import patch

        mgr = MCPClientManager({})
        observed: dict[str, str] = {}

        def stream_factory(_url: str = "", **kwargs: Any) -> Any:
            observed.update(kwargs["http_client"].headers)
            raise ConnectionError("transport stubbed out")

        async def reachable(key: str | tuple[str, str], url: str) -> None:
            return None

        mgr._tcp_probe = reachable  # type: ignore[method-assign]
        cfg = {
            "type": "streamable-http",
            "url": "https://mcp.example.com/mcp",
            "headers": {"X-Api-Key": "secret://fake/mcp/token"},
        }
        with (
            patch("turnstone.core.mcp_client.streamable_http_client", side_effect=stream_factory),
            pytest.raises(Exception),  # noqa: B017 - only the headers the transport saw matter
        ):
            asyncio.run(mgr._connect_one_pool(("user-1", "srv"), cfg, "user-token"))
        assert observed["x-api-key"] == "Bearer live-token"
        assert observed["authorization"] == "Bearer user-token"
        assert cfg["headers"] == {"X-Api-Key": "secret://fake/mcp/token"}

    def test_reconnect_asks_the_store_again(
        self, fake_store: FakeBackend, loop_mgr: MCPClientManager
    ) -> None:
        mgr = loop_mgr
        mgr._server_configs["srv"] = json.loads(json.dumps(STATIC_CFG))
        seen: list[dict[str, Any]] = []
        _capture_owner(mgr, seen)
        mgr.reconnect_sync("srv")
        fake_store.values["/mcp/token"] = "Bearer rotated"  # inside the cache TTL
        mgr.reconnect_sync("srv")
        assert seen[0]["headers"]["Authorization"] == "Bearer live-token"
        assert seen[1]["headers"]["Authorization"] == "Bearer rotated"
