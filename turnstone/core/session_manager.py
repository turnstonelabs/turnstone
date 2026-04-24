"""Unified manager for workstream-shaped sessions.

Collapses ``WorkstreamManager`` (interactive) and ``CoordinatorManager``
(coordinator) into one class. Kind-specific transport and session
construction live on a ``SessionKindAdapter`` Protocol; the manager
itself owns the invariant mechanics — slot accounting, eviction,
persistence, per-ws lock refcount for concurrent lazy rehydrate.
"""

from __future__ import annotations

import contextlib
import threading
import time
import uuid
from typing import TYPE_CHECKING, Any, Protocol

from turnstone.core.log import get_logger
from turnstone.core.workstream import Workstream, WorkstreamKind, WorkstreamState

if TYPE_CHECKING:
    from collections.abc import Callable

    from turnstone.core.session import ChatSession, SessionUI
    from turnstone.core.storage._protocol import StorageBackend

log = get_logger(__name__)


class SessionKindAdapter(Protocol):
    """Per-kind policies the shared ``SessionManager`` delegates to.

    The manager owns invariant mechanics. The adapter owns:

    - **Transport**: how lifecycle events (``ws_created`` /
      ``ws_state`` / ``ws_closed``) fan out. Interactive pushes onto a
      per-UI listener queue + global event queue; coordinator emits on
      the cluster collector's pseudo-node.
    - **Session construction**: what UI class wraps the workstream,
      what ``ChatSession`` factory signature applies.

    Intentionally NOT on the Protocol (see design brief's "Decisions
    settled during the pruning pass"): per-kind permission scope
    (static kind→scope map in handlers), child-spawn / quota gates
    (coordinator tool owns), children registry hooks (coordinator tool
    owns), ``active_id`` / ``switch`` focus state (frontend owns).
    """

    kind: WorkstreamKind

    def emit_created(self, ws: Workstream) -> None: ...
    def emit_rehydrated(self, ws: Workstream) -> None:
        """Fire the lifecycle event for a lazy-rehydrated workstream.

        Distinct from ``emit_created`` so coord-side adapters can
        perform storage-seeded registry rebuilds only on the resurrect
        path. A freshly-created workstream provably has zero children,
        so the rebuild query is wasted. Interactive adapters with no
        such registry just delegate to ``emit_created``.
        """

    def emit_state(self, ws: Workstream, state: WorkstreamState) -> None: ...
    def emit_closed(
        self,
        ws_id: str,
        *,
        reason: str = "closed",
        name: str = "",
    ) -> None:
        """Fire the close event. ``reason`` is "closed" for manual
        close, "evicted" for capacity eviction (frontend shows a
        distinct toast). ``name`` is the workstream's display name —
        the frontend eviction toast includes it so the user sees
        which workstream was evicted."""

    def cleanup_ui(self, ws: Workstream) -> None:
        """Unblock per-UI events on close; cancel + close the session."""

    def build_ui(self, ws: Workstream) -> SessionUI:
        """Construct the kind-specific UI for a fresh workstream."""

    def build_session(
        self,
        ws: Workstream,
        *,
        skill: str | None = None,
        model: str | None = None,
        client_type: str = "",
        **extra: Any,
    ) -> ChatSession:
        """Construct the ``ChatSession`` for a workstream whose ``ui`` is already attached.

        ``**extra`` is the pass-through for kind-specific per-call
        options (e.g. interactive's ``judge_model``). Each adapter
        ignores what it doesn't recognise; the manager stays
        kind-agnostic.
        """


class SessionManager:
    """Unified lifecycle manager for a single workstream kind.

    Instantiate once per kind: one for interactive on the node, one
    for coordinators on the console. The eviction pool is partitioned
    by kind — a coordinator can't evict an interactive workstream.
    """

    def __init__(
        self,
        adapter: SessionKindAdapter,
        *,
        storage: StorageBackend,
        max_active: int,
        node_id: str | None = None,
    ) -> None:
        if max_active < 1:
            raise ValueError(f"max_active must be >= 1, got {max_active}")
        self._adapter = adapter
        self._storage = storage
        self._max_active = max_active
        self._node_id = node_id
        self._workstreams: dict[str, Workstream] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()
        # Per-ws_id refcounted locks serializing concurrent lazy
        # rehydrate of the same ws_id. Ported from
        # ``CoordinatorManager._open_locks``: without refcounting, a
        # third arrival could allocate a fresh lock for the same ws_id
        # and defeat serialization on the failure path.
        self._open_locks: dict[str, tuple[threading.Lock, int]] = {}
        # CLI REPL focus state. The web UI tracks active tab itself;
        # the CLI uses these for ``/switch`` / ``/next``. Coordinator
        # manager never reads them.
        self._active_id: str | None = None
        self._eviction_count: int = 0
        # Optional state-change observer. The CLI sets this to a
        # callback that prints a background-attention notification
        # when a non-focused workstream transitions to ATTENTION.
        # Web/coord paths use the adapter's emit_state for their own
        # fan-out; this is a second, manager-level hook for callers
        # that don't consume SSE.
        self._on_state_change: Callable[[str, WorkstreamState], None] | None = None

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def max_active(self) -> int:
        return self._max_active

    @property
    def kind(self) -> WorkstreamKind:
        return self._adapter.kind

    @property
    def adapter(self) -> SessionKindAdapter:
        """The kind adapter this manager was constructed with.

        Exposed so callers (e.g. the HTTP route registrar) that already
        hold a manager don't have to re-thread the adapter through every
        construction layer.
        """
        return self._adapter

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._workstreams)

    @property
    def eviction_count(self) -> int:
        """Total number of workstreams auto-evicted by ``create`` / ``open``."""
        return self._eviction_count

    # ------------------------------------------------------------------
    # CLI focus state
    #
    # Used by the CLI REPL only — the web UI tracks active tab in
    # browser state and coordinator navigation is URL-based.
    # ------------------------------------------------------------------

    @property
    def active_id(self) -> str | None:
        return self._active_id

    def get_active(self) -> Workstream | None:
        with self._lock:
            if self._active_id is None:
                return None
            return self._workstreams.get(self._active_id)

    def switch(self, ws_id: str) -> Workstream | None:
        with self._lock:
            if ws_id in self._workstreams:
                self._active_id = ws_id
                return self._workstreams[ws_id]
        return None

    def switch_by_index(self, index: int) -> Workstream | None:
        """1-based index into the creation-order list."""
        with self._lock:
            if 1 <= index <= len(self._order):
                ws_id = self._order[index - 1]
                self._active_id = ws_id
                return self._workstreams.get(ws_id)
        return None

    def index_of(self, ws_id: str) -> int:
        """1-based creation-order index of a workstream, or 0 if absent."""
        with self._lock:
            try:
                return self._order.index(ws_id) + 1
            except ValueError:
                return 0

    # ------------------------------------------------------------------
    # create — new session
    # ------------------------------------------------------------------

    def create(
        self,
        *,
        user_id: str,
        name: str = "",
        skill: str | None = None,
        skill_id: str = "",
        skill_version: int = 0,
        ws_id: str = "",
        model: str | None = None,
        client_type: str = "",
        parent_ws_id: str | None = None,
        **extra_session_kwargs: Any,
    ) -> Workstream:
        """Construct a new workstream, persist, and register.

        Slot reservation + placeholder install happen under the lock
        (single-phase). Session construction runs outside the lock; on
        failure the in-memory slot is freed so capacity isn't leaked.
        The storage row survives construction failure — the next
        ``open(ws_id)`` retries session construction rather than
        forcing the user to create a brand-new workstream.

        Raises ``RuntimeError`` when the manager is at capacity with
        no idle workstream to evict — callers (HTTP handlers) translate
        this to 429.
        """
        ws_id = ws_id or uuid.uuid4().hex
        effective_name = name or f"ws-{ws_id[:4]}"

        with self._lock:
            ws, evicted = self._reserve_and_install_locked(
                ws_id, user_id=user_id, name=effective_name, parent_ws_id=parent_ws_id
            )

        if evicted is not None:
            self._adapter.cleanup_ui(evicted)
            self._adapter.emit_closed(evicted.id, reason="evicted", name=evicted.name)

        # Persist before session construction. Fail-closed: if the row
        # can't be written, the in-memory session would be invisible to
        # any lazy-rehydrate path and show up as "missing" after
        # restart — surface the storage failure now.
        try:
            self._storage.register_workstream(
                ws_id,
                node_id=self._node_id,
                user_id=user_id,
                name=ws.name,
                kind=self.kind,
                parent_ws_id=parent_ws_id,
                skill_id=skill_id,
                skill_version=skill_version,
            )
        except Exception:
            with self._lock:
                self._remove_locked(ws_id)
            raise

        try:
            ws.session = self._adapter.build_session(
                ws,
                skill=skill,
                model=model,
                client_type=client_type,
                **extra_session_kwargs,
            )
        except Exception:
            # Release the slot so capacity isn't leaked, and call
            # cleanup_ui on the placeholder so any listener/lock state
            # the UI factory allocated is released. Storage row stays:
            # the next open() on this ws_id retries construction.
            self._adapter.cleanup_ui(ws)
            with self._lock:
                self._remove_locked(ws_id)
            raise

        self._adapter.emit_created(ws)
        return ws

    # ------------------------------------------------------------------
    # open — lazy rehydrate for a persisted workstream
    # ------------------------------------------------------------------

    def open(self, ws_id: str) -> Workstream | None:
        """Rehydrate a persisted workstream on demand.

        Returns ``None`` when the row doesn't exist, doesn't match our
        kind, or is tombstoned (``state='deleted'``). Turnstone is a
        trusted-team tool — ownership is metadata for audit/display,
        not an access boundary; HTTP handlers gate callers at the
        scope level, not the row level.

        Serializes concurrent opens of the same ws_id through a
        per-ws refcounted lock so two GETs don't each construct a
        session and orphan a worker thread.
        """
        open_lock = self._acquire_open_lock(ws_id)
        try:
            with open_lock:
                with self._lock:
                    existing = self._workstreams.get(ws_id)
                    if existing is not None and existing.session is not None:
                        return existing

                row = self._storage.get_workstream(ws_id)
                if row is None or row.get("kind") != self.kind:
                    return None
                # ``deleted`` is a tombstone — never resurrect.
                # ``closed`` IS resurrectable; the Saved Workstreams
                # landing makes restore an explicit user action, and
                # ``_reserve_and_install_locked`` still enforces
                # max_active (evicting an idle peer or raising).
                if row.get("state") == "deleted":
                    return None

                with self._lock:
                    # Re-check fast path — another thread may have raced
                    # through the whole open while we checked storage.
                    existing = self._workstreams.get(ws_id)
                    if existing is not None and existing.session is not None:
                        return existing
                    ws, evicted = self._reserve_and_install_locked(
                        ws_id,
                        user_id=row.get("user_id") or "",
                        name=row.get("name") or f"ws-{ws_id[:4]}",
                        parent_ws_id=row.get("parent_ws_id"),
                    )

                if evicted is not None:
                    self._adapter.cleanup_ui(evicted)
                    self._adapter.emit_closed(evicted.id, reason="evicted", name=evicted.name)

                try:
                    ws.session = self._adapter.build_session(ws)
                except Exception:
                    # Clean up the UI the adapter built before re-raising
                    # so any listener/lock resources are released.
                    self._adapter.cleanup_ui(ws)
                    with self._lock:
                        self._remove_locked(ws_id)
                    raise

                if ws.session is not None and hasattr(ws.session, "resume"):
                    try:
                        ws.session.resume(ws_id)
                    except Exception:
                        log.debug("session_mgr.resume_failed ws=%s", ws_id[:8], exc_info=True)

                # No DB state-flip on resurrect. The in-memory session
                # is IDLE; the DB row may still say 'closed' from the
                # last close(). The next set_state() call syncs it
                # naturally; writing 'idle' here could race a concurrent
                # close() that writes 'closed' under self._lock.
                self._adapter.emit_rehydrated(ws)
                return ws
        finally:
            self._release_open_lock(ws_id)

    def _acquire_open_lock(self, ws_id: str) -> threading.Lock:
        with self._lock:
            entry = self._open_locks.get(ws_id)
            if entry is None:
                lk = threading.Lock()
                self._open_locks[ws_id] = (lk, 1)
                return lk
            lk, refs = entry
            self._open_locks[ws_id] = (lk, refs + 1)
            return lk

    def _release_open_lock(self, ws_id: str) -> None:
        with self._lock:
            entry = self._open_locks.get(ws_id)
            if entry is None:
                return
            lk, refs = entry
            if refs <= 1:
                self._open_locks.pop(ws_id, None)
            else:
                self._open_locks[ws_id] = (lk, refs - 1)

    # ------------------------------------------------------------------
    # close / set_state / close_idle
    # ------------------------------------------------------------------

    def close(self, ws_id: str) -> bool:
        """Soft-close: unload from memory + mark state=closed in storage.

        Returns ``True`` when a live workstream was removed,
        ``False`` if the id wasn't tracked.
        """
        with self._lock:
            ws = self._workstreams.pop(ws_id, None)
            if ws is None:
                return False
            if ws_id in self._order:
                self._order.remove(ws_id)
            if self._active_id == ws_id:
                self._active_id = self._order[0] if self._order else None

        self._adapter.cleanup_ui(ws)
        # Serialize the storage write against any in-flight set_state
        # via ws._lock. Setting ``_closed`` inside the lock makes the
        # close visible to set_state before we release — any set_state
        # that acquires ws._lock after us sees _closed=True and skips
        # its storage write.
        with ws._lock:
            ws._closed = True
            try:
                self._storage.update_workstream_state(ws_id, "closed")
            except Exception:
                log.debug("session_mgr.state_update_failed ws=%s", ws_id[:8], exc_info=True)
            try:
                self._storage.delete_workstream_override(ws_id)
            except Exception:
                log.debug("session_mgr.override_delete_failed ws=%s", ws_id[:8], exc_info=True)
        self._adapter.emit_closed(ws_id, name=ws.name)
        return True

    def set_state(
        self,
        ws_id: str,
        state: WorkstreamState,
        error_msg: str = "",
    ) -> None:
        """Update a workstream's state + fire the adapter's state event.

        Serializes against ``close()`` via ``ws._lock``: if close ran
        first, it set ``ws._closed=True`` and wrote ``state='closed'``
        to storage under the same lock — set_state sees the tombstone
        and skips its own write to avoid resurrecting a closed row.
        """
        with self._lock:
            ws = self._workstreams.get(ws_id)
            if ws is None:
                return
        with ws._lock:
            if ws._closed:
                # close() already ran; don't overwrite 'closed' in
                # storage with a lagging set_state write.
                return
            ws.state = state
            ws.last_active = time.monotonic()
            ws.error_message = error_msg
            try:
                self._storage.update_workstream_state(ws_id, state.value)
            except Exception:
                log.debug("session_mgr.state_update_failed ws=%s", ws_id[:8], exc_info=True)
        self._adapter.emit_state(ws, state)
        if self._on_state_change is not None:
            with contextlib.suppress(Exception):
                self._on_state_change(ws_id, state)

    def cancel(self, ws_id: str) -> bool:
        """Cancel in-flight generation and unblock any pending approval / plan.

        Does NOT unload the workstream — use ``close`` for that. The
        session stays live and can receive further messages. Returns
        ``False`` if the workstream isn't tracked.
        """
        ws = self.get(ws_id)
        if ws is None:
            return False
        if ws.session is not None and hasattr(ws.session, "cancel"):
            try:
                ws.session.cancel()
            except Exception:
                log.debug("session_mgr.cancel_failed ws=%s", ws_id[:8], exc_info=True)
        if ws.ui is not None:
            if hasattr(ws.ui, "resolve_approval"):
                with contextlib.suppress(Exception):
                    ws.ui.resolve_approval(False, "cancelled")
            if hasattr(ws.ui, "resolve_plan"):
                with contextlib.suppress(Exception):
                    ws.ui.resolve_plan("reject")
        return True

    def close_idle(self, max_age_seconds: float) -> list[str]:
        """Close IDLE workstreams inactive for more than ``max_age_seconds``.

        Returns the list of closed ws_ids. Unlike the old WSM version,
        this does NOT skip the last workstream — the default-startup
        relic is gone, callers can handle the 0-workstream case.

        Atomic pop per victim under ``self._lock`` (bug-5): a pending
        tool result can flip state IDLE→RUNNING between the snapshot
        and the close, so the state test + pop must run together.
        Batches every pop under one ``self._lock`` acquisition (perf-5)
        rather than locking once per victim.
        """
        now = time.monotonic()
        popped: list[Workstream] = []
        with self._lock:
            # Collect candidate ids first to avoid mutating
            # ``self._workstreams`` while iterating it.
            victims = [
                ws.id
                for ws in self._workstreams.values()
                if ws.state == WorkstreamState.IDLE and (now - ws.last_active) > max_age_seconds
            ]
            for ws_id in victims:
                ws = self._close_if_idle_locked(ws_id)
                if ws is not None:
                    popped.append(ws)

        closed_ids: list[str] = []
        for ws in popped:
            self._adapter.cleanup_ui(ws)
            # Mirrors ``close``: set ``ws._closed`` inside ws._lock
            # before the storage write so any concurrent set_state sees
            # the tombstone and skips its own write.
            with ws._lock:
                ws._closed = True
                try:
                    self._storage.update_workstream_state(ws.id, "closed")
                except Exception:
                    log.debug("session_mgr.state_update_failed ws=%s", ws.id[:8], exc_info=True)
                try:
                    self._storage.delete_workstream_override(ws.id)
                except Exception:
                    log.debug("session_mgr.override_delete_failed ws=%s", ws.id[:8], exc_info=True)
            self._adapter.emit_closed(ws.id, name=ws.name)
            closed_ids.append(ws.id)
        return closed_ids

    def _close_if_idle_locked(self, ws_id: str) -> Workstream | None:
        """Pop the workstream atomically if it's still IDLE.

        Caller must hold ``self._lock``. Returns the popped ws on
        success, ``None`` if it wasn't IDLE or not tracked. Used by
        :meth:`close_idle` so the state-check and pop happen under one
        lock acquisition — a pending tool result can flip state to
        RUNNING between an out-of-lock re-check and ``close`` picking
        up ``self._lock`` again.
        """
        ws = self._workstreams.get(ws_id)
        if ws is None or ws.state != WorkstreamState.IDLE:
            return None
        self._workstreams.pop(ws_id, None)
        if ws_id in self._order:
            self._order.remove(ws_id)
        if self._active_id == ws_id:
            self._active_id = self._order[0] if self._order else None
        return ws

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    def get(self, ws_id: str) -> Workstream | None:
        with self._lock:
            return self._workstreams.get(ws_id)

    def list_all(self) -> list[Workstream]:
        """Return workstreams in creation order."""
        with self._lock:
            return [self._workstreams[wid] for wid in self._order if wid in self._workstreams]

    # ------------------------------------------------------------------
    # Internal — slot reservation (caller holds self._lock)
    # ------------------------------------------------------------------

    def _reserve_and_install_locked(
        self,
        ws_id: str,
        *,
        user_id: str,
        name: str,
        parent_ws_id: str | None = None,
    ) -> tuple[Workstream, Workstream | None]:
        """Install a placeholder ``Workstream`` under ``self._lock``.

        Ported from ``CoordinatorManager._reserve_and_install_locked``:
        single-phase eviction, placeholders with ``session=None`` count
        toward capacity but are never themselves eviction candidates
        (a burst of concurrent creates must not evict each other —
        that path silently exceeded max_active on the old WSM side).

        Caller MUST hold ``self._lock``. UI allocation is included in
        the locked path so concurrent ``get()`` never observes a
        placeholder with ``ui=None``; only ``session`` lags.
        """
        if ws_id in self._workstreams:
            # Defensive — create() uses a fresh uuid and open()
            # serializes on the per-ws lock which already bounces the
            # repeated install via the fast path.
            raise RuntimeError(f"ws_id {ws_id[:8]!r} already tracked by SessionManager")

        evicted: Workstream | None = None
        if len(self._workstreams) >= self._max_active:
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
                raise RuntimeError(f"All {self._max_active} slots are active")
            self._workstreams.pop(oldest.id, None)
            if oldest.id in self._order:
                self._order.remove(oldest.id)
            if self._active_id == oldest.id:
                self._active_id = self._order[0] if self._order else None
            self._eviction_count += 1
            try:
                from turnstone.core.metrics import metrics as _m

                _m.record_eviction()
            except Exception:
                log.debug("session_mgr.metrics_eviction_failed", exc_info=True)
            evicted = oldest

        ws = Workstream(id=ws_id, name=name)
        ws.kind = self.kind
        ws.user_id = user_id
        ws.parent_ws_id = parent_ws_id if parent_ws_id else None
        try:
            ws.ui = self._adapter.build_ui(ws)
        except Exception:
            # An IDLE peer may already have been popped above; if we
            # propagate without unwinding, that peer leaks its session
            # + worker + UI listeners and no ws_closed reaches
            # subscribers.
            if evicted is not None:
                self._adapter.cleanup_ui(evicted)
                self._adapter.emit_closed(evicted.id, reason="evicted", name=evicted.name)
            raise
        self._workstreams[ws_id] = ws
        self._order.append(ws_id)
        if self._active_id is None:
            self._active_id = ws_id
        return ws, evicted

    def _remove_locked(self, ws_id: str) -> None:
        """Drop a (possibly-placeholder) workstream from tracking.

        Caller MUST hold ``self._lock``. Used on rollback paths when
        session construction or persistence fails after slot
        reservation — the placeholder otherwise pins capacity forever.
        """
        self._workstreams.pop(ws_id, None)
        if ws_id in self._order:
            self._order.remove(ws_id)
        if self._active_id == ws_id:
            self._active_id = self._order[0] if self._order else None
