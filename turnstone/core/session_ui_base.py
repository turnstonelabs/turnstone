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

import contextlib
import copy
import json
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

# Cap on the per-turn assistant content accumulator. The accumulator
# is piggybacked onto the ``ws_state:idle`` broadcast payload so the
# cluster collector / dashboard can render the freshly-emitted assistant
# turn without round-tripping storage; capping it keeps a runaway turn
# from ballooning the broadcast event past the listener queues' size
# budget. Lifted from WebUI in the rich ``ws_state`` payload work so
# coord broadcasts hit the same ceiling.
_MAX_TURN_CONTENT_CHARS = 256 * 1024


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
        self._ws_turn_content: list[str] = []
        self._ws_turn_content_size: int = 0
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

    def resolve_approval(
        self,
        approved: bool,
        feedback: str | None = None,
        *,
        always: bool = False,
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
                "always": bool(always),
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
                                # /dashboard + audit.
                                self._record_auto_approves(items)
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
            self._record_auto_approves(items)
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
        heuristic_verdicts: list[dict[str, Any]] = []
        for item in items:
            hv = item.get("_heuristic_verdict")
            if hv:
                heuristic_verdicts.append(hv)
                # Subclass-overridden Prometheus surface: WebUI feeds
                # the per-node /metrics endpoint, ConsoleCoordinatorUI
                # feeds the console's /metrics endpoint via ConsoleMetrics.
                self._record_judge_metric(hv)
        self._persist_intent_verdicts_bulk(heuristic_verdicts, default_tier="heuristic")

        with self._ws_lock:
            self._pending_verdicts = heuristic_verdicts

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
        if not self._approval_event.wait(timeout=self._APPROVAL_WAIT_TIMEOUT):
            # Approval timed out (e.g., user disconnected). Deny via
            # resolve_approval so verdicts and state are updated consistently.
            log.warning("Approval timed out for ws_id=%s", self.ws_id)
            self.resolve_approval(False, "Approval timed out after 1 hour")
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
                }
                for v in verdicts
            ]
            storage.create_intent_verdicts_bulk(rows)
        except Exception:
            log.debug("Failed to bulk-persist intent verdicts", exc_info=True)

    def _persist_intent_verdict(
        self,
        verdict: dict[str, Any],
        *,
        default_tier: str = "llm",
    ) -> None:
        """Persist an intent-judge verdict row.

        Used by both the async LLM-tier path (``on_intent_verdict``,
        default tier ``"llm"``) and the synchronous heuristic-tier
        path (``approve_tools``, caller passes ``default_tier="heuristic"``).
        ``default_tier`` only matters when the verdict dict doesn't
        already carry a ``tier`` key — both real producers always set it,
        but the fallback is the right call-site label so a malformed
        verdict still lands on the correct row classification.
        """
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
                tier=verdict.get("tier", default_tier),
                judge_model=verdict.get("judge_model", ""),
                latency_ms=verdict.get("latency_ms", 0),
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
        self._enqueue({"type": "reasoning", "text": text})

    def on_content_token(self, text: str) -> None:
        """Append to the turn-content accumulator (capped) + enqueue.

        The cap-check + append + size-update run under ``_ws_lock``
        so a concurrent :meth:`snapshot_and_consume_state_payload`
        IDLE/ERROR drain can't see a torn list mid-append. In
        production this is single-writer-per-ws (the worker thread)
        but the snapshot reader runs from coord's adapter via
        ``mgr.set_state``; without the lock the writer's append
        could land in an orphaned list reference the snapshot just
        swapped out. Lock hold is microseconds.
        """
        with self._ws_lock:
            if self._ws_turn_content_size < _MAX_TURN_CONTENT_CHARS:
                self._ws_turn_content.append(text)
                self._ws_turn_content_size += len(text)
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

    def on_user_reminder(self, reminders: list[dict[str, str]]) -> None:
        """Surface a metacognitive nudge as its own UI element.

        Reminders live on the user message dict's ``_reminders``
        side-channel and are spliced into ``content`` only at the
        provider boundary; this event is what lets every connected
        SSE consumer (other browser tabs, CLI mirrors, future channel
        adapters) render the reminder bubble in lockstep with the
        originating tab.  The history-replay path surfaces the same
        shape via ``_build_history`` so a tab reconnecting later
        renders the same bubble.
        """
        self._enqueue({"type": "user_reminder", "reminders": reminders})

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
            elif state == "error":
                self._ws_turn_content = []
                self._ws_turn_content_size = 0
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
