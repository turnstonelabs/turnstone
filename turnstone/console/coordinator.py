"""CoordinatorManager — console-side lifecycle for coordinator workstreams.

A coordinator workstream is a :class:`turnstone.core.session.ChatSession`
with ``kind="coordinator"`` running inside ``turnstone-console``.  Each
one has its own :class:`ConsoleCoordinatorUI`, :class:`CoordinatorClient`,
per-session JWT manager, and worker thread.  The manager tracks them in
a ``dict[ws_id, Workstream]`` (same dataclass
``turnstone/core/workstream.py`` uses) so dashboard aggregation reuses
the existing types.

Design notes:

- **No eager rehydration on startup.**  Coordinator rows survive in the
  DB, but spinning up every persisted coordinator's worker thread on
  console start would waste resources.  Rehydration happens lazily in
  ``open(ws_id)`` — called by the GET endpoint when a user browses to a
  persisted-but-not-loaded coordinator.
- **Shared session factory closure.**  The factory (built in
  ``session_factory.py``) captures ``registry`` / ``config_store`` /
  ``node_id`` / ``coord_client_factory`` so each ``create`` / ``open``
  call just forwards ``ws_id`` + ``user_id``.
- **max_active gate.**  Before constructing a new session, enforce
  ``coordinator.max_active``: evict the oldest idle coordinator when
  full, or raise ``RuntimeError`` (translated to HTTP 429 by the
  endpoint) if all slots are non-idle.
"""

from __future__ import annotations

import queue
import secrets
import threading
import time
from typing import TYPE_CHECKING, Any

from turnstone.console.collector import ClusterCollector
from turnstone.core.log import get_logger
from turnstone.core.workstream import (
    Workstream,
    WorkstreamKind,
    WorkstreamManager,
    WorkstreamState,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from turnstone.console.coordinator_ui import ConsoleCoordinatorUI
    from turnstone.core.session import ChatSession
    from turnstone.core.storage._protocol import StorageBackend

log = get_logger(__name__)


class CoordinatorManager:
    """Tracks live coordinator sessions in the console process."""

    # Pseudo-node id persisted on coordinator rows so ``workstreams.node_id``
    # stays non-NULL and list / audit surfaces can distinguish coordinators
    # from real-node workstreams.  Routed through the router as an
    # unroutable sentinel — coordinators never land on real nodes.
    # Bound from ``ClusterCollector.CONSOLE_PSEUDO_NODE_ID`` so the two
    # literals can't drift (the collector's eviction + query filters
    # key off the same string).
    NODE_ID = ClusterCollector.CONSOLE_PSEUDO_NODE_ID

    def __init__(
        self,
        *,
        session_factory: Callable[..., ChatSession],
        ui_factory: Callable[[str, str], ConsoleCoordinatorUI],
        storage: StorageBackend,
        max_active: int,
    ) -> None:
        if max_active < 1:
            raise ValueError(f"max_active must be >= 1, got {max_active}")
        self._session_factory = session_factory
        self._ui_factory = ui_factory
        self._storage = storage
        self._max_active = max_active
        self._workstreams: dict[str, Workstream] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()
        # Per-ws_id locks that serialize concurrent lazy rehydration of the
        # same workstream.  Without these, two GETs for the same persisted-
        # but-not-loaded ws_id both miss the fast path, both call the
        # session factory, and the second clobbers the first — orphaning
        # a worker thread and leaking SSE listeners.  The dict is guarded
        # by ``self._lock``; entries are refcounted so we only pop when
        # the last waiter releases — popping while a waiter still holds
        # the lock would let a fresh arrival allocate a different lock
        # for the same ws_id, breaking serialization on the failure path.
        self._open_locks: dict[str, tuple[threading.Lock, int]] = {}
        # Per-coordinator known-child ws_id set.  Populated lazily on
        # create/open from storage and updated live as the cluster fan-out
        # thread sees ws_created events with matching parent_ws_id.
        # Closed / deleted children stay in the registry so the tree UI
        # can keep rendering them grayed out; the authoritative render
        # path reads storage for state.  Bounded by eventual coordinator
        # close/eviction; for long-running coordinators with high child
        # churn the set grows monotonically — an LRU cap is a future
        # tech-debt item.
        self._children: dict[str, set[str]] = {}
        # Reverse index for O(1) child → coord lookup on every cluster
        # event.  Without this, every cluster event incurs a linear
        # scan over every coordinator's child set while holding the
        # fan-out lock — a hot-path tax that scales with both active
        # coordinators and their retained-history depth.
        self._child_to_coord: dict[str, str] = {}
        self._children_lock = threading.Lock()
        # Cluster-event fan-out: subscribes to the ClusterCollector's
        # listener channel, filters by known child ws_ids, and re-emits
        # child_ws_* events on the matching coordinator's UI.  Configured
        # lazily via ``start_child_event_fanout(collector)`` from the
        # console lifespan once both the manager and collector exist.
        self._collector: ClusterCollector | None = None
        self._collector_queue: queue.Queue[dict[str, Any]] | None = None
        self._fanout_thread: threading.Thread | None = None
        self._fanout_stop = threading.Event()
        # Lock-free presence cache for the fan-out dispatch path:
        # coord_ws_id -> (user_id, ui).  The dict is swapped by
        # reference (copy-on-write) under self._lock on every install
        # / remove so dispatch can read it without acquiring the
        # manager lock.  An in-flight dispatch that observes a stale
        # snapshot either sees the coordinator or doesn't — both are
        # safe outcomes.  Without this, every ws_created event in the
        # cluster serialized behind self._lock, contending against
        # every user-driven open/close/create.
        self._active_coords: dict[str, tuple[str, Any]] = {}

    @property
    def max_active(self) -> int:
        return self._max_active

    # ------------------------------------------------------------------
    # create — new coordinator session
    # ------------------------------------------------------------------

    def create(
        self,
        *,
        user_id: str,
        name: str = "",
        skill: str | None = None,
        initial_message: str = "",
        ws_id: str = "",
    ) -> Workstream:
        """Construct a new coordinator workstream and register it.

        Raises ``RuntimeError`` if the manager is at capacity and no
        idle coordinator can be evicted — the API endpoint translates
        this to HTTP 429.

        Concurrency model: the placeholder Workstream (session=None) is
        inserted into ``self._workstreams`` under ``self._lock`` as part
        of slot reservation, so concurrent ``create()`` callers observe
        the slot as used immediately.  Session construction happens
        outside the lock — on failure we re-acquire and remove the
        placeholder so capacity isn't leaked.
        """
        ws_id = ws_id or secrets.token_hex(16)
        with self._lock:
            ws, evicted = self._reserve_and_install_locked(
                ws_id, user_id, name or f"coord-{ws_id[:4]}"
            )

        if evicted is not None:
            self._cleanup(evicted)
            with self._children_lock:
                self._pop_coord_registry_locked(evicted.id)
            # Fan out a console-pseudo-node ``ws_closed`` for the evicted
            # coordinator so the home view drops it live (see #9).  The
            # eviction path otherwise has no observable signal — the row
            # would linger on other tabs' home views until the next hard
            # refresh.
            if self._collector is not None:
                try:
                    self._collector.emit_console_ws_closed(evicted.id)
                except Exception:
                    log.debug(
                        "coord_mgr.collector_evict_fanout_failed ws=%s",
                        evicted.id[:8],
                        exc_info=True,
                    )

        # Resolve skill name -> (template_id, applied_version) so the
        # workstreams row persists what was applied, not what was
        # requested.  Mirrors ``turnstone/server.py`` interactive-workstream
        # create; surfaced by ``inspect_workstream`` on coordinator rows so
        # operators can tell which skill the session is running.  A missing
        # / unknown skill name falls back to empty (no skill binding)
        # rather than raising — the session factory tolerates None.
        skill_id_resolved = ""
        skill_version_resolved = 0
        if skill:
            try:
                from turnstone.core.memory import get_skill_by_name

                skill_data = get_skill_by_name(skill)
            except Exception:
                log.debug(
                    "coord_mgr.skill_lookup_failed ws=%s skill=%s",
                    ws_id[:8],
                    skill,
                    exc_info=True,
                )
                skill_data = None
            if skill_data and skill_data.get("template_id"):
                skill_id_resolved = str(skill_data["template_id"])
                try:
                    # COUNT query avoids pulling every version row just
                    # for its length on the create hot path (#perf-2).
                    skill_version_resolved = (
                        self._storage.count_skill_versions(skill_id_resolved) + 1
                    )
                except Exception:
                    log.debug(
                        "coord_mgr.skill_version_failed ws=%s skill=%s",
                        ws_id[:8],
                        skill,
                        exc_info=True,
                    )
                    skill_version_resolved = 1

        # Persist before constructing the session so lazy rehydration on
        # restart can find the row even if the ChatSession build fails.
        # Fail-closed: a coordinator that's only in-memory would be
        # invisible to lazy rehydration and reappear as "missing" after
        # console restart, so surface the storage failure to the caller
        # (endpoint returns 500) rather than quietly limping along.
        try:
            self._storage.register_workstream(
                ws_id,
                node_id=self.NODE_ID,
                user_id=user_id,
                name=ws.name,
                kind=WorkstreamKind.COORDINATOR,
                parent_ws_id=None,
                skill_id=skill_id_resolved,
                skill_version=skill_version_resolved,
            )
        except Exception:
            log.warning(
                "coord_mgr.register_failed ws=%s",
                ws_id[:8],
                exc_info=True,
            )
            with self._lock:
                self._remove_locked(ws_id)
            raise

        try:
            ws.session = self._session_factory(
                ws.ui,
                None,  # model_alias — factory reads coordinator.model_alias
                ws_id,
                skill=skill,
                kind=WorkstreamKind.COORDINATOR,
                parent_ws_id=None,
            )
        except Exception:
            # Roll back the slot + persisted row on construction failure
            # so a misconfigured coordinator doesn't wedge capacity.
            with self._lock:
                self._remove_locked(ws_id)
            try:
                self._storage.delete_workstream(ws_id)
            except Exception:
                # Storage rollback failed — the orphan row will silently
                # fail rehydration on future opens.  Warn so operators
                # can see and clean up manually rather than debugging a
                # mysterious "coordinator missing" later.
                log.warning(
                    "coord_mgr.rollback_delete_failed ws=%s",
                    ws_id[:8],
                    exc_info=True,
                )
            raise

        if initial_message:
            self._spawn_worker(ws, initial_message)

        # Fan out a console-pseudo-node ``ws_created`` so the home view
        # sees the new coordinator live (no more polling — see #9).
        # Failure here must not break the create path — worst case the
        # view lags one refresh cycle until the next SSE tick.
        if self._collector is not None:
            try:
                self._collector.emit_console_ws_created(
                    ws_id,
                    name=ws.name,
                    user_id=user_id,
                    kind=WorkstreamKind.COORDINATOR.value,
                    state=ws.state.value,
                    parent_ws_id=None,
                )
            except Exception:
                log.debug(
                    "coord_mgr.collector_create_fanout_failed ws=%s",
                    ws_id[:8],
                    exc_info=True,
                )

        # Seed the known-children registry with an empty set so the
        # fan-out filter recognises this coordinator immediately when a
        # child's ws_created arrives.  Cheap in-memory write, no storage
        # round-trip — freshly created coordinators have no persisted
        # children to rebuild from.
        with self._children_lock:
            self._children.setdefault(ws_id, set())

        return ws

    # ------------------------------------------------------------------
    # open — lazy rehydration for a persisted coordinator
    # ------------------------------------------------------------------

    def open(self, ws_id: str, user_id: str) -> Workstream | None:
        """Rehydrate a persisted coordinator session on demand.

        Returns ``None`` if the row doesn't exist or doesn't belong to
        ``user_id``.  Callers that need ownership bypass (admin view)
        should call ``open_admin`` instead.
        """
        return self._open_impl(ws_id, user_id=user_id, admin=False)

    def open_admin(self, ws_id: str) -> Workstream | None:
        """Rehydrate regardless of ownership — admin paths only."""
        return self._open_impl(ws_id, user_id="", admin=True)

    def _open_impl(self, ws_id: str, *, user_id: str, admin: bool) -> Workstream | None:
        """Serialize concurrent open() for the same ws_id.

        Two concurrent GET /v1/api/coordinator/{ws_id} requests for the
        same persisted-but-unloaded ws_id must not each construct a
        session.  A per-ws_id lock ensures the second arrival sees the
        first thread's installed workstream and returns it instead of
        spinning up a duplicate.  The lock entry is refcounted so it
        survives until the last waiter releases — only then is it
        popped.  Popping earlier would let a third arrival allocate a
        fresh lock for the same ws_id, defeating the serialization.
        """
        with self._lock:
            entry = self._open_locks.get(ws_id)
            if entry is None:
                open_lock = threading.Lock()
                self._open_locks[ws_id] = (open_lock, 1)
            else:
                open_lock, refs = entry
                self._open_locks[ws_id] = (open_lock, refs + 1)
        try:
            with open_lock:
                # Fast-path: someone else installed the session while we
                # were waiting on the per-ws lock.
                with self._lock:
                    existing = self._workstreams.get(ws_id)
                    if existing is not None and existing.session is not None:
                        return existing

                row = self._storage.get_workstream(ws_id)
                if row is None or row.get("kind") != WorkstreamKind.COORDINATOR:
                    return None
                # ``deleted`` is a tombstone — the row is on its way out
                # and must never resurrect.  ``closed`` used to be in the
                # same bucket (so a stray URL revisit couldn't silently
                # reverse a Close), but the Saved Coordinators landing UI
                # makes restore an explicit user action: clicking a saved
                # card calls POST /open and the user is consenting to
                # reload.  ``_reserve_and_install_locked`` still enforces
                # ``max_active`` (evicting an idle peer or 429-ing) so the
                # safety the old guard provided now lives in the slot
                # accounting, not in a flat-refusal.
                if row.get("state") == "deleted":
                    return None
                row_owner = row.get("user_id") or ""
                # Strict equality (not short-circuit on empty row_owner)
                # — empty-owner rows must not allow non-admin callers to
                # rehydrate orphan / system-owned coordinators (would
                # consume a max_active slot + evict another tenant's
                # IDLE coordinator).
                if not admin and row_owner != user_id:
                    return None

                # Reserve the slot + install placeholder under the lock
                # so concurrent creates/opens count us toward capacity.
                with self._lock:
                    # Re-check fast path (another thread may have raced
                    # through the whole open while we checked storage).
                    existing = self._workstreams.get(ws_id)
                    if existing is not None and existing.session is not None:
                        return existing
                    ws, evicted = self._reserve_and_install_locked(
                        ws_id,
                        row_owner,
                        row.get("name") or f"coord-{ws_id[:4]}",
                    )

                if evicted is not None:
                    self._cleanup(evicted)
                    # Mirror the create() eviction path exactly — clears
                    # both the forward _children set AND the reverse
                    # _child_to_coord index.  A plain _children.pop
                    # would leak every evicted coordinator's reverse-
                    # index entries forever.
                    with self._children_lock:
                        self._pop_coord_registry_locked(evicted.id)
                    # Fan out a console-pseudo-node ``ws_closed`` for the
                    # evicted coordinator so other tabs drop the row
                    # live — without this, the rehydrate-path eviction
                    # was silent on the home view and stale rows
                    # lingered until a hard refresh (#9 follow-up).
                    if self._collector is not None:
                        try:
                            self._collector.emit_console_ws_closed(evicted.id)
                        except Exception:
                            log.debug(
                                "coord_mgr.collector_open_evict_fanout_failed ws=%s",
                                evicted.id[:8],
                                exc_info=True,
                            )

                try:
                    ws.session = self._session_factory(
                        ws.ui,
                        None,
                        ws_id,
                        skill=None,
                        kind=WorkstreamKind.COORDINATOR,
                        parent_ws_id=None,
                    )
                except Exception:
                    with self._lock:
                        self._remove_locked(ws_id)
                    raise

                # Restore message history from storage.
                if ws.session is not None and hasattr(ws.session, "resume"):
                    try:
                        ws.session.resume(ws_id)
                    except Exception:
                        log.debug(
                            "coord_mgr.resume_failed ws=%s",
                            ws_id[:8],
                            exc_info=True,
                        )

                # No DB state-flip on resurrect.  The in-memory session
                # is now IDLE; the DB row may still say 'closed' from the
                # last close().  We deliberately don't write 'idle' here
                # because:
                #   (a) it would race a concurrent close() (which writes
                #       'closed' under self._lock without acquiring this
                #       per-ws open_lock) and could overwrite the
                #       authoritative close;
                #   (b) the next set_state() call (any state transition
                #       — running, attention, idle-after-running) syncs
                #       the DB naturally;
                #   (c) the saved-coordinators list filters by state +
                #       excludes coordinators currently loaded into
                #       coord_mgr, so the stale 'closed' state on disk
                #       doesn't make a still-loaded coordinator appear
                #       as a saved card.
                # Rehydration: fan out a console-pseudo-node
                # ``ws_created`` so a tab that opens a persisted-but-
                # unloaded coordinator sees it live on every other
                # open home view (see #9).  Harmless re-emit —
                # clusterState's patch loop ignores duplicates.
                if self._collector is not None:
                    try:
                        self._collector.emit_console_ws_created(
                            ws_id,
                            name=ws.name,
                            user_id=ws.user_id or "",
                            kind=WorkstreamKind.COORDINATOR.value,
                            state=ws.state.value,
                            parent_ws_id=None,
                        )
                    except Exception:
                        log.debug(
                            "coord_mgr.collector_open_fanout_failed ws=%s",
                            ws_id[:8],
                            exc_info=True,
                        )
                # Rebuild the known-children registry from storage so the
                # cluster fan-out filter sees the coordinator's subtree
                # immediately after rehydration.
                self._rebuild_children_registry(ws_id)
                return ws
        finally:
            with self._lock:
                entry = self._open_locks.get(ws_id)
                if entry is not None:
                    lk, refs = entry
                    if refs <= 1:
                        self._open_locks.pop(ws_id, None)
                    else:
                        self._open_locks[ws_id] = (lk, refs - 1)

    # ------------------------------------------------------------------
    # Worker thread dispatch
    # ------------------------------------------------------------------

    def send(self, ws_id: str, message: str) -> bool:
        """Queue a message onto a coordinator session's ChatSession.

        Returns False if the coordinator isn't loaded in the manager or
        if the worker's pending-message queue is full (caller should
        surface 429 / backpressure).  Priority is parsed from the
        message prefix (``/high``, ``/urgent``, etc.) by
        :meth:`ChatSession.queue_message`.
        """
        ws = self.get(ws_id)
        if ws is None or ws.session is None:
            return False
        return self._spawn_worker(ws, message)

    def _spawn_worker(self, ws: Workstream, message: str) -> bool:
        """Start (or reuse) a worker thread that drives session.send.

        Returns True on successful enqueue (existing worker) or thread
        spawn (no live worker).  Returns False when an existing worker's
        queue is full — must NOT spawn a second concurrent worker on
        the same ChatSession (mutates messages, queued_messages,
        streaming state, LLM client cursors, approvals).
        """
        session = ws.session
        if session is None:
            return False
        # If a worker is already running for this ws, enqueue instead of
        # spawning a duplicate — ChatSession.queue_message handles FIFO.
        if (
            ws.worker_thread is not None
            and ws.worker_thread.is_alive()
            and hasattr(session, "queue_message")
        ):
            try:
                session.queue_message(message)
                return True
            except queue.Full:
                # Queue is at capacity AND a worker is still running.
                # Spawning a second thread on the same ChatSession would
                # corrupt history / cursors / approvals — return False so
                # the caller can surface backpressure (HTTP 429).
                log.warning(
                    "coord_mgr.queue_full ws=%s — message dropped (worker still busy)",
                    ws.id[:8],
                )
                return False
            except Exception:
                log.warning(
                    "coord_mgr.queue_message_failed ws=%s",
                    ws.id[:8],
                    exc_info=True,
                )
                return False

        def _run() -> None:
            try:
                session.send(message)
            except Exception as exc:
                log.exception("coord_mgr.worker_failed ws=%s", ws.id[:8])
                # Surface the failure to the coordinator's SSE stream so
                # the operator sees what broke instead of a bare "error"
                # badge — most common cause is a model-alias
                # misconfiguration (wrong provider for the model) which
                # the raw traceback narrows down quickly.
                ui = ws.ui
                if ui is not None and hasattr(ui, "on_error"):
                    try:
                        ui.on_error(f"{type(exc).__name__}: {exc}")
                    except Exception:
                        log.debug(
                            "coord_mgr.on_error_dispatch_failed ws=%s",
                            ws.id[:8],
                            exc_info=True,
                        )
                # Also mark the workstream state=error so the cluster
                # fan-out + dashboard reflect the failure.
                if ui is not None and hasattr(ui, "on_state_change"):
                    try:
                        ui.on_state_change(WorkstreamState.ERROR.value)
                    except Exception:
                        log.debug(
                            "coord_mgr.error_state_update_failed ws=%s",
                            ws.id[:8],
                            exc_info=True,
                        )

        t = threading.Thread(
            target=_run,
            name=f"coord-worker-{ws.id[:8]}",
            daemon=True,
        )
        ws.worker_thread = t
        t.start()
        return True

    # ------------------------------------------------------------------
    # Inspect / list / close
    # ------------------------------------------------------------------

    def get(self, ws_id: str) -> Workstream | None:
        with self._lock:
            return self._workstreams.get(ws_id)

    def list_for_user(self, user_id: str) -> list[Workstream]:
        """Return coordinators owned by ``user_id``.

        Does NOT include rows with empty ``user_id`` — those are either
        system-created sessions or rows migrated in without an owner,
        and returning them to every caller would leak ws_id + name +
        state across tenants.  Admins call :meth:`list_all` instead.
        """
        with self._lock:
            return [ws for ws in self._workstreams.values() if ws.user_id and ws.user_id == user_id]

    def list_all(self) -> list[Workstream]:
        with self._lock:
            return list(self._workstreams.values())

    def close(self, ws_id: str) -> bool:
        """Soft-close: unload from memory + mark state=closed in DB."""
        with self._lock:
            ws = self._workstreams.pop(ws_id, None)
            if ws is None:
                return False
            if ws_id in self._order:
                self._order.remove(ws_id)
            # Drop from the lock-free dispatch presence cache so
            # post-close ws_created events for this (now-gone)
            # coordinator don't keep reaching its orphaned UI.
            self._refresh_active_coords_locked(drop={ws_id})
        with self._children_lock:
            # Free the known-children registry so long-running consoles
            # don't accumulate stale entries as coordinators churn.  The
            # child event fan-out filter scans this map on every cluster
            # event; unbounded growth turns into a hot-loop tax.
            self._pop_coord_registry_locked(ws_id)
        self._cleanup(ws)
        try:
            self._storage.update_workstream_state(ws_id, "closed")
        except Exception:
            log.debug("coord_mgr.state_update_failed ws=%s", ws_id[:8], exc_info=True)
        # Fan out a console-pseudo-node ``ws_closed`` so the home view
        # drops the row live (see #9).  Best-effort; swallow failures.
        if self._collector is not None:
            try:
                self._collector.emit_console_ws_closed(ws_id)
            except Exception:
                log.debug(
                    "coord_mgr.collector_close_fanout_failed ws=%s",
                    ws_id[:8],
                    exc_info=True,
                )
        # Best-effort sweep: task_list rows may carry child_ws_id
        # pointers to workstreams that have since been hard-deleted.
        # Clearing the dead links keeps the persisted task envelope
        # honest and prevents v2 kanban from rendering broken arrows.
        # Routed through the CoordinatorClient so it acquires the same
        # per-ws ``_task_lock`` the add/update/remove/reorder paths
        # hold — a close() racing an in-flight task_list mutation
        # would otherwise lose the mutation (#bug-6).
        coord_client = getattr(getattr(ws, "session", None), "_coord_client", None)
        if coord_client is not None and hasattr(coord_client, "cleanup_dead_task_child_refs"):
            try:
                coord_client.cleanup_dead_task_child_refs(ws_id)
            except Exception:
                log.debug(
                    "coord_mgr.task_ref_cleanup_failed ws=%s",
                    ws_id[:8],
                    exc_info=True,
                )
        return True

    def cancel(self, ws_id: str) -> bool:
        """Cancel in-flight generation and unblock any pending approval/plan."""
        ws = self.get(ws_id)
        if ws is None:
            return False
        if ws.session is not None and hasattr(ws.session, "cancel"):
            try:
                ws.session.cancel()
            except Exception:
                log.debug("coord_mgr.cancel_failed ws=%s", ws_id[:8], exc_info=True)
        # Unblock any blocked approval or plan event.
        if ws.ui is not None:
            if hasattr(ws.ui, "resolve_approval"):
                ws.ui.resolve_approval(False, "cancelled")
            if hasattr(ws.ui, "resolve_plan"):
                ws.ui.resolve_plan("reject")
        return True

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _reserve_and_install_locked(
        self,
        ws_id: str,
        user_id: str,
        name: str,
    ) -> tuple[Workstream, Workstream | None]:
        """Install a placeholder Workstream under ``self._lock``.

        Caller MUST hold ``self._lock``.  The placeholder has
        ``session=None`` — the caller fills it in after construction.
        The UI is allocated here (cheap) so concurrent ``get()`` never
        observes a placeholder with ``ui=None``; the session is the
        only field that lags.

        Returns ``(placeholder, evicted)``.  ``evicted`` is a Workstream
        the caller must ``self._cleanup(...)`` outside the lock, or
        ``None`` when no eviction was needed.

        Raises ``RuntimeError`` when all slots are non-idle — the
        endpoint translates this to HTTP 429.
        """
        if ws_id in self._workstreams:
            # Defensive — should be unreachable: create() uses a fresh
            # random ws_id and open() serializes on per-ws locks that
            # already bounce the repeated call via the fast path.  Raise
            # loudly so any real regression surfaces.
            raise RuntimeError(f"ws_id {ws_id[:8]!r} already tracked by CoordinatorManager")
        evicted: Workstream | None = None
        if len(self._workstreams) >= self._max_active:
            # Only fully-constructed IDLE sessions are eviction candidates.
            # Placeholders (session=None) are in-flight creations that
            # count toward capacity but must not evict each other —
            # otherwise a burst of concurrent creates would evict every
            # prior placeholder and silently exceed ``max_active``.
            oldest: Workstream | None = None
            for wid in self._order:
                w = self._workstreams.get(wid)
                if w is None or w.session is None:
                    continue
                if w.state == WorkstreamState.IDLE and (
                    oldest is None or w.last_active < oldest.last_active
                ):
                    oldest = w
            if oldest is None:
                raise RuntimeError(f"All {self._max_active} coordinator slots are active")
            self._workstreams.pop(oldest.id, None)
            if oldest.id in self._order:
                self._order.remove(oldest.id)
            evicted = oldest
        ws = Workstream(id=ws_id, name=name or f"coord-{ws_id[:4]}")
        ws.kind = WorkstreamKind.COORDINATOR
        ws.user_id = user_id
        ws.parent_ws_id = None
        # ui_factory is fast (just allocates a ConsoleCoordinatorUI)
        # so holding the lock over it is fine.  Keeping it inside the
        # lock means every observer of the placeholder sees a non-None
        # ui; only ``session`` lags behind.
        ws.ui = self._ui_factory(ws_id, user_id)
        # Wire state/rename observers so the cluster collector's
        # console pseudo-node mirrors every state transition and rename
        # that reaches the UI (see #9).  Bind ``ws_id`` by default-arg
        # so the closure captures the concrete id — ``ws`` changes
        # across concurrent installs.
        collector = self._collector
        if collector is not None:

            def _state_fanout(state: str, _wid: str = ws_id) -> None:
                if collector is None:
                    return
                try:
                    collector.emit_console_ws_state(_wid, state)
                except Exception:
                    log.debug(
                        "coord_mgr.state_fanout_failed ws=%s",
                        _wid[:8],
                        exc_info=True,
                    )

            def _rename_fanout(name: str, _wid: str = ws_id) -> None:
                if collector is None:
                    return
                try:
                    collector.emit_console_ws_rename(_wid, name)
                except Exception:
                    log.debug(
                        "coord_mgr.rename_fanout_failed ws=%s",
                        _wid[:8],
                        exc_info=True,
                    )

            try:
                ws.ui._on_state_observer = _state_fanout
                ws.ui._on_rename_observer = _rename_fanout
            except Exception:
                log.debug(
                    "coord_mgr.attach_observers_failed ws=%s",
                    ws_id[:8],
                    exc_info=True,
                )
        self._workstreams[ws_id] = ws
        self._order.append(ws_id)
        # Track the evicted coordinator's id for the active-coords
        # snapshot update below — callers must hold _lock across both
        # install and evict.
        self._refresh_active_coords_locked(
            add={ws_id: (user_id, ws.ui)},
            drop=({evicted.id} if evicted is not None else None),
        )
        return ws, evicted

    def _remove_locked(self, ws_id: str) -> None:
        """Remove a (possibly-placeholder) workstream from tracking.

        Caller MUST hold ``self._lock``.
        """
        self._workstreams.pop(ws_id, None)
        if ws_id in self._order:
            self._order.remove(ws_id)
        self._refresh_active_coords_locked(drop={ws_id})

    def _refresh_active_coords_locked(
        self,
        *,
        add: dict[str, tuple[str, Any]] | None = None,
        drop: set[str] | None = None,
    ) -> None:
        """Copy-on-write swap of the lock-free active-coords snapshot.

        Caller MUST hold ``self._lock``.  The dispatch fan-out path
        reads ``self._active_coords`` without any lock — correctness
        depends on the reference itself being swapped atomically (a
        plain attribute assignment is atomic in CPython) rather than
        mutated in place.
        """
        current = self._active_coords
        changed = False
        new = dict(current)
        if drop:
            for wid in drop:
                if new.pop(wid, None) is not None:
                    changed = True
        if add:
            for wid, meta in add.items():
                if new.get(wid) != meta:
                    new[wid] = meta
                    changed = True
        if changed:
            self._active_coords = new

    def _cleanup(self, ws: Workstream) -> None:
        """Unblock events + cancel session.  Matches WorkstreamManager._cleanup_ui."""
        WorkstreamManager._cleanup_ui(ws)

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def touch(self, ws_id: str) -> None:
        """Update ``last_active`` timestamp for eviction ordering."""
        ws = self.get(ws_id)
        if ws is not None:
            ws.last_active = time.monotonic()

    # ------------------------------------------------------------------
    # Child-event fan-out
    # ------------------------------------------------------------------

    def _merge_child_ids_locked(self, coord_ws_id: str, child_ids: Iterable[str]) -> None:
        """Merge ``child_ids`` into ``coord_ws_id``'s forward + reverse maps.

        Caller MUST hold ``self._children_lock``.  Idempotent — re-
        adding an existing child is a no-op (the reverse-index pointer
        is already correct).  Empty / falsy entries in ``child_ids``
        are skipped.

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

        Called on ``open`` / rehydrate — ``create`` seeds an empty set
        directly (a freshly-created coordinator has no persisted
        children) and the extra storage query would just pay unnecessary
        latency on the hot create-path.  Closed / deleted children are
        intentionally kept in the registry so the tree UI can render
        them grayed out — state is authoritative in storage, not here.

        UNIONs with any entries the fan-out thread already added during
        the storage query window (the coordinator is installed in
        ``self._workstreams`` before we get here, so a ``ws_created``
        event for one of its children can fire in parallel and call
        ``_add_child`` on a fresh or pre-existing set).  Overwriting
        would drop that child.
        """
        # Tenant filter: only accept persisted children whose owning
        # user_id matches the coordinator's.  Pushed into SQL via the
        # ``user_id`` kwarg so cross-tenant rows never leave the DB —
        # previously we fetched everything and filtered in Python, which
        # depended on the in-loop user_id check catching forged
        # parent_ws_id rows (migration-era data, downgrade path).  An
        # empty coord_user_id is fail-closed: skip the rebuild entirely
        # rather than matching rows with blank owners (system-owned or
        # legacy rows would otherwise leak into the fan-out set).
        with self._lock:
            coord_ws = self._workstreams.get(coord_ws_id)
        coord_user_id = coord_ws.user_id if coord_ws is not None else ""
        if not coord_user_id:
            log.debug(
                "coord_mgr.rebuild_skipped_empty_owner coord=%s",
                coord_ws_id[:8],
            )
            return
        # Cap is a sentinel, not a hard limit.  The rebuild runs at most
        # once per console cold-start per coordinator, so the fetch cost
        # is irrelevant; what matters is visibility when a coord has
        # more children than the cap — previously the tail was dropped
        # silently on every restart.  Fetch ``_rebuild_limit + 1`` so a
        # coord with exactly ``_rebuild_limit`` children (no truncation
        # yet) doesn't trigger a false-positive warning; the +1 is the
        # sentinel that proves there's at least one more row in storage.
        _rebuild_limit = 10_000
        try:
            rows = self._storage.list_workstreams(
                limit=_rebuild_limit + 1,
                parent_ws_id=coord_ws_id,
                kind=None,
                user_id=coord_user_id,
            )
        except Exception:
            log.debug(
                "coord_mgr.rebuild_children_failed ws=%s",
                coord_ws_id[:8],
                exc_info=True,
            )
            rows = []
        if len(rows) > _rebuild_limit:
            log.warning(
                "coord_mgr.rebuild_children_truncated ws=%s limit=%d",
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

        O(1) via the ``_child_to_coord`` reverse index.  Cluster events
        fire on every token tick across the cluster; a linear scan
        here turned into a hot-path tax as the retained-history set
        grew.
        """
        with self._children_lock:
            return self._child_to_coord.get(child_ws_id)

    def children_snapshot(self, coord_ws_id: str) -> list[str]:
        """Return a snapshot of the coordinator's direct child ws_ids.

        Used by ``stop_cascade`` to iterate children without holding the
        registry lock during the per-child HTTP dispatch.  A mutation
        racing with the snapshot (child spawned mid-cascade) either
        lands before the snapshot and gets cancelled, or lands after
        and is out of scope for this batch — both outcomes are safe.
        Returns an empty list for unknown coordinators.
        """
        with self._children_lock:
            child_set = self._children.get(coord_ws_id)
            return list(child_set) if child_set else []

    def register_children(self, coord_ws_id: str, child_ws_ids: Iterable[str]) -> None:
        """Merge ``child_ws_ids`` into the coordinator's child set.  Idempotent."""
        with self._children_lock:
            self._merge_child_ids_locked(coord_ws_id, child_ws_ids)

    def _pop_coord_registry_locked(self, coord_ws_id: str) -> None:
        """Remove a coordinator's forward set + reverse-index entries.

        Caller MUST hold ``self._children_lock``.  Used by close /
        eviction paths so stale coordinators don't leak registry
        entries.  No-op if the coordinator is unknown.
        """
        child_set = self._children.pop(coord_ws_id, None)
        if child_set is None:
            return
        for cid in child_set:
            # Defensive: only clear the reverse entry if it still
            # points at THIS coordinator.  If a child has since been
            # reassigned (unusual but possible on schema changes), we
            # don't want to orphan the new owner's entry.
            if self._child_to_coord.get(cid) == coord_ws_id:
                self._child_to_coord.pop(cid, None)

    def _add_child(self, coord_ws_id: str, child_ws_id: str) -> bool:
        """Record a new child_ws_id for ``coord_ws_id``.

        Returns True when the child is newly added (first observation),
        False when it was already tracked.  Caller uses the return to
        decide whether to re-emit a ``child_ws_created`` event.
        """
        with self._children_lock:
            existing = self._children.setdefault(coord_ws_id, set())
            if child_ws_id in existing:
                return False
            existing.add(child_ws_id)
            self._child_to_coord[child_ws_id] = coord_ws_id
            return True

    def start_child_event_fanout(self, collector: ClusterCollector) -> None:
        """Subscribe to cluster events and start the filter + re-emit thread.

        Idempotent — calling twice is a no-op (already-started fan-out
        thread stays).  Called once from the console lifespan after both
        the collector and the coordinator manager are constructed.
        """
        if self._fanout_thread is not None and self._fanout_thread.is_alive():
            return
        self._collector = collector
        self._collector_queue = queue.Queue(maxsize=1000)
        # Ensure the "console" pseudo-node exists in the snapshot map so
        # emit_console_ws_* calls from create / close / open land on a
        # real node entry the snapshot will surface (see #9).
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
        # memory when the collector binds.  Prevents a race where early
        # creates happened before the collector was wired up and their
        # rows never showed on the snapshot.
        with self._lock:
            active = [(w.id, w) for w in self._workstreams.values()]
        for wid, ws in active:
            try:
                collector.emit_console_ws_created(
                    wid,
                    name=ws.name,
                    user_id=ws.user_id or "",
                    kind=WorkstreamKind.COORDINATOR.value,
                    state=ws.state.value,
                    parent_ws_id=None,
                )
            except Exception:
                log.debug(
                    "coord_mgr.collector_seed_failed ws=%s",
                    wid[:8],
                    exc_info=True,
                )
        self._fanout_stop.clear()
        t = threading.Thread(
            target=self._fanout_loop,
            name="coord-mgr-child-fanout",
            daemon=True,
        )
        self._fanout_thread = t
        t.start()

    def _prime_children_from_snapshot(self, snapshot: dict[str, Any]) -> None:
        """Populate ``_children`` + ``_child_to_coord`` from a collector
        snapshot.

        The snapshot's per-node workstreams carry ``parent_ws_id`` (phase
        3 propagated it end-to-end).  For every workstream whose parent
        is an in-memory coordinator, record the child so the fan-out
        filter sees it immediately.  Running under both locks keeps the
        registry write consistent with concurrent close / eviction.
        """
        nodes = snapshot.get("nodes", []) if isinstance(snapshot, dict) else []
        if not nodes:
            return
        # Bucket children by parent so each coordinator's forward set +
        # reverse index gets one helper call instead of one per child.
        by_parent: dict[str, list[str]] = {}
        with self._lock:
            known = set(self._workstreams)
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

        Safe to call multiple times; idempotent.  Invoked from the
        console lifespan teardown so SSE listener queues don't leak.
        """
        self._fanout_stop.set()
        t = self._fanout_thread
        q = self._collector_queue
        coll = self._collector
        self._fanout_thread = None
        self._collector_queue = None
        self._collector = None
        if coll is not None and q is not None:
            try:
                coll.unregister_listener(q)
            except Exception:
                log.debug("coord_mgr.unregister_listener_failed", exc_info=True)
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
                log.debug("coord_mgr.fanout.dispatch_failed", exc_info=True)

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
            # Lock-free presence + tenant check via the atomically-
            # swapped _active_coords snapshot.  The manager updates
            # this dict by reference under _lock on every install /
            # remove, so an in-flight dispatch that reads a stale
            # snapshot either sees the coord or doesn't — both
            # outcomes are safe (a coord that just evicted gets one
            # last fan-out; a coord that just installed gets one
            # missed event — the next state change picks it up).
            active = self._active_coords
            meta = active.get(parent)
            if meta is None:
                return
            coord_user_id, coord_ui = meta
            # Tenant-isolation gate: cross-tenant fan-out is the
            # vuln the server-side create endpoint also gates
            # against.  Defense-in-depth here means a spoofed
            # parent_ws_id from any path (bypassed server check,
            # migration-era data) still can't route a child's
            # real-time events into a foreign coordinator's SSE
            # stream.  Empty user_id on either side fails closed.
            event_user = event.get("user_id") or ""
            if not event_user or event_user != coord_user_id:
                return
            # Only _children_lock is needed for the registry write
            # now — the hot path no longer contends self._lock.
            # Re-check the active-coords snapshot INSIDE the lock
            # before mutating: a concurrent close()/eviction can pop
            # _children[parent] between the lock-free read at line 912
            # and the lock acquisition below, after which setdefault
            # would resurrect the entry — leaking the registry key
            # forever and enqueuing onto the now-closed coordinator's
            # UI.  Re-reading _active_coords here catches that race
            # without taking self._lock.
            newly_added = False
            with self._children_lock:
                if parent not in self._active_coords:
                    return
                existing = self._children.setdefault(parent, set())
                if ws_id not in existing:
                    existing.add(ws_id)
                    self._child_to_coord[ws_id] = parent
                    newly_added = True
            if not newly_added:
                return
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

        if etype in ("cluster_state", "ws_closed", "ws_rename"):
            coord_id = self._coord_for_child(ws_id)
            if coord_id is None:
                return
            owning_ws = self.get(coord_id)
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
    body = {**payload, "ws_id": coord_ws_id}
    try:
        enqueue(body)
    except Exception:
        log.debug("coord_mgr.enqueue_failed ws=%s", coord_ws_id[:8], exc_info=True)
