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

This module owns ONLY the dispatch decision, the ``_worker_running``
lifecycle, and the ownership-clear wake backstop
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

import queue
import threading
from typing import TYPE_CHECKING

from turnstone.core.log import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

    from turnstone.core.workstream import Workstream

log = get_logger(__name__)


def _retry_pending_wake(ws: Workstream) -> None:
    """Deliver nudges that arrived while the exiting worker owned *ws*.

    Runs in the worker's ``finally`` immediately after it cleared
    ``_worker_running`` (owner only — abandoned threads skip it).  The
    canonical strand it closes: the coordinator's ``idle_children``
    nudge, enqueued by ``CoordinatorIdleObserver`` during the IDLE
    fan-out at the end of the coord's send — the fan-out runs on the
    worker thread, so ``IdleNudgeWatcher``'s wake dispatch hits the
    reuse path and no-ops, and nothing else ever re-checks the queue.
    The same window covers a watch ``wake_fn`` firing while a worker
    is mid-exit.

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
        wake_workstream_if_pending(ws, trigger="worker-exit")
    except Exception:
        log.warning("session_worker.wake_retry_failed ws=%s", ws.id[:8], exc_info=True)


def send(
    ws: Workstream,
    *,
    enqueue: Callable[[], None],
    run: Callable[[], None],
    thread_name: str | None = None,
    worker_kind: str = "turn",
) -> bool:
    """Dispatch work onto a workstream's worker thread.

    Reuses a live worker via ``enqueue()`` when one is running; spawns
    a fresh daemon thread running ``run()`` otherwise. The
    check-and-spawn is atomic under ``ws._lock`` keyed on
    ``ws._worker_running`` (set before lock release, cleared in the
    spawned thread's ``finally`` block).

    Both callbacks are no-arg closures — callers close over the
    ``ChatSession`` they want to drive, so the worker can't be racing a
    concurrent ``ws.session`` swap.

    ``worker_kind`` classifies what the slot holds — ``"turn"`` (send /
    retry / wake / init, the default) or ``"command"`` (slash-command
    workers, including the minutes-long manual /compact).  Written to
    ``ws.worker_kind`` in the same lock acquisition as the
    ``(worker_thread, _worker_running)`` pair.  This is the ONLY site
    that sets ``_worker_running=True``, so the classification cannot be
    bypassed by a new dispatch caller.  The /send route parks while a
    command holds the slot instead of taking the interjection-queue
    path (whose length cap / cross-user guard are turn semantics); an
    ``enqueue`` callback that can fire during a command window (the
    coordinator adapter's, the init race's) must refuse rather than
    queue — see the command-window refusals at those closures.

    The refusal is DELIBERATELY not centralized here despite the three
    hand-written guards: each surface needs a different refusal channel
    (the /send route signals "re-park" via its ``queue_outcome`` flag;
    the coordinator adapter and the init race raise ``queue.Full`` into
    their existing backpressure statuses), and a central refusal inside
    this function can only return ``False`` — indistinguishable from
    queue-full/closed for the route's re-park decision and mislabeled by
    the init path's status derivation.  Making it distinguishable means
    a tri-state contract change across every dispatch caller, which is
    more surface than three four-line guards.  If you add a NEW enqueue
    closure that can queue turn work, copy the guard.

    Returns:
        ``True`` on successful enqueue (existing worker accepted) or
        thread spawn (no live worker).
        ``False`` when the workstream is already closed (see below), or
        when ``enqueue`` raises ``queue.Full`` (queue at capacity —
        caller surfaces 429) or any other exception (logged). Falling
        through to spawn a second worker on a full queue would corrupt
        ChatSession state.
    """
    name = thread_name or f"session-worker-{ws.id[:8]}"

    def _runner() -> None:
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
                    was_owner = True
            # Outside the lock (the retry's wake dispatch re-acquires it).
            # Owner only: an abandoned thread retrying would race the
            # successor's own exit backstop for no benefit.
            if was_owner:
                _retry_pending_wake(ws)

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
        # Set ``_worker_running`` AND assign ``ws.worker_thread`` under
        # the same lock acquisition — readers gating on either flag see
        # a coherent (worker_thread, _worker_running) pair. Without
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
        t = threading.Thread(target=_runner, name=name, daemon=True)
        ws.worker_thread = t
    # ``t.start()`` may run user code (worker body) before returning;
    # keep it outside the lock to avoid pinning ``ws._lock`` for the
    # full thread-creation cost.
    t.start()
    return True
