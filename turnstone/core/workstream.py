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
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import Callable

    from turnstone.core.session import ChatSession, SessionUI

# What KIND of work a worker slot holds.  A Literal (not a free-form str)
# so a typo'd comparison or a new dispatch caller passing "commands" is a
# type error instead of a silently never-firing command-window guard —
# the /send defer keys on exactly these values.
WorkerKind = Literal["", "turn", "command"]


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
# Deferred sends
# ---------------------------------------------------------------------------


@dataclass
class _PendingSend:
    """One send deferred while the /send order barrier holds.

    A /send that lands while a slash-command worker holds the slot
    (``ws.worker_kind == "command"`` — a manual /compact can hold it for
    minutes) or while earlier deferred entries are still pending is
    answered ``{"status": "queued", "deferred": true, "msg_id": ...}``
    immediately and registered here;
    :func:`turnstone.core.session_routes._drain_pending_sends` dispatches
    it full-fidelity when the slot frees.  This replaces the parked-POST
    design, which encoded "client disconnected" as "message retracted" —
    true only for the web composers' ✕-abort; every bounded caller (the
    coordinator client and the console proxy at timeout=30, SDKs, curl)
    times out instead, and its message was deliberately dropped for the
    whole window.

    ``attempt`` is the prebuilt one-session-capture dispatch closure
    (:func:`turnstone.core.session_routes._make_dispatch_attempt` with
    ``defer_fidelity=True``), so the drain stays endpoint-agnostic —
    everything kind-specific (attachments, spawn metrics, UI hooks) was
    captured at defer time.  ``retracted`` is flipped under ``ws._lock``
    by the DELETE dequeue fall-through; the drain never dispatches a
    retracted entry.

    Durability contract (documented in the API reference): node-local
    and in-memory, the interjection queue's lifetime — entries die with
    the workstream or the process, so "queued" is at-most-once intake,
    not durable acceptance.

    Invariant both the route's order barrier and the drain depend on
    (and the second term of :meth:`Workstream.send_barrier_active`
    exists to honor): **drain not alive ⇒ nothing claimed.**  The drain
    pops an entry only while it lives and re-inserts it at head on ANY
    non-dispatch outcome (rejection or crash), so a dead/absent drain
    means every accepted entry is on the list — the barrier term pair
    (list non-empty OR drain alive) therefore covers the claimed-entry
    window with no third state.

    (No ``priority`` field: dispatch is strictly FIFO — arrival order is
    the contract — and the queued response/event use the route's parsed
    local.  A deferred entry's ``!!!`` prefix still reaches the model:
    the full text dispatches as an ordinary send.)
    """

    msg_id: str
    attempt: Callable[[ChatSession], tuple[bool, dict[str, Any]]]
    retracted: bool = False


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
    # The project this workstream is attached to (None = none).  Children
    # inherit the parent's project_id at spawn.
    project_id: str | None = None
    # Slug of the persona the workstream was created with ("" = pre-persona).
    # Display carrier only — the session applies the stamped snapshot from
    # workstream_config, never this field.
    persona: str = ""
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
    # What KIND of work the current worker slot holds: "turn" (a send /
    # retry / wake — the default) or "command" (a slash-command worker,
    # including the minutes-long manual /compact).  Written under
    # ``_lock`` by ``session_worker.send`` in the same acquisition that
    # sets ``worker_thread``/``_worker_running``, so readers gating on
    # the running flag see a coherent triple.  A stale value after the
    # worker exits is harmless — every reader conjoins
    # ``_worker_running``.  The /send route defers (never queues) while
    # this reads "command": the mid-turn interjection queue is
    # turn-shaped (length cap, cross-user guard) and must be
    # unreachable during command windows — deferred entries live on
    # ``_pending_sends`` below.
    worker_kind: WorkerKind = field(default="", repr=False)
    # Sends deferred while the order barrier holds (full-fidelity
    # pending entries — see :class:`_PendingSend` above), dispatched in
    # arrival order by the per-workstream drain thread when the slot
    # frees.  Appends, retract-marks and claims all happen under
    # ``_lock``.  Node-local and in-memory: entries die with the
    # workstream or the process (the interjection queue's lifetime) —
    # the /send contract documents the at-most-once consequence.
    _pending_sends: list[_PendingSend] = field(default_factory=list, repr=False)
    # Single-flight guard for the drain (a daemon ``threading.Thread``
    # while one is live).  Written under ``_lock``; the drain clears it
    # before exiting so a later deferred send starts a fresh one.
    _pending_drain: threading.Thread | None = field(default=None, repr=False)
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

    def send_barrier_active(self) -> bool:
        """True while the /send order barrier holds — the ONE definition.

        The barrier is a two-term pair, and every dispatch surface that
        must not overtake acknowledged sends consults it here (the /send
        route's defer probe, ``CoordinatorAdapter.send``'s refusal, the
        queued-nudge wake gate) instead of hand-copying the terms:

        * ``_pending_sends`` non-empty — acknowledged entries are waiting
          (retract-marked husks count until the drain's loop-top purge:
          they still occupy their arrival slot).
        * the drain is alive — an entry may be CLAIMED (popped, dispatch
          in flight); the :class:`_PendingSend` invariant ("drain not
          alive ⇒ nothing claimed") is what makes these two terms
          exhaustive, with no third state.  The pair also covers the
          drain-spawn window because ``_defer_send`` appends the entry
          and starts the thread under one ``_lock`` acquisition — the
          list term is always set before a not-yet-started drain could
          be observed.

        Lockless callers (the wake gate, the coordinator adapter) get
        benign staleness in both directions: a stale True skips once
        more and the next barrier-clearing path re-runs the gate (every
        deferred turn's exit backstop, plus the drain's own clean exit);
        a stale False means the concurrent defer holds no order contract
        against the caller anyway.  Callers that need the answer atomic
        with a mutation (the route's probe-then-append) hold ``_lock``
        around the call.
        """
        drain = self._pending_drain
        return bool(self._pending_sends) or (drain is not None and drain.is_alive())
