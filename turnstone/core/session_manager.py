"""Unified manager for workstream-shaped sessions.

Collapses ``WorkstreamManager`` (interactive) and ``CoordinatorManager``
(coordinator) into one class. Kind-specific transport and session
construction live on a ``SessionKindAdapter`` Protocol; the manager
itself owns the invariant mechanics — slot accounting, eviction,
persistence, per-ws lock refcount for concurrent lazy rehydrate.
"""

from __future__ import annotations

import contextlib
import enum
import functools
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol

from turnstone.core.adapters._ui_cleanup import _broadcast_ws_closed_to_listeners
from turnstone.core.log import get_logger
from turnstone.core.model_registry import ModelClientConstructionError, UnknownModelAliasError
from turnstone.core.node_affinity import parse_required_node_id, require_execution_node
from turnstone.core.personas import snapshot_from_config
from turnstone.core.storage._protocol import WorkstreamLeaseHeldError, WorkstreamLeaseLostError
from turnstone.core.workstream import (
    Workstream,
    WorkstreamHistoryUnavailableError,
    WorkstreamKind,
    WorkstreamState,
    concrete_method,
)
from turnstone.core.workstream_lease import (
    LeaseKeeper,
    WorkstreamLease,
    new_lease_holder,
    record_lease_event,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from turnstone.core.child_event_bus import ChildEventBus
    from turnstone.core.session import ChatSession, SessionUI
    from turnstone.core.state_writer import StateWriter
    from turnstone.core.storage._protocol import LeaseFence, StorageBackend

log = get_logger(__name__)


class CloseOutcome(enum.Enum):
    """Detailed soft-close result for callers that expose refusal reasons."""

    CLOSED = "closed"
    NOT_FOUND = "not_found"
    UNRESOLVED_PERSISTENCE = "unresolved_persistence"
    CLEANUP_PENDING = "cleanup_pending"
    # Unloaded here, but the row was no longer this process's: another process
    # took it over (the workstream did not close), or a maintenance pass closed
    # and fenced it out after this copy's lease lapsed. HTTP answers it like
    # NOT_FOUND, so the console re-routes the close to any holder.
    OWNED_ELSEWHERE = "owned_elsewhere"


_FACTORY_DROPPED_LEASE = "the session factory did not pass workstream_lease to the session it built"


class SessionCapacityError(RuntimeError):
    """Every slot is busy and none can be evicted: hosts answer 429, try later."""

    def __init__(self, max_active: int) -> None:
        super().__init__(f"All {max_active} slots are active")


class SessionFactoryLeaseError(ValueError):
    """A session factory built a session without the slot's ``workstream_lease``.

    A ``ValueError``: hosts answer a misconfigured session factory with 503 and
    its message.
    """

    def __init__(self) -> None:
        super().__init__(_FACTORY_DROPPED_LEASE)


class _Release(enum.Enum):
    """What releasing one lease found."""

    RELEASED = "released"  # storage released it, or it was released before
    NOT_OURS = "not_ours"  # the handle was lost, or storage found another fence
    UNREACHABLE = "unreachable"  # storage failed; the lease expires on its own


def _session_has_unresolved_persistence(session: Any) -> bool:
    """Read the concrete ChatSession hook without MagicMock auto-vivification.

    Blocking probe — callable only from contexts holding no workstream or
    manager lock (it acquires the session's generation and handoff locks).
    Retirement scans use :func:`_session_persistence_blocks_retirement`.
    """
    check = concrete_method(session, "has_unresolved_conversation_persistence")
    return bool(check()) if check is not None else False


def _session_has_tool_structural_debt(session: Any) -> bool:
    """Read the concrete structural-cleanup hook after close preparation."""
    check = concrete_method(session, "has_tool_structural_debt")
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


def _session_unresolved_persistence_nowait(session: Any) -> bool | None:
    """Non-blocking unresolved probe for the per-second reconcile walk.

    The steady-state pass concludes "nothing to do" on almost every
    workstream almost every second; taking each session's generation and
    handoff locks to learn that contends the very locks turn commits use,
    forever, scaling with roster size.  Try-acquire instead: ``None``
    (locks busy) means a turn owns the session right now — by definition
    not a moment that needs an unattended repair — and the next one-second
    pass re-probes.  Compatibility/test doubles without the nowait hook
    keep the blocking read and their scripted answers.
    """
    probe = concrete_method(session, "has_unresolved_conversation_persistence_nowait")
    if probe is None:
        return _session_has_unresolved_persistence(session)
    state = probe()
    return None if state is None else bool(state)


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
        options (e.g. interactive's ``judge_model``), and for what the
        manager supplies: ``workstream_lease`` (and, on a create,
        ``fork_reservation_token``) must reach the ``ChatSession`` unchanged;
        a session built without the lease fails the create or open with
        :class:`SessionFactoryLeaseError`. An adapter may ignore other extras
        it doesn't recognise; the manager stays kind-agnostic.
        """


class ExactDeleteFn(Protocol):
    """Exact hard delete of one durable incarnation.

    ``lease`` is the manager's fence when it holds the workstream, else
    ``None``; storage then refuses while another process owns a live lease.
    """

    def __call__(self, *, lease: LeaseFence | None) -> bool: ...


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
    rehydrate). Interactive ``emit_created``, ``emit_closed`` and
    ``on_lease_retired`` are real, bounded global-queue publications.
    Coordinator's five methods are all real (cluster collector's pseudo-node
    sees every transition). See :class:`SessionKindAdapter` docstring for the
    asymmetry rationale.
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

    def on_lease_retired(self, ws: Workstream) -> None:
        """Bookkeeping for ``ws`` unloaded because another process took its row.

        Called instead of :meth:`emit_closed`: the workstream did not close, it
        lives on elsewhere. A kind whose ``emit_closed`` does more than announce
        the close (the coordinator closes its row on its own pseudo-node) does
        that part here.
        """

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
    #: Shared deadline for hosted sessions' admitted durable writes when the
    #: host releases leases at shutdown; well inside one lease TTL.
    _SHUTDOWN_DRAIN_SECONDS = 5.0

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
        # Owner leases (#988): this manager instance is one lease holder.
        # Every loaded incarnation holds a lease the keeper renews, and storage
        # refuses any session-owned write that does not present its current
        # fence, so two processes can never both write one workstream.
        self._lease_holder = new_lease_holder(node_id)
        self._lease_keeper = LeaseKeeper(
            storage,
            self._lease_holder,
            on_lost=self._on_leases_lost,
            label=adapter.kind.value,
        )
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
    def count(self) -> int:
        with self._lock:
            return len(self._workstreams)

    def lease_fence(self, ws_id: str) -> LeaseFence | None:
        """The fence a host presents when it writes ``ws_id`` directly, or ``None``.

        An HTTP write for a workstream hosted here presents its slot's fence.
        A lost or released handle still gives its fence, so the write is
        refused rather than sent unfenced (which could fence out the new
        owner). ``None`` once nothing here holds the id: the write then goes
        unfenced, as offline maintenance.
        """
        lease = self._handle_for(ws_id)
        return lease.fence if lease is not None else None

    def _handle_for(self, ws_id: str) -> WorkstreamLease | None:
        """This manager's handle for ``ws_id``: its slot's, whatever the state.

        A retained failed-delete tombstone counts as the slot. Otherwise the
        keeper's tracked handle, which covers an open or create between the
        grant and the slot holding it.
        """
        with self._lock:
            ws = self._workstreams.get(ws_id) or self._failed_delete_tombstones.get(ws_id)
        if ws is not None:
            with ws._lock:
                lease = ws._lease
            if lease is not None:
                return lease
        return self._lease_keeper.get(ws_id)

    def start_lease_keeper(self) -> None:
        """Start renewing this manager's leases. Hosts call it once at startup."""
        self._lease_keeper.start()

    def renew_leases_once(self) -> list[WorkstreamLease]:
        """Run one renewal pass now and retire what it finds lost (test seam)."""
        return self._lease_keeper.renew_once()

    def release_leases(self) -> None:
        """Release every lease at shutdown, once the writes it fences have landed.

        Hosts call this when their sessions are done, so another process can
        open the workstreams at once instead of waiting for the leases to
        expire. Each hosted session first stops admitting durable writes and
        drains those already admitted, under one shared deadline, while the
        keeper keeps renewing; the state writer then flushes while every fence
        is still valid. A session that does not drain in time keeps its
        leases, which expire on their own: releasing under a live write would
        refuse that write. The first storage failure stops the loop for the
        same reason.
        """
        deadline = time.monotonic() + self._SHUTDOWN_DRAIN_SECONDS
        kept: set[str] = set()
        with self._lock:
            slots = list(self._workstreams.values())
        # Latch every session first, so none keeps admitting writes (and
        # running work) while an earlier one drains: the shared budget then
        # goes to finishing what was admitted, not to new writes.
        for ws in slots:
            close = (
                concrete_method(ws.session, "close_publication") if ws.session is not None else None
            )
            if close is None:
                continue
            try:
                close()
            except Exception:
                log.warning("session_mgr.shutdown_latch_failed ws=%s", ws.id[:8], exc_info=True)
        for ws in slots:
            drain = (
                concrete_method(ws.session, "shutdown_publication_and_drain_durability")
                if ws.session is not None
                else None
            )
            if drain is None:
                continue
            try:
                drained = drain(timeout=max(0.0, deadline - time.monotonic()))
            except Exception:
                log.warning("session_mgr.shutdown_drain_failed ws=%s", ws.id[:8], exc_info=True)
                drained = False
            if drained is False:
                kept.add(ws.id)
        self._lease_keeper.stop()
        if self._state_writer is not None:
            try:
                self._state_writer.flush()
            except Exception:
                log.warning("session_mgr.shutdown_state_flush_failed", exc_info=True)
        for lease in self._lease_keeper.tracked():
            if lease.ws_id not in kept and self._release(lease) is _Release.UNREACHABLE:
                break

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
        required_node_id: str | None = None,
        defer_emit_created: bool = False,
        **extra_session_kwargs: Any,
    ) -> Workstream:
        """Create one workstream with an exact private rollback fence."""
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
            required_node_id=required_node_id,
            defer_emit_created=defer_emit_created,
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
        required_node_id: str | None = None,
        defer_emit_created: bool = False,
        **extra_session_kwargs: Any,
    ) -> Workstream:
        """Construct a new workstream, persist, and register.

        Slot reservation + placeholder install happen under the lock
        (single-phase). Session construction runs outside the lock; on
        failure both the in-memory slot and its unpublished durable
        reservation are released so capacity and IDs are not leaked.

        Raises ``RuntimeError`` when the manager is at capacity with
        no idle workstream to evict — callers (HTTP handlers) translate
        this to 429.

        ``defer_emit_created``: when ``True``, the workstream is reserved and
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
        required_node_id = parse_required_node_id(required_node_id)
        require_execution_node(required_node_id, self._node_id)
        requested_ws_id = ws_id
        while True:
            # Caller-chosen ids are contractual and collide loudly. Generated
            # ids are opaque implementation detail, so a live-ID collision
            # simply draws another UUID. The storage insert remains the
            # authoritative race-free check; a preflight alone cannot close a
            # cross-node collision window.
            ws_id = requested_ws_id or uuid.uuid4().hex
            effective_name = name or f"ws-{ws_id[:4]}"
            # Every deferred create needs a durable construction fence:
            # rollback, close and a storage clone must never delete or mutate
            # a row owned by a different in-flight creator. Deferred HTTP
            # creates remain in state=creating until commit publishes them.
            fork_reservation_token = uuid.uuid4().hex
            create_lane = self._acquire_open_lock(ws_id)
            create_lane.acquire()
            create_lane_released = False

            def _release_create_lane(
                lane: Any = create_lane,
                lane_ws_id: str = ws_id,
            ) -> None:
                nonlocal create_lane_released
                if create_lane_released:
                    return
                create_lane_released = True
                lane.release()
                self._release_open_lock(lane_ws_id)

            # Avoid allocating a UI or evicting an idle workstream for the
            # common caller-chosen collision case. The insert result below is
            # still the authoritative race-free reservation.
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
            except BaseException as exc:
                _release_create_lane()
                if not requested_ws_id and isinstance(exc, WorkstreamAlreadyExistsError):
                    continue
                raise

            # Persist before session construction. Fail-closed: if the row
            # can't be written, the in-memory session would be invisible to
            # any lazy-rehydrate path and show up as "missing" after restart.
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
                    required_node_id=required_node_id,
                )
                if inserted is False:
                    raise WorkstreamAlreadyExistsError(f"workstream {ws_id!r} already exists")
            except BaseException as exc:
                self._unwind_pending_create(ws, "register_failure")
                _release_create_lane()
                if not requested_ws_id and isinstance(exc, WorkstreamAlreadyExistsError):
                    continue
                raise

            # Lease the reservation before anything can build on it: the
            # constructor's config write and every later session write present
            # this fence.
            try:
                lease = self._acquire_lease(ws_id, fork_reservation_token, allow_creating=True)
                if lease is None:
                    raise RuntimeError(
                        f"workstream {ws_id!r} reservation vanished before its lease"
                    )
            except BaseException:
                self._unwind_pending_create(ws, "lease_failure")
                _release_create_lane()
                try:
                    self._storage.delete_workstream_if_fork_reserved(ws_id, fork_reservation_token)
                except Exception:
                    # The hidden creating row waits for stale-create recovery.
                    log.warning(
                        "session_mgr.create.lease_failure_delete_failed ws=%s",
                        ws_id[:8],
                        exc_info=True,
                    )
                raise
            with ws._lock:
                ws._lease = lease

            _release_create_lane()
            break

        built_session: Any | None = None
        try:
            if not lease.held:
                # Lost between the grant and the attach: the keeper reported it
                # before any slot held it, so nothing would retire this one.
                raise WorkstreamLeaseLostError(ws_id)
            session_kwargs = dict(extra_session_kwargs)
            session_kwargs["fork_reservation_token"] = fork_reservation_token
            session_kwargs["workstream_lease"] = lease
            try:
                built_session = self._adapter.build_session(
                    ws,
                    skill=skill,
                    model=model,
                    client_type=client_type,
                    **session_kwargs,
                )
            except WorkstreamLeaseHeldError as exc:
                if not lease.held:
                    raise
                # Nobody else can lease a creating row: an unfenced write the
                # constructor made met this create's own lease.
                raise SessionFactoryLeaseError() from exc
            built_session._fork_reservation_token = fork_reservation_token
            self._check_session_lease(ws, built_session)

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
        except BaseException:
            # Release the slot (Ctrl-C included) so capacity isn't leaked, and
            # call cleanup_ui on the placeholder so any listener/lock state
            # the UI factory allocated is released. The durable row is still
            # unpublished, so remove exactly that reservation too.
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
                deleted = False
                try:
                    deleted = bool(
                        self._storage.delete_workstream_if_fork_reserved(
                            ws_id,
                            fork_reservation_token,
                            lease=lease.fence,
                        )
                    )
                except Exception:
                    log.warning(
                        "session_mgr.failed_fork_create_cleanup ws=%s",
                        ws_id[:8],
                        exc_info=True,
                    )
                self._release_slot_lease(ws, row_deleted=deleted)
            raise

        if not defer_emit_created:
            try:
                committed = self.commit_create(ws)
            except BaseException:
                self.rollback_create(ws)
                raise
            if not committed:
                self.rollback_create(ws)
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

    def rollback_create(self, ws: Workstream) -> None:
        """Undo a create that was never published: free its slot and delete its row, exactly.

        Best effort. Covers an immediate publication that failed, and a create
        made with ``defer_emit_created=True`` that its caller decided not to
        commit.
        """

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
                    lease=self.own_row_fence(ws),
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
        the workstream should not be advertised at all. It releases the
        slot's owner lease too; a caller that also removes the persisted row
        does it in ``after_release`` or once this returns, because an
        unfenced delete is refused while the lease is live.

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
            # The caller's ``after_release`` usually deletes the unpublished
            # row without a fence, which storage refuses while a lease lives.
            self._release_slot_lease(candidate)
        if after_release is not None:
            after_release()
        return True

    # ------------------------------------------------------------------
    # open — lazy rehydrate for a persisted workstream
    # ------------------------------------------------------------------

    def open(self, ws_id: str) -> Workstream | None:
        """Rehydrate a persisted workstream on demand (see :meth:`open_with_outcome`)."""
        return self.open_with_outcome(ws_id)[0]

    def open_with_outcome(
        self,
        ws_id: str,
        *,
        _incarnation_attempt: int = 0,
    ) -> tuple[Workstream | None, bool]:
        """Rehydrate a persisted workstream on demand: ``(workstream, loaded_now)``.

        ``loaded_now`` is ``False`` when the workstream was already loaded
        here (another request won the race), so callers run their post-load
        work exactly once.

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
                        return None, False
                    existing = self._workstreams.get(ws_id)
                    if existing is not None and self._pending_creates.get(ws_id) is existing:
                        return None, False
                if existing is not None and existing.session is not None:
                    stopped = self._stopped_handle(existing)
                    if stopped is None:
                        return existing, False
                    # Its lease moved: retire the copy now (the id lane is this
                    # open's, reentrant) instead of serving one that cannot
                    # write, then open as usual, which names the new holder.
                    self._on_leases_lost([stopped])
                    with self._lock:
                        if self._workstreams.get(ws_id) is existing:
                            return existing, False

                # Bind every rehydrated object to the durable incarnation it
                # represents. Legacy tokenless rows are assigned a private
                # token atomically with this snapshot, so a later exact delete
                # can reject a stale endpoint snapshot before mutating a
                # same-id local successor.
                row = self._storage.ensure_workstream_incarnation_snapshot(ws_id)
                if row is None or row.get("kind") != self.kind:
                    return None, False
                # ``deleted`` is a tombstone — never resurrect.
                # ``closed`` IS resurrectable; the Saved Workstreams
                # landing makes restore an explicit user action, and
                # ``_reserve_and_install`` still enforces
                # max_active (evicting an idle peer or raising).
                if row.get("state") in {"creating", "deleted"}:
                    return None, False

                require_execution_node(row.get("required_node_id"), self._node_id)

                # Re-check + capacity admission happen inside the helper. The
                # per-id open lane prevents another opener for this id, while
                # the helper serializes any victim incarnation exactly.
                reservation_token = str(row.get("fork_reservation_token") or "")
                if not reservation_token:
                    raise RuntimeError(f"workstream {ws_id!r} incarnation snapshot has no token")
                # Lease before any slot is reserved, so a workstream another
                # process owns raises WorkstreamLeaseHeldError without evicting
                # an idle peer here. History loads after the grant, so nothing
                # the previous owner committed can be missed.
                lease = self._acquire_lease(ws_id, reservation_token)
                if lease is None:
                    # The snapshotted incarnation was replaced or retired.
                    return self._reopen_after_incarnation_race(ws_id, _incarnation_attempt, "lease")
                try:
                    ws, _evicted = self._reserve_and_install(
                        ws_id,
                        user_id=row.get("user_id") or "",
                        name=row.get("name") or f"ws-{ws_id[:4]}",
                        parent_ws_id=row.get("parent_ws_id"),
                        project_id=row.get("project_id"),
                        persona=row.get("persona") or "",
                        reservation_token=reservation_token,
                    )
                except BaseException:
                    self._release(lease)
                    raise
                with ws._lock:
                    ws._lease = lease

                try:
                    if not lease.held:
                        # Lost between the grant and the attach: the keeper
                        # reported it before any slot held it.
                        raise WorkstreamLeaseLostError(ws_id)
                    # Thread the persisted ``model_alias`` into
                    # ``build_session`` so reopened workstreams keep the
                    # model they were created with.  Pairs with the
                    # ``ChatSession.__init__`` skip-save guard: without
                    # both halves, ``_save_config`` clobbers persisted
                    # config with constructor defaults before
                    # ``ChatSession.rehydrate`` reads them back.  When
                    # ``model_validator`` is wired and the saved alias is
                    # no longer in the registry, drop it so the factory
                    # falls back to its default — the session_factory
                    # itself still raises on unknown aliases, since
                    # fresh-create paths want that to surface as a 503.
                    # Inside the unwind bracket, so a storage error here
                    # releases the slot and its lease; it raises as the
                    # session's own reads do (HTTP 503, retry shortly), never
                    # opening the workstream on defaults.
                    try:
                        saved_cfg = self._storage.load_workstream_config(ws_id)
                    except Exception as exc:
                        log.warning(
                            "session_mgr.config_read_failed ws=%s", ws_id[:8], exc_info=True
                        )
                        raise WorkstreamHistoryUnavailableError(ws_id) from exc
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

                    # Persona snapshot rides the same pre-construction lane as
                    # the saved alias: the constructor applies the four levers
                    # (tool merge, MCP gate, composition) inside __init__, so
                    # the stamp must land as a kwarg — rehydrate() is too late.
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
                    extra_build_kwargs: dict[str, Any] = {"workstream_lease": lease}
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

                        self._check_session_lease(ws, ws.session)
                        # A workstream with no turns opens empty, with its
                        # saved settings; a history read that fails raises
                        # WorkstreamHistoryUnavailableError (HTTP 503).
                        ws.session.rehydrate()

                        # ``rehydrate`` may restore a persisted alias that
                        # differs from the factory candidate (notably when an
                        # alias reappears between default construction and
                        # rehydrate). Validate the lane that will actually be
                        # returned.
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
                except BaseException:
                    # Build/rehydrate failures (Ctrl-C included) leave no
                    # usable session. Rehydrate can also have partially
                    # loaded history/config, so roll back the reserved slot
                    # and its lease and run the adapter's full UI cleanup.
                    # The raced-candidate replacement above is the only path
                    # that intentionally avoids cleanup_ui.
                    self._retire_rehydrate_slot(ws)
                    raise

                # Construction and resume perform multiple by-id storage reads outside the snapshot
                # transaction. A delete/re-register in that window would produce a hybrid object
                # (A's metadata/token with B's config/history). The lease taken above refuses other
                # processes' deletes while it is live, so only a lease that lapsed during the build
                # (renewal failing past the TTL) leaves this window open; re-read the private
                # incarnation witness anyway before this object is touched, advertised, or returned.
                # An openable successor gets a bounded retry from its own fresh snapshot; a deleted
                # or provisional row simply remains unavailable.
                try:
                    current_row = self._storage.ensure_workstream_incarnation_snapshot(ws_id)
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
                        return None, False
                    return self._reopen_after_incarnation_race(
                        ws_id, _incarnation_attempt, "witness"
                    )
                if not lease.held:
                    # The load found the lease gone (a refused fenced write
                    # marked it lost) on the same incarnation, checked just
                    # above: another process owns the workstream.
                    self._retire_rehydrate_slot(ws)
                    raise WorkstreamLeaseLostError(ws_id)

                # No DB state-flip on resurrect. The in-memory session
                # is IDLE; the DB row may still say 'closed' from the
                # last close(). The next set_state() call syncs it
                # naturally; writing 'idle' here could race a concurrent
                # close() that writes 'closed' under self._lock. The lease
                # taken above keeps the orphan reaper off this row however
                # old its ``updated``, which opening leaves alone.
                if self._event_emitter is not None:
                    self._event_emitter.emit_rehydrated(ws)
                return ws, True
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
            self._release_slot_lease(ws)

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
                self._release_slot_lease(candidate)
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
        delete_fn: ExactDeleteFn,
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
        delete_fn: ExactDeleteFn,
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

        ``delete_fn`` receives this manager's lease fence for a loaded
        incarnation (after its durability tail drained) and ``None``
        otherwise, so a workstream another process owns is refused with
        :class:`WorkstreamLeaseHeldError` instead of being deleted from under it.
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
            # No slot is keyed by this id, and an open or create holds this id
            # lane from its grant through the attach, so nothing here holds the
            # lease: the delete goes unfenced, refused while another process
            # holds a live one.
            deleted = delete_fn(lease=None)
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
                    # A stale local predecessor's lease cannot speak for the
                    # authorized successor row; that row is deleted unfenced.
                    deleted = delete_fn(
                        lease=None
                        if deleting_authorized_successor
                        else self.own_row_fence(candidate)
                    )

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
                self._release_slot_lease(candidate, row_deleted=True)
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
            owned_elsewhere = self._retire_failed_persisted_delete(candidate) is False
            if owned_elsewhere and not was_unadvertised:
                # The delete was refused because another process now owns the
                # row: this copy unloaded as on any lease loss (the
                # coordinator adapter drops its registry entry and row).
                self._announce_unload(candidate, False)

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

    def _retire_failed_persisted_delete(self, candidate: Workstream) -> bool | None:
        """Silently retire the exact object after a failed hard-delete.

        Returns ``None`` when the object was no longer here to retire, else
        whether its row was still ours (``False``: another process owns it).
        """
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
            return None
        try:
            self._adapter.cleanup_ui(candidate)
        except Exception:
            log.warning(
                "session_mgr.delete_persisted.failed_cleanup ws=%s",
                ws_id[:8],
                exc_info=True,
            )
        return self._release_slot_lease(candidate)

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
        """Soft-close one incarnation; ``True`` when this call unloaded it.

        That covers a workstream found owned by another process, which did
        not close: :meth:`close_with_outcome` tells the two apart.
        """
        return self.close_with_outcome(ws_id) in (CloseOutcome.CLOSED, CloseOutcome.OWNED_ELSEWHERE)

    def close_with_outcome(self, ws_id: str) -> CloseOutcome:
        """Soft-close on the stable per-id lane and retain refusal detail."""
        with self._id_lifecycle(ws_id):
            return self._close_serialized(ws_id)

    def _close_serialized(self, ws_id: str) -> CloseOutcome:
        """Soft-close: unload from memory + mark state=closed in storage.

        The detailed result lets HTTP callers distinguish an unresolved
        durability conflict from bounded cleanup that has not finished yet.
        """
        with self._lock:
            ws = self._workstreams.get(ws_id)
            if ws is None:
                return CloseOutcome.NOT_FOUND
            if (
                ws._create_publication_active
                and ws._create_publication_thread == threading.get_ident()
            ):
                return CloseOutcome.NOT_FOUND

        with ws._lifecycle_lock:
            with self._lock:
                if self._workstreams.get(ws_id) is not ws:
                    return CloseOutcome.NOT_FOUND
            # Close admission must become terminal to dispatch before the
            # session fence begins. ``prepare_soft_close`` may wait for an
            # admitted durability batch; leaving ``_closed`` false across that
            # wait lets a racing session_worker claim a fresh slot and report
            # the send accepted even though ChatSession will reject its later
            # generation claim. Both sides serialize on ``ws._lock``, making
            # this the linearization point for close versus dispatch.
            with ws._lock:
                if ws._closed:
                    return CloseOutcome.NOT_FOUND
                ws._closed = True
                ws._state_revision += 1

            prepared = False
            try:
                if not _session_prepare_soft_close(ws.session):
                    # The first failed fence wins the response classification.
                    # Structural debt means a cancelled turn still needs to
                    # journal its terminal TOOL receipts. Otherwise preserve
                    # the historical durability-conflict classification when
                    # the accepted conversation row is still unresolved.
                    if _session_has_tool_structural_debt(ws.session):
                        outcome = CloseOutcome.CLEANUP_PENDING
                    elif _session_has_unresolved_persistence(ws.session):
                        outcome = CloseOutcome.UNRESOLVED_PERSISTENCE
                    else:
                        outcome = CloseOutcome.CLEANUP_PENDING
                    log.warning(
                        "session_mgr.close_refused ws=%s reason=%s",
                        ws_id[:8],
                        outcome.value,
                    )
                    return outcome
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
                    return CloseOutcome.NOT_FOUND
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
            ours = self._finish_soft_close(
                ws, delete_unadvertised_fork=was_unadvertised and bool(ws._fork_reservation_token)
            )
            if not was_unadvertised:
                self._announce_unload(ws, ours)
            return CloseOutcome.CLOSED if ours else CloseOutcome.OWNED_ELSEWHERE

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
        fence = self.own_row_fence(ws)
        if self._state_writer is not None:
            self._state_writer.record(
                ws.id,
                state.value,
                flush_now=(state is WorkstreamState.ERROR),
                incarnation=ws._state_incarnation,
                lease=fence,
            )
            return
        try:
            self._storage.update_workstream_state(ws.id, state.value, lease=fence)
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

    def _persist_closed_state(self, ws: Workstream) -> bool:
        """Write the terminal row after all predecessor tails finish.

        Returns ``False`` when the row turned out to belong to another process
        (see :meth:`_write_closed_row_locked`).
        """
        try:
            with ws._state_tail_lock:
                return self._write_closed_row_locked(ws)
        finally:
            self._release_state_tail(ws)

    def _write_closed_row_locked(self, ws: Workstream) -> bool:
        """Tombstone buffered state, write ``closed`` and drop the override.

        The caller holds ``ws._state_tail_lock`` and owns its release. The
        write presents the slot's fence (a lost one makes it refused).
        Returns ``False``, leaving the override, when storage refused the
        write: the row is no longer this copy's to close, because another
        process took it over (the workstream lives on there) or a
        maintenance pass closed and fenced it out after this copy's lease
        lapsed.
        """
        if self._state_writer is not None:
            self._state_writer.discard(
                ws.id,
                tombstone=True,
                incarnation=ws._state_incarnation,
            )
        try:
            self._storage.update_workstream_state(ws.id, "closed", lease=self.own_row_fence(ws))
        except (WorkstreamLeaseLostError, WorkstreamLeaseHeldError):
            log.info("session_mgr.closed_row_owned_elsewhere ws=%s", ws.id[:8])
            return False
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
        return True

    def _announce_unload(self, ws: Workstream, ours: bool, *, reason: str = "") -> None:
        """Announce that ``ws`` unloaded here: ``ws_closed`` only while its row was ours.

        A workstream another process took over did not close; announcing it
        would drop it from dashboards and panes while it is live there. The
        emitter's lease-retired hook runs instead, also when maintenance closed
        the row after this copy's lease lapsed.
        """
        emitter = self._event_emitter
        if emitter is None:
            return
        if not ours:
            log.info("session_mgr.unload_owned_elsewhere ws=%s", ws.id[:8])
            try:
                emitter.on_lease_retired(ws)
            except Exception:
                log.warning("session_mgr.lease_retire_hook_failed ws=%s", ws.id[:8], exc_info=True)
            return
        if reason:
            emitter.emit_closed(ws.id, reason=reason, name=ws.name)
        else:
            emitter.emit_closed(ws.id, name=ws.name)

    def _delete_unadvertised_fork(self, ws: Workstream) -> bool:
        """Delete a pending fork only while its durable fence is still ours."""
        deleted = False
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
                        lease=self.own_row_fence(ws),
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
        return bool(deleted)

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
        a process death. The backend owns the atomic state/age/lease/incarnation
        check and complete dependent cleanup; this layer supplies the current
        manager snapshot.

        A creator leases its reservation as soon as it registers it, so a
        reservation whose creator is alive anywhere is never eligible, while a
        crashed creator's lease expires and its rows become reclaimable
        whatever node id the restarted process carries. Every workstream
        presently loaded by this manager, including pending creates, is
        excluded, and the age grace protects a create admitted just after the
        snapshot.

        Storage uncertainty fails closed and returns no ids.
        """
        with self._lock:
            loaded = list(self._workstreams.keys())
        cutoff = (datetime.now(UTC) - timedelta(seconds=max_age_seconds)).strftime(
            "%Y-%m-%dT%H:%M:%S"
        )
        try:
            reaped = self._storage.delete_stale_creating_reservations(self.kind, cutoff, loaded)
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

    def reconcile_unresolved_persistence(
        self,
        *,
        now: float | None = None,
        blocking: bool = False,
    ) -> list[str]:
        """Attempt every due transient conversation repair without manager locks.

        ``blocking`` selects the probe discipline.  The default non-blocking
        probe suits the per-second maintenance sweep: a workstream whose
        generation/handoff locks are momentarily held is skipped and re-probed
        on the next pass, so the steady-state walk never contends the locks
        turn commits use.  A ONE-SHOT caller has no next pass —
        ``_reserve_and_install`` runs this exactly once as a last-chance
        repair before refusing a create — and must force a definite answer,
        or the workstreams most likely to be skipped (the same contended ones
        that emptied its candidate list) never get their due repair and the
        create fails.  Only safe from callers holding no workstream or
        manager lock.

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
            if blocking:
                unresolved = _session_has_unresolved_persistence(session)
            else:
                probed = _session_unresolved_persistence_nowait(session)
                if probed is None:
                    # Probe contended: another caller holds the generation/
                    # handoff locks this instant.  Nothing on this sweep is
                    # due enough to block a live commit for — the next
                    # one-second pass re-probes.  One-shot callers pass
                    # ``blocking`` instead; for them there is no next pass.
                    continue
                unresolved = probed
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

          **Liveness scoping**: a workstream loaded by any live process
          carries a renewed owner lease, and the backend never closes a
          row whose lease is live, so rows another node has loaded stay
          protected however their ``node_id`` was stamped.  A crashed
          process stops renewing; its leases expire and its rows fall
          through to the reap, whatever node id the next process
          carries.

        Returns the combined list of closed ws_ids (in-memory first,
        then DB orphans).  Pass 1 emits ``ws_closed``, except for a copy
        found owned by another process, which is unloaded and announced
        as unloaded (its id is still returned); pass 2 does not, because
        never-loaded rows have no SSE listeners expecting them.

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
        # before the DB call. Rows other live processes have loaded are
        # protected by their owner leases (see the docstring).
        with self._lock:
            loaded = list(self._workstreams.keys())
        cutoff = (datetime.now(UTC) - timedelta(seconds=max_age_seconds)).strftime(
            "%Y-%m-%dT%H:%M:%S"
        )
        orphans: list[str] = []
        try:
            orphans = self._storage.bulk_close_stale_orphans(self.kind, cutoff, loaded)
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

            self._announce_unload(ws, self._finish_soft_close(ws))
            return True

    def _finish_soft_close(self, ws: Workstream, *, delete_unadvertised_fork: bool = False) -> bool:
        """Tear down a slot already removed from tracking: UI, durable close, lease.

        Writes ``closed`` (or deletes a pending fork that was never advertised),
        then releases the lease, even when the write raised: the slot is gone.
        Returns whether the row was still this process's to close.
        """
        ours = True
        deleted = False
        try:
            self._adapter.cleanup_ui(ws)
        finally:
            try:
                if delete_unadvertised_fork:
                    deleted = self._delete_unadvertised_fork(ws)
                else:
                    ours = self._persist_closed_state(ws)
            finally:
                released_ours = self._release_slot_lease(ws, row_deleted=deleted)
        return ours and released_ours

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    def loaded(self, ws_id: str) -> Workstream | None:
        """The slot for ``ws_id`` when it is loaded here and can still write.

        ``None`` while its session is still being built (an open in flight)
        or once its lease was lost (another process owns the workstream and
        the slot is about to retire): callers then go through
        :meth:`open_with_outcome`.
        """
        ws = self.get(ws_id)
        if ws is None or ws.session is None or self._stopped_handle(ws) is not None:
            return None
        return ws

    @staticmethod
    def _stopped_handle(ws: Workstream) -> WorkstreamLease | None:
        """The slot's handle once it no longer owns its row, else ``None``."""
        with ws._lock:
            lease = ws._lease
        return lease if lease is not None and lease.state == "lost" else None

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
                    # One shot, and the latch below makes it the only one:
                    # force a definite probe rather than skipping the
                    # contended sessions that are the likeliest reason this
                    # candidate list came back empty.  No lock is held here.
                    self.reconcile_unresolved_persistence(blocking=True)
                    continue
                raise SessionCapacityError(self._max_active)

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

    def _unwind_pending_create(self, ws: Workstream, stage: str) -> None:
        """Drop a create's pending placeholder before its session exists."""
        with self._lock:
            self._remove_locked(ws.id)
        try:
            self._adapter.cleanup_ui(ws)
        except Exception:
            log.warning(
                "session_mgr.create.cleanup_failed stage=%s ws=%s",
                stage,
                ws.id[:8],
                exc_info=True,
            )

    def _reopen_after_incarnation_race(
        self, ws_id: str, attempt: int, phase: str
    ) -> tuple[Workstream | None, bool]:
        """Reopen from a fresh snapshot after the incarnation changed under ``open``."""
        if attempt + 1 >= self._REHYDRATE_INCARNATION_ATTEMPTS:
            raise RuntimeError(
                f"workstream incarnation changed repeatedly while reopening {ws_id!r}"
            )
        log.warning(
            "session_mgr.rehydrate_incarnation_raced ws=%s attempt=%d phase=%s",
            ws_id[:8],
            attempt + 1,
            phase,
        )
        return self.open_with_outcome(ws_id, _incarnation_attempt=attempt + 1)

    # ------------------------------------------------------------------
    # Internal — owner lease
    # ------------------------------------------------------------------

    @staticmethod
    def own_row_fence(ws: Workstream) -> LeaseFence | None:
        """The fence for writes to ``ws``'s row, by this manager or its host.

        Derived from the slot's one handle, whatever its state: a lost or
        released handle makes the write refused, never unfenced, so it keeps
        working after the slot unloads. Before the lease is attached, writes
        go unfenced. :meth:`lease_fence` instead looks a workstream up by id
        and returns ``None`` once nothing here holds it.
        """
        with ws._lock:
            lease = ws._lease
        return lease.fence if lease is not None else None

    def _acquire_lease(
        self,
        ws_id: str,
        token: str,
        *,
        allow_creating: bool = False,
    ) -> WorkstreamLease | None:
        """Acquire this manager's owner lease on one exact incarnation.

        Returns ``None`` when that incarnation is gone and raises
        :class:`WorkstreamLeaseHeldError` while another process owns a live
        lease. Every earlier handle for the id was released when its slot
        left, so the keeper holds none for it.
        """
        try:
            grant = self._storage.acquire_workstream_lease(
                ws_id,
                incarnation_token=token,
                holder=self._lease_holder,
                node_id=self._node_id,
                ttl_seconds=self._lease_keeper.ttl_seconds,
                allow_creating=allow_creating,
            )
        except WorkstreamLeaseHeldError as exc:
            record_lease_event("conflict")
            log.debug(
                "session_mgr.lease_conflict ws=%s holder_node=%s retry_after_ms=%d",
                ws_id[:8],
                exc.holder_node_id,
                exc.retry_after_ms,
            )
            raise
        if grant is None:
            return None
        lease = WorkstreamLease(grant.fence)
        record_lease_event("acquired")
        if grant.previous_holder:
            record_lease_event("takeover")
            log.warning(
                "session_mgr.lease_takeover ws=%s previous_holder=%s previous_node=%s "
                "live=%s epoch=%d",
                ws_id[:8],
                grant.previous_holder,
                grant.previous_node_id,
                grant.took_over_live,
                lease.epoch,
            )
        self._lease_keeper.track(lease)
        return lease

    def _release(self, lease: WorkstreamLease, *, row_gone: bool = False) -> _Release:
        """Release one acquisition after its last durable write (best effort).

        ``row_gone`` says the caller just deleted the row this lease names, so
        there is nothing left in storage to release.
        """
        self._lease_keeper.untrack(lease)
        if not lease.mark_released():
            return _Release.NOT_OURS if lease.state == "lost" else _Release.RELEASED
        if not row_gone:
            try:
                released = self._storage.release_workstream_lease(lease.fence)
            except Exception:
                # The lease expires on its own; until then the row reads as owned.
                log.debug("session_mgr.lease_release_failed ws=%s", lease.ws_id[:8], exc_info=True)
                return _Release.UNREACHABLE
            if not released:
                return _Release.NOT_OURS
        record_lease_event("released")
        log.debug("session_mgr.lease_released ws=%s epoch=%d", lease.ws_id[:8], lease.epoch)
        return _Release.RELEASED

    def _release_slot_lease(self, ws: Workstream, *, row_deleted: bool = False) -> bool:
        """Release the slot's lease after its last durable write.

        The handle stays, so a late write presents its now released fence and
        is refused instead of landing unfenced. Returns ``False`` when the row
        turned out to be another process's (the handle was lost, or storage
        found another fence); an unknown outcome counts as ours.
        ``row_deleted`` says the caller just deleted the row.
        """
        with ws._lock:
            lease = ws._lease
        if lease is None:
            return True
        return self._release(lease, row_gone=row_deleted) is not _Release.NOT_OURS

    @staticmethod
    def _slot_holds(ws: Workstream, lease: WorkstreamLease) -> bool:
        with ws._lock:
            return ws._lease is lease

    @staticmethod
    def _check_session_lease(ws: Workstream, session: Any) -> None:
        """Check ``session`` writes with the slot's lease.

        A session factory that drops ``workstream_lease`` builds a session whose
        every write goes out unfenced, which storage refuses while this manager
        holds the row: the create or open fails here, rather than the session
        stopping at its first save as if another process owned the workstream.
        """
        with ws._lock:
            lease = ws._lease
        write_fence = concrete_method(session, "write_fence")
        if lease is not None and write_fence is not None and write_fence() != lease.fence:
            raise SessionFactoryLeaseError()

    def _on_leases_lost(self, leases: list[WorkstreamLease]) -> None:
        """Act on handles that no longer match their rows: stop every copy, then retire each.

        The slot (or retained failed-delete tombstone) holding a handle
        retires. Every session stops before any retirement waits for its id
        lane and teardown, so each notice reaches viewers (the CLI prompt)
        while the slot is still in front of them, however many losses arrived
        together. A handle no slot holds (an open or create between the grant
        and the attach) is ignored: the keeper already stopped renewing it,
        and that open or create finds it lost before its slot takes it.
        """
        holders: list[tuple[Workstream, WorkstreamLease]] = []
        for lease in leases:
            with self._lock:
                # A slot only ever holds its own workstream's lease.
                candidates = [
                    self._workstreams.get(lease.ws_id),
                    self._failed_delete_tombstones.get(lease.ws_id),
                ]
            for ws in candidates:
                if ws is not None and self._slot_holds(ws, lease):
                    holders.append((ws, lease))
                    break
        for ws, lease in holders:
            self._stop_session_for_lost_lease(ws, lease)
        for ws, lease in holders:
            try:
                self._retire_lost_lease(ws, lease)
            except Exception:
                log.warning("session_mgr.lease_retire_failed ws=%s", ws.id[:8], exc_info=True)

    @staticmethod
    def _stop_session_for_lost_lease(ws: Workstream, lease: WorkstreamLease) -> None:
        """Tell ``ws``'s session, if it has one yet, that its lease is lost (idempotent)."""
        session = ws.session
        stop = (
            concrete_method(session, "handle_workstream_lease_lost")
            if session is not None
            else None
        )
        if stop is None:
            return
        try:
            stop(lease)
        except Exception:
            log.warning("session_mgr.lease_retire_notify_failed ws=%s", ws.id[:8], exc_info=True)

    def _retire_lost_lease(self, ws: Workstream, lease: WorkstreamLease) -> None:
        """Unload ``ws``: its workstream now belongs to another process.

        No durable write (the row belongs to the new owner), no override
        delete and no ``ws_closed`` (the workstream did not close): the
        emitter's lease-retired hook announces the unload instead. Open panes
        receive the per-workstream closed sentinel and reconnect through the
        router, which leads them to the new owner. A pending create is left to
        its creator, which still owns the rollback.
        """
        with self._id_lifecycle(ws.id), ws._lifecycle_lock:
            with self._lock:
                tracked = self._workstreams.get(ws.id) is ws
                tombstoned = self._failed_delete_tombstones.get(ws.id) is ws
                if not (tracked or tombstoned) or self._pending_creates.get(ws.id) is ws:
                    return
                ws._lifecycle_terminal_active = True
                self._retain_state_tail_locked(ws)
            with ws._lock:
                ws._closed = True
                ws._state_revision += 1
            try:
                try:
                    with ws._state_tail_lock:
                        if self._state_writer is not None:
                            self._state_writer.discard(
                                ws.id,
                                tombstone=True,
                                incarnation=ws._state_incarnation,
                            )
                except Exception:
                    # A buffered state write would only meet the new owner's
                    # fence: never let it strand the retirement.
                    log.warning(
                        "session_mgr.lease_retire_state_tail_failed ws=%s",
                        ws.id[:8],
                        exc_info=True,
                    )
                with self._lock:
                    if self._workstreams.get(ws.id) is ws:
                        self._workstreams.pop(ws.id, None)
                        if ws.id in self._order:
                            self._order.remove(ws.id)
                        if self._active_id == ws.id:
                            self._active_id = self._first_visible_id_locked()
                    self._drop_delete_tombstone_locked(ws.id, candidate=ws)
                # Again here: an open in flight can attach its session after
                # ``_on_leases_lost`` found none to stop.
                self._stop_session_for_lost_lease(ws, lease)
                try:
                    self._adapter.cleanup_ui(ws)
                except Exception:
                    log.warning(
                        "session_mgr.lease_retire_cleanup_failed ws=%s",
                        ws.id[:8],
                        exc_info=True,
                    )
                self._release_slot_lease(ws)
            finally:
                self._release_state_tail(ws)
            # The row moved with the lease: the workstream lives on elsewhere.
            # Announced inside the id lane, as close does, so a reopen here
            # cannot land before this copy's retirement is told.
            self._announce_unload(ws, False)
        log.warning("session_mgr.lease_retired ws=%s epoch=%d", ws.id[:8], lease.epoch)

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
            # Eviction writes nothing durable: releasing the lease is what
            # tells whether the row was still ours to announce.
            ours = self._release_slot_lease(ws)
            if self._event_emitter is not None:
                try:
                    self._announce_unload(ws, ours, reason="evicted")
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
