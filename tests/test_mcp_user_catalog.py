"""Phase 7 integration tests — per-user catalog scoping.

These tests drive ``_connect_one_pool`` against a real
``streamablehttp_client`` with the SDK's response hook bound to an
``httpx.MockTransport`` (R6 verified the happy path closes cleanly and
a mid-discovery 401 propagates through anyio TaskGroup unwinding).
Direct method injection on ``MagicMock`` as the SOLE gate is
forbidden — invariant 14 of the OAuth-MCP RFC.

The test scaffolding mirrors ``tests/test_mcp_pool_auth_introspection.py``
(per-test ``running_loop_mgr`` fixture, ``_seed_oauth_server`` helper,
``_run_on_loop``) and adds a tool-discovery transport that returns
``initialize`` + ``tools/list`` responses programmatically.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from turnstone.core.mcp_client import (
    MCPClientManager,
    PoolEntryState,
    _AuthCapture,
    _make_capturing_http_factory,
)

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def running_loop_mgr() -> Any:
    """Background mcp-loop fixture. Mirrors test_mcp_pool_auth_introspection."""
    cfg: dict[str, Any] = {}
    mgr = MCPClientManager(cfg)
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True, name="mcp-pool-test-loop")
    thread.start()
    mgr._loop = loop
    try:
        yield mgr, loop, thread
    finally:

        async def _drain(m: MCPClientManager) -> None:
            task = m._user_pool_eviction_task
            if task is not None:
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task
                m._user_pool_eviction_task = None

        with contextlib.suppress(Exception):
            asyncio.run_coroutine_threadsafe(_drain(mgr), loop).result(timeout=2)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)


def _run_on_loop(loop: asyncio.AbstractEventLoop, coro: Any, timeout: float = 10) -> Any:
    fut = asyncio.run_coroutine_threadsafe(coro, loop)
    return fut.result(timeout=timeout)


def _build_mock_transport_factory(
    mgr: MCPClientManager,
    monkeypatch: pytest.MonkeyPatch,
    handler: Any,
) -> None:
    """Wrap ``_make_capturing_http_factory`` so the resulting httpx
    client uses an ``httpx.MockTransport(handler)``.

    The production capturing factory is preserved (we still get the
    response-hook plumbing); only the underlying transport is swapped.
    Mirrors the spike v3 pattern that R6 used to verify discovery
    behaviour against the real SDK.
    """
    real_factory = _make_capturing_http_factory

    def _wrapped_factory(
        capture: _AuthCapture,
        fired_event: asyncio.Event | None = None,
    ) -> Any:
        inner = real_factory(capture, fired_event=fired_event)

        def _factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
            client = inner(*args, **kwargs)
            client._transport = httpx.MockTransport(handler)
            return client

        return _factory

    monkeypatch.setattr(
        "turnstone.core.mcp_client._make_capturing_http_factory",
        _wrapped_factory,
    )


def _patch_tcp_probe(mgr: MCPClientManager, monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the TCP probe — MockTransport never opens a real socket."""

    async def _noop_probe(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(MCPClientManager, "_tcp_probe", _noop_probe)


def _make_jsonrpc_handler(
    *,
    init_response: dict[str, Any] | None = None,
    list_tools_response: dict[str, Any] | None = None,
    list_tools_status: int = 200,
    list_tools_error_payload: dict[str, Any] | None = None,
    list_tools_seq: list[dict[str, Any]] | None = None,
    counter: list[int] | None = None,
    record_bodies: list[str] | None = None,
) -> Any:
    """Build an httpx async handler for the ``streamable-http`` shape.

    Returns a coroutine the MockTransport invokes per request.
    """
    if init_response is None:
        init_response = {
            "jsonrpc": "2.0",
            "id": 0,
            "result": {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "fake", "version": "1"},
            },
        }
    if list_tools_response is None:
        list_tools_response = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"tools": []},
        }
    counter = counter if counter is not None else [0]
    list_tools_seq = list_tools_seq or []
    list_tools_index = [0]

    async def _handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(405)
        if req.method == "DELETE":
            return httpx.Response(200)
        body = req.content.decode() if req.content else ""
        if record_bodies is not None:
            record_bodies.append(body)
        if "notifications/initialized" in body:
            return httpx.Response(202)
        counter[0] += 1
        if "method" in body and '"initialize"' in body:
            return httpx.Response(
                200,
                headers={"content-type": "application/json", "mcp-session-id": "sess-1"},
                json=init_response,
            )
        # ``tools/list`` — by request order: explicit override first, then
        # the staged sequence, then fall back to ``list_tools_response``.
        if list_tools_status != 200:
            return httpx.Response(
                list_tools_status,
                headers={"www-authenticate": 'Bearer error="invalid_token"'},
                json=list_tools_error_payload or {"error": "unauthorized"},
            )
        if list_tools_seq and list_tools_index[0] < len(list_tools_seq):
            payload = list_tools_seq[list_tools_index[0]]
            list_tools_index[0] += 1
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                json=payload,
            )
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json=list_tools_response,
        )

    return _handler


def _list_tools_payload(tools: list[dict[str, Any]], req_id: int = 1) -> dict[str, Any]:
    """Build a minimal ``tools/list`` JSON-RPC result payload."""
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "result": {"tools": tools},
    }


def _tool_spec(name: str, description: str = "") -> dict[str, Any]:
    return {
        "name": name,
        "description": description or f"do {name}",
        "inputSchema": {"type": "object", "properties": {}},
    }


def _connect_pool(
    mgr: MCPClientManager,
    loop: asyncio.AbstractEventLoop,
    *,
    user_id: str,
    server_name: str,
    url: str = "https://mcp.example.com/sse",
    access_token: str = "access-aaa",
) -> PoolEntryState:
    """Drive ``_connect_one_pool`` once; returns the resulting entry."""
    cfg: dict[str, Any] = {"type": "streamable-http", "url": url, "headers": {}}
    capture = _AuthCapture()
    fired = asyncio.Event()

    async def _go() -> PoolEntryState:
        return await mgr._connect_one_pool(
            (user_id, server_name),
            cfg,
            access_token,
            auth_capture=capture,
            auth_fired_event=fired,
        )

    return _run_on_loop(loop, _go())


# ---------------------------------------------------------------------------
# Discovery on first connect — drives REAL streamablehttp_client + httpx
# MockTransport per invariant 14.
# ---------------------------------------------------------------------------


def test_discovery_on_pool_connect_via_real_streamable_http(
    running_loop_mgr: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_connect_one_pool`` discovers the user's tool catalog after
    ``initialize()`` and stores it on the entry.

    Drives through the real ``streamablehttp_client`` and the real
    SDK ``ClientSession`` (only the underlying httpx transport is
    swapped). Asserts on observable state rather than mock call count
    so a refactor that produces the same end state still passes.

    Verified by reverting the discovery block in ``_connect_one_pool``
    (the ``await session.list_tools()`` + ``_rebuild_user_tool_map``
    calls): this test fails because ``entry.tools`` stays ``None`` and
    ``_user_tool_map`` never gains the user_id key.
    """
    mgr, loop, _ = running_loop_mgr
    handler = _make_jsonrpc_handler(
        list_tools_response=_list_tools_payload(
            [_tool_spec("do_thing"), _tool_spec("other")],
        ),
    )
    _build_mock_transport_factory(mgr, monkeypatch, handler)
    _patch_tcp_probe(mgr, monkeypatch)

    entry = _connect_pool(mgr, loop, user_id="user-1", server_name="pool-srv")

    assert entry.session is not None
    assert entry.tools is not None
    tool_names = {t["function"]["name"] for t in entry.tools}
    assert tool_names == {"mcp__pool-srv__do_thing", "mcp__pool-srv__other"}
    # Per-user index reflects the discovery.
    user_map = mgr._user_tool_map.get("user-1")
    assert user_map is not None
    assert "mcp__pool-srv__do_thing" in user_map
    assert user_map["mcp__pool-srv__do_thing"] == ("pool-srv", "do_thing")
    # is_mcp_tool with the user's id sees the discovered tool.
    assert mgr.is_mcp_tool("mcp__pool-srv__do_thing", user_id="user-1") is True
    # Default (None) caller does NOT — pool is per-user.
    assert mgr.is_mcp_tool("mcp__pool-srv__do_thing") is False


def test_pool_tool_visibility_user_isolation(
    running_loop_mgr: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two users connecting to the same server have independent
    ``_user_tool_map`` entries; one user never sees another's catalog."""
    mgr, loop, _ = running_loop_mgr
    # Both users see the same set, but the per-user maps must stay
    # independent — privacy is a structural property even when the
    # contents happen to match.
    handler = _make_jsonrpc_handler(
        list_tools_response=_list_tools_payload([_tool_spec("shared_tool")]),
    )
    _build_mock_transport_factory(mgr, monkeypatch, handler)
    _patch_tcp_probe(mgr, monkeypatch)

    _connect_pool(mgr, loop, user_id="user-1", server_name="pool-srv")
    _connect_pool(mgr, loop, user_id="user-2", server_name="pool-srv")

    map_1 = mgr._user_tool_map.get("user-1")
    map_2 = mgr._user_tool_map.get("user-2")
    assert map_1 is not None and map_2 is not None
    assert map_1 is not map_2
    assert "mcp__pool-srv__shared_tool" in map_1
    assert "mcp__pool-srv__shared_tool" in map_2
    # Cross-user visibility is forbidden via is_mcp_tool: user-1's name
    # presence implies nothing about user-3.
    assert mgr.is_mcp_tool("mcp__pool-srv__shared_tool", user_id="user-3") is False


def test_eviction_drops_catalog_and_fires_listener(
    running_loop_mgr: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_evict_session`` clears ``entry.tools``, rebuilds the user's
    map (drops the now-empty entry), and fires user + admin listeners.

    Verified by reverting the catalog-cleanup block in ``_evict_session``
    (drop the ``evict.tools = None`` / ``_rebuild_user_tool_map`` /
    ``_notify_user_tool_listeners`` calls): the test fails because the
    listener never fires AND ``is_mcp_tool`` keeps returning True for
    the now-evicted tool.
    """
    mgr, loop, _ = running_loop_mgr
    handler = _make_jsonrpc_handler(
        list_tools_response=_list_tools_payload([_tool_spec("do_thing")]),
    )
    _build_mock_transport_factory(mgr, monkeypatch, handler)
    _patch_tcp_probe(mgr, monkeypatch)

    _connect_pool(mgr, loop, user_id="user-1", server_name="pool-srv")
    assert mgr.is_mcp_tool("mcp__pool-srv__do_thing", user_id="user-1") is True

    # Register one user-keyed and one admin (None) listener; both
    # MUST fire on this user's eviction.
    user_calls = [0]
    admin_calls = [0]
    other_calls = [0]

    def _user_cb() -> None:
        user_calls[0] += 1

    def _admin_cb() -> None:
        admin_calls[0] += 1

    def _other_cb() -> None:
        other_calls[0] += 1

    mgr.add_listener(_user_cb, user_id="user-1")
    mgr.add_listener(_admin_cb)  # admin / None
    mgr.add_listener(_other_cb, user_id="user-2")

    mgr._evict_session(("user-1", "pool-srv"))

    # Catalog cleared.
    entry = mgr._user_pool_entries[("user-1", "pool-srv")]
    assert entry.session is None
    assert entry.tools is None
    # User map dropped (no remaining pool entries for this user).
    assert "user-1" not in mgr._user_tool_map
    # is_mcp_tool no longer surfaces the evicted name.
    assert mgr.is_mcp_tool("mcp__pool-srv__do_thing", user_id="user-1") is False
    # Listener fan-out: matching user + admin fire, OTHER user does not.
    assert user_calls[0] == 1, f"user-keyed listener fired {user_calls[0]} times; expected 1"
    assert admin_calls[0] == 1, f"admin listener fired {admin_calls[0]} times; expected 1"
    assert other_calls[0] == 0, (
        f"unrelated user-2 listener fired {other_calls[0]} times; expected 0 — "
        "RFC §3.3 privacy violation."
    )


def test_close_pool_entry_if_idle_clears_catalog_and_fires_listener(
    running_loop_mgr: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LRU/TTL eviction (``_close_pool_entry_if_idle``) mirrors
    ``_evict_session``'s catalog-cleanup contract: drops the entry,
    prunes the notification-debounce dict, rebuilds the user's tool
    map, and fires user + admin listeners.

    Phase 7 round-2 review hardening (round2-1): the bug-2 fix added
    the catalog-cleanup block to this method but no integration test
    drove it — exactly the failure mode flagged in
    ``feedback_tests_through_boundaries.md``. Negative test: drop the
    ``_rebuild_user_tool_map`` / ``_notify_user_tool_listeners`` calls
    from ``_close_pool_entry_if_idle``'s post-pop block; this test
    fails because ``is_mcp_tool`` keeps returning ``True`` for the
    evicted name AND no listener fires.
    """
    mgr, loop, _ = running_loop_mgr
    handler = _make_jsonrpc_handler(
        list_tools_response=_list_tools_payload([_tool_spec("do_thing")]),
    )
    _build_mock_transport_factory(mgr, monkeypatch, handler)
    _patch_tcp_probe(mgr, monkeypatch)

    _connect_pool(mgr, loop, user_id="user-1", server_name="pool-srv")
    key = ("user-1", "pool-srv")
    assert mgr.is_mcp_tool("mcp__pool-srv__do_thing", user_id="user-1") is True
    # Sanity: in_flight must be 0 for the eviction path to proceed.
    assert mgr._user_pool_entries[key].in_flight == 0
    # Seed the debounce dict so the perf-1 prune is observable.
    mgr._last_pool_notification_refresh[key] = 0.0

    user_calls = [0]
    admin_calls = [0]
    other_calls = [0]

    def _user_cb() -> None:
        user_calls[0] += 1

    def _admin_cb() -> None:
        admin_calls[0] += 1

    def _other_cb() -> None:
        other_calls[0] += 1

    mgr.add_listener(_user_cb, user_id="user-1")
    mgr.add_listener(_admin_cb)  # admin / None
    mgr.add_listener(_other_cb, user_id="user-2")

    # Drive the LRU/TTL eviction path directly. ``open_lock`` is
    # uncontested (no concurrent dispatch) and ``in_flight`` is 0,
    # so the close proceeds without retry.
    _run_on_loop(loop, mgr._close_pool_entry_if_idle(key))

    # Entry fully removed (LRU eviction pops the dict — unlike
    # ``_evict_session`` which keeps the entry as a ``session=None``
    # phantom for the next dispatch to re-connect).
    assert key not in mgr._user_pool_entries
    assert key not in mgr._user_pool_last_used
    assert key not in mgr._user_pool_locks
    # perf-1 prune: debounce dict no longer carries the key.
    assert key not in mgr._last_pool_notification_refresh
    # Catalog cleanup ran in BOTH dicts (bug-1 sibling + bug-2 cleanup).
    assert "user-1" not in mgr._user_tool_map
    assert "user-1" not in mgr._user_tools
    assert mgr.is_mcp_tool("mcp__pool-srv__do_thing", user_id="user-1") is False
    # Listener fan-out: matching user + admin fire, OTHER user does not.
    assert user_calls[0] == 1, f"user-keyed listener fired {user_calls[0]} times; expected 1"
    assert admin_calls[0] == 1, f"admin listener fired {admin_calls[0]} times; expected 1"
    assert other_calls[0] == 0, (
        f"unrelated user-2 listener fired {other_calls[0]} times; expected 0 — "
        "RFC §3.3 privacy violation."
    )


def test_reconnect_after_eviction_repopulates_catalog(
    running_loop_mgr: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After eviction, the next ``_connect_one_pool`` re-populates the
    catalog from the SDK by construction — no extra glue required.

    Verified by reverting the discovery block in ``_connect_one_pool``:
    the reconnected entry's ``tools`` stays ``None`` and the user map
    never re-emerges.
    """
    mgr, loop, _ = running_loop_mgr
    # Sequence: first connect sees [tool_a]; eviction clears; second
    # connect (after backend rotates) sees [tool_b].
    handler = _make_jsonrpc_handler(
        list_tools_seq=[
            _list_tools_payload([_tool_spec("tool_a")]),
            _list_tools_payload([_tool_spec("tool_b")], req_id=1),
        ],
    )
    _build_mock_transport_factory(mgr, monkeypatch, handler)
    _patch_tcp_probe(mgr, monkeypatch)

    _connect_pool(mgr, loop, user_id="user-1", server_name="pool-srv")
    assert mgr.is_mcp_tool("mcp__pool-srv__tool_a", user_id="user-1") is True

    mgr._evict_session(("user-1", "pool-srv"))
    assert mgr.is_mcp_tool("mcp__pool-srv__tool_a", user_id="user-1") is False

    _connect_pool(mgr, loop, user_id="user-1", server_name="pool-srv")
    # Reconnect picked up the rotated catalog.
    assert mgr.is_mcp_tool("mcp__pool-srv__tool_b", user_id="user-1") is True
    # Old name was correctly purged — not still hanging around.
    assert mgr.is_mcp_tool("mcp__pool-srv__tool_a", user_id="user-1") is False


def test_refresh_pool_server_tools_isolates_user(
    running_loop_mgr: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_refresh_pool_server_tools`` refreshes THIS user's catalog only,
    leaving the static path and other users' catalogs untouched.

    Drives ``_refresh_pool_server_tools`` directly (which is also what
    the pool session's notification handler invokes) and asserts:
    * The pool entry's ``tools`` field reflects the new catalog.
    * ``_user_tool_map`` for the owning user is updated.
    * ``_tool_map`` (static) is untouched — invariant 1.
    * Other users' ``_user_tool_map`` entries are untouched.

    Verified by reverting ``_refresh_pool_server_tools`` to call
    ``_rebuild_tools()`` (the static path) instead of
    ``_rebuild_user_tool_map(user_id)``: this test fails because
    ``_tool_map`` is mutated and the user map doesn't pick up the
    rotated catalog.
    """
    mgr, loop, _ = running_loop_mgr
    handler = _make_jsonrpc_handler(
        list_tools_seq=[
            _list_tools_payload([_tool_spec("v1")]),
            _list_tools_payload([_tool_spec("v2")], req_id=2),
        ],
    )
    _build_mock_transport_factory(mgr, monkeypatch, handler)
    _patch_tcp_probe(mgr, monkeypatch)

    # Pre-seed a static-path entry to assert it stays untouched.
    from turnstone.core.mcp_client import StaticServerState

    sentinel_static = StaticServerState(name="static-srv", session=MagicMock())
    sentinel_static.tools = [
        {"type": "function", "function": {"name": "mcp__static-srv__static_one"}}
    ]
    mgr._static_servers["static-srv"] = sentinel_static
    mgr._tool_map["mcp__static-srv__static_one"] = ("static-srv", "static_one")

    # Pre-seed an unrelated user's pool entry so we can assert it's
    # untouched too.
    async def _seed_other_user() -> None:
        entry = await mgr._ensure_pool_entry(("user-2", "pool-srv"))
        entry.session = MagicMock()
        entry.tools = [{"type": "function", "function": {"name": "mcp__pool-srv__user2_only"}}]
        mgr._rebuild_user_tool_map("user-2")

    _run_on_loop(loop, _seed_other_user())
    # Sanity: user-2's map is set.
    assert "mcp__pool-srv__user2_only" in (mgr._user_tool_map.get("user-2") or {})

    _connect_pool(mgr, loop, user_id="user-1", server_name="pool-srv")
    assert mgr.is_mcp_tool("mcp__pool-srv__v1", user_id="user-1") is True

    # Trigger a refresh (simulating the notification handler invoking it).
    added_removed = _run_on_loop(loop, mgr._refresh_pool_server_tools(("user-1", "pool-srv")))
    added, removed = added_removed
    assert added == ["mcp__pool-srv__v2"]
    assert removed == ["mcp__pool-srv__v1"]

    # User-1 sees the new tool.
    assert mgr.is_mcp_tool("mcp__pool-srv__v2", user_id="user-1") is True
    assert mgr.is_mcp_tool("mcp__pool-srv__v1", user_id="user-1") is False

    # Static path untouched — hard invariant 1.
    assert mgr._tool_map == {"mcp__static-srv__static_one": ("static-srv", "static_one")}
    assert mgr._static_servers["static-srv"].tools == [
        {"type": "function", "function": {"name": "mcp__static-srv__static_one"}}
    ]
    # User-2's pool catalog untouched.
    assert mgr._user_tool_map["user-2"] == {
        "mcp__pool-srv__user2_only": ("pool-srv", "user2_only"),
    }


# ---------------------------------------------------------------------------
# Invariant 1 — canonical regression: static path byte-identical when
# oauth_user is disabled.
# ---------------------------------------------------------------------------


def test_static_path_byte_identical_with_oauth_user_disabled(
    running_loop_mgr: Any,
) -> None:
    """In a static-only deployment (no oauth_user config rows), the
    Phase 7 changes MUST leave every static-path catalog API byte-
    identical to pre-Phase-7 behaviour. This is invariant 1.

    Drives ``_connect_one`` (static path) and asserts:
    * ``get_tools()`` (no user_id) returns the static catalog only.
    * ``is_mcp_tool(name)`` (no user_id) works as before.
    * ``_tool_map`` is the only catalog index touched.
    * No ``_user_*`` state exists (no pool entries / no per-user maps).

    Static-path connect uses ``StaticServerState`` instead of pool
    entries, so this test seeds it directly rather than driving through
    ``_connect_one`` (which is exercised by the existing integration
    suite). The point is to assert the catalog API contract stays
    intact under Phase 7's signature widening.

    Verified by widening the default of ``get_tools(user_id)`` to
    ``user_id=""`` (instead of ``None``) — the test fails because
    ``get_tools()`` now goes through the per-user merge branch and
    starts paying for an empty-loop iteration that pre-Phase-7
    callers never saw.
    """
    mgr, _loop, _ = running_loop_mgr
    # Seed the static-path catalog directly (no pool plumbing needed).
    from turnstone.core.mcp_client import StaticServerState

    sentinel = StaticServerState(name="static-srv", session=MagicMock())
    static_tool = {
        "type": "function",
        "function": {
            "name": "mcp__static-srv__list",
            "description": "list",
            "parameters": {"type": "object", "properties": {}},
        },
    }
    sentinel.tools = [static_tool]
    mgr._static_servers["static-srv"] = sentinel
    mgr._tools = [static_tool]
    mgr._tool_map["mcp__static-srv__list"] = ("static-srv", "list")

    # Pre-Phase-7 caller pattern: no kwargs.
    pre_phase7_tools = mgr.get_tools()
    assert pre_phase7_tools == [static_tool]
    assert mgr.is_mcp_tool("mcp__static-srv__list") is True
    assert mgr.is_mcp_tool("nonexistent") is False
    # Per-user state does NOT exist when no pool entries are present.
    assert mgr._user_pool_entries == {}
    assert mgr._user_tool_map == {}


# ---------------------------------------------------------------------------
# R6 verification (production form): a 401 mid-discovery propagates
# rather than hangs. This test pins the empirical conclusion so a future
# SDK upgrade that re-introduces the hang surfaces here.
# ---------------------------------------------------------------------------


def test_pool_connect_list_tools_401_propagates(
    running_loop_mgr: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 401 returned to the discovery POST surfaces as an exception
    from ``_connect_one_pool`` within a bounded timeout (no hang).

    The SDK propagates the 401 through anyio TaskGroup unwinding (raises
    an ``ExceptionGroup`` from the surrounding ``streamablehttp_client``
    context); the production reading is "discovery does not need the
    carrier-race shape from ``_dispatch_pool_with_entry``."

    If a future SDK bump silently re-introduces the hang, this test
    will time out — ``_run_on_loop`` is invoked below with a 5s budget.
    """
    mgr, loop, _ = running_loop_mgr
    handler = _make_jsonrpc_handler(list_tools_status=401)
    _build_mock_transport_factory(mgr, monkeypatch, handler)
    _patch_tcp_probe(mgr, monkeypatch)

    cfg: dict[str, Any] = {
        "type": "streamable-http",
        "url": "https://mcp.example.com/sse",
        "headers": {},
    }
    capture = _AuthCapture()
    fired = asyncio.Event()
    t0 = time.monotonic()
    # The 401 surfaces as ``ExceptionGroup`` (anyio TaskGroup unwinding
    # wraps the underlying ``HTTPStatusError``); both are ``Exception``
    # subclasses. The point of the assertion is "the call returned/raised
    # within the timeout instead of hanging" — the type-discrimination
    # tests cover the carrier classification path.
    with pytest.raises(Exception):  # noqa: B017 — generic catch documents the no-hang assertion
        _run_on_loop(
            loop,
            mgr._connect_one_pool(
                ("user-1", "pool-srv"),
                cfg,
                "access-aaa",
                auth_capture=capture,
                auth_fired_event=fired,
            ),
            timeout=5,
        )
    elapsed = time.monotonic() - t0
    # Hard upper bound — the spike's happy path closed in ~0.05s, so a
    # 401 propagation that takes longer than 5s is the hang regression.
    assert elapsed < 5.0, f"_connect_one_pool 401 hang regression: {elapsed:.2f}s"
    # Carrier captured the 401 — the response hook fired before the
    # SDK propagated the failure, so the dispatcher would have the
    # auth signal even though discovery aborted.
    assert capture.status == 401, (
        f"401 mid-discovery did NOT surface to carrier; capture.status={capture.status}"
    )


# ---------------------------------------------------------------------------
# Listener fan-out — RFC §3.3 verification (Chunk 3)
# ---------------------------------------------------------------------------


def test_listener_identity_includes_user_id() -> None:
    """Listener identity is the ``(user_id, callback)`` tuple, not the
    callback alone — removing for user A leaves user B's registration
    intact even when the same callable was registered for both.

    Negative-test reproducer: revert ``add_listener`` /
    ``remove_listener`` to keep storage as a flat list of callables —
    this test fails because removing one registration accidentally
    removes BOTH (callable identity is shared).
    """
    mgr = MCPClientManager({})

    calls = []

    def _shared_cb() -> None:
        calls.append(time.monotonic())

    mgr.add_listener(_shared_cb, user_id="user-A")
    mgr.add_listener(_shared_cb, user_id="user-B")

    # Removing user-A's registration leaves user-B's intact.
    mgr.remove_listener(_shared_cb, user_id="user-A")

    # Static-path notify fires both registrations; with user-A removed,
    # only user-B fires now.
    mgr._notify_listeners()
    assert len(calls) == 1, (
        f"shared callback fired {len(calls)} times after removing user-A's "
        "registration; expected 1 (user-B's registration intact)"
    )

    # Now remove user-B's; static-path notify fires nobody.
    mgr.remove_listener(_shared_cb, user_id="user-B")
    calls.clear()
    mgr._notify_listeners()
    assert calls == [], "callback fired after BOTH user_id-keyed registrations removed"
