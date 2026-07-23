"""Boot-epoch staleness signal on the node-global SSE stream (#881).

The global ring buffer and its event counter are process-local: unlike the
per-ws lane (whose counter ``_seed_event_id_from_storage`` seeds from
``MAX(conversations.event_id)``), ``global_event_id_holder`` reboots at 0
with the process.  A bare integer cursor therefore cannot prove which boot
minted it — after a restart it is first "ahead" of the reborn ring (empty
replay slice) and then aliases into the new id space as the counter
re-grows, both of which used to draw ``replay_ok`` and silently skip the
restart boundary.

The fix stamps every global SSE ``id:`` as ``"{boot_epoch}-{counter}"``
and treats any cursor that does not carry the live epoch as stale,
answering ``replay_truncated`` (``reason="boot_epoch"``) plus the
``node_snapshot`` recovery floor.  These tests walk the reconnect matrix
at the :func:`turnstone.server.global_events_sse` boundary:

  cursor ∈ {absent, same-epoch, stale-epoch, legacy bare-int, garbage,
            negative, forged-against-empty-ring}
  ×  ring ∈ {empty, covers cursor, evicted past cursor}

The browser half (app.js capture/presentation) is pinned in
``test_app_js.py``; the end-to-end restart loop is
``scripts/recovery_e2e.py --scenario roster-restart``.
"""

from __future__ import annotations

import asyncio
import collections
import json
import queue
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace as SimpleNS
from typing import Any

import pytest
from starlette.requests import Request

import turnstone.server as server_mod

EPOCH = "0badf00d"  # test-pinned boot epoch (hex, like secrets.token_hex(4))


def _make_app_state(
    *,
    buffered: list[tuple[int, dict[str, Any]]] | None = None,
    epoch: str = EPOCH,
) -> SimpleNS:
    """Minimal ``app.state`` for the global SSE handler.

    Real lock / deque / list so registration and slicing run the
    production code paths; only the snapshot builder is stubbed — the
    replay-branch decisions under test never depend on its composition.
    The real ``_build_node_snapshot`` is exercised end-to-end by
    ``scripts/recovery_e2e.py --scenario roster-restart`` (snapshot
    membership + evict); its full field projection has no direct unit
    test today (``test_console.py`` covers only the CONSUMER side,
    feeding hand-built snapshot dicts to the collector).
    """
    buf: collections.deque[tuple[int, dict[str, Any]]] = collections.deque(maxlen=50)
    for item in buffered or []:
        buf.append(item)
    return SimpleNS(
        node_id="node-under-test",
        global_listeners=[],
        global_listeners_lock=threading.Lock(),
        global_event_buffer=buf,
        global_boot_epoch=epoch,
        sse_executor=None,  # live loop is never entered by these tests
    )


def _fake_request(
    app_state: SimpleNS,
    *,
    headers: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
) -> Request:
    """ASGI-scope-honest request carrying a service-scoped principal."""
    header_list = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    query_string = "&".join(f"{k}={v}" for k, v in query.items()).encode() if query else b""
    scope = {
        "type": "http",
        "method": "GET",
        "headers": header_list,
        "path": "/v1/api/events/global",
        "raw_path": b"/v1/api/events/global",
        "query_string": query_string,
        "app": SimpleNS(state=app_state),
        "state": {"auth_result": SimpleNS(scopes=["service"])},
    }

    async def _recv() -> dict[str, Any]:  # noqa: RUF029 — async signature required
        return {"type": "http.disconnect"}

    return Request(scope, receive=_recv)


_SNAPSHOT_STUB = {"type": "node_snapshot", "node_id": "node-under-test", "workstreams": []}


def _drain(
    monkeypatch: pytest.MonkeyPatch,
    *,
    buffered: list[tuple[int, dict[str, Any]]] | None = None,
    headers: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
    max_yields: int = 8,
    epoch: str = EPOCH,
) -> list[dict[str, Any]]:
    """Run the handler and collect its pre-live yields as raw dicts.

    ``max_yields`` must not exceed the pre-live yield count for the
    branch under test + 1 — the live loop blocks on an executor draw,
    so every test enumerates its expected frames and stops short.
    """
    monkeypatch.setattr(server_mod, "_build_node_snapshot", lambda _s: dict(_SNAPSHOT_STUB))
    app_state = _make_app_state(buffered=buffered, epoch=epoch)
    req = _fake_request(app_state, headers=headers, query=query)

    async def _run() -> list[dict[str, Any]]:
        # Guard against a miscounted ``max_yields`` reaching the live
        # loop (which blocks on an executor draw and would hang the
        # test + leak the draw thread) — fail fast and visibly instead.
        async with asyncio.timeout(10):
            # ``Any``: the endpoint is annotated ``-> Response``; the SSE
            # subtype's ``body_iterator`` is what the drain consumes.
            resp: Any = await server_mod.global_events_sse(req)
            out: list[dict[str, Any]] = []
            async for chunk in resp.body_iterator:
                out.append(chunk)
                if len(out) >= max_yields:
                    break
            await resp.body_iterator.aclose()
            return out

    return asyncio.run(_run())


def _data_frames(yields: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """JSON-decoded ``data:`` payloads, in yield order."""
    return [json.loads(y["data"]) for y in yields if "data" in y]


def _types(yields: list[dict[str, Any]]) -> list[str]:
    return [d.get("type", "") for d in _data_frames(yields)]


def _ev(eid: int, **extra: Any) -> tuple[int, dict[str, Any]]:
    """A buffered ring entry the way ``_global_fanout_thread`` stores it:
    the event dict carries ``_event_id`` and the tuple repeats the id."""
    return eid, {"type": "ws_state", "ws_id": f"ws-{eid}", "_event_id": eid, **extra}


# ---------------------------------------------------------------------------
# Fresh connect (no cursor)
# ---------------------------------------------------------------------------


def test_fresh_connect_gets_snapshot_no_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    yields = _drain(monkeypatch, buffered=[_ev(1)], max_yields=2)
    assert "retry" in yields[0]
    assert _types(yields) == ["node_snapshot"]


# ---------------------------------------------------------------------------
# Same-epoch cursors — ring logic must behave exactly as before
# ---------------------------------------------------------------------------


def test_same_epoch_cursor_replays_slice_without_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    yields = _drain(
        monkeypatch,
        buffered=[_ev(1), _ev(2), _ev(3)],
        headers={"Last-Event-ID": f"{EPOCH}-1"},
        max_yields=3,
    )
    frames = _data_frames(yields)
    assert [f["ws_id"] for f in frames] == ["ws-2", "ws-3"]
    assert "node_snapshot" not in _types(yields)
    assert "replay_truncated" not in _types(yields)


def test_replayed_and_live_ids_are_epoch_tagged(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every ``id:`` on the wire carries the live epoch — the browser
    echoes it verbatim on native reconnect, which is what lets the
    server prove cursor provenance without any client cooperation."""
    yields = _drain(
        monkeypatch,
        buffered=[_ev(1), _ev(2)],
        headers={"Last-Event-ID": f"{EPOCH}-0"},
        max_yields=3,
    )
    ids = [y["id"] for y in yields if "id" in y]
    assert ids == [f"{EPOCH}-1", f"{EPOCH}-2"]
    # And the internal ``_event_id`` never leaks onto the wire.
    assert all("_event_id" not in f for f in _data_frames(yields))


def test_same_epoch_cursor_at_head_is_caught_up_replay_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cursor at the newest id is the normal caught-up reconnect:
    nothing to replay, no snapshot, and crucially no false truncated.

    Asserted POSITIVELY: a sentinel live event is queued to the
    registered listener before draining, so the drain runs through the
    live loop and the sentinel must be the FIRST data frame — a spurious
    envelope or snapshot would land ahead of it.  (A drain truncated at
    the always-first ``retry`` frame asserts nothing — round-2 review.)
    """
    monkeypatch.setattr(server_mod, "_build_node_snapshot", lambda _s: dict(_SNAPSHOT_STUB))
    app_state = _make_app_state(buffered=[_ev(4), _ev(5)])
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="boot-epoch-test")
    app_state.sse_executor = executor
    req = _fake_request(app_state, headers={"Last-Event-ID": f"{EPOCH}-5"})

    async def _run() -> list[dict[str, Any]]:
        async with asyncio.timeout(10):
            resp: Any = await server_mod.global_events_sse(req)
            # Registered by handler-await time; the live sentinel the
            # drain below must reach as its first data frame.
            app_state.global_listeners[0].put_nowait(
                {"type": "ws_state", "ws_id": "sentinel", "_event_id": 6}
            )
            out: list[dict[str, Any]] = []
            async for chunk in resp.body_iterator:
                out.append(chunk)
                if len(out) >= 2:
                    break
            await resp.body_iterator.aclose()
            return out

    try:
        yields = asyncio.run(_run())
    finally:
        executor.shutdown(wait=True)
    frames = _data_frames(yields)
    assert [f.get("ws_id") for f in frames] == ["sentinel"]


def test_same_epoch_ring_miss_is_truncated_with_honest_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    yields = _drain(
        monkeypatch,
        buffered=[_ev(10), _ev(11)],
        headers={"Last-Event-ID": f"{EPOCH}-3"},
        max_yields=3,
    )
    envelope, snapshot = _data_frames(yields)
    assert envelope["type"] == "replay_truncated"
    assert envelope["reason"] == "ring_evicted"
    assert envelope["lost_count"] == 6  # ids 4..9 died with the ring
    assert envelope["earliest_available_id"] == 10
    assert snapshot["type"] == "node_snapshot"


# ---------------------------------------------------------------------------
# Stale / foreign cursors — the #881 class
# ---------------------------------------------------------------------------


def _assert_boot_epoch_truncated(yields: list[dict[str, Any]]) -> None:
    """Envelope (reason=boot_epoch, no invented counts) then snapshot,
    and no replay slice leaked around them."""
    frames = _data_frames(yields)
    assert [f["type"] for f in frames] == ["replay_truncated", "node_snapshot"]
    envelope = frames[0]
    assert envelope["reason"] == "boot_epoch"
    assert "lost_count" not in envelope
    assert "earliest_available_id" not in envelope


def test_prior_boot_cursor_against_regrown_ring_is_truncated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE restart-aliasing case: the reborn counter has re-grown past
    the stale cursor, so pre-#881 the ring sliced from it as if the ids
    were contiguous across the boot — silently skipping the restart
    boundary.  The epoch mismatch must win over the plausible slice."""
    yields = _drain(
        monkeypatch,
        buffered=[_ev(1), _ev(2), _ev(3)],
        headers={"Last-Event-ID": "deadbeef-2"},
        max_yields=3,
    )
    _assert_boot_epoch_truncated(yields)


def test_legacy_bare_int_cursor_on_empty_ring_is_truncated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The original #881 lie: empty reborn ring + pre-restart cursor
    used to answer ``replay_ok`` with nothing.  A bare-int cursor (no
    epoch half — pre-#881 client or prior-boot native echo) must draw
    the truncated floor instead of the silent gap."""
    yields = _drain(
        monkeypatch,
        buffered=[],
        headers={"Last-Event-ID": "42"},
        max_yields=3,
    )
    _assert_boot_epoch_truncated(yields)


def test_legacy_bare_int_cursor_on_live_ring_is_truncated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    yields = _drain(
        monkeypatch,
        buffered=[_ev(1), _ev(2)],
        headers={"Last-Event-ID": "1"},
        max_yields=3,
    )
    _assert_boot_epoch_truncated(yields)


def test_same_epoch_cursor_against_empty_ring_fails_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same-epoch ids imply a non-empty ring (single append site, first
    id 1, never cleared) — a same-epoch cursor over an empty ring is
    forged or a bug, and must fail to the snapshot floor rather than
    resurrect the silent-gap shape."""
    yields = _drain(
        monkeypatch,
        buffered=[],
        headers={"Last-Event-ID": f"{EPOCH}-5"},
        max_yields=3,
    )
    _assert_boot_epoch_truncated(yields)


@pytest.mark.parametrize(
    "cursor",
    [
        "not-a-cursor",  # wrong epoch, non-numeric counter
        f"{EPOCH}-xyz",  # live epoch, garbage counter
        f"{EPOCH}--5",  # live epoch, negative (forged) counter
        "-",  # empty epoch, empty counter
    ],
)
def test_unusable_cursor_shapes_all_draw_the_truncated_floor(
    monkeypatch: pytest.MonkeyPatch, cursor: str
) -> None:
    """A present-but-unusable cursor must NOT fall back to ``fresh``:
    fresh is the no-loss shape, and these callers provably lost events.
    (Pre-#881 the unparseable arm silently became fresh — the semantic
    shift is deliberate and this test pins it.)"""
    yields = _drain(
        monkeypatch,
        buffered=[_ev(1)],
        headers={"Last-Event-ID": cursor},
        max_yields=3,
    )
    _assert_boot_epoch_truncated(yields)


# ---------------------------------------------------------------------------
# Transport details
# ---------------------------------------------------------------------------


def test_query_param_fallback_matches_header_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Manual browser reconnects can't set headers on ``new
    EventSource(url)`` — the ``?last_event_id=`` fallback must run the
    same epoch logic (app.js presents its stored cursor this way)."""
    yields = _drain(
        monkeypatch,
        buffered=[_ev(1), _ev(2)],
        query={"last_event_id": f"{EPOCH}-1"},
        max_yields=2,
    )
    frames = _data_frames(yields)
    assert [f["ws_id"] for f in frames] == ["ws-2"]

    yields = _drain(
        monkeypatch,
        buffered=[_ev(1), _ev(2)],
        query={"last_event_id": "deadbeef-1"},
        max_yields=3,
    )
    _assert_boot_epoch_truncated(yields)


def test_epoch_is_hex_and_dashless_by_construction() -> None:
    """The parse splits on the FIRST ``-``; the epoch half must never
    contain one.  ``secrets.token_hex`` guarantees pure hex — this pin
    exists so a future 'readable epoch' refactor (timestamps, uuids
    with dashes) fails here instead of corrupting cursor parsing."""
    import secrets

    for _ in range(64):
        assert "-" not in secrets.token_hex(4)
    # And the production init uses token_hex — source-level pin.
    import inspect

    src = inspect.getsource(server_mod)
    assert "app.state.global_boot_epoch = secrets.token_hex(" in src


def test_snapshot_builds_after_registration_outside_the_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pins the branch's concurrency contract AT THE BUILD SITE: when
    ``_build_node_snapshot`` runs, (1) the listener is already
    REGISTERED — registration-before-build is the no-loss ordering
    (every event from registration on is queued, so the newer snapshot
    plus the queued deltas covers everything), and (2)
    ``global_listeners_lock`` is RELEASED — the O(workstreams) build
    must not stall the fan-out thread.  A refactor "harmonizing" this
    lane with the per-ws build-under-lock shape, or reordering
    registration after the build, fails here and nowhere else."""
    app_state = _make_app_state(buffered=[_ev(1)])
    probed: dict[str, Any] = {}

    def _probe(_s: Any) -> dict[str, Any]:
        probed["registered"] = len(app_state.global_listeners)
        acquired = app_state.global_listeners_lock.acquire(blocking=False)
        probed["lock_free"] = acquired
        if acquired:
            app_state.global_listeners_lock.release()
        return dict(_SNAPSHOT_STUB)

    monkeypatch.setattr(server_mod, "_build_node_snapshot", _probe)
    req = _fake_request(app_state, headers={"Last-Event-ID": "deadbeef-1"})

    async def _run() -> None:
        resp: Any = await server_mod.global_events_sse(req)
        await resp.body_iterator.aclose()

    asyncio.run(_run())
    assert probed == {"registered": 1, "lock_free": True}


def test_raising_snapshot_build_deregisters_the_listener(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The registration-window guard (#881 round-2 review): registration
    precedes the build, so a raising ``_build_node_snapshot`` fires
    before the generator — whose ``finally`` is the normal owner of
    de-registration — ever exists.  The window guard must remove the
    queue, or it sits dead in the fan-out list forever (the fan-out
    thread never removes listeners)."""
    app_state = _make_app_state(buffered=[_ev(1)])

    def _boom(_s: Any) -> dict[str, Any]:
        raise RuntimeError("storage hiccup")

    monkeypatch.setattr(server_mod, "_build_node_snapshot", _boom)
    req = _fake_request(app_state, headers={"Last-Event-ID": "deadbeef-1"})

    async def _run() -> None:
        await server_mod.global_events_sse(req)

    with pytest.raises(RuntimeError, match="storage hiccup"):
        asyncio.run(_run())
    assert app_state.global_listeners == []


def test_listener_registered_exactly_once_per_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Registration is atomic with the replay decision for every branch
    (stale cursors included) — the truncated floor must not skip or
    double the listener append."""
    monkeypatch.setattr(server_mod, "_build_node_snapshot", lambda _s: dict(_SNAPSHOT_STUB))
    app_state = _make_app_state(buffered=[_ev(1)])
    req = _fake_request(app_state, headers={"Last-Event-ID": "deadbeef-1"})

    async def _run() -> None:
        resp: Any = await server_mod.global_events_sse(req)
        assert len(app_state.global_listeners) == 1
        assert isinstance(app_state.global_listeners[0], queue.Queue)
        await resp.body_iterator.aclose()

    asyncio.run(_run())
