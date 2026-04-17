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
from typing import TYPE_CHECKING

from turnstone.core.log import get_logger
from turnstone.core.workstream import (
    Workstream,
    WorkstreamManager,
    WorkstreamState,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from turnstone.console.coordinator_ui import ConsoleCoordinatorUI
    from turnstone.core.session import ChatSession
    from turnstone.core.storage._protocol import StorageBackend

log = get_logger(__name__)


class CoordinatorManager:
    """Tracks live coordinator sessions in the console process."""

    # Pseudo-node id persisted on coordinator rows so ``workstreams.node_id``
    # stays non-NULL and list / audit surfaces can distinguish coordinators
    # from real-node workstreams.  The hash-ring router treats it as an
    # unroutable sentinel — coordinators never land on real nodes.
    NODE_ID = "console"

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
                kind="coordinator",
                parent_ws_id=None,
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
                kind="coordinator",
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
                if row is None or row.get("kind") != "coordinator":
                    return None
                # close()/delete() only soft-mark the row — refuse to
                # resurrect those sessions on any subsequent GET, or the
                # Close button becomes silently reversible on URL revisit
                # and burns max_active capacity.
                if row.get("state") in {"closed", "deleted"}:
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

                try:
                    ws.session = self._session_factory(
                        ws.ui,
                        None,
                        ws_id,
                        skill=None,
                        kind="coordinator",
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
            except Exception:
                log.exception("coord_mgr.worker_failed ws=%s", ws.id[:8])

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
        self._cleanup(ws)
        try:
            self._storage.update_workstream_state(ws_id, "closed")
        except Exception:
            log.debug("coord_mgr.state_update_failed ws=%s", ws_id[:8], exc_info=True)
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
        ws.kind = "coordinator"
        ws.user_id = user_id
        ws.parent_ws_id = None
        # ui_factory is fast (just allocates a ConsoleCoordinatorUI)
        # so holding the lock over it is fine.  Keeping it inside the
        # lock means every observer of the placeholder sees a non-None
        # ui; only ``session`` lags behind.
        ws.ui = self._ui_factory(ws_id, user_id)
        self._workstreams[ws_id] = ws
        self._order.append(ws_id)
        return ws, evicted

    def _remove_locked(self, ws_id: str) -> None:
        """Remove a (possibly-placeholder) workstream from tracking.

        Caller MUST hold ``self._lock``.
        """
        self._workstreams.pop(ws_id, None)
        if ws_id in self._order:
            self._order.remove(ws_id)

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
