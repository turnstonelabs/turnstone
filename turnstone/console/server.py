"""Cluster dashboard HTTP server for turnstone.

Serves the cluster-level dashboard UI and provides REST/SSE APIs
backed by the ClusterCollector.  Uses Starlette/ASGI with uvicorn.

Also provides:
- Workstream creation via HTTP dispatch to target server nodes
- Reverse proxy for server UIs so users only need console port access
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import functools
import html
import json
import logging
import math
import os
import queue
import re
import secrets
import textwrap
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
from sse_starlette import EventSourceResponse
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from turnstone.api.console_spec import build_console_spec
from turnstone.api.docs import make_docs_handler, make_openapi_handler
from turnstone.console.collector import ClusterCollector
from turnstone.console.coordinator_client import load_task_envelope
from turnstone.console.metrics import ConsoleMetrics
from turnstone.console.router import ConsoleRouter
from turnstone.core.audit import record_audit
from turnstone.core.auth import (
    JWT_AUD_CONSOLE,
    JWT_AUD_SERVER,
    AuthMiddleware,
    create_jwt,
    jwt_version_slot,
    require_permission,
)
from turnstone.core.rendezvous import NoAvailableNodeError
from turnstone.core.session_routes import (
    CoordOnlyVerbHandlers,
    SharedSessionVerbHandlers,
    register_coord_verbs,
    register_session_routes,
)
from turnstone.core.skill_kind import SkillKind
from turnstone.core.web_helpers import (
    read_json_or_400,
    require_storage_or_503,
)
from turnstone.core.workstream import WorkstreamKind

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable

    from starlette.requests import Request

    from turnstone.core.session import ChatSession
    from turnstone.core.storage._protocol import StorageBackend

log = logging.getLogger("turnstone.console.server")

# ---------------------------------------------------------------------------
# Static assets — loaded once at startup
# ---------------------------------------------------------------------------

_STATIC_DIR = Path(__file__).parent / "static"
_SHARED_DIR = Path(__file__).parent.parent / "shared_static"
_HTML = ""
_HTML_ETAG = ""


def _load_static() -> None:
    import hashlib

    from turnstone.core.web_helpers import version_html

    global _HTML, _HTML_ETAG
    _HTML = version_html((_STATIC_DIR / "index.html").read_text(encoding="utf-8"))
    _HTML_ETAG = '"' + hashlib.md5(_HTML.encode()).hexdigest()[:16] + '"'  # noqa: S324


# ---------------------------------------------------------------------------
# Query parameter helpers
# ---------------------------------------------------------------------------


def _parse_int(
    params: dict[str, str],
    name: str,
    default: int,
    minimum: int = 0,
    maximum: int = 10000,
) -> int:
    try:
        val = int(params.get(name, str(default)))
    except (ValueError, IndexError):
        val = default
    return max(minimum, min(val, maximum))


# ---------------------------------------------------------------------------
# Pure ASGI middleware
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Proxy helpers
# ---------------------------------------------------------------------------

# JS shim injected into proxied HTML when served through the console.
# Overrides fetch() and EventSource() so root-relative URLs (/v1/api/send etc.)
# route through the console proxy at /node/{node_id}/v1/api/... instead.
_JS_PROXY_SHIM = """\
(function(){
  var _pfx="PREFIX_PLACEHOLDER";
  var _oF=window.fetch;
  window.fetch=function(u,o){
    if(typeof u==="string"&&u.startsWith("/"))u=_pfx+u;
    return _oF.call(this,u,o);
  };
  var _oE=window.EventSource;
  window.EventSource=function(u,o){
    if(typeof u==="string"&&u.startsWith("/"))u=_pfx+u;
    return new _oE(u,o);
  };
  window.EventSource.prototype=_oE.prototype;
  window.EventSource.CONNECTING=_oE.CONNECTING;
  window.EventSource.OPEN=_oE.OPEN;
  window.EventSource.CLOSED=_oE.CLOSED;
})();
"""

_CONSOLE_BANNER_TEMPLATE = (
    '<div class="console-banner">'
    '<a href="/" class="ts-header-back-link" aria-label="Return to console">'
    '<span class="ts-header-back-link-arrow" aria-hidden="true">&larr;</span>'
    "<span>Console</span>"
    "</a>"
    '<span class="console-banner-sep" aria-hidden="true">\u2502</span>'
    '<a href="NODE_LINK_PLACEHOLDER" class="console-banner-node"'
    ' aria-label="Node: NODE_ID_PLACEHOLDER">'
    "NODE_ID_PLACEHOLDER</a>"
    "</div>"
)

# Injected <style>: offsets fixed-position overlays + styles the console
# return-banner against the server UI's existing design tokens.  The
# banner uses the shared .ts-header-back-link class (defined in
# shared_static/chat.css, loaded by the interactive UI) so both the
# coordinator-page back-link and this banner present identical back-to-
# console affordances.  Only the banner-local layout bits (sep + node
# link typography) stay scoped here.
_CONSOLE_PROXY_STYLE = (
    "<style>"
    ".dashboard-overlay{top:32px!important}"
    ".console-banner{background:var(--bg-surface);"
    "border-bottom:1px solid var(--border-strong);"
    "padding:4px 20px;font-family:var(--font-mono);font-size:11px;"
    "display:flex;align-items:center;gap:8px;position:relative;z-index:200}"
    ".console-banner-sep{color:var(--fg-dim);opacity:0.6}"
    ".console-banner-node{color:var(--fg-dim);text-decoration:none;"
    "font-size:10px;letter-spacing:0.02em}"
    ".console-banner-node:hover{color:var(--accent)}"
    "</style>"
)


_VALID_NODE_ID = re.compile(r"^[a-zA-Z0-9._-]+$")
_VALID_WS_ID_RE = re.compile(r"^[a-f0-9]{1,64}$")

_PROXY_JWT_EXPIRY_SECONDS = 300  # 5 min — ample for any request round-trip


_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")


async def _bounded_stream_preview(response: httpx.Response, cap: int = 200) -> str:
    """Streaming counterpart of :func:`_bounded_body_preview`.

    Iterates ``response.aiter_bytes`` up to ``cap * 1.3`` bytes (enough
    for ``cap`` chars post-UTF-8 decode) so a compromised / oversized
    upstream can't force the proxy to buffer an arbitrary error page
    just to populate a preview.  Shares the control-char scrub with
    the non-streaming helper so the output shape is identical across
    sites.
    """
    preview_chunks: list[bytes] = []
    read = 0
    byte_cap = int(cap * 1.3) + 1
    try:
        async for chunk in response.aiter_bytes():
            if not chunk:
                continue
            remaining = byte_cap - read
            if remaining <= 0:
                break
            preview_chunks.append(chunk[:remaining])
            read += len(preview_chunks[-1])
            if read >= byte_cap:
                break
    except Exception:
        return "<unreadable>"
    decoded = b"".join(preview_chunks).decode("utf-8", "replace")
    return _CONTROL_CHAR_RE.sub(" ", decoded)[:cap]


def _bounded_body_preview(text: str | bytes, cap: int = 200) -> str:
    """Return a body preview for 4xx logs capped at ``cap`` chars.

    Body is already in memory on a non-streaming httpx response; this
    helper exists so every call-site produces the same shape.

    Control characters (CR, LF, NUL, ...) are replaced with spaces
    before the cap: the preview flows into both ``log.warning`` records
    and the operator-facing 503 ``collector_scope_error`` body, and
    upstream-controlled newlines in either surface would let a
    compromised node forge additional log lines or masquerade as
    embedded remediation text.
    """
    if not text:
        return ""
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    return _CONTROL_CHAR_RE.sub(" ", text)[:cap]


def _proxy_auth_headers(request: Request) -> dict[str, str]:
    """Build auth headers for proxied requests to upstream servers.

    Mints a short-lived JWT carrying the real user's identity and scopes
    so the upstream server records correct audit attribution and enforces
    scope narrowing.  Falls back to the ServiceTokenManager when no user
    context is available.

    When the inbound request authenticated with a coordinator-minted JWT
    (``auth_result.token_source == "coordinator"``), the re-mint
    preserves that source AND the ``coord_ws_id`` custom claim so
    upstream audit rows retain coordinator-origin visibility.  For all
    other inbound sources the re-mint uses ``"console-proxy"`` as before.
    """
    auth_result = getattr(getattr(request, "state", None), "auth_result", None)
    jwt_secret: str = getattr(request.app.state, "jwt_secret", "")

    if auth_result is not None and auth_result.user_id and jwt_secret:
        # Preserve coordinator-origin source on re-mint — otherwise
        # every upstream call from a coordinator session would be
        # indistinguishable from a human-originated console proxy call.
        is_coord = auth_result.token_source == "coordinator"
        source = "coordinator" if is_coord else "console-proxy"
        extra: dict[str, Any] = {}
        if is_coord:
            coord_ws_id = auth_result.extra_claims.get("coord_ws_id")
            if coord_ws_id:
                extra["coord_ws_id"] = coord_ws_id
        token = create_jwt(
            user_id=auth_result.user_id,
            scopes=auth_result.scopes,
            source=source,
            secret=jwt_secret,
            audience=JWT_AUD_SERVER,
            permissions=auth_result.permissions,
            expiry_seconds=_PROXY_JWT_EXPIRY_SECONDS,
            extra_claims=extra or None,
        )
        return {"Authorization": f"Bearer {token}"}

    # Fallback: service identity via ServiceTokenManager.
    mgr = getattr(request.app.state, "proxy_token_mgr", None)
    if mgr is not None:
        return dict(mgr.bearer_header)

    return {}


# Action-name map for the routing proxy.  See ``turnstone/core/audit.py``
# module docstring for the canonical action-namespace registry.
_ROUTE_PROXY_AUDIT_ACTIONS: dict[str, str] = {
    "send": "route.workstream.send",
    "approve": "route.approve",
    "cancel": "route.cancel",
    "command": "route.command",
    "plan": "route.plan",
    "close": "route.workstream.close",
}


def _emit_route_audit(
    request: Request,
    action: str,
    ws_id: str,
    node_id: str,
) -> None:
    """Record an audit event for a successful routing-proxy hop.

    Caller must ensure the upstream response was 2xx; auditing failures
    is deferred (4xx/5xx are observable via ``_record_route``'s metrics
    path).  Reads the inbound ``auth_result`` directly so that
    coordinator-origin attribution lands in ``detail.src`` without
    relying on the ``_proxy_auth_headers`` re-mint.

    ``detail`` carries ``{src, node_id, coord_ws_id?}`` — ``coord_ws_id``
    only when the inbound JWT carried it (i.e. the call originated from
    a coordinator session).  Failures are swallowed; the proxied
    response must never break because of an audit-emission bug.
    """
    storage = getattr(request.app.state, "auth_storage", None)
    if storage is None:
        return
    auth = getattr(getattr(request, "state", None), "auth_result", None)
    user_id: str = (getattr(auth, "user_id", "") or "") if auth is not None else ""
    src: str = (getattr(auth, "token_source", "") or "") if auth is not None else ""
    coord_ws_id: str = ""
    if auth is not None:
        coord_ws_id = (getattr(auth, "extra_claims", None) or {}).get("coord_ws_id", "") or ""
    detail: dict[str, Any] = {"src": src, "node_id": node_id}
    if coord_ws_id:
        detail["coord_ws_id"] = coord_ws_id
    try:
        from turnstone.core.audit import record_audit

        record_audit(
            storage,
            user_id,
            action,
            "workstream",
            ws_id,
            detail,
            request.client.host if request.client else "",
        )
    except Exception:
        log.debug("route.audit_failed action=%s", action, exc_info=True)


def _get_server_url(request: Request, node_id: str) -> str | None:
    """Resolve node_id to its server_url via the collector."""
    if not node_id or not _VALID_NODE_ID.match(node_id) or len(node_id) > 256:
        return None
    collector: ClusterCollector = request.app.state.collector
    detail = collector.get_node_detail(node_id)
    if detail and detail.get("server_url"):
        url: str = detail["server_url"]
        return url.rstrip("/")
    return None


def _pick_best_node(collector: ClusterCollector) -> str:
    """Select the reachable node with the most available capacity."""
    nodes = collector.get_all_nodes()
    best_id = ""
    best_headroom = -1
    for n in nodes:
        if not n.get("reachable", False):
            continue
        headroom = n.get("max_ws", 10) - n.get("ws_total", 0)
        if headroom > best_headroom:
            best_headroom = headroom
            best_id = n["node_id"]
    return best_id


# ---------------------------------------------------------------------------
# Route handlers — dashboard
# ---------------------------------------------------------------------------


async def index(request: Request) -> Response:
    if request.headers.get("If-None-Match") == _HTML_ETAG:
        return Response(status_code=304, headers={"ETag": _HTML_ETAG, "Cache-Control": "no-cache"})
    resp = HTMLResponse(_HTML)
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["ETag"] = _HTML_ETAG
    return resp


async def cluster_overview(request: Request) -> JSONResponse:
    collector: ClusterCollector = request.app.state.collector
    return JSONResponse(collector.get_overview())


async def cluster_nodes(request: Request) -> JSONResponse:
    collector: ClusterCollector = request.app.state.collector
    params = dict(request.query_params)
    sort_by = params.get("sort", "activity")
    limit = _parse_int(params, "limit", 100, minimum=1, maximum=1000)
    offset = _parse_int(params, "offset", 0)

    # Extract meta.* filters for node metadata filtering
    meta_filters = {k[5:]: v for k, v in params.items() if k.startswith("meta.") and k[5:]}
    node_ids: set[str] | None = None
    if meta_filters:
        import json as _mf_json

        storage = getattr(request.app.state, "auth_storage", None)
        if storage is not None:
            # Values in the DB are JSON-encoded.  Try to use raw value if it is
            # already valid JSON (e.g. meta.cpu_count=4), otherwise wrap as string.
            encoded = {}
            for mk, mv in meta_filters.items():
                try:
                    _mf_json.loads(mv)
                    encoded[mk] = mv
                except (ValueError, TypeError):
                    encoded[mk] = _mf_json.dumps(mv)
            try:
                node_ids = storage.filter_nodes_by_metadata(encoded)
            except Exception:
                log.warning("cluster.metadata_filter_failed", exc_info=True)
                node_ids = None  # fall back to unfiltered
        if node_ids is not None and not node_ids:
            return JSONResponse({"nodes": [], "total": 0})

    nodes, total = collector.get_nodes(
        sort_by=sort_by, limit=limit, offset=offset, node_ids=node_ids
    )
    return JSONResponse({"nodes": nodes, "total": total})


def _coordinator_rows(request: Request) -> list[dict[str, Any]]:
    """Build per-coordinator dashboard rows for cluster_workstreams.

    Coordinators live on the console process, not on a cluster node, so
    they aren't represented in the collector's node SSE streams.  Merge
    them into the cluster view so the dashboard tree grouping can nest
    spawned children under their coordinator parent.

    Sources two lanes and merges by ws_id:

    - **In-memory** via :meth:`SessionManager.list_all` — carries live
      session state (model / model_alias / current workstream state)
      for currently-loaded coordinators.
    - **Persisted** via ``storage.list_workstreams(kind=COORDINATOR)``
      — includes closed / error / soft-deleted rows the manager has
      evicted from memory.  Without this, closed coordinators
      disappeared from the landing page the moment ``close`` fired.

    In-memory wins on ws_id conflict so live state stays authoritative
    for active sessions.

    Trusted-team visibility (post-#400): the cluster dashboard shows
    every coordinator regardless of caller identity; ``user_id`` is
    surfaced on each row as display metadata.
    """
    coord_mgr = getattr(request.app.state, "coord_mgr", None)
    if coord_mgr is None:
        return []
    try:
        wss = coord_mgr.list_all()
    except Exception:
        log.debug("cluster_workstreams.coord_list_failed", exc_info=True)
        return []

    def _str_sess_attr(sess: Any, name: str) -> str:
        val = getattr(sess, name, "") if sess else ""
        return val if isinstance(val, str) else ""

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for ws in wss:
        sess = getattr(ws, "session", None)
        rows.append(
            {
                "id": ws.id,
                "name": ws.name,
                "state": ws.state.value,
                "title": "",
                "node": "console",
                "server_url": "",
                "model": _str_sess_attr(sess, "model"),
                "model_alias": _str_sess_attr(sess, "model_alias"),
                "tokens": 0,
                "context_ratio": 0.0,
                "activity": "",
                "activity_state": "",
                "tool_calls": 0,
                "kind": WorkstreamKind.COORDINATOR.value,
                "parent_ws_id": None,
                "user_id": ws.user_id or "",
            }
        )
        seen.add(ws.id)

    # Second lane — persisted coordinator rows, used to surface
    # closed / error / deleted coordinators the manager has already
    # evicted from ``self._workstreams``.  Cluster-wide (trusted-team
    # visibility).
    storage = getattr(request.app.state, "auth_storage", None)
    if storage is None:
        return rows
    try:
        persisted = storage.list_workstreams(
            kind=WorkstreamKind.COORDINATOR,
            user_id=None,
            limit=200,
        )
    except Exception:
        log.debug("cluster_workstreams.coord_persisted_failed", exc_info=True)
        return rows

    for row in persisted:
        # SQLAlchemy Row — access via _mapping so future SELECT reorders
        # / new columns don't silently corrupt the projection (per the
        # storage-protocol guidance on list_workstreams).  Test doubles
        # must expose the same ._mapping attribute; positional indexing
        # was removed because it hard-coded column offsets that drift
        # with migrations.
        m = row._mapping
        row_id = m.get("ws_id") or ""
        if not row_id or row_id in seen:
            continue
        row_owner = m.get("user_id") or ""
        rows.append(
            {
                "id": row_id,
                "name": m.get("name") or f"coord-{row_id[:4]}",
                "state": str(m.get("state") or "idle"),
                "title": "",
                "node": "console",
                "server_url": "",
                "model": "",
                "model_alias": "",
                "tokens": 0,
                "context_ratio": 0.0,
                "activity": "",
                "activity_state": "",
                "tool_calls": 0,
                "kind": WorkstreamKind.COORDINATOR.value,
                "parent_ws_id": None,
                "user_id": row_owner,
            }
        )
    return rows


async def cluster_workstreams(request: Request) -> JSONResponse:
    collector: ClusterCollector = request.app.state.collector
    params = dict(request.query_params)
    state = params.get("state")
    node = params.get("node")
    search = params.get("search")
    sort_by = params.get("sort", "state")
    page = _parse_int(params, "page", 1, minimum=1)
    per_page = _parse_int(params, "per_page", 50, minimum=1, maximum=200)
    extra_rows = _coordinator_rows(request)
    ws_list, total = collector.get_workstreams(
        state=state,
        node=node,
        search=search,
        sort_by=sort_by,
        page=page,
        per_page=per_page,
        extra_rows=extra_rows,
    )
    pages = math.ceil(total / per_page) if per_page > 0 else 0
    return JSONResponse(
        {
            "workstreams": ws_list,
            "total": total,
            "page": page,
            "per_page": per_page,
            "pages": pages,
        }
    )


_CLUSTER_WS_LIVE_KEYS = (
    "state",
    "tokens",
    "context_ratio",
    "activity",
    "activity_state",
    "tool_calls",
    "model",
    "model_alias",
    "title",
    "name",
)


class _NodeDashboardCache:
    """Short-TTL per-node ``/v1/api/dashboard`` response cache.

    Prevents the O(N·M) fan-in that cluster_ws_detail would otherwise
    produce when a coordinator inspects N children hosted on a handful
    of nodes each serving M workstreams: one concurrent fetch per node
    per TTL window, de-duplicated via a per-node asyncio.Lock so a
    burst of concurrent requests collapses into a single upstream call.

    TTL is intentionally short (2s) — dashboard data is used for
    live-badge rendering where a 1–2s lag is acceptable.  Bounded cache
    size is unnecessary in practice: node_id count scales with cluster
    size (typically O(10–100)), not with request volume.
    """

    _TTL_SECONDS = 2.0

    def __init__(self) -> None:
        self._cache: dict[str, tuple[float, dict[str, Any] | None]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_lock = asyncio.Lock()

    async def get(
        self,
        node_id: str,
        server_url: str,
        client: httpx.AsyncClient,
        headers: dict[str, str],
    ) -> dict[str, Any] | None:
        now = time.monotonic()
        cached = self._cache.get(node_id)
        if cached is not None and now - cached[0] < self._TTL_SECONDS:
            return cached[1]
        async with self._locks_lock:
            lock = self._locks.get(node_id)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[node_id] = lock
        async with lock:
            # Re-check — a coalesced peer may have populated while we
            # waited on the per-node lock.
            cached = self._cache.get(node_id)
            if cached is not None and time.monotonic() - cached[0] < self._TTL_SECONDS:
                return cached[1]
            payload: dict[str, Any] | None = None
            try:
                resp = await client.get(
                    f"{server_url}/v1/api/dashboard",
                    headers=headers,
                    timeout=2.0,
                )
            except (httpx.HTTPError, TimeoutError):
                resp = None
            if resp is not None:
                status = resp.status_code
                if 200 <= status < 300:
                    try:
                        raw = resp.json()
                    except (ValueError, json.JSONDecodeError):
                        raw = None
                    if isinstance(raw, dict):
                        payload = raw
                elif 400 <= status < 500:
                    # 4xx on the /dashboard fetch means the caller's
                    # JWT (user or service-token fallback) lacks the
                    # required scopes — surface at WARNING so the
                    # drift doesn't hide behind a silent empty
                    # dashboard.  Return without caching so an
                    # operator scope fix is visible on the next
                    # request instead of after the TTL window; the
                    # per-node lock above is the hot-loop guard.
                    log.warning(
                        "proxy.dashboard_cache.4xx node=%s status=%d url=%s body=%s",
                        node_id,
                        status,
                        server_url,
                        _bounded_body_preview(resp.text),
                    )
                    return None
            self._cache[node_id] = (time.monotonic(), payload)
            return payload


def _coordinator_live_snapshot(ws: Any) -> dict[str, Any]:
    """Build a ``live`` block for an in-process coordinator workstream.

    Mirrors the shape a node's ``/v1/api/dashboard`` would produce for a
    workstream entry so the cluster-inspect merge is source-independent
    (the UI can't tell a coordinator's live block from a node's).  The
    ``pending_approval`` derived field is set to match the node branch
    (see :func:`_fetch_live_block`) — both origins must produce the same
    keys or the UI can't reliably read the flag.
    """
    sess = getattr(ws, "session", None)
    ui = getattr(ws, "ui", None)
    # `_pending_approval` is set precisely when an approval is actively
    # being awaited (populated by approve_tools, cleared by the resolve
    # path).  A freshly-constructed UI has `_pending_approval=None` and
    # `_approval_event` in its default unset state — the earlier
    # implementation read `not _approval_event.is_set()` as the signal
    # which fired True on every new coordinator, making the flag useless.
    pending_approval = ui is not None and getattr(ui, "_pending_approval", None) is not None

    def _str_attr(obj: Any, name: str) -> str:
        val = getattr(obj, name, "") if obj else ""
        return val if isinstance(val, str) else ""

    return {
        "state": ws.state.value if hasattr(ws.state, "value") else str(ws.state),
        "tokens": 0,
        "context_ratio": 0.0,
        "activity": "",
        "activity_state": "approval" if pending_approval else "",
        "tool_calls": 0,
        "model": _str_attr(sess, "model"),
        "model_alias": _str_attr(sess, "model_alias"),
        "title": "",
        "name": getattr(ws, "name", "") or "",
        "pending_approval": pending_approval,
    }


async def _fetch_live_block(
    request: Request, row: dict[str, Any], ws_id: str
) -> dict[str, Any] | None:
    """Fetch the per-workstream ``live`` block for cluster-inspect.

    For coordinator-hosted workstreams, read live state from the
    in-process :class:`SessionManager` (coordinator kind).  For
    node-backed workstreams, issue a short-timeout HTTP GET against
    the owning node's ``/v1/api/dashboard`` and project the matching
    entry.

    Always returns ``None`` on coordinator-not-loaded, node unreachable,
    wrong status, un-parseable payload, or no matching entry — the
    caller surfaces this as ``live=null`` with a 200 response.  Any
    unexpected exception propagates to the caller's correlation-id
    handler; internal degradations stay silent.
    """
    row_node_id = row.get("node_id") or ""
    row_kind = WorkstreamKind.from_raw(row.get("kind"))

    # Kind is the authoritative discriminator; the `"console"` node_id
    # sentinel is paired with coordinator rows only (see
    # ``ClusterCollector.CONSOLE_PSEUDO_NODE_ID``).  Branching purely
    # on kind avoids a subtle collision if a real node ever registers
    # with ``node_id="console"``.
    if row_kind == WorkstreamKind.COORDINATOR:
        coord_mgr = getattr(request.app.state, "coord_mgr", None)
        if coord_mgr is None:
            return None
        ws = coord_mgr.get(ws_id)
        if ws is None:
            return None
        return _coordinator_live_snapshot(ws)

    server_url = _get_server_url(request, row_node_id)
    if not server_url:
        return None
    client: httpx.AsyncClient = request.app.state.proxy_client
    # Route through the per-node dashboard cache — N concurrent
    # cluster_ws_detail calls to children on the same node collapse
    # to one upstream GET per 2s TTL window instead of N full
    # /dashboard fetches per call.
    cache = getattr(request.app.state, "dashboard_cache", None)
    if cache is not None:
        payload = await cache.get(row_node_id, server_url, client, _proxy_auth_headers(request))
    else:
        # Test harnesses / legacy embeddings may skip the cache; fall
        # back to the direct fetch so this function still works.
        try:
            resp = await client.get(
                f"{server_url}/v1/api/dashboard",
                headers=_proxy_auth_headers(request),
                timeout=2.0,
            )
        except (httpx.HTTPError, TimeoutError):
            return None
        status = resp.status_code
        if 400 <= status < 500:
            # 4xx on the direct /dashboard fetch is the same class
            # of auth/scope drift as the cached path above — surface
            # at WARNING so operators see it in ops logs.
            log.warning(
                "proxy.live_block.4xx node=%s status=%d url=%s body=%s",
                row_node_id,
                status,
                server_url,
                _bounded_body_preview(resp.text),
            )
            return None
        if not (200 <= status < 300):
            return None
        try:
            raw = resp.json()
        except (ValueError, httpx.HTTPError):
            return None
        payload = raw if isinstance(raw, dict) else None
    if payload is None:
        return None
    for entry in payload.get("workstreams", []) or []:
        if isinstance(entry, dict) and entry.get("id") == ws_id:
            live = {k: entry.get(k) for k in _CLUSTER_WS_LIVE_KEYS if k in entry}
            # Derived field — kept in lockstep with
            # _coordinator_live_snapshot so both origins produce the
            # same keys.
            live["pending_approval"] = live.get("activity_state") == "approval"
            return live
    return None


async def cluster_ws_detail(request: Request) -> JSONResponse:
    """GET /v1/api/cluster/ws/{ws_id}/detail — persisted row + live merge.

    Gated on ``admin.cluster.inspect``.  Aggregates the workstream's stored
    row with its live state from the owning node's ``/v1/api/dashboard``
    (for node-backed workstreams) or the in-process :class:`SessionManager`
    (for ``kind="coordinator"`` rows).

    Behavior:

    - ``persisted`` — the full ``get_workstream`` row (never ``None``).
    - ``live`` — live fields merged from the owning node, or ``null`` when
      the node is unreachable / the workstream isn't on its roster / the
      coordinator isn't in memory.  The caller always sees ``persisted``
      populated and a 200 response; node unreachability is signalled by
      ``live=null`` rather than an error status.
    - ``messages`` — tail-N conversation messages, default 20, capped at 200.

    404 masks ownership failures (match ``coordinator_detail``).
    Correlation-id masks unexpected exceptions in the merge path.
    """
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    err = require_permission(request, "admin.cluster.inspect")
    if err is not None:
        return err
    storage, err503 = require_storage_or_503(request)
    if err503 is not None:
        return err503

    ws_id = request.path_params.get("ws_id", "")
    if not _VALID_WS_ID_RE.match(ws_id):
        return JSONResponse({"error": "invalid ws_id"}, status_code=400)

    # Accept either ``?limit=`` (the canonical name used by
    # coordinator_history and the list_workstreams tool) or the
    # transitional ``?message_limit=`` from earlier phase-3 drafts.
    # ``?limit`` wins when both are set so callers migrating from the
    # older name can overlap without surprise.
    try:
        raw_limit = request.query_params.get("limit")
        if raw_limit is None:
            raw_limit = request.query_params.get("message_limit", "20")
        limit = int(raw_limit)
    except (TypeError, ValueError):
        limit = 20
    limit = max(0, min(limit, 200))

    # Offload the sync DB fetch to the default executor so the SSE event
    # loop doesn't stall on a slow query.  This handler is hit once per
    # child-row state tick in the tree UI (debounced 250ms, cached 5s
    # client-side) and cluster_ws_detail shares the event loop with
    # every coordinator's SSE stream.
    try:
        row = await asyncio.to_thread(storage.get_workstream, ws_id)
    except Exception:
        correlation_id = secrets.token_hex(4)
        log.warning(
            "cluster_ws_detail.storage_failed correlation_id=%s ws_id=%s",
            correlation_id,
            ws_id[:8],
            exc_info=True,
        )
        return JSONResponse(
            {
                "error": (
                    f"failed to read workstream (internal error). correlation_id={correlation_id}"
                )
            },
            status_code=500,
        )

    if row is None:
        return JSONResponse({"error": "workstream not found"}, status_code=404)

    try:
        live = await _fetch_live_block(request, row, ws_id)
    except Exception:
        correlation_id = secrets.token_hex(4)
        log.warning(
            "cluster_ws_detail.live_merge_failed correlation_id=%s ws_id=%s",
            correlation_id,
            ws_id[:8],
            exc_info=True,
        )
        live = None

    messages: list[dict[str, Any]] = []
    if limit > 0:
        try:
            # Tail-N bound pushed into SQL (load_messages supports limit
            # on both backends).  Offloaded to the default executor so
            # the async SSE loop stays unblocked under rapid fan-out.
            messages = await asyncio.to_thread(storage.load_messages, ws_id, limit=limit)
        except Exception:
            log.debug("cluster_ws_detail.load_messages_failed", exc_info=True)

    return JSONResponse(
        {
            "persisted": row,
            "live": live,
            "messages": messages,
        }
    )


# Upper bound on ids per bulk request.  Matches the per-coordinator
# fanout cap + leaves headroom; larger batches would defeat the per-node
# /v1/api/dashboard cache's batching benefit once the id set spans many
# nodes, at which point the caller should paginate client-side.
_CLUSTER_WS_LIVE_BULK_CAP = 50


async def cluster_ws_live_bulk(request: Request) -> JSONResponse:
    """GET /v1/api/cluster/ws/live?ids=a,b,c — bulk live-block fetch.

    Returns ``{results: {ws_id: live | null}, denied: [ws_id, ...],
    truncated: bool}``.

    Collapses the per-row fan-out that tree UIs with 30+ visible
    children produce — one HTTP round-trip per TTL window instead of
    one-per-row.  Reuses the same ``_fetch_live_block`` path as
    ``cluster_ws_detail`` so node-dashboard cache behaviour, coordinator
    in-process snapshots, and ownership masking stay consistent.

    Permission + ownership semantics match ``cluster_ws_detail``:
    gated on ``admin.cluster.inspect`` and rows the caller doesn't
    own surface in ``denied`` rather than ``results`` (so the endpoint
    can't be used as an existence oracle).  Missing ids also route to
    ``denied`` for the same reason.  ``ids`` over the cap is truncated
    with ``truncated=true`` so the model / frontend knows to paginate.
    """
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    err = require_permission(request, "admin.cluster.inspect")
    if err is not None:
        return err
    storage, err503 = require_storage_or_503(request)
    if err503 is not None:
        return err503

    raw_ids = request.query_params.get("ids", "") or ""
    # Split on comma; strip whitespace; drop empty / invalid entries.
    # Dedupe while preserving order so a caller passing the same id
    # twice doesn't double-bill the round-trip budget.
    seen: set[str] = set()
    cleaned: list[str] = []
    for chunk in raw_ids.split(","):
        wid = chunk.strip()
        if not wid or not _VALID_WS_ID_RE.match(wid):
            continue
        if wid in seen:
            continue
        seen.add(wid)
        cleaned.append(wid)
    truncated = False
    if len(cleaned) > _CLUSTER_WS_LIVE_BULK_CAP:
        truncated = True
        cleaned = cleaned[:_CLUSTER_WS_LIVE_BULK_CAP]
    if not cleaned:
        return JSONResponse({"results": {}, "denied": [], "truncated": False})

    try:
        rows = await asyncio.to_thread(storage.get_workstreams_batch, cleaned)
    except Exception:
        correlation_id = secrets.token_hex(4)
        log.warning(
            "cluster_ws_live_bulk.storage_failed correlation_id=%s count=%d",
            correlation_id,
            len(cleaned),
            exc_info=True,
        )
        return JSONResponse(
            {"error": f"storage error (internal). correlation_id={correlation_id}"},
            status_code=500,
        )

    results: dict[str, dict[str, Any] | None] = {}
    denied: list[str] = []
    owned_rows: list[tuple[str, dict[str, Any]]] = []
    for wid in cleaned:
        row = rows.get(wid)
        if row is None:
            # Missing rows route to ``denied`` rather than ``results``
            # so the endpoint can't be used as an existence oracle for
            # ids outside the caller's knowledge.
            denied.append(wid)
            continue
        owned_rows.append((wid, row))

    # Fetch live blocks concurrently — ``_fetch_live_block`` already
    # routes node-backed reads through the per-node dashboard cache,
    # so N concurrent fetches against the same node collapse to a
    # single upstream call per TTL window.
    async def _one(wid: str, row: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
        try:
            live = await _fetch_live_block(request, row, wid)
        except Exception:
            log.debug(
                "cluster_ws_live_bulk.one_failed ws=%s",
                wid[:8],
                exc_info=True,
            )
            live = None
        return wid, live

    gathered = await asyncio.gather(*(_one(wid, row) for wid, row in owned_rows))
    for wid, live in gathered:
        results[wid] = live
    return JSONResponse({"results": results, "denied": denied, "truncated": truncated})


async def cluster_node_detail(request: Request) -> JSONResponse:
    collector: ClusterCollector = request.app.state.collector
    node_id = request.path_params["node_id"]
    nv = _validate_node_id(node_id)
    if nv:
        return nv
    detail = collector.get_node_detail(node_id)
    if not detail:
        return JSONResponse({"error": "Node not found"}, status_code=404)

    # Attach metadata if available
    import json as _nd_json

    storage = getattr(request.app.state, "auth_storage", None)
    if storage is not None:
        try:
            raw = storage.get_node_metadata(node_id)
            entries = []
            for r in raw:
                try:
                    val = _nd_json.loads(r["value"])
                except (ValueError, TypeError):
                    val = r["value"]
                entries.append({"key": r["key"], "value": val, "source": r["source"]})
            detail["metadata"] = entries
        except Exception:
            log.warning("cluster.node_metadata_load_failed node_id=%s", node_id, exc_info=True)
            detail["metadata"] = []
    else:
        detail["metadata"] = []
    return JSONResponse(detail)


def _collector_scope_error(request: Request) -> JSONResponse | None:
    """Return a 503 if the boot self-check detected collector scope drift.

    Used by cluster-wide data endpoints so they refuse to serve an
    empty dashboard when the operator's configuration is broken —
    a clear 503 with remediation text is better than rendering a
    blank table full of "missing data" bugs.
    """
    err = getattr(request.app.state, "collector_scope_error", "") or ""
    if err:
        return JSONResponse(
            {"error": err, "reason": "collector_scope_drift"},
            status_code=503,
        )
    return None


async def cluster_snapshot(request: Request) -> JSONResponse:
    err = _collector_scope_error(request)
    if err is not None:
        return err
    collector: ClusterCollector = request.app.state.collector
    return JSONResponse(collector.get_snapshot())


async def cluster_events_sse(request: Request) -> Response:
    err = _collector_scope_error(request)
    if err is not None:
        return err
    collector: ClusterCollector = request.app.state.collector
    client_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=2000)

    async def event_generator() -> AsyncGenerator[dict[str, str], None]:
        loop = asyncio.get_running_loop()
        try:
            # Atomic snapshot+register — no event gap possible.
            snap = await loop.run_in_executor(
                None, collector.get_snapshot_and_register, client_queue
            )
            snap["type"] = "snapshot"
            yield {"data": json.dumps(snap)}

            while True:
                try:
                    event = await loop.run_in_executor(
                        None, functools.partial(client_queue.get, timeout=5)
                    )
                    yield {"data": json.dumps(event)}
                except queue.Empty:
                    pass  # poll timeout, retry
                if await request.is_disconnected():
                    break
        finally:
            collector.unregister_listener(client_queue)

    return EventSourceResponse(event_generator(), ping=5)


async def health(request: Request) -> JSONResponse:
    collector: ClusterCollector = request.app.state.collector
    overview = collector.get_overview()
    return JSONResponse(
        {
            "status": "ok",
            "service": "turnstone-console",
            "nodes": overview["nodes"],
            "workstreams": overview["workstreams"],
            "version_drift": overview.get("version_drift", False),
            "versions": overview.get("versions", []),
        }
    )


async def console_metrics_endpoint(request: Request) -> Response:
    """GET /metrics — Prometheus text exposition format for console metrics."""
    cm: ConsoleMetrics = request.app.state.console_metrics
    text = cm.generate_text()
    return Response(text, media_type="text/plain; version=0.0.4; charset=utf-8")


async def auth_login(request: Request) -> Response:
    """Authenticate via username:password or legacy token, return JWT."""
    from turnstone.core.auth import handle_auth_login

    return await handle_auth_login(request, JWT_AUD_CONSOLE)


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

    return await handle_auth_setup(request, JWT_AUD_CONSOLE)


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

    return await handle_auth_refresh(request, JWT_AUD_CONSOLE)


async def oidc_authorize(request: Request) -> Response:
    """GET /v1/api/auth/oidc/authorize — redirect to OIDC provider."""
    from turnstone.core.auth import handle_oidc_authorize

    return await handle_oidc_authorize(request, JWT_AUD_CONSOLE)


async def oidc_callback(request: Request) -> Response:
    """GET /v1/api/auth/oidc/callback — OIDC callback, exchange code for JWT."""
    from turnstone.core.auth import handle_oidc_callback

    return await handle_oidc_callback(request, JWT_AUD_CONSOLE)


# ---------------------------------------------------------------------------
# Route handlers — available models (lightweight, no admin permission)
# ---------------------------------------------------------------------------


async def list_available_models(request: Request) -> JSONResponse:
    """GET /v1/api/models — enabled model aliases for workstream creation."""
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err

    rows = storage.list_model_definitions(enabled_only=True)
    # Only expose alias/model/provider — rows also contain api_key, base_url, etc.
    models = [{"alias": r["alias"], "model": r["model"], "provider": r["provider"]} for r in rows]

    # Include effective defaults for clients (web UI, channel gateway).
    default_alias = ""
    channel_default_alias = ""
    cs = getattr(request.app.state, "config_store", None)
    if cs is not None:
        default_alias = cs.get("model.default_alias") or ""
        channel_default_alias = cs.get("channels.default_model_alias") or ""
    enabled_aliases = {r["alias"] for r in rows}
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


# ---------------------------------------------------------------------------
# Route handlers — workstream creation
# ---------------------------------------------------------------------------


async def create_workstream(request: Request) -> JSONResponse:
    """POST /v1/api/cluster/workstreams/new — create a workstream via HTTP.

    Three targeting modes:
    - ``node_id`` set to a specific node ID → POST to that node
    - ``node_id`` omitted or ``"auto"`` → console picks the node with most headroom
    - ``node_id`` set to ``"pool"`` → console picks any available node
    """
    from turnstone.core.web_helpers import read_json_or_400

    body = await read_json_or_400(request)
    if isinstance(body, JSONResponse):
        return body

    collector: ClusterCollector = request.app.state.collector

    raw_node_id = body.get("node_id", "")
    raw_name = body.get("name", "")
    raw_model = body.get("model", "")
    raw_judge_model = body.get("judge_model", "")
    raw_initial_message = body.get("initial_message", "")
    raw_skill = body.get("skill", "")
    raw_resume_ws = body.get("resume_ws", "")
    if not isinstance(raw_node_id, str):
        raw_node_id = "" if raw_node_id is None else None
    if not isinstance(raw_name, str):
        raw_name = "" if raw_name is None else None
    if not isinstance(raw_model, str):
        raw_model = "" if raw_model is None else None
    if not isinstance(raw_judge_model, str):
        raw_judge_model = "" if raw_judge_model is None else None
    if not isinstance(raw_initial_message, str):
        raw_initial_message = "" if raw_initial_message is None else None
    if not isinstance(raw_skill, str):
        raw_skill = "" if raw_skill is None else None
    if not isinstance(raw_resume_ws, str):
        raw_resume_ws = "" if raw_resume_ws is None else None
    if (
        raw_node_id is None
        or raw_name is None
        or raw_model is None
        or raw_judge_model is None
        or raw_initial_message is None
        or raw_skill is None
        or raw_resume_ws is None
    ):
        return JSONResponse(
            {
                "error": "node_id, name, model, judge_model, initial_message, skill, and resume_ws must be strings"
            },
            status_code=400,
        )
    node_id = raw_node_id
    name = raw_name[:256]
    model = raw_model[:128]
    judge_model = raw_judge_model[:128]
    initial_message = raw_initial_message[:4096]
    skill = raw_skill[:256]
    resume_ws = raw_resume_ws[:64]

    auth = getattr(getattr(request, "state", None), "auth_result", None)
    uid: str = getattr(auth, "user_id", "") or ""

    # Pool — pick any available node
    if node_id == "pool":
        node_id = _pick_best_node(collector)
        if not node_id:
            return JSONResponse({"error": "No reachable nodes available"}, status_code=503)

    # Auto-select node by most available capacity
    if not node_id or node_id == "auto":
        node_id = _pick_best_node(collector)
        if not node_id:
            return JSONResponse({"error": "No reachable nodes available"}, status_code=503)

    # Validate node exists and get its URL
    detail = collector.get_node_detail(node_id)
    if not detail:
        return JSONResponse({"error": "Node not found"}, status_code=404)

    server_url = detail.get("server_url", "")
    if not server_url:
        return JSONResponse({"error": "Node has no URL"}, status_code=502)

    ws_body = {
        "name": name,
        "model": model,
        "judge_model": judge_model,
        "initial_message": initial_message,
        "skill": skill,
        "resume_ws": resume_ws,
        "user_id": uid,
    }

    client: httpx.AsyncClient = request.app.state.proxy_client
    headers = _proxy_auth_headers(request)
    try:
        resp = await client.post(
            f"{server_url.rstrip('/')}/v1/api/workstreams/new",
            json=ws_body,
            headers=headers,
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        log.warning("Workstream dispatch to %s failed: %s", node_id, exc)
        return JSONResponse({"error": f"Dispatch to node {node_id} failed"}, status_code=502)

    return JSONResponse(
        {
            "status": "ok",
            "correlation_id": resp.json().get("ws_id", ""),
            "target_node": node_id,
        }
    )


# ---------------------------------------------------------------------------
# Route handlers — workstream routing proxy (rendezvous)
# ---------------------------------------------------------------------------


def _record_route(
    request: Request, method: str, status: int, t0: float, resp: Response
) -> Response:
    """Record routing metrics and return the response unchanged."""
    cm: ConsoleMetrics | None = getattr(request.app.state, "console_metrics", None)
    if cm is not None:
        cm.record_route(method, status, time.monotonic() - t0)
    return resp


async def route_create(request: Request) -> Response:
    """POST /v1/api/route/workstreams/new — create via rendezvous routing.

    Accepts both `application/json` and `multipart/form-data`. Multipart
    callers must include ``?ws_id=<hex>`` in the URL query string so the
    console can hash to the owning node before the multipart body lands —
    we do not parse the body just to peek at the metadata.
    """
    t0 = time.monotonic()
    router: ConsoleRouter | None = request.app.state.router
    ring_ready = router is not None and router.is_ready()
    if not ring_ready:
        # Router cache empty — the collector hasn't published a
        # services list yet.  One-shot refresh off the event loop
        # before giving up.
        if router is not None:
            await asyncio.to_thread(router.refresh_cache)
            ring_ready = router.is_ready()
        if not ring_ready:
            return _record_route(
                request,
                "create",
                503,
                t0,
                JSONResponse(
                    {"error": "Cluster routing not initialized"},
                    status_code=503,
                ),
            )
    assert router is not None

    raw_content_type = request.headers.get("content-type") or ""
    is_multipart = raw_content_type.lower().startswith("multipart/form-data")
    client: httpx.AsyncClient = request.app.state.proxy_client
    headers = _proxy_auth_headers(request)
    pin = False
    body: dict[str, Any] = {}
    raw_body: bytes = b""
    # Routing strategy is surfaced on the response so callers (the
    # coordinator's spawn_workstream tool especially) can explain why a
    # given node was chosen.  Set on every branch below.
    routing_strategy = "rendezvous"

    if is_multipart:
        # Multipart: caller must pass ws_id as a query param so we can
        # route without parsing the body.  Stream the raw bytes through
        # to the upstream so we don't lose the multipart framing.
        ws_id = request.query_params.get("ws_id", "").strip()
        if not ws_id:
            return _record_route(
                request,
                "create",
                400,
                t0,
                JSONResponse(
                    {"error": "ws_id query parameter required for multipart create"},
                    status_code=400,
                ),
            )
        try:
            ref = router.route(ws_id)
        except NoAvailableNodeError:
            return _record_route(
                request,
                "create",
                503,
                t0,
                JSONResponse(
                    {"error": "No available node for routing"},
                    status_code=503,
                ),
            )
        # Multipart callers pre-allocate ws_id (typically an attachment
        # follow-up against an existing workstream) — same hash-of-known-id
        # path resume_ws takes on the JSON branch.
        routing_strategy = "resume"
        raw_body = await request.body()
        # Forward the raw header verbatim — the multipart `boundary=` parameter
        # is case-sensitive and must match the bytes in the body exactly.
        upstream_headers = {**headers, "Content-Type": raw_content_type}
        try:
            resp = await client.post(
                f"{ref.url}/v1/api/workstreams/new",
                content=raw_body,
                headers=upstream_headers,
            )
        except httpx.HTTPError:
            return _record_route(
                request,
                "create",
                502,
                t0,
                JSONResponse(
                    {"error": f"upstream node {ref.node_id} unreachable"},
                    status_code=502,
                ),
            )
    else:
        try:
            body = await request.json()
        except Exception:
            return _record_route(
                request,
                "create",
                400,
                t0,
                JSONResponse(
                    {"error": "Invalid JSON body"},
                    status_code=400,
                ),
            )
        try:
            if body.get("resume_ws"):
                ref = router.route(body["resume_ws"])
                routing_strategy = "resume"
            elif body.get("target_node"):
                # Brute-force HRW search can take up to _GENERATE_ATTEMPT_CAP
                # iterations for skewed weights; off the event loop.
                ws_id = await asyncio.to_thread(router.generate_ws_id_for_node, body["target_node"])
                body["ws_id"] = ws_id
                ref = router.route(ws_id)
                pin = True
                routing_strategy = "target_node"
            else:
                ws_id = secrets.token_hex(16)
                body["ws_id"] = ws_id
                ref = router.route(ws_id)
        except NoAvailableNodeError:
            return _record_route(
                request,
                "create",
                503,
                t0,
                JSONResponse(
                    {"error": "No available node for routing"},
                    status_code=503,
                ),
            )

        try:
            resp = await client.post(
                f"{ref.url}/v1/api/workstreams/new", json=body, headers=headers
            )
        except httpx.HTTPError:
            return _record_route(
                request,
                "create",
                502,
                t0,
                JSONResponse(
                    {"error": f"upstream node {ref.node_id} unreachable"},
                    status_code=502,
                ),
            )

        # 503 retry with a new ws_id that hashes to a different node.
        # Multipart variant skips this branch — the body is bound to the
        # ws_id the caller chose, so re-routing would mean re-uploading.
        if resp.status_code == 503 and not pin and not body.get("resume_ws"):
            failed_node = ref.node_id
            found_alt = False
            for _ in range(10):
                ws_id = secrets.token_hex(16)
                try:
                    ref = router.route(ws_id)
                except NoAvailableNodeError:
                    break
                if ref.node_id != failed_node:
                    found_alt = True
                    break
            if not found_alt:
                return _record_route(
                    request,
                    "create",
                    resp.status_code,
                    t0,
                    Response(
                        content=resp.content,
                        status_code=resp.status_code,
                        headers=dict(resp.headers),
                    ),
                )
            body["ws_id"] = ws_id
            try:
                resp = await client.post(
                    f"{ref.url}/v1/api/workstreams/new", json=body, headers=headers
                )
            except httpx.HTTPError:
                return _record_route(
                    request,
                    "create",
                    502,
                    t0,
                    JSONResponse(
                        {"error": f"upstream node {ref.node_id} unreachable"},
                        status_code=502,
                    ),
                )

    if resp.status_code == 200:
        data = resp.json()
        data["node_url"] = ref.url
        # Audit attribution — multipart sets ``ws_id`` from the query
        # string; JSON sets it on the body (or carries ``resume_ws``
        # for a rehydrate).  Either way, this is the workstream the
        # caller actually landed on.
        if is_multipart:
            audit_ws_id = ws_id
        else:
            audit_ws_id = body.get("ws_id") or body.get("resume_ws", "") or ""
        # Return the storage-authoritative node_id so subsequent
        # inspect / list calls agree on the binding.  ``ref.node_id`` is
        # the rendezvous target AT SPAWN TIME — stale once membership
        # changes — and the node's own create handler is the source of
        # truth for what node_id got persisted on the workstream row.
        # Fall back to ref.node_id only when the storage lookup fails,
        # matching the previous behaviour so this change is strictly
        # additive.
        bound_node_id = ref.node_id
        storage = getattr(request.app.state, "auth_storage", None)
        if storage is not None and audit_ws_id:
            try:
                row = storage.get_workstream(audit_ws_id)
                stored_node = row.get("node_id") if isinstance(row, dict) else None
                if isinstance(stored_node, str) and stored_node:
                    bound_node_id = stored_node
            except Exception:
                log.debug(
                    "route_create.node_id_lookup_failed ws=%s",
                    audit_ws_id[:8] if audit_ws_id else "",
                    exc_info=True,
                )
        data["node_id"] = bound_node_id
        data["routing_strategy"] = routing_strategy
        _emit_route_audit(request, "route.workstream.create", audit_ws_id, bound_node_id)
        return _record_route(request, "create", 200, t0, JSONResponse(data))
    return _record_route(
        request,
        "create",
        resp.status_code,
        t0,
        Response(
            content=resp.content,
            status_code=resp.status_code,
            headers=dict(resp.headers),
        ),
    )


async def route_attachment_proxy(request: Request) -> Response:
    """Proxy ws-id-keyed attachment endpoints through the router.

    Handles all four shapes mounted under
    ``/v1/api/route/workstreams/{ws_id}/attachments[/...]``:

    - ``POST .../attachments`` — multipart upload (raw-body forward)
    - ``GET  .../attachments`` — list pending (JSON pass-through)
    - ``GET  .../attachments/{attachment_id}/content`` — raw bytes
    - ``DELETE .../attachments/{attachment_id}`` — JSON pass-through

    All variants forward ``Content-Type`` + auth headers so multipart
    framing survives, and propagate upstream response headers so the
    ``Content-Disposition`` / ``X-Content-Type-Options`` set by
    ``get_attachment_content`` reach the original caller intact.
    """
    method = "attach"
    t0 = time.monotonic()
    router: ConsoleRouter | None = request.app.state.router
    ring_ready = router is not None and router.is_ready()
    if not ring_ready:
        if router is not None:
            await asyncio.to_thread(router.refresh_cache)
            ring_ready = router.is_ready()
        if not ring_ready:
            return _record_route(
                request,
                method,
                503,
                t0,
                JSONResponse(
                    {"error": "Cluster routing not initialized"},
                    status_code=503,
                ),
            )
    assert router is not None

    ws_id = request.path_params.get("ws_id", "").strip()
    if not ws_id:
        return _record_route(
            request,
            method,
            400,
            t0,
            JSONResponse({"error": "ws_id required"}, status_code=400),
        )
    try:
        ref = router.route(ws_id)
    except (NoAvailableNodeError, ValueError):
        return _record_route(
            request,
            method,
            503,
            t0,
            JSONResponse({"error": "routing failed"}, status_code=503),
        )

    upstream_path = request.url.path.replace("/api/route/", "/api/", 1)
    if request.url.query:
        upstream_path += f"?{request.url.query}"

    client: httpx.AsyncClient = request.app.state.proxy_client
    headers = _proxy_auth_headers(request)
    upstream_headers: dict[str, str] = dict(headers)
    if request.method in ("POST", "PUT", "DELETE"):
        upstream_headers["Content-Type"] = request.headers.get(
            "content-type", "application/octet-stream"
        )
        body = await request.body()
        try:
            resp = await client.request(
                request.method,
                f"{ref.url}{upstream_path}",
                content=body,
                headers=upstream_headers,
            )
        except httpx.HTTPError:
            return _record_route(
                request,
                method,
                502,
                t0,
                JSONResponse(
                    {"error": f"upstream node {ref.node_id} unreachable"},
                    status_code=502,
                ),
            )
    else:
        try:
            resp = await client.get(f"{ref.url}{upstream_path}", headers=upstream_headers)
        except httpx.HTTPError:
            return _record_route(
                request,
                method,
                502,
                t0,
                JSONResponse(
                    {"error": f"upstream node {ref.node_id} unreachable"},
                    status_code=502,
                ),
            )

    # Preserve upstream headers — Content-Disposition + CSP set by the
    # /content handler must reach the original caller, and the upstream
    # already produced the correct Content-Type for both JSON and binary
    # payloads.  Drop hop-by-hop headers that the underlying transport
    # will manage itself.
    response_headers = {
        k: v
        for k, v in resp.headers.items()
        if k.lower()
        not in {"transfer-encoding", "content-encoding", "connection", "content-length"}
    }
    return _record_route(
        request,
        method,
        resp.status_code,
        t0,
        Response(
            content=resp.content,
            status_code=resp.status_code,
            headers=response_headers,
        ),
    )


async def route_proxy(request: Request) -> Response:
    """Generic routing proxy for send/approve/cancel/command/close."""
    t0 = time.monotonic()
    # Extract method name from path: /v1/api/route/send -> "send"
    method = request.url.path.rsplit("/", 1)[-1]
    if method == "close":
        method = "close"  # /route/workstreams/close
    router: ConsoleRouter | None = request.app.state.router
    ring_ready = router is not None and router.is_ready()
    if not ring_ready:
        if router is not None:
            await asyncio.to_thread(router.refresh_cache)
            ring_ready = router.is_ready()
        if not ring_ready:
            return _record_route(
                request,
                method,
                503,
                t0,
                JSONResponse(
                    {"error": "Cluster routing not initialized"},
                    status_code=503,
                ),
            )
    assert router is not None

    try:
        body = await request.json()
    except Exception:
        return _record_route(
            request,
            method,
            400,
            t0,
            JSONResponse(
                {"error": "Invalid JSON body"},
                status_code=400,
            ),
        )

    ws_id = body.get("ws_id", "")
    if not ws_id:
        return _record_route(
            request,
            method,
            400,
            t0,
            JSONResponse(
                {"error": "ws_id required"},
                status_code=400,
            ),
        )
    try:
        ref = router.route(ws_id)
    except (NoAvailableNodeError, ValueError):
        return _record_route(
            request,
            method,
            503,
            t0,
            JSONResponse(
                {"error": "routing failed"},
                status_code=503,
            ),
        )

    # Map /v1/api/route/... → /v1/api/... on the upstream server
    path = request.url.path
    upstream_path = path.replace("/api/route/", "/api/", 1)

    client: httpx.AsyncClient = request.app.state.proxy_client
    headers = _proxy_auth_headers(request)
    try:
        resp = await client.post(f"{ref.url}{upstream_path}", json=body, headers=headers)
    except httpx.HTTPError:
        return _record_route(
            request,
            method,
            502,
            t0,
            JSONResponse(
                {"error": f"upstream node {ref.node_id} unreachable"},
                status_code=502,
            ),
        )

    # Transparent retry on 404 (at most once):
    #
    # The rendezvous-selected node doesn't have the workstream.  Refresh
    # membership + overrides and re-route.  If the route changed (e.g., a
    # local-create override was added since the last cache load, or a
    # node has joined / dropped), retry on the new node.  If the route
    # is the same, return the 404 as-is — no loop, no scan.
    if resp.status_code == 404:
        # Off the event loop — force_refresh takes a blocking lock and
        # issues two storage queries.  Coalesces internally so a 404
        # stampede after a node churn doesn't N×-multiply DB reads.
        await asyncio.to_thread(router.force_refresh)
        try:
            new_ref = router.route(ws_id)
        except (NoAvailableNodeError, ValueError):
            new_ref = ref
        if new_ref.node_id != ref.node_id:
            try:
                resp = await client.post(
                    f"{new_ref.url}{upstream_path}", json=body, headers=headers
                )
            except httpx.HTTPError:
                return _record_route(
                    request,
                    method,
                    502,
                    t0,
                    JSONResponse(
                        {"error": f"retry node {new_ref.node_id} unreachable"},
                        status_code=502,
                    ),
                )
            ref = new_ref  # retried node — used for audit attribution (only emits on 2xx via the next block).

    if 200 <= resp.status_code < 300:
        action = _ROUTE_PROXY_AUDIT_ACTIONS.get(method)
        if action:
            _emit_route_audit(request, action, ws_id, ref.node_id)
    return _record_route(
        request,
        method,
        resp.status_code,
        t0,
        Response(
            content=resp.content,
            status_code=resp.status_code,
            headers=dict(resp.headers),
        ),
    )


async def route_workstream_delete(request: Request) -> Response:
    """POST /v1/api/route/workstreams/delete — proxy to the upstream delete endpoint.

    The upstream server exposes delete at ``POST /v1/api/workstreams/{ws_id}/delete``
    (path parameter), so ``route_proxy``'s ``/api/route/... → /api/...``
    rewrite doesn't apply.  This dedicated handler reads ``ws_id`` from the
    request body, routes to the owning node, and forwards to the path-parameter
    form.  Used by the coordinator's ``delete_workstream`` tool.
    """
    t0 = time.monotonic()
    router: ConsoleRouter | None = request.app.state.router
    ring_ready = router is not None and router.is_ready()
    if not ring_ready:
        if router is not None:
            await asyncio.to_thread(router.refresh_cache)
            ring_ready = router.is_ready()
        if not ring_ready:
            return _record_route(
                request,
                "delete",
                503,
                t0,
                JSONResponse(
                    {"error": "Cluster routing not initialized"},
                    status_code=503,
                ),
            )
    assert router is not None

    try:
        body = await request.json()
    except Exception:
        return _record_route(
            request,
            "delete",
            400,
            t0,
            JSONResponse({"error": "Invalid JSON body"}, status_code=400),
        )

    ws_id = body.get("ws_id", "")
    if not ws_id:
        return _record_route(
            request,
            "delete",
            400,
            t0,
            JSONResponse({"error": "ws_id required"}, status_code=400),
        )
    try:
        ref = router.route(ws_id)
    except (NoAvailableNodeError, ValueError):
        return _record_route(
            request,
            "delete",
            503,
            t0,
            JSONResponse({"error": "routing failed"}, status_code=503),
        )

    client: httpx.AsyncClient = request.app.state.proxy_client
    headers = _proxy_auth_headers(request)
    upstream_url = f"{ref.url}/v1/api/workstreams/{ws_id}/delete"
    try:
        resp = await client.post(upstream_url, json=body, headers=headers)
    except httpx.HTTPError:
        return _record_route(
            request,
            "delete",
            502,
            t0,
            JSONResponse(
                {"error": f"upstream node {ref.node_id} unreachable"},
                status_code=502,
            ),
        )

    if 200 <= resp.status_code < 300:
        _emit_route_audit(request, "route.workstream.delete", ws_id, ref.node_id)
    return _record_route(
        request,
        "delete",
        resp.status_code,
        t0,
        Response(
            content=resp.content,
            status_code=resp.status_code,
            headers=dict(resp.headers),
        ),
    )


async def route_lookup(request: Request) -> JSONResponse:
    """GET /v1/api/route — look up which node owns a workstream."""
    t0 = time.monotonic()
    router: ConsoleRouter | None = request.app.state.router
    ring_ready = router is not None and router.is_ready()
    if not ring_ready:
        if router is not None:
            await asyncio.to_thread(router.refresh_cache)
            ring_ready = router.is_ready()
        if not ring_ready:
            return _record_route(
                request,
                "route",
                503,
                t0,
                JSONResponse(
                    {"error": "Cluster routing not initialized"},
                    status_code=503,
                ),
            )  # type: ignore[return-value]
    assert router is not None

    ws_id = request.query_params.get("ws_id", "")
    if not ws_id:
        return _record_route(
            request,
            "route",
            400,
            t0,
            JSONResponse(
                {"error": "ws_id required"},
                status_code=400,
            ),
        )  # type: ignore[return-value]

    try:
        ref = router.route(ws_id)
    except NoAvailableNodeError:
        return _record_route(
            request,
            "route",
            503,
            t0,
            JSONResponse(
                {"error": "No available node for routing"},
                status_code=503,
            ),
        )  # type: ignore[return-value]

    return _record_route(
        request,
        "route",
        200,
        t0,
        JSONResponse(
            {"node_url": ref.url, "node_id": ref.node_id},
        ),
    )  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Route handlers — reverse proxy
# ---------------------------------------------------------------------------


async def proxy_index(request: Request) -> Response:
    """GET /node/{node_id}/ — serve proxied server UI with URL rewriting."""
    node_id = request.path_params["node_id"]
    server_url = _get_server_url(request, node_id)
    if not server_url:
        return JSONResponse({"error": "Node not found"}, status_code=404)

    client: httpx.AsyncClient = request.app.state.proxy_client
    safe_node = urllib.parse.quote(node_id, safe="")
    prefix = f"/node/{safe_node}"
    try:
        resp = await client.get(f"{server_url}/", headers=_proxy_auth_headers(request))
        if resp.status_code < 200 or resp.status_code >= 300:
            log.debug("Upstream %s returned status %s", node_id, resp.status_code)
            return JSONResponse(
                {"error": "Upstream server error", "status_code": resp.status_code},
                status_code=resp.status_code,
            )
        page = resp.text
        # Rewrite static asset paths
        page = page.replace('href="/static/', f'href="{prefix}/static/')
        page = page.replace('src="/static/', f'src="{prefix}/static/')
        page = page.replace('href="/shared/', f'href="{prefix}/shared/')
        page = page.replace('src="/shared/', f'src="{prefix}/shared/')
        # Inject console-return banner + proxy shim after <body>
        banner = _CONSOLE_BANNER_TEMPLATE.replace(
            "NODE_ID_PLACEHOLDER", html.escape(node_id)
        ).replace("NODE_LINK_PLACEHOLDER", html.escape(prefix + "/"))
        shim = (
            "<script>"
            + _JS_PROXY_SHIM.replace('"PREFIX_PLACEHOLDER"', json.dumps(prefix))
            + "</script>"
        )
        page = page.replace("<body>", "<body>" + banner + _CONSOLE_PROXY_STYLE + shim, 1)
        html_resp = HTMLResponse(page)
        html_resp.headers["Cache-Control"] = "no-cache"
        return html_resp
    except httpx.HTTPError as exc:
        log.debug("Proxy index error for %s: %s", node_id, exc)
        return JSONResponse({"error": "Node unreachable"}, status_code=502)


async def proxy_static(request: Request) -> Response:
    """GET /node/{node_id}/static/{path} — proxy static files."""
    node_id = request.path_params["node_id"]
    path = request.path_params["path"]
    server_url = _get_server_url(request, node_id)
    if not server_url:
        return JSONResponse({"error": "Node not found"}, status_code=404)

    client: httpx.AsyncClient = request.app.state.proxy_client
    try:
        resp = await client.get(
            f"{server_url}/static/{path}",
            headers=_proxy_auth_headers(request),
        )
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            media_type=resp.headers.get("content-type", "application/octet-stream"),
        )
    except httpx.HTTPError as exc:
        log.debug("Proxy static error for %s/%s: %s", node_id, path, exc)
        return JSONResponse({"error": "Node unreachable"}, status_code=502)


async def proxy_shared_static(request: Request) -> Response:
    """GET /node/{node_id}/shared/{path} — proxy shared static files."""
    node_id = request.path_params["node_id"]
    path = request.path_params["path"]
    server_url = _get_server_url(request, node_id)
    if not server_url:
        return JSONResponse({"error": "Node not found"}, status_code=404)

    client: httpx.AsyncClient = request.app.state.proxy_client
    try:
        resp = await client.get(
            f"{server_url}/shared/{path}",
            headers=_proxy_auth_headers(request),
        )
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            media_type=resp.headers.get("content-type", "application/octet-stream"),
        )
    except httpx.HTTPError as exc:
        log.debug("Proxy shared static error for %s/%s: %s", node_id, path, exc)
        return JSONResponse({"error": "Node unreachable"}, status_code=502)


async def proxy_api(request: Request) -> Response:
    """Proxy API requests to target node. Detects SSE vs regular."""
    node_id = request.path_params["node_id"]
    path = request.path_params["path"]
    server_url = _get_server_url(request, node_id)
    if not server_url:
        return JSONResponse({"error": "Node not found"}, status_code=404)

    # Detect if this came through the /v1/ proxy route
    api_prefix = "api"
    safe_node = urllib.parse.quote(node_id, safe="")
    if request.url.path.startswith(f"/node/{safe_node}/v1/api/"):
        api_prefix = "v1/api"

    # SSE detection: GET requests to events endpoints
    if request.method == "GET" and path in ("events", "events/global"):
        return await _proxy_sse(request, server_url, path, api_prefix=api_prefix)

    if request.method in ("POST", "PUT", "DELETE"):
        return await _proxy_post(request, server_url, path, api_prefix=api_prefix)

    return await _proxy_get(request, server_url, f"{api_prefix}/{path}")


async def proxy_non_api(request: Request) -> Response:
    """Proxy non-API GET endpoints (health, metrics) to target node."""
    node_id = request.path_params["node_id"]
    path = request.path_params["path"]
    server_url = _get_server_url(request, node_id)
    if not server_url:
        return JSONResponse({"error": "Node not found"}, status_code=404)
    return await _proxy_get(request, server_url, path)


async def _proxy_get(request: Request, server_url: str, path: str) -> Response:
    """Forward a GET request to the target server."""
    client: httpx.AsyncClient = request.app.state.proxy_client
    target = f"{server_url}/{path}"
    if request.url.query:
        target += f"?{request.url.query}"
    try:
        resp = await client.get(target, headers=_proxy_auth_headers(request))
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            media_type=resp.headers.get("content-type", "application/json"),
        )
    except httpx.HTTPError as exc:
        log.debug("Proxy GET error for %s: %s", target, exc)
        return JSONResponse({"error": "Node unreachable"}, status_code=502)


async def _proxy_post(
    request: Request, server_url: str, path: str, *, api_prefix: str = "api"
) -> Response:
    """Forward a non-GET request (POST/PUT/DELETE) to the target server."""
    client: httpx.AsyncClient = request.app.state.proxy_client
    body = await request.body()
    content_type = request.headers.get("content-type", "application/json")
    target = f"{server_url}/{api_prefix}/{path}"
    if request.url.query:
        target += f"?{request.url.query}"
    try:
        headers = {"Content-Type": content_type}
        headers.update(_proxy_auth_headers(request))
        resp = await client.request(
            request.method,
            target,
            content=body,
            headers=headers,
        )
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            media_type=resp.headers.get("content-type", "application/json"),
        )
    except httpx.HTTPError as exc:
        log.debug("Proxy %s error for %s/%s: %s", request.method, api_prefix, path, exc)
        return JSONResponse({"error": "Node unreachable"}, status_code=502)


async def _proxy_sse(
    request: Request, server_url: str, path: str, *, api_prefix: str = "api"
) -> Response:
    """Proxy an SSE stream from the target server to the browser.

    Relays raw bytes verbatim so server-side ping comments, event framing,
    and keepalives all pass through unchanged.
    """
    target = f"{server_url}/{api_prefix}/{path}"
    if request.url.query:
        target += f"?{request.url.query}"

    sse_client: httpx.AsyncClient = request.app.state.proxy_sse_client
    sse_auth = _proxy_auth_headers(request)

    async def raw_stream() -> AsyncGenerator[bytes, None]:
        try:
            async with sse_client.stream(
                "GET",
                target,
                headers={**sse_auth, "Accept": "text/event-stream", "Cache-Control": "no-store"},
                timeout=httpx.Timeout(connect=10, read=None, write=5, pool=None),
            ) as response:
                if response.status_code != 200:
                    status = response.status_code
                    body_preview = await _bounded_stream_preview(response)
                    # Non-200 from a service-auth-backed SSE path is
                    # operator-actionable: 4xx = scope/tenant drift,
                    # 5xx = upstream outage.  Raise the log floor to
                    # WARNING so it surfaces in ops logs; the browser
                    # also receives the error event for UX.
                    log.warning(
                        "proxy.sse.non_200 status=%d url=%s body=%s",
                        status,
                        target,
                        body_preview,
                    )
                    yield f"event: error\ndata: Upstream returned status {status}\n\n".encode()
                    return
                async for chunk in response.aiter_bytes():
                    if await request.is_disconnected():
                        return
                    yield chunk
        except httpx.HTTPError:
            log.debug("SSE proxy stream ended for %s", target)

    return StreamingResponse(
        raw_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Coordinator workstream endpoints — mounted under /v1/api/workstreams/*
# via register_session_routes + register_coord_verbs. Handler bodies stay
# named ``coordinator_*`` for now; the body-convergence follow-on lifts
# them into turnstone.core.session_routes with kind branching.
# ---------------------------------------------------------------------------


def _require_coord_mgr(request: Request) -> tuple[Any, JSONResponse | None]:
    """Resolve the coordinator manager or return a 503 with remediation.

    Returns ``(coord_mgr, None)`` on success, ``(None, JSONResponse)``
    when the coordinator subsystem isn't configured.
    """
    coord_mgr = getattr(request.app.state, "coord_mgr", None)
    config_store = getattr(request.app.state, "config_store", None)
    if coord_mgr is None:
        registry_err = getattr(request.app.state, "coord_registry_error", "") or ""
        msg = "Coordinator subsystem not initialized. " + (
            registry_err or "Check coordinator.model_alias and Models tab configuration."
        )
        return None, JSONResponse({"error": msg}, status_code=503)
    if config_store is None:
        return None, JSONResponse({"error": "ConfigStore unavailable"}, status_code=503)
    alias = (config_store.get("coordinator.model_alias") or "").strip()
    # Empty alias falls back to the registry's default model — operators
    # get a working coordinator on a freshly-provisioned console without
    # an extra manual setting.
    registry = getattr(request.app.state, "coord_registry", None)
    if registry is None:
        return None, JSONResponse(
            {
                "error": (
                    "ModelRegistry unavailable for coordinator sessions. "
                    "Restart the console after adding a model definition."
                )
            },
            status_code=503,
        )
    try:
        # ``resolve(None)`` uses ``registry.default``; the registry
        # raises when neither the explicit alias nor the default is
        # configured, which we translate to a 503 with remediation.
        registry.resolve(alias or None)
    except Exception as exc:
        hint = f"coordinator.model_alias '{alias}'" if alias else "the registry default alias"
        return None, JSONResponse(
            {
                "error": (
                    f"{hint} does not resolve: {exc}. "
                    "Configure a model in the admin Models tab, or set "
                    "``coordinator.model_alias`` in Settings to an existing alias."
                )
            },
            status_code=503,
        )
    return coord_mgr, None


def _require_admin_coordinator(
    request: Request, *, allow_service_bypass: bool = True
) -> JSONResponse | None:
    """Gate a coordinator endpoint on the ``admin.coordinator`` permission.

    Destructive endpoints (/restrict, /stop_cascade) pass
    ``allow_service_bypass=False`` so a service-scoped caller whose
    ``user_id`` matches the coord owner still needs an explicit grant.
    """
    return require_permission(
        request, "admin.coordinator", allow_service_bypass=allow_service_bypass
    )


def _resolve_coordinator_or_404(
    request: Request,
    coord_mgr: Any,
    storage: Any,
    ws_id: str,
    user_id: str,
) -> tuple[Any, JSONResponse | None]:
    """Resolve a coordinator workstream by id.

    Returns ``(ws, None)`` on success — ``ws`` is the in-memory
    ``Workstream`` when present, ``None`` when the coordinator is
    persisted but not loaded (callers may then fall through to
    ``storage`` directly or trigger lazy rehydration).  Returns
    ``(None, 404)`` on missing row / wrong kind / storage unavailable.

    Centralises the manager-first, storage-fallback, 404-mask ladder
    previously duplicated across ``coordinator_children`` /
    ``coordinator_tasks`` / ``coordinator_history``.  Turnstone is a
    trusted-team tool — ``user_id`` is metadata, not an access
    boundary, so this helper no longer gates on row ownership; scope
    auth (``admin.coordinator``) upstream is the gate.
    """
    del user_id  # retained in signature for caller-site clarity; not consulted here
    miss = JSONResponse({"error": "coordinator not found"}, status_code=404)
    ws = coord_mgr.get(ws_id) if coord_mgr is not None else None
    if ws is None:
        if storage is None:
            return None, miss
        try:
            row = storage.get_workstream(ws_id)
        except Exception:
            log.debug("resolve_coordinator.storage_failed ws=%s", ws_id[:8], exc_info=True)
            return None, miss
        if row is None or row.get("kind") != WorkstreamKind.COORDINATOR:
            return None, miss
        return None, None
    return ws, None


def _auth_user_id(request: Request) -> str:
    auth = getattr(getattr(request, "state", None), "auth_result", None)
    return getattr(auth, "user_id", "") or ""


def _auth_scopes(request: Request) -> set[str]:
    auth = getattr(getattr(request, "state", None), "auth_result", None)
    return set(getattr(auth, "scopes", []) or [])


async def coordinator_create(request: Request) -> JSONResponse:
    """POST /v1/api/workstreams/new — create a new coordinator session."""
    from turnstone.core.audit import record_audit
    from turnstone.core.web_helpers import read_json_or_400

    err = _require_admin_coordinator(request)
    if err is not None:
        return err
    coord_mgr, err503 = _require_coord_mgr(request)
    if err503 is not None:
        return err503
    body = await read_json_or_400(request)
    if isinstance(body, JSONResponse):
        return body
    user_id = _auth_user_id(request)
    if not user_id:
        return JSONResponse({"error": "authentication required"}, status_code=401)
    name = (body.get("name") or "").strip()
    skill = (body.get("skill") or "").strip() or None
    initial_message = (body.get("initial_message") or "").strip()
    # Pre-resolve skill → (template_id, applied_version) so the
    # persisted row records what was applied. SessionManager.create
    # is kind-agnostic and no longer does this lookup itself.
    skill_id_resolved = ""
    skill_version_resolved = 0
    if skill:
        from turnstone.core.memory import get_skill_by_name
        from turnstone.core.storage._registry import get_storage as _get_storage

        _st = _get_storage()
        skill_data = await asyncio.to_thread(get_skill_by_name, skill)
        if skill_data and skill_data.get("template_id") and _st is not None:
            skill_id_resolved = str(skill_data["template_id"])
            try:
                skill_version_resolved = (
                    await asyncio.to_thread(_st.count_skill_versions, skill_id_resolved) + 1
                )
            except Exception:
                log.debug("coord_create.skill_version_failed skill=%s", skill, exc_info=True)
                skill_version_resolved = 1
    try:
        # Offload to a worker thread — SessionManager.create runs
        # blocking storage calls (register_workstream) and the
        # session-factory invocation. Running inline on the async
        # event loop stalled the SSE manager + every other async
        # handler for the duration of the create (#perf-4).
        ws = await asyncio.to_thread(
            coord_mgr.create,
            user_id=user_id,
            name=name,
            skill=skill,
            skill_id=skill_id_resolved,
            skill_version=skill_version_resolved,
        )
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=429)
    except ValueError as exc:
        # Session factory raises ValueError on misconfigured alias —
        # surface as 503 with the factory's remediation text.
        return JSONResponse({"error": str(exc)}, status_code=503)
    except Exception:
        # Don't echo the exception text to the caller — it can leak
        # internals (stack frame names, file paths, etc.).  Log with a
        # short correlation id and return that to the client so support
        # can match a user report to the log line.
        correlation_id = secrets.token_hex(4)
        log.warning(
            "coordinator_create.failed correlation_id=%s",
            correlation_id,
            exc_info=True,
        )
        return JSONResponse(
            {
                "error": (
                    "failed to create coordinator (internal error). "
                    f"correlation_id={correlation_id}"
                )
            },
            status_code=500,
        )
    # Initial message spawns the first worker after the session has
    # been installed on the adapter. Previously this rode inside
    # ``coord_mgr.create`` via the old ``initial_message`` kwarg; the
    # unified SessionManager.create is kind-agnostic so the worker spawn
    # moved to the caller.
    if initial_message:
        coord_adapter = getattr(request.app.state, "coord_adapter", None)
        if coord_adapter is not None:
            coord_adapter.send(ws.id, initial_message)
    storage = getattr(request.app.state, "auth_storage", None)
    if storage is not None:
        try:
            record_audit(
                storage,
                user_id,
                "coordinator.create",
                "workstream",
                ws.id,
                {"coord_ws_id": ws.id, "src": "coordinator", "name": ws.name},
                request.client.host if request.client else "",
            )
        except Exception:
            log.debug("coordinator_create.audit_failed", exc_info=True)
    return JSONResponse({"ws_id": ws.id, "name": ws.name}, status_code=201)


async def coordinator_send(request: Request) -> JSONResponse:
    """POST /v1/api/workstreams/{ws_id}/send — queue a user message."""
    from turnstone.core.web_helpers import read_json_or_400

    err = _require_admin_coordinator(request)
    if err is not None:
        return err
    coord_mgr, err503 = _require_coord_mgr(request)
    if err503 is not None:
        return err503
    ws_id = request.path_params.get("ws_id", "")
    body = await read_json_or_400(request)
    if isinstance(body, JSONResponse):
        return body
    message = (body.get("message") or "").strip()
    if not message:
        return JSONResponse({"error": "message is required"}, status_code=400)
    ws = coord_mgr.get(ws_id)
    if ws is None:
        return JSONResponse({"error": "coordinator not found"}, status_code=404)
    coord_adapter = getattr(request.app.state, "coord_adapter", None)
    if coord_adapter is None or not coord_adapter.send(ws_id, message):
        # Distinguish "worker busy + queue full" from "ws not loaded".
        # If ws is loaded and session exists, the worker queue is full —
        # tell the client to back off rather than retry blindly.
        ws_now = coord_mgr.get(ws_id)
        if ws_now is not None and ws_now.session is not None:
            return JSONResponse({"error": "worker queue full; retry shortly"}, status_code=429)
        return JSONResponse({"error": "send failed"}, status_code=500)
    return JSONResponse({"status": "ok"})


async def coordinator_approve(request: Request) -> JSONResponse:
    """POST /v1/api/workstreams/{ws_id}/approve — unblock pending approval."""
    from turnstone.core.web_helpers import read_json_or_400

    err = _require_admin_coordinator(request)
    if err is not None:
        return err
    coord_mgr, err503 = _require_coord_mgr(request)
    if err503 is not None:
        return err503
    ws_id = request.path_params.get("ws_id", "")
    body = await read_json_or_400(request)
    if isinstance(body, JSONResponse):
        return body
    approved = bool(body.get("approved", False))
    feedback = body.get("feedback")
    always = bool(body.get("always", False))
    ws = coord_mgr.get(ws_id)
    if ws is None:
        return JSONResponse({"error": "coordinator not found"}, status_code=404)
    ui = ws.ui
    if ui is None or not hasattr(ui, "resolve_approval"):
        return JSONResponse(
            {"error": "coordinator UI does not support approval"},
            status_code=409,
        )
    if always and approved and getattr(ui, "_pending_approval", None):
        tool_names = {
            it.get("approval_label", "") or it.get("func_name", "")
            for it in ui._pending_approval.get("items", [])
            if it.get("needs_approval") and it.get("func_name") and not it.get("error")
        }
        tool_names.discard("")
        ui.auto_approve_tools.update(tool_names)
    ui.resolve_approval(approved, feedback)
    return JSONResponse({"status": "ok"})


async def coordinator_cancel(request: Request) -> JSONResponse:
    """POST /v1/api/workstreams/{ws_id}/cancel — cancel in-flight generation."""
    from turnstone.core.audit import record_audit

    err = _require_admin_coordinator(request)
    if err is not None:
        return err
    coord_mgr, err503 = _require_coord_mgr(request)
    if err503 is not None:
        return err503
    ws_id = request.path_params.get("ws_id", "")
    user_id = _auth_user_id(request)
    ws = coord_mgr.get(ws_id)
    if ws is None:
        return JSONResponse({"error": "coordinator not found"}, status_code=404)
    coord_mgr.cancel(ws_id)
    storage = getattr(request.app.state, "auth_storage", None)
    if storage is not None:
        try:
            record_audit(
                storage,
                user_id,
                "coordinator.cancel",
                "workstream",
                ws_id,
                {"coord_ws_id": ws_id, "src": "coordinator"},
                request.client.host if request.client else "",
            )
        except Exception:
            log.debug("coordinator_cancel.audit_failed", exc_info=True)
    return JSONResponse({"status": "ok"})


async def coordinator_close(request: Request) -> JSONResponse:
    """POST /v1/api/workstreams/{ws_id}/close — unload the session."""
    from turnstone.core.audit import record_audit

    err = _require_admin_coordinator(request)
    if err is not None:
        return err
    coord_mgr, err503 = _require_coord_mgr(request)
    if err503 is not None:
        return err503
    ws_id = request.path_params.get("ws_id", "")
    user_id = _auth_user_id(request)
    ws = coord_mgr.get(ws_id)
    if ws is None:
        return JSONResponse({"error": "coordinator not found"}, status_code=404)
    if not coord_mgr.close(ws_id):
        return JSONResponse({"error": "close failed"}, status_code=500)
    storage = getattr(request.app.state, "auth_storage", None)
    if storage is not None:
        try:
            record_audit(
                storage,
                user_id,
                "coordinator.close",
                "workstream",
                ws_id,
                {"coord_ws_id": ws_id, "src": "coordinator"},
                request.client.host if request.client else "",
            )
        except Exception:
            log.debug("coordinator_close.audit_failed", exc_info=True)
    return JSONResponse({"status": "ok"})


async def coordinator_events(request: Request) -> Response:
    """GET /v1/api/workstreams/{ws_id}/events — SSE event stream."""
    err = _require_admin_coordinator(request)
    if err is not None:
        return err
    coord_mgr, err503 = _require_coord_mgr(request)
    if err503 is not None:
        return err503
    ws_id = request.path_params.get("ws_id", "")
    ws = coord_mgr.get(ws_id)
    if ws is None:
        return JSONResponse({"error": "coordinator not found"}, status_code=404)
    ui = ws.ui
    if ui is None or not hasattr(ui, "_register_listener"):
        return JSONResponse({"error": "coordinator has no UI"}, status_code=409)

    client_queue = ui._register_listener()
    # Replay any pending approval so a reconnecting tab sees the prompt.
    pending = getattr(ui, "_pending_approval", None)
    if pending is not None:
        with contextlib.suppress(queue.Full):
            client_queue.put_nowait(pending)
    pending_plan = getattr(ui, "_pending_plan_review", None)
    if pending_plan is not None:
        with contextlib.suppress(queue.Full):
            client_queue.put_nowait(pending_plan)

    async def event_generator() -> AsyncGenerator[dict[str, Any], None]:
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.to_thread(client_queue.get, True, 1.0)
                    yield {"data": json.dumps(event)}
                except queue.Empty:
                    pass  # ping keeps the connection alive
        finally:
            ui._unregister_listener(client_queue)

    return EventSourceResponse(event_generator(), ping=5)


async def coordinator_history(request: Request) -> JSONResponse:
    """GET /v1/api/workstreams/{ws_id}/history — message history for page load.

    Supports a ``?limit=`` query param (default 100, max 500) bounding
    how many conversation rows are fetched from storage.  Long-lived
    coordinators can accumulate thousands of messages — the page load
    only needs the tail.
    """
    err = _require_admin_coordinator(request)
    if err is not None:
        return err
    coord_mgr, err503 = _require_coord_mgr(request)
    if err503 is not None:
        return err503
    ws_id = request.path_params.get("ws_id", "")
    user_id = _auth_user_id(request)
    storage = getattr(request.app.state, "auth_storage", None)
    _ws, err404 = _resolve_coordinator_or_404(request, coord_mgr, storage, ws_id, user_id)
    if err404 is not None:
        return err404

    try:
        limit = int(request.query_params.get("limit", "100"))
    except (TypeError, ValueError):
        limit = 100
    limit = max(1, min(limit, 500))

    messages: list[dict[str, Any]] = []
    if storage is not None:
        try:
            messages = storage.load_messages(ws_id, limit=limit)
        except Exception:
            log.debug("coordinator_history.load_failed ws=%s", ws_id[:8], exc_info=True)
    return JSONResponse({"ws_id": ws_id, "messages": messages})


async def coordinator_list(request: Request) -> JSONResponse:
    """GET /v1/api/workstreams — list active coordinator sessions for the caller."""
    err = _require_admin_coordinator(request)
    if err is not None:
        return err
    coord_mgr, err503 = _require_coord_mgr(request)
    if err503 is not None:
        return err503
    # Trusted-team visibility: any admin.coordinator-scoped caller
    # sees cluster-wide active coordinators.  ``user_id`` stays on the
    # response row as metadata for the UI.
    rows = coord_mgr.list_all()
    return JSONResponse(
        {
            "coordinators": [
                {
                    "ws_id": r.id,
                    "name": r.name,
                    "state": r.state.value,
                    "user_id": r.user_id,
                }
                for r in rows
            ]
        }
    )


async def coordinator_saved(request: Request) -> JSONResponse:
    """GET /v1/api/workstreams/saved — list saved (closed-but-persisted) coordinator
    sessions for the caller.

    Mirrors :func:`turnstone.server.list_saved_workstreams` for the
    coordinator surface so the frontend can render a "saved coordinators"
    card grid identically to the interactive "saved workstreams" sidebar.
    Same response item shape, same tenant/admin scoping.

    Returns ONLY rows with ``state='closed'`` — explicitly closed
    coordinators that the user can re-open via the saved card.  Active /
    in-flight rows live in the active list above; deleted rows are
    tombstones that ``_open_impl`` refuses to resurrect.

    Also filters out coordinators currently loaded into ``coord_mgr``
    (defence-in-depth: a coordinator could be 'closed' on disk but
    still in the warm pool right after a restart of the close-emit
    sequence races the in-memory pop).
    """
    from turnstone.core.memory import list_workstreams_with_history
    from turnstone.core.workstream import WorkstreamKind

    err = _require_admin_coordinator(request)
    if err is not None:
        return err

    # Trusted-team visibility (post-#400): any caller with the
    # ``admin.coordinator`` scope sees cluster-wide saved coordinators;
    # ``user_id`` stays as metadata on the persisted row but isn't a
    # filter here.
    # Offload the blocking storage call + the lock-acquiring list_all
    # off the event loop, matching coordinator_create's pattern (#perf-2
    # from the saved-coordinators review).  list_workstreams_with_history
    # runs a correlated COUNT subquery; coord_mgr.list_all() takes the
    # manager lock.  Either can stall every other async handler if run
    # inline.
    rows = await asyncio.to_thread(
        list_workstreams_with_history,
        limit=50,
        kind=WorkstreamKind.COORDINATOR,
        user_id=None,
        state="closed",
    )

    coord_mgr = getattr(request.app.state, "coord_mgr", None)
    loaded: set[str] = set()
    if coord_mgr is not None:
        try:
            loaded = await asyncio.to_thread(
                lambda: {ws.id for ws in coord_mgr.list_all()},
            )
        except Exception:
            log.debug("coordinator_saved.list_all_failed", exc_info=True)

    # Column order from list_workstreams_with_history is
    # (ws_id, alias, title, name, created, updated, count, node_id) — see
    # the SELECT in turnstone/core/storage/_sqlite.py:list_workstreams_with_history.
    # ``*_extra`` swallows the trailing node_id (and any future columns
    # appended at the tail); keep this comment in sync if the SELECT
    # changes the prefix order.
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
        if wid not in loaded
    ]
    # Key is ``coordinators`` (not ``workstreams``) for consistency with
    # the sibling coordinator_list endpoint.  Item shape matches the
    # interactive saved-workstreams response so the frontend card renderer
    # stays a 1:1 mirror.
    return JSONResponse({"coordinators": result})


async def coordinator_page(request: Request) -> Response:
    """GET /coordinator/{ws_id} — serve the one-pane coordinator HTML.

    The handler injects ``data-ws-id`` on the <html> tag so
    ``coordinator.js`` can read it without a separate API round-trip.
    Auth gating happens on the API endpoints the page calls — this
    handler simply serves the static template (same model as /static).
    """
    ws_id = request.path_params.get("ws_id", "")
    if not _VALID_WS_ID_RE.match(ws_id):
        return JSONResponse({"error": "invalid ws_id"}, status_code=400)
    template_path = _STATIC_DIR / "coordinator" / "index.html"
    if not template_path.is_file():
        return JSONResponse({"error": "coordinator UI template missing"}, status_code=500)
    try:
        body = template_path.read_text(encoding="utf-8")
    except OSError:
        return JSONResponse({"error": "failed to read coordinator UI template"}, status_code=500)
    # Inject the ws_id as an HTML attribute.  ws_id passed the
    # ``_VALID_WS_ID_RE`` gate above (hex only) so there's nothing
    # to HTML-escape; leave the replacement simple.
    body = body.replace("{{WS_ID}}", ws_id)
    return Response(body, media_type="text/html; charset=utf-8")


async def coordinator_detail(request: Request) -> JSONResponse:
    """GET /v1/api/workstreams/{ws_id} — detail + lazy rehydrate on miss."""
    err = _require_admin_coordinator(request)
    if err is not None:
        return err
    coord_mgr, err503 = _require_coord_mgr(request)
    if err503 is not None:
        return err503
    ws_id = request.path_params.get("ws_id", "")
    ws = coord_mgr.get(ws_id)
    if ws is None:
        # Lazy rehydration goes through the session factory, which can
        # raise the same exceptions coordinator_create handles — match
        # the correlation-id mask so stack traces don't leak through
        # the detail endpoint either.
        try:
            ws = coord_mgr.open(ws_id)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=503)
        except Exception:
            correlation_id = secrets.token_hex(4)
            log.warning(
                "coordinator_detail.rehydrate_failed correlation_id=%s ws_id=%s",
                correlation_id,
                ws_id[:8],
                exc_info=True,
            )
            return JSONResponse(
                {
                    "error": (
                        "failed to rehydrate coordinator (internal error). "
                        f"correlation_id={correlation_id}"
                    )
                },
                status_code=500,
            )
        if ws is None:
            return JSONResponse({"error": "coordinator not found"}, status_code=404)
    return JSONResponse(
        {
            "ws_id": ws.id,
            "name": ws.name,
            "state": ws.state.value,
            "user_id": ws.user_id,
            "kind": ws.kind,
        }
    )


async def coordinator_open(request: Request) -> JSONResponse:
    """POST /v1/api/workstreams/{ws_id}/open — explicit rehydration.

    Parity with the server's ``POST /v1/api/workstreams/{ws_id}/open``.
    ``coordinator_detail`` already rehydrates lazily on a GET miss; this
    endpoint gives SDK callers and operators a way to warm a coordinator
    without browsing to it.  Same ownership / 404-on-mismatch /
    correlation-id-masked error semantics as ``coordinator_detail``.
    """
    err = _require_admin_coordinator(request)
    if err is not None:
        return err
    coord_mgr, err503 = _require_coord_mgr(request)
    if err503 is not None:
        return err503
    ws_id = request.path_params.get("ws_id", "")
    if not ws_id:
        return JSONResponse({"error": "ws_id is required"}, status_code=400)
    ws = coord_mgr.get(ws_id)
    if ws is not None:
        return JSONResponse({"ws_id": ws.id, "name": ws.name, "already_loaded": True})
    try:
        ws = coord_mgr.open(ws_id)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    except Exception:
        correlation_id = secrets.token_hex(4)
        log.warning(
            "coordinator_open.rehydrate_failed correlation_id=%s ws_id=%s",
            correlation_id,
            ws_id[:8],
            exc_info=True,
        )
        return JSONResponse(
            {
                "error": (
                    f"failed to open coordinator (internal error). correlation_id={correlation_id}"
                )
            },
            status_code=500,
        )
    if ws is None:
        return JSONResponse({"error": "coordinator not found"}, status_code=404)
    return JSONResponse({"ws_id": ws.id, "name": ws.name})


_CHILDREN_PAGE_LIMIT = 200


def _coord_children_row(row: Any) -> dict[str, Any]:
    """Serialize a ``list_workstreams`` row for the /children response.

    Matches the ``list_children`` tool output shape so the tool and the UI
    endpoint agree: ``ws_id / node_id / name / state / created / updated
    / kind / parent_ws_id / skill_id / skill_version``.
    """
    try:
        m = row._mapping  # SQLAlchemy Row
    except AttributeError:
        m = {
            "ws_id": row[0],
            "node_id": row[1],
            "name": row[2],
            "state": row[3],
            "created": row[4],
            "updated": row[5],
            "kind": WorkstreamKind.from_raw(row[6] if len(row) > 6 else None),
            "parent_ws_id": row[7] if len(row) > 7 else None,
            "skill_id": row[8] if len(row) > 8 else None,
            "skill_version": row[9] if len(row) > 9 else None,
        }
    return {
        "ws_id": m["ws_id"],
        "node_id": m["node_id"],
        "name": m["name"],
        "state": m["state"],
        "created": m["created"],
        "updated": m["updated"],
        "kind": m["kind"],
        "parent_ws_id": m["parent_ws_id"],
        "skill_id": m["skill_id"],
        "skill_version": m["skill_version"],
    }


async def coordinator_children(request: Request) -> JSONResponse:
    """GET /v1/api/workstreams/{ws_id}/children — list direct children.

    Returns ``{items, truncated}`` with one row per interactive workstream
    whose ``parent_ws_id`` is the coordinator.  Matches the shape of the
    ``list_children`` tool so the tree UI and the model-facing tool see
    the same rows.

    Same ownership / 404-on-mismatch / admin-bypass semantics as
    ``coordinator_detail``.  Reads don't audit.
    """
    from turnstone.core.web_helpers import require_storage_or_503

    err = _require_admin_coordinator(request)
    if err is not None:
        return err
    coord_mgr, err503 = _require_coord_mgr(request)
    if err503 is not None:
        return err503
    storage, err503s = require_storage_or_503(request)
    if err503s is not None:
        return err503s

    ws_id = request.path_params.get("ws_id", "")
    if not _VALID_WS_ID_RE.match(ws_id):
        return JSONResponse({"error": "invalid ws_id"}, status_code=400)
    user_id = _auth_user_id(request)
    _ws, err404 = _resolve_coordinator_or_404(request, coord_mgr, storage, ws_id, user_id)
    if err404 is not None:
        return err404

    # Trusted-team visibility: any caller with admin.coordinator sees
    # the full child subtree.  ``user_id`` stays on each row as
    # metadata, not a filter.
    try:
        raw = storage.list_workstreams(
            limit=_CHILDREN_PAGE_LIMIT + 1,
            parent_ws_id=ws_id,
            kind=None,
            user_id=None,
        )
    except Exception:
        correlation_id = secrets.token_hex(4)
        log.warning(
            "coordinator_children.list_failed correlation_id=%s ws_id=%s",
            correlation_id,
            ws_id[:8],
            exc_info=True,
        )
        return JSONResponse(
            {"error": f"failed to list children. correlation_id={correlation_id}"},
            status_code=500,
        )

    # Compute truncated BEFORE the coordinator-filter pass so a
    # filtered-out sentinel row doesn't mask "there's more data".
    # Fetched `_CHILDREN_PAGE_LIMIT + 1` rows above as the sentinel;
    # if the DB returned that many, there's at least one more page.
    truncated = len(raw) > _CHILDREN_PAGE_LIMIT
    items: list[dict[str, Any]] = []
    for row in raw:
        serialized = _coord_children_row(row)
        # Drop nested coordinators defensively — current schema can't
        # produce them (the interactive-side SessionManager rejects
        # kind!=interactive), but the filter keeps the tree UI contract
        # stable across schema changes.
        if serialized.get("kind") == WorkstreamKind.COORDINATOR:
            continue
        items.append(serialized)
        if len(items) >= _CHILDREN_PAGE_LIMIT:
            break
    return JSONResponse({"items": items, "truncated": truncated})


def _coordinator_metrics_payload(
    *,
    ws_id: str,
    spawns_total: int = 0,
    spawns_last_hour: int = 0,
    child_state_counts: dict[str, int] | None = None,
    judge_fallback_rate: float = 0.0,
    intent_verdicts_sample: int = 0,
) -> dict[str, Any]:
    """Build the ``coordinator_metrics`` response dict.

    One source of truth for the response shape — the DENY short-circuit
    and the happy path both call this so a new field added tomorrow
    can't appear in one branch and not the other.  Wait-tool metrics
    are always zero placeholders; the harness doesn't persist them
    yet but scrapers key on the keys being present.
    """
    return {
        "ws_id": ws_id,
        "spawns_total": spawns_total,
        "spawns_last_hour": spawns_last_hour,
        "child_state_counts": child_state_counts or {},
        "judge_fallback_rate": judge_fallback_rate,
        "intent_verdicts_sample": intent_verdicts_sample,
        "wait_completions": 0,
        "wait_timeouts": 0,
        "wait_avg_elapsed": 0.0,
    }


async def coordinator_metrics(request: Request) -> JSONResponse:
    """GET /v1/api/workstreams/{ws_id}/metrics — per-coordinator health snapshot.

    Aggregates cheap, already-persisted signals into a one-shot "is
    this coordinator healthy?" answer for operators (#16).  No new
    persistence — everything derives from ``list_workstreams``
    (children) and ``list_intent_verdicts`` (judge telemetry).

    Fields:

    - ``spawns_total`` — children ever created under this coordinator
      (closed / deleted children included; the row persists through
      close and hard-delete cascades the row out but is rare).
    - ``spawns_last_hour`` — subset of the above whose ``created``
      timestamp is within the last 3600s.
    - ``child_state_counts`` — ``{state: count}`` grouped on the live
      children's state column.  Useful for spotting a coordinator
      whose children are all stuck in ``attention`` (approval queue).
    - ``judge_fallback_rate`` — fraction of recent intent verdicts
      whose ``tier`` contained ``fallback`` — indicates the judge's
      primary path is unavailable or misconfigured.  0.0 when no
      verdicts have been recorded.
    - ``wait_completions`` / ``wait_timeouts`` / ``wait_avg_elapsed``
      — placeholders returning 0 / 0 / 0.0; these require explicit
      wait-tool instrumentation (future work — the SSE events from
      #14 carry the data live but aren't persisted yet).

    Ownership / authz: same gate as ``coordinator_detail`` — 404-mask
    rows the caller doesn't own (no existence-oracle leak).
    """
    from turnstone.core.web_helpers import require_storage_or_503

    err = _require_admin_coordinator(request)
    if err is not None:
        return err
    coord_mgr, err503 = _require_coord_mgr(request)
    if err503 is not None:
        return err503
    storage, err503s = require_storage_or_503(request)
    if err503s is not None:
        return err503s

    ws_id = request.path_params.get("ws_id", "")
    if not _VALID_WS_ID_RE.match(ws_id):
        return JSONResponse({"error": "invalid ws_id"}, status_code=400)
    user_id = _auth_user_id(request)
    _ws, err404 = _resolve_coordinator_or_404(request, coord_mgr, storage, ws_id, user_id)
    if err404 is not None:
        return err404

    # Children metrics derived from aggregate SQL — avoids pulling
    # every hydrated row just to group by state and filter on created
    # (#perf-1).  Two cheap queries instead of a ``list_workstreams``
    # scan up to 10k rows.
    #
    # Trusted-team visibility: aggregates run cluster-wide per the
    # unified ownership model; ``user_id`` is not a filter here.
    from datetime import UTC, datetime

    now_epoch = time.time()
    hour_ago_iso = datetime.fromtimestamp(now_epoch - 3600, tz=UTC).strftime("%Y-%m-%dT%H:%M:%S")
    try:
        state_counts = await asyncio.to_thread(
            storage.count_workstreams_by_state,
            parent_ws_id=ws_id,
            user_id=None,
        )
    except Exception:
        log.debug("coordinator_metrics.state_counts_failed ws=%s", ws_id[:8], exc_info=True)
        state_counts = {}
    spawns_total = sum(state_counts.values())
    try:
        spawns_last_hour = await asyncio.to_thread(
            storage.count_workstreams_since,
            hour_ago_iso,
            parent_ws_id=ws_id,
            user_id=None,
        )
    except Exception:
        log.debug(
            "coordinator_metrics.spawns_last_hour_failed ws=%s",
            ws_id[:8],
            exc_info=True,
        )
        spawns_last_hour = 0

    # Intent verdicts — scoped to the coordinator itself (child verdicts
    # would require iterating every child's verdicts; deferred).  Small
    # cap (last 200) keeps this a cheap query; enough to compute a
    # meaningful rate for a busy coordinator.
    try:
        verdicts = await asyncio.to_thread(storage.list_intent_verdicts, ws_id=ws_id, limit=200)
    except Exception:
        log.debug("coordinator_metrics.list_verdicts_failed ws=%s", ws_id[:8], exc_info=True)
        verdicts = []
    total_verdicts = len(verdicts)
    fallback_count = 0
    for v in verdicts:
        tier = ""
        if isinstance(v, dict):
            tier = str(v.get("tier") or "")
        else:
            try:
                tier = str(v._mapping.get("tier") or "")
            except AttributeError:
                tier = ""
        if "fallback" in tier.lower():
            fallback_count += 1
    judge_fallback_rate = round(fallback_count / total_verdicts, 3) if total_verdicts > 0 else 0.0

    return JSONResponse(
        _coordinator_metrics_payload(
            ws_id=ws_id,
            spawns_total=spawns_total,
            spawns_last_hour=spawns_last_hour,
            child_state_counts=state_counts,
            judge_fallback_rate=judge_fallback_rate,
            intent_verdicts_sample=total_verdicts,
        )
    )


_RESTRICT_MAX_TOOLS = 256
_RESTRICT_MAX_TOOL_NAME_LEN = 128
# Bounded concurrency on bulk coordinator fan-out (stop_cascade,
# close_all_children).  Upstream coord_client calls have a 30s timeout;
# a 100-child cascade at this cap finishes in ~200s worst case,
# comfortably inside typical 300s proxy limits.
_COORD_FANOUT_MAX_CONCURRENCY = 16


async def _fanout_on_children(
    child_ids: list[str],
    coord_client: Any,
    action: Callable[[str], Any],
    *,
    log_tag: str,
    concurrency: int = _COORD_FANOUT_MAX_CONCURRENCY,
) -> tuple[list[str], list[str], list[str]]:
    """Bounded-concurrency fan-out over a coordinator's children.

    Returns ``(ok, failed, skipped)`` — ``ok`` = action succeeded,
    ``skipped`` = upstream 404 (already gone), ``failed`` = everything
    else (dispatch errors, exceptions, non-dict returns).  Routes all
    ids to ``failed`` when ``coord_client`` is None so the operator
    sees the unexpected state rather than a silent no-op.  Callers map
    the three buckets to endpoint-specific response keys (cancelled /
    closed / ...).
    """
    ok: list[str] = []
    failed: list[str] = []
    skipped: list[str] = []
    if not child_ids:
        return ok, failed, skipped
    if coord_client is None:
        return ok, list(child_ids), skipped

    sem = asyncio.Semaphore(concurrency)

    async def _one(cid: str) -> tuple[str, str]:
        async with sem:
            try:
                result = await asyncio.to_thread(action, cid)
                if not isinstance(result, dict):
                    return cid, "failed"
                if not result.get("error"):
                    return cid, "ok"
                # Stale registry entry (child row deleted) or upstream
                # 404 both mean "already gone".  Route to skipped so
                # operators can distinguish from dispatch failures.
                if result.get("status") == 404:
                    return cid, "skipped"
                return cid, "failed"
            except Exception:
                log.debug("%s.child_failed ws=%s", log_tag, cid[:8], exc_info=True)
                return cid, "failed"

    outcomes = await asyncio.gather(*(_one(cid) for cid in child_ids), return_exceptions=False)
    for cid, bucket in outcomes:
        if bucket == "ok":
            ok.append(cid)
        elif bucket == "skipped":
            skipped.append(cid)
        else:
            failed.append(cid)
    return ok, failed, skipped


async def _require_json_object(request: Request) -> dict[str, Any] | JSONResponse:
    """Parse the request body and require a JSON object.

    ``read_json_or_400`` only validates that the body parses as JSON,
    not that it's an object.  A ``null``/list/scalar body otherwise
    reaches ``body.get(...)`` and raises ``AttributeError`` → 500.
    """
    body = await read_json_or_400(request)
    if isinstance(body, JSONResponse):
        return body
    if not isinstance(body, dict):
        return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
    return body


async def _resolve_coord_session(
    request: Request,
    *,
    allow_service_bypass: bool = True,
) -> tuple[ChatSession, StorageBackend, str, str] | JSONResponse:
    """Return ``(session, storage, user_id, ws_id)`` or a gate-failure response.

    Destructive / capability-escalating handlers pass
    ``allow_service_bypass=False`` so a service token whose ``user_id``
    matches the coord owner still needs an explicit ``admin.coordinator``
    grant — matching the treatment ``/trust`` gives its perm gate.
    """
    err = _require_admin_coordinator(request, allow_service_bypass=allow_service_bypass)
    if err is not None:
        return err
    coord_mgr, err503 = _require_coord_mgr(request)
    if err503 is not None:
        return err503
    storage, err503s = require_storage_or_503(request)
    if err503s is not None:
        return err503s
    ws_id = request.path_params.get("ws_id", "")
    if not _VALID_WS_ID_RE.match(ws_id):
        return JSONResponse({"error": "invalid ws_id"}, status_code=400)
    user_id = _auth_user_id(request)
    ws, err404 = _resolve_coordinator_or_404(request, coord_mgr, storage, ws_id, user_id)
    if err404 is not None:
        return err404
    if ws is None or ws.session is None:
        return JSONResponse({"error": "coordinator not found"}, status_code=404)
    return ws.session, storage, user_id, ws_id


async def _emit_coord_audit(
    storage: StorageBackend,
    user_id: str,
    action: str,
    ws_id: str,
    detail: dict[str, Any],
    client_host: str,
) -> None:
    """Record a coordinator governance audit event off the event loop.

    Uses the console's dedicated audit executor when available so
    cancel-cascade bursts can't starve audit writes (and vice versa).
    Falls back to the default executor for test harnesses that don't
    wire one.
    """
    audit_exec = _audit_executor()
    loop = asyncio.get_running_loop()
    try:
        if audit_exec is not None:
            await loop.run_in_executor(
                audit_exec,
                record_audit,
                storage,
                user_id,
                action,
                "coordinator",
                ws_id,
                detail,
                client_host,
            )
        else:
            await asyncio.to_thread(
                record_audit,
                storage,
                user_id,
                action,
                "coordinator",
                ws_id,
                detail,
                client_host,
            )
    except Exception:
        log.debug("coord.audit.dispatch_failed", exc_info=True)


_audit_executor_ref: ThreadPoolExecutor | None = None


def _audit_executor() -> ThreadPoolExecutor | None:
    """Return the shared audit-writes executor, if the lifespan built one."""
    return _audit_executor_ref


def _set_audit_executor(executor: ThreadPoolExecutor | None) -> None:
    """Install or clear the process-wide audit executor (lifespan-owned)."""
    global _audit_executor_ref
    _audit_executor_ref = executor


async def coordinator_trust(request: Request) -> JSONResponse:
    """POST /v1/api/workstreams/{ws_id}/trust — toggle trusted-session mode."""
    trust_err = require_permission(request, "coordinator.trust.send", allow_service_bypass=False)
    if trust_err is not None:
        return trust_err
    resolved = await _resolve_coord_session(request)
    if isinstance(resolved, JSONResponse):
        return resolved
    session, storage, user_id, ws_id = resolved

    body = await _require_json_object(request)
    if isinstance(body, JSONResponse):
        return body
    raw_send = body.get("send")
    if not isinstance(raw_send, bool):
        return JSONResponse({"error": "body must carry {'send': bool}"}, status_code=400)

    before = session.get_trust_send()
    session.set_trust_send(raw_send)
    await _emit_coord_audit(
        storage,
        user_id,
        "coordinator.trust.toggled",
        ws_id,
        {"src": "coordinator", "send_before": before, "send_after": raw_send},
        request.client.host if request.client else "",
    )
    return JSONResponse({"status": "ok", "trust_send": raw_send})


async def coordinator_restrict(request: Request) -> JSONResponse:
    """POST /v1/api/workstreams/{ws_id}/restrict — revoke tool access mid-session."""
    resolved = await _resolve_coord_session(request, allow_service_bypass=False)
    if isinstance(resolved, JSONResponse):
        return resolved
    session, storage, user_id, ws_id = resolved

    body = await _require_json_object(request)
    if isinstance(body, JSONResponse):
        return body
    raw_revoke = body.get("revoke")
    if not isinstance(raw_revoke, list) or not all(isinstance(t, str) and t for t in raw_revoke):
        return JSONResponse(
            {"error": "body must carry {'revoke': [<tool_name>, ...]}"},
            status_code=400,
        )
    if len(raw_revoke) > _RESTRICT_MAX_TOOLS:
        return JSONResponse(
            {"error": f"revoke list exceeds {_RESTRICT_MAX_TOOLS} entries"},
            status_code=400,
        )
    if any(len(t) > _RESTRICT_MAX_TOOL_NAME_LEN for t in raw_revoke):
        return JSONResponse(
            {"error": f"tool names must be <= {_RESTRICT_MAX_TOOL_NAME_LEN} chars"},
            status_code=400,
        )

    additions = frozenset(raw_revoke)
    after = session.revoke_tools(additions)
    await _emit_coord_audit(
        storage,
        user_id,
        "coordinator.restricted",
        ws_id,
        {
            "src": "coordinator",
            "revoked": sorted(additions),
            "revoked_total": sorted(after),
        },
        request.client.host if request.client else "",
    )
    return JSONResponse({"status": "ok", "revoked_tools": sorted(after)})


async def coordinator_stop_cascade(request: Request) -> JSONResponse:
    """POST /v1/api/workstreams/{ws_id}/stop_cascade — cancel the subtree."""
    resolved = await _resolve_coord_session(request, allow_service_bypass=False)
    if isinstance(resolved, JSONResponse):
        return resolved
    session, storage, user_id, ws_id = resolved

    coord_mgr, err503 = _require_coord_mgr(request)
    if err503 is not None:
        return err503  # pragma: no cover — _resolve_coord_session already gated this
    coord_adapter = getattr(request.app.state, "coord_adapter", None)

    child_ids = list(coord_adapter.children_snapshot(ws_id)) if coord_adapter is not None else []
    coord_mgr.cancel(ws_id)

    coord_client: Any = getattr(session, "_coord_client", None)
    # ``action`` is only called when coord_client is live — the helper
    # short-circuits on None before invoking it.
    cancelled, failed, skipped = await _fanout_on_children(
        child_ids,
        coord_client,
        lambda cid: coord_client.cancel(cid),
        log_tag="coordinator_stop_cascade",
    )

    await _emit_coord_audit(
        storage,
        user_id,
        "coordinator.stopped_cascade",
        ws_id,
        {
            "src": "coordinator",
            "cancelled": cancelled,
            "failed": failed,
            "skipped": skipped,
        },
        request.client.host if request.client else "",
    )
    return JSONResponse(
        {
            "status": "ok",
            "cancelled": cancelled,
            "failed": failed,
            "skipped": skipped,
        }
    )


_CLOSE_ALL_CHILDREN_MAX_REASON_LEN = 512


async def coordinator_close_all_children(request: Request) -> JSONResponse:
    """POST /v1/api/workstreams/{ws_id}/close_all_children — soft-close the direct children.

    Near-twin of ``coordinator_stop_cascade`` — both fan out over
    ``children_snapshot`` via ``_fanout_on_children``.  Returns
    ``{closed, failed, skipped}``.  Unlike ``stop_cascade``, this does
    NOT recurse into grandchildren (the coordinator's model tool asks
    for a bounded teardown of its own fan-out; operator-level cascade
    stays behind ``stop_cascade``).
    """
    resolved = await _resolve_coord_session(request, allow_service_bypass=False)
    if isinstance(resolved, JSONResponse):
        return resolved
    session, storage, user_id, ws_id = resolved

    coord_mgr, err503 = _require_coord_mgr(request)
    if err503 is not None:
        return err503  # pragma: no cover — _resolve_coord_session already gated this
    del coord_mgr  # children_snapshot moved to the adapter
    coord_adapter = getattr(request.app.state, "coord_adapter", None)

    body = await _require_json_object(request)
    if isinstance(body, JSONResponse):
        return body
    raw_reason = body.get("reason", "")
    if raw_reason is not None and not isinstance(raw_reason, str):
        return JSONResponse(
            {"error": "reason must be a string"},
            status_code=400,
        )
    reason = (raw_reason or "").strip()
    if len(reason) > _CLOSE_ALL_CHILDREN_MAX_REASON_LEN:
        return JSONResponse(
            {"error": f"reason exceeds {_CLOSE_ALL_CHILDREN_MAX_REASON_LEN} chars"},
            status_code=400,
        )

    child_ids = list(coord_adapter.children_snapshot(ws_id)) if coord_adapter is not None else []

    coord_client: Any = getattr(session, "_coord_client", None)
    closed, failed, skipped = await _fanout_on_children(
        child_ids,
        coord_client,
        lambda cid: coord_client.close_workstream(cid, reason),
        log_tag="coordinator_close_all_children",
    )

    await _emit_coord_audit(
        storage,
        user_id,
        "coordinator.closed_all_children",
        ws_id,
        {
            "src": "coordinator",
            "reason": reason,
            "closed": closed,
            "failed": failed,
            "skipped": skipped,
        },
        request.client.host if request.client else "",
    )
    return JSONResponse(
        {
            "status": "ok",
            "closed": closed,
            "failed": failed,
            "skipped": skipped,
        }
    )


async def coordinator_tasks(request: Request) -> JSONResponse:
    """GET /v1/api/workstreams/{ws_id}/tasks — read task list envelope.

    Returns ``{"version": 1, "tasks": [...]}`` — the same shape the
    ``task_list(action="list")`` tool returns (less the list-tool's
    client-side 200-row slice; the UI handles its own pagination).

    Corrupt envelopes return an empty list for UI resilience.  The
    ``task_list`` tool remains the authoritative write path and surfaces
    corruption errors to the model on mutation attempts.
    """
    from turnstone.core.web_helpers import require_storage_or_503

    err = _require_admin_coordinator(request)
    if err is not None:
        return err
    coord_mgr, err503 = _require_coord_mgr(request)
    if err503 is not None:
        return err503
    storage, err503s = require_storage_or_503(request)
    if err503s is not None:
        return err503s

    ws_id = request.path_params.get("ws_id", "")
    if not _VALID_WS_ID_RE.match(ws_id):
        return JSONResponse({"error": "invalid ws_id"}, status_code=400)
    user_id = _auth_user_id(request)
    _ws, err404 = _resolve_coordinator_or_404(request, coord_mgr, storage, ws_id, user_id)
    if err404 is not None:
        return err404

    envelope, _corrupt = load_task_envelope(storage, ws_id)
    return JSONResponse(envelope)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


_PROBE_ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https"})
_PROBE_TIMEOUT_SECONDS = 2.0


def _probe_candidate_url(services: list[dict[str, Any]] | None) -> tuple[str, str]:
    """Pick the first service-registry entry usable as a scope-probe target.

    Returns ``(url, service_id)`` or ``("", "")`` when nothing is usable.
    The URL must carry an ``http``/``https`` scheme and a non-link-local
    host; the service-registry integrity is the primary defense, but
    the scheme + host filter here is defense-in-depth so a poisoned
    entry can't redirect the probe to a cloud metadata endpoint.
    """
    for svc in services or []:
        raw_url = (svc.get("url") or "").rstrip("/")
        nid = svc.get("service_id") or ""
        if not raw_url or not nid:
            continue
        try:
            parsed = urllib.parse.urlparse(raw_url)
        except ValueError:
            continue
        if parsed.scheme not in _PROBE_ALLOWED_SCHEMES:
            continue
        host = (parsed.hostname or "").lower()
        # 169.254.0.0/16 is the AWS / GCP instance metadata range;
        # an http target there would turn a compromised registry into
        # an SSRF to IMDS.  Loopback is retained for single-box dev.
        if host.startswith("169.254."):
            continue
        return raw_url, nid
    return "", ""


async def _verify_collector_service_scope(app: Starlette, client: httpx.AsyncClient) -> None:
    """Probe one upstream node to confirm the collector token's scopes.

    The console's :class:`ClusterCollector` authenticates to upstream
    nodes' ``/v1/api/events/global`` SSE endpoint, which is hard-gated
    on the ``service`` scope.  A collector token missing that scope
    silently 403s on every SSE connect and the cluster dashboard
    renders empty; this probe surfaces the drift at boot.

    Probes with ``expected_node_id=_scope-probe_`` so the upstream
    returns 409 (identity mismatch) immediately — a 409 means the
    scope gate was passed.  Any other 4xx = configuration drift:
    ``app.state.collector_scope_error`` is set non-empty, ``log.error``
    fires, and the cluster-snapshot endpoints return 503 with a
    remediation hint so the operator sees the problem in the first
    failing UI load instead of days later.

    Transient failures (network errors, 5xx, no nodes discovered yet)
    are logged at info/warning and do NOT refuse to serve — they
    can't be distinguished from legitimate "cluster is coming up"
    states.
    """
    storage = getattr(app.state, "auth_storage", None)
    token_mgr = getattr(app.state, "collector_token_mgr", None)
    if storage is None or token_mgr is None:
        log.info("collector_scope_probe.skipped reason=storage_or_token_missing")
        return
    # list_services is blocking DB I/O — offload so a slow / Postgres
    # backend doesn't stall the event loop during the probe window.
    try:
        services = await asyncio.to_thread(storage.list_services, "server", max_age_seconds=120)
    except Exception:
        # Storage-backend drift is itself a class of configuration
        # error worth surfacing — the original silent-skip here hid
        # exactly the kind of failure the probe was added to catch.
        log.warning(
            "collector_scope_probe.service_registry_unavailable",
            exc_info=True,
        )
        return
    probe_url, probe_node = _probe_candidate_url(services)
    if not probe_url:
        # Distinguish "registry empty" (normal pre-discovery) from
        # "registry populated but every entry malformed" (operator-
        # actionable drift) so the two aren't both logged as INFO
        # silent-skips.
        if services:
            log.warning(
                "collector_scope_probe.registry_malformed count=%d",
                len(services),
            )
        else:
            log.info("collector_scope_probe.skipped reason=no_nodes_registered")
        return
    headers = {"Authorization": f"Bearer {token_mgr.token}"}
    probe_target = f"{probe_url}/v1/api/events/global"
    try:
        resp = await client.get(
            probe_target,
            params={"expected_node_id": "_scope-probe_"},
            headers=headers,
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
    except (httpx.HTTPError, TimeoutError) as exc:
        log.warning(
            "collector_scope_probe.transient_error node=%s url=%s — %s",
            probe_node,
            probe_target,
            exc,
        )
        return
    status = resp.status_code
    if status == 409:
        # Identity mismatch is expected — it means the scope gate was
        # accepted and the handler got as far as the node_id check.
        log.info("collector_scope_probe.ok node=%s url=%s", probe_node, probe_target)
        return
    body_preview = _bounded_body_preview(resp.text)
    if 400 <= status < 500:
        # 403 = missing ``service`` scope on the collector token; 401 =
        # JWT rejected outright (secret mismatch / audience drift).
        # Either way refuse to serve the dashboard until the operator
        # fixes it.  Upstream body is attacker-controllable so we
        # delimit it explicitly in the 503 error text; the preview
        # already passed through the control-char scrub in
        # _bounded_body_preview.
        app.state.collector_scope_error = (
            f"collector token rejected by {probe_node} ({probe_target}): "
            f"HTTP {status} — upstream_body=<<<{body_preview}>>>. "
            "The collector's ServiceTokenManager scopes must include "
            "'service' and the JWT audience must match what upstream "
            "enforces."
        )
        log.error(
            "collector_scope_probe.drift node=%s url=%s status=%d — %s",
            probe_node,
            probe_target,
            status,
            body_preview,
        )
        return
    # 5xx / unexpected: likely a transient upstream problem rather
    # than our configuration — warn but don't refuse to serve.
    log.warning(
        "collector_scope_probe.unexpected_status node=%s url=%s status=%d — %s",
        probe_node,
        probe_target,
        status,
        body_preview,
    )


@asynccontextmanager
async def _lifespan(app: Starlette) -> AsyncGenerator[None, None]:
    # Create async HTTP clients for proxy routes.  Auth headers are NOT baked
    # in — _proxy_auth_headers() injects a fresh token per-request so JWTs
    # auto-rotate via ServiceTokenManager instead of expiring after 1 hour.
    # Size the pool above the fan-out limit to leave headroom for non-fan-out
    # proxy traffic (UI proxying, SSE streams, etc.).
    #
    # Build a ConfigStore so console settings reads get type validation and
    # caching instead of raw storage.get_system_setting() calls.
    storage = getattr(app.state, "auth_storage", None)
    config_store = None
    if storage:
        try:
            from turnstone.core.config_store import ConfigStore

            config_store = ConfigStore(storage)
        except Exception:
            log.warning("Failed to initialise ConfigStore", exc_info=True)
    app.state.config_store = config_store

    # Initialize rule registry for configurable judge rules
    app.state.rule_registry = None
    if config_store is not None:
        try:
            from turnstone.core.rule_registry import RuleRegistry

            app.state.rule_registry = RuleRegistry(storage=config_store.storage)
        except Exception:
            log.warning("Failed to initialise RuleRegistry", exc_info=True)

    fan_out = (
        config_store.get("cluster.node_fan_out_limit") if config_store else _NODE_FAN_OUT_LIMIT
    )
    app.state.fan_out_limit = fan_out
    # Build mTLS context for proxy clients if TLS is enabled
    _tls_mgr = getattr(app.state, "tls_manager", None)
    _proxy_ssl = _tls_mgr.get_client_ssl_context() if _tls_mgr and _tls_mgr.ca_initialized else None
    _proxy_verify: Any = _proxy_ssl if _proxy_ssl else True

    app.state.proxy_client = httpx.AsyncClient(
        timeout=30,
        limits=httpx.Limits(
            max_connections=fan_out + 50,
            max_keepalive_connections=min(fan_out // 4, 100),
        ),
        verify=_proxy_verify,
    )
    app.state.proxy_sse_client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=5, read=30, write=5, pool=5),
        limits=httpx.Limits(
            max_connections=1100, max_keepalive_connections=100, keepalive_expiry=30
        ),
        verify=_proxy_verify,
    )
    # Short-TTL per-node /v1/api/dashboard cache — coalesces the
    # cluster_ws_detail fan-in so N concurrent child-inspect calls to
    # the same node collapse to one upstream GET per TTL window.
    app.state.dashboard_cache = _NodeDashboardCache()
    # Dedicated small executor for governance audit writes.  Without
    # this, audit dispatches share the default thread pool with
    # ``coord_client.cancel`` calls from ``stop_cascade`` and any
    # other ``asyncio.to_thread`` caller — a burst on one path can
    # starve the other.  4 workers is ample headroom for
    # admin-driven audit traffic.
    audit_exec = ThreadPoolExecutor(max_workers=4, thread_name_prefix="coord-audit")
    app.state.audit_executor = audit_exec
    _set_audit_executor(audit_exec)
    # Populate the router's services cache if a router is configured
    _router: ConsoleRouter | None = getattr(app.state, "router", None)
    if _router is not None:
        _router.refresh_cache()
        if not _router.is_ready():
            log.warning("Router cache is empty after refresh — no nodes assigned")
    # Prove the collector's service-auth token is accepted by an
    # upstream node before the lifespan yields — a scope mismatch
    # otherwise only surfaces once an operator notices missing
    # dashboard rows.  See :func:`_verify_collector_service_scope`.
    await _verify_collector_service_scope(app, app.state.proxy_client)
    # Start scheduler if configured
    scheduler = getattr(app.state, "scheduler", None)
    if scheduler is not None:
        scheduler.start()
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
    # Register console in service registry so other services can discover it
    console_url = getattr(app.state, "console_url", "")
    _console_heartbeat_task: Any = None
    if console_url and storage:
        try:
            storage.register_service("console", "console", console_url)

            # Periodic heartbeat to keep the registration alive
            import asyncio

            async def _console_heartbeat() -> None:
                from turnstone.core.storage._registry import StorageUnavailableError

                while True:
                    await asyncio.sleep(30)
                    try:
                        storage.heartbeat_service("console", "console")
                    except StorageUnavailableError:
                        pass  # already logged by storage layer
                    except Exception:
                        log.warning("console.heartbeat_failed", exc_info=True)

            _console_heartbeat_task = asyncio.create_task(_console_heartbeat())
        except Exception:
            log.warning("Failed to register console service", exc_info=True)

    # TLS: init CA, issue console certs, start renewal
    tls_mgr = getattr(app.state, "tls_manager", None)
    if tls_mgr is not None:
        import socket

        try:
            if not tls_mgr.ca_initialized:
                await tls_mgr.init_ca()
            hostname = socket.gethostname()
            fqdn = socket.getfqdn()
            cert_hostnames = [hostname, "localhost", "127.0.0.1"]
            if fqdn != hostname:
                cert_hostnames.append(fqdn)
            extra_sans = os.environ.get("TURNSTONE_TLS_SANS", "")
            if extra_sans:
                cert_hostnames.extend(s.strip() for s in extra_sans.split(",") if s.strip())
            await tls_mgr.issue_console_certs(cert_hostnames)
            await tls_mgr.start_renewal()
            # Re-create proxy clients with mTLS context now that certs are ready
            client_ctx = tls_mgr.get_client_ssl_context()
            if client_ctx:
                await app.state.proxy_client.aclose()
                await app.state.proxy_sse_client.aclose()
                app.state.proxy_client = httpx.AsyncClient(
                    timeout=30,
                    limits=httpx.Limits(
                        max_connections=app.state.fan_out_limit + 50,
                        max_keepalive_connections=min(app.state.fan_out_limit // 4, 100),
                    ),
                    verify=client_ctx,
                )
                app.state.proxy_sse_client = httpx.AsyncClient(
                    timeout=httpx.Timeout(connect=5, read=30, write=5, pool=5),
                    limits=httpx.Limits(
                        max_connections=1100,
                        max_keepalive_connections=100,
                        keepalive_expiry=30,
                    ),
                    verify=client_ctx,
                )
                # Upgrade collector httpx client for mTLS node polling
                app.state.collector.upgrade_tls(tls_verify=client_ctx)
                log.info("tls.proxy_clients.upgraded")
        except Exception:
            log.warning("TLS initialization failed — continuing without TLS", exc_info=True)

    # Coordinator workstream plumbing.  Lazy — failure here is non-fatal;
    # the coordinator endpoints return 503 with a remediation message
    # when coord_mgr is None, so the rest of the console still works.
    app.state.coord_mgr = None
    app.state.coord_adapter = None
    app.state.coord_registry = None
    app.state.coord_registry_error = ""
    if storage and config_store:
        try:
            from turnstone.core.model_registry import load_model_registry

            try:
                coord_registry = load_model_registry(storage=storage)
                app.state.coord_registry = coord_registry
            except ValueError as exc:
                # No model rows configured.  Endpoint returns 503 with
                # the error text so admin sees remediation in the UI.
                app.state.coord_registry_error = str(exc)
                coord_registry = None

            if coord_registry is not None:
                from turnstone.console.collector import ClusterCollector
                from turnstone.console.coordinator_adapter import CoordinatorAdapter
                from turnstone.console.coordinator_client import (
                    CoordinatorClient,
                    CoordinatorTokenManager,
                )
                from turnstone.console.coordinator_ui import ConsoleCoordinatorUI
                from turnstone.console.session_factory import (
                    build_console_session_factory,
                )
                from turnstone.core.session_manager import SessionManager
                from turnstone.core.workstream import Workstream

                jwt_secret: str = getattr(app.state, "jwt_secret", "")
                console_bind_url: str = getattr(app.state, "console_url", "") or (
                    "http://127.0.0.1:8001"
                )

                def _ui_factory(ws: Workstream) -> ConsoleCoordinatorUI:
                    return ConsoleCoordinatorUI(ws_id=ws.id, user_id=ws.user_id or "")

                def _coord_client_factory(ws_id: str, user_id: str) -> CoordinatorClient:
                    ttl = int(config_store.get("coordinator.session_jwt_ttl_seconds"))
                    tm = CoordinatorTokenManager(
                        user_id=user_id or "system",
                        scopes=frozenset({"read", "write", "approve"}),
                        permissions=frozenset({"admin.coordinator"}),
                        secret=jwt_secret,
                        coord_ws_id=ws_id,
                        ttl_seconds=ttl,
                    )

                    def _token_factory() -> str:
                        return tm.token

                    return CoordinatorClient(
                        console_base_url=console_bind_url,
                        storage=storage,
                        token_factory=_token_factory,
                        coord_ws_id=ws_id,
                        user_id=user_id,
                    )

                coord_factory = build_console_session_factory(
                    registry=coord_registry,
                    config_store=config_store,
                    node_id="console",
                    coord_client_factory=_coord_client_factory,
                )
                coord_adapter = CoordinatorAdapter(
                    collector=app.state.collector,
                    ui_factory=_ui_factory,
                    session_factory=coord_factory,
                )
                coord_mgr = SessionManager(
                    coord_adapter,
                    storage=storage,
                    max_active=int(config_store.get("coordinator.max_active")),
                    node_id=ClusterCollector.CONSOLE_PSEUDO_NODE_ID,
                )
                # Late-bind the manager onto the adapter so
                # ``_rebuild_children_registry`` / ``send`` /
                # fan-out dispatch can call ``mgr.get(ws_id)``.
                coord_adapter.attach(coord_mgr)
                # Shared refs so ConsoleCoordinatorUI.on_state_change
                # flows state transitions through the unified manager
                # and on_rename fans out to the cluster dashboard.
                ConsoleCoordinatorUI._coord_mgr = coord_mgr
                ConsoleCoordinatorUI._collector = app.state.collector
                app.state.coord_mgr = coord_mgr
                app.state.coord_adapter = coord_adapter
                # Wire the cluster-event subscription so the coordinator's
                # SSE stream fans out filtered child_ws_* events.  Safe to
                # call even when the collector has no nodes yet — the
                # subscription just sits idle until the first node event.
                try:
                    coord_adapter.start_child_event_fanout(app.state.collector)
                except Exception:
                    log.warning("console.coordinator_child_fanout_init_failed", exc_info=True)
                log.info(
                    "console.coordinator_mgr_ready max_active=%s",
                    config_store.get("coordinator.max_active"),
                )
        except Exception:
            log.warning("console.coordinator_init_failed", exc_info=True)

    yield
    # Shutdown
    if _console_heartbeat_task is not None:
        _console_heartbeat_task.cancel()
    # Deregister console from services table
    if console_url and storage:
        try:
            storage.deregister_service("console", "console")
        except Exception:
            log.debug("console.deregister_failed", exc_info=True)
    tls_mgr = getattr(app.state, "tls_manager", None)
    if tls_mgr is not None:
        await tls_mgr.stop_renewal()
    if scheduler is not None:
        scheduler.stop()
    coord_adapter_shutdown = getattr(app.state, "coord_adapter", None)
    if coord_adapter_shutdown is not None:
        try:
            coord_adapter_shutdown.shutdown()
        except Exception:
            log.debug("console.coord_adapter_shutdown_failed", exc_info=True)
    # Drop the shared ConsoleCoordinatorUI refs on teardown so tests
    # that spin up multiple lifespan instances don't carry stale
    # manager/collector references across them.
    try:
        from turnstone.console.coordinator_ui import ConsoleCoordinatorUI

        ConsoleCoordinatorUI._coord_mgr = None
        ConsoleCoordinatorUI._collector = None
    except Exception:
        log.debug("console.coord_ui_refs_reset_failed", exc_info=True)
    await app.state.proxy_sse_client.aclose()
    await app.state.proxy_client.aclose()
    app.state.collector.stop()
    audit_exec_shutdown = getattr(app.state, "audit_executor", None)
    if audit_exec_shutdown is not None:
        _set_audit_executor(None)
        audit_exec_shutdown.shutdown(wait=True)


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------
# Admin API endpoints — user + token management
# ---------------------------------------------------------------------------


async def admin_list_users(request: Request) -> JSONResponse:
    """GET /v1/api/admin/users — list all users."""
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.users")
    if err:
        return err
    return JSONResponse({"users": storage.list_users()})


async def admin_create_user(request: Request) -> JSONResponse:
    """POST /v1/api/admin/users — create a new user."""
    import uuid

    from turnstone.core.auth import hash_password, require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.users")
    if err:
        return err

    from turnstone.core.web_helpers import read_json_or_400

    body = await read_json_or_400(request)
    if isinstance(body, JSONResponse):
        return body

    username = body.get("username", "").strip()
    display_name = body.get("display_name", "").strip()
    password = body.get("password", "")

    from turnstone.core.auth import is_valid_username

    if not is_valid_username(username):
        return JSONResponse(
            {"error": "Invalid username (1-64 chars: letters, digits, . _ -)"},
            status_code=400,
        )
    if not display_name:
        return JSONResponse({"error": "display_name is required"}, status_code=400)
    if not password or len(password) < 8:
        return JSONResponse({"error": "Password must be at least 8 characters"}, status_code=400)

    # Check username uniqueness
    if storage.get_user_by_username(username) is not None:
        return JSONResponse({"error": "Username already taken"}, status_code=409)

    user_id = uuid.uuid4().hex
    pw_hash = hash_password(password)
    storage.create_user(user_id, username, display_name, pw_hash)

    from turnstone.core.audit import record_audit

    audit_uid, ip = _audit_context(request)
    record_audit(
        storage,
        audit_uid,
        "user.create",
        "user",
        user_id,
        {"username": username},
        ip,
    )

    # Read back to get the storage-canonical created timestamp
    user = storage.get_user(user_id)
    return JSONResponse(
        {
            "user_id": user["user_id"],
            "username": user["username"],
            "display_name": user["display_name"],
            "created": user["created"],
        }
    )


async def admin_delete_user(request: Request) -> JSONResponse:
    """DELETE /v1/api/admin/users/{user_id} — delete user + cascade tokens."""
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.users")
    if err:
        return err
    user_id = request.path_params["user_id"]
    # Prevent self-deletion
    auth_result = getattr(request.state, "auth_result", None)
    if auth_result and auth_result.user_id == user_id:
        return JSONResponse({"error": "Cannot delete your own account"}, status_code=400)
    # Look up username for the audit trail before deleting
    target_user = storage.get_user(user_id)
    if storage.delete_user(user_id):
        from turnstone.core.audit import record_audit

        audit_uid, ip = _audit_context(request)
        record_audit(
            storage,
            audit_uid,
            "user.delete",
            "user",
            user_id,
            {"username": target_user.get("username", "") if target_user else ""},
            ip,
        )
        return JSONResponse({"status": "ok"})
    return JSONResponse({"error": "User not found"}, status_code=404)


async def admin_list_tokens(request: Request) -> JSONResponse:
    """GET /v1/api/admin/users/{user_id}/tokens — list tokens for a user."""
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.users")
    if err:
        return err
    user_id = request.path_params["user_id"]
    return JSONResponse({"tokens": storage.list_api_tokens(user_id)})


async def admin_create_token(request: Request) -> JSONResponse:
    """POST /v1/api/admin/users/{user_id}/tokens — create API token."""
    import uuid

    from turnstone.core.auth import generate_token, hash_token, require_permission, token_prefix
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.users")
    if err:
        return err
    user_id = request.path_params["user_id"]

    # Verify user exists
    if storage.get_user(user_id) is None:
        return JSONResponse({"error": "User not found"}, status_code=404)

    try:
        body: dict[str, Any] = await request.json()
    except (ValueError, json.JSONDecodeError):
        body = {}

    name = body.get("name", "")
    scopes = body.get("scopes", "read,write,approve")
    expires_days = body.get("expires_days")

    # Validate scopes
    from turnstone.core.auth import VALID_SCOPES

    requested = {s.strip() for s in scopes.split(",") if s.strip()}
    if not requested or not requested.issubset(VALID_SCOPES):
        return JSONResponse(
            {"error": "Invalid scopes (allowed: read, write, approve)"}, status_code=400
        )

    expires: str | None = None
    if expires_days is not None:
        from datetime import UTC, datetime, timedelta

        expires = (datetime.now(UTC) + timedelta(days=int(expires_days))).strftime(
            "%Y-%m-%dT%H:%M:%S"
        )

    raw = generate_token()
    tid = uuid.uuid4().hex
    storage.create_api_token(
        token_id=tid,
        token_hash=hash_token(raw),
        token_prefix=token_prefix(raw),
        user_id=user_id,
        name=name,
        scopes=scopes,
        expires=expires,
    )

    from turnstone.core.audit import record_audit

    audit_uid, ip = _audit_context(request)
    record_audit(
        storage,
        audit_uid,
        "token.create",
        "token",
        tid,
        {"name": name},
        ip,
    )

    return JSONResponse(
        {
            "token": raw,
            "token_id": tid,
            "token_prefix": token_prefix(raw),
            "scopes": scopes,
        }
    )


async def admin_revoke_token(request: Request) -> JSONResponse:
    """DELETE /v1/api/admin/tokens/{token_id} — revoke an API token."""
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.users")
    if err:
        return err
    token_id = request.path_params["token_id"]
    if storage.delete_api_token(token_id):
        from turnstone.core.audit import record_audit

        audit_uid, ip = _audit_context(request)
        record_audit(
            storage,
            audit_uid,
            "token.revoke",
            "token",
            token_id,
            {},
            ip,
        )
        return JSONResponse({"status": "ok"})
    return JSONResponse({"error": "Token not found"}, status_code=404)


# ---------------------------------------------------------------------------
# Admin: Channel user mapping
# ---------------------------------------------------------------------------


async def admin_list_channels(request: Request) -> JSONResponse:
    """GET /v1/api/admin/users/{user_id}/channels — list channel links for a user."""
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.users")
    if err:
        return err
    user_id = request.path_params["user_id"]
    channels = storage.list_channel_users_by_user(user_id)
    return JSONResponse({"channels": channels})


async def admin_create_channel(request: Request) -> JSONResponse:
    """POST /v1/api/admin/users/{user_id}/channels — link a channel account."""
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.users")
    if err:
        return err
    user_id = request.path_params["user_id"]

    from turnstone.core.web_helpers import read_json_or_400

    body = await read_json_or_400(request)
    if isinstance(body, JSONResponse):
        return body

    channel_type = body.get("channel_type", "").strip().lower()
    channel_user_id = body.get("channel_user_id", "").strip()

    if not channel_type:
        return JSONResponse({"error": "channel_type is required"}, status_code=400)
    if not channel_user_id:
        return JSONResponse({"error": "channel_user_id is required"}, status_code=400)
    if len(channel_type) > 64 or len(channel_user_id) > 256:
        return JSONResponse({"error": "Value too long"}, status_code=400)

    # Verify user exists
    if storage.get_user(user_id) is None:
        return JSONResponse({"error": "User not found"}, status_code=404)

    # Check for existing mapping
    existing = storage.get_channel_user(channel_type, channel_user_id)
    if existing is not None:
        return JSONResponse(
            {"error": f"Channel user already linked to user {existing['user_id']}"},
            status_code=409,
        )

    storage.create_channel_user(channel_type, channel_user_id, user_id)
    result = storage.get_channel_user(channel_type, channel_user_id)
    if result is None:
        return JSONResponse({"error": "Failed to create channel mapping"}, status_code=500)
    # Guard against race: another request may have claimed this channel_user_id.
    if result.get("user_id") != user_id:
        return JSONResponse(
            {"error": f"Channel user already linked to user {result['user_id']}"},
            status_code=409,
        )

    from turnstone.core.audit import record_audit

    audit_uid, ip = _audit_context(request)
    record_audit(
        storage,
        audit_uid,
        "channel.link",
        "channel",
        channel_user_id,
        {"channel_type": channel_type, "user_id": user_id},
        ip,
    )

    return JSONResponse(result)


async def admin_delete_channel(request: Request) -> JSONResponse:
    """DELETE /v1/api/admin/channels/{channel_type}/{channel_user_id} — unlink."""
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.users")
    if err:
        return err
    channel_type = request.path_params["channel_type"]
    channel_user_id = request.path_params["channel_user_id"]
    if storage.delete_channel_user(channel_type, channel_user_id):
        from turnstone.core.audit import record_audit

        audit_uid, ip = _audit_context(request)
        record_audit(
            storage,
            audit_uid,
            "channel.unlink",
            "channel",
            channel_user_id,
            {"channel_type": channel_type},
            ip,
        )
        return JSONResponse({"status": "ok"})
    return JSONResponse({"error": "Channel link not found"}, status_code=404)


# ---------------------------------------------------------------------------
# Admin API endpoints — OIDC identities
# ---------------------------------------------------------------------------


async def admin_list_oidc_identities(request: Request) -> JSONResponse:
    """GET /v1/api/admin/users/{user_id}/oidc-identities — list OIDC links for a user."""
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.users")
    if err:
        return err

    user_id = request.path_params["user_id"]
    identities = storage.list_oidc_identities_for_user(user_id)
    return JSONResponse({"oidc_identities": identities})


async def admin_delete_oidc_identity(request: Request) -> JSONResponse:
    """DELETE /v1/api/admin/oidc-identities?issuer=...&subject=... — unlink OIDC identity."""
    from turnstone.core.audit import record_audit
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.users")
    if err:
        return err

    issuer = request.query_params.get("issuer", "")
    subject = request.query_params.get("subject", "")
    if not issuer or not subject:
        return JSONResponse({"error": "issuer and subject required"}, status_code=400)

    # Look up before delete so audit captures which user was affected
    identity = storage.get_oidc_identity(issuer, subject)
    if not identity:
        return JSONResponse({"error": "Identity not found"}, status_code=404)

    storage.delete_oidc_identity(issuer, subject)

    audit_uid, ip = _audit_context(request)
    record_audit(
        storage,
        audit_uid,
        "oidc_identity.delete",
        "oidc_identity",
        f"{issuer}:{subject}",
        {"user_id": identity["user_id"]},
        ip,
    )

    return JSONResponse({"status": "ok"})


# ---------------------------------------------------------------------------
# Admin API endpoints — scheduled tasks
# ---------------------------------------------------------------------------


def _normalize_task_dict(task: dict[str, Any]) -> dict[str, Any]:
    """Convert DB row ints/csv to JSON-friendly bools/lists."""
    tools_str = task.get("auto_approve_tools", "")
    task["auto_approve_tools"] = [s.strip() for s in tools_str.split(",") if s.strip()]
    task["auto_approve"] = bool(task.get("auto_approve", 0))
    task["enabled"] = bool(task.get("enabled", 1))
    # Normalize notify_targets from JSON string to list
    import json as _json

    raw_nt = task.get("notify_targets", "[]")
    try:
        task["notify_targets"] = _json.loads(raw_nt) if isinstance(raw_nt, str) else raw_nt
    except (_json.JSONDecodeError, TypeError):
        task["notify_targets"] = []
    return task


def _compute_next_run(schedule_type: str, cron_expr: str, at_time: str) -> str:
    """Compute the next run time for a schedule. Empty string if invalid."""
    if schedule_type == "at":
        return at_time
    if schedule_type == "cron" and cron_expr:
        from datetime import UTC, datetime

        from croniter import croniter

        cron = croniter(cron_expr, datetime.now(UTC))
        next_dt = cron.get_next(datetime)
        return str(next_dt.strftime("%Y-%m-%dT%H:%M:%S"))
    return ""


def _validate_schedule_fields(schedule_type: str, cron_expr: str, at_time: str) -> str | None:
    """Validate schedule type/expression. Returns error string or None."""
    if schedule_type not in ("cron", "at"):
        return "schedule_type must be 'cron' or 'at'"
    if schedule_type == "cron":
        if not cron_expr:
            return "cron_expr is required when schedule_type is 'cron'"
        from croniter import croniter

        if not croniter.is_valid(cron_expr):
            return f"Invalid cron expression: {cron_expr}"
    if schedule_type == "at":
        if not at_time:
            return "at_time is required when schedule_type is 'at'"
        from datetime import UTC, datetime

        try:
            dt = datetime.fromisoformat(at_time)
            if dt.tzinfo is None:
                return (
                    "at_time must include a timezone offset (e.g. 2024-01-01T12:00:00Z or +00:00)"
                )
            if dt <= datetime.now(UTC):
                return "at_time must be in the future"
        except ValueError:
            return "at_time must be a valid ISO8601 timestamp with timezone"
    return None


async def admin_list_schedules(request: Request) -> JSONResponse:
    """GET /v1/api/admin/schedules — list all scheduled tasks."""
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.schedules")
    if err:
        return err
    tasks = storage.list_scheduled_tasks()
    for t in tasks:
        _normalize_task_dict(t)
    return JSONResponse({"schedules": tasks})


async def admin_create_schedule(request: Request) -> JSONResponse:
    """POST /v1/api/admin/schedules — create a scheduled task."""
    import uuid

    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import read_json_or_400, require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.schedules")
    if err:
        return err

    body = await read_json_or_400(request)
    if isinstance(body, JSONResponse):
        return body

    name = str(body.get("name", "")).strip()[:256]
    description = str(body.get("description", "")).strip()[:1024]
    schedule_type = str(body.get("schedule_type", "")).strip()
    cron_expr = str(body.get("cron_expr", "")).strip()[:256]
    at_time = str(body.get("at_time", "")).strip()[:64]
    target_mode = str(body.get("target_mode", "auto")).strip()[:256]
    model = str(body.get("model", "")).strip()[:128]
    initial_message = str(body.get("initial_message", "")).strip()[:4096]
    auto_approve = bool(body.get("auto_approve", False))
    raw_tools = body.get("auto_approve_tools", [])
    auto_approve_tools = raw_tools if isinstance(raw_tools, list) else []
    skill_name = str(body.get("skill", "")).strip()[:256]
    enabled = bool(body.get("enabled", True))

    # Validate notify_targets
    from turnstone.server import _validate_notify_targets

    raw_nt = body.get("notify_targets", "[]")
    if isinstance(raw_nt, list):
        import json as _json

        raw_nt = _json.dumps(raw_nt)
    notify_targets, nt_err = _validate_notify_targets(raw_nt)
    if nt_err:
        return JSONResponse({"error": nt_err}, status_code=400)

    if not name:
        return JSONResponse({"error": "name is required"}, status_code=400)
    if not initial_message:
        return JSONResponse({"error": "initial_message is required"}, status_code=400)
    if skill_name and not storage.get_prompt_template_by_name(skill_name):
        return JSONResponse({"error": f"Skill not found: {skill_name}"}, status_code=400)

    validation_err = _validate_schedule_fields(schedule_type, cron_expr, at_time)
    if validation_err:
        return JSONResponse({"error": validation_err}, status_code=400)

    if not target_mode:
        return JSONResponse({"error": "target_mode is required"}, status_code=400)

    # Cap total schedule count to prevent unbounded growth
    max_schedules = 200
    existing = storage.list_scheduled_tasks()
    if len(existing) >= max_schedules:
        return JSONResponse(
            {"error": f"Maximum of {max_schedules} schedules reached"}, status_code=409
        )

    next_run = _compute_next_run(schedule_type, cron_expr, at_time)
    task_id = uuid.uuid4().hex
    created_by = getattr(getattr(request, "state", None), "user_id", "")

    storage.create_scheduled_task(
        task_id=task_id,
        name=name,
        description=description,
        schedule_type=schedule_type,
        cron_expr=cron_expr,
        at_time=at_time,
        target_mode=target_mode,
        model=model,
        initial_message=initial_message,
        auto_approve=auto_approve,
        auto_approve_tools=auto_approve_tools,
        created_by=created_by,
        next_run=next_run if enabled else "",
        skill=skill_name,
        notify_targets=notify_targets,
    )

    if not enabled:
        # Storage backends default enabled=1 on create; persist user's choice
        storage.update_scheduled_task(task_id, enabled=False)

    task = storage.get_scheduled_task(task_id)
    if task:
        _normalize_task_dict(task)
    return JSONResponse(task)


async def admin_get_schedule(request: Request) -> JSONResponse:
    """GET /v1/api/admin/schedules/{task_id} — get single task."""
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.schedules")
    if err:
        return err
    task_id = request.path_params["task_id"]
    task = storage.get_scheduled_task(task_id)
    if task is None:
        return JSONResponse({"error": "Schedule not found"}, status_code=404)
    _normalize_task_dict(task)
    return JSONResponse(task)


async def admin_update_schedule(request: Request) -> JSONResponse:
    """PUT /v1/api/admin/schedules/{task_id} — partial update."""
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import read_json_or_400, require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.schedules")
    if err:
        return err
    task_id = request.path_params["task_id"]

    existing = storage.get_scheduled_task(task_id)
    if existing is None:
        return JSONResponse({"error": "Schedule not found"}, status_code=404)

    body = await read_json_or_400(request)
    if isinstance(body, JSONResponse):
        return body

    updates: dict[str, Any] = {}
    if "name" in body:
        updates["name"] = str(body["name"]).strip()[:256]
    if "description" in body:
        updates["description"] = str(body["description"]).strip()[:1024]
    if "schedule_type" in body:
        updates["schedule_type"] = str(body["schedule_type"]).strip()
    if "cron_expr" in body:
        updates["cron_expr"] = str(body["cron_expr"]).strip()[:256]
    if "at_time" in body:
        updates["at_time"] = str(body["at_time"]).strip()[:64]
    if "target_mode" in body:
        updates["target_mode"] = str(body["target_mode"]).strip()[:256]
    if "model" in body:
        updates["model"] = str(body["model"]).strip()[:128]
    if "initial_message" in body:
        updates["initial_message"] = str(body["initial_message"]).strip()[:4096]
    if "auto_approve" in body:
        updates["auto_approve"] = bool(body["auto_approve"])
    if "auto_approve_tools" in body:
        raw = body["auto_approve_tools"]
        updates["auto_approve_tools"] = raw if isinstance(raw, list) else []
    if "skill" in body:
        skill_val = str(body["skill"]).strip()[:256]
        if skill_val and not storage.get_prompt_template_by_name(skill_val):
            return JSONResponse({"error": f"Skill not found: {skill_val}"}, status_code=400)
        updates["skill"] = skill_val
    if "enabled" in body:
        updates["enabled"] = bool(body["enabled"])
    if "notify_targets" in body:
        from turnstone.server import _validate_notify_targets

        raw_nt = body["notify_targets"]
        if isinstance(raw_nt, list):
            import json as _json

            raw_nt = _json.dumps(raw_nt)
        nt_str, nt_err = _validate_notify_targets(raw_nt)
        if nt_err:
            return JSONResponse({"error": nt_err}, status_code=400)
        updates["notify_targets"] = nt_str

    # Validate schedule fields if changed
    stype = updates.get("schedule_type", existing["schedule_type"])
    cexpr = updates.get("cron_expr", existing["cron_expr"])
    atime = updates.get("at_time", existing["at_time"])
    schedule_fields_changed = (
        "schedule_type" in updates or "cron_expr" in updates or "at_time" in updates
    )
    if schedule_fields_changed:
        validation_err = _validate_schedule_fields(stype, cexpr, atime)
        if validation_err:
            return JSONResponse({"error": validation_err}, status_code=400)

    # Recompute next_run if schedule changed or enabled toggled
    if schedule_fields_changed or "enabled" in updates:
        enabled = updates.get("enabled", bool(existing.get("enabled", 1)))
        if enabled:
            # Re-validate at_time when re-enabling a one-shot task
            if stype == "at" and not schedule_fields_changed:
                validation_err = _validate_schedule_fields(stype, cexpr, atime)
                if validation_err:
                    return JSONResponse({"error": validation_err}, status_code=400)
            updates["next_run"] = _compute_next_run(stype, cexpr, atime)
        else:
            updates["next_run"] = ""

    storage.update_scheduled_task(task_id, **updates)
    task = storage.get_scheduled_task(task_id)
    if task:
        _normalize_task_dict(task)
    return JSONResponse(task)


async def admin_delete_schedule(request: Request) -> JSONResponse:
    """DELETE /v1/api/admin/schedules/{task_id} — delete task + runs."""
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.schedules")
    if err:
        return err
    task_id = request.path_params["task_id"]
    if storage.delete_scheduled_task(task_id):
        return JSONResponse({"status": "ok"})
    return JSONResponse({"error": "Schedule not found"}, status_code=404)


async def admin_list_schedule_runs(request: Request) -> JSONResponse:
    """GET /v1/api/admin/schedules/{task_id}/runs — run history."""
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.schedules")
    if err:
        return err
    task_id = request.path_params["task_id"]

    # Verify task exists
    if storage.get_scheduled_task(task_id) is None:
        return JSONResponse({"error": "Schedule not found"}, status_code=404)

    try:
        limit = max(1, min(int(request.query_params.get("limit", "50")), 200))
    except (ValueError, TypeError):
        limit = 50
    runs = storage.list_task_runs(task_id, limit=limit)
    return JSONResponse({"runs": runs})


# ---------------------------------------------------------------------------
# Admin API endpoints — watches (aggregated from nodes)
# ---------------------------------------------------------------------------


async def admin_list_watches(request: Request) -> JSONResponse:
    """GET /v1/api/admin/watches — aggregate watches from all nodes."""
    from turnstone.core.auth import require_permission

    err = require_permission(request, "admin.watches")
    if err:
        return err
    collector: ClusterCollector = request.app.state.collector
    nodes = collector.get_all_nodes()
    client: httpx.AsyncClient = request.app.state.proxy_client
    headers = _proxy_auth_headers(request)
    sem = asyncio.Semaphore(_get_fan_out_limit(request))

    async def _fetch_node(node: dict[str, Any]) -> list[dict[str, Any]]:
        server_url = (node.get("server_url") or "").rstrip("/")
        if not server_url:
            return []
        async with sem:
            try:
                resp = await client.get(f"{server_url}/v1/api/watches", headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    watches: list[dict[str, Any]] = data.get("watches", [])
                    # Tag each watch with node_id in case the server omits it
                    for w in watches:
                        if not w.get("node_id"):
                            w["node_id"] = node["node_id"]
                    return watches
            except Exception:
                log.debug(
                    "Failed to fetch watches from node %s",
                    node.get("node_id"),
                    exc_info=True,
                )
        return []

    tasks = [_fetch_node(n) for n in nodes]
    results = await asyncio.gather(*tasks)
    all_watches: list[dict[str, Any]] = []
    for batch in results:
        all_watches.extend(batch)
    # Sort: active first, then by created descending (stable sort trick)
    all_watches.sort(key=lambda w: w.get("created", ""), reverse=True)
    all_watches.sort(key=lambda w: not w.get("active", False))
    return JSONResponse({"watches": all_watches})


_VALID_WATCH_ID = re.compile(r"^[a-fA-F0-9]+$")

# Max concurrent outbound requests when fanning out to cluster nodes.
# Must stay below the httpx pool limit (set in _lifespan) to leave
# headroom for non-fan-out proxy traffic (UI proxying, SSE streams).
_NODE_FAN_OUT_LIMIT = 200  # fallback; prefer cluster.node_fan_out_limit from storage


def _get_fan_out_limit(request: Request) -> int:
    """Return the fan-out limit cached at startup on app.state."""
    return int(getattr(request.app.state, "fan_out_limit", _NODE_FAN_OUT_LIMIT))


async def admin_cancel_watch(request: Request) -> Response:
    """POST /v1/api/admin/watches/{watch_id}/cancel — proxy cancel to the owning node."""
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import read_json_or_400

    err = require_permission(request, "admin.watches")
    if err:
        return err
    watch_id = request.path_params["watch_id"]
    if not watch_id or not _VALID_WATCH_ID.match(watch_id) or len(watch_id) > 128:
        return JSONResponse({"error": "Invalid watch_id"}, status_code=400)

    body = await read_json_or_400(request)
    if isinstance(body, JSONResponse):
        return body

    node_id = str(body.get("node_id", "") or request.query_params.get("node_id", "")).strip()
    if not node_id:
        return JSONResponse({"error": "node_id is required"}, status_code=400)

    server_url = _get_server_url(request, node_id)
    if not server_url:
        return JSONResponse({"error": "Node not found"}, status_code=404)

    client: httpx.AsyncClient = request.app.state.proxy_client
    headers = {"Content-Type": "application/json"}
    headers.update(_proxy_auth_headers(request))
    try:
        resp = await client.post(
            f"{server_url}/v1/api/watches/{watch_id}/cancel",
            content=b"{}",
            headers=headers,
        )
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            media_type=resp.headers.get("content-type", "application/json"),
        )
    except httpx.HTTPError:
        return JSONResponse({"error": "Node unreachable"}, status_code=502)


# ---------------------------------------------------------------------------
# Admin API endpoints — governance (roles, orgs, policies, templates, usage, audit)
# ---------------------------------------------------------------------------


def _audit_context(request: Request) -> tuple[str, str]:
    """Extract (user_id, ip_address) from request for audit logging.

    Honors ``X-Forwarded-For`` only when the request appears to come
    through a trusted proxy (``X-Forwarded-Proto`` is set), matching the
    existing ``is_secure_request()`` trust model.  Falls back to
    ``request.client.host`` otherwise.
    """
    from turnstone.core.auth import is_secure_request

    auth_result = getattr(request.state, "auth_result", None)
    user_id = auth_result.user_id if auth_result else ""
    ip = ""
    # Only trust X-Forwarded-For when behind a proxy that sets X-Forwarded-Proto
    if is_secure_request(dict(request.headers), request.url.scheme):
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            ip = forwarded.split(",")[0].strip()
    if not ip:
        ip = request.client.host if request.client else ""
    return user_id, ip


_VALID_PERMISSIONS = frozenset(
    {
        "read",
        "write",
        "approve",
        "admin.users",
        "admin.roles",
        "admin.orgs",
        "admin.policies",
        "admin.prompt_policies",
        "admin.skills",
        "admin.audit",
        "admin.usage",
        "admin.schedules",
        "admin.watches",
        "admin.judge",
        "admin.memories",
        "admin.nodes",
        "admin.settings",
        "admin.mcp",
        "admin.models",
        # Coordinator workstream kind — granted to builtin-admin via
        # migration 040 so admins can create / manage coordinator
        # sessions out of the box.  Also grantable to non-admin users
        # via a custom role when per-user opt-in is desired.
        "admin.coordinator",
        # Cluster-wide live workstream inspect — troubleshooting
        # surface for GET /v1/api/cluster/ws/{ws_id}/detail.  Granted
        # to builtin-admin via migration 040.
        "admin.cluster.inspect",
        "tools.approve",
        "workstreams.create",
        "workstreams.close",
        "conversation.modify",
    }
)


async def admin_list_roles(request: Request) -> JSONResponse:
    """GET /v1/api/admin/roles — list all roles."""
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.roles")
    if err:
        return err
    return JSONResponse({"roles": storage.list_roles()})


async def admin_create_role(request: Request) -> JSONResponse:
    """POST /v1/api/admin/roles — create a new role."""
    import uuid

    from turnstone.core.audit import record_audit
    from turnstone.core.auth import is_valid_username, require_permission
    from turnstone.core.web_helpers import read_json_or_400, require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.roles")
    if err:
        return err

    body = await read_json_or_400(request)
    if isinstance(body, JSONResponse):
        return body

    name = str(body.get("name", "")).strip()[:128]
    display_name = str(body.get("display_name", "")).strip()[:256]
    permissions = str(body.get("permissions", "")).strip()

    if not is_valid_username(name):
        return JSONResponse(
            {"error": "Invalid name (1-64 chars: letters, digits, . _ -)"},
            status_code=400,
        )
    if not display_name:
        display_name = name

    # Validate permissions against the allowed set
    if permissions:
        perm_list = [p.strip() for p in permissions.split(",") if p.strip()]
        invalid = [p for p in perm_list if p not in _VALID_PERMISSIONS]
        if invalid:
            return JSONResponse(
                {"error": f"Invalid permissions: {', '.join(invalid)}"},
                status_code=400,
            )

    # Check for duplicate name
    if storage.get_role_by_name(name) is not None:
        return JSONResponse({"error": f"Role '{name}' already exists"}, status_code=409)

    role_id = uuid.uuid4().hex
    storage.create_role(
        role_id=role_id,
        name=name,
        display_name=display_name,
        permissions=permissions,
        builtin=False,
        org_id="",
    )

    audit_uid, ip = _audit_context(request)
    record_audit(storage, audit_uid, "role.create", "role", role_id, {"name": name}, ip)

    role = storage.get_role(role_id)
    if role is None:
        return JSONResponse({"error": "Role creation failed"}, status_code=500)
    return JSONResponse(role)


async def admin_update_role(request: Request) -> JSONResponse:
    """PUT /v1/api/admin/roles/{role_id} — update a custom role."""
    from turnstone.core.audit import record_audit
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import read_json_or_400, require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.roles")
    if err:
        return err

    role_id = request.path_params["role_id"]
    existing = storage.get_role(role_id)
    if existing is None:
        return JSONResponse({"error": "Role not found"}, status_code=404)
    if existing.get("builtin"):
        return JSONResponse({"error": "Cannot modify builtin role"}, status_code=400)

    body = await read_json_or_400(request)
    if isinstance(body, JSONResponse):
        return body

    updates: dict[str, Any] = {}
    if "display_name" in body:
        updates["display_name"] = str(body["display_name"]).strip()[:256]
    if "permissions" in body:
        raw_perms = str(body["permissions"]).strip()
        if raw_perms:
            perm_list = [p.strip() for p in raw_perms.split(",") if p.strip()]
            invalid = [p for p in perm_list if p not in _VALID_PERMISSIONS]
            if invalid:
                return JSONResponse(
                    {"error": f"Invalid permissions: {', '.join(invalid)}"},
                    status_code=400,
                )
        updates["permissions"] = raw_perms

    storage.update_role(role_id, **updates)

    audit_uid, ip = _audit_context(request)
    record_audit(storage, audit_uid, "role.update", "role", role_id, updates, ip)

    role = storage.get_role(role_id)
    return JSONResponse(role)


async def admin_delete_role(request: Request) -> JSONResponse:
    """DELETE /v1/api/admin/roles/{role_id} — delete a custom role."""
    from turnstone.core.audit import record_audit
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.roles")
    if err:
        return err

    role_id = request.path_params["role_id"]
    existing = storage.get_role(role_id)
    if existing is None:
        return JSONResponse({"error": "Role not found"}, status_code=404)
    if existing.get("builtin"):
        return JSONResponse({"error": "Cannot delete builtin role"}, status_code=400)

    storage.delete_role(role_id)

    audit_uid, ip = _audit_context(request)
    record_audit(
        storage,
        audit_uid,
        "role.delete",
        "role",
        role_id,
        {"name": existing.get("name", "")},
        ip,
    )

    return JSONResponse({"status": "ok"})


async def admin_list_user_roles(request: Request) -> JSONResponse:
    """GET /v1/api/admin/users/{user_id}/roles — list roles assigned to a user."""
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.users")
    if err:
        return err

    user_id = request.path_params["user_id"]
    return JSONResponse({"roles": storage.list_user_roles(user_id)})


async def admin_assign_role(request: Request) -> JSONResponse:
    """POST /v1/api/admin/users/{user_id}/roles — assign a role to a user."""
    from turnstone.core.audit import record_audit
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import read_json_or_400, require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.users")
    if err:
        return err

    user_id = request.path_params["user_id"]

    body = await read_json_or_400(request)
    if isinstance(body, JSONResponse):
        return body

    role_id = str(body.get("role_id", "")).strip()
    if not role_id:
        return JSONResponse({"error": "role_id is required"}, status_code=400)

    audit_uid, ip = _audit_context(request)

    # Validate that user exists
    if storage.get_user(user_id) is None:
        return JSONResponse({"error": "User not found"}, status_code=404)

    # Validate that role exists
    target_role = storage.get_role(role_id)
    if target_role is None:
        return JSONResponse({"error": "Role not found"}, status_code=404)

    # Prevent self-assignment
    auth_result = getattr(request.state, "auth_result", None)
    if auth_result and auth_result.user_id == user_id:
        return JSONResponse({"error": "Cannot modify own role assignments"}, status_code=403)

    # Ensure caller holds all permissions present in the target role
    target_perms = set(
        p.strip() for p in target_role.get("permissions", "").split(",") if p.strip()
    )
    if (
        auth_result
        and auth_result.permissions
        and not target_perms.issubset(auth_result.permissions)
    ):
        return JSONResponse(
            {"error": "Cannot assign role with permissions you do not hold"},
            status_code=403,
        )

    storage.assign_role(user_id, role_id, assigned_by=audit_uid)
    record_audit(
        storage,
        audit_uid,
        "role.assign",
        "user",
        user_id,
        {"role_id": role_id},
        ip,
    )

    return JSONResponse({"status": "ok"})


async def admin_unassign_role(request: Request) -> JSONResponse:
    """DELETE /v1/api/admin/users/{user_id}/roles/{role_id} — unassign a role."""
    from turnstone.core.audit import record_audit
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.users")
    if err:
        return err

    user_id = request.path_params["user_id"]
    role_id = request.path_params["role_id"]

    audit_uid, ip = _audit_context(request)

    # Prevent self-modification
    if audit_uid and audit_uid == user_id:
        return JSONResponse({"error": "Cannot modify own role assignments"}, status_code=403)

    if storage.unassign_role(user_id, role_id):
        record_audit(
            storage,
            audit_uid,
            "role.unassign",
            "user",
            user_id,
            {"role_id": role_id},
            ip,
        )
        return JSONResponse({"status": "ok"})
    return JSONResponse({"error": "Role assignment not found"}, status_code=404)


async def admin_list_orgs(request: Request) -> JSONResponse:
    """GET /v1/api/admin/orgs — list all organizations."""
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.orgs")
    if err:
        return err
    return JSONResponse({"orgs": storage.list_orgs()})


async def admin_get_org(request: Request) -> JSONResponse:
    """GET /v1/api/admin/orgs/{org_id} — get a single organization."""
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.orgs")
    if err:
        return err

    org_id = request.path_params["org_id"]
    org = storage.get_org(org_id)
    if org is None:
        return JSONResponse({"error": "Organization not found"}, status_code=404)
    return JSONResponse(org)


async def admin_update_org(request: Request) -> JSONResponse:
    """PUT /v1/api/admin/orgs/{org_id} — update an organization."""
    from turnstone.core.audit import record_audit
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import read_json_or_400, require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.orgs")
    if err:
        return err

    org_id = request.path_params["org_id"]
    existing = storage.get_org(org_id)
    if existing is None:
        return JSONResponse({"error": "Organization not found"}, status_code=404)

    body = await read_json_or_400(request)
    if isinstance(body, JSONResponse):
        return body

    updates: dict[str, Any] = {}
    if "display_name" in body:
        updates["display_name"] = str(body["display_name"]).strip()[:256]
    if "settings" in body:
        settings_str = str(body["settings"]).strip()
        try:
            json.loads(settings_str)
        except (json.JSONDecodeError, TypeError):
            return JSONResponse({"error": "settings must be valid JSON"}, status_code=400)
        updates["settings"] = settings_str

    storage.update_org(org_id, **updates)

    audit_uid, ip = _audit_context(request)
    record_audit(storage, audit_uid, "org.update", "org", org_id, updates, ip)

    org = storage.get_org(org_id)
    return JSONResponse(org)


async def admin_list_policies(request: Request) -> JSONResponse:
    """GET /v1/api/admin/policies — list all tool policies."""
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.policies")
    if err:
        return err
    return JSONResponse({"policies": storage.list_tool_policies()})


async def admin_create_policy(request: Request) -> JSONResponse:
    """POST /v1/api/admin/policies — create a tool policy."""
    import uuid

    from turnstone.core.audit import record_audit
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import read_json_or_400, require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.policies")
    if err:
        return err

    body = await read_json_or_400(request)
    if isinstance(body, JSONResponse):
        return body

    name = str(body.get("name", "")).strip()[:256]
    tool_pattern = str(body.get("tool_pattern", "")).strip()[:256]
    action = str(body.get("action", "")).strip().lower()
    priority = int(body.get("priority", 0)) if isinstance(body.get("priority"), (int, float)) else 0
    org_id = str(body.get("org_id", "")).strip()[:64]
    enabled = bool(body.get("enabled", True))

    if not name:
        return JSONResponse({"error": "name is required"}, status_code=400)
    if not tool_pattern:
        return JSONResponse({"error": "tool_pattern is required"}, status_code=400)
    if action not in ("allow", "deny", "ask"):
        return JSONResponse(
            {"error": "action must be one of: allow, deny, ask"},
            status_code=400,
        )

    audit_uid, ip = _audit_context(request)

    policy_id = uuid.uuid4().hex
    storage.create_tool_policy(
        policy_id=policy_id,
        name=name,
        tool_pattern=tool_pattern,
        action=action,
        priority=priority,
        org_id=org_id,
        enabled=enabled,
        created_by=audit_uid,
    )

    record_audit(
        storage,
        audit_uid,
        "policy.create",
        "policy",
        policy_id,
        {"name": name},
        ip,
    )

    policy = storage.get_tool_policy(policy_id)
    return JSONResponse(policy)


async def admin_update_policy(request: Request) -> JSONResponse:
    """PUT /v1/api/admin/policies/{policy_id} — update a tool policy."""
    from turnstone.core.audit import record_audit
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import read_json_or_400, require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.policies")
    if err:
        return err

    policy_id = request.path_params["policy_id"]
    existing = storage.get_tool_policy(policy_id)
    if existing is None:
        return JSONResponse({"error": "Policy not found"}, status_code=404)

    body = await read_json_or_400(request)
    if isinstance(body, JSONResponse):
        return body

    updates: dict[str, Any] = {}
    if "name" in body:
        updates["name"] = str(body["name"]).strip()[:256]
    if "tool_pattern" in body:
        updates["tool_pattern"] = str(body["tool_pattern"]).strip()[:256]
    if "action" in body:
        act = str(body["action"]).strip().lower()
        if act not in ("allow", "deny", "ask"):
            return JSONResponse(
                {"error": "action must be one of: allow, deny, ask"},
                status_code=400,
            )
        updates["action"] = act
    if "priority" in body:
        updates["priority"] = (
            int(body["priority"]) if isinstance(body["priority"], (int, float)) else 0
        )
    if "enabled" in body:
        updates["enabled"] = bool(body["enabled"])

    storage.update_tool_policy(policy_id, **updates)

    audit_uid, ip = _audit_context(request)
    record_audit(storage, audit_uid, "policy.update", "policy", policy_id, updates, ip)

    policy = storage.get_tool_policy(policy_id)
    return JSONResponse(policy)


async def admin_delete_policy(request: Request) -> JSONResponse:
    """DELETE /v1/api/admin/policies/{policy_id} — delete a tool policy."""
    from turnstone.core.audit import record_audit
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.policies")
    if err:
        return err

    policy_id = request.path_params["policy_id"]
    existing = storage.get_tool_policy(policy_id)
    if existing is None:
        return JSONResponse({"error": "Policy not found"}, status_code=404)

    storage.delete_tool_policy(policy_id)

    audit_uid, ip = _audit_context(request)
    record_audit(
        storage,
        audit_uid,
        "policy.delete",
        "policy",
        policy_id,
        {"name": existing.get("name", "")},
        ip,
    )

    return JSONResponse({"status": "ok"})


# ---------------------------------------------------------------------------
# Admin: Skills (thin layer over prompt templates with extended fields)
# ---------------------------------------------------------------------------

_VALID_ACTIVATIONS = {"named", "default", "search"}

# Fields that may be updated on installed (readonly) skills.
# These are local runtime configuration — not part of the SKILL.md spec —
# so they don't compromise the fidelity of an externally-sourced skill.
_SKILL_RUNTIME_CONFIG_FIELDS = frozenset(
    {
        "model",
        "temperature",
        "reasoning_effort",
        "max_tokens",
        "token_budget",
        "agent_max_turns",
        "auto_approve",
        "allowed_tools",
        "enabled",
        "notify_on_complete",
        "priority",
    }
)


def _parse_skill_session_config(body: dict[str, Any]) -> tuple[dict[str, Any], JSONResponse | None]:
    """Parse and validate session config fields from a skill request body.

    Returns (fields_dict, error_response). error_response is None on success.
    Only includes fields that are present in the body (for partial updates).
    """
    import json as _json

    fields: dict[str, Any] = {}

    if "model" in body:
        fields["model"] = str(body["model"] or "").strip()

    if "temperature" in body:
        temp = body["temperature"]
        if temp is not None and temp != "":
            try:
                temp = float(temp)
                if not (0.0 <= temp <= 2.0):
                    return {}, JSONResponse(
                        {"error": "temperature must be between 0 and 2"}, status_code=400
                    )
                fields["temperature"] = temp
            except (ValueError, TypeError):
                fields["temperature"] = None
        else:
            fields["temperature"] = None

    if "token_budget" in body:
        try:
            tb = int(body.get("token_budget", 0) or 0)
        except (ValueError, TypeError):
            return {}, JSONResponse({"error": "token_budget must be an integer"}, status_code=400)
        if tb < 0:
            return {}, JSONResponse({"error": "token_budget must be non-negative"}, status_code=400)
        fields["token_budget"] = tb

    if "max_tokens" in body:
        mt = body["max_tokens"]
        if mt is not None and mt != "":
            try:
                mt = int(mt)
            except (ValueError, TypeError):
                return {}, JSONResponse({"error": "max_tokens must be an integer"}, status_code=400)
            if mt < 1:
                return {}, JSONResponse({"error": "max_tokens must be positive"}, status_code=400)
            fields["max_tokens"] = mt
        else:
            fields["max_tokens"] = None

    if "agent_max_turns" in body:
        amt = body["agent_max_turns"]
        if amt is not None and amt != "":
            try:
                amt = int(amt)
            except (ValueError, TypeError):
                return {}, JSONResponse(
                    {"error": "agent_max_turns must be an integer"}, status_code=400
                )
            if amt < 1:
                return {}, JSONResponse(
                    {"error": "agent_max_turns must be positive"}, status_code=400
                )
            fields["agent_max_turns"] = amt
        else:
            fields["agent_max_turns"] = None

    if "reasoning_effort" in body:
        fields["reasoning_effort"] = str(body["reasoning_effort"] or "").strip()

    if "auto_approve" in body:
        fields["auto_approve"] = bool(body.get("auto_approve", False))

    if "enabled" in body:
        fields["enabled"] = bool(body.get("enabled", True))

    if "activation" in body:
        activation = str(body["activation"] or "named").strip()
        if activation not in _VALID_ACTIVATIONS:
            return {}, JSONResponse(
                {"error": f"activation must be one of: {', '.join(sorted(_VALID_ACTIVATIONS))}"},
                status_code=400,
            )
        fields["activation"] = activation

    if "notify_on_complete" in body:
        nc = str(body.get("notify_on_complete", "{}")).strip()
        if nc and nc != "{}":
            try:
                _json.loads(nc)
            except (_json.JSONDecodeError, TypeError):
                return {}, JSONResponse(
                    {"error": "notify_on_complete must be valid JSON"}, status_code=400
                )
        fields["notify_on_complete"] = nc

    if "allowed_tools" in body:
        at_raw = body.get("allowed_tools", "[]")
        if isinstance(at_raw, list):
            fields["allowed_tools"] = _json.dumps(at_raw)
        else:
            at_str = str(at_raw).strip()
            if at_str and not at_str.startswith("["):
                at_str = _json.dumps([t.strip() for t in at_str.split(",") if t.strip()])
            try:
                _json.loads(at_str or "[]")
            except (ValueError, TypeError):
                at_str = "[]"
            fields["allowed_tools"] = at_str or "[]"

    return fields, None


def _skill_to_response(r: dict[str, Any], resource_count: int = 0) -> dict[str, Any]:
    """Convert a storage skill dict to a JSON-safe response dict."""
    import contextlib
    import json as _json

    tags: list[str] = []
    with contextlib.suppress(ValueError, TypeError):
        tags = _json.loads(r.get("tags", "[]"))
    return {
        "template_id": r.get("template_id", ""),
        "name": r.get("name", ""),
        "category": r.get("category", ""),
        "description": r.get("description", ""),
        "content": r.get("content", ""),
        "tags": tags,
        "is_default": r.get("is_default", False),
        "activation": r.get("activation", "named"),
        "origin": r.get("origin", "manual"),
        "mcp_server": r.get("mcp_server", ""),
        "readonly": r.get("readonly", False),
        "author": r.get("author", ""),
        "version": r.get("version", "1.0.0"),
        "variables": r.get("variables", "[]"),
        "token_estimate": r.get("token_estimate", 0),
        "source_url": r.get("source_url", ""),
        "org_id": r.get("org_id", ""),
        "created_by": r.get("created_by", ""),
        # Session config fields
        "model": r.get("model", ""),
        "auto_approve": r.get("auto_approve", False),
        "temperature": r.get("temperature"),
        "reasoning_effort": r.get("reasoning_effort", ""),
        "max_tokens": r.get("max_tokens"),
        "token_budget": r.get("token_budget", 0),
        "agent_max_turns": r.get("agent_max_turns"),
        "notify_on_complete": r.get("notify_on_complete", "{}"),
        "enabled": r.get("enabled", True),
        "priority": r.get("priority", 0),
        "allowed_tools": r.get("allowed_tools", "[]"),
        "license": r.get("license", ""),
        "compatibility": r.get("compatibility", ""),
        "kind": r.get("kind", "any"),
        "risk_level": r.get("risk_level", ""),
        "scan_report": r.get("scan_report", "{}"),
        "scan_version": r.get("scan_version", ""),
        "resource_count": resource_count,
        "created": r.get("created", ""),
        "updated": r.get("updated", ""),
    }


async def admin_list_skills(request: Request) -> JSONResponse:
    """GET /v1/api/admin/skills — list all skills."""
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.skills")
    if err:
        return err
    params = dict(request.query_params)
    limit = _parse_int(params, "limit", 0, minimum=0, maximum=10000)
    offset = _parse_int(params, "offset", 0, minimum=0, maximum=100000)
    rows = storage.list_prompt_templates(limit=limit, offset=offset)
    total = storage.count_prompt_templates()
    skill_ids = [r["template_id"] for r in rows]
    rc_map = storage.count_skill_resources_bulk(skill_ids) if skill_ids else {}
    skills = [_skill_to_response(r, resource_count=rc_map.get(r["template_id"], 0)) for r in rows]
    return JSONResponse({"skills": skills, "total": total})


async def admin_get_skill(request: Request) -> JSONResponse:
    """GET /v1/api/admin/skills/{skill_id} — get a single skill."""
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.skills")
    if err:
        return err

    skill_id = request.path_params["skill_id"]
    skill = storage.get_prompt_template(skill_id)
    if skill is None:
        return JSONResponse({"error": "Skill not found"}, status_code=404)
    rc_map = storage.count_skill_resources_bulk([skill_id])
    return JSONResponse(_skill_to_response(skill, resource_count=rc_map.get(skill_id, 0)))


async def admin_create_skill(request: Request) -> JSONResponse:
    """POST /v1/api/admin/skills — create a skill."""
    import json as _json
    import uuid

    from turnstone.core.audit import record_audit
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import read_json_or_400, require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.skills")
    if err:
        return err

    body = await read_json_or_400(request)
    if isinstance(body, JSONResponse):
        return body

    # Treat an explicit JSON null the same as a missing key — ``.get(k, "")``
    # only falls back when the key is absent, so ``str(None)`` would otherwise
    # yield the literal string "None" and slip past the non-empty guards.
    name = str(body.get("name") or "").strip()[:256]
    content = str(body.get("content") or "").strip()[:32768]
    category = str(body.get("category") or "general").strip()[:64]
    description = str(body.get("description") or "").strip()[:1024]
    try:
        kind = SkillKind(str(body.get("kind") or "any").strip().lower()).value
    except ValueError:
        return JSONResponse(
            {"error": "kind must be one of: " + ", ".join(sorted(k.value for k in SkillKind))},
            status_code=400,
        )
    variables = str(body.get("variables", "[]")).strip()
    try:
        _json.loads(variables)
    except (_json.JSONDecodeError, TypeError):
        return JSONResponse({"error": "variables must be a valid JSON array"}, status_code=400)
    is_default = bool(body.get("is_default", False))
    org_id = str(body.get("org_id", "")).strip()[:64]
    author = str(body.get("author", "")).strip()[:256]
    version = str(body.get("version", "1.0.0")).strip()[:64]
    license_val = str(body.get("license", "")).strip()[:128]
    compatibility = str(body.get("compatibility", "")).strip()[:500]

    raw_tags = body.get("tags", [])
    if isinstance(raw_tags, list):
        tags_str = _json.dumps(raw_tags)
    else:
        tags_str = str(raw_tags).strip()
        try:
            _json.loads(tags_str)
        except (ValueError, TypeError):
            tags_str = "[]"

    token_estimate = len(content) // 4 if content else 0

    # Session config fields via shared helper
    session_fields, session_err = _parse_skill_session_config(body)
    if session_err:
        return session_err

    # Resolve activation / is_default sync
    activation = session_fields.pop("activation", "")
    if not activation:
        activation = "default" if is_default else "named"
    if activation == "default":
        is_default = True

    try:
        priority = max(-1000, min(1000, int(body.get("priority", 0) or 0)))
    except (ValueError, TypeError):
        priority = 0

    if not name:
        return JSONResponse({"error": "name is required"}, status_code=400)
    if not content:
        return JSONResponse({"error": "content is required"}, status_code=400)
    if not description:
        return JSONResponse({"error": "description is required"}, status_code=400)
    if storage.get_prompt_template_by_name(name):
        return JSONResponse({"error": "Skill name already exists"}, status_code=409)

    audit_uid, ip = _audit_context(request)

    skill_id = uuid.uuid4().hex
    storage.create_prompt_template(
        template_id=skill_id,
        name=name,
        category=category,
        content=content,
        variables=variables,
        is_default=is_default,
        org_id=org_id,
        created_by=audit_uid,
        description=description,
        tags=tags_str,
        version=version,
        author=author,
        skill_license=license_val,
        compatibility=compatibility,
        activation=activation,
        token_estimate=token_estimate,
        priority=priority,
        kind=kind,
        **session_fields,
    )

    record_audit(
        storage,
        audit_uid,
        "skill.create",
        "skill",
        skill_id,
        {"name": name},
        ip,
    )

    skill = storage.get_prompt_template(skill_id)
    return JSONResponse(_skill_to_response(skill))


async def admin_update_skill(request: Request) -> JSONResponse:
    """PUT /v1/api/admin/skills/{skill_id} — update a skill."""
    import json as _json

    from turnstone.core.audit import record_audit
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import read_json_or_400, require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.skills")
    if err:
        return err

    skill_id = request.path_params["skill_id"]
    existing = storage.get_prompt_template(skill_id)
    if existing is None:
        return JSONResponse({"error": "Skill not found"}, status_code=404)
    is_readonly = bool(existing.get("readonly"))

    body = await read_json_or_400(request)
    if isinstance(body, JSONResponse):
        return body

    # Session config fields via shared helper
    session_fields, session_err = _parse_skill_session_config(body)
    if session_err:
        return session_err

    updates: dict[str, Any] = dict(session_fields)
    if "name" in body:
        updates["name"] = str(body["name"]).strip()[:256]
        existing_by_name = storage.get_prompt_template_by_name(updates["name"])
        if existing_by_name and existing_by_name["template_id"] != skill_id:
            return JSONResponse({"error": "Skill name already exists"}, status_code=409)
    if "content" in body:
        content = str(body["content"]).strip()[:32768]
        updates["content"] = content
        updates["token_estimate"] = len(content) // 4 if content else 0
    if "category" in body:
        updates["category"] = str(body["category"]).strip()[:64]
    if "description" in body:
        # Match create: an operator can rewrite the description but
        # cannot blank it out — and ``null`` is treated the same as
        # blank so it can't coerce to the literal string "None".
        raw_description = body["description"]
        new_description = str(raw_description or "").strip()[:1024]
        if not new_description:
            return JSONResponse({"error": "description must not be empty"}, status_code=400)
        updates["description"] = new_description
    if "kind" in body:
        try:
            updates["kind"] = SkillKind(str(body["kind"] or "").strip().lower()).value
        except ValueError:
            return JSONResponse(
                {"error": "kind must be one of: " + ", ".join(sorted(k.value for k in SkillKind))},
                status_code=400,
            )
    if "variables" in body:
        var_str = str(body["variables"]).strip()
        try:
            _json.loads(var_str)
        except (_json.JSONDecodeError, TypeError):
            return JSONResponse({"error": "variables must be a valid JSON array"}, status_code=400)
        updates["variables"] = var_str
    if "is_default" in body:
        updates["is_default"] = bool(body["is_default"])
    if "activation" in updates and updates["activation"] == "default":
        updates["is_default"] = True
    if "author" in body:
        updates["author"] = str(body["author"]).strip()[:256]
    if "version" in body:
        updates["version"] = str(body["version"]).strip()[:64]
    if "license" in body:
        updates["license"] = str(body["license"]).strip()[:128]
    if "compatibility" in body:
        updates["compatibility"] = str(body["compatibility"]).strip()[:500]
    if "tags" in body:
        raw_tags = body["tags"]
        if isinstance(raw_tags, list):
            updates["tags"] = _json.dumps(raw_tags)
        else:
            tag_str = str(raw_tags).strip()
            try:
                _json.loads(tag_str)
            except (ValueError, TypeError):
                tag_str = "[]"
            updates["tags"] = tag_str
    if "priority" in body:
        try:
            updates["priority"] = max(-1000, min(1000, int(body["priority"] or 0)))
        except (ValueError, TypeError):
            updates["priority"] = 0

    # Installed (readonly) skills: restrict updates to runtime config only.
    # Spec/content fields are locked to preserve external-source fidelity.
    if is_readonly:
        updates = {k: v for k, v in updates.items() if k in _SKILL_RUNTIME_CONFIG_FIELDS}
        if not updates:
            return JSONResponse({"error": "No runtime config fields to update"}, status_code=400)

    # Snapshot current state for version history before applying update
    existing_versions = storage.list_skill_versions(skill_id)
    version_int = len(existing_versions) + 1
    audit_uid_pre, _ = _audit_context(request)
    storage.create_skill_version(
        skill_id=skill_id,
        version=version_int,
        snapshot=_json.dumps(existing, default=str),
        changed_by=audit_uid_pre,
    )

    storage.update_prompt_template(skill_id, **updates)

    audit_uid, ip = _audit_context(request)
    record_audit(
        storage,
        audit_uid,
        "skill.update.config" if is_readonly else "skill.update",
        "skill",
        skill_id,
        updates,
        ip,
    )

    updated_skill = storage.get_prompt_template(skill_id)
    rc_map = storage.count_skill_resources_bulk([skill_id])
    return JSONResponse(_skill_to_response(updated_skill, resource_count=rc_map.get(skill_id, 0)))


async def admin_delete_skill(request: Request) -> JSONResponse:
    """DELETE /v1/api/admin/skills/{skill_id} — delete a skill."""
    from turnstone.core.audit import record_audit
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.skills")
    if err:
        return err

    skill_id = request.path_params["skill_id"]
    existing = storage.get_prompt_template(skill_id)
    if existing is None:
        return JSONResponse({"error": "Skill not found"}, status_code=404)

    storage.delete_skill_resources(skill_id)
    storage.delete_skill_versions(skill_id)
    storage.delete_prompt_template(skill_id)

    audit_uid, ip = _audit_context(request)
    record_audit(
        storage,
        audit_uid,
        "skill.delete",
        "skill",
        skill_id,
        {"name": existing.get("name", "")},
        ip,
    )

    return JSONResponse({"status": "ok"})


async def admin_list_skill_versions(request: Request) -> JSONResponse:
    """GET /v1/api/admin/skills/{skill_id}/versions — version history."""
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.skills")
    if err:
        return err

    skill_id = request.path_params["skill_id"]
    versions = storage.list_skill_versions(skill_id)
    return JSONResponse({"versions": versions})


async def list_skills_summary(request: Request) -> JSONResponse:
    """GET /v1/api/skills — list available skills (summary)."""
    import contextlib
    import json as _json

    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
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


async def admin_usage(request: Request) -> JSONResponse:
    """GET /v1/api/admin/usage — query usage data."""
    from datetime import UTC, datetime, timedelta

    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.usage")
    if err:
        return err

    params = dict(request.query_params)
    since = params.get("since", "")
    until = params.get("until", "")
    user_id = params.get("user_id", "")
    model = params.get("model", "")
    group_by = params.get("group_by", "day")

    if group_by not in ("day", "hour", "model", "user"):
        return JSONResponse(
            {"error": "group_by must be one of: day, hour, model, user"},
            status_code=400,
        )

    if not since:
        since = (datetime.now(UTC) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S")

    summary = storage.query_usage(since=since, until=until, user_id=user_id, model=model)
    breakdown = storage.query_usage(
        since=since,
        until=until,
        user_id=user_id,
        model=model,
        group_by=group_by,
    )

    # Resolve user_id hex → username for display when grouped by user
    if group_by == "user" and breakdown:
        uid_to_name: dict[str, str] = {}
        for u in storage.list_users():
            uid_to_name[u["user_id"]] = u.get("username") or u["user_id"]
        for row in breakdown:
            raw_key = row.get("key", "")
            if raw_key and raw_key in uid_to_name:
                row["key"] = uid_to_name[raw_key]

    return JSONResponse({"summary": summary, "breakdown": breakdown})


async def admin_audit(request: Request) -> JSONResponse:
    """GET /v1/api/admin/audit — query audit events."""
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.audit")
    if err:
        return err

    params = dict(request.query_params)
    action = params.get("action", "")
    user_id = params.get("user_id", "")
    since = params.get("since", "")
    until = params.get("until", "")
    try:
        limit = max(1, min(int(params.get("limit", "50")), 200))
    except (ValueError, TypeError):
        limit = 50
    try:
        offset = max(int(params.get("offset", "0")), 0)
    except (ValueError, TypeError):
        offset = 0

    events = storage.list_audit_events(
        action=action,
        user_id=user_id,
        since=since,
        until=until,
        limit=limit,
        offset=offset,
    )
    total = storage.count_audit_events(
        action=action,
        user_id=user_id,
        since=since,
        until=until,
    )

    # Resolve user_id hex → username for display
    if events:
        uid_to_name: dict[str, str] = {}
        for u in storage.list_users():
            uid_to_name[u["user_id"]] = u.get("username") or u["user_id"]
        for ev in events:
            raw_uid = ev.get("user_id", "")
            if raw_uid and raw_uid in uid_to_name:
                ev["username"] = uid_to_name[raw_uid]

    return JSONResponse({"events": events, "total": total})


async def admin_list_verdicts(request: Request) -> JSONResponse:
    """GET /v1/api/admin/verdicts — list intent verdicts."""
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.judge")
    if err:
        return err

    params = dict(request.query_params)
    ws_id = params.get("ws_id", "")
    since = params.get("since", "")
    until = params.get("until", "")
    risk_level = params.get("risk_level", "")
    try:
        limit = max(1, min(int(params.get("limit", "100")), 500))
    except (ValueError, TypeError):
        limit = 100
    try:
        offset = max(int(params.get("offset", "0")), 0)
    except (ValueError, TypeError):
        offset = 0

    verdicts = storage.list_intent_verdicts(
        ws_id=ws_id,
        since=since,
        until=until,
        risk_level=risk_level,
        limit=limit,
        offset=offset,
    )

    total = storage.count_intent_verdicts(
        ws_id=ws_id,
        since=since,
        until=until,
        risk_level=risk_level,
    )
    return JSONResponse({"verdicts": verdicts, "total": total})


async def admin_list_output_assessments(request: Request) -> JSONResponse:
    """GET /v1/api/admin/output-assessments — list output guard assessments."""
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.judge")
    if err:
        return err

    params = dict(request.query_params)
    ws_id = params.get("ws_id", "")
    risk_level = params.get("risk_level", "")
    since = params.get("since", "")
    until = params.get("until", "")
    try:
        limit = max(1, min(int(params.get("limit", "100")), 500))
    except (ValueError, TypeError):
        limit = 100
    try:
        offset = max(int(params.get("offset", "0")), 0)
    except (ValueError, TypeError):
        offset = 0

    assessments = storage.list_output_assessments(
        ws_id=ws_id,
        risk_level=risk_level,
        since=since,
        until=until,
        limit=limit,
        offset=offset,
    )
    total = storage.count_output_assessments(
        ws_id=ws_id, risk_level=risk_level, since=since, until=until
    )
    return JSONResponse({"assessments": assessments, "total": total})


async def admin_rescan_skill(request: Request) -> JSONResponse:
    """POST /v1/api/admin/skills/{skill_id}/rescan — re-scan skill security."""
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.skills")
    if err:
        return err

    skill_id = request.path_params["skill_id"]
    skill = storage.get_prompt_template(skill_id)
    if not skill:
        return JSONResponse({"error": "Skill not found"}, status_code=404)

    from turnstone.core.storage._utils import scan_skill_content

    content = skill.get("content", "")
    allowed_tools = skill.get("allowed_tools", "[]")
    risk_level, scan_report, scan_version = scan_skill_content(content, allowed_tools)
    storage.update_prompt_template(
        skill_id,
        risk_level=risk_level,
        scan_report=scan_report,
        scan_version=scan_version,
    )
    return JSONResponse(
        {
            "risk_level": risk_level,
            "scan_report": scan_report,
            "scan_version": scan_version,
        }
    )


# ---------------------------------------------------------------------------
# Admin: Skill Resources
# ---------------------------------------------------------------------------

_ALLOWED_RESOURCE_DIRS = ("scripts/", "references/", "assets/")
_MAX_RESOURCE_SIZE = 100 * 1024  # 100KB
_MAX_RESOURCES_PER_SKILL = 10


async def admin_list_skill_resources(request: Request) -> JSONResponse:
    """GET /v1/api/admin/skills/{skill_id}/resources — list resources."""
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.skills")
    if err:
        return err

    skill_id = request.path_params["skill_id"]
    skill = storage.get_prompt_template(skill_id)
    if skill is None:
        return JSONResponse({"error": "Skill not found"}, status_code=404)

    rows = storage.list_skill_resources(skill_id)
    resources = [
        {
            "resource_id": r.get("resource_id", ""),
            "skill_id": r.get("skill_id", ""),
            "path": r.get("path", ""),
            "content_type": r.get("content_type", "text/plain"),
            "size": len(r.get("content", "")),
            "created": r.get("created", ""),
        }
        for r in rows
    ]
    return JSONResponse({"resources": resources})


async def admin_get_skill_resource(request: Request) -> JSONResponse:
    """GET /v1/api/admin/skills/{skill_id}/resources/{path:path} — get one resource."""
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.skills")
    if err:
        return err

    skill_id = request.path_params["skill_id"]
    path = request.path_params["path"]
    resource = storage.get_skill_resource(skill_id, path)
    if resource is None:
        return JSONResponse({"error": "Resource not found"}, status_code=404)
    return JSONResponse(
        {
            "resource_id": resource.get("resource_id", ""),
            "skill_id": resource.get("skill_id", ""),
            "path": resource.get("path", ""),
            "content": resource.get("content", ""),
            "content_type": resource.get("content_type", "text/plain"),
            "size": len(resource.get("content", "")),
            "created": resource.get("created", ""),
        }
    )


async def admin_create_skill_resource(request: Request) -> JSONResponse:
    """POST /v1/api/admin/skills/{skill_id}/resources — upload resource."""
    import uuid

    from turnstone.core.audit import record_audit
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import read_json_or_400, require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.skills")
    if err:
        return err

    skill_id = request.path_params["skill_id"]
    skill = storage.get_prompt_template(skill_id)
    if skill is None:
        return JSONResponse({"error": "Skill not found"}, status_code=404)
    if skill.get("readonly"):
        return JSONResponse({"error": "Installed skills are read-only"}, status_code=403)

    body = await read_json_or_400(request)
    if isinstance(body, JSONResponse):
        return body

    path = str(body.get("path", "")).strip()
    content = str(body.get("content", ""))
    content_type = str(body.get("content_type", "text/plain")).strip()[:64]

    if not path:
        return JSONResponse({"error": "path is required"}, status_code=400)
    # Normalize and reject path traversal
    import posixpath

    path = posixpath.normpath(path)
    if ".." in path.split("/") or "\x00" in path:
        return JSONResponse({"error": "Invalid path"}, status_code=400)
    if not any(path.startswith(d) for d in _ALLOWED_RESOURCE_DIRS):
        return JSONResponse(
            {"error": "path must start with scripts/, references/, or assets/"},
            status_code=400,
        )
    if len(content) > _MAX_RESOURCE_SIZE:
        return JSONResponse(
            {"error": f"Resource exceeds {_MAX_RESOURCE_SIZE // 1024}KB limit"},
            status_code=400,
        )

    existing = storage.list_skill_resources(skill_id)
    if len(existing) >= _MAX_RESOURCES_PER_SKILL:
        return JSONResponse(
            {"error": f"Maximum {_MAX_RESOURCES_PER_SKILL} resources per skill"},
            status_code=400,
        )
    if storage.get_skill_resource(skill_id, path) is not None:
        return JSONResponse({"error": "Resource path already exists"}, status_code=409)

    resource_id = uuid.uuid4().hex
    storage.create_skill_resource(
        resource_id=resource_id,
        skill_id=skill_id,
        path=path,
        content=content,
        content_type=content_type,
    )

    audit_uid, ip = _audit_context(request)
    record_audit(storage, audit_uid, "skill_resource.create", "skill", skill_id, {"path": path}, ip)

    created = storage.get_skill_resource(skill_id, path)
    return JSONResponse(
        {
            "resource_id": resource_id,
            "skill_id": skill_id,
            "path": path,
            "content_type": content_type,
            "size": len(content),
            "created": (created or {}).get("created", ""),
        },
        status_code=201,
    )


async def admin_delete_skill_resource(request: Request) -> JSONResponse:
    """DELETE /v1/api/admin/skills/{skill_id}/resources/{path:path} — delete resource."""
    from turnstone.core.audit import record_audit
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.skills")
    if err:
        return err

    skill_id = request.path_params["skill_id"]
    skill = storage.get_prompt_template(skill_id)
    if skill is None:
        return JSONResponse({"error": "Skill not found"}, status_code=404)
    if skill.get("readonly"):
        return JSONResponse({"error": "Installed skills are read-only"}, status_code=403)

    path = request.path_params["path"]
    deleted = storage.delete_skill_resource_by_path(skill_id, path)
    if not deleted:
        return JSONResponse({"error": "Resource not found"}, status_code=404)

    audit_uid, ip = _audit_context(request)
    record_audit(storage, audit_uid, "skill_resource.delete", "skill", skill_id, {"path": path}, ip)

    return JSONResponse({"status": "ok"})


# ---------------------------------------------------------------------------
# Admin: Skill Discovery
# ---------------------------------------------------------------------------


def _get_discovery_url(request: Request) -> str:
    """Get skills discovery URL via ConfigStore, config.toml, or default."""
    from turnstone.core.config import load_config
    from turnstone.core.skill_sources import DEFAULT_DISCOVERY_URL

    # ConfigStore: validated + cached
    config_store = getattr(request.app.state, "config_store", None)
    if config_store:
        val = config_store.get("skills.discovery_url")
        if val:
            return str(val)

    # Fall back to config.toml [skills] section
    skills_cfg = load_config("skills")
    url = skills_cfg.get("discovery_url", "")
    if url:
        return str(url)
    return DEFAULT_DISCOVERY_URL


async def admin_skill_discover(request: Request) -> JSONResponse:
    """GET /v1/api/admin/skills/discover — search external skill registries."""
    from turnstone.core.auth import require_permission
    from turnstone.core.skill_sources import SkillSourceError, SkillsShClient
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.skills")
    if err:
        return err

    q = str(request.query_params.get("q", "")).strip()
    if not q:
        return JSONResponse({"error": "Search query is required"}, status_code=400)
    try:
        limit = max(1, min(int(request.query_params.get("limit", "20")), 100))
    except (ValueError, TypeError):
        limit = 20

    discovery_url = _get_discovery_url(request)
    client = SkillsShClient(base_url=discovery_url)
    try:
        listings = await client.search(query=q, limit=limit)
    except SkillSourceError as exc:
        return JSONResponse({"error": f"Discovery error: {exc}"}, status_code=502)

    # Mark which skills are already installed (by source_url match)
    installed_map: dict[str, dict[str, str]] = {}
    for row in storage.list_installed_skill_urls():
        installed_map[row["source_url"]] = {
            "risk_level": row.get("risk_level", ""),
            "template_id": row.get("template_id", ""),
        }

    skills_out = []
    for listing in listings:
        is_installed = listing.source_url in installed_map if listing.source_url else False
        entry: dict[str, Any] = {
            "id": listing.id,
            "name": listing.name,
            "description": listing.description,
            "author": listing.author,
            "source": listing.source,
            "source_url": listing.source_url,
            "install_count": listing.install_count,
            "tags": listing.tags,
            "installed": is_installed,
        }
        if is_installed and listing.source_url:
            info = installed_map[listing.source_url]
            entry["risk_level"] = info["risk_level"]
            entry["template_id"] = info["template_id"]
        skills_out.append(entry)

    return JSONResponse({"skills": skills_out})


async def admin_skill_install(request: Request) -> JSONResponse:
    """POST /v1/api/admin/skills/install — install a skill from external source."""
    import uuid

    from turnstone.core.audit import record_audit
    from turnstone.core.auth import require_permission
    from turnstone.core.skill_sources import (
        SkillNotFoundError,
        SkillSourceError,
        SkillsShClient,
        fetch_skill_from_github,
        fetch_skills_from_github_repo,
    )
    from turnstone.core.web_helpers import read_json_or_400, require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.skills")
    if err:
        return err

    body = await read_json_or_400(request)
    if isinstance(body, JSONResponse):
        return body

    source = str(body.get("source", "")).strip()
    if source not in ("skills.sh", "github"):
        return JSONResponse({"error": "source must be 'skills.sh' or 'github'"}, status_code=400)

    try:
        if source == "skills.sh":
            skill_id_param = str(body.get("skill_id", "")).strip()
            if not skill_id_param:
                return JSONResponse({"error": "skill_id is required"}, status_code=400)

            discovery_url = _get_discovery_url(request)
            client = SkillsShClient(base_url=discovery_url)
            github_url = await client.resolve_github_url(skill_id_param)
            packages = [await fetch_skill_from_github(github_url)]
        else:
            url = str(body.get("url", "")).strip()
            if not url:
                return JSONResponse({"error": "url is required"}, status_code=400)
            try:
                packages = [await fetch_skill_from_github(url)]
            except SkillNotFoundError:
                # No root SKILL.md — try scanning for a multi-skill repo
                packages = await fetch_skills_from_github_repo(url)
    except SkillNotFoundError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except SkillSourceError as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    import json as _json

    audit_uid, ip = _audit_context(request)
    installed: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []

    for package in packages:
        pkg_source_url = package.listing.source_url

        # Check for duplicate by source_url
        if pkg_source_url and storage.get_skill_by_source_url(pkg_source_url):
            skipped.append({"name": package.parsed.name, "reason": "already installed"})
            continue

        # Check for duplicate by name
        if storage.get_prompt_template_by_name(package.parsed.name):
            skipped.append({"name": package.parsed.name, "reason": "name exists"})
            continue

        skill_id = uuid.uuid4().hex
        parsed = package.parsed
        tags_str = _json.dumps(parsed.tags)
        allowed_tools_str = _json.dumps(parsed.allowed_tools)
        content = parsed.content[:32768]
        token_estimate = len(content) // 4 if content else 0

        # Installer mirrors migration 043's placeholder — a SKILL.md
        # with no description otherwise fails the new non-empty
        # invariant, blocking installs from upstream catalogs the
        # operator doesn't control.
        skill_description = parsed.description.strip() or f"Skill: {parsed.name}"

        try:
            storage.create_prompt_template(
                template_id=skill_id,
                name=parsed.name,
                category="general",
                content=content,
                variables="[]",
                is_default=False,
                org_id="",
                created_by=audit_uid,
                origin="source",
                readonly=True,
                description=skill_description,
                tags=tags_str,
                source_url=pkg_source_url,
                version=parsed.version,
                author=parsed.author,
                skill_license=parsed.license,
                compatibility=parsed.compatibility,
                activation="named",
                token_estimate=token_estimate,
                allowed_tools=allowed_tools_str,
            )
        except Exception:
            skipped.append({"name": parsed.name, "reason": "conflict"})
            continue

        # Store bundled resources
        for res_path, res_content in package.resources.items():
            storage.create_skill_resource(
                resource_id=uuid.uuid4().hex,
                skill_id=skill_id,
                path=res_path,
                content=res_content,
            )

        record_audit(
            storage,
            audit_uid,
            "skill.install",
            "skill",
            skill_id,
            {"name": parsed.name, "source": source, "source_url": pkg_source_url},
            ip,
        )

        skill = storage.get_prompt_template(skill_id)
        if skill:
            installed.append(_skill_to_response(skill, resource_count=len(package.resources)))

    if not installed and skipped:
        # All skills were duplicates
        return JSONResponse(
            {
                "error": "All skills already installed",
                "installed": [],
                "skipped": skipped,
                "total": len(packages),
            },
            status_code=409,
        )

    # Consistent envelope for both single and batch installs
    return JSONResponse(
        {
            "installed": installed,
            "skipped": skipped,
            "total": len(packages),
        }
    )


# ---------------------------------------------------------------------------
# Admin: Memories
# ---------------------------------------------------------------------------


def _validate_memory_scope_filter(scope: str, scope_id: str) -> JSONResponse | None:
    """Validate scope/scope_id consistency for memory queries."""
    scope = scope.strip()
    scope_id = scope_id.strip()
    if scope == "global" and scope_id:
        return JSONResponse({"error": "scope_id is not allowed with global scope"}, status_code=400)
    if scope_id and not scope:
        return JSONResponse(
            {"error": "scope is required when scope_id is provided"}, status_code=400
        )
    return None


async def admin_list_memories(request: Request) -> JSONResponse:
    """GET /v1/api/admin/memories — list structured memories with filters."""
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.memories")
    if err:
        return err

    mem_type = request.query_params.get("type", "")
    scope = request.query_params.get("scope", "")
    scope_id = request.query_params.get("scope_id", "")
    err = _validate_memory_scope_filter(scope, scope_id)
    if err:
        return err
    try:
        limit = max(1, min(int(request.query_params.get("limit", "100")), 200))
    except (ValueError, TypeError):
        return JSONResponse({"error": "limit must be an integer"}, status_code=400)

    rows = storage.list_structured_memories(
        mem_type=mem_type, scope=scope, scope_id=scope_id, limit=limit
    )
    total = storage.count_structured_memories(mem_type=mem_type, scope=scope, scope_id=scope_id)
    return JSONResponse({"memories": rows, "total": total})


async def admin_search_memories(request: Request) -> JSONResponse:
    """GET /v1/api/admin/memories/search — search memories by query."""
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.memories")
    if err:
        return err

    query = request.query_params.get("q", "").strip()
    if not query:
        return JSONResponse({"error": "q is required"}, status_code=400)
    mem_type = request.query_params.get("type", "")
    scope = request.query_params.get("scope", "")
    scope_id = request.query_params.get("scope_id", "")
    err = _validate_memory_scope_filter(scope, scope_id)
    if err:
        return err
    try:
        limit = max(1, min(int(request.query_params.get("limit", "20")), 50))
    except (ValueError, TypeError):
        return JSONResponse({"error": "limit must be an integer"}, status_code=400)

    rows = storage.search_structured_memories(
        query, mem_type=mem_type, scope=scope, scope_id=scope_id, limit=limit
    )
    return JSONResponse({"memories": rows, "total": len(rows)})


async def admin_get_memory(request: Request) -> JSONResponse:
    """GET /v1/api/admin/memories/{memory_id} — get a single memory."""
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.memories")
    if err:
        return err

    memory_id = request.path_params["memory_id"]
    mem = storage.get_structured_memory(memory_id)
    if not mem:
        return JSONResponse({"error": "Memory not found"}, status_code=404)
    return JSONResponse(mem)


async def admin_delete_memory(request: Request) -> JSONResponse:
    """DELETE /v1/api/admin/memories/{memory_id} — delete a memory by ID."""
    from turnstone.core.audit import record_audit
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.memories")
    if err:
        return err

    memory_id = request.path_params["memory_id"]
    existing = storage.get_structured_memory(memory_id)
    if not existing:
        return JSONResponse({"error": "Memory not found"}, status_code=404)

    storage.delete_structured_memory_by_id(memory_id)

    audit_uid, ip = _audit_context(request)
    record_audit(
        storage,
        audit_uid,
        "memory.delete",
        "memory",
        memory_id,
        {"name": existing.get("name", ""), "scope": existing.get("scope", "")},
        ip,
    )

    return JSONResponse({"status": "ok"})


# ---------------------------------------------------------------------------
# Admin: System Settings
# ---------------------------------------------------------------------------


async def _publish_config_change(request: Request) -> None:
    """Fan out config-reload to all known server nodes (best-effort, async).

    Uses the collector's node registry, the shared async proxy client,
    and bounded concurrency via the fan-out semaphore.
    """
    # Reload the console's own ConfigStore so cached values stay fresh
    # (must happen even when collector is absent — e.g. standalone console)
    config_store = getattr(request.app.state, "config_store", None)
    if config_store:
        config_store.reload()

    collector = getattr(request.app.state, "collector", None)
    if not collector:
        return
    client: httpx.AsyncClient = request.app.state.proxy_client
    headers = _proxy_auth_headers(request)
    sem = asyncio.Semaphore(_get_fan_out_limit(request))

    async def _notify(url: str) -> None:
        async with sem:
            try:
                await client.post(
                    f"{url.rstrip('/')}/v1/api/_internal/config-reload",
                    headers=headers,
                    timeout=5.0,
                )
            except Exception:
                log.warning("Config reload failed for %s", url, exc_info=True)

    nodes = collector.get_all_nodes()
    tasks = [_notify(n["server_url"]) for n in nodes if n.get("server_url")]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def admin_list_settings(request: Request) -> JSONResponse:
    """GET /v1/api/admin/settings — list all settings with effective values."""
    from turnstone.core.auth import require_permission
    from turnstone.core.settings_registry import SETTINGS, deserialize_value
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.settings")
    if err:
        return err

    stored = {r["key"]: r for r in storage.list_system_settings() if r.get("node_id", "") == ""}

    settings: list[dict[str, Any]] = []
    for key, defn in sorted(SETTINGS.items()):
        row = stored.get(key)
        if row:
            try:
                val = deserialize_value(key, row["value"])
            except (ValueError, KeyError):
                val = row["value"]
            info = {
                "key": key,
                "value": "***" if defn.is_secret else val,
                "source": "storage",
                "type": defn.type,
                "description": defn.description,
                "section": defn.section,
                "is_secret": defn.is_secret,
                "node_id": row.get("node_id", ""),
                "changed_by": row.get("changed_by", ""),
                "updated": row.get("updated", ""),
                "restart_required": defn.restart_required,
            }
        else:
            info = {
                "key": key,
                "value": "***" if defn.is_secret else defn.default,
                "source": "default",
                "type": defn.type,
                "description": defn.description,
                "section": defn.section,
                "is_secret": defn.is_secret,
                "node_id": "",
                "changed_by": "",
                "updated": "",
                "restart_required": defn.restart_required,
            }
        settings.append(info)

    return JSONResponse({"settings": settings})


async def admin_settings_schema(request: Request) -> JSONResponse:
    """GET /v1/api/admin/settings/schema — return the full settings registry."""
    from turnstone.core.auth import require_permission
    from turnstone.core.settings_registry import SETTINGS

    err = require_permission(request, "admin.settings")
    if err:
        return err

    schema: list[dict[str, Any]] = []
    for key, defn in sorted(SETTINGS.items()):
        schema.append(
            {
                "key": key,
                "type": defn.type,
                "default": defn.default,
                "description": defn.description,
                "section": defn.section,
                "is_secret": defn.is_secret,
                "min_value": defn.min_value,
                "max_value": defn.max_value,
                "choices": defn.choices,
                "restart_required": defn.restart_required,
                "help": defn.help,
                "reference_url": defn.reference_url,
            }
        )

    return JSONResponse({"schema": schema})


async def admin_update_setting(request: Request) -> JSONResponse:
    """PUT /v1/api/admin/settings/{key} — set a setting value."""
    from turnstone.core.audit import record_audit
    from turnstone.core.auth import require_permission
    from turnstone.core.settings_registry import (
        serialize_value,
        validate_key,
        validate_value,
    )
    from turnstone.core.web_helpers import read_json_or_400, require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.settings")
    if err:
        return err

    key = request.path_params["key"]
    body = await read_json_or_400(request)
    if isinstance(body, JSONResponse):
        return body

    try:
        defn = validate_key(key)
    except ValueError:
        return JSONResponse({"error": f"Unknown setting: {key}"}, status_code=400)

    if "value" not in body:
        return JSONResponse({"error": "value is required"}, status_code=400)

    raw_value = body.get("value")

    # Secret sentinel: "***" means "keep existing value"
    if defn.is_secret and raw_value == "***":
        existing = storage.get_system_setting(key)
        return JSONResponse(
            {
                "key": key,
                "value": "***",
                "source": "storage" if existing else "default",
                "type": defn.type,
                "description": defn.description,
                "section": defn.section,
                "is_secret": True,
                "node_id": existing.get("node_id", "") if existing else "",
                "changed_by": existing.get("changed_by", "") if existing else "",
                "updated": existing.get("updated", "") if existing else "",
                "restart_required": defn.restart_required,
                "unchanged": True,
            }
        )

    try:
        typed_value = validate_value(key, raw_value)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    node_id = str(body.get("node_id", ""))
    audit_uid, ip = _audit_context(request)

    storage.upsert_system_setting(
        key=key,
        value=serialize_value(typed_value),
        node_id=node_id,
        is_secret=defn.is_secret,
        changed_by=audit_uid,
    )

    record_audit(
        storage,
        audit_uid,
        "setting.update",
        "setting",
        key,
        {"value": "***" if defn.is_secret else typed_value, "node_id": node_id},
        ip,
    )

    await _publish_config_change(request)

    return JSONResponse(
        {
            "key": key,
            "value": "***" if defn.is_secret else typed_value,
            "source": "storage",
            "type": defn.type,
            "description": defn.description,
            "section": defn.section,
            "is_secret": defn.is_secret,
            "node_id": node_id,
            "changed_by": audit_uid,
            "updated": "",
            "restart_required": defn.restart_required,
        }
    )


async def admin_delete_setting(request: Request) -> JSONResponse:
    """DELETE /v1/api/admin/settings/{key} — reset a setting to default."""
    from turnstone.core.audit import record_audit
    from turnstone.core.auth import require_permission
    from turnstone.core.settings_registry import validate_key
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.settings")
    if err:
        return err

    key = request.path_params["key"]
    try:
        defn = validate_key(key)
    except ValueError:
        return JSONResponse({"error": f"Unknown setting: {key}"}, status_code=400)

    node_id = request.query_params.get("node_id", "")
    deleted = storage.delete_system_setting(key, node_id=node_id)
    if not deleted:
        return JSONResponse({"error": f"Setting '{key}' not found in storage"}, status_code=404)

    audit_uid, ip = _audit_context(request)
    record_audit(
        storage,
        audit_uid,
        "setting.delete",
        "setting",
        key,
        {"node_id": node_id},
        ip,
    )

    await _publish_config_change(request)

    return JSONResponse({"status": "ok", "key": key, "default": defn.default})


# ---------------------------------------------------------------------------
# Admin: MCP Registry
# ---------------------------------------------------------------------------


def _get_registry_url(request: Request) -> str:
    """Get the MCP Registry URL via ConfigStore, config.toml, or default."""
    from turnstone.core.config import load_config
    from turnstone.core.mcp_registry import DEFAULT_REGISTRY_URL

    # ConfigStore: validated + cached
    config_store = getattr(request.app.state, "config_store", None)
    if config_store:
        val = config_store.get("mcp.registry_url")
        if val:
            return str(val)

    # Fall back to config.toml [mcp] section
    mcp_cfg = load_config("mcp")
    url = mcp_cfg.get("registry_url", "")
    if url:
        return str(url)

    return DEFAULT_REGISTRY_URL


async def admin_registry_search(request: Request) -> JSONResponse:
    """GET /v1/api/admin/mcp-registry/search — search the MCP Registry."""
    from turnstone.core.auth import require_permission
    from turnstone.core.mcp_registry import (
        MCPRegistryClient,
        MCPRegistryError,
        RegistryServer,
        registry_server_to_dict,
    )
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.mcp")
    if err:
        return err

    q = str(request.query_params.get("search", "")).strip()
    try:
        limit = max(1, min(int(request.query_params.get("limit", "20")), 100))
    except (ValueError, TypeError):
        limit = 20
    cursor = request.query_params.get("cursor") or None

    registry_url = _get_registry_url(request)

    async with MCPRegistryClient(base_url=registry_url) as client:
        try:
            result = await client.search(q=q, limit=limit, cursor=cursor)
        except MCPRegistryError as exc:
            return JSONResponse({"error": f"Registry error: {exc}"}, status_code=502)

    # Deduplicate: keep only isLatest entries, first occurrence per name wins.
    # Skip servers with no install source (no remotes and no packages).
    seen: dict[str, RegistryServer] = {}
    for srv in result.servers:
        if srv.meta and not srv.meta.is_latest:
            continue
        if not srv.remotes and not srv.packages:
            continue
        if srv.name not in seen:
            seen[srv.name] = srv
    deduped = list(seen.values())

    # Mark which servers are already installed
    installed: dict[str, dict[str, Any]] = {}
    for s in storage.list_mcp_servers():
        rn = s.get("registry_name")
        if rn:
            installed[rn] = s

    servers_out = []
    for srv in deduped:
        d = registry_server_to_dict(srv)
        existing = installed.get(srv.name)
        if existing:
            d["installed"] = True
            d["installed_server_id"] = existing["server_id"]
            d["installed_version"] = existing.get("registry_version", "")
            d["update_available"] = existing.get("registry_version", "") != srv.version
        else:
            d["installed"] = False
            d["installed_server_id"] = ""
            d["installed_version"] = ""
            d["update_available"] = False
        servers_out.append(d)

    return JSONResponse(
        {
            "servers": servers_out,
            "total": result.total_count,
            "next_cursor": result.next_cursor,
        }
    )


async def admin_registry_install(request: Request) -> JSONResponse:
    """POST /v1/api/admin/mcp-registry/install — install a server from the registry."""
    import uuid

    from turnstone.core.audit import record_audit
    from turnstone.core.auth import require_permission
    from turnstone.core.mcp_registry import (
        MCPRegistryClient,
        MCPRegistryError,
        resolve_install_config,
        sanitize_registry_name,
    )
    from turnstone.core.web_helpers import read_json_or_400, require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.mcp")
    if err:
        return err

    body = await read_json_or_400(request)
    if isinstance(body, JSONResponse):
        return body

    registry_name = str(body.get("registry_name", "")).strip()
    if not registry_name:
        return JSONResponse({"error": "registry_name is required"}, status_code=400)

    source = str(body.get("source", "")).strip()
    if source not in ("remote", "package"):
        return JSONResponse({"error": "source must be 'remote' or 'package'"}, status_code=400)

    try:
        index = int(body.get("index", 0))
    except (ValueError, TypeError):
        return JSONResponse({"error": "index must be an integer"}, status_code=400)
    variables = body.get("variables") or {}
    env_values = body.get("env") or {}
    header_values = body.get("headers") or {}
    if not isinstance(variables, dict):
        return JSONResponse({"error": "variables must be an object"}, status_code=400)
    if not isinstance(env_values, dict):
        return JSONResponse({"error": "env must be an object"}, status_code=400)
    if not isinstance(header_values, dict):
        return JSONResponse({"error": "headers must be an object"}, status_code=400)
    custom_name = str(body.get("name", "")).strip()

    # Check for duplicates
    existing = storage.get_mcp_server_by_registry_name(registry_name)
    if existing:
        return JSONResponse(
            {
                "error": (
                    f"Registry server '{registry_name}' is already installed "
                    f"as '{existing['name']}'"
                )
            },
            status_code=409,
        )

    # Check max servers
    current = storage.list_mcp_servers()
    max_servers = _get_mcp_max_servers(request)
    if len(current) >= max_servers:
        return JSONResponse({"error": f"Maximum {max_servers} servers"}, status_code=400)

    # Fetch the specific server from the registry
    registry_url = _get_registry_url(request)
    async with MCPRegistryClient(base_url=registry_url) as client:
        try:
            result = await client.search(q=registry_name, limit=100)
        except MCPRegistryError as exc:
            return JSONResponse({"error": f"Registry error: {exc}"}, status_code=502)

    # Find the exact server by name
    server = None
    for s in result.servers:
        if s.name == registry_name:
            server = s
            break
    if server is None:
        return JSONResponse(
            {"error": f"Server '{registry_name}' not found in registry"},
            status_code=404,
        )

    # Resolve install configuration
    try:
        config = resolve_install_config(server, source, index, variables)
    except MCPRegistryError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except (IndexError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    # Determine server name
    try:
        name = custom_name or sanitize_registry_name(registry_name)
    except MCPRegistryError as exc:
        return JSONResponse({"error": f"{exc}; provide a custom 'name'"}, status_code=400)
    if not name or not _MCP_NAME_RE.match(name) or "__" in name:
        return JSONResponse(
            {"error": f"Name '{name}' is invalid; provide a custom 'name'"},
            status_code=400,
        )
    if storage.get_mcp_server_by_name(name):
        return JSONResponse(
            {"error": f"Server name '{name}' already exists; provide a custom 'name'"},
            status_code=409,
        )

    # Merge user-provided env and header values
    merged_env = config.get("env", {})
    if isinstance(env_values, dict):
        merged_env.update(env_values)
    merged_headers = config.get("headers", {})
    if isinstance(header_values, dict):
        merged_headers.update(header_values)

    server_id = uuid.uuid4().hex
    audit_uid, ip = _audit_context(request)

    storage.create_mcp_server(
        server_id=server_id,
        name=name,
        transport=config["transport"],
        command=config.get("command", ""),
        args=json.dumps(config.get("args", [])),
        url=config.get("url", ""),
        headers=json.dumps(merged_headers),
        env=json.dumps(merged_env),
        auto_approve=False,
        enabled=True,
        created_by=audit_uid,
        registry_name=registry_name,
        registry_version=config["registry_version"],
        registry_meta=json.dumps(config["registry_meta"]),
    )

    record_audit(
        storage,
        audit_uid,
        "mcp_server.registry_install",
        "mcp_server",
        server_id,
        {"name": name, "registry_name": registry_name, "source": source},
        ip,
    )

    # Auto-reload nodes for one-click UX
    await _notify_nodes_mcp_reload(request)

    server_row = storage.get_mcp_server(server_id)
    return JSONResponse(_mcp_server_to_detail(_mask_mcp_secrets(server_row or {})))


# ---------------------------------------------------------------------------
# Admin: MCP Servers
# ---------------------------------------------------------------------------

_MCP_NAME_RE = re.compile(r"^[a-zA-Z0-9._-]+$")
_MCP_MAX_SERVERS = 200  # fallback; prefer cluster.mcp_max_servers from storage


def _get_mcp_max_servers(request: Request) -> int:
    """Read cluster.mcp_max_servers via ConfigStore (validated + cached)."""
    config_store = getattr(request.app.state, "config_store", None)
    if config_store:
        return int(config_store.get("cluster.mcp_max_servers"))
    return _MCP_MAX_SERVERS


def _mask_mcp_secrets(server: dict[str, Any], reveal: bool = False) -> dict[str, Any]:
    """Replace env/headers values with '***' unless reveal is True."""
    if reveal:
        return server
    s = dict(server)
    if s.get("env") and s["env"] != "{}":
        try:
            env_dict = json.loads(s["env"]) if isinstance(s["env"], str) else s["env"]
            s["env"] = json.dumps({k: "***" for k in env_dict})
        except (json.JSONDecodeError, TypeError):
            s["env"] = "{}"
    if s.get("headers") and s["headers"] != "{}":
        try:
            hdr_dict = json.loads(s["headers"]) if isinstance(s["headers"], str) else s["headers"]
            s["headers"] = json.dumps({k: "***" for k in hdr_dict})
        except (json.JSONDecodeError, TypeError):
            s["headers"] = "{}"
    return s


def _mcp_server_to_detail(
    server: dict[str, Any],
    node_statuses: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Convert a storage dict to a McpServerDetail-shaped dict."""
    d = dict(server)
    d["status"] = node_statuses or {}
    return d


async def _collect_mcp_status(
    request: Request,
) -> dict[str, dict[str, dict[str, Any]]]:
    """Query all nodes for MCP status. Returns {node_id: {server_name: status}}."""
    collector: ClusterCollector = request.app.state.collector
    nodes = collector.get_all_nodes()
    client: httpx.AsyncClient = request.app.state.proxy_client
    headers = _proxy_auth_headers(request)
    sem = asyncio.Semaphore(_get_fan_out_limit(request))

    async def _fetch(node: dict[str, Any]) -> tuple[str, dict[str, dict[str, Any]] | None]:
        node_id = node.get("node_id", "")
        url = node.get("server_url", "")
        if not url:
            return node_id, None
        async with sem:
            try:
                resp = await client.get(
                    f"{url.rstrip('/')}/v1/api/_internal/mcp-status",
                    headers=headers,
                    timeout=10,
                )
                if resp.status_code == 200:
                    return node_id, resp.json().get("servers", {})
            except Exception:
                log.debug("Failed to fetch MCP status from node %s", node_id, exc_info=True)
        return node_id, None

    results = await asyncio.gather(*[_fetch(n) for n in nodes])
    return {nid: servers for nid, servers in results if servers is not None}


async def admin_list_mcp_servers(request: Request) -> JSONResponse:
    """GET /v1/api/admin/mcp-servers — list all MCP server definitions."""
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.mcp")
    if err:
        return err

    reveal = str(request.query_params.get("reveal", "")).lower() in ("true", "1")
    servers = storage.list_mcp_servers()

    # Collect live status from all nodes
    node_statuses = await _collect_mcp_status(request)

    db_names: set[str] = set()
    result = []
    for s in servers:
        db_names.add(s["name"])
        # Build per-node status for this server
        per_node: dict[str, dict[str, Any]] = {}
        for node_id, node_servers in node_statuses.items():
            status = node_servers.get(s["name"])
            if status:
                per_node[node_id] = status
        s = _mask_mcp_secrets(s, reveal)
        result.append(_mcp_server_to_detail(s, per_node))

    # Merge config-sourced servers visible on nodes but not in DB
    config_names: set[str] = set()
    for node_servers in node_statuses.values():
        for name in node_servers:
            if name not in db_names:
                config_names.add(name)
    for name in sorted(config_names):
        # Build a synthetic read-only entry from node-reported data
        per_node = {}
        transport = "stdio"
        command = ""
        url = ""
        for node_id, node_servers in node_statuses.items():
            ns = node_servers.get(name)
            if ns:
                per_node[node_id] = ns
                transport = ns.get("transport", "stdio")
                command = ns.get("command", "")
                url = ns.get("url", "")
        result.append(
            {
                "server_id": "",
                "name": name,
                "transport": transport,
                "command": command,
                "args": "[]",
                "url": url,
                "headers": "{}",
                "env": "{}",
                "auto_approve": False,
                "enabled": True,
                "created_by": "",
                "created": "",
                "updated": "",
                "source": "config",
                "status": per_node,
            }
        )

    return JSONResponse({"servers": result})


async def admin_create_mcp_server(request: Request) -> JSONResponse:
    """POST /v1/api/admin/mcp-servers — create an MCP server definition."""
    import uuid

    from turnstone.core.audit import record_audit
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import read_json_or_400, require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.mcp")
    if err:
        return err

    body = await read_json_or_400(request)
    if isinstance(body, JSONResponse):
        return body

    name = str(body.get("name", "")).strip()[:64]
    transport = str(body.get("transport", "")).strip()
    if not name:
        return JSONResponse({"error": "name is required"}, status_code=400)
    if not _MCP_NAME_RE.match(name):
        return JSONResponse(
            {"error": "name must match [a-zA-Z0-9._-]+"},
            status_code=400,
        )
    if "__" in name:
        return JSONResponse(
            {"error": "name must not contain '__' (reserved delimiter)"},
            status_code=400,
        )
    if transport not in ("stdio", "streamable-http"):
        return JSONResponse(
            {"error": "transport must be 'stdio' or 'streamable-http'"},
            status_code=400,
        )
    if transport == "stdio" and not str(body.get("command", "")).strip():
        return JSONResponse({"error": "command is required for stdio transport"}, status_code=400)
    if transport == "streamable-http" and not str(body.get("url", "")).strip():
        return JSONResponse(
            {"error": "url is required for streamable-http transport"}, status_code=400
        )

    # Check max servers
    existing = storage.list_mcp_servers()
    max_servers = _get_mcp_max_servers(request)
    if len(existing) >= max_servers:
        return JSONResponse(
            {"error": f"Maximum {max_servers} servers"},
            status_code=400,
        )

    # Check name uniqueness
    if storage.get_mcp_server_by_name(name):
        return JSONResponse(
            {"error": f"Server '{name}' already exists"},
            status_code=409,
        )

    server_id = uuid.uuid4().hex
    audit_uid, ip = _audit_context(request)

    args_list = body.get("args", [])
    headers_dict = body.get("headers", {})
    env_dict = body.get("env", {})

    storage.create_mcp_server(
        server_id=server_id,
        name=name,
        transport=transport,
        command=str(body.get("command", "")).strip(),
        args=json.dumps(args_list) if isinstance(args_list, list) else "[]",
        url=str(body.get("url", "")).strip(),
        headers=json.dumps(headers_dict) if isinstance(headers_dict, dict) else "{}",
        env=json.dumps(env_dict) if isinstance(env_dict, dict) else "{}",
        auto_approve=bool(body.get("auto_approve", False)),
        enabled=bool(body.get("enabled", True)),
        created_by=audit_uid,
    )

    record_audit(
        storage,
        audit_uid,
        "mcp_server.create",
        "mcp_server",
        server_id,
        {"name": name},
        ip,
    )

    server = storage.get_mcp_server(server_id)
    return JSONResponse(_mcp_server_to_detail(_mask_mcp_secrets(server or {})))


async def admin_get_mcp_server(request: Request) -> JSONResponse:
    """GET /v1/api/admin/mcp-servers/{server_id} — get single MCP server."""
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.mcp")
    if err:
        return err

    server_id = request.path_params["server_id"]
    server = storage.get_mcp_server(server_id)
    if server is None:
        return JSONResponse({"error": "MCP server not found"}, status_code=404)

    node_statuses = await _collect_mcp_status(request)
    per_node: dict[str, dict[str, Any]] = {}
    for node_id, node_servers in node_statuses.items():
        status = node_servers.get(server["name"])
        if status:
            per_node[node_id] = status

    reveal = str(request.query_params.get("reveal", "")).lower() in ("true", "1")
    server = _mask_mcp_secrets(server, reveal)
    return JSONResponse(_mcp_server_to_detail(server, per_node))


async def admin_update_mcp_server(request: Request) -> JSONResponse:
    """PUT /v1/api/admin/mcp-servers/{server_id} — update an MCP server."""
    from turnstone.core.audit import record_audit
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import read_json_or_400, require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.mcp")
    if err:
        return err

    server_id = request.path_params["server_id"]
    existing = storage.get_mcp_server(server_id)
    if existing is None:
        return JSONResponse({"error": "MCP server not found"}, status_code=404)

    body = await read_json_or_400(request)
    if isinstance(body, JSONResponse):
        return body

    updates: dict[str, Any] = {}
    if "name" in body:
        name = str(body["name"]).strip()[:64]
        if not name:
            return JSONResponse({"error": "name cannot be empty"}, status_code=400)
        if not _MCP_NAME_RE.match(name):
            return JSONResponse(
                {"error": "name must match [a-zA-Z0-9._-]+"},
                status_code=400,
            )
        if "__" in name:
            return JSONResponse(
                {"error": "name must not contain '__'"},
                status_code=400,
            )
        if name != existing["name"] and storage.get_mcp_server_by_name(name):
            return JSONResponse(
                {"error": f"Server '{name}' already exists"},
                status_code=409,
            )
        updates["name"] = name
    if "transport" in body:
        transport = str(body["transport"]).strip()
        if transport not in ("stdio", "streamable-http"):
            return JSONResponse(
                {"error": "transport must be 'stdio' or 'streamable-http'"},
                status_code=400,
            )
        updates["transport"] = transport
    if "command" in body:
        updates["command"] = str(body["command"]).strip()
    if "args" in body:
        updates["args"] = json.dumps(body["args"]) if isinstance(body["args"], list) else "[]"
    if "url" in body:
        updates["url"] = str(body["url"]).strip()
    if "headers" in body:
        updates["headers"] = (
            json.dumps(body["headers"]) if isinstance(body["headers"], dict) else "{}"
        )
    if "env" in body:
        updates["env"] = json.dumps(body["env"]) if isinstance(body["env"], dict) else "{}"
    if "auto_approve" in body:
        updates["auto_approve"] = bool(body["auto_approve"])
    if "enabled" in body:
        updates["enabled"] = bool(body["enabled"])

    if updates:
        storage.update_mcp_server(server_id, **updates)

    audit_uid, ip = _audit_context(request)
    audit_detail = dict(updates)
    for _secret_key in ("env", "headers"):
        if _secret_key in audit_detail:
            audit_detail[_secret_key] = "(updated)"
    record_audit(
        storage,
        audit_uid,
        "mcp_server.update",
        "mcp_server",
        server_id,
        audit_detail,
        ip,
    )

    server = storage.get_mcp_server(server_id)
    return JSONResponse(_mcp_server_to_detail(_mask_mcp_secrets(server or {})))


async def admin_delete_mcp_server(request: Request) -> JSONResponse:
    """DELETE /v1/api/admin/mcp-servers/{server_id}."""
    from turnstone.core.audit import record_audit
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.mcp")
    if err:
        return err

    server_id = request.path_params["server_id"]
    existing = storage.get_mcp_server(server_id)
    if existing is None:
        return JSONResponse({"error": "MCP server not found"}, status_code=404)

    storage.delete_mcp_server(server_id)

    audit_uid, ip = _audit_context(request)
    record_audit(
        storage,
        audit_uid,
        "mcp_server.delete",
        "mcp_server",
        server_id,
        {"name": existing.get("name", "")},
        ip,
    )

    return JSONResponse({"status": "ok"})


async def _notify_nodes_mcp_reload(request: Request) -> dict[str, Any]:
    """Tell all nodes to re-read the mcp_servers DB table and reconcile."""
    collector: ClusterCollector = request.app.state.collector
    nodes = collector.get_all_nodes()
    client: httpx.AsyncClient = request.app.state.proxy_client
    headers = _proxy_auth_headers(request)
    sem = asyncio.Semaphore(_get_fan_out_limit(request))

    async def _notify(node: dict[str, Any]) -> tuple[str, Any]:
        node_id = node.get("node_id", "")
        url = node.get("server_url", "")
        if not url:
            return node_id, None
        async with sem:
            try:
                resp = await client.post(
                    f"{url.rstrip('/')}/v1/api/_internal/mcp-reload",
                    headers=headers,
                    timeout=30,
                )
                return node_id, resp.json()
            except Exception as exc:
                log.debug("Failed to notify node %s for MCP reload", node_id, exc_info=True)
                return node_id, {"error": str(exc)}

    results = await asyncio.gather(*[_notify(n) for n in nodes])
    return {nid: data for nid, data in results if data is not None}


async def admin_mcp_reload(request: Request) -> JSONResponse:
    """POST /v1/api/admin/mcp-servers/reload — tell nodes to re-read DB."""
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.mcp")
    if err:
        return err

    results = await _notify_nodes_mcp_reload(request)
    return JSONResponse({"status": "ok", "results": results})


async def admin_import_mcp_config(request: Request) -> JSONResponse:
    """POST /v1/api/admin/mcp-servers/import — import from pasted JSON config."""
    import uuid

    from turnstone.core.audit import record_audit
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import read_json_or_400, require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.mcp")
    if err:
        return err

    body = await read_json_or_400(request)
    if isinstance(body, JSONResponse):
        return body

    data = body.get("config")
    if not isinstance(data, dict):
        return JSONResponse(
            {"error": "config is required (JSON object with mcpServers key)"}, status_code=400
        )

    servers = data.get("mcpServers", {})
    if not isinstance(servers, dict) or not servers:
        return JSONResponse(
            {"error": "No mcpServers found in config"},
            status_code=400,
        )

    imported: list[str] = []
    skipped: list[str] = []
    errors: list[str] = []
    audit_uid, ip = _audit_context(request)
    current_count = len(storage.list_mcp_servers())
    max_servers = _get_mcp_max_servers(request)

    for srv_name, cfg in servers.items():
        srv_name = str(srv_name).strip()[:64]
        if not srv_name or not _MCP_NAME_RE.match(srv_name) or "__" in srv_name:
            errors.append(f"{srv_name}: invalid server name")
            continue
        if storage.get_mcp_server_by_name(srv_name):
            skipped.append(srv_name)
            continue
        if current_count >= max_servers:
            errors.append(f"{srv_name}: max servers reached")
            break

        transport = "stdio"
        if "url" in cfg or cfg.get("type") in ("http", "streamable-http"):
            transport = "streamable-http"

        # Coerce fields to expected types
        raw_args = cfg.get("args", [])
        raw_headers = cfg.get("headers", {})
        raw_env = cfg.get("env", {})
        if not isinstance(raw_args, list):
            errors.append(f"{srv_name}: args must be a list")
            continue
        if not isinstance(raw_headers, dict):
            errors.append(f"{srv_name}: headers must be an object")
            continue
        if not isinstance(raw_env, dict):
            errors.append(f"{srv_name}: env must be an object")
            continue

        server_id = uuid.uuid4().hex
        try:
            storage.create_mcp_server(
                server_id=server_id,
                name=srv_name,
                transport=transport,
                command=str(cfg.get("command", "")),
                args=json.dumps(raw_args),
                url=str(cfg.get("url", "")),
                headers=json.dumps(raw_headers),
                env=json.dumps(raw_env),
                auto_approve=False,
                enabled=True,
                created_by=audit_uid,
            )
            imported.append(srv_name)
            current_count += 1
        except Exception as exc:
            errors.append(f"{srv_name}: {exc}")

    if imported:
        record_audit(
            storage,
            audit_uid,
            "mcp_server.import",
            "mcp_server",
            "",
            {"imported": imported, "skipped": skipped},
            ip,
        )

    return JSONResponse({"imported": imported, "skipped": skipped, "errors": errors})


# ---------------------------------------------------------------------------
# Admin: Model Definitions
# ---------------------------------------------------------------------------

_MODEL_ALIAS_RE = re.compile(r"^[a-zA-Z0-9._-]+$")
_MODEL_PROVIDERS = frozenset({"openai", "anthropic", "openai-compatible", "google"})
_REASONING_EFFORT_CHOICES = frozenset(
    {"", "none", "minimal", "low", "medium", "high", "xhigh", "max"}
)
# Keep in sync with turnstone.core.providers._google.GOOGLE_DEFAULT_BASE_URL
_PROVIDER_DEFAULT_URLS: dict[str, str] = {
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com",
    "google": "https://generativelanguage.googleapis.com/v1beta/openai/",
}


def _mask_model_secrets(model: dict[str, Any]) -> dict[str, Any]:
    """Replace api_key with '***' (unconditional, write-only)."""
    m = dict(model)
    if m.get("api_key"):
        m["api_key"] = "***"
    return m


async def _collect_model_status(
    request: Request,
) -> dict[str, dict[str, dict[str, Any]]]:
    """Query all nodes for model status. Returns {node_id: {alias: info}}."""
    collector: ClusterCollector = request.app.state.collector
    nodes = collector.get_all_nodes()
    client: httpx.AsyncClient = request.app.state.proxy_client
    headers = _proxy_auth_headers(request)
    sem = asyncio.Semaphore(_get_fan_out_limit(request))

    async def _fetch(node: dict[str, Any]) -> tuple[str, dict[str, dict[str, Any]] | None]:
        node_id = node.get("node_id", "")
        url = node.get("server_url", "")
        if not url:
            return node_id, None
        async with sem:
            try:
                resp = await client.get(
                    f"{url.rstrip('/')}/v1/api/_internal/model-status",
                    headers=headers,
                    timeout=10,
                )
                if resp.status_code == 200:
                    return node_id, resp.json().get("models", {})
            except Exception:
                log.debug("Failed to fetch model status from node %s", node_id, exc_info=True)
        return node_id, None

    results = await asyncio.gather(*[_fetch(n) for n in nodes])
    return {nid: models for nid, models in results if models is not None}


async def _notify_nodes_model_reload(request: Request) -> dict[str, Any]:
    """Tell all nodes to re-read model definitions from DB and rebuild registry."""
    collector: ClusterCollector = request.app.state.collector
    nodes = collector.get_all_nodes()
    client: httpx.AsyncClient = request.app.state.proxy_client
    headers = _proxy_auth_headers(request)
    sem = asyncio.Semaphore(_get_fan_out_limit(request))

    async def _notify(node: dict[str, Any]) -> tuple[str, Any]:
        node_id = node.get("node_id", "")
        url = node.get("server_url", "")
        if not url:
            return node_id, None
        async with sem:
            try:
                resp = await client.post(
                    f"{url.rstrip('/')}/v1/api/_internal/model-reload",
                    headers=headers,
                    timeout=30,
                )
                return node_id, resp.json()
            except Exception as exc:
                log.debug("Failed to notify node %s for model reload", node_id, exc_info=True)
                return node_id, {"error": str(exc)}

    results = await asyncio.gather(*[_notify(n) for n in nodes])
    return {nid: data for nid, data in results if data is not None}


async def admin_list_model_definitions(request: Request) -> JSONResponse:
    """GET /v1/api/admin/model-definitions — list all model definitions."""
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.models")
    if err:
        return err

    db_models = storage.list_model_definitions()

    # Collect live status from all nodes
    node_statuses = await _collect_model_status(request)

    db_aliases: set[str] = set()
    result = []
    for m in db_models:
        db_aliases.add(m["alias"])
        m["source"] = "db"
        result.append(_mask_model_secrets(m))

    # Merge config-sourced models visible on nodes but not in DB
    config_aliases: set[str] = set()
    for node_models in node_statuses.values():
        for alias in node_models:
            if alias not in db_aliases:
                config_aliases.add(alias)
    for alias in sorted(config_aliases):
        # Build a synthetic read-only entry from node-reported data
        model_name = ""
        provider = "openai"
        context_window = 0
        cfg_temperature = None
        cfg_max_tokens = None
        cfg_reasoning_effort = None
        for node_models in node_statuses.values():
            nm = node_models.get(alias)
            if nm:
                model_name = nm.get("model", "")
                provider = nm.get("provider", "openai")
                context_window = nm.get("context_window", 0)
                cfg_temperature = nm.get("temperature")
                cfg_max_tokens = nm.get("max_tokens")
                cfg_reasoning_effort = nm.get("reasoning_effort")
                break
        result.append(
            {
                "definition_id": "",
                "alias": alias,
                "model": model_name,
                "provider": provider,
                "base_url": "",
                "api_key": "",
                "context_window": context_window,
                "capabilities": "{}",
                "enabled": True,
                "temperature": cfg_temperature,
                "max_tokens": cfg_max_tokens,
                "reasoning_effort": cfg_reasoning_effort,
                "source": "config",
                "created_by": "",
                "created": "",
                "updated": "",
            }
        )

    # Include the effective default alias so the UI can highlight it.
    # Prefer ConfigStore override, fall back to config.toml [model].default,
    # then validate against the actual enabled model list (same fallback
    # rules as load_model_registry).
    configured_default = ""
    cs = getattr(request.app.state, "config_store", None)
    if cs:
        configured_default = cs.get("model.default_alias") or ""
    if not configured_default:
        from turnstone.core.config import load_config as _load_cfg

        configured_default = _load_cfg().get("model", {}).get("default", "default")

    enabled_aliases = [m["alias"] for m in result if m.get("alias") and m.get("enabled", True)]
    enabled_set = set(enabled_aliases)
    if configured_default in enabled_set:
        default_alias = configured_default
    elif "default" in enabled_set:
        default_alias = "default"
    elif enabled_aliases:
        default_alias = enabled_aliases[0]
    else:
        default_alias = ""

    return JSONResponse({"models": result, "default_alias": default_alias})


async def admin_create_model_definition(request: Request) -> JSONResponse:
    """POST /v1/api/admin/model-definitions — create a model definition."""
    import uuid

    from turnstone.core.audit import record_audit
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import read_json_or_400, require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.models")
    if err:
        return err

    body = await read_json_or_400(request)
    if isinstance(body, JSONResponse):
        return body

    alias = str(body.get("alias", "")).strip()[:64]
    model_name = str(body.get("model", "")).strip()[:128]
    if not alias:
        return JSONResponse({"error": "alias is required"}, status_code=400)
    if not model_name:
        return JSONResponse({"error": "model is required"}, status_code=400)
    if not _MODEL_ALIAS_RE.match(alias):
        return JSONResponse(
            {"error": "alias must match [a-zA-Z0-9._-]+"},
            status_code=400,
        )

    # Check alias uniqueness
    if storage.get_model_definition_by_alias(alias):
        return JSONResponse(
            {"error": f"Model alias '{alias}' already exists"},
            status_code=409,
        )

    definition_id = uuid.uuid4().hex
    audit_uid, ip = _audit_context(request)

    provider = str(body.get("provider", "openai")).strip()
    if provider not in _MODEL_PROVIDERS:
        return JSONResponse(
            {"error": f"Unknown provider: {provider!r}"},
            status_code=400,
        )
    base_url = str(body.get("base_url", "")).strip()
    api_key = str(body.get("api_key", "")).strip()
    ctx_raw = body.get("context_window", 32768)
    context_window = max(0, int(ctx_raw)) if isinstance(ctx_raw, (int, float)) else 0
    caps = body.get("capabilities", {})
    capabilities = json.dumps(caps) if isinstance(caps, dict) else "{}"
    enabled = bool(body.get("enabled", True))

    # Per-model sampling overrides (None = use global default)
    temperature: float | None = None
    if body.get("temperature") is not None:
        try:
            temperature = float(body["temperature"])
        except (ValueError, TypeError):
            return JSONResponse({"error": "temperature must be a number"}, status_code=400)
        if not 0.0 <= temperature <= 2.0:
            return JSONResponse(
                {"error": "temperature must be between 0.0 and 2.0"}, status_code=400
            )
    max_tokens: int | None = None
    if body.get("max_tokens") is not None:
        try:
            max_tokens = int(body["max_tokens"])
        except (ValueError, TypeError):
            return JSONResponse({"error": "max_tokens must be an integer"}, status_code=400)
        if max_tokens < 1:
            return JSONResponse({"error": "max_tokens must be >= 1"}, status_code=400)
    reasoning_effort: str | None = None
    if body.get("reasoning_effort") is not None:
        reasoning_effort = str(body["reasoning_effort"]).strip()
        if reasoning_effort and reasoning_effort not in _REASONING_EFFORT_CHOICES:
            return JSONResponse(
                {"error": f"Invalid reasoning_effort: {reasoning_effort!r}"},
                status_code=400,
            )
        if not reasoning_effort:
            reasoning_effort = None

    storage.create_model_definition(
        definition_id=definition_id,
        alias=alias,
        model=model_name,
        provider=provider,
        base_url=base_url,
        api_key=api_key,
        context_window=context_window,
        capabilities=capabilities,
        enabled=enabled,
        created_by=audit_uid,
        temperature=temperature,
        max_tokens=max_tokens,
        reasoning_effort=reasoning_effort,
    )

    record_audit(
        storage,
        audit_uid,
        "model_definition.create",
        "model_definition",
        definition_id,
        {"alias": alias},
        ip,
    )

    created = storage.get_model_definition(definition_id)
    if created is None:
        return JSONResponse(
            {"error": f"Model alias '{alias}' already exists (concurrent insert)"},
            status_code=409,
        )
    return JSONResponse(_mask_model_secrets(created))


async def admin_get_model_definition(request: Request) -> JSONResponse:
    """GET /v1/api/admin/model-definitions/{definition_id}."""
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.models")
    if err:
        return err

    definition_id = request.path_params["definition_id"]
    model_def = storage.get_model_definition(definition_id)
    if model_def is None:
        return JSONResponse({"error": "Model definition not found"}, status_code=404)

    return JSONResponse(_mask_model_secrets(model_def))


async def admin_update_model_definition(request: Request) -> JSONResponse:
    """PUT /v1/api/admin/model-definitions/{definition_id}."""
    from turnstone.core.audit import record_audit
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import read_json_or_400, require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.models")
    if err:
        return err

    definition_id = request.path_params["definition_id"]
    existing = storage.get_model_definition(definition_id)
    if existing is None:
        return JSONResponse({"error": "Model definition not found"}, status_code=404)

    body = await read_json_or_400(request)
    if isinstance(body, JSONResponse):
        return body

    updates: dict[str, Any] = {}
    if "alias" in body:
        alias = str(body["alias"]).strip()[:64]
        if not alias:
            return JSONResponse({"error": "alias cannot be empty"}, status_code=400)
        if not _MODEL_ALIAS_RE.match(alias):
            return JSONResponse(
                {"error": "alias must match [a-zA-Z0-9._-]+"},
                status_code=400,
            )
        if alias != existing["alias"] and storage.get_model_definition_by_alias(alias):
            return JSONResponse(
                {"error": f"Model alias '{alias}' already exists"},
                status_code=409,
            )
        updates["alias"] = alias
    if "model" in body:
        model_val = str(body["model"]).strip()[:128]
        if not model_val:
            return JSONResponse({"error": "model cannot be empty"}, status_code=400)
        updates["model"] = model_val
    if "provider" in body:
        prov = str(body["provider"]).strip()
        if prov not in _MODEL_PROVIDERS:
            return JSONResponse(
                {"error": f"Unknown provider: {prov!r}"},
                status_code=400,
            )
        updates["provider"] = prov
    if "base_url" in body:
        updates["base_url"] = str(body["base_url"]).strip()
    if "api_key" in body:
        api_key = str(body["api_key"]).strip()
        # Sentinel "***" or empty string means "keep existing"
        if api_key and api_key != "***":
            updates["api_key"] = api_key
    if "context_window" in body:
        ctx_raw = body["context_window"]
        updates["context_window"] = max(0, int(ctx_raw)) if isinstance(ctx_raw, (int, float)) else 0
    if "capabilities" in body:
        caps = body["capabilities"]
        updates["capabilities"] = json.dumps(caps) if isinstance(caps, dict) else "{}"
    if "enabled" in body:
        updates["enabled"] = bool(body["enabled"])

    # Per-model sampling overrides — explicit null clears to "use global default"
    if "temperature" in body:
        raw_temp = body["temperature"]
        if raw_temp is None:
            updates["temperature"] = None
        else:
            try:
                temp_val = float(raw_temp)
            except (ValueError, TypeError):
                return JSONResponse({"error": "temperature must be a number"}, status_code=400)
            if not 0.0 <= temp_val <= 2.0:
                return JSONResponse(
                    {"error": "temperature must be between 0.0 and 2.0"},
                    status_code=400,
                )
            updates["temperature"] = temp_val
    if "max_tokens" in body:
        raw_mt = body["max_tokens"]
        if raw_mt is None:
            updates["max_tokens"] = None
        else:
            try:
                mt_val = int(raw_mt)
            except (ValueError, TypeError):
                return JSONResponse({"error": "max_tokens must be an integer"}, status_code=400)
            if mt_val < 1:
                return JSONResponse({"error": "max_tokens must be >= 1"}, status_code=400)
            updates["max_tokens"] = mt_val
    if "reasoning_effort" in body:
        raw_re = body["reasoning_effort"]
        if raw_re is None:
            updates["reasoning_effort"] = None
        else:
            re_val = str(raw_re).strip()
            if not re_val:
                updates["reasoning_effort"] = None
            elif re_val not in _REASONING_EFFORT_CHOICES:
                return JSONResponse(
                    {"error": f"Invalid reasoning_effort: {re_val!r}"},
                    status_code=400,
                )
            else:
                updates["reasoning_effort"] = re_val

    if updates:
        storage.update_model_definition(definition_id, **updates)

    audit_uid, ip = _audit_context(request)
    audit_detail = dict(updates)
    if "api_key" in audit_detail:
        audit_detail["api_key"] = "(updated)"
    record_audit(
        storage,
        audit_uid,
        "model_definition.update",
        "model_definition",
        definition_id,
        audit_detail,
        ip,
    )

    model_def = storage.get_model_definition(definition_id)
    return JSONResponse(_mask_model_secrets(model_def or {}))


async def admin_delete_model_definition(request: Request) -> JSONResponse:
    """DELETE /v1/api/admin/model-definitions/{definition_id}."""
    from turnstone.core.audit import record_audit
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.models")
    if err:
        return err

    definition_id = request.path_params["definition_id"]
    existing = storage.get_model_definition(definition_id)
    if existing is None:
        return JSONResponse({"error": "Model definition not found"}, status_code=404)

    storage.delete_model_definition(definition_id)

    audit_uid, ip = _audit_context(request)
    record_audit(
        storage,
        audit_uid,
        "model_definition.delete",
        "model_definition",
        definition_id,
        {"alias": existing.get("alias", "")},
        ip,
    )

    return JSONResponse({"status": "ok", "definition_id": definition_id})


async def admin_model_reload(request: Request) -> JSONResponse:
    """POST /v1/api/admin/model-definitions/reload — tell nodes to re-read DB."""
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.models")
    if err:
        return err

    # Ensure config (including model.default_alias) is fresh on all nodes
    # before they rebuild their model registries.
    await _publish_config_change(request)

    results = await _notify_nodes_model_reload(request)
    return JSONResponse({"status": "ok", "results": results})


async def admin_detect_model(request: Request) -> JSONResponse:
    """POST /v1/api/admin/model-definitions/detect — stateless endpoint probe."""
    import asyncio

    from turnstone.core.auth import require_permission
    from turnstone.core.model_registry import probe_model_endpoint
    from turnstone.core.web_helpers import read_json_or_400, require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.models")
    if err:
        return err

    body = await read_json_or_400(request)
    if isinstance(body, JSONResponse):
        return body

    provider = str(body.get("provider", "openai")).strip()
    base_url = str(body.get("base_url", "")).strip()
    api_key = str(body.get("api_key", "")).strip()
    model = str(body.get("model", "")).strip()
    definition_id = str(body.get("definition_id", "")).strip()

    if provider not in _MODEL_PROVIDERS:
        return JSONResponse({"error": f"Unknown provider: {provider!r}"}, status_code=400)

    # Resolve api_key from DB when the UI sends the masked sentinel
    if (not api_key or api_key == "***") and definition_id:
        row = storage.get_model_definition(definition_id)
        if row:
            api_key = row.get("api_key", "")
            if not base_url:
                base_url = row.get("base_url", "")

    # Apply provider default URL if still empty
    if not base_url:
        base_url = _PROVIDER_DEFAULT_URLS.get(provider, "")

    # For commercial endpoints an api_key is required
    _normalized = (base_url if "://" in base_url else f"https://{base_url}") if base_url else ""
    _hostname = (urllib.parse.urlparse(_normalized).hostname or "") if _normalized else ""
    if not api_key and (
        not base_url
        or _hostname == "api.openai.com"
        or _hostname.endswith(".openai.com")
        or _hostname == "api.anthropic.com"
        or _hostname.endswith(".anthropic.com")
        or _hostname.endswith(".googleapis.com")
    ):
        return JSONResponse({"error": "api_key is required"}, status_code=400)

    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(
        None, probe_model_endpoint, provider, base_url, api_key, model
    )
    return JSONResponse(result)


async def admin_model_capabilities(request: Request) -> JSONResponse:
    """GET /v1/api/admin/model-capabilities — static capability lookup."""
    from turnstone.core.auth import require_permission
    from turnstone.core.providers import lookup_model_capabilities

    err = require_permission(request, "admin.models")
    if err:
        return err

    provider = request.query_params.get("provider", "").strip()
    model = request.query_params.get("model", "").strip()

    if provider not in _MODEL_PROVIDERS:
        return JSONResponse({"error": f"Unknown provider: {provider!r}"}, status_code=400)
    if not model:
        return JSONResponse({"error": "model is required"}, status_code=400)

    caps = lookup_model_capabilities(provider, model)
    return JSONResponse(
        {
            "model": model,
            "provider": provider,
            "known": caps is not None,
            "capabilities": caps or {},
        }
    )


async def admin_known_models(request: Request) -> JSONResponse:
    """GET /v1/api/admin/model-capabilities/known — list known model name prefixes."""
    from turnstone.core.auth import require_permission
    from turnstone.core.providers import list_known_models

    err = require_permission(request, "admin.models")
    if err:
        return err

    provider = request.query_params.get("provider", "").strip()
    if provider not in _MODEL_PROVIDERS:
        return JSONResponse({"error": f"Unknown provider: {provider!r}"}, status_code=400)

    return JSONResponse({"provider": provider, "models": list_known_models(provider)})


# ---------------------------------------------------------------------------
# TLS endpoints
# ---------------------------------------------------------------------------


async def tls_ca_cert(request: Request) -> Response:
    """GET /v1/api/admin/tls/ca.pem — Download CA root certificate."""
    from turnstone.core.auth import require_permission

    err = require_permission(request, "admin.settings")
    if err:
        return err
    mgr = getattr(request.app.state, "tls_manager", None)
    if mgr is None or not mgr.ca_initialized:
        return JSONResponse({"error": "TLS not enabled"}, status_code=404)
    return Response(
        content=mgr.get_root_cert_pem(),
        media_type="application/x-pem-file",
        headers={"Content-Disposition": "attachment; filename=turnstone-ca.pem"},
    )


# ---------------------------------------------------------------------------
# Admin: Prompt Policies (system message composition)
# ---------------------------------------------------------------------------


async def admin_list_prompt_policies(request: Request) -> JSONResponse:
    """GET /v1/api/admin/prompt-policies — list all prompt policies."""
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.prompt_policies")
    if err:
        return err

    policies = storage.list_prompt_policies()
    return JSONResponse({"policies": policies})


async def admin_create_prompt_policy(request: Request) -> JSONResponse:
    """POST /v1/api/admin/prompt-policies — create a prompt policy."""
    import uuid

    from turnstone.core.audit import record_audit
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import read_json_or_400, require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.prompt_policies")
    if err:
        return err

    body = await read_json_or_400(request)
    if isinstance(body, JSONResponse):
        return body

    name = str(body.get("name", "")).strip()[:64]
    content = str(body.get("content", "")).strip()[:32768]
    if not name:
        return JSONResponse({"error": "name is required"}, status_code=400)
    if not content:
        return JSONResponse({"error": "content is required"}, status_code=400)

    try:
        priority = int(body.get("priority", 0))
    except (ValueError, TypeError):
        return JSONResponse({"error": "priority must be an integer"}, status_code=400)

    policy_id = uuid.uuid4().hex
    audit_uid, ip = _audit_context(request)

    storage.upsert_prompt_policy(
        {
            "policy_id": policy_id,
            "name": name,
            "content": content,
            "tool_gate": str(body.get("tool_gate", "")).strip(),
            "priority": priority,
            "enabled": bool(body.get("enabled", True)),
            "org_id": str(body.get("org_id", "")).strip(),
            "created_by": audit_uid,
        }
    )

    record_audit(
        storage,
        audit_uid,
        "prompt_policy.create",
        "prompt_policy",
        policy_id,
        {"name": name},
        ip,
    )

    return JSONResponse(storage.get_prompt_policy(policy_id) or {})


async def admin_get_prompt_policy(request: Request) -> JSONResponse:
    """GET /v1/api/admin/prompt-policies/{policy_id}."""
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.prompt_policies")
    if err:
        return err

    policy_id = request.path_params["policy_id"]
    policy = storage.get_prompt_policy(policy_id)
    if policy is None:
        return JSONResponse({"error": "Prompt policy not found"}, status_code=404)
    return JSONResponse(policy)


async def admin_update_prompt_policy(request: Request) -> JSONResponse:
    """PUT /v1/api/admin/prompt-policies/{policy_id}."""
    from turnstone.core.audit import record_audit
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import read_json_or_400, require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.prompt_policies")
    if err:
        return err

    policy_id = request.path_params["policy_id"]
    existing = storage.get_prompt_policy(policy_id)
    if existing is None:
        return JSONResponse({"error": "Prompt policy not found"}, status_code=404)

    body = await read_json_or_400(request)
    if isinstance(body, JSONResponse):
        return body

    update = dict(body)
    update["policy_id"] = policy_id
    if "name" in update:
        update["name"] = str(update["name"]).strip()[:64]
    if "priority" in update:
        try:
            update["priority"] = int(update["priority"])
        except (ValueError, TypeError):
            return JSONResponse({"error": "priority must be an integer"}, status_code=400)
    if "content" in update:
        update["content"] = str(update["content"]).strip()[:32768]
    if "tool_gate" in update:
        update["tool_gate"] = str(update["tool_gate"] or "").strip()
    if "enabled" in update:
        update["enabled"] = bool(update["enabled"])
    storage.upsert_prompt_policy(update)

    audit_uid, ip = _audit_context(request)
    record_audit(
        storage,
        audit_uid,
        "prompt_policy.update",
        "prompt_policy",
        policy_id,
        {"name": existing.get("name", "")},
        ip,
    )

    return JSONResponse(storage.get_prompt_policy(policy_id) or {})


async def admin_delete_prompt_policy(request: Request) -> JSONResponse:
    """DELETE /v1/api/admin/prompt-policies/{policy_id}."""
    from turnstone.core.audit import record_audit
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.prompt_policies")
    if err:
        return err

    policy_id = request.path_params["policy_id"]
    existing = storage.get_prompt_policy(policy_id)
    if existing is None:
        return JSONResponse({"error": "Prompt policy not found"}, status_code=404)

    storage.delete_prompt_policy(policy_id)

    audit_uid, ip = _audit_context(request)
    record_audit(
        storage,
        audit_uid,
        "prompt_policy.delete",
        "prompt_policy",
        policy_id,
        {"name": existing.get("name", "")},
        ip,
    )

    return JSONResponse({"status": "ok", "policy_id": policy_id})


# ---------------------------------------------------------------------------
# Admin: Judge (heuristic rules, output guard patterns, settings)
# ---------------------------------------------------------------------------

_JUDGE_RULE_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
_VALID_RISK_LEVELS = frozenset({"critical", "high", "medium", "low"})
_VALID_OG_RISK_LEVELS = frozenset({"high", "medium", "low"})  # no "critical" in output guard
_VALID_RECOMMENDATIONS = frozenset({"approve", "review", "deny"})
_VALID_TIERS = frozenset({"critical", "high", "medium", "low"})
_VALID_CATEGORIES = frozenset(
    {
        "prompt_injection",
        "credentials",
        "encoded_payloads",
        "adversarial_urls",
        "info_disclosure",
    }
)
_VALID_PATTERN_FLAGS = frozenset({"IGNORECASE", "MULTILINE", "DOTALL"})
_FLAG_NAME_RE = re.compile(r"^[a-z][a-z_]*$")


def _validate_regex_pattern(pattern: str, flags: int = 0) -> str | None:
    """Validate a regex pattern. Returns error message or None if valid.

    Compiles the pattern with the given flags, then probes against several
    test strings with a timeout to detect catastrophic backtracking.
    """
    try:
        compiled = re.compile(pattern, flags)
    except re.error as exc:
        return f"Invalid regex: {exc}"

    # Probe against several string shapes to detect catastrophic backtracking.
    test_strings = ["a" * 1000, "b" * 30 + "!", "A1b2C3" * 100]

    def _probe() -> None:
        for s in test_strings:
            compiled.search(s)

    try:
        from concurrent.futures import ThreadPoolExecutor
        from concurrent.futures import TimeoutError as FuturesTimeout

        pool = ThreadPoolExecutor(max_workers=1)
        try:
            pool.submit(_probe).result(timeout=0.5)
        except FuturesTimeout:
            return "Regex appears to have catastrophic backtracking"
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
    except Exception:
        return "Regex caused an error during test"
    return None


# -- Judge settings ---------------------------------------------------------


async def admin_list_judge_settings(request: Request) -> JSONResponse:
    """GET /v1/api/admin/judge/settings — list judge settings with schema."""
    from turnstone.core.auth import require_permission
    from turnstone.core.settings_registry import SETTINGS, deserialize_value
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.judge")
    if err:
        return err

    stored = {r["key"]: r for r in storage.list_system_settings() if r.get("node_id", "") == ""}

    result: list[dict[str, Any]] = []
    for key, defn in sorted(SETTINGS.items()):
        if not key.startswith("judge."):
            continue
        row = stored.get(key)
        if row:
            try:
                val = deserialize_value(key, row["value"])
            except (ValueError, KeyError):
                val = row["value"]
            entry = {
                "key": key,
                "type": defn.type,
                "default": defn.default,
                "description": defn.description,
                "help": defn.help,
                "value": "***" if defn.is_secret else val,
                "source": "storage",
                "is_secret": defn.is_secret,
                "min_value": defn.min_value,
                "max_value": defn.max_value,
                "choices": defn.choices,
                "restart_required": defn.restart_required,
            }
        else:
            entry = {
                "key": key,
                "type": defn.type,
                "default": defn.default,
                "description": defn.description,
                "help": defn.help,
                "value": "***" if defn.is_secret else defn.default,
                "source": "default",
                "is_secret": defn.is_secret,
                "min_value": defn.min_value,
                "max_value": defn.max_value,
                "choices": defn.choices,
                "restart_required": defn.restart_required,
            }
        result.append(entry)
    return JSONResponse({"settings": result})


async def admin_update_judge_setting(request: Request) -> JSONResponse:
    """PUT /v1/api/admin/judge/settings/{key} — update a judge setting."""
    from turnstone.core.audit import record_audit
    from turnstone.core.auth import require_permission
    from turnstone.core.settings_registry import SETTINGS
    from turnstone.core.web_helpers import read_json_or_400, require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.judge")
    if err:
        return err

    key = request.path_params["key"]
    if not key.startswith("judge."):
        return JSONResponse({"error": "Only judge.* settings allowed"}, status_code=400)

    defn = SETTINGS.get(key)
    if defn is None:
        return JSONResponse({"error": f"Unknown setting: {key}"}, status_code=404)

    body = await read_json_or_400(request)
    if isinstance(body, JSONResponse):
        return body

    value = body.get("value")
    if value is None:
        return JSONResponse({"error": "value is required"}, status_code=400)

    # Use ConfigStore for validation and persistence
    config_store = getattr(request.app.state, "config_store", None)
    if config_store is None:
        return JSONResponse({"error": "ConfigStore not available"}, status_code=503)

    # Handle secret sentinel
    if defn.is_secret and value == "***":
        return JSONResponse({"status": "ok", "key": key, "value": "***"})

    audit_uid, ip = _audit_context(request)
    try:
        config_store.set(key, value, changed_by=audit_uid)
    except (ValueError, TypeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    record_audit(
        storage,
        audit_uid,
        "setting.update",
        "setting",
        key,
        {"value": "***" if defn.is_secret else value},
        ip,
    )
    await _publish_config_change(request)

    effective = config_store.get(key, defn.default)
    return JSONResponse(
        {"status": "ok", "key": key, "value": "***" if defn.is_secret else effective}
    )


async def admin_delete_judge_setting(request: Request) -> JSONResponse:
    """DELETE /v1/api/admin/judge/settings/{key} — reset a judge setting to default."""
    from turnstone.core.audit import record_audit
    from turnstone.core.auth import require_permission
    from turnstone.core.settings_registry import SETTINGS
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.judge")
    if err:
        return err

    key = request.path_params["key"]
    if not key.startswith("judge."):
        return JSONResponse({"error": "Only judge.* settings allowed"}, status_code=400)

    defn = SETTINGS.get(key)
    if defn is None:
        return JSONResponse({"error": f"Unknown setting: {key}"}, status_code=404)

    config_store = getattr(request.app.state, "config_store", None)
    if config_store:
        config_store.delete(key)

    audit_uid, ip = _audit_context(request)
    record_audit(storage, audit_uid, "setting.delete", "setting", key, {}, ip)
    await _publish_config_change(request)

    return JSONResponse({"status": "ok", "key": key, "default": defn.default})


# -- Heuristic rules -------------------------------------------------------


async def admin_list_heuristic_rules(request: Request) -> JSONResponse:
    """GET /v1/api/admin/judge/heuristic-rules — list merged heuristic rules."""
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.judge")
    if err:
        return err

    # Get DB rules
    db_rules = storage.list_heuristic_rules()

    # Get built-in rules
    from turnstone.core.judge import _HEURISTIC_RULES

    # Build merged list
    result: list[dict[str, Any]] = []

    # Start with DB rules
    seen_names: set[str] = set()
    for row in db_rules:
        name = row["name"]
        seen_names.add(name)
        entry = dict(row)
        if row.get("builtin"):
            entry["source"] = "builtin-overridden" if row.get("enabled") else "builtin-disabled"
        else:
            entry["source"] = "db"
        result.append(entry)

    # Add built-ins not overridden in DB
    import json as _json

    for rule in _HEURISTIC_RULES:
        if rule.name not in seen_names:
            result.append(
                {
                    "rule_id": "",
                    "name": rule.name,
                    "risk_level": rule.risk_level,
                    "confidence": rule.confidence,
                    "recommendation": rule.recommendation,
                    "tool_pattern": rule.tool_pattern,
                    "arg_patterns": _json.dumps(rule.arg_patterns),
                    "intent_template": rule.intent_template,
                    "reasoning_template": rule.reasoning_template,
                    "tier": rule.risk_level,
                    "priority": 0,
                    "builtin": True,
                    "enabled": True,
                    "source": "builtin",
                    "created_by": "",
                    "created": "",
                    "updated": "",
                }
            )

    return JSONResponse({"rules": result})


async def admin_create_heuristic_rule(request: Request) -> JSONResponse:
    """POST /v1/api/admin/judge/heuristic-rules — create a heuristic rule."""
    import json as _json
    import uuid

    from turnstone.core.audit import record_audit
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import read_json_or_400, require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.judge")
    if err:
        return err

    body = await read_json_or_400(request)
    if isinstance(body, JSONResponse):
        return body

    name = str(body.get("name", "")).strip()[:64]
    if not name or not _JUDGE_RULE_NAME_RE.match(name):
        return JSONResponse(
            {"error": "name must match [a-z][a-z0-9_-]* (max 64 chars)"},
            status_code=400,
        )

    # Check uniqueness
    if storage.get_heuristic_rule_by_name(name):
        return JSONResponse({"error": f"Rule with name '{name}' already exists"}, status_code=409)

    risk_level = str(body.get("risk_level", "medium"))
    if risk_level not in _VALID_RISK_LEVELS:
        return JSONResponse(
            {"error": f"risk_level must be one of {sorted(_VALID_RISK_LEVELS)}"}, status_code=400
        )

    recommendation = str(body.get("recommendation", "review"))
    if recommendation not in _VALID_RECOMMENDATIONS:
        return JSONResponse(
            {"error": f"recommendation must be one of {sorted(_VALID_RECOMMENDATIONS)}"},
            status_code=400,
        )

    tier = str(body.get("tier", risk_level))
    if tier not in _VALID_TIERS:
        return JSONResponse(
            {"error": f"tier must be one of {sorted(_VALID_TIERS)}"}, status_code=400
        )

    try:
        confidence = float(body.get("confidence", 0.7))
        if not 0.0 <= confidence <= 1.0:
            raise ValueError
    except (ValueError, TypeError):
        return JSONResponse(
            {"error": "confidence must be a float between 0.0 and 1.0"}, status_code=400
        )

    tool_pattern = str(body.get("tool_pattern", "*"))
    if not tool_pattern:
        return JSONResponse({"error": "tool_pattern is required"}, status_code=400)

    # Validate arg_patterns
    arg_patterns = body.get("arg_patterns", [])
    if isinstance(arg_patterns, str):
        try:
            arg_patterns = _json.loads(arg_patterns)
        except _json.JSONDecodeError:
            return JSONResponse({"error": "arg_patterns must be a JSON array"}, status_code=400)
    if not isinstance(arg_patterns, list):
        return JSONResponse({"error": "arg_patterns must be a list"}, status_code=400)
    for i, pat in enumerate(arg_patterns):
        err_msg = _validate_regex_pattern(str(pat))
        if err_msg:
            return JSONResponse({"error": f"arg_patterns[{i}]: {err_msg}"}, status_code=400)

    try:
        priority = int(body.get("priority", 0))
    except (ValueError, TypeError):
        return JSONResponse({"error": "priority must be an integer"}, status_code=400)

    rule_id = uuid.uuid4().hex
    audit_uid, ip = _audit_context(request)

    storage.create_heuristic_rule(
        rule_id=rule_id,
        name=name,
        risk_level=risk_level,
        confidence=confidence,
        recommendation=recommendation,
        tool_pattern=tool_pattern,
        arg_patterns=_json.dumps(arg_patterns),
        intent_template=str(body.get("intent_template", "")),
        reasoning_template=str(body.get("reasoning_template", "")),
        tier=tier,
        priority=priority,
        builtin=bool(body.get("builtin", False)),
        enabled=bool(body.get("enabled", True)),
        created_by=audit_uid,
    )

    record_audit(
        storage, audit_uid, "heuristic_rule.create", "heuristic_rule", rule_id, {"name": name}, ip
    )

    # Reload rule registry
    rule_registry = getattr(request.app.state, "rule_registry", None)
    if rule_registry:
        rule_registry.reload()
    await _publish_config_change(request)

    return JSONResponse(storage.get_heuristic_rule(rule_id) or {}, status_code=201)


async def admin_get_heuristic_rule(request: Request) -> JSONResponse:
    """GET /v1/api/admin/judge/heuristic-rules/{rule_id}."""
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.judge")
    if err:
        return err

    rule_id = request.path_params["rule_id"]
    rule = storage.get_heuristic_rule(rule_id)
    if rule is None:
        return JSONResponse({"error": "Heuristic rule not found"}, status_code=404)
    return JSONResponse(rule)


async def admin_update_heuristic_rule(request: Request) -> JSONResponse:
    """PUT /v1/api/admin/judge/heuristic-rules/{rule_id}."""
    import json as _json

    from turnstone.core.audit import record_audit
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import read_json_or_400, require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.judge")
    if err:
        return err

    rule_id = request.path_params["rule_id"]
    existing = storage.get_heuristic_rule(rule_id)
    if existing is None:
        return JSONResponse({"error": "Heuristic rule not found"}, status_code=404)

    body = await read_json_or_400(request)
    if isinstance(body, JSONResponse):
        return body

    fields: dict[str, Any] = {}

    if "name" in body:
        name = str(body["name"]).strip()[:64]
        if not _JUDGE_RULE_NAME_RE.match(name):
            return JSONResponse({"error": "name must match [a-z][a-z0-9_-]*"}, status_code=400)
        existing_by_name = storage.get_heuristic_rule_by_name(name)
        if existing_by_name and existing_by_name.get("rule_id") != rule_id:
            return JSONResponse(
                {"error": f"Rule with name '{name}' already exists"}, status_code=409
            )
        fields["name"] = name

    if "risk_level" in body:
        if body["risk_level"] not in _VALID_RISK_LEVELS:
            return JSONResponse(
                {"error": f"risk_level must be one of {sorted(_VALID_RISK_LEVELS)}"},
                status_code=400,
            )
        fields["risk_level"] = body["risk_level"]

    if "recommendation" in body:
        if body["recommendation"] not in _VALID_RECOMMENDATIONS:
            return JSONResponse(
                {"error": f"recommendation must be one of {sorted(_VALID_RECOMMENDATIONS)}"},
                status_code=400,
            )
        fields["recommendation"] = body["recommendation"]

    if "tier" in body:
        if body["tier"] not in _VALID_TIERS:
            return JSONResponse(
                {"error": f"tier must be one of {sorted(_VALID_TIERS)}"}, status_code=400
            )
        fields["tier"] = body["tier"]

    if "confidence" in body:
        try:
            conf = float(body["confidence"])
            if not 0.0 <= conf <= 1.0:
                raise ValueError
            fields["confidence"] = conf
        except (ValueError, TypeError):
            return JSONResponse({"error": "confidence must be 0.0-1.0"}, status_code=400)

    if "tool_pattern" in body:
        fields["tool_pattern"] = str(body["tool_pattern"])

    if "arg_patterns" in body:
        ap = body["arg_patterns"]
        if isinstance(ap, str):
            try:
                ap = _json.loads(ap)
            except _json.JSONDecodeError:
                return JSONResponse({"error": "arg_patterns must be a JSON array"}, status_code=400)
        if not isinstance(ap, list):
            return JSONResponse({"error": "arg_patterns must be a list"}, status_code=400)
        for i, pat in enumerate(ap):
            err_msg = _validate_regex_pattern(str(pat))
            if err_msg:
                return JSONResponse({"error": f"arg_patterns[{i}]: {err_msg}"}, status_code=400)
        fields["arg_patterns"] = _json.dumps(ap)

    if "intent_template" in body:
        fields["intent_template"] = str(body["intent_template"])
    if "reasoning_template" in body:
        fields["reasoning_template"] = str(body["reasoning_template"])
    if "priority" in body:
        try:
            fields["priority"] = int(body["priority"])
        except (ValueError, TypeError):
            return JSONResponse({"error": "priority must be an integer"}, status_code=400)
    if "builtin" in body:
        fields["builtin"] = bool(body["builtin"])
    if "enabled" in body:
        fields["enabled"] = bool(body["enabled"])

    if fields:
        storage.update_heuristic_rule(rule_id, **fields)

    audit_uid, ip = _audit_context(request)
    record_audit(
        storage,
        audit_uid,
        "heuristic_rule.update",
        "heuristic_rule",
        rule_id,
        {"name": existing.get("name", "")},
        ip,
    )

    rule_registry = getattr(request.app.state, "rule_registry", None)
    if rule_registry:
        rule_registry.reload()
    await _publish_config_change(request)

    return JSONResponse(storage.get_heuristic_rule(rule_id) or {})


async def admin_delete_heuristic_rule(request: Request) -> JSONResponse:
    """DELETE /v1/api/admin/judge/heuristic-rules/{rule_id}."""
    from turnstone.core.audit import record_audit
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.judge")
    if err:
        return err

    rule_id = request.path_params["rule_id"]
    existing = storage.get_heuristic_rule(rule_id)
    if existing is None:
        return JSONResponse({"error": "Heuristic rule not found"}, status_code=404)

    storage.delete_heuristic_rule(rule_id)

    audit_uid, ip = _audit_context(request)
    record_audit(
        storage,
        audit_uid,
        "heuristic_rule.delete",
        "heuristic_rule",
        rule_id,
        {"name": existing.get("name", "")},
        ip,
    )

    rule_registry = getattr(request.app.state, "rule_registry", None)
    if rule_registry:
        rule_registry.reload()
    await _publish_config_change(request)

    return JSONResponse({"status": "ok", "rule_id": rule_id})


# -- Output guard patterns --------------------------------------------------


async def admin_list_output_guard_patterns(request: Request) -> JSONResponse:
    """GET /v1/api/admin/judge/output-guard-patterns — list merged output guard patterns."""
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.judge")
    if err:
        return err

    # Get DB patterns
    db_patterns = storage.list_output_guard_patterns()

    # Get built-in patterns
    from turnstone.core.output_guard import _BUILTIN_OG_PATTERNS

    # Build merged list
    result: list[dict[str, Any]] = []
    seen_names: set[str] = set()

    for row in db_patterns:
        name = row["name"]
        seen_names.add(name)
        entry = dict(row)
        if row.get("builtin"):
            entry["source"] = "builtin-overridden" if row.get("enabled") else "builtin-disabled"
        else:
            entry["source"] = "db"
        result.append(entry)

    # Add built-ins not overridden in DB
    import re as _re

    _flags_reverse = {
        _re.IGNORECASE: "IGNORECASE",
        _re.MULTILINE: "MULTILINE",
        _re.DOTALL: "DOTALL",
    }
    for pat in _BUILTIN_OG_PATTERNS:
        if pat.name not in seen_names:
            # Derive pattern_flags from compiled regex so overrides preserve them
            pf = ",".join(n for f, n in _flags_reverse.items() if pat.compiled.flags & f)
            result.append(
                {
                    "pattern_id": "",
                    "name": pat.name,
                    "category": pat.category,
                    "risk_level": pat.risk_level,
                    "pattern": pat.compiled.pattern,
                    "pattern_flags": pf,
                    "flag_name": pat.flag_name,
                    "annotation": pat.annotation,
                    "is_credential": pat.is_credential,
                    "redact_label": pat.redact_label,
                    "priority": pat.priority,
                    "builtin": True,
                    "enabled": True,
                    "source": "builtin",
                    "created_by": "",
                    "created": "",
                    "updated": "",
                }
            )

    return JSONResponse({"patterns": result})


async def admin_create_output_guard_pattern(request: Request) -> JSONResponse:
    """POST /v1/api/admin/judge/output-guard-patterns — create an output guard pattern."""
    import uuid

    from turnstone.core.audit import record_audit
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import read_json_or_400, require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.judge")
    if err:
        return err

    body = await read_json_or_400(request)
    if isinstance(body, JSONResponse):
        return body

    name = str(body.get("name", "")).strip()[:64]
    if not name or not _JUDGE_RULE_NAME_RE.match(name):
        return JSONResponse(
            {"error": "name must match [a-z][a-z0-9_-]* (max 64 chars)"},
            status_code=400,
        )

    # Check uniqueness
    if storage.get_output_guard_pattern_by_name(name):
        return JSONResponse(
            {"error": f"Pattern with name '{name}' already exists"}, status_code=409
        )

    # Validate pattern_flags first (needed for pattern validation)
    pattern_flags_raw = body.get("pattern_flags", "")
    if isinstance(pattern_flags_raw, list):
        pattern_flags_list = pattern_flags_raw
    elif isinstance(pattern_flags_raw, str) and pattern_flags_raw:
        pattern_flags_list = [f.strip() for f in pattern_flags_raw.split(",") if f.strip()]
    else:
        pattern_flags_list = []
    re_flags = 0
    for flag in pattern_flags_list:
        if flag not in _VALID_PATTERN_FLAGS:
            return JSONResponse(
                {
                    "error": f"Invalid pattern_flag '{flag}'; must be one of {sorted(_VALID_PATTERN_FLAGS)}"
                },
                status_code=400,
            )
        re_flags |= {"IGNORECASE": re.IGNORECASE, "MULTILINE": re.MULTILINE, "DOTALL": re.DOTALL}[
            flag
        ]
    pattern_flags = ",".join(pattern_flags_list)

    pattern = str(body.get("pattern", ""))
    if not pattern:
        return JSONResponse({"error": "pattern is required"}, status_code=400)
    err_msg = _validate_regex_pattern(pattern, re_flags)
    if err_msg:
        return JSONResponse({"error": err_msg}, status_code=400)

    category = str(body.get("category", ""))
    if category not in _VALID_CATEGORIES:
        return JSONResponse(
            {"error": f"category must be one of {sorted(_VALID_CATEGORIES)}"}, status_code=400
        )

    risk_level = str(body.get("risk_level", "medium"))
    if risk_level not in _VALID_OG_RISK_LEVELS:
        return JSONResponse(
            {"error": f"risk_level must be one of {sorted(_VALID_OG_RISK_LEVELS)}"},
            status_code=400,
        )

    flag_name = str(body.get("flag_name", ""))
    if not flag_name or not _FLAG_NAME_RE.match(flag_name):
        return JSONResponse({"error": "flag_name must match [a-z][a-z_]*"}, status_code=400)

    annotation = str(body.get("annotation", ""))

    is_credential = bool(body.get("is_credential", False))
    redact_label = str(body.get("redact_label", ""))
    if is_credential and not redact_label:
        return JSONResponse(
            {"error": "redact_label is required when is_credential is true"}, status_code=400
        )

    try:
        priority = int(body.get("priority", 0))
    except (ValueError, TypeError):
        return JSONResponse({"error": "priority must be an integer"}, status_code=400)

    pattern_id = uuid.uuid4().hex
    audit_uid, ip = _audit_context(request)

    storage.create_output_guard_pattern(
        pattern_id=pattern_id,
        name=name,
        category=category,
        risk_level=risk_level,
        pattern=pattern,
        flag_name=flag_name,
        annotation=annotation,
        pattern_flags=pattern_flags,
        is_credential=is_credential,
        redact_label=redact_label,
        priority=priority,
        builtin=bool(body.get("builtin", False)),
        enabled=bool(body.get("enabled", True)),
        created_by=audit_uid,
    )

    record_audit(
        storage,
        audit_uid,
        "output_guard_pattern.create",
        "output_guard_pattern",
        pattern_id,
        {"name": name},
        ip,
    )

    # Reload rule registry
    rule_registry = getattr(request.app.state, "rule_registry", None)
    if rule_registry:
        rule_registry.reload()
    await _publish_config_change(request)

    return JSONResponse(storage.get_output_guard_pattern(pattern_id) or {}, status_code=201)


async def admin_get_output_guard_pattern(request: Request) -> JSONResponse:
    """GET /v1/api/admin/judge/output-guard-patterns/{pattern_id}."""
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.judge")
    if err:
        return err

    pattern_id = request.path_params["pattern_id"]
    pattern = storage.get_output_guard_pattern(pattern_id)
    if pattern is None:
        return JSONResponse({"error": "Output guard pattern not found"}, status_code=404)
    return JSONResponse(pattern)


async def admin_update_output_guard_pattern(request: Request) -> JSONResponse:
    """PUT /v1/api/admin/judge/output-guard-patterns/{pattern_id}."""
    from turnstone.core.audit import record_audit
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import read_json_or_400, require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.judge")
    if err:
        return err

    pattern_id = request.path_params["pattern_id"]
    existing = storage.get_output_guard_pattern(pattern_id)
    if existing is None:
        return JSONResponse({"error": "Output guard pattern not found"}, status_code=404)

    body = await read_json_or_400(request)
    if isinstance(body, JSONResponse):
        return body

    fields: dict[str, Any] = {}

    if "name" in body:
        name = str(body["name"]).strip()[:64]
        if not _JUDGE_RULE_NAME_RE.match(name):
            return JSONResponse({"error": "name must match [a-z][a-z0-9_-]*"}, status_code=400)
        existing_by_name = storage.get_output_guard_pattern_by_name(name)
        if existing_by_name and existing_by_name.get("pattern_id") != pattern_id:
            return JSONResponse(
                {"error": f"Pattern with name '{name}' already exists"}, status_code=409
            )
        fields["name"] = name

    # Resolve pattern_flags first (needed for pattern validation)
    re_flags = 0
    if "pattern_flags" in body:
        pf_raw = body["pattern_flags"]
        if isinstance(pf_raw, list):
            pf_list = pf_raw
        elif isinstance(pf_raw, str) and pf_raw:
            pf_list = [f.strip() for f in pf_raw.split(",") if f.strip()]
        else:
            pf_list = []
        for flag in pf_list:
            if flag not in _VALID_PATTERN_FLAGS:
                return JSONResponse(
                    {
                        "error": f"Invalid pattern_flag '{flag}'; must be one of {sorted(_VALID_PATTERN_FLAGS)}"
                    },
                    status_code=400,
                )
        for flag in pf_list:
            re_flags |= {
                "IGNORECASE": re.IGNORECASE,
                "MULTILINE": re.MULTILINE,
                "DOTALL": re.DOTALL,
            }[flag]
        fields["pattern_flags"] = ",".join(pf_list)

    if "pattern" in body:
        pattern = str(body["pattern"])
        err_msg = _validate_regex_pattern(pattern, re_flags)
        if err_msg:
            return JSONResponse({"error": err_msg}, status_code=400)
        fields["pattern"] = pattern

    if "category" in body:
        if body["category"] not in _VALID_CATEGORIES:
            return JSONResponse(
                {"error": f"category must be one of {sorted(_VALID_CATEGORIES)}"}, status_code=400
            )
        fields["category"] = body["category"]

    if "risk_level" in body:
        if body["risk_level"] not in _VALID_OG_RISK_LEVELS:
            return JSONResponse(
                {"error": f"risk_level must be one of {sorted(_VALID_OG_RISK_LEVELS)}"},
                status_code=400,
            )
        fields["risk_level"] = body["risk_level"]

    if "flag_name" in body:
        fn = str(body["flag_name"])
        if not _FLAG_NAME_RE.match(fn):
            return JSONResponse({"error": "flag_name must match [a-z][a-z_]*"}, status_code=400)
        fields["flag_name"] = fn

    if "annotation" in body:
        fields["annotation"] = str(body["annotation"])

    if "is_credential" in body:
        fields["is_credential"] = bool(body["is_credential"])
    if "redact_label" in body:
        fields["redact_label"] = str(body["redact_label"])

    # Cross-field validation: is_credential requires redact_label
    final_is_cred = fields.get("is_credential", existing.get("is_credential", False))
    final_redact = fields.get("redact_label", existing.get("redact_label", ""))
    if final_is_cred and not final_redact:
        return JSONResponse(
            {"error": "redact_label is required when is_credential is true"}, status_code=400
        )

    if "priority" in body:
        try:
            fields["priority"] = int(body["priority"])
        except (ValueError, TypeError):
            return JSONResponse({"error": "priority must be an integer"}, status_code=400)
    if "builtin" in body:
        fields["builtin"] = bool(body["builtin"])
    if "enabled" in body:
        fields["enabled"] = bool(body["enabled"])

    if fields:
        storage.update_output_guard_pattern(pattern_id, **fields)

    audit_uid, ip = _audit_context(request)
    record_audit(
        storage,
        audit_uid,
        "output_guard_pattern.update",
        "output_guard_pattern",
        pattern_id,
        {"name": existing.get("name", "")},
        ip,
    )

    rule_registry = getattr(request.app.state, "rule_registry", None)
    if rule_registry:
        rule_registry.reload()
    await _publish_config_change(request)

    return JSONResponse(storage.get_output_guard_pattern(pattern_id) or {})


async def admin_delete_output_guard_pattern(request: Request) -> JSONResponse:
    """DELETE /v1/api/admin/judge/output-guard-patterns/{pattern_id}."""
    from turnstone.core.audit import record_audit
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    storage, err = require_storage_or_503(request)
    if err:
        return err
    err = require_permission(request, "admin.judge")
    if err:
        return err

    pattern_id = request.path_params["pattern_id"]
    existing = storage.get_output_guard_pattern(pattern_id)
    if existing is None:
        return JSONResponse({"error": "Output guard pattern not found"}, status_code=404)

    storage.delete_output_guard_pattern(pattern_id)

    audit_uid, ip = _audit_context(request)
    record_audit(
        storage,
        audit_uid,
        "output_guard_pattern.delete",
        "output_guard_pattern",
        pattern_id,
        {"name": existing.get("name", "")},
        ip,
    )

    rule_registry = getattr(request.app.state, "rule_registry", None)
    if rule_registry:
        rule_registry.reload()
    await _publish_config_change(request)

    return JSONResponse({"status": "ok", "pattern_id": pattern_id})


# -- Judge utility endpoints ------------------------------------------------


async def admin_judge_reload(request: Request) -> JSONResponse:
    """POST /v1/api/admin/judge/reload — reload rule registry on all nodes."""
    from turnstone.core.auth import require_permission

    err = require_permission(request, "admin.judge")
    if err:
        return err

    rule_registry = getattr(request.app.state, "rule_registry", None)
    if rule_registry:
        rule_registry.reload()

    await _publish_config_change(request)
    return JSONResponse({"status": "ok"})


async def admin_validate_regex(request: Request) -> JSONResponse:
    """POST /v1/api/admin/judge/validate-regex — test-compile a regex."""
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import read_json_or_400

    err = require_permission(request, "admin.judge")
    if err:
        return err

    body = await read_json_or_400(request)
    if isinstance(body, JSONResponse):
        return body

    pattern = str(body.get("pattern", ""))
    if not pattern:
        return JSONResponse({"error": "pattern is required"}, status_code=400)

    err_msg = _validate_regex_pattern(pattern)
    if err_msg:
        return JSONResponse({"valid": False, "error": err_msg})
    return JSONResponse({"valid": True})


def _validate_node_id(node_id: str) -> JSONResponse | None:
    """Return an error response if node_id is invalid, else None."""
    if not node_id or len(node_id) > 256 or not _VALID_NODE_ID.match(node_id):
        return JSONResponse({"error": "Invalid node ID"}, status_code=400)
    return None


async def admin_get_all_node_metadata(request: Request) -> JSONResponse:
    """GET /v1/api/admin/node-metadata — metadata for all nodes."""
    import json as _anm_json

    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    err = require_permission(request, "admin.nodes")
    if err:
        return err
    storage, serr = require_storage_or_503(request)
    if serr:
        return serr
    all_meta = storage.get_all_node_metadata()
    result: dict[str, list[dict[str, Any]]] = {}
    for nid, rows in all_meta.items():
        entries = []
        for r in rows:
            try:
                val = _anm_json.loads(r["value"])
            except (ValueError, TypeError):
                val = r["value"]
            entries.append({"key": r["key"], "value": val, "source": r["source"]})
        result[nid] = entries
    return JSONResponse({"nodes": result})


async def admin_get_node_metadata(request: Request) -> JSONResponse:
    """GET /v1/api/admin/nodes/{node_id}/metadata — all metadata for a node."""
    import json as _nm_json

    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    err = require_permission(request, "admin.nodes")
    if err:
        return err
    node_id = request.path_params["node_id"]
    nv = _validate_node_id(node_id)
    if nv:
        return nv
    storage, serr = require_storage_or_503(request)
    if serr:
        return serr
    rows = storage.get_node_metadata(node_id)
    metadata = []
    for r in rows:
        try:
            val = _nm_json.loads(r["value"])
        except (ValueError, TypeError):
            val = r["value"]
        metadata.append({"key": r["key"], "value": val, "source": r["source"]})
    return JSONResponse({"node_id": node_id, "metadata": metadata})


async def admin_set_node_metadata(request: Request) -> JSONResponse:
    """PUT /v1/api/admin/nodes/{node_id}/metadata — bulk set user metadata."""
    import json as _nm_json

    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import read_json_or_400, require_storage_or_503

    err = require_permission(request, "admin.nodes")
    if err:
        return err
    node_id = request.path_params["node_id"]
    nv = _validate_node_id(node_id)
    if nv:
        return nv
    body = await read_json_or_400(request)
    if isinstance(body, JSONResponse):
        return body
    entries = body.get("entries", [])
    if not entries:
        return JSONResponse({"error": "No entries provided"}, status_code=400)

    storage, serr = require_storage_or_503(request)
    if serr:
        return serr

    # Validate entries
    existing = {r["key"]: r["source"] for r in storage.get_node_metadata(node_id)}
    for e in entries:
        key = e.get("key", "")
        if not key:
            return JSONResponse({"error": "Empty key"}, status_code=400)
        if len(key) > 128:
            return JSONResponse(
                {"error": f"Key too long (max 128): {key[:32]}..."}, status_code=400
            )
        if "value" not in e:
            return JSONResponse({"error": f"Missing value for key: {key}"}, status_code=400)
        if existing.get(key) == "auto":
            return JSONResponse(
                {"error": f"Cannot overwrite auto-populated key: {key}"},
                status_code=400,
            )

    bulk = [(e["key"], _nm_json.dumps(e["value"]), "user") for e in entries]
    storage.set_node_metadata_bulk(node_id, bulk)
    return JSONResponse({"ok": True, "count": len(bulk)})


async def admin_set_node_metadata_key(request: Request) -> JSONResponse:
    """PUT /v1/api/admin/nodes/{node_id}/metadata/{key} — set single key."""
    import json as _nm_json

    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import read_json_or_400, require_storage_or_503

    err = require_permission(request, "admin.nodes")
    if err:
        return err
    node_id = request.path_params["node_id"]
    nv = _validate_node_id(node_id)
    if nv:
        return nv
    key = request.path_params["key"]
    if not key:
        return JSONResponse({"error": "Empty key"}, status_code=400)
    if len(key) > 128:
        return JSONResponse({"error": "Key too long (max 128)"}, status_code=400)

    storage, serr = require_storage_or_503(request)
    if serr:
        return serr
    existing = storage.get_node_metadata(node_id)
    for r in existing:
        if r["key"] == key and r["source"] == "auto":
            return JSONResponse(
                {"error": f"Cannot overwrite auto-populated key: {key}"},
                status_code=400,
            )

    body = await read_json_or_400(request)
    if isinstance(body, JSONResponse):
        return body
    if "value" not in body:
        return JSONResponse({"error": "Missing value"}, status_code=400)
    storage.set_node_metadata(node_id, key, _nm_json.dumps(body["value"]), source="user")
    return JSONResponse({"ok": True})


async def admin_delete_node_metadata_key(request: Request) -> JSONResponse:
    """DELETE /v1/api/admin/nodes/{node_id}/metadata/{key} — delete single key."""
    from turnstone.core.auth import require_permission
    from turnstone.core.web_helpers import require_storage_or_503

    err = require_permission(request, "admin.nodes")
    if err:
        return err
    node_id = request.path_params["node_id"]
    nv = _validate_node_id(node_id)
    if nv:
        return nv
    key = request.path_params["key"]
    if not key:
        return JSONResponse({"error": "Empty key"}, status_code=400)

    storage, serr = require_storage_or_503(request)
    if serr:
        return serr
    existing = storage.get_node_metadata(node_id)
    for r in existing:
        if r["key"] == key and r["source"] == "auto":
            return JSONResponse(
                {"error": f"Cannot delete auto-populated key: {key}"},
                status_code=400,
            )

    deleted = storage.delete_node_metadata(node_id, key)
    if not deleted:
        return JSONResponse({"error": "Key not found"}, status_code=404)
    return JSONResponse({"ok": True})


async def tls_ca_status(request: Request) -> JSONResponse:
    """GET /v1/api/admin/tls/ca — CA status."""
    from turnstone.core.auth import require_permission

    err = require_permission(request, "admin.settings")
    if err:
        return err
    mgr = getattr(request.app.state, "tls_manager", None)
    if mgr is None or not mgr.ca_initialized:
        return JSONResponse({"enabled": False})
    from turnstone.console.tls import _CA_CN

    certs = mgr.list_certs()
    return JSONResponse(
        {
            "enabled": True,
            "ca_cn": _CA_CN,
            "cert_count": len(certs),
            "certs": [
                {
                    "domain": c.domain,
                    "issued_at": c.issued_at.isoformat(),
                    "expires_at": c.expires_at.isoformat(),
                }
                for c in certs
            ],
        },
    )


async def tls_list_certs(request: Request) -> JSONResponse:
    """GET /v1/api/admin/tls/certs — List issued certificates."""
    from turnstone.core.auth import require_permission

    err = require_permission(request, "admin.settings")
    if err:
        return err
    mgr = getattr(request.app.state, "tls_manager", None)
    if mgr is None or not mgr.ca_initialized:
        return JSONResponse({"certs": []})
    certs = mgr.list_certs()
    return JSONResponse(
        {
            "certs": [
                {
                    "domain": c.domain,
                    "domains": list(c.domains),
                    "issued_at": c.issued_at.isoformat(),
                    "expires_at": c.expires_at.isoformat(),
                }
                for c in certs
            ],
        },
    )


async def tls_renew_cert(request: Request) -> JSONResponse:
    """POST /v1/api/admin/tls/certs/{domain}/renew — Force cert renewal."""
    from turnstone.core.auth import require_permission

    err = require_permission(request, "admin.settings")
    if err:
        return err
    mgr = getattr(request.app.state, "tls_manager", None)
    if mgr is None or not mgr.ca_initialized:
        return JSONResponse({"error": "TLS not enabled"}, status_code=404)
    domain = request.path_params["domain"]
    try:
        bundle = mgr.renew_cert(domain)
        return JSONResponse(
            {
                "domain": bundle.domain,
                "issued_at": bundle.issued_at.isoformat(),
                "expires_at": bundle.expires_at.isoformat(),
            },
        )
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def tls_delete_cert(request: Request) -> JSONResponse:
    """DELETE /v1/api/admin/tls/certs/{domain} — Delete a certificate."""
    from turnstone.core.auth import require_permission

    err = require_permission(request, "admin.settings")
    if err:
        return err
    mgr = getattr(request.app.state, "tls_manager", None)
    if mgr is None or not mgr.ca_initialized:
        return JSONResponse({"error": "TLS not enabled"}, status_code=404)
    domain = request.path_params["domain"]
    if not mgr.delete_cert(domain):
        return JSONResponse({"error": f"No cert for {domain}"}, status_code=404)
    return JSONResponse({"deleted": domain})


# ---------------------------------------------------------------------------
# ConfigStore env seeding
# ---------------------------------------------------------------------------


def _seed_config_from_env(config_store: Any, storage: Any) -> None:
    """Seed ConfigStore settings from environment variables.

    Checks for ``TURNSTONE_{SECTION}_{KEY}`` env vars and writes them
    to ConfigStore if they aren't already set. This allows container
    deployments to configure settings before the admin UI is available.

    Only seeds known settings from the registry to avoid storing garbage.
    Uses config_store.set() for proper validation, serialization, and
    cache invalidation.
    """
    from turnstone.core.settings_registry import SETTINGS

    for key in SETTINGS:
        env_name = "TURNSTONE_" + key.replace(".", "_").upper()
        env_val = os.environ.get(env_name)
        if env_val is None:
            continue
        # Only seed if not already stored (check raw storage to avoid
        # config_store cache, which may not reflect DB state yet)
        existing = storage.get_system_setting(key)
        if existing is not None:
            continue
        try:
            config_store.set(key, env_val, changed_by="env")
            log.info("config.seeded_from_env: %s from %s", key, env_name)
        except Exception:
            log.warning("config.seed_failed: %s from %s", key, env_name, exc_info=True)


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(
    *,
    collector: ClusterCollector,
    jwt_secret: str = "",
    auth_storage: Any = None,
    proxy_token_mgr: Any = None,
    cors_origins: list[str] | None = None,
    tls_manager: Any = None,
    console_url: str = "",
    router: ConsoleRouter | None = None,
    console_metrics: ConsoleMetrics | None = None,
) -> Starlette:
    """Build the Starlette ASGI application for the console dashboard."""
    _spec = build_console_spec()
    _openapi_handler = make_openapi_handler(_spec)
    _docs_handler = make_docs_handler()

    # Coord workstream HTTP tree mounts under the unified
    # ``/api/workstreams/`` shape. Handlers look the coord manager up
    # via ``request.app.state.coord_mgr`` because the manager is built
    # in the lifespan, after app construction.
    coord_workstream_routes: list[Any] = []
    register_session_routes(
        coord_workstream_routes,
        prefix="/api/workstreams",
        handlers=SharedSessionVerbHandlers(
            list_workstreams=coordinator_list,
            list_saved=coordinator_saved,
            create=coordinator_create,
            detail=coordinator_detail,
            open=coordinator_open,
            close=coordinator_close,
            send=coordinator_send,
            approve=coordinator_approve,
            cancel=coordinator_cancel,
            events=coordinator_events,
            history=coordinator_history,
        ),
    )
    register_coord_verbs(
        coord_workstream_routes,
        prefix="/api/workstreams",
        handlers=CoordOnlyVerbHandlers(
            children=coordinator_children,
            tasks=coordinator_tasks,
            metrics=coordinator_metrics,
            trust=coordinator_trust,
            restrict=coordinator_restrict,
            stop_cascade=coordinator_stop_cascade,
            close_all_children=coordinator_close_all_children,
        ),
    )

    app = Starlette(
        routes=[
            Route("/", index),
            Mount(
                "/v1",
                routes=[
                    *coord_workstream_routes,
                    Route("/api/cluster/overview", cluster_overview),
                    Route("/api/cluster/nodes", cluster_nodes),
                    Route("/api/cluster/workstreams", cluster_workstreams),
                    Route("/api/cluster/workstreams/new", create_workstream, methods=["POST"]),
                    Route("/api/cluster/ws/live", cluster_ws_live_bulk),
                    Route("/api/cluster/ws/{ws_id}/detail", cluster_ws_detail),
                    Route("/api/cluster/node/{node_id}", cluster_node_detail),
                    Route("/api/cluster/snapshot", cluster_snapshot),
                    Route("/api/cluster/events", cluster_events_sse),
                    # Workstream routing (rendezvous proxy to server nodes)
                    Route("/api/route/workstreams/new", route_create, methods=["POST"]),
                    Route("/api/route/send", route_proxy, methods=["POST"]),
                    Route("/api/route/approve", route_proxy, methods=["POST"]),
                    Route("/api/route/cancel", route_proxy, methods=["POST"]),
                    Route("/api/route/command", route_proxy, methods=["POST"]),
                    Route("/api/route/plan", route_proxy, methods=["POST"]),
                    Route("/api/route/workstreams/close", route_proxy, methods=["POST"]),
                    # Coordinator-only hard delete — forwards to the server's
                    # path-parameter form at /v1/api/workstreams/{ws_id}/delete.
                    Route(
                        "/api/route/workstreams/delete",
                        route_workstream_delete,
                        methods=["POST"],
                    ),
                    Route(
                        "/api/route/workstreams/{ws_id}/attachments",
                        route_attachment_proxy,
                        methods=["POST", "GET"],
                    ),
                    Route(
                        "/api/route/workstreams/{ws_id}/attachments/{attachment_id}",
                        route_attachment_proxy,
                        methods=["DELETE"],
                    ),
                    Route(
                        "/api/route/workstreams/{ws_id}/attachments/{attachment_id}/content",
                        route_attachment_proxy,
                        methods=["GET"],
                    ),
                    Route("/api/route", route_lookup, methods=["GET"]),
                    Route("/api/models", list_available_models),
                    Route("/api/skills", list_skills_summary),
                    Route("/api/auth/login", auth_login, methods=["POST"]),
                    Route("/api/auth/logout", auth_logout, methods=["POST"]),
                    Route("/api/auth/status", auth_status),
                    Route("/api/auth/setup", auth_setup, methods=["POST"]),
                    Route("/api/auth/whoami", auth_whoami),
                    Route("/api/auth/refresh", auth_refresh, methods=["POST"]),
                    Route("/api/auth/oidc/authorize", oidc_authorize),
                    Route("/api/auth/oidc/callback", oidc_callback),
                    Route("/api/admin/users", admin_list_users),
                    Route("/api/admin/users", admin_create_user, methods=["POST"]),
                    Route("/api/admin/users/{user_id}", admin_delete_user, methods=["DELETE"]),
                    Route("/api/admin/users/{user_id}/tokens", admin_list_tokens),
                    Route(
                        "/api/admin/users/{user_id}/tokens", admin_create_token, methods=["POST"]
                    ),
                    Route("/api/admin/tokens/{token_id}", admin_revoke_token, methods=["DELETE"]),
                    Route(
                        "/api/admin/users/{user_id}/channels",
                        admin_list_channels,
                    ),
                    Route(
                        "/api/admin/users/{user_id}/channels",
                        admin_create_channel,
                        methods=["POST"],
                    ),
                    Route(
                        "/api/admin/channels/{channel_type}/{channel_user_id}",
                        admin_delete_channel,
                        methods=["DELETE"],
                    ),
                    Route(
                        "/api/admin/users/{user_id}/oidc-identities",
                        admin_list_oidc_identities,
                    ),
                    Route(
                        "/api/admin/oidc-identities",
                        admin_delete_oidc_identity,
                        methods=["DELETE"],
                    ),
                    Route("/api/admin/schedules", admin_list_schedules),
                    Route("/api/admin/schedules", admin_create_schedule, methods=["POST"]),
                    Route("/api/admin/schedules/{task_id}", admin_get_schedule),
                    Route("/api/admin/schedules/{task_id}", admin_update_schedule, methods=["PUT"]),
                    Route(
                        "/api/admin/schedules/{task_id}",
                        admin_delete_schedule,
                        methods=["DELETE"],
                    ),
                    Route("/api/admin/schedules/{task_id}/runs", admin_list_schedule_runs),
                    Route("/api/admin/watches", admin_list_watches),
                    Route(
                        "/api/admin/watches/{watch_id}/cancel",
                        admin_cancel_watch,
                        methods=["POST"],
                    ),
                    # Governance: Roles
                    Route("/api/admin/roles", admin_list_roles),
                    Route("/api/admin/roles", admin_create_role, methods=["POST"]),
                    Route("/api/admin/roles/{role_id}", admin_update_role, methods=["PUT"]),
                    Route("/api/admin/roles/{role_id}", admin_delete_role, methods=["DELETE"]),
                    Route("/api/admin/users/{user_id}/roles", admin_list_user_roles),
                    Route(
                        "/api/admin/users/{user_id}/roles",
                        admin_assign_role,
                        methods=["POST"],
                    ),
                    Route(
                        "/api/admin/users/{user_id}/roles/{role_id}",
                        admin_unassign_role,
                        methods=["DELETE"],
                    ),
                    # Governance: Orgs
                    Route("/api/admin/orgs", admin_list_orgs),
                    Route("/api/admin/orgs/{org_id}", admin_get_org),
                    Route("/api/admin/orgs/{org_id}", admin_update_org, methods=["PUT"]),
                    # Governance: Tool policies
                    Route("/api/admin/policies", admin_list_policies),
                    Route("/api/admin/policies", admin_create_policy, methods=["POST"]),
                    Route(
                        "/api/admin/policies/{policy_id}",
                        admin_update_policy,
                        methods=["PUT"],
                    ),
                    Route(
                        "/api/admin/policies/{policy_id}",
                        admin_delete_policy,
                        methods=["DELETE"],
                    ),
                    # Governance: Skill Discovery
                    Route("/api/admin/skills/discover", admin_skill_discover),
                    Route(
                        "/api/admin/skills/install",
                        admin_skill_install,
                        methods=["POST"],
                    ),
                    # Governance: Skills
                    Route("/api/admin/skills", admin_list_skills),
                    Route("/api/admin/skills", admin_create_skill, methods=["POST"]),
                    Route("/api/admin/skills/{skill_id}", admin_get_skill),
                    Route(
                        "/api/admin/skills/{skill_id}",
                        admin_update_skill,
                        methods=["PUT"],
                    ),
                    Route(
                        "/api/admin/skills/{skill_id}",
                        admin_delete_skill,
                        methods=["DELETE"],
                    ),
                    Route(
                        "/api/admin/skills/{skill_id}/versions",
                        admin_list_skill_versions,
                    ),
                    # Governance: Skill Resources
                    Route(
                        "/api/admin/skills/{skill_id}/resources",
                        admin_list_skill_resources,
                    ),
                    Route(
                        "/api/admin/skills/{skill_id}/resources",
                        admin_create_skill_resource,
                        methods=["POST"],
                    ),
                    Route(
                        "/api/admin/skills/{skill_id}/resources/{path:path}",
                        admin_get_skill_resource,
                    ),
                    Route(
                        "/api/admin/skills/{skill_id}/resources/{path:path}",
                        admin_delete_skill_resource,
                        methods=["DELETE"],
                    ),
                    # Governance: Memories
                    Route("/api/admin/memories", admin_list_memories),
                    Route("/api/admin/memories/search", admin_search_memories),
                    Route("/api/admin/memories/{memory_id}", admin_get_memory),
                    Route(
                        "/api/admin/memories/{memory_id}",
                        admin_delete_memory,
                        methods=["DELETE"],
                    ),
                    # System: Settings
                    Route("/api/admin/settings", admin_list_settings),
                    Route("/api/admin/settings/schema", admin_settings_schema),
                    Route(
                        "/api/admin/settings/{key:path}",
                        admin_update_setting,
                        methods=["PUT"],
                    ),
                    Route(
                        "/api/admin/settings/{key:path}",
                        admin_delete_setting,
                        methods=["DELETE"],
                    ),
                    # System: MCP Registry
                    Route("/api/admin/mcp-registry/search", admin_registry_search),
                    Route(
                        "/api/admin/mcp-registry/install",
                        admin_registry_install,
                        methods=["POST"],
                    ),
                    # System: MCP Servers
                    Route("/api/admin/mcp-servers", admin_list_mcp_servers),
                    Route(
                        "/api/admin/mcp-servers",
                        admin_create_mcp_server,
                        methods=["POST"],
                    ),
                    Route(
                        "/api/admin/mcp-servers/import",
                        admin_import_mcp_config,
                        methods=["POST"],
                    ),
                    Route(
                        "/api/admin/mcp-servers/reload",
                        admin_mcp_reload,
                        methods=["POST"],
                    ),
                    Route(
                        "/api/admin/mcp-servers/{server_id}",
                        admin_get_mcp_server,
                    ),
                    Route(
                        "/api/admin/mcp-servers/{server_id}",
                        admin_update_mcp_server,
                        methods=["PUT"],
                    ),
                    Route(
                        "/api/admin/mcp-servers/{server_id}",
                        admin_delete_mcp_server,
                        methods=["DELETE"],
                    ),
                    # System: Model Definitions
                    Route("/api/admin/model-definitions", admin_list_model_definitions),
                    Route(
                        "/api/admin/model-definitions",
                        admin_create_model_definition,
                        methods=["POST"],
                    ),
                    Route(
                        "/api/admin/model-definitions/reload",
                        admin_model_reload,
                        methods=["POST"],
                    ),
                    Route(
                        "/api/admin/model-definitions/detect",
                        admin_detect_model,
                        methods=["POST"],
                    ),
                    Route(
                        "/api/admin/model-definitions/{definition_id}",
                        admin_get_model_definition,
                    ),
                    Route(
                        "/api/admin/model-definitions/{definition_id}",
                        admin_update_model_definition,
                        methods=["PUT"],
                    ),
                    Route(
                        "/api/admin/model-definitions/{definition_id}",
                        admin_delete_model_definition,
                        methods=["DELETE"],
                    ),
                    Route("/api/admin/model-capabilities", admin_model_capabilities),
                    Route(
                        "/api/admin/model-capabilities/known",
                        admin_known_models,
                    ),
                    # Governance: Prompt Policies
                    Route("/api/admin/prompt-policies", admin_list_prompt_policies),
                    Route(
                        "/api/admin/prompt-policies",
                        admin_create_prompt_policy,
                        methods=["POST"],
                    ),
                    Route(
                        "/api/admin/prompt-policies/{policy_id}",
                        admin_get_prompt_policy,
                    ),
                    Route(
                        "/api/admin/prompt-policies/{policy_id}",
                        admin_update_prompt_policy,
                        methods=["PUT"],
                    ),
                    Route(
                        "/api/admin/prompt-policies/{policy_id}",
                        admin_delete_prompt_policy,
                        methods=["DELETE"],
                    ),
                    # Governance: Judge Rules
                    Route("/api/admin/judge/settings", admin_list_judge_settings),
                    Route(
                        "/api/admin/judge/settings/{key:path}",
                        admin_update_judge_setting,
                        methods=["PUT"],
                    ),
                    Route(
                        "/api/admin/judge/settings/{key:path}",
                        admin_delete_judge_setting,
                        methods=["DELETE"],
                    ),
                    Route("/api/admin/judge/heuristic-rules", admin_list_heuristic_rules),
                    Route(
                        "/api/admin/judge/heuristic-rules",
                        admin_create_heuristic_rule,
                        methods=["POST"],
                    ),
                    Route(
                        "/api/admin/judge/heuristic-rules/{rule_id}",
                        admin_get_heuristic_rule,
                    ),
                    Route(
                        "/api/admin/judge/heuristic-rules/{rule_id}",
                        admin_update_heuristic_rule,
                        methods=["PUT"],
                    ),
                    Route(
                        "/api/admin/judge/heuristic-rules/{rule_id}",
                        admin_delete_heuristic_rule,
                        methods=["DELETE"],
                    ),
                    Route(
                        "/api/admin/judge/output-guard-patterns", admin_list_output_guard_patterns
                    ),
                    Route(
                        "/api/admin/judge/output-guard-patterns",
                        admin_create_output_guard_pattern,
                        methods=["POST"],
                    ),
                    Route(
                        "/api/admin/judge/output-guard-patterns/{pattern_id}",
                        admin_get_output_guard_pattern,
                    ),
                    Route(
                        "/api/admin/judge/output-guard-patterns/{pattern_id}",
                        admin_update_output_guard_pattern,
                        methods=["PUT"],
                    ),
                    Route(
                        "/api/admin/judge/output-guard-patterns/{pattern_id}",
                        admin_delete_output_guard_pattern,
                        methods=["DELETE"],
                    ),
                    Route(
                        "/api/admin/judge/reload",
                        admin_judge_reload,
                        methods=["POST"],
                    ),
                    Route(
                        "/api/admin/judge/validate-regex",
                        admin_validate_regex,
                        methods=["POST"],
                    ),
                    # Governance: Usage & Audit
                    Route("/api/admin/usage", admin_usage),
                    Route("/api/admin/audit", admin_audit),
                    # Governance: Intent Verdicts
                    Route("/api/admin/verdicts", admin_list_verdicts),
                    Route("/api/admin/output-assessments", admin_list_output_assessments),
                    Route(
                        "/api/admin/skills/{skill_id}/rescan",
                        admin_rescan_skill,
                        methods=["POST"],
                    ),
                    # Node metadata
                    Route("/api/admin/node-metadata", admin_get_all_node_metadata),
                    Route(
                        "/api/admin/nodes/{node_id}/metadata/{key}",
                        admin_set_node_metadata_key,
                        methods=["PUT"],
                    ),
                    Route(
                        "/api/admin/nodes/{node_id}/metadata/{key}",
                        admin_delete_node_metadata_key,
                        methods=["DELETE"],
                    ),
                    Route("/api/admin/nodes/{node_id}/metadata", admin_get_node_metadata),
                    Route(
                        "/api/admin/nodes/{node_id}/metadata",
                        admin_set_node_metadata,
                        methods=["PUT"],
                    ),
                    # TLS / ACME
                    Route("/api/admin/tls/ca", tls_ca_status),
                    Route("/api/admin/tls/ca.pem", tls_ca_cert),
                    Route("/api/admin/tls/certs", tls_list_certs),
                    Route(
                        "/api/admin/tls/certs/{domain}/renew",
                        tls_renew_cert,
                        methods=["POST"],
                    ),
                    Route(
                        "/api/admin/tls/certs/{domain}",
                        tls_delete_cert,
                        methods=["DELETE"],
                    ),
                ],
            ),
            Route("/health", health),
            Route("/metrics", console_metrics_endpoint),
            Route("/openapi.json", _openapi_handler),
            Route("/docs", _docs_handler),
            Mount("/static", app=StaticFiles(directory=str(_STATIC_DIR)), name="static"),
            Mount("/shared", app=StaticFiles(directory=str(_SHARED_DIR)), name="shared"),
            # Coordinator one-pane UI — the route serves a single
            # index.html template with the ws_id injected via data-ws-id
            # so coordinator.js can pull it without an extra round-trip.
            Route("/coordinator/{ws_id}", coordinator_page),
            # Proxy routes — serve server UI through console port
            Route("/node/{node_id}/", proxy_index),
            Route("/node/{node_id}/static/{path:path}", proxy_static),
            Route("/node/{node_id}/shared/{path:path}", proxy_shared_static),
            Route(
                "/node/{node_id}/v1/api/{path:path}",
                proxy_api,
                methods=["GET", "POST", "PUT", "DELETE"],
            ),
            Route(
                "/node/{node_id}/api/{path:path}",
                proxy_api,
                methods=["GET", "POST", "PUT", "DELETE"],
            ),
            Route("/node/{node_id}/{path:path}", proxy_non_api),
        ],
        middleware=_build_console_middleware(cors_origins),
        lifespan=_lifespan,
    )
    app.state.collector = collector
    app.state.jwt_secret = jwt_secret
    app.state.auth_storage = auth_storage
    app.state.proxy_token_mgr = proxy_token_mgr
    # Used by the boot scope probe to mint a token with the same
    # scopes the collector's SSE path uses.
    app.state.collector_token_mgr = getattr(collector, "_token_manager", None)
    # Set non-empty by the boot probe when upstream rejects the
    # collector token; cluster-dashboard endpoints then 503 with the
    # remediation hint until the operator fixes the scopes.
    app.state.collector_scope_error = ""
    app.state.console_url = console_url
    app.state.tls_manager = tls_manager
    app.state.router = router
    app.state.console_metrics = console_metrics or ConsoleMetrics()

    # Mount ACME responder whenever a TLS manager is configured.
    # ACMEResponder (lacme 1.0.2+) serves /ca.pem natively.
    if tls_manager is not None:
        from starlette.routing import Mount as RouteMount

        app.routes.insert(0, RouteMount("/acme", app=tls_manager.get_responder()))

    from turnstone.core.auth import LoginRateLimiter

    app.state.login_limiter = LoginRateLimiter()

    # OIDC configuration (opt-in via env vars)
    from turnstone.core.oidc import load_oidc_config

    oidc_config = load_oidc_config()
    app.state.oidc_config = oidc_config
    app.state.jwks_data = None  # populated after async discovery

    # Scheduler — start background thread if storage is available
    if auth_storage is not None:
        from turnstone.console.scheduler import TaskScheduler

        scheduler = TaskScheduler(
            collector=collector,
            storage=auth_storage,
            api_token="",
            token_manager=proxy_token_mgr,
        )
        app.state.scheduler = scheduler
    else:
        app.state.scheduler = None

    return app


def _build_console_middleware(cors_origins: list[str] | None = None) -> list[Middleware]:
    """Build the middleware stack with optional CORS."""
    stack: list[Middleware] = []
    if cors_origins:
        from turnstone.core.web_helpers import cors_middleware

        stack.append(cors_middleware(cors_origins))
    stack.append(
        Middleware(AuthMiddleware, jwt_audience=JWT_AUD_CONSOLE, jwt_version=jwt_version_slot())
    )
    return stack


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="turnstone console — cluster dashboard service.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              turnstone-console                              # default settings
              turnstone-console --port 9090                  # custom port
        """),
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Host to bind to (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8090,
        help="Port to listen on (default: 8090)",
    )
    from turnstone.core.log import add_log_args

    add_log_args(parser)
    from turnstone.core.config import add_config_arg, apply_config

    add_config_arg(parser)
    apply_config(parser, ["console", "auth"])
    args = parser.parse_args()

    from turnstone.core.log import configure_logging_from_args

    configure_logging_from_args(args, "console")

    from turnstone.core.auth import load_jwt_secret

    jwt_secret = load_jwt_secret()

    # Initialize storage early — the collector needs it for service discovery.
    auth_storage = None
    try:
        from turnstone.core.storage import init_storage

        db_backend = os.environ.get("TURNSTONE_DB_BACKEND", "sqlite")
        db_url = os.environ.get("TURNSTONE_DB_URL", "")
        db_path = os.environ.get("TURNSTONE_DB_PATH", "")
        auth_storage = init_storage(
            db_backend,
            path=db_path,
            url=db_url,
            sslmode=os.environ.get("TURNSTONE_DB_SSLMODE", ""),
            sslrootcert=os.environ.get("TURNSTONE_DB_SSLROOTCERT", ""),
            sslcert=os.environ.get("TURNSTONE_DB_SSLCERT", ""),
            sslkey=os.environ.get("TURNSTONE_DB_SSLKEY", ""),
        )
    except Exception:
        log.info("Console storage not available — admin API disabled, JWT-only auth")

    if auth_storage is None:
        log.error(
            "Storage backend is required for the console (service discovery). "
            "Set TURNSTONE_DB_PATH or TURNSTONE_DB_URL."
        )
        raise SystemExit(1)

    from turnstone.core.auth import JWT_AUD_SERVER, ServiceTokenManager

    # ``service`` scope is REQUIRED — ``/v1/api/events/global`` on every
    # upstream node hard-gates on it (server.py global_events_sse).
    # ``read`` is kept for legacy read-path compatibility.
    collector_token_mgr = ServiceTokenManager(
        user_id="console-collector",
        scopes=frozenset({"read", "service"}),
        source="console",
        secret=jwt_secret,
        audience=JWT_AUD_SERVER,
        expiry_hours=1,
    )
    log.info("console.collector_token_manager_created")

    router = ConsoleRouter(storage=auth_storage)
    console_metrics = ConsoleMetrics()

    collector = ClusterCollector(
        storage=auth_storage,
        token_manager=collector_token_mgr,
        router=router,
        console_metrics=console_metrics,
    )
    collector.start()

    _load_static()

    proxy_token_mgr = ServiceTokenManager(
        user_id="console-proxy",
        scopes=frozenset({"read", "write", "approve", "service"}),
        source="console",
        secret=jwt_secret,
        audience=JWT_AUD_SERVER,
        expiry_hours=1,
    )
    log.info("console.proxy_token_manager_created")

    from turnstone.core.web_helpers import parse_cors_origins

    cors_origins = parse_cors_origins()

    # TLS: initialize manager if enabled
    tls_mgr = None
    # Console URL for service registration — other services use this to discover the console.
    # Precedence: TURNSTONE_CONSOLE_URL env > auto-detect from bind address.
    # In Docker Compose, set TURNSTONE_CONSOLE_URL to the service name (e.g. http://console:8090).
    import socket as _socket

    _console_url_env = os.environ.get("TURNSTONE_CONSOLE_URL", "")
    if _console_url_env:
        console_url = _console_url_env
    else:
        _advertise_host = args.host
        if _advertise_host in ("0.0.0.0", "::", ""):
            _advertise_host = _socket.gethostname()
        console_url = f"http://{_advertise_host}:{args.port}"
    if auth_storage:
        try:
            from turnstone.core.config_store import ConfigStore

            _cs = ConfigStore(auth_storage)
            # Seed ConfigStore from env vars (TURNSTONE_{SECTION}_{KEY})
            _seed_config_from_env(_cs, auth_storage)
            if _cs.get("tls.enabled"):
                from turnstone.console.tls import TLSManager

                tls_mgr = TLSManager(auth_storage, config_store=_cs)
                # Init CA before create_app so ACME responder can be mounted
                import asyncio

                asyncio.run(tls_mgr.init_ca())
                # Upgrade scheme to https if no explicit URL was provided
                if not _console_url_env:
                    console_url = console_url.replace("http://", "https://")
                log.info("TLS enabled")
        except ImportError:
            log.warning("TLS enabled but lacme not installed — pip install turnstone[tls]")
            tls_mgr = None
        except Exception:
            log.warning("TLS initialization failed", exc_info=True)
            tls_mgr = None

        # Sync TLS state to ConfigStore so server nodes see the correct value.
        # Three cases:
        # 1. TLS succeeded → write true
        # 2. TLS not configured (DB false/unset) → write false (definitive)
        # 3. TLS configured (DB true) but init failed → don't overwrite
        #    (transient failure shouldn't permanently disable TLS)
        try:
            db_enabled = _cs.get("tls.enabled")
            if tls_mgr is not None:
                if not db_enabled:
                    _cs.set("tls.enabled", True, changed_by="console-startup")
            elif db_enabled:
                log.warning(
                    "tls.enabled is true in ConfigStore but TLS init failed — "
                    "server nodes will attempt TLS and fall back to plain HTTP"
                )
            else:
                _cs.set("tls.enabled", False, changed_by="console-startup")
        except Exception:
            log.debug("Failed to sync TLS state to ConfigStore", exc_info=True)

    app = create_app(
        collector=collector,
        jwt_secret=jwt_secret,
        auth_storage=auth_storage,
        proxy_token_mgr=proxy_token_mgr,
        cors_origins=cors_origins,
        tls_manager=tls_mgr,
        console_url=console_url,
        router=router,
        console_metrics=console_metrics,
    )

    log.info("Console starting on %s", console_url)
    log.info("Auth: enabled (JWT)")
    print("Press Ctrl+C to stop.")

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
