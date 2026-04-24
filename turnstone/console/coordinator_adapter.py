"""CoordinatorAdapter — SessionManager bridge for coordinator workstreams.

Emits lifecycle events via the ``ClusterCollector``'s console pseudo-node
fan-out. ``cleanup_ui`` ports the listener-queue + approval/plan event
unblocks from the old ``CoordinatorManager._cleanup`` path. Children
registry and quota concerns live in the coordinator tool, not here —
the adapter stays narrow.
"""

from __future__ import annotations

import contextlib
import queue
from typing import TYPE_CHECKING, Any

from turnstone.core.log import get_logger
from turnstone.core.workstream import Workstream, WorkstreamKind, WorkstreamState

if TYPE_CHECKING:
    from collections.abc import Callable

    from turnstone.console.collector import ClusterCollector
    from turnstone.console.coordinator_ui import ConsoleCoordinatorUI
    from turnstone.core.session import ChatSession, SessionUI

log = get_logger(__name__)


class CoordinatorAdapter:
    """Bridges SessionManager to the console's coordinator transport."""

    kind: WorkstreamKind = WorkstreamKind.COORDINATOR

    def __init__(
        self,
        *,
        collector: ClusterCollector,
        ui_factory: Callable[[Workstream], ConsoleCoordinatorUI],
        session_factory: Callable[..., ChatSession],
    ) -> None:
        self._collector = collector
        self._ui_factory = ui_factory
        self._session_factory = session_factory

    # ------------------------------------------------------------------
    # Lifecycle events — fan out via the ClusterCollector's pseudo-node
    # ------------------------------------------------------------------

    def emit_created(self, ws: Workstream) -> None:
        try:
            self._collector.emit_console_ws_created(
                ws.id,
                name=ws.name,
                user_id=ws.user_id,
                kind=ws.kind.value,
                state=ws.state.value,
                parent_ws_id=None,
            )
        except Exception:
            log.debug("coord_adapter.created_fanout_failed ws=%s", ws.id[:8], exc_info=True)

    def emit_state(self, ws: Workstream, state: WorkstreamState) -> None:
        try:
            self._collector.emit_console_ws_state(ws.id, state.value)
        except Exception:
            log.debug("coord_adapter.state_fanout_failed ws=%s", ws.id[:8], exc_info=True)

    def emit_closed(self, ws_id: str, *, reason: str = "closed") -> None:
        # ``reason`` is accepted for Protocol compatibility but the
        # collector's emit_console_ws_closed doesn't propagate it —
        # the console frontend's "evicted" special-case only fires for
        # real-node (interactive) ws_closed events.
        del reason
        try:
            self._collector.emit_console_ws_closed(ws_id)
        except Exception:
            log.debug("coord_adapter.closed_fanout_failed ws=%s", ws_id[:8], exc_info=True)

    # ------------------------------------------------------------------
    # UI cleanup — unblock pending events + broadcast ws_closed to listeners
    # ------------------------------------------------------------------

    def cleanup_ui(self, ws: Workstream) -> None:
        """Unblock pending events + close the session.

        Mirrors the old ``CoordinatorManager._cleanup`` (which itself
        delegated to ``WorkstreamManager._cleanup_ui``). Listener
        broadcast evicts the oldest event on ``queue.Full`` so an
        unresponsive browser tab can't block close.
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
                self._broadcast_ws_closed_to_listeners(ui)
        if ws.session is not None and hasattr(ws.session, "close"):
            ws.session.close()

    @staticmethod
    def _broadcast_ws_closed_to_listeners(ui: SessionUI) -> None:
        listeners = getattr(ui, "_listeners", None)
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

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def build_ui(self, ws: Workstream) -> SessionUI:
        return self._ui_factory(ws)

    def build_session(
        self,
        ws: Workstream,
        *,
        skill: str | None = None,
        model: str | None = None,
        client_type: str = "",
        **extra: Any,
    ) -> ChatSession:
        """Delegate to the injected coordinator ``session_factory`` closure."""
        del client_type  # coordinator session_factory doesn't use client_type
        return self._session_factory(
            ws.ui,
            model,
            ws.id,
            skill=skill,
            kind=ws.kind,
            parent_ws_id=ws.parent_ws_id,
            **extra,
        )
