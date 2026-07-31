"""Observer that nudges idle coordinators with unfinished work.

Subscribes to a coordinator-side :class:`SessionManager`'s state events
and enqueues one of two nudges on the coord's :class:`NudgeQueue` when a
coord transitions to :class:`WorkstreamState.IDLE`.  The
:class:`turnstone.core.idle_nudge_watcher.IdleNudgeWatcher` (registered
*after* this observer in the lifespan, so subscriber-order has the
observer fire first on the same IDLE event) then peeks the queue and
dispatches the wake send.

Two nudge types, in two different **classes**:

* ``idle_children`` — LIVENESS.  The coord went idle with active
  interactive children; the wake exists so their results are collected
  instead of abandoned.  Suggests ``wait_for_workstream``.
* ``idle_tasks`` — ADVICE.  The coord went idle holding open
  (``pending`` / ``in_progress``) entries on its own ``tasks`` list.
  Suggests reconciling the list, with the operator-escalation branch
  first.

The classes are INDEPENDENT: both conditions can hold in one IDLE
event, and both nudges then fire and CO-DELIVER in one drain, tasks
first — the grooming instruction is instant, the park instruction is
open-ended, so the batch ends on the wait.  Each nudge asserts only its
own domain: the tasks body never claims the children are gone — it
renders one OBSERVED fact line per child row in a live state (still
running, or stopped with the wait's immediate return noted), because
the liveness nudge can be blocked by its own cap or wait gate while
advice fires alone, and on a coordinator with no children it says
nothing about children at all.  Either way a consistent pair is two
true statements, not a contradiction.  One ordering caveat, accepted:
a cross-bracket pair (an older queued ``idle_children`` surviving into
a bracket that enqueues ``idle_tasks``) delivers children-first by
seq; both entries are still predicate-valid, so that is a tuning
miss, not a correctness one.  If small models measurably fumble even
the ordered pair, the named upgrade path is a single combined
checkpoint type selected at produce time — do NOT reintroduce a
cross-domain fire gate.

The class decides three behaviours, each ruled at its site:

* **Config gating.**  LIVENESS is never gated on ``memory.nudges`` —
  that switch's operator-facing help text promises memory-reminder
  control, and suppressing the wake would strand a coordinator whose
  children finish unobserved (see ``_plan_children``).  ADVICE
  gates through ``ChatSession._nudges_enabled``, which also suppresses
  the nudge when the persona envelope hides the ``tasks`` tool its body
  instructs (``NUDGE_REQUIRED_TOOL``).
* **Per-type caps, per-class cooldown** (:data:`_NUDGE_TYPE_CAPS` /
  :data:`_NUDGE_TYPE_HAS_COOLDOWN`) — ONE fire per type per idle
  bracket for both classes, re-armed by a real (non-wake) send.
  LIVENESS is cap-only: it has a single exit (enqueue → wake →
  delivered), so repeat fires would only re-prompt a model that
  already read the body, at one autonomous turn each.  ADVICE
  additionally carries ``memory.nudge_cooldown``, because advice is
  the class that can spam across brackets.  Both classes may fire in
  one bracket, so a drain carries at most 2 bodies.
* **Drain-predicate scope.**  Each predicate re-validates ONLY its
  own assertion.  The advice predicate never reads children at all: it
  drops when no open task remains, when a ``needs_user`` row parks the
  class, or when the envelope cannot be read.  The liveness predicate
  re-reads its own children question and drops when the answer is "no
  active children" — and when the read FAILS: a failed storage read
  never delivers a nudge, at drain exactly as at the fire gate.
  Dropping an already-charged entry on a read failure wastes that fire
  (nothing retries behind a dropped wake), and that cost is accepted —
  the fail-closed rule is unconditional and outranks the lost-wake
  pricing that once made this predicate deliver on an unknown read.

Gate order, ``idle_children`` (cheap → expensive; matches the code):
coordinator-kind → cap + cooldown peek → wait-tool skip →
children query (``[]`` → no fire; a FAILED read fails the whole
EVENT closed) → permission check (``nudge_allowed``) → atomic
charge → enqueue → record.

Gate order, ``idle_tasks``: coordinator-kind → operator-Stop gate
(``_generation_abandoned``) → ``_nudges_enabled`` (config +
``tasks``-tool visibility; a raise out of this config read fails the
EVENT closed) → cap + cooldown peek → asked-operator skip →
wait-tool skip → envelope read (corrupt → no fire; a FAILED read
fails the EVENT closed) →
live-child ``(ws_id, state)`` read (``[]``/rows → BODY CONTENT.  It
runs after every gate it could otherwise perturb, and what it FINDS
never changes whether this path fires — only a read that FAILS does,
and that silences the event, not just this path) → permission check
(``nudge_allowed``) → atomic charge → enqueue → record.

ONE rule spans both paths, from the owner and unqualified: these
nudges must not fire if a storage read fails.  ``_on_idle`` therefore
runs each path as a PLAN step (gates, reads, body — no side effects)
and COMMITS — permission check, atomic charge, enqueue, record — only
after both plans returned without a failed read.  A storage-backed
read that raises in either path silences BOTH classes for the event
and charges NEITHER cap; a transient failure costs neither class its
bracket, and the next IDLE event with working reads fires normally.
This is NOT the forbidden cross-domain fire gate: it keys on a read
FAILING, never on what a read FOUND, and no answer either read
returns can gate the other path.  Path FAULTS keep their separate
exception domains — a bug in one path's own machinery still costs
only that path's fire (see ``_on_idle``).

``_on_idle`` plans and commits the tasks path BEFORE the children
path so a same-event pair carries ascending seq in tasks-first
order — every drain path delivers in seq order, which is what makes
the ordering ruling hold with no queue changes.

Caps reset when the ws leaves IDLE for a non-wake reason (tracked by
``ChatSession._wake_source_tag``).  That state is per-process — if one
coordinator were ever live in two console processes the counters would
disagree across them (pre-existing shape, shared with every other
per-process cache).
"""

from __future__ import annotations

import contextlib
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeVar

from turnstone.console.coordinator_client import TASK_OPEN_STATUSES, load_task_envelope
from turnstone.core.log import get_logger
from turnstone.core.metacognition import (
    _cooldown_allows,
    field_str,
    format_idle_children_nudge,
    format_idle_tasks_nudge,
    nudge_allowed,
    record_nudge,
)
from turnstone.core.nudge_queue import WAKE_CHANNEL
from turnstone.core.trajectory import Role
from turnstone.core.workstream import WorkstreamKind, WorkstreamState

if TYPE_CHECKING:
    from collections.abc import Callable

    from turnstone.core.nudge_queue import NudgeQueue
    from turnstone.core.session import ChatSession
    from turnstone.core.session_manager import SessionManager
    from turnstone.core.storage._protocol import StorageBackend
    from turnstone.core.trajectory import Turn
    from turnstone.core.workstream import Workstream

log = get_logger(__name__)

# Active = the model can act on the child (it's still working,
# streaming, or waiting on user attention).  Excludes "idle" (the
# child is now waiting and can't be unblocked by the coord), "closed"
# (gone), "deleted" (gone), and "error" (the model can't unblock an
# errored child without operator intervention; the per-bracket cap
# handles repeat fires for stuck-error children).
_ACTIVE_CHILD_STATES: frozenset[str] = frozenset(
    {
        WorkstreamState.THINKING.value,
        WorkstreamState.RUNNING.value,
        WorkstreamState.ATTENTION.value,
    }
)

# LIVE = the row still describes a child this coordinator has.  A
# different question from the one above — "can the model act on it?" —
# and the one the tasks body's children fact lines speak about, so it
# is a strict superset: ``idle`` is in it precisely because an idle
# child may hold results nobody collected — the stopped-child fact
# line exists for exactly that row — and ``error`` is in it because an
# errored child still owns the work it was given (and is stopped in
# the same wait-terminal sense).
#
# Enum-derived rather than typed out.  ``WorkstreamState`` is exactly
# {idle, thinking, running, attention, error}, and the terminal strings
# the close and reap paths write — ``closed``, and the ``deleted``
# tombstone that today has readers but no writer — are NOT members, so
# "a row in a live state" needs no exclusion list to maintain and a
# state added to the enum joins this set with no edit here.
_LIVE_CHILD_STATES: frozenset[str] = frozenset(s.value for s in WorkstreamState)

# ONE fire per type per idle bracket.
#
# Keyed per TYPE, not a shared total: advice must never be able to
# spend the liveness budget, or a coord that used its wake on a task
# reminder reaches the silent-stall state (live children, no wake left)
# sooner than before ``idle_tasks`` existed.  Both classes may fire in
# one bracket (co-delivery), so the per-bracket ceiling is 2 bodies.
#
# The re-arm is a real (non-wake) send: ``_reset_caps_for`` clears the
# counters whenever the coord leaves IDLE without ``_wake_source_tag``
# set — the operator, a second participant on a shared workstream, an
# API-driven producer.  For LIVENESS, N real sends re-arming N fires is
# RULED DESIGN, not a defect: each re-arm is a genuine send, each fire
# is a fresh "you went idle again holding live children" bracket, and
# skipping any one of them is the silent stall this class exists to
# prevent.  The class that must NOT ride that re-arm rate is ADVICE,
# which is why cooldown is a per-CLASS property — see
# :data:`_NUDGE_TYPE_HAS_COOLDOWN` below.
#
# Every nudge type this observer emits MUST be registered here — the
# lookup KeyErrors on an unregistered type (surfacing via the path
# handlers' ``log.exception`` in ``_on_idle`` and any test that drives
# the new path) rather than inheriting either class's budget silently.
# Fail-loud is the point: defaulting a future liveness type to the
# advice budget (or vice versa) is exactly the misclassification this
# table exists to force a decision on.
_NUDGE_TYPE_CAPS: dict[str, int] = {
    "idle_children": 1,
    "idle_tasks": 1,
}

# Cooldown is a per-CLASS property.
#
# LIVENESS (``idle_children``) is cap-only — no cooldown, deliberately.
# An idle nudge has exactly ONE exit: enqueueing it makes the watcher
# dispatch a wake, and that wake delivers it.  Unlike the memory-class
# advisories — queued mid-turn to wait for whichever seam arrives next,
# so repeat fires buy extra chances at DELIVERY — there is no second
# seam to wait for here, and every re-arm is genuine progress (a real
# send, per the ruling above).  A wall-clock damper on the wake would
# only convert some of those brackets into silent stalls.
#
# ADVICE (``idle_tasks``) carries ``memory.nudge_cooldown`` (default
# 300 s; ``0`` remains the operator's opt-out).  Advice is the class
# that can spam: the cap re-arms on EVERY real send, so a coordinator
# receiving a stream of machine-driven messages while holding open
# tasks would otherwise be re-prompted once per message.  The stamp
# lives in ``session._metacog_state``, which ``_reset_caps_for`` never
# touches — the window deliberately SPANS bracket re-arms, which is the
# cross-bracket damping the cap alone cannot express.
#
# Same fail-loud registration rule as the cap table above: the lookup
# KeyErrors on an unregistered type, so a new type must declare its
# class here rather than inherit either behaviour silently.
_NUDGE_TYPE_HAS_COOLDOWN: dict[str, bool] = {
    "idle_children": False,
    "idle_tasks": True,
}


class _StorageReadError(Exception):
    """A storage-backed read one of the nudge paths depends on could not
    be served.

    Raised inside the PLAN phase and caught by ``_on_idle``, where it
    fails the whole event closed: neither nudge is queued and neither
    type's cap is charged.  Deliberately distinct from an ordinary path
    FAULT (any other exception escaping a path), which stays isolated to
    its own path — see the failure-domain comment in ``_on_idle``.
    ``read`` names the failed read for the event-level log line.
    """

    def __init__(self, read: str) -> None:
        super().__init__(read)
        self.read = read


_ReadResultT = TypeVar("_ReadResultT")


def _event_read(ws_id: str, read: str, fn: Callable[[], _ReadResultT]) -> _ReadResultT:
    """Run one storage-backed gate read under the event's fail-closed
    rule: a raise becomes :class:`_StorageReadError`, which ``_on_idle``
    turns into "neither nudge fires, neither cap is charged".

    Only READS run under this wrapper — the session's config-store
    lookups and the envelope fetch.  The two children queries carry
    their own internal ``try`` (they must also classify ragged rows) and
    signal failure by returning ``None``, which their call sites convert
    to the same exception.  Anything else a path does is a path FAULT
    when it raises, and stays isolated to that path — wrapping
    non-reads here would quietly widen the event veto into the shared
    failure domain the paths must not have.
    """
    try:
        return fn()
    except Exception as exc:
        log.debug(
            "coord_idle_observer.read_raised read=%s ws=%s",
            read,
            ws_id[:8],
            exc_info=True,
        )
        raise _StorageReadError(read) from exc


@dataclass(frozen=True)
class _NudgePlan:
    """One path's fully-computed intent to fire — built by a plan
    function, committed by ``CoordinatorIdleObserver._commit_plan`` only
    after BOTH paths planned without a failed storage read.

    Everything the commit needs is captured here, so the commit itself
    performs no storage-backed read: the event-level fail-closed
    decision cannot be invalidated by a read failing between the veto
    point and the enqueue.  ``cooldown_secs`` is the per-class value the
    gate head computed; re-reading it at commit would be one more
    config-store round-trip on the wrong side of the veto.  ``log_msg``
    / ``log_args`` carry the per-type success line so the commit tail
    stays one implementation for both types.
    """

    ws_id: str
    nudge_type: str
    text: str
    metadata: dict[str, Any] | None
    valid_until: Callable[[], bool]
    cooldown_secs: int
    log_msg: str
    log_args: tuple[object, ...]


# Soft cap on the snapshot query.  Higher than ``WAIT_MAX_WS_IDS`` so
# the SQL ``LIMIT`` (applied before the Python state filter) doesn't
# clip genuinely-active children whose ``updated`` timestamp is older
# than recently-closed siblings.  Realistic coord histories are far
# smaller than this; if a coord ever exceeds it, the formatter still
# truncates to ``WAIT_MAX_WS_IDS`` for the model-facing suggestion.
_ACTIVE_CHILDREN_QUERY_LIMIT = 200


class _DrainMemo(threading.local):
    """Per-thread, per-drain-pass memo for the liveness predicate's
    children answer.

    The LIVENESS predicate reads the children state through one memoised
    call.  Liveness entries ACCUMULATE ACROSS BRACKETS — the cap allows
    one fire per bracket and a real (non-wake) leave-IDLE re-arms it —
    so several can sit queued at once, and ``NudgeQueue.drain_entries``
    evaluates each ``valid_until`` independently.  Unmemoised, two reads
    microseconds apart can disagree (one raising → drop, fail-closed;
    the next succeeding against live children → deliver), which silently
    drops one wake while delivering its same-class sibling.  Same-pass
    entries must share one answer, whatever it is — one observation per
    drain pass keeps them coherent, and N queued entries cost one query,
    not N.

    The PASS is the scope — not a wall-clock TTL.  A TTL was wrong in
    both directions: a read slower than the TTL wrote an already-expired
    entry (no sharing exactly when storage is slow, the regime that
    produces the disagreement), and an entry younger than the TTL leaked
    a previous pass's answer into a later one — dropping a
    freshly-enqueued liveness entry on a stale ``False`` with its cap
    slot already spent, the lost wake this class exists to prevent.  The
    memo key is ``(ws_id, NudgeQueue.drain_pass())``: identical for
    every predicate one ``drain_entries`` call runs, different for the
    next call, no clock anywhere.  ``ws_id`` stays in the key because
    two queues' pass counters can coincide numerically.

    Thread-local because a pass evaluates its predicates sequentially on
    the draining thread: concurrent passes (necessarily different
    queues) each get their own slot, a dead thread's slot dies with it,
    and there is nothing to prune or lock.
    """

    key: tuple[str, int] | None = None
    answer: bool | None = None


class CoordinatorIdleObserver:
    """Subscribe to a coord SessionManager's IDLE events and enqueue
    ``idle_children`` / ``idle_tasks`` nudges when unfinished work
    remains.
    """

    def __init__(self, manager: SessionManager, storage: StorageBackend) -> None:
        self._manager = manager
        self._storage = storage
        self._callback: Callable[[str, WorkstreamState], None] | None = None
        # Per-ws fire counts keyed by ``ws_id`` → ``{nudge_type: count}``.
        # Two-level dict makes the "any caps for this ws?" check at
        # leave-IDLE an O(1) ``ws_id in self._fire_counts`` lookup
        # instead of an O(N_caps) scan over a flat tuple-keyed map.
        # Lock protects against race with the leave-IDLE reset path
        # running on a different thread (state events fire on the
        # calling thread of ``set_state`` — currently always the
        # worker thread that did the transition, but the lock keeps
        # the contract robust).
        self._fire_counts: dict[str, dict[str, int]] = {}
        self._fire_counts_lock = threading.Lock()
        # Drain-time children answer, scoped to one ``drain_entries``
        # pass so same-class entries in that pass cannot disagree and N
        # entries cost one query — see :class:`_DrainMemo` for why the
        # pass, not a TTL, is the scope.
        self._drain_memo = _DrainMemo()

    def start(self) -> None:
        """Idempotent — registering twice is a no-op."""
        if self._callback is not None:
            return

        def _on_state(ws_id: str, state: WorkstreamState) -> None:
            if state is not WorkstreamState.IDLE:
                # Reset hard-cap when leaving IDLE for a *real* reason
                # (not a wake-driven exit).  Skip the manager-lock /
                # session-attribute walk entirely when no caps are
                # accumulated for this ws — the common case for the
                # vast majority of state transitions.
                with self._fire_counts_lock:
                    has_caps = ws_id in self._fire_counts
                if not has_caps:
                    return
                ws = self._manager.get(ws_id)
                if ws is None or ws.session is None:
                    return
                # ``_wake_source_tag`` is set on the session iff a
                # wake send is in flight; if set, leaving IDLE is the
                # wake's own IDLE→THINKING→RUNNING transition and the
                # caps should NOT reset.  If unset, a real (non-wake)
                # send drove the coord forward — see the re-arm ruling
                # on :data:`_NUDGE_TYPE_CAPS` — and the caps clear so
                # the next genuine idle bracket is fresh.
                if not ws.session._wake_source_tag:
                    self._reset_caps_for(ws_id)
                return

            # state == IDLE branch.
            try:
                self._on_idle(ws_id)
            except Exception:
                log.exception("coord_idle_observer.on_idle_failed ws=%s", ws_id[:8])

        self._callback = _on_state
        self._manager.subscribe_to_state(_on_state)

    def shutdown(self) -> None:
        """Unsubscribe; idempotent."""
        cb = self._callback
        if cb is None:
            return
        with contextlib.suppress(Exception):
            self._manager.unsubscribe_from_state(cb)
        self._callback = None

    def _on_idle(self, ws_id: str) -> None:
        """Plan both nudge paths for one IDLE event, then commit.

        PLAN then COMMIT, in two stages, because the fail-closed rule is
        an EVENT property: if any storage read fails while this event is
        handled, neither nudge may be queued and neither cap charged —
        and the tasks path runs first, so only a commit deferred past
        the children path's reads can honour that for a children-read
        failure.  The tasks path plans and commits FIRST so a same-event
        pair enqueues in tasks-then-children seq order — every drain
        path delivers by seq, and the co-delivered batch should END on
        the open-ended park instruction, not start with it (module
        docstring).

        The children queries stay INSIDE the paths, after their cheap
        gates: most IDLE events short-circuit on the cap peek
        (microsecond dict lookups), and hoisting the reads here would
        put a ``list_workstreams`` round-trip on every coord state
        transition in the cluster.  The tasks path never GATES on what
        children are FOUND — each nudge asserts only its own domain;
        once its own gates have passed it reads one live-child
        ``(ws_id, state)`` list of its own, to fill in the body it
        plans, never to decide whether to fire
        (:meth:`_live_children_for_body`).  Two paths, two reads, and
        neither ANSWER is shared: the shape that would tempt a shared
        read is the shape that re-couples the domains.
        What the event does share is the failure signal — one failed
        read anywhere silences both classes — which keys on a read
        FAILING, never on what it found.
        """
        ws = self._manager.get(ws_id)
        if ws is None or ws.session is None:
            return
        if ws.kind is not WorkstreamKind.COORDINATOR:
            return
        # Bind the non-Optional session — mypy's narrowing from the
        # check above does not reach into the paths' closures.
        session = ws.session

        # THREE failure classes, each with its own arm, deliberately
        # written as literal statements per path:
        #
        # * A FAILED STORAGE READ (``_StorageReadError``) escalates to
        #   the event: the veto below returns before any commit, so
        #   neither nudge fires and neither cap is charged.  The second
        #   plan is skipped once the first read failed — a nudge buys an
        #   autonomous model turn and more storage traffic, so the last
        #   thing to send a failing backend is another query on behalf
        #   of an event that can no longer fire.
        # * A PATH FAULT (any other raise) stays isolated to its path.
        #   The classes are independent by design — per-type caps exist
        #   so the liveness budget is starvation-proof against advice —
        #   and a shared ``try`` once silently broke that: any raise
        #   inside the advice path (an unregistered type KeyError from
        #   ``_NUDGE_TYPE_CAPS``, a ragged envelope value) suppressed
        #   the liveness WAKE for the same event, stranding a
        #   coordinator whose children finish unobserved.  That
        #   isolation is a standing ruling about one path's own
        #   machinery CRASHING; it says nothing about a failed storage
        #   read, which is the event-level class above.
        # * A COMMIT FAULT is a path fault at the commit stage: commits
        #   are storage-free (plans carry everything), so a raise there
        #   is a wiring bug and must not strand the sibling's fire.
        #
        # Do NOT "tidy" the pairs into a loop or a list of callables:
        # the tasks-then-children ORDER is the co-delivery ruling (tasks
        # enqueues first so it carries the lower seq and the batch ends
        # on the park instruction), and a collection makes that order an
        # accident of iteration rather than a statement.
        tasks_plan: _NudgePlan | None = None
        children_plan: _NudgePlan | None = None
        failed_read: str | None = None
        try:
            tasks_plan = self._plan_tasks(ws, session)
        except _StorageReadError as fail:
            failed_read = fail.read
        except Exception:
            log.exception("coord_idle_observer.advice_path_failed ws=%s", ws_id[:8])
        if failed_read is None:
            try:
                children_plan = self._plan_children(ws, session)
            except _StorageReadError as fail:
                failed_read = fail.read
            except Exception:
                log.exception("coord_idle_observer.liveness_path_failed ws=%s", ws_id[:8])
        if failed_read is not None:
            log.debug(
                "coord_idle_observer.event_read_failed ws=%s read=%s (no nudges this event)",
                ws_id[:8],
                failed_read,
            )
            return
        if tasks_plan is not None:
            try:
                self._commit_plan(session, tasks_plan)
            except Exception:
                log.exception("coord_idle_observer.advice_path_failed ws=%s", ws_id[:8])
        if children_plan is not None:
            try:
                self._commit_plan(session, children_plan)
            except Exception:
                log.exception("coord_idle_observer.liveness_path_failed ws=%s", ws_id[:8])

    # ------------------------------------------------------------------
    # shared gate head / commit tail
    # ------------------------------------------------------------------

    def _common_gates_allow(
        self, session: ChatSession, ws_id: str, nudge_type: str
    ) -> tuple[bool, int]:
        """The gate head both paths run identically: the per-type cap
        peek and the per-class cooldown peek.

        Returns ``(allowed, cooldown_secs)`` — the cooldown value rides
        back so the plan can carry it to the commit's authoritative
        ``nudge_allowed`` check without a second config-store read on
        the wrong side of the event's veto point.  Kept in one place so
        a cross-cutting change to the cheap gates lands on both nudge
        types at once — this file has already paid once for editing the
        head twice.

        The table lookups run FIRST and OUTSIDE the fail-closed read
        wrapper, so an unregistered nudge type still KeyErrors loudly as
        a path fault (the fail-loud registration rule on
        :data:`_NUDGE_TYPE_CAPS` / :data:`_NUDGE_TYPE_HAS_COOLDOWN`)
        instead of being misread as a failed storage read.  The one
        storage-backed read here — ``memory.nudge_cooldown`` through
        ``session._mem_cfg``, made only for a cooldown-bearing (advice)
        type — runs under :func:`_event_read`: the config store is a
        storage surface, and a failed read of it fails the event closed
        like any other.

        The cooldown peek is the cheap half of the peek-then-
        authoritative split (``_cooldown_allows`` here, ``nudge_allowed``
        at the commit tail): the advice path does message walks and an
        envelope read after this gate, exactly the expensive work the
        peek exists to short-circuit.  ``memory.nudge_cooldown`` governs
        the memory-class advisories AND the advice-class idle nudge
        (:data:`_NUDGE_TYPE_HAS_COOLDOWN`); for a cap-only liveness type
        the cooldown is ``0`` and the peek is vacuous — one uniform
        head, per-class behaviour from the table.
        """
        if self._cap_reached(ws_id, nudge_type):
            return False, 0
        if _NUDGE_TYPE_HAS_COOLDOWN[nudge_type]:
            cooldown_secs = _event_read(
                ws_id, "nudge_cooldown", lambda: session._mem_cfg.nudge_cooldown
            )
        else:
            cooldown_secs = 0
        allowed = _cooldown_allows(
            nudge_type,
            session._metacog_state,
            cooldown_secs=cooldown_secs,
        )
        return allowed, cooldown_secs

    def _commit_plan(self, session: ChatSession, plan: _NudgePlan) -> bool:
        """The shared commit tail: ``nudge_allowed`` (the authoritative
        permission check), the formatter empty-body guard, the atomic
        cap charge, the enqueue, and finally ``record_nudge``.

        Runs strictly AFTER the event's veto point and performs NO
        storage-backed read — its inputs are the plan (built before the
        veto) and in-memory session state — so a read failure cannot
        appear between the decision "this event fires" and the enqueue.
        One implementation for both types so the charge discipline —
        cheap peek at plan time, authoritative :meth:`_try_charge` at
        the fire position — can never drift between paths.

        ``memory_count=0`` is deliberate, not a stub: ``nudge_allowed``
        reads ``memory_count`` only for the ``tool_error`` / ``resume``
        / ``start`` types, so paying 1-2 ``count_structured_memories``
        round-trips per IDLE event to compute the real number bought
        nothing.  If a future nudge type routed through here needs it,
        thread it explicitly.
        """
        # ``cooldown_secs`` rides in from the plan's gate head, per CLASS
        # (:data:`_NUDGE_TYPE_HAS_COOLDOWN`): ``memory.nudge_cooldown``
        # for advice — read once, microseconds before this, on the safe
        # side of the veto — and ``0`` for cap-only liveness, for which
        # this call still gates what a cap cannot express — unknown
        # type, and ``message_count <= 1`` (a rehydrated or
        # freshly-truncated session should not be nudged about work it
        # cannot yet see).
        if not nudge_allowed(
            plan.nudge_type,
            session._metacog_state,
            message_count=len(session.messages),
            memory_count=0,
            cooldown_secs=plan.cooldown_secs,
        ):
            return False
        if not plan.text:  # belt-and-braces: formatter empty-input guard
            return False
        if not self._try_charge(plan.ws_id, plan.nudge_type):
            return False

        # WAKE-ONLY delivery discipline: both
        # idle-nudge types ride the ``"wake"`` channel, which no user- or
        # tool-seam drain ever matches.  Each body speaks about an IDLE
        # state ("you went idle holding X"); delivered at a later user
        # message or tool batch it would describe a moment that no longer
        # exists, beside a user who just gave fresh instructions.  The
        # wake is the ONLY door out of the queue for these entries —
        # a Stop or an interjection-owned seam drops them, and the next
        # genuine idle bracket re-derives fresh ones over fresh reads.
        session._nudge_queue.enqueue(
            plan.nudge_type,
            plan.text,
            WAKE_CHANNEL,
            valid_until=plan.valid_until,
            metadata=plan.metadata,
        )
        # Record LAST — permission, then charge, then deliver, then
        # record.  The stamp this writes IS the advice cooldown window
        # (for a cap-only liveness type it gates nothing; the cap does),
        # so the order is what keeps it honest: a refused fire must
        # never burn budget.  ``should_nudge`` stamps at its own
        # permission check, before ``_try_charge`` (the authoritative
        # gate) has said yes — the loser of a cap race would then lose
        # its next fire too, having delivered nothing.
        # Reordering the CHARGE above the permission check would be
        # worse: ``nudge_allowed`` refuses for reasons the cheap peek
        # never sees (``message_count <= 1`` on a rehydrated session, an
        # unregistered type), so the cap would be charged on every one
        # of those refusals.  The stamp is also the per-type timestamp
        # other tooling reads.
        record_nudge(plan.nudge_type, session._metacog_state)
        log.info(plan.log_msg, *plan.log_args)
        return True

    # ------------------------------------------------------------------
    # idle_children — LIVENESS
    # ------------------------------------------------------------------

    def _plan_children(self, ws: Workstream, session: ChatSession) -> _NudgePlan | None:
        """Plan an ``idle_children`` fire when the coord went idle with
        active interactive children; ``None`` when this path declines.
        A children read that FAILS raises :class:`_StorageReadError`
        instead of declining — the event, not this path, owns that
        outcome (module docstring).

        LIVENESS class — deliberately NOT gated on
        ``ChatSession._nudges_enabled`` / ``memory.nudges``.  That
        switch's operator-facing help text promises control of
        memory-save reminders; suppressing this wake under it stranded
        coordinators whose children finished unobserved (results never
        collected, operator hand-restarts every stalled coord).  It is
        the only nudge producer that ever made that mistake — every
        other liveness-class producer (``watch_triggered``,
        ``background_shell_exit`` via the external-event rail,
        ``compaction_pending``) already bypasses the gate by
        construction.  Do not "unify" this with the advice path's gate.

        The body suggests ``wait_for_workstream`` but the wake fires
        even for a persona that hides that tool: the wake itself is the
        point, and the body is a ROSTER — the child list is useful to a
        model that cannot call the suggested tool (inspect or message
        them, or simply carry on knowing work is in flight).  This
        asymmetry with the advice path (which IS visibility-gated) is
        deliberate: the advice body's PRIMARY ask is typed bookkeeping
        through ``tasks(...)`` — the ``needs_user`` escalation above
        all — so a persona that hides ``tasks`` cannot take the action
        that nudge exists for, while the liveness body still carries
        its facts.
        """
        ws_id = ws.id

        allowed, cooldown_secs = self._common_gates_allow(session, ws_id, "idle_children")
        if not allowed:
            return None

        # Gate: skip if the coord's last assistant turn already used
        # ``wait_for_workstream``.  Don't nudge toward a tool the
        # model is already using.
        if self._last_assistant_used_wait(session):
            return None

        # Gate: the children query.  ``[]`` is a REAL empty answer and
        # declines this path.  ``None`` means the read was INDETERMINATE
        # (storage raised, or a row was too ragged to classify) — a
        # failed storage read, which fails the whole EVENT closed:
        # neither nudge fires and neither cap is charged, never a falsy
        # non-answer read as "no children".
        active = self._active_children(ws)
        if active is None:
            raise _StorageReadError("active_children")
        if not active:
            return None

        text = format_idle_children_nudge(active)

        # Structured ``_source_meta`` for the FE idle-children card — the
        # same child list ``format_idle_children_nudge`` rendered into
        # ``text`` above, so the card and the model-facing prose derive from
        # one source.
        #
        # CARD HONESTY: an idle-nudge card is the transcript's record of the
        # nudge — it renders what the model was TOLD, formatted for operator
        # readability, never content the model did not receive.  The body is
        # ids and states only (child names are model-authored and are not
        # lowered into the system turn — see the formatter's docstring), so
        # the meta carries exactly those two fields and NO ``name`` key: a
        # card showing names the model never got would falsify the
        # operator's mental model of what the coordinator knows.  The
        # browsing surface for child names is the children sidebar, which
        # renders live workstream rows, not this record.
        #
        # ``ws_id`` and ``state`` ride RAW: a storage row id and an
        # enum-derived state, neither of them user-authored text, so there
        # is no projection split here any more — the tasks path below keeps
        # its two-projection split because its fields ARE user-authored.
        # The FE derives its 8-char ``ident`` column from ``ws_id`` —
        # display formatting over the same full id the body's bullet now
        # carries whole (a bullet is a handle for the resolver, which
        # refuses prefixes; an ident is a label for the operator's eye).
        children_meta = [
            {
                "ws_id": c.get("ws_id", ""),
                "state": c.get("state", ""),
            }
            for c in active
        ]

        # Bind ws.id + user_id by closure so the predicate captures the
        # workstream identity (not the live ``ws`` reference, which
        # could mutate).  The queue is bound too: its ``drain_pass()``
        # is the memo scope for the children read.  The predicate runs
        # at drain time outside the queue lock, on whichever worker
        # thread drains the entry.
        bound_ws_id = ws.id
        bound_user_id = ws.user_id
        bound_queue = session._nudge_queue

        def _still_has_active_children() -> bool:
            now = self._children_state_at_drain(bound_ws_id, bound_user_id, bound_queue)
            if now is None:
                # A FAILED read at drain DROPS the entry — fail closed,
                # the same unqualified rule the fire gate follows: these
                # nudges must not fire when a storage read fails, and
                # delivery is the fire.  The cost is real and accepted:
                # the entry is an already-charged fire with no retry
                # behind it, so a transient blip here spends the
                # bracket's wake on nothing until a real send re-arms
                # it.  (This REVERSES the earlier fail-open reading,
                # which priced a lost wake above stale noise; the
                # fail-closed rule is unconditional and outranks that
                # trade.)
                return False
            return now

        return _NudgePlan(
            ws_id=ws_id,
            nudge_type="idle_children",
            text=text,
            metadata={"children": children_meta},
            valid_until=_still_has_active_children,
            cooldown_secs=cooldown_secs,
            log_msg="coord_idle_observer.enqueued ws=%s active_children=%d",
            log_args=(ws_id[:8], len(active)),
        )

    # ------------------------------------------------------------------
    # idle_tasks — ADVICE
    # ------------------------------------------------------------------

    def _plan_tasks(self, ws: Workstream, session: ChatSession) -> _NudgePlan | None:
        """Plan an ``idle_tasks`` fire when the coord went idle holding
        open tasks; ``None`` when this path declines.  A storage-backed
        read that FAILS raises :class:`_StorageReadError` instead of
        declining — the event, not this path, owns that outcome (module
        docstring).

        Fires independently of WHAT the children read finds — beside a
        same-event ``idle_children`` (co-delivery, tasks first) or alone
        while children run and the liveness nudge is blocked by its own
        cap or wait gate.  The body never presumes the children are done
        (its per-child observed fact lines plus the blocked-on-a-child
        branch are what make advice-alone honest beside running
        children), and its drain predicate reads no children at all.
        The one children read this path makes
        (:meth:`_live_children_for_body`) is the post-gate live-child
        ``(ws_id, state)`` list that selects between the children-aware
        body and the childless one, feeds the fact lines, and populates
        the branch's slots.

        It is NOT true that no outcome of that read can suppress the
        fire, and the earlier absolute saying so was wrong.  A read that
        RAISES suppresses it — and suppresses the sibling with it,
        before either charge: a failed storage read anywhere in the
        event means neither nudge fires and neither cap is charged,
        where an older rule declined this path alone and let the sibling
        fire on.  What remains inviolable is the narrower and
        actually-load-bearing claim: no ANSWER the read returns can
        change whether this fires.  Children present and children absent
        both fire.

        ADVICE class — gated on ``ChatSession._nudges_enabled``, which
        for this type means the ``memory.nudges`` config switch AND the
        persona envelope exposing the ``tasks`` tool
        (``NUDGE_REQUIRED_TOOL``): the body's PRIMARY ask — the typed
        ``needs_user`` escalation, the child link, the ``done`` record —
        is bookkeeping through ``tasks(...)``, so firing it at a persona
        that hides the tool asks for the one action it cannot take and
        produces "I don't have access" apology loops.  (The body also
        carries wait-on-a-child and take-the-step branches; the gate is
        ruled on the primary ask, not on every line being a ``tasks``
        call.)
        """
        ws_id = ws.id

        # Gate: the operator stopped this generation.  An abandoned turn
        # ends in ``_emit_state("idle")``, and that IDLE reaches here —
        # so without this check pressing Stop on a coord holding open
        # tasks enqueues a wake-eligible entry AFTER the cancel path
        # swept the queue's wake eligibility (``any`` demoted to quiet,
        # ``wake`` dropped), and the watcher resumes the workstream
        # seconds later.  The operator said stop; a task reminder is
        # not a reason to override that.
        #
        # ADVICE only.  Liveness deliberately still fires: a cancelled
        # coordinator can still have children running whose results
        # would otherwise be abandoned, which is the outcome that class
        # exists to prevent.
        if session._generation_abandoned:
            return None

        # ``_nudges_enabled`` is one gate read over storage-backed state
        # (``memory.nudges`` through the config store, plus the persona
        # envelope): a raise out of it is a failed read of this path's
        # gate inputs, not a path fault, and fails the event closed.
        if not _event_read(ws_id, "nudges_enabled", lambda: session._nudges_enabled("idle_tasks")):
            return None

        allowed, cooldown_secs = self._common_gates_allow(session, ws_id, "idle_tasks")
        if not allowed:
            return None

        # Gate: the coord's last assistant turn looks like a question put
        # to the operator.  Deliberately false-negative-biased — see the
        # method docstring; suppressing a legitimate nudge costs the
        # operator one "continue", while nudging a coord that correctly
        # escalated pushes it to guess on a decision that was not its to
        # make.
        if self._last_assistant_asked_operator(session):
            return None

        # Gate: the coord's last assistant turn already used
        # ``wait_for_workstream`` — it parked on a known wake source,
        # and waking it to groom tasks defeats the park.  Park signals
        # gate BOTH nudge paths; domain conditions gate only their own.
        # Nearly inert at a natural end-of-turn IDLE (the send loop only
        # breaks when the last turn carried no tool calls), so the
        # non-redundant coverage is a session rehydrated mid-wait —
        # shipped for the shared park-gate shape, not to close a live
        # hole.
        if self._last_assistant_used_wait(session):
            return None

        # Gate: read the task envelope, with a failed read SURFACED.
        # ``load_task_envelope``'s default swallows a storage raise into
        # an empty envelope — right for its UI read paths, fatal here:
        # this path would exit through the "no open tasks" gate having
        # never learned a read failed, while the sibling path fired on.
        # ``raise_on_read_failure=True`` routes the failure through
        # :func:`_event_read` to the event-level veto instead.  A
        # CORRUPT blob is different — that read SUCCEEDED and returned a
        # broken value — so it stays a path-local decline: refuse to
        # nudge about a list that cannot be parsed, and let the sibling
        # class speak for its own domain.
        #
        # Cost note (accepted): this row read + JSON parse runs on EVERY
        # IDLE event that clears the cheap gates, whatever the children
        # state — advice fires independently of liveness, so there is no
        # childless-only narrowing to lean on — and even for coords that
        # never used the tasks tool.  One indexed PK fetch on a worker
        # thread, comparable to the children query beside it; the
        # structural fix (a narrow ``load_workstream_config_key``
        # accessor) needs a storage-protocol change across both backends
        # and is not worth it at this rate.
        envelope, corrupt = _event_read(
            ws_id,
            "task_envelope",
            lambda: load_task_envelope(self._storage, ws_id, raise_on_read_failure=True),
        )
        if corrupt:
            log.debug(
                "coord_idle_observer.tasks_envelope_corrupt ws=%s (no nudge)",
                ws_id[:8],
            )
            return None
        open_tasks = self._open_tasks(envelope)
        if not open_tasks:
            return None
        # PARK WHILE ANY TASK NEEDS THE USER.
        # There is no task graph, so the harness cannot know whether an
        # open task is gated on a parked one's unanswered question —
        # task A ``in_progress`` may be waiting on exactly the decision
        # task B escalated.  Waking the model with "you have open tasks"
        # in that state invites it to take a step the user has not yet
        # licensed, which is the failure this feature exists to prevent.
        # The cost is accepted and known: one stale ``needs_user`` row
        # silences this nudge until the operator acts — but the pane's
        # "needs you" chip carries that escalation, the ball is
        # explicitly in the operator's court, and their answer is the
        # natural re-arm.  (This REVERSES the earlier fire-on-the-
        # pending-one reading; the relatedness argument was not weighed
        # when that reading was taken.)
        if self._has_needs_user(envelope):
            log.debug(
                "coord_idle_observer.tasks_parked_on_needs_user ws=%s (no nudge)",
                ws_id[:8],
            )
            return None

        # THE CLAIM IS ONE OPEN SET.  Everything downstream — the
        # model-facing body, the operator card's metadata, and the drain
        # predicate's validity question — describes that one set, and it
        # is projected exactly twice, both derivations rooted in the same
        # ``open_tasks`` list: the per-status counts
        # (:meth:`_open_counts`) that the counts line and the card share,
        # and the ``(id, status)`` pairs (:meth:`_open_task_ids`) that the
        # body's id block and its populated calls share.
        #
        # No task TEXT leaves storage on this path: no titles and no
        # notes are interpolated anywhere, so there is nothing to
        # sanitise for meaning and no strict/display projection split to
        # keep in lockstep.  IDS and STATUSES do leave, and they are the
        # server-minted exemption the belt-and-braces rule names — a
        # ``tsk_`` + ``secrets.token_hex`` id and a
        # ``TASK_OPEN_STATUSES`` member.  The formatter still runs the
        # sanitiser over each id as an ALTERATION check, because the
        # envelope is a JSON blob a hand-edited DB can leave ragged; that
        # check lives at the interpolation site, not here.  Any future
        # field that DOES lower stored task TEXT into the body must come
        # back through ``sanitize_name`` first, and into the card through
        # ``sanitize_display`` — the two-projection discipline the roster
        # body carried lives on in git history and in those two
        # functions' docstrings, not here.
        open_counts = self._open_counts(open_tasks)
        open_task_ids = self._open_task_ids(open_tasks)
        total_open = len(open_tasks)

        # Structured ``_source_meta`` for the FE idle-tasks card.
        #
        # CARD HONESTY: an idle-nudge card is the transcript's record of
        # the nudge — it renders what the model was TOLD, formatted for
        # operator readability, never content the model did not receive.
        # The card's fresh metadata is the counts — ``open`` (the total)
        # plus the same per-status split the counts line carries — and
        # deliberately NOT rows: a card showing titles the model never
        # got would falsify the operator's mental model of what the
        # coordinator knows.  The tasks pane remains the browsing surface
        # for rows.
        #
        # A SUBSET of the body, deliberately, and the rule survives the
        # body growing its id block: honesty here is "nothing the model
        # did not receive", not "everything it did".  Repeating the ids
        # would make the card a second, worse task browser sitting beside
        # the pane that already renders those rows BY id, and would move
        # a shipped FE contract for no operator gain.  The counts remain
        # the summary the card exists to be.
        tasks_meta = {"open": total_open, **open_counts}

        # The ONE children read this path makes, and the LAST thing it
        # does before handing the plan back to ``_on_idle``.
        #
        # WHAT IT MAY AND MAY NOT DECIDE, stated precisely, because the
        # absolute this comment used to carry ("nothing about WHETHER
        # this nudge fires can depend on it") was too wide and cost a
        # release's worth of confusion:
        #
        #   * children CONTENT never gates the fire.  "This coordinator
        #     has live children" and "this coordinator has none" both
        #     fire, with different bodies.  A read outcome that suppressed
        #     the fire because of what it FOUND would be the cross-domain
        #     gate this file's docstring forbids — the coupling that once
        #     let one domain starve the other.
        #   * a FAILED read gates the whole EVENT, and must: these nudges
        #     must not fire when a storage read fails — unqualified, both
        #     types, neither cap charged.  That is not the children
        #     domain reaching into this one; it keys on the read FAILING,
        #     never on what a read found, and the raise below is how the
        #     failure reaches ``_on_idle``'s veto instead of dying here
        #     as a path-local decline that leaves the sibling firing.
        #
        # WHY SILENCE BEATS A HEDGE on ``None`` (a ruling the event-wide
        # rule above subsumes).  A read that raises is evidence the
        # backend is unwell, and a nudge buys an autonomous model turn —
        # which buys more storage traffic, aimed at the thing that just
        # failed.  Firing there adds load exactly where load is the
        # problem.  The hedge's own justification also dissolves under
        # the prior question: a hedge exists to keep the BODY safe when
        # children are unknown, but not sending a body is safe too —
        # strictly safer, and free.  (The same ruling later removed the
        # hedge from the SUCCESSFUL-read body: the states are observed,
        # so the body renders one fact line per child instead of "may
        # still be running or may have finished".)
        #
        # BEFORE ANY CHARGE, structurally.  Plans carry no side effects,
        # and ``_on_idle`` reaches :meth:`_commit_plan` — where
        # :meth:`_try_charge` lives — only when no read failed, so a
        # transient storage blip costs NEITHER class its bracket and the
        # next IDLE event with a working read still fires.  A silence
        # that burned a slot would convert one failed query into a whole
        # bracket of lost nudges, which is the failure mode a
        # fail-closed rule is supposed to avoid, not create.
        #
        # Staleness, stated rather than assumed: child rows come to
        # exist under a coordinator through its own spawn tools (which
        # an idle coord cannot call without taking a turn) AND through
        # the ownership-gated ``parent_ws_id`` create on the ordinary
        # workstream-create route, which needs no coordinator turn at
        # all.  So an entry whose delivery is deferred (worker-busy,
        # send-barrier yield, a Stop-demoted entry delivering at the
        # next seam) can in principle carry a childless body to a
        # coord that gained a child inside that window — narrow, and
        # one-directional: a body with no children content asserts
        # NOTHING about children, while carrying it costs a measured
        # ``list_workstreams`` round-trip on every childless bracket.
        # A fact line can likewise outlive its state (a running child
        # idles before delivery), and that lands safe in the same
        # direction: the running line's instruction is check-before-
        # redoing, which is exactly right for a child that has just
        # finished, and an idle child does not restart.  A NEW
        # out-of-band creator class (task-agent lanes) widens the
        # window and must re-visit this condition.
        #
        # Cost: one ``list_workstreams`` fetch per IDLE event that clears
        # this path's own gates — not per fire, since the shared tail can
        # still refuse downstream.  It was an aggregate until the body
        # needed ids to populate its calls with; the rate is unchanged and
        # the per-read cost is now the liveness path's, which is the trade
        # the discovery round-trip it removes is worth (see
        # :meth:`_live_children_for_body`).
        children = self._live_children_for_body(ws)
        if children is None:
            raise _StorageReadError("live_children")
        text = format_idle_tasks_nudge(
            open_counts,
            open_task_ids=open_task_ids,
            children=children,
        )

        # Bind identity by closure (never the live ``ws``).  The
        # predicate runs at drain time on whichever worker thread drains
        # the entry.
        bound_ws_id = ws_id

        def _still_valid() -> bool:
            # The predicate re-validates ONLY the advice path's own
            # domain, never children — that scope is the standing
            # ruling, and it survives every body change so far unchanged
            # in SPIRIT while its letter moves with the body.  The
            # entry's ASSERTION is "you have open tasks"; that is stale
            # exactly when no open task remains, so "any open task
            # remains" is the whole validity question.
            #
            # The body names ids again, and that deliberately does NOT
            # restore the roster-era letter (at least one NAMED task is
            # still open).  The ids are a HANDLE, not a claim: the branch
            # calls are worked examples of a transition the model decides
            # on, so a row that closed between enqueue and drain makes one
            # example stale, not the body false — and a model that runs it
            # gets a typed error, which is cheaper than the wake this
            # would have dropped.  Reinstating the old letter would
            # resurrect the inversion the counts adoption fixed: a
            # coordinator that closed every listed row and opened a new
            # one would lose the nudge precisely when it still has open
            # work.  The N and the id set may both have drifted by drain
            # time; that is a tuning miss a fresh bracket re-derives.
            #
            # No children read, restated here because the temptation
            # recurs: an entry that outlives a bracket, survives a Stop
            # demotion, or is resurrected by a failed wake and then
            # delivers beside live children stays two true-enough
            # statements, not a contradiction — a children-bearing
            # body's fact lines state what was observed at enqueue and
            # their protections land safe under drift (a running child
            # that idled still deserves check-before-redoing; an idle
            # one does not restart), and a childless body asserts
            # nothing about children at all.
            #
            # Corrupt envelope → drop, and a FAILED read → drop: a
            # failed storage read never delivers a nudge, at drain
            # exactly as at the fire gate.  The read is STRICT
            # (``raise_on_read_failure=True``) so the failure lands in
            # this ``except`` with its honest log line instead of being
            # laundered into "no open tasks" by the loader's swallow.
            # ``_open_tasks`` sits INSIDE the try: it walks raw envelope
            # rows, so a ragged one is a data-shape condition, and
            # letting it escape would surface as ``predicate_raised`` —
            # which ``nudge_queue`` documents as a wiring bug, sending
            # operators after a phantom code defect.
            try:
                env, is_corrupt = load_task_envelope(
                    self._storage, bound_ws_id, raise_on_read_failure=True
                )
                if is_corrupt:
                    return False
                open_now = self._open_tasks(env)
                parked_now = self._has_needs_user(env)
            except Exception:
                log.debug(
                    "coord_idle_observer.predicate_tasks_failed ws=%s",
                    bound_ws_id[:8],
                    exc_info=True,
                )
                return False
            # The park rule holds at drain exactly as it does at fire: a
            # ``needs_user`` row that appeared AFTER enqueue (the model
            # escalated something, or a parallel actor did) makes "take
            # the next step" unsafe for the same no-graph reason, so the
            # queued entry dies rather than delivers.  Advice fails
            # closed; the operator's answer re-arms the bracket.
            return bool(open_now) and not parked_now

        return _NudgePlan(
            ws_id=ws_id,
            nudge_type="idle_tasks",
            text=text,
            metadata={"counts": tasks_meta},
            valid_until=_still_valid,
            cooldown_secs=cooldown_secs,
            log_msg="coord_idle_observer.enqueued_tasks ws=%s open_tasks=%d",
            log_args=(ws_id[:8], total_open),
        )

    # ------------------------------------------------------------------
    # storage reads
    # ------------------------------------------------------------------

    def _active_children(self, ws: Workstream) -> list[dict[str, str]] | None:
        """Query storage for the coord's interactive children whose state
        is in :data:`_ACTIVE_CHILD_STATES`.

        Returns a (possibly empty) list of ``{ws_id, state}`` rows on a
        successful read, or ``None`` when the answer is INDETERMINATE —
        the query raised, or a row was too ragged to classify.  The row
        projection deliberately EXCLUDES the child's ``name``: both
        consumers (the model-facing body and the card meta) are
        ids-and-states only, so the model-authored string never enters
        this pipeline at all — reintroducing it is a Safe Harness
        decision, not a convenience edit.  The distinction between the
        two return shapes is load-bearing: ``[]`` is a real "no children"
        answer (nothing to keep alive, no fire), while ``None`` is "we
        cannot tell" — a failed storage read, which the call site
        escalates to the event-level veto (neither nudge fires, neither
        cap is charged) instead of reading as a falsy no-answer.  An
        error collapsing into ``[]`` would erase that difference and
        silence the indeterminate case.

        This read serves the LIVENESS path only, and nothing here may be
        made to license or block the ADVICE fire on what it FINDS —
        advice makes its own read (:meth:`_live_children_for_body`),
        after its own gates, and a found-children outcome that skipped an
        advice fire would be the re-coupling this warning forbids.  A
        FAILED read is the one outcome that leaves this path's scope,
        and that is a shared failure POLICY, not coupling: it keys on
        the read failing, never on what a read found, and no answer
        either query returns ever crosses domains.  The advice drain
        predicate still reads no children at all.

        The whole row walk sits inside the ``try`` for the same
        fail-closed reason — a row missing ``state`` must yield
        "indeterminate" and take the designed read-failure route, not
        escape as a ``KeyError`` that logs a phantom path-fault
        traceback for what is really unreadable data.

        ``list_workstreams`` orders by ``updated DESC`` and applies its
        ``LIMIT`` in SQL before any state filter, so a coord with many
        recently-closed children could clip out genuinely-active rows
        whose ``updated`` timestamp is older.  We bump the limit well
        above ``NUDGE_IDLE_CHILDREN_WAIT_CAP`` to absorb that —
        realistic coord histories are far smaller than the bumped
        limit.  Pushing the state filter into SQL would be the
        structural fix, but that requires a storage-protocol change;
        flagged as a follow-up.
        """
        try:
            rows = self._storage.list_workstreams(
                limit=_ACTIVE_CHILDREN_QUERY_LIMIT,
                parent_ws_id=ws.id,
                kind=WorkstreamKind.INTERACTIVE,
                user_id=ws.user_id,
            )
            out: list[dict[str, str]] = []
            for row in rows:
                mapping = getattr(row, "_mapping", row)
                state = mapping["state"]
                if state not in _ACTIVE_CHILD_STATES:
                    continue
                out.append(
                    {
                        "ws_id": mapping["ws_id"],
                        "state": state,
                    }
                )
        except Exception:
            log.debug("coord_idle_observer.list_failed ws=%s", ws.id[:8], exc_info=True)
            return None
        return out

    def _children_state_at_drain(self, ws_id: str, user_id: str, queue: NudgeQueue) -> bool | None:
        """Drain-pass-memoised answer to "does this coord have active
        children?", for the LIVENESS ``valid_until`` predicate.

        One storage read per ``drain_entries`` pass, shared by every
        liveness entry that pass evaluates — coherence and economy from
        one mechanism, with the scoping rationale on
        :class:`_DrainMemo`.  ``None`` (indeterminate) is memoised like
        any other answer: re-querying after a failure could hand the
        second predicate a different answer, which is the disagreement
        the memo exists to prevent.  *queue* is the entry's own
        :class:`~turnstone.core.nudge_queue.NudgeQueue` — its
        ``drain_pass()`` is the pass identity.
        """
        memo = self._drain_memo
        key = (ws_id, queue.drain_pass())
        if memo.key == key:
            return memo.answer
        answer = self._active_children_now(ws_id, user_id)
        memo.key = key
        memo.answer = answer
        return answer

    def _active_children_now(self, ws_id: str, user_id: str) -> bool | None:
        """Drain-time answer to "does this coord have active children?"

        ``True`` / ``False`` on a successful read; ``None`` when the
        read was INDETERMINATE.  ``None`` fails CLOSED at its one
        consumer — the liveness predicate drops the entry, because a
        failed storage read never delivers a nudge (the advice predicate
        reads no children at all).  It stays a trichotomy rather than a
        baked-in bool so that ruling is visible at the consumer and the
        drain memo caches "indeterminate" as its own answer (a
        bool-returning helper here once inverted the failure direction
        through a bare ``not``).

        Uses the ``count_workstreams_by_state`` aggregate rather than
        the row-fetching gate query: the children predicate arms on the
        chat-loop user-attach path for every coord, where a row fetch
        is real latency.  The aggregate has NO kind filter while the
        enqueue gate filters ``kind=INTERACTIVE`` — an asymmetry, but a
        near-theoretical one: only two kinds exist and nothing today
        creates a COORDINATOR row with a parent, so the two questions
        coincide.  If nested coordinators ever land, either push a kind
        param into the aggregate (protocol change) or accept that the
        divergence lands in the safe direction — liveness over-delivers,
        and the body's live-child read that shares this asymmetry
        (:meth:`_live_children_for_body`) over-includes rows, which adds
        a fact line that is still TRUE of the row it names (the line
        renders the row's own observed state; only its kind diverged)
        and a ws_id to a ``mode="any"`` wait that returns on the first
        finisher anyway.  The advice DRAIN predicate reads no children
        at all.
        """
        try:
            counts = self._storage.count_workstreams_by_state(
                parent_ws_id=ws_id,
                user_id=user_id,
            )
        except Exception:
            log.debug(
                "coord_idle_observer.predicate_count_failed ws=%s",
                ws_id[:8],
                exc_info=True,
            )
            return None
        return any(counts.get(s, 0) > 0 for s in _ACTIVE_CHILD_STATES)

    def _live_children_for_body(self, ws: Workstream) -> list[tuple[str, str]] | None:
        """Enqueue-time answer to "which child rows of this coord are in
        a live state, and in which?", for the ``idle_tasks`` BODY only.

        A (possibly empty) list of ``(ws_id, state)`` pairs on a
        successful read; ``None`` when the read was INDETERMINATE (the
        query raised, or a row was too ragged to classify).  The
        THREE-WAY return is the point, and each value has a different
        consumer:

        * ``[]`` and a NON-EMPTY list both reach
          :func:`~turnstone.core.metacognition.format_idle_tasks_nudge`
          and choose between the childless body and the children-aware
          one.  These are the only two values production ever hands it,
          and since the formatter's children parameter became a required
          list they are the only two it can express.
        * ``None`` never reaches the formatter at all: the caller raises
          it to the event level, before any charge, and NEITHER nudge is
          sent — a failed storage read fails the whole event closed
          (module docstring).  A failed read must never be laundered
          into "this coordinator has no children", and the way it avoids
          that is by declining to speak rather than by hedging — the
          caller's comment carries the full argument.

        Callers must not collapse it to a bool.  ``[]`` and ``None`` are
        both falsy and mean opposite things: one is an answer, the other
        is the absence of one.

        It returns ``(ws_id, state)`` PAIRS rather than the bare ids it
        once did because the body renders the observed STATE per child
        (the fact lines) as well as populating the blocked-on-a-child
        branch's two slots.  Bare ids forced the body to hedge about
        states this very query had just read — "may still be running or
        may have finished" — which is manufactured uncertainty, ruled
        out: the harness renders the fact it holds.  One
        value serves the fact lines and the slots deliberately: a
        second derivation of the same storage read is a state where the
        two can disagree, which is unreachable when there is only one
        value.

        Deliberately :data:`_LIVE_CHILD_STATES`, not
        :data:`_ACTIVE_CHILD_STATES`: the fact lines speak about
        children that EXIST, and an idle child holding results nobody
        collected is exactly what the stopped-child line protects.
        Conditioning on the active set would drop the line from the one
        state whose protection is live.

        COST, changed and stated: this was a ``count_workstreams_by_state``
        aggregate while a bool was enough.  Ids need rows, so it is now a
        ``list_workstreams`` fetch — the same query class
        :meth:`_active_children` already runs on the liveness path, and
        the same one-per-IDLE-event-that-clears-this-path's-own-gates
        rate as before (the shared commit tail can still refuse
        downstream of it).  An IDLE event where BOTH paths clear their
        gates therefore costs two row fetches rather than a fetch and an
        aggregate.  They stay two reads, not one shared one: they ask
        different questions (ACTIVE and ``kind=INTERACTIVE`` there, LIVE
        and unfiltered here), and a shared read is the shape that
        re-couples the two domains.

        NO ``kind`` filter, preserving the aggregate's scope exactly —
        the same documented asymmetry with :meth:`_active_children` as
        before, and it still lands safely: only two kinds exist and
        nothing today creates a COORDINATOR row with a parent, so the
        questions coincide; if they ever diverge, an unfiltered read
        over-includes, which keeps a hedge that is unconditionally true
        and adds a ws_id to a ``mode="any"`` wait that returns on the
        first finisher regardless.

        The LIMIT is new exposure and is accepted: ``list_workstreams``
        orders by ``updated DESC`` and applies its limit in SQL before
        any state filter, so a coordinator holding more than
        :data:`_ACTIVE_CHILDREN_QUERY_LIMIT` child rows whose live ones
        are all older than that many terminal siblings would read as
        childless and lose its hedge.  The aggregate had no such clip.
        Realistic coord histories are far below the limit — the liveness
        roster has run under exactly this bound since it shipped — and
        the structural fix is the same one flagged there (push the state
        filter into SQL, a storage-protocol change across both backends).

        The whole row walk sits inside the ``try`` for the same
        fail-closed reason :meth:`_active_children` gives: a row missing
        ``state`` must yield "indeterminate", not escape as a
        ``KeyError``.
        """
        try:
            rows = self._storage.list_workstreams(
                limit=_ACTIVE_CHILDREN_QUERY_LIMIT,
                parent_ws_id=ws.id,
                user_id=ws.user_id,
            )
            out: list[tuple[str, str]] = []
            for row in rows:
                mapping = getattr(row, "_mapping", row)
                state = mapping["state"]
                if state not in _LIVE_CHILD_STATES:
                    continue
                out.append((mapping["ws_id"], state))
        except Exception:
            log.debug(
                "coord_idle_observer.body_children_list_failed ws=%s",
                ws.id[:8],
                exc_info=True,
            )
            return None
        return out

    @staticmethod
    def _open_tasks(envelope: dict[str, Any]) -> list[dict[str, str]]:
        """Filter a decoded task envelope to the rows that count as
        unfinished work (``TASK_OPEN_STATUSES``), NORMALISED: every
        returned row carries exactly ``id`` / ``title`` / ``status`` /
        ``note``, all ``str``.

        This is the single open-set derivation for everything the advice
        path does — the fire decision, the counts
        (:meth:`_open_counts`) that feed the body and the card, and the
        drain predicate's re-read — so no two of them can disagree on a
        ragged row.  ``field_str`` maps ``None`` → ``""`` — a bare
        ``str()`` here once rendered a JSON ``null`` note as a literal
        ``None`` line in the operator card while the prose showed
        nothing; the text fields are normalised even though nothing
        renders them today, because this is the shape contract every
        consumer of an open row inherits.  The envelope is a JSON blob a
        hand-edited DB or an older writer can leave ragged, and
        ``load_task_envelope`` shape-checks only the envelope, not the
        rows.
        """
        rows = envelope.get("tasks") or []
        if not isinstance(rows, list):
            return []
        out: list[dict[str, str]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            # Coerce BEFORE the membership test.  ``TASK_OPEN_STATUSES``
            # is a frozenset, so a non-hashable value (``status: []`` in
            # a hand-edited blob) raises ``TypeError`` from the ``in``
            # itself — not caught as an unknown status but escaping the
            # whole nudge path, which the observer swallows, silencing
            # this coordinator's nudge permanently.  Coerced, it simply
            # fails to match and the row is skipped.
            status = field_str(row.get("status"))
            if status not in TASK_OPEN_STATUSES:
                continue
            out.append(
                {
                    "id": field_str(row.get("id")),
                    "title": field_str(row.get("title")),
                    "status": status,
                    "note": field_str(row.get("note")),
                }
            )
        return out

    @staticmethod
    def _has_needs_user(envelope: dict[str, Any]) -> bool:
        """True when any row's status is ``needs_user`` — the advice
        park signal (fire gate AND drain predicate read this).

        Same coercion discipline as :meth:`_open_tasks`, for the same
        reason: a hand-edited row must fail the comparison, never
        escape it.  A ragged status is NOT ``needs_user`` and does not
        park — parking on garbage would let one corrupt row silence the
        nudge with no chip in the pane telling the operator why.
        """
        rows = envelope.get("tasks") or []
        if not isinstance(rows, list):
            return False
        return any(
            isinstance(row, dict) and field_str(row.get("status")) == "needs_user" for row in rows
        )

    @staticmethod
    def _open_task_ids(open_tasks: list[dict[str, str]]) -> list[tuple[str, str]]:
        """``[(id, status), ...]`` over *open_tasks*, in envelope order —
        the ONE projection the body's id block and its populated
        ``tasks(...)`` calls are rendered from.

        A pair, not a row: the formatter cannot render a title it never
        receives, which is a structural guarantee where "the body carries
        no task text" was previously a rule written in three docstrings.
        The ``title`` and ``note`` :meth:`_open_tasks` normalises stay
        where they are — nothing on this path consumes them, and the
        shape contract is theirs to keep.

        Ordering is the envelope's own (append order, so oldest first),
        which is what makes a re-render of an unchanged list byte-stable.
        No filtering happens here: an id-less or ragged-id row is the
        FORMATTER's to reject, at the point of interpolation, because
        "is this id safe and runnable to render?" is a rendering
        question and answering it twice is how two answers start to
        disagree.  *open_tasks* must be :meth:`_open_tasks` output.
        """
        return [(t["id"], t["status"]) for t in open_tasks]

    @staticmethod
    def _open_counts(open_tasks: list[dict[str, str]]) -> dict[str, int]:
        """Per-status counts over *open_tasks* — the ONE derivation of
        the numbers both downstream consumers read (the counts line
        :func:`~turnstone.core.metacognition.format_idle_tasks_nudge`
        renders, and the FE card's ``counts`` metadata), so the body and
        the card cannot drift about what the coordinator was told.

        EVERY open status is present, zero counts included, so the
        counts line's shape is constant across coordinators — a line
        that dropped its zero terms would make "1 pending" ambiguous
        between "nothing in progress" and "in_progress not reported".
        The vocabulary is ``TASK_OPEN_STATUSES`` itself — imported,
        never restated — so a status added to the open set joins the
        line and the card with no edit here.  Built in sorted order for
        a deterministic mapping (the card serialises it as given); the
        formatter sorts again on render, so neither side depends on the
        other's ordering.  *open_tasks* must be :meth:`_open_tasks`
        output — rows already filtered to the open set — which is what
        makes ``sum(counts.values()) == len(open_tasks)`` hold: each
        open row holds exactly one open status.
        """
        return {
            status: sum(1 for t in open_tasks if t["status"] == status)
            for status in sorted(TASK_OPEN_STATUSES)
        }

    # ------------------------------------------------------------------
    # cap accounting
    # ------------------------------------------------------------------

    def _cap_reached(self, ws_id: str, nudge_type: str) -> bool:
        """Cheap advisory peek at this type's per-bracket budget.

        May be stale by the time the fire happens — :meth:`_try_charge`
        at the enqueue position is the authority.  The peek exists so a
        capped-out coord short-circuits before the message walk and the
        storage reads — the same peek-then-authoritative split the
        cooldown runs (``_cooldown_allows`` in the gate head,
        ``nudge_allowed`` at the commit tail).
        """
        with self._fire_counts_lock:
            ws_caps = self._fire_counts.get(ws_id, {})
            return ws_caps.get(nudge_type, 0) >= _NUDGE_TYPE_CAPS[nudge_type]

    def _try_charge(self, ws_id: str, nudge_type: str) -> bool:
        """Atomically check-and-charge one fire of *nudge_type*.

        The single lock hold closes the check-then-act window the
        separate peek + record pair left open (two concurrent IDLE
        events for one ws could both pass the peek and over-fire).
        Sits immediately before the enqueue — NOT at the peek position —
        so a refused fire (question heuristic, no children, a
        ``nudge_allowed`` refusal) never burns budget: the cap counts
        nudges enqueued, not IDLE events observed.  The enqueue after a
        successful charge cannot fail (the queue is unbounded and the
        channel literal is valid), so no rollback path is needed.
        """
        cap = _NUDGE_TYPE_CAPS[nudge_type]
        with self._fire_counts_lock:
            ws_caps = self._fire_counts.setdefault(ws_id, {})
            if ws_caps.get(nudge_type, 0) >= cap:
                return False
            ws_caps[nudge_type] = ws_caps.get(nudge_type, 0) + 1
            return True

    def _reset_caps_for(self, ws_id: str) -> None:
        """Drop every nudge-type cap counter for ``ws_id`` on a real
        (non-wake) leave-IDLE event — a real send drove the coord
        forward, so the next idle bracket's CAPS start fresh for both
        classes.  The advice cooldown stamp lives in
        ``session._metacog_state`` and deliberately survives this reset:
        cross-bracket damping is its whole job
        (:data:`_NUDGE_TYPE_HAS_COOLDOWN`).  Whole-ws pop keeps the O(1)
        ``ws_id in self._fire_counts`` fast path intact.
        """
        with self._fire_counts_lock:
            self._fire_counts.pop(ws_id, None)

    # ------------------------------------------------------------------
    # last-assistant-turn heuristics
    # ------------------------------------------------------------------

    @staticmethod
    def _last_assistant_turn(session: ChatSession) -> Turn | None:
        """The most recent ASSISTANT turn, or ``None`` for a fresh
        session.  Shared by both skip heuristics so the "first assistant
        turn walking back" rule lives in exactly one place — a future
        change to how that turn is located (skipping synthesized system
        turns, say) cannot fix one heuristic and silently leave the
        other on the old rule.
        """
        for msg in reversed(session.messages):
            if msg.role is Role.ASSISTANT:
                return msg
        return None

    def _last_assistant_used_wait(self, session: ChatSession) -> bool:
        """True when the coord's most recent assistant turn issued a
        ``wait_for_workstream`` tool call — it is already using the tool
        the liveness nudge would suggest.
        """
        msg = self._last_assistant_turn(session)
        if msg is None:
            return False
        return any(tc.name == "wait_for_workstream" for tc in msg.tool_calls)

    def _last_assistant_asked_operator(self, session: ChatSession) -> bool:
        """True when the coord's most recent assistant turn looks like a
        question put to the *operator* rather than a premature stop.

        A coord that ends its turn asking the operator something has
        stopped for the right reason, and nudging it to resume pushes it
        to answer its own question — the exact failure ``idle_tasks``
        exists to avoid.

        The heuristic is narrow on purpose.  A trailing ``?`` alone
        over-fires: a turn that called ``send_to_workstream`` and ended
        with a question was addressing a *child*, and a turn that ends
        "shall I check the config?" mid-work is rhetorical.  Requiring
        the turn to carry **no tool calls** removes the first class
        cheaply.  The second class survives, so this stays
        false-negative-biased by design: the cost of over-suppressing is
        the operator typing "continue", while the cost of
        under-suppressing is a coordinator guessing on a decision it
        correctly escalated.

        Reads ``Turn.text`` (which joins the turn's ``TextBlock``s)
        rather than ``Turn.content``, which is a tuple of content
        blocks.
        """
        msg = self._last_assistant_turn(session)
        if msg is None:
            return False
        if msg.tool_calls:
            return False
        return msg.text.rstrip().endswith("?")
