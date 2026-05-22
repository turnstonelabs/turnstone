"""Tests for the SSE reconnect-with-replay foundation.

Covers the three commits of the reconnect-with-replay PR at the
boundaries that matter:

  - :meth:`SessionUIBase.register_listener_with_replay` — the
    per-ws ring buffer + ``Last-Event-ID`` slice semantics
    (replay_ok / truncated / empty-buffer edge cases, order
    preservation under concurrent emit, no skipped ids on
    ``queue.Full``, cross-thread emit/replay consistency).
  - :func:`make_events_handler` — ``id:`` field on every yielded
    event from the buffer (replay or live), jittered ``retry:`` on
    the first yield, ``replay_truncated`` envelope on stale
    ``Last-Event-ID``, snapshot skip when replay covers the gap.

The browser-side guard for the ``onerror`` close pattern lives in
``test_app_js.py`` alongside the other static JS guards.
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace as SimpleNS
from typing import Any
from unittest.mock import MagicMock

from starlette.requests import Request

from turnstone.core.session_routes import (
    SessionEndpointConfig,
    make_events_handler,
)
from turnstone.core.session_ui_base import SessionUIBase

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _ConcreteUI(SessionUIBase):
    """Minimal concrete subclass for direct UI tests."""


def _make_ui(ws_id: str = "ws-1") -> _ConcreteUI:
    return _ConcreteUI(ws_id=ws_id, user_id="u1")


def _fake_request(
    *,
    headers: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
    path_params: dict[str, str] | None = None,
) -> Request:
    """Construct a Starlette ``Request`` for the events handler.

    The handler reads ``request.headers``, ``request.query_params``,
    ``request.path_params``, and awaits ``request.is_disconnected()``.
    Building a real ASGI scope keeps the test honest about the values
    those properties resolve from.
    """
    header_list = []
    if headers:
        for k, v in headers.items():
            header_list.append((k.lower().encode(), v.encode()))
    query_string = "&".join(f"{k}={v}" for k, v in query.items()).encode() if query else b""
    scope = {
        "type": "http",
        "method": "GET",
        "headers": header_list,
        "path": "/events",
        "raw_path": b"/events",
        "query_string": query_string,
        "path_params": path_params or {},
        "app": MagicMock(),
    }

    async def _recv() -> dict[str, Any]:  # noqa: RUF029 — async signature required
        return {"type": "http.disconnect"}

    return Request(scope, receive=_recv)


async def _drain_until(
    gen: Any,
    *,
    max_events: int = 200,
    stop_predicate=lambda _ev: False,
) -> list[dict[str, Any]]:
    """Pull up to ``max_events`` from an async generator, then close.

    The events handler runs forever once it enters the live drain;
    tests must explicitly stop after the events of interest land.
    """
    collected: list[dict[str, Any]] = []
    async for ev in gen:
        collected.append(ev)
        if stop_predicate(ev) or len(collected) >= max_events:
            break
    await gen.aclose()
    return collected


# ---------------------------------------------------------------------------
# register_listener_with_replay — per-ws ring buffer slice semantics
# ---------------------------------------------------------------------------


def test_replay_holds_events_through_empty_listeners_period() -> None:
    """The load-bearing property of the new ring buffer: events fired
    while NO listener is registered must still be replayable to a
    later subscriber whose ``Last-Event-ID`` predates them.  Pre-PR,
    events to an empty listener list went on the floor — that's the
    behaviour the entire reconnect-with-replay foundation replaces.
    """
    ui = _make_ui()
    # No listeners — fire 10 events.
    for i in range(10):
        ui._enqueue({"type": "tool_started", "name": f"t{i}"})
    # Reconnect-style register with Last-Event-ID=0 (client saw nothing).
    lq, replay, status, lost, earliest = ui.register_listener_with_replay(0)
    assert status == "replay_ok"
    assert lost == 0
    assert earliest == 1
    assert len(replay) == 10
    assert [ev["name"] for ev in replay] == [f"t{i}" for i in range(10)]
    # Each replayed event carries its _event_id so the events handler
    # can emit the SSE id: field — verified by inspecting the slice.
    assert [ev["_event_id"] for ev in replay] == list(range(1, 11))


def test_replay_with_last_event_id_skips_already_seen_events() -> None:
    """Client says it last saw id=5 — replay yields only events 6+,
    not the whole buffer."""
    ui = _make_ui()
    for i in range(8):
        ui._enqueue({"type": "tool_started", "name": f"t{i}"})
    lq, replay, status, lost, earliest = ui.register_listener_with_replay(5)
    assert status == "replay_ok"
    assert lost == 0
    assert [ev["_event_id"] for ev in replay] == [6, 7, 8]


def test_replay_truncated_when_last_event_id_predates_buffer() -> None:
    """When the buffer has evicted events the client wanted, return
    ``truncated`` with the lost-count gap so the handler can emit the
    explicit envelope and fall through to snapshot recovery."""
    ui = _make_ui()
    # Override the buffer cap for the test so we don't have to fire
    # 2001 events to trigger eviction.
    import collections

    ui._event_buffer = collections.deque(maxlen=5)
    for i in range(20):
        ui._enqueue({"type": "tool_started", "name": f"t{i}"})
    # Buffer now holds ids 16..20 (5 most recent of 20 emitted).
    lq, replay, status, lost, earliest = ui.register_listener_with_replay(3)
    assert status == "truncated"
    assert earliest == 16
    assert lost == 12  # earliest-1 - last_event_id = 15 - 3
    assert replay == []


def test_replay_empty_buffer_returns_replay_ok_empty() -> None:
    """Cold-start ws with zero events ever: replay_ok / empty list.
    A spurious ``replay_truncated`` envelope on a freshly-opened
    workstream would be confusing and incorrect."""
    ui = _make_ui()
    lq, replay, status, lost, earliest = ui.register_listener_with_replay(0)
    assert status == "replay_ok"
    assert replay == []
    assert lost == 0
    assert earliest == 0


def test_replay_registers_listener_atomically_with_buffer_snapshot() -> None:
    """Atomicity contract: under ``_listeners_lock`` we both snapshot
    the buffer AND register the listener.  A writer's ``_enqueue``
    takes the same lock, so an event landing after the snapshot
    arrives in the listener queue (live) — never in BOTH the replay
    and the live queue, and never in NEITHER."""
    ui = _make_ui()
    ui._enqueue({"type": "tool_started", "name": "before"})
    lq, replay, _, _, _ = ui.register_listener_with_replay(0)
    # Now fire after registration — must arrive live, NOT in replay.
    ui._enqueue({"type": "tool_started", "name": "after"})
    assert [ev["name"] for ev in replay] == ["before"]
    live = lq.get_nowait()
    assert live["name"] == "after"
    assert live["_event_id"] == 2


def test_event_id_monotonic_under_concurrent_writers() -> None:
    """Load-bearing invariant for any replay protocol — if monotonicity
    ever breaks (e.g. someone moves the id-increment outside the
    lock), reconnect-with-replay silently re-orders events.  Stress
    with multiple writer threads."""
    ui = _make_ui()
    n_writers = 4
    per_writer = 200
    barrier = threading.Barrier(n_writers)

    def _writer(tag: str) -> None:
        barrier.wait()
        for i in range(per_writer):
            ui._enqueue({"type": "tool_started", "name": f"{tag}-{i}"})

    threads = [threading.Thread(target=_writer, args=(f"w{w}",)) for w in range(n_writers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Walk the buffer in deque order — ids must be strictly monotonic.
    ids = [eid for eid, _ in ui._event_buffer]
    assert ids == sorted(ids), "event_id ordering broke under concurrent writers"
    assert ids == list(range(ids[0], ids[-1] + 1)), "event_id skipped under concurrency"
    assert ids[-1] == n_writers * per_writer


def test_event_id_does_not_skip_when_listener_queue_full() -> None:
    """If a slow listener's queue is full, the per-listener
    ``put_nowait`` is silently dropped — but the counter must NOT
    skip.  A subsequently-registered listener with
    ``Last-Event-ID=0`` must see ALL the ids from the buffer
    (1..N), not a sparse subset.  Pre-bug-class: moving the
    id-increment inside the per-listener loop would create phantom
    "gaps" the truncation detector would misread."""
    ui = _make_ui()
    slow_lq = ui._register_listener(maxsize=1)
    slow_lq.put_nowait({"placeholder": True})  # full immediately
    # Fire 10 events — 9 will hit queue.Full and be suppressed.
    for i in range(10):
        ui._enqueue({"type": "tool_started", "name": f"t{i}"})
    # Replay from id=0 — fresh listener gets all 10, ids 1..10 dense.
    _, replay, status, _, _ = ui.register_listener_with_replay(0)
    assert status == "replay_ok"
    assert [ev["_event_id"] for ev in replay] == list(range(1, 11))


def test_cross_thread_writer_and_replay_observer_consistent() -> None:
    """A worker thread fires ``_enqueue`` while another thread calls
    ``register_listener_with_replay``.  The replay snapshot must be
    gap-free — no half-written deque state visible to the reader.
    Guards against the iteration-during-mutation hazard that a casual
    implementation could introduce if the buffer copy out of the lock
    isn't taken correctly."""
    ui = _make_ui()
    n = 500
    done = threading.Event()

    def _writer() -> None:
        for i in range(n):
            ui._enqueue({"type": "tool_started", "name": f"t{i}"})
        done.set()

    snap_box: dict[str, Any] = {}

    def _reader() -> None:
        # Wait briefly so the writer is mid-flight.
        threading.Event().wait(0.001)
        _, replay, status, _, earliest = ui.register_listener_with_replay(0)
        snap_box["replay"] = replay
        snap_box["status"] = status
        snap_box["earliest"] = earliest

    w = threading.Thread(target=_writer)
    r = threading.Thread(target=_reader)
    w.start()
    r.start()
    w.join()
    r.join()

    replay = snap_box["replay"]
    # Replay snapshot is consistent — ids contiguous, no gaps.
    ids = [ev["_event_id"] for ev in replay]
    assert ids == sorted(ids)
    if ids:
        assert ids == list(range(ids[0], ids[-1] + 1)), (
            "gap observed in replay snapshot — torn deque state visible"
        )


def test_event_id_persists_across_turn_boundaries() -> None:
    """Resetting ``_event_id`` to 0 at turn boundaries would silently
    mis-replay a long-lived SSE subscriber whose ``Last-Event-ID``
    was from a prior turn.  Mirrors the pre-existing
    ``test_inflight_seq_monotonic_across_turn_boundaries`` invariant
    on the snap_seq side, extended to the buffer/replay side."""
    ui = _make_ui()
    ui.on_content_token("turn-N tok1 ")
    ui.on_content_token("turn-N tok2 ")
    seq_before = ui._event_id
    ui.on_turn_committed()
    ui.on_turn_start()
    ui.on_content_token("turn-N+1 tok1")
    seq_after = ui._event_id
    assert seq_after > seq_before, "counter regressed across turn boundary"
    # Replay from mid-turn-N must still serve turn-N+1's content.
    _, replay, status, _, _ = ui.register_listener_with_replay(seq_before)
    assert status == "replay_ok"
    assert len(replay) == 1
    assert replay[0]["text"] == "turn-N+1 tok1"


def test_replay_ok_skips_in_progress_snapshot_path() -> None:
    """When ``last_event_id`` is provided AND replay covers the gap,
    ``register_listener_with_replay`` returns ``replay_ok`` without
    touching the inflight content/reasoning snapshot machinery.  The
    events handler uses this branch to skip emitting the
    ``in_progress_snapshot`` event (which would otherwise double-
    render content the buffered events already contain)."""
    ui = _make_ui()
    ui.on_content_token("partial ")
    # Replay path: returns replay_ok and a synthetic snap is NOT taken
    # (we test the handler-side behavior in the handler tests below).
    lq, replay, status, _, _ = ui.register_listener_with_replay(0)
    assert status == "replay_ok"
    # The buffered event carries the partial content as a content event.
    assert any(ev.get("type") == "content" for ev in replay)


# ---------------------------------------------------------------------------
# make_events_handler — id: / retry: / replay_truncated / branch behaviour
# ---------------------------------------------------------------------------


def _wire_events_handler(ui: _ConcreteUI) -> Any:
    """Build a minimal ``make_events_handler`` closure that returns
    yields suitable for the EventSourceResponse generator.

    Calls the closure with a fake request; returns the inner generator
    AFTER it has been started so the test can iterate yields directly.
    """
    ws = SimpleNS(id=ui.ws_id, ui=ui, state=SimpleNS(value="idle"))
    mgr = MagicMock()
    mgr.get.return_value = ws

    cfg = SessionEndpointConfig(
        permission_gate=None,
        manager_lookup=lambda _r: (mgr, None),
        tenant_check=None,
        not_found_label="Workstream not found",
        audit_action_prefix="workstream",
        events_replay=None,
        events_replay_prepare=None,
    )
    return make_events_handler(cfg)


def _drain_handler_yields(
    ui: _ConcreteUI,
    *,
    headers: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
    max_yields: int = 10,
) -> tuple[list[Any], str]:
    """Synchronous helper: spin up the handler, drain up to N yields,
    return ``(raw_yields, decoded_blob)``.  Uses ``asyncio.run`` so
    tests don't depend on pytest-asyncio / pytest-anyio plugin config.

    The decoded blob is the textual SSE concatenation — assertion
    targets in the tests below grep against it.  Raw yields are
    returned for shape-level assertions (e.g. the first-yield
    ``retry`` check).
    """
    handler = _wire_events_handler(ui)
    req = _fake_request(headers=headers, query=query, path_params={"ws_id": ui.ws_id})

    async def _run() -> list[Any]:
        resp = await handler(req)
        out: list[Any] = []
        async for chunk in resp.body_iterator:
            out.append(chunk)
            if len(out) >= max_yields:
                break
        await resp.body_iterator.aclose()
        return out

    yields = asyncio.run(_run())
    # The events handler yields plain dicts ({"data": ..., "id": ...,
    # "retry": ..., ...}); sse-starlette's response layer encodes
    # them into SSE wire format at serve-time.  For introspection,
    # render each dict into the equivalent SSE textual form so the
    # tests can grep against the canonical encoded representation
    # AND have access to the raw dicts for shape-level assertions.
    text_parts: list[str] = []
    for y in yields:
        if isinstance(y, bytes):
            text_parts.append(y.decode(errors="replace"))
        elif isinstance(y, str):
            text_parts.append(y)
        elif isinstance(y, dict):
            # Mirror sse-starlette's encoding contract — one field
            # per line, terminating blank line per event.
            for field in ("id", "event", "retry", "data", "comment"):
                if field in y:
                    text_parts.append(f"{field}: {y[field]}")
            text_parts.append("")
        elif hasattr(y, "encode"):
            encoded = y.encode()
            text_parts.append(
                encoded.decode(errors="replace") if isinstance(encoded, bytes) else str(encoded)
            )
        else:
            text_parts.append(str(y))
    return yields, "\n".join(text_parts)


def test_handler_emits_retry_on_first_yield() -> None:
    """First yield of the events handler must include a jittered
    ``retry`` field in the [2500, 4500] ms range so 6-pane reconnects
    don't lockstep on EventSource's default ~3 s interval."""
    ui = _make_ui()
    _, blob = _drain_handler_yields(ui, max_yields=1)
    # The retry: SSE field appears in the encoded blob.
    import re

    match = re.search(r"retry:\s*(\d+)", blob)
    assert match is not None, f"first yield missing retry: line\n{blob}"
    retry = int(match.group(1))
    assert 2500 <= retry <= 4500, f"retry {retry} outside jitter band [2500, 4500]"


def test_handler_replay_ok_skips_snapshot_emits_id() -> None:
    """``Last-Event-ID`` + buffer covers gap → emit buffered events
    with SSE ``id:`` field, SKIP the in-progress snapshot (it would
    double-render content the buffered events already carry)."""
    ui = _make_ui()
    ui.on_content_token("hello ")
    ui.on_content_token("world")
    _, blob = _drain_handler_yields(ui, headers={"Last-Event-ID": "0"}, max_yields=6)
    # No in_progress_snapshot anywhere on the replay_ok path.
    assert "in_progress_snapshot" not in blob, (
        "replay_ok must not emit in_progress_snapshot — it duplicates "
        f"buffered content. blob:\n{blob}"
    )
    # Every buffered content event got an id: line.
    assert "id: 1" in blob, f"missing id: 1 in:\n{blob}"
    assert "id: 2" in blob, f"missing id: 2 in:\n{blob}"


def test_handler_truncated_emits_envelope_then_snapshot() -> None:
    """Stale ``Last-Event-ID`` + buffer too short → emit
    ``replay_truncated`` envelope, THEN fall through to the
    fresh-style replay (state_change + in_progress_snapshot) as the
    recovery floor."""
    import collections

    ui = _make_ui()
    ui._event_buffer = collections.deque(maxlen=3)
    for i in range(10):
        ui.on_content_token(f"t{i}")
    _, blob = _drain_handler_yields(ui, headers={"Last-Event-ID": "1"}, max_yields=8)

    assert "replay_truncated" in blob, (
        f"stale Last-Event-ID must emit replay_truncated envelope; got:\n{blob}"
    )
    # Recovery floor: in_progress_snapshot carries the partial content
    # the evicted events represented.
    assert "in_progress_snapshot" in blob, (
        f"truncated path must fall through to in_progress_snapshot; got:\n{blob}"
    )


def test_handler_fresh_path_skips_replay_truncated() -> None:
    """No ``Last-Event-ID`` → fresh-connect behaviour (today's path
    unchanged: state_change + in_progress_snapshot + live).  No
    replay_truncated envelope should ever appear on a fresh
    connect."""
    ui = _make_ui()
    ui.on_content_token("hello ")
    _, blob = _drain_handler_yields(ui, max_yields=5)

    assert "replay_truncated" not in blob
    # Fresh connect emits the snapshot.
    assert "in_progress_snapshot" in blob


def test_handler_malformed_last_event_id_falls_back_to_fresh() -> None:
    """Defence against intermediaries that mangle the header — a
    non-integer ``Last-Event-ID`` must not be treated as ``0`` (which
    could trigger spurious replays) nor crash the handler.  Falls
    through to the fresh-connect path."""
    ui = _make_ui()
    _, blob = _drain_handler_yields(
        ui,
        headers={"Last-Event-ID": "abc-not-an-int"},
        max_yields=3,
    )
    assert "replay_truncated" not in blob


def test_handler_query_param_fallback_is_honoured() -> None:
    """The manual-reconnect path can't set custom headers on
    ``new EventSource(url)`` — the browser sends
    ``?last_event_id=N`` instead.  Handler must honour the query
    param identically to the header."""
    ui = _make_ui()
    ui.on_content_token("hello")
    _, blob = _drain_handler_yields(ui, query={"last_event_id": "0"}, max_yields=4)

    # Replay path: in_progress_snapshot SKIPPED, id:1 present.
    assert "in_progress_snapshot" not in blob
    assert "id: 1" in blob
