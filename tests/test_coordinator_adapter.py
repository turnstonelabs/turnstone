"""Tests for CoordinatorAdapter.

Mirrors test_interactive_adapter.py: focuses on the transport contract
(what gets sent to the ClusterCollector) and cleanup_ui behavior
(unblock listener queues, cancel session). The SessionManager-level
tests in test_session_manager.py cover the lifecycle path.
"""

from __future__ import annotations

import queue
import threading
from typing import Any
from unittest.mock import MagicMock

from turnstone.console.coordinator_adapter import CoordinatorAdapter
from turnstone.core.workstream import Workstream, WorkstreamKind, WorkstreamState


class _StubCoordUI:
    """Stub matching the subset of ConsoleCoordinatorUI the adapter touches."""

    def __init__(self) -> None:
        self._approval_event = threading.Event()
        self._approval_result: tuple[bool, str | None] = (True, "initial")
        self._plan_event = threading.Event()
        self._plan_result: str = "accept"
        self._fg_event = threading.Event()
        self._listeners_lock = threading.Lock()
        self._listeners: list[queue.Queue[dict[str, Any]]] = []


class _StubSession:
    def __init__(self) -> None:
        self.cancelled = False
        self.closed = False

    def cancel(self) -> None:
        self.cancelled = True

    def close(self) -> None:
        self.closed = True


def _make_adapter(
    collector: Any = None,
    *,
    ui_factory: Any = None,
    session_factory: Any = None,
) -> tuple[CoordinatorAdapter, MagicMock]:
    collector = collector or MagicMock()
    adapter = CoordinatorAdapter(
        collector=collector,
        ui_factory=ui_factory or (lambda ws: _StubCoordUI()),
        session_factory=session_factory or (lambda *a, **kw: _StubSession()),
    )
    return adapter, collector


def _make_ws(**overrides: Any) -> Workstream:
    ws = Workstream(id="coord-1", name="my-coord")
    ws.kind = WorkstreamKind.COORDINATOR
    ws.user_id = "u1"
    ws.ui = _StubCoordUI()
    ws.session = _StubSession()
    for k, v in overrides.items():
        setattr(ws, k, v)
    return ws


# ---------------------------------------------------------------------------
# Transport — emit_created / emit_state / emit_closed
# ---------------------------------------------------------------------------


def test_emit_created_calls_collector_with_coord_fields() -> None:
    adapter, collector = _make_adapter()
    ws = _make_ws()
    adapter.emit_created(ws)
    collector.emit_console_ws_created.assert_called_once_with(
        "coord-1",
        name="my-coord",
        user_id="u1",
        kind=WorkstreamKind.COORDINATOR.value,
        state=WorkstreamState.IDLE.value,
        parent_ws_id=None,
    )


def test_emit_state_calls_collector_state() -> None:
    """Post-rich-payload, emit_state passes tokens / context_ratio /
    activity / activity_state / content kwargs read from ws.ui's
    snapshot. Default values (zeros / empty strings) when the UI
    hasn't recorded any per-ws metrics yet."""
    adapter, collector = _make_adapter()
    ws = _make_ws()
    adapter.emit_state(ws, WorkstreamState.RUNNING)
    collector.emit_console_ws_state.assert_called_once_with(
        "coord-1",
        WorkstreamState.RUNNING.value,
        tokens=0,
        context_ratio=0.0,
        activity="",
        activity_state="",
        content="",
    )


def test_emit_closed_calls_collector_closed() -> None:
    adapter, collector = _make_adapter()
    adapter.emit_closed("coord-1")
    collector.emit_console_ws_closed.assert_called_once_with("coord-1")


def test_emit_closed_swallows_reason_kwarg() -> None:
    """The console collector doesn't propagate a 'reason' — the console
    frontend's evicted special-case only fires for real-node
    workstreams. Protocol compatibility only."""
    adapter, collector = _make_adapter()
    adapter.emit_closed("coord-1", reason="evicted")
    collector.emit_console_ws_closed.assert_called_once_with("coord-1")


def test_emit_tolerates_collector_exception() -> None:
    collector = MagicMock()
    collector.emit_console_ws_created.side_effect = RuntimeError("collector dead")
    collector.emit_console_ws_state.side_effect = RuntimeError("collector dead")
    collector.emit_console_ws_closed.side_effect = RuntimeError("collector dead")
    adapter, _ = _make_adapter(collector=collector)
    ws = _make_ws()
    # All three must swallow — the session lifecycle must not break
    # because the collector had a transient failure.
    adapter.emit_created(ws)
    adapter.emit_state(ws, WorkstreamState.RUNNING)
    adapter.emit_closed("coord-1")


# ---------------------------------------------------------------------------
# cleanup_ui
# ---------------------------------------------------------------------------


def test_cleanup_ui_unblocks_events_and_broadcasts_to_listeners() -> None:
    adapter, _ = _make_adapter()
    ws = _make_ws()
    ws.ui._approval_event.clear()  # type: ignore[attr-defined]
    ws.ui._plan_event.clear()  # type: ignore[attr-defined]
    ws.ui._fg_event.clear()  # type: ignore[attr-defined]
    lq: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=5)
    ws.ui._listeners.append(lq)  # type: ignore[attr-defined]

    adapter.cleanup_ui(ws)

    assert ws.ui._approval_event.is_set()  # type: ignore[attr-defined]
    assert ws.ui._plan_event.is_set()  # type: ignore[attr-defined]
    assert ws.ui._fg_event.is_set()  # type: ignore[attr-defined]
    assert ws.ui._approval_result == (False, None)  # type: ignore[attr-defined]
    assert ws.ui._plan_result == "reject"  # type: ignore[attr-defined]
    assert lq.get_nowait() == {"type": "ws_closed"}
    assert ws.ui._listeners == []  # type: ignore[attr-defined]
    assert ws.session.cancelled is True  # type: ignore[attr-defined]
    assert ws.session.closed is True  # type: ignore[attr-defined]


def test_cleanup_ui_listener_full_queue_evicts_head() -> None:
    adapter, _ = _make_adapter()
    ws = _make_ws()
    lq: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
    lq.put_nowait({"type": "stale"})
    ws.ui._listeners.append(lq)  # type: ignore[attr-defined]
    adapter.cleanup_ui(ws)
    assert lq.get_nowait() == {"type": "ws_closed"}


def test_cleanup_ui_tolerates_missing_session_and_ui() -> None:
    adapter, _ = _make_adapter()
    ws = _make_ws()
    ws.session = None
    ws.ui = None
    adapter.cleanup_ui(ws)  # no crash


# ---------------------------------------------------------------------------
# Construction passthrough
# ---------------------------------------------------------------------------


def test_build_session_forwards_skill_model_kind_parent() -> None:
    captured: dict[str, Any] = {}

    def _sf(ui: Any, model: str | None, ws_id: str, **kwargs: Any) -> Any:
        captured["ui"] = ui
        captured["model"] = model
        captured["ws_id"] = ws_id
        captured.update(kwargs)
        return _StubSession()

    adapter, _ = _make_adapter(session_factory=_sf)
    ws = _make_ws()
    ws.parent_ws_id = None
    adapter.build_session(ws, skill="coordinator", model="gpt-5")
    assert captured["ui"] is ws.ui
    assert captured["model"] == "gpt-5"
    assert captured["skill"] == "coordinator"
    assert captured["kind"] == WorkstreamKind.COORDINATOR
    assert captured["parent_ws_id"] is None
    # client_type intentionally NOT forwarded — coord session_factory
    # doesn't accept it (fixed as 'console').
    assert "client_type" not in captured


def test_build_ui_delegates_to_ui_factory() -> None:
    captured_ws: list[Workstream] = []

    def _ui_factory(ws: Workstream) -> Any:
        captured_ws.append(ws)
        return _StubCoordUI()

    adapter, _ = _make_adapter(ui_factory=_ui_factory)
    ws = _make_ws()
    result = adapter.build_ui(ws)
    assert captured_ws == [ws]
    assert isinstance(result, _StubCoordUI)


# ---------------------------------------------------------------------------
# Worker dispatch — _spawn_worker / send
# ---------------------------------------------------------------------------


class _SendSession:
    """ChatSession stub with send / queue_message accounting."""

    def __init__(
        self,
        *,
        queue_full: bool = False,
        send_gate: threading.Event | None = None,
    ) -> None:
        self.send_calls: list[str] = []
        self.queue_calls: list[str] = []
        self._queue_full = queue_full
        # When set, ``send`` blocks on this event — lets the test pin a
        # worker inside session.send while a second thread races through
        # _spawn_worker, proving the lock gate (not Thread.is_alive) is
        # what serialises them.
        self._send_gate = send_gate
        self._send_lock = threading.Lock()
        self.cancelled = False
        self.closed = False

    def send(
        self,
        message: str,
        attachments: Any = None,
        send_id: str | None = None,
    ) -> None:
        if self._send_gate is not None:
            self._send_gate.wait(timeout=2.0)
        with self._send_lock:
            self.send_calls.append(message)

    def queue_message(
        self,
        message: str,
        attachment_ids: Any = None,
        queue_msg_id: str | None = None,
    ) -> None:
        if self._queue_full:
            raise queue.Full
        self.queue_calls.append(message)

    def cancel(self) -> None:
        self.cancelled = True

    def close(self) -> None:
        self.closed = True


class _StubManager:
    """Minimal SessionManager stub exposing ``get`` for adapter.send."""

    def __init__(self, ws: Workstream | None = None) -> None:
        self._ws = ws

    def get(self, ws_id: str) -> Workstream | None:
        if self._ws is not None and self._ws.id == ws_id:
            return self._ws
        return None


class TestCoordinatorAdapterWorkerDispatch:
    def test_spawn_worker_reuses_when_worker_running(self) -> None:
        adapter, _ = _make_adapter()
        ws = _make_ws()
        session = _SendSession()
        ws.session = session  # type: ignore[assignment]
        ws._worker_running = True  # pre-existing worker
        adapter.attach(_StubManager(ws))  # type: ignore[arg-type]

        assert adapter.send(ws.id, "hello") is True
        assert session.queue_calls == ["hello"]
        assert session.send_calls == []
        # worker_thread not replaced
        assert ws.worker_thread is None

    def test_spawn_worker_returns_false_on_queue_full(self) -> None:
        adapter, _ = _make_adapter()
        ws = _make_ws()
        session = _SendSession(queue_full=True)
        ws.session = session  # type: ignore[assignment]
        ws._worker_running = True
        adapter.attach(_StubManager(ws))  # type: ignore[arg-type]

        assert adapter.send(ws.id, "hello") is False
        assert session.send_calls == []

    def test_spawn_worker_concurrent_calls_produce_one_worker(self) -> None:
        """Bug-1 reproducer: two simultaneous send() calls under ws._lock
        must land as exactly one ChatSession.send and one queued message,
        not two parallel workers on the same ChatSession."""
        adapter, _ = _make_adapter()
        ws = _make_ws()
        send_gate = threading.Event()
        session = _SendSession(send_gate=send_gate)
        ws.session = session  # type: ignore[assignment]
        adapter.attach(_StubManager(ws))  # type: ignore[arg-type]

        results: list[bool] = []
        start_barrier = threading.Barrier(2)
        results_lock = threading.Lock()

        def _caller(msg: str) -> None:
            start_barrier.wait(timeout=1.0)
            r = adapter.send(ws.id, msg)
            with results_lock:
                results.append(r)

        t1 = threading.Thread(target=_caller, args=("first",))
        t2 = threading.Thread(target=_caller, args=("second",))
        t1.start()
        t2.start()
        # Both callers return quickly: the winner spawns the worker
        # (returns True immediately) and the loser queues (returns True).
        t1.join(timeout=3.0)
        t2.join(timeout=3.0)
        assert not t1.is_alive() and not t2.is_alive()
        # At this point session.send is still blocked on send_gate —
        # the second caller MUST have taken the queue path.
        assert len(session.queue_calls) == 1
        # Release the worker and let it finish.
        send_gate.set()
        if ws.worker_thread is not None:
            ws.worker_thread.join(timeout=3.0)

        assert results == [True, True]
        assert len(session.send_calls) == 1
        assert set(session.send_calls + session.queue_calls) == {"first", "second"}
        assert ws._worker_running is False

    def test_worker_finally_clears_running_flag(self) -> None:
        adapter, _ = _make_adapter()
        ws = _make_ws()
        session = _SendSession()
        ws.session = session  # type: ignore[assignment]
        adapter.attach(_StubManager(ws))  # type: ignore[arg-type]

        assert adapter.send(ws.id, "hello") is True
        assert ws.worker_thread is not None
        ws.worker_thread.join(timeout=2.0)
        assert ws._worker_running is False
        assert session.send_calls == ["hello"]


# ---------------------------------------------------------------------------
# Children registry
# ---------------------------------------------------------------------------


class TestCoordinatorAdapterChildrenRegistry:
    """Adapter-level integration with :class:`ChildrenRegistry`.

    Pure-registry invariants (forward/reverse consistency, idempotent
    merge, locking) live in ``test_children_registry.py``. These
    tests cover the adapter's wiring: that ``emit_*`` paths drive the
    registry correctly and that the snapshot-priming bridge between
    a collector snapshot and the registry preserves merge semantics.
    """

    def test_emit_created_installs_parent(self) -> None:
        adapter, _ = _make_adapter()
        ws = _make_ws()
        adapter.emit_created(ws)
        assert adapter._registry.children_of(ws.id) == []
        assert adapter._registry.ui_for(ws.id) is ws.ui

    def test_emit_rehydrated_calls_rebuild(self) -> None:
        adapter, _ = _make_adapter()
        calls: list[str] = []
        # Monkeypatch the rebuild hook to count invocations without
        # requiring a real storage backend.
        adapter._rebuild_children_registry = calls.append  # type: ignore[method-assign, assignment]
        ws = _make_ws()
        adapter.emit_created(ws)
        assert calls == []
        adapter.emit_rehydrated(ws)
        assert calls == [ws.id]

    def test_emit_closed_uninstalls_parent_and_clears_children(self) -> None:
        adapter, _ = _make_adapter()
        adapter._registry.install("coord-a", object())
        adapter._registry.install("coord-b", object())
        adapter._registry.merge_children("coord-a", ["child-a1", "child-a2"])
        adapter._registry.merge_children("coord-b", ["child-b1"])

        adapter.emit_closed("coord-a")

        assert adapter._registry.ui_for("coord-a") is None
        assert adapter._registry.children_of("coord-a") == []
        assert adapter._registry.parent_for("child-a1") is None
        assert adapter._registry.parent_for("child-a2") is None
        # coord-b untouched
        assert adapter._registry.parent_for("child-b1") == "coord-b"
        assert adapter._registry.ui_for("coord-b") is not None

    def test_prime_children_from_snapshot_merges_without_overwriting(self) -> None:
        # Snapshot priming now lives on ClusterChildSource (production
        # path). The adapter no longer carries its own duplicate copy.
        from turnstone.core.child_source import ClusterChildSource

        adapter, _ = _make_adapter()
        adapter._registry.merge_children("coord-a", ["child-a1"])

        source = ClusterChildSource(
            collector=MagicMock(),
            registry=adapter._registry,
            parents_provider=lambda: ["coord-a"],
        )

        snapshot = {
            "nodes": [
                {
                    "workstreams": [
                        {"id": "child-a2", "parent_ws_id": "coord-a"},
                        # Unknown parent — skipped
                        {"id": "child-x", "parent_ws_id": "coord-unknown"},
                        # Missing fields — skipped
                        {"id": "", "parent_ws_id": "coord-a"},
                    ],
                },
            ],
        }
        source._prime_from_snapshot(snapshot)
        assert set(adapter._registry.children_of("coord-a")) == {
            "child-a1",
            "child-a2",
        }
        assert adapter._registry.parent_for("child-a2") == "coord-a"
        assert adapter._registry.parent_for("child-x") is None


# ---------------------------------------------------------------------------
# Dispatch — _dispatch_child_event
# ---------------------------------------------------------------------------


class _UIRecorder:
    """UI stub capturing _enqueue payloads for dispatch assertions."""

    def __init__(self) -> None:
        self.enqueued: list[dict[str, Any]] = []

    def _enqueue(self, payload: dict[str, Any]) -> None:
        self.enqueued.append(payload)


class TestCoordinatorAdapterDispatchChildEvent:
    def _setup(
        self, coord_id: str = "coord-a"
    ) -> tuple[CoordinatorAdapter, _UIRecorder, Workstream]:
        adapter, _ = _make_adapter()
        coord_ws = _make_ws()
        coord_ws.id = coord_id
        recorder = _UIRecorder()
        coord_ws.ui = recorder  # type: ignore[assignment]
        adapter._registry.install(coord_id, recorder)
        adapter.attach(_StubManager(coord_ws))  # type: ignore[arg-type]
        return adapter, recorder, coord_ws

    def test_dispatch_unknown_parent_drops_event(self) -> None:
        adapter, recorder, _ = self._setup()
        adapter._dispatch_child_event(
            {"type": "ws_created", "ws_id": "orphan", "parent_ws_id": "coord-unknown"}
        )
        adapter._dispatch_child_event({"type": "cluster_state", "ws_id": "orphan"})
        adapter._dispatch_child_event({"type": "ws_closed", "ws_id": "orphan"})
        assert recorder.enqueued == []

    def test_dispatch_ws_created_routes_to_parent_coord_ui(self) -> None:
        adapter, recorder, _ = self._setup()
        adapter._dispatch_child_event(
            {
                "type": "ws_created",
                "ws_id": "child-a1",
                "parent_ws_id": "coord-a",
                "name": "kid",
                "node_id": "node-1",
            }
        )
        assert len(recorder.enqueued) == 1
        payload = recorder.enqueued[0]
        assert payload["type"] == "child_ws_created"
        assert payload["child_ws_id"] == "child-a1"
        assert payload["parent_ws_id"] == "coord-a"
        # Reverse index updated for subsequent cluster_state events.
        assert adapter._registry.parent_for("child-a1") == "coord-a"

    def test_dispatch_cluster_state_routes_via_reverse_index(self) -> None:
        adapter, recorder, _ = self._setup()
        adapter._registry.merge_children("coord-a", ["child-a1"])
        adapter._dispatch_child_event(
            {
                "type": "cluster_state",
                "ws_id": "child-a1",
                "state": "running",
                "tokens": 42,
                "node_id": "node-1",
            }
        )
        assert len(recorder.enqueued) == 1
        payload = recorder.enqueued[0]
        assert payload["type"] == "child_ws_state"
        assert payload["state"] == "running"
        assert payload["tokens"] == 42

    def test_dispatch_ws_closed_routes_to_parent_coord(self) -> None:
        adapter, recorder, _ = self._setup()
        adapter._registry.merge_children("coord-a", ["child-a1"])
        adapter._dispatch_child_event(
            {"type": "ws_closed", "ws_id": "child-a1", "reason": "evicted"}
        )
        assert len(recorder.enqueued) == 1
        payload = recorder.enqueued[0]
        assert payload["type"] == "child_ws_closed"
        assert payload["reason"] == "evicted"
        assert payload["parent_ws_id"] == "coord-a"

    def test_dispatch_adds_ws_id_in_place(self) -> None:
        """perf-6: _enqueue_on_ui mutates the payload dict in place with
        the coord's ws_id so the browser can discriminate child events."""
        adapter, recorder, _ = self._setup()
        adapter._registry.merge_children("coord-a", ["child-a1"])
        adapter._dispatch_child_event(
            {
                "type": "cluster_state",
                "ws_id": "child-a1",
                "state": "running",
            }
        )
        assert recorder.enqueued[0]["ws_id"] == "coord-a"

    def test_dispatch_cluster_state_does_not_carry_pending_approval_detail(
        self,
    ) -> None:
        """Stage 3 cleanup — the ``pending_approval_detail`` piggyback
        on ``cluster_state`` is gone. Approval items now arrive via
        bulk fetch (triggered by ``activity_state="approval"`` in the
        browser); verdicts via ``child_ws_intent_verdict``; resolution
        via ``child_ws_approval_resolved``. The state event carries
        only state + activity_state — no detail field."""
        adapter, recorder, _ = self._setup()
        adapter._registry.merge_children("coord-a", ["child-a1"])
        adapter._dispatch_child_event(
            {
                "type": "cluster_state",
                "ws_id": "child-a1",
                "state": "running",
                "activity_state": "approval",
            }
        )
        assert len(recorder.enqueued) == 1
        payload = recorder.enqueued[0]
        assert payload["type"] == "child_ws_state"
        assert payload["activity_state"] == "approval"
        assert "pending_approval_detail" not in payload

    def test_dispatch_intent_verdict_emits_child_ws_intent_verdict(self) -> None:
        """Stage 3 Step 6 — explicit verdict events are re-emitted as
        child_ws_intent_verdict on the parent's SSE so the tree UI
        renders the risk pill without polling."""
        adapter, recorder, _ = self._setup()
        adapter._registry.merge_children("coord-a", ["child-a1"])
        verdict = {
            "call_id": "c1",
            "risk_level": "low",
            "confidence": 0.92,
            "recommendation": "approve",
        }
        adapter._dispatch_child_event(
            {
                "type": "intent_verdict",
                "ws_id": "child-a1",
                "node_id": "node-1",
                "verdict": verdict,
            }
        )
        assert len(recorder.enqueued) == 1
        payload = recorder.enqueued[0]
        assert payload["type"] == "child_ws_intent_verdict"
        assert payload["child_ws_id"] == "child-a1"
        assert payload["parent_ws_id"] == "coord-a"
        assert payload["node_id"] == "node-1"
        assert payload["verdict"] == verdict

    def test_dispatch_intent_verdict_unknown_child_drops(self) -> None:
        adapter, recorder, _ = self._setup()
        adapter._dispatch_child_event(
            {
                "type": "intent_verdict",
                "ws_id": "ws-orphan",
                "verdict": {"call_id": "c1"},
            }
        )
        assert recorder.enqueued == []

    def test_dispatch_approval_resolved_emits_child_ws_approval_resolved(
        self,
    ) -> None:
        """Stage 3 Step 6 — paired with intent_verdict; clears the
        pending-approval pill on the parent's tree UI in lockstep
        with the actual decision."""
        adapter, recorder, _ = self._setup()
        adapter._registry.merge_children("coord-a", ["child-a1"])
        adapter._dispatch_child_event(
            {
                "type": "approval_resolved",
                "ws_id": "child-a1",
                "node_id": "node-1",
                "approved": True,
                "feedback": "lgtm",
                "always": False,
            }
        )
        assert len(recorder.enqueued) == 1
        payload = recorder.enqueued[0]
        assert payload["type"] == "child_ws_approval_resolved"
        assert payload["child_ws_id"] == "child-a1"
        assert payload["parent_ws_id"] == "coord-a"
        assert payload["approved"] is True
        assert payload["feedback"] == "lgtm"
        assert payload["always"] is False

    def test_dispatch_approval_resolved_coerces_missing_fields(self) -> None:
        """Older nodes mid-rolling-upgrade may omit approved / always /
        feedback; dispatch coerces to safe defaults."""
        adapter, recorder, _ = self._setup()
        adapter._registry.merge_children("coord-a", ["child-a1"])
        adapter._dispatch_child_event({"type": "approval_resolved", "ws_id": "child-a1"})
        assert len(recorder.enqueued) == 1
        payload = recorder.enqueued[0]
        assert payload["approved"] is False
        assert payload["feedback"] == ""
        assert payload["always"] is False

    def test_dispatch_approval_resolved_unknown_child_drops(self) -> None:
        """Symmetric to the intent_verdict drop test — events for
        ws_ids the registry doesn't know about silently drop instead
        of fanning out to a parent that has no business seeing them."""
        adapter, recorder, _ = self._setup()
        adapter._dispatch_child_event(
            {
                "type": "approval_resolved",
                "ws_id": "ws-orphan",
                "approved": True,
            },
        )
        assert recorder.enqueued == []

    def test_dispatch_approve_request_emits_child_ws_approve_request(
        self,
    ) -> None:
        """Push path for the initial approval items — eliminates the
        bulk-fetch race that left the coord row stuck on a loading
        placeholder when the bulk fetch landed in the gap between
        _emit_state(ATTENTION) and approve_tools setting _pending_approval."""
        adapter, recorder, _ = self._setup()
        adapter._registry.merge_children("coord-a", ["child-a1"])
        detail = {
            "type": "approve_request",
            "items": [{"call_id": "c1", "header": "tool x"}],
            "judge_pending": True,
        }
        adapter._dispatch_child_event(
            {
                "type": "approve_request",
                "ws_id": "child-a1",
                "node_id": "node-1",
                "detail": detail,
            },
        )
        assert len(recorder.enqueued) == 1
        payload = recorder.enqueued[0]
        assert payload["type"] == "child_ws_approve_request"
        assert payload["child_ws_id"] == "child-a1"
        assert payload["parent_ws_id"] == "coord-a"
        assert payload["node_id"] == "node-1"
        assert payload["detail"] == detail

    def test_dispatch_approve_request_unknown_child_drops(self) -> None:
        adapter, recorder, _ = self._setup()
        adapter._dispatch_child_event(
            {
                "type": "approve_request",
                "ws_id": "ws-orphan",
                "detail": {"items": []},
            },
        )
        assert recorder.enqueued == []
