"""Transport owner-task lifecycle + anyio cancel-scope zombie regressions.

Covers the two bugs behind the flaky-MCP-server 100%-CPU incident:

* Bug 1 — an anyio cancel scope whose host task has finished can never be
  exited; once cancelled (SDK task-group child death, or a teardown racing a
  connect) anyio re-delivers cancellation to it via ``call_soon`` every loop
  iteration, forever. The fix routes every transport cm through a long-lived
  per-server OWNER task (enter, park, exit — all in one task) with a
  one-cancel close protocol; these tests pin the protocol's behavior.
* Bug 2 — ``BaseExceptionGroup`` (BaseException-derived) escaping
  ``except Exception`` killed ``_connect_all`` before the health/sweep loops
  were created, silently disabling all autonomous recovery.

The live end-to-end flap test (real server, SIGKILL cycle) lives in
``test_mcp_live_flaky_server.py``; these are fast mock-transport unit tests.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from turnstone.core.mcp_client import MCPClientManager

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def running_loop_mgr():
    """Background-loop fixture matching the static-path test convention."""
    cfg: dict[str, Any] = {"srv": {"type": "stdio", "command": "fake-cmd"}}
    mgr = MCPClientManager(cfg)
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True, name="mcp-owner-test-loop")
    thread.start()
    mgr._loop = loop
    try:
        yield mgr, loop, thread
    finally:

        async def _drain(m: MCPClientManager) -> None:
            for attr in (
                "_user_pool_eviction_task",
                "_user_token_sweep_task",
                "_static_health_task",
            ):
                task = getattr(m, attr)
                if task is not None:
                    task.cancel()
                    with contextlib.suppress(BaseException):
                        await task
                    setattr(m, attr, None)
            for state in m._static_servers.values():
                owner = state.owner_task
                if owner is not None and not owner.done():
                    if state.close_requested is not None:
                        state.close_requested.set()
                    owner.cancel()
                    with contextlib.suppress(BaseException):
                        await owner

        with contextlib.suppress(Exception):
            asyncio.run_coroutine_threadsafe(_drain(mgr), loop).result(timeout=5)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        if not thread.is_alive():
            loop.close()


def _run(loop: asyncio.AbstractEventLoop, coro: Any, timeout: float = 5.0) -> Any:
    return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=timeout)


def _make_session_mock() -> AsyncMock:
    """A ClientSession-shaped mock good enough for connect + discovery."""
    session = AsyncMock()
    session.initialize = AsyncMock()
    session.get_server_capabilities = MagicMock(return_value=None)
    session.list_tools = AsyncMock(return_value=MagicMock(tools=[]))
    return session


def _fake_transport_and_session(mgr_module_patches: dict[str, Any]) -> dict[str, Any]:
    """Build fake stdio transport + ClientSession cms, recording enter/exit."""
    events: list[str] = []
    session = _make_session_mock()

    @asynccontextmanager
    async def fake_stdio_client(_params: Any):
        events.append("transport_enter")
        try:
            yield (AsyncMock(), AsyncMock())
        finally:
            events.append("transport_exit")

    @asynccontextmanager
    async def fake_client_session_cm():
        events.append("session_enter")
        try:
            yield session
        finally:
            events.append("session_exit")

    def fake_client_session(_read: Any, _write: Any, message_handler: Any = None):
        return fake_client_session_cm()

    mgr_module_patches["stdio_client"] = fake_stdio_client
    mgr_module_patches["ClientSession"] = fake_client_session
    return {"events": events, "session": session}


# ---------------------------------------------------------------------------
# Owner lifecycle
# ---------------------------------------------------------------------------


class TestTransportOwnerLifecycle:
    def test_connect_installs_owner_and_teardown_closes_gracefully(self, running_loop_mgr) -> None:
        mgr, loop, _ = running_loop_mgr
        patches: dict[str, Any] = {}
        fake = _fake_transport_and_session(patches)

        with (
            patch("turnstone.core.mcp_client.stdio_client", patches["stdio_client"]),
            patch("turnstone.core.mcp_client.ClientSession", patches["ClientSession"]),
        ):
            _run(loop, mgr._connect_one_locked("srv", mgr._server_configs["srv"]))
            state = mgr._static_servers["srv"]
            assert state.session is fake["session"]
            owner = state.owner_task
            assert owner is not None and not owner.done()
            assert state.close_requested is not None
            assert fake["events"] == ["transport_enter", "session_enter"]

            _run(loop, mgr._teardown_static_session("srv"))

        # Graceful close: the parked owner exits via the event — no cancel —
        # and unwinds BOTH cms in-task, inner-out.
        assert owner.done() and not owner.cancelled()
        assert fake["events"] == [
            "transport_enter",
            "session_enter",
            "session_exit",
            "transport_exit",
        ]
        assert state.session is None
        assert state.owner_task is None
        assert state.close_requested is None

    def test_owner_death_evicts_session(self, running_loop_mgr) -> None:
        """Trigger-A observer: the transport collapsing under a live session
        (owner task dies without a requested close) evicts the session so the
        health loop / next dispatch reconnects instead of probing a corpse."""
        mgr, loop, _ = running_loop_mgr
        patches: dict[str, Any] = {}
        fake = _fake_transport_and_session(patches)

        with (
            patch("turnstone.core.mcp_client.stdio_client", patches["stdio_client"]),
            patch("turnstone.core.mcp_client.ClientSession", patches["ClientSession"]),
        ):
            _run(loop, mgr._connect_one_locked("srv", mgr._server_configs["srv"]))
            state = mgr._static_servers["srv"]
            owner = state.owner_task
            assert owner is not None and state.session is fake["session"]

            # Simulate the transport task group collapsing: the owner gets a
            # stray cancellation (exactly what anyio's scope delivery does).
            loop.call_soon_threadsafe(owner.cancel)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and state.owner_task is not None:
                time.sleep(0.02)

        assert owner.done()
        assert state.session is None  # evicted by the done-callback
        assert state.owner_task is None
        # The cms were still unwound in-task despite the stray cancel.
        assert fake["events"][-2:] == ["session_exit", "transport_exit"]

    def test_base_exception_escape_resolves_waiter_and_propagates(self, running_loop_mgr) -> None:
        """A BaseException-derived escape that is neither CancelledError nor
        Exception/group (a library control-flow escape; SystemExit and
        KeyboardInterrupt take the same path but additionally stop the loop —
        asyncio semantics, unobservable in-process) is NOT swallowed — it
        propagates from the owner task — but the waiter must still be resolved
        with a transport-failure error, or the connecting caller would block
        until its outer bound (and ``_connect_all``'s initial connect has
        none)."""
        mgr, loop, _ = running_loop_mgr

        class _TransportLibraryEscape(BaseException):
            pass

        @asynccontextmanager
        async def escaping_stdio_client(_params: Any):
            raise _TransportLibraryEscape("control-flow escape")
            yield  # pragma: no cover

        async def _drive() -> tuple[BaseException | None, BaseException | None]:
            ready: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
            close_requested = asyncio.Event()
            owner = asyncio.create_task(
                mgr._static_transport_owner(
                    "srv", mgr._server_configs["srv"], ready, close_requested
                )
            )
            waiter_exc: BaseException | None = None
            try:
                await ready
            except BaseException as e:  # noqa: BLE001 - asserting the exact type below
                waiter_exc = e
            await asyncio.wait({owner}, timeout=5)
            owner_exc = owner.exception() if owner.done() and not owner.cancelled() else None
            return waiter_exc, owner_exc

        with patch("turnstone.core.mcp_client.stdio_client", escaping_stdio_client):
            waiter_exc, owner_exc = _run(loop, _drive(), timeout=10)

        assert isinstance(waiter_exc, ConnectionError)  # waiter resolved, never hung
        assert isinstance(owner_exc, _TransportLibraryEscape)  # propagated, unswallowed

    def test_connect_failure_unwinds_owner_and_raises(self, running_loop_mgr) -> None:
        mgr, loop, _ = running_loop_mgr

        @asynccontextmanager
        async def failing_stdio_client(_params: Any):
            raise ConnectionError("refused")
            yield  # pragma: no cover

        with (
            patch("turnstone.core.mcp_client.stdio_client", failing_stdio_client),
            pytest.raises(ConnectionError, match="refused"),
        ):
            _run(loop, mgr._connect_one_locked("srv", mgr._server_configs["srv"]))

        state = mgr._static_servers["srv"]
        assert state.session is None
        assert state.owner_task is None

        async def _no_owner_tasks() -> int:
            return sum(
                1
                for t in asyncio.all_tasks()
                if t.get_name().startswith("mcp-transport-owner:") and not t.done()
            )

        assert _run(loop, _no_owner_tasks()) == 0

    def test_caller_cancel_mid_connect_does_not_abandon_cms(self, running_loop_mgr) -> None:
        """Bug-1 core regression: cancelling the CONNECTING caller (attempt
        timeout, shutdown, sync boundary giving up) must close the owner via
        the one-cancel protocol — the transport cm still exits, in-task."""
        mgr, loop, _ = running_loop_mgr
        events: list[str] = []
        entered = asyncio.Event()

        @asynccontextmanager
        async def hanging_stdio_client(_params: Any):
            events.append("transport_enter")
            try:
                entered.set()
                await asyncio.sleep(3600)  # server accepted, then stalled
                yield (AsyncMock(), AsyncMock())
            finally:
                events.append("transport_exit")

        async def _drive() -> None:
            connect = asyncio.create_task(
                mgr._connect_one_locked("srv", mgr._server_configs["srv"])
            )
            await asyncio.wait_for(entered.wait(), timeout=5)
            connect.cancel()  # the attempt-timeout / shutdown shape
            with contextlib.suppress(asyncio.CancelledError):
                await connect
            # The owner must be closed (one cancel) and fully unwound.
            deadline = asyncio.get_running_loop().time() + 5
            while asyncio.get_running_loop().time() < deadline:
                owners = [
                    t
                    for t in asyncio.all_tasks()
                    if t.get_name().startswith("mcp-transport-owner:") and not t.done()
                ]
                if not owners:
                    return
                await asyncio.sleep(0.02)
            raise AssertionError("owner task still alive after caller cancel")

        with patch("turnstone.core.mcp_client.stdio_client", hanging_stdio_client):
            _run(loop, _drive(), timeout=15)

        assert events == ["transport_enter", "transport_exit"]
        assert mgr._static_servers["srv"].session is None


# ---------------------------------------------------------------------------
# Bug 2: BaseExceptionGroup vs except Exception
# ---------------------------------------------------------------------------


class TestBaseExceptionGroupHardening:
    def test_connect_all_survives_group_and_starts_loops(self, running_loop_mgr) -> None:
        """A transport failure wrapped in BaseExceptionGroup (e.g. an
        accept-then-RST server collapsing the SDK task group with a stray
        CancelledError inside) must not kill ``_connect_all`` before the
        health/sweep loops are started — that silently disabled ALL
        autonomous recovery."""
        mgr, loop, _ = running_loop_mgr
        # Pin the loop cadences: the assertions below require both loops to be
        # ENABLED, independent of whatever mcp config the environment carries.
        mgr._user_token_sweep_s = 240.0
        mgr._static_health_check_s = 30.0

        async def _exploding_connect(name: str, _cfg: dict[str, Any]) -> None:
            raise BaseExceptionGroup("transport collapsed", [asyncio.CancelledError()])

        with patch.object(mgr, "_connect_one", side_effect=_exploding_connect):
            _run(loop, mgr._connect_all())

        assert mgr._connected.is_set()
        assert "srv" in mgr._last_error
        health = mgr._static_health_task
        sweep = mgr._user_token_sweep_task
        assert health is not None and not health.done()
        assert sweep is not None and not sweep.done()

    def test_health_loop_survives_group(self, running_loop_mgr) -> None:
        mgr, loop, _ = running_loop_mgr
        ticks: list[int] = []

        async def _tick_then_group() -> float:
            ticks.append(1)
            if len(ticks) == 1:
                raise BaseExceptionGroup("boom", [asyncio.CancelledError()])
            return 3600.0

        mgr._static_health_check_s = 0.05  # quick recovery sleep after the group
        with patch.object(mgr, "_static_health_tick", side_effect=_tick_then_group):

            async def _drive() -> asyncio.Task[None]:
                task = asyncio.create_task(mgr._static_health_loop())
                deadline = asyncio.get_running_loop().time() + 5
                while asyncio.get_running_loop().time() < deadline and len(ticks) < 2:
                    await asyncio.sleep(0.02)
                assert len(ticks) >= 2, "loop died on BaseExceptionGroup"
                assert not task.done()
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                return task

            _run(loop, _drive(), timeout=10)


# ---------------------------------------------------------------------------
# Orphaned-scope disarm backstop
# ---------------------------------------------------------------------------


class TestScopeDisarmBackstop:
    def test_disarms_exactly_the_all_done_scope_on_this_loop(self, running_loop_mgr) -> None:
        """One sweep over three armed scopes must touch EXACTLY the true
        orphan: the all-done-tasks scope hosted on the mcp-loop. The
        live-task scope (its task may still drain the scope) and the
        hostless scope (loop unknown — not ours to reach into) stay armed.
        Asserting ``disarmed == 1`` discriminates both failure directions:
        a no-op sweep and an over-eager one."""
        mgr, loop, _ = running_loop_mgr

        async def _arm_and_sweep() -> dict[str, Any]:
            from anyio._backends._asyncio import CancelScope

            this_loop = asyncio.get_running_loop()

            async def _noop() -> None:
                return None

            blocker = asyncio.Event()

            async def _parked() -> None:
                await blocker.wait()

            done_task = asyncio.create_task(_noop())
            await done_task
            live_task = asyncio.create_task(_parked())
            await asyncio.sleep(0)

            orphan = CancelScope()
            orphan._host_task = done_task
            orphan._tasks.add(done_task)
            orphan._cancel_handle = this_loop.call_soon(lambda: None)

            live_scope = CancelScope()
            live_scope._host_task = live_task
            live_scope._tasks.add(live_task)
            live_scope._cancel_handle = this_loop.call_soon(lambda: None)

            hostless = CancelScope()
            hostless._tasks.add(done_task)
            hostless._cancel_handle = this_loop.call_soon(lambda: None)

            mgr._last_scope_disarm = 0.0
            disarmed = mgr._maybe_disarm_orphaned_scopes("unit test")
            results = {
                "disarmed": disarmed,
                "orphan_handle_cleared": orphan._cancel_handle is None,
                "orphan_tasks_cleared": len(orphan._tasks) == 0,
                "live_still_armed": live_scope._cancel_handle is not None,
                "live_task_kept": live_task in live_scope._tasks,
                "hostless_still_armed": hostless._cancel_handle is not None,
                "rate_limited_second": mgr._maybe_disarm_orphaned_scopes("again"),
            }
            for scope in (live_scope, hostless):
                if scope._cancel_handle is not None:
                    scope._cancel_handle.cancel()
                    scope._cancel_handle = None
                scope._tasks.clear()
            blocker.set()
            await live_task
            return results

        r = _run(loop, _arm_and_sweep())
        assert r["disarmed"] == 1
        assert r["orphan_handle_cleared"] and r["orphan_tasks_cleared"]
        assert r["live_still_armed"] and r["live_task_kept"]
        assert r["hostless_still_armed"]
        assert r["rate_limited_second"] == 0
