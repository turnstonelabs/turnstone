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

import contextlib
import queue
import threading
from typing import TYPE_CHECKING, Any

from turnstone.core.log import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

log = get_logger(__name__)

# Per-queue cap keeps a slow SSE consumer from bloating memory.  Matches
# WebUI's listener queue size.
_LISTENER_QUEUE_MAX = 500

# Hard cap on how long a worker thread blocks waiting for an approval /
# plan-review decision.  Exported as a constant so both blocking paths
# stay in lockstep and a future `coordinator.approval_timeout_seconds`
# setting can swap the literal.
_APPROVAL_WAIT_TIMEOUT = 3600


class ConsoleCoordinatorUI:
    """SessionUI for a single coordinator session in the console.

    Thread-safe: the ChatSession worker thread calls the ``on_*`` methods;
    HTTP handlers (``_register_listener`` / ``resolve_*``) run on the
    event loop.  All shared state is guarded by ``_listeners_lock`` or
    threading primitives.
    """

    def __init__(self, ws_id: str = "", user_id: str = "") -> None:
        self.ws_id = ws_id
        self._user_id = user_id
        # SSE listener fan-out — one per connected browser tab.
        self._listeners: list[queue.Queue[dict[str, Any]]] = []
        self._listeners_lock = threading.Lock()
        # External observers for state/rename events — set by the
        # CoordinatorManager on install so the cluster collector's
        # console pseudo-node sees state transitions without the
        # manager needing to wrap the UI methods.  Both callables
        # take a single string (new state / new name); a failing
        # observer is swallowed (see on_state_change / on_rename).
        self._on_state_observer: Callable[[str], None] | None = None
        self._on_rename_observer: Callable[[str], None] | None = None
        # Approval blocking — the worker thread calls approve_tools which
        # waits on _approval_event; the /approve endpoint sets it via
        # resolve_approval.
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
        # Foreground event — compatible with _cleanup_ui's hasattr check.
        self._fg_event = threading.Event()
        self._fg_event.set()

    # ------------------------------------------------------------------
    # Listener plumbing (SSE)
    # ------------------------------------------------------------------

    def _enqueue(self, data: dict[str, Any]) -> None:
        """Fan an event out to all registered SSE listener queues."""
        if "ws_id" not in data:
            data = {**data, "ws_id": self.ws_id}
        with self._listeners_lock:
            snapshot = list(self._listeners)
        for lq in snapshot:
            with contextlib.suppress(queue.Full):
                lq.put_nowait(data)

    def _register_listener(self) -> queue.Queue[dict[str, Any]]:
        """Create and register a per-client queue."""
        client_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=_LISTENER_QUEUE_MAX)
        with self._listeners_lock:
            self._listeners.append(client_queue)
        return client_queue

    def _unregister_listener(self, client_queue: queue.Queue[dict[str, Any]]) -> None:
        with self._listeners_lock, contextlib.suppress(ValueError):
            self._listeners.remove(client_queue)

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

    def resolve_approval(self, approved: bool, feedback: str | None = None) -> None:
        """Called by the POST /v1/api/coordinator/{ws_id}/approve handler."""
        self._approval_result = (approved, feedback)
        self._enqueue(
            {
                "type": "approval_resolved",
                "approved": approved,
                "feedback": feedback or "",
            }
        )
        self._approval_event.set()

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

    def resolve_plan(self, feedback: str) -> None:
        self._plan_result = feedback
        if self._pending_plan_review is None:
            self._plan_event.set()
            return
        self._pending_plan_review = None
        self._enqueue({"type": "plan_resolved", "feedback": feedback})
        self._plan_event.set()

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
        self._enqueue({"type": "state_change", "state": state})
        observer = self._on_state_observer
        if observer is not None:
            try:
                observer(state)
            except Exception:
                log.debug("coord_ui.state_observer_failed ws=%s", self.ws_id, exc_info=True)

    def on_rename(self, name: str) -> None:
        self._enqueue({"type": "rename", "name": name})
        observer = self._on_rename_observer
        if observer is not None:
            try:
                observer(name)
            except Exception:
                log.debug("coord_ui.rename_observer_failed ws=%s", self.ws_id, exc_info=True)

    def on_intent_verdict(self, verdict: dict[str, Any]) -> None:
        # Coordinator sessions use the intent judge like any other session.
        # Surface verdicts to the UI for visibility, but skip the
        # persistence + late-decision plumbing that WebUI does — those
        # are acceptable to defer for v1 and add alongside the broader
        # audit-on-proxy work in a follow-up.
        self._enqueue({"type": "intent_verdict", **verdict})

    def on_output_warning(self, call_id: str, assessment: dict[str, Any]) -> None:
        self._enqueue({"type": "output_warning", "call_id": call_id, **assessment})
