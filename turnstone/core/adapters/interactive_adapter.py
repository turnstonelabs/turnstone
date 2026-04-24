"""InteractiveAdapter — SessionManager bridge for interactive workstreams.

Emits lifecycle events onto the process-wide ``global_queue`` the SSE
fan-out thread copies to every connected browser tab. Uses
``WebUI``'s per-UI listener set for ``cleanup_ui`` — the same hooks
(``_approval_event`` / ``_plan_event`` / ``_fg_event`` + the
``ws_closed`` broadcast) the old ``WorkstreamManager._cleanup_ui``
touched.
"""

from __future__ import annotations

import contextlib
import queue
from typing import TYPE_CHECKING, Any

from turnstone.core.log import get_logger
from turnstone.core.workstream import Workstream, WorkstreamKind, WorkstreamState

if TYPE_CHECKING:
    from collections.abc import Callable

    from turnstone.core.session import ChatSession, SessionUI

log = get_logger(__name__)


class InteractiveAdapter:
    """Bridges SessionManager to the interactive node's transport."""

    kind: WorkstreamKind = WorkstreamKind.INTERACTIVE

    def __init__(
        self,
        *,
        global_queue: queue.Queue[dict[str, Any]],
        ui_factory: Callable[[Workstream], SessionUI],
        session_factory: Callable[..., ChatSession],
    ) -> None:
        self._global_queue = global_queue
        self._ui_factory = ui_factory
        self._session_factory = session_factory

    # ------------------------------------------------------------------
    # Lifecycle events — push onto the process-wide SSE queue
    # ------------------------------------------------------------------

    def emit_created(self, ws: Workstream) -> None:
        model = ""
        model_alias = ""
        if ws.session is not None:
            model = getattr(ws.session, "model", "") or ""
            model_alias = getattr(ws.session, "model_alias", "") or ""
        self._enqueue(
            {
                "type": "ws_created",
                "ws_id": ws.id,
                "name": ws.name,
                "model": model,
                "model_alias": model_alias,
                "kind": ws.kind,
                "parent_ws_id": ws.parent_ws_id,
                "user_id": ws.user_id,
            }
        )

    def emit_state(self, ws: Workstream, state: WorkstreamState) -> None:
        # WebUI._broadcast_state already emits richer payloads (tokens,
        # context_ratio, activity) from inside the session's normal
        # flow — that path is preserved. This hook is the
        # manager-level state-flip signal for observers that only care
        # about the transition itself; keep it minimal.
        self._enqueue(
            {
                "type": "ws_state",
                "ws_id": ws.id,
                "state": state.value,
                "kind": ws.kind,
                "parent_ws_id": ws.parent_ws_id,
            }
        )

    def emit_closed(self, ws_id: str, *, reason: str = "closed") -> None:
        self._enqueue({"type": "ws_closed", "ws_id": ws_id, "reason": reason})

    def _enqueue(self, event: dict[str, Any]) -> None:
        with contextlib.suppress(queue.Full):
            self._global_queue.put_nowait(event)

    # ------------------------------------------------------------------
    # UI cleanup — unblock pending events + broadcast ws_closed to listeners
    # ------------------------------------------------------------------

    def cleanup_ui(self, ws: Workstream) -> None:
        """Unblock pending approval / plan / foreground events + close session.

        Ported from the old ``WorkstreamManager._cleanup_ui``. The
        ``hasattr`` checks guard stub UIs used in tests — the real
        ``WebUI`` always has these attributes.
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
        """Push ``ws_closed`` into every per-UI listener queue so SSE
        generators unwind promptly.

        Eviction-safe: on ``queue.Full``, drop the oldest event and
        retry rather than failing open. Listeners are cleared after so
        subsequent events don't re-fire on a closed workstream.
        """
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
        """Delegate to the injected ``session_factory`` closure.

        ``extra`` forwards kind-specific options the interactive
        session_factory understands — ``judge_model`` is the current
        live one; anything the factory doesn't accept raises
        ``TypeError`` at call time, which is the same fail-loud
        behaviour as direct argument mismatch.
        """
        return self._session_factory(
            ws.ui,
            model,
            ws.id,
            skill=skill,
            client_type=client_type,
            kind=ws.kind,
            parent_ws_id=ws.parent_ws_id,
            **extra,
        )
