"""CoordinatorAdapter — SessionManager bridge for coordinator workstreams.

Emits lifecycle events via the ``ClusterCollector``'s console pseudo-node
fan-out. ``cleanup_ui`` ports the listener-queue + approval/plan event
unblocks from the old ``CoordinatorManager._cleanup`` path. Also hosts
the children-registry and cluster-event fan-out thread — these are
coord-specific concerns the shared :class:`SessionManager` doesn't know
about, so they live here rather than polluting the manager's surface.

The adapter takes a late-bound reference to its owning manager via
:meth:`attach` (called right after the manager is constructed). Some
paths need ``manager.get(ws_id)`` to read a live coordinator's user_id
for the storage-seeded children rebuild.
"""

from __future__ import annotations

import queue
import threading
from typing import TYPE_CHECKING, Any

from turnstone.core import session_worker
from turnstone.core.adapters._ui_cleanup import cleanup_session_ui
from turnstone.core.log import get_logger
from turnstone.core.workstream import Workstream, WorkstreamKind, WorkstreamState

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from turnstone.console.collector import ClusterCollector
    from turnstone.console.coordinator_ui import ConsoleCoordinatorUI
    from turnstone.core.attachments import Attachment
    from turnstone.core.session import ChatSession, SessionUI
    from turnstone.core.session_manager import SessionManager

log = get_logger(__name__)


class CoordinatorAdapter:
    """Bridges SessionManager to the console's coordinator transport."""

    kind: WorkstreamKind = WorkstreamKind.COORDINATOR

    def __init__(
        self,
        *,
        collector: ClusterCollector,
        ui_factory: Callable[[Workstream], ConsoleCoordinatorUI],
        session_factory: Callable[..., ChatSession],
    ) -> None:
        self._collector = collector
        self._ui_factory = ui_factory
        self._session_factory = session_factory
        # Late-bound via ``attach`` after the SessionManager is built.
        # The manager constructor takes the adapter, so we can't pass
        # the manager to the adapter's ``__init__`` — break the cycle
        # with a setter called from console startup.
        self._manager: SessionManager | None = None
        # Per-coordinator known-child ws_id set. Populated lazily on
        # create/open from storage and updated live as the cluster fan-out
        # thread sees ws_created events with matching parent_ws_id.
        # Closed / deleted children stay in the registry so the tree UI
        # can keep rendering them grayed out; the authoritative render
        # path reads storage for state. Bounded by eventual coordinator
        # close/eviction.
        self._children: dict[str, set[str]] = {}
        # Reverse index for O(1) child → coord lookup on every cluster
        # event. Without this, every cluster event incurs a linear scan
        # over every coordinator's child set while holding the fan-out
        # lock — a hot-path tax that scales with both active coordinators
        # and their retained-history depth.
        self._child_to_coord: dict[str, str] = {}
        self._children_lock = threading.Lock()
        # Cluster-event fan-out: subscribes to the ClusterCollector's
        # listener channel, filters by known child ws_ids, and re-emits
        # child_ws_* events on the matching coordinator's UI. Configured
        # lazily via ``start_child_event_fanout(collector)`` from the
        # console lifespan once both the manager and collector exist.
        self._collector_queue: queue.Queue[dict[str, Any]] | None = None
        self._fanout_thread: threading.Thread | None = None
        self._fanout_stop = threading.Event()
        # Coord-ws-id → UI map for the fan-out dispatch path. Read
        # and written under ``self._children_lock`` alongside the
        # forward/reverse child maps. (Previously the value was
        # ``(user_id, ui)`` with a copy-on-write dict swap so the
        # dispatch could read it lock-free — but _dispatch_child_event
        # already re-validates the parent under _children_lock anyway,
        # so the lock-free snapshot was premature. The user_id half is
        # also dead after a46dab1 dropped row-level ownership gates.)
        self._active_coords: dict[str, Any] = {}

    def attach(self, manager: SessionManager) -> None:
        """Late-bind the owning :class:`SessionManager`.

        Called once from the console lifespan right after the manager is
        constructed. The manager is needed for ``manager.get(ws_id)``
        inside the storage-seeded children rebuild (which must read the
        coordinator's live ``user_id`` for the SQL tenant filter).
        """
        self._manager = manager

    # ------------------------------------------------------------------
    # Lifecycle events — fan out via the ClusterCollector's pseudo-node
    # ------------------------------------------------------------------

    def emit_created(self, ws: Workstream) -> None:
        # Fresh create: no persisted children exist yet, so skip the
        # storage-seeded rebuild (perf-3). The fan-out registry still
        # needs the empty forward/presence entries so
        # ``_dispatch_child_event`` recognises this coordinator when
        # its first child is spawned.
        self._install_coord_registry(ws)
        self._fanout_console_ws_created(ws)

    def emit_rehydrated(self, ws: Workstream) -> None:
        # Resurrected coordinator: the subtree IS persisted, so pull
        # it from storage after the registry seed so a ``ws_created``
        # for an already-spawned child that fires mid-rebuild merges
        # cleanly.
        self._install_coord_registry(ws)
        self._rebuild_children_registry(ws.id)
        self._fanout_console_ws_created(ws)

    def _install_coord_registry(self, ws: Workstream) -> None:
        """Seed the children registry + presence map for ``ws``.

        Shared by ``emit_created`` and ``emit_rehydrated`` — the
        difference between the two is purely whether we then rebuild
        from storage.
        """
        with self._children_lock:
            self._children.setdefault(ws.id, set())
            self._active_coords[ws.id] = ws.ui

    def _fanout_console_ws_created(self, ws: Workstream) -> None:
        try:
            self._collector.emit_console_ws_created(
                ws.id,
                name=ws.name,
                user_id=ws.user_id,
                kind=ws.kind.value,
                state=ws.state.value,
                parent_ws_id=None,
            )
        except Exception:
            log.debug("coord_adapter.created_fanout_failed ws=%s", ws.id[:8], exc_info=True)

    def emit_state(self, ws: Workstream, state: WorkstreamState) -> None:
        """Fan a state-change with the rich payload snapshot to the collector.

        Reads tokens / context_ratio / activity / content from
        ``ws.ui`` via the lifted :meth:`SessionUIBase.snapshot_and_consume_state_payload`
        helper (which handles the IDLE/ERROR ``_ws_turn_content``
        consume + clear under ``_ws_lock``). Pre-rich-payload this
        broadcast was state-only; the dashboard's coord row now
        renders the same tokens / activity / content fields
        interactive rows do.

        Defensive: ``ws.ui`` can be ``None`` mid-eviction; in that
        case we still broadcast the state-change with empty rich
        fields so the dashboard's coord row still flips state.
        """
        ui = ws.ui
        if ui is not None and hasattr(ui, "snapshot_and_consume_state_payload"):
            payload = ui.snapshot_and_consume_state_payload(state.value)
        else:
            payload = {
                "tokens": 0,
                "context_ratio": 0.0,
                "activity": "",
                "activity_state": "",
                "content": "",
            }
        try:
            self._collector.emit_console_ws_state(
                ws.id,
                state.value,
                tokens=payload["tokens"],
                context_ratio=payload["context_ratio"],
                activity=payload["activity"],
                activity_state=payload["activity_state"],
                content=payload["content"],
            )
        except Exception:
            log.debug("coord_adapter.state_fanout_failed ws=%s", ws.id[:8], exc_info=True)

    def emit_closed(
        self,
        ws_id: str,
        *,
        reason: str = "closed",
        name: str = "",
    ) -> None:
        # ``reason`` / ``name`` accepted for Protocol compatibility but
        # the collector's emit_console_ws_closed doesn't propagate
        # them — the console frontend's "evicted" special-case + name
        # toast only fire for real-node (interactive) ws_closed events.
        del reason, name
        # Drop the coordinator's children-registry entries AND its
        # presence slot. Mirrors the eviction/close paths from the old
        # CoordinatorManager (which did the same under _children_lock
        # + _lock respectively). A plain _children.pop without clearing
        # the reverse index would leak every evicted coordinator's
        # child→parent pointers forever.
        with self._children_lock:
            self._pop_coord_registry_locked(ws_id)
            self._active_coords.pop(ws_id, None)
        try:
            self._collector.emit_console_ws_closed(ws_id)
        except Exception:
            log.debug("coord_adapter.closed_fanout_failed ws=%s", ws_id[:8], exc_info=True)

    # ------------------------------------------------------------------
    # UI cleanup — unblock pending events + broadcast ws_closed to listeners
    # ------------------------------------------------------------------

    def cleanup_ui(self, ws: Workstream) -> None:
        """Unblock pending events + close the session.

        Mirrors the old ``CoordinatorManager._cleanup`` (which itself
        delegated to ``WorkstreamManager._cleanup_ui``). Delegates to
        :func:`cleanup_session_ui` — shared with the interactive
        adapter which runs the identical sequence.
        """
        cleanup_session_ui(ws)

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def build_ui(self, ws: Workstream) -> SessionUI:
        return self._ui_factory(ws)

    def build_session(
        self,
        ws: Workstream,
        *,
        skill: str | None = None,
        model: str | None = None,
        client_type: str = "",
        **extra: Any,
    ) -> ChatSession:
        """Delegate to the injected coordinator ``session_factory`` closure."""
        del client_type  # coordinator session_factory doesn't use client_type
        return self._session_factory(
            ws.ui,
            model,
            ws.id,
            skill=skill,
            kind=ws.kind,
            parent_ws_id=ws.parent_ws_id,
            **extra,
        )

    # ------------------------------------------------------------------
    # Worker dispatch — delegates to turnstone.core.session_worker
    # ------------------------------------------------------------------

    def send(
        self,
        ws_id: str,
        message: str,
        *,
        attachments: list[Attachment] | None = None,
        send_id: str | None = None,
    ) -> bool:
        """Queue a message onto a coordinator session's ChatSession.

        Returns False if the coordinator isn't loaded in the manager or
        if the worker's pending-message queue is full (caller should
        surface 429 / backpressure). Priority is parsed from the message
        prefix (``/high``, ``/urgent``, etc.) by :meth:`ChatSession.queue_message`.

        Worker spawn / reuse mechanics live in
        :func:`turnstone.core.session_worker.send`; the closures below
        carry coord-specific error surfacing (UI ``on_error`` +
        ``on_state_change=error``) so the dashboard reflects the
        failure instead of a bare ``error`` badge.

        Optional ``attachments`` + ``send_id`` carry create-time
        attachments onto the first turn dispatched by the lifted
        ``create`` handler's ``_coord_create_post_install``. The
        send_id token must match the reservation already taken
        against the attachment rows (see
        :func:`turnstone.core.attachments.reserve_and_resolve_attachments`);
        the worker's failure path unreserves so a worker crash
        doesn't leave the rows soft-locked. Both kwargs default to
        ``None`` so the steady-state ``coord_adapter.send`` call
        sites (no attachments) keep working unchanged.
        """
        mgr = self._manager
        if mgr is None:
            raise RuntimeError(
                "CoordinatorAdapter: manager not attached — call attach(mgr) after construction"
            )
        ws = mgr.get(ws_id)
        if ws is None or ws.session is None:
            return False

        ws_ref = ws
        session = ws.session
        # Capture into locals so the worker closure doesn't pull
        # mutable kwargs through the call-site frame after return.
        _attachments = attachments or None
        _send_id = send_id if _attachments else None
        _user_id = ws.user_id

        def _run() -> None:
            try:
                session.send(message, attachments=_attachments, send_id=_send_id)
            except Exception as exc:
                # Unreserve any attachments we soft-locked for this
                # send_id so the rows return to pending and don't stay
                # locked forever after a worker crash. Mirrors the
                # interactive create-with-attachments worker pattern.
                if _attachments and _send_id:
                    from turnstone.core.memory import (
                        unreserve_attachments as _unreserve,
                    )

                    try:
                        _unreserve(_send_id, ws_ref.id, _user_id)
                    except Exception:
                        log.debug(
                            "coord_adapter.attachment_unreserve_failed ws=%s",
                            ws_ref.id[:8],
                            exc_info=True,
                        )
                log.exception("coord_adapter.worker_failed ws=%s", ws_ref.id[:8])
                # Surface the failure to the coordinator's SSE stream
                # so the operator sees what broke instead of a bare
                # "error" badge — most common cause is a model-alias
                # misconfig (wrong provider for the model) which the
                # raw traceback narrows down quickly.
                ui = ws_ref.ui
                if ui is not None and hasattr(ui, "on_error"):
                    try:
                        ui.on_error(f"{type(exc).__name__}: {exc}")
                    except Exception:
                        log.debug(
                            "coord_adapter.on_error_dispatch_failed ws=%s",
                            ws_ref.id[:8],
                            exc_info=True,
                        )
                # Also mark the workstream state=error so the cluster
                # fan-out + dashboard reflect the failure.
                if ui is not None and hasattr(ui, "on_state_change"):
                    try:
                        ui.on_state_change(WorkstreamState.ERROR.value)
                    except Exception:
                        log.debug(
                            "coord_adapter.error_state_update_failed ws=%s",
                            ws_ref.id[:8],
                            exc_info=True,
                        )

        def _enqueue() -> None:
            # ``queue_message`` takes attachment *ids* + ``queue_msg_id``
            # (which doubles as the cross-table reservation token); the
            # send_id we hold IS that token. Convert Attachment objects
            # to id list at enqueue time so the queued turn picks the
            # files up at dequeue.
            att_ids = [a.attachment_id for a in _attachments] if _attachments else None
            session.queue_message(message, attachment_ids=att_ids, queue_msg_id=_send_id)

        return session_worker.send(
            ws,
            enqueue=_enqueue,
            run=_run,
            thread_name=f"coord-worker-{ws.id[:8]}",
        )

    # ------------------------------------------------------------------
    # Children registry
    # ------------------------------------------------------------------

    def _merge_child_ids_locked(self, coord_ws_id: str, child_ids: Iterable[str]) -> None:
        """Merge ``child_ids`` into ``coord_ws_id``'s forward + reverse maps.

        Caller MUST hold ``self._children_lock``. Idempotent — re-adding
        an existing child is a no-op (the reverse-index pointer is
        already correct). Empty / falsy entries in ``child_ids`` are
        skipped.

        Sole write-path for bulk registry updates so
        ``_rebuild_children_registry`` (storage-seeded) and
        ``_prime_children_from_snapshot`` (collector-seeded) agree on
        ordering and reverse-index invariants.
        """
        existing = self._children.setdefault(coord_ws_id, set())
        for cid in child_ids:
            if cid and cid not in existing:
                existing.add(cid)
                self._child_to_coord[cid] = coord_ws_id

    def _rebuild_children_registry(self, coord_ws_id: str) -> None:
        """Populate ``self._children[coord_ws_id]`` from storage.

        Called on ``emit_created`` (covers both ``create`` — empty result
        set, cheap query — and ``open`` rehydrate where the subtree is
        persisted). Closed / deleted children are intentionally kept in
        the registry so the tree UI can render them grayed out; state is
        authoritative in storage, not here.

        UNIONs with any entries the fan-out thread already added during
        the storage query window. Overwriting would drop that child.
        """
        mgr = self._manager
        if mgr is None:
            raise RuntimeError(
                "CoordinatorAdapter: manager not attached — call attach(mgr) after construction"
            )
        storage = getattr(mgr, "_storage", None)
        if storage is None:
            return
        # Tenant filter: only accept persisted children whose owning
        # user_id matches the coordinator's. Pushed into SQL via the
        # ``user_id`` kwarg so cross-tenant rows never leave the DB. An
        # empty coord_user_id is fail-closed: skip the rebuild entirely
        # rather than matching rows with blank owners (system-owned or
        # legacy rows would otherwise leak into the fan-out set).
        coord_ws = mgr.get(coord_ws_id)
        coord_user_id = coord_ws.user_id if coord_ws is not None else ""
        if not coord_user_id:
            log.debug(
                "coord_adapter.rebuild_skipped_empty_owner coord=%s",
                coord_ws_id[:8],
            )
            return
        # Cap is a sentinel, not a hard limit. Fetch limit+1 so a coord
        # with exactly ``_rebuild_limit`` children doesn't trigger a
        # false-positive warning; the +1 is the sentinel that proves
        # there's at least one more row in storage.
        _rebuild_limit = 10_000
        try:
            rows = storage.list_workstreams(
                limit=_rebuild_limit + 1,
                parent_ws_id=coord_ws_id,
                kind=None,
                user_id=coord_user_id,
            )
        except Exception:
            log.debug(
                "coord_adapter.rebuild_children_failed ws=%s",
                coord_ws_id[:8],
                exc_info=True,
            )
            rows = []
        if len(rows) > _rebuild_limit:
            log.warning(
                "coord_adapter.rebuild_children_truncated ws=%s limit=%d",
                coord_ws_id[:8],
                _rebuild_limit,
            )
            rows = rows[:_rebuild_limit]
        child_ids: list[str] = []
        for r in rows:
            try:
                m = r._mapping
                child_id = m["ws_id"]
            except AttributeError:
                child_id = r[0] if r else ""
            if not child_id:
                continue
            child_ids.append(child_id)
        with self._children_lock:
            self._merge_child_ids_locked(coord_ws_id, child_ids)

    def _coord_for_child(self, child_ws_id: str) -> str | None:
        """Reverse-lookup: which coordinator owns this child ws_id?

        O(1) via the ``_child_to_coord`` reverse index. Cluster events
        fire on every token tick across the cluster; a linear scan here
        turned into a hot-path tax as the retained-history set grew.
        """
        with self._children_lock:
            return self._child_to_coord.get(child_ws_id)

    def children_snapshot(self, coord_ws_id: str) -> list[str]:
        """Return a snapshot of the coordinator's direct child ws_ids.

        Used by ``stop_cascade`` to iterate children without holding the
        registry lock during the per-child HTTP dispatch. A mutation
        racing with the snapshot (child spawned mid-cascade) either
        lands before the snapshot and gets cancelled, or lands after
        and is out of scope for this batch — both outcomes are safe.
        Returns an empty list for unknown coordinators.
        """
        with self._children_lock:
            child_set = self._children.get(coord_ws_id)
            return list(child_set) if child_set else []

    def _pop_coord_registry_locked(self, coord_ws_id: str) -> None:
        """Remove a coordinator's forward set + reverse-index entries.

        Caller MUST hold ``self._children_lock``. Used by close /
        eviction paths so stale coordinators don't leak registry
        entries. No-op if the coordinator is unknown.
        """
        child_set = self._children.pop(coord_ws_id, None)
        if child_set is None:
            return
        for cid in child_set:
            # Defensive: only clear the reverse entry if it still points
            # at THIS coordinator. If a child has since been reassigned
            # (unusual but possible on schema changes), we don't want to
            # orphan the new owner's entry.
            if self._child_to_coord.get(cid) == coord_ws_id:
                self._child_to_coord.pop(cid, None)

    # ------------------------------------------------------------------
    # Cluster-event fan-out thread
    # ------------------------------------------------------------------

    def start_child_event_fanout(self, collector: ClusterCollector) -> None:
        """Subscribe to cluster events and start the filter + re-emit thread.

        Idempotent — calling twice is a no-op (already-started fan-out
        thread stays). Called once from the console lifespan after both
        the collector and the session manager are constructed.
        """
        if self._fanout_thread is not None and self._fanout_thread.is_alive():
            return
        self._collector = collector
        self._collector_queue = queue.Queue(maxsize=1000)
        # Ensure the "console" pseudo-node exists in the snapshot map so
        # emit_console_ws_* calls from create / close / open land on a
        # real node entry the snapshot will surface.
        collector.ensure_console_pseudo_node()
        # Register with the collector — use the existing listener channel
        # the browser SSE fan-out uses; the collector treats our queue as
        # just another subscriber.
        snapshot = collector.get_snapshot_and_register(self._collector_queue)
        # Prime the child registry from the snapshot so a coordinator
        # that opens right after a console restart sees already-live
        # children without waiting for the next ``ws_state`` tick to
        # discover them via the fan-out path.
        self._prime_children_from_snapshot(snapshot)
        # Seed the pseudo-node with any coordinators already loaded in
        # memory when the collector binds. Prevents a race where early
        # creates happened before the collector was wired up and their
        # rows never showed on the snapshot.
        mgr = self._manager
        if mgr is not None:
            for ws in mgr.list_all():
                try:
                    collector.emit_console_ws_created(
                        ws.id,
                        name=ws.name,
                        user_id=ws.user_id or "",
                        kind=WorkstreamKind.COORDINATOR.value,
                        state=ws.state.value,
                        parent_ws_id=None,
                    )
                except Exception:
                    log.debug(
                        "coord_adapter.collector_seed_failed ws=%s",
                        ws.id[:8],
                        exc_info=True,
                    )
        self._fanout_stop.clear()
        t = threading.Thread(
            target=self._fanout_loop,
            name="coord-adapter-child-fanout",
            daemon=True,
        )
        self._fanout_thread = t
        t.start()

    def _prime_children_from_snapshot(self, snapshot: dict[str, Any]) -> None:
        """Populate ``_children`` + ``_child_to_coord`` from a collector snapshot.

        The snapshot's per-node workstreams carry ``parent_ws_id``. For
        every workstream whose parent is an in-memory coordinator,
        record the child so the fan-out filter sees it immediately.
        """
        nodes = snapshot.get("nodes", []) if isinstance(snapshot, dict) else []
        if not nodes:
            return
        mgr = self._manager
        if mgr is None:
            raise RuntimeError(
                "CoordinatorAdapter: manager not attached — call attach(mgr) after construction"
            )
        by_parent: dict[str, list[str]] = {}
        known = {ws.id for ws in mgr.list_all()}
        for node in nodes:
            for entry in node.get("workstreams", []) or []:
                parent = entry.get("parent_ws_id") or ""
                child_id = entry.get("id") or ""
                if not parent or not child_id or parent not in known:
                    continue
                by_parent.setdefault(parent, []).append(child_id)
        if not by_parent:
            return
        with self._children_lock:
            for parent, kids in by_parent.items():
                self._merge_child_ids_locked(parent, kids)

    def shutdown(self) -> None:
        """Stop the fan-out thread and unregister from the collector.

        Safe to call multiple times; idempotent. Invoked from the
        console lifespan teardown so SSE listener queues don't leak.
        """
        self._fanout_stop.set()
        t = self._fanout_thread
        q = self._collector_queue
        coll = self._collector
        self._fanout_thread = None
        self._collector_queue = None
        if coll is not None and q is not None:
            try:
                coll.unregister_listener(q)
            except Exception:
                log.debug("coord_adapter.unregister_listener_failed", exc_info=True)
        if t is not None:
            t.join(timeout=2.0)

    def _fanout_loop(self) -> None:
        """Drain collector events, filter by known children, dispatch."""
        q = self._collector_queue
        if q is None:
            return
        while not self._fanout_stop.is_set():
            try:
                event = q.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                self._dispatch_child_event(event)
            except Exception:
                log.debug("coord_adapter.fanout.dispatch_failed", exc_info=True)

    def _dispatch_child_event(self, event: dict[str, Any]) -> None:
        """Match a cluster event to a coordinator and re-emit on its UI.

        Events of interest:

        - ``ws_created`` with ``parent_ws_id`` matching an in-memory
          coordinator → add to registry + re-emit as
          ``child_ws_created``.
        - ``cluster_state`` / ``ws_closed`` / ``ws_rename`` whose
          ``ws_id`` is in any coordinator's known-children registry →
          re-emit as ``child_ws_state`` / ``child_ws_closed`` /
          ``child_ws_rename``.

        Events for ws_ids we don't own silently drop — the filter lives
        on the server so each coordinator's SSE stream stays small.
        """
        etype = event.get("type") or ""
        ws_id = event.get("ws_id") or ""
        if not etype or not ws_id:
            return

        if etype == "ws_created":
            parent = event.get("parent_ws_id") or ""
            if not parent:
                return
            # Presence check + registry mutation under the same lock:
            # a concurrent close()/eviction can pop the entry between
            # the check and the mutation, after which a bare setdefault
            # would resurrect the entry — leaking the registry key and
            # enqueuing onto the closed coordinator's UI. Trusted-team
            # posture (#400 / a46dab1) means no per-event tenant gate
            # here; scope-level auth at the SSE endpoint is the only
            # boundary.
            with self._children_lock:
                coord_ui = self._active_coords.get(parent)
                if coord_ui is None:
                    return
                existing = self._children.setdefault(parent, set())
                if ws_id in existing:
                    return
                existing.add(ws_id)
                self._child_to_coord[ws_id] = parent
            payload = {
                "type": "child_ws_created",
                "ws_id": ws_id,
                "child_ws_id": ws_id,
                "parent_ws_id": parent,
                "node_id": event.get("node_id", ""),
                "name": event.get("name", ""),
                "title": event.get("title", ""),
            }
            _enqueue_on_ui(coord_ui, parent, payload)
            return

        if etype in ("cluster_state", "ws_closed", "ws_rename"):
            coord_id = self._coord_for_child(ws_id)
            if coord_id is None:
                return
            mgr = self._manager
            owning_ws = mgr.get(coord_id) if mgr is not None else None
            if owning_ws is None or owning_ws.ui is None:
                return
            if etype == "cluster_state":
                child_event = {
                    "type": "child_ws_state",
                    "child_ws_id": ws_id,
                    "parent_ws_id": coord_id,
                    "state": event.get("state", ""),
                    "tokens": event.get("tokens", 0),
                    "node_id": event.get("node_id", ""),
                    # activity_state lets the JS detect approval-state
                    # transitions and fire urgent live-bulk fetches so
                    # inline approve/deny buttons render in lockstep
                    # with the child entering attention (instead of
                    # waiting up to 5s for the next TTL window).
                    "activity_state": event.get("activity_state", ""),
                }
            elif etype == "ws_closed":
                child_event = {
                    "type": "child_ws_closed",
                    "child_ws_id": ws_id,
                    "parent_ws_id": coord_id,
                    "reason": event.get("reason", ""),
                }
            else:  # ws_rename
                child_event = {
                    "type": "child_ws_rename",
                    "child_ws_id": ws_id,
                    "parent_ws_id": coord_id,
                    "name": event.get("name", ""),
                }
            _enqueue_on_ui(owning_ws.ui, coord_id, child_event)


def _enqueue_on_ui(ui: Any, coord_ws_id: str, payload: dict[str, Any]) -> None:
    """Dispatch ``payload`` onto the coordinator UI's listener fan-out.

    ``ConsoleCoordinatorUI`` auto-stamps ``ws_id`` on enqueue, but the
    child-fanout payloads already carry ``child_ws_id`` + ``parent_ws_id``.
    Stamp the coordinator's own ws_id too so the browser event handler
    can discriminate child_* events from its own-session events purely
    from the payload.
    """
    enqueue = getattr(ui, "_enqueue", None)
    if enqueue is None:
        return
    # Mutate in place — the dispatch path owns ``payload`` and doesn't
    # reuse it after the enqueue call (perf-6).
    payload["ws_id"] = coord_ws_id
    try:
        enqueue(payload)
    except Exception:
        log.debug("coord_adapter.enqueue_failed ws=%s", coord_ws_id[:8], exc_info=True)
