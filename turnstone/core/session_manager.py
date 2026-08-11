"""Unified manager for workstream-shaped sessions.

Collapses ``WorkstreamManager`` (interactive) and ``CoordinatorManager``
(coordinator) into one class. Kind-specific transport and session
construction live on a ``SessionKindAdapter`` Protocol; the manager
itself owns the invariant mechanics — slot accounting, eviction,
persistence, per-ws lock refcount for concurrent lazy rehydrate.
"""

from __future__ import annotations

import contextlib
import functools
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol

from turnstone.core.adapters._ui_cleanup import _broadcast_ws_closed_to_listeners
from turnstone.core.log import get_logger
from turnstone.core.model_registry import ModelClientConstructionError, UnknownModelAliasError
from turnstone.core.personas import snapshot_from_config
from turnstone.core.workstream import (
    Workstream,
    WorkstreamKind,
    WorkstreamState,
    concrete_method,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from turnstone.core.child_event_bus import ChildEventBus
    from turnstone.core.session import ChatSession, SessionUI
    from turnstone.core.state_writer import StateWriter
    from turnstone.core.storage._protocol import StorageBackend

log = get_logger(__name__)


def _session_has_unresolved_persistence(session: Any) -> bool:
    """Read the concrete ChatSession hook without MagicMock auto-vivification.

    Blocking probe — callable only from contexts holding no workstream or
    manager lock (it acquires the session's generation and handoff locks).
    Retirement scans use :func:`_session_persistence_blocks_retirement`.
    """
    check = concrete_method(session, "has_unresolved_conversation_persistence")
    return bool(check()) if check is not None else False


def _session_persistence_blocks_retirement(session: Any) -> bool:
    """Non-blocking retirement gate: unresolved OR momentarily unprobeable.

    The idle-close and eviction scans call this while holding ``ws._lock``
    (and, for the eviction comprehension, the manager lock). The probe must
    therefore never block on the session's generation/handoff locks — that
    inverts the generation→workstream/manager order force-cancel's finalizer
    and deferred state publication hold, an AB/BA deadlock (round-4 review).
    ``None`` (locks busy) reads as True: a busy session is simply not
    retirable this sweep; the next sweep re-probes.
    """
    probe = concrete_method(session, "has_unresolved_conversation_persistence_nowait")
    if probe is None:
        # Compatibility/test doubles carry no real locks; the blocking read
        # is safe and preserves their scripted answers.
        return _session_has_unresolved_persistence(session)
    state = probe()
    return state is None or bool(state)


def _session_prepare_soft_close(session: Any) -> bool:
    """Run the concrete close fence; compatibility/test doubles have no hook."""
    prepare = concrete_method(session, "prepare_soft_close")
    if prepare is None:
        return not _session_has_unresolved_persistence(session)
    return bool(prepare())


def _session_reconcile_unresolved_persistence_if_due(session: Any, now: float) -> bool:
    """Call the concrete retry seam without MagicMock auto-vivification."""
    reconcile = concrete_method(session, "reconcile_unresolved_persistence_if_due")
    return bool(reconcile(now=now)) if reconcile is not None else False


def _session_conversation_persistence_fatal_revision(session: Any) -> int | None:
    """Return a concrete session's exact persistence-owned fatal revision."""
    read_revision = concrete_method(session, "conversation_persistence_fatal_revision")
    revision = read_revision() if read_revision is not None else None
    return revision if isinstance(revision, int) and not isinstance(revision, bool) else None


def _session_acknowledge_conversation_persistence_recovery(
    session: Any,
    revision: int,
) -> bool:
    """Retire only the exact fatal latch that a manager repair recovered."""
    acknowledge = concrete_method(session, "acknowledge_conversation_persistence_state_recovery")
    return bool(acknowledge(revision)) if acknowledge is not None else False


def _notify_persistence_state_changed(ui: Any) -> None:
    """Refresh a concrete UI projection after manager-owned ERROR recovery."""
    callback = concrete_method(ui, "on_persistence_state_changed")
    if callback is None:
        return
    try:
        callback()
    except Exception:
        log.debug("session_mgr.persistence_state_refresh_failed", exc_info=True)


class WorkstreamAlreadyExistsError(RuntimeError):
    """A create request did not acquire a fresh durable workstream id."""


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

# A create normally publishes in one request, but provider construction, a
# large fork clone, or attachment validation can legitimately take longer than
# a heartbeat window. Keep crash recovery independent from idle-session policy
# and deliberately conservative: long-lived hosts run this maintenance at most
# once per five minutes, the direct CLI runs one boot pass, and only
# reservations abandoned for two hours qualify.
STALE_CREATE_GRACE_SECONDS = 2 * 60 * 60
STALE_CREATE_SWEEP_INTERVAL_SECONDS = 5 * 60
PERSISTENCE_RECONCILE_INTERVAL_SECONDS = 1.0


class SessionKindAdapter(Protocol):
    """Per-kind construction + cleanup policies the shared ``SessionManager`` delegates to.

    The manager owns invariant mechanics. The adapter owns:

    - **Session construction**: what UI class wraps the workstream,
      what ``ChatSession`` factory signature applies.
    - **UI cleanup**: unblocking pending approval / foreground
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
    method — interactive's ``emit_state`` / ``emit_rehydrated`` are
    documented no-op stubs because those events fire from out-of-band
    channels (``WebUI._broadcast_state`` for state and the open handler for
    rehydrate). Interactive ``emit_created`` and ``emit_closed`` are real,
    bounded global-queue publications. Coordinator's four methods are all
    real (cluster collector's pseudo-node sees every transition). See
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

    _REHYDRATE_BIND_ATTEMPTS = 3
    _REHYDRATE_INCARNATION_ATTEMPTS = 3

    def __init__(
        self,
        adapter: SessionKindAdapter,
        *,
        storage: StorageBackend,
        max_active: int,
        node_id: str | None = None,
        state_writer: StateWriter | None = None,
        event_emitter: SessionEventEmitter | None = None,
        model_validator: Callable[[str], bool] | None = None,
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
        # Optional registry-membership check applied to the persisted
        # ``model_alias`` on the rehydrate path before threading it
        # into ``build_session``.  Production wiring passes
        # ``registry.has_alias``; an alias that has been removed from
        # the registry since the workstream was created is filtered
        # out so the session_factory falls back to its default rather
        # than raising.  Restricted to the rehydrate path — fresh
        # creates still want unknown aliases to surface as 503.
        self._model_validator = model_validator
        self._node_id = node_id
        self._workstreams: dict[str, Workstream] = {}
        # A hard-delete whose storage outcome is ambiguous may be the sole
        # owner of an accepted conversation repair journal. Keep that exact
        # terminal object off every user/capacity surface until maintenance or
        # an explicit delete retry proves it safe to retire.
        self._failed_delete_tombstones: dict[str, Workstream] = {}
        self._failed_delete_unadvertised: set[str] = set()
        # Deferred creates are addressable internally before their pre-commit
        # transaction finishes. Keep the exact reservation object beyond a
        # racing close/delete so the rollback cannot ABA-delete a successor
        # that reuses the caller-chosen id.
        self._pending_creates: dict[str, Workstream] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()
        # State storage + observer tails use a per-id lane that survives a
        # close/reopen overlap.  The lane never owns lifecycle state: callers
        # retain it briefly under ``_lock``, then run storage and callbacks
        # with only the lane held.  Entries disappear once no live workstream
        # and no running tail references them, so the map is bounded by live
        # and actively-unwinding workstreams.
        self._state_tail_locks: dict[str, threading.Lock] = {}
        self._state_tail_users: dict[str, int] = {}
        self._state_incarnation = 0
        # IDs removed for capacity remain unavailable until their terminal
        # cleanup/event tail completes. This closes the pop→same-id-open ABA
        # without holding the global manager lock over callbacks.
        self._retiring_ids: set[str] = set()
        # Per-ws_id refcounted locks serializing concurrent lazy
        # rehydrate of the same ws_id. Ported from
        # ``CoordinatorManager._open_locks``: without refcounting, a
        # third arrival could allocate a fresh lock for the same ws_id
        # and defeat serialization on the failure path.
        self._open_locks: dict[str, tuple[threading.RLock, int]] = {}
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
    def child_event_bus(self) -> ChildEventBus | None:
        """Delegate to the adapter's per-workstream wakeup bus.

        Returns ``None`` for adapters that don't host one (today only the
        coord adapter does; interactive's child surface is degenerate
        and has nothing to wait on yet). Manager-level property gives
        adapter-agnostic callers (tests, future cross-kind tools) a
        stable lookup that doesn't depend on knowing which adapter is
        attached.
        """
        return getattr(self._adapter, "child_event_bus", None)

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
            ws = self._workstreams.get(self._active_id)
            if ws is not None and self._pending_creates.get(ws.id) is ws:
                return None
            return ws

    def switch(self, ws_id: str) -> Workstream | None:
        with self._lock:
            if ws_id in self._workstreams and ws_id not in self._pending_creates:
                self._active_id = ws_id
                return self._workstreams[ws_id]
        return None

    def switch_by_index(self, index: int) -> Workstream | None:
        """1-based index into the creation-order list."""
        with self._lock:
            visible = [
                ws_id
                for ws_id in self._order
                if self._pending_creates.get(ws_id) is not self._workstreams.get(ws_id)
            ]
            if 1 <= index <= len(visible):
                ws_id = visible[index - 1]
                self._active_id = ws_id
                return self._workstreams.get(ws_id)
        return None

    def index_of(self, ws_id: str) -> int:
        """1-based creation-order index of a workstream, or 0 if absent."""
        with self._lock:
            visible = [
                wid
                for wid in self._order
                if self._pending_creates.get(wid) is not self._workstreams.get(wid)
            ]
            try:
                return visible.index(ws_id) + 1
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
        project_id: str | None = None,
        persona: str = "",
        defer_emit_created: bool = False,
        _fork_reservation: bool = False,
        **extra_session_kwargs: Any,
    ) -> Workstream:
        """Create one workstream with an exact durable incarnation fence."""
        return self._create_serialized(
            user_id=user_id,
            name=name,
            skill=skill,
            skill_id=skill_id,
            skill_version=skill_version,
            ws_id=ws_id,
            model=model,
            client_type=client_type,
            parent_ws_id=parent_ws_id,
            project_id=project_id,
            persona=persona,
            defer_emit_created=defer_emit_created,
            _fork_reservation=_fork_reservation,
            **extra_session_kwargs,
        )

    def _create_serialized(
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
        project_id: str | None = None,
        persona: str = "",
        defer_emit_created: bool = False,
        _fork_reservation: bool = False,
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

        ``defer_emit_created``: when ``True``, the workstream is reserved and
        fully constructed but hidden from ordinary lookup/list/open surfaces;
        the ``emit_created`` call is skipped. The caller takes ownership of
        advertising it — typically by calling :meth:`commit_create` after
        running additional
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
        requested_ws_id = ws_id
        ws_id = ws_id or uuid.uuid4().hex
        effective_name = name or f"ws-{ws_id[:4]}"
        # Every deferred create needs a durable incarnation fence: rollback,
        # close and a storage clone must never delete or mutate a same-id row
        # that another process registered after this reservation was retired.
        # Every manager-created row gets a durable incarnation token. Deferred
        # HTTP creates also remain in state=creating until commit publishes
        # them, so cross-node open/delete cannot observe a half-built session.
        fork_reservation_token = uuid.uuid4().hex
        create_lane = self._acquire_open_lock(ws_id)
        create_lane.acquire()
        create_lane_released = False

        def _release_create_lane() -> None:
            nonlocal create_lane_released
            if create_lane_released:
                return
            create_lane_released = True
            create_lane.release()
            self._release_open_lock(ws_id)

        # Avoid allocating a UI or evicting an idle workstream for the common
        # caller-chosen collision case. The insert result below is still the
        # authoritative race-free reservation.
        try:
            if requested_ws_id and self._storage.get_workstream(ws_id) is not None:
                raise WorkstreamAlreadyExistsError(f"workstream {ws_id!r} already exists")

            ws, _evicted = self._reserve_and_install(
                ws_id,
                user_id=user_id,
                name=effective_name,
                parent_ws_id=parent_ws_id,
                project_id=project_id,
                persona=persona,
                pending=True,
                reservation_token=fork_reservation_token,
            )
        except BaseException:
            _release_create_lane()
            raise

        # Persist before session construction. Fail-closed: if the row
        # can't be written, the in-memory session would be invisible to
        # any lazy-rehydrate path and show up as "missing" after
        # restart — surface the storage failure now.
        try:
            inserted = self._storage.register_workstream(
                ws_id,
                node_id=self._node_id,
                user_id=user_id,
                name=ws.name,
                state="creating",
                kind=self.kind,
                parent_ws_id=parent_ws_id,
                project_id=project_id,
                persona=persona,
                skill_id=skill_id,
                skill_version=skill_version,
                fork_reservation_token=fork_reservation_token,
            )
            if inserted is False:
                raise WorkstreamAlreadyExistsError(f"workstream {ws_id!r} already exists")
        except BaseException:
            with self._lock:
                self._remove_locked(ws_id)
                if self._pending_creates.get(ws_id) is ws:
                    self._pending_creates.pop(ws_id, None)
            try:
                self._adapter.cleanup_ui(ws)
            except Exception:
                log.warning(
                    "session_mgr.create.register_failure_cleanup_failed ws=%s",
                    ws_id[:8],
                    exc_info=True,
                )
            _release_create_lane()
            raise

        _release_create_lane()

        built_session: Any | None = None
        try:
            session_kwargs = dict(extra_session_kwargs)
            session_kwargs["fork_reservation_token"] = fork_reservation_token
            built_session = self._adapter.build_session(
                ws,
                skill=skill,
                model=model,
                client_type=client_type,
                **session_kwargs,
            )
            built_session._fork_reservation_token = fork_reservation_token

            # Construction may block on provider/config/storage work. A
            # terminal caller is allowed to retire the pending placeholder in
            # that window, but the completed candidate must then be closed —
            # never attach a live session to an object no longer owned by the
            # manager registry.
            with ws._lifecycle_lock, self._lock:
                owned = (
                    self._workstreams.get(ws_id) is ws and self._pending_creates.get(ws_id) is ws
                )
                if owned:
                    ws.session = built_session
            if not owned:
                self._retire_built_session(built_session, ws_id)
                built_session = None
                raise RuntimeError(f"workstream {ws_id!r} was retired during construction")
        except Exception:
            # Release the slot so capacity isn't leaked, and call
            # cleanup_ui on the placeholder so any listener/lock state
            # the UI factory allocated is released. Storage row stays:
            # the next open() on this ws_id retries construction. A pending
            # fork is the exception: its HTTP rollback bracket was never
            # entered, so remove exactly that durable reservation here.
            with ws._lifecycle_lock:
                with self._lock:
                    owned = (
                        self._workstreams.get(ws_id) is ws
                        and self._pending_creates.get(ws_id) is ws
                    )
                    if owned:
                        self._remove_locked(ws_id)
                        self._pending_creates.pop(ws_id, None)
                if owned:
                    try:
                        self._adapter.cleanup_ui(ws)
                    except Exception:
                        log.warning(
                            "session_mgr.create.session_failure_cleanup_failed ws=%s",
                            ws_id[:8],
                            exc_info=True,
                        )
            if built_session is not None and ws.session is not built_session:
                self._retire_built_session(built_session, ws_id)
            if owned and fork_reservation_token:
                try:
                    self._storage.delete_workstream_if_fork_reserved(
                        ws_id,
                        fork_reservation_token,
                    )
                except Exception:
                    log.warning(
                        "session_mgr.failed_fork_create_cleanup ws=%s",
                        ws_id[:8],
                        exc_info=True,
                    )
            raise

        if not defer_emit_created:
            try:
                committed = self.commit_create(ws)
            except BaseException:
                self._rollback_direct_create(ws)
                raise
            if not committed:
                self._rollback_direct_create(ws)
                raise RuntimeError(f"workstream {ws_id!r} was retired during creation")
        return ws

    @staticmethod
    def _retire_built_session(candidate: Any, ws_id: str) -> None:
        """Best-effort retirement for a candidate that lost create ownership."""
        if hasattr(candidate, "cancel"):
            try:
                candidate.cancel()
            except Exception:
                log.debug(
                    "session_mgr.create_candidate_cancel_failed ws=%s",
                    ws_id[:8],
                    exc_info=True,
                )
        if hasattr(candidate, "close"):
            try:
                candidate.close()
            except Exception:
                log.debug(
                    "session_mgr.create_candidate_close_failed ws=%s",
                    ws_id[:8],
                    exc_info=True,
                )

    def _rollback_direct_create(self, ws: Workstream) -> None:
        """Best-effort exact rollback when immediate publication fails."""

        def _delete_reserved() -> None:
            try:
                self._storage.delete_workstream_if_fork_reserved(
                    ws.id,
                    ws._fork_reservation_token,
                )
            except Exception:
                log.warning(
                    "session_mgr.direct_create_rollback_failed ws=%s",
                    ws.id[:8],
                    exc_info=True,
                )

        self.discard(
            ws.id,
            expected=ws,
            after_release=_delete_reserved,
        )

    def commit_create(self, ws: Workstream) -> bool:
        """Publish a deferred create on its stable per-id lifecycle lane."""
        with self._id_lifecycle(ws.id):
            return self._commit_create_serialized(ws)

    def _commit_create_serialized(self, ws: Workstream) -> bool:
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
        still the exact tracked pending reservation and that ``_emit_created_fired``
        is not already set. Either failure logs a warning and returns
        without firing the event — duplicate calls and calls after
        :meth:`discard` become safe no-ops. Symmetric to
        :meth:`discard`'s warning when invoked on an already-
        advertised workstream; together the two methods make the
        deferred-create bracket robust against the obvious caller-bug shapes.
        The bounded emitter runs in that same critical section so close/delete
        can never publish a terminal event ahead of lifecycle birth.
        """
        # Per-object lifecycle serialization keeps the global manager lock out
        # of adapter/listener callbacks. L→M is the sole acquisition order:
        # terminal paths snapshot under M, release it, take L, then revalidate
        # under M. A same-thread terminal callback sees the active flag and
        # refuses instead of recursively retiring a half-published object.
        with ws._lifecycle_lock:
            with self._lock:
                if ws._emit_created_fired:
                    log.warning(
                        "session_mgr.commit_create.already_fired ws=%s",
                        ws.id[:8] if ws.id else "",
                    )
                    return False
                tracked = self._workstreams.get(ws.id)
                pending = self._pending_creates.get(ws.id)
                if tracked is not ws or pending is not ws:
                    log.warning(
                        "session_mgr.commit_create.untracked ws=%s",
                        ws.id[:8] if ws.id else "",
                    )
                    return False
                ws._create_publication_active = True
                ws._create_publication_thread = threading.get_ident()

            try:
                published = self._storage.publish_deferred_create(
                    ws.id,
                    ws._fork_reservation_token,
                )
            except BaseException:
                with self._lock:
                    ws._create_publication_active = False
                    ws._create_publication_thread = None
                raise
            if not published:
                with self._lock:
                    ws._create_publication_active = False
                    ws._create_publication_thread = None
                log.warning(
                    "session_mgr.commit_create.reservation_lost ws=%s",
                    ws.id[:8] if ws.id else "",
                )
                return False

            with self._lock:
                # Lifecycle serialization prevents a conforming terminal path
                # from changing ownership between the durable CAS and this
                # bounded publication phase.
                if (
                    self._workstreams.get(ws.id) is not ws
                    or self._pending_creates.get(ws.id) is not ws
                ):
                    ws._create_publication_active = False
                    ws._create_publication_thread = None
                    return False
                ws._emit_created_fired = True
            try:
                if self._event_emitter is not None:
                    self._event_emitter.emit_created(ws)
            except BaseException:
                # Publication has already crossed the durable creating→idle
                # CAS, so rolling the local object back would expose an idle
                # durable row with no owning session. Production emitters are
                # bounded and exception-isolated; isolate custom emitters here
                # as well and complete the lifecycle transition.
                log.warning(
                    "session_mgr.commit_create.emit_failed ws=%s",
                    ws.id[:8] if ws.id else "",
                    exc_info=True,
                )
                with self._lock:
                    ws._create_publication_active = False
                    ws._create_publication_thread = None

            with self._lock:
                ws._create_publication_active = False
                ws._create_publication_thread = None
                if self._pending_creates.get(ws.id) is not ws:
                    # No conforming terminal path can remove the reservation
                    # while L is held. Treat a custom same-thread mutation as
                    # a failed commit rather than exposing a phantom object.
                    ws._emit_created_fired = False
                    return False
                self._pending_creates.pop(ws.id, None)
                if self._active_id is None:
                    self._active_id = ws.id
            return True

    def discard(
        self,
        ws_id: str,
        *,
        expected: Workstream | None = None,
        before_release: Callable[[], None] | None = None,
        after_release: Callable[[], None] | None = None,
    ) -> bool:
        """Discard one pending incarnation on its per-id lifecycle lane."""
        with self._id_lifecycle(ws_id):
            return self._discard_serialized(
                ws_id,
                expected=expected,
                before_release=before_release,
                after_release=after_release,
            )

    def _discard_serialized(
        self,
        ws_id: str,
        *,
        expected: Workstream | None = None,
        before_release: Callable[[], None] | None = None,
        after_release: Callable[[], None] | None = None,
    ) -> bool:
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
        ``_emit_created_fired`` flag is set and an event emitter exists — that
        means the workstream was already advertised to lifecycle subscribers
        (either created without ``defer_emit_created`` or committed
        via :meth:`commit_create`), and discarding now leaves a stale
        ``ws_created`` on the wire with no matching ``ws_closed``.
        Discard still completes (returns ``True``) so the slot is
        freed, but the warning surfaces the caller-bug for triage.
        Use :meth:`close` instead when the workstream's lifecycle
        was advertised and now needs to be retracted.
        """
        with self._lock:
            tracked = self._workstreams.get(ws_id)
            pending = self._pending_creates.get(ws_id)
            if expected is not None and (tracked is not expected or pending is not expected):
                return False
            candidate = expected or tracked or pending
            if candidate is None:
                return False
            if (
                candidate._create_publication_active
                and candidate._create_publication_thread == threading.get_ident()
            ):
                return False

        # L→M is the only nested lifecycle order. A concurrent commit holds L
        # through birth publication; after it releases, the exact-pending check
        # below makes an HTTP rollback a harmless no-op.
        with candidate._lifecycle_lock:
            with self._lock:
                tracked = self._workstreams.get(ws_id)
                pending = self._pending_creates.get(ws_id)
                if expected is not None and (tracked is not expected or pending is not expected):
                    return False
                ws = tracked if tracked is candidate else None
                owned_pending = pending is candidate
                if ws is None and not owned_pending:
                    return False

            # Pending-upload cleanup must happen while this incarnation still
            # owns the id. Releasing the manager slot first would let a same-id
            # successor stage uploads that this rollback could erase.
            if before_release is not None:
                before_release()

            with self._lock:
                if self._workstreams.get(ws_id) is not candidate:
                    return False
                if expected is not None and self._pending_creates.get(ws_id) is not expected:
                    return False
                self._workstreams.pop(ws_id, None)
                if self._pending_creates.get(ws_id) is candidate:
                    self._pending_creates.pop(ws_id, None)
                if ws_id in self._order:
                    self._order.remove(ws_id)
                if self._active_id == ws_id:
                    self._active_id = self._first_visible_id_locked()
                self._prune_state_tail_locked(
                    ws_id,
                    expected_lock=candidate._state_tail_lock,
                )
            if candidate._emit_created_fired and self._event_emitter is not None:
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
        if candidate is not None:
            try:
                self._adapter.cleanup_ui(candidate)
            except Exception:
                # Rollback ownership has already been released. Cleanup is
                # best-effort and must not hide the successful removal from
                # the caller, which still owns deleting the durable row.
                log.warning(
                    "session_mgr.discard.cleanup_failed ws=%s",
                    ws_id[:8] if ws_id else "",
                    exc_info=True,
                )
        if after_release is not None:
            after_release()
        return True

    # ------------------------------------------------------------------
    # open — lazy rehydrate for a persisted workstream
    # ------------------------------------------------------------------

    def open(
        self,
        ws_id: str,
        *,
        _incarnation_attempt: int = 0,
    ) -> Workstream | None:
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
                    if ws_id in self._retiring_ids or ws_id in self._failed_delete_tombstones:
                        return None
                    existing = self._workstreams.get(ws_id)
                    if existing is not None and self._pending_creates.get(ws_id) is existing:
                        return None
                    if existing is not None and existing.session is not None:
                        return existing

                # Bind every rehydrated object to the durable incarnation it
                # represents. Legacy tokenless rows are assigned a private
                # token atomically with this snapshot, so a later exact delete
                # can reject a stale endpoint snapshot before mutating a
                # same-id local successor.
                incarnation_snapshot = getattr(
                    self._storage,
                    "ensure_workstream_incarnation_snapshot",
                    None,
                )
                if callable(incarnation_snapshot):
                    row = incarnation_snapshot(ws_id)
                else:
                    incarnation_snapshot = None
                    row = self._storage.get_workstream(ws_id)
                if row is None or row.get("kind") != self.kind:
                    return None
                # ``deleted`` is a tombstone — never resurrect.
                # ``closed`` IS resurrectable; the Saved Workstreams
                # landing makes restore an explicit user action, and
                # ``_reserve_and_install`` still enforces
                # max_active (evicting an idle peer or raising).
                if row.get("state") in {"creating", "deleted"}:
                    return None

                # Re-check + capacity admission happen inside the helper. The
                # per-id open lane prevents another opener for this id, while
                # the helper serializes any victim incarnation exactly.
                reservation_token = str(row.get("fork_reservation_token") or "")
                if incarnation_snapshot is not None and not reservation_token:
                    raise RuntimeError(f"workstream {ws_id!r} incarnation snapshot has no token")
                ws, _evicted = self._reserve_and_install(
                    ws_id,
                    user_id=row.get("user_id") or "",
                    name=row.get("name") or f"ws-{ws_id[:4]}",
                    parent_ws_id=row.get("parent_ws_id"),
                    project_id=row.get("project_id"),
                    persona=row.get("persona") or "",
                    reservation_token=reservation_token,
                )

                # Thread the persisted ``model_alias`` into
                # ``build_session`` so reopened workstreams keep the
                # model they were created with.  Pairs with the
                # ``ChatSession.__init__`` skip-save guard: without
                # both halves, ``_save_config`` clobbers persisted
                # config with constructor defaults before
                # ``ChatSession.resume`` reads them back.  When
                # ``model_validator`` is wired and the saved alias is
                # no longer in the registry, drop it so the factory
                # falls back to its default — the session_factory
                # itself still raises on unknown aliases, since
                # fresh-create paths want that to surface as a 503.
                saved_cfg = self._storage.load_workstream_config(ws_id)
                saved_alias = (saved_cfg.get("model_alias") or None) if saved_cfg else None
                if (
                    saved_alias
                    and self._model_validator is not None
                    and not self._model_validator(saved_alias)
                ):
                    log.warning(
                        "session_mgr.stale_alias_dropped ws=%s alias=%s",
                        ws_id[:8],
                        saved_alias,
                    )
                    saved_alias = None

                try:
                    # Persona snapshot rides the same pre-construction lane as
                    # the saved alias: the constructor applies the four levers
                    # (tool merge, MCP gate, composition) inside __init__, so
                    # the stamp must land as a kwarg — resume() is too late.
                    # A corrupt/partial stamp raises here (loud construction
                    # error), never silently reverting to a default envelope.
                    # No stamp = legacy pre-persona workstream: the kwarg is
                    # omitted entirely so factories that predate it keep
                    # working.  Inside the unwind bracket: a parse raise must
                    # release the placeholder slot exactly like a
                    # build_session failure, or the ws_id stays tracked
                    # forever (pinning a max_active slot and turning every
                    # later open() into the already-tracked RuntimeError).
                    persona_snapshot = snapshot_from_config(saved_cfg or {})
                    extra_build_kwargs: dict[str, Any] = {}
                    if persona_snapshot is not None:
                        extra_build_kwargs["persona_snapshot"] = persona_snapshot

                    requested_alias = saved_alias
                    for bind_attempt in range(self._REHYDRATE_BIND_ATTEMPTS):
                        try:
                            ws.session = self._adapter.build_session(
                                ws,
                                model=requested_alias,
                                **extra_build_kwargs,
                            )
                        except ModelClientConstructionError:
                            # The alias still exists but its client/provider is
                            # broken.  Never reinterpret that operator-visible
                            # construction cause as an alias-removal fallback.
                            raise
                        except UnknownModelAliasError as exc:
                            # ModelRegistry's alias miss carries the concrete alias.
                            # For a saved alias, require that exact alias.  For
                            # ``model=None``, extract the concrete default the
                            # factory raced on.  In both cases a fresh validator
                            # miss is required before retrying; unrelated and
                            # indeterminate failures remain visible.
                            raced_alias = self._raced_unknown_alias(exc, requested_alias)
                            if (
                                raced_alias is None
                                or not self._model_alias_disappeared(raced_alias)
                                or bind_attempt + 1 >= self._REHYDRATE_BIND_ATTEMPTS
                            ):
                                raise
                            self._log_model_alias_race(ws_id, raced_alias, "build")
                            requested_alias = None
                            continue

                        # Validate the alias the factory ACTUALLY bound, not the
                        # nullable persisted request.  A default build resolves
                        # ``model=None`` to a concrete alias before construction;
                        # a reload can retire that client in either side of
                        # resume just like it can for an explicit saved alias.
                        candidate_alias = self._rehydrate_candidate_alias(
                            ws,
                            requested_alias,
                        )
                        if self._model_alias_disappeared(candidate_alias):
                            self._log_model_alias_race(
                                ws_id,
                                candidate_alias,
                                "before_resume",
                            )
                            self._retire_rehydrate_candidate(ws)
                            if bind_attempt + 1 >= self._REHYDRATE_BIND_ATTEMPTS:
                                raise RuntimeError(
                                    "model registry changed repeatedly while reopening "
                                    f"workstream {ws_id!r}"
                                )
                            requested_alias = None
                            continue

                        if ws.session is not None and hasattr(ws.session, "resume"):
                            ws.session.resume(ws_id)

                        # ``resume`` may adopt a persisted alias that differs
                        # from the factory candidate (notably when an alias
                        # reappears between default construction and resume).
                        # Validate the lane that will actually be returned.
                        resumed_alias = self._rehydrate_candidate_alias(
                            ws,
                            candidate_alias,
                        )
                        if self._model_alias_disappeared(resumed_alias):
                            self._log_model_alias_race(
                                ws_id,
                                resumed_alias,
                                "during_resume",
                            )
                            self._retire_rehydrate_candidate(ws)
                            if bind_attempt + 1 >= self._REHYDRATE_BIND_ATTEMPTS:
                                raise RuntimeError(
                                    "model registry changed repeatedly while reopening "
                                    f"workstream {ws_id!r}"
                                )
                            requested_alias = None
                            continue
                        break
                except Exception:
                    # Build/resume failures leave no usable session. Resume
                    # can also have partially loaded history/config, so roll
                    # back the reserved slot and run the adapter's full UI
                    # cleanup.  The raced-candidate replacement above is the
                    # only path that intentionally avoids cleanup_ui.
                    self._retire_rehydrate_slot(ws)
                    raise

                # Construction and resume perform multiple by-id storage
                # reads outside the snapshot transaction. A remote
                # delete/re-register in that window can otherwise produce a
                # hybrid object (A's metadata/token with B's config/history).
                # Re-read the private incarnation witness before this object
                # is touched, advertised, or returned. An openable successor
                # gets a bounded retry from its own fresh snapshot; a deleted
                # or provisional row simply remains unavailable.
                try:
                    current_row = (
                        incarnation_snapshot(ws_id)
                        if incarnation_snapshot is not None
                        else self._storage.get_workstream(ws_id)
                    )
                except BaseException:
                    self._retire_rehydrate_slot(ws)
                    raise
                current_openable = (
                    current_row is not None
                    and current_row.get("kind") == self.kind
                    and current_row.get("state") not in {"creating", "deleted"}
                )
                current_token = (
                    str(current_row.get("fork_reservation_token") or "")
                    if current_row is not None
                    else ""
                )
                if not current_openable or current_token != reservation_token:
                    self._retire_rehydrate_slot(ws)
                    if not current_openable:
                        return None
                    if _incarnation_attempt + 1 >= self._REHYDRATE_INCARNATION_ATTEMPTS:
                        raise RuntimeError(
                            f"workstream incarnation changed repeatedly while reopening {ws_id!r}"
                        )
                    log.warning(
                        "session_mgr.rehydrate_incarnation_raced ws=%s attempt=%d",
                        ws_id[:8],
                        _incarnation_attempt + 1,
                    )
                    return self.open(
                        ws_id,
                        _incarnation_attempt=_incarnation_attempt + 1,
                    )

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

    def _model_alias_disappeared(self, alias: str | None) -> bool:
        """Whether a fresh validator read proves *alias* is now absent.

        Validator failure is not proof of removal.  Preserve the original
        construction/resume outcome in that case rather than converting an
        infrastructure error into a default-model retry.
        """
        validator = self._model_validator
        if not alias or validator is None:
            return False
        try:
            return not validator(alias)
        except Exception:
            log.debug(
                "session_mgr.saved_alias_recheck_failed alias=%s",
                alias,
                exc_info=True,
            )
            return False

    @staticmethod
    def _raced_unknown_alias(
        exc: UnknownModelAliasError,
        requested_alias: str | None,
    ) -> str | None:
        """Return the registry alias when it matches the attempted binding."""
        missing_alias = exc.alias
        if requested_alias is not None and missing_alias != requested_alias:
            return None
        return missing_alias

    @staticmethod
    def _rehydrate_candidate_alias(ws: Workstream, requested_alias: str | None) -> str | None:
        """Concrete alias bound by a candidate, with a legacy-adapter fallback."""
        candidate = ws.session
        if candidate is None:
            return requested_alias
        actual_alias = getattr(candidate, "model_alias", None)
        return actual_alias if isinstance(actual_alias, str) and actual_alias else requested_alias

    @staticmethod
    def _log_model_alias_race(ws_id: str, alias: str | None, phase: str) -> None:
        log.warning(
            "session_mgr.stale_alias_raced ws=%s alias=%s phase=%s",
            ws_id[:8],
            alias,
            phase,
        )

    @staticmethod
    def _retire_rehydrate_candidate(ws: Workstream) -> None:
        """Cancel/close a stale candidate without closing its Workstream or UI."""
        candidate = ws.session
        ws.session = None
        if candidate is None:
            return
        if hasattr(candidate, "cancel"):
            try:
                candidate.cancel()
            except Exception:
                log.debug(
                    "session_mgr.rehydrate_candidate_cancel_failed ws=%s",
                    ws.id[:8],
                    exc_info=True,
                )
        if hasattr(candidate, "close"):
            try:
                candidate.close()
            except Exception:
                # The coherent default still has to be constructed.  This is a
                # best-effort resource retirement, not permission to broadcast
                # a workstream close or abandon the reserved slot.
                log.debug(
                    "session_mgr.rehydrate_candidate_close_failed ws=%s",
                    ws.id[:8],
                    exc_info=True,
                )

    def _retire_rehydrate_slot(self, ws: Workstream) -> None:
        """Cleanup and remove one exact failed rehydrate placeholder."""
        try:
            self._adapter.cleanup_ui(ws)
        finally:
            with self._lock:
                if self._workstreams.get(ws.id) is ws:
                    self._remove_locked(ws.id)

    @contextlib.contextmanager
    def _id_lifecycle(self, ws_id: str) -> Iterator[None]:
        """Serialize all local incarnations of one logical workstream id."""
        lifecycle_lock = self._acquire_open_lock(ws_id)
        try:
            with lifecycle_lock:
                yield
        finally:
            self._release_open_lock(ws_id)

    def _acquire_open_lock(self, ws_id: str) -> threading.RLock:
        with self._lock:
            entry = self._open_locks.get(ws_id)
            if entry is None:
                lk = threading.RLock()
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
        """Retire live state on the stable per-id lifecycle lane."""
        with self._id_lifecycle(ws_id):
            return self._delete_serialized(ws_id, name=name)

    def _delete_serialized(self, ws_id: str, *, name: str = "") -> bool:
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
            candidate = self._workstreams.get(ws_id) or self._pending_creates.get(ws_id)
            if candidate is not None and (
                candidate._create_publication_active
                and candidate._create_publication_thread == threading.get_ident()
            ):
                return False

        if candidate is None:
            if self._event_emitter is not None:
                self._event_emitter.emit_closed(ws_id, reason="deleted", name=name)
            return False

        with candidate._lifecycle_lock:
            with self._lock:
                if self._workstreams.get(ws_id) is not candidate:
                    return False
                was_unadvertised = self._pending_creates.get(ws_id) is candidate
                candidate._lifecycle_terminal_active = True
                self._retain_state_tail_locked(candidate)

            with candidate._lock:
                candidate._closed = True
                candidate._state_revision += 1

            try:
                with candidate._state_tail_lock:
                    if self._state_writer is not None:
                        self._state_writer.discard(
                            ws_id,
                            tombstone=True,
                            incarnation=candidate._state_incarnation,
                        )

                with self._lock:
                    if self._workstreams.get(ws_id) is not candidate:
                        candidate._lifecycle_terminal_active = False
                        return False
                    self._workstreams.pop(ws_id, None)
                    if was_unadvertised:
                        self._pending_creates.pop(ws_id, None)
                    if ws_id in self._order:
                        self._order.remove(ws_id)
                    if self._active_id == ws_id:
                        self._active_id = self._first_visible_id_locked()

                # cleanup_ui outside the manager lock — mirrors close().
                try:
                    self._adapter.cleanup_ui(candidate)
                except Exception:
                    log.warning(
                        "session_mgr.delete.cleanup_failed ws=%s",
                        ws_id[:8],
                        exc_info=True,
                    )
                if self._event_emitter is not None and not was_unadvertised:
                    event_name = name or candidate.name
                    self._event_emitter.emit_closed(
                        ws_id,
                        reason="deleted",
                        name=event_name,
                    )
                return True
            finally:
                self._release_state_tail(candidate)

    def delete_persisted(
        self,
        ws_id: str,
        *,
        delete_fn: Callable[[], bool],
        name: str = "",
        expected_reservation_token: str = "",
    ) -> bool:
        """Hard-delete one incarnation on the stable per-id lifecycle lane."""
        with self._id_lifecycle(ws_id):
            return self._delete_persisted_serialized(
                ws_id,
                delete_fn=delete_fn,
                name=name,
                expected_reservation_token=expected_reservation_token,
            )

    def _delete_persisted_serialized(
        self,
        ws_id: str,
        *,
        delete_fn: Callable[[], bool],
        name: str = "",
        expected_reservation_token: str = "",
    ) -> bool:
        """Delete durable + live state under one lifecycle admission.

        The HTTP hard-delete path previously deleted the row and only then
        retired the manager object. A deferred create could publish in that
        gap and return success for a row that no longer existed. For a loaded
        incarnation, hold its lifecycle lock across the storage delete and
        exact-object retirement. Pending creates use their durable reservation
        token, so a delete/re-register ABA cannot erase the replacement row.
        """
        with self._lock:
            candidate = (
                self._workstreams.get(ws_id)
                or self._pending_creates.get(ws_id)
                or self._failed_delete_tombstones.get(ws_id)
            )
            if candidate is not None and (
                candidate._create_publication_active
                and candidate._create_publication_thread == threading.get_ident()
            ):
                return False

        if candidate is None:
            deleted = delete_fn()
            if deleted and self._event_emitter is not None:
                self._event_emitter.emit_closed(ws_id, reason="deleted", name=name)
            return deleted

        with candidate._lifecycle_lock:
            with self._lock:
                if (
                    self._workstreams.get(ws_id) is not candidate
                    and self._failed_delete_tombstones.get(ws_id) is not candidate
                ):
                    return False
                token_direction_needed = bool(
                    expected_reservation_token
                    and candidate._fork_reservation_token != expected_reservation_token
                )

            # The endpoint's authorized durable snapshot may have gone stale
            # before it entered this manager's per-id lane, or this manager may
            # still hold the predecessor of the endpoint's current row.
            # Resolve that direction without the global manager mutex: the
            # per-id + object lifecycle lanes stabilize ``candidate`` while a
            # database row lock may legitimately block.
            current_row: dict[str, Any] | None = None
            current_token = ""
            if token_direction_needed:
                current_row = self._storage.ensure_workstream_incarnation_snapshot(ws_id)
                current_token = (
                    str(current_row.get("fork_reservation_token") or "")
                    if current_row is not None
                    else ""
                )

            with self._lock:
                if (
                    self._workstreams.get(ws_id) is not candidate
                    and self._failed_delete_tombstones.get(ws_id) is not candidate
                ):
                    return False
                # * durable == local: request is stale; leave local untouched
                # * durable == expected: local is stale; retire it, then let
                #   delete_fn conditionally delete the authorized successor
                # * third/missing incarnation: request changed again; no-op
                if token_direction_needed:
                    if current_token == candidate._fork_reservation_token:
                        return False
                    if current_token != expected_reservation_token:
                        return False
                deleting_authorized_successor = bool(
                    token_direction_needed and current_token == expected_reservation_token
                )
                was_unadvertised = (
                    False
                    if deleting_authorized_successor
                    else (
                        self._pending_creates.get(ws_id) is candidate
                        or ws_id in self._failed_delete_unadvertised
                    )
                )
                delete_event_name = name or (
                    str(current_row.get("name") or "")
                    if deleting_authorized_successor and current_row is not None
                    else candidate.name
                )
                candidate._lifecycle_terminal_active = True
                self._retain_state_tail_locked(candidate)

            with candidate._lock:
                candidate._closed = True
                candidate._state_revision += 1

            deleted = False
            try:
                # Stop new generation commits and drain every durability batch
                # admitted before the terminal latch. Worker/send flags alone
                # are insufficient: an accepted save_message closure can
                # outlive both and otherwise recreate conversation rows after
                # the workstream delete.
                drain_durability = getattr(
                    candidate.session,
                    "shutdown_publication_and_drain_durability",
                    None,
                )
                if callable(drain_durability):
                    try:
                        drain_durability()
                    except BaseException:
                        # A successful exact delete makes an unresolved
                        # conversation repair irrelevant. Continue to that
                        # authoritative operation; an ambiguous outcome below
                        # retains the journal tombstone.
                        log.warning(
                            "session_mgr.delete_persisted.terminal_repair_failed ws=%s",
                            ws_id[:8],
                            exc_info=True,
                        )

                # Drain every admitted predecessor state write before the hard
                # delete. The per-id lifecycle lane prevents a successor from
                # registering until this tail is fully tombstoned and the
                # terminal event has published.
                with candidate._state_tail_lock:
                    if self._state_writer is not None:
                        self._state_writer.discard(
                            ws_id,
                            tombstone=True,
                            incarnation=candidate._state_incarnation,
                        )
                    deleted = delete_fn()

                if not deleted:
                    # A conforming exact-delete false normally proves a
                    # missing/replaced incarnation. Treat it as an ambiguous
                    # storage outcome nevertheless: a transient implementation
                    # or wrapper may return false while the same durable row
                    # survives. Never discard that row's only structural or
                    # conversation repair owner in the latter case.
                    self._dispose_ambiguous_failed_delete(
                        candidate,
                        was_unadvertised=was_unadvertised,
                    )
                    return False

                with self._lock:
                    if (
                        self._workstreams.get(ws_id) is not candidate
                        and self._failed_delete_tombstones.get(ws_id) is not candidate
                    ):
                        # The id + object lifecycle lanes make this impossible
                        # for conforming paths; never emit against a replacement.
                        candidate._lifecycle_terminal_active = False
                        return False
                    self._workstreams.pop(ws_id, None)
                    self._drop_delete_tombstone_locked(ws_id)
                    if self._pending_creates.get(ws_id) is candidate:
                        self._pending_creates.pop(ws_id, None)
                    if ws_id in self._order:
                        self._order.remove(ws_id)
                    if self._active_id == ws_id:
                        self._active_id = self._first_visible_id_locked()

                try:
                    self._adapter.cleanup_ui(candidate)
                except Exception:
                    log.warning(
                        "session_mgr.delete_persisted.cleanup_failed ws=%s",
                        ws_id[:8],
                        exc_info=True,
                    )
                if self._event_emitter is not None and not was_unadvertised:
                    self._event_emitter.emit_closed(
                        ws_id,
                        reason="deleted",
                        name=delete_event_name,
                    )
                return True
            except BaseException:
                self._dispose_ambiguous_failed_delete(
                    candidate,
                    was_unadvertised=was_unadvertised,
                )
                raise
            finally:
                self._release_state_tail(candidate)

    def _dispose_ambiguous_failed_delete(
        self,
        candidate: Workstream,
        *,
        was_unadvertised: bool,
    ) -> None:
        """One disposition for every ambiguous exact-delete outcome.

        The false-return and raise paths of ``_delete_persisted`` must
        stay behaviorally identical: a policy edit applied to one fork
        only would let the rarer path silently retire a tombstone that is
        the sole owner of an accepted repair journal.
        """
        disposition = self._failed_delete_durable_disposition(candidate)
        if disposition in {"missing", "different"}:
            # Missing/different proves this object's journal can no
            # longer repair the durable row. Retire silently: the
            # probe is not atomic with lifecycle fan-out, so a remote
            # same-id successor could be created before a tokenless
            # close event and be erased from collector/client caches.
            self._retire_failed_persisted_delete(candidate)
        elif _session_has_unresolved_persistence(candidate.session):
            # Same or unreadable durable incarnation plus unresolved
            # journal is the one lossless failure state: hide it from
            # open/create/capacity, retain it for an idempotent exact-
            # delete retry, and emit no false close. Its ws-id-only row
            # closures must never background-replay across an ABA.
            self._retain_failed_persisted_delete_tombstone(
                candidate,
                was_unadvertised=was_unadvertised,
            )
        else:
            # The durable prefix is complete, so the historical
            # retire-and-rehydrate behavior remains safe.
            self._retire_failed_persisted_delete(candidate)

    def _drop_delete_tombstone_locked(
        self,
        ws_id: str,
        *,
        candidate: Workstream | None = None,
    ) -> bool:
        """Retire the (tombstone, unadvertised-flag) PAIR under ``self._lock``.

        The two structures are only ever mutated together: a pop that missed
        the flag discard would leave a stale unadvertised marker that
        suppresses a later same-id delete's ``ws_closed`` event, and a
        discard that outlived an identity-gated pop would strip a REPLACEMENT
        tombstone's flag (round-5 review — both halves of the drift). When
        ``candidate`` is supplied and a different object holds the tombstone,
        neither half is touched.
        """
        if candidate is not None and self._failed_delete_tombstones.get(ws_id) is not candidate:
            return False
        self._failed_delete_tombstones.pop(ws_id, None)
        self._failed_delete_unadvertised.discard(ws_id)
        return True

    def _retain_delete_tombstone_locked(
        self,
        ws_id: str,
        candidate: Workstream,
        *,
        was_unadvertised: bool,
    ) -> None:
        """Install the (tombstone, unadvertised-flag) PAIR under ``self._lock``."""
        self._failed_delete_tombstones[ws_id] = candidate
        if was_unadvertised:
            self._failed_delete_unadvertised.add(ws_id)

    def _retire_failed_persisted_delete(self, candidate: Workstream) -> None:
        """Silently retire the exact object after a failed hard-delete."""
        ws_id = candidate.id
        retired = False
        with self._lock:
            if self._workstreams.get(ws_id) is candidate:
                self._workstreams.pop(ws_id, None)
                retired = True
            if self._drop_delete_tombstone_locked(ws_id, candidate=candidate):
                retired = True
            if self._pending_creates.get(ws_id) is candidate:
                self._pending_creates.pop(ws_id, None)
            if ws_id in self._order:
                self._order.remove(ws_id)
            if self._active_id == ws_id:
                self._active_id = self._first_visible_id_locked()
        if not retired:
            return
        try:
            self._adapter.cleanup_ui(candidate)
        except Exception:
            log.warning(
                "session_mgr.delete_persisted.failed_cleanup ws=%s",
                ws_id[:8],
                exc_info=True,
            )

    def _retain_failed_persisted_delete_tombstone(
        self,
        candidate: Workstream,
        *,
        was_unadvertised: bool,
    ) -> None:
        """Hide an exact terminal object while preserving its repair journal."""
        ws_id = candidate.id
        with self._lock:
            if self._workstreams.get(ws_id) is candidate:
                self._workstreams.pop(ws_id, None)
            if self._pending_creates.get(ws_id) is candidate:
                self._pending_creates.pop(ws_id, None)
            if ws_id in self._order:
                self._order.remove(ws_id)
            if self._active_id == ws_id:
                self._active_id = self._first_visible_id_locked()
            self._retain_delete_tombstone_locked(
                ws_id, candidate, was_unadvertised=was_unadvertised
            )
        ui = candidate.ui
        if ui is not None and hasattr(ui, "_listeners_lock"):
            # A retained tombstone is terminal to every user-facing surface,
            # but cleanup_ui would destroy the session/journal that makes the
            # ambiguous delete lossless. Quiesce only its per-workstream SSE
            # transports; the sentinel is consumed internally and is not a
            # false lifecycle ws_closed event.
            _broadcast_ws_closed_to_listeners(ui)

    def _failed_delete_durable_disposition(self, candidate: Workstream) -> str:
        """Classify the exact durable incarnation after an ambiguous delete."""
        snapshot = getattr(self._storage, "ensure_workstream_incarnation_snapshot", None)
        if not callable(snapshot):
            return "unknown"
        try:
            row = snapshot(candidate.id)
        except Exception:
            log.warning(
                "session_mgr.delete_persisted.snapshot_failed ws=%s",
                candidate.id[:8],
                exc_info=True,
            )
            return "unknown"
        if row is None:
            return "missing"
        durable_token = str(row.get("fork_reservation_token") or "")
        if durable_token != candidate._fork_reservation_token:
            return "different"
        return "same"

    # ------------------------------------------------------------------
    # close / set_state / close_idle
    # ------------------------------------------------------------------

    def close(self, ws_id: str) -> bool:
        """Soft-close one incarnation on the stable per-id lifecycle lane."""
        with self._id_lifecycle(ws_id):
            return self._close_serialized(ws_id)

    def _close_serialized(self, ws_id: str) -> bool:
        """Soft-close: unload from memory + mark state=closed in storage.

        Returns ``True`` when a live workstream was removed,
        ``False`` if the id wasn't tracked.
        """
        with self._lock:
            ws = self._workstreams.get(ws_id)
            if ws is None:
                return False
            if (
                ws._create_publication_active
                and ws._create_publication_thread == threading.get_ident()
            ):
                return False

        with ws._lifecycle_lock:
            with self._lock:
                if self._workstreams.get(ws_id) is not ws:
                    return False
            # Close admission must become terminal to dispatch before the
            # session fence begins. ``prepare_soft_close`` may wait for an
            # admitted durability batch; leaving ``_closed`` false across that
            # wait lets a racing session_worker claim a fresh slot and report
            # the send accepted even though ChatSession will reject its later
            # generation claim. Both sides serialize on ``ws._lock``, making
            # this the linearization point for close versus dispatch.
            with ws._lock:
                if ws._closed:
                    return False
                ws._closed = True
                ws._state_revision += 1

            prepared = False
            try:
                if not _session_prepare_soft_close(ws.session):
                    # An accepted conversation row is still unresolved.
                    # Removing the sole live handoff journal would make that
                    # row disappear on reopen; retain the exact workstream so
                    # storage recovery can reconcile it idempotently.
                    return False
                prepared = True
            finally:
                if not prepared:
                    # The session refused (or raised during) preparation, so
                    # this incarnation remains live. Advance rather than
                    # restoring the old revision: a deferred state write that
                    # observed the temporary tombstone must not regain
                    # ownership through a revision ABA.
                    with ws._lock:
                        ws._closed = False
                        ws._state_revision += 1
            with self._lock:
                if self._workstreams.get(ws_id) is not ws:
                    return False
                self._workstreams.pop(ws_id, None)
                was_unadvertised = self._pending_creates.get(ws_id) is ws
                if was_unadvertised:
                    self._pending_creates.pop(ws_id, None)
                self._retain_state_tail_locked(ws)
                if ws_id in self._order:
                    self._order.remove(ws_id)
                if self._active_id == ws_id:
                    self._active_id = self._first_visible_id_locked()

            # The dispatch tombstone was published before session preparation.
            # Storage and cleanup may block, but unrelated workstreams and
            # manager lookups do not.
            try:
                self._adapter.cleanup_ui(ws)
            finally:
                if was_unadvertised and ws._fork_reservation_token:
                    self._delete_unadvertised_fork(ws)
                else:
                    self._persist_closed_state(ws)
            if self._event_emitter is not None and not was_unadvertised:
                self._event_emitter.emit_closed(ws_id, name=ws.name)
            return True

    def set_state(
        self,
        ws_id: str,
        state: WorkstreamState,
        error_msg: str = "",
    ) -> None:
        """Update state, then persist and publish on the workstream tail lane."""
        admitted = self._admit_state_change(ws_id, state, error_msg)
        if admitted is None:
            return
        ws, revision = admitted
        self._run_state_tail(ws, revision, state)

    def set_state_deferred(
        self,
        ws_id: str,
        state: WorkstreamState,
        *,
        deferred_persistence: list[Callable[[], None]],
        error_msg: str = "",
        after_persist: Callable[[], None] | None = None,
        owner_valid: Callable[[], bool] | None = None,
    ) -> bool:
        """Mutate live state now; defer durable and observer publication.

        Generation-owned session commits use this split form so the short
        lifecycle lock never spans a database flush or subscriber callback.
        The deferred closure rechecks the workstream tombstone under
        ``ws._lock``: a close that wins after live admission makes the whole
        delayed transition inert, including adapter/subscriber and optional
        session-local publication.  Direct callers keep :meth:`set_state`'s
        historical persist-before-publish ordering.
        """
        if not self._owner_is_valid(owner_valid):
            return False
        admitted = self._admit_state_change(ws_id, state, error_msg)
        if admitted is None:
            return False
        ws, revision = admitted

        def _persist_then_publish() -> None:
            published = self._run_state_tail(
                ws,
                revision,
                state,
                owner_valid=owner_valid,
            )
            if published and after_persist is not None:
                after_persist()

        deferred_persistence.append(_persist_then_publish)
        return True

    def _admit_state_change(
        self,
        ws_id: str,
        state: WorkstreamState,
        error_msg: str,
    ) -> tuple[Workstream, int] | None:
        """Apply the bounded in-memory half of one state transition."""
        with self._lock:
            ws = self._workstreams.get(ws_id)
            if ws is None:
                return None
        with ws._lock:
            if ws._closed:
                return None
            self._apply_live_state(ws, state, error_msg)
            revision = ws._state_revision
        return ws, revision

    @staticmethod
    def _apply_live_state(
        ws: Workstream,
        state: WorkstreamState,
        error_msg: str,
    ) -> None:
        ws.state = state
        ws.last_active = time.monotonic()
        ws.error_message = error_msg
        ws._state_revision += 1

    def _persist_state(self, ws: Workstream, state: WorkstreamState) -> None:
        """Persist one accepted state without any lifecycle lock held."""
        if self._state_writer is not None:
            self._state_writer.record(
                ws.id,
                state.value,
                flush_now=(state is WorkstreamState.ERROR),
                incarnation=ws._state_incarnation,
            )
            return
        try:
            self._storage.update_workstream_state(ws.id, state.value)
        except Exception:
            log.debug(
                "session_mgr.state_update_failed ws=%s",
                ws.id[:8],
                exc_info=True,
            )

    def _run_state_tail(
        self,
        ws: Workstream,
        revision: int,
        state: WorkstreamState,
        *,
        owner_valid: Callable[[], bool] | None = None,
    ) -> bool:
        """Run storage + observers in the shared per-id serial lane.

        A tail that has not started may be overtaken and becomes a cheap
        no-op.  Once a tail starts, close and successor tails wait for it, so
        storage and publication cannot reorder across direct/deferred callers
        or an ABA reopen.  Only the lane lock spans storage/callbacks; manager,
        workstream, and ChatSession generation locks never do.
        """
        if not self._retain_current_state_tail(ws):
            return False
        try:
            with ws._state_tail_lock:
                if not self._owner_is_valid(owner_valid) or not self._state_is_current(
                    ws,
                    revision,
                ):
                    return False
                self._persist_state(ws, state)
                if not self._owner_is_valid(owner_valid) or not self._state_is_current(
                    ws,
                    revision,
                ):
                    return False

                # This current-revision check is the publication
                # linearization.  Preparing the coordinator payload may
                # destructively drain terminal content, so stale revisions
                # never reach it.  A successor admitted after this point waits
                # on the same lane before its own tail can publish.
                event_publish = self._prepare_state_event(ws, state)
                if not self._owner_is_valid(owner_valid):
                    return False
                self._publish_state_change(
                    ws,
                    state,
                    event_publish=event_publish,
                )
                return True
        finally:
            self._release_state_tail(ws)

    @staticmethod
    def _owner_is_valid(owner_valid: Callable[[], bool] | None) -> bool:
        if owner_valid is None:
            return True
        try:
            return owner_valid()
        except Exception:
            log.debug("session_mgr.state_owner_check_failed", exc_info=True)
            return False

    def _state_is_current(self, ws: Workstream, revision: int) -> bool:
        with self._lock:
            if self._workstreams.get(ws.id) is not ws:
                return False
        with ws._lock:
            return not ws._closed and ws._state_revision == revision

    def _retain_current_state_tail(self, ws: Workstream) -> bool:
        with self._lock:
            if self._workstreams.get(ws.id) is not ws:
                return False
            self._retain_state_tail_locked(ws)
            return True

    def _retain_state_tail_locked(self, ws: Workstream) -> None:
        """Retain ``ws``'s lane. Caller owns the manager lock."""
        self._state_tail_locks.setdefault(ws.id, ws._state_tail_lock)
        self._state_tail_users[ws.id] = self._state_tail_users.get(ws.id, 0) + 1

    def _release_state_tail(self, ws: Workstream) -> None:
        with self._lock:
            users = self._state_tail_users.get(ws.id, 0)
            if users <= 1:
                self._state_tail_users.pop(ws.id, None)
                self._prune_state_tail_locked(
                    ws.id,
                    expected_lock=ws._state_tail_lock,
                )
            else:
                self._state_tail_users[ws.id] = users - 1

    def _prune_state_tail_locked(
        self,
        ws_id: str,
        *,
        expected_lock: threading.Lock | None = None,
    ) -> None:
        """Drop an unused per-id state lane. Caller owns manager lock."""
        if (
            ws_id in self._workstreams
            or ws_id in self._pending_creates
            or self._state_tail_users.get(ws_id, 0) > 0
        ):
            return
        current = self._state_tail_locks.get(ws_id)
        if expected_lock is not None and current is not expected_lock:
            return
        self._state_tail_locks.pop(ws_id, None)

    def _persist_closed_state(self, ws: Workstream) -> None:
        """Write the terminal row after all predecessor tails finish."""
        try:
            with ws._state_tail_lock:
                if self._state_writer is not None:
                    self._state_writer.discard(
                        ws.id,
                        tombstone=True,
                        incarnation=ws._state_incarnation,
                    )
                try:
                    self._storage.update_workstream_state(ws.id, "closed")
                except Exception:
                    log.debug(
                        "session_mgr.state_update_failed ws=%s",
                        ws.id[:8],
                        exc_info=True,
                    )
                try:
                    self._storage.delete_workstream_override(ws.id)
                except Exception:
                    log.debug(
                        "session_mgr.override_delete_failed ws=%s",
                        ws.id[:8],
                        exc_info=True,
                    )
        finally:
            self._release_state_tail(ws)

    def _delete_unadvertised_fork(self, ws: Workstream) -> None:
        """Delete a pending fork only while its durable fence is still ours."""
        try:
            with ws._state_tail_lock:
                if self._state_writer is not None:
                    self._state_writer.discard(
                        ws.id,
                        tombstone=True,
                        incarnation=ws._state_incarnation,
                    )
                try:
                    deleted = self._storage.delete_workstream_if_fork_reserved(
                        ws.id,
                        ws._fork_reservation_token,
                    )
                    if not deleted:
                        log.debug(
                            "session_mgr.pending_fork_delete_lost_reservation ws=%s",
                            ws.id[:8],
                        )
                except Exception:
                    # Never fall back to delete-by-id: a replacement durable
                    # row may now own this caller-known workstream id.
                    log.warning(
                        "session_mgr.pending_fork_delete_failed ws=%s",
                        ws.id[:8],
                        exc_info=True,
                    )
        finally:
            self._release_state_tail(ws)

    def _prepare_state_event(
        self,
        ws: Workstream,
        state: WorkstreamState,
    ) -> Callable[[], None] | None:
        """Capture an immutable adapter payload for a deferred transition."""
        if self._event_emitter is None:
            return None
        prepare = getattr(self._event_emitter, "prepare_state_event", None)
        if prepare is not None:
            prepared = prepare(ws, state)

            def _publish_prepared() -> None:
                prepared()

            return _publish_prepared
        # Compatibility for external emitters whose contract is state-only.
        return functools.partial(self._event_emitter.emit_state, ws, state)

    def _publish_state_change(
        self,
        ws: Workstream,
        state: WorkstreamState,
        *,
        event_publish: Callable[[], None] | None = None,
    ) -> None:
        """Emit the bounded adapter/subscriber half of a state transition."""
        if event_publish is not None:
            event_publish()
        elif self._event_emitter is not None:
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
                callback(ws.id, state)

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
        """Cancel in-flight generation and unblock any pending approval.

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
            resolve_all = getattr(ws.ui, "resolve_all_approvals", None)
            resolve_one = getattr(ws.ui, "resolve_approval", None)
            with contextlib.suppress(Exception):
                if callable(resolve_all):
                    resolve_all(False, "cancelled")
                elif callable(resolve_one):
                    # Compatibility for older/minimal UI implementations.
                    resolve_one(False, "cancelled")
        return True

    def reap_stale_creating_reservations(
        self,
        max_age_seconds: float = STALE_CREATE_GRACE_SECONDS,
    ) -> list[str]:
        """Hard-delete crash-abandoned hidden create reservations.

        This maintenance is independent from :meth:`close_idle`: disabling
        idle eviction must not disable recovery of caller-known ids stranded by
        a process death. The backend owns the atomic state/age/incarnation
        check and complete dependent cleanup; this layer supplies the current
        manager snapshot plus cluster liveness.

        The current process's ``node_id`` is intentionally not treated as a
        live-owner exemption by the backend. Stable ids (notably ``console``
        and configured ``TURNSTONE_NODE_ID`` values) survive process restarts,
        so an old reservation bearing our id must become reclaimable. Every
        workstream presently loaded by this manager, including pending creates,
        is excluded, and the age grace protects a create admitted just after
        the snapshot.

        Liveness or storage uncertainty fails closed and returns no ids.
        """
        service_type = self._service_type
        if service_type is None:
            log.debug(
                "session_mgr.stale_create_reap_no_service_type kind=%s",
                self.kind.value,
            )
            return []
        with self._lock:
            loaded = list(self._workstreams.keys())
        try:
            live_services = self._storage.list_services(service_type)
            live_node_ids = [
                str(service["service_id"]) for service in live_services if service.get("service_id")
            ]
        except Exception:
            log.debug(
                "session_mgr.stale_create_reap_liveness_failed kind=%s",
                self.kind.value,
                exc_info=True,
            )
            return []

        cutoff = (datetime.now(UTC) - timedelta(seconds=max_age_seconds)).strftime(
            "%Y-%m-%dT%H:%M:%S"
        )
        try:
            reaped = self._storage.delete_stale_creating_reservations(
                self.kind,
                cutoff,
                loaded,
                live_node_ids=live_node_ids,
                local_node_id=self._node_id,
            )
        except Exception:
            log.debug(
                "session_mgr.stale_create_reap_failed kind=%s",
                self.kind.value,
                exc_info=True,
            )
            return []
        if reaped:
            log.info(
                "session_mgr.stale_create_reaped count=%d kind=%s",
                len(reaped),
                self.kind.value,
            )
        return reaped

    def reconcile_unresolved_persistence(self, *, now: float | None = None) -> list[str]:
        """Attempt every due transient conversation repair without manager locks.

        The maintenance owner is shared per process; sessions retain only a
        monotonic due timestamp. Permanent commit conflicts deliberately stay
        fail-stopped until explicit deletion instead of consuming retry work.
        Returns ids whose prefix became durable this pass or whose previously
        reconciled persistence-owned ``ERROR`` state was retired, plus hidden
        delete tombstones retired after a probe proved their durable
        incarnation missing or different.
        """
        check_at = time.monotonic() if now is None else now
        with self._lock:
            candidates = [
                ws
                for ws in self._workstreams.values()
                if ws.session is not None and self._pending_creates.get(ws.id) is not ws
            ]
            delete_tombstones = list(self._failed_delete_tombstones.values())

        repaired: list[str] = []
        for ws in candidates:
            with ws._lock:
                session = ws.session
                if (
                    session is None
                    or ws._closed
                    or ws._worker_running
                    or ws._lifecycle_terminal_active
                ):
                    continue
                error_state_revision = (
                    ws._state_revision if ws.state is WorkstreamState.ERROR else None
                )
            fatal_revision = (
                _session_conversation_persistence_fatal_revision(session)
                if error_state_revision is not None
                else None
            )
            unresolved = _session_has_unresolved_persistence(session)
            attempted = False
            if unresolved:
                try:
                    attempted = _session_reconcile_unresolved_persistence_if_due(
                        session,
                        check_at,
                    )
                except Exception:
                    log.warning(
                        "session_mgr.persistence_reconcile_failed ws=%s",
                        ws.id[:8],
                        exc_info=True,
                    )
                    continue
            recovery_ready = (attempted and not _session_has_unresolved_persistence(session)) or (
                not unresolved and fatal_revision is not None
            )
            if recovery_ready:
                repaired.append(ws.id)
                idle_revision: int | None = None
                if fatal_revision is not None:
                    # A persistence failure is a fatal turn outcome and leaves
                    # the workstream ERROR. Once its exact journal boundary is
                    # repaired, retire only that unchanged error state. A new
                    # worker, successor session, lifecycle tombstone, or any
                    # intervening state revision wins and keeps its state.
                    with self._lock:
                        still_owned = (
                            self._workstreams.get(ws.id) is ws
                            and self._pending_creates.get(ws.id) is not ws
                            and not ws._lifecycle_terminal_active
                        )
                    if (
                        still_owned
                        and _session_conversation_persistence_fatal_revision(session)
                        == fatal_revision
                    ):
                        with ws._lock:
                            if (
                                ws.session is session
                                and not ws._closed
                                and not ws._worker_running
                                and ws.state is WorkstreamState.ERROR
                                and ws._state_revision == error_state_revision
                            ):
                                self._apply_live_state(ws, WorkstreamState.IDLE, "")
                                idle_revision = ws._state_revision
                if idle_revision is not None:
                    assert fatal_revision is not None
                    _session_acknowledge_conversation_persistence_recovery(
                        session,
                        fatal_revision,
                    )
                    try:
                        published = self._run_state_tail(
                            ws,
                            idle_revision,
                            WorkstreamState.IDLE,
                        )
                    except Exception:
                        log.warning(
                            "session_mgr.persistence_recovery_state_failed ws=%s",
                            ws.id[:8],
                            exc_info=True,
                        )
                    else:
                        if published:
                            _notify_persistence_state_changed(ws.ui)

        # Ambiguous hard-delete objects are intentionally absent from the
        # ordinary candidate list. A missing/different durable incarnation is
        # proof that their predecessor journal can never be applied and may be
        # retired. A same/unknown incarnation remains hidden for an explicit
        # token-guarded delete retry: captured row closures are keyed only by
        # ws_id, so a snapshot-then-background-save would race a remote same-id
        # replacement and write predecessor history into it.
        for tombstone in delete_tombstones:
            with self._id_lifecycle(tombstone.id), tombstone._lifecycle_lock:
                with self._lock:
                    if self._failed_delete_tombstones.get(tombstone.id) is not tombstone:
                        continue
                disposition = self._failed_delete_durable_disposition(tombstone)
                if disposition in {"missing", "different"}:
                    self._retire_failed_persisted_delete(tombstone)
                    repaired.append(tombstone.id)
        return repaired

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
        closed_ids: list[str] = []
        now = time.monotonic()
        with self._lock:
            candidates = [
                ws
                for ws in self._workstreams.values()
                if self._pending_creates.get(ws.id) is not ws
            ]

        for ws in candidates:
            with self._id_lifecycle(ws.id):
                if self._close_idle_candidate(ws, now, max_age_seconds):
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

    def _close_idle_candidate(
        self,
        ws: Workstream,
        now: float,
        max_age_seconds: float,
    ) -> bool:
        """Retire one still-idle, worker-free incarnation."""
        # The id lane prevents a successor incarnation from publishing until
        # this terminal tail and ws_closed event are complete. The object lock
        # serializes with its own create publication/delete path. State and
        # worker admission are owned by ``ws._lock`` rather than the manager
        # lock, so revalidate both together and install the tombstone first.
        with ws._lifecycle_lock:
            with self._lock:
                if self._workstreams.get(ws.id) is not ws or self._pending_creates.get(ws.id) is ws:
                    return False
            with ws._lock:
                if (
                    ws._closed
                    or ws.state is not WorkstreamState.IDLE
                    or ws._worker_running
                    or _session_persistence_blocks_retirement(ws.session)
                    or (now - ws.last_active) <= max_age_seconds
                ):
                    return False
                ws._closed = True
                ws._state_revision += 1
            with self._lock:
                if self._workstreams.get(ws.id) is not ws:
                    return False
                self._workstreams.pop(ws.id, None)
                if ws.id in self._order:
                    self._order.remove(ws.id)
                if self._active_id == ws.id:
                    self._active_id = self._first_visible_id_locked()
                self._retain_state_tail_locked(ws)

            try:
                self._adapter.cleanup_ui(ws)
            finally:
                self._persist_closed_state(ws)
            if self._event_emitter is not None:
                self._event_emitter.emit_closed(ws.id, name=ws.name)
            return True

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    def get(self, ws_id: str) -> Workstream | None:
        with self._lock:
            ws = self._workstreams.get(ws_id)
            if ws is not None and self._pending_creates.get(ws_id) is ws:
                return None
            return ws

    def list_all(self) -> list[Workstream]:
        """Return workstreams in creation order."""
        with self._lock:
            return [
                self._workstreams[wid]
                for wid in self._order
                if wid in self._workstreams and wid not in self._pending_creates
            ]

    # ------------------------------------------------------------------
    # Internal — slot reservation
    # ------------------------------------------------------------------

    def _reserve_and_install(
        self,
        ws_id: str,
        *,
        user_id: str,
        name: str,
        parent_ws_id: str | None = None,
        project_id: str | None = None,
        persona: str = "",
        pending: bool = False,
        reservation_token: str = "",
    ) -> tuple[Workstream, Workstream | None]:
        """Install one slot, retiring only an exact idle worker-free victim.

        Capacity selection is merely a hint. The victim's stable per-id lane
        and object lifecycle lock are acquired before state is revalidated and
        a tombstone is claimed under ``ws._lock``. Worker admission uses that
        same lock, so an IDLE workstream whose turn/command was admitted cannot
        be evicted between the hint and the terminal claim.
        """
        persistence_recovery_attempted = False
        while True:
            with self._lock:
                if ws_id in self._retiring_ids or ws_id in self._failed_delete_tombstones:
                    raise WorkstreamAlreadyExistsError(f"workstream {ws_id!r} is retiring")
                if ws_id in self._workstreams or ws_id in self._pending_creates:
                    raise WorkstreamAlreadyExistsError(
                        f"workstream {ws_id!r} already tracked by SessionManager"
                    )
                if len(self._workstreams) < self._max_active:
                    ws = self._install_workstream_locked(
                        ws_id,
                        user_id=user_id,
                        name=name,
                        parent_ws_id=parent_ws_id,
                        project_id=project_id,
                        persona=persona,
                        pending=pending,
                        reservation_token=reservation_token,
                    )
                    return ws, None
                candidates = sorted(
                    (
                        candidate
                        for candidate in self._workstreams.values()
                        if candidate.session is not None
                        and self._pending_creates.get(candidate.id) is not candidate
                        and not candidate._lifecycle_terminal_active
                        and not candidate._closed
                        and candidate.state is WorkstreamState.IDLE
                        and not candidate._worker_running
                        and not candidate.send_barrier_active()
                        and not _session_persistence_blocks_retirement(candidate.session)
                    ),
                    key=lambda candidate: candidate.last_active,
                )
            if not candidates:
                if not persistence_recovery_attempted:
                    persistence_recovery_attempted = True
                    self.reconcile_unresolved_persistence()
                    continue
                raise RuntimeError(f"All {self._max_active} slots are active")

            for victim in candidates:
                install_exc: BaseException | None = None
                with self._id_lifecycle(victim.id), victim._lifecycle_lock:
                    with self._lock:
                        if (
                            self._workstreams.get(victim.id) is not victim
                            or self._pending_creates.get(victim.id) is victim
                            or victim._lifecycle_terminal_active
                        ):
                            continue
                        victim._lifecycle_terminal_active = True

                    with victim._lock:
                        if (
                            victim._closed
                            or victim.state is not WorkstreamState.IDLE
                            or victim._worker_running
                            or victim.send_barrier_active()
                            or _session_persistence_blocks_retirement(victim.session)
                        ):
                            worker_free_idle = False
                        else:
                            worker_free_idle = True
                            victim._closed = True
                            victim._state_revision += 1
                    if not worker_free_idle:
                        with self._lock:
                            victim._lifecycle_terminal_active = False
                        continue

                    with self._lock:
                        if self._workstreams.get(victim.id) is not victim:
                            victim._lifecycle_terminal_active = False
                            restore_victim = True
                        elif len(self._workstreams) < self._max_active:
                            # Another terminal path freed a different slot while
                            # we acquired this candidate. Do not over-evict.
                            victim._lifecycle_terminal_active = False
                            restore_victim = True
                        else:
                            restore_victim = False
                            victim_order_index = (
                                self._order.index(victim.id)
                                if victim.id in self._order
                                else len(self._order)
                            )
                            victim_was_active = self._active_id == victim.id
                            self._workstreams.pop(victim.id, None)
                            if victim.id in self._order:
                                self._order.remove(victim.id)
                            if victim_was_active:
                                self._active_id = self._first_visible_id_locked()
                            try:
                                ws = self._install_workstream_locked(
                                    ws_id,
                                    user_id=user_id,
                                    name=name,
                                    parent_ws_id=parent_ws_id,
                                    project_id=project_id,
                                    persona=persona,
                                    pending=pending,
                                    reservation_token=reservation_token,
                                )
                            except BaseException as exc:
                                # UI construction failed before the replacement
                                # became observable. Restore the incumbent and
                                # its order/focus exactly; it was not evicted.
                                self._workstreams[victim.id] = victim
                                self._order.insert(victim_order_index, victim.id)
                                if victim_was_active:
                                    self._active_id = victim.id
                                victim._lifecycle_terminal_active = False
                                restore_victim = True
                                install_exc = exc
                            else:
                                self._retiring_ids.add(victim.id)
                                self._retain_state_tail_locked(victim)
                                self._eviction_count += 1
                                try:
                                    from turnstone.core.metrics import metrics as _m

                                    _m.record_eviction()
                                except Exception:
                                    log.debug(
                                        "session_mgr.metrics_eviction_failed",
                                        exc_info=True,
                                    )

                    if restore_victim:
                        with victim._lock:
                            victim._closed = False
                            victim._state_revision += 1
                        if install_exc is not None:
                            raise install_exc
                        # Capacity changed under us; resnapshot rather than
                        # retiring an unnecessary second workstream.
                        break

                    self._finish_eviction(victim)
                    return ws, victim

            # Every hinted candidate either admitted work or changed lifecycle
            # before its exact claim. Recompute from current authoritative state.

    def _install_workstream_locked(
        self,
        ws_id: str,
        *,
        user_id: str,
        name: str,
        parent_ws_id: str | None = None,
        project_id: str | None = None,
        persona: str = "",
        pending: bool = False,
        reservation_token: str = "",
    ) -> Workstream:
        """Install a placeholder ``Workstream`` under ``self._lock``.

        Placeholders with ``session=None`` count toward capacity but are never
        themselves eviction candidates (a burst of concurrent creates must not
        evict each other). Victim admission is owned by
        :meth:`_reserve_and_install`; this helper only performs the atomic
        registry insertion once a slot is available or claimed.

        Caller MUST hold ``self._lock``. UI allocation is included in
        the locked path so concurrent ``get()`` never observes a
        placeholder with ``ui=None``; only ``session`` lags.
        """
        if ws_id in self._workstreams or ws_id in self._pending_creates:
            # Defensive — create() uses a fresh uuid and open()
            # serializes on the per-ws lock which already bounces the
            # repeated install via the fast path.
            raise WorkstreamAlreadyExistsError(
                f"workstream {ws_id!r} already tracked by SessionManager"
            )

        ws = Workstream(id=ws_id, name=name)
        state_tail_lock = self._state_tail_locks.get(ws_id)
        if state_tail_lock is None:
            state_tail_lock = threading.Lock()
            self._state_tail_locks[ws_id] = state_tail_lock
        self._state_incarnation += 1
        ws._state_incarnation = self._state_incarnation
        ws._state_tail_lock = state_tail_lock
        ws.kind = self.kind
        ws.user_id = user_id
        ws.parent_ws_id = parent_ws_id if parent_ws_id else None
        ws.project_id = project_id if project_id else None
        ws.persona = persona
        try:
            ws.ui = self._adapter.build_ui(ws)
            if self._state_writer is not None:
                self._state_writer.reopen(
                    ws_id,
                    incarnation=ws._state_incarnation,
                )
        except BaseException:
            self._prune_state_tail_locked(
                ws_id,
                expected_lock=ws._state_tail_lock,
            )
            raise
        self._workstreams[ws_id] = ws
        self._order.append(ws_id)
        if reservation_token:
            ws._fork_reservation_token = reservation_token
        if pending:
            self._pending_creates[ws_id] = ws
        if self._active_id is None:
            self._active_id = ws_id
        if pending and self._active_id == ws_id:
            self._active_id = self._first_visible_id_locked()
        return ws

    def _first_visible_id_locked(self) -> str | None:
        """First advertised workstream id in creation order.

        Caller holds ``self._lock``. Pending creates occupy capacity and order
        slots but must never become CLI focus before lifecycle birth.
        """
        for ws_id in self._order:
            ws = self._workstreams.get(ws_id)
            if ws is not None and self._pending_creates.get(ws_id) is not ws:
                return ws_id
        return None

    def _remove_locked(self, ws_id: str) -> None:
        """Drop a (possibly-placeholder) workstream from tracking.

        Caller MUST hold ``self._lock``. Used on rollback paths when
        session construction or persistence fails after slot
        reservation — the placeholder otherwise pins capacity forever.
        """
        removed = self._workstreams.pop(ws_id, None)
        if self._pending_creates.get(ws_id) is removed:
            self._pending_creates.pop(ws_id, None)
        if ws_id in self._order:
            self._order.remove(ws_id)
        if self._active_id == ws_id:
            self._active_id = self._first_visible_id_locked()
        self._prune_state_tail_locked(ws_id)

    def _finish_eviction(self, ws: Workstream) -> None:
        """Complete one already-reserved eviction and release its id fence."""
        try:
            # Drain every predecessor state tail after the live tombstone. A
            # buffered writer is tombstoned for this in-memory incarnation but
            # the durable row deliberately remains reopenable at its last state.
            try:
                with ws._state_tail_lock:
                    if self._state_writer is not None:
                        self._state_writer.discard(
                            ws.id,
                            tombstone=True,
                            incarnation=ws._state_incarnation,
                        )
            except Exception:
                log.warning(
                    "session_mgr.eviction_state_tail_failed ws=%s",
                    ws.id[:8],
                    exc_info=True,
                )
            try:
                self._adapter.cleanup_ui(ws)
            except Exception:
                log.warning(
                    "session_mgr.eviction_cleanup_failed ws=%s",
                    ws.id[:8],
                    exc_info=True,
                )
            if self._event_emitter is not None:
                try:
                    self._event_emitter.emit_closed(
                        ws.id,
                        reason="evicted",
                        name=ws.name,
                    )
                except Exception:
                    log.warning(
                        "session_mgr.eviction_emit_failed ws=%s",
                        ws.id[:8],
                        exc_info=True,
                    )
        finally:
            with self._lock:
                self._retiring_ids.discard(ws.id)
            self._release_state_tail(ws)
