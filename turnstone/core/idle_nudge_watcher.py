"""Idle wake-trigger for the metacog NudgeQueue pipeline.

Hosts :class:`IdleNudgeWatcher` plus the
:func:`install_idle_nudge_watcher` / :func:`shutdown_idle_nudge_watchers`
lifespan helpers.  Pulled out of :mod:`turnstone.core.metacognition`
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
from turnstone.core.nudge_queue import USER_DRAIN
from turnstone.core.workstream import WorkstreamState

if TYPE_CHECKING:
    from collections.abc import Callable

    from turnstone.core.session_manager import SessionManager

log = get_logger(__name__)


class IdleNudgeWatcher:
    """Convert a workstream IDLE transition into a wake send when the
    session has queued nudges.

    Subscribes to :meth:`SessionManager.subscribe_to_state` and listens
    for ``WorkstreamState.IDLE``.  If the workstream's
    :class:`NudgeQueue` has any drainable entry for the wake's drain
    filter (``USER_DRAIN`` — channels ``"user"`` or ``"any"``),
    dispatches via ``session_worker.send`` with a no-op ``enqueue``
    callback.  Tool-only entries don't fire the wake — they belong to
    the next tool-result seam, not a synthetic empty user turn —
    otherwise every IDLE event with a queued tool advisory would spawn
    a wake daemon that immediately no-ops at
    ``deliver_wake_nudge_from_queue``'s drain guard.

    **Race semantics.**  ``session_worker.send`` decides atomically
    under ``ws._lock`` whether a worker thread already owns the
    workstream.  Three outcomes:

    * No worker → spawn a new daemon that calls
      :meth:`ChatSession.deliver_wake_nudge_from_queue` (the wake
      drains its own queue and runs the synthetic empty-user turn).
    * Worker running → call our ``enqueue`` lambda, which is a no-op.
      The wake is silently dropped; the queued nudge stays in
      ``NudgeQueue`` and the in-flight worker picks it up at its next
      user-message-attach or tool-result seam (whichever fires first
      for the entry's channel).  This is the load-bearing fallback —
      we never spawn a competing worker.
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
            if ws is None or ws.session is None:
                return
            session = ws.session
            if not session._nudge_queue.has_pending(USER_DRAIN):
                return
            session_worker.send(
                ws,
                enqueue=lambda: None,
                run=session.deliver_wake_nudge_from_queue,
                thread_name=f"wake-nudge-{ws.id[:8]}",
            )

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
