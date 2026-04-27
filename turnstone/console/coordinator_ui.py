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
- No per-node Prometheus metrics — the console has no /metrics
  endpoint. WebUI's ``_metrics.record_*`` calls don't apply here.

Contract: this class must conform to :class:`turnstone.core.session.SessionUI`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from turnstone.core.log import get_logger
from turnstone.core.session_ui_base import SessionUIBase
from turnstone.core.workstream import WorkstreamState

if TYPE_CHECKING:
    from turnstone.console.collector import ClusterCollector
    from turnstone.core.session_manager import SessionManager

log = get_logger(__name__)

# Hard cap on how long a worker thread blocks waiting for an approval /
# plan-review decision.  Exported as a constant so both blocking paths
# stay in lockstep and a future `coordinator.approval_timeout_seconds`
# setting can swap the literal.
_APPROVAL_WAIT_TIMEOUT = 3600


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
    # ------------------------------------------------------------------

    def approve_tools(self, items: list[dict[str, Any]]) -> tuple[bool, str | None]:
        self._reset_approval_cycle()
        pending = [it for it in items if it.get("needs_approval") and not it.get("error")]

        serialized = []
        for item in items:
            entry: dict[str, Any] = {
                "call_id": item.get("call_id", ""),
                "header": item.get("header", ""),
                "preview": item.get("preview", ""),
                "func_name": item.get("func_name", ""),
                "approval_label": item.get("approval_label", item.get("func_name", "")),
                "needs_approval": item.get("needs_approval", False),
                "error": item.get("error"),
            }
            serialized.append(entry)

        if not pending:
            # Nothing to approve; broadcast tool info anyway so the UI
            # can render the tool preview.
            if serialized:
                self._enqueue({"type": "tools_auto_approved", "items": serialized})
            return True, None

        # Per-tool auto-approve: 'Always approve this tool' adds the
        # tool name to ``auto_approve_tools``.  This must short-circuit
        # independently of the blanket ``auto_approve`` flag — matches
        # the WebUI two-tier contract (turnstone/server.py).
        if self.auto_approve_tools:
            pending_names = {it.get("func_name", "") for it in pending if it.get("func_name")}
            if pending_names and pending_names.issubset(self.auto_approve_tools):
                self._enqueue({"type": "tools_auto_approved", "items": serialized})
                return True, None

        # Blanket auto-approve (set e.g. during scripted
        # restart-rehydration) — also matches WebUI semantics.
        if self.auto_approve:
            self._enqueue({"type": "tools_auto_approved", "items": serialized})
            return True, None

        self._approval_event.clear()
        self._pending_approval = {
            "type": "approve_request",
            "items": serialized,
            "judge_pending": False,
        }
        self._enqueue(self._pending_approval)
        if not self._approval_event.wait(timeout=_APPROVAL_WAIT_TIMEOUT):
            log.warning("coord_ui.approval_timeout ws=%s", self.ws_id)
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

    # ``resolve_approval`` inherited from :class:`SessionUIBase`.

    def on_plan_review(self, content: str) -> str:
        # Coordinator sessions don't fire plan_agent (AGENT_TOOLS is []
        # for coordinator kind) so this path shouldn't normally run.
        # Implemented defensively for SessionUI protocol compatibility.
        self._plan_event.clear()
        self._pending_plan_review = {"type": "plan_review", "content": content}
        self._enqueue(self._pending_plan_review)
        if not self._plan_event.wait(timeout=_APPROVAL_WAIT_TIMEOUT):
            log.warning("coord_ui.plan_review_timeout ws=%s", self.ws_id)
            self.resolve_plan("reject")
        self._pending_plan_review = None
        return self._plan_result

    # ``resolve_plan`` inherited from :class:`SessionUIBase`.

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

    # ``on_intent_verdict`` and ``on_output_warning`` inherited from
    # :class:`SessionUIBase`. Coordinator sessions now persist verdicts
    # and output assessments to storage alongside the interactive path
    # (the "skip the persistence" deferral note has been retired).
