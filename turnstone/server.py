"""Web server frontend for turnstone.

Provides a browser-based chat UI that mirrors the terminal CLI experience.
Uses Starlette (ASGI) with uvicorn for the server, communicating with the
browser via Server-Sent Events (SSE) for streaming and HTTP POST for user
actions.

Supports multiple concurrent workstreams (tabs), each with independent
ChatSession and event streams.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import functools
import json
import os
import queue
import sys
import textwrap
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sse_starlette import EventSourceResponse
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from turnstone import __version__
from turnstone.api.docs import make_docs_handler, make_openapi_handler
from turnstone.api.server_spec import build_server_spec
from turnstone.core.auth import JWT_AUD_SERVER, AuthMiddleware
from turnstone.core.log import get_logger
from turnstone.core.metrics import metrics as _metrics
from turnstone.core.ratelimit import resolve_client_ip
from turnstone.core.session import ChatSession, GenerationCancelled, SessionUI  # noqa: F401
from turnstone.core.tools import TOOLS  # noqa: F401 — available for introspection
from turnstone.core.workstream import Workstream, WorkstreamManager, WorkstreamState

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, MutableMapping

    from starlette.types import ASGIApp, Receive, Scope, Send

# ---------------------------------------------------------------------------
# Static assets — loaded once at startup from turnstone/ui/static/
# ---------------------------------------------------------------------------

log = get_logger(__name__)

_STATIC_DIR = Path(__file__).parent / "ui" / "static"
_SHARED_DIR = Path(__file__).parent / "shared_static"
_HTML = (_STATIC_DIR / "index.html").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# WebUI — implements SessionUI for browser-based interaction
# ---------------------------------------------------------------------------

_MAX_TURN_CONTENT_CHARS = 256 * 1024  # cap piggybacked content on idle events


class WebUI:
    """Browser-based UI using SSE for streaming and HTTP POST for actions.

    Implements the SessionUI protocol from turnstone.core.session.
    Each workstream gets its own WebUI instance.
    """

    # Shared global event queue for state-change broadcasts across all
    # workstreams.  Set by main() before any WebUI instances are created.
    _global_queue: queue.Queue[dict[str, Any]] | None = None  # bounded in main()
    _workstream_mgr: WorkstreamManager | None = None

    def __init__(self, ws_id: str = "", user_id: str = "") -> None:
        self.ws_id = ws_id
        self._user_id = user_id
        self._listeners: list[queue.Queue[dict[str, Any]]] = []
        self._listeners_lock = threading.Lock()
        self._approval_event = threading.Event()
        self._approval_result: tuple[bool, str | None] = (False, None)
        self._pending_approval: dict[str, Any] | None = None  # re-sent on SSE reconnect
        self._plan_event = threading.Event()
        self._plan_result: str = ""
        self.auto_approve = False
        self.auto_approve_tools: set[str] = set()
        # Per-workstream metrics accumulators (written by worker thread, read by metrics handler)
        self._ws_lock = threading.Lock()
        self._ws_prompt_tokens: int = 0
        self._ws_completion_tokens: int = 0
        self._ws_messages: int = 0
        self._ws_tool_calls: dict[str, int] = {}
        self._ws_tool_calls_reported: int = 0  # last cumulative total sent to usage
        self._ws_context_ratio: float = 0.0
        # Activity tracking for dashboard (current tool / thinking / approval)
        self._ws_current_activity: str = ""
        self._ws_activity_state: str = ""  # "tool" | "approval" | "thinking" | ""
        # Verdicts awaiting user_decision update on approval resolution
        self._pending_verdicts: list[dict[str, Any]] = []
        # Last user decision for late-arriving verdicts (set in resolve_approval)
        self._last_verdict_decision: str = ""
        # Content accumulator — tokens appended in on_content_token(), joined
        # and piggybacked onto the ws_state:idle global SSE event, then reset.
        self._ws_turn_content: list[str] = []
        self._ws_turn_content_size: int = 0

    def _enqueue(self, data: dict[str, Any]) -> None:
        with self._listeners_lock:
            snapshot = list(self._listeners)
        for lq in snapshot:
            with contextlib.suppress(queue.Full):
                lq.put_nowait(data)

    def _register_listener(self) -> queue.Queue[dict[str, Any]]:
        """Create a per-client queue and register it as a listener."""
        client_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=500)
        with self._listeners_lock:
            self._listeners.append(client_queue)
        return client_queue

    def _unregister_listener(self, client_queue: queue.Queue[dict[str, Any]]) -> None:
        """Remove a client queue from the listeners list."""
        with self._listeners_lock, contextlib.suppress(ValueError):
            self._listeners.remove(client_queue)

    def _broadcast_state(self, state: str) -> None:
        """Send a state-change event to the global SSE channel."""
        if WebUI._global_queue is not None:
            with self._ws_lock:
                tokens = self._ws_prompt_tokens + self._ws_completion_tokens
                ctx = self._ws_context_ratio
                activity = self._ws_current_activity
                activity_state = self._ws_activity_state
            event: dict[str, Any] = {
                "type": "ws_state",
                "ws_id": self.ws_id,
                "state": state,
                "tokens": tokens,
                "context_ratio": ctx,
                "activity": activity,
                "activity_state": activity_state,
            }
            if state == "idle":
                event["content"] = "".join(self._ws_turn_content)
                self._ws_turn_content = []
                self._ws_turn_content_size = 0
            elif state == "error":
                self._ws_turn_content = []
                self._ws_turn_content_size = 0
            try:
                WebUI._global_queue.put_nowait(event)
            except queue.Full:
                log.debug("Global SSE queue full, dropping %s event", event.get("type"))

    def _broadcast_activity(self) -> None:
        """Send an activity-change event to the global SSE channel."""
        if WebUI._global_queue is not None:
            with self._ws_lock:
                activity = self._ws_current_activity
                activity_state = self._ws_activity_state
            with contextlib.suppress(queue.Full):
                WebUI._global_queue.put_nowait(
                    {
                        "type": "ws_activity",
                        "ws_id": self.ws_id,
                        "activity": activity,
                        "activity_state": activity_state,
                    }
                )

    # --- SessionUI protocol ---

    def on_thinking_start(self) -> None:
        with self._ws_lock:
            self._ws_current_activity = "Thinking\u2026"
            self._ws_activity_state = "thinking"
        self._broadcast_activity()
        self._enqueue({"type": "thinking_start"})

    def on_thinking_stop(self) -> None:
        self._enqueue({"type": "thinking_stop"})

    def on_reasoning_token(self, text: str) -> None:
        self._enqueue({"type": "reasoning", "text": text})

    def on_content_token(self, text: str) -> None:
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

    def approve_tools(self, items: list[dict[str, Any]]) -> tuple[bool, str | None]:
        self._last_verdict_decision = ""  # reset for new approval cycle
        pending = [it for it in items if it.get("needs_approval") and not it.get("error")]

        # Always send tool info to the browser
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
            if "_heuristic_verdict" in item:
                entry["verdict"] = item["_heuristic_verdict"]
            serialized.append(entry)

        # -- Tool policy evaluation -----------------------------------------------
        # Check admin-defined tool policies before the auto_approve check.
        if pending:
            try:
                from turnstone.core.policy import evaluate_tool_policies_batch
                from turnstone.core.storage._registry import get_storage

                storage = get_storage()
                if storage is not None:
                    tool_names = [
                        it.get("approval_label", "") or it.get("func_name", "")
                        for it in pending
                        if it.get("func_name")
                    ]
                    if tool_names:
                        verdicts = evaluate_tool_policies_batch(storage, tool_names)
                        still_pending = []
                        for it in pending:
                            policy_name = it.get("approval_label", "") or it.get("func_name", "")
                            verdict = verdicts.get(policy_name)
                            if verdict == "deny":
                                it["denied"] = True
                                it["denial_msg"] = (
                                    f"Blocked by tool policy (pattern match for '{policy_name}')"
                                )
                            elif verdict == "allow":
                                it["needs_approval"] = False
                            else:
                                still_pending.append(it)
                        # Rebuild serialized to reflect policy verdicts
                        serialized = []
                        for it in items:
                            rebuilt: dict[str, Any] = {
                                "call_id": it.get("call_id", ""),
                                "header": it.get("header", ""),
                                "preview": it.get("preview", ""),
                                "func_name": it.get("func_name", ""),
                                "approval_label": it.get("approval_label", it.get("func_name", "")),
                                "needs_approval": it.get("needs_approval", False),
                                "error": it.get("denial_msg") if it.get("denied") else None,
                            }
                            if "_heuristic_verdict" in it:
                                rebuilt["verdict"] = it["_heuristic_verdict"]
                            serialized.append(rebuilt)
                        # If all were resolved by policy, check if any were denied
                        if not still_pending:
                            any_denied = any(it.get("denied") for it in items)
                            if any_denied:
                                self._enqueue({"type": "tool_info", "items": serialized})
                                return False, "Blocked by tool policy"
                        pending = still_pending
            except Exception:
                log.debug("Tool policy evaluation failed", exc_info=True)
        # -- End tool policy evaluation -------------------------------------------

        # Per-tool auto-approve check (from workstream template or interactive "Always")
        if pending and self.auto_approve_tools:
            pending_names = {
                it.get("approval_label", "") or it.get("func_name", "")
                for it in pending
                if it.get("func_name")
            }
            if pending_names and pending_names.issubset(self.auto_approve_tools):
                pending = []

        # Budget override requires explicit approval — never auto-approved by
        # blanket auto_approve (tool policies can still allow it explicitly).
        has_budget_override = any(it.get("func_name") == "__budget_override__" for it in pending)
        if not pending or (self.auto_approve and not has_budget_override):
            # Track auto-approved tool activity
            first = items[0] if items else {}
            label = first.get("func_name", "")
            preview = first.get("preview", "")[:80]
            with self._ws_lock:
                self._ws_current_activity = f"\u2699 {label}: {preview}" if label else ""
                self._ws_activity_state = "tool" if label else ""
            self._broadcast_activity()
            self._enqueue({"type": "tool_info", "items": serialized})
            return True, None

        # Track pending approval activity
        first_pending = pending[0]
        label = first_pending.get("func_name", "")
        preview = first_pending.get("preview", "")[:60]
        with self._ws_lock:
            self._ws_current_activity = f"\u23f3 Awaiting approval: {label} \u2014 {preview}"
            self._ws_activity_state = "approval"
        self._broadcast_activity()

        # Persist heuristic verdicts and track for user_decision update.
        # Build list locally, then assign under lock to avoid racing with
        # the judge daemon thread's on_intent_verdict() appends.
        heuristic_verdicts: list[dict[str, Any]] = []
        for item in items:
            hv = item.get("_heuristic_verdict")
            if hv:
                heuristic_verdicts.append(hv)
                try:
                    from turnstone.core.storage._registry import get_storage

                    storage = get_storage()
                    if storage is not None:
                        storage.create_intent_verdict(
                            verdict_id=hv.get("verdict_id", ""),
                            ws_id=self.ws_id,
                            call_id=hv.get("call_id", ""),
                            func_name=hv.get("func_name", ""),
                            func_args=hv.get("func_args", ""),
                            intent_summary=hv.get("intent_summary", ""),
                            risk_level=hv.get("risk_level", "medium"),
                            confidence=hv.get("confidence", 0.5),
                            recommendation=hv.get("recommendation", "review"),
                            reasoning=hv.get("reasoning", ""),
                            evidence=json.dumps(hv.get("evidence", [])),
                            tier=hv.get("tier", "heuristic"),
                            judge_model=hv.get("judge_model", ""),
                            latency_ms=hv.get("latency_ms", 0),
                        )
                except Exception:
                    log.debug("Failed to persist heuristic verdict", exc_info=True)
                _metrics.record_judge_verdict(
                    hv.get("tier", "heuristic"),
                    hv.get("risk_level", "medium"),
                    hv.get("latency_ms", 0),
                )

        with self._ws_lock:
            self._pending_verdicts = heuristic_verdicts

        # Send approval request and block
        judge_pending = bool(any(it.get("_heuristic_verdict") for it in items))
        self._approval_event.clear()
        self._pending_approval = {
            "type": "approve_request",
            "items": serialized,
            "judge_pending": judge_pending,
        }
        self._enqueue(self._pending_approval)
        if not self._approval_event.wait(timeout=3600):
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

    def on_tool_result(self, call_id: str, name: str, output: str) -> None:
        _metrics.record_tool_call(name)
        with self._ws_lock:
            self._ws_tool_calls[name] = self._ws_tool_calls.get(name, 0) + 1
            self._ws_current_activity = ""
            self._ws_activity_state = ""
        self._broadcast_activity()
        self._enqueue({"type": "tool_result", "call_id": call_id, "name": name, "output": output})

    def on_tool_output_chunk(self, call_id: str, chunk: str) -> None:
        self._enqueue({"type": "tool_output_chunk", "call_id": call_id, "chunk": chunk})

    def on_status(self, usage: dict[str, Any], context_window: int, effort: str) -> None:
        total_tok = usage["prompt_tokens"] + usage["completion_tokens"]
        pct = total_tok / context_window * 100 if context_window > 0 else 0
        cache_creation = usage.get("cache_creation_tokens", 0)
        cache_read = usage.get("cache_read_tokens", 0)
        _metrics.record_tokens(usage["prompt_tokens"], usage["completion_tokens"])
        _metrics.record_cache_tokens(cache_creation, cache_read)
        _metrics.record_context_ratio(total_tok / context_window if context_window > 0 else 0.0)
        with self._ws_lock:
            self._ws_prompt_tokens += usage["prompt_tokens"]
            self._ws_completion_tokens += usage["completion_tokens"]
            self._ws_context_ratio = total_tok / context_window if context_window > 0 else 0.0
            tool_total = sum(self._ws_tool_calls.values())
            tool_count = tool_total - self._ws_tool_calls_reported
            self._ws_tool_calls_reported = tool_total
        self._enqueue(
            {
                "type": "status",
                "prompt_tokens": usage["prompt_tokens"],
                "completion_tokens": usage["completion_tokens"],
                "total_tokens": total_tok,
                "context_window": context_window,
                "pct": round(pct, 1),
                "effort": effort,
                "cache_creation_tokens": cache_creation,
                "cache_read_tokens": cache_read,
            }
        )
        # Record usage event for governance dashboard
        try:
            from turnstone.core.storage._registry import get_storage

            storage = get_storage()
            if storage is not None:
                import uuid

                storage.record_usage_event(
                    event_id=uuid.uuid4().hex,
                    user_id=self._user_id,
                    ws_id=self.ws_id,
                    node_id="",
                    model=usage.get("model", ""),
                    prompt_tokens=usage["prompt_tokens"],
                    completion_tokens=usage["completion_tokens"],
                    tool_calls_count=tool_count,
                    cache_creation_tokens=cache_creation,
                    cache_read_tokens=cache_read,
                )
        except Exception:
            log.warning("Failed to record usage event", exc_info=True)

    def on_plan_review(self, content: str) -> str:
        self._plan_event.clear()
        self._enqueue({"type": "plan_review", "content": content})
        if not self._plan_event.wait(timeout=3600):
            log.warning("Plan review timed out for ws_id=%s", self.ws_id)
            self._plan_result = ""
        return self._plan_result

    def on_info(self, message: str) -> None:
        self._enqueue({"type": "info", "message": message})

    def on_error(self, message: str) -> None:
        _metrics.record_error()
        self._enqueue({"type": "error", "message": message})

    def on_state_change(self, state: str) -> None:
        # Update the Workstream object so dashboard/polling sees the new state
        if WebUI._workstream_mgr is not None:
            try:
                ws_state = WorkstreamState(state)
            except ValueError:
                log.debug("Ignoring unknown state %r for ws %s", state, self.ws_id)
            else:
                WebUI._workstream_mgr.set_state(self.ws_id, ws_state)
        self._broadcast_state(state)

    def on_rename(self, name: str) -> None:
        """Update the workstream's display name and broadcast to all clients."""
        if WebUI._global_queue is not None:
            with contextlib.suppress(queue.Full):
                WebUI._global_queue.put_nowait(
                    {"type": "ws_rename", "ws_id": self.ws_id, "name": name}
                )

    def on_intent_verdict(self, verdict: dict[str, Any]) -> None:
        """Deliver LLM judge verdict to frontend via SSE."""
        self._enqueue({"type": "intent_verdict", **verdict})
        # Persist the LLM verdict (fire-and-forget)
        try:
            from turnstone.core.storage._registry import get_storage

            storage = get_storage()
            if storage is not None:
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
        _metrics.record_judge_verdict(
            verdict.get("tier", "llm"),
            verdict.get("risk_level", "medium"),
            verdict.get("latency_ms", 0),
        )
        # If approval already resolved, update user_decision immediately.
        # Read decision under lock to avoid racing with resolve_approval().
        with self._ws_lock:
            decision = self._last_verdict_decision
        if decision:
            try:
                from turnstone.core.storage._registry import get_storage

                storage = get_storage()
                if storage is not None:
                    storage.update_intent_verdict(
                        verdict.get("verdict_id", ""), user_decision=decision
                    )
            except Exception:
                log.debug("Failed to update late verdict user_decision", exc_info=True)
        else:
            with self._ws_lock:
                self._pending_verdicts.append(verdict)

    def on_output_warning(self, call_id: str, assessment: dict[str, Any]) -> None:
        """Deliver output guard warning to frontend via SSE + persist."""
        self._enqueue({"type": "output_warning", "call_id": call_id, **assessment})
        # Fire-and-forget persistence
        try:
            from turnstone.core.storage._registry import get_storage

            storage = get_storage()
            if storage is not None:
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

    def resolve_approval(self, approved: bool, feedback: str | None = None) -> None:
        """Resolve a pending approval, whether triggered by the HTTP handler
        (user approves/denies in the browser) or by server-initiated flows
        such as cancellations or timeouts."""
        self._approval_result = (approved, feedback)
        self._enqueue(
            {
                "type": "approval_resolved",
                "approved": approved,
                "feedback": feedback or "",
            }
        )
        # Update user_decision on all tracked verdicts (fire-and-forget).
        # Swap-and-clear + set decision under lock to avoid racing with
        # the daemon judge thread's on_intent_verdict() appends.
        decision_str = "approved" if approved else "denied"
        with self._ws_lock:
            pending = self._pending_verdicts
            self._pending_verdicts = []
            self._last_verdict_decision = decision_str
        if pending:
            try:
                from turnstone.core.storage._registry import get_storage

                storage = get_storage()
                if storage is not None:
                    for v in pending:
                        vid = v.get("verdict_id", "")
                        if vid:
                            storage.update_intent_verdict(vid, user_decision=decision_str)
            except Exception:
                log.debug("Failed to update verdict user_decision", exc_info=True)
        self._approval_event.set()

    def resolve_plan(self, feedback: str) -> None:
        """Called by the HTTP handler when the user responds to a plan."""
        self._plan_result = feedback
        self._plan_event.set()


# ---------------------------------------------------------------------------
# History builder
# ---------------------------------------------------------------------------


def _build_history(
    session: ChatSession, has_pending_approval: bool = False
) -> list[dict[str, Any]]:
    """Build a history replay list from ChatSession messages.

    When ``has_pending_approval`` is True, the last assistant entry's
    tool_calls are marked ``"pending": True`` so the client renders them
    as awaiting approval rather than as already-approved.

    Tool results whose content starts with "Denied by user" are marked
    ``"denied": True``, and the corresponding assistant entry that
    issued the tool calls is also marked ``"denied": True`` so the
    client can render the correct badge.
    """
    history = []
    for msg in session.messages:
        entry = {"role": msg["role"], "content": msg.get("content")}
        if msg.get("tool_calls"):
            entry["tool_calls"] = [
                {
                    "id": tc.get("id", ""),
                    "name": tc["function"]["name"],
                    "arguments": tc["function"].get("arguments", ""),
                }
                for tc in msg["tool_calls"]
            ]
        # Detect denied/blocked tool results by their content prefix.
        if msg.get("role") == "tool":
            content = msg.get("content", "")
            if isinstance(content, str) and (
                content.startswith("Denied by user") or content.startswith("Blocked")
            ):
                entry["denied"] = True
        history.append(entry)

    # Propagate denial from tool results to their parent assistant entry.
    last_assistant_idx: int | None = None
    for idx, entry in enumerate(history):
        if entry.get("tool_calls"):
            last_assistant_idx = idx
        elif entry.get("role") == "tool" and entry.get("denied") and last_assistant_idx is not None:
            history[last_assistant_idx]["denied"] = True

    # Mark last assistant tool call as pending if approval is outstanding.
    if has_pending_approval:
        for entry in reversed(history):
            if entry.get("tool_calls"):
                entry["pending"] = True
                break
    return history


# ---------------------------------------------------------------------------
# Pure ASGI middleware (NOT BaseHTTPMiddleware — that breaks SSE streaming)
# ---------------------------------------------------------------------------


class RateLimitMiddleware:
    """Per-IP token-bucket rate limiting."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        if request.method == "OPTIONS":
            await self.app(scope, receive, send)
            return
        limiter = getattr(request.app.state, "rate_limiter", None)
        if limiter is None:
            await self.app(scope, receive, send)
            return
        if not request.client:
            # No peer address — cannot enforce per-IP limit; pass through
            await self.app(scope, receive, send)
            return
        client_ip = request.client.host
        xff = request.headers.get("X-Forwarded-For", "")
        client_ip = resolve_client_ip(client_ip, xff, limiter.trusted_proxies)
        path = request.url.path
        allowed, retry_after = limiter.check(client_ip, path)
        if not allowed:
            _metrics.record_ratelimit_reject()
            response = JSONResponse(
                {"error": "Rate limit exceeded", "retry_after": round(retry_after, 1)},
                status_code=429,
                headers={"Retry-After": str(int(retry_after) + 1)},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


class MetricsMiddleware:
    """Record request method, path, status, and latency."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        t0 = time.monotonic()
        status_code = 500
        original_send = send

        async def capture_send(message: MutableMapping[str, Any]) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await original_send(message)

        request = Request(scope)
        try:
            await self.app(scope, receive, capture_send)
        finally:
            _metrics.record_request(
                request.method, request.url.path, status_code, time.monotonic() - t0
            )


class LogContextMiddleware:
    """Set structlog context variables (request_id, ws_id) per request."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        import structlog

        from turnstone.core.log import ctx_request_id, ctx_ws_id

        rid = uuid.uuid4().hex[:8]
        tok_rid = ctx_request_id.set(rid)
        # Extract ws_id from query params if present
        request = Request(scope)
        ws_id = request.query_params.get("ws_id", "")
        tok_ws = ctx_ws_id.set(ws_id) if ws_id else None
        try:
            await self.app(scope, receive, send)
        finally:
            ctx_request_id.reset(tok_rid)
            if tok_ws is not None:
                ctx_ws_id.reset(tok_ws)
            structlog.contextvars.clear_contextvars()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Helper — workstream lookup (replaces self._get_ws on the old handler)
# ---------------------------------------------------------------------------


def _get_ws(
    mgr: WorkstreamManager, ws_id: str | None
) -> tuple[Workstream, WebUI] | tuple[None, None]:
    """Look up workstream by id.  Returns (Workstream, WebUI) or (None, None)."""
    if not ws_id:
        return None, None
    ws = mgr.get(ws_id)
    if ws and ws.ui:
        ui: WebUI = ws.ui  # type: ignore[assignment]
        return ws, ui
    return None, None


# ---------------------------------------------------------------------------
# Route handlers — all async
# ---------------------------------------------------------------------------


async def index(request: Request) -> HTMLResponse:
    """GET / — serve the embedded HTML client."""
    return HTMLResponse(_HTML)


async def events_sse(request: Request) -> Response:
    """GET /v1/api/events — per-workstream SSE event stream."""
    mgr = request.app.state.workstreams
    ws_id = request.query_params.get("ws_id")
    ws, ui = _get_ws(mgr, ws_id)
    if not ws or not ui:
        return JSONResponse({"error": "Unknown workstream"}, status_code=404)

    # Each client gets its own queue — no drain needed.
    client_queue = ui._register_listener()

    async def event_generator() -> AsyncGenerator[dict[str, str], None]:
        assert ws.session is not None
        session: ChatSession = ws.session
        # Connected event
        yield {
            "data": json.dumps(
                {
                    "type": "connected",
                    "model": session.model,
                    "model_alias": session.model_alias or "",
                    "skip_permissions": ui.auto_approve,
                }
            )
        }
        # History replay
        history = _build_history(session, has_pending_approval=ui._pending_approval is not None)
        if history:
            yield {"data": json.dumps({"type": "history", "messages": history})}
        # Re-inject pending approval
        if ui._pending_approval is not None:
            yield {"data": json.dumps(ui._pending_approval)}

        _metrics.record_sse_connect()
        try:
            loop = asyncio.get_running_loop()
            executor = request.app.state.sse_executor
            while True:
                try:
                    event = await loop.run_in_executor(
                        executor, functools.partial(client_queue.get, timeout=5)
                    )
                    if event.get("type") == "ws_closed":
                        return
                    yield {"data": json.dumps(event)}
                except queue.Empty:
                    pass
        finally:
            _metrics.record_sse_disconnect()
            ui._unregister_listener(client_queue)

    return EventSourceResponse(event_generator(), ping=5)


async def global_events_sse(request: Request) -> Response:
    """GET /v1/api/events/global — global SSE event stream."""
    client_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1000)
    listeners = request.app.state.global_listeners
    listeners_lock = request.app.state.global_listeners_lock
    with listeners_lock:
        listeners.append(client_queue)

    async def event_generator() -> AsyncGenerator[dict[str, str], None]:
        _metrics.record_sse_connect()
        try:
            loop = asyncio.get_running_loop()
            executor = request.app.state.sse_executor
            while True:
                try:
                    event = await loop.run_in_executor(
                        executor, functools.partial(client_queue.get, timeout=5)
                    )
                    yield {"data": json.dumps(event)}
                except queue.Empty:
                    pass
        finally:
            _metrics.record_sse_disconnect()
            with listeners_lock:
                if client_queue in listeners:
                    listeners.remove(client_queue)

    return EventSourceResponse(event_generator(), ping=5)


async def list_workstreams(request: Request) -> JSONResponse:
    """GET /v1/api/workstreams — list all workstreams."""
    mgr: WorkstreamManager = request.app.state.workstreams
    result = []
    for ws in mgr.list_all():
        result.append(
            {
                "id": ws.id,
                "name": ws.name,
                "state": ws.state.value,
            }
        )
    return JSONResponse({"workstreams": result})


async def dashboard(request: Request) -> JSONResponse:
    """GET /v1/api/dashboard — enriched workstream data + aggregate stats."""
    from turnstone.core.memory import get_workstream_display_name

    mgr: WorkstreamManager = request.app.state.workstreams
    wss = mgr.list_all()
    total_tokens = 0
    total_tool_calls = 0
    active_count = 0
    ws_list = []
    for ws in wss:
        ui: WebUI = ws.ui  # type: ignore[assignment]
        with ui._ws_lock:
            tok = ui._ws_prompt_tokens + ui._ws_completion_tokens
            tc = sum(ui._ws_tool_calls.values())
            ctx = ui._ws_context_ratio
            activity = ui._ws_current_activity
            activity_state = ui._ws_activity_state
        total_tokens += tok
        total_tool_calls += tc
        if ws.state.value != "idle":
            active_count += 1
        title = ""
        if ws.session:
            title = get_workstream_display_name(ws.session.ws_id) or ""
        ws_list.append(
            {
                "id": ws.id,
                "name": ws.name,
                "state": ws.state.value,
                "title": title,
                "tokens": tok,
                "context_ratio": round(ctx, 3),
                "activity": activity,
                "activity_state": activity_state,
                "tool_calls": tc,
                "node": "local",
                "model": ws.session.model if ws.session else "",
                "model_alias": ws.session.model_alias if ws.session else "",
            }
        )
    uptime_sec = round(time.monotonic() - _metrics.start_time)
    return JSONResponse(
        {
            "workstreams": ws_list,
            "aggregate": {
                "total_tokens": total_tokens,
                "total_tool_calls": total_tool_calls,
                "active_count": active_count,
                "total_count": len(ws_list),
                "uptime_seconds": uptime_sec,
                "node": "local",
            },
        }
    )


async def list_saved_workstreams(request: Request) -> JSONResponse:
    """GET /v1/api/workstreams/saved — list saved workstreams with conversation history."""
    from turnstone.core.memory import list_workstreams_with_history

    rows = list_workstreams_with_history(limit=50)
    result = [
        {
            "ws_id": wid,
            "alias": alias,
            "title": title,
            "created": created,
            "updated": updated,
            "message_count": count,
        }
        for wid, alias, title, created, updated, count, *_extra in rows
    ]
    return JSONResponse({"workstreams": result})


async def list_skills_summary(request: Request) -> JSONResponse:
    """GET /v1/api/skills — list available skills (summary)."""
    import json as _json

    from turnstone.core.storage._registry import get_storage

    try:
        storage = get_storage()
    except Exception:
        return JSONResponse({"error": "Storage not available"}, status_code=503)
    rows = storage.list_prompt_templates()
    skills = []
    for r in rows:
        if not r.get("enabled", True):
            continue
        tags: list[str] = []
        with contextlib.suppress(ValueError, TypeError):
            tags = _json.loads(r.get("tags", "[]"))
        skills.append(
            {
                "name": r["name"],
                "category": r.get("category", ""),
                "description": r.get("description", ""),
                "tags": tags,
                "is_default": r.get("is_default", False),
                "activation": r.get("activation", "named"),
                "origin": r.get("origin", "manual"),
                "author": r.get("author", ""),
                "version": r.get("version", "1.0.0"),
            }
        )
    return JSONResponse({"skills": skills})


def _count_ws_states(wss: list[Workstream]) -> dict[str, int]:
    """Count workstream states for health/metrics endpoints."""
    counts = dict.fromkeys(("idle", "thinking", "running", "attention", "error"), 0)
    for ws in wss:
        counts[ws.state.value] = counts.get(ws.state.value, 0) + 1
    return counts


async def health(request: Request) -> JSONResponse:
    """GET /health — server health status."""
    mgr: WorkstreamManager = request.app.state.workstreams
    wss = mgr.list_all()
    states = _count_ws_states(wss)
    monitor = getattr(request.app.state, "health_monitor", None)
    backend_ok = monitor.is_healthy if monitor else True
    data: dict[str, Any] = {
        "status": "ok" if backend_ok else "degraded",
        "version": __version__,
        "node_id": getattr(request.app.state, "node_id", ""),
        "uptime_seconds": round(time.monotonic() - _metrics.start_time, 2),
        "model": _metrics.model,
        "max_ws": mgr.max_workstreams,
        "workstreams": {"total": len(wss), **states},
        "backend": {
            "status": "up" if backend_ok else "down",
            "circuit_state": monitor.circuit_state.value if monitor else "closed",
        },
    }
    mc = getattr(request.app.state, "mcp_client", None)
    if mc:
        data["mcp"] = {
            "servers": mc.server_count,
            "resources": mc.resource_count,
            "prompts": mc.prompt_count,
        }
    return JSONResponse(data)


async def metrics_endpoint(request: Request) -> Response:
    """GET /metrics — Prometheus text exposition format."""
    mgr: WorkstreamManager = request.app.state.workstreams
    wss = mgr.list_all()
    states = _count_ws_states(wss)
    ws_data = []
    for ws in wss:
        ui: WebUI = ws.ui  # type: ignore[assignment]
        with ui._ws_lock:
            ws_data.append(
                {
                    "ws_id": ws.id,
                    "name": ws.name,
                    "prompt_tokens": ui._ws_prompt_tokens,
                    "completion_tokens": ui._ws_completion_tokens,
                    "messages": ui._ws_messages,
                    "tool_calls": dict(ui._ws_tool_calls),
                    "context_ratio": ui._ws_context_ratio,
                }
            )
    mcp_info = None
    mc = getattr(request.app.state, "mcp_client", None)
    if mc:
        mcp_info = {
            "servers": mc.server_count,
            "resources": mc.resource_count,
            "prompts": mc.prompt_count,
            "errors": mc.error_count,
        }
    content = _metrics.generate_text(
        workstream_states=states,
        total_workstreams=len(wss),
        workstream_metrics=ws_data,
        mcp_info=mcp_info,
    )
    return Response(content, media_type="text/plain; version=0.0.4; charset=utf-8")


def _make_watch_dispatch(ws: Workstream, session: ChatSession, ui: Any) -> Any:
    """Create a dispatch function for watch results on a workstream.

    Handles both idle (start worker thread) and busy (enqueue for IDLE drain)
    cases.  Mirrors the ``send_message`` worker-thread pattern.
    """
    pending = session._watch_pending

    def dispatch(msg: str) -> None:
        if ws.worker_thread and ws.worker_thread.is_alive():
            # Workstream is busy — queue for drain at IDLE (Path A)
            pending.put({"message": msg})
            return

        # Workstream is idle — start a worker thread (Path B)
        def run() -> None:
            try:
                session.send(msg)
            except Exception as exc:
                if ui:
                    ui.on_error(f"Watch error: {exc}")

        t = threading.Thread(target=run, daemon=True)
        ws.worker_thread = t
        t.start()

    return dispatch


async def send_message(request: Request) -> JSONResponse:
    """POST /v1/api/send — send a user message to the workstream."""
    from turnstone.core.web_helpers import read_json_or_400

    body = await read_json_or_400(request)
    if isinstance(body, JSONResponse):
        return body
    message = body.get("message", "").strip()
    ws_id = body.get("ws_id")
    if not message:
        return JSONResponse({"error": "Empty message"}, status_code=400)
    mgr = request.app.state.workstreams
    ws, ui = _get_ws(mgr, ws_id)
    if not ws or not ui:
        return JSONResponse({"error": "Unknown workstream"}, status_code=404)
    # Atomically check-and-start to prevent two concurrent workers on the
    # same session (ChatSession.send() is not thread-safe).
    with ws._lock:
        if ws.worker_thread and ws.worker_thread.is_alive():
            ui._enqueue(
                {
                    "type": "busy_error",
                    "message": "Already processing a request. Please wait.",
                }
            )
            return JSONResponse({"status": "busy"})
        session = ws.session
        assert session is not None

        def run() -> None:
            assert ui is not None
            try:
                session.send(message)
            except GenerationCancelled:
                # Safety net — send() normally handles this internally.
                ui._enqueue({"type": "stream_end"})
                ui.on_state_change("idle")
            except Exception as e:
                ui.on_error(f"Error: {e}")
                ui._enqueue({"type": "stream_end"})
                ui.on_state_change("error")

        t = threading.Thread(target=run, daemon=True)
        ws.worker_thread = t
        t.start()
    _metrics.record_message_sent()
    with ui._ws_lock:
        ui._ws_messages += 1
    return JSONResponse({"status": "ok"})


async def approve(request: Request) -> JSONResponse:
    """POST /v1/api/approve — approve or deny a tool call."""
    from turnstone.core.web_helpers import read_json_or_400

    body = await read_json_or_400(request)
    if isinstance(body, JSONResponse):
        return body
    approved = body.get("approved", False)
    feedback = body.get("feedback")
    always = body.get("always", False)
    ws_id = body.get("ws_id")
    mgr = request.app.state.workstreams
    ws, ui = _get_ws(mgr, ws_id)
    if not ws or not ui:
        return JSONResponse({"error": "Unknown workstream"}, status_code=404)
    if always and approved and ui._pending_approval:
        tool_names = {
            it.get("approval_label", "") or it.get("func_name", "")
            for it in ui._pending_approval.get("items", [])
            if it.get("needs_approval") and it.get("func_name") and not it.get("error")
        }
        tool_names.discard("")
        tool_names.discard("__budget_override__")
        if tool_names:
            ui.auto_approve_tools.update(tool_names)
    ui.resolve_approval(approved, feedback)
    return JSONResponse({"status": "ok"})


async def plan_feedback(request: Request) -> JSONResponse:
    """POST /v1/api/plan — respond to a plan review."""
    from turnstone.core.web_helpers import read_json_or_400

    body = await read_json_or_400(request)
    if isinstance(body, JSONResponse):
        return body
    feedback = body.get("feedback", "")
    ws_id = body.get("ws_id")
    mgr = request.app.state.workstreams
    ws, ui = _get_ws(mgr, ws_id)
    if not ws or not ui:
        return JSONResponse({"error": "Unknown workstream"}, status_code=404)
    ui.resolve_plan(feedback)
    return JSONResponse({"status": "ok"})


async def cancel_generation(request: Request) -> JSONResponse:
    """POST /v1/api/cancel — cancel the active generation in a workstream."""
    from turnstone.core.web_helpers import read_json_or_400

    body = await read_json_or_400(request)
    if isinstance(body, JSONResponse):
        return body
    ws_id = body.get("ws_id")
    mgr = request.app.state.workstreams
    ws, ui = _get_ws(mgr, ws_id)
    if not ws or not ui:
        return JSONResponse({"error": "Unknown workstream"}, status_code=404)
    session = ws.session
    if session is None:
        return JSONResponse({"error": "No session"}, status_code=400)
    # Only act if generation is actually in progress
    if ws.worker_thread and ws.worker_thread.is_alive():
        # Set the cooperative cancel flag (worker thread checks at checkpoints)
        session.cancel()
        # Unblock any pending approval/plan review waits
        ui.resolve_approval(False, "Cancelled by user")
        ui.resolve_plan("reject")
        # Emit cancelled SSE event so SDK consumers get a typed signal
        ui._enqueue({"type": "cancelled"})
    return JSONResponse({"status": "ok"})


async def command(request: Request) -> JSONResponse:
    """POST /v1/api/command — execute a slash command."""
    from turnstone.core.web_helpers import read_json_or_400

    body = await read_json_or_400(request)
    if isinstance(body, JSONResponse):
        return body
    cmd = body.get("command", "").strip()
    ws_id = body.get("ws_id")
    if not cmd:
        return JSONResponse({"error": "Empty command"}, status_code=400)

    mgr = request.app.state.workstreams
    ws, ui = _get_ws(mgr, ws_id)
    if not ws or not ui:
        return JSONResponse({"error": "Unknown workstream"}, status_code=404)
    assert ws.session is not None

    try:
        should_exit = ws.session.handle_command(cmd)
        if should_exit:
            ui.on_info("Session ended. You can close this tab.")
        # Handle UI updates for workstream-changing commands
        cmd_word = cmd.strip().split(None, 1)[0].lower()
        if cmd_word in ("/clear", "/new"):
            ui._enqueue({"type": "clear_ui"})
        elif cmd_word == "/resume":
            ui._enqueue({"type": "clear_ui"})
            history = _build_history(ws.session)
            if history:
                ui._enqueue({"type": "history", "messages": history})
        # Sync in-memory workstream name after any command that can change it.
        # This ensures /api/workstreams and future page loads see the right name.
        if cmd_word in ("/name", "/resume"):
            from turnstone.core.memory import get_workstream_display_name

            updated_name = get_workstream_display_name(ws.session.ws_id) if ws.session else None
            if updated_name:
                ws.name = updated_name
    except Exception as e:
        ui.on_error(f"Command error: {e}")

    return JSONResponse({"status": "ok"})


async def create_workstream(request: Request) -> JSONResponse:
    """POST /v1/api/workstreams/new — create a new workstream."""
    from turnstone.core.web_helpers import read_json_or_400

    body = await read_json_or_400(request)
    if isinstance(body, JSONResponse):
        return body
    mgr: WorkstreamManager = request.app.state.workstreams
    skip: bool = request.app.state.skip_permissions
    auth = getattr(getattr(request, "state", None), "auth_result", None)
    uid: str = getattr(auth, "user_id", "") or ""
    # Trusted services (bridge, console) may forward the real user_id in the
    # request body when creating workstreams on behalf of a user.  Only service
    # identities are trusted — end-user tokens (including console-proxy tokens
    # that carry the real user's identity) must not override user_id.
    trusted_sources = {"bridge", "console"}
    if (
        body.get("user_id")
        and isinstance(body["user_id"], str)
        and auth is not None
        and auth.token_source in trusted_sources
    ):
        uid = body["user_id"]
    body_skill = body.get("skill", "")
    resume_ws_id = body.get("resume_ws", "")
    # Resolve skill — applies content + session config (model, temperature, etc.)
    # Skip when resuming: the resumed session restores its own skill from config.
    skill_data: dict[str, Any] | None = None
    if body_skill and not resume_ws_id:
        from turnstone.core.memory import get_skill_by_name

        skill_data = get_skill_by_name(body_skill)
        if not skill_data or not skill_data.get("enabled", False):
            return JSONResponse(
                {"error": f"Skill not found or disabled: {body_skill}"},
                status_code=400,
            )
    resolved_model = body.get("model") or None
    if skill_data and skill_data.get("model"):
        resolved_model = skill_data["model"]
    resolved_skill: str | None = body_skill if skill_data else None
    applied_skill_version = 0
    if skill_data:
        from turnstone.core.storage import get_storage as _get_storage

        _st = _get_storage()
        applied_skill_version = len(_st.list_skill_versions(skill_data["template_id"])) + 1
    try:
        ws = mgr.create(
            name=body.get("name", ""),
            ui_factory=lambda wid: WebUI(ws_id=wid, user_id=uid),
            model=resolved_model,
            skill=resolved_skill,
            skill_id=skill_data["template_id"] if skill_data else "",
            skill_version=applied_skill_version,
        )
        assert isinstance(ws.ui, WebUI)
        if skip or body.get("auto_approve", False):
            ws.ui.auto_approve = True
        # Register watch runner for this workstream
        runner = getattr(request.app.state, "watch_runner", None)
        if runner and ws.session:
            ws.session.set_watch_runner(
                runner, dispatch_fn=_make_watch_dispatch(ws, ws.session, ws.ui)
            )
        # Emit eviction event if a workstream was evicted to make room
        evicted = mgr.last_evicted
        if evicted is not None:
            gq: queue.Queue[dict[str, Any]] = request.app.state.global_queue
            with contextlib.suppress(queue.Full):
                gq.put_nowait(
                    {
                        "type": "ws_closed",
                        "ws_id": evicted.id,
                        "name": evicted.name,
                        "reason": "evicted",
                    }
                )
        # Atomic workstream resume during creation.
        resumed = False
        message_count = 0
        if resume_ws_id and ws.session is not None:
            from turnstone.core.memory import get_workstream_display_name, resolve_workstream

            target_id = resolve_workstream(resume_ws_id)
            if target_id and ws.session.resume(target_id):
                resumed = True
                message_count = len(ws.session.messages)
                ws.name = get_workstream_display_name(target_id) or ws.name
                ui = ws.ui
                if isinstance(ui, WebUI):
                    ui._enqueue({"type": "clear_ui"})
                    history = _build_history(ws.session)
                    if history:
                        ui._enqueue({"type": "history", "messages": history})

        # Apply skill session config (only for new workstreams with a skill)
        if skill_data and not resumed and ws.session:
            sess = ws.session
            # Session settings from skill
            if skill_data.get("temperature") is not None:
                sess.temperature = skill_data["temperature"]
            if skill_data.get("reasoning_effort"):
                sess.reasoning_effort = skill_data["reasoning_effort"]
            if skill_data.get("max_tokens") is not None:
                sess.max_tokens = skill_data["max_tokens"]
            if skill_data.get("token_budget", 0) > 0:
                sess._token_budget = skill_data["token_budget"]
            if skill_data.get("agent_max_turns") is not None:
                sess.agent_max_turns = skill_data["agent_max_turns"]
            # Approval policy
            if skill_data.get("auto_approve"):
                ws.ui.auto_approve = True
            allowed = skill_data.get("allowed_tools", "")
            if allowed and allowed != "[]":
                # Parse as JSON array or comma-separated
                import json as _json

                try:
                    tools_list = _json.loads(allowed)
                except (ValueError, TypeError):
                    tools_list = [t.strip() for t in allowed.split(",") if t.strip()]
                if tools_list:
                    ws.ui.auto_approve_tools = set(tools_list)
            # Metadata
            sess._notify_on_complete = skill_data.get("notify_on_complete", "{}")
            sess._applied_skill_id = skill_data["template_id"]
            sess._applied_skill_version = applied_skill_version
            if skill_data.get("content"):
                sess._applied_skill_content = skill_data["content"]
            sess._save_config()

        return JSONResponse(
            {
                "ws_id": ws.id,
                "name": ws.name,
                "resumed": resumed,
                "message_count": message_count,
            }
        )
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


async def close_workstream(request: Request) -> JSONResponse:
    """POST /v1/api/workstreams/close — close a workstream."""
    from turnstone.core.web_helpers import read_json_or_400

    body = await read_json_or_400(request)
    if isinstance(body, JSONResponse):
        return body
    ws_id = str(body.get("ws_id", ""))
    mgr = request.app.state.workstreams
    if mgr.close(ws_id):
        return JSONResponse({"status": "ok"})
    return JSONResponse({"error": "Cannot close last workstream"}, status_code=400)


async def list_watches(request: Request) -> JSONResponse:
    """GET /v1/api/watches — list active watches, optionally filtered by ws_id."""
    from turnstone.core.storage._registry import get_storage

    storage = get_storage()
    if not storage:
        return JSONResponse({"watches": []})
    ws_id = request.query_params.get("ws_id")
    if ws_id:
        watches = storage.list_watches_for_ws(ws_id)
    else:
        node_id = getattr(request.app.state, "node_id", "")
        watches = storage.list_watches_for_node(node_id) if node_id else []
    return JSONResponse({"watches": watches})


async def cancel_watch(request: Request) -> JSONResponse:
    """POST /v1/api/watches/{watch_id}/cancel — cancel an active watch."""
    from turnstone.core.storage._registry import get_storage

    watch_id = request.path_params["watch_id"]
    storage = get_storage()
    if not storage:
        return JSONResponse({"error": "Storage unavailable"}, status_code=500)
    watch = storage.get_watch(watch_id)
    if not watch:
        return JSONResponse({"error": "Watch not found"}, status_code=404)
    # Verify node ownership in multi-node deployments
    node_id = getattr(request.app.state, "node_id", "")
    watch_node = watch.get("node_id", "")
    if watch_node and node_id and watch_node != node_id:
        return JSONResponse({"error": "Watch belongs to another node"}, status_code=403)
    storage.update_watch(watch_id, active=False, next_poll="")
    return JSONResponse({"status": "ok", "watch_id": watch_id})


# ---------------------------------------------------------------------------
# Memory endpoints
# ---------------------------------------------------------------------------

_VALID_MEMORY_TYPES = frozenset({"user", "project", "feedback", "reference"})
_VALID_MEMORY_SCOPES = frozenset({"global", "workstream", "user"})
_MAX_MEMORY_CONTENT = 65536  # hard upper bound; server may enforce lower via config


def _validate_scope_scope_id(
    scope: str, scope_id: str, *, require_scope_id: bool = False
) -> JSONResponse | None:
    """Validate scope/scope_id consistency. Returns error response or None."""
    scope = scope.strip()
    scope_id = scope_id.strip()
    if scope == "global" and scope_id:
        return JSONResponse(
            {"error": "scope_id is not allowed with global scope"},
            status_code=400,
        )
    if scope_id and not scope:
        return JSONResponse(
            {"error": "scope is required when scope_id is provided"},
            status_code=400,
        )
    if require_scope_id and scope in ("workstream", "user") and not scope_id:
        return JSONResponse(
            {"error": f"scope_id is required for {scope} scope"},
            status_code=400,
        )
    return None


def _resolve_user_scope_id(
    request: Request, provided_scope_id: str = ""
) -> tuple[str, JSONResponse | None]:
    """Resolve and validate scope_id for user-scoped memory.

    Always binds to the authenticated user's identity.  If a scope_id is
    provided and doesn't match, returns 403 to prevent cross-user access.
    """
    auth = getattr(getattr(request, "state", None), "auth_result", None)
    uid: str = getattr(auth, "user_id", "") or ""
    if not uid:
        return "", JSONResponse(
            {"error": "User scope requires authentication with a user identity"},
            status_code=400,
        )
    if provided_scope_id and provided_scope_id != uid:
        return "", JSONResponse(
            {"error": "Cannot access another user's memories"},
            status_code=403,
        )
    return uid, None


async def list_memories(request: Request) -> JSONResponse:
    """GET /v1/api/memories — list memories with optional filters."""
    from turnstone.core.memory import list_structured_memories

    mem_type = request.query_params.get("type", "")
    scope = request.query_params.get("scope", "")
    scope_id = request.query_params.get("scope_id", "")
    try:
        limit = min(int(request.query_params.get("limit", "100")), 200)
    except (ValueError, TypeError):
        return JSONResponse({"error": "limit must be an integer"}, status_code=400)
    err = _validate_scope_scope_id(scope, scope_id)
    if err:
        return err
    if scope == "user":
        scope_id, err = _resolve_user_scope_id(request, scope_id)
        if err:
            return err
    rows = list_structured_memories(mem_type=mem_type, scope=scope, scope_id=scope_id, limit=limit)
    return JSONResponse({"memories": rows, "total": len(rows)})


async def save_memory(request: Request) -> JSONResponse:
    """POST /v1/api/memories — save (upsert) a structured memory."""
    from turnstone.core.memory import save_structured_memory
    from turnstone.core.web_helpers import read_json_or_400

    body = await read_json_or_400(request)
    if isinstance(body, JSONResponse):
        return body
    name = str(body.get("name", "")).strip()
    content = str(body.get("content", "")).strip()
    if not name or len(name) > 256:
        return JSONResponse({"error": "name is required (max 256 characters)"}, status_code=400)
    if not content:
        return JSONResponse({"error": "content is required"}, status_code=400)
    if len(content) > _MAX_MEMORY_CONTENT:
        return JSONResponse(
            {"error": f"content exceeds {_MAX_MEMORY_CONTENT} character limit"},
            status_code=400,
        )
    description = str(body.get("description", ""))
    mem_type = str(body.get("type", "project"))
    scope = str(body.get("scope", "global"))
    scope_id = str(body.get("scope_id", ""))
    if mem_type not in _VALID_MEMORY_TYPES:
        return JSONResponse(
            {"error": f"invalid type: {mem_type}; must be one of {sorted(_VALID_MEMORY_TYPES)}"},
            status_code=400,
        )
    if scope not in _VALID_MEMORY_SCOPES:
        return JSONResponse(
            {"error": f"invalid scope: {scope}; must be one of {sorted(_VALID_MEMORY_SCOPES)}"},
            status_code=400,
        )
    if scope == "user":
        scope_id, err = _resolve_user_scope_id(request, scope_id)
        if err:
            return err
    err = _validate_scope_scope_id(scope, scope_id, require_scope_id=True)
    if err:
        return err
    # save_structured_memory normalises the name internally
    from turnstone.core.memory import normalize_key

    normalized_name = normalize_key(name)
    memory_id, old_content = save_structured_memory(
        name, content, description=description, mem_type=mem_type, scope=scope, scope_id=scope_id
    )
    if not memory_id:
        return JSONResponse({"error": "Failed to save memory"}, status_code=500)
    from turnstone.core.storage._registry import get_storage

    storage = get_storage()
    mem = storage.get_structured_memory(memory_id) if storage else None
    if not mem:
        return JSONResponse(
            {"memory_id": memory_id, "name": normalized_name, "status": "saved"},
            status_code=201,
        )
    status_code = 200 if old_content is not None else 201
    return JSONResponse(mem, status_code=status_code)


async def search_memories(request: Request) -> JSONResponse:
    """POST /v1/api/memories/search — search memories by query.

    Uses POST for the request body but requires only read scope (non-mutating).
    """
    from turnstone.core.memory import search_structured_memories as search_fn
    from turnstone.core.web_helpers import read_json_or_400

    body = await read_json_or_400(request)
    if isinstance(body, JSONResponse):
        return body
    query = str(body.get("query", "")).strip()
    if not query:
        return JSONResponse({"error": "query is required"}, status_code=400)
    mem_type = str(body.get("type", ""))
    scope = str(body.get("scope", ""))
    scope_id = str(body.get("scope_id", ""))
    try:
        limit = min(int(body.get("limit", 20)), 50)
    except (ValueError, TypeError):
        return JSONResponse({"error": "limit must be an integer"}, status_code=400)
    err = _validate_scope_scope_id(scope, scope_id)
    if err:
        return err
    if scope == "user":
        scope_id, err = _resolve_user_scope_id(request, scope_id)
        if err:
            return err
    rows = search_fn(query, mem_type=mem_type, scope=scope, scope_id=scope_id, limit=limit)
    return JSONResponse({"memories": rows, "total": len(rows)})


async def delete_memory_endpoint(request: Request) -> JSONResponse:
    """DELETE /v1/api/memories/{name} — delete a memory by name and scope."""
    from turnstone.core.memory import delete_structured_memory, normalize_key

    name = normalize_key(request.path_params["name"])
    scope = request.query_params.get("scope", "global")
    if scope not in _VALID_MEMORY_SCOPES:
        return JSONResponse(
            {"error": f"invalid scope: {scope}; must be one of {sorted(_VALID_MEMORY_SCOPES)}"},
            status_code=400,
        )
    scope_id = request.query_params.get("scope_id", "")
    if scope == "user":
        scope_id, err = _resolve_user_scope_id(request, scope_id)
        if err:
            return err
    err = _validate_scope_scope_id(scope, scope_id, require_scope_id=True)
    if err:
        return err
    if delete_structured_memory(name, scope, scope_id):
        return JSONResponse({"status": "ok", "name": name})
    return JSONResponse({"error": f"Memory '{name}' not found"}, status_code=404)


async def auth_login(request: Request) -> Response:
    """POST /v1/api/auth/login — authenticate and return JWT."""
    from turnstone.core.auth import handle_auth_login

    return await handle_auth_login(request, JWT_AUD_SERVER)


async def auth_logout(request: Request) -> Response:
    """POST /v1/api/auth/logout — clear auth cookie."""
    from turnstone.core.auth import handle_auth_logout

    return await handle_auth_logout(request)


async def auth_status(request: Request) -> Response:
    """GET /v1/api/auth/status — public endpoint for login UI state detection."""
    from turnstone.core.auth import handle_auth_status

    return await handle_auth_status(request)


async def auth_setup(request: Request) -> Response:
    """POST /v1/api/auth/setup — create first admin user (public, one-time only)."""
    from turnstone.core.auth import handle_auth_setup

    return await handle_auth_setup(request, JWT_AUD_SERVER)


async def auth_whoami(request: Request) -> Response:
    """GET /v1/api/auth/whoami — return authenticated user info."""
    from turnstone.core.auth import handle_auth_whoami

    return await handle_auth_whoami(request)


async def oidc_authorize(request: Request) -> Response:
    """GET /v1/api/auth/oidc/authorize — redirect to OIDC provider."""
    from turnstone.core.auth import handle_oidc_authorize

    return await handle_oidc_authorize(request, JWT_AUD_SERVER)


async def oidc_callback(request: Request) -> Response:
    """GET /v1/api/auth/oidc/callback — OIDC callback, exchange code for JWT."""
    from turnstone.core.auth import handle_oidc_callback

    return await handle_oidc_callback(request, JWT_AUD_SERVER)


def config_reload(request: Request) -> JSONResponse:
    """POST /v1/api/_internal/config-reload — invalidate config cache."""
    cs = getattr(request.app.state, "config_store", None)
    if not cs:
        return JSONResponse({"status": "noop"})
    cs.reload()
    return JSONResponse({"status": "ok"})


# -- internal MCP management -----------------------------------------------


def internal_mcp_reload(request: Request) -> JSONResponse:
    """POST /v1/api/_internal/mcp-reload — re-read mcp_servers table and reconcile."""
    from turnstone.core.storage._registry import get_storage

    storage = get_storage()
    mcp_mgr = getattr(request.app.state, "mcp_client", None)
    if mcp_mgr is None:
        # Create a new manager if none exists
        from turnstone.core.mcp_client import MCPClientManager

        mcp_mgr = MCPClientManager({})
        mcp_mgr.start()
        request.app.state.mcp_client = mcp_mgr

    result = mcp_mgr.reconcile_sync(storage)
    return JSONResponse({"status": "ok", **result})


def internal_mcp_status(request: Request) -> JSONResponse:
    """GET /v1/api/_internal/mcp-status — return MCP server status."""
    mcp_mgr = getattr(request.app.state, "mcp_client", None)
    if mcp_mgr is None:
        return JSONResponse({"servers": {}})

    return JSONResponse({"servers": mcp_mgr.get_all_server_status()})


# ---------------------------------------------------------------------------
# Global SSE fan-out
# ---------------------------------------------------------------------------


def _idle_cleanup_thread(
    mgr: WorkstreamManager,
    timeout_sec: float,
    global_queue: queue.Queue[dict[str, Any]],
    rate_limiter: Any = None,
) -> None:
    """Periodically close IDLE workstreams and clean up rate limiter buckets."""
    check_every = min(300.0, timeout_sec / 4)  # check at 1/4 of timeout, max 5 min
    while True:
        time.sleep(check_every)
        closed = mgr.close_idle(timeout_sec)
        for ws_id in closed:
            with contextlib.suppress(queue.Full):
                global_queue.put_nowait({"type": "ws_closed", "ws_id": ws_id, "reason": "idle"})
        if rate_limiter is not None:
            rate_limiter.cleanup()


def _global_fanout_thread(
    source_queue: queue.Queue[dict[str, Any]],
    listeners: list[queue.Queue[dict[str, Any]]],
    lock: threading.Lock,
) -> None:
    """Reads events from the source queue and copies them to all listener queues."""
    while True:
        try:
            event = source_queue.get()
            with lock:
                snapshot = list(listeners)
            for lq in snapshot:
                with contextlib.suppress(queue.Full):
                    lq.put_nowait(event)  # drop if a listener is backed up
        except Exception:
            log.debug("Global fan-out error", exc_info=True)


# ---------------------------------------------------------------------------
# Lifespan context manager
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _lifespan(app: Starlette) -> AsyncGenerator[None, None]:
    """Start background threads and handle shutdown."""
    # Dedicated executor for SSE queue polling so it doesn't compete
    # with the default asyncio executor (which caps at ~32 workers).
    app.state.sse_executor = ThreadPoolExecutor(max_workers=200, thread_name_prefix="sse")
    # Start global event fan-out thread
    fanout = threading.Thread(
        target=_global_fanout_thread,
        args=(
            app.state.global_queue,
            app.state.global_listeners,
            app.state.global_listeners_lock,
        ),
        daemon=True,
    )
    fanout.start()
    # Start idle cleanup thread if configured
    if app.state.idle_timeout > 0:
        cleanup = threading.Thread(
            target=_idle_cleanup_thread,
            args=(
                app.state.workstreams,
                app.state.idle_timeout * 60,
                app.state.global_queue,
                app.state.rate_limiter,
            ),
            daemon=True,
        )
        cleanup.start()
    # Start watch runner (periodic command polling)
    if app.state.watch_runner:
        app.state.watch_runner.start()
    # OIDC discovery (if configured)
    oidc_config = app.state.oidc_config
    if oidc_config.enabled:
        from turnstone.core.oidc import discover_oidc

        try:
            oidc_config = await discover_oidc(oidc_config)
            app.state.oidc_config = oidc_config
        except Exception:
            log.warning("OIDC discovery failed — OIDC login disabled", exc_info=True)
        if oidc_config.enabled and oidc_config.jwks_uri:
            try:
                from turnstone.core.oidc import fetch_jwks

                app.state.jwks_data = await fetch_jwks(oidc_config.jwks_uri)
                log.info(
                    "OIDC enabled: %s (%s)",
                    oidc_config.provider_name,
                    oidc_config.issuer,
                )
            except Exception:
                log.warning(
                    "OIDC JWKS prefetch failed — will retry on first login",
                    exc_info=True,
                )
    # TLS: start auto-renewal if client was initialized
    tls_client = getattr(app.state, "tls_client", None)
    if tls_client is not None:
        try:
            await tls_client.start_renewal()
        except Exception:
            log.warning("TLS auto-renewal startup failed", exc_info=True)

    yield
    # Shutdown
    tls_client = getattr(app.state, "tls_client", None)
    if tls_client is not None:
        await tls_client.stop_renewal()
    if app.state.watch_runner:
        app.state.watch_runner.stop()
    if app.state.health_monitor:
        app.state.health_monitor.stop()
    if app.state.mcp_client:
        app.state.mcp_client.shutdown()
    if app.state.registry:
        app.state.registry.shutdown()
    app.state.sse_executor.shutdown(wait=True, cancel_futures=True)


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def _build_middleware(cors_origins: list[str] | None = None) -> list[Middleware]:
    """Build the middleware stack with optional CORS."""
    stack: list[Middleware] = [
        Middleware(LogContextMiddleware),
        Middleware(MetricsMiddleware),
    ]
    if cors_origins:
        from turnstone.core.web_helpers import cors_middleware

        stack.append(cors_middleware(cors_origins))
    stack.extend(
        [
            Middleware(AuthMiddleware, jwt_audience=JWT_AUD_SERVER),
            Middleware(RateLimitMiddleware),
        ]
    )
    return stack


def create_app(
    *,
    workstreams: WorkstreamManager,
    global_queue: queue.Queue[dict[str, Any]],
    global_listeners: list[queue.Queue[dict[str, Any]]],
    global_listeners_lock: threading.Lock,
    skip_permissions: bool,
    auth_config: Any,
    jwt_secret: str = "",
    auth_storage: Any = None,
    health_monitor: Any = None,
    rate_limiter: Any = None,
    mcp_client: Any = None,
    registry: Any = None,
    idle_timeout: int = 0,
    node_id: str = "",
    cors_origins: list[str] | None = None,
    watch_runner: Any = None,
    judge_config: Any = None,
    config_store: Any = None,
) -> Starlette:
    """Create and configure the Starlette ASGI application."""
    _spec = build_server_spec()
    _openapi_handler = make_openapi_handler(_spec)
    _docs_handler = make_docs_handler()

    app = Starlette(
        routes=[
            Route("/", index),
            Mount(
                "/v1",
                routes=[
                    Route("/api/events", events_sse),
                    Route("/api/events/global", global_events_sse),
                    Route("/api/workstreams", list_workstreams),
                    Route("/api/dashboard", dashboard),
                    Route("/api/workstreams/saved", list_saved_workstreams),
                    Route("/api/skills", list_skills_summary),
                    Route("/api/send", send_message, methods=["POST"]),
                    Route("/api/approve", approve, methods=["POST"]),
                    Route("/api/plan", plan_feedback, methods=["POST"]),
                    Route("/api/command", command, methods=["POST"]),
                    Route("/api/cancel", cancel_generation, methods=["POST"]),
                    Route("/api/workstreams/new", create_workstream, methods=["POST"]),
                    Route("/api/workstreams/close", close_workstream, methods=["POST"]),
                    Route("/api/watches", list_watches),
                    Route("/api/watches/{watch_id}/cancel", cancel_watch, methods=["POST"]),
                    Route("/api/memories", list_memories),
                    Route("/api/memories", save_memory, methods=["POST"]),
                    Route("/api/memories/search", search_memories, methods=["POST"]),
                    Route("/api/memories/{name}", delete_memory_endpoint, methods=["DELETE"]),
                    Route("/api/auth/login", auth_login, methods=["POST"]),
                    Route("/api/auth/logout", auth_logout, methods=["POST"]),
                    Route("/api/auth/status", auth_status),
                    Route("/api/auth/setup", auth_setup, methods=["POST"]),
                    Route("/api/auth/whoami", auth_whoami),
                    Route("/api/auth/oidc/authorize", oidc_authorize),
                    Route("/api/auth/oidc/callback", oidc_callback),
                    Route("/api/_internal/config-reload", config_reload, methods=["POST"]),
                    Route("/api/_internal/mcp-reload", internal_mcp_reload, methods=["POST"]),
                    Route("/api/_internal/mcp-status", internal_mcp_status),
                ],
            ),
            Route("/health", health),
            Route("/metrics", metrics_endpoint),
            Route("/openapi.json", _openapi_handler),
            Route("/docs", _docs_handler),
            Mount("/static", app=StaticFiles(directory=str(_STATIC_DIR)), name="static"),
            Mount("/shared", app=StaticFiles(directory=str(_SHARED_DIR)), name="shared"),
        ],
        middleware=_build_middleware(cors_origins),
        lifespan=_lifespan,
    )
    app.state.workstreams = workstreams
    app.state.global_queue = global_queue
    app.state.global_listeners = global_listeners
    app.state.global_listeners_lock = global_listeners_lock
    app.state.skip_permissions = skip_permissions
    app.state.auth_config = auth_config
    app.state.jwt_secret = jwt_secret
    app.state.auth_storage = auth_storage
    app.state.health_monitor = health_monitor
    app.state.rate_limiter = rate_limiter
    app.state.mcp_client = mcp_client
    app.state.registry = registry
    app.state.idle_timeout = idle_timeout
    app.state.node_id = node_id
    app.state.watch_runner = watch_runner
    app.state.judge_config = judge_config
    app.state.config_store = config_store

    from turnstone.core.auth import LoginRateLimiter

    app.state.login_limiter = LoginRateLimiter()

    # OIDC configuration (opt-in via env vars)
    from turnstone.core.oidc import load_oidc_config

    oidc_config = load_oidc_config()
    app.state.oidc_config = oidc_config
    app.state.jwks_data = None  # populated after async discovery

    return app


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="turnstone web server — browser-based chat UI.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              turnstone-server                            # auto-detect model, serve on :8080
              turnstone-server --port 3000                # custom port
              turnstone-server --model kappa_20b_131k     # explicit model
              turnstone-server --skip-permissions          # auto-approve all tools
        """),
    )
    parser.add_argument(
        "--base-url",
        default="http://localhost:8000/v1",
        help="OpenAI-compatible API base URL (default: http://localhost:8000/v1)",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Model name (default: auto-detect from server)",
    )
    parser.add_argument(
        "--skill",
        default=None,
        help="Skill name (replaces default skills)",
    )
    parser.add_argument(
        "--provider",
        default="openai",
        choices=["openai", "anthropic"],
        help="LLM provider for the default model (default: openai)",
    )
    parser.add_argument(
        "--resume",
        default=None,
        metavar="WS",
        help="Resume a previous workstream by alias or ws_id",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="API key (default: $OPENAI_API_KEY, or 'dummy' for local servers)",
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Host to bind to (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Port to listen on (default: 8080)",
    )
    # MCP config path is bootstrap-critical (needed before ConfigStore for tool loading)
    parser.add_argument(
        "--mcp-config",
        default=None,
        metavar="PATH",
        help="Path to MCP server config file (standard mcpServers JSON format)",
    )
    from turnstone.core.log import add_log_args

    add_log_args(parser)
    from turnstone.core.config import add_config_arg, apply_config

    add_config_arg(parser)
    # Only load bootstrap sections from config.toml — all other settings
    # are managed by ConfigStore (database-backed) after storage init.
    apply_config(parser, ["api", "server", "database"])
    args = parser.parse_args()

    from turnstone.core.log import configure_logging_from_args

    configure_logging_from_args(args, "server")

    # Initialize storage backend
    from turnstone.core.storage import init_storage

    db_backend = getattr(args, "db_backend", None) or os.environ.get(
        "TURNSTONE_DB_BACKEND", "sqlite"
    )
    db_url = getattr(args, "db_url", None) or os.environ.get("TURNSTONE_DB_URL", "")
    db_path = getattr(args, "db_path", None) or os.environ.get("TURNSTONE_DB_PATH", "")
    db_pool_size = int(
        getattr(args, "db_pool_size", None) or os.environ.get("TURNSTONE_DB_POOL_SIZE", "2")
    )
    init_storage(db_backend, path=db_path, url=db_url, pool_size=db_pool_size)

    # Server-owned node identity (needed before ConfigStore for node_id scoping)
    def _default_node_id() -> str:
        """Generate a node_id: ``{hostname}_{4hex}``, or a UUID on failure."""
        suffix = uuid.uuid4().hex[:4]
        try:
            host = socket.gethostname()
            if host and host != "localhost":
                return f"{host}_{suffix}"
        except OSError:
            pass
        return uuid.uuid4().hex[:12]

    _node_id = os.environ.get("TURNSTONE_NODE_ID") or _default_node_id()

    from turnstone.core.log import ctx_node_id

    ctx_node_id.set(_node_id)

    # Database-backed config store — single source of truth for non-bootstrap
    # settings.  Created early so all subsequent init code can read from it.
    from turnstone.core.config_store import ConfigStore
    from turnstone.core.storage import get_storage as _get_cs_storage

    config_store = ConfigStore(storage=_get_cs_storage(), node_id=_node_id)

    # Warn about config.toml keys that are now managed by ConfigStore
    from turnstone.core.config import warn_migrated_settings

    warn_migrated_settings()

    # Prune stale / empty workstreams on startup
    from turnstone.core.memory import prune_workstreams

    prune_workstreams(retention_days=config_store.get("session.retention_days"), log_fn=print)

    # Create client and detect model
    provider_name = args.provider
    api_key = (
        args.api_key
        or os.environ.get("ANTHROPIC_API_KEY" if provider_name == "anthropic" else "OPENAI_API_KEY")
        or "dummy"
    )
    base_url = args.base_url
    if provider_name == "anthropic" and base_url == "http://localhost:8000/v1":
        base_url = "https://api.anthropic.com"
    from turnstone.core.providers import create_client

    client = create_client(provider_name, base_url=base_url, api_key=api_key)

    cs_model = config_store.get("model.name")
    cli_model = args.model
    effective_model = cli_model or cs_model or None
    if effective_model:
        model = effective_model
        detected_ctx = None
    else:
        from turnstone.core.model_registry import detect_model

        model, detected_ctx = detect_model(client, provider=provider_name, fatal=False)
        if model is None:
            # LLM backend unreachable — start with a placeholder model name.
            # The health monitor will report degraded and the circuit breaker
            # will prevent requests until the backend comes up.
            model = "unavailable"

    # Use detected context window, fall back to ConfigStore override or 32768
    cfg_ctx = config_store.get("model.context_window")
    if detected_ctx:
        context_window = detected_ctx
        log.info("Context window: %s (detected from backend)", f"{context_window:,}")
    elif cfg_ctx:  # 0 = auto-detect (no override)
        context_window = cfg_ctx
    else:
        context_window = 32768

    # Build model registry (reads [models.*] sections from config.toml)
    from turnstone.core.model_registry import load_model_registry

    registry = load_model_registry(
        base_url=base_url,
        api_key=api_key,
        model=model,
        context_window=context_window,
        provider=provider_name,
    )

    # Initialize MCP client (connects to configured MCP servers, if any)
    from turnstone.core.mcp_client import create_mcp_client
    from turnstone.core.storage._registry import get_storage as _get_storage

    mcp_config_cli = args.mcp_config  # CLI-only (no config.toml for this)
    mcp_client = create_mcp_client(
        mcp_config_cli or config_store.get("mcp.config_path") or None,
        refresh_interval=config_store.get("mcp.refresh_interval"),
        storage=_get_storage(),
    )

    # Backend health monitor with circuit breaker
    from turnstone.core.healthcheck import BackendHealthMonitor

    health_monitor = BackendHealthMonitor(
        client=client,
        probe_interval=config_store.get("health.backend_probe_interval"),
        probe_timeout=config_store.get("health.backend_probe_timeout"),
        failure_threshold=config_store.get("health.circuit_breaker_threshold"),
        cooldown=config_store.get("health.circuit_breaker_cooldown"),
    )
    health_monitor.start()

    # Per-IP rate limiter
    from turnstone.core.ratelimit import RateLimiter

    rate_limiter = RateLimiter(
        enabled=config_store.get("ratelimit.enabled"),
        rate=config_store.get("ratelimit.requests_per_second"),
        burst=config_store.get("ratelimit.burst"),
        trusted_proxies=config_store.get("ratelimit.trusted_proxies"),
    )

    # Set up global event queue for state-change broadcasts
    global_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=10000)
    global_listeners: list[queue.Queue[dict[str, Any]]] = []
    global_listeners_lock = threading.Lock()
    WebUI._global_queue = global_queue

    # Config builders — shared between startup logging and session factory.
    # Re-read from ConfigStore each call so hot-reload works.
    from turnstone.core.judge import JudgeConfig
    from turnstone.core.memory_relevance import MemoryConfig

    def _build_judge_config() -> JudgeConfig:
        return JudgeConfig(
            enabled=config_store.get("judge.enabled"),
            model=config_store.get("judge.model"),
            provider=config_store.get("judge.provider"),
            base_url=config_store.get("judge.base_url"),
            api_key=config_store.get("judge.api_key"),
            confidence_threshold=config_store.get("judge.confidence_threshold"),
            max_context_ratio=config_store.get("judge.max_context_ratio"),
            timeout=config_store.get("judge.timeout"),
            read_only_tools=config_store.get("judge.read_only_tools"),
            output_guard=config_store.get("judge.output_guard"),
            redact_secrets=config_store.get("judge.redact_secrets"),
        )

    def _build_memory_config() -> MemoryConfig:
        return MemoryConfig(
            relevance_k=config_store.get("memory.relevance_k"),
            fetch_limit=config_store.get("memory.fetch_limit"),
            max_content=config_store.get("memory.max_content"),
            nudge_cooldown=config_store.get("memory.nudge_cooldown"),
            nudges=config_store.get("memory.nudges"),
        )

    judge_config = _build_judge_config()
    if judge_config.enabled:
        log.info(
            "Judge: enabled (model=%s, threshold=%.2f)",
            judge_config.model or model,
            judge_config.confidence_threshold,
        )

    # Session factory — captures shared config (including config_store for hot-reload)
    def session_factory(
        ui: SessionUI | None,
        model_alias: str | None = None,
        ws_id: str | None = None,
        *,
        skill: str | None = None,
    ) -> ChatSession:
        assert ui is not None
        r_client, r_model, r_cfg = registry.resolve(model_alias)
        uid = getattr(ui, "_user_id", "") or ""

        # Re-resolve from ConfigStore so new workstreams pick up hot-reloaded settings.
        live_memory_config = _build_memory_config()
        live_judge_config = _build_judge_config()

        return ChatSession(
            client=r_client,
            model=r_model,
            ui=ui,
            instructions=config_store.get("session.instructions") or None,
            temperature=config_store.get("model.temperature"),
            max_tokens=config_store.get("model.max_tokens"),
            tool_timeout=config_store.get("tools.timeout"),
            reasoning_effort=config_store.get("model.reasoning_effort"),
            context_window=r_cfg.context_window,
            compact_max_tokens=config_store.get("session.compact_max_tokens"),
            auto_compact_pct=config_store.get("session.auto_compact_pct"),
            agent_max_turns=config_store.get("tools.agent_max_turns"),
            tool_truncation=config_store.get("tools.truncation"),
            mcp_client=mcp_client,
            registry=registry,
            model_alias=model_alias or registry.default,
            health_monitor=health_monitor,
            node_id=_node_id,
            ws_id=ws_id,
            tool_search=config_store.get("tools.search"),
            tool_search_threshold=config_store.get("tools.search_threshold"),
            tool_search_max_results=config_store.get("tools.search_max_results"),
            web_search_backend=config_store.get("tools.web_search_backend"),
            skill=skill or args.skill or None,
            judge_config=live_judge_config,
            user_id=uid,
            memory_config=live_memory_config,
            config_store=config_store,
        )

    # Create WatchRunner (periodic command polling, server-level)
    from turnstone.core.storage import get_storage as _get_storage
    from turnstone.core.watch import WatchRunner

    # Create workstream manager first (watch restore_fn captures it)
    manager = WorkstreamManager(
        session_factory,
        max_workstreams=config_store.get("server.max_workstreams"),
        node_id=_node_id,
    )
    WebUI._workstream_mgr = manager

    def _watch_restore_fn(ws_id: str) -> Any:
        """Restore an evicted workstream so a watch can deliver results.

        Returns a callable that starts a worker thread to send() the watch
        result.  Unlike the normal dispatch path (which enqueues for IDLE
        drain), the restored workstream has no active send() loop, so we
        must start a worker thread directly — same pattern as send_message().
        """
        try:
            ws = manager.create(
                ui_factory=lambda wid: WebUI(ws_id=wid),
            )
            # Restored workstreams run unattended — auto-approve tool calls
            # to avoid blocking forever on approval with no connected user.
            if isinstance(ws.ui, WebUI):
                ws.ui.auto_approve = True
            if ws.session:
                ws.session.resume(ws_id)
                dispatch_fn = _make_watch_dispatch(ws, ws.session, ws.ui)
                ws.session.set_watch_runner(_watch_runner, dispatch_fn=dispatch_fn)
                return dispatch_fn
        except RuntimeError:
            log.warning("watch_restore: cannot restore ws %s (all slots active)", ws_id)
        return None

    _watch_runner = WatchRunner(
        storage=_get_storage(),
        node_id=_node_id,
        tool_timeout=config_store.get("tools.timeout"),
        restore_fn=_watch_restore_fn,
    )
    ws = manager.create(
        name="default",
        ui_factory=lambda wid: WebUI(ws_id=wid),
    )
    assert isinstance(ws.ui, WebUI)
    if config_store.get("tools.skip_permissions"):
        ws.ui.auto_approve = True

    # Handle --resume
    assert ws.session is not None
    ws.session.set_watch_runner(
        _watch_runner, dispatch_fn=_make_watch_dispatch(ws, ws.session, ws.ui)
    )
    if args.resume:
        from turnstone.core.memory import resolve_workstream

        target_id = resolve_workstream(args.resume)
        if not target_id:
            log.error("Workstream not found: %s", args.resume)
            sys.exit(1)
        if not ws.session.resume(target_id):
            log.error("Workstream '%s' has no messages.", args.resume)
            sys.exit(1)
        log.info("Resumed workstream %s (%d messages)", target_id, len(ws.session.messages))

    # Record detected model and judge status in metrics
    _metrics.model = model
    _metrics.set_judge_enabled(judge_config.enabled if judge_config else False)

    # Auth config
    from turnstone.core.auth import load_auth_config, load_jwt_secret
    from turnstone.core.storage import get_storage

    auth_config = load_auth_config()
    jwt_secret = load_jwt_secret() if auth_config.enabled else ""
    if auth_config.enabled:
        log.info("Auth: enabled (%d config token(s))", len(auth_config.tokens))

    # Build the ASGI app
    from turnstone.core.web_helpers import parse_cors_origins

    cors_origins = parse_cors_origins()

    _skip_perms = config_store.get("tools.skip_permissions")
    app = create_app(
        workstreams=manager,
        global_queue=global_queue,
        global_listeners=global_listeners,
        global_listeners_lock=global_listeners_lock,
        skip_permissions=_skip_perms,
        auth_config=auth_config,
        jwt_secret=jwt_secret,
        auth_storage=get_storage(),
        health_monitor=health_monitor,
        rate_limiter=rate_limiter,
        mcp_client=mcp_client,
        registry=registry,
        idle_timeout=config_store.get("server.workstream_idle_timeout"),
        node_id=_node_id,
        cors_origins=cors_origins,
        watch_runner=_watch_runner,
        judge_config=judge_config,
        config_store=config_store,
    )

    log.info("Server starting on http://%s:%s", args.host, args.port)
    log.info("Model: %s", model)
    if registry.count > 1:
        others = [a for a in registry.list_aliases() if a != registry.default]
        log.info("Models: %s (default), %s", registry.default, ", ".join(others))
    if mcp_client:
        mcp_tools = mcp_client.get_tools()
        if mcp_tools:
            log.info("MCP tools: %d from %d server(s)", len(mcp_tools), mcp_client.server_count)
        mcp_client.set_storage(get_storage())
    log.info(
        "Health monitor: probe every %ss, circuit breaker threshold=%s",
        config_store.get("health.backend_probe_interval"),
        config_store.get("health.circuit_breaker_threshold"),
    )
    if rate_limiter.enabled:
        log.info(
            "Rate limiter: %s req/s, burst=%s",
            config_store.get("ratelimit.requests_per_second"),
            config_store.get("ratelimit.burst"),
        )
    log.info("Max workstreams: %s", config_store.get("server.max_workstreams"))
    log.info("Node ID: %s", _node_id)

    # TLS: request cert from console ACME if enabled
    ssl_kwargs: dict[str, Any] = {}
    if config_store.get("tls.enabled"):
        try:
            import asyncio
            import socket
            import tempfile

            from turnstone.core.tls import TLSClient

            hostname = socket.getfqdn()
            hostnames = [hostname, "localhost", "127.0.0.1"]
            # Only add bind host if it's a concrete address
            if args.host not in ("0.0.0.0", "::", ""):
                hostnames.append(args.host)
            tls_client = TLSClient(
                storage=get_storage(),
                hostnames=hostnames,
            )
            asyncio.run(tls_client.init())
            bundle = tls_client.bundle
            if bundle:
                # Write PEM to temp files for uvicorn (restricted permissions)
                _tls_temp_files: list[str] = []

                def _write_pem(data: bytes, suffix: str = ".pem") -> str:
                    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
                        f.write(data)
                        name = f.name
                    os.chmod(name, 0o600)
                    _tls_temp_files.append(name)
                    return name

                ssl_kwargs["ssl_certfile"] = _write_pem(bundle.fullchain_pem)
                ssl_kwargs["ssl_keyfile"] = _write_pem(bundle.key_pem)
                if tls_client.ca_pem:
                    ssl_kwargs["ssl_ca_certs"] = _write_pem(tls_client.ca_pem)
                    import ssl as _ssl

                    ssl_kwargs["ssl_cert_reqs"] = _ssl.CERT_REQUIRED

                # Store client on app state for lifespan renewal
                app.state.tls_client = tls_client

                # Clean up temp files on exit
                import atexit

                def _cleanup_tls_files() -> None:
                    import contextlib

                    for path in _tls_temp_files:
                        with contextlib.suppress(OSError):
                            os.unlink(path)

                atexit.register(_cleanup_tls_files)
                log.info("TLS enabled — serving HTTPS")
            else:
                log.warning("TLS enabled but no cert available")
        except Exception:
            log.warning("TLS initialization failed — serving plain HTTP", exc_info=True)

    print("Press Ctrl+C to stop.")

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning", **ssl_kwargs)


if __name__ == "__main__":
    main()
