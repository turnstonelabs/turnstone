"""Idle wake-triggers for the metacog NudgeQueue pipeline.

Hosts the two wake entry points plus their lifespan helpers:

* :class:`IdleNudgeWatcher` — event-driven: a workstream transitions
  to IDLE while nudges are ALREADY queued.
* :func:`wake_workstream_if_pending` — the shared wake gate, also
  called directly by asynchronous producers that enqueue onto an
  ALREADY-idle workstream (the watch dispatch closure, via
  ``ChatSession.set_watch_runner``'s ``wake_fn``).  Such producers see
  no IDLE transition — the workstream has been idle all along — so
  the watcher alone would leave their entries queued until the next
  user message.

Pulled out of :mod:`turnstone.core.metacognition`
because the watcher is subscriber-lifecycle / runtime-orchestration
code with different concerns from the static nudge-text templates and
detection heuristics that live in metacognition; mixing them grew the
metacog module past its single-responsibility line.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

from turnstone.core import session_worker
from turnstone.core.log import get_logger
from turnstone.core.nudge_queue import WAKE_PENDING, NudgeQueue
from turnstone.core.workstream import WorkstreamState

if TYPE_CHECKING:
    from collections.abc import Callable

    from turnstone.core.session_manager import SessionManager
    from turnstone.core.workstream import Workstream

log = get_logger(__name__)


def wake_workstream_if_pending(ws: Workstream, *, trigger: str = "unspecified") -> bool:
    """Spawn a wake send for *ws* when it is idle with drainable nudges.

    The shared gate behind both wake triggers:

    * :class:`IdleNudgeWatcher` — the workstream just transitioned to
      IDLE with nudges already queued.
    * the watch dispatch closure (``ChatSession.set_watch_runner``'s
      ``wake_fn``) — a watch fired on a workstream that is ALREADY
      idle, so no IDLE transition will ever re-check the queue.

    *trigger* is a short label naming which path requested the wake
    (``"idle-transition"``, ``"watch-fire"``, ``"worker-exit"``); it is
    used only to tag the log lines below and never affects control flow.

    Gates, in order:

    * ``ws.session is None`` — workstream tracked but session not
      built — or a session whose ``_nudge_queue`` is not a real
      :class:`NudgeQueue` (bare stubs; mock sessions).  The wake
      contract REQUIRES real drain semantics: the spawned worker's
      ``deliver_wake_nudge_from_queue`` must actually CONSUME what
      ``has_pending`` saw, or the worker-exit backstop respawns wake
      workers forever — a mock queue's truthy ``has_pending`` plus a
      no-op deliver is exactly that storm, so the gate refuses on
      TYPE, not just presence.
    * ``ws._closed`` — ``close()`` already ran (or is racing us); its
      storage row says ``closed`` and a wake send would drive a
      torn-down session.  Lockless FAST-PATH only: a stale ``False``
      falls through to ``session_worker.send``, which re-checks
      ``_closed`` under ``ws._lock`` — the same lock ``close()`` sets
      it under — and refuses, so a wake racing a close can never spawn
      a worker on the torn-down session.
    * ``ws.state is not IDLE`` — a busy workstream's worker drains the
      queue at its own seams (``ATTENTION``/``THINKING``/``RUNNING``
      all imply a live worker), and ``ERROR`` stays parked for the
      operator rather than burning inference unattended.
    * nothing gate-eligible under ``WAKE_PENDING`` — tool-only/quiet entries
      belong to the next tool-result seam, not a synthetic empty user
      turn (``deliver_wake_nudge_from_queue`` would no-op on them).

    Past the gates, exactly one info line is emitted per call:

    * ``nudge_wake.deferred_worker_busy`` — the reuse-path drop: a
      worker owned the workstream, so ``session_worker.send`` called
      the no-op ``enqueue`` instead of spawning.  The entry stays
      queued; the owning worker's exit backstop (or its next drain
      seam) delivers it.
    * ``nudge_wake.dispatched`` — a fresh wake daemon was spawned.
    * ``nudge_wake.refused`` — ``session_worker.send`` declined the
      spawn: its authoritative under-lock ``_closed`` re-check caught a
      teardown this gate's lockless peek missed.  The entry dies with
      the workstream; logged here so a dropped wake is traceable to its
      trigger during production troubleshooting.

    Returns ``True`` iff the wake was handed to
    ``session_worker.send`` — which may still downgrade it to a no-op
    enqueue when a worker owns the workstream (see the race-semantics
    section on :class:`IdleNudgeWatcher`).
    """
    session = ws.session
    if session is None or ws._closed or ws.state is not WorkstreamState.IDLE:
        return False
    nudge_queue = getattr(session, "_nudge_queue", None)
    # Gate on WAKE_PENDING, not USER_DRAIN: ``"quiet"`` entries (external
    # events demoted by a user cancel) deliver at the next legitimate seam
    # but must never themselves wake the workstream the user just stopped.
    if not isinstance(nudge_queue, NudgeQueue) or not nudge_queue.has_pending(WAKE_PENDING):
        return False

    deferred = False

    def _noop_enqueue() -> None:
        nonlocal deferred
        deferred = True

    ok = session_worker.send(
        ws,
        enqueue=_noop_enqueue,
        run=session.deliver_wake_nudge_from_queue,
        thread_name=f"wake-nudge-{ws.id[:8]}",
    )
    if deferred:
        log.info("nudge_wake.deferred_worker_busy ws=%s trigger=%s", ws.id[:8], trigger)
    elif ok:
        log.info("nudge_wake.dispatched ws=%s trigger=%s", ws.id[:8], trigger)
    else:
        log.info("nudge_wake.refused ws=%s trigger=%s", ws.id[:8], trigger)
    return ok


class IdleNudgeWatcher:
    """Convert a workstream IDLE transition into a wake send when the
    session has queued nudges.

    Subscribes to :meth:`SessionManager.subscribe_to_state` and listens
    for ``WorkstreamState.IDLE``, then defers to
    :func:`wake_workstream_if_pending` (the shared gate — see its
    docstring for the full gate order).  If the workstream's
    :class:`NudgeQueue` has any drainable entry for the wake's drain
    gate (``WAKE_PENDING`` — channels ``"user"`` or ``"any"``), the
    gate dispatches via ``session_worker.send`` with a no-op
    ``enqueue`` callback.  Tool-only entries don't fire the wake —
    they belong to the next tool-result seam, not a synthetic empty
    user turn — otherwise every IDLE event with a queued tool advisory
    would spawn a wake daemon that immediately no-ops at
    ``deliver_wake_nudge_from_queue``'s drain guard.

    This watcher only covers nudges that are already queued when the
    IDLE transition fires.  Producers that enqueue asynchronously onto
    an already-idle workstream (watch fires) call
    :func:`wake_workstream_if_pending` themselves — there is no state
    transition for this watcher to observe in that case.

    **Race semantics.**  ``session_worker.send`` decides atomically
    under ``ws._lock`` whether a worker thread already owns the
    workstream.  Three outcomes:

    * No worker → spawn a new daemon that calls
      :meth:`ChatSession.deliver_wake_nudge_from_queue` (the wake
      drains its own queue and runs the synthetic empty-user turn).
    * Worker running → call our ``enqueue`` lambda, which is a no-op.
      The wake is silently dropped; the queued nudge stays in
      ``NudgeQueue``.  We never spawn a competing worker.  This branch
      is the COMMON case for IDLE-transition wakes, not the exception:
      ``set_state`` subscribers fire on the calling thread, and IDLE
      is emitted from inside ``run()`` at the end of a send — so the
      transitioning worker still owns the flag while this watcher
      dispatches.  Delivery is then owed to one of two follow-ups:
      the in-flight worker's next drain seam (when IDLE fired
      mid-turn), or — for the end-of-send case, where no later seam
      exists — ``session_worker``'s ownership-clear backstop
      (``_retry_pending_wake``), which re-runs
      :func:`wake_workstream_if_pending` the moment the worker exits.
    * Workstream gone (``ws is None``) or session not built
      (``ws.session is None``) → bail.

    **Subscription order matters.**  When a workstream-kind-specific
    observer (e.g. ``CoordinatorIdleObserver``) needs to *enqueue* a
    nudge on the same IDLE event before this watcher *peeks* the
    queue, the observer must register first so that
    ``SessionManager.set_state``'s subscriber loop fires it earlier in
    the same synchronous fan-out.

    **Kind-agnostic.**  Fires for any workstream regardless of
    :class:`WorkstreamKind`.  Producers decide what to enqueue.
    """

    def __init__(self, manager: SessionManager) -> None:
        self._manager = manager
        self._callback: Callable[[str, WorkstreamState], None] | None = None

    def start(self) -> None:
        """Idempotent — registering twice is a no-op."""
        if self._callback is not None:
            return

        def _on_state(ws_id: str, state: WorkstreamState) -> None:
            if state is not WorkstreamState.IDLE:
                return
            ws = self._manager.get(ws_id)
            if ws is None:
                return
            wake_workstream_if_pending(ws, trigger="idle-transition")

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


_APP_STATE_ATTR = "_idle_nudge_watchers"


def install_idle_nudge_watcher(app: Any, manager: SessionManager) -> IdleNudgeWatcher:
    """Construct + start an :class:`IdleNudgeWatcher` and register it
    for lifespan teardown via :func:`shutdown_idle_nudge_watchers`.

    Multiple watchers may be installed against different
    :class:`SessionManager` instances on the same ``app`` (e.g. the
    interactive manager + the coord manager on a multi-kind host).
    All of them get torn down by a single
    :func:`shutdown_idle_nudge_watchers` call.

    Returns the watcher so the caller can run additional setup
    against the same manager — but the typical site doesn't need
    the return value.
    """
    watcher = IdleNudgeWatcher(manager)
    watcher.start()
    watchers: list[IdleNudgeWatcher] = getattr(app.state, _APP_STATE_ATTR, [])
    if not watchers:
        # First watcher on this app — initialise the list.  Avoids
        # mutating a default arg or sharing the list across apps.
        setattr(app.state, _APP_STATE_ATTR, watchers)
    watchers.append(watcher)
    return watcher


def shutdown_idle_nudge_watchers(app: Any) -> None:
    """Shut down every watcher installed via
    :func:`install_idle_nudge_watcher`.  No-op if none.
    """
    watchers: list[IdleNudgeWatcher] = getattr(app.state, _APP_STATE_ATTR, [])
    for watcher in watchers:
        watcher.shutdown()
    watchers.clear()
