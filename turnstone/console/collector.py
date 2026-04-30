"""Cluster state collector — aggregates data from all turnstone nodes.

Discovers nodes via the service registry (StorageBackend) and subscribes
to each node's ``/v1/api/events/global`` SSE stream for real-time state
updates.  A single asyncio event loop on one dedicated thread multiplexes
all SSE connections, scaling to 1000+ nodes.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import queue
import random
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx
import httpx_sse

from turnstone.core.workstream import WorkstreamKind

if TYPE_CHECKING:
    from turnstone.console.metrics import ConsoleMetrics
    from turnstone.console.router import ConsoleRouter
    from turnstone.core.auth import ServiceTokenManager
    from turnstone.core.storage._protocol import StorageBackend

log = logging.getLogger("turnstone.console.collector")


@dataclass
class NodeSnapshot:
    """In-memory snapshot of a single node's state."""

    node_id: str = ""
    server_url: str = ""
    started: float = 0.0
    last_seen: float = 0.0  # monotonic time of last successful data
    max_ws: int = 10  # max workstreams (capacity)
    workstreams: dict[str, dict[str, Any]] = field(default_factory=dict)
    health: dict[str, Any] = field(default_factory=dict)
    aggregate: dict[str, Any] = field(default_factory=dict)
    reachable: bool = True
    # Last unreachable-reason string (e.g. ``"HTTP 403"``,
    # ``"ConnectError"``, ``"node_id mismatch"``).  Surfaced through
    # ``get_snapshot`` / ``get_nodes`` / ``get_node_detail`` so ops
    # dashboards + the console node-list can show WHY a node is down
    # without operators having to tail the collector log.  Cleared
    # when the node reconnects successfully.
    reachable_reason: str = ""


class ClusterCollector:
    """Aggregates cluster state from the service registry and per-node SSE streams.

    Two daemon threads:
    1. Node discovery — queries the service registry every ``discovery_interval`` seconds
    2. SSE manager — single asyncio event loop multiplexing SSE connections to all nodes
    """

    def __init__(
        self,
        storage: StorageBackend,
        discovery_interval: float = 60.0,
        http_timeout: float = 30.0,
        token_manager: ServiceTokenManager | None = None,
        tls_verify: Any = True,
        tls_cert: tuple[str, str] | None = None,
        router: ConsoleRouter | None = None,
        console_metrics: ConsoleMetrics | None = None,
    ):
        self._storage = storage
        self._discovery_interval = discovery_interval
        self._http_timeout = http_timeout
        self._token_manager = token_manager
        self._router = router
        self._console_metrics = console_metrics
        self._tls_verify = tls_verify
        self._tls_cert = tls_cert

        self._lock = threading.Lock()
        self._nodes: dict[str, NodeSnapshot] = {}
        self._running = False
        self._threads: list[threading.Thread] = []

        # SSE fan-out to browser clients
        self._listeners: list[queue.Queue[dict[str, Any]]] = []
        self._listeners_lock = threading.Lock()

        # SSE manager state (managed by the asyncio event loop thread)
        self._sse_loop: asyncio.AbstractEventLoop | None = None
        self._sse_tasks: dict[str, asyncio.Task[None]] = {}
        self._sse_stop_events: dict[str, asyncio.Event] = {}
        self._sse_async_client: httpx.AsyncClient | None = None

    def upgrade_tls(self, tls_verify: Any = True, tls_cert: tuple[str, str] | None = None) -> None:
        """Update TLS settings for future SSE connections."""
        self._tls_verify = tls_verify
        self._tls_cert = tls_cert
        # If the async client is running, replace it on the event loop.
        if self._sse_loop is not None and self._sse_loop.is_running():
            asyncio.run_coroutine_threadsafe(self._replace_async_client(), self._sse_loop)

    async def _replace_async_client(self) -> None:
        """Replace the async httpx client (called on the SSE event loop).

        Closing the old client terminates its underlying connections, which
        causes active ``_node_sse_task`` coroutines to raise and reconnect
        using the new client with updated TLS settings.
        """
        old = self._sse_async_client
        self._sse_async_client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10, read=None, write=5, pool=None),
            limits=httpx.Limits(max_connections=2000, max_keepalive_connections=1500),
            verify=self._tls_verify,
            cert=self._tls_cert,
        )
        if old is not None:
            await old.aclose()

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Start background threads."""
        self._running = True
        for target, name in [
            (self._discovery_loop, "console-discovery"),
            (self._sse_manager_thread, "console-sse"),
        ]:
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)
        log.info("ClusterCollector started")

    def stop(self) -> None:
        """Stop all threads and clean up resources.

        Sets ``_running = False`` which causes the SSE manager coroutine to
        exit naturally (its ``while self._running`` loop terminates), running
        its ``finally`` cleanup (cancel tasks, close AsyncClient).
        """
        self._running = False
        # Request cancellation of all SSE tasks so they don't block the
        # manager's cleanup. The manager coroutine exits when _running is
        # False and handles remaining task cancellation in its finally block.
        if self._sse_loop is not None and self._sse_loop.is_running():
            for node_id in list(self._sse_tasks):
                asyncio.run_coroutine_threadsafe(self._stop_node(node_id), self._sse_loop)
        # Wait for background threads to finish their shutdown.
        for t in self._threads:
            t.join(timeout=5)
        log.info("ClusterCollector stopped")

    def _fanout(self, event: dict[str, Any]) -> None:
        """Copy an event to all registered SSE listener queues."""
        with self._listeners_lock:
            for q in self._listeners:
                with contextlib.suppress(queue.Full):
                    q.put_nowait(event)

    # -- auth helpers --------------------------------------------------------

    def _auth_headers(self) -> dict[str, str]:
        """Build auth headers for the current SSE connection."""
        if self._token_manager is not None:
            return {"Authorization": f"Bearer {self._token_manager.token}"}
        return {}

    # -- SSE manager ---------------------------------------------------------

    def _sse_manager_thread(self) -> None:
        """Run asyncio event loop that manages all node SSE connections."""
        self._sse_loop = asyncio.new_event_loop()
        try:
            self._sse_loop.run_until_complete(self._sse_manager())
        finally:
            self._sse_loop.close()
            self._sse_loop = None

    async def _sse_manager(self) -> None:
        """Top-level coroutine — runs until collector stops."""
        self._sse_async_client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10, read=None, write=5, pool=None),
            limits=httpx.Limits(max_connections=2000, max_keepalive_connections=1500),
            verify=self._tls_verify,
            cert=self._tls_cert,
        )
        try:
            while self._running:
                await asyncio.sleep(1)
        finally:
            # Cancel all remaining tasks
            for task in self._sse_tasks.values():
                task.cancel()
            for task in self._sse_tasks.values():
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            self._sse_tasks.clear()
            self._sse_stop_events.clear()
            await self._sse_async_client.aclose()
            self._sse_async_client = None

    async def _start_node(self, node_id: str) -> None:
        """Start an SSE task for a node (called on the SSE event loop)."""
        if node_id in self._sse_tasks:
            return  # already running
        stop = asyncio.Event()
        self._sse_stop_events[node_id] = stop
        self._sse_tasks[node_id] = asyncio.create_task(
            self._node_sse_task(node_id, stop),
            name=f"sse-{node_id}",
        )

    async def _stop_node(self, node_id: str) -> None:
        """Stop an SSE task for a node (called on the SSE event loop)."""
        stop = self._sse_stop_events.pop(node_id, None)
        if stop:
            stop.set()
        task = self._sse_tasks.pop(node_id, None)
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _node_sse_task(self, node_id: str, stop_event: asyncio.Event) -> None:
        """Persistent SSE connection to a single server node."""
        backoff = 1.0
        while not stop_event.is_set() and self._running:
            url = self._get_node_url(node_id)
            if not url or self._sse_async_client is None:
                break
            base = url.rstrip("/")
            try:
                async with httpx_sse.aconnect_sse(
                    self._sse_async_client,
                    "GET",
                    f"{base}/v1/api/events/global",
                    params={"expected_node_id": node_id},
                    headers=self._auth_headers(),
                ) as source:
                    status = source.response.status_code
                    if status == 409:
                        log.warning("Node identity mismatch for %s at %s", node_id, url)
                        self._mark_unreachable(node_id, reason="node_id mismatch")
                        break  # stop reconnecting — wrong node at this URL
                    # 4xx from upstream is ALWAYS an operator-actionable
                    # configuration problem (missing service scope,
                    # expired JWT secret mismatch, tenant misconfig) —
                    # surface at warning so it shows up in ops logs
                    # instead of silently burning SSE reconnect budget
                    # at debug.  403 in particular was the long-standing
                    # "console dashboard is empty" footgun when the
                    # collector token lacked ``service`` scope.
                    if 400 <= status < 500:
                        # Bounded body read — iterate aiter_bytes() up
                        # to the preview cap so a malicious / oversized
                        # upstream can't force the collector to buffer
                        # an arbitrary HTML error page just to log a
                        # 200-char preview.  Stops pulling bytes as
                        # soon as we have enough.
                        body_preview = ""
                        try:
                            preview_cap = 256  # >200 chars after UTF-8 decode
                            chunks: list[bytes] = []
                            bytes_read = 0
                            async for chunk in source.response.aiter_bytes():
                                if not chunk:
                                    continue
                                remaining = preview_cap - bytes_read
                                if remaining <= 0:
                                    break
                                chunks.append(chunk[:remaining])
                                bytes_read += len(chunks[-1])
                                if bytes_read >= preview_cap:
                                    break
                            body_preview = b"".join(chunks).decode("utf-8", "replace")[:200]
                        except Exception:
                            body_preview = "<unreadable>"
                        log.warning(
                            "SSE %d from node %s at %s — %s",
                            status,
                            node_id,
                            url,
                            body_preview,
                        )
                        self._mark_unreachable(node_id, reason=f"HTTP {status}")
                        await asyncio.sleep(min(backoff, 30) + random.random())
                        backoff = min(backoff * 2, 30)
                        continue
                    source.response.raise_for_status()
                    async for sse in source.aiter_sse():
                        if stop_event.is_set():
                            break
                        if not sse.data:
                            continue  # ping/comment frame
                        try:
                            data = json.loads(sse.data)
                        except json.JSONDecodeError:
                            log.debug("Invalid SSE JSON from node %s", node_id)
                            continue
                        etype = data.get("type", "")
                        if etype == "node_snapshot":
                            # Client-side identity check (defense in depth)
                            if data.get("node_id") != node_id:
                                log.warning(
                                    "Snapshot node_id mismatch: expected %s, got %s",
                                    node_id,
                                    data.get("node_id"),
                                )
                                self._mark_unreachable(node_id, reason="node_id mismatch")
                                break
                            self._apply_snapshot(node_id, data)
                            backoff = 1.0
                        else:
                            self._apply_delta(node_id, data)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Network / timeout / TLS errors — expected during brief
                # node restarts.  Keep at debug so the log doesn't flood
                # on every backoff cycle; the warning above already
                # covers configuration-level failures operators need to
                # see.
                log.debug("SSE error for node %s: %r", node_id, exc, exc_info=True)
                self._mark_unreachable(node_id, reason=type(exc).__name__)
                await asyncio.sleep(min(backoff, 30) + random.random())
                backoff = min(backoff * 2, 30)

    def _get_node_url(self, node_id: str) -> str:
        """Get the server URL for a node (thread-safe)."""
        with self._lock:
            node = self._nodes.get(node_id)
            return node.server_url if node else ""

    def _mark_unreachable(self, node_id: str, reason: str = "") -> None:
        """Mark a node as unreachable (thread-safe).

        ``reason`` is a short human-readable diagnostic (e.g.
        ``"HTTP 403"``, ``"ConnectError"``) surfaced via the snapshot
        + node endpoints so operators can see WHY a node is down.
        """
        with self._lock:
            node = self._nodes.get(node_id)
            if node:
                node.reachable = False
                if reason:
                    node.reachable_reason = reason

    # -- node discovery ------------------------------------------------------

    def _discovery_loop(self) -> None:
        """Periodically scan the service registry for active nodes."""
        from turnstone.core.storage._registry import StorageUnavailableError

        while self._running:
            try:
                self._discover_nodes()
            except StorageUnavailableError:
                pass  # already logged by storage layer
            except Exception:
                log.exception("Node discovery error")
            time.sleep(self._discovery_interval)

    def _discover_nodes(self) -> None:
        """Query the service registry and update the node map."""
        raw_services = self._storage.list_services("server", max_age_seconds=120)
        active_ids = set()
        pending_events: list[dict[str, Any]] = []
        new_nodes: list[str] = []
        lost_nodes: list[str] = []

        with self._lock:
            for svc in raw_services:
                nid = svc.get("service_id", "")
                if not nid:
                    continue
                active_ids.add(nid)
                # Parse optional metadata JSON for max_ws, started
                meta: dict[str, Any] = {}
                raw_meta = svc.get("metadata", "")
                if raw_meta:
                    with contextlib.suppress(json.JSONDecodeError, TypeError):
                        meta = json.loads(raw_meta)
                url = svc.get("url", "")
                if nid not in self._nodes:
                    self._nodes[nid] = NodeSnapshot(
                        node_id=nid,
                        server_url=url,
                        started=meta.get("started", 0.0),
                        max_ws=meta.get("max_ws", 10),
                    )
                    pending_events.append({"type": "node_joined", "node_id": nid})
                    new_nodes.append(nid)
                    log.info("Discovered node: %s", nid)
                else:
                    self._nodes[nid].server_url = url or self._nodes[nid].server_url
                    self._nodes[nid].max_ws = meta.get("max_ws", self._nodes[nid].max_ws)

            # Remove nodes whose heartbeats expired — the ``console``
            # pseudo-node is permanent (hosts coordinator workstreams,
            # not a real service-registered node) so it's exempt from
            # eviction.  Without this guard, every discovery tick
            # deleted the pseudo-node + fanned out a spurious
            # node_lost, breaking the home-view coordinator list (#9).
            lost = [
                nid
                for nid in self._nodes
                if nid not in active_ids and nid != self.CONSOLE_PSEUDO_NODE_ID
            ]
            for nid in lost:
                del self._nodes[nid]
                pending_events.append({"type": "node_lost", "node_id": nid})
                lost_nodes.append(nid)
                log.info("Lost node: %s", nid)
        for event in pending_events:
            self._fanout(event)

        # Manage SSE tasks for new/lost nodes
        if self._sse_loop is not None and self._sse_loop.is_running():
            for nid in new_nodes:
                asyncio.run_coroutine_threadsafe(self._start_node(nid), self._sse_loop)
            for nid in lost_nodes:
                asyncio.run_coroutine_threadsafe(self._stop_node(nid), self._sse_loop)

        # Drive the router's cache from the collector's discovery
        # thread so the async route() handlers stay pure-in-memory.
        # The unconditional refresh also picks up admin-written
        # workstream_overrides between membership events.
        if self._router is not None:
            try:
                self._router.refresh_cache()
            except Exception:
                log.debug("Router refresh failed", exc_info=True)
            if self._console_metrics is not None:
                self._console_metrics.set_router_info(
                    self._router.node_count(),
                    self._router.version,
                )

    # -- SSE event handlers --------------------------------------------------

    def _reconcile_node(
        self, node_id: str, node: NodeSnapshot, new_ws_list: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Diff new workstream data against the current snapshot.

        Returns a list of pending events. Caller must hold ``_lock``.
        Updates ``node.workstreams`` in place.
        """
        pending: list[dict[str, Any]] = []
        old_ids = {k for k in node.workstreams if k}
        new_ws: dict[str, dict[str, Any]] = {}
        for ws in new_ws_list:
            ws_id = ws.get("id", "")
            if not ws_id:
                continue
            ws["node"] = node_id
            ws["server_url"] = node.server_url
            new_ws[ws_id] = ws
        new_ids = set(new_ws.keys())
        # Additions
        for ws_id in sorted(new_ids - old_ids):
            ws = new_ws[ws_id]
            pending.append(
                {
                    "type": "ws_created",
                    "ws_id": ws_id,
                    "name": ws.get("title", "") or ws.get("name", ""),
                    "title": ws.get("title", ""),
                    "node_id": node_id,
                    "kind": WorkstreamKind.from_raw(ws.get("kind")),
                    "parent_ws_id": ws.get("parent_ws_id"),
                }
            )
        # Removals
        for ws_id in sorted(old_ids - new_ids):
            pending.append({"type": "ws_closed", "ws_id": ws_id})
        # State and name changes on existing workstreams
        for ws_id in sorted(new_ids & old_ids):
            old_ws = node.workstreams.get(ws_id, {})
            new_w = new_ws[ws_id]
            old_state = old_ws.get("state", "")
            new_state = new_w.get("state", "")
            if old_state != new_state:
                pending.append(
                    {
                        "type": "cluster_state",
                        "ws_id": ws_id,
                        "state": new_state,
                        "node_id": node_id,
                        "tokens": new_w.get("tokens", 0),
                        "content": new_w.get("content", ""),
                        "kind": WorkstreamKind.from_raw(new_w.get("kind")),
                        "parent_ws_id": new_w.get("parent_ws_id"),
                        "activity_state": new_w.get("activity_state", ""),
                        "pending_approval_detail": new_w.get("pending_approval_detail"),
                    }
                )
            old_name = old_ws.get("title", "") or old_ws.get("name", "")
            new_name = new_w.get("title", "") or new_w.get("name", "")
            if old_name != new_name and new_name:
                pending.append({"type": "ws_rename", "ws_id": ws_id, "name": new_name})
        node.workstreams = new_ws
        return pending

    def _apply_snapshot(self, node_id: str, data: dict[str, Any]) -> None:
        """Apply a ``node_snapshot`` SSE event to the in-memory state."""
        pending_events: list[dict[str, Any]] = []
        with self._lock:
            node = self._nodes.get(node_id)
            if not node:
                return
            node.last_seen = time.monotonic()
            node.reachable = True
            # Clear the diagnostic on successful reconnect so the
            # snapshot doesn't keep reporting a stale cause.
            node.reachable_reason = ""
            node.health = data.get("health", {})
            node.aggregate = data.get("aggregate", {})
            pending_events = self._reconcile_node(node_id, node, data.get("workstreams", []))
        for event in pending_events:
            self._fanout(event)

    def _apply_delta(self, node_id: str, data: dict[str, Any]) -> None:
        """Apply a single delta SSE event to the in-memory state."""
        etype = data.get("type", "")
        if not etype:
            return
        pending_events: list[dict[str, Any]] = []
        with self._lock:
            node = self._nodes.get(node_id)
            if not node:
                return
            node.last_seen = time.monotonic()

            if etype == "ws_state":
                # Server emits ws_state; translate to cluster_state for browser.
                # Only fan out if the workstream is known — a ws_state arriving
                # before ws_created (race) is silently absorbed on reconnect.
                ws_id = data.get("ws_id", "")
                ws = node.workstreams.get(ws_id)
                if ws:
                    ws["state"] = data.get("state", ws.get("state", ""))
                    ws["tokens"] = data.get("tokens", ws.get("tokens", 0))
                    ws["context_ratio"] = data.get("context_ratio", ws.get("context_ratio", 0))
                    ws["activity"] = data.get("activity", ws.get("activity", ""))
                    ws["activity_state"] = data.get("activity_state", ws.get("activity_state", ""))
                    # kind/parent_ws_id: defensive update from ws_state event.
                    # These rarely change but the event carries them so the
                    # collector's entry stays authoritative even if a delta
                    # lands before the originating ws_created (e.g. on reconnect).
                    if "kind" in data:
                        ws["kind"] = data["kind"]
                    if "parent_ws_id" in data:
                        ws["parent_ws_id"] = data["parent_ws_id"]
                    # ``pending_approval_detail`` overwrites (no
                    # ``ws.get`` fallback): the node's broadcast gate
                    # on ``_pending_approval is not None`` means the
                    # field is absent from ``data`` exactly when no
                    # approval is pending — falling back to the cached
                    # value would resurrect a stale detail after the
                    # approval resolved.  Without this assignment the
                    # cached ``node.workstreams`` dict served by
                    # ``get_node_detail`` / ``get_snapshot`` between
                    # reconciliations would render stale approve/deny
                    # buttons on closed approvals.
                    ws["pending_approval_detail"] = data.get("pending_approval_detail")
                    pending_events.append(
                        {
                            "type": "cluster_state",
                            "ws_id": ws_id,
                            "state": data.get("state", ""),
                            "node_id": node_id,
                            "tokens": data.get("tokens", 0),
                            "content": data.get("content", ""),
                            "kind": WorkstreamKind.from_raw(ws.get("kind")),
                            "parent_ws_id": ws.get("parent_ws_id"),
                            "activity_state": ws.get("activity_state", ""),
                            "pending_approval_detail": data.get("pending_approval_detail"),
                        }
                    )

            elif etype == "ws_activity":
                ws_id = data.get("ws_id", "")
                ws = node.workstreams.get(ws_id)
                if ws:
                    ws["activity"] = data.get("activity", "")
                    ws["activity_state"] = data.get("activity_state", "")
                # Activity events are not forwarded to cluster SSE — only state changes

            elif etype == "ws_created":
                ws_id = data.get("ws_id", "")
                ws_kind = WorkstreamKind.from_raw(data.get("kind"))
                ws_parent = data.get("parent_ws_id")
                # user_id travels on the event so console-side fan-out
                # can enforce tenant isolation — a coordinator must
                # never receive child_ws_* events for workstreams it
                # doesn't own.  Empty string when the emitter didn't
                # populate it (older nodes).
                ws_user = data.get("user_id", "") or ""
                if ws_id and ws_id not in node.workstreams:
                    node.workstreams[ws_id] = {
                        "id": ws_id,
                        "name": data.get("name", ""),
                        "state": "idle",
                        "node": node_id,
                        "server_url": node.server_url,
                        "model": data.get("model", ""),
                        "model_alias": data.get("model_alias", ""),
                        "tokens": 0,
                        "context_ratio": 0.0,
                        "activity": "",
                        "activity_state": "",
                        "tool_calls": 0,
                        "title": "",
                        "kind": ws_kind,
                        "parent_ws_id": ws_parent,
                        "user_id": ws_user,
                    }
                pending_events.append(
                    {
                        "type": "ws_created",
                        "ws_id": ws_id,
                        "name": data.get("title", "") or data.get("name", ""),
                        "title": data.get("title", ""),
                        "node_id": node_id,
                        "kind": ws_kind,
                        "parent_ws_id": ws_parent,
                        "user_id": ws_user,
                    }
                )

            elif etype == "ws_closed":
                ws_id = data.get("ws_id", "")
                node.workstreams.pop(ws_id, None)
                pending_events.append({"type": "ws_closed", "ws_id": ws_id})

            elif etype == "ws_rename":
                ws_id = data.get("ws_id", "")
                name = data.get("name", "")
                ws = node.workstreams.get(ws_id)
                if ws and name:
                    ws["name"] = name
                pending_events.append({"type": "ws_rename", "ws_id": ws_id, "name": name})

            elif etype == "health_changed":
                # Update the health dict's backend status in-place
                bstatus = data.get("backend_status", "")
                if bstatus:
                    if not node.health:
                        node.health = {}
                    backend = node.health.setdefault("backend", {})
                    backend["status"] = "up" if bstatus == "healthy" else "down"
                    node.health["status"] = "ok" if bstatus == "healthy" else "degraded"
                # Not forwarded to cluster SSE — next snapshot refreshes UI

            elif etype == "aggregate":
                node.aggregate = {
                    "total_tokens": data.get("total_tokens", 0),
                    "total_tool_calls": data.get("total_tool_calls", 0),
                    "active_count": data.get("active_count", 0),
                    "total_count": data.get("total_count", 0),
                }
                # Not forwarded to cluster SSE — overview queries read from snapshot

        for event in pending_events:
            self._fanout(event)

    # -- query methods (thread-safe) -----------------------------------------

    def get_overview(self) -> dict[str, Any]:
        """Return cluster overview: state counts, totals, aggregate stats.

        Excludes the ``"console"`` pseudo-node — coordinators are not
        compute-node workstreams and counting them would inflate the
        cluster summary.  The home view surfaces coordinators via the
        active-coordinators list instead.
        """
        states = {"running": 0, "thinking": 0, "attention": 0, "idle": 0, "error": 0}
        total_tokens = 0
        total_tool_calls = 0
        total_ws = 0
        mcp_servers = 0
        mcp_resources = 0
        mcp_prompts = 0
        versions: set[str] = set()
        with self._lock:
            for nid, node in self._nodes.items():
                if nid == self.CONSOLE_PSEUDO_NODE_ID:
                    continue
                for ws in node.workstreams.values():
                    state = ws.get("state", "idle")
                    states[state] = states.get(state, 0) + 1
                    total_ws += 1
                total_tokens += node.aggregate.get("total_tokens", 0)
                total_tool_calls += node.aggregate.get("total_tool_calls", 0)
                ver = node.health.get("version", "")
                if ver:
                    versions.add(ver)
                mcp = node.health.get("mcp", {})
                mcp_servers += mcp.get("servers", 0)
                mcp_resources += mcp.get("resources", 0)
                mcp_prompts += mcp.get("prompts", 0)
            node_count = sum(1 for nid in self._nodes if nid != self.CONSOLE_PSEUDO_NODE_ID)
        result: dict[str, Any] = {
            "nodes": node_count,
            "workstreams": total_ws,
            "states": states,
            "aggregate": {
                "total_tokens": total_tokens,
                "total_tool_calls": total_tool_calls,
            },
            "version_drift": len(versions) > 1,
            "versions": sorted(versions),
        }
        if mcp_servers:
            result["mcp_servers"] = mcp_servers
            result["mcp_resources"] = mcp_resources
            result["mcp_prompts"] = mcp_prompts
        return result

    def get_version_info(self) -> dict[str, Any]:
        """Return per-node version map and drift flag."""
        with self._lock:
            versions = {
                n.node_id: n.health.get("version", "")
                for n in self._nodes.values()
                if n.health.get("version")
            }
            unique = set(versions.values())
        return {
            "versions": versions,
            "unique_versions": sorted(unique),
            "drift": len(unique) > 1,
        }

    def get_nodes(
        self,
        sort_by: str = "activity",
        limit: int | None = 100,
        offset: int = 0,
        node_ids: set[str] | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """Return sorted, paginated node list with per-node counts.

        Pass ``limit=None`` to return all nodes (no pagination).
        Pass ``node_ids`` to restrict results to the given set.
        """
        with self._lock:
            items = []
            for node in self._nodes.values():
                # Hide the ``"console"`` pseudo-node from compute-node
                # listings — it's a synthetic carrier for coordinator
                # workstreams, not a real node operators target.
                if node.node_id == self.CONSOLE_PSEUDO_NODE_ID:
                    continue
                if node_ids is not None and node.node_id not in node_ids:
                    continue
                ws_states = {
                    "running": 0,
                    "thinking": 0,
                    "attention": 0,
                    "idle": 0,
                    "error": 0,
                }
                for ws in node.workstreams.values():
                    s = ws.get("state", "idle")
                    ws_states[s] = ws_states.get(s, 0) + 1
                # Use aggregate tokens if available, else sum from workstreams
                agg_tokens = node.aggregate.get("total_tokens", 0)
                if not agg_tokens:
                    agg_tokens = sum(ws.get("tokens", 0) for ws in node.workstreams.values())
                items.append(
                    {
                        "node_id": node.node_id,
                        "server_url": node.server_url,
                        "ws_total": len(node.workstreams),
                        "ws_running": ws_states["running"],
                        "ws_thinking": ws_states["thinking"],
                        "ws_attention": ws_states["attention"],
                        "ws_idle": ws_states["idle"],
                        "ws_error": ws_states["error"],
                        "total_tokens": agg_tokens,
                        "ws_tokens": agg_tokens,
                        "max_ws": node.max_ws,
                        "started": node.started,
                        "last_seen": node.last_seen,
                        "reachable": node.reachable,
                        "reachable_reason": node.reachable_reason,
                        "health": node.health,
                        "version": node.health.get("version", ""),
                    }
                )
            total = len(items)

        # Sort (secondary key: node_id for stable ordering)
        if sort_by == "activity":
            items.sort(key=lambda n: (-(n["ws_running"] + n["ws_attention"]), n["node_id"]))
        elif sort_by == "tokens":
            items.sort(key=lambda n: (-n["total_tokens"], n["node_id"]))
        elif sort_by == "name":
            items.sort(key=lambda n: n["node_id"])

        if limit is None:
            return items[offset:], total
        return items[offset : offset + limit], total

    def get_all_nodes(self) -> list[dict[str, Any]]:
        """Return all nodes without pagination (for fan-out operations)."""
        nodes, _ = self.get_nodes(sort_by="activity", limit=None)
        return nodes

    def get_workstreams(
        self,
        state: str | None = None,
        node: str | None = None,
        search: str | None = None,
        sort_by: str = "state",
        page: int = 1,
        per_page: int = 50,
        extra_rows: list[dict[str, Any]] | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """Return filtered, sorted, paginated workstreams + total count.

        ``extra_rows`` are merged into the unpaginated pool before
        filter / sort / paginate — used by callers that contribute
        console-local rows (e.g. coordinator workstreams) that aren't
        tracked on any node's SSE stream.
        """
        with self._lock:
            all_ws = []
            for n in self._nodes.values():
                # Skip the ``"console"`` pseudo-node — coordinator rows
                # are contributed by the ``_coordinator_rows`` caller
                # (via ``extra_rows``) which applies tenancy filtering.
                # Without this skip, non-admin callers would see every
                # tenant's coordinators via ``/v1/api/cluster/workstreams``.
                if n.node_id == self.CONSOLE_PSEUDO_NODE_ID:
                    continue
                for ws in n.workstreams.values():
                    all_ws.append(dict(ws))
            if extra_rows:
                all_ws.extend(dict(r) for r in extra_rows)

        # Filter
        if state:
            all_ws = [ws for ws in all_ws if ws.get("state") == state]
        if node:
            all_ws = [ws for ws in all_ws if ws.get("node") == node]
        if search:
            q = search.lower()
            all_ws = [
                ws
                for ws in all_ws
                if q in ws.get("name", "").lower()
                or q in ws.get("title", "").lower()
                or q in ws.get("node", "").lower()
            ]

        # Sort
        state_order = {
            "running": 0,
            "thinking": 1,
            "attention": 2,
            "error": 3,
            "idle": 4,
        }
        if sort_by == "state":
            all_ws.sort(key=lambda ws: state_order.get(ws.get("state", "idle"), 9))
        elif sort_by == "tokens":
            all_ws.sort(key=lambda ws: ws.get("tokens", 0), reverse=True)
        elif sort_by == "name":
            all_ws.sort(key=lambda ws: ws.get("name", ""))

        total = len(all_ws)
        start = (page - 1) * per_page
        page_ws = all_ws[start : start + per_page]
        return page_ws, total

    def get_node_detail(self, node_id: str) -> dict[str, Any] | None:
        """Return a single node's workstreams and health."""
        with self._lock:
            node = self._nodes.get(node_id)
            if not node:
                return None
            return {
                "node_id": node.node_id,
                "server_url": node.server_url,
                "health": dict(node.health),
                "workstreams": [dict(ws) for ws in node.workstreams.values()],
                "aggregate": dict(node.aggregate),
                "reachable": node.reachable,
                "reachable_reason": node.reachable_reason,
            }

    def get_snapshot(self) -> dict[str, Any]:
        """Build a complete cluster snapshot under a single lock.

        Returns everything the UI needs to render the full dashboard:
        all nodes with their workstreams plus pre-computed overview aggregates.
        """
        with self._lock:
            return self._build_snapshot_locked()

    def get_snapshot_and_register(self, q: queue.Queue[dict[str, Any]]) -> dict[str, Any]:
        """Build snapshot and register listener atomically.

        Acquiring both locks ensures no event can be published between
        the snapshot read and the listener registration — the client
        receives the snapshot followed by every subsequent event with
        no gap.
        """
        with self._lock:
            snap = self._build_snapshot_locked()
            with self._listeners_lock:
                self._listeners.append(q)
        return snap

    def _build_snapshot_locked(self) -> dict[str, Any]:
        """Build snapshot data — caller must hold ``_lock``."""
        nodes_out = []
        states: dict[str, int] = {
            "running": 0,
            "thinking": 0,
            "attention": 0,
            "idle": 0,
            "error": 0,
        }
        total_tokens = 0
        total_tool_calls = 0
        total_ws = 0
        mcp_servers = 0
        mcp_resources = 0
        mcp_prompts = 0
        versions: set[str] = set()

        for node in self._nodes.values():
            ws_list = []
            for ws in node.workstreams.values():
                ws_list.append(dict(ws))
                s = ws.get("state", "idle")
                states[s] = states.get(s, 0) + 1
                total_ws += 1

            total_tokens += node.aggregate.get("total_tokens", 0)
            total_tool_calls += node.aggregate.get("total_tool_calls", 0)
            ver = node.health.get("version", "")
            if ver:
                versions.add(ver)
            mcp = node.health.get("mcp", {})
            mcp_servers += mcp.get("servers", 0)
            mcp_resources += mcp.get("resources", 0)
            mcp_prompts += mcp.get("prompts", 0)

            nodes_out.append(
                {
                    "node_id": node.node_id,
                    "server_url": node.server_url,
                    "max_ws": node.max_ws,
                    "reachable": node.reachable,
                    "reachable_reason": node.reachable_reason,
                    "version": ver,
                    "health": dict(node.health),
                    "aggregate": dict(node.aggregate),
                    "workstreams": ws_list,
                }
            )

        node_count = len(self._nodes)

        overview: dict[str, Any] = {
            "nodes": node_count,
            "workstreams": total_ws,
            "states": states,
            "aggregate": {
                "total_tokens": total_tokens,
                "total_tool_calls": total_tool_calls,
            },
            "version_drift": len(versions) > 1,
            "versions": sorted(versions),
        }
        if mcp_servers:
            overview["mcp_servers"] = mcp_servers
            overview["mcp_resources"] = mcp_resources
            overview["mcp_prompts"] = mcp_prompts

        return {
            "nodes": nodes_out,
            "overview": overview,
            "timestamp": time.time(),
        }

    # -- SSE listener management ---------------------------------------------

    def register_listener(self, q: queue.Queue[dict[str, Any]]) -> None:
        """Register a queue for SSE event fan-out."""
        with self._listeners_lock:
            self._listeners.append(q)

    def unregister_listener(self, q: queue.Queue[dict[str, Any]]) -> None:
        """Unregister a queue from SSE event fan-out."""
        with self._listeners_lock:
            if q in self._listeners:
                self._listeners.remove(q)

    # ------------------------------------------------------------------
    # Console pseudo-node — coordinator workstreams live here (#9)
    # ------------------------------------------------------------------
    #
    # Coordinators run on the console process, not on a cluster node,
    # so the SSE stream the collector manages for real nodes never
    # surfaces them.  To avoid a parallel polling channel the home view
    # had to drive itself, the coordinator manager drives a pseudo-node
    # here: register the node on startup, upsert workstream entries on
    # create / close / state-change, and fan out matching ws_created /
    # ws_closed / cluster_state events so the browser's existing
    # clusterState machinery picks them up live.

    CONSOLE_PSEUDO_NODE_ID = "console"

    def ensure_console_pseudo_node(self) -> None:
        """Install the ``"console"`` pseudo-node in the snapshot map.

        Idempotent — a second call is a no-op.  No SSE task is started
        for this node (it has no real server URL and no remote state to
        mirror); the coordinator manager feeds events directly via
        :meth:`emit_console_ws_created` / :meth:`emit_console_ws_closed`
        / :meth:`emit_console_ws_state`.
        """
        with self._lock:
            if self.CONSOLE_PSEUDO_NODE_ID in self._nodes:
                return
            self._nodes[self.CONSOLE_PSEUDO_NODE_ID] = NodeSnapshot(
                node_id=self.CONSOLE_PSEUDO_NODE_ID,
                server_url="",
                started=time.time(),
                last_seen=time.monotonic(),
                max_ws=0,
                reachable=True,
            )

    def emit_console_ws_created(
        self,
        ws_id: str,
        *,
        name: str,
        user_id: str,
        kind: str,
        state: str = "idle",
        parent_ws_id: str | None = None,
    ) -> None:
        """Record a new coordinator row on the console pseudo-node + fan out.

        Best-effort: if the pseudo-node doesn't exist yet the call is
        dropped rather than raising.  Matches the shape
        :func:`_apply_delta` produces for real-node ``ws_created`` events
        so the browser's ``patchClusterState`` handler stays uniform.
        """
        self.ensure_console_pseudo_node()
        pending: list[dict[str, Any]] = []
        now = time.time()
        with self._lock:
            node = self._nodes.get(self.CONSOLE_PSEUDO_NODE_ID)
            if node is None:
                return
            if ws_id not in node.workstreams:
                node.workstreams[ws_id] = {
                    "id": ws_id,
                    "name": name,
                    "state": state,
                    "node": self.CONSOLE_PSEUDO_NODE_ID,
                    "server_url": "",
                    "tokens": 0,
                    "context_ratio": 0.0,
                    "activity": "",
                    "activity_state": "",
                    "tool_calls": 0,
                    "title": "",
                    "kind": kind,
                    "parent_ws_id": parent_ws_id,
                    "user_id": user_id or "",
                    "updated": now,
                }
            pending.append(
                {
                    "type": "ws_created",
                    "ws_id": ws_id,
                    "name": name,
                    "title": "",
                    "node_id": self.CONSOLE_PSEUDO_NODE_ID,
                    "kind": kind,
                    "parent_ws_id": parent_ws_id,
                    "user_id": user_id or "",
                }
            )
        for event in pending:
            self._fanout(event)

    def emit_console_ws_closed(self, ws_id: str) -> None:
        """Drop the coordinator row from the console pseudo-node + fan out."""
        with self._lock:
            node = self._nodes.get(self.CONSOLE_PSEUDO_NODE_ID)
            if node is None:
                return
            node.workstreams.pop(ws_id, None)
        self._fanout({"type": "ws_closed", "ws_id": ws_id})

    def emit_console_ws_state(
        self,
        ws_id: str,
        state: str,
        *,
        tokens: int = 0,
        context_ratio: float = 0.0,
        activity: str = "",
        activity_state: str = "",
        content: str = "",
    ) -> None:
        """Update the coordinator row's state on the console pseudo-node + fan out.

        The keyword args carry the rich-payload snapshot the cluster
        dashboard renders for both kinds (matches the interactive
        ``ws_state`` event shape the SSE relay produces in
        :meth:`_apply_delta`). Pre-rich-payload coord broadcast was
        state-only with ``tokens=0`` / ``content=""`` hardcoded —
        the dashboard's coord row showed the state column updating
        but no token count, no activity, no per-turn content. With
        the lift, all four populate (per the per-ws metric writes
        :class:`ConsoleCoordinatorUI` inherits from
        :class:`SessionUIBase`), so coord rows match interactive in
        the cluster overview.

        Defaults are kept so :class:`turnstone.console.coordinator_adapter.CoordinatorAdapter`
        is the only production caller wiring the rich kwargs; tests
        and any future call site can stay state-only without
        breaking.
        """
        with self._lock:
            node = self._nodes.get(self.CONSOLE_PSEUDO_NODE_ID)
            if node is None:
                return
            entry = node.workstreams.get(ws_id)
            if entry is None:
                return
            entry["state"] = state
            entry["tokens"] = tokens
            entry["context_ratio"] = context_ratio
            entry["activity"] = activity
            entry["activity_state"] = activity_state
        self._fanout(
            {
                "type": "cluster_state",
                "ws_id": ws_id,
                "state": state,
                "node_id": self.CONSOLE_PSEUDO_NODE_ID,
                "tokens": tokens,
                "content": content,
                "kind": WorkstreamKind.COORDINATOR.value,
                "parent_ws_id": None,
                "activity_state": activity_state,
            }
        )

    def update_console_ws_activity(
        self,
        ws_id: str,
        *,
        activity: str,
        activity_state: str,
    ) -> None:
        """Update a coord row's live activity transition (no fan-out).

        Mirrors :meth:`_apply_delta`'s ``ws_activity`` handler for
        real-node workstreams: writes the in-memory pseudo-node
        entry but does NOT fan out a separate event over the cluster
        SSE stream (interactive doesn't either — activity is
        snapshot data piggybacked on subsequent state-change
        broadcasts). The dashboard's per-ws polling reads the
        in-memory row, so live activity ticks land on the next
        snapshot fetch even without a dedicated SSE event.

        Named ``update_*`` rather than ``emit_*`` to flag the
        no-fan-out asymmetry vs. the rest of the
        ``emit_console_ws_*`` family (``_created`` / ``_closed`` /
        ``_state`` / ``_rename`` all call ``self._fanout`` — this
        one doesn't).

        Best-effort: drop silently if the pseudo-node or the row
        isn't present (e.g. activity tick arrives between row pop
        and listener re-registration during evict).
        """
        with self._lock:
            node = self._nodes.get(self.CONSOLE_PSEUDO_NODE_ID)
            if node is None:
                return
            entry = node.workstreams.get(ws_id)
            if entry is None:
                return
            entry["activity"] = activity
            entry["activity_state"] = activity_state

    def emit_console_ws_rename(self, ws_id: str, name: str) -> None:
        """Rename the coordinator row + fan out ``ws_rename``."""
        if not name:
            return
        with self._lock:
            node = self._nodes.get(self.CONSOLE_PSEUDO_NODE_ID)
            if node is None:
                return
            entry = node.workstreams.get(ws_id)
            if entry is None:
                return
            entry["name"] = name
        self._fanout({"type": "ws_rename", "ws_id": ws_id, "name": name})
