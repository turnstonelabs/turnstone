"""Strategy interface for delivering child workstream lifecycle events.

Two implementations bind to the unified :class:`ChildrenRegistry`:

- :class:`SameNodeChildSource` — subscribes to a :class:`SessionManager`'s
  state-change callbacks. In-process, no transport. For interactive
  workstreams that spawn children locally (no cluster routing).
- :class:`ClusterChildSource` — subscribes to a :class:`ClusterCollector`'s
  listener channel and runs a daemon thread that drains the queue and
  pushes events to the sink. For coordinator workstreams whose
  children are routed across the cluster by hash bucket.

The strategy doesn't translate events into UI-shaped payloads; that's
the sink's job. This split keeps the strategy generic across kinds and
lets the consumer (e.g. ``CoordinatorAdapter._dispatch_child_event``)
own the per-kind translation.

Sink signature: ``Callable[[dict[str, Any]], None]``. The sink is
responsible for filtering by registry membership; strategies push raw
events without registry-side filtering so the sink can decide whether
to act based on its own state.
"""

from __future__ import annotations

import queue
import threading
from typing import TYPE_CHECKING, Any, Protocol

from turnstone.core.log import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from turnstone.core.children_registry import ChildrenRegistry
    from turnstone.core.workstream import WorkstreamState

log = get_logger(__name__)


class ChildSource(Protocol):
    """Subscription strategy for child workstream lifecycle events."""

    def start(self, sink: Callable[[dict[str, Any]], None]) -> None:
        """Begin delivering events to ``sink``. Idempotent."""

    def shutdown(self) -> None:
        """Stop the strategy. Idempotent; safe to call multiple times."""


class _CollectorProtocol(Protocol):
    """Subset of :class:`ClusterCollector` that :class:`ClusterChildSource` consumes.

    Defined here (not imported) to keep ``turnstone/core/`` free of
    ``turnstone/console/`` imports AND to let test fakes satisfy the
    type signature without subclassing the real collector.
    """

    def get_snapshot_and_register(self, q: queue.Queue[dict[str, Any]]) -> dict[str, Any]:
        """Register ``q`` and return the current snapshot."""

    def unregister_listener(self, q: queue.Queue[dict[str, Any]]) -> None:
        """Drop ``q`` from the listener set."""


class _ManagerProtocol(Protocol):
    """Subset of :class:`SessionManager` that :class:`SameNodeChildSource` consumes.

    Lets test fakes participate in the strategy's typed surface
    without forcing a full SessionManager construction.
    """

    def subscribe_to_state(self, callback: Callable[[str, WorkstreamState], None]) -> None:
        """Register ``callback`` for state-change events."""

    def unsubscribe_from_state(self, callback: Callable[[str, WorkstreamState], None]) -> None:
        """Remove a previously-registered ``callback``."""


class SameNodeChildSource:
    """In-process child events via :class:`SessionManager` state observer.

    Subscribes to the manager's state-change callbacks (registered via
    :meth:`SessionManager.subscribe_to_state`). For each transition on
    a workstream that's a known child (per the
    :class:`ChildrenRegistry` reverse index), synthesises a
    cluster-state-shaped event and pushes it to the sink.

    Used by interactive workstreams when they gain spawn capability —
    children live on the same node as the parent, so no cluster routing
    is needed and event fan-out is in-process.
    """

    def __init__(
        self,
        manager: _ManagerProtocol,
        registry: ChildrenRegistry,
    ) -> None:
        self._manager = manager
        self._registry = registry
        self._sink: Callable[[dict[str, Any]], None] | None = None
        self._callback: Callable[[str, WorkstreamState], None] | None = None

    def start(self, sink: Callable[[dict[str, Any]], None]) -> None:
        if self._callback is not None:
            return  # idempotent — already started
        self._sink = sink

        def _on_state(ws_id: str, state: WorkstreamState) -> None:
            sink_fn = self._sink
            if sink_fn is None:
                return
            # Cheap pre-filter: skip dispatch for transitions on
            # workstreams that aren't children of any in-memory parent.
            # ``has_children`` is a lock-free dict-truthiness read; the
            # ``parent_for`` call below would otherwise acquire the
            # registry lock on every state change even when no
            # children exist (the steady state for an interactive
            # manager). The cluster strategy can't pre-filter because
            # the collector queue carries all events; here we have the
            # information to skip the synthesis entirely.
            if not self._registry.has_children():
                return
            if self._registry.parent_for(ws_id) is None:
                return
            # ``pending_approval_detail`` deliberately omitted — the
            # field was removed from cluster_state end-to-end in the
            # Stage 3 cleanup pass. Approval items arrive via bulk
            # fetch; verdicts via the explicit intent_verdict event;
            # resolution via approval_resolved.
            event = {
                "type": "cluster_state",
                "ws_id": ws_id,
                "state": state.value,
                "node_id": "",
                "tokens": 0,
                "activity_state": "",
            }
            try:
                sink_fn(event)
            except Exception:
                log.debug("same_node_child_source.sink_failed", exc_info=True)

        self._callback = _on_state
        self._manager.subscribe_to_state(_on_state)

    def shutdown(self) -> None:
        cb = self._callback
        if cb is None:
            return
        try:
            self._manager.unsubscribe_from_state(cb)
        except Exception:
            log.debug("same_node_child_source.unsubscribe_failed", exc_info=True)
        self._callback = None
        self._sink = None


class ClusterChildSource:
    """Cross-node child events via :class:`ClusterCollector` subscription.

    Refactor of the existing fan-out machinery from
    ``CoordinatorAdapter`` (was ``_collector_queue`` +
    ``_fanout_thread`` + ``_fanout_loop``). Subscribes as a listener on
    the collector's broadcast channel and runs a daemon thread that
    drains the queue, pushing each event to the sink.

    On :meth:`start`, also primes the registry from the collector's
    snapshot so a parent that re-installs after a console restart sees
    its already-live children without waiting for the next state tick.
    The ``parents_provider`` callback returns the set of in-memory
    parent ws_ids for snapshot filtering — only children whose parent
    is currently installed get merged.
    """

    def __init__(
        self,
        collector: _CollectorProtocol,
        registry: ChildrenRegistry,
        *,
        parents_provider: Callable[[], Iterable[str]],
    ) -> None:
        self._collector = collector
        self._registry = registry
        self._parents_provider = parents_provider
        self._sink: Callable[[dict[str, Any]], None] | None = None
        self._queue: queue.Queue[dict[str, Any]] | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self, sink: Callable[[dict[str, Any]], None]) -> None:
        if self._thread is not None and self._thread.is_alive():
            return  # idempotent — already started
        self._sink = sink
        self._queue = queue.Queue(maxsize=1000)
        snapshot = self._collector.get_snapshot_and_register(self._queue)
        self._prime_from_snapshot(snapshot)
        self._stop.clear()
        t = threading.Thread(
            target=self._loop,
            name="cluster-child-source",
            daemon=True,
        )
        self._thread = t
        t.start()

    def shutdown(self) -> None:
        self._stop.set()
        t = self._thread
        q = self._queue
        coll = self._collector
        self._thread = None
        self._queue = None
        if coll is not None and q is not None:
            try:
                coll.unregister_listener(q)
            except Exception:
                log.debug(
                    "cluster_child_source.unregister_listener_failed",
                    exc_info=True,
                )
        if t is not None:
            t.join(timeout=2.0)
        self._sink = None

    def _loop(self) -> None:
        q = self._queue
        if q is None:
            return
        while not self._stop.is_set():
            try:
                event = q.get(timeout=1.0)
            except queue.Empty:
                continue
            sink = self._sink
            if sink is None:
                continue
            try:
                sink(event)
            except Exception:
                log.debug("cluster_child_source.dispatch_failed", exc_info=True)

    def _prime_from_snapshot(self, snapshot: dict[str, Any]) -> None:
        """Populate the registry from a collector snapshot.

        For every workstream in the snapshot whose ``parent_ws_id``
        names a currently-installed parent (per ``parents_provider``),
        merge it into the registry. Caller-installed parents that
        appear in the snapshot are seeded; unknown parents are skipped
        — they'll be picked up by the live fan-out path once their
        ``ws_created`` event arrives.
        """
        nodes = snapshot.get("nodes", []) if isinstance(snapshot, dict) else []
        if not nodes:
            return
        known_parents = set(self._parents_provider())
        if not known_parents:
            return
        by_parent: dict[str, list[str]] = {}
        for node in nodes:
            for entry in node.get("workstreams", []) or []:
                parent = entry.get("parent_ws_id") or ""
                child_id = entry.get("id") or ""
                if not parent or not child_id or parent not in known_parents:
                    continue
                by_parent.setdefault(parent, []).append(child_id)
        for parent, kids in by_parent.items():
            self._registry.merge_children(parent, kids)
