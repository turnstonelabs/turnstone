"""Workstream manager — concurrent independent chat sessions.

A workstream is an independent conversation with its own ChatSession and UI
adapter.  The WorkstreamManager coordinates multiple workstreams, tracks their
states, and lets frontends (CLI, Web) multiplex user attention across them.
"""

from __future__ import annotations

import enum
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from turnstone.core.session import ChatSession, SessionUI


# ---------------------------------------------------------------------------
# State enum
# ---------------------------------------------------------------------------


class WorkstreamState(enum.Enum):
    IDLE = "idle"  # waiting for user input
    THINKING = "thinking"  # LLM is streaming
    RUNNING = "running"  # tools executing
    ATTENTION = "attention"  # blocked on approval / plan review
    ERROR = "error"  # last operation failed


# ---------------------------------------------------------------------------
# Workstream dataclass
# ---------------------------------------------------------------------------


@dataclass
class Workstream:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    name: str = ""
    state: WorkstreamState = WorkstreamState.IDLE
    session: ChatSession | None = None
    ui: SessionUI | None = None
    worker_thread: threading.Thread | None = None
    error_message: str = ""
    last_active: float = field(default_factory=time.monotonic, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        if not self.name:
            self.name = f"ws-{self.id[:4]}"


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


class WorkstreamManager:
    """Manages multiple concurrent workstreams, each with its own ChatSession."""

    MAX_WORKSTREAMS = 10

    def __init__(
        self,
        session_factory: Callable[[SessionUI | None], ChatSession],
    ):
        """
        Args:
            session_factory: callable(ui) -> ChatSession.  Captures shared
                config (client, model, temperature, …) so the manager can
                create sessions without knowing those details.
        """
        self._session_factory: Callable[[SessionUI | None], ChatSession] = session_factory
        self._workstreams: dict[str, Workstream] = {}
        self._order: list[str] = []  # creation order
        self._active_id: str | None = None
        self._lock = threading.Lock()
        self._on_state_change: Callable[[str, WorkstreamState], None] | None = None

    # -- creation / destruction ---------------------------------------------

    def create(
        self,
        name: str = "",
        ui_factory: Callable[..., SessionUI] | None = None,
    ) -> Workstream:
        """Create a new workstream.  Returns the new ws."""
        ws = Workstream(name=name)
        if ui_factory:
            ws.ui = ui_factory(ws.id)
        ws.session = self._session_factory(ws.ui)
        with self._lock:
            if len(self._workstreams) >= self.MAX_WORKSTREAMS:
                raise RuntimeError(f"Maximum of {self.MAX_WORKSTREAMS} workstreams reached")
            self._workstreams[ws.id] = ws
            self._order.append(ws.id)
            if self._active_id is None:
                self._active_id = ws.id
        return ws

    def close(self, ws_id: str) -> bool:
        """Close a workstream.  Returns False if it's the last one."""
        with self._lock:
            if len(self._workstreams) <= 1:
                return False
            ws = self._workstreams.pop(ws_id, None)
            if ws is None:
                return False
            self._order.remove(ws_id)
            if self._active_id == ws_id:
                self._active_id = self._order[0]
        # Unblock any waiting approval/plan events so worker thread can exit
        if ws.ui:
            if hasattr(ws.ui, "_approval_event"):
                ws.ui._approval_result = False, None  # type: ignore[attr-defined]
                ws.ui._approval_event.set()
            if hasattr(ws.ui, "_plan_event"):
                ws.ui._plan_result = "reject"  # type: ignore[attr-defined]
                ws.ui._plan_event.set()
            if hasattr(ws.ui, "_fg_event"):
                ws.ui._fg_event.set()
        return True

    # -- lookup -------------------------------------------------------------

    def get(self, ws_id: str) -> Workstream | None:
        with self._lock:
            return self._workstreams.get(ws_id)

    @property
    def active_id(self) -> str | None:
        return self._active_id

    def get_active(self) -> Workstream | None:
        with self._lock:
            return self._workstreams.get(self._active_id) if self._active_id else None

    def list_all(self) -> list[Workstream]:
        """Return workstreams in creation order."""
        with self._lock:
            return [self._workstreams[wid] for wid in self._order if wid in self._workstreams]

    def index_of(self, ws_id: str) -> int:
        """1-based index of a workstream, or 0 if not found."""
        with self._lock:
            try:
                return self._order.index(ws_id) + 1
            except ValueError:
                return 0

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._workstreams)

    # -- switching ----------------------------------------------------------

    def switch(self, ws_id: str) -> Workstream | None:
        """Switch active workstream.  Returns new active or None."""
        with self._lock:
            if ws_id in self._workstreams:
                self._active_id = ws_id
                return self._workstreams[ws_id]
        return None

    def switch_by_index(self, index: int) -> Workstream | None:
        """Switch by 1-based index (creation order)."""
        with self._lock:
            if 1 <= index <= len(self._order):
                ws_id = self._order[index - 1]
                self._active_id = ws_id
                return self._workstreams.get(ws_id)
        return None

    # -- state management ---------------------------------------------------

    def set_state(self, ws_id: str, state: WorkstreamState, error_msg: str = "") -> None:
        """Update a workstream's state.  Called by UI adapters."""
        ws = self._workstreams.get(ws_id)
        if ws:
            with ws._lock:
                ws.state = state
                ws.last_active = time.monotonic()
                ws.error_message = error_msg
            if self._on_state_change:
                self._on_state_change(ws_id, state)

    def close_idle(self, max_age_seconds: float) -> list[str]:
        """Close IDLE workstreams inactive for more than *max_age_seconds*.

        Skips the last workstream and any workstream not in IDLE state.
        Returns a list of closed ws_ids.
        """
        now = time.monotonic()
        with self._lock:
            snapshot = list(self._workstreams.values())
            expired = sorted(
                [
                    ws
                    for ws in snapshot
                    if ws.state == WorkstreamState.IDLE and (now - ws.last_active) > max_age_seconds
                ],
                key=lambda ws: ws.last_active,  # oldest first
            )
            # Never leave zero workstreams
            max_closeable = max(0, len(snapshot) - 1)
            to_close = [ws.id for ws in expired[:max_closeable]]

        closed = []
        for ws_id in to_close:
            ws = self._workstreams.get(ws_id)
            # Re-check state to guard against race between collection and close
            if ws and ws.state == WorkstreamState.IDLE and self.close(ws_id):
                closed.append(ws_id)
        return closed
