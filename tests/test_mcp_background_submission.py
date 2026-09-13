"""MCP submissions must tolerate shutdown before scheduling."""

from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import threading
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests._proc_helpers import poll_until
from turnstone.core import mcp_client

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Iterator

_BODIES = {
    "prime_user_pools": "_prime_user_pools",
    "schedule_prime_user_server": "_prime_user_server_logged",
    "evict_user_session": "_drop_catalog_locked",
}
_PRIMES = {"prime_user_pools", "schedule_prime_user_server"}


class _SubmissionProbe:
    """Observe real submissions, optionally pausing before argument evaluation.

    Pausing attribute lookup allows shutdown to clear the manager's loop between
    the admission guard and submission. The scheduler itself is never stubbed.
    Other threads, including the one running shutdown, use asyncio unchanged.
    """

    def __init__(self) -> None:
        self.caller = threading.current_thread()
        self.before_submit: Callable[[], None] | None = None
        self.coroutines: list[Coroutine[Any, Any, Any]] = []
        self.futures: list[concurrent.futures.Future[Any]] = []

    def __getattr__(self, name: str) -> Any:
        if name == "run_coroutine_threadsafe" and threading.current_thread() is self.caller:
            if self.before_submit is not None:
                self.before_submit()
            return self.submit
        return getattr(asyncio, name)

    def submit(
        self, coro: Coroutine[Any, Any, Any], loop: asyncio.AbstractEventLoop
    ) -> concurrent.futures.Future[Any]:
        self.coroutines.append(coro)
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        self.futures.append(future)
        return future


@pytest.fixture
def manager(monkeypatch: pytest.MonkeyPatch) -> Iterator[mcp_client.MCPClientManager]:
    monkeypatch.setattr(mcp_client, "load_config", lambda *_: {})
    manager = mcp_client.MCPClientManager({})
    manager._user_token_sweep_s = 0
    manager._static_health_check_s = 0
    manager._oauth_user_server_names = {"sample"}
    manager._app_state = SimpleNamespace()
    manager._storage = MagicMock()
    for name in (*_BODIES.values(), "_ensure_static_connected"):
        monkeypatch.setattr(manager, name, AsyncMock())
    monkeypatch.setattr(manager, "_cb_record_failure", MagicMock())
    try:
        yield manager
    finally:
        manager.shutdown()


@pytest.fixture
def probe(monkeypatch: pytest.MonkeyPatch) -> Iterator[_SubmissionProbe]:
    probe = _SubmissionProbe()
    monkeypatch.setattr(mcp_client, "asyncio", probe)
    try:
        yield probe
    finally:
        # Clean rejected allocations even when a negative control fails an
        # assertion, so its warning cannot bleed into another test.
        for coro in probe.coroutines:
            if inspect.getcoroutinestate(coro) == inspect.CORO_CREATED:
                coro.close()


def _invoke(manager: mcp_client.MCPClientManager, bridge: str) -> object:
    # Observe the runtime return even though these APIs are annotated as void.
    method: Callable[..., object] = getattr(manager, bridge)
    if bridge == "_cb_auto_reconnect":
        return method("sample")
    if bridge == "prime_user_pools":
        return method("user-1")
    if bridge == "schedule_prime_user_server":
        return method(
            user_id="user-1",
            server_name="sample",
            access_token="synthetic-token",
            server_row={"transport": "streamable-http", "url": "https://mcp.example.com"},
        )
    return method("user-1", "sample")


def _assert_no_body_ran(manager: mcp_client.MCPClientManager) -> None:
    for name in (*_BODIES.values(), "_ensure_static_connected"):
        getattr(manager, name).assert_not_awaited()
    cast("MagicMock", manager._cb_record_failure).assert_not_called()
    assert manager._storage.mock_calls == []


@pytest.mark.parametrize("bridge", _BODIES)
def test_no_loop_does_not_allocate_or_run(
    manager: mcp_client.MCPClientManager, probe: _SubmissionProbe, bridge: str
) -> None:
    assert _invoke(manager, bridge) is None
    assert probe.coroutines == []
    for name in _BODIES.values():
        getattr(manager, name).assert_not_called()
    _assert_no_body_ran(manager)


@pytest.mark.parametrize("bridge", [*_BODIES, "_cb_auto_reconnect"])
def test_closed_loop_closes_rejected_coroutine(
    manager: mcp_client.MCPClientManager, probe: _SubmissionProbe, bridge: str
) -> None:
    loop = asyncio.new_event_loop()
    loop.close()
    manager._loop = loop
    manager._server_configs["sample"] = {"url": "https://mcp.example.com"}
    try:
        if bridge == "_cb_auto_reconnect":
            with pytest.raises(RuntimeError, match="MCP server 'sample' reconnect failed:"):
                _invoke(manager, bridge)
        else:
            assert _invoke(manager, bridge) is None
        if bridge in _PRIMES:
            assert probe.coroutines == []
            getattr(manager, _BODIES[bridge]).assert_not_called()
        else:
            assert len(probe.coroutines) == 1
            assert inspect.getcoroutinestate(probe.coroutines[0]) == inspect.CORO_CLOSED
        assert probe.futures == []
        _assert_no_body_ran(manager)
    finally:
        manager._loop = None


@pytest.mark.parametrize("bridge", _BODIES)
def test_successful_submission_runs_on_manager_loop(
    manager: mcp_client.MCPClientManager, probe: _SubmissionProbe, bridge: str
) -> None:
    manager.start()
    probe.coroutines.clear()
    probe.futures.clear()
    ran = threading.Event()
    loops = []

    async def body(*args: Any) -> None:
        loops.append(asyncio.get_running_loop())
        ran.set()

    getattr(manager, _BODIES[bridge]).side_effect = body
    assert _invoke(manager, bridge) is None
    assert ran.wait(5)
    if bridge in _PRIMES:
        assert probe.futures == []
    else:
        assert len(probe.futures) == 1
        probe.futures[0].result(timeout=5)
    getattr(manager, _BODIES[bridge]).assert_awaited_once()
    assert loops == [manager._loop]


@pytest.mark.parametrize("bridge", [*sorted(_BODIES.keys() - _PRIMES), "_cb_auto_reconnect"])
def test_shutdown_between_admission_and_submission(
    manager: mcp_client.MCPClientManager, probe: _SubmissionProbe, bridge: str
) -> None:
    manager.start()
    manager._server_configs["sample"] = {"url": "https://mcp.example.com"}
    loop, loop_thread = manager._loop, manager._thread
    assert loop is not None and loop_thread is not None
    probe.coroutines.clear()
    probe.futures.clear()
    entered, release = threading.Event(), threading.Event()
    result: concurrent.futures.Future[object] = concurrent.futures.Future()

    def pause() -> None:
        entered.set()
        assert release.wait(5), "shutdown did not release the submitting thread"

    def invoke() -> None:
        try:
            result.set_result(_invoke(manager, bridge))
        except BaseException as exc:
            result.set_exception(exc)

    caller = threading.Thread(target=invoke, name="mcp-submission-test")
    probe.caller = caller
    probe.before_submit = pause
    caller.start()
    try:
        assert entered.wait(5), "caller did not reach submission"
        manager.shutdown()
        assert manager._loop is None
        assert loop.is_closed()
        assert not loop_thread.is_alive()
    finally:
        release.set()
        caller.join(timeout=5)
        assert not caller.is_alive()

    if bridge == "_cb_auto_reconnect":
        with pytest.raises(RuntimeError, match="MCP server 'sample' reconnect failed:"):
            result.result(timeout=5)
    else:
        assert result.result(timeout=5) is None
    assert len(probe.coroutines) == 1
    assert inspect.getcoroutinestate(probe.coroutines[0]) == inspect.CORO_CLOSED
    assert probe.futures == []
    _assert_no_body_ran(manager)


@pytest.mark.parametrize("bridge", sorted(_PRIMES))
def test_shutdown_rejects_queued_and_late_primes(
    manager: mcp_client.MCPClientManager, bridge: str
) -> None:
    manager.start()
    loop = manager._loop
    assert loop is not None
    parked, release = threading.Event(), threading.Event()

    def park_loop() -> None:
        parked.set()
        assert release.wait(5)

    loop.call_soon_threadsafe(park_loop)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        try:
            assert parked.wait(5)
            _invoke(manager, bridge)  # accepted callback, not executed yet
            shutdown = executor.submit(manager.shutdown)
            assert poll_until(lambda: not manager._accepting_primes)
            _invoke(manager, bridge)  # loop still exists, admission is closed
        finally:
            release.set()
        shutdown.result(timeout=5)
    getattr(manager, _BODIES[bridge]).assert_not_called()
    assert loop.is_closed()


@pytest.mark.parametrize("bridge", sorted(_PRIMES))
def test_shutdown_drains_accepted_prime_before_loop_close(
    manager: mcp_client.MCPClientManager, bridge: str
) -> None:
    manager.start()
    loop = manager._loop
    assert loop is not None
    entered, cancelled = threading.Event(), threading.Event()
    release = asyncio.Event()

    async def body(*args: Any) -> None:
        entered.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()
            await release.wait()

    getattr(manager, _BODIES[bridge]).side_effect = body
    _invoke(manager, bridge)
    assert entered.wait(5)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        shutdown = executor.submit(manager.shutdown)
        try:
            assert cancelled.wait(5)
            assert not shutdown.done()
            assert not loop.is_closed()
            _invoke(manager, bridge)
        finally:
            loop.call_soon_threadsafe(release.set)
        shutdown.result(timeout=5)
    getattr(manager, _BODIES[bridge]).assert_awaited_once()
    assert loop.is_closed()
    assert not asyncio.all_tasks(loop)
