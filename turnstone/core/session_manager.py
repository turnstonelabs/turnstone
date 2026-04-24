"""Unified manager for workstream-shaped sessions.

Collapses ``WorkstreamManager`` (interactive) and ``CoordinatorManager``
(coordinator) into one class. Kind-specific transport and session
construction live on a ``SessionKindAdapter`` Protocol; the manager
itself owns the invariant mechanics — slot accounting, eviction,
persistence, per-ws lock refcount for concurrent lazy rehydrate.

Stage 1 scaffolding: this module exists so later steps can fill in
``create`` / ``open`` / ``close`` / ``set_state`` / ``list_all`` / etc.
against a stable Protocol and construction signature. No production
code imports from here yet.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from turnstone.core.session import ChatSession, SessionUI
    from turnstone.core.storage._protocol import StorageBackend
    from turnstone.core.workstream import Workstream, WorkstreamKind, WorkstreamState


class SessionKindAdapter(Protocol):
    """Per-kind policies the shared ``SessionManager`` delegates to.

    The manager owns invariant mechanics. The adapter owns:

    - **Transport**: how lifecycle events (``ws_created`` /
      ``ws_state`` / ``ws_closed``) fan out. Interactive pushes onto a
      per-UI listener queue + global event queue; coordinator emits on
      the cluster collector's pseudo-node.
    - **Session construction**: what UI class wraps the workstream,
      what ``ChatSession`` factory signature applies.

    Things intentionally NOT on the Protocol (see the design brief's
    "Decisions settled during the pruning pass"): per-kind permission
    scope (static kind→scope map in handlers), child-spawn / quota
    gates (coordinator tool owns), children registry hooks (coordinator
    tool owns), ``active_id`` / ``switch`` focus state (frontend owns).
    """

    kind: WorkstreamKind

    def emit_created(self, ws: Workstream) -> None: ...
    def emit_state(self, ws: Workstream, state: WorkstreamState) -> None: ...
    def emit_closed(self, ws_id: str) -> None: ...

    def cleanup_ui(self, ws: Workstream) -> None:
        """Unblock per-UI events on close.

        Interactive unblocks ``_approval_event`` / ``_plan_event`` /
        ``_fg_event`` on ``WebUI``; coordinator unblocks listener
        queues on ``ConsoleCoordinatorUI``. Both kinds also call
        ``ws.session.cancel()`` + ``ws.session.close()`` when present.
        """

    def build_ui(self, ws: Workstream) -> SessionUI:
        """Construct the kind-specific UI for a fresh workstream."""

    def build_session(
        self,
        ws: Workstream,
        *,
        skill: str | None = None,
        model: str | None = None,
        client_type: str = "",
    ) -> ChatSession:
        """Construct the ``ChatSession`` for a workstream whose ``ui`` is already attached."""


class SessionManager:
    """Unified lifecycle manager for a single workstream kind.

    Instantiate once per kind: one for interactive on the node, one
    for coordinators on the console. The eviction pool is partitioned
    by kind — a coordinator can't evict an interactive workstream.
    """

    def __init__(
        self,
        adapter: SessionKindAdapter,
        *,
        storage: StorageBackend,
        max_active: int,
        node_id: str | None = None,
    ) -> None:
        if max_active < 1:
            raise ValueError(f"max_active must be >= 1, got {max_active}")
        self._adapter = adapter
        self._storage = storage
        self._max_active = max_active
        self._node_id = node_id
        self._workstreams: dict[str, Workstream] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()
        # Per-ws_id refcounted locks serializing concurrent lazy
        # rehydrate of the same ws_id. Ported from
        # ``CoordinatorManager._open_locks``: without refcounting, a
        # third arrival could allocate a fresh lock for the same ws_id
        # and defeat serialization on the failure path.
        self._open_locks: dict[str, tuple[threading.Lock, int]] = {}

    @property
    def max_active(self) -> int:
        return self._max_active

    @property
    def kind(self) -> WorkstreamKind:
        return self._adapter.kind
