"""Console routing layer — routes workstream requests to server nodes.

Uses durable execution requirements before placement overrides and rendezvous
(HRW) hashing over the live ``services`` table. Requirements are read from the
saved row on each resolution, so a missing cache entry never releases a pin.

**Cache ownership**: the router's cache is push-driven by the
collector's background discovery thread. ``route()`` also reads the saved
workstream row; async callers must run it in a worker thread.
The collector calls ``refresh_cache()`` on every discovery tick and
again immediately on observed membership changes (node_joined /
node_lost).  ``force_refresh()`` exists for the 404-retry path;
callers must wrap it in ``asyncio.to_thread`` when invoking from an
async handler so its DB read doesn't stall the event loop.

Each resolution costs one indexed workstream read and, for flexible placement,
O(N) hash computes. Node membership and placement overrides remain cached.
"""

from __future__ import annotations

import json
import secrets
import threading
from typing import TYPE_CHECKING, Any

from turnstone.core.node_affinity import NodeAffinityError, parse_required_node_id
from turnstone.core.rendezvous import NoAvailableNodeError, NodeRef, select

if TYPE_CHECKING:
    from collections.abc import Callable

    from turnstone.core.storage._protocol import StorageBackend

# Brute-force attempt cap for ``generate_ws_id_for_node``.  Expected
# attempts for a weight-w_t target in a cluster with total weight W is
# W/w_t (the target wins w_t/W of keys).  At typical scale (N≤50,
# weights ∈ {1..4}) the worst case is ~200 attempts; the cap is well
# above that to absorb pathologically-skewed configurations without
# spurious failures.
_GENERATE_ATTEMPT_CAP = 65_536


def _parse_weight(metadata_json: str) -> int:
    """Pull the ``weight`` key out of a service-registry metadata blob.

    A single corrupt row must not abort the cache refresh — fall back to
    weight=1 on any parse / type / value error, including JSON shapes
    that aren't dicts (``null``, lists, scalars).
    """
    try:
        meta = json.loads(metadata_json or "{}")
    except (json.JSONDecodeError, TypeError):
        return 1
    if not isinstance(meta, dict):
        return 1
    try:
        weight = int(meta.get("weight", 1))
    except (TypeError, ValueError):
        return 1
    return max(weight, 1)


class ConsoleRouter:
    """Routes workstream operations to the correct server node.

    Thread-safe.  All state mutation goes through ``_lock``; lookups
    snapshot the node list under the lock and run the rendezvous select
    outside it (the select is a pure function over an immutable list).
    """

    def __init__(self, storage: StorageBackend) -> None:
        self._storage = storage
        self._lock = threading.Lock()
        self._nodes: list[NodeRef] = []
        self._overrides: dict[str, NodeRef] = {}
        self._refresh_lock = threading.Lock()
        # Monotonic counter bumped on every successful refresh — used by
        # the metrics gauge.  Strictly increasing so dashboards can
        # detect when membership stops being refreshed.
        self._refresh_counter: int = 0

    # ------------------------------------------------------------------
    # Cache management
    # ------------------------------------------------------------------

    def refresh_cache(self) -> bool:
        """Reload live-node list + overrides from storage.

        Thread-safe: if another thread is already refreshing, this call
        returns False immediately (the in-flight refresh will publish
        the latest state).  Returns True if the membership changed
        compared to the previous load.

        Called by the collector's discovery thread on every tick — never
        invoke from an async event-loop handler (this is a blocking DB
        read).  Use ``force_refresh`` if you need a guaranteed-fresh
        view, and wrap that in ``asyncio.to_thread``.
        """
        if not self._refresh_lock.acquire(blocking=False):
            return False
        try:
            return self._refresh_locked()
        finally:
            self._refresh_lock.release()

    def force_refresh(self) -> bool:
        """Refresh now, blocking if another refresh is in progress.

        Used by the 404-retry path in the routing proxy when ``route()``
        sent the request to a node that doesn't have the workstream —
        the retry needs a guaranteed-fresh view of membership +
        overrides before giving up.

        Async callers must wrap this in ``asyncio.to_thread`` — the
        method takes a blocking lock and issues storage queries.
        """
        with self._refresh_lock:
            return self._refresh_locked()

    def _refresh_locked(self) -> bool:
        services = self._storage.list_services("server", max_age_seconds=120)
        new_nodes = sorted(
            (
                NodeRef(
                    node_id=s["service_id"],
                    url=s["url"],
                    weight=_parse_weight(s.get("metadata", "{}")),
                )
                for s in services
                if s.get("service_id") and s.get("url")
            ),
            key=lambda n: n.node_id,
        )
        nodes_by_id = {n.node_id: n for n in new_nodes}

        overrides_rows = self._storage.list_workstream_overrides()
        new_overrides: dict[str, NodeRef] = {}
        for row in overrides_rows:
            ref = nodes_by_id.get(row["node_id"])
            if ref is not None:
                new_overrides[row["ws_id"]] = ref

        with self._lock:
            changed = new_nodes != self._nodes or new_overrides != self._overrides
            self._nodes = new_nodes
            self._overrides = new_overrides
            self._refresh_counter += 1
        return changed

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------

    def route(
        self, ws_id: str, *, can_read: Callable[[dict[str, Any]], bool] | None = None
    ) -> NodeRef:
        """Route a workstream to its assigned node.

        Priority:
        A durable requirement wins over location overrides and HRW. Invisible
        rows use the same placement as unknown IDs, avoiding a private-source
        oracle through unavailable-node responses. Callers without a filter
        are trusted internal consumers.
        """
        if not ws_id:
            raise NoAvailableNodeError("invalid ws_id: empty")
        try:
            row = self._storage.get_workstream(ws_id)
            visible = row is None or can_read is None or can_read(row)
            required = (
                parse_required_node_id(row.get("required_node_id")) if row and visible else None
            )
        except Exception as exc:
            raise NoAvailableNodeError("Cannot resolve workstream execution requirement") from exc
        if required:
            return self.required_node(required)
        with self._lock:
            ref = self._overrides.get(ws_id) if visible else None
            if ref is not None:
                return ref
            nodes = self._nodes  # snapshot — list is replaced wholesale on refresh
        if not nodes:
            raise NoAvailableNodeError("no live nodes")
        return select(ws_id, nodes)

    def required_node(self, node_id: str) -> NodeRef:
        """Resolve one required identity without permitting placement fallback."""
        with self._lock:
            for ref in self._nodes:
                if ref.node_id == node_id:
                    return ref
        raise NodeAffinityError(node_id, unavailable=True)

    def remember_override(self, ws_id: str, ref: NodeRef) -> None:
        """Publish one just-confirmed durable placement into this cache.

        Routed creation returns only after the node transaction has persisted
        its workstream override.  Publishing that successful route here closes
        the collector-delay window for immediate lookup, attachment, delete,
        and liveness requests handled by the same console process.  A later
        full refresh remains authoritative and replaces the whole map.
        """
        if not ws_id:
            raise ValueError("ws_id required")
        # Serialize with the storage snapshot + wholesale publish in
        # ``_refresh_locked``.  Taking only ``_lock`` here would let a refresh
        # that queried before the node commit overwrite this newer placement
        # after we returned it to the caller.
        with self._refresh_lock, self._lock:
            self._overrides[ws_id] = ref

    def route_url(self, ws_id: str) -> str:
        """Convenience — return just the URL for the target node."""
        return self.route(ws_id).url

    # ------------------------------------------------------------------
    # Readiness and introspection
    # ------------------------------------------------------------------

    def is_ready(self) -> bool:
        """True if the router knows about at least one live node."""
        with self._lock:
            return bool(self._nodes)

    def node_count(self) -> int:
        """Number of distinct live nodes in the current view."""
        with self._lock:
            return len(self._nodes)

    @property
    def version(self) -> int:
        """Monotonic counter bumped on every successful cache refresh.

        Surfaced by the collector's ``set_ring_info`` gauge so a
        dashboard can detect when membership stops being refreshed.
        Strictly increasing across the process lifetime.
        """
        with self._lock:
            return self._refresh_counter

    # ------------------------------------------------------------------
    # Workstream ID generation
    # ------------------------------------------------------------------

    def generate_ws_id_for_node(self, node_id: str) -> str:
        """Generate a 32-hex-char ws_id where rendezvous selects *node_id*.

        Brute-force loop: pick a random candidate, check whether HRW
        picks the target.  Expected attempts ≈ ``W/w_t`` where ``W`` is
        total cluster weight and ``w_t`` is the target's weight.  Cap
        at ``_GENERATE_ATTEMPT_CAP`` to bound worst case for skewed
        configurations.
        """
        with self._lock:
            nodes = list(self._nodes)
        if not nodes:
            raise NoAvailableNodeError(f"no live node {node_id!r}")
        if not any(n.node_id == node_id for n in nodes):
            raise NoAvailableNodeError(f"no live node {node_id!r}")

        for _ in range(_GENERATE_ATTEMPT_CAP):
            candidate = secrets.token_hex(16)
            if select(candidate, nodes).node_id == node_id:
                return candidate
        raise NoAvailableNodeError(
            f"could not generate ws_id targeting {node_id!r} after {_GENERATE_ATTEMPT_CAP} attempts"
        )
