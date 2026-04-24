"""Shared scaffolding for :class:`SessionUI` implementations.

Both :class:`turnstone.server.WebUI` (interactive node UI) and
:class:`turnstone.console.coordinator_ui.ConsoleCoordinatorUI` wrap a
:class:`~turnstone.core.session.ChatSession` and fan events out over
SSE to one or more connected browser tabs. They also block the worker
thread on two pending-input gates (tool approval, plan review) that
HTTP handlers resolve.

That skeleton — plus per-workstream metrics tracking, intent-verdict
bookkeeping, and output-warning persistence — lives here. Subclasses
add only kind-specific broadcast (``_broadcast_state`` /
``_broadcast_activity`` override hooks) and any node-level metrics
adapters (``_metrics.record_*`` calls stay on ``WebUI`` since they
feed the node's prometheus endpoint).

This module intentionally does not satisfy
:class:`turnstone.core.session.SessionUI` by itself — some ``on_*``
method bodies (``on_thinking_start``, ``on_state_change``, ``on_rename``)
still require subclass implementation.
"""

from __future__ import annotations

import contextlib
import json
import queue
import threading
import uuid
from typing import Any

from turnstone.core.log import get_logger

log = get_logger(__name__)

# Matches WebUI's historical listener queue size and the coordinator
# UI's ``_LISTENER_QUEUE_MAX``. Per-queue cap keeps a slow SSE consumer
# from bloating memory.
_DEFAULT_LISTENER_QUEUE_MAX = 500


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
        # Verdicts from the LLM intent judge — tracked so
        # ``resolve_approval`` can stamp a ``user_decision`` onto every
        # verdict that fired during this approval round.
        self._pending_verdicts: list[dict[str, Any]] = []
        self._last_verdict_decision: str = ""
        # Verdict cache for SSE reconnect replay (tab switching
        # shouldn't lose the judge's final call on a just-run tool).
        self._llm_verdicts: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Listener plumbing (SSE)
    # ------------------------------------------------------------------

    def _enqueue(self, data: dict[str, Any]) -> None:
        """Fan ``data`` out to every registered listener queue.

        Stamps ``ws_id`` on the payload if not already present so the
        browser can validate it belongs to the pane's current
        workstream. Shallow-copies on stamp to avoid mutating a
        caller-owned dict.
        """
        if "ws_id" not in data:
            data = {**data, "ws_id": self.ws_id}
        with self._listeners_lock:
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

    def resolve_approval(self, approved: bool, feedback: str | None = None) -> None:
        """Unblock a pending approval with the caller's decision.

        Broadcasts ``approval_resolved`` so every connected tab can
        dismiss its prompt modal in sync (e.g. desktop dismisses when
        phone approves). Updates ``user_decision`` on every LLM
        intent-verdict that fired during this approval round — the
        audit trail reflects what the user actually chose.
        """
        decision_str = "approved" if approved else "denied"
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
            }
        )
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

    # ------------------------------------------------------------------
    # Intent-judge + output-guard plumbing
    # ------------------------------------------------------------------

    # Hard cap on the in-memory verdict cache so a long-running session
    # can't grow unbounded. FIFO eviction on insert.
    _LLM_VERDICT_CACHE_MAX = 50

    def on_intent_verdict(self, verdict: dict[str, Any]) -> None:
        """Deliver an LLM intent-judge verdict to the frontend + persist.

        Caches under ``_ws_lock`` for SSE reconnect replay, persists
        the row to storage, and either records the caller's
        ``user_decision`` immediately (if the approval already
        resolved) or parks the verdict in ``_pending_verdicts`` for
        ``resolve_approval`` to stamp on close.
        """
        call_id = verdict.get("call_id", "")
        if call_id:
            with self._ws_lock:
                if (
                    len(self._llm_verdicts) >= self._LLM_VERDICT_CACHE_MAX
                    and call_id not in self._llm_verdicts
                ):
                    oldest_key = next(iter(self._llm_verdicts))
                    del self._llm_verdicts[oldest_key]
                self._llm_verdicts[call_id] = verdict
        self._enqueue({"type": "intent_verdict", **verdict})
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
        with self._ws_lock:
            decision = self._last_verdict_decision
            if not decision:
                self._pending_verdicts.append(verdict)
        if decision:
            self._persist_verdict_decisions([verdict], decision)

    def _persist_intent_verdict(self, verdict: dict[str, Any]) -> None:
        try:
            from turnstone.core.storage._registry import get_storage

            storage = get_storage()
            if storage is None:
                return
            storage.create_intent_verdict(
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
                tier=verdict.get("tier", "llm"),
                judge_model=verdict.get("judge_model", ""),
                latency_ms=verdict.get("latency_ms", 0),
            )
        except Exception:
            log.debug("Failed to persist LLM verdict", exc_info=True)

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
