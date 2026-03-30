"""Console routing layer — routes workstream requests to server nodes.

Maintains an in-memory flat array of 65536 bucket->NodeRef entries populated
from the hash_ring_buckets table.  Routing is O(1): cache[int(ws_id[:4], 16)].
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

from turnstone.core.hash_ring import RING_SIZE, NoAvailableNodeError

if TYPE_CHECKING:
    from turnstone.core.storage._protocol import StorageBackend

log = logging.getLogger("turnstone.console.router")


@dataclass(frozen=True, slots=True)
class NodeRef:
    """A server node that can receive proxied requests."""

    node_id: str
    url: str


class ConsoleRouter:
    """Routes workstream operations to the correct server node.

    Maintains an in-memory flat array of 65536 bucket->NodeRef entries,
    populated from the hash_ring_buckets table.  All routing is a
    single O(1) array lookup: ``cache[int(ws_id[:4], 16)]``.
    """

    def __init__(self, storage: StorageBackend) -> None:
        self._storage = storage
        self._cache: list[NodeRef | None] = [None] * RING_SIZE
        self._overrides: dict[str, NodeRef] = {}
        self._version: int = 0
        self._refresh_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Cache management
    # ------------------------------------------------------------------

    def refresh_cache(self) -> bool:
        """Reload the assignment cache from DB.

        Thread-safe: if another thread is already refreshing, this call
        returns False immediately (the other thread's refresh will apply).
        Returns True if the cache changed compared to the previous load.
        """
        if not self._refresh_lock.acquire(blocking=False):
            return False  # another thread is refreshing
        try:
            return self._refresh_cache_locked()
        finally:
            self._refresh_lock.release()

    def _refresh_cache_locked(self) -> bool:
        """Inner refresh — must be called with _refresh_lock held."""
        # Load node URLs from services table
        members = self._storage.list_services("server", max_age_seconds=120)
        nodes: dict[str, NodeRef] = {
            m["service_id"]: NodeRef(m["service_id"], m["url"]) for m in members
        }

        # Load bucket assignments into flat array
        buckets = self._storage.list_ring_buckets()
        new_cache: list[NodeRef | None] = [None] * RING_SIZE
        for row in buckets:
            ref = nodes.get(row["node_id"])
            if ref is not None:
                new_cache[row["bucket"]] = ref

        # Load per-workstream overrides (pinned workstreams)
        overrides = self._storage.list_workstream_overrides()
        new_overrides: dict[str, NodeRef] = {}
        for row in overrides:
            ref = nodes.get(row["node_id"])
            if ref is not None:
                new_overrides[row["ws_id"]] = ref

        changed = new_cache != self._cache or new_overrides != self._overrides

        # Atomic swap
        self._overrides = new_overrides
        self._cache = new_cache

        return changed

    def check_version(self) -> bool:
        """Poll the rebalancer version and refresh if it changed.

        Returns True if a refresh was triggered.
        """
        setting = self._storage.get_system_setting("rebalancer_version", node_id="")
        if setting is not None:
            try:
                version = int(json.loads(setting.get("value", "0")))
            except (json.JSONDecodeError, TypeError, ValueError):
                version = 0
        else:
            version = 0

        if version != self._version:
            self.refresh_cache()
            self._version = version
            return True
        return False

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------

    def route(self, ws_id: str) -> NodeRef:
        """Route a workstream to its assigned node.

        Priority:
        1. Per-workstream override (pinned to a specific node)
        2. Bucket assignment (first 4 hex chars -> array index)
        """
        ref = self._overrides.get(ws_id)
        if ref is not None:
            return ref
        bucket = int(ws_id[:4], 16)
        ref = self._cache[bucket]
        if ref is None:
            raise NoAvailableNodeError(f"bucket {bucket} not assigned")
        return ref

    def route_url(self, ws_id: str) -> str:
        """Convenience — return just the URL for the target node."""
        return self.route(ws_id).url

    # ------------------------------------------------------------------
    # Readiness and introspection
    # ------------------------------------------------------------------

    def is_ready(self) -> bool:
        """Return True if at least one bucket is assigned."""
        return any(ref is not None for ref in self._cache)

    def node_count(self) -> int:
        """Count distinct nodes present in the cache."""
        return len({ref.node_id for ref in self._cache if ref is not None})

    # ------------------------------------------------------------------
    # Workstream ID generation
    # ------------------------------------------------------------------

    def generate_ws_id_for_node(self, node_id: str) -> str:
        """Generate a routable workstream ID targeting *node_id*.

        The first 4 hex chars encode a bucket owned by the node; the
        remaining 28 hex chars are random (32 chars total).
        """
        for bucket, ref in enumerate(self._cache):
            if ref is not None and ref.node_id == node_id:
                return f"{bucket:04x}" + secrets.token_hex(14)
        raise NoAvailableNodeError(f"no bucket assigned to node {node_id!r}")
