"""SessionUI implementation for console-hosted coordinator workstreams.

Mirrors ``turnstone.server.WebUI`` but scoped to the console's needs:

- Per-session SSE listener fan-out (inherited from
  :class:`SessionUIBase` — same ``threading.Lock`` + queue list
  pattern WebUI uses).
- ``threading.Event`` + ``_approval_result`` / ``_plan_result`` for
  blocking the worker thread until a console endpoint delivers the
  decision (inherited).
- Per-ws metric tracking + turn-content accumulator + activity
  bookkeeping (inherited from :class:`SessionUIBase` post the rich
  ``ws_state`` payload lift). Coord populates the same
  ``_ws_prompt_tokens`` / ``_ws_context_ratio`` /
  ``_ws_current_activity`` fields interactive does, and
  ``coord_adapter.emit_state`` reads them under lock to broadcast
  the rich payload to the cluster collector.
- No global SSE broadcast channel — the console isn't a node and
  has no ``/v1/api/events/global`` analog. State + activity
  broadcasts route through the cluster collector instead
  (``coord_adapter.emit_state`` for state changes;
  :meth:`_broadcast_activity` override for live activity ticks).
- Console-side Prometheus metrics — the console exposes ``/metrics``
  backed by :class:`ConsoleMetrics` (lighter than the per-node
  :class:`MetricsCollector` but the judge-verdict counter is parity
  shape so a cluster-wide PromQL query rolls up coord + interactive
  uniformly). Wired here via the ``_console_metrics`` class attribute
  set at console startup.

Contract: this class must conform to :class:`turnstone.core.session.SessionUI`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from turnstone.core.log import get_logger
from turnstone.core.session_ui_base import SessionUIBase, fire_judge_verdict_metric
from turnstone.core.workstream import WorkstreamState

if TYPE_CHECKING:
    from turnstone.console.collector import ClusterCollector
    from turnstone.console.metrics import ConsoleMetrics
    from turnstone.core.session_manager import SessionManager

log = get_logger(__name__)


class ConsoleCoordinatorUI(SessionUIBase):
    """SessionUI for a single coordinator session in the console.

    Thread-safe: the ChatSession worker thread calls the ``on_*`` methods;
    HTTP handlers (``_register_listener`` / ``resolve_*``) run on the
    event loop.  All shared state is guarded by ``_listeners_lock`` or
    threading primitives.
    """

    # Shared reference to the unified :class:`SessionManager` for
    # coordinator workstreams. Set once at console startup so
    # ``on_state_change`` can flow state transitions through
    # ``mgr.set_state`` (which owns the storage write + adapter
    # emit_state fan-out).  Mirrors ``WebUI._workstream_mgr``.
    _coord_mgr: SessionManager | None = None
    # Shared reference to the :class:`ClusterCollector`. Set at
    # console startup so ``on_rename`` can fan out to the cluster
    # dashboard (the old ``_on_rename_observer`` plumbing went away
    # with CoordinatorManager; this replaces it without reviving the
    # closure-per-install pattern).
    _collector: ClusterCollector | None = None
    # Shared reference to the console's :class:`ConsoleMetrics`
    # instance. Set at console startup so ``_record_judge_metric`` and
    # ``on_intent_verdict`` can fire ``turnstone_judge_verdicts_total``
    # the same way the per-node ``WebUI`` does. ``None`` until the
    # lifespan wires it (and during tests that don't spin up the full
    # console app).
    _console_metrics: ConsoleMetrics | None = None

    # ------------------------------------------------------------------
    # SessionUI protocol — streaming
    #
    # ``on_thinking_start`` / ``on_thinking_stop`` / ``on_reasoning_token``
    # / ``on_content_token`` / ``on_stream_end`` / ``on_tool_output_chunk``
    # / ``on_tool_result`` / ``on_status`` / ``on_info`` / ``on_error``
    # are inherited from :class:`SessionUIBase`. The lifted bodies do
    # the per-ws metric writes the rich ``ws_state`` cluster broadcast
    # reads (``_ws_prompt_tokens`` / ``_ws_context_ratio`` /
    # ``_ws_current_activity`` / ``_ws_turn_content``); the cluster
    # collector then renders coord rows on the dashboard with the same
    # tokens / activity / content fields interactive rows have. Pre-lift
    # coord populated none of these — the dashboard's coord row showed
    # state-only.
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # SessionUI protocol — approvals
    #
    # ``approve_tools`` / ``resolve_approval`` / ``resolve_plan`` are
    # inherited from :class:`SessionUIBase`. The shared body covers
    # tool-policy gating, per-tool auto-approve, blanket auto-approve,
    # heuristic-verdict persistence, and activity tagging the same way
    # interactive sessions get them. ``__budget_override__`` is
    # interactive-only today; the carve-out in the shared body is a
    # no-op on coord (coord workstreams don't have token budgets).
    # ------------------------------------------------------------------

    def on_plan_review(self, content: str) -> str:
        # Coordinator sessions don't fire plan_agent (AGENT_TOOLS is []
        # for coordinator kind) so this path shouldn't normally run.
        # Implemented defensively for SessionUI protocol compatibility.
        self._plan_event.clear()
        self._pending_plan_review = {"type": "plan_review", "content": content}
        self._enqueue(self._pending_plan_review)
        if not self._plan_event.wait(timeout=self._APPROVAL_WAIT_TIMEOUT):
            log.warning("coord_ui.plan_review_timeout ws=%s", self.ws_id)
            self.resolve_plan("reject")
        self._pending_plan_review = None
        return self._plan_result

    # ------------------------------------------------------------------
    # SessionUI protocol — broadcast hook + state change + rename
    # ------------------------------------------------------------------

    def _broadcast_activity(self) -> None:
        """Fan a current-activity snapshot out to the cluster collector.

        Overrides the no-op base hook on :class:`SessionUIBase`. The
        lifted streaming bodies (``on_thinking_start`` /
        ``on_tool_result`` / ``on_stream_end``) call this whenever
        ``_ws_current_activity`` flips, so the cluster dashboard's
        coord rows show live activity transitions between state
        changes the same way interactive rows do. Reads under
        ``_ws_lock`` for snapshot consistency with the worker
        thread's writes; collector failure is logged at debug —
        activity broadcast is observational, never block the worker.

        Dedups back-to-back identical activity ticks against the
        last-emitted ``(activity, activity_state)`` tuple. A
        tool-heavy turn fires many ``on_tool_result`` calls in
        succession that all clear the activity to ``("", "")``;
        without this dedup each one acquires the cluster collector's
        main lock for a no-op write. (Pre-lift coord didn't broadcast
        activity at all, so this is genuinely new contention worth
        gating.) The dedup state is updated **only after a successful
        collector call** — if the collector raises mid-broadcast,
        the next identical tick must retry instead of being silently
        suppressed (otherwise a transient collector failure would
        strand the dashboard's coord row at the pre-failure activity
        until the activity actually changes).
        """
        collector = ConsoleCoordinatorUI._collector
        if collector is None:
            return
        with self._ws_lock:
            activity = self._ws_current_activity
            activity_state = self._ws_activity_state
            current = (activity, activity_state)
            if current == self._last_broadcast_activity:
                return
        try:
            collector.update_console_ws_activity(
                self.ws_id,
                activity=activity,
                activity_state=activity_state,
            )
        except Exception:
            log.debug(
                "coord_ui.activity_fanout_failed ws=%s",
                self.ws_id,
                exc_info=True,
            )
            return
        # Update dedup state only on successful broadcast so a
        # transient collector failure doesn't suppress the next
        # identical tick's retry. Re-acquire the lock briefly —
        # the worker thread is the only writer to
        # ``_last_broadcast_activity``, so the only race is with
        # another concurrent ``_broadcast_activity`` that just
        # took the same snapshot; whichever lands the assignment
        # last wins, and both correspond to the same broadcast
        # tuple anyway.
        with self._ws_lock:
            self._last_broadcast_activity = current

    def _broadcast_intent_verdict(self, verdict: dict[str, Any]) -> None:
        """Fan an LLM intent-judge verdict to the cluster collector.

        Stage 3 Step 5 — overrides the no-op base hook so a coord
        workstream that produces its own verdict (rare — coord agents
        don't typically run the LLM judge) gets a first-class
        cluster-bus event for the parent's tree UI. The far more
        common path is a CHILD workstream firing its verdict on a
        real node; that path goes through ``WebUI._broadcast_intent_verdict``
        → global SSE → collector ``_apply_delta``.
        """
        collector = ConsoleCoordinatorUI._collector
        if collector is None:
            return
        try:
            collector.emit_console_ws_intent_verdict(self.ws_id, verdict)
        except Exception:
            log.debug(
                "coord_ui.intent_verdict_fanout_failed ws=%s",
                self.ws_id,
                exc_info=True,
            )

    def _broadcast_approval_resolved(
        self,
        approved: bool,
        feedback: str | None = None,
        *,
        always: bool = False,
    ) -> None:
        """Fan an ``approval_resolved`` decision to the cluster collector.

        Stage 3 Step 5 — paired with :meth:`_broadcast_intent_verdict`.
        Same rationale: coord-direct approvals are rare; the typical
        path is a child workstream resolving on its node, with that
        node's WebUI broadcasting through the global queue.
        """
        collector = ConsoleCoordinatorUI._collector
        if collector is None:
            return
        try:
            collector.emit_console_ws_approval_resolved(
                self.ws_id,
                approved=approved,
                feedback=feedback or "",
                always=always,
            )
        except Exception:
            log.debug(
                "coord_ui.approval_resolved_fanout_failed ws=%s",
                self.ws_id,
                exc_info=True,
            )

    def _broadcast_approve_request(self, detail: dict[str, Any]) -> None:
        """Fan an ``approve_request`` payload to the cluster collector.

        Push path for the initial approval items. Same rationale as
        the other two broadcast hooks: coord-self approvals are rare
        (the LLM judge isn't wired on the console coord today), but
        the override exists for symmetry and lights up the same path
        a future coord-self gate would use.
        """
        collector = ConsoleCoordinatorUI._collector
        if collector is None:
            return
        try:
            collector.emit_console_ws_approve_request(self.ws_id, detail)
        except Exception:
            log.debug(
                "coord_ui.approve_request_fanout_failed ws=%s",
                self.ws_id,
                exc_info=True,
            )

    def on_state_change(self, state: str) -> None:
        # Flow state transitions through the unified SessionManager so
        # the storage write + adapter emit_state fan-out stay in lockstep
        # with set_state() callers elsewhere. Mirrors WebUI's pattern.
        if ConsoleCoordinatorUI._coord_mgr is not None:
            try:
                ws_state = WorkstreamState(state)
            except ValueError:
                log.debug("coord_ui.unknown_state state=%r ws=%s", state, self.ws_id)
            else:
                try:
                    ConsoleCoordinatorUI._coord_mgr.set_state(self.ws_id, ws_state)
                except Exception:
                    log.debug(
                        "coord_ui.set_state_failed ws=%s",
                        self.ws_id,
                        exc_info=True,
                    )
        self._enqueue({"type": "state_change", "state": state})

    def on_rename(self, name: str) -> None:
        self._enqueue({"type": "rename", "name": name})
        # Fan out to the cluster collector so the dashboard's coord
        # row updates live. Previously routed through an
        # ``_on_rename_observer`` closure the old CoordinatorManager
        # installed; now the UI reaches the collector directly via a
        # class attribute set at console startup. None during tests
        # that don't spin up a real collector.
        collector = ConsoleCoordinatorUI._collector
        if collector is not None:
            try:
                collector.emit_console_ws_rename(self.ws_id, name)
            except Exception:
                log.debug(
                    "coord_ui.rename_fanout_failed ws=%s",
                    self.ws_id,
                    exc_info=True,
                )

    # ``on_output_warning`` inherited from :class:`SessionUIBase`.
    # Coordinator sessions persist verdicts and output assessments to
    # storage alongside the interactive path.

    # ------------------------------------------------------------------
    # Prometheus metric hooks — fire ``turnstone_judge_verdicts_total``
    # against the console's :class:`ConsoleMetrics` instance so the
    # console's /metrics endpoint surfaces coord verdicts the same way
    # the per-node /metrics surfaces interactive ones. Two call sites:
    #
    # - :meth:`_record_judge_metric` — heuristic tier, fired from the
    #   shared ``approve_tools`` body during the synchronous
    #   approval gate.
    # - :meth:`on_intent_verdict` — LLM tier, fired by the daemon
    #   judge thread asynchronously.
    #
    # Both end up at ``record_judge_verdict(tier, risk, latency_ms)`` —
    # mirrors ``WebUI``'s pattern at ``server.py``. ``None`` guard
    # covers the test-fixture case where the console lifespan didn't
    # wire the class attribute.
    # ------------------------------------------------------------------

    def _record_judge_metric(self, verdict: dict[str, Any]) -> None:
        cm = ConsoleCoordinatorUI._console_metrics
        if cm is None:
            return
        fire_judge_verdict_metric(cm, verdict, "heuristic")

    def on_intent_verdict(self, verdict: dict[str, Any]) -> None:
        super().on_intent_verdict(verdict)
        cm = ConsoleCoordinatorUI._console_metrics
        if cm is None:
            return
        fire_judge_verdict_metric(cm, verdict, "llm")
