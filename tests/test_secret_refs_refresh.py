"""The secret refresh timer: when hosts start it, its interval, and how it runs and stops."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import threading
import time
from typing import TYPE_CHECKING, Any

from tests._secret_refs_helpers import install_fake_resolver
from turnstone.core.secret_refs import SecretResolver, start_refresh
from turnstone.core.secret_refs import _refresh as refresh_module
from turnstone.core.secret_refs import _resolver as resolver_module
from turnstone.core.secret_refs._config import parse_secrets_config

if TYPE_CHECKING:
    import pytest


def test_no_store_starts_no_timer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(resolver_module, "_instance", SecretResolver(parse_secrets_config({})))
    assert start_refresh(lambda: None, what="test") is None


def test_the_interval_is_the_cache_ttl_with_a_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[float] = []

    async def record(refresh: Any, *, interval: float, what: str) -> None:
        seen.append(interval)

    monkeypatch.setattr(refresh_module, "refresh_periodically", record)

    async def start(ttl: float) -> None:
        install_fake_resolver(monkeypatch, {}, ttl=ttl)
        task = start_refresh(lambda: None, what="test")
        assert task is not None
        await task

    asyncio.run(start(600.0))
    asyncio.run(start(5.0))
    assert seen == [600.0, refresh_module.MIN_REFRESH_SECONDS]


def test_runs_until_cancelled_and_survives_a_failing_run() -> None:
    calls: list[int] = []

    def refresh() -> None:
        calls.append(len(calls))
        if len(calls) == 1:
            raise RuntimeError("store down")

    async def run() -> None:
        task = asyncio.create_task(
            refresh_module.refresh_periodically(refresh, interval=0.01, what="test")
        )
        while len(calls) < 3:
            await asyncio.sleep(0.01)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(asyncio.wait_for(run(), timeout=5))
    assert calls[:3] == [0, 1, 2]


def test_cancelling_waits_for_the_run_in_flight() -> None:
    events: list[str] = []
    started = threading.Event()

    def refresh() -> None:
        started.set()
        time.sleep(0.2)
        events.append("run finished")

    async def run() -> None:
        task = asyncio.create_task(
            refresh_module.refresh_periodically(refresh, interval=0.01, what="test")
        )
        await asyncio.to_thread(started.wait, 5)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        events.append("cancel returned")

    asyncio.run(asyncio.wait_for(run(), timeout=5))
    assert events == ["run finished", "cancel returned"]


def test_every_host_starts_the_timer_before_serving_and_stops_it_after() -> None:
    """A host that never starts it picks up a rotated key only on Sync or restart."""
    from turnstone import server
    from turnstone.console import server as console_server

    for source, call in (
        (inspect.getsource(server._lifespan), "_refresh_models_from_store(app.state)"),
        (
            inspect.getsource(console_server._lifespan),
            "_refresh_coord_registry(app.state, storage, from_timer=True)",
        ),
    ):
        started = source.index("start_refresh(")
        serving = source.index("\n    yield\n")
        stopped = source.index("_secret_refresh_task.cancel()")
        assert started < serving < stopped
        assert call in source
