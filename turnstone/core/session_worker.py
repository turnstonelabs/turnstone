"""Shared worker-thread dispatch for SessionManager workstreams.

Both the interactive ``/v1/api/workstreams/{ws_id}/send`` HTTP handler
and the coordinator ``CoordinatorAdapter.send`` need the same atomic
check-and-(spawn-or-queue)
on a workstream: if a worker thread is already driving
:meth:`ChatSession.send`, append the new message to its pending queue;
otherwise spawn a fresh daemon thread. The decision is taken under
``ws._lock`` keyed on ``ws._worker_running`` so two concurrent senders
can never spawn parallel workers on the same ChatSession (mutating
history, queued messages, streaming state and approvals).

The bug history this guards is documented in
``1.5.0-session-manager-stage-1.md`` (bug-1, bug-2): using
``Thread.is_alive()`` as the gate was racy — the worker could exit
between the check and a ``queue_message`` call, stranding the message
with no consumer. The flag transitions atomically inside the same lock
this module holds, so both coord and interactive callers inherit the
fix.

This module owns ONLY the dispatch decision, immutable slot-time session
claim, the ``_worker_running``/force-abandonable lifecycle, and the
ownership-clear wake backstop
(:func:`_retry_pending_wake`). Per-kind concerns — session resolution,
attachment resolution, error surfacing, UI callbacks,
``GenerationCancelled`` handling — live in the caller's
``enqueue`` / ``run`` no-arg closures.

The wake backstop exists because IDLE state fans out from INSIDE
``run()`` (``set_state`` subscribers fire on the calling thread — the
worker that did the transition).  Any wake the IDLE fan-out dispatches
(``IdleNudgeWatcher``) therefore lands on the reuse path while this
worker still owns the flag and no-ops; with IDLE emitted at the END of
a send there is no later seam in this worker to drain the queue, so
the nudge would strand until the next user message.  Re-running the
wake gate at the exact moment ownership clears is the only spot that
closes the window without ever racing a competing worker.
"""

from __future__ import annotations

import contextvars
import dataclasses
import queue
import threading
from typing import TYPE_CHECKING, Any

from turnstone.core.log import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

    from turnstone.core.workstream import WorkerKind, Workstream

log = get_logger(__name__)


def foreign_queue_conflict(session: object, principal_id: str) -> bool:
    """Shared ``before_spawn`` predicate for every fresh-turn dispatch gate.

    True when another participant's persistence-retained queued input must
    refuse this spawn. One predicate serves the /send route, the destructive
    retry dispatcher, and the coordinator adapter — a change to which owners
    count as foreign lands once (round-5 review: three hand copies).
    Per-surface refusal REPORTING (409 status, outcome dict, log line) stays
    at the call sites. Unauthenticated dispatch (empty principal) passes: the
    partitioned pop retains foreign rows structurally, so the gate is a
    courtesy refusal for the authenticated lanes, not the enforcement.
    """
    from turnstone.core.workstream import concrete_method

    if not principal_id:
        return False
    conflict = concrete_method(session, "has_foreign_queued_messages")
    return conflict is not None and bool(conflict(principal_id))


def claimed_slot_queue_admission(
    ws: Workstream,
    acting_user_id: str,
) -> tuple[str, dict[str, Any]] | None:
    """Slot-owner queue admission shared by /send and the coordinator adapter.

    Called under ``ws._lock``. Returns ``(claimed_principal, base queue
    kwargs)`` threading the immutable slot owner as ``turn_principal_id`` —
    or ``None`` when a DIFFERENT authenticated participant holds the slot:
    the claim captured with the slot is authoritative even before the new
    worker thread finishes rebinding the ChatSession, whose sticky actor may
    still name the previous turn (round-5 review: two hand copies of this
    comparison + threading).
    """
    claimed_principal = ws._worker_principal_id
    if acting_user_id and claimed_principal and acting_user_id != claimed_principal:
        return None
    kwargs: dict[str, Any] = {"interjector_user_id": acting_user_id}
    if claimed_principal:
        kwargs["turn_principal_id"] = claimed_principal
    return claimed_principal, kwargs


def release_slot_locked(ws: Workstream) -> None:
    """Return the worker slot to its unclaimed state — THE release field set.

    Caller holds ``ws._lock`` and has already verified the release is
    legitimate (thread-identity / abandonable checks are per-site policy;
    the four-field invariant is owned here).  This module declares itself
    sole owner of the slot lifecycle: a claim field added to the spawn
    branch must land here in the same change, or a released slot keeps a
    stale value that the next admission reads as live (e.g. queue
    admission comparing against a departed principal).
    """
    ws.worker_thread = None
    ws._worker_running = False
    ws._worker_principal_id = ""
    ws._worker_force_abandonable = True


def reclassify_slot_locked(
    ws: Workstream,
    *,
    worker_kind: WorkerKind,
    principal_id: str,
    force_abandonable: bool,
) -> None:
    """Reclassify a HELD slot in place — the retry command→turn flip.

    Caller holds ``ws._lock`` and has verified it still owns the slot.
    Same single-ownership rule as :func:`release_slot_locked`: the
    classification field set lives here so the spawn branch and the
    reclassify site cannot drift.
    """
    ws.worker_kind = worker_kind
    ws._worker_principal_id = principal_id.strip()
    ws._worker_force_abandonable = force_abandonable


@dataclasses.dataclass(frozen=True, slots=True)
class WorkerClaim:
    """Immutable ChatSession admission captured with a fresh worker slot.

    ``session`` is an identity fence, not retained application state: it keeps
    a nested/direct send for another session from accidentally consuming this
    thread's claim. ``cancel_epoch`` linearizes the slot claim with Stop and
    terminal admission. ``cancel_event`` plus its captured state detects an
    exceptional structural poison that lands after capture without rejecting
    an Event already set by a completed history truncation.
    """

    session: object = dataclasses.field(repr=False)
    principal_id: str
    cancel_epoch: int
    cancel_event: threading.Event = dataclasses.field(repr=False)
    cancel_event_was_set: bool


_active_worker_claim: contextvars.ContextVar[WorkerClaim | None] = contextvars.ContextVar(
    "turnstone_active_worker_claim",
    default=None,
)


def current_worker_claim(session: object) -> WorkerClaim | None:
    """Return this thread's slot-time claim when it belongs to *session*."""

    claim = _active_worker_claim.get()
    return claim if claim is not None and claim.session is session else None


def _retry_pending_wake(
    ws: Workstream,
    *,
    exclude_interjection_signature: object | None = None,
) -> None:
    """Deliver nudges or a tail-raced interjection after ownership clears.

    Runs in the worker's ``finally`` immediately after it cleared
    ``_worker_running`` (owner only — abandoned threads skip it).  The
    canonical strand it closes: the coordinator's ``idle_children``
    nudge, enqueued by ``CoordinatorIdleObserver`` during the IDLE
    fan-out at the end of the coord's send — the fan-out runs on the
    worker thread, so ``IdleNudgeWatcher``'s wake dispatch hits the
    reuse path and no-ops, and nothing else ever re-checks the queue.
    The same window covers a watch ``wake_fn`` firing while a worker
    is mid-exit.

    The same seam closes a user-send race: an interjection can enqueue after
    ``ChatSession.send`` performed its final flush but before this runner's
    ``finally`` clears the slot.  The worker-exit call uniquely enables the
    gate's session-owned queue snapshot claim; ordinary idle/watch callers
    remain nudge-only, and a restored failed snapshot is not retried in a loop.

    The wake gate
    (:func:`~turnstone.core.idle_nudge_watcher.wake_workstream_if_pending`)
    owns every defensive check — session missing, bare stub without a
    NudgeQueue (watch-style dispatchers drive sessions that aren't
    installed on the workstream), closed, non-idle, nothing pending —
    and its ``session_worker.send`` dispatch is the same atomic spawn
    as any other: a successor worker claimed between our flag-clear
    and the retry just downgrades the wake to a no-op enqueue again,
    and THAT worker's own exit re-runs this backstop.  Convergence is
    owned by the producers' gates (cooldown, hard caps, ``valid_until``
    predicates): a wake worker whose drain empties the queue retries
    once at its own exit, sees nothing pending, and stops.
    """
    # Local import: idle_nudge_watcher imports this module at top level.
    from turnstone.core.idle_nudge_watcher import wake_workstream_if_pending

    try:
        wake_workstream_if_pending(
            ws,
            trigger="worker-exit",
            include_interjections=True,
            exclude_interjection_signature=exclude_interjection_signature,
        )
    except Exception:
        log.warning("session_worker.wake_retry_failed ws=%s", ws.id[:8], exc_info=True)


def send(
    ws: Workstream,
    *,
    enqueue: Callable[[], None],
    run: Callable[[], None],
    expected_session: object | None = None,
    before_spawn: Callable[[], bool] | None = None,
    thread_name: str | None = None,
    worker_kind: WorkerKind = "turn",
    principal_id: str = "",
    force_abandonable: bool = True,
    interjection_wake_signature: object | None = None,
) -> bool:
    """Dispatch work onto a workstream's worker thread.

    Reuses a live worker via ``enqueue()`` when one is running; spawns
    a fresh daemon thread running ``run()`` otherwise. The
    check-and-spawn is atomic under ``ws._lock`` keyed on
    ``ws._worker_running`` (set before lock release, cleared in the
    spawned thread's ``finally`` block).

    Both callbacks are no-arg closures. Production callers pass the exact
    closure-bound ``ChatSession`` as ``expected_session``; identity is checked
    under ``ws._lock`` before either callback can run, so a concurrent
    ``ws.session`` swap refuses the stale dispatch. The optional default keeps
    low-level compatibility sessions and test doubles on their legacy path.

    ``worker_kind`` classifies what the slot holds — ``"turn"`` (send /
    retry / wake / init, the default) or ``"command"`` (slash-command
    workers, including the minutes-long manual /compact).  Written to
    ``ws.worker_kind`` in the same lock acquisition as the
    ``(worker_thread, _worker_running)`` pair.  This is the ONLY site
    that sets ``_worker_running=True``, so the classification cannot be
    bypassed by a new dispatch caller.  The /send route DEFERS while a
    command holds the slot instead of taking the interjection-queue
    path (whose length cap / cross-user guard are turn semantics): its
    enqueue closure reports the window and the route registers the send
    on ``ws._pending_sends`` for the drain task to dispatch when the
    window closes.  An ``enqueue`` callback that can fire during a
    command window (the coordinator adapter's, the init race's) must
    refuse rather than queue — see the command-window refusals at those
    closures.

    The refusal is DELIBERATELY not centralized here despite the
    hand-written guards: each surface needs a different refusal channel
    (the /send route's closure signals "command window" via its
    ``queue_outcome`` flag — the defer trigger; the coordinator adapter
    and the init race raise ``queue.Full`` into their existing
    backpressure statuses), and a central refusal inside this function
    can only return ``False`` — indistinguishable from queue-full/closed
    exactly where the route must distinguish "defer this" from "drop
    this".  Making it distinguishable means a tri-state contract change
    across every dispatch caller — more surface than the guards it
    replaces.  (A check inside this function's locked section would be
    race-free; the cost is the contract change, not atomicity.)  If you
    add a NEW enqueue closure that can queue turn work, copy the guard.

    ``principal_id`` is the authenticated owner of a fresh turn slot. It is
    installed atomically with the worker claim, before the spawned thread does
    the broader ChatSession actor rebind. Queueing callers can therefore
    reject a different participant without consulting stale mutable session
    identity. Internal and unauthenticated workers leave it empty.

    ``force_abandonable=False`` reserves the slot across operator force-cancel.
    Use it for destructive history/lifecycle mutations whose half-completed
    transaction cannot safely overlap a successor. The cancel path may still
    signal cooperative cancellation, but must leave the worker/thread/slot
    claim intact until this runner's owner-conditional ``finally`` clears it.

    ``interjection_wake_signature`` is the exact queue snapshot that caused a
    queue-only wake spawn.  It is carried only by that successfully spawned
    runner; on exit the backstop excludes the same restored snapshot, avoiding
    a preamble-failure hot loop while still admitting any changed queue.  A
    reuse/refusal/spawn-error path starts no runner and therefore installs no
    suppression that could hide work from a competing real worker's exit.

    ``before_spawn`` is an optional admission check that runs under the same
    ``ws._lock`` acquisition immediately before a fresh slot is claimed.  It
    may inspect queue-owner state, but must not acquire the session generation
    lock or manager state locks: generation commits can publish through the UI
    while holding that lock and then acquire ``ws._lock``. Returning ``False``
    refuses the dispatch without spawning. Reuse-path admission remains the
    caller's ``enqueue`` responsibility.

    Returns:
        ``True`` on successful enqueue (existing worker accepted) or
        thread spawn (no live worker).
        ``False`` when the workstream is already closed (see below), or
        when ``enqueue`` raises ``queue.Full`` (queue at capacity —
        caller surfaces 429) or any other exception (logged). Falling
        through to spawn a second worker on a full queue would corrupt
        ChatSession state.

    Raises:
        Whatever ``Thread.start()`` raised when the spawn itself fails
        (thread exhaustion, ``MemoryError``) — the slot claim is rolled
        back first, so the workstream stays dispatchable once resources
        recover instead of wedging behind a flag no thread will clear.
    """
    name = thread_name or f"session-worker-{ws.id[:8]}"

    worker_claim: WorkerClaim | None = None

    # Capture the ChatSession's monotonic cancellation edge before taking the
    # workstream lock. Generation commits can reach UI state publication while
    # holding ``_generation_lock`` and then acquire ``ws._lock``; doing this
    # capture in the opposite order creates a concrete AB/BA deadlock. A Stop
    # or terminal transition between this conservative snapshot and slot
    # installation only advances the epoch, so send entry rejects the stale
    # witness rather than erasing that lifecycle edge.
    claim_session = expected_session if expected_session is not None else ws.session
    capture_claim = getattr(claim_session, "_capture_worker_claim", None)
    if callable(capture_claim):
        try:
            worker_claim = capture_claim(principal_id)
        except Exception:
            log.info(
                "session_worker.claim_refused ws=%s",
                ws.id[:8],
                exc_info=True,
            )
            return False

    def _runner() -> None:
        claim_token = _active_worker_claim.set(worker_claim)
        try:
            run()
        except Exception:
            # Per-kind callers wrap their own try/except inside ``run``
            # for typed surfacing (UI on_error, GenerationCancelled,
            # reservation cleanup). This catch is defense-in-depth —
            # ensures ``_worker_running`` is always cleared even if a
            # caller forgets to handle a new exception class. Daemon
            # threads don't receive SystemExit/KeyboardInterrupt, so
            # ``Exception`` is sufficient — no need to widen to
            # ``BaseException`` (and accidentally catch generator-
            # close style signals if the runtime ever delivers them).
            log.exception("session_worker.uncaught ws=%s", ws.id[:8])
        finally:
            _active_worker_claim.reset(claim_token)
            was_owner = False
            with ws._lock:
                # Only clear the flag if THIS thread is still the current
                # worker.  A force-cancel abandons the worker
                # (``ws.worker_thread = None``) and a follow-up send may
                # already have spawned a successor (``ws.worker_thread`` =
                # the new thread); an abandoned thread finishing late must
                # not clear the flag out from under that live successor —
                # else a third send sees ``_worker_running=False`` and
                # spawns a second concurrent worker on the same session.
                if ws.worker_thread is threading.current_thread():
                    ws._worker_running = False
                    ws._worker_principal_id = ""
                    ws._worker_force_abandonable = True
                    was_owner = True
            # Outside the lock (the retry's wake dispatch re-acquires it).
            # Owner only: an abandoned thread retrying would race the
            # successor's own exit backstop for no benefit.
            if was_owner:
                _retry_pending_wake(
                    ws,
                    exclude_interjection_signature=interjection_wake_signature,
                )

    with ws._lock:
        if ws._closed:
            # Authoritative closed-check: ``SessionManager.close`` sets
            # ``_closed`` under this same lock, so unlike the wake gate's
            # lockless peek this read cannot go stale.  Without it, a
            # wake (or send) racing ``close()`` spawns a worker that runs
            # a full unattended turn — inference, tool calls, storage
            # writes — on a workstream whose ``ws_closed`` already fired.
            log.info("session_worker.closed_refused ws=%s", ws.id[:8])
            return False
        if expected_session is not None and ws.session is not expected_session:
            # Callbacks close over ``expected_session``. This identity fence is
            # required even for compatibility sessions that expose no
            # WorkerClaim: capturing a valid witness from a replacement cannot
            # authorize mutations through closures still bound to its detached
            # predecessor.
            log.info("session_worker.session_swap_refused ws=%s", ws.id[:8])
            return False
        if isinstance(worker_claim, WorkerClaim):
            # The claim was captured before ``ws._lock`` to preserve the
            # generation -> UI -> workstream lock order. A predecessor can
            # poison structural cleanup in that gap and then either still own
            # this slot (enqueue arm) or release it (spawn arm). Revalidate
            # only through ChatSession's explicitly lock-free witness check;
            # reacquiring its generation lock here would invert that order.
            revalidate = getattr(worker_claim.session, "_worker_claim_is_current", None)
            try:
                claim_is_current = bool(
                    worker_claim.session is ws.session
                    and callable(revalidate)
                    and revalidate(worker_claim)
                )
            except Exception:
                claim_is_current = False
            if not claim_is_current:
                log.info("session_worker.stale_claim_refused ws=%s", ws.id[:8])
                return False
        if ws._worker_running:
            try:
                enqueue()
                return True
            except queue.Full:
                # Existing worker still alive but queue at capacity —
                # spawning a second thread on the same ChatSession
                # would corrupt history / cursors / approvals. Surface
                # backpressure to the caller.
                log.warning(
                    "session_worker.queue_full ws=%s — message dropped (worker still busy)",
                    ws.id[:8],
                )
                return False
            except Exception:
                log.warning(
                    "session_worker.queue_failed ws=%s",
                    ws.id[:8],
                    exc_info=True,
                )
                return False
        if before_spawn is not None:
            try:
                if not before_spawn():
                    return False
            except Exception:
                log.warning(
                    "session_worker.spawn_admission_failed ws=%s",
                    ws.id[:8],
                    exc_info=True,
                )
                return False
        # Set ``_worker_running``, actor identity, and ``ws.worker_thread``
        # under the same lock acquisition — readers gating on the running
        # flag see one coherent slot claim. Without
        # this, a reader could observe ``_worker_running=True`` while
        # ``ws.worker_thread`` still points at the previous (already-
        # exited) thread, breaking every ``ws.worker_thread is me``
        # identity check downstream.
        #
        # Thread() construction stays inside the lock so we don't
        # allocate one on the enqueue path (a hot path for busy
        # workstreams). The constructor is microsecond-cheap, so the
        # lock-window cost is dominated by the spawn branch's identity
        # write either way.
        ws._worker_running = True
        ws.worker_kind = worker_kind
        ws._worker_principal_id = principal_id.strip()
        ws._worker_force_abandonable = force_abandonable
        t = threading.Thread(target=_runner, name=name, daemon=True)
        ws.worker_thread = t
    # ``t.start()`` may run user code (worker body) before returning;
    # keep it outside the lock to avoid pinning ``ws._lock`` for the
    # full thread-creation cost.
    try:
        t.start()
    except Exception:
        # Thread creation failed (RuntimeError under thread exhaustion,
        # MemoryError): the slot was claimed under the lock above, but the
        # flag's normal clearer is ``_runner``'s finally — on a thread
        # that will never run.  Without this release, ``_worker_running``
        # stays True forever on a workstream that LOOKS idle (the worker
        # never fired a state change), every future dispatch takes the
        # reuse path into a queue with no consumer, and only an operator
        # force-cancel unwedges it.  Identity-guarded like ``_runner``'s
        # clear: a concurrent force-cancel may already have cleared or
        # replaced the slot, and this must not clobber a live successor.
        # Re-raise rather than return False: callers' crash paths (the
        # deferred-send drain's per-iteration handler with its backoff)
        # are shaped for exceptions, and a False here would masquerade as
        # queue-full backpressure.
        with ws._lock:
            if ws.worker_thread is t:
                release_slot_locked(ws)
        log.exception("session_worker.spawn_failed ws=%s — slot released", ws.id[:8])
        raise
    return True
