"""Foreground MCP deadlines must not be mistaken for server failures."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import anyio
import pytest

from tests.conftest import _run_on_loop, _seed_static_state, stop_loop_thread
from turnstone.core.mcp_client import MCPClientManager, PoolEntryState

_KEY = ("user-1", "srv")
_ROW = {"name": "srv", "transport": "streamable-http", "url": "http://127.0.0.1:1/mcp"}
_SDK_METHODS = {"tool": "call_tool", "resource": "read_resource", "prompt": "get_prompt"}


@pytest.fixture(params=["tool", "resource", "prompt"])
def kind(request):
    return request.param


@pytest.fixture
def dispatch_env(monkeypatch):
    mgr = MCPClientManager({})
    session = SimpleNamespace(
        call_tool=AsyncMock(), read_resource=AsyncMock(), get_prompt=AsyncMock()
    )
    _seed_static_state(mgr, "srv", session=session)
    mgr._tool_map["mcp__srv__op"] = ("srv", "op")
    mgr._resource_map["test://resource"] = ("srv", "test://resource")
    mgr._prompt_map["mcp__srv__op"] = ("srv", "op")
    mgr.set_app_state(SimpleNamespace())
    monkeypatch.setattr(
        mgr,
        "_pool_lookup_checked",
        AsyncMock(return_value=(SimpleNamespace(token="test-token"), None)),
    )
    loop = asyncio.new_event_loop()
    mgr._loop = loop
    thread = threading.Thread(target=loop.run_forever, daemon=True, name="mcp-deadline-test")
    thread.start()

    async def seed():
        entry = PoolEntryState(key=_KEY, open_lock=asyncio.Lock(), session=session)
        mgr._user_pool_entries[_KEY] = entry
        return entry

    try:
        yield mgr, session, _run_on_loop(loop, seed()), loop
    finally:

        async def drain():
            tasks = asyncio.all_tasks() - {asyncio.current_task()}
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        _run_on_loop(loop, drain())
        stop_loop_thread(loop, thread)


def _call(mgr: MCPClientManager, kind: str, pooled: bool, *, timeout: int = 5) -> Any:
    if pooled:
        kwargs = dict(user_id=_KEY[0], server_name=_KEY[1], server_row=_ROW, timeout=timeout)
        if kind == "tool":
            return mgr._dispatch_pool_sync(original_name="op", arguments={}, **kwargs)
        if kind == "resource":
            return mgr._dispatch_pool_resource_sync(uri="test://resource", **kwargs)
        return mgr._dispatch_pool_prompt_sync(original_name="op", arguments={}, **kwargs)
    if kind == "tool":
        return mgr.call_tool_sync("mcp__srv__op", {}, timeout=timeout)
    if kind == "resource":
        return mgr.read_resource_sync("test://resource", timeout=timeout)
    return mgr.get_prompt_sync("mcp__srv__op", timeout=timeout)


@pytest.mark.parametrize("pooled", [False, True], ids=["static", "pool"])
def test_caller_deadline_cancels_locally_without_changing_breaker(dispatch_env, kind, pooled):
    mgr, session, entry, loop = dispatch_env
    cancelled = threading.Event()

    async def slow(*args, **kwargs):
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    method = getattr(session, _SDK_METHODS[kind])
    method.side_effect = slow
    mgr._consecutive_failures["srv"] = 2
    with pytest.raises(TimeoutError, match="timed out after 1s"):
        _call(mgr, kind, pooled, timeout=1)
    assert cancelled.wait(timeout=2)

    async def wait_for_cleanup():
        while entry.in_flight or entry.open_lock.locked() or mgr._static_servers["srv"].in_flight:
            await asyncio.sleep(0)

    _run_on_loop(loop, asyncio.wait_for(wait_for_cleanup(), timeout=2))
    method.assert_awaited_once()
    assert mgr._consecutive_failures["srv"] == 2
    assert "srv" not in mgr._circuit_open_until
    assert entry.session is session
    assert mgr._static_servers["srv"].session is session


@pytest.mark.parametrize("pooled", [False, True], ids=["static", "pool"])
@pytest.mark.parametrize("error_type", [TimeoutError, anyio.BrokenResourceError])
def test_operation_failure_preserves_error_and_counts_once(dispatch_env, kind, pooled, error_type):
    mgr, session, entry, _ = dispatch_env
    error = error_type("operation failed")
    getattr(session, _SDK_METHODS[kind]).side_effect = error
    mgr._consecutive_failures["srv"] = 1

    with pytest.raises(error_type) as caught:
        _call(mgr, kind, pooled)

    assert caught.value is error
    assert mgr._consecutive_failures["srv"] == 2
    state = entry if pooled else mgr._static_servers["srv"]
    # Static generic operation errors count without transport eviction; the
    # pool classifier has always treated operation TimeoutError as transport.
    if pooled or error_type is anyio.BrokenResourceError:
        assert state.session is None
    else:
        assert state.session is session


def test_queued_pool_deadline_leaves_holder_and_breaker_untouched(dispatch_env, kind):
    mgr, session, entry, loop = dispatch_env
    _run_on_loop(loop, entry.open_lock.acquire())
    mgr._consecutive_failures["srv"] = 2
    try:
        with pytest.raises(TimeoutError, match="timed out after 1s"):
            _call(mgr, kind, True, timeout=1)
        assert mgr._consecutive_failures["srv"] == 2
        assert "srv" not in mgr._circuit_open_until
        assert entry.session is session
        assert entry.open_lock.locked()
        getattr(session, _SDK_METHODS[kind]).assert_not_awaited()
    finally:
        loop.call_soon_threadsafe(entry.open_lock.release)


@pytest.mark.parametrize("status", [None, 401, 403])
def test_cancelled_pool_waiter_does_not_borrow_holder_auth(dispatch_env, kind, status, monkeypatch):
    mgr, session, entry, loop = dispatch_env
    # Cancellation must never reach auth classification.
    classify = Mock(wraps=mgr._classify_failure)
    monkeypatch.setattr(mgr, "_classify_failure", classify)
    mgr._consecutive_failures["srv"] = 2

    async def scenario():
        reached = asyncio.Event()

        async def lookup(*args, **kwargs):
            reached.set()
            return SimpleNamespace(token="test-token"), None

        monkeypatch.setattr(mgr, "_pool_lookup_checked", lookup)
        kwargs = dict(user_id=_KEY[0], server_name=_KEY[1], server_row=_ROW)
        if kind == "tool":
            op = mgr._dispatch_pool(original_name="op", arguments={}, **kwargs)
        elif kind == "resource":
            op = mgr._dispatch_pool_resource(uri="test://resource", **kwargs)
        else:
            op = mgr._dispatch_pool_prompt(original_name="op", arguments={}, **kwargs)
        async with entry.open_lock:
            task = asyncio.create_task(op)
            try:
                await asyncio.wait_for(reached.wait(), timeout=2)
                entry.auth_capture.status = status
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert entry.session is session
                assert entry.open_lock.locked()
                assert entry.auth_capture.status == status
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    _run_on_loop(loop, scenario())
    classify.assert_not_called()
    assert mgr._consecutive_failures["srv"] == 2
    getattr(session, _SDK_METHODS[kind]).assert_not_awaited()
