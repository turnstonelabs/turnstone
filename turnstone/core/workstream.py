"""Workstream types — the kind enum, state enum, and per-workstream dataclass.

A workstream is an independent conversation with its own ChatSession and UI
adapter. Lifecycle (create/open/close/set_state/eviction/SSE fan-out) lives
on :class:`turnstone.core.session_manager.SessionManager`; this module only
defines the data types both interactive and coordinator kinds share.
"""

from __future__ import annotations

import enum
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from turnstone.core.session import ChatSession, SessionUI


# ---------------------------------------------------------------------------
# Kind enum — single source of truth for the workstream dispatch classifier
# ---------------------------------------------------------------------------


class WorkstreamKind(enum.StrEnum):
    """Classifier for which manager hosts a workstream.

    StrEnum so members are drop-in ``str`` replacements for the DB column,
    JSON payloads, and existing ``==`` comparisons against raw strings.
    Narrow internal annotations to this type; wide boundaries (HTTP body,
    DB row) stay ``str`` and parse via ``WorkstreamKind(raw)`` / ``from_raw``
    at the edge.
    """

    INTERACTIVE = "interactive"  # hosted by the node's interactive SessionManager
    COORDINATOR = "coordinator"  # hosted by the console's coordinator SessionManager

    @classmethod
    def from_raw(
        cls,
        value: WorkstreamKind | str | None,
        *,
        default: WorkstreamKind | None = None,
    ) -> WorkstreamKind:
        """Parse an externally-supplied kind value with a fallback for missing data.

        Handles the three shapes that arrive from storage rows and wire
        payloads — already-an-enum, non-empty string, None/empty — so the
        ``WorkstreamKind(x or WorkstreamKind.INTERACTIVE.value)`` dance
        (``or`` short-circuits on a truthy enum member and skips the
        default, forcing every caller to reach for ``.value``) collapses
        into a single predictable call.

        ``default`` defaults to ``INTERACTIVE`` when omitted. Raises
        ``ValueError`` for a non-empty string that doesn't match any
        known kind — callers that want to coerce unknowns to the default
        should catch and fall back explicitly.
        """
        effective_default = default if default is not None else cls.INTERACTIVE
        if value is None or value == "":
            return effective_default
        return cls(value)


# ---------------------------------------------------------------------------
# State enum
# ---------------------------------------------------------------------------


class WorkstreamState(enum.Enum):
    IDLE = "idle"  # waiting for user input
    THINKING = "thinking"  # LLM is streaming
    RUNNING = "running"  # tools executing
    ATTENTION = "attention"  # blocked on approval / plan review
    ERROR = "error"  # last operation failed


# States the orphan reaper (``SessionManager.close_idle`` pass 2 +
# ``StorageBackend.bulk_close_stale_orphans``) is allowed to flip to
# ``closed`` for rows past the staleness cutoff.  Excludes ``ERROR``
# deliberately — error rows are user-investigatable and shouldn't be
# auto-reaped — and excludes ``CLOSED`` (terminal).  Centralized here
# so the storage backends and FakeStorage all agree; if a new transient
# state is added to ``WorkstreamState``, deciding whether it joins
# this set is part of the change rather than an after-the-fact
# audit across three files.
BULK_CLOSE_STATE_VALUES: frozenset[str] = frozenset(
    {
        WorkstreamState.IDLE.value,
        WorkstreamState.THINKING.value,
        WorkstreamState.RUNNING.value,
        WorkstreamState.ATTENTION.value,
    }
)


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
    notify_targets: str = "[]"
    # Owning user_id. Populated by the SessionManager so attribution
    # survives across restarts / lazy rehydration.
    user_id: str = ""
    # Classifier reused by both interactive and coordinator managers —
    # no parallel type hierarchy.
    kind: WorkstreamKind = WorkstreamKind.INTERACTIVE
    # Non-None for children spawned by a coordinator.
    parent_ws_id: str | None = None
    # Tombstone: set by ``SessionManager.close`` under ``_lock`` so a
    # racing ``set_state`` can detect the close before it overwrites
    # the persisted ``state='closed'`` row. Guarded by ``_lock``.
    _closed: bool = field(default=False, repr=False)
    # True while a worker thread is actively running ``ChatSession.send``.
    # Toggled under ``_lock`` by ``turnstone.core.session_worker.send``
    # (and the few sites that spawn workers directly — see
    # ``server.py``'s init-message + retry-after-rewind paths) so
    # concurrent dispatches can safely decide queue-vs-spawn without
    # racing ``Thread.is_alive()``. Used by both interactive and
    # coordinator paths since Stage 2 P1.
    _worker_running: bool = field(default=False, repr=False)
    # True once ``SessionManager.commit_create`` (or the non-deferred
    # path through ``SessionManager.create``) has fired the lifecycle
    # ``emit_created`` event for this workstream. Used by
    # :meth:`SessionManager.discard` (warns when set — abandoning an
    # already-advertised ws leaves a stale ``ws_created`` on the wire
    # with no matching ``ws_closed``) and by
    # :meth:`SessionManager.commit_create` itself (no-ops when set,
    # to make the idempotent-second-call and the
    # commit-after-discard caller-bug paths safe). The non-deferred
    # ``create`` sets this immediately before calling
    # ``emit_created``; ``commit_create`` sets it under the manager
    # lock alongside the tracked-ws check so a racing ``discard`` can
    # never see it without also seeing the slot already popped.
    _emit_created_fired: bool = field(default=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        if not self.name:
            self.name = f"ws-{self.id[:4]}"
