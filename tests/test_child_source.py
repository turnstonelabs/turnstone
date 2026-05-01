"""Unit tests for :mod:`turnstone.core.child_source`.

Covers both strategies in isolation against fakes — no live collector,
no live SessionManager. Adapter-level integration coverage continues to
live in ``test_coordinator_adapter.py``.
"""

from __future__ import annotations

import contextlib
import time
from typing import TYPE_CHECKING, Any

from turnstone.core.child_source import ClusterChildSource, SameNodeChildSource
from turnstone.core.children_registry import ChildrenRegistry
from turnstone.core.workstream import WorkstreamState

if TYPE_CHECKING:
    import queue


# ---------------------------------------------------------------------------
# SameNodeChildSource
# ---------------------------------------------------------------------------


class _FakeManager:
    """Minimal SessionManager stand-in implementing the subscribe API."""

    def __init__(self) -> None:
        self.subscribers: list[Any] = []

    def subscribe_to_state(self, callback: Any) -> None:
        self.subscribers.append(callback)

    def unsubscribe_from_state(self, callback: Any) -> None:
        with contextlib.suppress(ValueError):
            self.subscribers.remove(callback)

    def fire(self, ws_id: str, state: WorkstreamState) -> None:
        for cb in self.subscribers:
            cb(ws_id, state)


class TestSameNodeChildSource:
    def test_start_subscribes_to_manager(self) -> None:
        mgr = _FakeManager()
        registry = ChildrenRegistry()
        src = SameNodeChildSource(mgr, registry)
        sink_calls: list[dict[str, Any]] = []
        src.start(sink=sink_calls.append)
        assert len(mgr.subscribers) == 1

    def test_state_change_for_known_child_pushes_to_sink(self) -> None:
        mgr = _FakeManager()
        registry = ChildrenRegistry()
        registry.install("p1", object())
        registry.add_child("p1", "c1")
        src = SameNodeChildSource(mgr, registry)
        sink_calls: list[dict[str, Any]] = []
        src.start(sink=sink_calls.append)

        mgr.fire("c1", WorkstreamState.RUNNING)

        assert len(sink_calls) == 1
        ev = sink_calls[0]
        assert ev["type"] == "cluster_state"
        assert ev["ws_id"] == "c1"
        assert ev["state"] == "running"

    def test_state_change_for_unknown_workstream_is_dropped(self) -> None:
        mgr = _FakeManager()
        registry = ChildrenRegistry()
        src = SameNodeChildSource(mgr, registry)
        sink_calls: list[dict[str, Any]] = []
        src.start(sink=sink_calls.append)

        # No registry entry — pre-filter drops the event without
        # invoking the sink.
        mgr.fire("ws-unknown", WorkstreamState.IDLE)
        assert sink_calls == []

    def test_shutdown_unsubscribes(self) -> None:
        mgr = _FakeManager()
        registry = ChildrenRegistry()
        src = SameNodeChildSource(mgr, registry)
        src.start(sink=lambda ev: None)
        assert len(mgr.subscribers) == 1
        src.shutdown()
        assert mgr.subscribers == []

    def test_start_is_idempotent(self) -> None:
        mgr = _FakeManager()
        registry = ChildrenRegistry()
        src = SameNodeChildSource(mgr, registry)
        src.start(sink=lambda ev: None)
        src.start(sink=lambda ev: None)
        # Second start is a no-op; only one subscription.
        assert len(mgr.subscribers) == 1

    def test_sink_exception_does_not_propagate(self) -> None:
        mgr = _FakeManager()
        registry = ChildrenRegistry()
        registry.install("p1", object())
        registry.add_child("p1", "c1")
        src = SameNodeChildSource(mgr, registry)

        def bad_sink(ev: dict[str, Any]) -> None:
            raise RuntimeError("sink boom")

        src.start(sink=bad_sink)
        # Should not raise — the strategy catches sink failures and logs.
        mgr.fire("c1", WorkstreamState.RUNNING)


# ---------------------------------------------------------------------------
# ClusterChildSource
# ---------------------------------------------------------------------------


class _FakeCollector:
    """Minimal ClusterCollector stand-in providing the listener API."""

    def __init__(self, snapshot: dict[str, Any] | None = None) -> None:
        self._snapshot = snapshot or {"nodes": []}
        self.queues: list[queue.Queue[dict[str, Any]]] = []
        self.unregistered: list[queue.Queue[dict[str, Any]]] = []

    def get_snapshot_and_register(self, q: queue.Queue[dict[str, Any]]) -> dict[str, Any]:
        self.queues.append(q)
        return self._snapshot

    def unregister_listener(self, q: queue.Queue[dict[str, Any]]) -> None:
        self.unregistered.append(q)

    def emit(self, event: dict[str, Any]) -> None:
        """Push an event to all registered listener queues."""
        for q in self.queues:
            q.put(event)


class TestClusterChildSource:
    def test_start_subscribes_to_collector(self) -> None:
        coll = _FakeCollector()
        registry = ChildrenRegistry()
        src = ClusterChildSource(
            collector=coll,
            registry=registry,
            parents_provider=list,
        )
        try:
            src.start(sink=lambda ev: None)
            assert len(coll.queues) == 1
        finally:
            src.shutdown()

    def test_start_primes_registry_from_snapshot(self) -> None:
        snapshot = {
            "nodes": [
                {
                    "workstreams": [
                        {"id": "c1", "parent_ws_id": "p1"},
                        {"id": "c2", "parent_ws_id": "p1"},
                        # Unknown parent — dropped
                        {"id": "x", "parent_ws_id": "p-unknown"},
                    ],
                },
            ],
        }
        coll = _FakeCollector(snapshot)
        registry = ChildrenRegistry()
        registry.install("p1", object())
        src = ClusterChildSource(
            collector=coll,
            registry=registry,
            parents_provider=lambda: ["p1"],
        )
        try:
            src.start(sink=lambda ev: None)
            assert set(registry.children_of("p1")) == {"c1", "c2"}
            assert registry.parent_for("x") is None
        finally:
            src.shutdown()

    def test_event_dispatched_to_sink(self) -> None:
        coll = _FakeCollector()
        registry = ChildrenRegistry()
        src = ClusterChildSource(
            collector=coll,
            registry=registry,
            parents_provider=list,
        )
        sink_calls: list[dict[str, Any]] = []

        try:
            src.start(sink=sink_calls.append)
            coll.emit({"type": "cluster_state", "ws_id": "c1", "state": "running"})
            # Daemon thread loop has 1.0s queue timeout; poll briefly.
            for _ in range(20):
                if sink_calls:
                    break
                time.sleep(0.05)
            assert len(sink_calls) == 1
            assert sink_calls[0]["ws_id"] == "c1"
        finally:
            src.shutdown()

    def test_shutdown_unregisters_and_joins_thread(self) -> None:
        coll = _FakeCollector()
        registry = ChildrenRegistry()
        src = ClusterChildSource(
            collector=coll,
            registry=registry,
            parents_provider=list,
        )
        src.start(sink=lambda ev: None)
        src.shutdown()
        assert coll.unregistered == coll.queues
        # Second shutdown is a no-op (idempotent).
        src.shutdown()

    def test_start_is_idempotent(self) -> None:
        coll = _FakeCollector()
        registry = ChildrenRegistry()
        src = ClusterChildSource(
            collector=coll,
            registry=registry,
            parents_provider=list,
        )
        try:
            src.start(sink=lambda ev: None)
            src.start(sink=lambda ev: None)
            assert len(coll.queues) == 1
        finally:
            src.shutdown()

    def test_sink_exception_does_not_kill_thread(self) -> None:
        coll = _FakeCollector()
        registry = ChildrenRegistry()
        src = ClusterChildSource(
            collector=coll,
            registry=registry,
            parents_provider=list,
        )
        survived_calls: list[dict[str, Any]] = []
        call_count = [0]

        def flaky_sink(ev: dict[str, Any]) -> None:
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("first one boom")
            survived_calls.append(ev)

        try:
            src.start(sink=flaky_sink)
            coll.emit({"type": "cluster_state", "ws_id": "c1", "state": "x"})
            coll.emit({"type": "cluster_state", "ws_id": "c2", "state": "y"})
            for _ in range(40):
                if survived_calls:
                    break
                time.sleep(0.05)
            assert len(survived_calls) == 1
            assert survived_calls[0]["ws_id"] == "c2"
        finally:
            src.shutdown()


# Multi-subscriber observer tests for ``SessionManager.subscribe_to_state``
# / ``unsubscribe_from_state`` live in ``test_session_manager.py`` where
# the proper FakeAdapter / FakeStorage construction helpers already exist.
