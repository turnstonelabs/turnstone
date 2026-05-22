"""Shared scaffolding for :class:`SessionUI` implementations.

Both :class:`turnstone.server.WebUI` (interactive node UI) and
:class:`turnstone.console.coordinator_ui.ConsoleCoordinatorUI` wrap a
:class:`~turnstone.core.session.ChatSession` and fan events out over
SSE to one or more connected browser tabs. They also block the worker
thread on two pending-input gates (tool approval, plan review) that
HTTP handlers resolve.

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
import copy
import json
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


def _resolve_event_buffer_max() -> int:
    """Read ``TURNSTONE_SSE_EVENT_BUFFER_MAX`` env override at import time.

    Default 2000 events covers ~10-40 seconds of cloud-provider streaming
    (50-200 events/sec per active stream) — enough to make any typical
    network blip or intermediary timeout transparent to the browser.
    Local-inference deployments (vLLM, llama.cpp at 500-2000 tok/s) can
    burn through the cap in under a second; operators on those workloads
    can raise the bound knowing the tradeoff (linear memory growth ×
    100-workstream design ceiling).  Below-cap reconnects always hit the
    replay path; above-cap reconnects fall back to the snapshot recovery
    floor with an explicit ``replay_truncated`` envelope so the client
    knows it lost live ticks.
    """
    raw = os.environ.get("TURNSTONE_SSE_EVENT_BUFFER_MAX", "").strip()
    if not raw:
        return 2000
    try:
        n = int(raw)
    except ValueError:
        return 2000
    return n if n > 0 else 2000


# Per-ws ring buffer for ``Last-Event-ID`` SSE replay.  Holds the most
# recent events keyed by monotonic ``_event_id``; deque ``maxlen`` evicts
# oldest automatically.  See :func:`_resolve_event_buffer_max` for the
# sizing rationale.
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

    The five reasons reflect the disjoint set of paths that bypass
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
    """

    SKILL = "skill"
    ALWAYS = "always"
    POLICY = "policy"
    BLANKET = "blanket"
    AUTO_APPROVE_TOOLS = "auto_approve_tools"

    ALL: frozenset[str] = frozenset({SKILL, ALWAYS, POLICY, BLANKET, AUTO_APPROVE_TOOLS})


class SessionUIBase:
    """SSE listener fan-out + approval/plan event machinery.

    Thread-safety: the ChatSession worker thread calls the ``on_*``
    methods (and the approval/plan blocking helpers that live on
    subclasses); HTTP handlers drive ``_register_listener`` /
    ``_unregister_listener`` / ``resolve_approval`` / ``resolve_plan``
    from the event loop. All shared state is guarded by
    ``_listeners_lock`` or ``threading.Event`` primitives.
    """

    def __init__(self, ws_id: str = "", user_id: str = "") -> None:
        self.ws_id = ws_id
        self._user_id = user_id
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
        # Approval blocking — the worker thread calls approve_tools
        # which waits on _approval_event; the /approve endpoint sets
        # it via resolve_approval.
        self._approval_event = threading.Event()
        self._approval_result: tuple[bool, str | None] = (False, None)
        # Pending approval shape — re-sent on SSE reconnect so a user
        # switching tabs still sees the prompt.
        self._pending_approval: dict[str, Any] | None = None
        self._plan_event = threading.Event()
        self._plan_result: str = ""
        self._pending_plan_review: dict[str, Any] | None = None
        self.auto_approve = False
        self.auto_approve_tools: set[str] = set()
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
        # Verdicts from the LLM intent judge — tracked so
        # ``resolve_approval`` can stamp a ``user_decision`` onto every
        # verdict that fired during this approval round.
        self._pending_verdicts: list[dict[str, Any]] = []
        self._last_verdict_decision: str = ""
        # Verdict cache for SSE reconnect replay (tab switching
        # shouldn't lose the judge's final call on a just-run tool).
        self._llm_verdicts: dict[str, dict[str, Any]] = {}
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

    # ------------------------------------------------------------------
    # Listener plumbing (SSE)
    # ------------------------------------------------------------------

    def _enqueue(self, data: dict[str, Any]) -> None:
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
        """
        if "ws_id" not in data:
            data = {**data, "ws_id": self.ws_id}
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
    ) -> tuple[queue.Queue[dict[str, Any]], list[dict[str, Any]], str, int, int]:
        """Register a listener AND capture buffered events for replay.

        Used by :func:`make_events_handler` when the client sends
        ``Last-Event-ID`` (header or ``?last_event_id=`` query-param
        fallback for the manual-reconnect path).  Returns

            ``(client_queue, replay_events, status, lost_count,
            earliest_available_id)``

        where ``status`` is one of ``"replay_ok"`` (caller emits the
        replay events then drops into live drain, skipping
        ``replay_cb`` / ``state_change`` / ``in_progress_snapshot``)
        or ``"truncated"`` (caller emits a ``replay_truncated``
        envelope then falls through to the fresh-connect replay path
        as the recovery floor — the snapshot picks up the partial
        content/reasoning that the evicted events would have carried).

        Atomicity contract: under ``_listeners_lock`` we both snapshot
        the buffer AND register the listener.  A writer's
        :meth:`_enqueue` takes the same lock, so events either
          - land in the buffer snapshot but NOT the listener queue
            (writer ran before our lock acquire — caught by the
            replay slice), or
          - land in the listener queue but NOT the buffer snapshot
            (writer ran after our lock release — live drain handles
            them, ``_event_id`` is strictly above
            ``earliest_available_id``).
        No event is double-delivered, none is lost across the
        registration boundary.

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
        starts evicting only after 2000 events have been enqueued —
        which means the counter is >= 2000 and the client's
        last_event_id is below earliest, so they get ``truncated``
        instead).  We can't distinguish the two without a separate
        ``highest_evicted_id`` tracker; treating empty as ``replay_ok``
        is the safe choice for the genuine cold-start case (no false
        ``replay_truncated`` envelopes on freshly-opened workstreams).
        """
        client_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=maxsize)
        with self._listeners_lock:
            buffered = list(self._event_buffer)
            self._listeners.append(client_queue)
        if not buffered:
            return client_queue, [], "replay_ok", 0, 0
        earliest_id = buffered[0][0]
        if last_event_id < earliest_id - 1:
            lost_count = (earliest_id - 1) - last_event_id
            return client_queue, [], "truncated", lost_count, earliest_id
        replay_events = [ev for eid, ev in buffered if eid > last_event_id]
        return client_queue, replay_events, "replay_ok", 0, earliest_id

    # ------------------------------------------------------------------
    # Approval / plan blocking gates
    # ------------------------------------------------------------------

    def _reset_approval_cycle(self) -> None:
        """Clear per-round verdict state at the start of a new approval.

        Subclasses call this at the top of ``approve_tools`` so late
        verdicts from the previous round can't leak their
        ``user_decision`` onto the next round's verdicts, and the SSE
        reconnect replay cache doesn't serve stale tool verdicts to a
        client that just switched tabs mid-approval.
        """
        with self._ws_lock:
            self._last_verdict_decision = ""
            self._llm_verdicts.clear()

    def resolve_approval(
        self,
        approved: bool,
        feedback: str | None = None,
        *,
        always: bool = False,
        timeout: bool = False,
    ) -> None:
        """Unblock a pending approval with the caller's decision.

        Broadcasts ``approval_resolved`` so every connected tab can
        dismiss its prompt modal in sync (e.g. desktop dismisses when
        phone approves). Updates ``user_decision`` on every LLM
        intent-verdict that fired during this approval round — the
        audit trail reflects what the user actually chose.

        ``always`` reports whether the resolving caller asked for
        "Approve + Always" (the tool name has been added to
        ``auto_approve_tools`` upstream by the HTTP handler — this
        method only echoes the intent on the SSE event so peer tabs
        can label their resolved-status pill correctly).  Keyword-only
        + default ``False`` so the four pre-existing callers (cancel,
        timeout, channel adapters) compile unchanged.

        ``timeout`` flips the persisted ``user_decision`` from
        ``"denied"`` to ``"timeout"`` so the audit trail can
        distinguish an active user denial from a passive
        approval-timeout expiry — the feedback string carries the
        same information today but operators querying on the
        ``user_decision`` column alone could not tell them apart.
        Mutually exclusive with ``approved=True`` (a timeout is a
        passive denial); the combination raises ``ValueError`` so a
        future caller can't accidentally ship a row whose audit
        column says ``"timeout"`` while the SSE event reports
        ``approved=True``.
        """
        if timeout and approved:
            raise ValueError("resolve_approval: timeout=True is incompatible with approved=True")
        decision_str = "timeout" if timeout else ("approved" if approved else "denied")
        # Swap-and-clear + set decision under lock to avoid racing
        # with the daemon judge thread's ``on_intent_verdict`` appends.
        with self._ws_lock:
            pending = self._pending_verdicts
            self._pending_verdicts = []
            self._last_verdict_decision = decision_str
        if pending:
            self._persist_verdict_decisions(pending, decision_str)
        self._approval_result = (approved, feedback)
        self._enqueue(
            {
                "type": "approval_resolved",
                "approved": approved,
                "feedback": feedback or "",
                "always": bool(always),
            }
        )
        # Kind-specific cross-stream broadcast — ConsoleCoordinatorUI
        # overrides to push onto the cluster bus so a coord parent's
        # tree UI clears the pending-approval pill in lockstep with
        # the actual decision. Stage 3 Step 4.
        self._broadcast_approval_resolved(approved, feedback, always=always)
        self._approval_event.set()

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

    def resolve_plan(self, feedback: str) -> None:
        """Unblock a pending plan review with the caller's verdict.

        ``cancel_generation`` calls this unconditionally to unblock
        any wait, so the path has to be safe when no plan is pending
        (just signal and skip the broadcast).
        """
        self._plan_result = feedback
        if self._pending_plan_review is None:
            self._plan_event.set()
            return
        # Clear pending BEFORE broadcasting so a client reconnecting
        # in the window between enqueue and clear cannot receive both
        # the replayed plan_review (SSE re-injection at the connect
        # handler) AND the live plan_resolved. Mirrors the
        # approval_resolved pattern above.
        self._pending_plan_review = None
        self._enqueue({"type": "plan_resolved", "feedback": feedback})
        self._plan_event.set()

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
        7. Emit the ``approve_request`` and block on ``_approval_event``
           up to ``_APPROVAL_WAIT_TIMEOUT``.

        ``__budget_override__`` is interactive-only today (coord
        workstreams don't have token budgets), but the carve-out check
        is cheap (``any(...)`` over pending) and is a no-op on coord;
        kept unconditional so a future coord-skill path picks it up
        for free.
        """
        self._reset_approval_cycle()
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
        # ``resolve_approval`` on close.  ``_pending_verdicts`` only
        # tracks the latter — auto-approved verdicts are already final.
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

        with self._ws_lock:
            self._pending_verdicts = pending_verdicts

        # Record any items the policy block already auto-approved
        # before falling through to the prompt — without this the
        # mixed-policy-then-prompt path leaves the policy bypass
        # invisible to /dashboard (the auto-approve fall-through never
        # runs since pending is non-empty + blanket inactive).
        # No-op when no items are auto-approve-tagged.
        self._record_auto_approves(items)

        # Send approval request and block
        judge_pending = any(it.get("_heuristic_verdict") for it in items)
        self._approval_event.clear()
        self._pending_approval = {
            "type": "approve_request",
            "items": self._serialize_approval_items(items),
            "judge_pending": judge_pending,
        }
        self._enqueue(self._pending_approval)
        # Cross-stream broadcast — push the items via the cluster bus
        # so a coord parent's tree UI can render the inline approve/deny
        # block without waiting for a bulk fetch. Without this, the
        # bulk fetch races with this assignment: the state transition
        # to ATTENTION fires upstream BEFORE approve_tools runs (see
        # session.py:_emit_state("attention") preceding ui.approve_tools),
        # so a bulk fetch landing in the ~50-200ms window between
        # _emit_state and this point sees ``_pending_approval=None``
        # and returns ``pending_approval_detail: null``. The 5s TTL
        # then locks the coord row on a "loading" placeholder until
        # the next state event triggers a refresh — which never comes
        # while parked on _approval_event.wait. The push path
        # eliminates the race.
        self._broadcast_approve_request(self._pending_approval)
        if not self._approval_event.wait(timeout=self._APPROVAL_WAIT_TIMEOUT):
            # Approval timed out (e.g., user disconnected). Deny via
            # resolve_approval so verdicts and state are updated consistently.
            # Feedback string derives from ``_APPROVAL_WAIT_TIMEOUT`` so the
            # text follows the constant if the timeout knob moves.
            log.warning("Approval timed out for ws_id=%s", self.ws_id)
            self.resolve_approval(
                False,
                f"Approval timed out after {self._APPROVAL_WAIT_TIMEOUT}s",
                timeout=True,
            )
        self._pending_approval = None
        approved, feedback = self._approval_result

        if not approved:
            denial_msg = "Denied by user"
            if feedback:
                denial_msg += f": {feedback}"
            for item in pending:
                item["denied"] = True
                item["denial_msg"] = denial_msg

        return approved, feedback

    # ------------------------------------------------------------------
    # Intent-judge + output-guard plumbing
    # ------------------------------------------------------------------

    # Hard cap on the in-memory verdict cache so a long-running session
    # can't grow unbounded. FIFO eviction on insert.
    _LLM_VERDICT_CACHE_MAX = 50

    # Hard cap on how long a worker thread blocks waiting for an
    # approval / plan-review decision. Subclasses' ``approve_tools`` and
    # ``on_plan_review`` reference this rather than the literal so a
    # future ``settings.approval_timeout_seconds`` knob can swap it in
    # one place.
    _APPROVAL_WAIT_TIMEOUT = 3600

    def on_intent_verdict(self, verdict: dict[str, Any]) -> None:
        """Deliver an LLM intent-judge verdict to the frontend + persist.

        Caches under ``_ws_lock`` for SSE reconnect replay, persists
        the row to storage, and either records the caller's
        ``user_decision`` immediately (if the approval already
        resolved) or parks the verdict in ``_pending_verdicts`` for
        ``resolve_approval`` to stamp on close.

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
        if call_id:
            with self._ws_lock:
                if (
                    len(self._llm_verdicts) >= self._LLM_VERDICT_CACHE_MAX
                    and call_id not in self._llm_verdicts
                ):
                    oldest_key = next(iter(self._llm_verdicts))
                    del self._llm_verdicts[oldest_key]
                self._llm_verdicts[call_id] = verdict
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
        # Decision check + either queue or flag-for-persist happen
        # under ONE lock acquisition so resolve_approval can't swap-
        # and-clear _pending_verdicts between our read and our
        # append. Without this: decide "no decision yet" → release
        # lock → resolve_approval swaps the list + sets decision →
        # we re-acquire and append to the fresh (empty) list; verdict
        # sits queued until the next round, gets stamped with the
        # WRONG decision.  Storage UPDATE happens outside the lock
        # on the already-resolved path — no contention with other
        # ws-scoped work.
        # If ``auto_reason`` was stamped above, the verdict already
        # carries the final ``user_decision`` for this row.  Neither
        # path below applies: appending to ``_pending_verdicts`` would
        # cause ``resolve_approval`` (on the manual-approval sibling
        # in a mixed batch) to overwrite the auto-reason with
        # ``"approved"``/``"denied"``/``"timeout"``; the
        # ``_persist_verdict_decisions`` immediate-stamp path would
        # overwrite it the same way from a prior cycle's decision.
        # Skip both so the audit trail keeps the auto-approve reason.
        if auto_reason:
            return
        with self._ws_lock:
            decision = self._last_verdict_decision
            if not decision:
                self._pending_verdicts.append(verdict)
        if decision:
            self._persist_verdict_decisions([verdict], decision)

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
            # Plain INSERT (not UPSERT) at the bulk site.  The race
            # where a daemon-judge verdict lands BEFORE this bulk
            # write IS reachable today: ``_evaluate_intent``
            # (session.py) spawns the daemon thread before
            # ``approve_tools`` is called, and the daemon's first
            # emission (heuristic-only short batch, fast LLM response,
            # or cancel-event ``_deliver_fallbacks`` from judge.py)
            # can fire ``_persist_intent_verdict`` before this bulk
            # INSERT runs.  Outcome of that race is unchanged by the
            # per-row UPSERT switch: the bulk INSERT statement aborts
            # on PK collision regardless of whether the colliding row
            # was planted by INSERT or UPSERT, and the wrapping
            # ``try/except`` swallows it.  Race A (daemon fires AFTER
            # bulk) IS improved by the fix: heuristic→llm_fallback
            # upgrade-in-place now lands.  Future bulk-side hardening
            # (``ON CONFLICT DO NOTHING``) would preserve the OTHER
            # rows in the batch when one collides, but would keep the
            # daemon's ``tier`` ("llm"/"llm_fallback") for the
            # colliding row instead of the bulk's heuristic stamp.
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

    def serialize_pending_approval_detail(self) -> dict[str, Any] | None:
        """Build the inline approval payload for dashboard projection.

        Merges :attr:`_pending_approval` items with the per-call_id
        LLM verdict cache so a coordinator's children-tree row can
        render inline approve/deny buttons + judge-verdict pill
        without making a separate per-child round-trip. Returns
        ``None`` when no approval is pending.

        The shape mirrors the inline tool-call info already broadcast
        via the ``approve_request`` SSE event but adds a
        ``judge_verdict`` sibling per item (looked up from
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
        pending = self._pending_approval
        if pending is None:
            return None
        items = pending.get("items") or []
        if not items:
            return None
        call_ids = [item.get("call_id", "") for item in items]
        # Snapshot verdict references under lock, deepcopy after
        # release. Writers (``on_intent_verdict`` daemon judge
        # thread + ``_reset_approval_cycle`` worker thread) only
        # ASSIGN entries, never mutate them in place — so a snapped
        # reference is stable after the lock drops, and the
        # subsequent deepcopy can run lock-free even though
        # verdict dicts carry list-typed ``evidence`` that
        # downstream callers might mutate. This keeps the lock
        # window O(N items) without paying the deepcopy cost
        # (O(verdict size)) under contention with the per-token
        # write path that also holds ``_ws_lock``.
        with self._ws_lock:
            verdict_refs = {
                cid: self._llm_verdicts[cid]
                for cid in call_ids
                if cid and cid in self._llm_verdicts
            }
        verdicts = {cid: copy.deepcopy(v) for cid, v in verdict_refs.items()}
        serialized: list[dict[str, Any]] = []
        for item in items:
            cid = item.get("call_id", "")
            serialized.append(
                {
                    "call_id": cid,
                    "header": item.get("header", ""),
                    "preview": item.get("preview", ""),
                    "func_name": item.get("func_name", ""),
                    "approval_label": item.get("approval_label", ""),
                    "needs_approval": item.get("needs_approval", False),
                    "error": item.get("error"),
                    "heuristic_verdict": item.get("verdict"),
                    "judge_verdict": verdicts.get(cid),
                }
            )
        # Primary call_id = first non-empty in list order (matches the
        # 409 response shape from ``make_approve_handler`` so the UI
        # can render the same identifier the server thinks is current).
        primary = next((cid for cid in call_ids if cid), "")
        return {
            "call_id": primary,
            "judge_pending": bool(pending.get("judge_pending", False)),
            "items": serialized,
        }

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
        consistency with :meth:`serialize_pending_approval_detail`
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
        """Deliver an output-guard warning + persist its assessment row."""
        self._enqueue({"type": "output_warning", "call_id": call_id, **assessment})
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
        the full rationale.

        The cap-check + append + size-update run under ``_ws_lock``
        so a concurrent
        :meth:`snapshot_and_consume_state_payload` IDLE/ERROR drain or
        a concurrent :meth:`register_listener_with_in_progress_snapshot`
        can't see a torn list mid-append. In production this is
        single-writer-per-ws (the worker thread) but the snapshot
        reader runs from coord's adapter via ``mgr.set_state``;
        without the lock the writer's append could land in an
        orphaned list reference the snapshot just swapped out. Lock
        hold is microseconds.
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
        try:
            from turnstone.core.storage._registry import get_storage

            storage = get_storage()
            if storage is not None:
                storage.record_usage_event(
                    event_id=uuid.uuid4().hex,
                    user_id=self._user_id,
                    ws_id=self.ws_id,
                    node_id="",
                    model=usage.get("model", ""),
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    tool_calls_count=tool_count,
                    cache_creation_tokens=cache_creation,
                    cache_read_tokens=cache_read,
                )
        except Exception:
            log.warning("Failed to record usage event", exc_info=True)

    def on_info(self, message: str) -> None:
        self._enqueue({"type": "info", "message": message})

    def on_error(self, message: str) -> None:
        self._enqueue({"type": "error", "message": message})

    def on_user_reminder(self, reminders: list[dict[str, Any]], source: str | None = None) -> None:
        """Surface a metacognitive user-channel nudge as its own UI
        element.

        Reminders live on the user message dict's ``_reminders``
        side-channel and are spliced into ``content`` only at the
        provider boundary; this event is what lets every connected
        SSE consumer (other browser tabs, CLI mirrors, future channel
        adapters) render the reminder bubble in lockstep with the
        originating tab.  The history-replay path surfaces the same
        shape via ``_build_history`` so a tab reconnecting later
        renders the same bubble.

        ``source`` mirrors the user-message dict's ``_source`` field
        (today only ``"system_nudge"`` for wake-driven reminders).  The
        frontend uses it to render the thin ``.msg.user.system-nudge``
        marker before anchoring the reminder bubbles below it.
        """
        evt: dict[str, Any] = {"type": "user_reminder", "reminders": reminders}
        if source:
            evt["source"] = source
        self._enqueue(evt)

    def on_tool_reminder(self, reminders: list[dict[str, Any]], tool_call_id: str) -> None:
        """Surface a metacognitive tool-channel nudge (``tool_error`` /
        ``repeat``) as its own UI element below the tool result that
        triggered it.

        Tool-channel reminders ride the same ``_reminders``
        side-channel pattern as the user channel — kept out of
        ``content`` so compaction / title-gen / channel adapters never
        see the nudge text, spliced into the wire only via
        ``_apply_reminders_for_provider``.  ``tool_call_id`` is the
        anchor the frontend uses to render the bubble below the
        specific tool result that triggered the batch's reminder.
        """
        self._enqueue(
            {
                "type": "tool_reminder",
                "reminders": reminders,
                "tool_call_id": tool_call_id,
            }
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
    ) -> None:
        """Fan an ``approval_resolved`` decision out to the kind's transport.

        Default: no-op. ``ConsoleCoordinatorUI`` overrides to push to
        the cluster bus so the parent coordinator's tree UI can clear
        the pending-approval pill in sync with the actual decision.
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
