"""Shared worker-thread dispatch for SessionManager workstreams.

Both the interactive ``/v1/api/send`` HTTP handler and the coordinator
``CoordinatorAdapter.send`` need the same atomic check-and-(spawn-or-queue)
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

This module owns ONLY the dispatch decision and the
``_worker_running`` lifecycle. Per-kind concerns — session resolution,
attachments reservation, error surfacing, UI callbacks,
``GenerationCancelled`` handling — live in the caller's
``enqueue`` / ``run`` no-arg closures.
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


def send(
    ws: Workstream,
    *,
    enqueue: Callable[[], None],
    run: Callable[[], None],
    thread_name: str | None = None,
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

    Returns:
        ``True`` on successful enqueue (existing worker accepted) or
        thread spawn (no live worker).
        ``False`` when ``enqueue`` raises ``queue.Full`` (queue at
        capacity — caller surfaces 429) or any other exception
        (logged). Falling through to spawn a second worker on a full
        queue would corrupt ChatSession state.
    """
    with ws._lock:
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
        # Mark before releasing the lock so a concurrent caller observes
        # the running state and takes the enqueue path instead of
        # spawning a parallel worker.
        ws._worker_running = True

    name = thread_name or f"session-worker-{ws.id[:8]}"

    def _runner() -> None:
        try:
            run()
        except BaseException:
            # Per-kind callers wrap their own try/except inside ``run``
            # for typed surfacing (UI on_error, GenerationCancelled,
            # reservation cleanup). This catch is defense-in-depth —
            # ensures ``_worker_running`` is always cleared even if a
            # caller forgets to handle a new exception class.
            log.exception("session_worker.uncaught ws=%s", ws.id[:8])
        finally:
            with ws._lock:
                ws._worker_running = False

    t = threading.Thread(target=_runner, name=name, daemon=True)
    ws.worker_thread = t
    t.start()
    return True
