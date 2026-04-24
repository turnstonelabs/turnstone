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

from turnstone.core.adapters._ui_cleanup import cleanup_session_ui
from turnstone.core.log import get_logger
from turnstone.core.workstream import Workstream, WorkstreamKind, WorkstreamState

if TYPE_CHECKING:
    from collections.abc import Callable

    from turnstone.core.session import ChatSession, SessionUI
    from turnstone.core.session_manager import SessionManager

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
        # Late-bound via ``attach`` after the SessionManager is built.
        # Mirrors the coord-side pattern: the manager's ctor takes the
        # adapter, so we can't pass the manager to the adapter's
        # ``__init__`` — break the cycle with a setter called from the
        # CLI entry point.
        self._manager: SessionManager | None = None

    def attach(self, manager: SessionManager) -> None:
        """Late-bind the owning :class:`SessionManager`.

        Called once from the CLI entry right after the manager is
        constructed. Enables ``ui_factory`` closures to resolve the
        manager via :attr:`manager` without the ``list``-ref hack.
        """
        self._manager = manager

    @property
    def manager(self) -> SessionManager:
        """The attached manager. Raises if :meth:`attach` hasn't run."""
        mgr = self._manager
        if mgr is None:
            raise RuntimeError(
                "InteractiveAdapter: manager not attached — call attach(mgr) after construction"
            )
        return mgr

    # ------------------------------------------------------------------
    # Lifecycle events — push onto the process-wide SSE queue
    # ------------------------------------------------------------------

    def emit_created(self, ws: Workstream) -> None:
        # No-op on interactive. The create_workstream HTTP handler
        # (turnstone/server.py) fires ws_created to the global queue
        # AFTER attachment validation so a rejected upload doesn't
        # surface a phantom create→close pair. Firing here would
        # duplicate the event and reintroduce the phantom.
        pass

    def emit_state(self, ws: Workstream, state: WorkstreamState) -> None:
        # No-op on interactive. WebUI._broadcast_state emits the full
        # ws_state payload (tokens + context_ratio + activity) via the
        # SessionUI.on_state_change callback chain; firing here would
        # duplicate the event with a thinner payload.
        pass

    def emit_rehydrated(self, ws: Workstream) -> None:
        # No-op on interactive (mirrors emit_created).
        pass

    def emit_closed(
        self,
        ws_id: str,
        *,
        reason: str = "closed",
        name: str = "",
    ) -> None:
        # Sole transport path for ws_closed on interactive. The old
        # create_workstream HTTP handler used to fire this inline (with
        # name + reason="evicted" on the eviction path) — Stage 1
        # consolidated that here so there's exactly one emission point.
        # ``name`` powers the frontend eviction toast.
        self._enqueue(
            {
                "type": "ws_closed",
                "ws_id": ws_id,
                "reason": reason,
                "name": name,
            }
        )

    def _enqueue(self, event: dict[str, Any]) -> None:
        with contextlib.suppress(queue.Full):
            self._global_queue.put_nowait(event)

    # ------------------------------------------------------------------
    # UI cleanup — unblock pending events + broadcast ws_closed to listeners
    # ------------------------------------------------------------------

    def cleanup_ui(self, ws: Workstream) -> None:
        """Unblock pending approval / plan / foreground events + close session.

        Ported from the old ``WorkstreamManager._cleanup_ui``. Delegates
        to :func:`cleanup_session_ui` — the coordinator adapter runs
        the identical sequence.
        """
        cleanup_session_ui(ws)

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
