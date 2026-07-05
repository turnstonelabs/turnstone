"""Shared scaffolding for :class:`SessionUI` implementations.

Both :class:`turnstone.server.WebUI` (interactive node UI) and
:class:`turnstone.console.coordinator_ui.ConsoleCoordinatorUI` wrap a
:class:`~turnstone.core.session.ChatSession` and fan events out over
SSE to one or more connected browser tabs. They also block the worker
thread on a pending-input gate (tool approval) that HTTP handlers
resolve.

That skeleton — plus per-workstream metrics tracking, intent-verdict
bookkeeping, output-warning persistence, and the canonical
``on_status`` / ``on_content_token`` / activity-tracking bodies —
lives here. Subclasses add only kind-specific broadcast
(``_broadcast_state`` / ``_broadcast_activity`` override hooks) and
any node-level metrics adapters (``_metrics.record_*`` calls stay on
``WebUI`` since they feed the node's prometheus endpoint).

This module intentionally does not satisfy
:class:`turnstone.core.session.SessionUI` by itself — ``on_state_change``
and ``on_rename`` still require subclass implementation, since their
storage/transport routing is kind-specific.
"""

from __future__ import annotations

import collections
import contextlib
import contextvars
import copy
import json
import math
import os
import queue
import threading
import time
import uuid
from typing import Any

from turnstone.core.log import get_logger

log = get_logger(__name__)

# Matches WebUI's historical listener queue size and the coordinator
# UI's ``_LISTENER_QUEUE_MAX``. Per-queue cap keeps a slow SSE consumer
# from bloating memory.
_DEFAULT_LISTENER_QUEUE_MAX = 500

# Recall: how many finished task agents' projected sub-trajectories to retain
# in memory for /history card rebuilds.  LRU-bounded so a marathon workstream
# can't grow it without limit; eviction (and a cold reopen, which starts empty)
# recalls honestly as "not retained" rather than a fabricated 0-step card.
_AGENT_TRAJECTORY_CAP = 256

# How many resolved-cycle decisions to remember (call_id → decision) so a
# late LLM verdict — the run-to-completion judge can deliver seconds after
# its gate resolved — still lands with the decision the operator actually
# took.  Bounded because call_ids of resolved cycles accrue forever on a
# long-running workstream.  Invariant for the cap: it must exceed the
# number of call_ids the session can resolve inside one judge-deadline
# window (the daemon's delivery bound), or an eviction could orphan a
# still-in-flight verdict as forever-"pending" in the audit table.  A
# smart-approvals storm across parallel task agents resolves tens of
# calls per second at the extreme; 1024 covers minutes of that, and the
# entries are (str, (str, Event)) — negligible memory.
_RECENT_DECISION_CAP = 1024


class ApprovalCycle:
    """One in-flight human-approval round on a workstream.

    The approval gate used to be a per-UI singleton — one pending card,
    one ``threading.Event``, one result slot.  Parallel task agents
    broke that: each sub-agent thread runs its own gate, so several
    rounds are now live at once and each needs its own wait/result/
    verdict bookkeeping.  ``approve_tools`` registers one of these per
    human-gated batch in ``_approval_cycles`` (insertion-ordered =
    oldest-first); HTTP resolvers route a decision to exactly one cycle
    by ``cycle_id`` / member ``call_id`` (or the oldest, for legacy
    selector-less clients).

    Lifecycle: created + registered by the gate thread, resolved
    exactly once by :meth:`SessionUIBase.resolve_approval` (double
    resolution is a guarded no-op), unregistered by the gate thread
    after its wait returns.  ``event``/``result`` are per-cycle so one
    decision can never leak onto a concurrent sibling round — the
    cross-approval hazard the singleton design had.
    """

    __slots__ = (
        "call_ids",
        "card",
        "cycle_id",
        "decision",
        "event",
        "items",
        "judge_event",
        "pending_verdicts",
        "resolved",
        "result",
    )

    def __init__(
        self,
        items: list[dict[str, Any]],
        card: dict[str, Any],
        judge_event: threading.Event | None,
    ) -> None:
        self.cycle_id: str = card.get("cycle_id", "") or uuid.uuid4().hex
        self.items = items
        self.card = card
        self.call_ids: set[str] = {it.get("call_id", "") for it in items if it.get("call_id")}
        # The judge generation that evaluated this batch — identity-
        # compared against the delivering daemon's cancel event so a
        # stale generation (a prior turn's run-to-completion daemon
        # whose provider reused call_ids) can never satisfy THIS
        # cycle's Smart-Approvals wait or park verdicts on it.
        self.judge_event: threading.Event | None = judge_event
        self.event = threading.Event()
        self.result: tuple[bool, str | None] = (False, None)
        self.resolved = False
        self.decision = ""
        # LLM verdicts that fired during this round, awaiting the
        # operator's decision stamp (was the UI-global
        # ``_pending_verdicts`` list).
        self.pending_verdicts: list[dict[str, Any]] = []


# Sub-agent scope, PER-THREAD.  A task agent's progress chatter reaches the UI as
# ``on_info`` lines ("[task done] N chars", a tool's "fetched N chars") that
# carry no call_id — they can't nest under the task card and would escape to the
# top level, so the web pane drops them while a sub-agent runs.  A contextvar,
# not a session-global counter, so the suppression follows the sub-agent's OWN
# thread: a parallel sibling tool running in another pool thread keeps its info
# lines.  Incremented/decremented by begin_/end_agent_scope around each
# ``_run_agent``; the CLI overrides ``on_info`` and is unaffected (no card → the
# lines are its only signal).
_agent_scope_var: contextvars.ContextVar[int] = contextvars.ContextVar(
    "turnstone_agent_scope_depth", default=0
)


def _resolve_event_buffer_max() -> int:
    """Read ``TURNSTONE_SSE_EVENT_BUFFER_MAX`` env override at import time.

    Default 50000 events.  Two pressures push the cap larger than a
    casual reading of "how many events does an SSE stream see":

    1. Local-inference deployments stream at 500–2000 tok/s per
       active model.  Each token is an ``_enqueue`` call, so a single
       active workstream can fire ~2000 events/sec sustained.  At
       the 50000 cap that buys ~25 s of pure token streaming before
       truncation; at typical cloud-provider rates (50–200 events/sec
       per stream) it's minutes of coverage.
    2. Browsers throttle the SSE-drain microtask aggressively when
       the tab isn't visible (Chrome's background-tab budget drops
       to ~1 wake/min after ~5 min hidden).  A backgrounded tab can
       legitimately go tens of seconds without draining its
       EventSource buffer — and PR-G (drop-pings-let-it-die)
       deliberately closes those connections on hide.  Reconnect-with-
       replay is the recovery path; if the buffer evicted in the
       interim, the snapshot floor is all that's left.

    Why not coalesce consecutive content/reasoning tokens?  A naive
    text-merge breaks the replay-slice semantic: a coalesced entry
    has the latest ``_event_id`` but text that includes content the
    client already received under an earlier id, so any consumer
    with ``last_event_id`` falling INSIDE the coalesced span would
    double-render.  A correctness-preserving coalesce would need a
    per-consumer high-water tracker we deliberately don't maintain
    (consumers register and disconnect independently).  Bigger cap
    + simple per-event storage avoids the trap.

    Memory cost is ~200–500 bytes per event (deque node + dict
    overhead + payload), so 50000 × 100-ws design ceiling caps at
    roughly 2.5 GB worst-case — and practically never anywhere
    close because the cap is the per-ws ceiling, not per-ws steady-
    state.  Operators on heavier workloads can raise via
    ``TURNSTONE_SSE_EVENT_BUFFER_MAX``; below-cap reconnects always
    hit the replay path, above-cap reconnects fall back to the
    snapshot recovery floor with an explicit ``replay_truncated``
    envelope.
    """
    raw = os.environ.get("TURNSTONE_SSE_EVENT_BUFFER_MAX", "").strip()
    default = 50000
    if not raw:
        return default
    try:
        n = int(raw)
    except ValueError:
        return default
    return n if n > 0 else default


# Per-ws ring buffer for ``Last-Event-ID`` SSE replay.  Holds the most
# recent events keyed by monotonic ``_event_id``; deque ``maxlen`` evicts
# oldest automatically.  See :func:`_resolve_event_buffer_max` for the
# sizing rationale (why 50000 and not 2000; why no in-buffer coalescing).
_EVENT_BUFFER_MAX = _resolve_event_buffer_max()


# Cap on the assistant content / reasoning accumulators. Used by two
# independent buffer pairs:
#  - ``_ws_turn_content`` (multi-turn, drained at idle/error) — the
#    IDLE-piggyback payload the cluster collector / dashboard renders
#    without round-tripping storage.
#  - ``_ws_inflight_content`` / ``_ws_inflight_reasoning`` (per-turn,
#    drained at :meth:`on_turn_start`) — the SSE refresh-resume
#    snapshot a reconnecting client sees for the in-progress turn.
# 512 KiB gives headroom for current commercial models; bump if a
# single turn legitimately exceeds it.
_MAX_TURN_CONTENT_CHARS = 512 * 1024


def fire_judge_verdict_metric(
    metrics: Any,
    verdict: dict[str, Any],
    default_tier: str,
) -> None:
    """Fire ``record_judge_verdict`` on the given Prometheus collector.

    Both :class:`turnstone.server.WebUI` (per-node ``MetricsCollector``)
    and :class:`turnstone.console.coordinator_ui.ConsoleCoordinatorUI`
    (console-side :class:`ConsoleMetrics`) route their hook overrides
    through this helper. Pins the ``(tier, risk_level, latency_ms)``
    extraction shape so a future signature change to
    ``record_judge_verdict`` lands in one place instead of four.

    ``default_tier`` is the call-site label (``"heuristic"`` or
    ``"llm"``) used only when the verdict dict doesn't already carry
    a ``tier`` key — both real producers always set it, but the
    fallback keeps a malformed verdict on the right histogram bucket.
    """
    metrics.record_judge_verdict(
        verdict.get("tier", default_tier),
        verdict.get("risk_level", "medium"),
        verdict.get("latency_ms", 0),
    )


class AutoApproveReason:
    """Source vocabulary for ``auto_approve_reason`` annotations.

    Pinned as constants so writers can't typo a reason silently and
    desync the wire from the JS pill renderer.  Kept on a class
    rather than an Enum so the wire-format string IS the value (no
    ``.value`` dance at every emit site, and no surprise behaviour
    if a consumer compares against the literal).

    The six reasons reflect the disjoint set of paths that bypass
    the operator approval gate:

    - :attr:`SKILL` — workstream's skill template populated
      ``auto_approve_tools`` from its ``allowed_tools`` JSON list at
      create time.  The dashboard pill flags this so an operator
      can see when a child is silently auto-approving because of a
      previously-installed skill they may have forgotten about.
    - :attr:`ALWAYS` — operator clicked "Approve + Always" on a
      tool earlier in this session, adding its name to
      ``auto_approve_tools``.
    - :attr:`POLICY` — admin-defined ``tool_policies`` row with
      ``action='allow'`` matched the tool name (or pattern).
    - :attr:`BLANKET` — workstream-level ``auto_approve=True`` flag
      (server config / skill ``auto_approve``).  Drains every
      remaining pending tool unconditionally except for
      ``__budget_override__`` which always prompts.
    - :attr:`AUTO_APPROVE_TOOLS` — fallback when ``auto_approve_tools``
      contains a name but the per-tool source map was never
      populated (legacy / pre-source-tracking instances).  Visible
      as a generic pill rather than a misleading ``skill`` /
      ``always`` claim.
    - :attr:`SMART_APPROVAL` — Smart Approvals (``judge.smart_approvals``)
      auto-approved the call because the intent judge's LLM verdict
      recommended ``approve`` with confidence at or above
      ``judge.confidence_threshold``.  Distinct pill so an operator
      can see the judge — not a policy or a prior "Always" click —
      cleared this call.
    """

    SKILL = "skill"
    ALWAYS = "always"
    POLICY = "policy"
    BLANKET = "blanket"
    AUTO_APPROVE_TOOLS = "auto_approve_tools"
    SMART_APPROVAL = "smart_approval"

    ALL: frozenset[str] = frozenset(
        {SKILL, ALWAYS, POLICY, BLANKET, AUTO_APPROVE_TOOLS, SMART_APPROVAL}
    )


class SessionUIBase:
    """SSE listener fan-out + approval event machinery.

    Thread-safety: the ChatSession worker thread calls the ``on_*``
    methods (and the approval blocking helpers that live on
    subclasses); HTTP handlers drive ``_register_listener`` /
    ``_unregister_listener`` / ``resolve_approval`` from the event
    loop. All shared state is guarded by ``_listeners_lock`` /
    ``_ws_lock`` or ``threading.Event`` primitives.

    Two ``on_*`` methods are additionally safe to call from a
    *concurrent* auxiliary thread (e.g. background title generation in
    ``ChatSession._generate_title``, or ``task_agent`` sub-agents), even
    while the worker thread is mid-stream: :meth:`on_aux_usage` (a
    storage ``usage_event`` write + thread-safe metric counters — it
    touches none of the ``_ws_lock``-guarded inflight state
    :meth:`on_status`/token writers mutate) and :meth:`on_rename` (a
    queue / locked fan-out). Keep those two free of unguarded
    ``_ws_*`` writes so the auxiliary-thread guarantee holds.
    """

    def __init__(self, ws_id: str = "", user_id: str = "") -> None:
        self.ws_id = ws_id
        self._user_id = user_id
        # Acting user of the current/last turn (the ``bind_acting_user``
        # initiator, owner fallback) — pushed by ``ChatSession._emit_state``
        # so web clients can gate cross-user sends on a shared workstream
        # (disable send when busy AND this id != the viewer's own). Carries the
        # owner id even on a single-user authenticated session (the gate just
        # no-ops there — it equals the viewer); empty only on unauthenticated
        # lanes or before the session has emitted any state.
        self._acting_user_id: str = ""
        # SSE listener fan-out — one queue per connected browser tab.
        self._listeners: list[queue.Queue[dict[str, Any]]] = []
        self._listeners_lock = threading.Lock()
        # Per-ws event ring buffer for ``Last-Event-ID`` SSE replay.
        # Holds ``(event_id, event_dict)`` tuples; deque ``maxlen``
        # evicts the oldest automatically when the cap is hit.  The
        # listener fan-out path stamps every event with a monotonic
        # ``_event_id`` (see :meth:`_enqueue`) and appends here under
        # the same ``_listeners_lock`` that gates the per-listener
        # queues — keeps the buffer and the live fan-out in lockstep.
        # A reconnecting client with a ``Last-Event-ID`` header (or
        # ``?last_event_id=N`` query-param fallback for manual reconnect
        # paths that can't set custom headers) is served the slice of
        # the buffer past that id; clients whose ``Last-Event-ID``
        # predates the buffer's earliest retained id get a
        # ``replay_truncated`` envelope plus the in-progress snapshot
        # as the recovery floor.  Guarded by ``_listeners_lock`` (NOT
        # ``_ws_lock``) so a writer holding ``_ws_lock`` for the
        # inflight-buffer append doesn't serialize the buffer write
        # against unrelated readers.
        self._event_buffer: collections.deque[tuple[int, dict[str, Any]]] = collections.deque(
            maxlen=_EVENT_BUFFER_MAX
        )
        # Monotonic per-ws event counter.  Stamps every fan-out event
        # (every ``_enqueue`` call) and also drives the existing
        # ``_seq`` snapshot-dedup tag on token events (``content`` /
        # ``reasoning``) — one counter, two consumers.  Renamed from
        # the pre-replay ``_ws_inflight_seq`` because the counter now
        # spans every event, not just the inflight token stream.
        # Guarded by ``_listeners_lock`` (incremented under that lock
        # in :meth:`_enqueue`); the snapshot helper for the in-progress
        # replay path captures it under ``_listeners_lock`` too.
        self._event_id: int = 0
        # Sub-agent step tagging: child tool call_id -> parent task_agent
        # call_id.  ``_enqueue`` reads it to stamp ``parent_call_id`` on every
        # child event (tool_pending / approve_request / tool_result /
        # tool_output_chunk / output_warning) so the UI can nest a task agent's
        # steps under its card.  Written by ``note_agent_child`` /
        # ``clear_agent_children`` (the session brackets each ``_run_agent``);
        # keyed on the immutable call_id so it stays correct under the parent's
        # 4-wide parallel tool pool (several task agents in flight at once).  Its
        # own lock so the hot fan-out path never serializes on ``_listeners_lock``.
        self._agent_children: dict[str, str] = {}
        self._agent_children_lock = threading.Lock()
        # Recall store: a finished task agent's projected sub-trajectory (step
        # items: id/name/arguments/output/is_error), keyed by its (parent)
        # call_id, so /history can rebuild the collapsible card after a fresh
        # connect / reopen while the workstream is still in memory.  LRU-bounded
        # (oldest evicted past the cap); IN-MEMORY ONLY — durable persistence is
        # deferred, so a cold reopen (new session) finds it empty and renders the
        # flat parent record.  Guarded by ``_agent_children_lock`` (same low-rate
        # agent-state path).  [[HYPOTHESIS]] the sub-trajectory is the ledger;
        # an absent one is unknown ("not retained"), never none ("0 steps").
        self._agent_trajectories: collections.OrderedDict[str, list[dict[str, Any]]] = (
            collections.OrderedDict()
        )
        # (Sub-agent ``on_info`` suppression is per-thread via the module-level
        # ``_agent_scope_var`` contextvar — no instance field.)
        # Approval blocking — each gate thread (main loop, or a parallel
        # task-agent thread) registers an :class:`ApprovalCycle` here and
        # waits on the CYCLE's own event; the /approve endpoint routes a
        # decision to exactly one cycle via resolve_approval.  Insertion-
        # ordered (dict) = oldest-first, which is the resolution order for
        # legacy selector-less approvals.  Guarded by ``_ws_lock``.
        self._approval_cycles: dict[str, ApprovalCycle] = {}
        # Legacy single-slot view of the OLDEST live cycle's card — many
        # read-only consumers (cancel gating, dashboard booleans, history
        # ``awaiting_approval``) only need "is something pending"; they
        # keep working unchanged against this view.  Maintained under
        # ``_ws_lock`` by ``_refresh_pending_approval_view`` on every
        # register/unregister.  Multi-cycle-aware surfaces (SSE replay,
        # dashboard detail, approve routing) use ``_approval_cycles``.
        self._pending_approval: dict[str, Any] | None = None
        self.auto_approve = False
        self.auto_approve_tools: set[str] = set()
        # Smart Approvals (``judge.smart_approvals``): when enabled, a
        # tool call whose LLM intent verdict recommends ``approve`` with
        # confidence ≥ ``smart_approval_threshold`` is auto-approved
        # without an operator prompt.  ChatSession pushes these three
        # values onto the UI from the live judge config each turn (just
        # before ``approve_tools``) so a hot-reloaded settings change
        # takes effect on the next batch.  Defaults keep the feature off
        # for any UI the session doesn't configure (eval, fixtures).
        self.smart_approvals_enabled = False
        self.smart_approval_threshold = 0.95
        # How long ``approve_tools`` waits for the async LLM verdict
        # before falling back to a human prompt (fail-closed).  Bounded
        # by the judge timeout; the wait returns early the moment every
        # pending call has a verdict.
        self.smart_approval_wait_seconds = 0.0
        # Per-tool source for ``auto_approve_tools`` membership.  Two
        # writers populate the set with semantically different intent:
        #
        # - **Skill template** at create time (the ``allowed_tools``
        #   JSON list landing on ``auto_approve_tools`` from
        #   ``server.py``'s skill block) — operator may not have
        #   explicitly opted in tool-by-tool.
        # - **User "Approve + Always"** click at runtime — explicit
        #   per-tool consent from the live operator.
        #
        # Without per-tool source tracking the dashboard can't tell
        # the operator WHICH path silently approved a tool call.
        # Maps ``approval_label_or_func_name → source_string``;
        # callers populate at the same point they update the set
        # itself.  Default empty when neither writer ran (e.g. CLI
        # ``/always`` doesn't set this — pre-existing).
        self._auto_approve_tools_source: dict[str, str] = {}
        # Ring buffer of recent auto-approve events for /dashboard
        # visibility — a child workstream whose tool calls bypass the
        # approval gate (skill allowlist / blanket / admin policy)
        # would otherwise leave no operator-facing trace at all.  Fed
        # by :meth:`_record_auto_approves`; surfaced through
        # :meth:`serialize_recent_auto_approvals` and the per-ws
        # ``/dashboard`` payload.  Capped so a long-running skill
        # workstream can't fill the live block with stale rows.
        self._recent_auto_approvals: list[dict[str, Any]] = []
        # Maps ``call_id`` → ``(auto_approve_reason, inserted_ts)`` for
        # verdicts that arrive AFTER ``approve_tools`` already returned.
        # The LLM judge tier is asynchronous: ``on_intent_verdict`` can
        # fire seconds later for a tool that ``approve_tools``
        # short-circuited via one of the auto-approve branches.  Without
        # this lookup the late-arriving LLM verdict lands with
        # ``user_decision="pending"`` and stays that way forever (no
        # ``resolve_approval`` cycle on the auto-approve path).
        #
        # Lifetime is bounded by ``_AUTO_APPROVE_REASON_TTL`` rather
        # than by a count-cap or by session lifetime: a fixed cap
        # would silently break the fix on the (N+1)th in-flight
        # auto-approve; "evict on consume" alone would leak entries
        # whenever the LLM judge is disabled (no ``on_intent_verdict``
        # ever fires to drain them).  TTL means entries clear lazily
        # on the next ``_record_auto_approves`` write whether or not
        # the LLM judge tier is active.  Guarded by ``_ws_lock``.
        self._auto_approve_reasons: dict[str, tuple[str, float]] = {}
        # Foreground gate — used by the CLI's WorkstreamTerminalUI to
        # block output when the workstream is in the background.
        # Starts set so non-CLI UIs can skip any explicit management.
        # Also read by cleanup_session_ui so close() can unblock any
        # waiter.
        self._fg_event = threading.Event()
        self._fg_event.set()
        # Per-workstream metrics + verdict bookkeeping. Written by the
        # worker thread (on_tool_result / on_status / on_intent_verdict)
        # and read by HTTP handlers (/metrics, /dashboard). Guarded by
        # ``_ws_lock`` so HTTP reads see a consistent snapshot even
        # mid-turn. Coord sessions previously tracked none of this —
        # they share a ChatSession that emits the same usage/verdict
        # hooks, so the dashboard gets coord visibility for free once
        # a consumer (future work) wires it up.
        self._ws_lock = threading.Lock()
        self._ws_prompt_tokens: int = 0
        self._ws_completion_tokens: int = 0
        self._ws_messages: int = 0
        self._ws_tool_calls: dict[str, int] = {}
        self._ws_tool_calls_reported: int = 0
        self._ws_context_ratio: float = 0.0
        self._ws_turn_tool_calls: int = 0
        # Activity tracking for dashboard ("thinking" / "tool" / "").
        self._ws_current_activity: str = ""
        self._ws_activity_state: str = ""
        # Turn-content accumulator: assistant tokens piggybacked onto
        # the ``ws_state:idle`` broadcast so the dashboard renders the
        # turn without an extra storage round-trip. Cleared on IDLE /
        # ERROR transitions by :meth:`snapshot_and_consume_state_payload`.
        # Multi-turn (per-``send()``) — accumulates across all internal
        # turns within one user-facing send.
        self._ws_turn_content: list[str] = []
        self._ws_turn_content_size: int = 0
        # Per-turn inflight accumulators: the in-progress turn's content
        # + reasoning, exposed to a reconnecting SSE client via the
        # ``in_progress_snapshot`` event so a mid-stream page refresh
        # restores the partial assistant text. Reset at the start of
        # each turn by :meth:`on_turn_start` (separate from the multi-
        # turn IDLE-piggyback buffer above so prior committed turns
        # don't leak into the snapshot and double-render against the
        # replayed history).  The per-turn dedup-tag counter
        # (``_event_id``) is initialised above alongside the per-ws
        # event ring buffer — one monotonic counter drives both the
        # ``Last-Event-ID`` replay slice AND the existing snapshot
        # ``_seq <= snap_seq`` filter; see :meth:`_enqueue` for the
        # stamping pattern.
        self._ws_inflight_content: list[str] = []
        self._ws_inflight_content_size: int = 0
        self._ws_inflight_reasoning: list[str] = []
        self._ws_inflight_reasoning_size: int = 0
        # Last broadcast (activity, activity_state) tuple — used by
        # :meth:`_broadcast_activity` overrides to dedup back-to-back
        # identical activity ticks. Tool-heavy turns can fire many
        # ``on_tool_result`` calls in succession that all clear the
        # activity to ``("", "")``; without the dedup, each fan-out
        # acquires the cluster collector's lock for a no-op write.
        # ``None`` until the first broadcast so the first emit always
        # fires.
        self._last_broadcast_activity: tuple[str, str] | None = None
        # Decisions of recently-resolved cycles (call_id → decision
        # string) so a late LLM verdict — the run-to-completion daemon
        # can deliver after its gate resolved — is stamped with what the
        # operator actually chose.  Replaces the single
        # ``_last_verdict_decision`` string, which under concurrent
        # cycles stamped sibling B's late verdicts with sibling A's
        # decision.  FIFO-bounded; guarded by ``_ws_lock``.
        self._recent_decisions: collections.OrderedDict[str, tuple[str, threading.Event | None]] = (
            collections.OrderedDict()
        )
        # Verdict cache for SSE reconnect replay (tab switching
        # shouldn't lose the judge's final call on a just-run tool).
        self._llm_verdicts: dict[str, dict[str, Any]] = {}
        # Judge-generation origin per cached verdict: ``call_id`` →
        # ``id(cancel_event)`` of the daemon that delivered it.  The
        # Smart-Approvals qualification loop compares this against the
        # batch's own generation so a stale daemon's verdict (reused
        # call_id across turns) can satisfy neither the wait nor the
        # auto-approve bar.  Evicted in lockstep with ``_llm_verdicts``.
        self._verdict_origins: dict[str, int] = {}
        # Signalled by ``on_intent_verdict`` whenever an LLM verdict
        # lands in ``_llm_verdicts``; Smart Approvals (``approve_tools``)
        # waits on it for the verdicts of the calls it's about to gate.
        # Shares ``_ws_lock`` so the wait + the cache write are one
        # critical section (no separate lock to order).
        self._verdict_cond = threading.Condition(self._ws_lock)
        # Re-populate the recent-auto-approve ring buffer from the
        # audit log so the dashboard pill survives UI rebuilds —
        # saved-workstream rehydrate / coord→node click-through /
        # process restart all build a fresh UI whose buffer would
        # otherwise start empty even though the audit row is still
        # there.  Best-effort: storage outage / not-yet-wired silently
        # leaves the buffer empty (the next live auto-approve will
        # populate it).  Runs last in __init__ so all the lock + state
        # fields the replay touches are already initialised.
        self.replay_recent_auto_approvals_from_audit()
        # Reseed the monotonic event-id counter from the persisted
        # high-water so the ``Last-Event-ID`` cursor space stays
        # monotonic across UI rebuilds (process restart / rehydrate /
        # coord→node click-through).  Without this it would restart at
        # 0 and re-issue ids the prior process already stamped onto
        # ``conversations.event_id`` rows, corrupting cursor ordering.
        self._seed_event_id_from_storage()

    # ------------------------------------------------------------------
    # Listener plumbing (SSE)
    # ------------------------------------------------------------------

    def _enqueue(self, data: dict[str, Any]) -> int:
        """Fan ``data`` out to every registered listener queue.

        Stamps ``ws_id`` on the payload if not already present so the
        browser can validate it belongs to the pane's current
        workstream.  Stamps a monotonic ``_event_id`` on every event
        (drives the ``Last-Event-ID`` replay buffer) and additionally
        stamps the per-turn snapshot dedup tag ``_seq`` on token
        events (``content`` / ``reasoning``) so the existing
        in-progress snapshot dedup at the events handler stays
        byte-identical.  Shallow-copies before each stamp so a
        caller-owned dict is never mutated.

        The counter increment, the buffer append, AND the listener
        snapshot all run under ``_listeners_lock`` so a concurrent
        :meth:`register_listener_with_in_progress_snapshot` or
        :meth:`register_listener_with_replay` sees a consistent
        ``(event_id, listeners, buffer)`` tuple — no event is
        fanned out to a not-yet-registered listener AND missing from
        the replay buffer.

        Returns the monotonic ``_event_id`` assigned to this event so a
        caller that also persists the same turn (e.g.
        ``ChatSession._append_system_turn``) can stamp the row with the
        matching id, keeping the ``/history`` resume cursor and the live
        event stream aligned.
        """
        if "ws_id" not in data:
            data = {**data, "ws_id": self.ws_id}
        # Only events that can carry a child step — a top-level ``call_id`` or an
        # ``items`` list — need the parent-tag lookup; skip the lock+scan for the
        # high-frequency rest (content / reasoning / status / info / …).
        if self._agent_children and ("call_id" in data or "items" in data):
            data = self._stamp_agent_parent(data)
        with self._listeners_lock:
            self._event_id += 1
            event_id = self._event_id
            data = {**data, "_event_id": event_id}
            if data.get("type") in ("content", "reasoning"):
                # Preserve the existing dedup contract: only token
                # events carry the ``_seq`` tag.  Non-token events
                # (``tool_started``, ``state_change``, …) keep
                # bypassing the snapshot filter by absence of ``_seq``.
                data = {**data, "_seq": event_id}
            self._event_buffer.append((event_id, data))
            snapshot = list(self._listeners)
        for lq in snapshot:
            with contextlib.suppress(queue.Full):
                lq.put_nowait(data)
        return event_id

    def _stamp_agent_parent(self, data: dict[str, Any]) -> dict[str, Any]:
        """Stamp ``parent_call_id`` on a sub-agent's child event.

        A task agent's sub-tool events flow through the same emit path as
        top-level tool events; the only thing marking them as a *child* is the
        ``call_id`` registered in ``_agent_children`` by the session running
        ``_run_agent``.  Stamping at this one fan-out choke point (rather than at
        each call site) also catches events emitted from a tool's own background
        thread — a streaming bash's ``tool_output_chunk`` — which an ambient
        context var set on the worker thread would miss.  Returns a shallow copy
        when it stamps; the input dict is never mutated."""
        with self._agent_children_lock:
            if not self._agent_children:
                return data
            cid = data.get("call_id")
            if isinstance(cid, str) and cid in self._agent_children:
                data = {**data, "parent_call_id": self._agent_children[cid]}
            items = data.get("items")
            if isinstance(items, list) and any(
                isinstance(it, dict) and it.get("call_id") in self._agent_children for it in items
            ):
                data = {
                    **data,
                    "items": [
                        {**it, "parent_call_id": self._agent_children[it["call_id"]]}
                        if isinstance(it, dict) and it.get("call_id") in self._agent_children
                        else it
                        for it in items
                    ],
                }
        return data

    def note_agent_child(self, child_call_id: str, parent_call_id: str) -> None:
        """Register a sub-agent's child tool call so ``_enqueue`` tags its events
        with ``parent_call_id``.  Called by the session for each sub-tool a
        ``_run_agent`` issues, before the tool emits anything.

        The session namespaces each sub-agent's child ids by parent
        (``f"{parent_call_id}::{tc_id}"``) before registering them here, so the
        key is unique even for local servers that assign per-response sequential
        ids (``call_0``) — two task agents in the parent's 4-wide pool can't
        collide and mis-nest steps."""
        if not child_call_id or not parent_call_id:
            return
        with self._agent_children_lock:
            self._agent_children[child_call_id] = parent_call_id

    def clear_agent_children(self, parent_call_id: str) -> None:
        """Drop every child registered under ``parent_call_id`` (the task agent
        finished).  Bounds the registry to in-flight task agents.  Deletes in
        place rather than reallocating the whole dict, so one agent completing
        doesn't churn other in-flight agents' entries."""
        with self._agent_children_lock:
            for c in [c for c, p in self._agent_children.items() if p == parent_call_id]:
                del self._agent_children[c]

    def on_agent_step(self, parent_call_id: str, item: dict[str, Any]) -> None:
        """Paint a sub-agent's auto-executed tool step as pending under its
        parent card so it shows live before it completes.  (Approval-gated
        sub-tools paint via :meth:`approve_tools`.)  The child is already
        registered via ``note_agent_child``, so ``_enqueue`` stamps
        ``parent_call_id`` onto this ``tool_pending``."""
        self._enqueue({"type": "tool_pending", "items": self._serialize_approval_items([item])})

    def begin_agent_scope(self) -> None:
        """Enter a task agent's execution.  Until the matching
        :meth:`end_agent_scope`, the web pane drops ``on_info`` lines from THIS
        thread (the task card carries the sub-agent's visible output, so the info
        chatter would only escape to the top level).  Per-thread via
        :data:`_agent_scope_var` so a parallel SIBLING tool in another pool
        thread isn't suppressed; depth-counted for safety though task agents
        don't nest.  The session brackets each ``_run_agent`` with this pair."""
        _agent_scope_var.set(_agent_scope_var.get() + 1)

    def end_agent_scope(self) -> None:
        """Leave a task agent's execution (see :meth:`begin_agent_scope`)."""
        _agent_scope_var.set(max(0, _agent_scope_var.get() - 1))

    def stash_agent_trajectory(self, call_id: str, steps: list[dict[str, Any]]) -> None:
        """Retain a finished task agent's projected sub-trajectory (step items)
        keyed by its call_id so ``/history`` can rebuild the card on a fresh
        connect / reopen while the workstream is in memory.  LRU-bounded; the
        newest write is most-recently-used, oldest evicted past the cap.  See
        :data:`_AGENT_TRAJECTORY_CAP` and the field comment in ``__init__``."""
        if not call_id:
            return
        with self._agent_children_lock:
            self._agent_trajectories[call_id] = steps
            self._agent_trajectories.move_to_end(call_id)
            while len(self._agent_trajectories) > _AGENT_TRAJECTORY_CAP:
                self._agent_trajectories.popitem(last=False)

    def get_agent_trajectory(self, call_id: str) -> list[dict[str, Any]] | None:
        """Read a stashed sub-trajectory, or ``None`` if not retained (evicted,
        or a cold reopen with an empty store).  ``None`` is the honest "unknown"
        signal — the caller renders the flat parent record, never a 0-step card."""
        if not call_id:
            return None
        with self._agent_children_lock:
            return self._agent_trajectories.get(call_id)

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

    def register_listener_with_in_progress_snapshot(
        self, maxsize: int = _DEFAULT_LISTENER_QUEUE_MAX
    ) -> tuple[queue.Queue[dict[str, Any]], dict[str, Any]]:
        """Register a listener AND snapshot the per-turn inflight buffers.

        Used by :func:`make_events_handler` (the fresh-connect path,
        and the ``replay_truncated`` fallback path) so a SSE subscriber
        connecting mid-stream can be told the in-progress turn's content
        and reasoning text-so-far in a one-shot ``in_progress_snapshot``
        event, on top of the kind-specific replay (history / pending).
        The ``Last-Event-ID`` replay path (see
        :meth:`register_listener_with_replay`) bypasses this — the
        buffered events already carry the partial token stream.

        Lock acquisition order: ``_ws_lock`` (outer) → ``_listeners_lock``
        (inner) — matches the writer's order in :meth:`on_content_token`
        / :meth:`on_reasoning_token` (``_ws_lock`` then ``_enqueue``'s
        ``_listeners_lock``).  Nested under both locks we read
        ``inflight_content``, ``inflight_reasoning``, AND the
        ``_event_id`` counter as a consistent triple, plus register
        the listener.  Writers calling :meth:`_enqueue` block on
        ``_listeners_lock`` for the snapshot's duration so no event is
        fanned out between counter-read and listener-registration —
        every event with ``_event_id > snap_seq`` lands in the
        registered listener's queue, every event with
        ``_event_id <= snap_seq`` is already covered by the snapshot's
        ``content`` / ``reasoning`` text or by token events that the
        events handler's ``_seq <= snap_seq`` filter drops.

        Returns ``(client_queue, snapshot_dict)`` where ``snapshot_dict``
        has keys ``content`` (str), ``reasoning`` (str), ``seq`` (int).
        Caller checks for non-empty content / reasoning to decide
        whether to yield the event at all (empty snapshots are common
        between turns and on freshly-opened workstreams).

        Joins the captured fragments OUTSIDE the locks — bounded at
        ``_MAX_TURN_CONTENT_CHARS`` but still O(n) over fragments, so
        worth not blocking concurrent on-token writers for the
        duration. The shallow ``list(...)`` copies under the lock mean
        subsequent appends to the live buffers don't mutate our view.
        """
        client_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=maxsize)
        with self._ws_lock:
            captured_content = list(self._ws_inflight_content)
            captured_reasoning = list(self._ws_inflight_reasoning)
            with self._listeners_lock:
                self._listeners.append(client_queue)
                snap_seq = self._event_id
        return client_queue, {
            "content": "".join(captured_content),
            "reasoning": "".join(captured_reasoning),
            "seq": snap_seq,
        }

    def register_listener_with_replay(
        self,
        last_event_id: int,
        maxsize: int = _DEFAULT_LISTENER_QUEUE_MAX,
    ) -> tuple[
        queue.Queue[dict[str, Any]],
        list[dict[str, Any]],
        str,
        int,
        int,
        dict[str, Any],
    ]:
        """Register a listener AND capture buffered events for replay
        AND snapshot the per-turn inflight content/reasoning + snap_seq
        in one atomic-against-writers step.

        Used by :func:`make_events_handler` when the client sends
        ``Last-Event-ID`` (header or ``?last_event_id=`` query-param
        fallback for the manual-reconnect path).  Returns

            ``(client_queue, replay_events, status, lost_count,
            earliest_available_id, snapshot)``

        where ``status`` is one of ``"replay_ok"`` (caller emits the
        replay events then drops into live drain, skipping
        ``replay_cb`` / ``state_change`` / ``in_progress_snapshot``)
        or ``"truncated"`` (caller emits a ``replay_truncated``
        envelope then falls through to the fresh-connect replay path
        as the recovery floor — the snapshot picks up the partial
        content/reasoning that the evicted events would have carried).
        ``snapshot`` has the same shape as
        :meth:`register_listener_with_in_progress_snapshot`'s second
        return value: ``{"content": str, "reasoning": str, "seq": int}``.

        Atomicity contract: under ``_ws_lock`` (outer) + ``_listeners_lock``
        (inner) — matches writer order in :meth:`on_content_token` —
        we snapshot the buffer, the listener registration, the
        inflight content/reasoning, AND the ``_event_id`` counter as
        a consistent tuple.  Writers' :meth:`_enqueue` blocks on
        ``_listeners_lock`` for the duration, so events either
          - land in the buffer snapshot but NOT the listener queue
            (writer ran before our lock acquire — caught by the
            replay slice on the ``replay_ok`` path, or by the
            content snapshot on the ``truncated`` path), or
          - land in the listener queue but NOT the buffer snapshot
            (writer ran after our lock release — live drain handles
            them, ``_event_id`` is strictly above
            ``earliest_available_id`` AND strictly above
            ``snapshot["seq"]``).
        No event is double-delivered, none is lost across the
        registration boundary.  Crucially, the truncated path can
        now use ``snapshot["seq"]`` as the live-drain ``snap_seq``
        filter — the events handler's existing ``_seq <= snap_seq``
        dedup catches any token event that landed in the listener
        queue AND was covered by the snapshot's content/reasoning
        text (prevents double-rendering after a truncated emit).

        ``last_event_id`` semantics:
          - ``< earliest_available_id - 1`` → ``"truncated"``.
            ``lost_count`` is the minimum gap (the buffer may have
            evicted strictly more than this — we only know the
            lower bound from what's still retained).
          - ``>= earliest_available_id - 1`` → ``"replay_ok"``.  Replay
            events are those with id strictly greater than
            ``last_event_id`` (the client has already seen everything
            up to and including ``last_event_id``).

        Empty buffer: returned ``status="replay_ok"`` with empty
        ``replay_events`` regardless of ``last_event_id``.  This is
        the cold-start case (the ws just bootstrapped with no events
        ever) and the all-quiet case (a long-idle ws past which all
        events fall out of the buffer cap, but in practice the buffer
        starts evicting only after the cap is hit — which means the
        counter is at the cap and the client's last_event_id is below
        earliest, so they get ``truncated`` instead).  We can't
        distinguish the two without a separate ``highest_evicted_id``
        tracker; treating empty as ``replay_ok`` is the safe choice
        for the genuine cold-start case (no false ``replay_truncated``
        envelopes on freshly-opened workstreams).
        """
        client_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=maxsize)
        # Lock order matches writer: ``_ws_lock`` outer, ``_listeners_lock``
        # inner.  Both inflight buffers AND the buffer slice AND the
        # ``_event_id`` counter AND the listener registration captured
        # as one atomic against any concurrent ``_enqueue``.  The string
        # joins for content/reasoning happen OUTSIDE the locks (bounded
        # at ``_MAX_TURN_CONTENT_CHARS`` but O(n) over fragments — not
        # worth blocking on-token writers for the duration).  See
        # the per-fresh-path helper for the same rationale.
        with self._ws_lock:
            captured_content = list(self._ws_inflight_content)
            captured_reasoning = list(self._ws_inflight_reasoning)
            with self._listeners_lock:
                buffered = list(self._event_buffer)
                self._listeners.append(client_queue)
                snap_seq = self._event_id
        snapshot: dict[str, Any] = {
            "content": "".join(captured_content),
            "reasoning": "".join(captured_reasoning),
            "seq": snap_seq,
        }
        if not buffered:
            return client_queue, [], "replay_ok", 0, 0, snapshot
        earliest_id = buffered[0][0]
        if last_event_id < earliest_id - 1:
            lost_count = (earliest_id - 1) - last_event_id
            return client_queue, [], "truncated", lost_count, earliest_id, snapshot
        replay_events = [ev for eid, ev in buffered if eid > last_event_id]
        return client_queue, replay_events, "replay_ok", 0, earliest_id, snapshot

    def can_replay_from(self, cursor: int) -> bool:
        """Would an SSE connect with ``Last-Event-ID = cursor`` replay a
        non-empty delta through the ``replay_ok`` path?

        This is the ``/history`` gate for the fresh-connect fast-forward:
        the handler returns a resume cursor (and drops the in-flight turn
        from the committed snapshot) ONLY when this is true, so the
        in-flight turn is rebuilt from the ring buffer via the existing
        delta replay.  When it is false — empty buffer (cold reload /
        process restart), an evicted/truncated cursor, or simply no
        events past ``cursor`` — the handler keeps the in-flight turn in
        ``/history`` and returns no cursor, so the connect takes the
        synthetic-snapshot floor (preserving the #610 in-flight render
        and never leaving a turn unrenderable).

        Mirrors :meth:`register_listener_with_replay`'s slice semantics
        without registering a listener:
          - empty buffer → False (nothing buffered to fast-forward);
          - ``cursor < earliest_id - 1`` → False (would be ``truncated``);
          - no event id strictly greater than ``cursor`` → False (the
            delta would be empty — nothing in-flight to replay).
        """
        with self._listeners_lock:
            if not self._event_buffer:
                return False
            earliest_id = self._event_buffer[0][0]
            latest_id = self._event_buffer[-1][0]
        if cursor < earliest_id - 1:
            return False
        return latest_id > cursor

    # ------------------------------------------------------------------
    # Approval blocking gates
    # ------------------------------------------------------------------

    def _purge_round_verdicts(
        self,
        call_ids: set[str],
        keep_origin: threading.Event | None = None,
    ) -> None:
        """Evict stale verdict state for the call_ids entering a new gate.

        Successor of the old whole-cache ``_reset_approval_cycle``: with
        concurrent cycles a full clear wiped a live sibling's verdicts
        mid-wait, so eviction is now scoped to the entering batch.  It
        protects the same two things the reset did — a provider that
        reuses call_ids across turns can't have a PRIOR round's cached
        verdict pre-satisfy this round's Smart-Approvals wait, and the
        reconnect-replay cache can't serve that stale verdict onto the
        new round's card — without touching any other cycle's state.

        ``keep_origin`` is the entering batch's own judge generation
        (its cancel event): a cached verdict THAT generation delivered
        survives the purge.  The judge daemon is spawned before the
        gate is entered, so a fast judge can deliver ahead of this
        purge (the policy DB round-trip sits in between) — evicting its
        verdict would stall the Smart-Approvals wait to its full budget
        and send an already-cleared batch to a human.  Prior-round
        decisions for a reused call_id are evicted unconditionally:
        this round has not been decided yet, whatever generation ruled
        before.
        """
        with self._ws_lock:
            for cid in call_ids:
                if keep_origin is None or self._verdict_origins.get(cid) != id(keep_origin):
                    self._llm_verdicts.pop(cid, None)
                    self._verdict_origins.pop(cid, None)
                self._recent_decisions.pop(cid, None)

    # ------------------------------------------------------------------
    # Approval-cycle registry
    # ------------------------------------------------------------------

    def _refresh_pending_approval_view(self) -> None:
        """Point the legacy ``_pending_approval`` slot at the oldest cycle.

        Caller must hold ``_ws_lock``.  Boolean-ish consumers (cancel
        gating, dashboard ``pending_approval`` flags, history
        ``awaiting_approval``) read the slot directly; they see "some
        cycle is live", which is exactly the question they ask.
        """
        first = next(iter(self._approval_cycles.values()), None)
        self._pending_approval = first.card if first is not None else None

    def _register_approval_cycle(self, cycle: ApprovalCycle) -> None:
        with self._ws_lock:
            self._approval_cycles[cycle.cycle_id] = cycle
            self._refresh_pending_approval_view()

    def _unregister_approval_cycle(self, cycle: ApprovalCycle) -> None:
        with self._ws_lock:
            self._approval_cycles.pop(cycle.cycle_id, None)
            self._refresh_pending_approval_view()

    def _select_cycle_locked(
        self,
        *,
        cycle_id: str | None = None,
        call_id: str | None = None,
    ) -> ApprovalCycle | None:
        """Pick the cycle a decision addresses.  Caller holds ``_ws_lock``.

        Precedence: explicit ``cycle_id`` → member ``call_id`` → the
        oldest unresolved cycle (legacy clients that never learned to
        send a selector: CLI wrappers, channel adapters, old tabs).
        Only unresolved cycles are eligible — a second click racing the
        gate thread's unregister must not re-resolve.
        """
        if cycle_id:
            cycle = self._approval_cycles.get(cycle_id)
            return cycle if cycle is not None and not cycle.resolved else None
        if call_id:
            for cycle in self._approval_cycles.values():
                if call_id in cycle.call_ids and not cycle.resolved:
                    return cycle
            return None
        return next((c for c in self._approval_cycles.values() if not c.resolved), None)

    def find_approval_cycle(
        self,
        *,
        cycle_id: str | None = None,
        call_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Public card lookup for HTTP handlers (pre-resolve inspection).

        Returns the matched cycle's ``approve_request`` card (the dict
        also used for SSE replay), or ``None``.  Same selector
        precedence as resolution, so "which cycle will this decision
        hit" and "which cycle did it hit" can never disagree.
        """
        with self._ws_lock:
            cycle = self._select_cycle_locked(cycle_id=cycle_id, call_id=call_id)
            return cycle.card if cycle is not None else None

    def pending_approval_cards(self) -> list[dict[str, Any]]:
        """All live cycles' ``approve_request`` cards, oldest first.

        SSE fresh-connect/reconnect replay yields each so a returning
        tab repaints EVERY outstanding prompt, not just the newest.
        """
        with self._ws_lock:
            return [c.card for c in self._approval_cycles.values()]

    def resolve_approval(
        self,
        approved: bool,
        feedback: str | None = None,
        *,
        always: bool = False,
        timeout: bool = False,
        call_id: str | None = None,
        cycle_id: str | None = None,
    ) -> str | None:
        """Unblock ONE pending approval cycle with the caller's decision.

        Routes to a single :class:`ApprovalCycle` — by ``cycle_id``, by
        member ``call_id``, or (selector-less legacy callers: CLI
        wrappers, channel adapters, cancel/timeout paths) the oldest
        unresolved cycle.  Returns the resolved ``cycle_id`` or ``None``
        when nothing matched — a second click racing the gate thread, or
        a stale selector.  One decision can no longer fan out to every
        parked gate: each cycle owns its event/result, which is the
        whole fix for the cross-approval hazard the singleton slot had.

        Broadcasts ``approval_resolved`` (now carrying ``cycle_id`` +
        ``call_ids``) so every connected tab dismisses the RIGHT prompt
        in sync. Updates ``user_decision`` on every LLM intent-verdict
        that fired during this cycle's round — the audit trail reflects
        what the user actually chose for THESE calls.

        ``always`` reports whether the resolving caller asked for
        "Approve + Always" (the tool name has been added to
        ``auto_approve_tools`` upstream by the HTTP handler — this
        method only echoes the intent on the SSE event so peer tabs
        can label their resolved-status pill correctly).

        ``timeout`` flips the persisted ``user_decision`` from
        ``"denied"`` to ``"timeout"`` so the audit trail can
        distinguish an active user denial from a passive
        approval-timeout expiry.  Mutually exclusive with
        ``approved=True`` (a timeout is a passive denial); the
        combination raises ``ValueError``.
        """
        if timeout and approved:
            raise ValueError("resolve_approval: timeout=True is incompatible with approved=True")
        decision_str = "timeout" if timeout else ("approved" if approved else "denied")
        # Select + mark resolved + swap verdicts out under ONE lock
        # acquisition so a concurrent resolver can't double-resolve and
        # the judge daemon's ``on_intent_verdict`` can't append to a
        # list we're about to stamp.
        with self._ws_lock:
            cycle = self._select_cycle_locked(cycle_id=cycle_id, call_id=call_id)
            if cycle is None:
                return None
            cycle.resolved = True
            cycle.decision = decision_str
            cycle.result = (approved, feedback)
            pending = cycle.pending_verdicts
            cycle.pending_verdicts = []
            # Remember the decision per call_id — tagged with the
            # cycle's judge generation — so this round's late verdicts
            # (run-to-completion daemon) stamp correctly even after the
            # cycle is unregistered, while a STALE generation's late
            # verdict under a reused call_id can be told apart and
            # stamped ``superseded`` instead of stealing this decision.
            for cid in cycle.call_ids:
                self._recent_decisions[cid] = (decision_str, cycle.judge_event)
                self._recent_decisions.move_to_end(cid)
            while len(self._recent_decisions) > _RECENT_DECISION_CAP:
                self._recent_decisions.popitem(last=False)
        if pending:
            self._persist_verdict_decisions(pending, decision_str)
        sorted_call_ids = sorted(cycle.call_ids)
        self._enqueue(
            {
                "type": "approval_resolved",
                "approved": approved,
                "feedback": feedback or "",
                "always": bool(always),
                "cycle_id": cycle.cycle_id,
                "call_ids": sorted_call_ids,
            }
        )
        # Kind-specific cross-stream broadcast — ConsoleCoordinatorUI
        # overrides to push onto the cluster bus so a coord parent's
        # tree UI clears the pending-approval pill in lockstep with
        # the actual decision. Stage 3 Step 4.
        self._broadcast_approval_resolved(
            approved,
            feedback,
            always=always,
            cycle_id=cycle.cycle_id,
            call_ids=tuple(sorted_call_ids),
        )
        cycle.event.set()
        return cycle.cycle_id

    def resolve_all_approvals(
        self,
        approved: bool,
        feedback: str | None = None,
        *,
        timeout: bool = False,
    ) -> int:
        """Resolve EVERY live cycle with one decision; returns the count.

        For the sweep paths where the operator's intent addresses the
        workstream, not a specific batch: cancel ("stop everything"),
        session close, worker-recovery.  Loops :meth:`resolve_approval`
        oldest-first so per-cycle bookkeeping (verdict stamps, SSE
        dismissals, per-cycle events) runs identically to a targeted
        resolution.

        Deliberately optimistic about concurrency: the scan re-runs
        after every attempt, so a cycle that a gate timeout (or another
        resolver) claims between our scan and our
        :meth:`resolve_approval` call is simply not counted — it was
        resolved either way — and the rescan picks up cycles that
        REGISTER mid-sweep, which a snapshot-then-resolve loop would
        miss.  Termination: every iteration either resolves its target
        or observes it already resolved; resolved cycles never re-enter
        the scan.
        """
        count = 0
        while True:
            with self._ws_lock:
                target = next((c for c in self._approval_cycles.values() if not c.resolved), None)
                target_id = target.cycle_id if target is not None else None
            if target_id is None:
                return count
            if self.resolve_approval(approved, feedback, timeout=timeout, cycle_id=target_id):
                count += 1

    @staticmethod
    def _persist_verdict_decisions(
        pending: list[dict[str, Any]],
        decision_str: str,
    ) -> None:
        """Fire-and-forget UPDATE of ``user_decision`` on each verdict row."""
        try:
            from turnstone.core.storage._registry import get_storage

            storage = get_storage()
            if storage is None:
                return
            for v in pending:
                vid = v.get("verdict_id", "")
                if vid:
                    storage.update_intent_verdict(vid, user_decision=decision_str)
        except Exception:
            log.debug("Failed to update verdict user_decision", exc_info=True)

    def approve_tools(self, items: list[dict[str, Any]]) -> tuple[bool, str | None]:
        """Two-phase approval gate for a batch of tool calls.

        Shared body for both interactive (:class:`turnstone.server.WebUI`)
        and coordinator (:class:`turnstone.console.coordinator_ui.ConsoleCoordinatorUI`)
        sessions. Order of resolution:

        1. Reset the per-round verdict cache so late LLM verdicts from
           the previous round can't leak onto this one.
        2. Evaluate admin-defined tool policies (deny short-circuits;
           allow tags items as auto-approved with ``AutoApproveReason.POLICY``).
        3. Per-tool auto-approve via ``self.auto_approve_tools`` (skill
           ``allowed_tools`` and operator "Approve + Always").
        4. Budget-override carve-out + blanket ``self.auto_approve``.
           Synthetic ``__budget_override__`` items always prompt.
        5. Activity tagging + ``_broadcast_activity`` so the dashboard
           reflects approval state.
        6. Heuristic verdict persistence (one row per ``_heuristic_verdict``
           item) + ``_record_judge_metric`` hook (subclass-overridden to
           feed the node's or console's Prometheus collector).
        7. Register an :class:`ApprovalCycle`, emit its ``approve_request``
           card, and block on the CYCLE's event up to
           ``_APPROVAL_WAIT_TIMEOUT``.

        ``__budget_override__`` is interactive-only today (coord
        workstreams don't have token budgets), but the carve-out check
        is cheap (``any(...)`` over pending) and is a no-op on coord;
        kept unconditional so a future coord-skill path picks it up
        for free.

        Reentrant by design: several gate threads (main loop + parallel
        task agents) run this body concurrently, each against its own
        :class:`ApprovalCycle`.  Shared state is touched only under
        ``_ws_lock`` and scoped to this batch's call_ids.
        """
        # The batch's judge generation, stamped by ``_evaluate_intent`` —
        # one event per spawn, shared by every item in the batch.  Read
        # before the purge so the purge can spare verdicts this very
        # generation already delivered.
        judge_event = next(
            (it.get("_judge_event") for it in items if it.get("_judge_event") is not None),
            None,
        )
        self._purge_round_verdicts(
            {it.get("call_id", "") for it in items if it.get("call_id")},
            keep_origin=judge_event,
        )

        # Early-paint the pending tool batch — BEFORE the tool-policy lookup,
        # the Smart Approvals verdict wait (``judge.smart_approvals`` parks here
        # for up to ``judge.timeout``), and the human gate — so the operator
        # sees what the model just committed to and can hit Stop in an emergency
        # the instant the call appears, not only once the judge has ruled.  This
        # is a UI paint ONLY: the authoritative ``tool_info`` / ``approve_request``
        # / ``tool_result`` events that follow carry the resolved state and
        # upgrade THIS SAME construct in place — both UIs key the card by
        # ``call_id`` and morph it rather than appending a duplicate — and on
        # reconnect the replayed ``Last-Event-ID`` slice reconstructs it
        # identically.  No persistence / audit / verdict bookkeeping happens
        # here; those stay on the resolved-state events so this can't perturb
        # the gate's accounting.  ``_heuristic_verdict`` is already attached
        # (``_evaluate_intent`` runs before the gate), so the card paints with
        # the heuristic verdict + a "judge analysing" cue from the first frame.
        if items:
            self._enqueue({"type": "tool_pending", "items": self._serialize_approval_items(items)})

        pending = [it for it in items if it.get("needs_approval") and not it.get("error")]

        # ``__budget_override__`` is a synthetic UI-only pseudo-tool injected
        # by ChatSession.send when a skill's token budget is exhausted; its
        # whole purpose is to force an operator prompt before the next turn
        # spends past the cap. Read from the pre-filter ``items`` list (not
        # ``pending``) so a wildcard ``*: allow`` policy or a stray entry in
        # ``auto_approve_tools`` cannot strip the override from ``pending``
        # before the carve-out gate at the auto-approve fall-through can see
        # it. Same intent gates the policy block above.
        has_budget_override = any(it.get("func_name") == "__budget_override__" for it in items)

        # -- Tool policy evaluation -----------------------------------------------
        # Check admin-defined tool policies before the auto_approve check.
        # ``__budget_override__`` is excluded from policy matching: it is a
        # synthetic UI-only pseudo-tool that exists specifically to force an
        # operator prompt when a skill's token budget is exhausted, so a
        # wildcard ``*: allow`` policy must never auto-approve it. Same
        # rationale gates the carve-out check below at line 470.
        if pending:
            try:
                from turnstone.core.policy import evaluate_tool_policies_batch
                from turnstone.core.storage._registry import get_storage

                storage = get_storage()
                if storage is not None:
                    tool_names = [
                        it.get("approval_label", "") or it.get("func_name", "")
                        for it in pending
                        if it.get("func_name") and it.get("func_name") != "__budget_override__"
                    ]
                    if tool_names:
                        verdicts = evaluate_tool_policies_batch(storage, tool_names)
                        still_pending = []
                        for it in pending:
                            policy_name = it.get("approval_label", "") or it.get("func_name", "")
                            # Synthetic budget-override item bypasses policy
                            # matching entirely — falls through to the carve-out
                            # gate so an operator always sees the prompt.
                            if it.get("func_name") == "__budget_override__":
                                still_pending.append(it)
                                continue
                            verdict = verdicts.get(policy_name)
                            if verdict == "deny":
                                it["denied"] = True
                                it["denial_msg"] = (
                                    f"Blocked by tool policy (pattern match for '{policy_name}')"
                                )
                            elif verdict == "allow":
                                # Admin-defined ``allow`` rule fires the
                                # auto-approve gate without any UI prompt.
                                # Tag for /dashboard visibility so the
                                # operator can see which calls bypassed
                                # the prompt and why.
                                it["needs_approval"] = False
                                self._tag_auto_approved([it], AutoApproveReason.POLICY)
                            else:
                                still_pending.append(it)
                        # If all were resolved by policy, check if any were denied
                        if not still_pending:
                            any_denied = any(it.get("denied") for it in items)
                            if any_denied:
                                # Record the policy-allowed siblings before
                                # the early return — the fall-through
                                # branch never runs on this path, so without
                                # this the policy bypass is invisible to
                                # /dashboard + audit.  ``_record_auto_approves``
                                # MUST run before ``_persist_auto_approved_*``
                                # so the call_id → reason lookup map is
                                # populated before the heuristic INSERTs go
                                # in: otherwise an LLM judge verdict firing
                                # in the gap lands with ``user_decision=
                                # "pending"`` and stays that way.
                                self._record_auto_approves(items)
                                self._persist_auto_approved_heuristic_verdicts(items)
                                self._enqueue(
                                    {
                                        "type": "tool_info",
                                        "items": self._serialize_approval_items(items),
                                    }
                                )
                                return False, "Blocked by tool policy"
                        pending = still_pending
            except Exception:
                log.debug("Tool policy evaluation failed", exc_info=True)
        # -- End tool policy evaluation -------------------------------------------

        # Per-tool auto-approve check (from workstream template or interactive "Always").
        # Suppressed when a budget-override item is present so the carve-out
        # at the next gate stays effective even if ``__budget_override__`` ever
        # lands in ``auto_approve_tools`` (defensive — listings filter it out
        # today, but the worker can be configured by a skill template).
        if pending and self.auto_approve_tools and not has_budget_override:
            pending_names = {
                it.get("approval_label", "") or it.get("func_name", "")
                for it in pending
                if it.get("func_name")
            }
            if pending_names and pending_names.issubset(self.auto_approve_tools):
                # Tag each formerly-pending item with the per-tool source
                # recorded when ``auto_approve_tools`` was populated:
                # ``skill`` (skill template's ``allowed_tools``) /
                # ``always`` (user "Approve + Always" click) / fallback
                # ``auto_approve_tools`` for legacy or unknown writers.
                # Visibility for the skill-vs-explicit conflation
                # flagged on the coord tree dashboard.
                self._tag_auto_approved(
                    pending,
                    AutoApproveReason.AUTO_APPROVE_TOOLS,
                    source_map=self._auto_approve_tools_source,
                )
                pending = []

        # Budget override requires explicit approval — never auto-approved by
        # blanket auto_approve (tool policies can still allow it explicitly,
        # but the policy block above carves out ``__budget_override__`` so
        # that path is unreachable too). ``has_budget_override`` was computed
        # from the pre-filter ``items`` list at the top of the function so a
        # policy/auto-approve pass that drained the override from ``pending``
        # cannot disarm this gate.
        blanket_active = self.auto_approve and not has_budget_override

        # -- Smart Approvals (judge.smart_approvals) -----------------------------
        # Last automatic gate before the human prompt, after the explicit
        # operator-configured ones (policy / "Always" / blanket): wait
        # briefly for the async LLM intent verdict and auto-approve every
        # still-pending call the judge cleared with a high-confidence
        # ``approve``.  Skipped under blanket auto-approve (everything is
        # approved already) and when a ``__budget_override__`` pseudo-tool
        # is present (it must always reach a human).
        if (
            pending
            and self.smart_approvals_enabled
            and not blanket_active
            and not has_budget_override
        ):
            pending = self._apply_smart_approvals(pending)

        if not pending or blanket_active:
            if blanket_active and pending:
                # Blanket flag drained the rest of pending — tag so the
                # dashboard can distinguish from
                # ``auto_approve_tools`` / ``policy``.  No need to
                # clear ``pending`` here: the function returns inside
                # this block without reading it again.
                self._tag_auto_approved(pending, AutoApproveReason.BLANKET)
            # Track auto-approved tool activity
            first = items[0] if items else {}
            label = first.get("func_name", "")
            preview = first.get("preview", "")[:80]
            with self._ws_lock:
                self._ws_current_activity = f"⚙ {label}: {preview}" if label else ""
                self._ws_activity_state = "tool" if label else ""
            self._broadcast_activity()
            # ``_record_auto_approves`` runs FIRST so the call_id → reason
            # lookup is populated before the heuristic INSERT can race
            # against a concurrent LLM judge verdict — see the matching
            # comment on the policy-deny branch above.
            self._record_auto_approves(items)
            self._persist_auto_approved_heuristic_verdicts(items)
            self._enqueue({"type": "tool_info", "items": self._serialize_approval_items(items)})
            return True, None

        # Track pending approval activity
        first_pending = pending[0]
        label = first_pending.get("func_name", "")
        preview = first_pending.get("preview", "")[:60]
        with self._ws_lock:
            self._ws_current_activity = f"⏳ Awaiting approval: {label} — {preview}"
            self._ws_activity_state = "approval"
        self._broadcast_activity()

        # Persist heuristic verdicts and track for user_decision update.
        # Build list locally, then assign under lock to avoid racing with
        # the judge daemon thread's on_intent_verdict() appends. Storage
        # write goes through the bulk path so a tool-heavy turn pays one
        # commit instead of N (was visible as time-to-render-prompt
        # latency for fan-out turns); the per-item Prometheus call stays
        # in the loop because it's a lock+increment, not a DB round-trip.
        #
        # ``user_decision`` is stamped per-verdict here so the row lands
        # with a meaningful value at insert: auto-approved items
        # (mixed-path case: policy allowed some, others still prompt)
        # carry their auto_approve_reason directly; items still pending
        # operator decision carry ``"pending"`` and get updated by
        # ``resolve_approval`` on close.  The cycle's ``pending_verdicts``
        # only tracks the latter — auto-approved verdicts are already
        # final.
        heuristic_verdicts: list[dict[str, Any]] = []
        pending_verdicts: list[dict[str, Any]] = []
        for item in items:
            hv = item.get("_heuristic_verdict")
            if not hv:
                continue
            if item.get("auto_approved"):
                hv["user_decision"] = item.get("auto_approve_reason", "") or "pending"
            else:
                hv["user_decision"] = "pending"
                pending_verdicts.append(hv)
            heuristic_verdicts.append(hv)
            # Subclass-overridden Prometheus surface: WebUI feeds
            # the per-node /metrics endpoint, ConsoleCoordinatorUI
            # feeds the console's /metrics endpoint via ConsoleMetrics.
            self._record_judge_metric(hv)
        self._persist_intent_verdicts_bulk(heuristic_verdicts, default_tier="heuristic")

        # Record any items the policy block already auto-approved
        # before falling through to the prompt — without this the
        # mixed-policy-then-prompt path leaves the policy bypass
        # invisible to /dashboard (the auto-approve fall-through never
        # runs since pending is non-empty + blanket inactive).
        # No-op when no items are auto-approve-tagged.
        self._record_auto_approves(items)

        # Send approval request and block.  ``judge_pending`` tells the UI
        # whether to expect LLM verdicts still in flight: true only when a
        # judged item does NOT yet have its LLM verdict cached.  Under Smart
        # Approvals the gate already waited for every verdict, so they are
        # present and this is false (no spurious "judge working" spinner /
        # poll); in the normal async flow they haven't arrived yet → true.
        #
        # The card carries a ``cycle_id`` so clients and HTTP resolvers
        # address THIS round among concurrent siblings (parallel task
        # agents run their own gates through this same body).
        cycle_id = uuid.uuid4().hex
        card: dict[str, Any] = {
            "type": "approve_request",
            "cycle_id": cycle_id,
            "items": self._serialize_approval_items(items),
        }
        cycle = ApprovalCycle(items, card, judge_event)
        with self._ws_lock:
            # Evict any cached verdict for this batch's call_ids that a
            # STALE judge generation delivered into the purge→register
            # window (delivery is concurrent with this gate; the
            # entry-time purge can't see arrivals that land during the
            # policy round-trip or the Smart-Approvals wait).  By
            # registration time these call_ids belong to THIS
            # generation — a wrong-generation entry would blank the
            # "judge analysing" cue below, be adopted into
            # ``pending_verdicts`` for decision-stamping, and replay
            # onto the new card on reconnect.  Once the cycle is
            # registered, ``on_intent_verdict``'s owner check keeps
            # such deliveries out on its own.
            if judge_event is not None:
                for cid in {it.get("call_id", "") for it in items if it.get("call_id")}:
                    if cid in self._llm_verdicts and self._verdict_origins.get(cid) != id(
                        judge_event
                    ):
                        del self._llm_verdicts[cid]
                        self._verdict_origins.pop(cid, None)
            judge_pending = any(
                it.get("_heuristic_verdict") and it.get("call_id", "") not in self._llm_verdicts
                for it in items
            )
            card["judge_pending"] = judge_pending
            # Park this round's verdicts on the cycle: the heuristic rows
            # just persisted as ``"pending"``, plus any LLM verdicts that
            # arrived EARLY (the Smart-Approvals wait runs before the
            # cycle exists, so ``on_intent_verdict`` cached them with no
            # cycle to park on).  ``resolve_approval`` stamps the final
            # ``user_decision`` on exactly this set.  Smart-approved
            # calls were pulled out + stamped by
            # ``_finalize_smart_verdicts`` already and their cached dicts
            # carry a non-"pending" decision, so the early sweep skips
            # them.
            pending_cids = {hv.get("call_id") for hv in pending_verdicts}
            early_llm = [
                v
                for cid, v in self._llm_verdicts.items()
                if cid in pending_cids and v.get("user_decision", "pending") == "pending"
            ]
            cycle.pending_verdicts = pending_verdicts + early_llm
            self._approval_cycles[cycle.cycle_id] = cycle
            self._refresh_pending_approval_view()
        self._enqueue(card)
        # Cross-stream broadcast — push the items via the cluster bus
        # so a coord parent's tree UI can render the inline approve/deny
        # block without waiting for a bulk fetch. Without this, the
        # bulk fetch races with this assignment: the state transition
        # to ATTENTION fires upstream BEFORE approve_tools runs (see
        # session.py:_emit_state("attention") preceding ui.approve_tools),
        # so a bulk fetch landing in the ~50-200ms window between
        # _emit_state and this point sees no live cycle and returns
        # ``pending_approval_detail: null``. The 5s TTL then locks the
        # coord row on a "loading" placeholder until the next state
        # event triggers a refresh — which never comes while parked on
        # the cycle's event.wait. The push path eliminates the race.
        self._broadcast_approve_request(card)
        # Smart Approvals waited for the LLM verdicts BEFORE this card was
        # built, so on_intent_verdict already fanned out their
        # ``intent_verdict`` events while no card existed — a live client
        # dropped them and the chip would stay on the heuristic value until
        # a reload re-merged the cache.  Re-emit them now, after the card,
        # to restore the normal approve_request → intent_verdict ordering so
        # the live chip updates.  No-op in the normal async flow (cache is
        # empty here) and when the feature is off.
        if self.smart_approvals_enabled:
            self._replay_pending_verdicts(items)
        try:
            if not cycle.event.wait(timeout=self._APPROVAL_WAIT_TIMEOUT):
                # Approval timed out (e.g., user disconnected). Deny via
                # resolve_approval so verdicts and state are updated
                # consistently — targeted at THIS cycle so a sibling gate
                # timing out can't deny someone else's round.  Feedback
                # string derives from ``_APPROVAL_WAIT_TIMEOUT`` so the
                # text follows the constant if the timeout knob moves.
                log.warning("Approval timed out for ws_id=%s", self.ws_id)
                self.resolve_approval(
                    False,
                    f"Approval timed out after {self._APPROVAL_WAIT_TIMEOUT}s",
                    timeout=True,
                    cycle_id=cycle.cycle_id,
                )
        finally:
            # Gate thread owns the cycle's lifecycle end-to-end; the
            # resolver only flips its state.  Unregister even if the
            # wait raises so a dead gate can't strand a zombie card.
            self._unregister_approval_cycle(cycle)
        approved, feedback = cycle.result

        if not approved:
            denial_msg = "Denied by user"
            if feedback:
                denial_msg += f": {feedback}"
            for item in pending:
                item["denied"] = True
                item["denial_msg"] = denial_msg

        return approved, feedback

    # ------------------------------------------------------------------
    # Smart Approvals (judge.smart_approvals)
    # ------------------------------------------------------------------

    def _apply_smart_approvals(self, pending: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Auto-approve a tool batch the LLM judge cleared confidently.

        **Batch-atomic.**  Waits (bounded by ``smart_approval_wait_seconds``)
        for the async LLM verdicts of EVERY pending call, then auto-approves
        the whole batch only if EVERY call qualifies; a single non-qualifying
        call sends the entire batch to a human.  Parallel tool calls are one
        unit of intent — approving the safe-looking members while a sibling
        is held would let a multi-step action through piecemeal — so it is
        all-or-nothing.

        A call qualifies when its LLM verdict is a *completed* ``approve`` —
        tier ``"llm"`` (NOT the ``"llm_fallback"`` error tier) at confidence
        ≥ ``smart_approval_threshold`` — AND the deterministic heuristic did
        not *explicitly* flag it ``deny`` / ``critical``.  That heuristic
        floor only blocks the explicit danger verdicts: the heuristic
        DEFAULT for an unmatched tool is ``review``, and upgrading ``review``
        → ``approve`` on a confident LLM verdict is exactly this feature's
        job, so ``review`` is not a floor.  But a call the pattern rules
        matched as ``deny`` / ``critical`` (e.g. ``rm -rf /``) is never
        cleared by the (promptable) LLM — those always reach a human.

        Returns ``[]`` when the whole batch is auto-approved, or *pending*
        unchanged when anything is uncertain (review/deny/low-confidence/
        error/timeout/heuristic-danger/no-verdict) — fails closed.
        """
        # Only calls the judge actually evaluated carry a heuristic verdict;
        # the ``__budget_override__`` pseudo-tool is never smart-approved, so
        # its presence makes ``candidates`` smaller than ``pending`` and the
        # batch-completeness check below holds the whole batch for a human.
        candidates = [
            it
            for it in pending
            if it.get("_heuristic_verdict") and it.get("func_name") != "__budget_override__"
        ]
        needed = {it.get("call_id", "") for it in candidates if it.get("call_id")}
        if not needed:
            return pending
        # The whole batch must be eligible before we pay the verdict wait:
        #   - every pending call must be a judged candidate — an unjudged
        #     sibling or the ``__budget_override__`` pseudo-tool makes
        #     candidates < pending, AND
        #   - call_ids must be unique — some local models emit duplicate
        #     non-empty tool-call ids (``_ensure_tool_call_ids`` only fills
        #     MISSING ones), which collapse in the ``needed`` set and would let
        #     one verdict clear two distinct calls (their args differ).
        # Either mismatch → hold the whole batch for a human.  Checked
        # pre-wait so an ineligible batch never pays the (up-to-timeout) wait.
        if len(candidates) != len(pending) or len(needed) != len(candidates):
            return pending
        # The per-round verdict cache is FIFO-capped; a batch with more calls
        # than the cap can't hold every verdict at once, so the wait could
        # never see them all and would stall to its full budget.  Hold such
        # (pathological) batches for a human.
        if len(needed) > self._LLM_VERDICT_CACHE_MAX:
            log.info("judge.smart_approval.batch_too_large", ws_id=self.ws_id, count=len(needed))
            return pending
        self._await_llm_verdicts(needed, self.smart_approval_wait_seconds)

        threshold = self.smart_approval_threshold
        # This batch's judge generation — every cached verdict must have
        # been delivered by THIS spawn's daemon.  A verdict of a stale
        # generation (prior turn's run-to-completion daemon + a provider
        # that reuses call_ids) fails qualification and the batch goes to
        # a human.  ``None`` when the caller never ran the judge (legacy
        # tests) — origin check skipped, matching the old behavior.
        expected_gen = next(
            (it.get("_judge_event") for it in candidates if it.get("_judge_event") is not None),
            None,
        )
        qualified: dict[str, dict[str, Any]] = {}
        with self._ws_lock:
            for it in candidates:
                cid = it.get("call_id", "")
                v = self._llm_verdicts.get(cid)
                # Require a COMPLETED LLM verdict — tier "llm", not the
                # "llm_fallback" error carry-over ("heuristic" never lands in
                # this cache) — recommending "approve" at/above threshold,
                # delivered by this batch's own judge generation.
                if (
                    v is None
                    or v.get("tier") != "llm"
                    or v.get("recommendation") != "approve"
                    or self._verdict_confidence(v) < threshold
                    or (
                        expected_gen is not None
                        and self._verdict_origins.get(cid) != id(expected_gen)
                    )
                ):
                    return pending  # one fails → none of the batch auto-approves
                hv = it.get("_heuristic_verdict") or {}
                if hv.get("recommendation") == "deny" or hv.get("risk_level") == "critical":
                    return pending  # explicit deterministic danger flag → human
                qualified[cid] = v

        # Whole batch qualified.  Clear the gate flag (mirrors the policy
        # ``allow`` branch) so each call is treated as resolved: the coord
        # pill renders (``auto_approved && !needs_approval``) and the denial
        # sweep in ChatSession._execute_tools leaves them to execute.  Attach
        # the driving LLM verdict so the auto-approved tool row renders it
        # (llm tier, approve) instead of the cautious heuristic carry-over,
        # which would read contradictorily beside the SMART_APPROVAL pill.
        for it in candidates:
            it["needs_approval"] = False
            it["_llm_verdict"] = qualified.get(it.get("call_id", ""))
        self._tag_auto_approved(candidates, AutoApproveReason.SMART_APPROVAL)
        self._finalize_smart_verdicts(needed)
        log.info("judge.smart_approval", ws_id=self.ws_id, approved=len(candidates))
        return []

    @staticmethod
    def _verdict_confidence(verdict: dict[str, Any]) -> float:
        """Verdict confidence clamped to ``[0.0, 1.0]``; ``0.0`` if malformed.

        Rejects non-finite values explicitly: ``min``/``max`` would let a NaN
        through as ``1.0`` (every comparison with NaN is False), and
        ``json.loads`` accepts ``NaN`` by default — so a verdict reporting
        ``"confidence": NaN`` could otherwise clear the auto-approve bar.
        """
        try:
            confidence = float(verdict.get("confidence", 0.0))
        except (TypeError, ValueError):
            return 0.0
        if not math.isfinite(confidence):
            return 0.0
        return max(0.0, min(1.0, confidence))

    def _await_llm_verdicts(self, needed: set[str], budget_seconds: float) -> None:
        """Block until every call_id in *needed* has an LLM verdict cached.

        Returns the instant the last verdict lands; otherwise gives up
        after *budget_seconds* and leaves the missing calls for the human
        gate (fail-closed).  ``on_intent_verdict`` notifies
        ``_verdict_cond`` on every cache write, and the judge delivers
        exactly one verdict (LLM or ``llm_fallback``) per call, so the
        common case is an early return at real judge latency rather than a
        full-budget wait.
        """
        if budget_seconds <= 0 or not needed:
            return
        deadline = time.monotonic() + budget_seconds
        with self._verdict_cond:
            while not needed.issubset(self._llm_verdicts.keys()):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                self._verdict_cond.wait(timeout=remaining)

    def _finalize_smart_verdicts(self, smart_ids: set[str]) -> None:
        """Stamp ``smart_approval`` on the LLM verdicts of auto-approved calls.

        The verdicts arrived during ``_await_llm_verdicts`` — before any
        cycle existed for this round (the gate registers its
        :class:`ApprovalCycle` only after the wait, and a fully
        smart-approved batch never registers one at all) — so they live
        only in the ``_llm_verdicts`` cache.  Stamp the cached dict in
        place (the reconnect-replay payload reflects the final decision,
        and the non-"pending" ``user_decision`` keeps every later parking
        sweep away from it) and UPDATE the persisted rows.  The matching
        heuristic verdict is stamped by ``approve_tools``'s own
        persistence path via the ``auto_approved`` tag set just before
        this call.
        """
        stamped: list[dict[str, Any]] = []
        with self._ws_lock:
            for cid in smart_ids:
                v = self._llm_verdicts.get(cid)
                if v is not None:
                    v["user_decision"] = AutoApproveReason.SMART_APPROVAL
                    stamped.append(v)
        if stamped:
            self._persist_verdict_decisions(stamped, AutoApproveReason.SMART_APPROVAL)

    def _replay_pending_verdicts(self, items: list[dict[str, Any]]) -> None:
        """Re-emit already-cached LLM verdicts for the human-pending calls.

        Mirrors the reconnect-replay re-injection: after the
        ``approve_request`` card exists, re-send each pending call's cached
        ``intent_verdict`` so a live client (which dropped the events the
        Smart Approvals wait fanned out before the card) applies them to the
        chip.  Snapshots under the lock, fans out without it.
        """
        cids = {it.get("call_id", "") for it in items if it.get("call_id")}
        with self._ws_lock:
            verdicts = [dict(self._llm_verdicts[c]) for c in cids if c in self._llm_verdicts]
        for verdict in verdicts:
            self._enqueue({"type": "intent_verdict", **verdict})
            self._broadcast_intent_verdict(verdict)

    # ------------------------------------------------------------------
    # Intent-judge + output-guard plumbing
    # ------------------------------------------------------------------

    # Hard cap on the in-memory verdict cache so a long-running session
    # can't grow unbounded. FIFO eviction on insert.
    _LLM_VERDICT_CACHE_MAX = 50

    # Hard cap on how long a worker thread blocks waiting for an
    # approval decision. Subclasses' ``approve_tools`` references this
    # rather than the literal so a future
    # ``settings.approval_timeout_seconds`` knob can swap it in one
    # place.
    _APPROVAL_WAIT_TIMEOUT = 3600

    def on_intent_verdict(
        self,
        verdict: dict[str, Any],
        judge_event: object | None = None,
    ) -> None:
        """Deliver an LLM intent-judge verdict to the frontend + persist.

        Caches under ``_ws_lock`` for SSE reconnect replay, persists
        the row to storage, and either records the caller's
        ``user_decision`` immediately (if the approval already
        resolved) or parks the verdict on its owning
        :class:`ApprovalCycle` for ``resolve_approval`` to stamp.

        ``judge_event`` is the delivering daemon's cancel event —
        its identity names the judge GENERATION.  When the verdict's
        call_id belongs to a live cycle evaluated by a DIFFERENT
        generation (a provider that reuses call_ids across turns +
        a prior turn's run-to-completion daemon still delivering),
        the verdict is persisted for audit only: no cache write, no
        cond notify, no park — a stale ``approve`` must never satisfy
        the new round's Smart-Approvals wait.  ``None`` (legacy/test
        callers) skips the generation check.

        When the verdict arrives for a call_id that ``approve_tools``
        already auto-approved (the LLM judge is async and can fire
        seconds after the auto-approve path returned), stamp the
        ``auto_approve_reason`` onto the verdict before persist so
        the row lands with a meaningful ``user_decision`` instead of
        the default ``"pending"`` (which would never be updated for
        this code path).
        """
        call_id = verdict.get("call_id", "")
        auto_reason = ""
        decision = ""
        if call_id:
            with self._ws_lock:
                owner = next(
                    (c for c in self._approval_cycles.values() if call_id in c.call_ids),
                    None,
                )
                if (
                    owner is not None
                    and judge_event is not None
                    and owner.judge_event is not None
                    and owner.judge_event is not judge_event
                ):
                    # Stale generation aimed at a LIVE cycle — the one
                    # collision that could smart-approve the wrong call.
                    stale = dict(verdict)
                    stale.setdefault("user_decision", "superseded")
                    self._persist_intent_verdict(stale)
                    return
                if (
                    len(self._llm_verdicts) >= self._LLM_VERDICT_CACHE_MAX
                    and call_id not in self._llm_verdicts
                ):
                    oldest_key = next(iter(self._llm_verdicts))
                    del self._llm_verdicts[oldest_key]
                    self._verdict_origins.pop(oldest_key, None)
                self._llm_verdicts[call_id] = verdict
                self._verdict_origins[call_id] = id(judge_event)
                # Wake any Smart Approvals wait parked on this call's
                # verdict (``_verdict_cond`` shares ``_ws_lock``, so the
                # notify is valid here and the waiter re-checks its
                # call-id set on wake).
                self._verdict_cond.notify_all()
                # Pop (not get) — once consumed the entry isn't useful
                # again; TTL pruning at the writer side keeps the
                # never-consumed case bounded too.
                entry = self._auto_approve_reasons.pop(call_id, None)
                if entry is not None:
                    auto_reason = entry[0]
            if auto_reason:
                verdict["user_decision"] = auto_reason
        self._enqueue({"type": "intent_verdict", **verdict})
        # Kind-specific cross-stream broadcast — ConsoleCoordinatorUI
        # overrides to push onto the cluster bus so a coord parent's
        # tree UI sees the verdict without polling. Default is no-op
        # (the per-ws ``_enqueue`` above already covers WebUI's own
        # SSE listeners). Stage 3 Step 4.
        self._broadcast_intent_verdict(verdict)
        self._persist_intent_verdict(verdict)
        # If ``auto_reason`` was stamped above, the verdict already
        # carries the final ``user_decision`` for this row.  Neither
        # path below applies: parking on a cycle would cause
        # ``resolve_approval`` (on the manual-approval sibling in a
        # mixed batch) to overwrite the auto-reason with
        # ``"approved"``/``"denied"``/``"timeout"``; the
        # ``_persist_verdict_decisions`` immediate-stamp path would
        # overwrite it the same way from the round's decision.
        # Skip both so the audit trail keeps the auto-approve reason.
        if auto_reason:
            return
        with self._ws_lock:
            # Park-or-stamp under ONE lock acquisition so
            # ``resolve_approval`` can't interleave: it marks the cycle
            # resolved AND records the per-call decision atomically
            # under this same lock, so exactly one of the two branches
            # below sees the verdict — parked pre-decision (the resolve
            # stamps it from ``pending_verdicts``) or stamped
            # post-decision (from ``_recent_decisions``).  A two-step
            # check-then-park reintroduces the audit-corruption race
            # the old single-slot code fixed.
            #
            # Skip both when the verdict already carries a final
            # ``user_decision`` — Smart Approvals'
            # ``_finalize_smart_verdicts`` runs on the worker thread
            # the moment ``_await_llm_verdicts`` wakes (this method's
            # ``notify_all`` above) and can stamp ``smart_approval``
            # on this same cached dict before we get here; re-parking
            # it would let a later resolution overwrite the audit row.
            # No live owner and no decision (pre-cycle Smart-Approvals
            # arrival) → cache-only: the gate's registration sweep
            # picks pending verdicts up from ``_llm_verdicts`` when
            # the cycle is created.
            if verdict.get("user_decision", "pending") == "pending":
                owner = next(
                    (
                        c
                        for c in self._approval_cycles.values()
                        if call_id in c.call_ids and not c.resolved
                    ),
                    None,
                )
                if owner is not None:
                    owner.pending_verdicts.append(verdict)
                else:
                    recent = self._recent_decisions.get(call_id)
                    if recent is not None:
                        prior_decision, origin = recent
                        if (
                            judge_event is not None
                            and origin is not None
                            and origin is not judge_event
                        ):
                            # The recorded decision belongs to a
                            # DIFFERENT generation's round under a
                            # reused call_id — the round this verdict
                            # was judging resolved and fell out of
                            # ``_recent_decisions`` before delivery.
                            # Stamping it with the other round's
                            # decision would claim the operator ruled
                            # on THIS verdict; ``superseded`` is the
                            # honest terminal state (same vocabulary
                            # as :meth:`on_superseded_intent_verdict`).
                            decision = "superseded"
                        else:
                            decision = prior_decision
        if decision:
            self._persist_verdict_decisions([verdict], decision)

    def on_superseded_intent_verdict(self, verdict: dict[str, Any]) -> None:
        """Persist (audit-only) a verdict whose judge generation was superseded.

        ``ChatSession._on_verdict`` routes here instead of
        :meth:`on_intent_verdict` when a newer turn has replaced the
        daemon's generation.  The live surfaces deliberately stay
        untouched — no ``_llm_verdicts`` cache write, no SSE enqueue,
        no ``_verdict_cond`` notify, no ``_pending_verdicts`` park —
        because the verdict's call_id belongs to an already-resolved
        batch, and a model that reuses call_ids across turns could
        ride a stale cached ``approve`` into a wrongful Smart Approval
        of a *different* call.  Dropping the verdict entirely (the
        previous behavior) kept that safety property but left a
        permanent hole in ``intent_verdicts``: the audit table said
        "the judge never answered" for calls it actually ruled on.

        ``user_decision`` is stamped ``"superseded"`` — no decision was
        ever taken on THIS verdict; its call's gate resolved before the
        judge finished.  The stamp only lands on fresh ``tier="llm"``
        rows: a superseded *fallback* reuses its heuristic row's
        verdict_id, and ``upsert_intent_verdict`` excludes
        ``user_decision`` from the on-conflict SET, so the decision
        already recorded on that row survives the tier upgrade.
        """
        row = dict(verdict)
        row.setdefault("user_decision", "superseded")
        self._persist_intent_verdict(row)

    def _record_judge_metric(self, verdict: dict[str, Any]) -> None:
        """Extension point for transport-specific Prometheus metrics.

        ``approve_tools`` calls this for each persisted heuristic
        verdict. Subclasses override to fan the verdict into their
        own metrics collector:

        - ``WebUI`` writes to the per-node ``MetricsCollector`` so the
          node's /metrics endpoint surfaces ``turnstone_judge_verdicts_total``.
        - ``ConsoleCoordinatorUI`` writes to ``ConsoleMetrics`` so the
          console's /metrics endpoint surfaces the same metric name —
          a cluster-wide PromQL query rolls coord and interactive
          verdicts up uniformly.

        Default no-op covers test fixtures and any future SessionUI
        impl that doesn't expose a /metrics surface. Mirrors the
        pattern used for ``_broadcast_state`` / ``_broadcast_activity``.
        """
        del verdict  # default impl: no metrics surface

    def _persist_intent_verdicts_bulk(
        self,
        verdicts: list[dict[str, Any]],
        *,
        default_tier: str = "heuristic",
    ) -> None:
        """Bulk-insert a list of intent-judge verdicts in one transaction.

        Used by ``approve_tools`` so the per-turn heuristic-verdict
        persistence doesn't block on N×commit before the approval UI
        enqueues. Each verdict dict mirrors the keyword args of
        :meth:`_persist_intent_verdict`; ``ws_id`` is stamped from
        ``self.ws_id`` and ``evidence`` is JSON-encoded so the row
        shape matches the per-row path. Storage failure is best-effort
        (logged at debug) — the verdict cache and UI dispatch run
        independently of the DB write.
        """
        if not verdicts:
            return
        try:
            from turnstone.core.storage._registry import get_storage

            storage = get_storage()
            if storage is None:
                return
            rows = [
                {
                    "verdict_id": v.get("verdict_id", ""),
                    "ws_id": self.ws_id,
                    "call_id": v.get("call_id", ""),
                    "func_name": v.get("func_name", ""),
                    "func_args": v.get("func_args", ""),
                    "intent_summary": v.get("intent_summary", ""),
                    "risk_level": v.get("risk_level", "medium"),
                    "confidence": v.get("confidence", 0.5),
                    "recommendation": v.get("recommendation", "review"),
                    "reasoning": v.get("reasoning", ""),
                    "evidence": json.dumps(v.get("evidence", [])),
                    "tier": v.get("tier", default_tier),
                    "judge_model": v.get("judge_model", ""),
                    "latency_ms": v.get("latency_ms", 0),
                    "user_decision": v.get("user_decision", "pending"),
                }
                for v in verdicts
            ]
            # The daemon-judge race where a verdict lands BEFORE this
            # bulk write IS reachable: ``_evaluate_intent`` (session.py)
            # spawns the daemon thread before ``approve_tools`` is
            # called, and the daemon's first emission (heuristic-only
            # short batch, fast LLM response, or cancel-event
            # ``_deliver_fallbacks`` from judge.py) can fire
            # ``_persist_intent_verdict`` first — a fallback UPSERT
            # plants the very ``verdict_id`` this batch is about to
            # INSERT.  The bulk site inserts ``ON CONFLICT DO NOTHING``
            # so that one collision skips only its own row: the rest of
            # the batch still lands, and the colliding row keeps the
            # daemon's ``llm_fallback`` tier upgrade instead of being
            # regressed to the heuristic stamp.  (Plain INSERT here used
            # to abort the entire statement — and the ``try/except``
            # below swallowed it — discarding the whole batch's
            # heuristic rows.)
            storage.create_intent_verdicts_bulk(rows)
        except Exception:
            log.debug("Failed to bulk-persist intent verdicts", exc_info=True)

    def _persist_intent_verdict(
        self,
        verdict: dict[str, Any],
        *,
        default_tier: str = "llm",
    ) -> None:
        """Persist an intent-judge verdict row via UPSERT.

        Used by the async LLM-tier path (``on_intent_verdict``,
        default tier ``"llm"``).  Routes through ``upsert_intent_verdict``
        because ``tier="llm_fallback"`` verdicts deliberately reuse the
        heuristic verdict's ``verdict_id`` (see ``judge.py`` —
        ``_deliver_fallbacks`` and the in-loop fallback path)
        so the row gets "upgraded in place" from heuristic →
        llm_fallback.  A plain INSERT would collide on the PK and the
        upgrade would be lost to a silently-swallowed exception.
        ``default_tier`` only matters when the verdict dict doesn't
        already carry a ``tier`` key — both real producers always set
        it, but the fallback is the right call-site label so a
        malformed verdict still lands on the correct row
        classification.
        """
        try:
            from turnstone.core.storage._registry import get_storage

            storage = get_storage()
            if storage is None:
                return
            storage.upsert_intent_verdict(
                verdict_id=verdict.get("verdict_id", ""),
                ws_id=self.ws_id,
                call_id=verdict.get("call_id", ""),
                func_name=verdict.get("func_name", ""),
                func_args=verdict.get("func_args", ""),
                intent_summary=verdict.get("intent_summary", ""),
                risk_level=verdict.get("risk_level", "medium"),
                confidence=verdict.get("confidence", 0.5),
                recommendation=verdict.get("recommendation", "review"),
                reasoning=verdict.get("reasoning", ""),
                evidence=json.dumps(verdict.get("evidence", [])),
                tier=verdict.get("tier", default_tier),
                judge_model=verdict.get("judge_model", ""),
                latency_ms=verdict.get("latency_ms", 0),
                user_decision=verdict.get("user_decision", "pending"),
            )
        except Exception:
            log.debug("Failed to persist intent verdict", exc_info=True)

    def serialize_pending_approval_details(self) -> list[dict[str, Any]]:
        """Per-cycle inline-approval payloads, EVERY live cycle, oldest first.

        Merges each cycle's card items with the per-call_id LLM verdict
        cache so a coordinator's children-tree row can render inline
        approve/deny buttons + judge-verdict pill without a separate
        per-child round-trip; one entry per live cycle (parallel task
        agents each gate their own batch), each addressable by its
        ``cycle_id``.  Empty when nothing is pending.

        The per-entry shape mirrors the ``approve_request`` SSE event
        but adds a ``judge_verdict`` sibling per item (looked up from
        :attr:`_llm_verdicts`, the cache seeded by
        :meth:`on_intent_verdict`). Heuristic verdicts already carried
        on items are forwarded as ``heuristic_verdict``.

        Cross-tenant exposure note: this method is read by
        ``GET /v1/api/dashboard`` (read scope, cross-tenant under the
        trusted-team posture documented at ``server.py:1119-1120``).
        Judge ``reasoning`` / ``evidence`` / ``func_args`` therefore
        cross the same boundary as ``activity`` and ``tokens`` already
        do — intentional for inline coord approval, not a leak. If
        the deployment posture ever drops the trusted-team
        assumption, project the verdict to a safe subset before
        embedding (or move the field behind ``admin.cluster.inspect``).
        """
        with self._ws_lock:
            cards = [(c.cycle_id, c.card) for c in self._approval_cycles.values()]
            # Snapshot verdict references under lock, deepcopy after
            # release. Writers (``on_intent_verdict`` daemon judge
            # thread) only ASSIGN entries, never mutate them in place —
            # so a snapped reference is stable after the lock drops, and
            # the subsequent deepcopy can run lock-free even though
            # verdict dicts carry list-typed ``evidence`` that
            # downstream callers might mutate. This keeps the lock
            # window O(N items) without paying the deepcopy cost
            # (O(verdict size)) under contention with the per-token
            # write path that also holds ``_ws_lock``.
            verdict_refs = dict(self._llm_verdicts)
        verdicts: dict[str, dict[str, Any]] = {}
        details: list[dict[str, Any]] = []
        for cycle_id, card in cards:
            items = card.get("items") or []
            if not items:
                continue
            call_ids = [item.get("call_id", "") for item in items]
            serialized: list[dict[str, Any]] = []
            for item in items:
                cid = item.get("call_id", "")
                if cid in verdict_refs and cid not in verdicts:
                    verdicts[cid] = copy.deepcopy(verdict_refs[cid])
                serialized.append(
                    {
                        "call_id": cid,
                        "header": item.get("header", ""),
                        "preview": item.get("preview", ""),
                        "func_name": item.get("func_name", ""),
                        "approval_label": item.get("approval_label", ""),
                        "needs_approval": item.get("needs_approval", False),
                        "error": item.get("error"),
                        # Card items are already wire-shaped by
                        # ``_serialize_approval_items`` — the heuristic
                        # verdict rides under ``heuristic_verdict``.
                        "heuristic_verdict": item.get("heuristic_verdict"),
                        "judge_verdict": verdicts.get(cid),
                    }
                )
            # Primary call_id = first non-empty in list order (matches the
            # 409 response shape from ``make_approve_handler`` so the UI
            # can render the same identifier the server thinks is current).
            primary = next((cid for cid in call_ids if cid), "")
            details.append(
                {
                    "cycle_id": cycle_id,
                    "call_id": primary,
                    "judge_pending": bool(card.get("judge_pending", False)),
                    "items": serialized,
                }
            )
        return details

    # ---------------------------------------------------------------
    # Auto-approve visibility — shared by interactive + coord UIs
    # ---------------------------------------------------------------
    #
    # When a tool call gets approved without an explicit operator
    # click (admin tool policy, skill ``allowed_tools`` allowlist,
    # blanket ``auto_approve``, or "Approve + Always" memory) the
    # coord-tree dashboard would otherwise have no surface to show
    # WHICH tools bypassed the gate or WHY.  These helpers feed two
    # sinks: per-item annotations on the ``tool_info`` SSE event
    # (live operator visibility) and a short ring buffer surfaced
    # via ``/dashboard`` (post-hoc visibility for the coord tree).

    # Cap on the per-ws ring buffer.  Sized for "the operator opens
    # the row in the next minute" — older entries roll off so the
    # /dashboard payload stays bounded on long-running skill
    # workstreams that auto-approve dozens of tool calls per turn.
    _RECENT_AUTO_APPROVALS_MAX = 10

    # TTL on the call_id → auto_approve_reason map.  Sized to comfortably
    # cover the LLM judge's worst-case latency (cold start + a slow model
    # + queue depth).  Pruning happens lazily at write time so the cost
    # is paid only on the next auto-approve event; a session that goes
    # quiet after auto-approving never pays the prune cost at all but
    # the resident-set is also tiny.
    _AUTO_APPROVE_REASON_TTL = 60.0

    @staticmethod
    def _serialize_approval_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Project each item to the wire shape the SSE event payload uses.

        Single source of truth for both ``approve_request`` and
        ``tool_info`` payloads — pre-fix the WebUI rebuilt this
        twice (once eagerly, once after policy evaluation), and
        adding new fields meant editing both copies in lockstep.
        Forwards ``auto_approved`` / ``auto_approve_reason`` when
        the upstream pipeline tagged the item, so the operator can
        see which path silently approved each call.

        Heuristic verdict surfaces under ``heuristic_verdict`` for
        consistency with :meth:`serialize_pending_approval_details`
        and :class:`api.server_schemas.PendingApprovalItem` — pre-
        fix this method emitted ``verdict`` while the dashboard
        payload used ``heuristic_verdict``, leaving JS consumers
        with two code paths for the same field.
        """
        out: list[dict[str, Any]] = []
        for it in items:
            denied = it.get("denied")
            entry: dict[str, Any] = {
                "call_id": it.get("call_id", ""),
                "header": it.get("header", ""),
                "preview": it.get("preview", ""),
                "func_name": it.get("func_name", ""),
                "approval_label": it.get("approval_label", it.get("func_name", "")),
                "needs_approval": it.get("needs_approval", False),
                "error": it.get("denial_msg") if denied else it.get("error"),
            }
            if "_heuristic_verdict" in it:
                entry["heuristic_verdict"] = it["_heuristic_verdict"]
            if it.get("_llm_verdict"):
                # The completed LLM verdict that drove a Smart Approval.
                # Sent so the auto-approved tool row renders the llm-tier
                # approve/risk that actually cleared the call, instead of the
                # cautious heuristic carry-over — which reads contradictorily
                # next to the SMART_APPROVAL pill (e.g. "review/medium" + ✓).
                entry["judge_verdict"] = it["_llm_verdict"]
            if it.get("auto_approved"):
                entry["auto_approved"] = True
                entry["auto_approve_reason"] = it.get("auto_approve_reason", "")
            out.append(entry)
        return out

    def _tag_auto_approved(
        self,
        pending: list[dict[str, Any]],
        reason: str,
        *,
        source_map: dict[str, str] | None = None,
    ) -> None:
        """Mark each pending item as auto-approved with the given reason.

        Used at the three branch points in ``approve_tools`` (policy
        ``allow`` / ``auto_approve_tools.issubset`` / blanket
        ``auto_approve``) so both :class:`turnstone.server.WebUI` and
        :class:`turnstone.console.coordinator_ui.ConsoleCoordinatorUI`
        share the loop body — pre-fix the per-branch tag loops were
        copy-pasted line-for-line across the two subclasses.

        ``source_map`` is the per-tool source dict
        (``_auto_approve_tools_source``) populated by the skill
        template / "Approve + Always" writers.  When provided, each
        item's reason is looked up by ``approval_label_or_func_name``
        and falls back to ``reason`` when the entry is missing
        (legacy / unknown writer).  Pass ``None`` for the policy /
        blanket branches that don't carry per-tool source.
        """
        for it in pending:
            it["auto_approved"] = True
            if source_map is not None:
                name = it.get("approval_label", "") or it.get("func_name", "")
                it["auto_approve_reason"] = source_map.get(name, reason)
            else:
                it["auto_approve_reason"] = reason

    def _persist_auto_approved_heuristic_verdicts(self, items: list[dict[str, Any]]) -> None:
        """Persist heuristic verdicts for items the auto-approve path resolved.

        The manual-approval block at the bottom of ``approve_tools``
        handles its own verdict persistence (and stamps
        ``user_decision`` per item — auto-approved items in a
        mixed-path batch carry their reason, pending items carry
        ``"pending"``).  The auto-approve early-return branches
        (policy-allow-with-deny, blanket flag, auto_approve_tools
        match) used to drop heuristic verdicts on the floor — an
        operator querying ``user_decision`` for the auto-approve
        reason would find no row at all, conflating "we auto-approved
        silently" with "the judge didn't run".  This helper closes
        that gap: walk ``items``, persist each auto-approved verdict
        with its reason stamped, and fan a metric row per verdict.
        Safe to call with empty items.
        """
        if not items:
            return
        verdicts: list[dict[str, Any]] = []
        for it in items:
            if not it.get("auto_approved"):
                continue
            hv = it.get("_heuristic_verdict")
            if not hv:
                continue
            hv["user_decision"] = it.get("auto_approve_reason", "") or "pending"
            verdicts.append(hv)
            self._record_judge_metric(hv)
        if verdicts:
            self._persist_intent_verdicts_bulk(verdicts, default_tier="heuristic")

    def _record_auto_approves(self, items: list[dict[str, Any]]) -> None:
        """Append auto-approved items to the per-ws ring buffer + audit log.

        Called from ``approve_tools`` immediately before the
        ``tool_info`` emit so the /dashboard payload reflects the
        same set of tools the SSE consumer just saw.  No-op when
        ``items`` is empty (every item was a "no approval needed"
        read-only tool, not an auto-approve).

        Also writes one ``tool.auto_approved`` audit row per call so
        operators have a durable forensic record beyond the SSE
        stream + ring buffer (both ephemeral).  Audit failure is
        logged but never raised — visibility is best-effort and
        must not break the tool-execution path.
        """
        if not items:
            return
        ts = time.time()
        appended = [
            {
                "call_id": it.get("call_id", ""),
                "func_name": it.get("func_name", ""),
                "approval_label": it.get("approval_label", "") or it.get("func_name", ""),
                "auto_approve_reason": it.get("auto_approve_reason", ""),
                "ts": ts,
            }
            for it in items
            if it.get("auto_approved")
        ]
        if not appended:
            return
        with self._ws_lock:
            self._recent_auto_approvals.extend(appended)
            overflow = len(self._recent_auto_approvals) - self._RECENT_AUTO_APPROVALS_MAX
            if overflow > 0:
                self._recent_auto_approvals = self._recent_auto_approvals[overflow:]
            # Mirror call_id → reason into the lookup map so a late
            # ``on_intent_verdict`` (LLM judge tier) can stamp the
            # right ``user_decision`` instead of leaving the verdict
            # stuck as ``"pending"`` forever.  Prune expired entries
            # first (lazy TTL eviction) so a session with the LLM
            # judge disabled doesn't accumulate entries that will
            # never be consumed.  Skip the rebuild when the map is
            # empty or all entries are still fresh — the common case
            # on a healthy LLM-judge-enabled session where entries
            # drain via ``on_intent_verdict.pop`` within the TTL.
            cutoff = ts - self._AUTO_APPROVE_REASON_TTL
            if self._auto_approve_reasons and any(
                ins_ts < cutoff for _, ins_ts in self._auto_approve_reasons.values()
            ):
                self._auto_approve_reasons = {
                    cid: (reason, ins_ts)
                    for cid, (reason, ins_ts) in self._auto_approve_reasons.items()
                    if ins_ts >= cutoff
                }
            for entry in appended:
                cid = entry["call_id"]
                if cid:
                    self._auto_approve_reasons[cid] = (
                        entry["auto_approve_reason"],
                        ts,
                    )
        # Audit emission — one row per ``approve_tools`` call (not one
        # per item) keeps the audit table from blowing up on
        # tool-heavy turns while still capturing every tool name +
        # reason in the detail payload.
        try:
            from turnstone.core.audit import record_audit
            from turnstone.core.storage._registry import get_storage

            storage = get_storage()
            if storage is None:
                return
            tools = [
                {
                    "func_name": entry["func_name"],
                    "approval_label": entry["approval_label"],
                    "reason": entry["auto_approve_reason"],
                    "call_id": entry["call_id"],
                }
                for entry in appended
            ]
            record_audit(
                storage,
                self._user_id,
                "tool.auto_approved",
                "workstream",
                self.ws_id,
                {"tools": tools, "count": len(tools)},
            )
        except Exception:
            log.debug("auto_approve.audit_failed ws=%s", self.ws_id, exc_info=True)

    def _seed_event_id_from_storage(self) -> None:
        """Reseed :attr:`_event_id` from the persisted high-water mark.

        The per-ws event-id counter (the ``Last-Event-ID`` replay cursor)
        is in-memory and restarts at 0 on every UI construction — process
        restart, saved-workstream rehydrate, coord→node click-through.
        Without reseeding, the new process would re-issue ids the prior
        one already stamped onto ``conversations.event_id`` rows, so a
        ``/history`` cursor would point into the wrong generation and the
        fresh-connect fast-forward would mis-slice the ring buffer.

        Best-effort: any storage error (or no persisted event_id yet)
        leaves the counter at 0.  No-op without a ``ws_id`` (test
        fixture).  Called from ``__init__`` before any listener can
        register, so no lock is needed around the counter write.
        """
        if not self.ws_id:
            return
        try:
            from turnstone.core.storage._registry import get_storage

            storage = get_storage()
            if storage is None:
                return
            mx = storage.get_max_event_id(self.ws_id)
            if mx is not None and mx > self._event_id:
                self._event_id = int(mx)
        except Exception:
            log.debug("ui.seed_event_id_failed ws=%s", self.ws_id[:8], exc_info=True)

    def replay_recent_auto_approvals_from_audit(self) -> None:
        """Seed :attr:`_recent_auto_approvals` from the audit log.

        Without this the dashboard pill vanishes on every UI rebuild
        — a saved-workstream rehydrate / coord→node click-through /
        process restart all build a fresh ``WebUI`` whose ring
        buffer starts empty even though the audit table still holds
        the bypass history.  Replaying recent ``tool.auto_approved``
        rows on construction makes the pill survive these
        transitions; the audit row is the canonical durable
        record, the ring buffer is just its in-memory mirror.

        Best-effort: any storage / parse error silently leaves the
        buffer empty — the next live auto-approve will populate it
        as before.  No-op when ``ws_id`` is unset (test fixture).
        """
        if not self.ws_id:
            return
        try:
            from turnstone.core.storage._registry import get_storage

            storage = get_storage()
            if storage is None:
                return
            rows = storage.list_audit_events(
                action="tool.auto_approved",
                resource_id=self.ws_id,
                limit=self._RECENT_AUTO_APPROVALS_MAX,
            )
        except Exception:
            log.debug(
                "auto_approve.replay_from_audit_failed ws=%s",
                self.ws_id,
                exc_info=True,
            )
            return
        appended: list[dict[str, Any]] = []
        # ``list_audit_events`` returns DESC; reverse so the oldest
        # row lands first in the buffer (chronological order matches
        # what live appends produce).
        for row in reversed(rows or []):
            try:
                detail = json.loads(row.get("detail") or "{}")
            except (TypeError, ValueError):
                continue
            tools = detail.get("tools") or []
            ts = self._parse_audit_timestamp(row.get("timestamp", ""))
            for t in tools:
                if not isinstance(t, dict):
                    continue
                appended.append(
                    {
                        "call_id": t.get("call_id", "") or "",
                        "func_name": t.get("func_name", "") or "",
                        "approval_label": (t.get("approval_label") or t.get("func_name", "") or ""),
                        "auto_approve_reason": t.get("reason", "") or "",
                        "ts": ts,
                    }
                )
        if not appended:
            return
        with self._ws_lock:
            # Replay merges with whatever the live path may have
            # appended in the interleaving window between
            # construction and replay completion: replay rows go
            # first, then any live entries, and the buffer is
            # capped from the head.
            live = list(self._recent_auto_approvals)
            merged = appended + live
            self._recent_auto_approvals = merged[-self._RECENT_AUTO_APPROVALS_MAX :]

    @staticmethod
    def _parse_audit_timestamp(ts_str: str) -> float:
        """Parse the audit row's ISO-8601 timestamp into epoch seconds.

        Audit rows are written as ``datetime.now(UTC).strftime(...)``
        without a timezone marker (see ``storage/_sqlite.py:record_audit_event``),
        so ``datetime.fromisoformat`` returns a *naive* datetime.
        Calling ``.timestamp()`` on a naive datetime interprets it
        in the server's local timezone — wrong here, since the
        source clock is UTC.  Stamp UTC explicitly before
        converting so a non-UTC server doesn't render pill
        timestamps off by hours.

        Falls back to ``0.0`` on a missing / malformed timestamp so
        the buffer entry still surfaces (just with a "no time"
        indicator the JS pill renderer treats as missing).
        """
        if not ts_str:
            return 0.0
        try:
            from datetime import UTC, datetime

            dt = datetime.fromisoformat(ts_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt.timestamp()
        except (TypeError, ValueError):
            return 0.0

    def serialize_recent_auto_approvals(self) -> list[dict[str, Any]]:
        """Return a snapshot of the recent-auto-approves ring buffer.

        Read by the per-ws ``/dashboard`` payload (and the
        cross-cluster live-bulk projection on the console) so the
        coord-tree row can render an "auto-approved by …" pill.
        Returns a shallow copy under ``_ws_lock`` so the caller
        can't mutate the buffer mid-iteration.
        """
        with self._ws_lock:
            return [dict(entry) for entry in self._recent_auto_approvals]

    def on_output_warning(self, call_id: str, assessment: dict[str, Any]) -> None:
        """Deliver an output-guard warning to the live UI stream.

        Persistence is decoupled: the session calls
        :meth:`record_output_assessment` directly for each tier
        (heuristic / llm) so a single tool call's two-tier evaluation
        produces two rows.  This method only fires the UI event.
        """
        self._enqueue({"type": "output_warning", "call_id": call_id, **assessment})

    def record_output_assessment(
        self,
        call_id: str,
        assessment: dict[str, Any],
        *,
        tier: str = "heuristic",
        reasoning: str = "",
        judge_model: str = "",
        latency_ms: int = 0,
        confidence: float = 0.0,
    ) -> None:
        """Persist one output-guard assessment row.

        Called by the session once per tier.  A ``"heuristic"`` row is
        written when the regex stage produced signal (risk!="none" or
        flags) OR when the heuristic and LLM verdicts disagreed.  When the
        LLM stage ran it writes one row: ``"llm"`` on success (``reasoning``
        carries the model's explanation), or ``"llm_error"`` on failure
        (timeout / parse error / provider error — ``reasoning`` carries the
        error reason).  The distinct ``"llm_error"`` tier lets audit tell
        "LLM attempted but failed" from "LLM was never enabled" AND keeps the
        replay merge from treating a risk="none" failure row as a verdict
        that could shadow the heuristic finding.
        """
        try:
            from turnstone.core.storage._registry import get_storage

            storage = get_storage()
            if storage is None:
                return
            storage.record_output_assessment(
                assessment_id=uuid.uuid4().hex,
                ws_id=self.ws_id,
                call_id=call_id,
                func_name=assessment.get("func_name", ""),
                flags=json.dumps(assessment.get("flags", [])),
                risk_level=assessment.get("risk_level", "none"),
                annotations=json.dumps(assessment.get("annotations", [])),
                output_length=assessment.get("output_length", 0),
                redacted=assessment.get("redacted", False),
                tier=tier,
                reasoning=reasoning,
                judge_model=judge_model,
                latency_ms=latency_ms,
                confidence=confidence,
            )
        except Exception:
            log.debug("Failed to persist output assessment", exc_info=True)

    # ------------------------------------------------------------------
    # Streaming + status — lifted from WebUI in the rich ``ws_state``
    # payload work so coord populates the same per-ws metric fields the
    # cluster broadcast reads. Subclasses override ``_broadcast_state``
    # and ``_broadcast_activity`` for kind-specific transport
    # (interactive: per-node global SSE queue; coord: cluster collector).
    # ``WebUI`` additionally overrides ``on_status`` / ``on_tool_result``
    # to layer Prometheus ``_metrics.record_*`` calls (node-only) on top
    # of the shared writes.
    # ------------------------------------------------------------------

    def _reset_inflight_buffers_locked(self) -> None:
        """Clear the per-turn inflight content + reasoning. Caller holds ``_ws_lock``.

        ``_event_id`` is INTENTIONALLY not reset — it must remain
        monotonically increasing for the lifetime of the UI so a
        long-lived SSE subscriber's ``snap_seq`` cutoff stays a valid
        high-water mark across turn boundaries AND a ``Last-Event-ID``
        replay can still slice the buffer correctly across resets.
        If we reset to 0 at every turn, turn N+1's first M tokens
        (M = the snap_seq the subscriber captured mid-turn-N) would
        all carry ``_seq <= snap_seq`` and get silently dropped by
        the dedup filter in :func:`make_events_handler`; and a
        ``Last-Event-ID`` from before the reset would point into the
        OLD numbering and silently mis-replay.  ``_event_id`` is just
        an opaque monotonic tag — its absolute value doesn't matter,
        only that it never decreases for the lifetime of the UI.
        """
        self._ws_inflight_content = []
        self._ws_inflight_content_size = 0
        self._ws_inflight_reasoning = []
        self._ws_inflight_reasoning_size = 0

    def on_turn_start(self) -> None:
        """Reset inflight buffers at the top of each ``send()`` iteration.

        Defensive — covers the FIRST iteration of a fresh ``send()``
        where a prior ``send()`` may have crashed mid-stream and left
        stale content in the buffers. Steady-state, the buffers are
        already empty at this point because :meth:`on_turn_committed`
        cleared them right after the last assistant message committed.
        """
        with self._ws_lock:
            self._reset_inflight_buffers_locked()

    def on_turn_committed(self) -> None:
        """Reset inflight buffers right after the assistant message commits.

        The committed message is now in ``session.messages`` (the
        history source for SSE replay), so leaving the same text in
        the inflight buffer would double-render it on a refresh during
        the post-commit tool-execution window — history shows the
        committed turn AND the ``in_progress_snapshot`` shows the
        same text again.

        Future cross-turn reasoning persistence (some commercial
        models want reasoning preserved across user turns, not just
        within the current send) will override this hook to copy
        inflight reasoning to a per-message persistence store BEFORE
        clearing — keeping the `current vs historical` boundary clean.
        """
        with self._ws_lock:
            self._reset_inflight_buffers_locked()

    def on_thinking_start(self) -> None:
        """Track that the model is thinking; broadcast activity + enqueue."""
        with self._ws_lock:
            self._ws_current_activity = "Thinking…"
            self._ws_activity_state = "thinking"
        self._broadcast_activity()
        self._enqueue({"type": "thinking_start"})

    def on_thinking_stop(self) -> None:
        self._enqueue({"type": "thinking_stop"})

    def on_reasoning_token(self, text: str) -> None:
        """Append to the inflight reasoning buffer (capped) + enqueue.

        Mirrors :meth:`on_content_token`'s shape.  The ``_seq`` dedup
        tag is stamped by :meth:`_enqueue` against the per-ws
        ``_event_id`` counter, which advances on EVERY emit
        regardless of whether the inflight cap rejected the append.
        If the seq stalled at high-water-pre-cap, subscribers
        registering after the cap is hit would capture
        ``snap_seq == high-water`` and every subsequent live token
        (with the same stalled seq) would be filter-dropped as
        "already in your snapshot" — silently losing the rest of
        the stream. The cap is a buffer-size limit, NOT a "stop
        streaming" signal.

        Tokens past the cap are absent from ``snap.reasoning`` (the
        snapshot text was truncated at cap) but the live stream
        continues normally past them — refresh-after-cap renders the
        snapshot text up to the cap and then live tokens past it,
        with a visual gap equal to the past-cap chunk. No silent
        drop of subsequent tokens.

        **Lock coupling**: ``_enqueue`` is called WHILE still
        holding ``_ws_lock`` so the inflight append AND the
        ``_event_id`` advancement happen atomically against a
        snapshot reader.  Without this coupling a reader could
        capture the inflight (with the new text) and read
        ``_event_id`` BEFORE the writer's ``_enqueue`` bumped it,
        producing a ``snap_seq`` lower than the new event's
        ``_event_id``.  The new event would then slip past the
        ``_seq <= snap_seq`` live-drain dedup and double-render
        the text the snapshot already contained.  Acquisition
        order ``_ws_lock`` (outer) → ``_listeners_lock`` (inner via
        ``_enqueue``) matches the snapshot helpers, so no deadlock.
        """
        with self._ws_lock:
            if self._ws_inflight_reasoning_size < _MAX_TURN_CONTENT_CHARS:
                self._ws_inflight_reasoning.append(text)
                self._ws_inflight_reasoning_size += len(text)
            self._enqueue({"type": "reasoning", "text": text})

    def on_content_token(self, text: str) -> None:
        """Append to both turn-content buffers (capped) + enqueue.

        Writes under ``_ws_lock`` to two independent buffers:
         - ``_ws_turn_content`` (multi-turn, drained at idle/error)
           — fuels the dashboard's IDLE-piggyback content payload.
         - ``_ws_inflight_content`` (per-turn, drained at
           :meth:`on_turn_start`) — fuels the SSE ``in_progress_snapshot``
           event a reconnecting client sees on mid-stream refresh.

        Both caps are checked independently.  The ``_seq`` dedup tag
        is stamped by :meth:`_enqueue` against the per-ws
        ``_event_id`` counter, which advances on EVERY emit
        regardless of cap state — see :meth:`on_reasoning_token` for
        the full rationale, including why ``_enqueue`` runs while
        still holding ``_ws_lock`` (the lock coupling that makes
        ``snap_seq`` a true high-water mark for the snapshot text).

        The cap-check + append + size-update + enqueue all run under
        ``_ws_lock`` so a concurrent
        :meth:`snapshot_and_consume_state_payload` IDLE/ERROR drain or
        a concurrent :meth:`register_listener_with_in_progress_snapshot`
        / :meth:`register_listener_with_replay` sees a consistent
        ``(inflight_content, _event_id)`` pair.  In production this
        is single-writer-per-ws (the worker thread) but the snapshot
        reader runs from coord's adapter via ``mgr.set_state``;
        without the lock the writer's append could land in an
        orphaned list reference the snapshot just swapped out, AND
        the inflight/counter pair could de-sync.  Lock hold is
        microseconds (the fan-out's ``put_nowait`` calls are O(N
        listeners) but each is a single non-blocking enqueue).
        """
        with self._ws_lock:
            if self._ws_turn_content_size < _MAX_TURN_CONTENT_CHARS:
                self._ws_turn_content.append(text)
                self._ws_turn_content_size += len(text)
            if self._ws_inflight_content_size < _MAX_TURN_CONTENT_CHARS:
                self._ws_inflight_content.append(text)
                self._ws_inflight_content_size += len(text)
            self._enqueue({"type": "content", "text": text})

    def on_stream_end(self) -> None:
        with self._ws_lock:
            self._ws_current_activity = ""
            self._ws_activity_state = ""
        self._broadcast_activity()
        self._enqueue({"type": "stream_end"})

    def on_tool_result(
        self,
        call_id: str,
        name: str,
        output: str,
        *,
        is_error: bool = False,
    ) -> None:
        """Track per-ws tool-call counts + clear activity + enqueue.

        Subclasses can override to add kind-specific bookkeeping (e.g.
        ``WebUI`` calls :func:`_metrics.record_tool_call` on top of the
        shared writes); call ``super().on_tool_result(...)`` to keep
        the per-ws counters consistent.
        """
        with self._ws_lock:
            self._ws_tool_calls[name] = self._ws_tool_calls.get(name, 0) + 1
            self._ws_turn_tool_calls += 1
            self._ws_current_activity = ""
            self._ws_activity_state = ""
        self._broadcast_activity()
        event: dict[str, Any] = {
            "type": "tool_result",
            "call_id": call_id,
            "name": name,
            "output": output,
        }
        if is_error:
            event["is_error"] = True
        self._enqueue(event)

    def on_tool_output_chunk(self, call_id: str, chunk: str) -> None:
        self._enqueue({"type": "tool_output_chunk", "call_id": call_id, "chunk": chunk})

    def on_status(self, usage: dict[str, Any], context_window: int, effort: str) -> None:
        """Record per-ws token / context counters + enqueue + persist usage.

        Shared body: writes ``_ws_prompt_tokens`` / ``_ws_completion_tokens``
        / ``_ws_context_ratio`` under ``_ws_lock``, fans the ``status``
        event to listener queues, and persists a ``usage_event`` row to
        storage for governance dashboards. Subclasses (currently
        :class:`WebUI`) can override to layer node-only Prometheus
        ``_metrics.record_*`` calls before / after; call
        ``super().on_status(...)`` to keep the per-ws counters and
        usage-event row consistent.

        Behaviour change for coord: pre-lift coord's ``on_status`` was a
        thin enqueue-only stub; the lift turns it into the same writes
        WebUI does, so the cluster collector's rich ``ws_state``
        broadcast reads non-zero ``tokens`` / ``context_ratio`` for
        coord rows, AND coord ``usage_event`` rows now persist to
        storage (governance gains visibility into coordinator token
        consumption).

        ``usage`` field access is defensive (``.get(..., 0)``) — pre-
        lift coord's stub used the same defensive pattern, and a
        provider-translation bug that produces a partial ``usage``
        dict shouldn't be surfaced as a worker-thread KeyError.
        """
        prompt_tokens = usage.get("prompt_tokens", 0)
        completion_tokens = usage.get("completion_tokens", 0)
        total_tok = prompt_tokens + completion_tokens
        pct = total_tok / context_window * 100 if context_window > 0 else 0
        cache_creation = usage.get("cache_creation_tokens", 0)
        cache_read = usage.get("cache_read_tokens", 0)
        with self._ws_lock:
            self._ws_prompt_tokens += prompt_tokens
            self._ws_completion_tokens += completion_tokens
            self._ws_context_ratio = total_tok / context_window if context_window > 0 else 0.0
            tool_total = sum(self._ws_tool_calls.values())
            tool_count = tool_total - self._ws_tool_calls_reported
            self._ws_tool_calls_reported = tool_total
            turn_tool_calls = self._ws_turn_tool_calls
            turn_count = self._ws_messages
        self._enqueue(
            {
                "type": "status",
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tok,
                "context_window": context_window,
                "pct": round(pct, 1),
                "effort": effort,
                "cache_creation_tokens": cache_creation,
                "cache_read_tokens": cache_read,
                "tool_calls_this_turn": turn_tool_calls,
                "turn_count": turn_count,
            }
        )
        self._write_usage_row(
            model=usage.get("model", ""),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            tool_calls_count=tool_count,
            cache_creation_tokens=cache_creation,
            cache_read_tokens=cache_read,
        )

    def on_aux_usage(self, usage: dict[str, Any]) -> None:
        """Persist a ``usage_event`` for an auxiliary (non-main-loop) LLM call.

        Title generation, conversation compaction, web-fetch
        summarisation, and task sub-agents all run through the
        provider's non-streaming ``create_completion`` and never reach
        :meth:`on_status` — so without this their tokens are invisible to
        the governance usage dashboard, undercounting real consumption
        (potentially by a large factor for agent-heavy workstreams).

        Deliberately narrower than :meth:`on_status`: it records ONLY the
        storage row.  It does NOT enqueue a ``status`` UI event, advance
        ``_ws_prompt_tokens`` / ``_ws_context_ratio`` (an auxiliary
        prompt is not the main conversation's live context — folding it
        in would make the status-bar context gauge lie), or touch the
        tool-call delta counter.  ``tool_calls_count`` is recorded as 0:
        any tools a sub-agent or judge calls internally are its own
        concern, not part of this workstream's surfaced tool tally.

        ``WebUI`` overrides this to also feed the node Prometheus token
        counters; call ``super().on_aux_usage(...)`` to keep the
        ``usage_event`` row consistent.
        """
        self._write_usage_row(
            model=usage.get("model", ""),
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            tool_calls_count=0,
            cache_creation_tokens=usage.get("cache_creation_tokens", 0),
            cache_read_tokens=usage.get("cache_read_tokens", 0),
        )

    def _write_usage_row(
        self,
        *,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        tool_calls_count: int,
        cache_creation_tokens: int,
        cache_read_tokens: int,
    ) -> None:
        """Persist one ``usage_event`` row, swallowing + logging storage
        errors so a reporting-sink failure never breaks a live turn.

        Shared by :meth:`on_status` (main-loop turns, real tool-call
        delta) and :meth:`on_aux_usage` (auxiliary calls, with
        ``tool_calls_count=0``) so the get-storage / record / except-log
        shape lives in one place and can't drift if the
        ``record_usage_event`` signature changes.
        """
        try:
            from turnstone.core.storage._registry import get_storage

            storage = get_storage()
            if storage is not None:
                storage.record_usage_event(
                    event_id=uuid.uuid4().hex,
                    user_id=self._user_id,
                    ws_id=self.ws_id,
                    node_id="",
                    model=model,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    tool_calls_count=tool_calls_count,
                    cache_creation_tokens=cache_creation_tokens,
                    cache_read_tokens=cache_read_tokens,
                )
        except Exception:
            log.warning("Failed to record usage event", exc_info=True)

    def on_info(self, message: str) -> None:
        # Inside a task agent (on THIS thread), progress chatter ("[task done] N
        # chars", a tool's "fetched N chars") carries no call_id, so it can't
        # nest under the task card — drop it on the web pane rather than let it
        # escape to the top level.  Per-thread, so a parallel sibling tool's info
        # still shows.  The card shows the steps + result; the CLI overrides this
        # method and keeps the lines (no card there).
        if _agent_scope_var.get() > 0:
            return
        self._enqueue({"type": "info", "message": message})

    def on_error(self, message: str) -> None:
        self._enqueue({"type": "error", "message": message})

    def on_system_turn(
        self, content: str, source: str, meta: dict[str, Any] | None = None
    ) -> int | None:
        """Surface a first-class operator-context system turn as its own
        UI element.

        Operator context (output-guard findings, user interjections,
        metacognitive nudges, watch results) lives in the conversation
        trajectory as a real ``{"role": "system", "_source": <kind>, ...}``
        turn (see ``tool_advisory.make_system_turn``).  This event lets every
        connected SSE consumer (other browser tabs, CLI mirrors, future
        channel adapters) render the operator bubble in lockstep with the
        originating tab's optimistic render.  The history-replay path
        surfaces the same shape via ``project_history_messages`` (the REST
        ``/history`` projection) so a tab reconnecting later renders the
        same bubble.  ``source`` carries the turn's ``_source`` kind so
        the frontend can label / style the bubble; ``meta`` carries the
        turn's structured per-kind fields (``watch_triggered``'s
        ``watch_name`` / command / poll counters) so the frontend can rebuild
        per-kind rendering (the watch-result card).  ``None`` for kinds with
        no structured data.

        Returns the SSE ``_event_id`` assigned to the emitted event so the
        caller persists the row with the matching id (``None`` for UIs
        without an event stream).
        """
        return self._enqueue(
            {"type": "system_turn", "content": content, "source": source, "meta": meta or None}
        )

    # ------------------------------------------------------------------
    # Broadcast hooks — kind-specific transport.
    #
    # Default implementations are no-ops: subclasses override to fan
    # the snapshot out to their kind's transport (interactive: per-node
    # global SSE queue; coord: cluster collector). The shared on_* methods
    # above call these unconditionally so adding broadcast on a future
    # kind only requires the override.
    # ------------------------------------------------------------------

    def _broadcast_state(self, state: str) -> None:  # noqa: ARG002 — hook stub
        """Fan a state-change snapshot out to the kind's transport.

        Default: no-op. Subclasses override.
        """

    def _broadcast_activity(self) -> None:
        """Fan a current-activity snapshot out to the kind's transport.

        Default: no-op. Subclasses override.
        """

    def _broadcast_intent_verdict(self, verdict: dict[str, Any]) -> None:  # noqa: ARG002 — hook stub
        """Fan an LLM intent-judge verdict out to the kind's transport.

        Default: no-op. ``ConsoleCoordinatorUI`` overrides to push a
        ``intent_verdict`` event onto the cluster bus so the parent
        coordinator's tree UI can render the risk pill + verdict
        result without polling. Stage 3 Step 4: hook only — the
        cluster-bus event class lands in Step 5.
        """

    def _broadcast_approval_resolved(
        self,
        approved: bool,  # noqa: ARG002 — hook stub
        feedback: str | None = None,  # noqa: ARG002 — hook stub
        *,
        always: bool = False,  # noqa: ARG002 — hook stub
        cycle_id: str = "",  # noqa: ARG002 — hook stub
        call_ids: tuple[str, ...] = (),  # noqa: ARG002 — hook stub
    ) -> None:
        """Fan an ``approval_resolved`` decision out to the kind's transport.

        Default: no-op. ``ConsoleCoordinatorUI`` overrides to push to
        the cluster bus so the parent coordinator's tree UI can clear
        the pending-approval pill in sync with the actual decision.
        ``cycle_id`` / ``call_ids`` name WHICH cycle resolved — with
        parallel task agents several are live, and a bare decision
        would clear the wrong block.
        """

    def _broadcast_approve_request(self, detail: dict[str, Any]) -> None:  # noqa: ARG002 — hook stub
        """Fan an ``approve_request`` payload out to the kind's transport.

        Default: no-op. ``WebUI`` and ``ConsoleCoordinatorUI`` override
        to push the items list (the same dict that landed in
        ``_pending_approval``) onto their respective transports. The
        push path eliminates the bulk-fetch race that otherwise
        leaves coord rows stuck on a loading placeholder when the
        bulk fetch lands in the gap between the state transition to
        ATTENTION and ``_pending_approval`` being set.
        """

    # ------------------------------------------------------------------
    # State-broadcast snapshot helper
    # ------------------------------------------------------------------

    def snapshot_and_consume_state_payload(self, state: str) -> dict[str, Any]:
        """Return the rich-payload snapshot a state-change broadcast carries.

        Reads under ``_ws_lock`` so the snapshot is internally consistent
        with concurrent ``on_status`` / ``on_tool_result`` /
        ``on_content_token`` writes (all of which take the same lock).
        For terminal states (``"idle"`` / ``"error"``) the turn-content
        accumulator is also swapped out — IDLE piggybacks the joined
        content onto the broadcast so the dashboard renders the
        assistant turn without an extra storage round-trip; ERROR
        clears without emitting (the broadcast is just for the state
        transition).

        The IDLE branch swaps the accumulator OUT under the lock and
        joins the captured list OUTSIDE the lock — keeps the lock
        hold to microseconds even on a 256 KiB turn, and decouples
        the join walk from any concurrent ``on_content_token`` racing
        the swap.

        Returns a dict with keys ``tokens`` / ``context_ratio`` /
        ``activity`` / ``activity_state`` / ``content``. Callers fan
        the dict out via their kind's transport
        (:meth:`_broadcast_state` or, for coord,
        :meth:`turnstone.console.coordinator_adapter.CoordinatorAdapter.emit_state`).
        """
        captured_content: list[str] = []
        with self._ws_lock:
            tokens = self._ws_prompt_tokens + self._ws_completion_tokens
            ctx = self._ws_context_ratio
            activity = self._ws_current_activity
            activity_state = self._ws_activity_state
            if state == "idle":
                captured_content = self._ws_turn_content
                self._ws_turn_content = []
                self._ws_turn_content_size = 0
                # Drain the per-turn inflight buffers too so a refresh
                # post-cancel doesn't double-render against history.
                # On the success path :meth:`on_turn_committed` already
                # cleared inflight at ``messages.append`` time, so this
                # is a no-op there. On cancel/error/exception paths
                # nothing else clears inflight — this single chokepoint
                # covers them all.
                self._reset_inflight_buffers_locked()
            elif state == "error":
                self._ws_turn_content = []
                self._ws_turn_content_size = 0
                self._reset_inflight_buffers_locked()
        # Join outside the lock — bounded at _MAX_TURN_CONTENT_CHARS but
        # still O(n) over the captured fragments, so worth not blocking
        # concurrent on_content_token writers for the duration.
        content = "".join(captured_content) if captured_content else ""
        return {
            "tokens": tokens,
            "context_ratio": ctx,
            "activity": activity,
            "activity_state": activity_state,
            "content": content,
        }
