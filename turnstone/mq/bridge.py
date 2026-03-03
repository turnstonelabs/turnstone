"""Bridge service — connects message queues to turnstone-server's HTTP API.

The bridge (listener+speaker) reads commands from an inbound queue, drives
workstreams on the turnstone-server via HTTP, consumes SSE for progress, and
publishes events to outbound pub/sub channels.

Run as: ``turnstone-bridge --server-url http://localhost:8080``
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import socket
import threading
import time
import uuid
from typing import TYPE_CHECKING, Any

import httpx

from turnstone.mq.broker import RedisBroker
from turnstone.mq.protocol import (
    AckEvent,
    ApprovalRequestEvent,
    ClusterStateEvent,
    ContentEvent,
    ErrorEvent,
    HealthResponseEvent,
    InboundMessage,
    InfoEvent,
    NodeListEvent,
    OutboundEvent,
    PlanReviewEvent,
    ReasoningEvent,
    StateChangeEvent,
    StatusEvent,
    StreamEndEvent,
    ToolInfoEvent,
    ToolOutputChunkEvent,
    ToolResultEvent,
    TurnCompleteEvent,
    WorkstreamClosedEvent,
    WorkstreamCreatedEvent,
    WorkstreamListEvent,
    WorkstreamRenameEvent,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

log = logging.getLogger("turnstone.mq.bridge")

# Server's default safe tools (auto-approved without user confirmation)
DEFAULT_SAFE_TOOLS = frozenset(["read_file", "search", "man", "remember", "recall", "forget"])


def _default_node_id() -> str:
    """Generate a default node_id: ``{hostname}_{4hex}``, or a UUID on failure."""
    suffix = uuid.uuid4().hex[:4]
    try:
        host = socket.gethostname()
        if host and host != "localhost":
            return f"{host}_{suffix}"
    except OSError:
        pass
    return uuid.uuid4().hex[:12]


class Bridge:
    """Connects a message broker to turnstone-server's HTTP API.

    Threading model::

        Main Thread: Inbound loop (BLPOP on broker)
        Global SSE Thread: GET /api/events/global
        Per-WS SSE Thread × N: GET /api/events?ws_id=X
    """

    def __init__(
        self,
        server_url: str = "http://localhost:8080",
        broker: RedisBroker | None = None,
        approval_timeout: float = 300,
        prefix: str = "turnstone",
        node_id: str = "",
        heartbeat_ttl: int = 60,
        auth_token: str = "",
    ) -> None:
        self._server_url = server_url.rstrip("/")
        self._broker = broker or RedisBroker()
        self._approval_timeout = approval_timeout
        self._prefix = prefix
        self._node_id = node_id or _default_node_id()
        self._heartbeat_ttl = heartbeat_ttl
        self._started_at = time.time()
        self._auth_token = auth_token

        # Shared httpx client for short-lived POST requests (main thread only)
        headers: dict[str, str] = {}
        if auth_token:
            headers["Authorization"] = f"Bearer {auth_token}"
        self._http = httpx.Client(base_url=self._server_url, timeout=30, headers=headers)

        # Protected by _lock — accessed from main, global SSE, and per-ws SSE threads
        self._lock = threading.Lock()
        self._ws_threads: dict[str, threading.Thread] = {}
        self._ws_auto_approve: dict[str, bool] = {}
        self._ws_approve_tools: dict[str, set[str]] = {}
        self._active_sends: dict[str, str] = {}  # ws_id → correlation_id
        self._running = True

    # -- public entry point --------------------------------------------------

    def run(self) -> None:
        """Block until shutdown (KeyboardInterrupt)."""
        log.info("Bridge starting — node=%s server=%s", self._node_id, self._server_url)
        self._recover_workstreams()

        heartbeat_t = threading.Thread(target=self._heartbeat_loop, daemon=True)
        heartbeat_t.start()

        global_t = threading.Thread(target=self._global_sse_loop, daemon=True)
        global_t.start()

        try:
            self._inbound_loop()
        except KeyboardInterrupt:
            log.info("Bridge shutting down")
        finally:
            self._running = False
            self._http.close()
            self._broker.close()

    # -- recovery ------------------------------------------------------------

    def _recover_workstreams(self) -> None:
        """Discover active workstreams on startup and register ownership."""
        try:
            resp = self._http.get("/api/workstreams")
            data = resp.json()
            for ws in data.get("workstreams", []):
                ws_id = ws["id"]
                log.info("Recovered workstream %s (%s)", ws_id, ws.get("name", ""))
                self._broker.set_ws_owner(ws_id, self._node_id)
                self._start_ws_sse(ws_id)
        except Exception as exc:
            log.warning("Could not recover workstreams: %s", exc)

    # -- inbound loop --------------------------------------------------------

    def _inbound_loop(self) -> None:
        while self._running:
            raw = self._broker.pop_inbound(timeout=5, node_id=self._node_id)
            if raw is None:
                continue
            try:
                msg = InboundMessage.from_json(raw)
                self._dispatch(msg)
            except Exception as exc:
                log.error("Failed to process inbound message: %s", exc)
                self._publish_global(ErrorEvent(message=f"Failed to process message: {exc}"))

    def _dispatch(self, msg: InboundMessage) -> None:
        # Messages that need routing (have ws_id or target_node)
        routed_handlers = {
            "send": self._handle_send,
            "command": self._handle_command,
            "create_workstream": self._handle_create_ws,
            "close_workstream": self._handle_close_ws,
        }
        # Messages that are always local (no routing needed)
        local_handlers = {
            "approve": self._handle_approve,
            "plan_feedback": self._handle_plan_feedback,
            "list_workstreams": self._handle_list_ws,
            "health": self._handle_health,
            "list_nodes": self._handle_list_nodes,
        }

        if msg.type in routed_handlers:
            self._route_or_process(msg, routed_handlers[msg.type])
        elif msg.type in local_handlers:
            local_handlers[msg.type](msg)
        else:
            self._publish_global(
                ErrorEvent(
                    correlation_id=msg.correlation_id,
                    message=f"Unknown message type: {msg.type!r}",
                )
            )

    def _route_or_process(
        self, msg: InboundMessage, handler: Callable[[InboundMessage], None]
    ) -> None:
        """Route a message to the correct node, or process locally."""
        target = getattr(msg, "target_node", "")
        ws_id = getattr(msg, "ws_id", "")

        # Directed to a different node?
        if target and target != self._node_id:
            log.debug("Routing to node %s: %s", target, msg.type)
            self._broker.push_inbound(msg.to_json(), node_id=target)
            return

        # Existing workstream owned by another node?
        if ws_id:
            owner = self._broker.get_ws_owner(ws_id)
            if owner and owner != self._node_id:
                log.debug("Re-routing to owner %s for ws %s", owner, ws_id)
                self._broker.push_inbound(msg.to_json(), node_id=owner)
                return

        handler(msg)

    # -- handlers ------------------------------------------------------------

    def _handle_send(self, msg: InboundMessage) -> None:
        ws_id = getattr(msg, "ws_id", "")
        message = getattr(msg, "message", "")
        auto_approve = getattr(msg, "auto_approve", False)
        auto_approve_tools = getattr(msg, "auto_approve_tools", [])
        name = getattr(msg, "name", "")

        # Auto-create workstream if needed
        if not ws_id:
            ws_id = self._create_ws_on_server(
                name=name,
                auto_approve=auto_approve,
                auto_approve_tools=auto_approve_tools,
                correlation_id=msg.correlation_id,
            )
            if not ws_id:
                return  # error already published
        else:
            # Update approval settings for existing workstream
            with self._lock:
                if auto_approve:
                    self._ws_auto_approve[ws_id] = True
                if auto_approve_tools:
                    self._ws_approve_tools[ws_id] = set(auto_approve_tools)

        with self._lock:
            self._active_sends[ws_id] = msg.correlation_id

        resp = self._http.post("/api/send", json={"message": message, "ws_id": ws_id})
        data = resp.json()

        self._publish_ws(
            ws_id,
            AckEvent(
                ws_id=ws_id,
                correlation_id=msg.correlation_id,
                status="ok" if data.get("status") == "ok" else "error",
                detail=data.get("error", ""),
            ),
        )

    def _handle_approve(self, msg: InboundMessage) -> None:
        request_id = getattr(msg, "request_id", "")
        if request_id:
            self._broker.push_response(request_id, msg.to_json())

    def _handle_plan_feedback(self, msg: InboundMessage) -> None:
        request_id = getattr(msg, "request_id", "")
        if request_id:
            self._broker.push_response(request_id, msg.to_json())

    def _handle_command(self, msg: InboundMessage) -> None:
        ws_id = getattr(msg, "ws_id", "")
        command = getattr(msg, "command", "")
        resp = self._http.post("/api/command", json={"command": command, "ws_id": ws_id})
        data = resp.json()
        self._publish_ws(
            ws_id,
            AckEvent(
                ws_id=ws_id,
                correlation_id=msg.correlation_id,
                status="ok" if data.get("status") == "ok" else "error",
                detail=data.get("error", ""),
            ),
        )

    def _handle_create_ws(self, msg: InboundMessage) -> None:
        name = getattr(msg, "name", "")
        auto_approve = getattr(msg, "auto_approve", False)
        auto_approve_tools = getattr(msg, "auto_approve_tools", [])
        model = getattr(msg, "model", "")
        self._create_ws_on_server(
            name=name,
            auto_approve=auto_approve,
            auto_approve_tools=auto_approve_tools,
            correlation_id=msg.correlation_id,
            model=model,
        )

    def _handle_close_ws(self, msg: InboundMessage) -> None:
        ws_id = getattr(msg, "ws_id", "")
        resp = self._http.post("/api/workstreams/close", json={"ws_id": ws_id})
        data = resp.json()
        self._publish_ws(
            ws_id,
            AckEvent(
                ws_id=ws_id,
                correlation_id=msg.correlation_id,
                status="ok" if data.get("status") == "ok" else "error",
                detail=data.get("error", ""),
            ),
        )

    def _handle_list_ws(self, msg: InboundMessage) -> None:
        resp = self._http.get("/api/workstreams")
        data = resp.json()
        self._publish_global(
            WorkstreamListEvent(
                correlation_id=msg.correlation_id,
                workstreams=data.get("workstreams", []),
            )
        )

    def _handle_health(self, msg: InboundMessage) -> None:
        resp = self._http.get("/health")
        data = resp.json()
        self._publish_global(
            HealthResponseEvent(
                correlation_id=msg.correlation_id,
                data=data,
            )
        )

    # -- workstream creation helper ------------------------------------------

    def _create_ws_on_server(
        self,
        name: str,
        auto_approve: bool,
        auto_approve_tools: list[str],
        correlation_id: str,
        model: str = "",
    ) -> str:
        """Create a workstream on the server.  Returns ws_id or empty on error."""
        try:
            payload: dict[str, Any] = {"name": name, "auto_approve": auto_approve}
            if model:
                payload["model"] = model
            resp = self._http.post(
                "/api/workstreams/new",
                json=payload,
            )
            data = resp.json()
            if "error" in data:
                self._publish_global(
                    AckEvent(
                        correlation_id=correlation_id,
                        status="error",
                        detail=data["error"],
                    )
                )
                return ""
            ws_id: str = data["ws_id"]
            ws_name = data.get("name", "")

            self._broker.set_ws_owner(ws_id, self._node_id)

            with self._lock:
                if auto_approve:
                    self._ws_auto_approve[ws_id] = True
                if auto_approve_tools:
                    self._ws_approve_tools[ws_id] = set(auto_approve_tools)

            self._start_ws_sse(ws_id)

            self._publish_global(
                WorkstreamCreatedEvent(
                    ws_id=ws_id,
                    name=ws_name,
                    correlation_id=correlation_id,
                )
            )
            self._publish_cluster(
                WorkstreamCreatedEvent(
                    ws_id=ws_id,
                    name=ws_name,
                    correlation_id=correlation_id,
                )
            )
            return ws_id
        except Exception as exc:
            self._publish_global(
                AckEvent(
                    correlation_id=correlation_id,
                    status="error",
                    detail=str(exc),
                )
            )
            return ""

    # -- SSE consumption -----------------------------------------------------

    def _start_ws_sse(self, ws_id: str) -> None:
        with self._lock:
            if ws_id in self._ws_threads and self._ws_threads[ws_id].is_alive():
                return
            t = threading.Thread(target=self._ws_sse_loop, args=(ws_id,), daemon=True)
            self._ws_threads[ws_id] = t
            t.start()

    def _ws_sse_loop(self, ws_id: str) -> None:
        """Consume per-workstream SSE and forward events."""
        # Each SSE thread gets its own httpx client (not thread-safe to share)
        sse_headers: dict[str, str] = {}
        if self._auth_token:
            sse_headers["Authorization"] = f"Bearer {self._auth_token}"
        with httpx.Client(
            base_url=self._server_url, timeout=None, headers=sse_headers
        ) as sse_client:
            while self._running:
                try:
                    with sse_client.stream("GET", f"/api/events?ws_id={ws_id}") as resp:
                        for data in _iter_sse_data(resp):
                            if not self._running:
                                break
                            self._handle_ws_event(ws_id, data)
                except Exception as exc:
                    if self._running:
                        log.debug("WS SSE reconnecting (%s): %s", ws_id, exc)
                        time.sleep(2)

    def _handle_ws_event(self, ws_id: str, data: dict[str, Any]) -> None:
        etype = data.get("type", "")

        if etype == "content":
            self._publish_ws(ws_id, ContentEvent(ws_id=ws_id, text=data.get("text", "")))
        elif etype == "reasoning":
            self._publish_ws(ws_id, ReasoningEvent(ws_id=ws_id, text=data.get("text", "")))
        elif etype == "tool_info":
            self._publish_ws(ws_id, ToolInfoEvent(ws_id=ws_id, items=data.get("items", [])))
        elif etype == "approve_request":
            self._handle_approval(ws_id, data)
        elif etype == "plan_review":
            self._handle_plan_review(ws_id, data)
        elif etype == "tool_output_chunk":
            self._publish_ws(
                ws_id,
                ToolOutputChunkEvent(
                    ws_id=ws_id,
                    call_id=data.get("call_id", ""),
                    chunk=data.get("chunk", ""),
                ),
            )
        elif etype == "tool_result":
            self._publish_ws(
                ws_id,
                ToolResultEvent(
                    ws_id=ws_id,
                    call_id=data.get("call_id", ""),
                    name=data.get("name", ""),
                    output=data.get("output", ""),
                ),
            )
        elif etype == "status":
            self._publish_ws(
                ws_id,
                StatusEvent(
                    ws_id=ws_id,
                    prompt_tokens=data.get("prompt_tokens", 0),
                    completion_tokens=data.get("completion_tokens", 0),
                    total_tokens=data.get("total_tokens", 0),
                    context_window=data.get("context_window", 0),
                    pct=data.get("pct", 0),
                    effort=data.get("effort", ""),
                ),
            )
        elif etype == "error":
            self._publish_ws(ws_id, ErrorEvent(ws_id=ws_id, message=data.get("message", "")))
        elif etype == "info":
            self._publish_ws(ws_id, InfoEvent(ws_id=ws_id, message=data.get("message", "")))
        elif etype == "stream_end":
            self._publish_ws(ws_id, StreamEndEvent(ws_id=ws_id))

    def _handle_approval(self, ws_id: str, data: dict[str, Any]) -> None:
        """Handle an approval request — auto-approve or forward to client."""
        items = data.get("items", [])

        # Check if all tools can be auto-approved
        with self._lock:
            if self._ws_auto_approve.get(ws_id):
                self._api_approve(ws_id, approved=True)
                return
            approve_set = self._ws_approve_tools.get(ws_id, DEFAULT_SAFE_TOOLS)

        tool_names = {it.get("func_name", "") for it in items if it.get("needs_approval")}

        if tool_names and tool_names.issubset(approve_set):
            self._api_approve(ws_id, approved=True)
            return

        # Forward to client — spawn a thread so we don't block SSE consumption
        request_id = uuid.uuid4().hex[:12]
        self._publish_ws(
            ws_id,
            ApprovalRequestEvent(
                ws_id=ws_id,
                correlation_id=request_id,
                items=items,
            ),
        )

        def _wait_approval() -> None:
            raw_resp = self._broker.pop_response(request_id, timeout=self._approval_timeout)
            if raw_resp:
                resp_msg = InboundMessage.from_json(raw_resp)
                approved = getattr(resp_msg, "approved", False)
                feedback = getattr(resp_msg, "feedback", None)
                always = getattr(resp_msg, "always", False)
                self._api_approve(ws_id, approved=approved, feedback=feedback)
                if always:
                    with self._lock:
                        self._ws_auto_approve[ws_id] = True
            else:
                log.warning("Approval timeout for ws %s — denying", ws_id)
                self._api_approve(ws_id, approved=False, feedback="Approval timed out")

        threading.Thread(target=_wait_approval, daemon=True).start()

    def _handle_plan_review(self, ws_id: str, data: dict[str, Any]) -> None:
        """Handle a plan review request — auto-approve or forward to client."""
        with self._lock:
            if self._ws_auto_approve.get(ws_id):
                self._http.post("/api/plan", json={"feedback": "", "ws_id": ws_id})
                return

        request_id = uuid.uuid4().hex[:12]
        self._publish_ws(
            ws_id,
            PlanReviewEvent(
                ws_id=ws_id,
                correlation_id=request_id,
                content=data.get("content", ""),
            ),
        )

        def _wait_plan() -> None:
            raw_resp = self._broker.pop_response(request_id, timeout=self._approval_timeout)
            if raw_resp:
                resp_msg = InboundMessage.from_json(raw_resp)
                feedback = getattr(resp_msg, "feedback", "")
                self._http.post("/api/plan", json={"feedback": feedback, "ws_id": ws_id})
            else:
                log.warning("Plan review timeout for ws %s — rejecting", ws_id)
                self._http.post("/api/plan", json={"feedback": "reject", "ws_id": ws_id})

        threading.Thread(target=_wait_plan, daemon=True).start()

    def _api_approve(
        self,
        ws_id: str,
        approved: bool,
        feedback: str | None = None,
    ) -> None:
        body: dict[str, Any] = {"approved": approved, "ws_id": ws_id}
        if feedback:
            body["feedback"] = feedback
        self._http.post("/api/approve", json=body)

    # -- global SSE ----------------------------------------------------------

    def _global_sse_loop(self) -> None:
        # Own httpx client for the long-lived SSE connection
        sse_headers: dict[str, str] = {}
        if self._auth_token:
            sse_headers["Authorization"] = f"Bearer {self._auth_token}"
        with httpx.Client(
            base_url=self._server_url, timeout=None, headers=sse_headers
        ) as sse_client:
            while self._running:
                try:
                    with sse_client.stream("GET", "/api/events/global") as resp:
                        for data in _iter_sse_data(resp):
                            if not self._running:
                                break
                            self._handle_global_event(data)
                except Exception as exc:
                    if self._running:
                        log.debug("Global SSE reconnecting: %s", exc)
                        time.sleep(2)

    def _handle_global_event(self, data: dict[str, Any]) -> None:
        etype = data.get("type", "")
        ws_id = data.get("ws_id", "")

        if etype == "ws_state":
            state = data.get("state", "")
            self._publish_ws(ws_id, StateChangeEvent(ws_id=ws_id, state=state))
            self._publish_global(StateChangeEvent(ws_id=ws_id, state=state))
            self._publish_cluster(
                ClusterStateEvent(
                    ws_id=ws_id,
                    state=state,
                    node_id=self._node_id,
                    tokens=data.get("tokens", 0),
                    context_ratio=data.get("context_ratio", 0.0),
                    activity=data.get("activity", ""),
                    activity_state=data.get("activity_state", ""),
                )
            )

            # Completion detection
            if state == "idle":
                with self._lock:
                    cid = self._active_sends.pop(ws_id, None)
                if cid:
                    self._publish_ws(ws_id, TurnCompleteEvent(ws_id=ws_id, correlation_id=cid))

        elif etype == "ws_rename":
            self._publish_global(WorkstreamRenameEvent(ws_id=ws_id, name=data.get("name", "")))
            self._publish_cluster(WorkstreamRenameEvent(ws_id=ws_id, name=data.get("name", "")))

        elif etype == "ws_closed":
            self._publish_global(WorkstreamClosedEvent(ws_id=ws_id))
            self._publish_cluster(WorkstreamClosedEvent(ws_id=ws_id))
            self._broker.del_ws_owner(ws_id)
            with self._lock:
                self._ws_threads.pop(ws_id, None)
                self._ws_auto_approve.pop(ws_id, None)
                self._ws_approve_tools.pop(ws_id, None)
                self._active_sends.pop(ws_id, None)

    # -- heartbeat -----------------------------------------------------------

    def _heartbeat_loop(self) -> None:
        """Periodically register this node in the broker."""
        while self._running:
            self._broker.register_node(
                self._node_id,
                {"server_url": self._server_url, "started": self._started_at},
                ttl=self._heartbeat_ttl,
            )
            time.sleep(self._heartbeat_ttl / 2)

    # -- node listing --------------------------------------------------------

    def _handle_list_nodes(self, msg: InboundMessage) -> None:
        nodes = self._broker.list_nodes()
        self._publish_global(NodeListEvent(correlation_id=msg.correlation_id, nodes=nodes))

    # -- publish helpers -----------------------------------------------------

    def _publish_ws(self, ws_id: str, event: OutboundEvent) -> None:
        channel = f"{self._prefix}:events:{ws_id}"
        self._broker.publish_outbound(channel, event.to_json())

    def _publish_global(self, event: OutboundEvent) -> None:
        self._broker.publish_outbound(f"{self._prefix}:events:global", event.to_json())

    def _publish_cluster(self, event: OutboundEvent) -> None:
        self._broker.publish_outbound(f"{self._prefix}:events:cluster", event.to_json())


# ---------------------------------------------------------------------------
# SSE parsing helper
# ---------------------------------------------------------------------------


def _iter_sse_data(resp: httpx.Response) -> Iterator[dict[str, Any]]:
    """Yield parsed JSON dicts from an SSE stream."""
    for line in resp.iter_lines():
        if line.startswith("data: "):
            with contextlib.suppress(json.JSONDecodeError):
                data: dict[str, Any] = json.loads(line[6:])
                yield data
        # SSE keepalive comments (lines starting with ':') are ignored


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="turnstone message queue bridge — connects Redis queues to turnstone-server"
    )
    parser.add_argument(
        "--server-url",
        default="http://localhost:8080",
        help="turnstone-server URL (default: %(default)s)",
    )
    parser.add_argument(
        "--redis-host", default="localhost", help="Redis host (default: %(default)s)"
    )
    parser.add_argument(
        "--redis-port", type=int, default=6379, help="Redis port (default: %(default)s)"
    )
    parser.add_argument(
        "--redis-password",
        default=os.environ.get("REDIS_PASSWORD"),
        help="Redis password (default: $REDIS_PASSWORD)",
    )
    parser.add_argument(
        "--redis-db", type=int, default=0, help="Redis DB number (default: %(default)s)"
    )
    parser.add_argument(
        "--approval-timeout",
        type=float,
        default=300,
        help="Seconds to wait for approval responses (default: %(default)s)",
    )
    parser.add_argument(
        "--node-id",
        default="",
        help="Node identifier for multi-node routing (default: hostname)",
    )
    parser.add_argument(
        "--heartbeat-ttl",
        type=int,
        default=60,
        help="Heartbeat TTL in seconds (default: %(default)s)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log level (default: %(default)s)",
    )
    parser.add_argument(
        "--auth-token",
        default=os.environ.get("TURNSTONE_AUTH_TOKEN", ""),
        help="Bearer token for authenticating to turnstone-server (default: $TURNSTONE_AUTH_TOKEN)",
    )
    from turnstone.core.config import apply_config

    apply_config(parser, ["bridge", "redis", "auth"])
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    broker = RedisBroker(
        host=args.redis_host,
        port=args.redis_port,
        db=args.redis_db,
        password=args.redis_password,
    )
    bridge = Bridge(
        server_url=args.server_url,
        broker=broker,
        approval_timeout=args.approval_timeout,
        node_id=args.node_id,
        heartbeat_ttl=args.heartbeat_ttl,
        auth_token=args.auth_token,
    )
    bridge.run()


if __name__ == "__main__":
    main()
