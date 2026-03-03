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
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sse_starlette import EventSourceResponse
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from turnstone import __version__
from turnstone.core.metrics import metrics as _metrics
from turnstone.core.session import ChatSession, SessionUI  # noqa: F401
from turnstone.core.tools import TOOLS  # noqa: F401 — available for introspection
from turnstone.core.workstream import Workstream, WorkstreamManager

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, MutableMapping

    from starlette.types import ASGIApp, Receive, Scope, Send

# ---------------------------------------------------------------------------
# Static assets — loaded once at startup from turnstone/ui/static/
# ---------------------------------------------------------------------------

_STATIC_DIR = Path(__file__).parent / "ui" / "static"
_HTML = (_STATIC_DIR / "index.html").read_text(encoding="utf-8")
_CSS = (_STATIC_DIR / "style.css").read_text(encoding="utf-8")
_JS = (_STATIC_DIR / "app.js").read_text(encoding="utf-8")


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
    """Build a history replay list from session messages.

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


class AuthMiddleware:
    """Check auth tokens on every HTTP request."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        # Skip auth for CORS preflight — CORSMiddleware handles it
        if request.method == "OPTIONS":
            await self.app(scope, receive, send)
            return
        from turnstone.core.auth import check_request

        auth_config = request.app.state.auth_config
        method = request.method
        path = request.url.path
        auth_header = request.headers.get("Authorization")
        cookie_header = request.headers.get("Cookie")
        allowed, status, msg = check_request(auth_config, method, path, auth_header, cookie_header)
        if not allowed:
            response = JSONResponse({"error": msg}, status_code=status)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


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
        client_ip = request.client.host if request.client else "unknown"
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _read_json(request: Request) -> dict[str, Any]:
    """Read JSON body from request, returning {} on invalid/missing JSON."""
    try:
        body: dict[str, Any] = await request.json()
        return body
    except (ValueError, json.JSONDecodeError):
        return {}


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
    """GET /api/events — per-workstream SSE event stream."""
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
    """GET /api/events/global — global SSE event stream."""
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
    """GET /api/workstreams — list all workstreams."""
    mgr: WorkstreamManager = request.app.state.workstreams
    result = []
    for ws in mgr.list_all():
        result.append(
            {
                "id": ws.id,
                "name": ws.name,
                "state": ws.state.value,
                "session_id": ws.session.session_id if ws.session else None,
            }
        )
    return JSONResponse({"workstreams": result})


async def dashboard(request: Request) -> JSONResponse:
    """GET /api/dashboard — enriched workstream data + aggregate stats."""
    from turnstone.core.memory import get_session_name

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
            title = get_session_name(ws.session.session_id) or ""
        ws_list.append(
            {
                "id": ws.id,
                "name": ws.name,
                "state": ws.state.value,
                "session_id": ws.session.session_id if ws.session else None,
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


async def list_sessions_endpoint(request: Request) -> JSONResponse:
    """GET /api/sessions — list saved sessions."""
    from turnstone.core.memory import list_sessions

    rows = list_sessions(limit=50)
    sessions = [
        {
            "session_id": sid,
            "alias": alias,
            "title": title,
            "created": created,
            "updated": updated,
            "message_count": count,
        }
        for sid, alias, title, created, updated, count in rows
    ]
    return JSONResponse({"sessions": sessions})


async def health(request: Request) -> JSONResponse:
    """GET /health — server health status."""
    mgr: WorkstreamManager = request.app.state.workstreams
    wss = mgr.list_all()
    states: dict[str, int] = {
        "idle": 0,
        "thinking": 0,
        "running": 0,
        "attention": 0,
        "error": 0,
    }
    for ws in wss:
        state = ws.state.value
        states[state] = states.get(state, 0) + 1
    monitor = getattr(request.app.state, "health_monitor", None)
    backend_ok = monitor.is_healthy if monitor else True
    data: dict[str, Any] = {
        "status": "ok" if backend_ok else "degraded",
        "version": __version__,
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
    states: dict[str, int] = {
        "idle": 0,
        "thinking": 0,
        "running": 0,
        "attention": 0,
        "error": 0,
    }
    ws_data = []
    for ws in wss:
        state = ws.state.value
        states[state] = states.get(state, 0) + 1
        ui: WebUI = ws.ui  # type: ignore[assignment]
        with ui._ws_lock:
            ws_data.append(
                {
                    "ws_id": ws.id,
                    "name": ws.name,
                    "session_id": ws.session.session_id if ws.session else "",
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
    """POST /api/send — send a user message to the workstream."""
    body = await _read_json(request)
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
    """POST /api/approve — approve or deny a tool call."""
    body = await _read_json(request)
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
    """POST /api/plan — respond to a plan review."""
    body = await _read_json(request)
    feedback = body.get("feedback", "")
    ws_id = body.get("ws_id")
    mgr = request.app.state.workstreams
    ws, ui = _get_ws(mgr, ws_id)
    if not ws or not ui:
        return JSONResponse({"error": "Unknown workstream"}, status_code=404)
    ui.resolve_plan(feedback)
    return JSONResponse({"status": "ok"})


async def command(request: Request) -> JSONResponse:
    """POST /api/command — execute a slash command."""
    body = await _read_json(request)
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
        # Handle UI updates for session-changing commands
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
            from turnstone.core.memory import get_session_name

            updated_name = get_session_name(ws.session.session_id)
            if updated_name:
                ws.name = updated_name
    except Exception as e:
        ui.on_error(f"Command error: {e}")

    return JSONResponse({"status": "ok"})


async def create_workstream(request: Request) -> JSONResponse:
    """POST /api/workstreams/new — create a new workstream."""
    body = await _read_json(request)
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
        return JSONResponse({"ws_id": ws.id, "name": ws.name})
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


async def close_workstream(request: Request) -> JSONResponse:
    """POST /api/workstreams/close — close a workstream."""
    body = await _read_json(request)
    ws_id = str(body.get("ws_id", ""))
    mgr = request.app.state.workstreams
    if mgr.close(ws_id):
        return JSONResponse({"status": "ok"})
    return JSONResponse({"error": "Cannot close last workstream"}, status_code=400)


async def auth_login(request: Request) -> Response:
    """POST /api/auth/login — authenticate with a token."""
    from turnstone.core.auth import make_set_cookie

    body = await _read_json(request)
    token = body.get("token", "")
    auth_config = request.app.state.auth_config
    role = auth_config.check(token)
    if role:
        response = JSONResponse({"status": "ok", "role": role})
        response.headers["Set-Cookie"] = make_set_cookie(token)
        return response
    return JSONResponse({"error": "Invalid token"}, status_code=401)


async def auth_logout(request: Request) -> Response:
    """POST /api/auth/logout — clear auth cookie."""
    from turnstone.core.auth import make_clear_cookie

    response = JSONResponse({"status": "ok"})
    response.headers["Set-Cookie"] = make_clear_cookie()
    return response


# ---------------------------------------------------------------------------
# Model auto-detection (shared with cli.py)
# ---------------------------------------------------------------------------


def detect_model(client: Any, provider: str = "openai") -> tuple[str, int | None]:
    """Auto-detect model — delegates to :func:`turnstone.core.model_registry.detect_model`."""
    from turnstone.core.model_registry import detect_model as _detect

    return _detect(client, provider=provider)


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


def create_app(
    *,
    workstreams: WorkstreamManager,
    global_queue: queue.Queue[dict[str, Any]],
    global_listeners: list[queue.Queue[dict[str, Any]]],
    global_listeners_lock: threading.Lock,
    skip_permissions: bool,
    auth_config: Any,
    health_monitor: Any = None,
    rate_limiter: Any = None,
    mcp_client: Any = None,
    registry: Any = None,
    idle_timeout: int = 0,
) -> Starlette:
    """Create and configure the Starlette ASGI application."""
    app = Starlette(
        routes=[
            Route("/", index),
            Route("/api/events", events_sse),
            Route("/api/events/global", global_events_sse),
            Route("/api/workstreams", list_workstreams),
            Route("/api/dashboard", dashboard),
            Route("/api/sessions", list_sessions_endpoint),
            Route("/health", health),
            Route("/metrics", metrics_endpoint),
            Route("/api/send", send_message, methods=["POST"]),
            Route("/api/approve", approve, methods=["POST"]),
            Route("/api/plan", plan_feedback, methods=["POST"]),
            Route("/api/command", command, methods=["POST"]),
            Route("/api/workstreams/new", create_workstream, methods=["POST"]),
            Route("/api/workstreams/close", close_workstream, methods=["POST"]),
            Route("/api/auth/login", auth_login, methods=["POST"]),
            Route("/api/auth/logout", auth_logout, methods=["POST"]),
            Mount("/static", app=StaticFiles(directory=str(_STATIC_DIR)), name="static"),
        ],
        middleware=[
            Middleware(MetricsMiddleware),
            Middleware(
                CORSMiddleware,
                allow_origins=["*"],
                allow_methods=["GET", "POST", "OPTIONS"],
                allow_headers=["Content-Type", "Authorization"],
            ),
            Middleware(AuthMiddleware),
            Middleware(RateLimitMiddleware),
        ],
        lifespan=_lifespan,
    )
    app.state.workstreams = workstreams
    app.state.global_queue = global_queue
    app.state.global_listeners = global_listeners
    app.state.global_listeners_lock = global_listeners_lock
    app.state.skip_permissions = skip_permissions
    app.state.auth_config = auth_config
    app.state.health_monitor = health_monitor
    app.state.rate_limiter = rate_limiter
    app.state.mcp_client = mcp_client
    app.state.registry = registry
    app.state.idle_timeout = idle_timeout
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
        "--resume",
        default=None,
        metavar="SESSION",
        help="Resume a previous session by alias or session_id",
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
        "--session-retention-days",
        type=int,
        default=90,
        metavar="DAYS",
        help="Delete unnamed sessions older than DAYS days on startup, 0 to disable (default: 90)",
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
    from turnstone.core.config import apply_config

    apply_config(
        parser,
        ["api", "model", "session", "tools", "server", "mcp", "ratelimit", "health"],
    )
    args = parser.parse_args()

    # Prune stale / empty sessions on startup
    from turnstone.core.memory import prune_sessions

    prune_sessions(retention_days=args.session_retention_days, log_fn=print)

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
        model, detected_ctx = detect_model(client, provider=provider_name)

    # Use detected context window when the user hasn't explicitly set one
    context_window = args.context_window
    if detected_ctx and context_window == 131072:  # default unchanged
        context_window = detected_ctx
        print(f"Context window: {context_window:,} (detected from backend)")

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

    mcp_client = create_mcp_client(getattr(args, "mcp_config", None))

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
    )

    # Set up global event queue for state-change broadcasts
    global_queue: queue.Queue[dict[str, Any]] = queue.Queue()
    global_listeners: list[queue.Queue[dict[str, Any]]] = []
    global_listeners_lock = threading.Lock()
    WebUI._global_queue = global_queue

    # Session factory — captures shared config
    def session_factory(ui: SessionUI | None, model_alias: str | None = None) -> ChatSession:
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
        )

    # Create workstream manager and initial workstream
    manager = WorkstreamManager(session_factory, max_workstreams=args.max_workstreams)
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
        from turnstone.core.memory import resolve_session

        target_id = resolve_session(args.resume)
        if not target_id:
            print(f"Session not found: {args.resume}")
            sys.exit(1)
        if not ws.session.resume_session(target_id):
            print(f"Session '{args.resume}' has no messages.")
            sys.exit(1)
        print(f"Resumed session {target_id} ({len(ws.session.messages)} messages)")

    # Record detected model in metrics
    _metrics.model = model

    # Auth config
    from turnstone.core.auth import load_auth_config

    auth_config = load_auth_config()
    if auth_config.enabled:
        print(f"Auth: enabled ({len(auth_config.tokens)} token(s) configured)")

    # Build the ASGI app
    app = create_app(
        workstreams=manager,
        global_queue=global_queue,
        global_listeners=global_listeners,
        global_listeners_lock=global_listeners_lock,
        skip_permissions=args.skip_permissions,
        auth_config=auth_config,
        health_monitor=health_monitor,
        rate_limiter=rate_limiter,
        mcp_client=mcp_client,
        registry=registry,
        idle_timeout=args.workstream_idle_timeout,
    )

    print(f"turnstone web server running on http://{args.host}:{args.port}")
    print(f"Model: {model}")
    if registry.count > 1:
        others = [a for a in registry.list_aliases() if a != registry.default]
        print(f"Models: {registry.default} (default), {', '.join(others)}")
    if mcp_client:
        mcp_tools = mcp_client.get_tools()
        if mcp_tools:
            print(f"MCP tools: {len(mcp_tools)} from {mcp_client.server_count} server(s)")
    print(
        f"Health monitor: probe every {args.health_probe_interval}s, "
        f"circuit breaker threshold={args.circuit_breaker_threshold}"
    )
    if rate_limiter.enabled:
        print(f"Rate limiter: {args.ratelimit_rps} req/s, burst={args.ratelimit_burst}")
    print(f"Max workstreams: {args.max_workstreams}")
    print("Press Ctrl+C to stop.")

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
