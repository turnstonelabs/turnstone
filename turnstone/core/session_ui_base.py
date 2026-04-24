"""Shared scaffolding for :class:`SessionUI` implementations.

Both :class:`turnstone.server.WebUI` (interactive node UI) and
:class:`turnstone.console.coordinator_ui.ConsoleCoordinatorUI` wrap a
:class:`~turnstone.core.session.ChatSession` and fan events out over
SSE to one or more connected browser tabs. They also block the worker
thread on two pending-input gates (tool approval, plan review) that
HTTP handlers resolve.

That skeleton lives here. Subclasses add kind-specific behaviour:

- ``WebUI`` adds per-UI token / activity metrics, broadcasts rich
  ``ws_state`` events with those metrics, and threads intent-verdict
  bookkeeping through ``resolve_approval``.
- ``ConsoleCoordinatorUI`` adds a class-level ``ClusterCollector``
  reference so ``on_rename`` fans out to the console dashboard.

Both keep their own ``on_*`` protocol method bodies — the enqueued
payload shapes differ (WebUI stamps per-UI metrics inline; console
coordinator UI keeps payloads minimal because the cluster collector
handles state-change fan-out separately).

This module intentionally does not satisfy
:class:`turnstone.core.session.SessionUI` by itself — the ``on_*``
methods are required by the Protocol and are subclass responsibility.
"""

from __future__ import annotations

import contextlib
import queue
import threading
from typing import Any

# Matches WebUI's historical listener queue size and the coordinator
# UI's ``_LISTENER_QUEUE_MAX``. Per-queue cap keeps a slow SSE consumer
# from bloating memory.
_DEFAULT_LISTENER_QUEUE_MAX = 500


class SessionUIBase:
    """SSE listener fan-out + approval/plan event machinery.

    Thread-safety: the ChatSession worker thread calls the ``on_*``
    methods (and the approval/plan blocking helpers that live on
    subclasses); HTTP handlers drive ``_register_listener`` /
    ``_unregister_listener`` / ``resolve_approval`` / ``resolve_plan``
    from the event loop. All shared state is guarded by
    ``_listeners_lock`` or ``threading.Event`` primitives.
    """

    def __init__(self, ws_id: str = "", user_id: str = "") -> None:
        self.ws_id = ws_id
        self._user_id = user_id
        # SSE listener fan-out — one queue per connected browser tab.
        self._listeners: list[queue.Queue[dict[str, Any]]] = []
        self._listeners_lock = threading.Lock()
        # Approval blocking — the worker thread calls approve_tools
        # which waits on _approval_event; the /approve endpoint sets
        # it via resolve_approval.
        self._approval_event = threading.Event()
        self._approval_result: tuple[bool, str | None] = (False, None)
        # Pending approval shape — re-sent on SSE reconnect so a user
        # switching tabs still sees the prompt.
        self._pending_approval: dict[str, Any] | None = None
        self._plan_event = threading.Event()
        self._plan_result: str = ""
        self._pending_plan_review: dict[str, Any] | None = None
        self.auto_approve = False
        self.auto_approve_tools: set[str] = set()
        # Foreground gate — used by the CLI's WorkstreamTerminalUI to
        # block output when the workstream is in the background.
        # Starts set so non-CLI UIs can skip any explicit management.
        # Also read by cleanup_session_ui so close() can unblock any
        # waiter.
        self._fg_event = threading.Event()
        self._fg_event.set()

    # ------------------------------------------------------------------
    # Listener plumbing (SSE)
    # ------------------------------------------------------------------

    def _enqueue(self, data: dict[str, Any]) -> None:
        """Fan ``data`` out to every registered listener queue.

        Stamps ``ws_id`` on the payload if not already present so the
        browser can validate it belongs to the pane's current
        workstream. Shallow-copies on stamp to avoid mutating a
        caller-owned dict.
        """
        if "ws_id" not in data:
            data = {**data, "ws_id": self.ws_id}
        with self._listeners_lock:
            snapshot = list(self._listeners)
        for lq in snapshot:
            with contextlib.suppress(queue.Full):
                lq.put_nowait(data)

    def _register_listener(
        self, maxsize: int = _DEFAULT_LISTENER_QUEUE_MAX
    ) -> queue.Queue[dict[str, Any]]:
        """Create a per-client queue and register it as a listener."""
        client_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=maxsize)
        with self._listeners_lock:
            self._listeners.append(client_queue)
        return client_queue

    def _unregister_listener(self, client_queue: queue.Queue[dict[str, Any]]) -> None:
        """Remove a client queue from the listener list."""
        with self._listeners_lock, contextlib.suppress(ValueError):
            self._listeners.remove(client_queue)

    # ------------------------------------------------------------------
    # Approval / plan blocking gates
    # ------------------------------------------------------------------

    def resolve_approval(self, approved: bool, feedback: str | None = None) -> None:
        """Unblock a pending approval with the caller's decision.

        Broadcasts ``approval_resolved`` so every connected tab can
        dismiss its prompt modal in sync (e.g. desktop dismisses when
        phone approves). Subclasses override to add side effects
        (e.g. intent-verdict bookkeeping on the interactive side) —
        they should call ``super().resolve_approval(...)`` at the end
        so any pre-resolve state updates land before the worker thread
        unblocks.
        """
        self._approval_result = (approved, feedback)
        self._enqueue(
            {
                "type": "approval_resolved",
                "approved": approved,
                "feedback": feedback or "",
            }
        )
        self._approval_event.set()

    def resolve_plan(self, feedback: str) -> None:
        """Unblock a pending plan review with the caller's verdict.

        ``cancel_generation`` calls this unconditionally to unblock
        any wait, so the path has to be safe when no plan is pending
        (just signal and skip the broadcast).
        """
        self._plan_result = feedback
        if self._pending_plan_review is None:
            self._plan_event.set()
            return
        # Clear pending BEFORE broadcasting so a client reconnecting
        # in the window between enqueue and clear cannot receive both
        # the replayed plan_review (SSE re-injection at the connect
        # handler) AND the live plan_resolved. Mirrors the
        # approval_resolved pattern above.
        self._pending_plan_review = None
        self._enqueue({"type": "plan_resolved", "feedback": feedback})
        self._plan_event.set()
