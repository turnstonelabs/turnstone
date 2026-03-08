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
import logging
import os
import queue
import socket
import sys
import textwrap
import threading
import time
import uuid
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
from turnstone.core.metrics import metrics as _metrics
from turnstone.core.ratelimit import resolve_client_ip
from turnstone.core.session import ChatSession, SessionUI  # noqa: F401
from turnstone.core.tools import TOOLS  # noqa: F401 — available for introspection
from turnstone.core.workstream import Workstream, WorkstreamManager, WorkstreamState

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, MutableMapping

    from starlette.types import ASGIApp, Receive, Scope, Send

# ---------------------------------------------------------------------------
# Static assets — loaded once at startup from turnstone/ui/static/
# ---------------------------------------------------------------------------

log = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).parent / "ui" / "static"
_SHARED_DIR = Path(__file__).parent / "shared_static"
_HTML = (_STATIC_DIR / "index.html").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# WebUI — implements SessionUI for browser-based interaction
# ---------------------------------------------------------------------------


class WebUI:
    """Browser-based UI using SSE for streaming and HTTP POST for actions.

    Implements the SessionUI protocol from turnstone.core.session.
    Each workstream gets its own WebUI instance.
    """

    # Shared global event queue for state-change broadcasts across all
    # workstreams.  Set by main() before any WebUI instances are created.
    _global_queue: queue.Queue[dict[str, Any]] | None = None
    _workstream_mgr: WorkstreamManager | None = None

    def __init__(self, ws_id: str = "") -> None:
        self.ws_id = ws_id
        self._event_queue: queue.Queue[dict[str, Any]] = queue.Queue()
        self._sse_generation = 0  # incremented on each new SSE connection
        self._approval_event = threading.Event()
        self._approval_result: tuple[bool, str | None] = (False, None)
        self._pending_approval: dict[str, Any] | None = None  # re-sent on SSE reconnect
        self._plan_event = threading.Event()
        self._plan_result: str = ""
        self.auto_approve = False
        # Per-workstream metrics accumulators (written by worker thread, read by metrics handler)
        self._ws_lock = threading.Lock()
        self._ws_prompt_tokens: int = 0
        self._ws_completion_tokens: int = 0
        self._ws_messages: int = 0
        self._ws_tool_calls: dict[str, int] = {}
        self._ws_context_ratio: float = 0.0
        # Activity tracking for dashboard (current tool / thinking / approval)
        self._ws_current_activity: str = ""
        self._ws_activity_state: str = ""  # "tool" | "approval" | "thinking" | ""

    def _enqueue(self, data: dict[str, Any]) -> None:
        self._event_queue.put(data)

    def _broadcast_state(self, state: str) -> None:
        """Send a state-change event to the global SSE channel."""
        if WebUI._global_queue is not None:
            with self._ws_lock:
                tokens = self._ws_prompt_tokens + self._ws_completion_tokens
                ctx = self._ws_context_ratio
                activity = self._ws_current_activity
                activity_state = self._ws_activity_state
            WebUI._global_queue.put(
                {
                    "type": "ws_state",
                    "ws_id": self.ws_id,
                    "state": state,
                    "tokens": tokens,
                    "context_ratio": ctx,
                    "activity": activity,
                    "activity_state": activity_state,
                }
            )

    def _broadcast_activity(self) -> None:
        """Send an activity-change event to the global SSE channel."""
        if WebUI._global_queue is not None:
            with self._ws_lock:
                activity = self._ws_current_activity
                activity_state = self._ws_activity_state
            WebUI._global_queue.put(
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
        self._enqueue({"type": "content", "text": text})

    def on_stream_end(self) -> None:
        with self._ws_lock:
            self._ws_current_activity = ""
            self._ws_activity_state = ""
        self._broadcast_activity()
        self._enqueue({"type": "stream_end"})

    def approve_tools(self, items: list[dict[str, Any]]) -> tuple[bool, str | None]:
        pending = [it for it in items if it.get("needs_approval") and not it.get("error")]

        # Always send tool info to the browser
        serialized = []
        for item in items:
            serialized.append(
                {
                    "call_id": item.get("call_id", ""),
                    "header": item.get("header", ""),
                    "preview": item.get("preview", ""),
                    "func_name": item.get("func_name", ""),
                    "approval_label": item.get("approval_label", item.get("func_name", "")),
                    "needs_approval": item.get("needs_approval", False),
                    "error": item.get("error"),
                }
            )

        if not pending or self.auto_approve:
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

        # Send approval request and block
        self._approval_event.clear()
        self._pending_approval = {"type": "approve_request", "items": serialized}
        self._enqueue(self._pending_approval)
        self._approval_event.wait()
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
        _metrics.record_tokens(usage["prompt_tokens"], usage["completion_tokens"])
        _metrics.record_context_ratio(total_tok / context_window if context_window > 0 else 0.0)
        with self._ws_lock:
            self._ws_prompt_tokens += usage["prompt_tokens"]
            self._ws_completion_tokens += usage["completion_tokens"]
            self._ws_context_ratio = total_tok / context_window if context_window > 0 else 0.0
        self._enqueue(
            {
                "type": "status",
                "prompt_tokens": usage["prompt_tokens"],
                "completion_tokens": usage["completion_tokens"],
                "total_tokens": total_tok,
                "context_window": context_window,
                "pct": round(pct, 1),
                "effort": effort,
            }
        )

    def on_plan_review(self, content: str) -> str:
        self._plan_event.clear()
        self._enqueue({"type": "plan_review", "content": content})
        self._plan_event.wait()
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
            WebUI._global_queue.put({"type": "ws_rename", "ws_id": self.ws_id, "name": name})

    def resolve_approval(self, approved: bool, feedback: str | None = None) -> None:
        """Called by the HTTP handler when the user approves/denies."""
        self._approval_result = (approved, feedback)
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
        history.append(entry)
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

    ui._sse_generation += 1
    my_gen = ui._sse_generation
    # Drain stale events.  A race with the worker thread is acceptable:
    # worst case we discard one fresh event, and the client catches up
    # via the history replay above.
    while not ui._event_queue.empty():
        try:
            ui._event_queue.get_nowait()
        except queue.Empty:
            break

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
            while my_gen == ui._sse_generation:
                try:
                    event = await loop.run_in_executor(
                        None, functools.partial(ui._event_queue.get, timeout=5)
                    )
                    yield {"data": json.dumps(event)}
                except queue.Empty:
                    pass
                if await request.is_disconnected():
                    break
        finally:
            _metrics.record_sse_disconnect()

    return EventSourceResponse(event_generator(), ping=5)


async def global_events_sse(request: Request) -> Response:
    """GET /v1/api/events/global — global SSE event stream."""
    client_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=500)
    listeners = request.app.state.global_listeners
    listeners_lock = request.app.state.global_listeners_lock
    with listeners_lock:
        listeners.append(client_queue)

    async def event_generator() -> AsyncGenerator[dict[str, str], None]:
        _metrics.record_sse_connect()
        try:
            loop = asyncio.get_running_loop()
            while True:
                try:
                    event = await loop.run_in_executor(
                        None, functools.partial(client_queue.get, timeout=5)
                    )
                    yield {"data": json.dumps(event)}
                except queue.Empty:
                    pass
                if await request.is_disconnected():
                    break
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
        "workstreams": {"total": len(wss), **states},
        "backend": {
            "status": "up" if backend_ok else "down",
            "circuit_state": monitor.circuit_state.value if monitor else "closed",
        },
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
    content = _metrics.generate_text(
        workstream_states=states,
        total_workstreams=len(wss),
        workstream_metrics=ws_data,
    )
    return Response(content, media_type="text/plain; version=0.0.4; charset=utf-8")


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
    if always and approved:
        ui.auto_approve = True
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
    try:
        ws = mgr.create(
            name=body.get("name", ""),
            ui_factory=lambda wid: WebUI(ws_id=wid),
            model=body.get("model") or None,
        )
        assert isinstance(ws.ui, WebUI)
        if skip or body.get("auto_approve", False):
            ws.ui.auto_approve = True
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
        resume_ws_id = body.get("resume_ws", "")
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
            pass


# ---------------------------------------------------------------------------
# Lifespan context manager
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _lifespan(app: Starlette) -> AsyncGenerator[None, None]:
    """Start background threads and handle shutdown."""
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
    yield
    # Shutdown
    if app.state.health_monitor:
        app.state.health_monitor.stop()
    if app.state.mcp_client:
        app.state.mcp_client.shutdown()
    if app.state.registry:
        app.state.registry.shutdown()


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
                    Route("/api/send", send_message, methods=["POST"]),
                    Route("/api/approve", approve, methods=["POST"]),
                    Route("/api/plan", plan_feedback, methods=["POST"]),
                    Route("/api/command", command, methods=["POST"]),
                    Route("/api/workstreams/new", create_workstream, methods=["POST"]),
                    Route("/api/workstreams/close", close_workstream, methods=["POST"]),
                    Route("/api/auth/login", auth_login, methods=["POST"]),
                    Route("/api/auth/logout", auth_logout, methods=["POST"]),
                    Route("/api/auth/status", auth_status),
                    Route("/api/auth/setup", auth_setup, methods=["POST"]),
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

    from turnstone.core.auth import LoginRateLimiter

    app.state.login_limiter = LoginRateLimiter()
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
        "--instructions",
        default=None,
        help="Developer instructions injected as developer message",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=0.5,
        help="Sampling temperature (default: 0.5)",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=32768,
        help="Max completion tokens (default: 32768)",
    )
    parser.add_argument(
        "--tool-timeout",
        type=int,
        default=30,
        help="Bash command timeout in seconds (default: 30)",
    )
    parser.add_argument(
        "--reasoning-effort",
        default="medium",
        choices=["none", "minimal", "low", "medium", "high", "xhigh", "max"],
        help="Reasoning effort level (default: medium)",
    )
    parser.add_argument(
        "--provider",
        default="openai",
        choices=["openai", "anthropic"],
        help="LLM provider for the default model (default: openai)",
    )
    parser.add_argument(
        "--context-window",
        type=int,
        default=131072,
        help="Context window size in tokens (default: 131072)",
    )
    parser.add_argument(
        "--compact-max-tokens",
        type=int,
        default=32768,
        help="Max tokens for compaction summary (default: 32768)",
    )
    parser.add_argument(
        "--auto-compact-pct",
        type=float,
        default=0.8,
        help="Auto-compact when prompt exceeds this fraction of context window (default: 0.8)",
    )
    parser.add_argument(
        "--agent-max-turns",
        type=int,
        default=-1,
        help="Max tool turns for agent sub-sessions, -1 for unlimited (default: -1)",
    )
    parser.add_argument(
        "--tool-truncation",
        type=int,
        default=0,
        help="Tool output truncation limit in chars, 0 for auto (50%% of context window) (default: 0)",
    )
    parser.add_argument(
        "--tool-search",
        choices=["auto", "on", "off"],
        default="auto",
        help="Dynamic tool search: auto (enable when tool count exceeds threshold), on, off (default: auto)",
    )
    parser.add_argument(
        "--tool-search-threshold",
        type=int,
        default=20,
        help="Min tools before tool search activates (default: 20)",
    )
    parser.add_argument(
        "--tool-search-max-results",
        type=int,
        default=5,
        help="Max tools returned per tool search query (default: 5)",
    )
    parser.add_argument(
        "--resume",
        default=None,
        metavar="WS",
        help="Resume a previous workstream by alias or ws_id",
    )
    parser.add_argument(
        "--skip-permissions",
        action="store_true",
        help="Auto-approve all tool calls (no confirmation prompts)",
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
    parser.add_argument(
        "--retention-days",
        type=int,
        default=90,
        metavar="DAYS",
        help="Delete unnamed workstreams older than DAYS days on startup, 0 to disable (default: 90)",
    )
    parser.add_argument(
        "--workstream-idle-timeout",
        type=int,
        default=120,
        metavar="MINUTES",
        help="Close IDLE workstreams after MINUTES of inactivity, 0 to disable (default: 120)",
    )
    parser.add_argument(
        "--mcp-config",
        default=None,
        metavar="PATH",
        help="Path to MCP server config file (standard mcpServers JSON format)",
    )

    from turnstone.core.config import nonneg_float

    parser.add_argument(
        "--mcp-refresh-interval",
        type=nonneg_float,
        default=14400,
        metavar="SECONDS",
        help="Periodic MCP tool refresh interval for servers without push notifications (default: 14400 = 4h, 0 to disable)",
    )
    parser.add_argument(
        "--max-workstreams",
        type=int,
        default=10,
        help="Maximum concurrent workstreams, auto-evicts idle when full (default: 10)",
    )
    parser.add_argument(
        "--ratelimit-enabled",
        action="store_true",
        default=False,
        help="Enable per-IP rate limiting",
    )
    parser.add_argument(
        "--ratelimit-rps",
        type=float,
        default=10.0,
        help="Rate limit: requests per second per client IP (default: 10.0)",
    )
    parser.add_argument(
        "--ratelimit-burst",
        type=int,
        default=20,
        help="Rate limit: burst size (default: 20)",
    )
    parser.add_argument(
        "--ratelimit-trusted-proxies",
        default="",
        help="Trusted proxy CIDRs for X-Forwarded-For parsing (comma-separated, e.g. '10.0.0.0/8,172.16.0.0/12')",
    )
    parser.add_argument(
        "--health-probe-interval",
        type=float,
        default=30.0,
        help="Backend health probe interval in seconds (default: 30)",
    )
    parser.add_argument(
        "--health-probe-timeout",
        type=float,
        default=5.0,
        help="Backend health probe timeout in seconds (default: 5)",
    )
    parser.add_argument(
        "--circuit-breaker-threshold",
        type=int,
        default=5,
        help="Consecutive failures to open circuit breaker (default: 5)",
    )
    parser.add_argument(
        "--circuit-breaker-cooldown",
        type=float,
        default=60.0,
        help="Circuit breaker cooldown in seconds (default: 60)",
    )
    from turnstone.core.log import add_log_args

    add_log_args(parser)
    from turnstone.core.config import apply_config

    apply_config(
        parser,
        ["api", "model", "session", "tools", "server", "mcp", "ratelimit", "health", "database"],
    )
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
        getattr(args, "db_pool_size", None) or os.environ.get("TURNSTONE_DB_POOL_SIZE", "5")
    )
    init_storage(db_backend, path=db_path, url=db_url, pool_size=db_pool_size)

    # Prune stale / empty workstreams on startup
    from turnstone.core.memory import prune_workstreams

    prune_workstreams(retention_days=args.retention_days, log_fn=print)

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
    if args.model:
        model = args.model
        detected_ctx = None
    else:
        from turnstone.core.model_registry import detect_model

        model, detected_ctx = detect_model(client, provider=provider_name)

    # Use detected context window when the user hasn't explicitly set one
    context_window = args.context_window
    if detected_ctx and context_window == 131072:  # default unchanged
        context_window = detected_ctx
        log.info("Context window: %s (detected from backend)", f"{context_window:,}")

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

    mcp_client = create_mcp_client(
        getattr(args, "mcp_config", None),
        refresh_interval=getattr(args, "mcp_refresh_interval", 14400),
    )

    # Backend health monitor with circuit breaker
    from turnstone.core.healthcheck import BackendHealthMonitor

    health_monitor = BackendHealthMonitor(
        client=client,
        probe_interval=args.health_probe_interval,
        probe_timeout=args.health_probe_timeout,
        failure_threshold=args.circuit_breaker_threshold,
        cooldown=args.circuit_breaker_cooldown,
    )
    health_monitor.start()

    # Per-IP rate limiter
    from turnstone.core.ratelimit import RateLimiter

    rate_limiter = RateLimiter(
        enabled=args.ratelimit_enabled,
        rate=args.ratelimit_rps,
        burst=args.ratelimit_burst,
        trusted_proxies=args.ratelimit_trusted_proxies,
    )

    # Set up global event queue for state-change broadcasts
    global_queue: queue.Queue[dict[str, Any]] = queue.Queue()
    global_listeners: list[queue.Queue[dict[str, Any]]] = []
    global_listeners_lock = threading.Lock()
    WebUI._global_queue = global_queue

    # Server-owned node identity
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

    # Session factory — captures shared config
    def session_factory(
        ui: SessionUI | None,
        model_alias: str | None = None,
        ws_id: str | None = None,
    ) -> ChatSession:
        assert ui is not None
        r_client, r_model, r_cfg = registry.resolve(model_alias)
        return ChatSession(
            client=r_client,
            model=r_model,
            ui=ui,
            instructions=args.instructions,
            temperature=args.temperature,
            max_tokens=args.max_tokens,
            tool_timeout=args.tool_timeout,
            reasoning_effort=args.reasoning_effort,
            context_window=r_cfg.context_window,
            compact_max_tokens=args.compact_max_tokens,
            auto_compact_pct=args.auto_compact_pct,
            agent_max_turns=args.agent_max_turns,
            tool_truncation=args.tool_truncation,
            mcp_client=mcp_client,
            registry=registry,
            model_alias=model_alias or registry.default,
            health_monitor=health_monitor,
            node_id=_node_id,
            ws_id=ws_id,
            tool_search=args.tool_search,
            tool_search_threshold=args.tool_search_threshold,
            tool_search_max_results=args.tool_search_max_results,
        )

    # Create workstream manager and initial workstream
    manager = WorkstreamManager(
        session_factory, max_workstreams=args.max_workstreams, node_id=_node_id
    )
    WebUI._workstream_mgr = manager
    ws = manager.create(
        name="default",
        ui_factory=lambda wid: WebUI(ws_id=wid),
    )
    assert isinstance(ws.ui, WebUI)
    if args.skip_permissions:
        ws.ui.auto_approve = True

    # Handle --resume
    assert ws.session is not None
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

    # Record detected model in metrics
    _metrics.model = model

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

    app = create_app(
        workstreams=manager,
        global_queue=global_queue,
        global_listeners=global_listeners,
        global_listeners_lock=global_listeners_lock,
        skip_permissions=args.skip_permissions,
        auth_config=auth_config,
        jwt_secret=jwt_secret,
        auth_storage=get_storage(),
        health_monitor=health_monitor,
        rate_limiter=rate_limiter,
        mcp_client=mcp_client,
        registry=registry,
        idle_timeout=args.workstream_idle_timeout,
        node_id=_node_id,
        cors_origins=cors_origins,
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
    log.info(
        "Health monitor: probe every %ss, circuit breaker threshold=%s",
        args.health_probe_interval,
        args.circuit_breaker_threshold,
    )
    if rate_limiter.enabled:
        proxy_info = (
            f", trusted proxies: {args.ratelimit_trusted_proxies}"
            if args.ratelimit_trusted_proxies
            else ""
        )
        log.info(
            "Rate limiter: %s req/s, burst=%s%s",
            args.ratelimit_rps,
            args.ratelimit_burst,
            proxy_info,
        )
    log.info("Max workstreams: %s", args.max_workstreams)
    log.info("Node ID: %s", _node_id)
    print("Press Ctrl+C to stop.")

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
