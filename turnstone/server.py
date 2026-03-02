"""Web server frontend for turnstone.

Provides a browser-based chat UI that mirrors the terminal CLI experience.
Uses only Python stdlib (http.server, json, threading, queue) for the server,
communicating with the browser via Server-Sent Events (SSE) for streaming
and HTTP POST for user actions.

Supports multiple concurrent workstreams (tabs), each with independent
ChatSession and event streams.
"""

import argparse
import json
import os
import queue
import sys
import textwrap
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from socketserver import ThreadingMixIn
from urllib.parse import urlparse, parse_qs

from openai import OpenAI

from turnstone.core.metrics import metrics as _metrics
from turnstone.core.session import ChatSession, SessionUI  # noqa: F401
from turnstone.core.tools import TOOLS  # noqa: F401 — available for introspection
from turnstone.core.workstream import WorkstreamManager, WorkstreamState

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
    _global_queue: queue.Queue | None = None

    def __init__(self, ws_id: str = ""):
        self.ws_id = ws_id
        self._event_queue: queue.Queue = queue.Queue()
        self._sse_generation = 0  # incremented on each new SSE connection
        self._approval_event = threading.Event()
        self._approval_result: tuple[bool, str | None] = (False, None)
        self._pending_approval: dict | None = None  # re-sent on SSE reconnect
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

    def _enqueue(self, data: dict):
        self._event_queue.put(data)

    def _broadcast_state(self, state: str):
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

    def _broadcast_activity(self):
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

    def on_thinking_start(self):
        with self._ws_lock:
            self._ws_current_activity = "Thinking\u2026"
            self._ws_activity_state = "thinking"
        self._broadcast_activity()
        self._enqueue({"type": "thinking_start"})

    def on_thinking_stop(self):
        self._enqueue({"type": "thinking_stop"})

    def on_reasoning_token(self, text: str):
        self._enqueue({"type": "reasoning", "text": text})

    def on_content_token(self, text: str):
        self._enqueue({"type": "content", "text": text})

    def on_stream_end(self):
        with self._ws_lock:
            self._ws_current_activity = ""
            self._ws_activity_state = ""
        self._broadcast_activity()
        self._enqueue({"type": "stream_end"})

    def approve_tools(self, items: list[dict]) -> tuple[bool, str | None]:
        pending = [
            it for it in items if it.get("needs_approval") and not it.get("error")
        ]

        # Always send tool info to the browser
        serialized = []
        for item in items:
            serialized.append(
                {
                    "header": item.get("header", ""),
                    "preview": item.get("preview", ""),
                    "func_name": item.get("func_name", ""),
                    "approval_label": item.get(
                        "approval_label", item.get("func_name", "")
                    ),
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
                self._ws_current_activity = (
                    f"\u2699 {label}: {preview}" if label else ""
                )
                self._ws_activity_state = "tool" if label else ""
            self._broadcast_activity()
            self._enqueue({"type": "tool_info", "items": serialized})
            return True, None

        # Track pending approval activity
        first_pending = pending[0]
        label = first_pending.get("func_name", "")
        preview = first_pending.get("preview", "")[:60]
        with self._ws_lock:
            self._ws_current_activity = (
                f"\u23f3 Awaiting approval: {label} \u2014 {preview}"
            )
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

    def on_tool_result(self, name: str, output: str):
        _metrics.record_tool_call(name)
        with self._ws_lock:
            self._ws_tool_calls[name] = self._ws_tool_calls.get(name, 0) + 1
            self._ws_current_activity = ""
            self._ws_activity_state = ""
        self._broadcast_activity()
        self._enqueue({"type": "tool_result", "name": name, "output": output})

    def on_status(self, usage: dict, context_window: int, effort: str):
        total_tok = usage["prompt_tokens"] + usage["completion_tokens"]
        pct = total_tok / context_window * 100 if context_window > 0 else 0
        _metrics.record_tokens(usage["prompt_tokens"], usage["completion_tokens"])
        _metrics.record_context_ratio(
            total_tok / context_window if context_window > 0 else 0.0
        )
        with self._ws_lock:
            self._ws_prompt_tokens += usage["prompt_tokens"]
            self._ws_completion_tokens += usage["completion_tokens"]
            self._ws_context_ratio = (
                total_tok / context_window if context_window > 0 else 0.0
            )
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

    def on_info(self, message: str):
        self._enqueue({"type": "info", "message": message})

    def on_error(self, message: str):
        _metrics.record_error()
        self._enqueue({"type": "error", "message": message})

    def on_state_change(self, state: str):
        self._broadcast_state(state)

    def on_rename(self, name: str):
        """Update the workstream's display name and broadcast to all clients."""
        if WebUI._global_queue is not None:
            WebUI._global_queue.put(
                {"type": "ws_rename", "ws_id": self.ws_id, "name": name}
            )

    def resolve_approval(self, approved: bool, feedback: str | None = None):
        """Called by the HTTP handler when the user approves/denies."""
        self._approval_result = (approved, feedback)
        self._approval_event.set()

    def resolve_plan(self, feedback: str):
        """Called by the HTTP handler when the user responds to a plan."""
        self._plan_result = feedback
        self._plan_event.set()


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------


def _build_history(session, has_pending_approval: bool = False) -> list[dict]:
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


class TurnstoneHTTPHandler(BaseHTTPRequestHandler):
    """HTTP handler for the turnstone web server.

    Serves the embedded HTML client and provides API endpoints for
    SSE streaming, message sending, tool approval, and workstream management.
    """

    # Suppress default logging to stderr
    def log_message(self, format, *args):
        pass

    def _set_headers(self, status=200, content_type="application/json"):
        self._response_status = status
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            return {}

    def _send_json(self, data: dict, status=200):
        self._set_headers(status, "application/json")
        self.wfile.write(json.dumps(data).encode("utf-8"))

    def _get_ws(self, ws_id: str | None):
        """Look up workstream by id.  Returns (Workstream, WebUI) or (None, None)."""
        if not ws_id:
            return None, None
        mgr: WorkstreamManager = self.server.workstreams  # type: ignore[attr-defined]
        ws = mgr.get(ws_id)
        if ws:
            return ws, ws.ui
        return None, None

    def _check_auth(self, method: str, path: str) -> bool:
        """Return True if authorized.  Sends 401/403 and returns False otherwise."""
        from turnstone.core.auth import check_request

        auth_config = self.server.auth_config  # type: ignore[attr-defined]
        auth_header = self.headers.get("Authorization")
        cookie_header = self.headers.get("Cookie")
        allowed, status, msg = check_request(
            auth_config, method, path, auth_header, cookie_header
        )
        if not allowed:
            self._send_json({"error": msg}, status)
        return allowed

    def do_GET(self):
        _t0 = time.monotonic()
        self._response_status = 200
        parsed = urlparse(self.path)
        try:
            if not self._check_auth("GET", parsed.path):
                return
            self._do_GET(parsed)
        finally:
            _metrics.record_request(
                "GET", parsed.path, self._response_status, time.monotonic() - _t0
            )

    def _do_GET(self, parsed):
        if parsed.path == "/":
            self._set_headers(200, "text/html; charset=utf-8")
            self.wfile.write(_HTML.encode("utf-8"))

        elif parsed.path == "/static/style.css":
            self.send_response(200)
            self.send_header("Content-Type", "text/css; charset=utf-8")
            self.send_header("Cache-Control", "max-age=3600")
            self.end_headers()
            self.wfile.write(_CSS.encode("utf-8"))

        elif parsed.path == "/static/app.js":
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript; charset=utf-8")
            self.send_header("Cache-Control", "max-age=3600")
            self.end_headers()
            self.wfile.write(_JS.encode("utf-8"))

        elif parsed.path == "/api/events":
            qs = parse_qs(parsed.query)
            ws_id = qs.get("ws_id", [None])[0]
            ws, ui = self._get_ws(ws_id)
            if not ws or not ui:
                self._send_json({"error": "Unknown workstream"}, 404)
                return

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()

            # Bump generation so any previous SSE handler exits
            ui._sse_generation += 1
            my_gen = ui._sse_generation

            # Drain stale events from the queue
            while not ui._event_queue.empty():
                try:
                    ui._event_queue.get_nowait()
                except queue.Empty:
                    break

            # Send connected event with model info
            session: ChatSession = ws.session
            connected_data = json.dumps(
                {
                    "type": "connected",
                    "model": session.model,
                    "skip_permissions": ui.auto_approve,
                }
            )
            self.wfile.write(f"data: {connected_data}\n\n".encode("utf-8"))
            self.wfile.flush()

            # Send conversation history for replay
            history = _build_history(
                session, has_pending_approval=ui._pending_approval is not None
            )
            if history:
                history_data = json.dumps({"type": "history", "messages": history})
                self.wfile.write(f"data: {history_data}\n\n".encode("utf-8"))
                self.wfile.flush()

            # Re-inject a pending approval request if one was interrupted by a tab switch.
            if ui._pending_approval is not None:
                pa_data = json.dumps(ui._pending_approval)
                self.wfile.write(f"data: {pa_data}\n\n".encode("utf-8"))
                self.wfile.flush()

            # Long-running SSE loop
            try:
                while my_gen == ui._sse_generation:
                    try:
                        event = ui._event_queue.get(timeout=5)
                        data = json.dumps(event)
                        self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                        self.wfile.flush()
                    except queue.Empty:
                        # Send keepalive comment to prevent timeout
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass  # Client disconnected

        elif parsed.path == "/api/events/global":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()

            gq: queue.Queue = self.server.global_queue  # type: ignore[attr-defined]

            # Each global SSE client gets its own consumer queue
            # (since queue.Queue is single-consumer, we fan out via a listener list)
            client_queue: queue.Queue = queue.Queue(maxsize=500)
            listeners: list = self.server.global_listeners  # type: ignore[attr-defined]
            listeners_lock: threading.Lock = self.server.global_listeners_lock  # type: ignore[attr-defined]
            with listeners_lock:
                listeners.append(client_queue)

            try:
                while True:
                    try:
                        event = client_queue.get(timeout=5)
                        data = json.dumps(event)
                        self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                        self.wfile.flush()
                    except queue.Empty:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                with listeners_lock:
                    if client_queue in listeners:
                        listeners.remove(client_queue)

        elif parsed.path == "/api/workstreams":
            mgr: WorkstreamManager = self.server.workstreams  # type: ignore[attr-defined]
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
            self._send_json({"workstreams": result})

        elif parsed.path == "/api/dashboard":
            self._handle_dashboard()

        elif parsed.path == "/api/sessions":
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
            self._send_json({"sessions": sessions})

        elif parsed.path == "/health":
            self._handle_health()

        elif parsed.path == "/metrics":
            self._handle_metrics()

        else:
            self._set_headers(404, "text/plain")
            self.wfile.write(b"Not found")

    def do_POST(self):
        _t0 = time.monotonic()
        self._response_status = 200
        try:
            if not self._check_auth("POST", self.path):
                return
            self._do_POST()
        finally:
            _metrics.record_request(
                "POST", self.path, self._response_status, time.monotonic() - _t0
            )

    def _do_POST(self):
        if self.path == "/api/send":
            body = self._read_body()
            message = body.get("message", "").strip()
            ws_id = body.get("ws_id")
            if not message:
                self._send_json({"error": "Empty message"}, 400)
                return

            ws, ui = self._get_ws(ws_id)
            if not ws or not ui:
                self._send_json({"error": "Unknown workstream"}, 404)
                return

            # Check if already processing
            if ws.worker_thread and ws.worker_thread.is_alive():
                ui._enqueue(
                    {
                        "type": "busy_error",
                        "message": "Already processing a request. Please wait.",
                    }
                )
                self._send_json({"status": "busy"})
                return

            def run():
                try:
                    ws.session.send(message)
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
            self._send_json({"status": "ok"})

        elif self.path == "/api/approve":
            body = self._read_body()
            approved = body.get("approved", False)
            feedback = body.get("feedback")
            always = body.get("always", False)
            ws_id = body.get("ws_id")

            ws, ui = self._get_ws(ws_id)
            if not ws or not ui:
                self._send_json({"error": "Unknown workstream"}, 404)
                return
            if always and approved:
                ui.auto_approve = True
            ui.resolve_approval(approved, feedback)
            self._send_json({"status": "ok"})

        elif self.path == "/api/plan":
            body = self._read_body()
            feedback = body.get("feedback", "")
            ws_id = body.get("ws_id")

            ws, ui = self._get_ws(ws_id)
            if not ws or not ui:
                self._send_json({"error": "Unknown workstream"}, 404)
                return
            ui.resolve_plan(feedback)
            self._send_json({"status": "ok"})

        elif self.path == "/api/command":
            body = self._read_body()
            command = body.get("command", "").strip()
            ws_id = body.get("ws_id")
            if not command:
                self._send_json({"error": "Empty command"}, 400)
                return

            ws, ui = self._get_ws(ws_id)
            if not ws or not ui:
                self._send_json({"error": "Unknown workstream"}, 404)
                return

            try:
                should_exit = ws.session.handle_command(command)
                if should_exit:
                    ui.on_info("Session ended. You can close this tab.")
                # Handle UI updates for session-changing commands
                cmd_word = command.strip().split(None, 1)[0].lower()
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

            self._send_json({"status": "ok"})

        elif self.path == "/api/workstreams/new":
            body = self._read_body()
            mgr: WorkstreamManager = self.server.workstreams  # type: ignore[attr-defined]
            skip: bool = self.server.skip_permissions  # type: ignore[attr-defined]
            try:
                ws = mgr.create(
                    name=body.get("name", ""),
                    ui_factory=lambda wid: WebUI(ws_id=wid),
                )
                if skip or body.get("auto_approve", False):
                    ws.ui.auto_approve = True
                self._send_json({"ws_id": ws.id, "name": ws.name})
            except RuntimeError as e:
                self._send_json({"error": str(e)}, 400)

        elif self.path == "/api/workstreams/close":
            body = self._read_body()
            ws_id = body.get("ws_id")
            mgr: WorkstreamManager = self.server.workstreams  # type: ignore[attr-defined]
            if mgr.close(ws_id):
                self._send_json({"status": "ok"})
            else:
                self._send_json({"error": "Cannot close last workstream"}, 400)

        elif self.path == "/api/auth/login":
            from turnstone.core.auth import make_set_cookie

            body = self._read_body()
            token = body.get("token", "")
            auth_config = self.server.auth_config  # type: ignore[attr-defined]
            role = auth_config.check(token)
            if role:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Set-Cookie", make_set_cookie(token))
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(
                    json.dumps({"status": "ok", "role": role}).encode("utf-8")
                )
            else:
                self._send_json({"error": "Invalid token"}, 401)

        elif self.path == "/api/auth/logout":
            from turnstone.core.auth import make_clear_cookie

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Set-Cookie", make_clear_cookie())
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')

        else:
            self._set_headers(404, "text/plain")
            self.wfile.write(b"Not found")

    def _handle_health(self):
        """Return server health status as JSON."""
        mgr: WorkstreamManager = self.server.workstreams  # type: ignore[attr-defined]
        wss = mgr.list_all()
        states: dict = {
            "idle": 0,
            "thinking": 0,
            "running": 0,
            "attention": 0,
            "error": 0,
        }
        for ws in wss:
            state = ws.state.value
            states[state] = states.get(state, 0) + 1
        data = {
            "status": "ok",
            "version": "0.2.0",
            "uptime_seconds": round(time.monotonic() - _metrics.start_time, 2),
            "model": _metrics.model,
            "workstreams": {"total": len(wss), **states},
        }
        self._send_json(data)

    def _handle_dashboard(self):
        """Return enriched workstream data + aggregate stats for the dashboard."""
        from turnstone.core.memory import get_session_name

        mgr: WorkstreamManager = self.server.workstreams  # type: ignore[attr-defined]
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
                }
            )
        uptime_sec = round(time.monotonic() - _metrics.start_time)
        self._send_json(
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

    def _handle_metrics(self):
        """Return Prometheus text exposition format metrics."""
        mgr: WorkstreamManager = self.server.workstreams  # type: ignore[attr-defined]
        wss = mgr.list_all()
        states: dict = {
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
        self._set_headers(200, "text/plain; version=0.0.4; charset=utf-8")
        self.wfile.write(content.encode("utf-8"))

    def do_OPTIONS(self):
        """Handle CORS preflight."""
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()


# ---------------------------------------------------------------------------
# Threaded HTTP server (module-level so tests can import it)
# ---------------------------------------------------------------------------


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """HTTP server that handles each request in a separate thread.

    Required for SSE (long-polling GET) and concurrent POST handlers to work
    simultaneously.
    """

    daemon_threads = True


# ---------------------------------------------------------------------------
# Model auto-detection (shared with cli.py)
# ---------------------------------------------------------------------------


def detect_model(client: OpenAI) -> str:
    """Auto-detect the model from vLLM's /v1/models endpoint."""
    try:
        models = client.models.list()
        model_ids = [m.id for m in models.data]
        if not model_ids:
            print("Error: No models found at server. Use --model to specify.")
            sys.exit(1)
        if len(model_ids) == 1:
            return model_ids[0]
        print(f"Available models: {', '.join(model_ids)}")
        print(f"Using: {model_ids[0]} (override with --model)")
        return model_ids[0]
    except Exception as e:
        print(f"Error: Could not connect to server: {e}")
        print("Is vLLM running? Start it or use --base-url to point elsewhere.")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Global SSE fan-out
# ---------------------------------------------------------------------------


def _idle_cleanup_thread(
    mgr: WorkstreamManager, timeout_sec: float, global_queue: queue.Queue
):
    """Periodically close IDLE workstreams that have been inactive too long."""
    check_every = min(300.0, timeout_sec / 4)  # check at ¼ of timeout, max 5 min
    while True:
        time.sleep(check_every)
        closed = mgr.close_idle(timeout_sec)
        for ws_id in closed:
            try:
                global_queue.put_nowait({"type": "ws_closed", "ws_id": ws_id})
            except queue.Full:
                pass


def _global_fanout_thread(
    source_queue: queue.Queue, listeners: list, lock: threading.Lock
):
    """Reads events from the source queue and copies them to all listener queues."""
    while True:
        try:
            event = source_queue.get()
            with lock:
                snapshot = list(listeners)
            for lq in snapshot:
                try:
                    lq.put_nowait(event)
                except queue.Full:
                    pass  # drop if a listener is backed up
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def main():
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
        help="vLLM API base URL (default: http://localhost:8000/v1)",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Model name (default: auto-detect from server)",
    )
    parser.add_argument(
        "--persona",
        default=None,
        help="Persona name injected as system message",
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
        choices=["low", "medium", "high"],
        help="Reasoning effort level (default: medium)",
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
    from turnstone.core.config import apply_config

    apply_config(parser, ["api", "model", "session", "tools", "server"])
    args = parser.parse_args()

    # Prune stale / empty sessions on startup
    from turnstone.core.memory import prune_sessions

    prune_sessions(retention_days=args.session_retention_days, log_fn=print)

    # Create OpenAI client
    api_key = args.api_key or os.environ.get("OPENAI_API_KEY") or "dummy"
    client = OpenAI(
        base_url=args.base_url,
        api_key=api_key,
    )

    # Detect or use provided model
    if args.model:
        model = args.model
    else:
        model = detect_model(client)

    # Set up global event queue for state-change broadcasts
    global_queue: queue.Queue = queue.Queue()
    global_listeners: list = []
    global_listeners_lock = threading.Lock()
    WebUI._global_queue = global_queue

    # Session factory — captures shared config
    def session_factory(ui):
        return ChatSession(
            client=client,
            model=model,
            ui=ui,
            persona=args.persona,
            instructions=args.instructions,
            temperature=args.temperature,
            max_tokens=args.max_tokens,
            tool_timeout=args.tool_timeout,
            reasoning_effort=args.reasoning_effort,
            context_window=args.context_window,
            compact_max_tokens=args.compact_max_tokens,
            auto_compact_pct=args.auto_compact_pct,
            agent_max_turns=args.agent_max_turns,
            tool_truncation=args.tool_truncation,
        )

    # Create workstream manager and initial workstream
    manager = WorkstreamManager(session_factory)
    ws = manager.create(
        name="default",
        ui_factory=lambda wid: WebUI(ws_id=wid),
    )
    if args.skip_permissions:
        ws.ui.auto_approve = True

    # Handle --resume
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

    # Create and configure threaded HTTP server (SSE needs a persistent
    # connection, so POSTs must be handled on separate threads).
    server = ThreadedHTTPServer((args.host, args.port), TurnstoneHTTPHandler)
    server.workstreams = manager  # type: ignore[attr-defined]
    server.global_queue = global_queue  # type: ignore[attr-defined]
    server.global_listeners = global_listeners  # type: ignore[attr-defined]
    server.global_listeners_lock = global_listeners_lock  # type: ignore[attr-defined]
    server.skip_permissions = args.skip_permissions  # type: ignore[attr-defined]

    from turnstone.core.auth import load_auth_config

    auth_config = load_auth_config()
    server.auth_config = auth_config  # type: ignore[attr-defined]
    if auth_config.enabled:
        print(f"Auth: enabled ({len(auth_config.tokens)} token(s) configured)")

    # Start global event fan-out thread
    fanout = threading.Thread(
        target=_global_fanout_thread,
        args=(global_queue, global_listeners, global_listeners_lock),
        daemon=True,
    )
    fanout.start()

    if args.workstream_idle_timeout > 0:
        cleanup = threading.Thread(
            target=_idle_cleanup_thread,
            args=(manager, args.workstream_idle_timeout * 60, global_queue),
            daemon=True,
        )
        cleanup.start()

    print(f"turnstone web server running on http://{args.host}:{args.port}")
    print(f"Model: {model}")
    if args.persona:
        print(f"Persona: {args.persona}")
    print("Press Ctrl+C to stop.")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()


if __name__ == "__main__":
    main()
