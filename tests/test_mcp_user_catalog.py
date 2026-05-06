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
    list_resources_response: dict[str, Any] | None = None,
    list_resource_templates_response: dict[str, Any] | None = None,
    list_prompts_response: dict[str, Any] | None = None,
    list_resources_seq: list[dict[str, Any]] | None = None,
    list_prompts_seq: list[dict[str, Any]] | None = None,
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
    if list_resources_response is None:
        list_resources_response = {
            "jsonrpc": "2.0",
            "id": 2,
            "result": {"resources": []},
        }
    if list_resource_templates_response is None:
        list_resource_templates_response = {
            "jsonrpc": "2.0",
            "id": 3,
            "result": {"resourceTemplates": []},
        }
    if list_prompts_response is None:
        list_prompts_response = {
            "jsonrpc": "2.0",
            "id": 4,
            "result": {"prompts": []},
        }
    counter = counter if counter is not None else [0]
    list_tools_seq = list_tools_seq or []
    list_tools_index = [0]
    list_resources_seq = list_resources_seq or []
    list_resources_index = [0]
    list_prompts_seq = list_prompts_seq or []
    list_prompts_index = [0]

    def _extract_id(body: str) -> Any:
        """Pull the JSON-RPC request id out of *body* via a tiny regex.

        The SDK assigns request ids sequentially per :class:`ClientSession`
        (initialize=0, list_tools=1, list_resources=2, ...). The mock
        responses MUST echo the same id so the SDK's correlator can route
        the response back to the awaiting future. Extracting from the
        request body lets a single response template work regardless of
        which list method ran first.
        """
        import json as _json
        import re as _re

        match = _re.search(r'"id"\s*:\s*(\d+)', body)
        if match:
            return _json.loads(match.group(1))
        return 1

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
        req_id = _extract_id(body)
        if "method" in body and '"initialize"' in body:
            response_payload = dict(init_response)
            response_payload["id"] = req_id
            return httpx.Response(
                200,
                headers={"content-type": "application/json", "mcp-session-id": "sess-1"},
                json=response_payload,
            )
        if '"resources/templates/list"' in body:
            payload = dict(list_resource_templates_response)
            payload["id"] = req_id
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                json=payload,
            )
        if '"resources/list"' in body:
            if list_resources_seq and list_resources_index[0] < len(list_resources_seq):
                payload = dict(list_resources_seq[list_resources_index[0]])
                list_resources_index[0] += 1
            else:
                payload = dict(list_resources_response)
            payload["id"] = req_id
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                json=payload,
            )
        if '"prompts/list"' in body:
            if list_prompts_seq and list_prompts_index[0] < len(list_prompts_seq):
                payload = dict(list_prompts_seq[list_prompts_index[0]])
                list_prompts_index[0] += 1
            else:
                payload = dict(list_prompts_response)
            payload["id"] = req_id
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                json=payload,
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
            payload = dict(list_tools_seq[list_tools_index[0]])
            list_tools_index[0] += 1
        else:
            payload = dict(list_tools_response)
        payload["id"] = req_id
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json=payload,
        )

    return _handler


def _list_tools_payload(tools: list[dict[str, Any]], req_id: int = 1) -> dict[str, Any]:
    """Build a minimal ``tools/list`` JSON-RPC result payload."""
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "result": {"tools": tools},
    }


def _list_resources_payload(resources: list[dict[str, Any]], req_id: int = 2) -> dict[str, Any]:
    """Build a minimal ``resources/list`` JSON-RPC result payload."""
    return {"jsonrpc": "2.0", "id": req_id, "result": {"resources": resources}}


def _list_prompts_payload(prompts: list[dict[str, Any]], req_id: int = 4) -> dict[str, Any]:
    """Build a minimal ``prompts/list`` JSON-RPC result payload."""
    return {"jsonrpc": "2.0", "id": req_id, "result": {"prompts": prompts}}


def _init_response_with_caps(
    *, resources: bool = False, prompts: bool = False, tools: bool = True
) -> dict[str, Any]:
    """Build an ``initialize`` response that advertises selected capabilities."""
    caps: dict[str, Any] = {}
    if tools:
        caps["tools"] = {"listChanged": False}
    if resources:
        caps["resources"] = {"listChanged": False}
    if prompts:
        caps["prompts"] = {"listChanged": False}
    return {
        "jsonrpc": "2.0",
        "id": 0,
        "result": {
            "protocolVersion": "2025-06-18",
            "capabilities": caps,
            "serverInfo": {"name": "fake", "version": "1"},
        },
    }


def _tool_spec(name: str, description: str = "") -> dict[str, Any]:
    return {
        "name": name,
        "description": description or f"do {name}",
        "inputSchema": {"type": "object", "properties": {}},
    }


def _resource_spec(uri: str, *, name: str = "") -> dict[str, Any]:
    return {"uri": uri, "name": name or uri, "mimeType": "text/plain"}


def _prompt_spec(name: str, *, description: str = "") -> dict[str, Any]:
    return {
        "name": name,
        "description": description or f"prompt {name}",
        "arguments": [],
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


# ---------------------------------------------------------------------------
# Phase 7b — listener-identity widening for resources and prompts
# ---------------------------------------------------------------------------


def test_resource_listener_identity_includes_user_id() -> None:
    """``add_resource_listener`` / ``remove_resource_listener`` use the
    ``(user_id, callback)`` tuple as identity, mirroring the tool path.

    Negative-test reproducer: revert the listener storage to a flat
    list of callables — this test fails because removing one
    registration accidentally removes both.
    """
    mgr = MCPClientManager({})
    calls = []

    def _shared_cb() -> None:
        calls.append(1)

    mgr.add_resource_listener(_shared_cb, user_id="user-A")
    mgr.add_resource_listener(_shared_cb, user_id="user-B")
    mgr.remove_resource_listener(_shared_cb, user_id="user-A")
    mgr._notify_resource_listeners()
    assert len(calls) == 1, (
        f"shared callback fired {len(calls)} times after removing user-A; expected 1"
    )

    mgr.remove_resource_listener(_shared_cb, user_id="user-B")
    calls.clear()
    mgr._notify_resource_listeners()
    assert calls == [], "callback fired after BOTH registrations removed"


def test_prompt_listener_identity_includes_user_id() -> None:
    """Mirror of :func:`test_resource_listener_identity_includes_user_id`
    for prompt listeners — Phase 7b widens both APIs."""
    mgr = MCPClientManager({})
    calls = []

    def _shared_cb() -> None:
        calls.append(1)

    mgr.add_prompt_listener(_shared_cb, user_id="user-A")
    mgr.add_prompt_listener(_shared_cb, user_id="user-B")
    mgr.remove_prompt_listener(_shared_cb, user_id="user-A")
    mgr._notify_prompt_listeners()
    assert len(calls) == 1, (
        f"shared callback fired {len(calls)} times after removing user-A; expected 1"
    )

    mgr.remove_prompt_listener(_shared_cb, user_id="user-B")
    calls.clear()
    mgr._notify_prompt_listeners()
    assert calls == [], "callback fired after BOTH registrations removed"


def test_user_resource_listeners_isolated_to_matching_user() -> None:
    """``_notify_user_resource_listeners(user_id)`` fires only matching
    user-id listeners + admin (None), never another user's listener.
    RFC §3.3 — pool catalog change is private to its owning user.
    """
    mgr = MCPClientManager({})
    user_a_calls = [0]
    user_b_calls = [0]
    admin_calls = [0]

    def _user_a() -> None:
        user_a_calls[0] += 1

    def _user_b() -> None:
        user_b_calls[0] += 1

    def _admin() -> None:
        admin_calls[0] += 1

    mgr.add_resource_listener(_user_a, user_id="user-A")
    mgr.add_resource_listener(_user_b, user_id="user-B")
    mgr.add_resource_listener(_admin)  # admin / None

    mgr._notify_user_resource_listeners("user-A")
    assert user_a_calls[0] == 1
    assert user_b_calls[0] == 0, (
        f"unrelated user-B listener fired {user_b_calls[0]} times — RFC §3.3 violation"
    )
    assert admin_calls[0] == 1


def test_user_prompt_listeners_isolated_to_matching_user() -> None:
    """Mirror for prompt listeners — see resource version above."""
    mgr = MCPClientManager({})
    user_a_calls = [0]
    user_b_calls = [0]
    admin_calls = [0]

    def _user_a() -> None:
        user_a_calls[0] += 1

    def _user_b() -> None:
        user_b_calls[0] += 1

    def _admin() -> None:
        admin_calls[0] += 1

    mgr.add_prompt_listener(_user_a, user_id="user-A")
    mgr.add_prompt_listener(_user_b, user_id="user-B")
    mgr.add_prompt_listener(_admin)

    mgr._notify_user_prompt_listeners("user-A")
    assert user_a_calls[0] == 1
    assert user_b_calls[0] == 0, (
        f"unrelated user-B listener fired {user_b_calls[0]} times — RFC §3.3 violation"
    )
    assert admin_calls[0] == 1


# ---------------------------------------------------------------------------
# Phase 7b — _rebuild_user_resource_map / _rebuild_user_prompt_map
# (sibling-cache invariant; loop-only writes; per-user isolation)
# ---------------------------------------------------------------------------


def test_rebuild_user_resource_map_isolates_users(running_loop_mgr: Any) -> None:
    """``_rebuild_user_resource_map`` populates user-A's resource map
    from user-A's pool entries only; user-B remains untouched. Empty
    rebuilds drop the user_id key from all three sibling dicts.
    """
    mgr, loop, _ = running_loop_mgr

    async def _seed() -> None:
        a_entry = await mgr._ensure_pool_entry(("user-A", "pool-srv"))
        a_entry.session = MagicMock()
        a_entry.resources = [
            {
                "uri": "res://a/1",
                "name": "r1",
                "description": "",
                "mimeType": "",
                "server": "pool-srv",
            },
        ]
        b_entry = await mgr._ensure_pool_entry(("user-B", "pool-srv"))
        b_entry.session = MagicMock()
        b_entry.resources = [
            {
                "uri": "res://b/1",
                "name": "r2",
                "description": "",
                "mimeType": "",
                "server": "pool-srv",
            },
        ]
        mgr._rebuild_user_resource_map("user-A")
        mgr._rebuild_user_resource_map("user-B")

    _run_on_loop(loop, _seed())

    a_map = mgr._user_resource_map.get("user-A") or {}
    b_map = mgr._user_resource_map.get("user-B") or {}
    assert "res://a/1" in a_map and "res://b/1" not in a_map
    assert "res://b/1" in b_map and "res://a/1" not in b_map
    assert mgr._user_resources["user-A"][0]["uri"] == "res://a/1"
    assert mgr._user_resources["user-B"][0]["uri"] == "res://b/1"

    # Empty rebuild after clearing entries drops the key from all three
    # dicts so idle users don't retain empty-list sentinels.
    async def _clear() -> None:
        mgr._user_pool_entries[("user-A", "pool-srv")].resources = None
        mgr._rebuild_user_resource_map("user-A")

    _run_on_loop(loop, _clear())
    assert "user-A" not in mgr._user_resource_map
    assert "user-A" not in mgr._user_resources
    assert "user-A" not in mgr._user_template_prefixes


def test_rebuild_user_resource_map_carries_template_prefixes(
    running_loop_mgr: Any,
) -> None:
    """Template entries (``r["template"] == True``) populate the
    per-user template-prefix dict; the longer-prefix-wins collision
    policy mirrors the static path."""
    mgr, loop, _ = running_loop_mgr

    async def _seed() -> None:
        entry = await mgr._ensure_pool_entry(("user-A", "pool-srv"))
        entry.session = MagicMock()
        entry.resources = [
            {
                "uri": "res://item/{id}",
                "name": "items",
                "description": "",
                "mimeType": "",
                "server": "pool-srv",
                "template": True,
            },
        ]
        mgr._rebuild_user_resource_map("user-A")

    _run_on_loop(loop, _seed())
    prefixes = mgr._user_template_prefixes.get("user-A") or {}
    assert "res://item/" in prefixes
    assert prefixes["res://item/"] == ("pool-srv", "res://item/{id}")


def test_rebuild_user_prompt_map_isolates_users(running_loop_mgr: Any) -> None:
    """Mirror of resource isolation test for prompts."""
    mgr, loop, _ = running_loop_mgr

    async def _seed() -> None:
        a_entry = await mgr._ensure_pool_entry(("user-A", "pool-srv"))
        a_entry.session = MagicMock()
        a_entry.prompts = [
            {
                "name": "mcp__pool-srv__a_prompt",
                "original_name": "a_prompt",
                "server": "pool-srv",
                "description": "",
                "arguments": [],
            }
        ]
        b_entry = await mgr._ensure_pool_entry(("user-B", "pool-srv"))
        b_entry.session = MagicMock()
        b_entry.prompts = [
            {
                "name": "mcp__pool-srv__b_prompt",
                "original_name": "b_prompt",
                "server": "pool-srv",
                "description": "",
                "arguments": [],
            }
        ]
        mgr._rebuild_user_prompt_map("user-A")
        mgr._rebuild_user_prompt_map("user-B")

    _run_on_loop(loop, _seed())
    assert "mcp__pool-srv__a_prompt" in (mgr._user_prompt_map.get("user-A") or {})
    assert "mcp__pool-srv__a_prompt" not in (mgr._user_prompt_map.get("user-B") or {})
    assert "mcp__pool-srv__b_prompt" in (mgr._user_prompt_map.get("user-B") or {})


def test_shutdown_clears_user_resource_and_prompt_state() -> None:
    """``shutdown`` releases all per-user catalog dicts (Phase 7b
    addition); without this clear, idle users would retain
    map+list+template_prefix sentinels across the manager lifecycle.
    """
    mgr = MCPClientManager({})
    # Seed all per-user state directly (no loop required for the dict
    # mutation; ``shutdown`` is also no-op when ``_loop`` is None).
    mgr._user_tool_map["user-A"] = {"x": ("s", "x")}
    mgr._user_tools["user-A"] = [{"function": {"name": "x"}}]
    mgr._user_resource_map["user-A"] = {"u": ("s", "u")}
    mgr._user_resources["user-A"] = [{"uri": "u"}]
    mgr._user_template_prefixes["user-A"] = {"u/": ("s", "u/{id}")}
    mgr._user_prompt_map["user-A"] = {"p": ("s", "p")}
    mgr._user_prompts["user-A"] = [{"name": "p"}]

    mgr.shutdown()
    assert mgr._user_tool_map == {}
    assert mgr._user_tools == {}
    assert mgr._user_resource_map == {}
    assert mgr._user_resources == {}
    assert mgr._user_template_prefixes == {}
    assert mgr._user_prompt_map == {}
    assert mgr._user_prompts == {}


# ---------------------------------------------------------------------------
# Phase 7b — public read API widening (get_resources / get_prompts /
# is_mcp_prompt / *_count_for_user / _match_template)
# ---------------------------------------------------------------------------


def test_get_resources_user_id_none_returns_static_only() -> None:
    """``get_resources()`` with no kwargs returns the static-only
    catalog — preserves the pre-Phase-7b contract for legacy callers
    (admin endpoints, boot-time logging)."""
    mgr = MCPClientManager({})
    static_res = {"uri": "res://static/1", "server": "static-srv"}
    pool_res = {"uri": "res://pool/1", "server": "pool-srv"}
    mgr._resources = [static_res]
    mgr._user_resources["user-A"] = [pool_res]

    assert mgr.get_resources() == [static_res]
    # Sibling user_id-less default doesn't reach into per-user state.
    assert mgr.get_resources(None) == [static_res]


def test_get_resources_user_id_returns_per_user_first_then_static() -> None:
    """Per-user-first ordering (scope decision 0.1): pool resources
    come BEFORE static in the merged list. The same user with no pool
    entries sees just the static catalog."""
    mgr = MCPClientManager({})
    static_res = {"uri": "res://static/1", "server": "static-srv"}
    pool_res = {"uri": "res://pool/1", "server": "pool-srv"}
    mgr._resources = [static_res]
    mgr._user_resources["user-A"] = [pool_res]

    merged = mgr.get_resources("user-A")
    assert merged[0]["uri"] == "res://pool/1"  # per-user-first
    assert merged[1]["uri"] == "res://static/1"

    # User-B has no pool entries → static-only merged view.
    assert mgr.get_resources("user-B") == [static_res]


def test_get_prompts_user_id_returns_per_user_first_then_static() -> None:
    """Mirror of resource version for prompts."""
    mgr = MCPClientManager({})
    static_p = {"name": "mcp__static-srv__p1", "server": "static-srv"}
    pool_p = {"name": "mcp__pool-srv__p1", "server": "pool-srv"}
    mgr._prompts = [static_p]
    mgr._user_prompts["user-A"] = [pool_p]

    merged = mgr.get_prompts("user-A")
    assert merged[0]["name"] == "mcp__pool-srv__p1"
    assert merged[1]["name"] == "mcp__static-srv__p1"
    assert mgr.get_prompts() == [static_p]


def test_is_mcp_prompt_user_id_extends_lookup_to_pool() -> None:
    """``is_mcp_prompt(name, user_id="...")`` returns True when the
    name lives in either the static map OR the user's per-user prompt
    map. Without the kwarg, only the static map is consulted."""
    mgr = MCPClientManager({})
    mgr._prompt_map["mcp__static-srv__p"] = ("static-srv", "p")
    mgr._user_prompt_map["user-A"] = {"mcp__pool-srv__p": ("pool-srv", "p")}

    assert mgr.is_mcp_prompt("mcp__static-srv__p") is True
    assert mgr.is_mcp_prompt("mcp__pool-srv__p") is False  # no user_id → static only
    assert mgr.is_mcp_prompt("mcp__pool-srv__p", user_id="user-A") is True
    assert mgr.is_mcp_prompt("mcp__pool-srv__p", user_id="user-B") is False


def test_resource_count_for_user_includes_pool() -> None:
    """``resource_count_for_user`` reports static + the user's pool;
    the legacy ``resource_count`` property stays static-only (admin
    endpoints rely on the property contract)."""
    mgr = MCPClientManager({})
    mgr._resources = [{"uri": "res://static/1"}, {"uri": "res://static/2"}]
    mgr._user_resources["user-A"] = [{"uri": "res://pool/1"}]

    assert mgr.resource_count == 2  # process-global, unchanged
    assert mgr.resource_count_for_user(None) == 2
    assert mgr.resource_count_for_user("user-A") == 3
    assert mgr.resource_count_for_user("user-B") == 2  # no pool entries


def test_prompt_count_for_user_includes_pool() -> None:
    """Mirror of resource version for prompts."""
    mgr = MCPClientManager({})
    mgr._prompts = [{"name": "p1"}]
    mgr._user_prompts["user-A"] = [{"name": "pa"}, {"name": "pb"}]

    assert mgr.prompt_count == 1
    assert mgr.prompt_count_for_user("user-A") == 3
    assert mgr.prompt_count_for_user("user-B") == 1


def test_match_template_per_user_first_resolution() -> None:
    """Per-user templates win over static templates at the same prefix
    (scope decision 0.1). Without ``user_id``, only static prefixes
    are consulted."""
    mgr = MCPClientManager({})
    mgr._template_prefixes["res://item/"] = ("static-srv", "res://item/{id}")
    mgr._user_template_prefixes["user-A"] = {
        "res://item/": ("pool-srv", "res://item/{id}"),
    }

    # Pre-Phase-7b call (no user_id) keeps static behaviour.
    assert mgr._match_template("res://item/42") == ("static-srv", "res://item/{id}")
    # User-A sees the per-user template first.
    assert mgr._match_template("res://item/42", user_id="user-A") == (
        "pool-srv",
        "res://item/{id}",
    )
    # User-B has no per-user templates → falls through to static.
    assert mgr._match_template("res://item/42", user_id="user-B") == (
        "static-srv",
        "res://item/{id}",
    )


def test_match_template_longest_prefix_within_user_scope() -> None:
    """Within the per-user index, longest-prefix-wins matching applies
    just like the static path. The static fall-through MUST NOT win
    when the per-user index already produced a match."""
    mgr = MCPClientManager({})
    mgr._template_prefixes["res://"] = ("static-srv", "res://{rest}")
    mgr._user_template_prefixes["user-A"] = {
        "res://item/": ("pool-srv", "res://item/{id}"),
    }
    # User-A's longer-prefix template wins even though static has a
    # shorter prefix that would also match.
    assert mgr._match_template("res://item/42", user_id="user-A") == (
        "pool-srv",
        "res://item/{id}",
    )


# ---------------------------------------------------------------------------
# Phase 7b — discovery via REAL streamablehttp_client + httpx.MockTransport
# (invariant 14: boundary-crossing code drives through real SDK plumbing).
# ---------------------------------------------------------------------------


def test_pool_resource_discovery_on_connect_via_real_streamable_http(
    running_loop_mgr: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_connect_one_pool`` discovers the user's resource catalog after
    ``initialize()`` (capability-gated) and stores it on the entry plus
    the per-user resource map.

    Negative-test reproducer: revert the discovery + rebuild block in
    ``_connect_one_pool`` (drop the ``list_resources`` call and the
    ``_rebuild_user_resource_map`` invocation): this test fails because
    ``entry.resources`` stays None and ``_user_resource_map`` never
    gains the user_id key.
    """
    mgr, loop, _ = running_loop_mgr
    handler = _make_jsonrpc_handler(
        init_response=_init_response_with_caps(resources=True),
        list_resources_response=_list_resources_payload(
            [_resource_spec("res://test/1"), _resource_spec("res://test/2")]
        ),
    )
    _build_mock_transport_factory(mgr, monkeypatch, handler)
    _patch_tcp_probe(mgr, monkeypatch)

    entry = _connect_pool(mgr, loop, user_id="user-1", server_name="pool-srv")

    assert entry.session is not None
    assert entry.supports_resources is True
    assert entry.resources is not None
    uris = {r["uri"] for r in entry.resources}
    assert uris == {"res://test/1", "res://test/2"}
    user_map = mgr._user_resource_map.get("user-1")
    assert user_map is not None
    assert user_map["res://test/1"] == ("pool-srv", "res://test/1")
    # Per-user resource list reflects discovery.
    assert {r["uri"] for r in mgr._user_resources["user-1"]} == {"res://test/1", "res://test/2"}


def test_pool_prompt_discovery_on_connect_via_real_streamable_http(
    running_loop_mgr: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_connect_one_pool`` discovers the user's prompt catalog
    capability-gated. Mirror of resource discovery test."""
    mgr, loop, _ = running_loop_mgr
    handler = _make_jsonrpc_handler(
        init_response=_init_response_with_caps(prompts=True),
        list_prompts_response=_list_prompts_payload(
            [_prompt_spec("greet"), _prompt_spec("summary")]
        ),
    )
    _build_mock_transport_factory(mgr, monkeypatch, handler)
    _patch_tcp_probe(mgr, monkeypatch)

    entry = _connect_pool(mgr, loop, user_id="user-1", server_name="pool-srv")

    assert entry.session is not None
    assert entry.supports_prompts is True
    assert entry.prompts is not None
    names = {p["name"] for p in entry.prompts}
    assert names == {"mcp__pool-srv__greet", "mcp__pool-srv__summary"}
    user_map = mgr._user_prompt_map.get("user-1")
    assert user_map is not None
    assert user_map["mcp__pool-srv__greet"] == ("pool-srv", "greet")
    # is_mcp_prompt with user_id surfaces the discovered prompt.
    assert mgr.is_mcp_prompt("mcp__pool-srv__greet", user_id="user-1") is True
    assert mgr.is_mcp_prompt("mcp__pool-srv__greet") is False


def test_pool_discovery_skips_resources_and_prompts_without_capability(
    running_loop_mgr: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the server does NOT advertise resources / prompts in its
    initialize response, ``_connect_one_pool`` MUST skip the discovery
    round-trips entirely. ``entry.supports_resources`` /
    ``supports_prompts`` stay False; the catalog stays None.

    Negative-test: drop the capability gates in ``_connect_one_pool``
    (force unconditional discovery) — this test fails because the
    handler returns no resources/list response, the SDK times out, and
    the connect coroutine raises.
    """
    mgr, loop, _ = running_loop_mgr
    # init_response defaults declare ONLY tools.
    handler = _make_jsonrpc_handler()
    _build_mock_transport_factory(mgr, monkeypatch, handler)
    _patch_tcp_probe(mgr, monkeypatch)

    entry = _connect_pool(mgr, loop, user_id="user-1", server_name="pool-srv")

    assert entry.session is not None
    assert entry.supports_resources is False
    assert entry.supports_prompts is False
    assert entry.resources is None
    assert entry.prompts is None
    # No per-user resource/prompt map entry for this user.
    assert "user-1" not in mgr._user_resource_map
    assert "user-1" not in mgr._user_prompt_map


def test_pool_resource_user_isolation(
    running_loop_mgr: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two users connecting to the same resource-bearing server have
    independent per-user resource maps; one user never sees another's
    catalog."""
    mgr, loop, _ = running_loop_mgr
    handler = _make_jsonrpc_handler(
        init_response=_init_response_with_caps(resources=True),
        list_resources_response=_list_resources_payload([_resource_spec("res://shared")]),
    )
    _build_mock_transport_factory(mgr, monkeypatch, handler)
    _patch_tcp_probe(mgr, monkeypatch)

    _connect_pool(mgr, loop, user_id="user-1", server_name="pool-srv")
    _connect_pool(mgr, loop, user_id="user-2", server_name="pool-srv")

    map_1 = mgr._user_resource_map.get("user-1") or {}
    map_2 = mgr._user_resource_map.get("user-2") or {}
    assert map_1 is not map_2
    assert "res://shared" in map_1 and "res://shared" in map_2


def test_pool_prompt_user_isolation(running_loop_mgr: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Mirror for prompts — independent per-user prompt maps."""
    mgr, loop, _ = running_loop_mgr
    handler = _make_jsonrpc_handler(
        init_response=_init_response_with_caps(prompts=True),
        list_prompts_response=_list_prompts_payload([_prompt_spec("shared_prompt")]),
    )
    _build_mock_transport_factory(mgr, monkeypatch, handler)
    _patch_tcp_probe(mgr, monkeypatch)

    _connect_pool(mgr, loop, user_id="user-1", server_name="pool-srv")
    _connect_pool(mgr, loop, user_id="user-2", server_name="pool-srv")

    map_1 = mgr._user_prompt_map.get("user-1") or {}
    map_2 = mgr._user_prompt_map.get("user-2") or {}
    assert map_1 is not map_2
    assert "mcp__pool-srv__shared_prompt" in map_1
    assert "mcp__pool-srv__shared_prompt" in map_2


# ---------------------------------------------------------------------------
# Phase 7b — refresh on notification (resources/prompts list_changed)
# ---------------------------------------------------------------------------


def test_refresh_pool_server_resources_isolates_user(
    running_loop_mgr: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_refresh_pool_server_resources`` refreshes THIS user's catalog only,
    leaving the static path and other users' catalogs untouched.

    Negative-test: revert the refresh to call ``_rebuild_resources()``
    (the static path) instead of ``_rebuild_user_resource_map(user_id)``;
    this test fails because the static map is mutated and the user map
    doesn't pick up the rotated catalog.
    """
    mgr, loop, _ = running_loop_mgr
    handler = _make_jsonrpc_handler(
        init_response=_init_response_with_caps(resources=True),
        list_resources_seq=[
            _list_resources_payload([_resource_spec("res://v1")]),
            _list_resources_payload([_resource_spec("res://v2")]),
        ],
    )
    _build_mock_transport_factory(mgr, monkeypatch, handler)
    _patch_tcp_probe(mgr, monkeypatch)

    # Pre-seed a static-path resource so we can assert it's untouched.
    from turnstone.core.mcp_client import StaticServerState

    sentinel_static = StaticServerState(name="static-srv", session=MagicMock())
    sentinel_static.resources = [
        {"uri": "res://static/keep", "server": "static-srv", "name": "", "mimeType": ""}
    ]
    mgr._static_servers["static-srv"] = sentinel_static
    mgr._resource_map["res://static/keep"] = ("static-srv", "res://static/keep")

    # Pre-seed user-2's pool entry so we can assert it stays untouched.
    async def _seed_other_user() -> None:
        entry = await mgr._ensure_pool_entry(("user-2", "pool-srv"))
        entry.session = MagicMock()
        entry.supports_resources = True
        entry.resources = [
            {"uri": "res://user2_only", "server": "pool-srv", "name": "", "mimeType": ""}
        ]
        mgr._rebuild_user_resource_map("user-2")

    _run_on_loop(loop, _seed_other_user())
    assert "res://user2_only" in (mgr._user_resource_map.get("user-2") or {})

    _connect_pool(mgr, loop, user_id="user-1", server_name="pool-srv")
    assert "res://v1" in (mgr._user_resource_map.get("user-1") or {})

    added_removed = _run_on_loop(loop, mgr._refresh_pool_server_resources(("user-1", "pool-srv")))
    added, removed = added_removed
    assert added == ["res://v2"]
    assert removed == ["res://v1"]

    # User-1 sees the new resource.
    assert "res://v2" in (mgr._user_resource_map.get("user-1") or {})
    assert "res://v1" not in (mgr._user_resource_map.get("user-1") or {})
    # Static path untouched — invariant 1.
    assert mgr._resource_map == {"res://static/keep": ("static-srv", "res://static/keep")}
    # User-2's pool catalog untouched.
    assert mgr._user_resource_map["user-2"] == {
        "res://user2_only": ("pool-srv", "res://user2_only")
    }


def test_refresh_pool_server_prompts_isolates_user(
    running_loop_mgr: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mirror for prompts — refresh isolates to one user."""
    mgr, loop, _ = running_loop_mgr
    handler = _make_jsonrpc_handler(
        init_response=_init_response_with_caps(prompts=True),
        list_prompts_seq=[
            _list_prompts_payload([_prompt_spec("v1")]),
            _list_prompts_payload([_prompt_spec("v2")]),
        ],
    )
    _build_mock_transport_factory(mgr, monkeypatch, handler)
    _patch_tcp_probe(mgr, monkeypatch)

    # Pre-seed a static-path prompt and another user's pool prompt.
    from turnstone.core.mcp_client import StaticServerState

    sentinel = StaticServerState(name="static-srv", session=MagicMock())
    sentinel.prompts = [
        {
            "name": "mcp__static-srv__keep",
            "original_name": "keep",
            "server": "static-srv",
            "description": "",
            "arguments": [],
        }
    ]
    mgr._static_servers["static-srv"] = sentinel
    mgr._prompt_map["mcp__static-srv__keep"] = ("static-srv", "keep")

    async def _seed_other_user() -> None:
        entry = await mgr._ensure_pool_entry(("user-2", "pool-srv"))
        entry.session = MagicMock()
        entry.supports_prompts = True
        entry.prompts = [
            {
                "name": "mcp__pool-srv__user2",
                "original_name": "user2",
                "server": "pool-srv",
                "description": "",
                "arguments": [],
            }
        ]
        mgr._rebuild_user_prompt_map("user-2")

    _run_on_loop(loop, _seed_other_user())

    _connect_pool(mgr, loop, user_id="user-1", server_name="pool-srv")
    assert "mcp__pool-srv__v1" in (mgr._user_prompt_map.get("user-1") or {})

    added_removed = _run_on_loop(loop, mgr._refresh_pool_server_prompts(("user-1", "pool-srv")))
    added, removed = added_removed
    assert added == ["mcp__pool-srv__v2"]
    assert removed == ["mcp__pool-srv__v1"]

    assert mgr._prompt_map == {"mcp__static-srv__keep": ("static-srv", "keep")}
    assert mgr._user_prompt_map["user-2"] == {"mcp__pool-srv__user2": ("pool-srv", "user2")}


def test_refresh_pool_server_resources_skips_when_capability_unset(
    running_loop_mgr: Any,
) -> None:
    """If ``entry.supports_resources`` is False, the refresh returns
    ``([], [])`` without making any list calls — no SDK round-trip."""
    mgr, loop, _ = running_loop_mgr

    async def _seed() -> None:
        entry = await mgr._ensure_pool_entry(("user-1", "pool-srv"))
        entry.session = MagicMock()  # session present but capability unset
        entry.supports_resources = False

    _run_on_loop(loop, _seed())
    added, removed = _run_on_loop(loop, mgr._refresh_pool_server_resources(("user-1", "pool-srv")))
    assert added == []
    assert removed == []


def test_refresh_pool_server_prompts_skips_when_capability_unset(
    running_loop_mgr: Any,
) -> None:
    """Mirror for prompts."""
    mgr, loop, _ = running_loop_mgr

    async def _seed() -> None:
        entry = await mgr._ensure_pool_entry(("user-1", "pool-srv"))
        entry.session = MagicMock()
        entry.supports_prompts = False

    _run_on_loop(loop, _seed())
    added, removed = _run_on_loop(loop, mgr._refresh_pool_server_prompts(("user-1", "pool-srv")))
    assert added == []
    assert removed == []


# ---------------------------------------------------------------------------
# Phase 7b — symmetric eviction (resources/prompts cleared + listeners fire)
# ---------------------------------------------------------------------------


def test_eviction_clears_resource_and_prompt_catalogs_and_fires_listeners(
    running_loop_mgr: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_evict_session`` clears ``entry.resources`` and ``entry.prompts``,
    rebuilds both per-user maps (drops the now-empty entries), and
    fires the matching user-keyed + admin listeners for ALL three
    catalogs (tools, resources, prompts).

    Negative-test: drop the resource/prompt cleanup additions in
    ``_evict_session``: this test fails because the user-resource map
    keeps the evicted URIs and no resource listener fires.
    """
    mgr, loop, _ = running_loop_mgr
    handler = _make_jsonrpc_handler(
        init_response=_init_response_with_caps(resources=True, prompts=True),
        list_resources_response=_list_resources_payload([_resource_spec("res://r/1")]),
        list_prompts_response=_list_prompts_payload([_prompt_spec("p1")]),
    )
    _build_mock_transport_factory(mgr, monkeypatch, handler)
    _patch_tcp_probe(mgr, monkeypatch)

    _connect_pool(mgr, loop, user_id="user-1", server_name="pool-srv")
    assert "res://r/1" in (mgr._user_resource_map.get("user-1") or {})
    assert "mcp__pool-srv__p1" in (mgr._user_prompt_map.get("user-1") or {})

    res_calls = [0]
    prompt_calls = [0]
    other_res_calls = [0]
    other_prompt_calls = [0]

    def _user_res_cb() -> None:
        res_calls[0] += 1

    def _user_prompt_cb() -> None:
        prompt_calls[0] += 1

    def _other_res_cb() -> None:
        other_res_calls[0] += 1

    def _other_prompt_cb() -> None:
        other_prompt_calls[0] += 1

    mgr.add_resource_listener(_user_res_cb, user_id="user-1")
    mgr.add_resource_listener(_other_res_cb, user_id="user-2")
    mgr.add_prompt_listener(_user_prompt_cb, user_id="user-1")
    mgr.add_prompt_listener(_other_prompt_cb, user_id="user-2")

    mgr._evict_session(("user-1", "pool-srv"))

    entry = mgr._user_pool_entries[("user-1", "pool-srv")]
    assert entry.session is None
    assert entry.tools is None
    assert entry.resources is None
    assert entry.prompts is None
    # Per-user maps drop the evicted entries.
    assert "user-1" not in mgr._user_resource_map
    assert "user-1" not in mgr._user_prompt_map
    # Listeners fire for the evicted user but not for the unrelated user.
    assert res_calls[0] == 1
    assert prompt_calls[0] == 1
    assert other_res_calls[0] == 0, "unrelated user-2 resource listener fired — RFC §3.3 violation"
    assert other_prompt_calls[0] == 0, "unrelated user-2 prompt listener fired — RFC §3.3 violation"


def test_close_pool_entry_if_idle_clears_resource_and_prompt_catalogs(
    running_loop_mgr: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LRU/TTL eviction (``_close_pool_entry_if_idle``) symmetric
    cleanup for resources & prompts. Bug-pair to the tools-only
    cleanup added in Phase 7 round-2."""
    mgr, loop, _ = running_loop_mgr
    handler = _make_jsonrpc_handler(
        init_response=_init_response_with_caps(resources=True, prompts=True),
        list_resources_response=_list_resources_payload([_resource_spec("res://r/1")]),
        list_prompts_response=_list_prompts_payload([_prompt_spec("p1")]),
    )
    _build_mock_transport_factory(mgr, monkeypatch, handler)
    _patch_tcp_probe(mgr, monkeypatch)

    _connect_pool(mgr, loop, user_id="user-1", server_name="pool-srv")
    key = ("user-1", "pool-srv")

    res_calls = [0]
    prompt_calls = [0]

    def _res_cb() -> None:
        res_calls[0] += 1

    def _prompt_cb() -> None:
        prompt_calls[0] += 1

    mgr.add_resource_listener(_res_cb, user_id="user-1")
    mgr.add_prompt_listener(_prompt_cb, user_id="user-1")

    _run_on_loop(loop, mgr._close_pool_entry_if_idle(key))

    # Entry fully removed.
    assert key not in mgr._user_pool_entries
    # Per-user catalogs cleared.
    assert "user-1" not in mgr._user_resource_map
    assert "user-1" not in mgr._user_prompt_map
    # Listeners fired exactly once.
    assert res_calls[0] == 1
    assert prompt_calls[0] == 1


def test_reconnect_after_eviction_repopulates_resource_and_prompt_catalogs(
    running_loop_mgr: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After eviction, the next ``_connect_one_pool`` re-populates the
    resource and prompt catalogs from the SDK. Bug-class: an extra
    glue layer would only repopulate tools."""
    mgr, loop, _ = running_loop_mgr
    handler = _make_jsonrpc_handler(
        init_response=_init_response_with_caps(resources=True, prompts=True),
        list_resources_seq=[
            _list_resources_payload([_resource_spec("res://a")]),
            _list_resources_payload([_resource_spec("res://b")]),
        ],
        list_prompts_seq=[
            _list_prompts_payload([_prompt_spec("p_a")]),
            _list_prompts_payload([_prompt_spec("p_b")]),
        ],
    )
    _build_mock_transport_factory(mgr, monkeypatch, handler)
    _patch_tcp_probe(mgr, monkeypatch)

    _connect_pool(mgr, loop, user_id="user-1", server_name="pool-srv")
    assert "res://a" in (mgr._user_resource_map.get("user-1") or {})
    assert "mcp__pool-srv__p_a" in (mgr._user_prompt_map.get("user-1") or {})

    mgr._evict_session(("user-1", "pool-srv"))
    assert "user-1" not in mgr._user_resource_map
    assert "user-1" not in mgr._user_prompt_map

    _connect_pool(mgr, loop, user_id="user-1", server_name="pool-srv")
    # New catalogs reflect the rotated payload.
    assert "res://b" in (mgr._user_resource_map.get("user-1") or {})
    assert "res://a" not in (mgr._user_resource_map.get("user-1") or {})
    assert "mcp__pool-srv__p_b" in (mgr._user_prompt_map.get("user-1") or {})
    assert "mcp__pool-srv__p_a" not in (mgr._user_prompt_map.get("user-1") or {})
