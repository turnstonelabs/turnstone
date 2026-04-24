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
import collections
import contextlib
import functools
import hashlib
import json
import os
import queue
import re
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
from turnstone.core.adapters.interactive_adapter import InteractiveAdapter
from turnstone.core.auth import (
    DENY_EMPTY_SUB,
    JWT_AUD_SERVER,
    AuthMiddleware,
    _DenyFilter,
    jwt_version_slot,
)
from turnstone.core.log import get_logger
from turnstone.core.metrics import metrics as _metrics
from turnstone.core.ratelimit import resolve_client_ip
from turnstone.core.session import ChatSession, GenerationCancelled, SessionUI  # noqa: F401
from turnstone.core.session_manager import SessionManager
from turnstone.core.session_routes import (
    AttachmentHandlers,
    SharedSessionVerbHandlers,
    register_session_routes,
)
from turnstone.core.session_ui_base import SessionUIBase
from turnstone.core.tools import TOOLS  # noqa: F401 — available for introspection
from turnstone.core.web_helpers import version_html as _version_html
from turnstone.core.workstream import (
    Workstream,
    WorkstreamKind,
    WorkstreamState,
)
from turnstone.prompts import ClientType

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, MutableMapping

    from starlette.types import ASGIApp, Receive, Scope, Send

# ---------------------------------------------------------------------------
# Static assets — loaded once at startup from turnstone/ui/static/
# ---------------------------------------------------------------------------

log = get_logger(__name__)

_STATIC_DIR = Path(__file__).parent / "ui" / "static"
_SHARED_DIR = Path(__file__).parent / "shared_static"
_HTML = _version_html((_STATIC_DIR / "index.html").read_text(encoding="utf-8"))
_HTML_ETAG = '"' + hashlib.md5(_HTML.encode()).hexdigest()[:16] + '"'  # noqa: S324
_VALID_WS_ID = re.compile(r"^[0-9a-f]{32}$")


# ---------------------------------------------------------------------------
# WebUI — implements SessionUI for browser-based interaction
# ---------------------------------------------------------------------------

_MAX_TURN_CONTENT_CHARS = 256 * 1024  # cap piggybacked content on idle events

# Orphan-attachment-reservation sweep cadence.  Threshold is measured
# against the storage layer's `reserved_at` column (time the row last
# transitioned into reserved state, NOT upload time), so a 1-hour cap
# is safely longer than any realistic single send without risking the
# unreservation of attachments uploaded long ago but reserved fresh.
_ORPHAN_SWEEP_INTERVAL_S = 30 * 60
_ORPHAN_SWEEP_THRESHOLD_S = 1 * 3600


class WebUI(SessionUIBase):
    """Browser-based UI using SSE for streaming and HTTP POST for actions.

    Implements the SessionUI protocol from turnstone.core.session.
    Each workstream gets its own WebUI instance.
    """

    # Shared global event queue for state-change broadcasts across all
    # workstreams.  Set by main() before any WebUI instances are created.
    _global_queue: queue.Queue[dict[str, Any]] | None = None  # bounded in main()
    _workstream_mgr: SessionManager | None = None

    def __init__(
        self,
        ws_id: str = "",
        user_id: str = "",
        *,
        kind: WorkstreamKind = WorkstreamKind.INTERACTIVE,
        parent_ws_id: str | None = None,
    ) -> None:
        super().__init__(ws_id=ws_id, user_id=user_id)
        # Cached for broadcast event payloads — both are immutable for
        # the lifetime of the workstream, so locking the manager on
        # every state/activity tick to re-read them burns lock budget.
        self._kind = kind
        # Normalize empty string to None at the UI boundary so the
        # invariant "parent_ws_id is either a non-empty string or None"
        # holds in every ws_state/ws_activity event payload — mirrors
        # the storage-layer normalization at register_workstream.
        self._parent_ws_id = parent_ws_id if parent_ws_id else None
        # Content accumulator — tokens appended in on_content_token(),
        # joined and piggybacked onto the ws_state:idle global SSE
        # event, then reset. WebUI-specific optimization; coord sessions
        # don't broadcast rich ws_state payloads.
        self._ws_turn_content: list[str] = []
        self._ws_turn_content_size: int = 0

    # ``_enqueue`` / ``_register_listener`` / ``_unregister_listener``
    # inherited from :class:`SessionUIBase`.

    def _ws_kind_and_parent(self) -> tuple[WorkstreamKind, str | None]:
        """Return cached (kind, parent_ws_id) for broadcast event payloads.

        Stored on the UI at construction time — both fields are
        immutable for the lifetime of the workstream, so re-reading
        them from the manager under lock on every broadcast was a
        process-wide serialization tax on every activity tick.
        """
        return self._kind, self._parent_ws_id

    def _broadcast_state(self, state: str) -> None:
        """Send a state-change event to the global SSE channel."""
        if WebUI._global_queue is not None:
            with self._ws_lock:
                tokens = self._ws_prompt_tokens + self._ws_completion_tokens
                ctx = self._ws_context_ratio
                activity = self._ws_current_activity
                activity_state = self._ws_activity_state
            kind, parent_ws_id = self._ws_kind_and_parent()
            event: dict[str, Any] = {
                "type": "ws_state",
                "ws_id": self.ws_id,
                "state": state,
                "tokens": tokens,
                "context_ratio": ctx,
                "activity": activity,
                "activity_state": activity_state,
                "kind": kind,
                "parent_ws_id": parent_ws_id,
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
            kind, parent_ws_id = self._ws_kind_and_parent()
            with contextlib.suppress(queue.Full):
                WebUI._global_queue.put_nowait(
                    {
                        "type": "ws_activity",
                        "ws_id": self.ws_id,
                        "activity": activity,
                        "activity_state": activity_state,
                        "kind": kind,
                        "parent_ws_id": parent_ws_id,
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
        self._reset_approval_cycle()
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

    def on_tool_result(
        self,
        call_id: str,
        name: str,
        output: str,
        *,
        is_error: bool = False,
    ) -> None:
        _metrics.record_tool_call(name)
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
            turn_tool_calls = self._ws_turn_tool_calls
            turn_count = self._ws_messages
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
                "tool_calls_this_turn": turn_tool_calls,
                "turn_count": turn_count,
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
        self._pending_plan_review = {"type": "plan_review", "content": content}
        self._enqueue(self._pending_plan_review)
        if not self._plan_event.wait(timeout=3600):
            log.warning("Plan review timed out for ws_id=%s", self.ws_id)
            self._plan_result = ""
        self._pending_plan_review = None
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
        # Also send to per-workstream listeners so the browser UI can track
        # busy/idle transitions (stream_end fires per-segment, not per-turn).
        self._enqueue({"type": "state_change", "state": state})

    def on_rename(self, name: str) -> None:
        """Update the workstream's display name and broadcast to all clients."""
        if WebUI._global_queue is not None:
            with contextlib.suppress(queue.Full):
                WebUI._global_queue.put_nowait(
                    {"type": "ws_rename", "ws_id": self.ws_id, "name": name}
                )

    def on_intent_verdict(self, verdict: dict[str, Any]) -> None:
        """Extend :meth:`SessionUIBase.on_intent_verdict` with a
        node-level prometheus metric update.
        """
        super().on_intent_verdict(verdict)
        _metrics.record_judge_verdict(
            verdict.get("tier", "llm"),
            verdict.get("risk_level", "medium"),
            verdict.get("latency_ms", 0),
        )

    # ``on_output_warning`` inherited from :class:`SessionUIBase`.

    # ``resolve_approval`` / ``resolve_plan`` inherited from
    # :class:`SessionUIBase`. Intent-verdict decision propagation lives
    # in the base now — both interactive and coord share the same
    # bookkeeping.


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
        content = msg.get("content")
        attachments_meta: list[dict[str, Any]] = []
        # User messages with attachments carry list content (text +
        # image_url / document parts).  The UI wants a plain-text bubble
        # plus a derived pill cluster — split the list content here so
        # the client never has to interpret provider-shaped parts.
        if msg.get("role") == "user" and isinstance(content, list):
            text_parts: list[str] = []
            for part in content:
                if not isinstance(part, dict):
                    continue
                ptype = part.get("type")
                if ptype == "text":
                    text_parts.append(str(part.get("text", "")))
                elif ptype == "image_url":
                    attachments_meta.append({"kind": "image", "filename": "", "mime_type": ""})
                elif ptype == "document":
                    d = part.get("document", {})
                    attachments_meta.append(
                        {
                            "kind": "text",
                            "filename": str(d.get("name", "")),
                            "mime_type": str(d.get("media_type", "")),
                        }
                    )
            content = "\n".join(text_parts)
        # Prefer the authoritative side-channel (set by
        # reconstruct_messages on history replay) — it carries image
        # filenames that the image_url part itself can't express.
        side_meta = msg.get("_attachments_meta")
        if isinstance(side_meta, list) and side_meta:
            attachments_meta = [
                {
                    "kind": str(m.get("kind") or ""),
                    "filename": str(m.get("filename") or ""),
                    "mime_type": str(m.get("mime_type") or ""),
                }
                for m in side_meta
                if isinstance(m, dict)
            ]
        entry = {"role": msg["role"], "content": content}
        if attachments_meta:
            entry["attachments"] = attachments_meta
        if msg.get("tool_calls"):
            entry["tool_calls"] = [
                {
                    "id": tc.get("id", ""),
                    "name": tc["function"]["name"],
                    "arguments": tc["function"].get("arguments", ""),
                }
                for tc in msg["tool_calls"]
            ]
        # Detect denied/blocked/errored tool results by their content prefix.
        if msg.get("role") == "tool":
            content = msg.get("content", "")
            if isinstance(content, str):
                if content.startswith("Denied by user") or content.startswith("Blocked"):
                    entry["denied"] = True
                # Use persisted flag if available, fall back to text
                # heuristic for historical data that predates is_error.
                if (
                    msg.get("is_error")
                    or content.startswith("Error")
                    or content.startswith("Command timed out")
                    or content.startswith("Search timed out")
                    or content.startswith("Unknown tool:")
                    or content.startswith("JSON parse error:")
                    or content.startswith("MCP prompt timed out")
                    or content.startswith("MCP prompt error")
                ):
                    entry["is_error"] = True
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


def _get_ws(mgr: SessionManager, ws_id: str | None) -> tuple[Workstream, WebUI] | tuple[None, None]:
    """Look up workstream by id.  Returns (Workstream, WebUI) or (None, None)."""
    if not ws_id:
        return None, None
    ws = mgr.get(ws_id)
    if ws and ws.ui:
        ui: WebUI = ws.ui  # type: ignore[assignment]
        return ws, ui
    return None, None


def _audit_context(request: Request) -> tuple[str, str]:
    """Extract (user_id, ip_address) from request for audit logging."""
    auth = getattr(getattr(request, "state", None), "auth_result", None)
    uid: str = auth.user_id if auth else ""
    ip = ""
    if request.client:
        ip = request.client.host
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        from turnstone.core.auth import is_secure_request

        if is_secure_request(dict(request.headers), request.url.scheme):
            ip = forwarded.split(",")[0].strip()
    return uid, ip


# ---------------------------------------------------------------------------
# Route handlers — all async
# ---------------------------------------------------------------------------


async def index(request: Request) -> Response:
    """GET / — serve the embedded HTML client."""
    if request.headers.get("If-None-Match") == _HTML_ETAG:
        return Response(status_code=304, headers={"ETag": _HTML_ETAG, "Cache-Control": "no-cache"})
    resp = HTMLResponse(_HTML)
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["ETag"] = _HTML_ETAG
    return resp


async def events_sse(request: Request) -> Response:
    """GET /v1/api/events — per-workstream SSE event stream."""
    mgr = request.app.state.workstreams
    ws_id = request.query_params.get("ws_id")
    # Subscribing to another tenant's stream would leak their messages,
    # tool calls, and pending approvals in real time.  Gate before
    # _register_listener so non-owners get a flat 404 (no enumeration).
    # In-memory fast path via mgr= keeps SSE resilient to DB blips.
    _owner, err = _require_ws_access(request, ws_id or "", mgr=mgr)
    if err:
        return err
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
        # Replay last status so the per-pane status bar populates on resume
        if session._last_usage is not None:
            u = session._last_usage
            total_tok = u["prompt_tokens"] + u["completion_tokens"]
            cw = session.context_window
            pct = total_tok / cw * 100 if cw > 0 else 0
            with ui._ws_lock:
                turn_tool_calls = ui._ws_turn_tool_calls
                turn_count = ui._ws_messages
            yield {
                "data": json.dumps(
                    {
                        "type": "status",
                        "prompt_tokens": u["prompt_tokens"],
                        "completion_tokens": u["completion_tokens"],
                        "total_tokens": total_tok,
                        "context_window": cw,
                        "pct": round(pct, 1),
                        "effort": session.reasoning_effort,
                        "cache_creation_tokens": u.get("cache_creation_tokens", 0),
                        "cache_read_tokens": u.get("cache_read_tokens", 0),
                        "tool_calls_this_turn": turn_tool_calls,
                        "turn_count": turn_count,
                    }
                )
            }
        # History replay
        history = _build_history(session, has_pending_approval=ui._pending_approval is not None)
        if history:
            yield {"data": json.dumps({"type": "history", "messages": history})}
        # Re-inject pending approval or plan review
        if ui._pending_approval is not None:
            yield {"data": json.dumps(ui._pending_approval)}
            # Replay any LLM verdicts received since the approval was sent
            with ui._ws_lock:
                cached_verdicts = list(ui._llm_verdicts.values())
            for v in cached_verdicts:
                yield {"data": json.dumps({"type": "intent_verdict", **v})}
        if ui._pending_plan_review is not None:
            yield {"data": json.dumps(ui._pending_plan_review)}

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
                    pass  # poll timeout, retry
        finally:
            _metrics.record_sse_disconnect()
            ui._unregister_listener(client_queue)

    return EventSourceResponse(event_generator(), ping=5)


def _build_node_snapshot(app_state: Any) -> dict[str, Any]:
    """Build a complete node state snapshot for SSE consumers.

    Includes workstream list, health, and aggregate — everything the console
    collector needs to populate a ``NodeSnapshot`` without polling.
    """
    from turnstone.core.memory import get_workstream_display_name

    mgr: SessionManager = app_state.workstreams
    wss = mgr.list_all()
    total_tokens = 0
    total_tool_calls = 0
    active_count = 0
    ws_list = []
    for ws in wss:
        ui = ws.ui
        if hasattr(ui, "_ws_lock"):
            with ui._ws_lock:  # type: ignore[union-attr]
                tok = ui._ws_prompt_tokens + ui._ws_completion_tokens  # type: ignore[union-attr]
                tc = sum(ui._ws_tool_calls.values())  # type: ignore[union-attr]
                ctx = ui._ws_context_ratio  # type: ignore[union-attr]
                activity = ui._ws_current_activity  # type: ignore[union-attr]
                activity_state = ui._ws_activity_state  # type: ignore[union-attr]
        else:
            tok = tc = 0
            ctx = 0.0
            activity = activity_state = ""
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
                "name": title or ws.name,
                "state": ws.state.value,
                "title": title,
                "tokens": tok,
                "context_ratio": round(ctx, 3),
                "activity": activity,
                "activity_state": activity_state,
                "tool_calls": tc,
                "model": ws.session.model if ws.session else "",
                "model_alias": ws.session.model_alias if ws.session else "",
                "kind": ws.kind,
                "parent_ws_id": ws.parent_ws_id,
                "user_id": ws.user_id,
            }
        )
    return {
        "type": "node_snapshot",
        "node_id": getattr(app_state, "node_id", ""),
        "workstreams": ws_list,
        "health": _build_health_dict(app_state),
        "aggregate": {
            "total_tokens": total_tokens,
            "total_tool_calls": total_tool_calls,
            "active_count": active_count,
            "total_count": len(ws_list),
        },
    }


async def global_events_sse(request: Request) -> Response:
    """GET /v1/api/events/global — global SSE event stream.

    Supports optional ``?expected_node_id=X`` query parameter for node identity
    verification.  If present and the server's node_id does not match, returns
    409 Conflict immediately.

    On connect, emits a ``node_snapshot`` event with the full node state
    (workstreams, health, aggregate) followed by real-time delta events.
    The snapshot and listener registration are atomic — no events are lost.
    """
    # -- Service-scope gate ---------------------------------------------------
    # The global stream carries cluster-wide workstream inventory across
    # every tenant (user_id, kind, parent_ws_id, token counts) — intended
    # for the console's ClusterCollector, not end-user browsers.  Require
    # a service-scoped token so an authenticated end-user can't subscribe
    # and observe cluster state for other tenants.
    if "service" not in _auth_scopes(request):
        return JSONResponse({"error": "service scope required"}, status_code=403)

    # -- Node identity check --------------------------------------------------
    expected = request.query_params.get("expected_node_id")
    actual_node_id = getattr(request.app.state, "node_id", "")
    if expected and expected != actual_node_id:
        return JSONResponse(
            {
                "error": "node_id mismatch" if actual_node_id else "node_id unavailable",
                "expected": expected,
                "actual": actual_node_id,
            },
            status_code=409,
        )

    # -- Atomic snapshot + listener registration ------------------------------
    client_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1000)
    listeners = request.app.state.global_listeners
    listeners_lock = request.app.state.global_listeners_lock

    # Hold the listeners lock while building the snapshot AND registering.
    # The fanout thread also acquires this lock when snapshotting the listener
    # list, so events that land on global_queue during snapshot build will be
    # distributed to our queue after we release — gap-free.
    with listeners_lock:
        snapshot = _build_node_snapshot(request.app.state)
        listeners.append(client_queue)

    async def event_generator() -> AsyncGenerator[dict[str, str], None]:
        _metrics.record_sse_connect()
        try:
            # Emit snapshot as first event
            yield {"data": json.dumps(snapshot)}
            loop = asyncio.get_running_loop()
            executor = request.app.state.sse_executor
            while True:
                try:
                    event = await loop.run_in_executor(
                        executor, functools.partial(client_queue.get, timeout=5)
                    )
                    yield {"data": json.dumps(event)}
                except queue.Empty:
                    pass  # poll timeout, retry
        finally:
            _metrics.record_sse_disconnect()
            with listeners_lock:
                if client_queue in listeners:
                    listeners.remove(client_queue)

    return EventSourceResponse(event_generator(), ping=5)


async def list_workstreams(request: Request) -> JSONResponse:
    """GET /v1/api/workstreams — list workstreams visible to any
    authenticated caller.

    Trusted-team visibility: listing returns the full cluster set
    across all owners (no per-user filter).  Self-hosted deployments
    are the assumed shape; console operators already see cluster-wide
    state via service scope, and a per-user filter would also hide
    ownerless rows like the auto-created ``name="default"`` startup
    workstream from every authenticated caller.

    Per-workstream mutations (``/send``, ``/close``, ``/open``,
    ``/title``, ``/delete``, ``/refresh-title``) keep their independent
    ownership checks — see those handlers for the cross-tenant guards
    that stay in force.  This endpoint exposes metadata only (name,
    state, kind, parent_ws_id); message history requires the per-
    workstream ownership gate on ``/history``.

    For a multi-tenant SaaS deployment, the right boundary is a real
    ``tenant_id`` column with row-level filtering at the storage
    layer, not an empty-user_id heuristic.
    """
    from turnstone.core.memory import get_workstream_display_name

    mgr: SessionManager = request.app.state.workstreams
    result = []
    for ws in mgr.list_all():
        title = get_workstream_display_name(ws.id) or ws.name
        # kind + parent_ws_id mirror the shape /dashboard returns below so
        # client consumers (SDK, frontend, integrators) see one consistent
        # row schema across adjacent endpoints instead of a subset here
        # and a superset there.
        result.append(
            {
                "id": ws.id,
                "name": title,
                "state": ws.state.value,
                "kind": ws.kind,
                "parent_ws_id": ws.parent_ws_id,
            }
        )
    return JSONResponse({"workstreams": result})


async def dashboard(request: Request) -> JSONResponse:
    """GET /v1/api/dashboard — enriched workstream data + aggregate stats."""
    from turnstone.core.memory import get_workstream_display_name

    mgr: SessionManager = request.app.state.workstreams
    # No per-user filter — see list_workstreams above for the rationale
    # (trusted-team deployment shape; mutations stay owner-gated).
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
                "name": title or ws.name,
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
                "kind": ws.kind,
                "parent_ws_id": ws.parent_ws_id,
                "user_id": ws.user_id,
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
    """GET /v1/api/workstreams/saved — list saved interactive workstream
    metadata, visible to any authenticated caller.

    Trusted-team visibility: returns the cluster-wide set across all
    owners (no per-user filter) — see ``list_workstreams`` for the
    rationale.  This endpoint exposes summary fields only (ws_id,
    alias, title, name, created, updated, message_count); message
    history requires the per-workstream ownership gate on
    ``/v1/api/workstreams/{ws_id}/history``.

    Resuming an owned saved workstream goes through ``/open``'s
    ownership check; ownerless persisted rows (legacy / migration /
    startup ``name="default"``) are claimable by any authenticated
    caller via ``/open``, consistent with the same trusted-team model.

    Restricted to ``kind="interactive"`` — the interactive UI's "saved
    workstreams" sidebar is not a coordinator surface, and coordinator
    rows (which persist conversation history too) would otherwise leak
    into it.
    """
    from turnstone.core.memory import list_workstreams_with_history
    from turnstone.core.workstream import WorkstreamKind

    rows = list_workstreams_with_history(
        limit=50,
        kind=WorkstreamKind.INTERACTIVE,
        user_id=None,
    )
    result = [
        {
            "ws_id": wid,
            "alias": alias,
            "title": title,
            "name": name,
            "created": created,
            "updated": updated,
            "message_count": count,
        }
        for wid, alias, title, name, created, updated, count, *_extra in rows
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


async def list_available_models(request: Request) -> JSONResponse:
    """GET /v1/api/models — list available model aliases."""
    registry = getattr(request.app.state, "registry", None)
    if registry is None:
        return JSONResponse({"models": []})
    models = []
    for alias in registry.list_aliases():
        cfg = registry.get_config(alias)
        models.append(
            {
                "alias": cfg.alias,
                "model": cfg.model,
                "provider": cfg.provider,
            }
        )
    # Include effective defaults for clients (web UI, channel gateway).
    cs = getattr(request.app.state, "config_store", None)
    default_alias = ""
    channel_default_alias = ""
    if cs is not None:
        default_alias = cs.get("model.default_alias") or ""
        channel_default_alias = cs.get("channels.default_model_alias") or ""
    if not default_alias:
        default_alias = registry.default
    # Clear defaults that point to unknown/disabled aliases.
    enabled_aliases = set(registry.list_aliases())
    if default_alias and default_alias not in enabled_aliases:
        default_alias = ""
    if channel_default_alias and channel_default_alias not in enabled_aliases:
        channel_default_alias = ""
    return JSONResponse(
        {
            "models": models,
            "default_alias": default_alias,
            "channel_default_alias": channel_default_alias,
        }
    )


def _count_ws_states(wss: list[Workstream]) -> dict[str, int]:
    """Count workstream states for health/metrics endpoints."""
    counts = dict.fromkeys(("idle", "thinking", "running", "attention", "error"), 0)
    for ws in wss:
        counts[ws.state.value] = counts.get(ws.state.value, 0) + 1
    return counts


def _build_health_dict(app_state: Any) -> dict[str, Any]:
    """Assemble health status dict from app state.

    Shared by the ``/health`` endpoint and the global SSE snapshot.
    """
    mgr: SessionManager = app_state.workstreams
    wss = mgr.list_all()
    states = _count_ws_states(wss)
    health_reg = getattr(app_state, "health_registry", None)
    registry = getattr(app_state, "registry", None)
    tracker = None
    if health_reg and registry:
        # Prefer ConfigStore runtime override, fall back to registry default
        config_store = getattr(app_state, "config_store", None)
        effective_alias = None
        if config_store:
            effective_alias = config_store.get("model.default_alias") or None
        if effective_alias:
            tracker = health_reg.get_tracker_for_alias(registry, effective_alias)
        if tracker is None:
            tracker = health_reg.get_tracker_for_alias(registry, registry.default)
    backend_ok = tracker.is_healthy if tracker else True
    data: dict[str, Any] = {
        "status": "ok" if backend_ok else "degraded",
        "version": __version__,
        "node_id": getattr(app_state, "node_id", ""),
        "uptime_seconds": round(time.monotonic() - _metrics.start_time, 2),
        "model": _metrics.model,
        "max_ws": mgr.max_active,
        "workstreams": {"total": len(wss), **states},
        "backend": {
            "status": "up" if backend_ok else "down",
        },
    }
    mc = getattr(app_state, "mcp_client", None)
    if mc:
        data["mcp"] = {
            "servers": mc.server_count,
            "resources": mc.resource_count,
            "prompts": mc.prompt_count,
        }
    return data


async def health(request: Request) -> JSONResponse:
    """GET /health — server health status."""
    return JSONResponse(_build_health_dict(request.app.state))


async def metrics_endpoint(request: Request) -> Response:
    """GET /metrics — Prometheus text exposition format."""
    mgr: SessionManager = request.app.state.workstreams
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
        with ws._lock:
            if ws.worker_thread and ws.worker_thread.is_alive():
                # Workstream is busy — queue for drain at IDLE (Path A)
                try:
                    pending.put_nowait({"message": msg})
                except queue.Full:
                    log.warning(
                        "Watch pending queue full, dropping result for ws %s",
                        ws.id,
                    )
                return

            # Workstream is idle — start a worker thread (Path B)
            # Mirrors the send_message() run() pattern for proper cleanup.
            def run() -> None:
                me = threading.current_thread()
                try:
                    session.send(msg)
                except GenerationCancelled:
                    if ws.worker_thread is me and ui:
                        ui.on_stream_end()
                        ui.on_state_change("idle")
                except Exception as exc:
                    if ws.worker_thread is me and ui:
                        ui.on_error(f"Watch error: {exc}")
                        ui.on_stream_end()
                        ui.on_state_change("error")

            t = threading.Thread(target=run, daemon=True)
            ws.worker_thread = t
            t.start()

    return dispatch


async def send_message(request: Request) -> JSONResponse:
    """POST /v1/api/send — send or queue a user message.

    DELETE /v1/api/send — remove a queued message by ``msg_id``.
    """
    from turnstone.core.web_helpers import read_json_or_400

    body = await read_json_or_400(request)
    if isinstance(body, JSONResponse):
        return body

    # DELETE — remove a queued message
    if request.method == "DELETE":
        ws_id = body.get("ws_id")
        msg_id = body.get("msg_id")
        if not msg_id:
            return JSONResponse({"error": "msg_id required"}, status_code=400)
        mgr = request.app.state.workstreams
        ws, ui = _get_ws(mgr, ws_id)
        if not ws or not ui:
            return JSONResponse({"error": "Unknown workstream"}, status_code=404)
        session = ws.session
        if session is None:
            return JSONResponse({"error": "No session"}, status_code=400)
        removed = session.dequeue_message(msg_id)
        return JSONResponse({"status": "removed" if removed else "not_found"})

    # POST — send or queue
    message = body.get("message", "").strip()
    ws_id = body.get("ws_id")
    if not message:
        return JSONResponse({"error": "Empty message"}, status_code=400)
    mgr = request.app.state.workstreams
    ws, ui = _get_ws(mgr, ws_id)
    if not ws or not ui:
        return JSONResponse({"error": "Unknown workstream"}, status_code=404)

    # --- Atomic reserve-then-dispatch for attachments ---------------------
    # Generate a send token up front and reserve attachments BEFORE we
    # commit to queueing or starting a worker.  The reserved set is the
    # source of truth — overlapping requests can't select the same row.
    # Idle path and busy path both reserve, so session.send / dequeue can
    # consume with defense-in-depth (matching reserved_for_msg_id).
    from turnstone.core.attachments import Attachment
    from turnstone.core.memory import (
        get_attachments as _get_attachments,
    )
    from turnstone.core.memory import (
        get_pending_attachments_with_content as _get_pending_with_content,
    )
    from turnstone.core.memory import (
        reserve_attachments as _reserve,
    )

    # Actor resolution: service-scoped callers file attachments under the
    # workstream owner (matches upload/list/delete semantics).  Returns
    # 404 on missing/foreign workstreams.
    attach_user_id, err = _require_ws_access(request, ws_id or "")
    if err:
        return err
    # _require_ws_access already 404'd on missing ws_id; the explicit
    # check keeps the type-checker happy without a bare assert.
    if not isinstance(ws_id, str):
        return JSONResponse({"error": "ws_id required"}, status_code=400)

    # Full UUID hex — this token scopes both the attachment reservation
    # and the eventual consume, so keep the full 128 bits.
    send_id = uuid.uuid4().hex
    raw_ids = body.get("attachment_ids")
    auto_consume_rows: list[dict[str, Any]] = []
    if raw_ids is None:
        # Auto-consume: pull the current user's pending (unreserved)
        # rows in creation order IN ONE QUERY (bytes included) — we'll
        # reserve them below and skip the second fetch.  The reserve
        # call is scoped to message_id IS NULL AND reserved_for_msg_id
        # IS NULL so a concurrent reservation can't double-book.
        auto_consume_rows = _get_pending_with_content(ws_id, attach_user_id)
        requested_ids = [str(r["attachment_id"]) for r in auto_consume_rows]
    elif isinstance(raw_ids, list) and raw_ids:
        # Cap inbound id-list length so a hostile client can't blow up
        # the storage IN (...) clause with millions of bogus ids.
        from turnstone.core.attachments import MAX_PENDING_ATTACHMENTS_PER_USER_WS

        if len(raw_ids) > MAX_PENDING_ATTACHMENTS_PER_USER_WS:
            return JSONResponse(
                {
                    "error": (
                        f"Too many attachment_ids (max {MAX_PENDING_ATTACHMENTS_PER_USER_WS})"
                    ),
                    "code": "too_many",
                },
                status_code=400,
            )
        requested_ids = [str(x) for x in raw_ids if x]
    else:
        requested_ids = []

    reserved_ids: list[str] = (
        _reserve(requested_ids, send_id, ws_id, attach_user_id) if requested_ids else []
    )
    # Preserve request order; reserve returned a set that may be a
    # strict subset (lost a race, already consumed, etc.).  Silently
    # drop losers — the user can re-upload if needed and sees the
    # partial outcome via the UI's chip-clearing on success.
    reserved_set = set(reserved_ids)
    ordered_reserved: list[str] = [aid for aid in requested_ids if aid in reserved_set]

    resolved_atts: list[Attachment] = []
    if ordered_reserved:
        # Prefer the bytes we already fetched on the auto-consume path.
        # Bytes were pre-reserve-call though, so reserved_for_msg_id
        # needs refresh from the authoritative row.  Re-fetch if the
        # auto-fetch is stale or empty.
        if auto_consume_rows and all(
            str(r["attachment_id"]) in set(ordered_reserved) for r in auto_consume_rows
        ):
            rows_by_id = {str(r["attachment_id"]): r for r in auto_consume_rows}
            # reserved_for_msg_id was None at pre-fetch; patch in the token
            # so the belt-and-braces scope check below doesn't reject the
            # rows we just reserved.
            for r in rows_by_id.values():
                r["reserved_for_msg_id"] = send_id
        else:
            rows = _get_attachments(ordered_reserved)
            rows_by_id = {str(r["attachment_id"]): r for r in rows}
        for aid in ordered_reserved:
            row = rows_by_id.get(aid)
            if not row:
                continue
            r = row
            # Scope check — belt and braces on top of the reservation.
            if (
                r.get("ws_id") != ws_id
                or r.get("user_id") != attach_user_id
                or r.get("message_id") is not None
                or r.get("reserved_for_msg_id") != send_id
            ):
                continue
            content = r.get("content")
            if not isinstance(content, bytes):
                continue
            resolved_atts.append(
                Attachment(
                    attachment_id=str(r["attachment_id"]),
                    filename=str(r.get("filename") or ""),
                    mime_type=str(r.get("mime_type") or "application/octet-stream"),
                    kind=str(r.get("kind") or ""),
                    content=content,
                )
            )

    def _release_reservation_on_fail() -> None:
        """Unreserve if we bail out before dispatching."""
        if reserved_ids:
            from turnstone.core.memory import unreserve_attachments as _unreserve

            _unreserve(send_id, ws_id, attach_user_id)

    # Atomically check-and-start to prevent two concurrent workers on the
    # same session (ChatSession.send() is not thread-safe).
    # If cancel was requested, poll briefly for the worker to exit before
    # rejecting.  Snapshot the thread ref since force-cancel can set it to
    # None concurrently.  Uses async sleep to avoid blocking the event loop.
    worker = ws.worker_thread
    if worker and worker.is_alive() and ws.session and ws.session._cancel_event.is_set():
        for _ in range(30):  # up to 3s in 100ms steps
            await asyncio.sleep(0.1)
            if not worker.is_alive():
                break
    with ws._lock:
        if ws.worker_thread and ws.worker_thread.is_alive():
            # Queue the message for injection at the next tool-result seam
            # instead of rejecting outright.  Attachments were already
            # reserved above using ``send_id`` as the token — we pass
            # the same id in as ``queue_msg_id`` so the queue entry, the
            # reservation, and the eventual consume all share one token.
            if ws.session is not None:
                try:
                    cleaned, priority, msg_id = ws.session.queue_message(
                        message,
                        attachment_ids=list(ordered_reserved),
                        queue_msg_id=send_id,
                    )
                except queue.Full:
                    _release_reservation_on_fail()
                    return JSONResponse({"status": "queue_full"})
                ui._enqueue(
                    {
                        "type": "message_queued",
                        "message": cleaned,
                        "priority": priority,
                        "msg_id": msg_id,
                    }
                )
                # Report the reservation outcome so the UI can clear
                # only the chips that actually got attached, leaving
                # un-reserved ones visible for retry.
                dropped = [aid for aid in requested_ids if aid not in reserved_set]
                return JSONResponse(
                    {
                        "status": "queued",
                        "priority": priority,
                        "msg_id": msg_id,
                        "attached_ids": list(ordered_reserved),
                        "dropped_attachment_ids": dropped,
                    }
                )
            _release_reservation_on_fail()
            ui._enqueue(
                {
                    "type": "busy_error",
                    "message": "Already processing a request. Please wait.",
                }
            )
            return JSONResponse({"status": "busy"})
        session = ws.session
        if session is None:
            _release_reservation_on_fail()
            return JSONResponse({"error": "No session"}, status_code=500)

        def run() -> None:
            assert ui is not None
            me = threading.current_thread()
            try:
                session.send(
                    message,
                    attachments=resolved_atts or None,
                    send_id=send_id,
                )
            except GenerationCancelled:
                # Safety net — send() normally handles this internally.
                # If this thread was force-abandoned, ws.worker_thread will
                # have been set to None — don't emit spurious events.
                _release_reservation_on_fail()
                if ws.worker_thread is me:
                    ui.on_stream_end()
                    ui.on_state_change("idle")
            except Exception as e:
                # Release the reservation so the attachments don't stay
                # soft-locked forever when the worker crashes before
                # reaching the consume step.  Safe-by-idempotency: once
                # mark_attachments_consumed has cleared the token, a
                # follow-up unreserve is a no-op.
                _release_reservation_on_fail()
                if ws.worker_thread is me:
                    ui.on_error(f"Error: {e}")
                    ui.on_stream_end()
                    ui.on_state_change("error")

        t = threading.Thread(target=run, daemon=True)
        ws.worker_thread = t
        t.start()
    _metrics.record_message_sent()
    with ui._ws_lock:
        ui._ws_messages += 1
        ui._ws_turn_tool_calls = 0
    dropped = [aid for aid in requested_ids if aid not in reserved_set]
    return JSONResponse(
        {
            "status": "ok",
            "attached_ids": list(ordered_reserved),
            "dropped_attachment_ids": dropped,
        }
    )


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
    # Cross-tenant guard: resolving a pending tool approval on another
    # tenant's workstream is RCE-adjacent (the victim queued a command
    # expecting to decide themselves).  Gate before touching the UI.
    # Pass mgr= so the check uses the in-memory ws.user_id and survives
    # transient storage outages.
    _owner, err = _require_ws_access(request, str(ws_id or ""), mgr=mgr)
    if err:
        return err
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
    _owner, err = _require_ws_access(request, str(ws_id or ""), mgr=mgr)
    if err:
        return err
    ws, ui = _get_ws(mgr, ws_id)
    if not ws or not ui:
        return JSONResponse({"error": "Unknown workstream"}, status_code=404)
    ui.resolve_plan(feedback)
    return JSONResponse({"status": "ok"})


async def cancel_generation(request: Request) -> JSONResponse:
    """POST /v1/api/cancel — cancel the active generation in a workstream.

    Returns ``{status, dropped}`` where ``dropped`` captures a forensic
    snapshot of what was in flight at cancel time: any pending approval's
    tool names, queued-message count and a short preview, and whether a
    worker thread was actively generating.  The model-invoked
    ``cancel_workstream`` tool passes this through so a coordinator can
    tell operators what it just killed instead of a bare ``{cancelled:
    true}``.  Absent keys mean "nothing observable in that lane".
    """
    from turnstone.core.web_helpers import read_json_or_400

    body = await read_json_or_400(request)
    if isinstance(body, JSONResponse):
        return body
    ws_id = body.get("ws_id")
    mgr = request.app.state.workstreams
    _owner, err = _require_ws_access(request, str(ws_id or ""), mgr=mgr)
    if err:
        return err
    ws, ui = _get_ws(mgr, ws_id)
    if not ws or not ui:
        return JSONResponse({"error": "Unknown workstream"}, status_code=404)
    session = ws.session
    if session is None:
        return JSONResponse({"error": "No session"}, status_code=400)
    force = body.get("force", False) is True
    was_running = bool(ws.worker_thread and ws.worker_thread.is_alive())
    dropped = _capture_cancel_forensics(session, ui, was_running=was_running)
    # Only act if generation is actually in progress
    if was_running:
        # Set the cooperative cancel flag (worker thread checks at checkpoints)
        session.cancel()
        # Unblock any pending approval/plan review waits
        ui.resolve_approval(False, "Cancelled by user")
        ui.resolve_plan("reject")
        if force:
            # Force cancel: abandon the stuck worker thread (daemon, will
            # die on process exit or stream timeout) and emit stream_end
            # so the UI and session recover immediately.  The per-generation
            # cancel event stays set so the abandoned thread still kills
            # subprocesses at its next checkpoint.
            with ws._lock:
                ws.worker_thread = None
            ui._enqueue({"type": "stream_end"})
            ui.on_state_change("idle")
        else:
            # Emit cancelled SSE event so SDK consumers get a typed signal
            ui._enqueue({"type": "cancelled"})
    return JSONResponse({"status": "ok", "dropped": dropped})


def _capture_cancel_forensics(session: Any, ui: Any, *, was_running: bool) -> dict[str, Any]:
    """Snapshot in-flight session state for the cancel response.

    Pure read — never mutates ``session`` or ``ui``.  Fields are
    best-effort: any attribute miss (test double, alternate UI) falls
    through to "not observable".  Kept short so a coordinator surfacing
    the dropped dict doesn't bloat the tool-result payload.
    """
    out: dict[str, Any] = {"was_running": was_running}
    pending = getattr(ui, "_pending_approval", None)
    if isinstance(pending, dict):
        tool_names: list[str] = []
        first_call_id = ""
        for item in pending.get("items", []) or []:
            if not isinstance(item, dict):
                continue
            if not item.get("needs_approval"):
                continue
            name = item.get("approval_label") or item.get("func_name") or ""
            if name:
                tool_names.append(str(name))
            if not first_call_id:
                first_call_id = str(item.get("call_id") or "")
        if tool_names:
            out["pending_approval"] = {
                "tool_names": tool_names,
                "call_id": first_call_id,
            }
    queued = getattr(session, "_queued_messages", None)
    if queued:
        try:
            count = len(queued)
        except TypeError:
            count = 0
        preview = ""
        try:
            first = next(iter(queued.values()))
            if isinstance(first, tuple) and first:
                # Run through the credential-redactor before truncating so
                # pasted secrets / connection strings / JWTs in the queued
                # message don't land verbatim in the cancel_workstream
                # tool result (which gets persisted to the coordinator's
                # conversation history AND fanned out via SSE).  Matches
                # the close_workstream.reason persistence path (phase 5).
                from turnstone.core.output_guard import redact_credentials

                preview = redact_credentials(str(first[0]))[:120]
        except StopIteration:
            pass
        except Exception:
            preview = ""
        if count:
            out["queued_messages"] = {"count": count, "first_preview": preview}
    return out


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
    _owner, err = _require_ws_access(request, str(ws_id or ""), mgr=mgr)
    if err:
        return err

    ws, ui = _get_ws(mgr, ws_id)
    if not ws or not ui:
        return JSONResponse({"error": "Unknown workstream"}, status_code=404)
    assert ws.session is not None

    try:
        # Permission gate for conversation-modifying commands
        cmd_word = cmd.strip().split(None, 1)[0].lower()
        if cmd_word in ("/rewind", "/retry"):
            from turnstone.core.auth import require_permission

            err = require_permission(request, "conversation.modify")
            if err:
                ui.on_error("Permission denied: conversation.modify required")
                return err
            # Prevent rewind/retry while a generation is in progress
            with ws._lock:
                if ws.worker_thread and ws.worker_thread.is_alive():
                    ui._enqueue(
                        {
                            "type": "busy_error",
                            "message": "Cannot rewind/retry while processing.",
                        }
                    )
                    return JSONResponse({"status": "busy"})

        should_exit = ws.session.handle_command(cmd)
        if should_exit:
            ui.on_info("Session ended. You can close this tab.")
        # Handle UI updates for workstream-changing commands
        if cmd_word in ("/clear", "/new"):
            ui._enqueue({"type": "clear_ui"})
        elif cmd_word == "/resume":
            ui._enqueue({"type": "clear_ui"})
            history = _build_history(ws.session)
            if history:
                ui._enqueue({"type": "history", "messages": history})
        elif cmd_word in ("/rewind", "/retry"):
            # Refresh frontend with truncated history
            ui._enqueue({"type": "clear_ui"})
            history = _build_history(ws.session)
            if history:
                ui._enqueue({"type": "history", "messages": history})
            # Audit trail
            storage = getattr(request.app.state, "auth_storage", None)
            if storage:
                from turnstone.core.audit import record_audit

                audit_uid, ip = _audit_context(request)
                record_audit(
                    storage,
                    audit_uid,
                    f"conversation.{cmd_word[1:]}",
                    "workstream",
                    ws.id,
                    {"command": cmd, "ws_id": ws.id},
                    ip,
                )
            # Dispatch deferred retry in background thread
            retry_msg = ws.session._pending_retry
            if retry_msg:
                ws.session._pending_retry = None
                session = ws.session

                def run_retry() -> None:
                    me = threading.current_thread()
                    try:
                        session.send(retry_msg)
                    except GenerationCancelled:
                        if ws.worker_thread is me:
                            ui.on_stream_end()
                            ui.on_state_change("idle")
                    except Exception as exc:
                        if ws.worker_thread is me:
                            ui.on_error(f"Error: {exc}")
                            ui.on_stream_end()
                            ui.on_state_change("error")

                with ws._lock:
                    if ws.worker_thread and ws.worker_thread.is_alive():
                        ui.on_error("Cannot retry: workstream is busy")
                    else:
                        t = threading.Thread(target=run_retry, daemon=True)
                        ws.worker_thread = t
                        t.start()
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


# ---------------------------------------------------------------------------
# Notification helpers — completion delivery for scheduled workstreams
# ---------------------------------------------------------------------------

_MAX_NOTIFY_TARGETS = 10


def _validate_notify_targets(raw: Any) -> tuple[str, str]:
    """Validate and normalize notify_targets input.

    Returns (json_string, error_message). Error is empty on success.
    """
    if not raw:
        return "[]", ""
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return "[]", "notify_targets must be valid JSON"
    elif isinstance(raw, list):
        parsed = raw
    else:
        return "[]", "notify_targets must be a JSON array or string"

    if not isinstance(parsed, list):
        return "[]", "notify_targets must be a JSON array"

    if len(parsed) > _MAX_NOTIFY_TARGETS:
        return "[]", f"notify_targets limited to {_MAX_NOTIFY_TARGETS} entries"

    normalized: list[dict[str, str]] = []
    for i, t in enumerate(parsed):
        if not isinstance(t, dict):
            return "[]", f"notify_targets[{i}] must be an object"
        if "channel_type" not in t:
            return "[]", f"notify_targets[{i}] missing channel_type"

        has_channel_id = "channel_id" in t and t.get("channel_id") is not None
        has_user_id = "user_id" in t and t.get("user_id") is not None
        if has_channel_id and has_user_id:
            return "[]", f"notify_targets[{i}] must specify only one of channel_id or user_id"
        if not has_channel_id and not has_user_id:
            return "[]", f"notify_targets[{i}] requires channel_id or user_id"

        normalized_target: dict[str, str] = {}
        for key in ("channel_type", "channel_id", "user_id"):
            val = t.get(key)
            if val is None:
                continue
            if not isinstance(val, str):
                return "[]", f"notify_targets[{i}].{key} must be a non-empty string <= 256 chars"
            stripped = val.strip()
            if not stripped:
                return "[]", f"notify_targets[{i}].{key} must be a non-empty string <= 256 chars"
            if len(stripped) > 256:
                return "[]", f"notify_targets[{i}].{key} must be a non-empty string <= 256 chars"
            normalized_target[key] = stripped

        normalized.append(normalized_target)

    return json.dumps(normalized), ""


def _extract_last_assistant_content(session: Any) -> str:
    """Return the text content of the last assistant message."""
    for msg in reversed(session.messages):
        if msg.get("role") == "assistant":
            content = msg.get("content", "")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                parts = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text = block.get("text")
                        if isinstance(text, str) and text:
                            parts.append(text)
                return "\n".join(parts)
    return ""


def _fire_notify_targets(ws: Any, content: str) -> None:
    """Send completion notifications to all configured targets."""
    if not ws.notify_targets:
        return
    if not content:
        content = "(Task completed — no output captured)"

    try:
        targets = json.loads(ws.notify_targets)
    except (json.JSONDecodeError, TypeError):
        return
    if not targets or not isinstance(targets, list):
        return

    from turnstone.core.session import _notify_auth_headers
    from turnstone.core.storage import get_storage

    storage = get_storage()
    auth_headers = _notify_auth_headers()
    task_name = ws.name or ws.id[:8]

    for target in targets:
        if not isinstance(target, dict):
            continue
        channel_type = target.get("channel_type", "")
        resolved: dict[str, str] = {}
        if "channel_id" in target:
            resolved = {"channel_type": channel_type, "channel_id": target["channel_id"]}
        elif "user_id" in target:
            resolved = {"channel_type": channel_type, "channel_id": target["user_id"]}
        else:
            continue

        payload = {
            "target": resolved,
            "message": content,
            "title": f"Schedule: {task_name}",
            "ws_id": ws.id,
        }

        _deliver_notification(storage, payload, auth_headers)


def _deliver_notification(
    storage: Any,
    payload: dict[str, Any],
    auth_headers: dict[str, str],
) -> None:
    """POST to channel gateway /v1/api/notify with retry."""
    import httpx

    for attempt in range(3):
        services = storage.list_services("channel", max_age_seconds=120)
        if not services:
            if attempt < 2:
                time.sleep(1.0 if attempt == 0 else 3.0)
                continue
            log.warning("notify_completion.no_services")
            return

        for svc in services:
            url = svc["url"].rstrip("/") + "/v1/api/notify"
            if not url.startswith(("http://", "https://")):
                continue
            try:
                resp = httpx.post(url, json=payload, timeout=10, headers=auth_headers)
                if resp.status_code < 300:
                    # Verify at least one target was delivered (mirrors _exec_notify)
                    try:
                        data = resp.json()
                        results = data.get("results") if isinstance(data, dict) else None
                        if isinstance(results, list) and any(
                            isinstance(r, dict) and r.get("status") == "sent" for r in results
                        ):
                            log.info("notify_completion.delivered", ws_id=payload.get("ws_id"))
                            return
                    except Exception:
                        log.debug("notify_completion.response_parse_error", url=url, exc_info=True)
                    log.warning("notify_completion.no_successful_delivery", url=url)
                    continue
                log.warning(
                    "notify_completion.failed",
                    status=resp.status_code,
                    url=url,
                )
            except Exception:
                log.exception("notify_completion.error", url=url)
                continue

        if attempt < 2:
            time.sleep(1.0 if attempt == 0 else 3.0)


def _reserve_and_resolve_attachments(
    requested_ids: list[str],
    send_id: str,
    ws_id: str,
    user_id: str,
) -> tuple[list[Any], list[str], list[str]]:
    """Reserve attachment ids for ``send_id`` and resolve to Attachment objects.

    Returns ``(resolved, ordered_reserved, dropped)``. ``dropped`` is the
    subset of *requested_ids* that could not be reserved (already consumed,
    lost a race, or cross-scope).  Used by the create-with-attachments
    path; ``send_message`` has its own inlined variant with an
    auto-consume fast path that reuses bytes fetched during selection.
    """
    from turnstone.core.attachments import Attachment
    from turnstone.core.memory import get_attachments as _get_attachments
    from turnstone.core.memory import reserve_attachments as _reserve

    if not requested_ids:
        return [], [], []

    reserved_ids: list[str] = _reserve(requested_ids, send_id, ws_id, user_id)
    reserved_set = set(reserved_ids)
    ordered_reserved: list[str] = [aid for aid in requested_ids if aid in reserved_set]
    dropped: list[str] = [aid for aid in requested_ids if aid not in reserved_set]

    resolved: list[Any] = []
    if ordered_reserved:
        rows = _get_attachments(ordered_reserved)
        rows_by_id = {str(r["attachment_id"]): r for r in rows}
        for aid in ordered_reserved:
            r = rows_by_id.get(aid)
            if not r:
                continue
            if (
                r.get("ws_id") != ws_id
                or r.get("user_id") != user_id
                or r.get("message_id") is not None
                or r.get("reserved_for_msg_id") != send_id
            ):
                continue
            content = r.get("content")
            if not isinstance(content, bytes):
                continue
            resolved.append(
                Attachment(
                    attachment_id=str(r["attachment_id"]),
                    filename=str(r.get("filename") or ""),
                    mime_type=str(r.get("mime_type") or "application/octet-stream"),
                    kind=str(r.get("kind") or ""),
                    content=content,
                )
            )
    return resolved, ordered_reserved, dropped


def _validate_and_save_uploaded_files(
    files: list[tuple[str, str, bytes]],
    ws_id: str,
    user_id: str,
) -> tuple[list[str], JSONResponse | None]:
    """Classify + save a list of ``(filename, claimed_mime, data)`` tuples.

    Applies the same validation rules as ``upload_attachment`` (magic-byte
    image sniffing, UTF-8 text decode, per-kind size cap, per-(ws,user)
    pending cap) under the shared ``_attachment_upload_lock``.

    Returns ``(attachment_ids, None)`` on success or ``(ids_saved_so_far,
    JSONResponse)`` on the first failure so the caller can roll back any
    partial state.
    """
    from turnstone.core.attachments import (
        IMAGE_SIZE_CAP,
        MAX_PENDING_ATTACHMENTS_PER_USER_WS,
        TEXT_DOC_SIZE_CAP,
    )
    from turnstone.core.memory import list_pending_attachments, save_attachment

    saved_ids: list[str] = []
    if not files:
        return saved_ids, None

    lock = _attachment_upload_lock(ws_id, user_id)
    with lock:
        pending_count = len(list_pending_attachments(ws_id, user_id))
        for filename, claimed_mime, data in files:
            if not data:
                return saved_ids, JSONResponse({"error": "Empty file"}, status_code=400)
            sniffed_image = _sniff_image_mime(data)
            if sniffed_image is not None:
                if len(data) > IMAGE_SIZE_CAP:
                    return saved_ids, JSONResponse(
                        {
                            "error": (
                                f"Image too large ({len(data):,} bytes); "
                                f"cap is {IMAGE_SIZE_CAP:,} bytes."
                            ),
                            "code": "too_large",
                        },
                        status_code=413,
                    )
                kind = "image"
                mime = sniffed_image
            else:
                if len(data) > TEXT_DOC_SIZE_CAP:
                    return saved_ids, JSONResponse(
                        {
                            "error": (
                                f"Text document too large ({len(data):,} bytes); "
                                f"cap is {TEXT_DOC_SIZE_CAP:,} bytes."
                            ),
                            "code": "too_large",
                        },
                        status_code=413,
                    )
                mime_or_err = _classify_text_attachment(filename, claimed_mime, data)
                if mime_or_err[0] is None:
                    return saved_ids, JSONResponse(
                        {"error": mime_or_err[1], "code": "unsupported"},
                        status_code=400,
                    )
                kind = "text"
                mime = mime_or_err[0]

            if pending_count + 1 > MAX_PENDING_ATTACHMENTS_PER_USER_WS:
                return saved_ids, JSONResponse(
                    {
                        "error": (
                            f"Too many pending attachments "
                            f"(max {MAX_PENDING_ATTACHMENTS_PER_USER_WS} pending per workstream)"
                        ),
                        "code": "too_many",
                    },
                    status_code=409,
                )
            attachment_id = uuid.uuid4().hex
            save_attachment(
                attachment_id,
                ws_id,
                user_id,
                filename,
                mime,
                len(data),
                kind,
                data,
            )
            saved_ids.append(attachment_id)
            pending_count += 1
    return saved_ids, None


async def create_workstream(request: Request) -> JSONResponse:
    """POST /v1/api/workstreams/new — create a new workstream.

    Accepts two content types:

    - ``application/json`` (default): body is a :class:`CreateWorkstreamRequest`.
    - ``multipart/form-data``: one ``meta`` field (JSON object, same shape
      as the JSON body) plus zero-or-more ``file`` parts. Files are saved
      as attachments under the new workstream and reserved onto the first
      ``initial_message`` turn (if provided) before the worker dispatches.
    """
    from turnstone.core.attachments import IMAGE_SIZE_CAP
    from turnstone.core.audit import record_audit
    from turnstone.core.memory import get_workstream_display_name
    from turnstone.core.web_helpers import (
        read_json_or_400,
        read_multipart_create_or_400,
    )

    content_type = (request.headers.get("content-type") or "").lower()
    uploaded_files: list[tuple[str, str, bytes]] = []
    body: dict[str, Any]
    if content_type.startswith("multipart/form-data"):
        # Multipart cap: up to MAX_PENDING × image cap, plus slack for
        # JSON meta + multipart framing.  Per-file size is enforced in
        # _validate_and_save_uploaded_files against the kind-specific cap.
        parsed = await read_multipart_create_or_400(
            request,
            max_files=10,
            max_per_file_bytes=IMAGE_SIZE_CAP,
            max_total_bytes=10 * IMAGE_SIZE_CAP,
        )
        if isinstance(parsed, JSONResponse):
            return parsed
        body, uploaded_files = parsed
    else:
        json_body = await read_json_or_400(request)
        if isinstance(json_body, JSONResponse):
            return json_body
        body = json_body
    mgr: SessionManager = request.app.state.workstreams
    skip: bool = request.app.state.skip_permissions
    auth = getattr(getattr(request, "state", None), "auth_result", None)
    uid: str = getattr(auth, "user_id", "") or ""
    # Trusted services (console) may forward the real user_id in the request
    # body when creating workstreams on behalf of a user.  Only service
    # identities are trusted — end-user tokens (including console-proxy tokens
    # that carry the real user's identity) must not override user_id.
    trusted_sources = {"console"}
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
    requested_ws_id = body.get("ws_id", "") or ""
    if not isinstance(requested_ws_id, str):
        requested_ws_id = ""
    if requested_ws_id and not _VALID_WS_ID.match(requested_ws_id):
        return JSONResponse({"error": "invalid ws_id format"}, status_code=400)
    # Disallow attachments + resume_ws in the same request — semantics
    # are unclear (resume forks an existing ws, but attachments are for
    # the *fresh* turn).  Caller should resume first, then upload via
    # the standard endpoint.  Checked before mgr.create() so we don't
    # waste work on a request we'll reject.
    if uploaded_files and resume_ws_id:
        return JSONResponse(
            {"error": "attachments cannot be combined with resume_ws"},
            status_code=400,
        )
    # Kind + parent relationship: coordinator-spawned children forward
    # these from the console's CoordinatorClient.  Default kind is
    # ``INTERACTIVE``; coordinators themselves are created by the console's
    # own SessionManager (coordinator kind) and never land in this handler.
    # Only
    # ``INTERACTIVE`` is accepted here — requests that try to create a
    # coordinator via the generic workstream endpoint are rejected, and
    # unknown kinds (typos, future-only values) are 400 rather than being
    # silently coerced.  User-facing edge: the storage_edge and manager
    # layers below re-validate, see comments at those sites for why.
    try:
        body_kind = WorkstreamKind.from_raw(body.get("kind"))
    except ValueError:
        return JSONResponse(
            {"error": f"unknown workstream kind {body.get('kind')!r}"},
            status_code=400,
        )
    if body_kind != WorkstreamKind.INTERACTIVE:
        return JSONResponse(
            {
                "error": (
                    "coordinator workstreams must be created on the console via "
                    "POST /v1/api/workstreams/new (with admin.coordinator scope)"
                )
            },
            status_code=400,
        )
    body_parent = body.get("parent_ws_id") or None
    if body_parent is not None:
        # Ownership gate: parent_ws_id is client-supplied in the request
        # body, so a malicious caller could previously point a new
        # interactive workstream at another tenant's coordinator — the
        # coordinator's SSE fan-out would then route child_ws_* events
        # (carrying the attacker's name/state/tokens) to that victim.
        # Validate against storage: the parent must exist, be a
        # coordinator, and belong to the same user.  Coordinator-spawned
        # children satisfy this by construction (the coordinator's JWT
        # `sub` claim is the owning user and parent_ws_id is the coord's
        # own ws_id); external clients that fabricate the field get 403.
        from turnstone.core.storage._registry import get_storage as _get_storage_for_parent

        _pstorage = _get_storage_for_parent()
        parent_row = _pstorage.get_workstream(body_parent) if _pstorage else None
        if parent_row is None:
            return JSONResponse(
                {"error": "parent_ws_id does not reference a known workstream"},
                status_code=400,
            )
        if (
            parent_row.get("kind") != WorkstreamKind.COORDINATOR
            or (parent_row.get("user_id") or "") != uid
        ):
            return JSONResponse(
                {"error": "parent_ws_id must reference a coordinator you own"},
                status_code=403,
            )
    try:
        ws = mgr.create(
            user_id=uid,
            name=body.get("name", ""),
            model=resolved_model,
            skill=resolved_skill,
            skill_id=skill_data["template_id"] if skill_data else "",
            skill_version=applied_skill_version,
            ws_id=requested_ws_id,
            client_type=body.get("client_type", "") or "",
            judge_model=body.get("judge_model", "") or None,
            parent_ws_id=body_parent,
        )
        if not isinstance(ws.ui, WebUI):
            raise TypeError(f"Expected WebUI, got {type(ws.ui).__name__}")
        if skip or body.get("auto_approve", False):
            ws.ui.auto_approve = True
        # Register watch runner for this workstream
        runner = getattr(request.app.state, "watch_runner", None)
        if runner and ws.session:
            ws.session.set_watch_runner(
                runner, dispatch_fn=_make_watch_dispatch(ws, ws.session, ws.ui)
            )
        gq: queue.Queue[dict[str, Any]] = request.app.state.global_queue

        # Save attachments BEFORE the ws_created broadcast so failed
        # validation doesn't make SSE consumers flash a workstream that
        # never really existed.  Validate + save happens early; rollback
        # is silent (no ws_created → no ws_closed needed).
        attachment_ids: list[str] = []
        if uploaded_files:
            saved_ids, save_err = _validate_and_save_uploaded_files(uploaded_files, ws.id, uid)
            if save_err is not None:
                from turnstone.core.memory import delete_workstream as _delete_ws

                with contextlib.suppress(Exception):
                    mgr.close(ws.id)
                with contextlib.suppress(Exception):
                    _delete_ws(ws.id)
                return save_err
            attachment_ids = saved_ids

        # Emit creation event on global queue for SSE consumers (console).
        # Deferred until past attachment validation so a rejected create
        # doesn't surface a phantom create→close pair.
        display_name = get_workstream_display_name(ws.id) or ws.name
        with contextlib.suppress(queue.Full):
            gq.put_nowait(
                {
                    "type": "ws_created",
                    "ws_id": ws.id,
                    "name": display_name,
                    "model": ws.session.model if ws.session else "",
                    "model_alias": ws.session.model_alias if ws.session else "",
                    "kind": ws.kind,
                    "parent_ws_id": ws.parent_ws_id,
                    # Owner id is propagated through the cluster event
                    # stream so console-side fan-out can enforce tenant
                    # isolation — a coordinator must never receive
                    # child_ws_* events for workstreams it doesn't own.
                    "user_id": ws.user_id,
                }
            )
        # Tamper-evident audit trail.  Lives alongside the broadcast
        # event (not replacing it — the broadcast is ephemeral UI
        # signalling, the audit row survives for forensic review).
        _audit_storage = getattr(request.app.state, "auth_storage", None)
        if _audit_storage is not None:
            _, _audit_ip = _audit_context(request)
            record_audit(
                _audit_storage,
                uid,
                "workstream.created",
                "workstream",
                ws.id,
                {"kind": str(ws.kind), "parent_ws_id": ws.parent_ws_id},
                _audit_ip,
            )
        # Atomic workstream resume during creation.
        resumed = False
        message_count = 0
        if resume_ws_id and ws.session is not None:
            from turnstone.core.memory import resolve_workstream

            target_id = resolve_workstream(resume_ws_id)
            if target_id and ws.session.resume(target_id, fork=True):
                resumed = True
                message_count = len(ws.session.messages)
                # If the user provided a custom name, set it as the fork's alias
                # so it takes priority in display.  Otherwise keep the
                # auto-generated name so auto-title can run fresh.
                user_name = body.get("name", "").strip()
                if user_name:
                    from turnstone.core.memory import set_workstream_alias

                    set_workstream_alias(ws.id, user_name)
                    ws.name = user_name
                ui = ws.ui
                if isinstance(ui, WebUI):
                    ui._enqueue({"type": "clear_ui"})
                    history = _build_history(ws.session)
                    if history:
                        ui._enqueue({"type": "history", "messages": history})
                # Broadcast a rename so the tab picks up the correct fork name
                # (the ws_created event fired before fork with the pre-fork name).
                with contextlib.suppress(queue.Full):
                    gq.put_nowait({"type": "ws_rename", "ws_id": ws.id, "name": ws.name})

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

        # Resolve notify_targets: schedule targets override skill targets
        notify_targets_raw = body.get("notify_targets", "[]")
        if isinstance(notify_targets_raw, list):
            notify_targets_raw = json.dumps(notify_targets_raw)
        nt_str, nt_err = _validate_notify_targets(notify_targets_raw)
        if nt_err:
            return JSONResponse({"error": nt_err}, status_code=400)
        # Skill fallback (only if schedule didn't specify targets)
        if nt_str == "[]" and skill_data:
            skill_notify = skill_data.get("notify_on_complete", "[]")
            if skill_notify and skill_notify != "{}" and skill_notify != "[]":
                fallback_str, fallback_err = _validate_notify_targets(skill_notify)
                if not fallback_err:
                    nt_str = fallback_str
        ws.notify_targets = nt_str

        # Pin locally-created workstreams so the console routes to this node.
        # Console-routed creates pass ws_id in the request body — those are
        # already bucket-aligned and don't need an override. Direct creates
        # (web UI, watch, TurnstoneInit) generate their own ws_id, which may
        # hash to a bucket assigned to a different node.
        if not requested_ws_id:
            node_id = getattr(request.app.state, "node_id", "")
            if node_id:
                try:
                    from turnstone.core.storage import get_storage as _gs

                    _gs().set_workstream_override(ws.id, node_id, reason="local")
                except Exception:
                    log.debug("Failed to set routing override for %s", ws.id, exc_info=True)

        # If an initial_message was provided, send it as the first user message.
        # This replaces the old bridge behavior where CreateWorkstreamMessage
        # carried initial_message and the bridge sent it as a follow-up.
        initial_message = body.get("initial_message", "").strip()
        if initial_message and ws.session is not None:
            session = ws.session
            # Reserve any attachments uploaded in this request before the
            # worker dispatches.  Mirrors the /send endpoint pattern: the
            # send_id token scopes both the reservation and the eventual
            # consume.  Unreserve on worker failure so the rows don't stay
            # soft-locked forever.
            send_id = uuid.uuid4().hex
            resolved_atts: list[Any] = []
            if attachment_ids:
                resolved_atts, _ord, _drop = _reserve_and_resolve_attachments(
                    attachment_ids, send_id, ws.id, uid
                )

            def _run_initial() -> None:
                try:
                    session.send(
                        initial_message,
                        attachments=resolved_atts or None,
                        send_id=send_id if resolved_atts else None,
                    )
                except (Exception, GenerationCancelled):
                    if attachment_ids:
                        from turnstone.core.memory import (
                            unreserve_attachments as _unreserve,
                        )

                        with contextlib.suppress(Exception):
                            _unreserve(send_id, ws.id, uid)
                    if isinstance(ws.ui, WebUI):
                        ws.ui.on_stream_end()
                        ws.ui.on_state_change("idle")
                finally:
                    try:
                        last_content = _extract_last_assistant_content(session)
                        _fire_notify_targets(ws, last_content)
                    except Exception:
                        log.warning("notify_completion.hook_error", ws_id=ws.id, exc_info=True)

            t = threading.Thread(target=_run_initial, daemon=True, name=f"ws-init-{ws.id[:8]}")
            ws.worker_thread = t
            t.start()

        return JSONResponse(
            {
                "ws_id": ws.id,
                "name": ws.name,
                "resumed": resumed,
                "message_count": message_count,
                "attachment_ids": attachment_ids,
            }
        )
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


async def close_workstream(request: Request) -> JSONResponse:
    """POST /v1/api/workstreams/close — close a workstream.

    Optional ``reason`` from the request body is persisted on the
    workstream's config row so post-mortem tooling (and the coordinator's
    ``inspect_workstream``) can surface why the workstream was retired
    without scraping the audit log.
    """
    from turnstone.core.audit import record_audit
    from turnstone.core.output_guard import redact_credentials
    from turnstone.core.web_helpers import read_json_or_400

    body = await read_json_or_400(request)
    if isinstance(body, JSONResponse):
        return body
    ws_id = str(body.get("ws_id", ""))
    raw_reason = body.get("reason", "")
    # Cap reason length so a model that accidentally / maliciously dumps
    # a multi-KB blob (or a captured secret) can't grow workstream_config
    # rows without bound.  512 BYTES is enough for any human-readable
    # close reason; anything longer is suspicious.  Slice on UTF-8 bytes
    # (not code points) so a CJK / emoji-heavy payload can't sneak past
    # the cap at 3-4x the documented budget.  ``errors="ignore"`` drops
    # any partial code point left at the truncation boundary.  Then run
    # the same credential-redaction the output guard applies to tool
    # output — a model under prompt injection that dumps a captured
    # secret in ``reason`` doesn't get to plant it in plaintext audit
    # logs / workstream_config.
    if isinstance(raw_reason, str):
        capped = raw_reason.strip().encode("utf-8")[:512].decode("utf-8", errors="ignore")
        reason = redact_credentials(capped)
    else:
        reason = ""
    mgr = request.app.state.workstreams
    # Cross-tenant close would abort another tenant's running generation.
    # _require_ws_access returns 404 on non-owner — same shape as the
    # "last workstream" (400) / "not found" (404) branches below.
    _owner_uid, err = _require_ws_access(request, ws_id, mgr=mgr)
    if err:
        return err
    ws_before = mgr.get(ws_id)
    if not ws_before:
        return JSONResponse({"error": "Workstream not found"}, status_code=404)
    if mgr.close(ws_id):
        storage = getattr(request.app.state, "auth_storage", None)
        # Persist before the ws_closed event reaches consumers so any
        # downstream reader sees the close_reason in the same
        # observation window as the state change. The adapter-level
        # emit_closed fires inside mgr.close() which already returned
        # — strictly speaking the event beat us here, but the
        # close_reason is advisory UI metadata, not a sync barrier.
        if reason and storage is not None:
            try:
                storage.save_workstream_config(ws_id, {"close_reason": reason})
            except Exception:
                log.debug(
                    "ws.close.reason_persist_failed ws=%s",
                    ws_id[:8] if ws_id else "",
                    exc_info=True,
                )
        if storage is not None:
            _, ip = _audit_context(request)
            audit_detail: dict[str, Any] = {
                "kind": str(ws_before.kind),
                "parent_ws_id": ws_before.parent_ws_id,
            }
            if reason:
                audit_detail["reason"] = reason
            record_audit(
                storage,
                _auth_user_id(request),
                "workstream.closed",
                "workstream",
                ws_id,
                audit_detail,
                ip,
            )
        return JSONResponse({"status": "ok"})
    # close() returned False — the ws was popped between the mgr.get()
    # check above and our mgr.close() call (another close racing in).
    # Return 404 so the API contract matches a "not found" caller
    # experience; the old "Cannot close last workstream" 400 went away
    # with the default-startup workstream and there's no scenario where
    # this branch means anything other than "the ws isn't tracked here".
    return JSONResponse({"error": "Workstream not found"}, status_code=404)


async def delete_workstream_endpoint(request: Request) -> JSONResponse:
    """POST /v1/api/workstreams/{ws_id}/delete — permanently delete a saved workstream."""
    from turnstone.core.audit import record_audit
    from turnstone.core.log import get_logger
    from turnstone.core.memory import delete_workstream

    log = get_logger(__name__)
    ws_id = request.path_params.get("ws_id", "")
    if not ws_id:
        log.warning("ws.delete.failed", reason="empty_ws_id")
        return JSONResponse({"error": "ws_id is required"}, status_code=400)
    # Cross-tenant delete would destroy another tenant's workstream,
    # conversations, and attachments in one call.  _require_ws_access
    # returns 404 on mismatch so existence isn't enumerable.
    owner_uid, err = _require_ws_access(request, ws_id)
    if err:
        return err
    storage = getattr(request.app.state, "auth_storage", None)
    kind: str = ""
    parent_ws_id: str | None = None
    _, ip = _audit_context(request)
    try:
        # Snapshot kind/parent for the audit record before the delete
        # wipes the row.  Inside the try so a transient storage error
        # surfaces through the endpoint's redacted 500 handler below
        # rather than as an unhandled exception.
        if storage is not None:
            row = storage.get_workstream(ws_id) or {}
            kind = row.get("kind", "")
            parent_ws_id = row.get("parent_ws_id")
        if delete_workstream(ws_id):
            log.info("ws.deleted", ws_id=ws_id[:8])
            if storage is not None:
                record_audit(
                    storage,
                    _auth_user_id(request),
                    "workstream.deleted",
                    "workstream",
                    ws_id,
                    {"kind": str(kind), "parent_ws_id": parent_ws_id},
                    ip,
                )
            return JSONResponse({"deleted": ws_id})
        log.warning("ws.delete.failed", reason="not_found", ws_id=ws_id[:8])
        return JSONResponse({"error": "Workstream not found"}, status_code=404)
    except Exception as e:
        log.exception("ws.delete.error", ws_id=ws_id[:8], error=str(e))
        return JSONResponse({"error": "Delete failed"}, status_code=500)


async def refresh_workstream_title(request: Request, ws_id: str = "") -> JSONResponse:
    """POST /v1/api/workstreams/{ws_id}/refresh-title — regenerate workstream title via LLM."""
    from turnstone.core.log import get_logger
    from turnstone.core.memory import get_workstream_display_name

    log = get_logger(__name__)
    ws_id = request.path_params.get("ws_id", "")
    log.info("ws.title.refresh_requested", ws_id=ws_id[:8] if ws_id else "empty")
    mgr = request.app.state.workstreams
    _owner, err = _require_ws_access(request, ws_id, mgr=mgr)
    if err:
        return err
    ws = mgr.get(ws_id)
    if not ws or not ws.session:
        log.warning(
            "ws.title.refresh_failed",
            ws_id=ws_id[:8] if ws_id else "empty",
            reason="workstream_not_found",
        )
        return JSONResponse({"error": "Workstream not found or not active"}, status_code=404)
    # Fetch current title so the LLM can generate something different
    current_title = get_workstream_display_name(ws_id) or ""
    log.info("ws.title.refresh_triggered", ws_id=ws_id[:8], current_title=current_title[:50])
    ws.session.request_title_refresh(current_title)
    return JSONResponse({"status": "ok"})


async def set_workstream_title(request: Request, ws_id: str = "") -> JSONResponse:
    """POST /v1/api/workstreams/{ws_id}/title — set workstream title manually.

    Stores the user-chosen title as the workstream *alias* so it takes
    priority over the LLM auto-generated title in the display name
    fallback chain (alias -> title -> name).
    """
    from turnstone.core.log import get_logger
    from turnstone.core.memory import set_workstream_alias
    from turnstone.core.web_helpers import read_json_or_400

    log = get_logger(__name__)
    ws_id = request.path_params.get("ws_id", "")
    log.info("ws.title.set_requested", ws_id=ws_id[:8] if ws_id else "empty")
    if not ws_id:
        return JSONResponse({"error": "ws_id is required"}, status_code=400)
    mgr = request.app.state.workstreams
    _owner, err = _require_ws_access(request, ws_id, mgr=mgr)
    if err:
        return err
    body = await read_json_or_400(request)
    if isinstance(body, JSONResponse):
        return body
    title = str(body.get("title", "")).strip()
    if not title:
        return JSONResponse({"error": "title is required"}, status_code=400)
    title = title[:80]
    if not set_workstream_alias(ws_id, title):
        log.warning("ws.title.set_alias_conflict", ws_id=ws_id[:8], title=title[:50])
        return JSONResponse(
            {"error": "That name is already used by another workstream"},
            status_code=409,
        )
    log.info("ws.title.set_alias_updated", ws_id=ws_id[:8])
    ws = mgr.get(ws_id)
    if ws and ws.session and ws.session.ui:
        ws.session.ui.on_rename(title)
    log.info("ws.title.set_success", ws_id=ws_id[:8], title=title)
    return JSONResponse({"status": "ok", "title": title})


# ---------------------------------------------------------------------------
# Workstream attachments
# ---------------------------------------------------------------------------


# Per-(ws_id, user_id) lock serializing the count-check → insert on
# upload.  Guards the pending-cap against a concurrent-upload TOCTOU race
# within a single process.  Multi-process deployments would need an
# additional DB-side check, but turnstone-server runs one process per node.
#
# Uses ``threading.Lock`` (not ``asyncio.Lock``) on purpose: Starlette's
# TestClient — and any framework that runs each request on a fresh
# anyio task — can leave a cached ``asyncio.Lock`` bound to a stale,
# closed event loop, and the next acquire deadlocks silently.  A
# threading.Lock is loop-agnostic, and the critical section here is
# short (one COUNT, one INSERT) so blocking the event loop briefly is
# acceptable.
#
# Bounded LRU eviction prevents unbounded growth on long-running nodes:
# when the map exceeds the soft cap we drop the oldest *unlocked* entries
# (a held lock means an upload is in flight — never evict those).
_ATTACHMENT_UPLOAD_LOCKS_MAX = 1024
_attachment_upload_locks: collections.OrderedDict[tuple[str, str], threading.Lock] = (
    collections.OrderedDict()
)
_attachment_upload_locks_mx = threading.Lock()


def _attachment_upload_lock(ws_id: str, user_id: str) -> threading.Lock:
    key = (ws_id, user_id)
    with _attachment_upload_locks_mx:
        lock = _attachment_upload_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _attachment_upload_locks[key] = lock
        else:
            # Touch for LRU
            _attachment_upload_locks.move_to_end(key)
        # Opportunistic eviction once we exceed the soft cap.  Skip
        # held locks (an upload is in flight under that key).
        if len(_attachment_upload_locks) > _ATTACHMENT_UPLOAD_LOCKS_MAX:
            for stale_key in list(_attachment_upload_locks):
                if len(_attachment_upload_locks) <= _ATTACHMENT_UPLOAD_LOCKS_MAX:
                    break
                if stale_key == key:
                    continue  # never evict the lock we're handing out
                stale = _attachment_upload_locks[stale_key]
                # threading.Lock has no public locked() — use the
                # non-blocking acquire-and-release probe instead.
                if stale.acquire(blocking=False):
                    stale.release()
                    del _attachment_upload_locks[stale_key]
        return lock


_TEXT_ATTACHMENT_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".c",
        ".conf",
        ".cpp",
        ".css",
        ".go",
        ".h",
        ".hpp",
        ".html",
        ".ini",
        ".java",
        ".js",
        ".json",
        ".jsx",
        ".md",
        ".py",
        ".rs",
        ".sh",
        ".sql",
        ".toml",
        ".ts",
        ".tsx",
        ".txt",
        ".xml",
        ".yaml",
        ".yml",
    }
)


def _sniff_image_mime(data: bytes) -> str | None:
    """Return a canonical image MIME type by inspecting magic bytes.

    Returns ``None`` if the bytes don't match any supported image
    format.  Do not trust the client-provided ``Content-Type`` alone.
    """
    if len(data) < 12:
        return None
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _classify_text_attachment(
    filename: str, claimed_mime: str, data: bytes
) -> tuple[str | None, str | None]:
    """Return ``(canonical_mime, error)`` for a candidate text upload.

    Accepts MIMEs starting with ``text/`` or in an application allowlist,
    OR a filename with a known text-file extension.  The payload must
    decode as UTF-8.  Returns ``(None, error_message)`` on rejection.
    """
    import os

    allowed_app_mimes = {
        "application/json",
        "application/xml",
        "application/x-yaml",
        "application/yaml",
        "application/toml",
    }
    mime_ok = claimed_mime.startswith("text/") or claimed_mime in allowed_app_mimes
    ext_ok = os.path.splitext(filename)[1].lower() in _TEXT_ATTACHMENT_EXTENSIONS
    if not (mime_ok or ext_ok):
        return None, (
            f"Unsupported file type: {claimed_mime or 'unknown'} (filename: {filename!r})"
        )
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return None, "Text attachment is not valid UTF-8"
    # Normalize MIME — prefer the claimed one if sensible, else text/plain.
    if mime_ok and claimed_mime:
        return claimed_mime, None
    return "text/plain", None


def _auth_user_id(request: Request) -> str:
    """Return the authenticated user's id (empty string when absent)."""
    auth = getattr(getattr(request, "state", None), "auth_result", None)
    return str(getattr(auth, "user_id", "") or "")


def _auth_scopes(request: Request) -> set[str]:
    auth = getattr(getattr(request, "state", None), "auth_result", None)
    return set(getattr(auth, "scopes", []) or [])


def _effective_user_filter(request: Request) -> str | None | _DenyFilter:
    """Resolve the effective ``user_id`` filter for a tenant-scoped aggregate.

    Server-side analog of the console helper of the same name.  Node
    servers authenticate with JWTs that carry a ``sub`` (the workstream
    owner) plus scopes; there is no "admin" role on the node side.
    The service scope is the sole cluster-wide bypass (used by the
    console routing proxy).

    Returns:

    - ``None`` — service-scoped caller; no tenant filter.
    - ``str`` — end-user caller with a resolved uid; storage helpers
      MUST receive ``user_id=<uid>`` and push the filter into SQL.
    - :data:`DENY_EMPTY_SUB` — end-user caller whose ``sub`` claim is
      blank.  Callers MUST short-circuit with their endpoint's
      empty-shape response.

    Mirrors the class-level tenancy contract on
    :class:`~turnstone.core.storage._protocol.StorageBackend`.
    """
    if "service" in _auth_scopes(request):
        return None
    uid = _auth_user_id(request)
    if not uid:
        return DENY_EMPTY_SUB
    return uid


def _require_ws_access(
    request: Request,
    ws_id: str,
    *,
    mgr: SessionManager | None = None,
) -> tuple[str, JSONResponse | None]:
    """Resolve ``ws_id`` to its owner, 404-ing when the row doesn't exist.

    Turnstone is a trusted-team tool: scope-level auth (e.g.
    ``admin.workstreams``) is the only gate; row-level ownership is not
    enforced here.  Returns ``(owner_user_id, None)`` on success — the
    persisted owner id, which attachments should be filed under so
    existing storage shape is preserved.  Falls back to the caller's
    own uid when the row has no recorded owner.

    When ``mgr`` is provided and the workstream is live in the manager,
    trust its cached ``user_id`` instead of round-tripping storage —
    keeps in-memory-only handlers (approve / plan / cancel / command /
    close / SSE / title) functional during transient DB outages and
    trims the hot-path by one query.  Handlers that act on
    persisted-but-not-loaded workstreams (``/delete``, ``/open``) omit
    ``mgr`` and fall through to the storage path.
    """
    caller = _auth_user_id(request)

    if mgr is not None:
        ws_mem = mgr.get(ws_id)
        if ws_mem is not None:
            return ws_mem.user_id or caller, None
        # Not in memory — fall through to storage so /delete etc.
        # still resolve persisted-but-not-loaded rows.

    from turnstone.core.memory import get_workstream_owner

    owner = get_workstream_owner(ws_id)
    if owner is None:
        return "", JSONResponse({"error": "Workstream not found"}, status_code=404)
    return owner or caller, None


async def upload_attachment(request: Request) -> JSONResponse:
    """POST /v1/api/workstreams/{ws_id}/attachments — upload one file.

    Multipart body with a single ``file`` field.  Validates size + MIME
    + magic bytes, enforces per-(ws,user) pending cap, then stores.
    """
    from turnstone.core.attachments import (
        IMAGE_SIZE_CAP,
        MAX_PENDING_ATTACHMENTS_PER_USER_WS,
        TEXT_DOC_SIZE_CAP,
    )
    from turnstone.core.memory import list_pending_attachments, save_attachment
    from turnstone.core.web_helpers import read_multipart_file_or_400

    ws_id = request.path_params.get("ws_id", "")
    if not ws_id:
        return JSONResponse({"error": "ws_id is required"}, status_code=400)

    user_id, err = _require_ws_access(request, ws_id)
    if err:
        return err

    # Cap at image size (largest permitted type) — per-kind cap enforced below.
    got = await read_multipart_file_or_400(request, field="file", max_bytes=IMAGE_SIZE_CAP)
    if isinstance(got, JSONResponse):
        return got
    filename, claimed_mime, data = got

    if not data:
        return JSONResponse({"error": "Empty file"}, status_code=400)

    # Classify: image (magic-byte sniff) vs text (mime/ext + UTF-8 decode)
    sniffed_image = _sniff_image_mime(data)
    if sniffed_image is not None:
        if len(data) > IMAGE_SIZE_CAP:
            return JSONResponse(
                {
                    "error": (
                        f"Image too large ({len(data):,} bytes); cap is {IMAGE_SIZE_CAP:,} bytes."
                    ),
                    "code": "too_large",
                },
                status_code=413,
            )
        kind = "image"
        mime = sniffed_image
    else:
        if len(data) > TEXT_DOC_SIZE_CAP:
            return JSONResponse(
                {
                    "error": (
                        f"Text document too large ({len(data):,} bytes); "
                        f"cap is {TEXT_DOC_SIZE_CAP:,} bytes."
                    ),
                    "code": "too_large",
                },
                status_code=413,
            )
        mime_or_err = _classify_text_attachment(filename, claimed_mime, data)
        if mime_or_err[0] is None:
            return JSONResponse({"error": mime_or_err[1], "code": "unsupported"}, status_code=400)
        kind = "text"
        mime = mime_or_err[0]

    # Serialize count-check + save per (ws, user) so concurrent uploads
    # can't both pass a check that sees count == cap-1.  Plain
    # threading.Lock (not asyncio.Lock) — see _attachment_upload_lock
    # for why.  The critical section is short, so blocking the event
    # loop briefly is acceptable.
    lock = _attachment_upload_lock(ws_id, user_id)
    with lock:
        if len(list_pending_attachments(ws_id, user_id)) >= MAX_PENDING_ATTACHMENTS_PER_USER_WS:
            return JSONResponse(
                {
                    "error": (
                        f"Too many pending attachments "
                        f"(max {MAX_PENDING_ATTACHMENTS_PER_USER_WS} pending per workstream)"
                    ),
                    "code": "too_many",
                },
                status_code=409,
            )
        attachment_id = uuid.uuid4().hex
        save_attachment(
            attachment_id,
            ws_id,
            user_id,
            filename,
            mime,
            len(data),
            kind,
            data,
        )
    return JSONResponse(
        {
            "attachment_id": attachment_id,
            "filename": filename,
            "mime_type": mime,
            "size_bytes": len(data),
            "kind": kind,
        }
    )


async def list_attachments(request: Request) -> JSONResponse:
    """GET /v1/api/workstreams/{ws_id}/attachments — list current user's
    pending (unconsumed) attachments for this workstream.
    """
    from turnstone.core.memory import list_pending_attachments

    ws_id = request.path_params.get("ws_id", "")
    if not ws_id:
        return JSONResponse({"error": "ws_id is required"}, status_code=400)
    user_id, err = _require_ws_access(request, ws_id)
    if err:
        return err
    rows = list_pending_attachments(ws_id, user_id)
    return JSONResponse({"attachments": rows})


async def get_attachment_content(request: Request) -> Response:
    """GET /v1/api/workstreams/{ws_id}/attachments/{attachment_id}/content —
    raw bytes of the attachment with its stored ``Content-Type``.

    The caller must own the workstream (or hold service scope).
    Unknown / cross-workstream ids return 404 to avoid leaking existence.
    """
    from turnstone.core.memory import get_attachment

    ws_id = request.path_params.get("ws_id", "")
    attachment_id = request.path_params.get("attachment_id", "")
    if not ws_id or not attachment_id:
        return JSONResponse({"error": "ws_id and attachment_id are required"}, status_code=400)
    user_id, err = _require_ws_access(request, ws_id)
    if err:
        return err
    row = get_attachment(attachment_id)
    # Scope on user_id too — in an unowned workstream different users
    # could otherwise fetch each other's blobs via id-guessing.  Mask
    # cross-user / cross-ws as 404 to avoid leaking existence.
    if not row or row.get("ws_id") != ws_id or row.get("user_id") != user_id:
        return JSONResponse({"error": "Not found"}, status_code=404)
    body = row.get("content") or b""
    kind = row.get("kind") or ""
    stored_mime = row.get("mime_type") or "application/octet-stream"
    filename = str(row.get("filename") or "attachment")
    # Force text/plain for text kinds — avoids same-origin HTML/SVG
    # rendering if a user uploaded an HTML-ish text file.  Images keep
    # their sniffed MIME (the allowlist is strict: png/jpeg/gif/webp).
    response_mime = "text/plain; charset=utf-8" if kind == "text" else stored_mime
    # Sanitize filename for Content-Disposition (quotes / CRLF only —
    # browsers tolerate most other characters).  RFC 6266 filename*=
    # would be more complete but isn't needed for the inline-attachment
    # use case here.
    safe_name = filename.replace('"', "").replace("\r", "").replace("\n", "")
    headers = {
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; sandbox",
        "Content-Disposition": f'inline; filename="{safe_name}"',
        "Cache-Control": "private, no-store",
    }
    return Response(body, media_type=response_mime, headers=headers)


async def delete_attachment(request: Request) -> JSONResponse:
    """DELETE /v1/api/workstreams/{ws_id}/attachments/{attachment_id} —
    remove a pending attachment.  Consumed attachments return 404.
    """
    from turnstone.core.memory import delete_attachment as _delete

    ws_id = request.path_params.get("ws_id", "")
    attachment_id = request.path_params.get("attachment_id", "")
    if not ws_id or not attachment_id:
        return JSONResponse({"error": "ws_id and attachment_id are required"}, status_code=400)
    user_id, err = _require_ws_access(request, ws_id)
    if err:
        return err
    deleted = _delete(attachment_id, ws_id, user_id)
    if not deleted:
        return JSONResponse({"error": "Not found"}, status_code=404)
    return JSONResponse({"status": "deleted"})


async def open_workstream(request: Request) -> JSONResponse:
    """POST /v1/api/workstreams/{ws_id}/open — load a saved workstream into memory.

    Unlike resume (which creates a NEW workstream and forks), this endpoint
    loads the existing workstream into memory with its original ws_id preserved.
    """
    from turnstone.core.log import get_logger
    from turnstone.core.memory import get_workstream_display_name, resolve_workstream
    from turnstone.core.storage import get_storage as _get_storage

    log = get_logger(__name__)
    ws_id = request.path_params.get("ws_id", "")
    if not ws_id:
        return JSONResponse({"error": "ws_id is required"}, status_code=400)

    resolved_id = resolve_workstream(ws_id)
    if not resolved_id:
        return JSONResponse({"error": "Workstream not found"}, status_code=404)

    mgr: SessionManager = request.app.state.workstreams

    if mgr.get(resolved_id):
        return JSONResponse(
            {
                "ws_id": resolved_id,
                "name": get_workstream_display_name(resolved_id) or resolved_id,
                "already_loaded": True,
            }
        )

    _st = _get_storage()
    ws_row = _st.get_workstream(resolved_id)
    if not ws_row:
        return JSONResponse({"error": "Workstream not found in storage"}, status_code=404)

    # Coordinator workstreams live on the console process, not on server
    # nodes.  Refuse to rehydrate one here so we don't silently build a
    # coordinator-kind ChatSession with coord_client=None (A/S1 guard).
    if ws_row.get("kind") != WorkstreamKind.INTERACTIVE:
        return JSONResponse(
            {"error": "Workstream is not an interactive kind"},
            status_code=400,
        )

    uid: str = _auth_user_id(request)

    # Trusted-team model: scope-level auth (already enforced by the
    # middleware) is the only gate.  ``user_id`` is metadata for audit
    # + display, not an access boundary, so any authenticated caller
    # can rehydrate any persisted workstream.  Fall back to the
    # caller's uid when the row has no recorded owner.
    stored_owner = (ws_row.get("user_id") or "").strip()
    owner_uid = stored_owner or uid

    try:
        ws = mgr.create(
            user_id=owner_uid,
            name=ws_row.get("name", ""),
            ws_id=resolved_id,
            parent_ws_id=ws_row.get("parent_ws_id"),
        )
    except Exception as e:
        log.warning("ws.open.create_failed", ws_id=resolved_id[:8], error=str(e))
        return JSONResponse({"error": f"Failed to load workstream: {e}"}, status_code=500)

    if not isinstance(ws.ui, WebUI):
        msg = f"Expected WebUI, got {type(ws.ui).__name__}"
        raise TypeError(msg)

    if ws.session is not None and ws.session.resume(resolved_id):
        ws.name = get_workstream_display_name(resolved_id) or ws.name
        ui = ws.ui
        ui._enqueue({"type": "clear_ui"})
        history = _build_history(ws.session)
        if history:
            ui._enqueue({"type": "history", "messages": history})

    gq: queue.Queue[dict[str, Any]] = request.app.state.global_queue
    with contextlib.suppress(queue.Full):
        gq.put_nowait(
            {
                "type": "ws_created",
                "ws_id": ws.id,
                "name": ws.name,
                "model": ws.session.model if ws.session else "",
                "model_alias": ws.session.model_alias if ws.session else "",
                "kind": ws.kind,
                "parent_ws_id": ws.parent_ws_id,
                "user_id": ws.user_id,
            }
        )

    # Audit the rehydration so console-side forensic review can distinguish
    # rehydrated workstreams from fresh creates (same broadcast shape; the
    # audit action name is the disambiguator).
    _audit_storage = getattr(request.app.state, "auth_storage", None)
    if _audit_storage is not None:
        from turnstone.core.audit import record_audit as _record_audit

        _, _audit_ip = _audit_context(request)
        _record_audit(
            _audit_storage,
            _auth_user_id(request),
            "workstream.opened",
            "workstream",
            ws.id,
            {"kind": str(ws.kind), "parent_ws_id": ws.parent_ws_id},
            _audit_ip,
        )

    log.info("ws.opened", ws_id=resolved_id[:8])
    return JSONResponse({"ws_id": ws.id, "name": ws.name})


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


async def auth_refresh(request: Request) -> Response:
    """POST /v1/api/auth/refresh — extend the auth cookie's expiry.

    Requires a currently-valid cookie (auth middleware enforces).  Re-
    resolves user permissions from storage so role changes propagate.
    """
    from turnstone.core.auth import handle_auth_refresh

    return await handle_auth_refresh(request, JWT_AUD_SERVER)


async def oidc_authorize(request: Request) -> Response:
    """GET /v1/api/auth/oidc/authorize — redirect to OIDC provider."""
    from turnstone.core.auth import handle_oidc_authorize

    return await handle_oidc_authorize(request, JWT_AUD_SERVER)


async def oidc_callback(request: Request) -> Response:
    """GET /v1/api/auth/oidc/callback — OIDC callback, exchange code for JWT."""
    from turnstone.core.auth import handle_oidc_callback

    return await handle_oidc_callback(request, JWT_AUD_SERVER)


def list_interface_settings(request: Request) -> JSONResponse:
    """GET /v1/api/admin/settings — return interface settings from ConfigStore.

    This lightweight endpoint mirrors the console's admin settings endpoint
    so that the main UI can load interface preferences (theme, close_tab_action)
    when accessed directly or through the console proxy.  Only returns the
    ``interface.*`` settings — full admin management is on the console.
    """
    from turnstone.core.settings_registry import SETTINGS

    cs = getattr(request.app.state, "config_store", None)
    settings: list[dict[str, Any]] = []
    for key, defn in sorted(SETTINGS.items()):
        if not key.startswith("interface."):
            continue
        value = cs.get(key) if cs else defn.default
        settings.append(
            {
                "key": key,
                "value": value,
                "source": "storage" if cs and key in cs.stored_keys() else "default",
                "type": defn.type,
                "description": defn.description,
                "section": defn.section,
            }
        )
    return JSONResponse({"settings": settings})


async def update_interface_setting(request: Request, key: str = "") -> JSONResponse:
    """POST /v1/api/admin/settings/{key} — update an interface.* setting.

    Lightweight endpoint so the main UI (served via the console proxy) can
    persist interface preferences without needing a PUT route.  Only
    ``interface.*`` keys are accepted; full admin management stays on the
    console.

    Writes with ``node_id=""`` (global scope) so the console admin page
    and all nodes see the same value.
    """
    from turnstone.core.log import get_logger
    from turnstone.core.settings_registry import SETTINGS, serialize_value, validate_value
    from turnstone.core.storage import get_storage as _get_storage
    from turnstone.core.web_helpers import read_json_or_400

    log = get_logger(__name__)
    key = request.path_params.get("key", "")
    if not key.startswith("interface."):
        return JSONResponse({"error": "only interface.* settings accepted"}, status_code=400)
    if key not in SETTINGS:
        return JSONResponse({"error": f"unknown setting: {key}"}, status_code=400)

    body = await read_json_or_400(request)
    if isinstance(body, JSONResponse):
        return body
    if "value" not in body:
        return JSONResponse({"error": "value is required"}, status_code=400)

    try:
        typed_value = validate_value(key, body["value"])
    except (ValueError, KeyError) as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    # Write to storage with global scope (node_id="") so the console
    # admin page and all nodes read the same value.
    storage = _get_storage()
    if storage is None:
        return JSONResponse({"error": "storage unavailable"}, status_code=503)
    defn = SETTINGS[key]
    storage.upsert_system_setting(
        key=key,
        value=serialize_value(typed_value),
        node_id="",
        is_secret=defn.is_secret,
    )

    # Update the local ConfigStore cache so this node sees the change
    # immediately (without waiting for a config-reload).
    cs = getattr(request.app.state, "config_store", None)
    if cs is not None:
        cs.reload()

    log.info("interface_setting.updated", key=key, value=typed_value)

    # Broadcast settings_changed so other connected clients pick it up
    gq = getattr(request.app.state, "global_queue", None)
    if gq is not None:
        with contextlib.suppress(queue.Full):
            gq.put_nowait({"type": "settings_changed"})

    return JSONResponse({"status": "ok", "key": key, "value": typed_value})


def config_reload(request: Request) -> JSONResponse:
    """POST /v1/api/_internal/config-reload — invalidate config cache."""
    cs = getattr(request.app.state, "config_store", None)
    gq = getattr(request.app.state, "global_queue", None)
    if not cs:
        return JSONResponse({"status": "noop"})
    cs.reload()
    # Apply routing overrides to the live registry — admin settings updates
    # fan out via this endpoint and would otherwise not affect plan/task
    # routing until a model-reload or restart.
    registry = getattr(request.app.state, "registry", None)
    if registry is not None:
        _apply_routing_overrides(registry, cs)
    # Broadcast settings_changed event to all connected clients
    if gq is not None:
        with contextlib.suppress(queue.Full):
            gq.put_nowait({"type": "settings_changed"})
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
        mcp_mgr.set_storage(storage)
        request.app.state.mcp_client = mcp_mgr
        # Update shared ref so session_factory sees the new client
        mcp_ref = getattr(request.app.state, "mcp_ref", None)
        if mcp_ref is not None:
            mcp_ref[0] = mcp_mgr

    result = mcp_mgr.reconcile_sync(storage)
    return JSONResponse({"status": "ok", **result})


def internal_mcp_status(request: Request) -> JSONResponse:
    """GET /v1/api/_internal/mcp-status — return MCP server status."""
    mcp_mgr = getattr(request.app.state, "mcp_client", None)
    if mcp_mgr is None:
        return JSONResponse({"servers": {}})

    return JSONResponse({"servers": mcp_mgr.get_all_server_status()})


# -- internal model management -----------------------------------------------


def _effective_routing(
    cs: Any,
    base_models: dict[str, Any],
    base_default: str,
    base_plan_model: str | None,
    base_task_model: str | None,
    base_plan_effort: str | None,
    base_task_effort: str | None,
) -> tuple[str, str | None, str | None, str | None, str | None]:
    """Compute (default, plan_model, task_model, plan_effort, task_effort)
    after layering ConfigStore overrides on top of the supplied base values.

    Aliases require existence in *base_models* (silently dropped otherwise);
    effort values were validated against SettingDef choices on write, so a
    truthiness check is sufficient at apply time.

    Returns the base values unchanged when *cs* is None.
    """
    eff_default = base_default
    eff_plan_model = base_plan_model
    eff_task_model = base_task_model
    eff_plan_effort = base_plan_effort
    eff_task_effort = base_task_effort
    if cs is not None:
        cs_default = cs.get("model.default_alias")
        if cs_default and cs_default in base_models:
            eff_default = cs_default
        cs_plan_alias = cs.get("model.plan_alias")
        if cs_plan_alias and cs_plan_alias in base_models:
            eff_plan_model = cs_plan_alias
        cs_task_alias = cs.get("model.task_alias")
        if cs_task_alias and cs_task_alias in base_models:
            eff_task_model = cs_task_alias
        cs_plan_effort = cs.get("model.plan_effort")
        if cs_plan_effort:
            eff_plan_effort = cs_plan_effort
        cs_task_effort = cs.get("model.task_effort")
        if cs_task_effort:
            eff_task_effort = cs_task_effort
    return eff_default, eff_plan_model, eff_task_model, eff_plan_effort, eff_task_effort


def _broadcast_agent_tool_schema_refresh(app_state: Any) -> None:
    """Tell every active session on this node to re-render its plan_agent /
    task_agent tool descriptions.  Best-effort: a session that lacks the
    method (older code path or test stub) is skipped silently.

    Called after a registry reload that may have added/removed model
    aliases, so the calling LLMs see an updated `model` parameter
    description on their next turn.
    """
    mgr = getattr(app_state, "workstreams", None)
    if mgr is None:
        return
    try:
        workstreams = mgr.list_all()
    except Exception:
        return
    for ws in workstreams:
        session = getattr(ws, "session", None)
        refresh = getattr(session, "refresh_agent_tool_schemas", None)
        if refresh is None:
            continue
        with contextlib.suppress(Exception):
            refresh()


def _apply_routing_overrides(registry: Any, cs: Any) -> bool:
    """Apply ConfigStore routing overrides to a live *registry* in place.

    Used by the startup path and by ``config_reload`` (admin settings
    update fan-out) — both keep the existing model definitions and only
    rewrite routing fields.  Returns True when a reload happened.
    """
    eff = _effective_routing(
        cs,
        registry.models,
        registry.default,
        registry.plan_model,
        registry.task_model,
        registry.plan_effort,
        registry.task_effort,
    )
    if (
        eff[0] != registry.default
        or eff[1] != registry.plan_model
        or eff[2] != registry.task_model
        or eff[3] != registry.plan_effort
        or eff[4] != registry.task_effort
    ):
        registry.reload(
            registry.models,
            eff[0],
            registry.fallback,
            registry.agent_model,
            plan_model=eff[1],
            task_model=eff[2],
            plan_effort=eff[3],
            task_effort=eff[4],
        )
        return True
    return False


def internal_model_reload(request: Request) -> JSONResponse:
    """POST /v1/api/_internal/model-reload — rebuild registry from DB + config."""
    from turnstone.core.model_registry import load_model_registry
    from turnstone.core.storage._registry import get_storage

    registry = getattr(request.app.state, "registry", None)
    cli_args = getattr(request.app.state, "cli_model_args", None)
    if registry is None or cli_args is None:
        return JSONResponse({"status": "error", "reason": "no registry"}, status_code=503)

    new_registry = load_model_registry(
        base_url=cli_args["base_url"],
        api_key=cli_args["api_key"],
        model=cli_args["model"],
        context_window=cli_args["context_window"],
        provider=cli_args["provider"],
        storage=get_storage(),
    )
    cs = getattr(request.app.state, "config_store", None)
    if cs is not None:
        cs.reload()  # Ensure latest settings from DB
    eff_default, eff_plan_model, eff_task_model, eff_plan_effort, eff_task_effort = (
        _effective_routing(
            cs,
            new_registry.models,
            new_registry.default,
            new_registry.plan_model,
            new_registry.task_model,
            new_registry.plan_effort,
            new_registry.task_effort,
        )
    )
    if eff_default != new_registry.default:
        log.info(
            "ConfigStore override: using '%s' as default model (registry had '%s')",
            eff_default,
            new_registry.default,
        )

    # No-op fast path: skip reload when nothing changed (avoids client churn
    # on broadcast model-reloads where this node has no pending changes).
    unchanged = (
        new_registry.models == registry.models
        and new_registry.fallback == registry.fallback
        and new_registry.agent_model == registry.agent_model
        and eff_default == registry.default
        and eff_plan_model == registry.plan_model
        and eff_task_model == registry.task_model
        and eff_plan_effort == registry.plan_effort
        and eff_task_effort == registry.task_effort
    )
    if unchanged:
        new_registry.shutdown()
        return JSONResponse({"status": "ok", "aliases": registry.list_aliases(), "noop": True})

    try:
        registry.reload(
            new_registry.models,
            eff_default,
            new_registry.fallback,
            new_registry.agent_model,
            plan_model=eff_plan_model,
            task_model=eff_task_model,
            plan_effort=eff_plan_effort,
            task_effort=eff_task_effort,
        )
    except ValueError as exc:
        return JSONResponse({"status": "error", "reason": str(exc)}, status_code=422)
    finally:
        new_registry.shutdown()

    # Ensure health trackers exist for any newly-added backends
    health_reg = getattr(request.app.state, "health_registry", None)
    if health_reg:
        for alias in registry.list_aliases():
            cfg = registry.get_config(alias)
            health_reg.get_tracker(provider=cfg.provider, base_url=cfg.base_url)

    # Push the new alias list into active sessions so plan_agent/task_agent
    # `model` parameter descriptions reflect the current registry.
    _broadcast_agent_tool_schema_refresh(request.app.state)

    return JSONResponse({"status": "ok", "aliases": registry.list_aliases()})


def internal_model_status(request: Request) -> JSONResponse:
    """GET /v1/api/_internal/model-status — return this node's model aliases."""
    registry = getattr(request.app.state, "registry", None)
    if registry is None:
        return JSONResponse({"models": {}})

    models: dict[str, dict[str, Any]] = {}
    for alias in registry.list_aliases():
        cfg = registry.get_config(alias)
        models[alias] = {
            "model": cfg.model,
            "provider": cfg.provider,
            "source": cfg.source,
            "context_window": cfg.context_window,
            "enabled": True,
            "temperature": cfg.temperature,
            "max_tokens": cfg.max_tokens,
            "reasoning_effort": cfg.reasoning_effort,
        }
    return JSONResponse({"models": models})


# ---------------------------------------------------------------------------
# Global SSE fan-out
# ---------------------------------------------------------------------------


def _emit_health_changed(
    status: str, gq: queue.Queue[dict[str, Any]], app_state: Any = None
) -> None:
    """Push a health_changed event onto the global SSE queue.

    Called from the BackendHealthTracker callback on state transitions.
    *status* is ``"healthy"`` or ``"degraded"``.

    Also updates the global ``turnstone_backend_up`` metric using the
    effective default backend's health (not the backend that triggered
    this callback, which may be a non-default fallback).
    """
    if app_state is not None:
        _update_backend_metric(app_state)
    with contextlib.suppress(queue.Full):
        gq.put_nowait(
            {
                "type": "health_changed",
                "backend_status": status,
            }
        )


def _update_backend_metric(app_state: Any) -> None:
    """Update ``turnstone_backend_up`` from the effective default's tracker.

    Called on any backend state change.  Only the effective default
    backend drives this global metric — fallback backend transitions
    do not affect it.
    """
    health_reg = getattr(app_state, "health_registry", None)
    registry = getattr(app_state, "registry", None)
    if not health_reg or not registry:
        return
    config_store = getattr(app_state, "config_store", None)
    effective = None
    if config_store:
        effective = config_store.get("model.default_alias") or None
    tracker = None
    if effective:
        tracker = health_reg.get_tracker_for_alias(registry, effective)
    if tracker is None:
        tracker = health_reg.get_tracker_for_alias(registry, registry.default)
    if tracker is not None:
        _metrics.set_backend_status(tracker.is_healthy)


def _aggregate_emitter_thread(
    mgr: SessionManager,
    global_queue: queue.Queue[dict[str, Any]],
    interval: float = 10.0,
) -> None:
    """Periodically emit aggregate token/tool_call totals on the global SSE queue.

    Runs as a daemon thread so the console receives periodic updates without
    having to poll ``/v1/api/dashboard``.
    """
    while True:
        time.sleep(interval)
        total_tokens = 0
        total_tool_calls = 0
        active_count = 0
        try:
            for ws in mgr.list_all():
                ui = ws.ui
                if hasattr(ui, "_ws_lock"):
                    with ui._ws_lock:  # type: ignore[union-attr]
                        tok = ui._ws_prompt_tokens + ui._ws_completion_tokens  # type: ignore[union-attr]
                        tc = sum(ui._ws_tool_calls.values())  # type: ignore[union-attr]
                else:
                    tok = 0
                    tc = 0
                total_tokens += tok
                total_tool_calls += tc
                if ws.state.value != "idle":
                    active_count += 1
            with contextlib.suppress(queue.Full):
                global_queue.put_nowait(
                    {
                        "type": "aggregate",
                        "total_tokens": total_tokens,
                        "total_tool_calls": total_tool_calls,
                        "active_count": active_count,
                        "total_count": len(mgr.list_all()),
                    }
                )
        except Exception:
            log.debug("Aggregate emitter error", exc_info=True)


def _idle_cleanup_thread(
    mgr: SessionManager,
    timeout_sec: float,
    global_queue: queue.Queue[dict[str, Any]],
    rate_limiter: Any = None,
) -> None:
    """Periodically close IDLE workstreams and clean up rate limiter buckets.

    ``mgr.close_idle`` fires the adapter's ``emit_closed`` for each
    victim, which pushes ``ws_closed`` onto ``global_queue`` with
    ``reason="closed"``. The old manual emission here (``reason="idle"``)
    is gone — the frontend didn't differentiate "idle" from "closed"
    anyway and the duplicate event caused spurious UI flicker.
    """
    del global_queue  # adapter handles the emission
    check_every = min(300.0, timeout_sec / 4)  # check at 1/4 of timeout, max 5 min
    while True:
        time.sleep(check_every)
        mgr.close_idle(timeout_sec)
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
    # Start aggregate emitter thread for SSE consumers
    agg_emitter = threading.Thread(
        target=_aggregate_emitter_thread,
        args=(app.state.workstreams, app.state.global_queue),
        daemon=True,
    )
    agg_emitter.start()
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

    # Sweep stale attachment reservations left over from process crashes
    # between reserve_attachments and consume/unreserve.  Run once at
    # startup (catches anything orphaned by the previous process), then
    # periodically as defense-in-depth.
    from turnstone.core.memory import sweep_orphan_reservations as _sweep_orphans

    try:
        n = await asyncio.to_thread(_sweep_orphans, _ORPHAN_SWEEP_THRESHOLD_S)
        if n:
            log.info("attachments.orphan_sweep.startup", swept=n)
    except Exception:
        log.warning("attachments.orphan_sweep.startup_failed", exc_info=True)

    _orphan_sweep_stop = asyncio.Event()

    async def _orphan_sweep_loop() -> None:
        while not _orphan_sweep_stop.is_set():
            try:
                await asyncio.wait_for(_orphan_sweep_stop.wait(), timeout=_ORPHAN_SWEEP_INTERVAL_S)
                return  # stop event fired
            except TimeoutError:
                pass
            try:
                n = await asyncio.to_thread(_sweep_orphans, _ORPHAN_SWEEP_THRESHOLD_S)
                if n:
                    log.info("attachments.orphan_sweep.periodic", swept=n)
            except Exception:
                log.warning("attachments.orphan_sweep.periodic_failed", exc_info=True)

    _orphan_sweep_task = asyncio.create_task(_orphan_sweep_loop())
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

    # Register in service registry and start heartbeat
    _heartbeat_task: asyncio.Task[None] | None = None
    _svc_node_id: str = getattr(app.state, "node_id", "")
    _svc_url: str = getattr(app.state, "advertise_url", "")
    if _svc_node_id and _svc_url:
        from turnstone.core.storage import get_storage as _get_svc_storage

        _svc_storage = _get_svc_storage()
        _svc_storage.register_service("server", _svc_node_id, _svc_url)
        log.info("server.service_registered", node_id=_svc_node_id, url=_svc_url)

        # Collect and store node metadata (auto + config)
        try:
            from turnstone.core.config import load_config as _load_meta_config
            from turnstone.core.node_info import collect_node_info

            _auto_info = collect_node_info()
            _meta_entries: list[tuple[str, str, str]] = [
                (k, json.dumps(v), "auto") for k, v in _auto_info.items()
            ]
            _cfg_meta = _load_meta_config("metadata")
            _meta_entries.extend((k, json.dumps(v), "config") for k, v in _cfg_meta.items())
            if _meta_entries:
                # Clear stale auto/config rows from a prior run before upserting
                _svc_storage.delete_node_metadata_by_source(_svc_node_id, "auto")
                _svc_storage.delete_node_metadata_by_source(_svc_node_id, "config")
                _svc_storage.set_node_metadata_bulk(_svc_node_id, _meta_entries)
                log.info(
                    "server.node_metadata_stored",
                    node_id=_svc_node_id,
                    count=len(_meta_entries),
                )
        except Exception:
            log.warning("server.node_metadata_failed", node_id=_svc_node_id, exc_info=True)

        async def _heartbeat_loop() -> None:
            """Periodically update service heartbeat."""
            from turnstone.core.storage._registry import StorageUnavailableError

            while True:
                await asyncio.sleep(30)
                try:
                    await asyncio.to_thread(_svc_storage.heartbeat_service, "server", _svc_node_id)
                except StorageUnavailableError:
                    pass  # already logged by storage layer
                except Exception:
                    log.exception("server.heartbeat_failed")

        _heartbeat_task = asyncio.create_task(_heartbeat_loop())

    yield
    # Shutdown
    if _heartbeat_task is not None:
        _heartbeat_task.cancel()
    if _svc_node_id and _svc_url:
        from turnstone.core.storage import get_storage as _get_svc_dereg

        try:
            _dereg_storage = _get_svc_dereg()
            await asyncio.to_thread(_dereg_storage.deregister_service, "server", _svc_node_id)
            await asyncio.to_thread(
                _dereg_storage.delete_node_metadata_by_source, _svc_node_id, "auto"
            )
            await asyncio.to_thread(
                _dereg_storage.delete_node_metadata_by_source, _svc_node_id, "config"
            )
            log.info("server.service_deregistered", node_id=_svc_node_id)
        except Exception:
            log.exception("server.deregister_failed")
    tls_client = getattr(app.state, "tls_client", None)
    if tls_client is not None:
        await tls_client.stop_renewal()
    if app.state.watch_runner:
        app.state.watch_runner.stop()
    # Stop the orphan-reservation sweep loop
    _orphan_sweep_stop.set()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await _orphan_sweep_task
    # health_registry is stateless (no background threads) — nothing to stop
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
            Middleware(AuthMiddleware, jwt_audience=JWT_AUD_SERVER, jwt_version=jwt_version_slot()),
            Middleware(RateLimitMiddleware),
        ]
    )
    return stack


def create_app(
    *,
    workstreams: SessionManager,
    global_queue: queue.Queue[dict[str, Any]],
    global_listeners: list[queue.Queue[dict[str, Any]]],
    global_listeners_lock: threading.Lock,
    skip_permissions: bool,
    jwt_secret: str = "",
    auth_storage: Any = None,
    health_registry: Any = None,
    rate_limiter: Any = None,
    mcp_client: Any = None,
    mcp_ref: list[Any] | None = None,
    registry: Any = None,
    idle_timeout: int = 0,
    node_id: str = "",
    cors_origins: list[str] | None = None,
    watch_runner: Any = None,
    judge_config: Any = None,
    config_store: Any = None,
    advertise_url: str = "",
) -> Starlette:
    """Create and configure the Starlette ASGI application."""
    _spec = build_server_spec()
    _openapi_handler = make_openapi_handler(_spec)
    _docs_handler = make_docs_handler()

    # Workstream HTTP tree — owned by the shared registrar in
    # ``turnstone.core.session_routes`` so the console mounts the same
    # shape against its coord manager.
    v1_routes: list[Any] = [
        Route("/api/events", events_sse),
        Route("/api/events/global", global_events_sse),
    ]
    register_session_routes(
        v1_routes,
        prefix="/api/workstreams",
        handlers=SharedSessionVerbHandlers(
            list_workstreams=list_workstreams,
            list_saved=list_saved_workstreams,
            create=create_workstream,
            close_legacy=close_workstream,
            delete=delete_workstream_endpoint,
            open=open_workstream,
            refresh_title=refresh_workstream_title,
            set_title=set_workstream_title,
            attachments=AttachmentHandlers(
                upload=upload_attachment,
                list=list_attachments,
                get_content=get_attachment_content,
                delete=delete_attachment,
            ),
        ),
    )
    v1_routes.append(Route("/api/dashboard", dashboard))

    app = Starlette(
        routes=[
            Route("/", index),
            Mount(
                "/v1",
                routes=[
                    *v1_routes,
                    Route("/api/skills", list_skills_summary),
                    Route("/api/models", list_available_models),
                    Route("/api/send", send_message, methods=["POST", "DELETE"]),
                    Route("/api/approve", approve, methods=["POST"]),
                    Route("/api/plan", plan_feedback, methods=["POST"]),
                    Route("/api/command", command, methods=["POST"]),
                    Route("/api/cancel", cancel_generation, methods=["POST"]),
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
                    Route("/api/auth/refresh", auth_refresh, methods=["POST"]),
                    Route("/api/auth/oidc/authorize", oidc_authorize),
                    Route("/api/auth/oidc/callback", oidc_callback),
                    Route("/api/admin/settings", list_interface_settings),
                    Route(
                        "/api/admin/settings/{key:path}",
                        update_interface_setting,
                        methods=["POST", "PUT"],
                    ),
                    Route("/api/_internal/config-reload", config_reload, methods=["POST"]),
                    Route("/api/_internal/mcp-reload", internal_mcp_reload, methods=["POST"]),
                    Route("/api/_internal/mcp-status", internal_mcp_status),
                    Route(
                        "/api/_internal/model-reload",
                        internal_model_reload,
                        methods=["POST"],
                    ),
                    Route("/api/_internal/model-status", internal_model_status),
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
    app.state.jwt_secret = jwt_secret
    app.state.auth_storage = auth_storage
    app.state.health_registry = health_registry
    app.state.rate_limiter = rate_limiter
    app.state.mcp_client = mcp_client
    app.state.mcp_ref = mcp_ref
    app.state.registry = registry
    app.state.idle_timeout = idle_timeout
    app.state.node_id = node_id
    app.state.watch_runner = watch_runner
    app.state.judge_config = judge_config
    app.state.config_store = config_store
    app.state.advertise_url = advertise_url

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

    import socket

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
    init_storage(
        db_backend,
        path=db_path,
        url=db_url,
        pool_size=db_pool_size,
        sslmode=getattr(args, "db_sslmode", None) or os.environ.get("TURNSTONE_DB_SSLMODE", ""),
        sslrootcert=getattr(args, "db_sslrootcert", None)
        or os.environ.get("TURNSTONE_DB_SSLROOTCERT", ""),
        sslcert=getattr(args, "db_sslcert", None) or os.environ.get("TURNSTONE_DB_SSLCERT", ""),
        sslkey=getattr(args, "db_sslkey", None) or os.environ.get("TURNSTONE_DB_SSLKEY", ""),
    )

    # Server-owned node identity (needed before ConfigStore for node_id scoping)
    def _default_node_id() -> str:
        """Generate a node_id: ``{hostname}_{4hex}``, or a UUID on failure."""
        suffix = uuid.uuid4().hex[:4]
        try:
            host = socket.gethostname()
            if host and host != "localhost":
                return f"{host}_{suffix}"
        except OSError:
            pass  # hostname unavailable, fall back to UUID
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

    cli_model = args.model
    effective_model = cli_model or None
    if effective_model:
        model = effective_model
        detected_ctx = None
    else:
        from turnstone.core.model_registry import detect_model

        model, detected_ctx = detect_model(client, provider=provider_name, fatal=False)
        if model is None:
            # LLM backend unreachable — no CLI model specified.
            # Set empty so load_model_registry skips the CLI "default"
            # entry and relies on DB / config.toml models instead.
            model = ""

    # Use detected context window, fall back to 32768
    if detected_ctx:
        context_window = detected_ctx
        log.info("Context window: %s (detected from backend)", f"{context_window:,}")
    else:
        context_window = 32768

    # Build model registry (reads [models.*] + database model definitions)
    from turnstone.core.model_registry import load_model_registry
    from turnstone.core.storage._registry import get_storage as _get_storage

    registry = load_model_registry(
        base_url=base_url,
        api_key=api_key,
        model=model,
        context_window=context_window,
        provider=provider_name,
        storage=_get_storage(),
    )

    # Apply runtime overrides from ConfigStore for default alias plus the
    # per-kind sub-agent routing.  Only triggers a reload when at least one
    # ConfigStore value differs from what the registry loaded from disk.
    # ConfigStore returns the SettingDef default ("" for these keys) when
    # unset — distinct from the registry's None for unconfigured fields.
    config_store.reload()  # symmetry with internal_model_reload's cs.reload()
    _apply_routing_overrides(registry, config_store)

    # Initialize MCP client (connects to configured MCP servers, if any)
    from turnstone.core.mcp_client import create_mcp_client

    mcp_config_cli = args.mcp_config  # CLI-only (no config.toml for this)
    mcp_client = create_mcp_client(
        mcp_config_cli or config_store.get("mcp.config_path") or None,
        refresh_interval=config_store.get("mcp.refresh_interval"),
        storage=_get_storage(),
    )
    # Mutable ref so session_factory always sees the latest MCP client,
    # including ones created by internal_mcp_reload after startup.
    _mcp_ref: list[Any] = [mcp_client]

    # Per-backend passive health tracking (no active probes / circuit breakers)
    from turnstone.core.healthcheck import HealthTrackerRegistry

    # Set up global event queue for state-change broadcasts (created early so
    # the health tracker callback can reference it).
    global_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=10000)
    global_listeners: list[queue.Queue[dict[str, Any]]] = []
    global_listeners_lock = threading.Lock()
    WebUI._global_queue = global_queue

    # Mutable ref so the health callback can access app.state after app
    # creation (same pattern as _mcp_ref).
    _app_ref: list[Any] = [None]

    health_registry = HealthTrackerRegistry(
        failure_threshold=config_store.get("health.failure_threshold"),
        on_state_changed=lambda _backend, state: _emit_health_changed(
            state, global_queue, _app_ref[0].state if _app_ref[0] else None
        ),
    )

    # Eagerly create trackers for all registered backends.  Sessions use
    # read-only lookups (get_tracker_for_alias) and never create trackers
    # on the hot path, so every backend must be registered here.
    for _alias in registry.list_aliases():
        _cfg = registry.get_config(_alias)
        health_registry.get_tracker(provider=_cfg.provider, base_url=_cfg.base_url)

    # Per-IP rate limiter
    from turnstone.core.ratelimit import RateLimiter

    rate_limiter = RateLimiter(
        enabled=config_store.get("ratelimit.enabled"),
        rate=config_store.get("ratelimit.requests_per_second"),
        burst=config_store.get("ratelimit.burst"),
        trusted_proxies=config_store.get("ratelimit.trusted_proxies"),
    )

    # Config builders — shared between startup logging and session factory.
    # Re-read from ConfigStore each call so hot-reload works.
    from turnstone.core.judge import JudgeConfig
    from turnstone.core.memory_relevance import MemoryConfig

    def _build_judge_config() -> JudgeConfig:
        return JudgeConfig(
            enabled=config_store.get("judge.enabled"),
            model=config_store.get("judge.model"),
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
    def _effective_default_alias() -> str:
        """Return the runtime-effective default model alias.

        Checks ConfigStore for a ``model.default_alias`` override first,
        then falls back to the registry's static default.
        """
        cs_alias: str = config_store.get("model.default_alias")
        if cs_alias and registry.has_alias(cs_alias):
            return cs_alias
        return registry.default

    def session_factory(
        ui: SessionUI | None,
        model_alias: str | None = None,
        ws_id: str | None = None,
        *,
        skill: str | None = None,
        client_type: str = "",
        judge_model: str | None = None,
        kind: WorkstreamKind = WorkstreamKind.INTERACTIVE,
        parent_ws_id: str | None = None,
    ) -> ChatSession:
        assert ui is not None
        # Resolve the effective alias once and use it consistently
        # for both client resolution and ChatSession.model_alias.
        model_alias = model_alias or _effective_default_alias()
        r_client, r_model, r_cfg = registry.resolve(model_alias)
        # Read MCP client from shared ref — may have been replaced after startup
        # by internal_mcp_reload (Sync to Nodes) when no --mcp-config was passed.
        live_mcp_client = _mcp_ref[0]
        uid = getattr(ui, "_user_id", "") or ""

        # Resolve username from user_id for system message context
        _username = ""
        if uid:
            try:
                from turnstone.core.storage._registry import get_storage as _gs

                _st = _gs()
                if _st:
                    _u = _st.get_user(uid)
                    if _u:
                        _username = _u.get("username", "")
            except Exception:
                log.debug("Failed to resolve username for uid %s", uid, exc_info=True)

        # Re-resolve from ConfigStore so new workstreams pick up hot-reloaded settings.
        live_memory_config = _build_memory_config()
        live_judge_config = _build_judge_config()
        if live_judge_config and judge_model:
            import dataclasses

            # Override the config-default judge model with the per-call
            # alias, but DON'T replace the alias with the resolved
            # underlying model id.  IntentJudge.__init__ does the full
            # resolution (alias → client + provider + model) and
            # pre-rewriting ``model`` to the underlying id strands the
            # alias context — IntentJudge then has no way to recover the
            # alias's provider/client and falls back to the session's
            # provider with a model name that provider may not support
            # (silent ``llm_fallback`` verdicts).  ``registry.resolve``
            # is called purely as a typo / unknown-alias guard.
            try:
                registry.resolve(judge_model)
                live_judge_config = dataclasses.replace(
                    live_judge_config,
                    model=judge_model,
                )
            except Exception as e:
                log.warning("Failed to resolve judge_model %r: %s", judge_model, e)

        # Per-model sampling overrides take priority over global defaults
        eff_temperature = (
            r_cfg.temperature
            if r_cfg.temperature is not None
            else config_store.get("model.temperature")
        )
        eff_max_tokens = (
            r_cfg.max_tokens
            if r_cfg.max_tokens is not None
            else config_store.get("model.max_tokens")
        )
        eff_reasoning_effort = (
            r_cfg.reasoning_effort
            if r_cfg.reasoning_effort is not None
            else config_store.get("model.reasoning_effort")
        )

        return ChatSession(
            client=r_client,
            model=r_model,
            ui=ui,
            instructions=config_store.get("session.instructions") or None,
            temperature=eff_temperature,
            max_tokens=eff_max_tokens,
            tool_timeout=config_store.get("tools.timeout"),
            reasoning_effort=eff_reasoning_effort,
            context_window=r_cfg.context_window,
            compact_max_tokens=config_store.get("session.compact_max_tokens"),
            auto_compact_pct=config_store.get("session.auto_compact_pct"),
            agent_max_turns=config_store.get("tools.agent_max_turns"),
            tool_truncation=config_store.get("tools.truncation"),
            mcp_client=live_mcp_client,
            registry=registry,
            model_alias=model_alias,
            health_registry=health_registry,
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
            client_type=ClientType(client_type)
            if client_type in {ct.value for ct in ClientType}
            else ClientType.WEB,
            username=_username,
            kind=kind,
            parent_ws_id=parent_ws_id,
        )

    # Create WatchRunner (periodic command polling, server-level)
    from turnstone.core.storage import get_storage as _get_storage
    from turnstone.core.watch import WatchRunner

    # Create session manager first (watch restore_fn captures it).
    interactive_adapter = InteractiveAdapter(
        global_queue=global_queue,
        ui_factory=lambda ws: WebUI(
            ws_id=ws.id,
            user_id=ws.user_id,
            kind=ws.kind,
            parent_ws_id=ws.parent_ws_id,
        ),
        session_factory=session_factory,
    )
    manager = SessionManager(
        interactive_adapter,
        storage=_get_storage(),
        max_active=config_store.get("server.max_workstreams"),
        node_id=_node_id,
    )
    interactive_adapter.attach(manager)
    WebUI._workstream_mgr = manager

    def _watch_restore_fn(ws_id: str) -> Any:
        """Restore an evicted workstream so a watch can deliver results.

        Returns a callable that starts a worker thread to send() the watch
        result.  Unlike the normal dispatch path (which enqueues for IDLE
        drain), the restored workstream has no active send() loop, so we
        must start a worker thread directly — same pattern as send_message().
        """
        try:
            ws = manager.create(user_id="", name="watch-restore")
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

    # ``--resume`` lazily creates a workstream scoped to the resumed
    # content. Without ``--resume`` no default workstream is spawned;
    # the web UI handles the 0-ws state and users create workstreams
    # on demand via POST /v1/api/workstreams.
    if args.resume:
        from turnstone.core.memory import resolve_workstream

        target_id = resolve_workstream(args.resume)
        if not target_id:
            log.error("Workstream not found: %s", args.resume)
            sys.exit(1)
        ws = manager.create(user_id="", name="resumed")
        if not isinstance(ws.ui, WebUI):
            raise TypeError(f"Expected WebUI, got {type(ws.ui).__name__}")
        if config_store.get("tools.skip_permissions"):
            ws.ui.auto_approve = True
        assert ws.session is not None
        ws.session.set_watch_runner(
            _watch_runner, dispatch_fn=_make_watch_dispatch(ws, ws.session, ws.ui)
        )
        if not ws.session.resume(target_id):
            log.error("Workstream '%s' has no messages.", args.resume)
            sys.exit(1)
        log.info("Resumed workstream %s (%d messages)", target_id, len(ws.session.messages))

    # Record detected model and judge status in metrics
    _metrics.model = model
    _metrics.set_judge_enabled(judge_config.enabled if judge_config else False)

    # Auth config
    from turnstone.core.auth import load_jwt_secret
    from turnstone.core.storage import get_storage

    jwt_secret = load_jwt_secret()
    log.info("Auth: enabled (JWT)")

    # Build the ASGI app
    from turnstone.core.web_helpers import parse_cors_origins

    cors_origins = parse_cors_origins()

    # Construct advertise URL for service registration.  Priority:
    # 1. TURNSTONE_ADVERTISE_URL env var (required in Docker/k8s where
    #    gethostname() returns a container ID that peers can't resolve)
    # 2. Explicit --host (not a wildcard bind address)
    # 3. socket.gethostname() (bare-metal fallback; getfqdn() does
    #    reverse DNS which often truncates the hostname)
    _advertise_url = os.environ.get("TURNSTONE_ADVERTISE_URL", "")
    if not _advertise_url:
        _advertise_host = args.host if args.host not in ("0.0.0.0", "::") else socket.gethostname()
        _advertise_url = f"http://{_advertise_host}:{args.port}"

    _skip_perms = config_store.get("tools.skip_permissions")
    app = create_app(
        workstreams=manager,
        global_queue=global_queue,
        global_listeners=global_listeners,
        global_listeners_lock=global_listeners_lock,
        skip_permissions=_skip_perms,
        jwt_secret=jwt_secret,
        auth_storage=get_storage(),
        health_registry=health_registry,
        rate_limiter=rate_limiter,
        mcp_client=mcp_client,
        mcp_ref=_mcp_ref,
        registry=registry,
        idle_timeout=config_store.get("server.workstream_idle_timeout"),
        node_id=_node_id,
        cors_origins=cors_origins,
        watch_runner=_watch_runner,
        judge_config=judge_config,
        config_store=config_store,
        advertise_url=_advertise_url,
    )

    # Wire app ref so health callbacks can access app.state for metrics
    _app_ref[0] = app

    # Store CLI model args for hot-reload (internal_model_reload reads these)
    app.state.cli_model_args = {
        "base_url": base_url,
        "api_key": api_key,
        "model": model,
        "context_window": context_window,
        "provider": provider_name,
        "_user_specified_model": bool(effective_model),
    }

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
        "Health tracking: failure_threshold=%s",
        config_store.get("health.failure_threshold"),
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

            from turnstone.core.tls import TLSClient

            hostname = socket.gethostname()
            fqdn = socket.getfqdn()
            hostnames = [hostname, "localhost", "127.0.0.1"]
            if fqdn != hostname:
                hostnames.append(fqdn)
            # Only add bind host if it's a concrete address
            if args.host not in ("0.0.0.0", "::", ""):
                hostnames.append(args.host)
            # Additional SANs from env (e.g. Docker service name)
            extra_sans = os.environ.get("TURNSTONE_TLS_SANS", "")
            if extra_sans:
                hostnames.extend(s.strip() for s in extra_sans.split(",") if s.strip())
            tls_client = TLSClient(
                storage=get_storage(),
                hostnames=hostnames,
            )
            asyncio.run(tls_client.init())
            bundle = tls_client.bundle
            if bundle:
                from lacme.mtls import write_pem_files_persistent

                pem_paths = write_pem_files_persistent(
                    bundle,
                    ca_pem=tls_client.ca_pem,
                )
                ssl_kwargs.update(pem_paths.as_uvicorn_kwargs())
                if tls_client.ca_pem:
                    import ssl as _ssl

                    ssl_kwargs["ssl_cert_reqs"] = _ssl.CERT_REQUIRED

                # Store client on app state for lifespan renewal
                app.state.tls_client = tls_client
                # Update advertise URL to HTTPS now that TLS is active
                if _advertise_url.startswith("http://"):
                    app.state.advertise_url = _advertise_url.replace("http://", "https://", 1)
                else:
                    app.state.advertise_url = _advertise_url
                log.info("TLS enabled — serving HTTPS")
            else:
                log.warning("TLS enabled but no cert available")
        except Exception as exc:
            log.warning(
                "TLS initialization failed — serving plain HTTP: %s: %s",
                type(exc).__name__,
                exc,
            )
            log.debug("TLS init traceback", exc_info=True)

    print("Press Ctrl+C to stop.")

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning", **ssl_kwargs)


if __name__ == "__main__":
    main()
