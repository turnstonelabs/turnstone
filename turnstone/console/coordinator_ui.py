"""SessionUI implementation for console-hosted coordinator workstreams.

Mirrors ``turnstone.server.WebUI`` but scoped to the console's needs:

- Per-session SSE listener fan-out (same ``threading.Lock`` + queue list
  pattern as WebUI).
- ``threading.Event`` + ``_approval_result`` / ``_plan_result`` for
  blocking the worker thread until a console endpoint delivers the
  decision.
- No global broadcast channel and no per-node metrics — the console is
  not a node.  Dashboard aggregation for coordinator sessions lands in
  Phase D; here we only emit events the one-pane UI consumes.

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
    # ------------------------------------------------------------------

    def on_thinking_start(self) -> None:
        self._enqueue({"type": "thinking_start"})

    def on_thinking_stop(self) -> None:
        self._enqueue({"type": "thinking_stop"})

    def on_reasoning_token(self, text: str) -> None:
        self._enqueue({"type": "reasoning", "text": text})

    def on_content_token(self, text: str) -> None:
        self._enqueue({"type": "content", "text": text})

    def on_stream_end(self) -> None:
        self._enqueue({"type": "stream_end"})

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
    # SessionUI protocol — tool results + status + misc
    # ------------------------------------------------------------------

    def on_tool_result(
        self,
        call_id: str,
        name: str,
        output: str,
        *,
        is_error: bool = False,
    ) -> None:
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
        total = usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)
        pct = round(total / context_window * 100, 1) if context_window > 0 else 0
        self._enqueue(
            {
                "type": "status",
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
                "total_tokens": total,
                "context_window": context_window,
                "pct": pct,
                "effort": effort,
                "cache_creation_tokens": usage.get("cache_creation_tokens", 0),
                "cache_read_tokens": usage.get("cache_read_tokens", 0),
            }
        )

    def on_info(self, message: str) -> None:
        self._enqueue({"type": "info", "message": message})

    def on_error(self, message: str) -> None:
        self._enqueue({"type": "error", "message": message})

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
