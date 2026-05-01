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

if TYPE_CHECKING:
    from collections.abc import Iterable

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
from turnstone.core import session_worker
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
from turnstone.core.session_replay import session_replay_preamble
from turnstone.core.session_routes import (
    AttachmentUploadHelpers,
    SessionEndpointConfig,
    SharedSessionVerbHandlers,
    make_approve_handler,
    make_attachment_handlers,
    make_cancel_handler,
    make_close_handler,
    make_create_handler,
    make_dequeue_handler,
    make_detail_handler,
    make_events_handler,
    make_history_handler,
    make_list_handler,
    make_open_handler,
    make_saved_handler,
    make_send_handler,
    register_session_routes,
)
from turnstone.core.session_ui_base import (
    AutoApproveReason,
    SessionUIBase,
    _put_with_priority,
    fire_judge_verdict_metric,
)
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

    # ``_enqueue`` / ``_register_listener`` / ``_unregister_listener``
    # inherited from :class:`SessionUIBase`. ``_ws_turn_content`` /
    # ``_ws_turn_content_size`` accumulator fields lifted to
    # :class:`SessionUIBase` so coord can populate them too.

    def _ws_kind_and_parent(self) -> tuple[WorkstreamKind, str | None]:
        """Return cached (kind, parent_ws_id) for broadcast event payloads.

        Stored on the UI at construction time — both fields are
        immutable for the lifetime of the workstream, so re-reading
        them from the manager under lock on every broadcast was a
        process-wide serialization tax on every activity tick.
        """
        return self._kind, self._parent_ws_id

    def _broadcast_state(self, state: str) -> None:
        """Send a state-change event to the global SSE channel.

        Reads the rich-payload snapshot via the lifted
        :meth:`SessionUIBase.snapshot_and_consume_state_payload`
        helper, then puts the assembled ``ws_state`` event on the
        global queue. The snapshot helper handles the IDLE/ERROR
        ``_ws_turn_content`` consume + clear under ``_ws_lock``.
        """
        if WebUI._global_queue is not None:
            payload = self.snapshot_and_consume_state_payload(state)
            kind, parent_ws_id = self._ws_kind_and_parent()
            event: dict[str, Any] = {
                "type": "ws_state",
                "ws_id": self.ws_id,
                "state": state,
                "tokens": payload["tokens"],
                "context_ratio": payload["context_ratio"],
                "activity": payload["activity"],
                "activity_state": payload["activity_state"],
                "kind": kind,
                "parent_ws_id": parent_ws_id,
            }
            if state == "idle":
                event["content"] = payload["content"]
            # ``pending_approval_detail`` is NO LONGER piggybacked on
            # state-change events (Stage 3 cleanup). Symmetric event
            # flow now: initial approval items arrive via bulk fetch
            # triggered by the ``activity_state="approval"`` transition,
            # individual verdicts via the explicit
            # ``intent_verdict`` event class, and resolution via
            # ``approval_resolved``. Reducer no longer has to dedupe
            # the piggyback path against the explicit one.
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

    def _broadcast_intent_verdict(self, verdict: dict[str, Any]) -> None:
        """Send an LLM intent-judge verdict to the global SSE channel.

        Stage 3 Step 5 — the cluster collector's ``_apply_delta``
        forwards this verbatim to the cluster bus, where coord
        adapters dispatch it as ``child_ws_intent_verdict`` for the
        owning parent's tree UI. Unlike the existing
        ``pending_approval_detail`` piggyback on ``ws_state``, this
        fires WHENEVER a verdict lands — including the common case
        where the judge daemon writes during ``attention`` with no
        state transition to ride along on.
        """
        if WebUI._global_queue is not None:
            # Critical event — evict an oldest item if the global
            # queue is full rather than dropping the verdict.
            _put_with_priority(
                WebUI._global_queue,
                {
                    "type": "intent_verdict",
                    "ws_id": self.ws_id,
                    "verdict": verdict,
                },
                critical=True,
            )

    def _broadcast_approval_resolved(
        self,
        approved: bool,
        feedback: str | None = None,
        *,
        always: bool = False,
    ) -> None:
        """Send an ``approval_resolved`` decision to the global SSE channel.

        Clears the parent's pending-approval pill in lockstep with
        the actual decision rather than waiting for the next
        state-change piggyback.
        """
        if WebUI._global_queue is not None:
            # Critical event — evict an oldest item if the global
            # queue is full rather than dropping the resolution.
            _put_with_priority(
                WebUI._global_queue,
                {
                    "type": "approval_resolved",
                    "ws_id": self.ws_id,
                    "approved": approved,
                    "feedback": feedback or "",
                    "always": bool(always),
                },
                critical=True,
            )

    def _broadcast_approve_request(self, detail: dict[str, Any]) -> None:
        """Send an ``approve_request`` payload to the global SSE channel.

        Push path for the initial approval items so a coord parent's
        tree UI can render the inline approve/deny block immediately
        without waiting for a bulk-fetch round-trip. The bulk fetch
        races with ``_pending_approval`` being set inside
        ``approve_tools`` (the state transition to ATTENTION fires
        upstream first); the push path eliminates that race entirely.
        """
        if WebUI._global_queue is not None:
            # Critical event — evict an oldest item if the global
            # queue is full rather than dropping the items.
            _put_with_priority(
                WebUI._global_queue,
                {
                    "type": "approve_request",
                    "ws_id": self.ws_id,
                    "detail": detail,
                },
                critical=True,
            )

    # --- SessionUI protocol ---
    #
    # ``on_thinking_start`` / ``on_thinking_stop`` / ``on_reasoning_token``
    # / ``on_content_token`` / ``on_stream_end`` / ``on_tool_output_chunk``
    # / ``on_info`` / ``on_error`` are inherited from
    # :class:`SessionUIBase`. ``on_status`` and ``on_tool_result`` are
    # overridden below to layer Prometheus ``_metrics.record_*`` calls
    # (node-only) on top of the shared per-ws metric writes.

    # ``approve_tools`` is inherited from :class:`SessionUIBase`. The
    # node-level prometheus metric for heuristic verdicts is layered via
    # the ``_record_judge_metric`` hook below so the lifted body stays
    # transport-agnostic.

    def _record_judge_metric(self, verdict: dict[str, Any]) -> None:
        """Layer the per-node Prometheus metric on top of the shared body.

        ``SessionUIBase.approve_tools`` calls this for each persisted
        heuristic verdict; ``ConsoleCoordinatorUI`` overrides the same
        hook to feed the console's ``ConsoleMetrics`` — same metric
        name, so a cluster-wide PromQL query rolls coord and
        interactive verdicts up uniformly. The LLM-tier counterpart
        lives in ``on_intent_verdict`` below — same metric, different
        tier label, same ``record_judge_verdict`` call.
        """
        fire_judge_verdict_metric(_metrics, verdict, "heuristic")

    def on_tool_result(
        self,
        call_id: str,
        name: str,
        output: str,
        *,
        is_error: bool = False,
    ) -> None:
        """Layer node-only Prometheus metrics on top of the shared body."""
        _metrics.record_tool_call(name)
        super().on_tool_result(call_id, name, output, is_error=is_error)

    def on_status(self, usage: dict[str, Any], context_window: int, effort: str) -> None:
        """Layer node-only Prometheus metrics on top of the shared body.

        ``_metrics.record_*`` calls feed the node's prometheus
        endpoint; the per-ws counter writes, the ``status`` event
        enqueue, and the ``usage_event`` storage row are inherited
        from :meth:`SessionUIBase.on_status`. ``usage`` field access
        is defensive for parity with the lifted body.
        """
        prompt_tokens = usage.get("prompt_tokens", 0)
        completion_tokens = usage.get("completion_tokens", 0)
        total_tok = prompt_tokens + completion_tokens
        cache_creation = usage.get("cache_creation_tokens", 0)
        cache_read = usage.get("cache_read_tokens", 0)
        _metrics.record_tokens(prompt_tokens, completion_tokens)
        _metrics.record_cache_tokens(cache_creation, cache_read)
        _metrics.record_context_ratio(total_tok / context_window if context_window > 0 else 0.0)
        super().on_status(usage, context_window, effort)

    def on_plan_review(self, content: str) -> str:
        self._plan_event.clear()
        self._pending_plan_review = {"type": "plan_review", "content": content}
        self._enqueue(self._pending_plan_review)
        if not self._plan_event.wait(timeout=self._APPROVAL_WAIT_TIMEOUT):
            log.warning("Plan review timed out for ws_id=%s", self.ws_id)
            self._plan_result = ""
        self._pending_plan_review = None
        return self._plan_result

    def on_error(self, message: str) -> None:
        """Layer node-only Prometheus error counter on top of the shared body."""
        _metrics.record_error()
        super().on_error(message)

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
        fire_judge_verdict_metric(_metrics, verdict, "llm")

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
    # Metacognitive nudges live on the message dict's ``_reminders``
    # side-channel — user messages carry user-channel nudges
    # (correction / denial / resume / start / completion), tool
    # messages carry tool-channel nudges (tool_error / repeat).  Both
    # are surfaced separately on each entry so the UI can render them
    # as their own bubble (live via ``user_reminder`` /
    # ``tool_reminder`` SSE events; replay via this propagation).
    # ``content`` never carries the ``<system-reminder>`` envelope —
    # that splice is transient, applied to a wire-bound copy in
    # ``ChatSession._apply_reminders_for_provider``.
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
        # Surface the ``_reminders`` side-channel so a tab reconnecting
        # via /history renders the same metacognitive nudge bubble the
        # originating tab saw live (user-channel reminders via
        # ``user_reminder`` SSE; tool-channel via ``tool_reminder``).
        # Reminders are in-memory only (not persisted to DB), so this
        # only fires for the originating session.
        reminders = msg.get("_reminders")
        if isinstance(reminders, list):
            # Filter first so an all-malformed _reminders doesn't set the
            # field to []; absent vs. empty-list should mean the same
            # thing on the wire.
            clean_reminders = [
                {"type": str(r.get("type") or ""), "text": str(r.get("text") or "")}
                for r in reminders
                if isinstance(r, dict)
            ]
            if clean_reminders:
                entry["reminders"] = clean_reminders
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
# Per-kind policies passed to the lifted session_routes handlers
# ---------------------------------------------------------------------------


def _interactive_manager_lookup(
    request: Request,
) -> tuple[SessionManager | None, JSONResponse | None]:
    """Return the interactive ``SessionManager`` from app.state.

    Interactive always has the manager loaded (it's constructed
    synchronously at server startup), so the 503 branch is unused
    on this side. Matches the :attr:`SessionEndpointConfig.manager_lookup`
    callable shape so the lifted handler bodies can call it uniformly.
    """
    return request.app.state.workstreams, None


def _interactive_tenant_check(
    request: Request, ws_id: str, mgr: SessionManager
) -> JSONResponse | None:
    """Cross-tenant gate for the lifted session handlers.

    Forwards to :func:`_require_ws_access`, which returns 404 on
    owner mismatch (the interactive trusted-team model).
    """
    _owner, err = _require_ws_access(request, ws_id, mgr=mgr)
    return err


def _audit_close_workstream(
    request: Request,
    ws_id: str,
    ws_before: Workstream,
    reason: str,
) -> None:
    """Record the ``workstream.closed`` audit event for interactive close.

    Passed to :func:`make_close_handler` as the ``audit_emit``
    callable. ``storage`` is guaranteed non-``None`` by the lifted
    handler's upstream gate; the ``getattr`` fallback is defensive
    consistency with the rest of the storage access pattern.
    """
    from turnstone.core.audit import record_audit

    storage = getattr(request.app.state, "auth_storage", None)
    if storage is None:
        return
    _, ip = _audit_context(request)
    detail: dict[str, Any] = {
        "kind": str(ws_before.kind),
        "parent_ws_id": ws_before.parent_ws_id,
    }
    if reason:
        detail["reason"] = reason
    record_audit(
        storage,
        _auth_user_id(request),
        "workstream.closed",
        "workstream",
        ws_id,
        detail,
        ip,
    )


def _interactive_events_replay(
    ws: Workstream, ui: Any, request: Request
) -> Iterable[dict[str, Any]]:
    """Initial SSE replay payload for interactive ``events`` connections.

    Pre-lift ``events_sse`` yielded five things on connect: a
    ``connected`` event with model + skip_permissions; a ``status``
    event with the workstream's last token usage + context %; the
    full conversation ``history`` (with pending-approval flagging on
    the last assistant entry's tool calls); the pending approval
    prompt + cached intent verdicts (if a prompt is pending); the
    pending plan-review (if a review is pending). The lifted
    ``make_events_handler`` body delegates that yield sequence to
    this callback so the kind-specific shape stays in this module.

    Pure read — never mutates ``ws`` / ``ui`` / ``session``.
    """
    del request  # not needed; replay reads ws/ui/session state
    session = ws.session
    if session is None:
        # Defensive — the lifted body's UI presence check guarantees
        # the workstream made it past placeholder state, but the
        # session can still be detached on the close-then-reopen path.
        return

    # Connected + status preamble — same shape coord replays use; the
    # shared helper keeps the two surfaces from drifting on a future
    # field add.
    yield from session_replay_preamble(session, ui)

    # History replay — pending-approval flag rides on the last
    # assistant entry's tool_calls so the client renders them as
    # awaiting approval rather than already approved.
    pending_approval = getattr(ui, "_pending_approval", None)
    history = _build_history(session, has_pending_approval=pending_approval is not None)
    if history:
        yield {"type": "history", "messages": history}

    # Pending approval re-injection (so a reconnecting tab sees the
    # prompt) + cached LLM verdicts received since the prompt fired.
    if pending_approval is not None:
        yield pending_approval
        with ui._ws_lock:
            cached_verdicts = list(ui._llm_verdicts.values())
        for v in cached_verdicts:
            yield {"type": "intent_verdict", **v}

    # Pending plan-review re-injection.
    pending_plan = getattr(ui, "_pending_plan_review", None)
    if pending_plan is not None:
        yield pending_plan


def _interactive_open_post_load(request: Request, ws: Workstream) -> None:
    """Post-load hook for the lifted interactive ``open`` body.

    Runs after ``mgr.open(ws_id)`` returns the workstream (which
    internally already attempted ``ws.session.resume(ws_id)`` and
    fired ``InteractiveAdapter.emit_rehydrated`` — the latter being
    a no-op stub on interactive per the documented asymmetry). This
    callback handles the interactive-only out-of-band emissions:

    1. Sync the workstream's name to the persisted display alias
       (a user-renamed workstream stores its alias separately from
       the manager's in-memory name).
    2. Replay clear_ui + history onto the per-workstream UI listener
       queue so a freshly-connected browser tab sees the conversation
       state. Only fires when ``ws.session.messages`` is non-empty
       (resume succeeded and there's history to show).
    3. Enqueue ``ws_created`` onto the global SSE queue so dashboards
       and other multi-workstream consumers see the rehydrate. The
       handler-side emission is the load-bearing path on interactive;
       ``InteractiveAdapter.emit_rehydrated`` is a no-op stub
       precisely because this enqueue lives here.
    """
    from turnstone.core.memory import get_workstream_display_name

    ws.name = get_workstream_display_name(ws.id) or ws.name
    ui = ws.ui
    session = ws.session
    if isinstance(ui, WebUI) and session is not None and session.messages:
        ui._enqueue({"type": "clear_ui"})
        history = _build_history(session)
        if history:
            ui._enqueue({"type": "history", "messages": history})

    gq: queue.Queue[dict[str, Any]] | None = getattr(request.app.state, "global_queue", None)
    if gq is not None:
        with contextlib.suppress(queue.Full):
            gq.put_nowait(
                {
                    "type": "ws_created",
                    "ws_id": ws.id,
                    "name": ws.name,
                    "model": session.model if session else "",
                    "model_alias": session.model_alias if session else "",
                    "kind": ws.kind,
                    "parent_ws_id": ws.parent_ws_id,
                    "user_id": ws.user_id,
                }
            )


def _audit_workstream_opened(request: Request, ws: Workstream) -> None:
    """Record the ``workstream.opened`` audit event.

    Passed to :func:`make_open_handler` as the ``audit_emit``
    callable. Mirrors :func:`_audit_close_workstream`'s shape.
    Distinguishing rehydrate from fresh-create in the audit trail
    (same ``ws_created`` SSE shape on the wire; the audit action
    name is the disambiguator) is the original justification for
    this row.
    """
    from turnstone.core.audit import record_audit

    storage = getattr(request.app.state, "auth_storage", None)
    if storage is None:
        return
    _, ip = _audit_context(request)
    record_audit(
        storage,
        _auth_user_id(request),
        "workstream.opened",
        "workstream",
        ws.id,
        {"kind": str(ws.kind), "parent_ws_id": ws.parent_ws_id},
        ip,
    )


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
        # ``pending_approval_detail`` mirrors the dashboard handler's
        # projection so the console collector's reconnect-via-snapshot
        # path (``_reconcile_node``) can carry the rich approval payload
        # across reconnects — without it, a child sitting in approval-
        # pending across a console restart or network blip would render
        # with no buttons until the next state change. Same data, same
        # ``read`` scope as ``/v1/api/dashboard``.
        approval_detail: dict[str, Any] | None = None
        if ui is not None and hasattr(ui, "serialize_pending_approval_detail"):
            approval_detail = ui.serialize_pending_approval_detail()
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
                "pending_approval_detail": approval_detail,
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
                "ws_id": ws.id,
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
                "pending_approval_detail": ui.serialize_pending_approval_detail(),
                # Per-ws ring buffer of recent auto-approves (last 10).
                # Lets the coord-tree render a "recently auto-approved
                # by skill X" pill without a per-child round-trip — the
                # tools-bypassed-the-prompt set is otherwise invisible
                # to anyone watching the dashboard tree.
                "recent_auto_approvals": ui.serialize_recent_auto_approvals(),
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

    Handles both busy (enqueue into ``session._watch_pending`` for drain at IDLE)
    and idle (spawn a worker thread) cases via the shared
    :func:`turnstone.core.session_worker.send` dispatcher. The shared
    dispatcher's ``_worker_running`` gate keeps watches and the path-keyed
    send endpoint from racing into parallel workers on the same ChatSession.
    """
    pending = session._watch_pending

    def dispatch(msg: str) -> None:
        def _enq() -> None:
            # Watches don't pump through ChatSession.queue_message; instead
            # they drop into the IDLE-drain pending queue. Swallow Full
            # locally — drop-on-full is the long-standing watch behavior
            # (no 429 surface).
            try:
                pending.put_nowait({"message": msg})
            except queue.Full:
                log.warning(
                    "Watch pending queue full, dropping result for ws %s",
                    ws.id,
                )

        def _run() -> None:
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

        session_worker.send(
            ws,
            enqueue=_enq,
            run=_run,
            thread_name=f"watch-worker-{ws.id[:8]}",
        )

    return dispatch


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
            # Prevent rewind/retry while a generation is in progress.
            # Gate on ``_worker_running`` (not ``worker_thread.is_alive()``)
            # for parity with session_worker.send: spawn paths set the
            # flag before assigning ws.worker_thread, so a reader using
            # the old gate could see a stale dead thread while a new
            # worker is in the middle of starting.
            with ws._lock:
                if ws._worker_running:
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
                    finally:
                        with ws._lock:
                            ws._worker_running = False

                # Inlined rather than via ``session_worker.send`` because
                # retry-when-busy is a hard reject (UI error, no fallback
                # queue) — the shared dispatcher's enqueue/spawn shape
                # doesn't fit. We gate on ``_worker_running`` for parity
                # with that dispatcher so the two paths can't race into
                # parallel workers on the same ChatSession.
                with ws._lock:
                    if ws._worker_running:
                        ui.on_error("Cannot retry: workstream is busy")
                    else:
                        ws._worker_running = True
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


async def _interactive_create_validate_request(
    request: Request,
    body: dict[str, Any],
    uid: str,
    uploaded_files: list[tuple[str, str, bytes]],
) -> JSONResponse | None:
    """Per-kind pre-create gates for interactive workstreams.

    Wired onto :attr:`SessionEndpointConfig.create_validate_request`
    and called by :func:`make_create_handler` after body parsing
    but before skill resolution / ``mgr.create``. Returns the
    rejection response or ``None`` to continue.

    Gates:
    - ws_id format must match :data:`_VALID_WS_ID` (32 hex chars)
      when supplied.
    - attachments + resume_ws combo is disallowed (resume forks an
      existing ws; attachments belong on the *fresh* turn — caller
      should resume first, then upload via the standard endpoint).
    - body kind must be ``INTERACTIVE``: coordinator workstreams
      land on the console handler with ``admin.coordinator`` scope,
      not this one. Unknown / future kind values 400 rather than
      silently coerce.
    - parent_ws_id (when supplied) must reference a coordinator
      owned by ``uid``. Without this gate an attacker could point a
      new interactive workstream at someone else's coordinator and
      receive that coordinator's child_ws_* SSE events
      (name/state/tokens leak).
    - notify_targets (when supplied) must validate.
      :func:`_validate_notify_targets` is pure-read and doesn't need
      ``ws`` to be built — gating here preserves pre-lift's 400
      semantic for caller-supplied input. Without this pre-create
      gate a malformed ``notify_targets`` would land in
      ``post_install`` (after ``mgr.create``, audit emit, and the
      ``ws_created`` broadcast) and the only available signal is to
      raise — which the factory turns into 500. 400 at the gate is
      correct shape for client-input validation.
    """
    requested_ws_id = body.get("ws_id", "") or ""
    if not isinstance(requested_ws_id, str):
        requested_ws_id = ""
    if requested_ws_id and not _VALID_WS_ID.match(requested_ws_id):
        return JSONResponse({"error": "invalid ws_id format"}, status_code=400)
    resume_ws_id = body.get("resume_ws", "") or ""
    if uploaded_files and resume_ws_id:
        return JSONResponse(
            {"error": "attachments cannot be combined with resume_ws"},
            status_code=400,
        )
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
    notify_targets_raw = body.get("notify_targets", "[]")
    if isinstance(notify_targets_raw, list):
        notify_targets_raw = json.dumps(notify_targets_raw)
    _, nt_err = _validate_notify_targets(notify_targets_raw)
    if nt_err:
        return JSONResponse({"error": nt_err}, status_code=400)
    return None


def _interactive_create_build_kwargs(
    request: Request,
    body: dict[str, Any],
    uid: str,
    skill_data: dict[str, Any] | None,
    skill_id: str,
    applied_skill_version: int,
) -> dict[str, Any]:
    """Build kwargs for ``mgr.create`` from a parsed interactive create body.

    Wired onto :attr:`SessionEndpointConfig.create_build_kwargs`. The
    factory threads the resolved skill_data + skill_id + version
    through; this builder picks the right model (skill override
    beats body) and assembles the full kwargs dict that
    ``SessionManager.create`` accepts (including the kind-specific
    ``judge_model`` / ``client_type`` / ``parent_ws_id`` extras).
    """
    resolved_model = body.get("model") or None
    if skill_data and skill_data.get("model"):
        resolved_model = skill_data["model"]
    requested_ws_id = body.get("ws_id", "") or ""
    if not isinstance(requested_ws_id, str):
        requested_ws_id = ""
    # Use the canonical skill name from the resolved row rather than
    # ``body["skill"]`` so a whitespace-padded request body
    # (``"skill": "  my-skill "``) doesn't persist a name that fails
    # later session-side lookups. The factory strips the lookup key
    # but the raw value used to flow through unchanged.
    canonical_skill = str(skill_data["name"]) if skill_data and skill_data.get("name") else None
    return {
        "user_id": uid,
        "name": body.get("name", ""),
        "model": resolved_model,
        "skill": canonical_skill,
        "skill_id": skill_id,
        "skill_version": applied_skill_version,
        "ws_id": requested_ws_id,
        "client_type": body.get("client_type", "") or "",
        "judge_model": body.get("judge_model", "") or None,
        "parent_ws_id": body.get("parent_ws_id") or None,
    }


async def _interactive_create_post_install(
    request: Request,
    ws: Workstream,
    body: dict[str, Any],
    uid: str,
    skill_data: dict[str, Any] | None,
    applied_skill_version: int,
    attachment_ids: list[str],
) -> dict[str, Any]:
    """Tail end of interactive create: per-WebUI bookkeeping + dispatch.

    Wired onto :attr:`SessionEndpointConfig.create_post_install`.
    Runs after the workstream is fully built, attachments saved,
    and audit emitted. Sequence:

    1. Cast ``ws.ui`` to :class:`WebUI` (defence in depth — the
       interactive adapter's session factory is the only path that
       reaches this handler).
    2. Apply ``auto_approve`` from server-wide ``skip_permissions``
       or per-request body.
    3. Register the watch runner for the workstream's session.
    4. Broadcast ``ws_created`` on the global SSE queue. Held until
       this point so a rejected attachment validation produces no
       phantom create→close pair on the SSE stream.
    5. Atomic resume: if ``body["resume_ws"]`` is set, fork the
       referenced session into the new ws_id, push history into the
       UI listener queue, and rebroadcast ``ws_rename`` so the tab
       picks up the fork's display name.
    6. Apply the skill's session config (temperature / reasoning /
       max_tokens / approval policy / metadata).
    7. Resolve notify_targets (schedule targets win over skill
       fallback).
    8. Pin the workstream's routing to this node when no caller-
       supplied ``ws_id`` was provided (direct creates).
    9. Spawn the initial-message worker thread when ``initial_message``
       is set, reserving any uploaded attachments for that first
       turn.

    Returns ``{resumed, message_count}`` for the response. On the
    no-resume path both default to ``False`` / ``0``.
    """
    from turnstone.core.memory import get_workstream_display_name

    if not isinstance(ws.ui, WebUI):
        raise TypeError(f"Expected WebUI, got {type(ws.ui).__name__}")
    skip: bool = request.app.state.skip_permissions
    if skip or body.get("auto_approve", False):
        ws.ui.auto_approve = True
    runner = getattr(request.app.state, "watch_runner", None)
    if runner and ws.session:
        ws.session.set_watch_runner(runner, dispatch_fn=_make_watch_dispatch(ws, ws.session, ws.ui))
    gq: queue.Queue[dict[str, Any]] = request.app.state.global_queue
    # Emit ``ws_created`` on the global queue for SSE consumers
    # (console). Held until past attachment validation in the
    # factory so a rejected upload doesn't flash a workstream that
    # never really existed.
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
                # Owner id propagates through the cluster event
                # stream so console-side fan-out can enforce tenant
                # isolation — a coordinator must never receive
                # child_ws_* events for workstreams it doesn't own.
                "user_id": ws.user_id,
            }
        )

    # Atomic workstream resume during creation.
    resumed = False
    message_count = 0
    resume_ws_id = body.get("resume_ws", "") or ""
    if resume_ws_id and ws.session is not None:
        from turnstone.core.memory import resolve_workstream

        target_id = resolve_workstream(resume_ws_id)
        if target_id and ws.session.resume(target_id, fork=True):
            resumed = True
            message_count = len(ws.session.messages)
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
            with contextlib.suppress(queue.Full):
                gq.put_nowait({"type": "ws_rename", "ws_id": ws.id, "name": ws.name})

    # Apply skill session config (only for new workstreams with a skill).
    if skill_data and not resumed and ws.session:
        sess = ws.session
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
        if skill_data.get("auto_approve"):
            ws.ui.auto_approve = True
        allowed = skill_data.get("allowed_tools", "")
        if allowed and allowed != "[]":
            import json as _json

            try:
                tools_list = _json.loads(allowed)
            except (ValueError, TypeError):
                tools_list = [t.strip() for t in allowed.split(",") if t.strip()]
            if tools_list:
                ws.ui.auto_approve_tools = set(tools_list)
                # Tag each as skill-sourced so the dashboard can show
                # "auto-approved by skill X" instead of a generic
                # auto-approval pill — distinguishes the (often
                # surprising) skill-template path from a deliberate
                # operator "Approve + Always" click.
                ws.ui._auto_approve_tools_source = {t: AutoApproveReason.SKILL for t in tools_list}
        sess._notify_on_complete = skill_data.get("notify_on_complete", "{}")
        sess._applied_skill_id = skill_data["template_id"]
        sess._applied_skill_version = applied_skill_version
        if skill_data.get("content"):
            sess._applied_skill_content = skill_data["content"]
        sess._save_config()

    # notify_targets: schedule targets override skill targets. The
    # validator already gated malformed input as 400; here we just
    # canonicalise (list → JSON-encoded string) and apply the skill
    # fallback if the caller didn't supply targets.
    notify_targets_raw = body.get("notify_targets", "[]")
    if isinstance(notify_targets_raw, list):
        notify_targets_raw = json.dumps(notify_targets_raw)
    nt_str, _ = _validate_notify_targets(notify_targets_raw)
    if nt_str == "[]" and skill_data:
        skill_notify = skill_data.get("notify_on_complete", "[]")
        if skill_notify and skill_notify != "{}" and skill_notify != "[]":
            fallback_str, fallback_err = _validate_notify_targets(skill_notify)
            if not fallback_err:
                nt_str = fallback_str
    ws.notify_targets = nt_str

    # Pin locally-created workstreams so the console routes to this node.
    requested_ws_id = body.get("ws_id", "") or ""
    if not requested_ws_id:
        node_id = getattr(request.app.state, "node_id", "")
        if node_id:
            try:
                from turnstone.core.storage import get_storage as _gs

                _gs().set_workstream_override(ws.id, node_id, reason="local")
            except Exception:
                log.debug("Failed to set routing override for %s", ws.id, exc_info=True)

    # Initial-message worker thread.
    initial_message = body.get("initial_message", "").strip()
    if initial_message and ws.session is not None:
        from turnstone.core.attachments import (
            reserve_and_resolve_attachments as _reserve_and_resolve,
        )

        session = ws.session
        send_id = uuid.uuid4().hex
        resolved_atts: list[Any] = []
        if attachment_ids:
            resolved_atts, _ord, _drop = _reserve_and_resolve(attachment_ids, send_id, ws.id, uid)

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
                with ws._lock:
                    ws._worker_running = False

        # Inlined rather than via ``session_worker.send`` because at
        # workstream creation no live worker can exist by
        # construction — the enqueue branch of the shared dispatch
        # is dead code here. ``_worker_running`` + ``ws.worker_thread``
        # are set together under ``ws._lock`` so a path-keyed send
        # arriving immediately after creation observes the running
        # state via the shared session_worker gate instead of racing
        # into a parallel worker.
        with ws._lock:
            ws._worker_running = True
            t = threading.Thread(target=_run_initial, daemon=True, name=f"ws-init-{ws.id[:8]}")
            ws.worker_thread = t
            t.start()

    return {"resumed": resumed, "message_count": message_count}


def _audit_workstream_created(
    request: Request,
    ws: Workstream,
    body: dict[str, Any],
    uid: str,
) -> None:
    """Audit emitter for the interactive ``workstream.created`` event.

    Wired onto :func:`make_create_handler` as ``audit_emit``. Runs
    after the workstream is built and attachments saved; failures
    are caught + logged at ``warning`` by the factory without
    changing the create handler's successful 200 response.
    """
    from turnstone.core.audit import record_audit

    _audit_storage = getattr(request.app.state, "auth_storage", None)
    if _audit_storage is None:
        return
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
    name: str = ""
    _, ip = _audit_context(request)
    try:
        # Snapshot row fields before the delete wipes the row.  Inside
        # the try so a transient storage error surfaces through the
        # endpoint's redacted 500 handler below rather than as an
        # unhandled exception.  ``kind`` / ``parent_ws_id`` go into the
        # audit record; ``name`` is forwarded ONLY to ``mgr.delete`` so
        # the ``ws_closed`` event payload carries the same field other
        # terminal transitions emit (interactive operators see the name
        # in close toasts; coord-side ``child_ws_closed`` ignores it
        # but the global queue contract is uniform).  ``name`` is
        # deliberately NOT in the audit detail — display names can be
        # long / operator-noisy and aren't needed for forensic recall
        # (ws_id + kind + parent are enough).
        if storage is not None:
            row = storage.get_workstream(ws_id) or {}
            kind = row.get("kind", "")
            parent_ws_id = row.get("parent_ws_id")
            name = row.get("name", "") or ""
        if delete_workstream(ws_id):
            log.info("ws.deleted", ws_id=ws_id[:8])
            # Fire ``ws_closed`` with ``reason='deleted'`` so the
            # cluster collector → coord adapter chain re-emits as
            # ``child_ws_closed`` and the operator's child-tree drops
            # the row.  Without this the row stays visible (with its
            # last-known state) until a full reload — a model that
            # spawns→completes→deletes children leaves an
            # ever-growing tree on the dashboard.  Best-effort: an
            # emit failure must not roll back the storage delete or
            # 500 the response.
            mgr = getattr(request.app.state, "workstreams", None)
            if mgr is not None:
                try:
                    mgr.delete(ws_id, name=name)
                except Exception:
                    log.warning("ws.delete.event_emit_failed", ws_id=ws_id[:8], exc_info=True)
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


def _auth_user_id(request: Request) -> str:
    """Return the authenticated user's id (empty string when absent).

    Thin shim over :func:`turnstone.core.web_helpers.auth_user_id` —
    kept as a module-level alias so existing call sites don't need a
    sweeping rename. The lifted helper is the canonical version
    (shared by both kinds since P1.5).
    """
    from turnstone.core.web_helpers import auth_user_id

    return auth_user_id(request)


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

    Thin shim over :func:`turnstone.core.web_helpers.resolve_workstream_owner` —
    kept as a module-level alias for the many existing callers.
    Both helpers preserve the same trusted-team semantics: any
    authenticated caller resolves to the row's recorded owner (with
    fallback to caller uid on unowned rows). The lifted version is
    the canonical implementation post-P1.5.
    """
    from turnstone.core.web_helpers import resolve_workstream_owner

    return resolve_workstream_owner(request, ws_id, mgr=mgr, not_found_label="Workstream not found")


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
    # Start the buffered state-writer flusher
    state_writer = getattr(app.state, "state_writer", None)
    if state_writer is not None:
        state_writer.start()

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

        # Collect and store node metadata (auto + config).
        # ``collect_node_info`` runs synchronous probes (sysfs reads,
        # /proc reads, IMDS HTTP requests).  Off-load to a worker
        # thread so the IMDS path's worst-case latency (~1 s on a
        # misidentified-cloud host) doesn't block the event loop
        # during the rest of the lifespan startup work.
        try:
            from turnstone.core.config import load_config as _load_meta_config
            from turnstone.core.node_info import collect_node_info

            _auto_info = await asyncio.to_thread(collect_node_info)
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
    # Drain + stop the buffered state-writer. shutdown() joins a
    # daemon thread and runs synchronous DB writes; offload to a
    # worker thread so we don't block the lifespan event loop and
    # delay other teardown tasks.
    state_writer = getattr(app.state, "state_writer", None)
    if state_writer is not None:
        await asyncio.to_thread(state_writer.shutdown)
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
    state_writer: Any = None,
) -> Starlette:
    """Create and configure the Starlette ASGI application."""
    _spec = build_server_spec()
    _openapi_handler = make_openapi_handler(_spec)
    _docs_handler = make_docs_handler()

    # Workstream HTTP tree — owned by the shared registrar in
    # ``turnstone.core.session_routes`` so the console mounts the same
    # shape against its coord manager. The lifted handler factories
    # (``make_approve_handler``, ``make_close_handler``) capture the
    # kind-specific ``SessionEndpointConfig`` via closure.
    def _interactive_attachment_owner(
        request: Request, ws_id: str, _mgr: SessionManager
    ) -> tuple[str, JSONResponse | None]:
        """Resolve attachment owner for interactive workstreams via
        :func:`_require_ws_access`. Mirrors the pre-P1.5 inline logic
        in ``send_message`` — uses the storage path (``mgr`` not
        passed) so tests with MagicMock managers don't trip on a
        magic-mocked ``ws.user_id``."""
        return _require_ws_access(request, ws_id)

    def _interactive_spawn_metrics(_request: Request, ui: Any) -> None:
        """Per-conversation metrics fired once per send that spawns a
        fresh worker. Coord wires its own
        :func:`turnstone.console.server._coord_spawn_metrics` (the
        Prometheus-free analog) post the rich ``ws_state`` payload
        lift — both kinds need the per-UI counter writes so the
        cluster broadcast renders the same per-turn shape.

        The per-UI counters live on :class:`SessionUIBase`
        (inherited by both :class:`WebUI` and
        :class:`turnstone.console.coordinator_ui.ConsoleCoordinatorUI`),
        so the ``hasattr`` guards survive only as defence against a
        future ``SessionUI`` subclass that doesn't extend the base.
        """
        _metrics.record_message_sent()
        if (
            hasattr(ui, "_ws_lock")
            and hasattr(ui, "_ws_messages")
            and hasattr(ui, "_ws_turn_tool_calls")
        ):
            with ui._ws_lock:
                ui._ws_messages += 1
                ui._ws_turn_tool_calls = 0

    from turnstone.core.attachments import (
        classify_text_attachment as _classify_text_attachment,
    )
    from turnstone.core.attachments import (
        sniff_image_mime as _sniff_image_mime,
    )
    from turnstone.core.attachments import (
        upload_lock as _attachment_upload_lock,
    )

    interactive_attachment_helpers = AttachmentUploadHelpers(
        sniff_image_mime=_sniff_image_mime,
        classify_text_attachment=_classify_text_attachment,
        upload_lock=_attachment_upload_lock,
    )
    from turnstone.core.memory import (
        get_workstream_display_names as _get_ws_display_names,
    )
    from turnstone.core.memory import resolve_workstream as _resolve_workstream_alias

    interactive_endpoint_config = SessionEndpointConfig(
        permission_gate=None,  # interactive auth is enforced at the middleware layer
        manager_lookup=_interactive_manager_lookup,
        tenant_check=_interactive_tenant_check,
        not_found_label="Workstream not found",
        audit_action_prefix="workstream",
        supports_attachments=True,
        attachment_owner_resolver=_interactive_attachment_owner,
        attachment_helpers=interactive_attachment_helpers,
        spawn_metrics=_interactive_spawn_metrics,
        emit_message_queued=True,
        cancel_forensics=_capture_cancel_forensics,
        open_resolve_alias=_resolve_workstream_alias,
        open_post_load=_interactive_open_post_load,
        events_replay=_interactive_events_replay,
        # Pre-lift ``events_sse`` used the dedicated 200-thread
        # ``sse_executor`` so SSE polling stayed isolated from
        # every other ``asyncio.to_thread`` caller in the process
        # (storage, router, audit). Restore that isolation under
        # the lifted contract. The console's coord endpoint wires
        # its own ``coord_sse_executor`` on the same lookup hook —
        # see ``turnstone/console/server.py``.
        sse_executor_lookup=lambda request: request.app.state.sse_executor,
        create_supports_attachments=True,
        create_supports_user_id_override=True,
        create_validate_request=_interactive_create_validate_request,
        create_build_kwargs=_interactive_create_build_kwargs,
        create_post_install=_interactive_create_post_install,
        # Bulk display-name resolution for the active list — one
        # ``SELECT ... WHERE ws_id IN (...)`` for the whole snapshot
        # instead of N per-row queries. Returns a {ws_id: title-or-None}
        # dict; the lifted body falls back to ``ws.name`` per-row.
        list_resolve_titles=_get_ws_display_names,
        # Explicit kind classifier for the lifted list/saved factory's
        # storage filter — required to avoid silently filtering for
        # the wrong kind when a future kind is added.
        list_kind=WorkstreamKind.INTERACTIVE,
        # No state filter: the interactive saved sidebar shows every
        # persisted workstream the storage layer doesn't already
        # tombstone (deleted rows are excluded at the SQL level).
        saved_state_filter=None,
        # No in-memory exclusion: an interactive workstream that's
        # both saved AND loaded is a normal display state.
        saved_loaded_lookup=None,
    )
    approve_handler = make_approve_handler(interactive_endpoint_config)
    close_handler = make_close_handler(
        interactive_endpoint_config,
        audit_emit=_audit_close_workstream,
        supports_close_reason=True,
    )
    cancel_handler = make_cancel_handler(interactive_endpoint_config)
    open_handler = make_open_handler(
        interactive_endpoint_config,
        audit_emit=_audit_workstream_opened,
    )
    events_handler = make_events_handler(interactive_endpoint_config)
    send_handler = make_send_handler(interactive_endpoint_config)
    dequeue_handler = make_dequeue_handler(interactive_endpoint_config)
    attachment_handlers = make_attachment_handlers(interactive_endpoint_config)
    create_handler = make_create_handler(
        interactive_endpoint_config,
        audit_emit=_audit_workstream_created,
    )
    list_handler = make_list_handler(interactive_endpoint_config)
    saved_handler = make_saved_handler(interactive_endpoint_config)
    history_handler = make_history_handler(interactive_endpoint_config)
    detail_handler = make_detail_handler(interactive_endpoint_config)
    v1_routes: list[Any] = [
        Route("/api/events/global", global_events_sse),
    ]
    register_session_routes(
        v1_routes,
        prefix="/api/workstreams",
        handlers=SharedSessionVerbHandlers(
            list_workstreams=list_handler,  # lifted: shared body
            list_saved=saved_handler,  # lifted: shared body
            create=create_handler,  # lifted: shared body
            delete=delete_workstream_endpoint,
            detail=detail_handler,  # lifted: shared body (interactive feature gain)
            open=open_handler,  # lifted: shared body
            close=close_handler,  # lifted: shared body
            refresh_title=refresh_workstream_title,
            set_title=set_workstream_title,
            send=send_handler,  # lifted: shared body (P1.5)
            dequeue=dequeue_handler,  # lifted (P1.5) — DELETE /send
            approve=approve_handler,  # lifted: shared body
            cancel=cancel_handler,  # lifted: shared body
            events=events_handler,  # lifted: shared body
            history=history_handler,  # lifted: shared body (interactive feature gain)
            attachments=attachment_handlers,  # lifted: shared body (P1.5)
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
                    Route("/api/plan", plan_feedback, methods=["POST"]),
                    Route("/api/command", command, methods=["POST"]),
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
    app.state.state_writer = state_writer
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
    parser.add_argument(
        "--skip-permissions",
        action="store_true",
        help="Auto-approve all tool calls (no confirmation prompts)",
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
    from turnstone.core.state_writer import StateWriter

    state_writer = StateWriter(_get_storage())
    manager = SessionManager(
        interactive_adapter,
        storage=_get_storage(),
        max_active=config_store.get("server.max_workstreams"),
        node_id=_node_id,
        state_writer=state_writer,
        # InteractiveAdapter satisfies SessionEventEmitter for the
        # ``ws_closed`` transport path; emit_created / emit_state /
        # emit_rehydrated are no-ops because those events fire from
        # out-of-band paths (create handler + WebUI._broadcast_state).
        event_emitter=interactive_adapter,
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
        if args.skip_permissions or config_store.get("tools.skip_permissions"):
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

    _skip_perms = args.skip_permissions or config_store.get("tools.skip_permissions")
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
        state_writer=state_writer,
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
