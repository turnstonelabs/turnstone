"""Cluster state collector — aggregates data from all turnstone nodes.

Discovers nodes via Redis heartbeat keys, polls each node's /api/dashboard
endpoint for workstream data, and subscribes to the cluster event channel
for real-time state changes.
"""

from __future__ import annotations

import contextlib
import json
import logging
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from turnstone.mq.broker import RedisBroker

log = logging.getLogger("turnstone.console.collector")


@dataclass
class NodeSnapshot:
    """In-memory snapshot of a single node's state."""

    node_id: str = ""
    server_url: str = ""
    started: float = 0.0
    last_seen: float = 0.0  # monotonic time of last successful poll
    max_ws: int = 10  # max workstreams (capacity)
    workstreams: dict[str, dict[str, Any]] = field(default_factory=dict)
    health: dict[str, Any] = field(default_factory=dict)
    aggregate: dict[str, Any] = field(default_factory=dict)
    reachable: bool = True


class ClusterCollector:
    """Aggregates cluster state from Redis and per-node HTTP APIs.

    Three daemon threads:
    1. Event subscriber — real-time state changes from {prefix}:events:cluster
    2. Node discovery — scans heartbeat keys every ``discovery_interval`` seconds
    3. Poll loop — fetches /api/dashboard from each node every ``poll_interval`` seconds
    """

    def __init__(
        self,
        broker: RedisBroker,
        prefix: str = "turnstone",
        poll_interval: float = 10.0,
        discovery_interval: float = 15.0,
        max_poll_workers: int = 50,
        http_timeout: float = 5.0,
        auth_token: str = "",
    ):
        self._broker = broker
        self._prefix = prefix
        self._poll_interval = poll_interval
        self._discovery_interval = discovery_interval
        self._max_poll_workers = max_poll_workers
        self._http_timeout = http_timeout

        self._lock = threading.Lock()
        self._nodes: dict[str, NodeSnapshot] = {}
        self._running = False
        self._threads: list[threading.Thread] = []
        self._poll_pool = ThreadPoolExecutor(max_workers=max_poll_workers)
        headers = {}
        if auth_token:
            headers["Authorization"] = f"Bearer {auth_token}"
        self._http_client = httpx.Client(timeout=http_timeout, headers=headers)

        # SSE fan-out to browser clients
        self._listeners: list[queue.Queue[dict[str, Any]]] = []
        self._listeners_lock = threading.Lock()

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Start background threads."""
        self._running = True
        for target, name in [
            (self._event_loop, "console-events"),
            (self._discovery_loop, "console-discovery"),
            (self._poll_loop, "console-poll"),
        ]:
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)
        log.info("ClusterCollector started")

    def stop(self) -> None:
        """Stop all threads and clean up resources."""
        self._running = False
        self._poll_pool.shutdown(wait=False)
        self._http_client.close()
        log.info("ClusterCollector stopped")

    # -- event subscription --------------------------------------------------

    def _event_loop(self) -> None:
        """Subscribe to cluster events for real-time updates."""
        while self._running:
            try:
                self._broker.subscribe_cluster(self._on_cluster_event)
                while self._running:
                    time.sleep(1)
            except Exception:
                log.exception("Cluster subscription error, reconnecting in 5s")
                time.sleep(5)

    def _on_cluster_event(self, raw: str) -> None:
        """Handle a cluster event from Redis pub/sub."""
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return

        etype = data.get("type", "")
        ws_id = data.get("ws_id", "")
        node_id = data.get("node_id", "")

        with self._lock:
            if etype == "cluster_state" and node_id in self._nodes:
                node = self._nodes[node_id]
                if ws_id in node.workstreams:
                    ws = node.workstreams[ws_id]
                    ws["state"] = data.get("state", ws.get("state", "idle"))
                    if "tokens" in data:
                        ws["tokens"] = data["tokens"]
                    if "context_ratio" in data:
                        ws["context_ratio"] = data["context_ratio"]
                    if "activity" in data:
                        ws["activity"] = data["activity"]
                    if "activity_state" in data:
                        ws["activity_state"] = data["activity_state"]

            elif etype == "ws_created" and node_id:
                if node_id in self._nodes:
                    node = self._nodes[node_id]
                    node.workstreams[ws_id] = {
                        "id": ws_id,
                        "name": data.get("name", ""),
                        "state": "idle",
                        "node": node_id,
                        "server_url": node.server_url,
                        "title": "",
                        "tokens": 0,
                        "context_ratio": 0.0,
                        "activity": "",
                        "activity_state": "",
                        "tool_calls": 0,
                    }

            elif etype == "ws_closed":
                for node in self._nodes.values():
                    node.workstreams.pop(ws_id, None)

            elif etype == "ws_rename":
                for node in self._nodes.values():
                    if ws_id in node.workstreams:
                        node.workstreams[ws_id]["name"] = data.get("name", "")

        # Fan out to SSE listeners
        self._fanout(data)

    def _fanout(self, event: dict[str, Any]) -> None:
        """Copy an event to all registered SSE listener queues."""
        with self._listeners_lock:
            for q in self._listeners:
                with contextlib.suppress(queue.Full):
                    q.put_nowait(event)

    # -- node discovery ------------------------------------------------------

    def _discovery_loop(self) -> None:
        """Periodically scan Redis for active nodes."""
        while self._running:
            try:
                self._discover_nodes()
            except Exception:
                log.exception("Node discovery error")
            time.sleep(self._discovery_interval)

    def _discover_nodes(self) -> None:
        """Scan heartbeat keys and update the node map."""
        active = self._broker.list_nodes()
        active_ids = set()
        pending_events = []
        with self._lock:
            for meta in active:
                nid = meta.get("node_id", "")
                if not nid:
                    continue
                active_ids.add(nid)
                if nid not in self._nodes:
                    self._nodes[nid] = NodeSnapshot(
                        node_id=nid,
                        server_url=meta.get("server_url", ""),
                        started=meta.get("started", 0.0),
                        max_ws=meta.get("max_ws", 10),
                    )
                    pending_events.append({"type": "node_joined", "node_id": nid})
                    log.info("Discovered node: %s", nid)
                else:
                    self._nodes[nid].server_url = meta.get(
                        "server_url", self._nodes[nid].server_url
                    )

            # Remove nodes whose heartbeats expired
            lost = [nid for nid in self._nodes if nid not in active_ids]
            for nid in lost:
                del self._nodes[nid]
                pending_events.append({"type": "node_lost", "node_id": nid})
                log.info("Lost node: %s", nid)
        for event in pending_events:
            self._fanout(event)

    # -- polling -------------------------------------------------------------

    def _poll_loop(self) -> None:
        """Periodically fetch /api/dashboard from each node."""
        while self._running:
            try:
                self._poll_all_nodes()
            except Exception:
                log.exception("Poll loop error")
            time.sleep(self._poll_interval)

    def _poll_all_nodes(self) -> None:
        """Fetch dashboard data from all known nodes in parallel."""
        with self._lock:
            targets = [
                (n.node_id, n.server_url)
                for n in self._nodes.values()
                if n.server_url and n.server_url.startswith("http")
            ]

        if not targets:
            return

        futures = {self._poll_pool.submit(self._fetch_node, nid, url): nid for nid, url in targets}
        for future in as_completed(futures):
            nid = futures[future]
            try:
                dashboard, health = future.result()
                self._apply_poll(nid, dashboard, health)
            except Exception:
                log.debug("Failed to poll node %s", nid)
                with self._lock:
                    if nid in self._nodes:
                        self._nodes[nid].reachable = False

    def _fetch_node(self, node_id: str, server_url: str) -> tuple[dict[str, Any], dict[str, Any]]:
        """Fetch /api/dashboard and /health from a single node."""
        base = server_url.rstrip("/")
        dash_resp = self._http_client.get(f"{base}/api/dashboard")
        dash_data: dict[str, Any] = dash_resp.json()
        try:
            health_resp = self._http_client.get(f"{base}/health")
            health_data: dict[str, Any] = health_resp.json()
        except Exception:
            health_data = {}
        return dash_data, health_data

    def _apply_poll(self, node_id: str, dashboard: dict[str, Any], health: dict[str, Any]) -> None:
        """Apply polled data to the in-memory node snapshot."""
        ws_list = dashboard.get("workstreams", [])
        aggregate = dashboard.get("aggregate", {})
        with self._lock:
            node = self._nodes.get(node_id)
            if not node:
                return
            node.last_seen = time.monotonic()
            node.reachable = True
            node.health = health
            node.aggregate = aggregate
            # Replace workstreams entirely from the authoritative poll
            node.workstreams = {}
            for ws in ws_list:
                ws["node"] = node_id
                ws["server_url"] = node.server_url
                node.workstreams[ws.get("id", "")] = ws

    # -- query methods (thread-safe) -----------------------------------------

    def get_overview(self) -> dict[str, Any]:
        """Return cluster overview: state counts, totals, aggregate stats."""
        states = {"running": 0, "thinking": 0, "attention": 0, "idle": 0, "error": 0}
        total_tokens = 0
        total_tool_calls = 0
        total_ws = 0
        versions: set[str] = set()
        with self._lock:
            for node in self._nodes.values():
                for ws in node.workstreams.values():
                    state = ws.get("state", "idle")
                    states[state] = states.get(state, 0) + 1
                    total_ws += 1
                total_tokens += node.aggregate.get("total_tokens", 0)
                total_tool_calls += node.aggregate.get("total_tool_calls", 0)
                ver = node.health.get("version", "")
                if ver:
                    versions.add(ver)
            node_count = len(self._nodes)
        return {
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
        self, sort_by: str = "activity", limit: int = 100, offset: int = 0
    ) -> tuple[list[dict[str, Any]], int]:
        """Return sorted, paginated node list with per-node counts."""
        with self._lock:
            items = []
            for node in self._nodes.values():
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
                        "health": node.health,
                        "version": node.health.get("version", ""),
                    }
                )
            total = len(items)

        # Sort
        if sort_by == "activity":
            items.sort(key=lambda n: n["ws_running"] + n["ws_attention"], reverse=True)
        elif sort_by == "tokens":
            items.sort(key=lambda n: n["total_tokens"], reverse=True)
        elif sort_by == "name":
            items.sort(key=lambda n: n["node_id"])

        return items[offset : offset + limit], total

    def get_workstreams(
        self,
        state: str | None = None,
        node: str | None = None,
        search: str | None = None,
        sort_by: str = "state",
        page: int = 1,
        per_page: int = 50,
    ) -> tuple[list[dict[str, Any]], int]:
        """Return filtered, sorted, paginated workstreams + total count."""
        with self._lock:
            all_ws = []
            for n in self._nodes.values():
                for ws in n.workstreams.values():
                    all_ws.append(dict(ws))

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
