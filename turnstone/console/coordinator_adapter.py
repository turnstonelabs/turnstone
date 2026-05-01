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

from typing import TYPE_CHECKING, Any

from turnstone.core import session_worker
from turnstone.core.adapters._ui_cleanup import cleanup_session_ui
from turnstone.core.child_source import ClusterChildSource
from turnstone.core.children_registry import ChildrenRegistry
from turnstone.core.log import get_logger
from turnstone.core.workstream import Workstream, WorkstreamKind, WorkstreamState

if TYPE_CHECKING:
    from collections.abc import Callable

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
        # Children registry — universal parent → children + reverse
        # lookup primitive. Lifted from inline data on this adapter to
        # ``turnstone.core.children_registry.ChildrenRegistry`` (Stage 3
        # Step 1) so the same primitive can serve interactive
        # workstreams when they gain spawn capability. The dispatch
        # path (`_dispatch_child_event`) calls into the registry
        # atomically; everything else delegates through the legacy
        # method names which still exist as thin shims for the
        # cluster-routing + cleanup callers.
        self._registry = ChildrenRegistry()
        # Cross-node child events arrive via ``ClusterChildSource``
        # (Stage 3 Step 2): a strategy that subscribes to the
        # collector's listener channel and runs a daemon thread that
        # drains the queue, pushing each event to the sink. The sink
        # is :meth:`_dispatch_child_event` so the existing translation
        # logic (cluster_state → child_ws_state, etc.) stays in one
        # place. Constructed lazily by ``start_child_event_fanout`` so
        # the collector reference is available.
        self._child_source: ClusterChildSource | None = None

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
        self._registry.install(ws.id, ws.ui)
        self._fanout_console_ws_created(ws)

    def emit_rehydrated(self, ws: Workstream) -> None:
        # Resurrected coordinator: the subtree IS persisted, so pull
        # it from storage after the registry seed so a ``ws_created``
        # for an already-spawned child that fires mid-rebuild merges
        # cleanly.
        self._registry.install(ws.id, ws.ui)
        self._rebuild_children_registry(ws.id)
        self._fanout_console_ws_created(ws)

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
        # presence slot. A plain pop without clearing the reverse
        # index would leak every evicted coordinator's child→parent
        # pointers forever — :meth:`ChildrenRegistry.uninstall`
        # handles forward set + reverse entries + presence atomically.
        self._registry.uninstall(ws_id)
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
            except Exception:
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
                # ``session.send()`` already surfaced the failure to the
                # SSE stream (``ui.on_error``), persisted the sanitized
                # exception text into ``workstream_config.last_error``
                # for the inspecting parent coord, and emitted state=
                # error for the cluster fan-out / dashboard via
                # :meth:`ChatSession._record_fatal_error`.  The adapter
                # owns ONLY the worker-level cleanup (attachments,
                # logging) above.

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
        self._registry.merge_children(coord_ws_id, child_ids)

    def children_snapshot(self, coord_ws_id: str) -> list[str]:
        """Return a snapshot of the coordinator's direct child ws_ids.

        Used by ``stop_cascade`` to iterate children without holding
        the registry lock during the per-child HTTP dispatch. A
        mutation racing with the snapshot (child spawned mid-cascade)
        either lands before (cancelled) or after (out of scope for
        this batch) — both safe. Returns an empty list for unknown
        coordinators.
        """
        return self._registry.children_of(coord_ws_id)

    # ------------------------------------------------------------------
    # Cluster-event fan-out thread
    # ------------------------------------------------------------------

    def start_child_event_fanout(self, collector: ClusterCollector) -> None:
        """Subscribe to cluster events via :class:`ClusterChildSource`.

        Idempotent — already-started ChildSource stays. Called once
        from the console lifespan after both the collector and the
        session manager are constructed.
        """
        if self._child_source is not None:
            return
        self._collector = collector
        # Coord-specific transport setup: the "console" pseudo-node
        # must exist in the snapshot map BEFORE any
        # ``emit_console_ws_*`` calls (and before the snapshot is
        # taken inside ``ChildSource.start``) so those emits land on
        # a real node entry the snapshot surfaces.
        collector.ensure_console_pseudo_node()
        mgr = self._manager
        if mgr is None:
            raise RuntimeError(
                "CoordinatorAdapter: manager not attached — call attach(mgr) after construction"
            )
        # Build + start the strategy. The sink is
        # :meth:`_dispatch_child_event` so cluster events flow through
        # the same translation path that synthesises ``child_ws_*``
        # payloads for the parent's UI.
        source = ClusterChildSource(
            collector=collector,
            registry=self._registry,
            parents_provider=lambda: [ws.id for ws in mgr.list_all()],
        )
        source.start(sink=self._dispatch_child_event)
        self._child_source = source
        # Seed the pseudo-node with any coordinators already loaded in
        # memory when the collector binds. Prevents a race where early
        # creates happened before the collector was wired up and their
        # rows never showed on the snapshot. (Coord-specific — interactive
        # has no analogous pseudo-node.)
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

    def shutdown(self) -> None:
        """Stop the ChildSource and unregister from the collector.

        Safe to call multiple times; idempotent. Invoked from the
        console lifespan teardown so SSE listener queues don't leak.
        """
        src = self._child_source
        self._child_source = None
        if src is not None:
            src.shutdown()

    def _dispatch_child_event(self, event: dict[str, Any]) -> None:
        """Match a cluster event to a coordinator and re-emit on its UI.

        Events of interest:

        - ``ws_created`` with ``parent_ws_id`` matching an in-memory
          coordinator → add to registry + re-emit as
          ``child_ws_created``.
        - ``cluster_state`` / ``ws_closed`` / ``ws_rename`` /
          ``intent_verdict`` / ``approval_resolved`` whose ``ws_id``
          is in any coordinator's known-children registry → re-emit as
          ``child_ws_state`` / ``child_ws_closed`` /
          ``child_ws_rename`` / ``child_ws_intent_verdict`` /
          ``child_ws_approval_resolved``.

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
            # Atomic check-and-route under the registry's lock: a
            # concurrent close()/eviction can pop the parent's entry
            # between the presence check and the mutation, so the two
            # must happen together. ``add_child`` returns the parent's
            # UI on success or None on (a) parent not installed, or
            # (b) duplicate child. Trusted-team posture (#400 / a46dab1)
            # means no per-event tenant gate here; scope-level auth at
            # the SSE endpoint is the only boundary.
            coord_ui = self._registry.add_child(parent, ws_id)
            if coord_ui is None:
                return
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

        if etype in (
            "cluster_state",
            "ws_closed",
            "ws_rename",
            "intent_verdict",
            "approval_resolved",
            "approve_request",
        ):
            coord_id = self._registry.parent_for(ws_id)
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
                    # ``activity_state`` lets the JS detect approval
                    # transitions and trigger a bulk fetch for the
                    # initial detail. The detail itself no longer
                    # piggybacks here (Stage 3 cleanup) — verdicts
                    # arrive via the explicit ``intent_verdict`` event
                    # class and resolution via ``approval_resolved``.
                    "activity_state": event.get("activity_state", ""),
                }
            elif etype == "ws_closed":
                child_event = {
                    "type": "child_ws_closed",
                    "child_ws_id": ws_id,
                    "parent_ws_id": coord_id,
                    "reason": event.get("reason", ""),
                }
            elif etype == "ws_rename":
                child_event = {
                    "type": "child_ws_rename",
                    "child_ws_id": ws_id,
                    "parent_ws_id": coord_id,
                    "name": event.get("name", ""),
                }
            elif etype == "intent_verdict":
                # Per-coord re-emit of an explicit verdict event so
                # the tree UI can render the risk pill + verdict
                # result without polling. The corresponding
                # ``cluster_state`` event no longer carries the
                # detail (the piggyback was removed end-to-end);
                # the bulk fetch on initial approval entry plus this
                # explicit event class are the only carriers.
                child_event = {
                    "type": "child_ws_intent_verdict",
                    "child_ws_id": ws_id,
                    "parent_ws_id": coord_id,
                    "node_id": event.get("node_id", ""),
                    "verdict": event.get("verdict") or {},
                }
            elif etype == "approval_resolved":
                child_event = {
                    "type": "child_ws_approval_resolved",
                    "child_ws_id": ws_id,
                    "parent_ws_id": coord_id,
                    "node_id": event.get("node_id", ""),
                    "approved": bool(event.get("approved", False)),
                    "feedback": event.get("feedback", "") or "",
                    "always": bool(event.get("always", False)),
                }
            else:  # approve_request
                # Push path for the initial approval items —
                # eliminates the bulk-fetch race that previously left
                # the coord row stuck on a loading placeholder when
                # the bulk fetch landed in the gap between the state
                # transition to ATTENTION and ``_pending_approval``
                # being set inside ``approve_tools``.
                child_event = {
                    "type": "child_ws_approve_request",
                    "child_ws_id": ws_id,
                    "parent_ws_id": coord_id,
                    "node_id": event.get("node_id", ""),
                    "detail": event.get("detail") or {},
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
