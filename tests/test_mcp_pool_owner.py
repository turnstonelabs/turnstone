"""Pool transport owner-task lifecycle + anyio cancel-scope regressions.

The pool (auth_type=oauth_user) sibling of ``test_mcp_transport_owner.py``.
Each ``(user, server)`` pool entry's transport + ``ClientSession`` cms are now
entered, parked, and exited by ONE long-lived owner task
(``_pool_transport_owner``) with a one-cancel close protocol, so a cancel scope
whose host task has finished can never be left re-delivering cancellation in a
``call_soon`` loop (the SDK #2147 100%-CPU spin). These fast mock-transport
tests pin that protocol for the pool path; the real-server integration coverage
lives in ``test_mcp_pool_auth_integration.py``.
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

from turnstone.core.mcp_client import MCPClientManager, PoolEntryState, _AuthCapture

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def running_loop_mgr():
    """Background-loop fixture matching the pool-path test convention.

    Teardown drains the eviction / sweep / health tasks AND any parked pool
    transport owner a successful connect left installed — the conftest fails
    leaked threads and an undrained owner is destroyed pending at GC.
    """
    cfg: dict[str, Any] = {}
    mgr = MCPClientManager(cfg)
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True, name="mcp-pool-owner-test-loop")
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
                    await asyncio.gather(task, return_exceptions=True)
                    setattr(m, attr, None)
            for entry in list(m._user_pool_entries.values()):
                owner = entry.owner_task
                if owner is not None and not owner.done():
                    if entry.close_requested is not None:
                        entry.close_requested.set()
                    owner.cancel()
                    await asyncio.gather(owner, return_exceptions=True)

        with contextlib.suppress(Exception):
            asyncio.run_coroutine_threadsafe(_drain(mgr), loop).result(timeout=5)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        if not thread.is_alive():
            loop.close()


def _run(loop: asyncio.AbstractEventLoop, coro: Any, timeout: float = 5.0) -> Any:
    return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=timeout)


def _http_cfg() -> dict[str, Any]:
    return {"type": "streamable-http", "url": "https://mcp.example.com/mcp", "headers": {}}


def _make_pool_session_mock() -> AsyncMock:
    """A ClientSession-shaped mock good enough for pool connect + discovery."""
    session = AsyncMock()
    session.initialize = AsyncMock()
    # None caps → resources/prompts discovery is skipped; only list_tools runs.
    session.get_server_capabilities = MagicMock(return_value=None)
    session.list_tools = AsyncMock(return_value=MagicMock(tools=[]))
    return session


def _fake_transport_and_session(patches: dict[str, Any]) -> dict[str, Any]:
    """Build fake streamable-http transport + ClientSession cms.

    Records enter/exit events and captures the kwargs that reach
    ``streamablehttp_client`` (so the bearer-header / factory contract is
    observable).
    """
    events: list[str] = []
    captured_kwargs: dict[str, Any] = {}
    session = _make_pool_session_mock()

    @asynccontextmanager
    async def fake_streamablehttp_client(**kwargs: Any):
        captured_kwargs.clear()
        captured_kwargs.update(kwargs)
        events.append("transport_enter")
        try:
            yield (AsyncMock(), AsyncMock(), lambda: None)
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

    patches["streamablehttp_client"] = fake_streamablehttp_client
    patches["ClientSession"] = fake_client_session
    return {"events": events, "session": session, "kwargs": captured_kwargs}


async def _connect_under_lock(
    mgr: MCPClientManager, key: tuple[str, str], cfg: dict[str, Any], **kw: Any
) -> PoolEntryState:
    """Drive ``_connect_one_pool`` the way production does — under open_lock."""
    entry = await mgr._ensure_pool_entry(key)
    async with entry.open_lock:
        return await mgr._connect_one_pool(key, cfg, "tok-aaa", **kw)


# ---------------------------------------------------------------------------
# Owner lifecycle
# ---------------------------------------------------------------------------


class TestPoolTransportOwnerLifecycle:
    def test_connect_installs_owner_and_teardown_closes_gracefully(self, running_loop_mgr) -> None:
        mgr, loop, _ = running_loop_mgr
        patches: dict[str, Any] = {}
        fake = _fake_transport_and_session(patches)
        key = ("user-1", "pool-srv")

        with (
            patch(
                "turnstone.core.mcp_client.streamablehttp_client", patches["streamablehttp_client"]
            ),
            patch("turnstone.core.mcp_client.ClientSession", patches["ClientSession"]),
            patch.object(mgr, "_tcp_probe", new=AsyncMock()),
        ):
            entry = _run(loop, _connect_under_lock(mgr, key, _http_cfg()))
            assert entry.session is fake["session"]
            owner = entry.owner_task
            assert owner is not None and not owner.done()
            assert entry.close_requested is not None
            assert fake["events"] == ["transport_enter", "session_enter"]

            _run(loop, mgr._teardown_pool_entry(key))

        # Graceful close: the parked owner exits via the event — no cancel —
        # and unwinds BOTH cms in-task, inner-out (session before transport).
        assert owner.done() and not owner.cancelled()
        assert fake["events"] == [
            "transport_enter",
            "session_enter",
            "session_exit",
            "transport_exit",
        ]
        assert entry.session is None
        assert entry.owner_task is None
        assert entry.close_requested is None
        # The entry itself is NOT popped — teardown leaves map/catalog cleanup
        # to callers.
        assert key in mgr._user_pool_entries

    def test_owner_death_during_discovery_fails_fast(self, running_loop_mgr) -> None:
        """The owner-died branch of ``_await_owner_discovery`` — the reason the
        helper exists: discovery runs in the caller while the transport is
        hosted by the owner, so a transport collapse mid-discovery cancels the
        OWNER and a bare await on the response stream would hang until the 30s
        phase timeout. The race must convert that into a PROMPT
        ``ConnectionError``, reap the parked discovery future, and leave the
        entry torn down."""
        mgr, loop, _ = running_loop_mgr
        patches: dict[str, Any] = {}
        fake = _fake_transport_and_session(patches)
        key = ("user-1", "pool-srv")

        discovery_parked = asyncio.Event()

        async def _parked_list_tools() -> Any:
            discovery_parked.set()
            await asyncio.sleep(3600)  # the transport never answers

        fake["session"].list_tools = AsyncMock(side_effect=_parked_list_tools)

        async def _drive() -> tuple[float, BaseException | None]:
            entry = await mgr._ensure_pool_entry(key)

            async def _collapse_owner_when_parked() -> None:
                await discovery_parked.wait()
                owner = entry.owner_task  # installed before discovery begins
                assert owner is not None
                # The transport task group collapsing under live discovery
                # (e.g. an upstream 401) surfaces as the owner being cancelled.
                owner.cancel()

            collapser = asyncio.create_task(_collapse_owner_when_parked())
            t0 = asyncio.get_running_loop().time()
            exc: BaseException | None = None
            try:
                async with entry.open_lock:
                    await mgr._connect_one_pool(key, _http_cfg(), "tok-aaa")
            except Exception as e:
                # The expected ConnectionError; anything else (a cancel leak,
                # an interpreter exit) propagates and fails the test loudly.
                exc = e
            _ = await collapser  # synchronization point; failures propagate
            return asyncio.get_running_loop().time() - t0, exc

        with (
            patch(
                "turnstone.core.mcp_client.streamablehttp_client", patches["streamablehttp_client"]
            ),
            patch("turnstone.core.mcp_client.ClientSession", patches["ClientSession"]),
            patch.object(mgr, "_tcp_probe", new=AsyncMock()),
        ):
            elapsed, exc = _run(loop, _drive(), timeout=15)

        assert isinstance(exc, ConnectionError)
        assert "died during discovery" in str(exc)
        assert elapsed < 5.0  # prompt fail — not the 30s phase timeout
        entry = mgr._user_pool_entries[key]
        assert entry.session is None  # discovery-failure teardown ran
        assert entry.owner_task is None

    def test_cancelled_discovery_future_converts_to_connection_error(
        self, running_loop_mgr
    ) -> None:
        """A discovery future that completes CANCELLED without this race's own
        reap (an SDK-internal cancellation shape) is the transport-failure
        class, not the caller's cancellation — ``_await_owner_discovery`` must
        surface it as ``ConnectionError``, never a bare ``CancelledError`` the
        caller would misread as its own cancel."""
        mgr, loop, _ = running_loop_mgr

        async def _drive() -> BaseException | None:
            parked = asyncio.Event()

            async def _parked_owner() -> None:
                await parked.wait()

            owner = asyncio.create_task(_parked_owner())
            await asyncio.sleep(0)

            async def _self_cancelling_discovery() -> Any:
                # A coroutine raising CancelledError makes its wrapping task
                # complete CANCELLED — the shape of an SDK-internal cancel.
                raise asyncio.CancelledError

            exc: BaseException | None = None
            try:
                await mgr._await_owner_discovery(owner, _self_cancelling_discovery())
            except (Exception, asyncio.CancelledError) as e:
                # Exception covers the expected ConnectionError; CancelledError
                # covers the exact regression this test guards (the bare cancel
                # leaking through instead of being converted).
                exc = e
            parked.set()
            _ = await owner  # synchronization point; failures propagate
            return exc

        exc = _run(loop, _drive())
        assert isinstance(exc, ConnectionError)
        assert "cancelled by transport failure" in str(exc)

    def test_teardown_single_cancel_escalation(self, running_loop_mgr) -> None:
        """A parked owner whose in-task unwind stalls past the graceful window
        gets EXACTLY ONE cancel — never a second (a second abandons an anyio
        scope exit mid-flight and mints the zombie the protocol prevents)."""
        mgr, loop, _ = running_loop_mgr
        mgr._OWNER_CLOSE_GRACE_S = 0.1
        mgr._OWNER_CANCEL_GRACE_S = 1.0

        events: list[str] = []
        cancels = {"n": 0}
        session = _make_pool_session_mock()

        @asynccontextmanager
        async def fake_streamablehttp_client(**_kwargs: Any):
            events.append("transport_enter")
            try:
                yield (AsyncMock(), AsyncMock(), lambda: None)
            finally:
                events.append("transport_exit")

        @asynccontextmanager
        async def fake_session_cm():
            events.append("session_enter")
            try:
                yield session
            finally:
                # Stall the graceful unwind so teardown must escalate; count
                # each cancellation that reaches this in-task exit.
                try:
                    await asyncio.sleep(3600)
                except asyncio.CancelledError:
                    cancels["n"] += 1
                    raise
                finally:
                    events.append("session_exit")

        def fake_session(_read: Any, _write: Any, message_handler: Any = None):
            return fake_session_cm()

        key = ("user-1", "pool-srv")
        with (
            patch("turnstone.core.mcp_client.streamablehttp_client", fake_streamablehttp_client),
            patch("turnstone.core.mcp_client.ClientSession", fake_session),
            patch.object(mgr, "_tcp_probe", new=AsyncMock()),
        ):
            entry = _run(loop, _connect_under_lock(mgr, key, _http_cfg()))
            owner = entry.owner_task
            assert owner is not None
            _run(loop, mgr._teardown_pool_entry(key), timeout=10)

        assert owner.done() and owner.cancelled()
        assert cancels["n"] == 1
        assert events[-1] == "transport_exit"
        assert entry.session is None and entry.owner_task is None

    def test_owner_death_evicts_session_keeps_entry_and_catalog(self, running_loop_mgr) -> None:
        """The transport collapsing under a live session (owner dies with no
        requested close) evicts the session via the done-callback but leaves the
        entry AND its discovered catalog in place for the next dispatch."""
        mgr, loop, _ = running_loop_mgr
        patches: dict[str, Any] = {}
        fake = _fake_transport_and_session(patches)
        key = ("user-1", "pool-srv")

        with (
            patch(
                "turnstone.core.mcp_client.streamablehttp_client", patches["streamablehttp_client"]
            ),
            patch("turnstone.core.mcp_client.ClientSession", patches["ClientSession"]),
            patch.object(mgr, "_tcp_probe", new=AsyncMock()),
        ):
            entry = _run(loop, _connect_under_lock(mgr, key, _http_cfg()))
            owner = entry.owner_task
            assert owner is not None and entry.session is fake["session"]
            # Seed a catalog so we can prove the death-callback leaves it alone.
            entry.tools = [{"name": "mcp__pool-srv__ping", "server": "pool-srv"}]

            # Simulate the transport task group collapsing: the owner gets a
            # stray cancellation (exactly what anyio's scope delivery does).
            loop.call_soon_threadsafe(owner.cancel)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and entry.owner_task is not None:
                time.sleep(0.02)

        assert owner.done()
        assert entry.session is None  # evicted by the done-callback
        assert entry.owner_task is None
        # Third session-drop site of the bearer-clearing sweep: the entry
        # may now cool indefinitely, so the dead plaintext bearer copy
        # must not cool with it.
        assert entry.bound_token is None
        assert key in mgr._user_pool_entries  # entry kept
        assert entry.tools == [
            {"name": "mcp__pool-srv__ping", "server": "pool-srv"}
        ]  # catalog kept
        # The cms were still unwound in-task despite the stray cancel.
        assert fake["events"][-2:] == ["session_exit", "transport_exit"]

    def test_caller_cancel_mid_connect_does_not_abandon_cms(self, running_loop_mgr) -> None:
        """Cancelling the CONNECTING caller (an eviction giving up, shutdown, a
        sync boundary timing out) must close the owner via the one-cancel
        protocol — the transport cm still exits, in-task."""
        mgr, loop, _ = running_loop_mgr
        events: list[str] = []
        entered = asyncio.Event()
        key = ("user-1", "pool-srv")

        @asynccontextmanager
        async def hanging_streamablehttp_client(**_kwargs: Any):
            events.append("transport_enter")
            try:
                entered.set()
                await asyncio.sleep(3600)  # server accepted, then stalled
                yield (AsyncMock(), AsyncMock(), lambda: None)
            finally:
                events.append("transport_exit")

        async def _drive() -> None:
            entry = await mgr._ensure_pool_entry(key)

            async def _connect() -> None:
                async with entry.open_lock:
                    await mgr._connect_one_pool(key, _http_cfg(), "tok-aaa")

            connect = asyncio.create_task(_connect())
            await asyncio.wait_for(entered.wait(), timeout=5)
            connect.cancel()  # the attempt-timeout / shutdown shape
            with contextlib.suppress(asyncio.CancelledError):
                _ = await connect  # only the expected cancel is absorbed
            # The owner must be closed (one cancel) and fully unwound.
            deadline = asyncio.get_running_loop().time() + 5
            while asyncio.get_running_loop().time() < deadline:
                owners = [
                    t
                    for t in asyncio.all_tasks()
                    if t.get_name().startswith("mcp-pool-owner:") and not t.done()
                ]
                if not owners:
                    return
                await asyncio.sleep(0.02)
            raise AssertionError("owner task still alive after caller cancel")

        with (
            patch("turnstone.core.mcp_client.streamablehttp_client", hanging_streamablehttp_client),
            patch.object(mgr, "_tcp_probe", new=AsyncMock()),
        ):
            _run(loop, _drive(), timeout=15)

        assert events == ["transport_enter", "transport_exit"]
        assert mgr._user_pool_entries[key].session is None


# ---------------------------------------------------------------------------
# Client-kwargs contract (bearer header + auth-capture factory)
# ---------------------------------------------------------------------------


class TestPoolOwnerClientKwargs:
    def test_client_factory_present_iff_auth_capture(self, running_loop_mgr) -> None:
        """The caller builds ``client_kwargs`` and the owner passes them to
        ``streamablehttp_client`` verbatim: the auth-capture
        ``httpx_client_factory`` is present exactly when a carrier is supplied,
        and the per-user bearer always reaches the wire."""
        mgr, loop, _ = running_loop_mgr
        key = ("user-1", "pool-srv")

        # With auth_capture → factory present.
        patches_a: dict[str, Any] = {}
        fake_a = _fake_transport_and_session(patches_a)
        with (
            patch(
                "turnstone.core.mcp_client.streamablehttp_client",
                patches_a["streamablehttp_client"],
            ),
            patch("turnstone.core.mcp_client.ClientSession", patches_a["ClientSession"]),
            patch.object(mgr, "_tcp_probe", new=AsyncMock()),
        ):
            _run(loop, _connect_under_lock(mgr, key, _http_cfg(), auth_capture=_AuthCapture()))
            assert "httpx_client_factory" in fake_a["kwargs"]
            assert fake_a["kwargs"]["headers"]["Authorization"] == "Bearer tok-aaa"
            _run(loop, mgr._teardown_pool_entry(key))

        # Without auth_capture → factory absent (but bearer still present).
        patches_b: dict[str, Any] = {}
        fake_b = _fake_transport_and_session(patches_b)
        with (
            patch(
                "turnstone.core.mcp_client.streamablehttp_client",
                patches_b["streamablehttp_client"],
            ),
            patch("turnstone.core.mcp_client.ClientSession", patches_b["ClientSession"]),
            patch.object(mgr, "_tcp_probe", new=AsyncMock()),
        ):
            _run(loop, _connect_under_lock(mgr, key, _http_cfg()))
            assert "httpx_client_factory" not in fake_b["kwargs"]
            assert fake_b["kwargs"]["headers"]["Authorization"] == "Bearer tok-aaa"
            _run(loop, mgr._teardown_pool_entry(key))
