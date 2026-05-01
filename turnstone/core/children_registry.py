"""Universal parent → children registry for SessionManager.

Pure data + lookups; no IO, no transport. Lifted from
:class:`turnstone.console.coordinator_adapter.CoordinatorAdapter` where
it lived bound to the coordinator kind. The lift is what lets the
``ChildSource`` strategies (Step 2) plug into a single shared primitive
regardless of whether children are local (interactive) or cluster-routed
(coordinator).

Storage rebuild and snapshot priming happen in the caller — typically
the ``ChildSource`` implementation that owns the relevant transport.
The registry exposes :meth:`merge_children` for bulk seeding so callers
that compute child id lists from any source can feed them in without
the registry needing to know about storage shapes or collector
snapshots.

Threading: every public method is internally locked. Helpers suffixed
``_locked`` require the caller to already hold :attr:`_lock`.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable


class ChildrenRegistry:
    """Tracks parent → children + reverse lookup for in-memory parents.

    The forward index (``_children``) is parent_ws_id → set of child ws_ids.
    The reverse index (``_child_to_parent``) is child_ws_id → parent_ws_id.
    The presence map (``_active``) is parent_ws_id → UI ref, used by the
    dispatch path to atomically check-and-route in one lock acquisition.

    Closed / deleted children stay in the registry until their owning
    parent is uninstalled — the tree UI keeps rendering them grayed out;
    state authority lives in storage, not here.
    """

    def __init__(self) -> None:
        self._children: dict[str, set[str]] = {}
        self._child_to_parent: dict[str, str] = {}
        self._lock = threading.Lock()
        self._active: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Lifecycle — install / uninstall a parent
    # ------------------------------------------------------------------

    def install(self, parent_ws_id: str, ui: Any) -> None:
        """Seed the forward set + presence map for a new parent.

        Idempotent — re-installing re-points the UI but leaves the
        existing child set intact. Mirrors the original
        ``_install_coord_registry`` semantics so a coordinator that
        rehydrates after a crash doesn't lose its known-children.
        """
        with self._lock:
            self._children.setdefault(parent_ws_id, set())
            self._active[parent_ws_id] = ui

    def uninstall(self, parent_ws_id: str) -> None:
        """Drop a parent: forward set, reverse-index entries, presence.

        No-op if the parent is unknown. Used by close / eviction paths.
        """
        with self._lock:
            self._uninstall_locked(parent_ws_id)

    # ------------------------------------------------------------------
    # Mutation — register children under a parent
    # ------------------------------------------------------------------

    def add_child(self, parent_ws_id: str, child_ws_id: str) -> Any | None:
        """Register a child under a parent. Returns parent's UI or None.

        Returns the parent's UI on success (so the dispatch path can
        atomically check-and-route in one lock acquisition). Returns
        ``None`` if the parent isn't installed (concurrent close /
        eviction) or if the child is already registered (duplicate
        ws_created from the cluster fan-out).
        """
        with self._lock:
            ui = self._active.get(parent_ws_id)
            if ui is None:
                return None
            existing = self._children.setdefault(parent_ws_id, set())
            if child_ws_id in existing:
                return None
            existing.add(child_ws_id)
            self._child_to_parent[child_ws_id] = parent_ws_id
            return ui

    def merge_children(self, parent_ws_id: str, child_ws_ids: Iterable[str]) -> None:
        """Bulk-merge child_ids under a parent. Idempotent.

        Sole bulk write-path — used by both storage-seeded rebuilds and
        snapshot-seeded priming so reverse-index ordering invariants
        hold regardless of which seed source races first.
        """
        with self._lock:
            self._merge_locked(parent_ws_id, child_ws_ids)

    # ------------------------------------------------------------------
    # Lookups
    # ------------------------------------------------------------------

    def parent_for(self, child_ws_id: str) -> str | None:
        """Reverse lookup: which parent owns this child? O(1)."""
        with self._lock:
            return self._child_to_parent.get(child_ws_id)

    def has_children(self) -> bool:
        """Lock-free fast path: any child registered under any parent?

        Reads ``bool(self._child_to_parent)`` without taking the lock.
        Dict-truthiness is a single GIL-atomic read, so callers on
        the hot state-broadcast path can short-circuit without paying
        the lock acquisition when the registry is empty (the steady
        state for an interactive manager today). The answer is best-
        effort — if a child is added concurrently with the read the
        caller may falsely return ``False``, but the next state event
        will pick up the change correctly.
        """
        return bool(self._child_to_parent)

    def children_of(self, parent_ws_id: str) -> list[str]:
        """Snapshot copy of the parent's child ws_ids.

        Returned list is a copy so callers can iterate without holding
        the registry lock during per-child work. A mutation racing with
        the snapshot either lands before (included) or after (excluded)
        — both outcomes are safe for cascade-style dispatch.
        """
        with self._lock:
            child_set = self._children.get(parent_ws_id)
            return list(child_set) if child_set else []

    def ui_for(self, parent_ws_id: str) -> Any | None:
        """Look up the UI registered for a parent."""
        with self._lock:
            return self._active.get(parent_ws_id)

    def parents(self) -> list[str]:
        """Snapshot copy of installed parent ws_ids."""
        with self._lock:
            return list(self._active)

    # ------------------------------------------------------------------
    # Locked helpers — caller must hold ``self._lock``
    # ------------------------------------------------------------------

    def _merge_locked(self, parent_ws_id: str, child_ws_ids: Iterable[str]) -> None:
        """Idempotent merge under caller's lock. Empty/falsy ids skipped."""
        existing = self._children.setdefault(parent_ws_id, set())
        for cid in child_ws_ids:
            if cid and cid not in existing:
                existing.add(cid)
                self._child_to_parent[cid] = parent_ws_id

    def _uninstall_locked(self, parent_ws_id: str) -> None:
        """Pop forward set + presence + own reverse-index entries.

        Defensive: only clears reverse entries that still point at
        ``parent_ws_id``. Schema-shaped reassignments (rare but
        possible) shouldn't orphan the new owner's entry.
        """
        child_set = self._children.pop(parent_ws_id, None)
        self._active.pop(parent_ws_id, None)
        if child_set is None:
            return
        for cid in child_set:
            if self._child_to_parent.get(cid) == parent_ws_id:
                self._child_to_parent.pop(cid, None)
