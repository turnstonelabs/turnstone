"""Concurrent node MCP reloads must share one manager and serialize reconciliation."""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from starlette.applications import Starlette
from starlette.routing import Route

from turnstone import server
from turnstone.core import mcp_client
from turnstone.core.mcp_client import MCPClientManager

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from turnstone.core.storage._sqlite import SQLiteBackend


class _ObservedReloadLock:
    """Expose contention while retaining real thread-lock acquisition and release."""

    def __init__(self, lock: threading.Lock) -> None:
        self.contended = threading.Event()
        self._lock = lock

    def __enter__(self) -> None:
        if not self._lock.acquire(blocking=False):
            self.contended.set()
            assert self._lock.acquire(timeout=5), "MCP reload lock was not released"

    def __exit__(self, *exc: Any) -> None:
        self._lock.release()


_Node = tuple[Starlette, _ObservedReloadLock, list[MCPClientManager]]


@pytest.fixture
def node(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    sqlite_backend_factory: Callable[[str], SQLiteBackend],
) -> Iterator[_Node]:
    storage = sqlite_backend_factory(str(tmp_path / "reload.db"))
    app = Starlette(
        routes=[Route("/v1/api/_internal/mcp-reload", server.internal_mcp_reload, methods=["POST"])]
    )
    app.state.auth_storage = storage
    app.state.mcp_client = None
    app.state.mcp_ref = [None]
    lock = _ObservedReloadLock(getattr(server, "_MCP_RELOAD_LOCK", threading.Lock()))
    # The absent-lock baseline can still reach the competing startup/reconcile,
    # letting the tests fail on duplicate managers or overlapping reconciliation.
    monkeypatch.setattr(server, "_MCP_RELOAD_LOCK", lock, raising=False)
    monkeypatch.setattr("turnstone.core.storage._registry.get_storage", lambda: storage)
    monkeypatch.setattr(mcp_client, "load_config", lambda _section: {"user_token_sweep_seconds": 0})

    managers: list[MCPClientManager] = []
    threads: list[threading.Thread] = []
    real_start = MCPClientManager.start

    def track_start(manager: MCPClientManager) -> None:
        managers.append(manager)
        real_start(manager)
        assert manager._thread is not None
        threads.append(manager._thread)

    monkeypatch.setattr(MCPClientManager, "start", track_start)
    try:
        yield app, lock, managers
    finally:
        for manager in managers:
            if manager._thread is not None:
                manager.shutdown()
        assert all(not thread.is_alive() for thread in threads)


def _reload_pair(app: Starlette, *, concurrent: bool = True) -> list[httpx.Response]:
    async def run() -> list[httpx.Response]:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://node.example.com",
        ) as client:
            if concurrent:
                return list(
                    await asyncio.gather(
                        client.post("/v1/api/_internal/mcp-reload"),
                        client.post("/v1/api/_internal/mcp-reload"),
                    )
                )
            return [await client.post("/v1/api/_internal/mcp-reload") for _ in range(2)]

    return asyncio.run(run())


def test_concurrent_reload_constructs_one_manager(
    node: _Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, lock, managers = node
    tracked_start = MCPClientManager.start
    count_lock = threading.Lock()
    starts = 0

    def pause_start(manager: MCPClientManager) -> None:
        nonlocal starts
        tracked_start(manager)
        with count_lock:
            starts += 1
            first = starts == 1
        if first:
            assert lock.contended.wait(timeout=5), "second reload never reached startup"
        else:
            # Without serialization the second request starts another manager.
            lock.contended.set()

    monkeypatch.setattr(MCPClientManager, "start", pause_start)
    responses = _reload_pair(app)

    assert [response.status_code for response in responses] == [200, 200]
    assert len(managers) == 1
    manager = managers[0]
    assert app.state.mcp_client is manager
    assert app.state.mcp_ref[0] is manager
    assert manager._storage is app.state.auth_storage
    assert manager._app_state is app.state
    thread = manager._thread
    assert thread is not None and thread.is_alive()
    app.state.mcp_client.shutdown()
    assert not thread.is_alive()


def test_concurrent_reload_serializes_existing_manager_reconciliation(
    node: _Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, lock, managers = node
    manager = MCPClientManager({})
    manager.start()
    manager.set_storage(app.state.auth_storage)
    manager.set_app_state(app.state)
    app.state.mcp_client = manager
    app.state.mcp_ref[0] = manager
    real_reconcile = MCPClientManager.reconcile_sync
    count_lock = threading.Lock()
    calls = active = peak = 0

    def pause_reconcile(manager: MCPClientManager, storage: Any) -> dict[str, Any]:
        nonlocal calls, active, peak
        with count_lock:
            calls += 1
            active += 1
            peak = max(peak, active)
            first = calls == 1
        try:
            if first:
                assert lock.contended.wait(timeout=5), "second reload never reached reconciliation"
            else:
                lock.contended.set()
            return real_reconcile(manager, storage)
        finally:
            with count_lock:
                active -= 1

    monkeypatch.setattr(MCPClientManager, "reconcile_sync", pause_reconcile)
    responses = _reload_pair(app)

    assert [response.status_code for response in responses] == [200, 200]
    assert calls == 2
    assert peak == 1
    assert managers == [manager]
    assert app.state.mcp_client is manager
    assert app.state.mcp_ref[0] is manager


def test_reconcile_exception_releases_reload_lock(
    node: _Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _lock, managers = node
    real_reconcile = MCPClientManager.reconcile_sync
    calls = 0

    def fail_once(manager: MCPClientManager, storage: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("reconciliation failed")
        return real_reconcile(manager, storage)

    monkeypatch.setattr(MCPClientManager, "reconcile_sync", fail_once)
    responses = _reload_pair(app, concurrent=False)

    assert [response.status_code for response in responses] == [500, 200]
    assert responses[1].json() == {"status": "ok", "added": [], "removed": [], "updated": []}
    assert calls == 2
    assert len(managers) == 1
    assert app.state.mcp_client is managers[0]
    assert app.state.mcp_ref[0] is managers[0]
