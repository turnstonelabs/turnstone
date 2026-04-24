"""Shared ``cleanup_ui`` body for ``SessionKindAdapter`` implementations.

Both :class:`turnstone.core.adapters.interactive_adapter.InteractiveAdapter`
and :class:`turnstone.console.coordinator_adapter.CoordinatorAdapter`
drive the exact same cleanup sequence on close: unblock any pending
approval / plan / foreground event on the UI, broadcast ``ws_closed``
to every per-UI listener queue so SSE generators unwind promptly, then
cancel + close the session. The implementations were byte-identical
duplicates — pull them into one function both adapters delegate to.
"""

from __future__ import annotations

import contextlib
import queue
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from turnstone.core.session import SessionUI
    from turnstone.core.workstream import Workstream


def cleanup_session_ui(ws: Workstream) -> None:
    """Shared SessionKindAdapter cleanup_ui implementation.

    Unblocks pending approval / plan / foreground events on the
    workstream's UI, broadcasts ``ws_closed`` to per-UI listener
    queues, then cancels + closes the session. The ``hasattr`` checks
    guard stub UIs used in tests — the real ``WebUI`` /
    ``ConsoleCoordinatorUI`` always have these attributes.
    """
    if ws.session is not None and hasattr(ws.session, "cancel"):
        ws.session.cancel()
    ui = ws.ui
    if ui is not None:
        if hasattr(ui, "_approval_event"):
            ui._approval_result = False, None  # type: ignore[attr-defined]
            ui._approval_event.set()
        if hasattr(ui, "_plan_event"):
            ui._plan_result = "reject"  # type: ignore[attr-defined]
            ui._plan_event.set()
        if hasattr(ui, "_fg_event"):
            ui._fg_event.set()
        if hasattr(ui, "_listeners_lock"):
            _broadcast_ws_closed_to_listeners(ui)
    if ws.session is not None and hasattr(ws.session, "close"):
        ws.session.close()


def _broadcast_ws_closed_to_listeners(ui: SessionUI) -> None:
    """Push ``ws_closed`` into every per-UI listener queue so SSE
    generators unwind promptly.

    Eviction-safe: on ``queue.Full``, drop the oldest event and retry
    rather than failing open. Listeners are cleared after so
    subsequent events don't re-fire on a closed workstream.
    """
    listeners: Any = getattr(ui, "_listeners", None)
    listeners_lock = getattr(ui, "_listeners_lock", None)
    if listeners is None or listeners_lock is None:
        return
    with listeners_lock:
        for lq in listeners:
            try:
                lq.put_nowait({"type": "ws_closed"})
            except queue.Full:
                with contextlib.suppress(queue.Empty):
                    lq.get_nowait()
                with contextlib.suppress(queue.Full):
                    lq.put_nowait({"type": "ws_closed"})
        listeners.clear()
