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
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol

from turnstone.core.log import get_logger
from turnstone.core.workstream import Workstream, WorkstreamKind, WorkstreamState

if TYPE_CHECKING:
    from collections.abc import Callable

    from turnstone.core.session import ChatSession, SessionUI
    from turnstone.core.state_writer import StateWriter
    from turnstone.core.storage._protocol import StorageBackend

log = get_logger(__name__)


# Maps each workstream kind to the ``services.service_type`` its hosting
# process registers under.  Used by ``SessionManager.close_idle`` pass 2
# to enumerate live peer processes for orphan-reaper liveness scoping.
# Server processes register as ``("server", node_id, ...)`` (see
# ``turnstone/server.py``); the console process as ``("console",
# "console", ...)`` (see ``turnstone/console/server.py``).  Deriving from
# kind here removes a duplicated-config footgun: any caller that builds
# a ``SessionManager`` automatically gets the correct service_type for
# its kind, with no risk of miswiring INTERACTIVE→"console" or vice
# versa.
_KIND_SERVICE_TYPE: dict[WorkstreamKind, str] = {
    WorkstreamKind.INTERACTIVE: "server",
    WorkstreamKind.COORDINATOR: "console",
}


class SessionKindAdapter(Protocol):
    """Per-kind construction + cleanup policies the shared ``SessionManager`` delegates to.

    The manager owns invariant mechanics. The adapter owns:

    - **Session construction**: what UI class wraps the workstream,
      what ``ChatSession`` factory signature applies.
    - **UI cleanup**: unblocking pending approval / plan / foreground
      events when a workstream closes.

    Lifecycle event fan-out (``ws_created`` / ``ws_state`` /
    ``ws_closed``) lives on a *separate* Protocol —
    :class:`SessionEventEmitter` — wired through the manager's
    optional ``event_emitter`` kwarg. Both production adapters
    implement *both* Protocols. The asymmetry is in *which* emit
    methods carry real bodies:

    - Coordinator: all four ``emit_*`` are real — every transition
      fans out via the cluster collector's pseudo-node.
    - Interactive: only ``emit_closed`` is load-bearing (it's the
      sole transport path for ``ws_closed`` onto the global SSE
      queue); ``emit_created`` / ``emit_state`` / ``emit_rehydrated``
      are documented no-op stubs because those events fire from
      out-of-band paths (the create HTTP handler enqueues
      ``ws_created`` after attachment validation;
      ``WebUI._broadcast_state`` enqueues a richer ``ws_state``
      payload than this Protocol carries).

    The manager's ``if self._event_emitter is not None`` guard
    handles the case where no emitter is wired at all — used by
    tests that don't care about the event side effects, and reserved
    for future kinds whose lifecycle transitions don't fan out
    anywhere.

    Intentionally NOT on the Protocol (see design brief's "Decisions
    settled during the pruning pass"): per-kind permission scope
    (static kind→scope map in handlers), child-spawn / quota gates
    (coordinator tool owns), children registry hooks (coordinator tool
    owns), ``active_id`` / ``switch`` focus state (frontend owns).
    """

    kind: WorkstreamKind

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


class SessionEventEmitter(Protocol):
    """Optional transport fan-out for lifecycle events.

    Wired into :class:`SessionManager` via the ``event_emitter`` kwarg.
    Both production adapters implement this Protocol; the manager's
    ``if self._event_emitter is not None`` guard exists for kinds /
    tests that omit an emitter entirely.

    Implementing the Protocol does not commit a kind to wiring every
    method — interactive's ``emit_created`` / ``emit_state`` /
    ``emit_rehydrated`` are documented no-op stubs because those
    events fire from out-of-band channels (``WebUI._broadcast_state``
    for state, the create HTTP handler for ``ws_created`` after
    attachment validation). Only ``emit_closed`` carries a real body
    on interactive. Coordinator's four methods are all real (cluster
    collector's pseudo-node sees every transition). See
    :class:`SessionKindAdapter` docstring for the asymmetry rationale.
    """

    def emit_created(self, ws: Workstream) -> None:
        """Fire the lifecycle event for a freshly created workstream."""

    def emit_rehydrated(self, ws: Workstream) -> None:
        """Fire the lifecycle event for a lazy-rehydrated workstream.

        Distinct from ``emit_created`` so emitters can do extra setup
        only on the resurrect path (the coordinator emitter rebuilds
        its children registry from storage on rehydrate; a fresh
        ``create`` provably has zero children, so the rebuild query is
        skipped).
        """

    def emit_state(self, ws: Workstream, state: WorkstreamState) -> None:
        """Fire the state-transition event."""

    def emit_closed(
        self,
        ws_id: str,
        *,
        reason: str = "closed",
        name: str = "",
    ) -> None:
        """Fire the close event.

        ``reason`` is ``"closed"`` for manual close, ``"evicted"`` for
        capacity eviction (frontend shows a distinct toast). ``name``
        is the workstream's display name — the eviction toast
        includes it so the user sees which workstream was evicted.
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
        state_writer: StateWriter | None = None,
        event_emitter: SessionEventEmitter | None = None,
    ) -> None:
        if max_active < 1:
            raise ValueError(f"max_active must be >= 1, got {max_active}")
        self._adapter = adapter
        self._storage = storage
        self._max_active = max_active
        # Optional buffered state-writer. Pass one in for production
        # paths so non-terminal ``set_state`` writes don't hold
        # ``ws._lock`` across a sync DB UPDATE. Tests can leave it
        # None and get the legacy direct-write behaviour.
        self._state_writer = state_writer
        # Optional lifecycle-event emitter. Wired by both production
        # lifespans (interactive's emitter is the adapter itself, which
        # also satisfies the Protocol; coord wires its own adapter the
        # same way). When ``None``, the manager skips every emit_*
        # call — used by tests that don't care about the event side
        # effects, and reserved for future kinds whose lifecycle
        # transitions don't fan out anywhere.
        self._event_emitter = event_emitter
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
        # State-change subscribers. Multi-subscriber to support the
        # CLI's background-attention notification AND the in-process
        # ``SameNodeChildSource`` strategy that delivers child
        # workstream state changes to a parent's UI without going
        # through the cluster bus. Each callback fires under
        # exception-suppression so one failing subscriber doesn't
        # block the others. Subscribers register via
        # :meth:`subscribe_to_state`. ``_state_subscribers_lock``
        # guards mutation + snapshot — set_state copies the list
        # under the lock then iterates the snapshot unlocked so a
        # slow subscriber doesn't block subscribe/unsubscribe (and
        # so concurrent subscribe/unsubscribe during a state event
        # can't shift the iterator's index — caught by /review bug-1).
        self._state_subscribers: list[Callable[[str, WorkstreamState], None]] = []
        self._state_subscribers_lock = threading.Lock()

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
    def _service_type(self) -> str | None:
        """``services.service_type`` this manager's hosting process registers
        under, derived from its ``kind``.  Used by ``close_idle`` pass 2 to
        enumerate live peer processes.  Returns ``None`` for kinds that have
        no production service mapping (only the two existing kinds map
        today; ``None`` would be a marker for a future kind without a
        clustered hosting model)."""
        return _KIND_SERVICE_TYPE.get(self.kind)

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
        defer_emit_created: bool = False,
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

        ``defer_emit_created``: when ``True``, the ``emit_created`` call
        on the configured event emitter is skipped. The caller takes
        ownership of advertising the new workstream — typically by
        calling :meth:`commit_create` after running additional
        post-create work that might roll the create back (e.g. the
        Stage 2 ``create`` HTTP handler runs uploaded-attachment
        validation post-create and rolls the workstream back via
        :meth:`discard` on validation failure; deferring the emit
        means a rolled-back create produces no phantom create→close
        pair on the cluster events stream).

        Default ``False`` preserves the legacy "advertise immediately"
        contract for direct callers (test fixtures, the CLI REPL,
        anything that doesn't have a post-create gate).

        Caller-bug if ``defer_emit_created=True`` is set but neither
        :meth:`commit_create` nor :meth:`discard` is ever called: the
        slot is held forever (capacity leak). The HTTP handler bracket
        runs both terminations within a single request lifecycle.
        """
        ws_id = ws_id or uuid.uuid4().hex
        effective_name = name or f"ws-{ws_id[:4]}"

        with self._lock:
            ws, evicted = self._reserve_and_install_locked(
                ws_id, user_id=user_id, name=effective_name, parent_ws_id=parent_ws_id
            )

        if evicted is not None:
            self._adapter.cleanup_ui(evicted)
            if self._event_emitter is not None:
                self._event_emitter.emit_closed(evicted.id, reason="evicted", name=evicted.name)

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

        if not defer_emit_created:
            # Mark fired BEFORE the actual emit so a concurrent
            # ``discard`` can't observe ``False`` after the event
            # already fanned out. The flag is observational (powers
            # the discard warning); strict ordering relative to the
            # event isn't load-bearing for fan-out correctness.
            ws._emit_created_fired = True
            if self._event_emitter is not None:
                self._event_emitter.emit_created(ws)
        return ws

    def commit_create(self, ws: Workstream) -> None:
        """Fire the deferred ``emit_created`` event for ``ws``.

        Pairs with :meth:`create` called with
        ``defer_emit_created=True``. The caller is responsible for
        invoking ``commit_create`` exactly once per deferred ``create``,
        before any state-change events flow (so subscribers see the
        ``ws_created`` event before the first ``ws_state``). On the
        rollback branch the caller invokes :meth:`discard` instead.

        Idempotent against a missing emitter — when ``event_emitter`` is
        ``None`` (test fixtures, future kinds without an emitter wired)
        the lifecycle event is skipped but ``ws._emit_created_fired``
        is still set so a subsequent :meth:`discard` correctly
        identifies the workstream as committed (the warning path
        treats "committed" as a contract assertion, not as "actually
        broadcast somewhere").

        Caller-bug guard: under the manager lock, check that ``ws`` is
        still tracked by this manager and that ``_emit_created_fired``
        is not already set. Either failure logs a warning and returns
        without firing the event — duplicate calls and calls after
        :meth:`discard` become safe no-ops. Symmetric to
        :meth:`discard`'s warning when invoked on an already-
        advertised workstream; together the two methods make the
        deferred-create bracket robust against the obvious caller-
        bug shapes.
        """
        with self._lock:
            if ws._emit_created_fired:
                # Duplicate commit_create call. Could surface as a
                # double ``ws_created`` on the wire if we proceeded;
                # warn instead and bail.
                log.warning(
                    "session_mgr.commit_create.already_fired ws=%s",
                    ws.id[:8] if ws.id else "",
                )
                return
            tracked = self._workstreams.get(ws.id)
            if tracked is not ws:
                # Workstream was discarded (or never tracked, or
                # replaced by a same-id reuse — which would be a
                # different ws object). Firing emit_created for an
                # untracked ws_id leaks a phantom ``ws_created`` to
                # subscribers with no matching close. Warn + bail.
                log.warning(
                    "session_mgr.commit_create.untracked ws=%s",
                    ws.id[:8] if ws.id else "",
                )
                return
            # Both checks passed — flip the flag under the lock so a
            # racing discard sees True before our emit completes
            # outside the lock. (Manager lock is not held during the
            # emit itself: emit_created on coord acquires its own
            # locks for collector + children-registry updates and
            # holding the manager lock during fan-out would couple
            # the two unnecessarily.)
            ws._emit_created_fired = True
        if self._event_emitter is not None:
            self._event_emitter.emit_created(ws)

    def discard(self, ws_id: str) -> bool:
        """Release a workstream's in-memory slot WITHOUT firing ``emit_closed``.

        Use after :meth:`create` was called with
        ``defer_emit_created=True`` and a post-create check determined
        the workstream should not be advertised at all. Callers that
        also want to remove the persisted storage row should call
        ``turnstone.core.memory.delete_workstream(ws_id)`` separately
        — :meth:`discard` only owns the in-memory side, mirroring the
        split between ``mgr.create``'s slot reservation and
        ``self._storage.register_workstream``'s row write.

        Distinct from :meth:`close`:

        - ``close`` advertises the transition (``emit_closed``) and
          writes ``state='closed'`` to storage so the workstream is
          re-openable later.
        - ``discard`` does neither — the workstream's existence was
          never advertised (caller deferred ``emit_created``) so there
          is no transition to advertise, and the row should be
          deleted (not soft-closed) since the create is being
          unwound.

        Returns ``True`` when a workstream was removed, ``False`` if
        the id wasn't tracked. The method is safe to call from the
        HTTP handler's rollback path under
        ``contextlib.suppress(Exception)`` even if ``cleanup_ui``
        raises — the in-memory slot release runs first under the
        lock, so capacity is freed before any UI-cleanup error
        surfaces.

        Logs a ``warning`` when the workstream's
        ``_emit_created_fired`` flag is set — that means the
        workstream was already advertised to lifecycle subscribers
        (either created without ``defer_emit_created`` or committed
        via :meth:`commit_create`), and discarding now leaves a stale
        ``ws_created`` on the wire with no matching ``ws_closed``.
        Discard still completes (returns ``True``) so the slot is
        freed, but the warning surfaces the caller-bug for triage.
        Use :meth:`close` instead when the workstream's lifecycle
        was advertised and now needs to be retracted.
        """
        with self._lock:
            ws = self._workstreams.pop(ws_id, None)
            if ws is None:
                return False
            if ws_id in self._order:
                self._order.remove(ws_id)
            if self._active_id == ws_id:
                self._active_id = self._order[0] if self._order else None
        if ws._emit_created_fired:
            # Caller-bug path: the workstream was already advertised
            # via ``emit_created`` — a clean rollback would need
            # ``close`` (which fires ``emit_closed``) to retract the
            # advertisement, not ``discard``. Surface the misuse so
            # operators / future contributors can find the call site
            # via the log line; we still complete the in-memory
            # release so the slot is freed.
            log.warning(
                "session_mgr.discard.after_emit_created ws=%s",
                ws_id[:8] if ws_id else "",
            )
        # cleanup_ui runs OUTSIDE the manager lock to match
        # ``close``'s ordering — UI cleanup may join worker threads
        # or do other potentially-blocking work that must not hold
        # the slot-accounting mutex.
        self._adapter.cleanup_ui(ws)
        return True

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
                    if self._event_emitter is not None:
                        self._event_emitter.emit_closed(
                            evicted.id, reason="evicted", name=evicted.name
                        )

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
                        # Resume can leave the session in a partial state
                        # (``ChatSession.resume`` assigns ``self.messages``
                        # before the config-restore block, so a failure
                        # mid-restore loads the conversation but with
                        # default ``temperature`` / ``max_tokens`` /
                        # tool config). Treating this as success would
                        # silently 200 with broken state; the user's next
                        # send would run with default config instead of
                        # the persisted config. Roll the slot back so the
                        # caller surfaces a 5xx and the storage row stays
                        # available for a retry. Mirrors the
                        # build_session-failure unwind above.
                        self._adapter.cleanup_ui(ws)
                        with self._lock:
                            self._remove_locked(ws_id)
                        raise

                # No DB state-flip on resurrect. The in-memory session
                # is IDLE; the DB row may still say 'closed' from the
                # last close(). The next set_state() call syncs it
                # naturally; writing 'idle' here could race a concurrent
                # close() that writes 'closed' under self._lock.
                #
                # Bump only ``updated`` (no state write) so this row's
                # timestamp is fresh against the orphan-reaper cutoff —
                # otherwise a concurrent close_idle pass-2 in this same
                # process could clobber a freshly-rehydrated row whose
                # ``updated`` is older than the cutoff.  The pure-
                # timestamp write is safe against concurrent close()
                # because close still wins on the state column.
                try:
                    self._storage.touch_workstream(ws_id)
                except Exception:
                    log.debug(
                        "session_mgr.touch_workstream_failed ws=%s",
                        ws_id[:8],
                        exc_info=True,
                    )
                if self._event_emitter is not None:
                    self._event_emitter.emit_rehydrated(ws)
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
    # delete — hard-delete event broadcast (storage row is caller's job)
    # ------------------------------------------------------------------

    def delete(self, ws_id: str, *, name: str = "") -> bool:
        """Drop the in-memory slot if present + emit ``ws_closed`` with
        ``reason="deleted"`` so subscribers (cluster collector → coord
        adapter → child-tree UI) can drop the row.

        Storage row removal is the **caller's** responsibility — the
        delete HTTP endpoint already calls
        :func:`turnstone.core.memory.delete_workstream` before invoking
        this; the manager only handles the in-memory + event side so
        the lifecycle event lands on the same global queue every other
        terminal transition uses.

        Distinct from :meth:`close` (which writes ``state='closed'`` so
        the row is re-openable later) and :meth:`discard` (which fires
        no event because it's the rollback partner of an unwound
        ``defer_emit_created`` create).  Hard-delete advertises a
        terminal transition with ``reason="deleted"`` regardless of
        whether the workstream was loaded — a row that was closed
        (and therefore unloaded from memory) before being deleted
        still needs the broadcast so a long-lived dashboard tab
        drops the entry from its tree.

        Returns ``True`` when an in-memory slot was released, ``False``
        when the id wasn't tracked.  The event fires either way; the
        return value is informational for callers that care about
        capacity accounting.
        """
        with self._lock:
            ws = self._workstreams.pop(ws_id, None)
            if ws is not None:
                if ws_id in self._order:
                    self._order.remove(ws_id)
                if self._active_id == ws_id:
                    self._active_id = self._order[0] if self._order else None
        if ws is not None:
            # cleanup_ui outside the manager lock — mirrors the close()
            # ordering so any blocking UI teardown can't pin the
            # slot-accounting mutex.
            self._adapter.cleanup_ui(ws)
        if self._event_emitter is not None:
            # Fall back to the workstream's name when the caller didn't
            # snapshot one (the event payload's ``name`` field surfaces
            # in operator toasts on real-node closures; coord-side
            # ``child_ws_closed`` ignores it but the global queue
            # consumers don't all do so).
            event_name = name or (ws.name if ws is not None else "")
            self._event_emitter.emit_closed(ws_id, reason="deleted", name=event_name)
        return ws is not None

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
        # its storage write. ``state_writer.discard`` (when present)
        # drops any pending buffered transient AND waits for any
        # in-flight flush, so a late-flushing 'running' can't land in
        # storage AFTER our sync 'closed' write and resurrect the
        # closed row.
        with ws._lock:
            ws._closed = True
            if self._state_writer is not None:
                self._state_writer.discard(ws_id)
            try:
                self._storage.update_workstream_state(ws_id, "closed")
            except Exception:
                log.debug("session_mgr.state_update_failed ws=%s", ws_id[:8], exc_info=True)
            try:
                self._storage.delete_workstream_override(ws_id)
            except Exception:
                log.debug("session_mgr.override_delete_failed ws=%s", ws_id[:8], exc_info=True)
        if self._event_emitter is not None:
            self._event_emitter.emit_closed(ws_id, name=ws.name)
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
            # Terminal ERROR transitions flush sync — error-surfacing
            # paths (dashboard, audit) must observe the row durably
            # before any caller sees the state-change event. Non-
            # terminal transitions buffer through state_writer so we
            # don't hold ws._lock across a Postgres round-trip.
            if self._state_writer is not None:
                self._state_writer.record(
                    ws_id,
                    state.value,
                    flush_now=(state is WorkstreamState.ERROR),
                )
            else:
                try:
                    self._storage.update_workstream_state(ws_id, state.value)
                except Exception:
                    log.debug("session_mgr.state_update_failed ws=%s", ws_id[:8], exc_info=True)
        if self._event_emitter is not None:
            self._event_emitter.emit_state(ws, state)
        # Snapshot under the subscribers lock so concurrent
        # subscribe / unsubscribe can't shift the iterator's index
        # mid-dispatch (skipping or repeating callbacks). Iterate
        # the snapshot WITHOUT the lock so a slow callback doesn't
        # block subscribe / unsubscribe.
        with self._state_subscribers_lock:
            subscribers = list(self._state_subscribers)
        for callback in subscribers:
            with contextlib.suppress(Exception):
                callback(ws_id, state)

    # ------------------------------------------------------------------
    # State-change subscription
    # ------------------------------------------------------------------

    def subscribe_to_state(self, callback: Callable[[str, WorkstreamState], None]) -> None:
        """Register ``callback`` to fire on every workstream state change.

        Multiple subscribers are supported and fire in registration order.
        Each callback is wrapped in exception-suppression so a failing
        subscriber doesn't block the others. Use
        :meth:`unsubscribe_from_state` to remove.
        """
        with self._state_subscribers_lock:
            self._state_subscribers.append(callback)

    def unsubscribe_from_state(self, callback: Callable[[str, WorkstreamState], None]) -> None:
        """Remove a previously-registered state-change callback. No-op if absent."""
        with self._state_subscribers_lock, contextlib.suppress(ValueError):
            self._state_subscribers.remove(callback)

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

        Two-pass shape:

        - Pass 1 (in-memory): close loaded ``IDLE`` rows whose
          ``ws.last_active`` (monotonic) is past timeout.  Closes only
          ``IDLE`` so legitimately-attentive rows (waiting for user
          response) stay live.
        - Pass 2 (DB orphans): bulk-close DB rows of this manager's
          kind whose ``updated`` is past the wall-clock cutoff and
          which are not currently loaded.  This catches workstreams
          left behind by prior process incarnations — a process crash
          /restart leaves rows in non-terminal states forever
          otherwise.  Closes ``idle/thinking/attention/running``
          because any matching row is by definition not loaded by any
          live process and cannot be in a live interaction.

          **Liveness scoping** (the rendezvous router's primitive
          since PR #384): when ``self._service_type`` resolves to a
          known service type — both production kinds do — pass 2
          calls ``storage.list_services`` to enumerate peer processes
          with recent heartbeats and protects rows whose ``node_id``
          matches a live ``service_id`` from reap, even when *this*
          manager is on a different node.  This is essential for
          containerized deployments with dynamic hostnames: dead-pod
          rows fall out of the live set after the heartbeat window
          and become reapable; alive-pod rows stay protected as long
          as the owner heartbeats.  A future kind with no service
          registration would resolve ``_service_type`` to ``None``
          and skip the live-services lookup (single-process / CLI).

          **Conservative fallback**: if ``list_services`` raises,
          pass 2 is skipped entirely this tick — never reap when
          liveness state is unknown.  Pass 1 still runs.  Next tick
          retries the lookup.

        Returns the combined list of closed ws_ids (in-memory first,
        then DB orphans).  Pass 1 emits ``ws_closed``; pass 2 does
        not, because never-loaded rows have no SSE listeners
        expecting them.

        Atomic pop per victim under ``self._lock`` (bug-5): a pending
        tool result can flip state IDLE→RUNNING between the snapshot
        and the close, so the state test + pop must run together.
        Batches every pop under one ``self._lock`` acquisition (perf-5)
        rather than locking once per victim.  The DB pass runs OUTSIDE
        ``self._lock`` — only a brief lock to snapshot loaded keys —
        so a slow UPDATE doesn't block create/get/set_state.
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
            # the tombstone and skips its own write. ``state_writer.discard``
            # drops any pending buffered transient + waits for any
            # in-flight flush so the sync 'closed' write is the final
            # one for this ws_id.
            with ws._lock:
                ws._closed = True
                if self._state_writer is not None:
                    self._state_writer.discard(ws.id)
                try:
                    self._storage.update_workstream_state(ws.id, "closed")
                except Exception:
                    log.debug("session_mgr.state_update_failed ws=%s", ws.id[:8], exc_info=True)
                try:
                    self._storage.delete_workstream_override(ws.id)
                except Exception:
                    log.debug("session_mgr.override_delete_failed ws=%s", ws.id[:8], exc_info=True)
            if self._event_emitter is not None:
                self._event_emitter.emit_closed(ws.id, name=ws.name)
            closed_ids.append(ws.id)

        # Pass 2: reap DB orphans of this kind older than the cutoff.
        # Snapshot loaded keys under self._lock briefly so a concurrent
        # create/load doesn't get its row clobbered by the UPDATE; release
        # before the DB call.
        #
        # Liveness scoping uses ``services.last_heartbeat`` — the same
        # primitive the rendezvous router (PR #384) uses for routing.  A
        # row's ``node_id`` is stamped at create time and never updated;
        # in containerized deployments with dynamic hostnames the dead
        # pod's ``node_id`` points at a service that's no longer
        # heartbeating, so the row falls through to reap.  Conversely,
        # rows whose ``node_id`` matches a heartbeating service are
        # protected even when *this* manager is on a different node —
        # the alive peer may legitimately have them loaded.
        with self._lock:
            loaded = list(self._workstreams.keys())
        cutoff = (datetime.now(UTC) - timedelta(seconds=max_age_seconds)).strftime(
            "%Y-%m-%dT%H:%M:%S"
        )
        live_node_ids: list[str] | None = None
        skip_pass_2 = False
        if self._service_type is not None:
            try:
                live_services = self._storage.list_services(self._service_type)
                live_node_ids = [
                    str(svc["service_id"]) for svc in live_services if svc.get("service_id")
                ]
            except Exception:
                # Conservative fallback: skip pass 2 entirely this tick
                # so we can't accidentally reap rows whose owners we
                # failed to enumerate.  Next tick retries.
                log.debug(
                    "session_mgr.list_services_failed kind=%s",
                    self.kind.value,
                    exc_info=True,
                )
                skip_pass_2 = True
        orphans: list[str] = []
        if not skip_pass_2:
            try:
                orphans = self._storage.bulk_close_stale_orphans(
                    self.kind, cutoff, loaded, live_node_ids=live_node_ids
                )
            except Exception:
                log.debug(
                    "session_mgr.bulk_close_orphans_failed kind=%s",
                    self.kind.value,
                    exc_info=True,
                )
        if orphans:
            log.info(
                "session_mgr.bulk_close_orphans count=%d kind=%s",
                len(orphans),
                self.kind.value,
            )
        closed_ids.extend(orphans)
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
                if self._event_emitter is not None:
                    self._event_emitter.emit_closed(evicted.id, reason="evicted", name=evicted.name)
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
