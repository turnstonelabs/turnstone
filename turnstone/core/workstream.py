"""Workstream manager — concurrent independent conversations.

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
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Protocol

    from turnstone.core.session import ChatSession, SessionUI

    class _SessionFactory(Protocol):
        def __call__(
            self,
            ui: SessionUI | None,
            model_alias: str | None = ...,
            ws_id: str | None = ...,
            *,
            skill: str | None = ...,
            client_type: str = ...,
        ) -> ChatSession: ...


# ---------------------------------------------------------------------------
# State enum
# ---------------------------------------------------------------------------


class WorkstreamState(enum.Enum):
    IDLE = "idle"  # waiting for user input
    THINKING = "thinking"  # LLM is streaming
    RUNNING = "running"  # tools executing
    ATTENTION = "attention"  # blocked on approval / plan review
    ERROR = "error"  # last operation failed


_ACTIVE_STATES = {"running", "thinking", "attention"}


# ---------------------------------------------------------------------------
# Workstream dataclass
# ---------------------------------------------------------------------------


@dataclass
class Workstream:
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
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

    def __init__(
        self,
        session_factory: _SessionFactory,
        *,
        max_workstreams: int = 50,
        node_id: str | None = None,
    ):
        """
        Args:
            session_factory: callable(ui, model_alias, ws_id, *, skill) -> ChatSession.
                Captures shared config (registry, temperature, …) so the
                manager can create ChatSession instances without knowing
                those details.  *model_alias* selects a model from the
                registry (None = default).  *ws_id* is the persistent
                identity used for all storage operations.
            max_workstreams: Maximum number of concurrent workstreams.  When at
                capacity, ``create()`` will auto-evict the oldest IDLE
                workstream before raising.
            node_id: Server node identity (persisted with workstreams).
        """
        if max_workstreams < 1:
            raise ValueError(f"max_workstreams must be >= 1, got {max_workstreams}")
        self._session_factory: _SessionFactory = session_factory
        self._node_id = node_id
        self._max_workstreams: int = max_workstreams
        self._workstreams: dict[str, Workstream] = {}
        self._order: list[str] = []  # creation order
        self._active_id: str | None = None
        self._lock = threading.Lock()
        self._on_state_change: Callable[[str, WorkstreamState], None] | None = None
        self._evictions: int = 0
        self._last_evicted: Workstream | None = None

    @property
    def max_workstreams(self) -> int:
        """Configured maximum concurrent workstreams."""
        return self._max_workstreams

    @property
    def eviction_count(self) -> int:
        """Number of workstreams auto-evicted by ``create()``."""
        return self._evictions

    @property
    def last_evicted(self) -> Workstream | None:
        """The most recently evicted workstream, or ``None``."""
        return self._last_evicted

    # -- creation / destruction ---------------------------------------------

    def create(
        self,
        name: str = "",
        ui_factory: Callable[..., SessionUI] | None = None,
        model: str | None = None,
        skill: str | None = None,
        skill_id: str = "",
        skill_version: int = 0,
        ws_id: str = "",
        client_type: str = "",
    ) -> Workstream:
        """Create a new workstream.  Returns the new ws.

        If the manager is at capacity, the oldest IDLE workstream is
        automatically evicted.  If **all** workstreams are non-idle, a
        ``RuntimeError`` is raised.

        Args:
            model: Optional model alias from the registry.  ``None`` uses the
                default model.
            skill: Optional skill name.
            skill_id: Template ID of the skill (for lineage tracking).
            skill_version: Version of the skill at creation time.
            ws_id: Optional workstream ID.  If non-empty, used as-is instead of
                generating a new UUID.
        """
        # Fast-fail capacity check (avoids expensive ChatSession creation when full).
        first_evicted: Workstream | None = None
        with self._lock:
            if len(self._workstreams) >= self._max_workstreams:
                first_evicted = self._evict_oldest_idle_locked()
                if first_evicted is None:
                    raise RuntimeError(f"All {self._max_workstreams} workstreams are active")

        # Cleanup first-phase eviction outside the lock (may trigger callbacks).
        if first_evicted is not None:
            self._cleanup_ui(first_evicted)
            self._last_evicted = first_evicted
            from turnstone.core.memory import decrement_bucket_count as _dbc1
            from turnstone.core.memory import delete_workstream_override as _dwo1
            from turnstone.core.metrics import metrics as _m1

            _dbc1(first_evicted.id, active=False)  # evicted ws is always idle
            _dwo1(first_evicted.id)
            _m1.record_eviction()

        # Create workstream and ChatSession outside the lock (construction is
        # expensive — involves LLM client setup and DB writes).
        ws = Workstream(id=ws_id, name=name) if ws_id else Workstream(name=name)
        if ui_factory:
            ws.ui = ui_factory(ws.id)
        factory_kwargs: dict[str, Any] = {"skill": skill}
        if client_type:
            factory_kwargs["client_type"] = client_type
        ws.session = self._session_factory(ws.ui, model, ws.id, **factory_kwargs)

        # Authoritative insert under lock with re-check (another thread may
        # have filled capacity while we were unlocked).
        second_evicted: Workstream | None = None
        with self._lock:
            if len(self._workstreams) >= self._max_workstreams:
                second_evicted = self._evict_oldest_idle_locked()
                if second_evicted is None:
                    raise RuntimeError(f"All {self._max_workstreams} workstreams are active")
            self._workstreams[ws.id] = ws
            self._order.append(ws.id)
            if self._active_id is None:
                self._active_id = ws.id

        # Persist to storage only after successful insertion
        from turnstone.core.memory import increment_bucket_count, register_workstream

        register_workstream(
            ws.id,
            node_id=self._node_id,
            name=ws.name,
            skill_id=skill_id,
            skill_version=skill_version,
        )
        increment_bucket_count(ws.id)

        # Cleanup second-phase eviction outside the lock.
        if second_evicted is not None:
            self._cleanup_ui(second_evicted)
            self._last_evicted = second_evicted
            from turnstone.core.memory import decrement_bucket_count as _dbc2
            from turnstone.core.memory import delete_workstream_override as _dwo2
            from turnstone.core.metrics import metrics as _m2

            _dbc2(second_evicted.id, active=False)
            _dwo2(second_evicted.id)
            _m2.record_eviction()
        return ws

    # -- eviction helpers ---------------------------------------------------

    def _evict_oldest_idle_locked(self) -> Workstream | None:
        """Find and remove the oldest IDLE workstream.

        **Must be called while ``self._lock`` is held.**  Returns the evicted
        ``Workstream`` (caller is responsible for UI cleanup) or ``None`` if no
        IDLE workstreams exist.
        """
        oldest: Workstream | None = None
        for wid in self._order:
            ws = self._workstreams[wid]
            if ws.state == WorkstreamState.IDLE and (
                oldest is None or ws.last_active < oldest.last_active
            ):
                oldest = ws
        if oldest is None:
            return None
        del self._workstreams[oldest.id]
        self._order.remove(oldest.id)
        if self._active_id == oldest.id:
            self._active_id = self._order[0] if self._order else None
        self._evictions += 1
        return oldest

    @staticmethod
    def _cleanup_ui(ws: Workstream) -> None:
        """Unblock pending approval/plan/foreground events on a workstream."""
        # Cancel any in-flight generation so the worker thread stops promptly.
        if ws.session and hasattr(ws.session, "cancel"):
            ws.session.cancel()
        if ws.ui:
            if hasattr(ws.ui, "_approval_event"):
                ws.ui._approval_result = False, None  # type: ignore[attr-defined]
                ws.ui._approval_event.set()
            if hasattr(ws.ui, "_plan_event"):
                ws.ui._plan_result = "reject"  # type: ignore[attr-defined]
                ws.ui._plan_event.set()
            if hasattr(ws.ui, "_fg_event"):
                ws.ui._fg_event.set()
            # Notify SSE listeners so generators exit promptly
            if hasattr(ws.ui, "_listeners_lock"):
                import contextlib
                import queue as _queue

                with ws.ui._listeners_lock:
                    for lq in ws.ui._listeners:  # type: ignore[attr-defined]
                        try:
                            lq.put_nowait({"type": "ws_closed"})
                        except _queue.Full:
                            with contextlib.suppress(_queue.Empty):
                                lq.get_nowait()
                            with contextlib.suppress(_queue.Full):
                                lq.put_nowait({"type": "ws_closed"})
                    ws.ui._listeners.clear()  # type: ignore[attr-defined]
        # Release MCP listener registration
        if ws.session and hasattr(ws.session, "close"):
            ws.session.close()

    def close(self, ws_id: str) -> bool:
        """Close a workstream.  Returns False if it's the last one."""
        with self._lock:
            if len(self._workstreams) <= 1:
                return False
            ws = self._workstreams.pop(ws_id, None)
            if ws is None:
                return False
            was_active = ws.state.value in _ACTIVE_STATES
            self._order.remove(ws_id)
            if self._active_id == ws_id:
                self._active_id = self._order[0]
        # Unblock any waiting approval/plan events so worker thread can exit
        self._cleanup_ui(ws)
        from turnstone.core.memory import (
            decrement_bucket_count,
            delete_workstream_override,
            update_workstream_state,
        )

        update_workstream_state(ws_id, "closed")
        decrement_bucket_count(ws_id, active=was_active)
        delete_workstream_override(ws_id)
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
                old_active = ws.state.value in _ACTIVE_STATES
                ws.state = state
                ws.last_active = time.monotonic()
                ws.error_message = error_msg
                from turnstone.core.memory import adjust_bucket_active, update_workstream_state

                update_workstream_state(ws_id, state.value)
                new_active = state.value in _ACTIVE_STATES
                if old_active and not new_active:
                    adjust_bucket_active(ws_id, -1)
                elif new_active and not old_active:
                    adjust_bucket_active(ws_id, 1)
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
