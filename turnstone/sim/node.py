"""Simulated turnstone node.

A SimNode replaces Bridge + Server + ChatSession with a lightweight async
coroutine that talks directly to Redis via the real RedisBroker.  External
observers (TurnstoneClient, turnstone-console) see identical protocol
behaviour.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import TYPE_CHECKING

from turnstone.mq.protocol import (
    AckEvent,
    ClusterStateEvent,
    ContentEvent,
    ErrorEvent,
    HealthResponseEvent,
    InboundMessage,
    NodeListEvent,
    OutboundEvent,
    StateChangeEvent,
    StatusEvent,
    StreamEndEvent,
    ToolResultEvent,
    TurnCompleteEvent,
    WorkstreamClosedEvent,
    WorkstreamCreatedEvent,
    WorkstreamListEvent,
)
from turnstone.sim.engine import SimEngine, ToolSimulationError

if TYPE_CHECKING:
    from turnstone.mq.broker import RedisBroker
    from turnstone.sim.config import SimConfig
    from turnstone.sim.metrics import MetricsCollector

log = logging.getLogger("turnstone.sim.node")


class SimWorkstream:
    """Lightweight workstream state machine."""

    def __init__(
        self,
        ws_id: str,
        name: str,
        node: SimNode,
        engine: SimEngine,
        config: SimConfig,
    ):
        self.ws_id = ws_id
        self.name = name
        self.state = "idle"
        self._node = node
        self._engine = engine
        self._config = config
        self._turn_count = 0
        self._total_tokens = 0  # accumulated across turns

    async def process_turn(self, message: str, correlation_id: str) -> None:
        """Simulate a complete turn: LLM stream -> optional tools -> final."""
        t_start = time.monotonic()
        self._turn_count += 1

        try:
            rounds = 0
            while True:
                # LLM thinking + streaming
                self._set_state("thinking", correlation_id)
                content, tool_calls = await self._engine.simulate_llm_response(
                    rounds == 0,
                )
                await self._stream_content(content, correlation_id)

                if not tool_calls or rounds >= self._config.max_tool_rounds:
                    break

                # Tool execution
                self._set_state("running", correlation_id)
                for tc in tool_calls:
                    name = tc["name"]
                    try:
                        output = await self._engine.simulate_tool_execution(name)
                    except ToolSimulationError as exc:
                        output = f"Error: {exc}"
                        self._node._metrics.record_error(
                            self._node.node_id,
                            str(exc),
                        )
                    self._node._publish_ws(
                        self.ws_id,
                        ToolResultEvent(
                            ws_id=self.ws_id,
                            correlation_id=correlation_id,
                            name=name,
                            output=output,
                        ),
                    )

                rounds += 1

            # Finished — publish status, idle, turn complete
            self._publish_status(correlation_id)
            self._set_state("idle", correlation_id)
            self._node._publish_ws(
                self.ws_id,
                TurnCompleteEvent(
                    ws_id=self.ws_id,
                    correlation_id=correlation_id,
                ),
            )
            self._node._publish_global(
                TurnCompleteEvent(
                    ws_id=self.ws_id,
                    correlation_id=correlation_id,
                ),
            )

        except Exception as exc:
            self._set_state("error", correlation_id)
            self._node._publish_ws(
                self.ws_id,
                ErrorEvent(
                    ws_id=self.ws_id,
                    correlation_id=correlation_id,
                    message=str(exc),
                ),
            )
            self._node._metrics.record_error(self._node.node_id, str(exc))

        finally:
            latency = time.monotonic() - t_start
            self._node._metrics.record_turn(
                self.ws_id,
                self._node.node_id,
                latency,
            )

    async def _stream_content(self, text: str, correlation_id: str) -> None:
        """Simulate token-by-token streaming."""
        if not text:
            return
        # Count tokens (~1 token per word) and accumulate
        self._total_tokens += len(text.split())
        chunk_size = max(1, len(text) // 8)
        token_delay = 1.0 / max(1, self._config.llm_token_rate)
        for i in range(0, len(text), chunk_size):
            chunk = text[i : i + chunk_size]
            self._node._publish_ws(
                self.ws_id,
                ContentEvent(
                    ws_id=self.ws_id,
                    correlation_id=correlation_id,
                    text=chunk,
                ),
            )
            await asyncio.sleep(token_delay * len(chunk.split()))
        self._node._publish_ws(
            self.ws_id,
            StreamEndEvent(ws_id=self.ws_id, correlation_id=correlation_id),
        )

    def _set_state(self, state: str, correlation_id: str) -> None:
        self.state = state
        self._node._publish_global(
            StateChangeEvent(
                ws_id=self.ws_id,
                correlation_id=correlation_id,
                state=state,
            ),
        )
        # prompt tokens ~= 2x completion tokens for a realistic ratio
        total = self._total_tokens * 3
        ctx_ratio = round(total / self._config.context_window, 3) if total else 0.0
        self._node._publish_cluster(
            ClusterStateEvent(
                ws_id=self.ws_id,
                state=state,
                node_id=self._node.node_id,
                tokens=total,
                context_ratio=ctx_ratio,
            ),
        )

    def _publish_status(self, correlation_id: str) -> None:
        total = self._total_tokens * 3  # prompt ~= 2x completion
        cw = self._config.context_window
        self._node._publish_ws(
            self.ws_id,
            StatusEvent(
                ws_id=self.ws_id,
                correlation_id=correlation_id,
                prompt_tokens=self._total_tokens * 2,
                completion_tokens=self._total_tokens,
                total_tokens=total,
                context_window=cw,
                pct=round(total / cw, 3) if cw else 0,
                effort="medium",
            ),
        )


class SimNode:
    """A lightweight simulated turnstone node.

    Replaces Bridge + Server + ChatSession with direct Redis protocol
    interaction.
    """

    def __init__(
        self,
        node_id: str,
        broker: RedisBroker,
        config: SimConfig,
        metrics: MetricsCollector,
    ):
        self.node_id = node_id
        self._broker = broker
        self._config = config
        self._metrics = metrics
        # Derive per-node seed so each node has unique RNG sequences
        import random

        node_seed = None
        if config.seed is not None:
            node_seed = hash((config.seed, node_id))
        self._engine = SimEngine(config, rng=random.Random(node_seed))
        self._workstreams: dict[str, SimWorkstream] = {}
        self._running = True
        self._started_at = time.time()
        self._prefix = config.prefix

    @property
    def workstream_count(self) -> int:
        return len(self._workstreams)

    # -- message handling ----------------------------------------------------

    async def handle_message(self, raw: str) -> None:
        """Parse and dispatch an inbound message."""
        try:
            msg = InboundMessage.from_json(raw)
            await self._dispatch(msg)
        except Exception as exc:
            log.error("SimNode %s dispatch error: %s", self.node_id, exc)
            self._publish_global(ErrorEvent(message=f"SimNode error: {exc}"))

    async def _dispatch(self, msg: InboundMessage) -> None:
        handlers = {
            "send": self._handle_send,
            "create_workstream": self._handle_create_ws,
            "close_workstream": self._handle_close_ws,
            "list_workstreams": self._handle_list_ws,
            "health": self._handle_health,
            "list_nodes": self._handle_list_nodes,
        }
        handler = handlers.get(msg.type)
        if handler:
            await handler(msg)
        else:
            log.debug("SimNode %s ignoring message type: %s", self.node_id, msg.type)

    async def _handle_send(self, msg: InboundMessage) -> None:
        ws_id = getattr(msg, "ws_id", "")
        message = getattr(msg, "message", "")
        cid = msg.correlation_id

        # Find or create workstream
        if ws_id and ws_id in self._workstreams:
            ws = self._workstreams[ws_id]
        elif len(self._workstreams) >= self._config.max_ws_per_node:
            self._publish_global(
                ErrorEvent(
                    correlation_id=cid,
                    message=f"Node {self.node_id} at capacity ({self._config.max_ws_per_node} ws)",
                ),
            )
            return
        else:
            ws = self._create_workstream(
                name=getattr(msg, "name", ""),
                correlation_id=cid,
            )

        self._publish_ws(
            ws.ws_id,
            AckEvent(ws_id=ws.ws_id, correlation_id=cid, status="ok"),
        )
        await ws.process_turn(message, cid)

    async def _handle_create_ws(self, msg: InboundMessage) -> None:
        if len(self._workstreams) >= self._config.max_ws_per_node:
            self._publish_global(
                ErrorEvent(
                    correlation_id=msg.correlation_id,
                    message=f"Node {self.node_id} at capacity ({self._config.max_ws_per_node} ws)",
                ),
            )
            return
        name = getattr(msg, "name", "")
        ws = self._create_workstream(name=name, correlation_id=msg.correlation_id)
        self._publish_ws(
            ws.ws_id,
            AckEvent(ws_id=ws.ws_id, correlation_id=msg.correlation_id, status="ok"),
        )

    async def _handle_close_ws(self, msg: InboundMessage) -> None:
        ws_id = getattr(msg, "ws_id", "")
        ws = self._workstreams.pop(ws_id, None)
        if ws:
            self._broker.del_ws_owner(ws_id)
            event = WorkstreamClosedEvent(
                ws_id=ws_id,
                correlation_id=msg.correlation_id,
            )
            self._publish_global(event)
            self._publish_cluster(event)

    async def _handle_list_ws(self, msg: InboundMessage) -> None:
        ws_list = [
            {"id": ws.ws_id, "name": ws.name, "state": ws.state}
            for ws in self._workstreams.values()
        ]
        self._publish_global(
            WorkstreamListEvent(
                correlation_id=msg.correlation_id,
                workstreams=ws_list,
            ),
        )

    async def _handle_health(self, msg: InboundMessage) -> None:
        self._publish_global(
            HealthResponseEvent(
                correlation_id=msg.correlation_id,
                data={
                    "status": "ok",
                    "node_id": self.node_id,
                    "sim": True,
                    "workstreams": len(self._workstreams),
                },
            ),
        )

    async def _handle_list_nodes(self, msg: InboundMessage) -> None:
        nodes = self._broker.list_nodes()
        self._publish_global(
            NodeListEvent(correlation_id=msg.correlation_id, nodes=nodes),
        )

    # -- workstream lifecycle ------------------------------------------------

    def _create_workstream(
        self,
        name: str = "",
        correlation_id: str = "",
    ) -> SimWorkstream:
        ws_id = uuid.uuid4().hex[:8]
        if not name:
            name = f"sim-ws-{ws_id[:4]}"
        ws = SimWorkstream(ws_id, name, self, self._engine, self._config)
        self._workstreams[ws_id] = ws
        self._broker.set_ws_owner(ws_id, self.node_id)
        event = WorkstreamCreatedEvent(
            ws_id=ws_id,
            correlation_id=correlation_id,
            name=name,
        )
        self._publish_global(event)
        # Also publish to cluster channel so the console discovers the ws.
        # Include node_id (the console collector keys on it).
        self._publish_cluster(
            ClusterStateEvent(
                ws_id=ws_id,
                state="idle",
                node_id=self.node_id,
            ),
        )
        # The cluster channel expects a ws_created with node_id for the
        # collector's _on_cluster_event handler.
        self._broker.publish_outbound(
            f"{self._prefix}:events:cluster",
            json.dumps(
                {
                    "type": "ws_created",
                    "ws_id": ws_id,
                    "name": name,
                    "node_id": self.node_id,
                    "correlation_id": correlation_id,
                }
            ),
        )
        return ws

    # -- heartbeat -----------------------------------------------------------

    def heartbeat_once(self) -> None:
        """Register a single heartbeat with the broker."""
        self._broker.register_node(
            self.node_id,
            {
                "server_url": f"sim://{self.node_id}",
                "started": self._started_at,
                "sim": True,
                "workstreams": len(self._workstreams),
                "max_ws": self._config.max_ws_per_node,
            },
            ttl=self._config.heartbeat_ttl,
        )

    # -- shutdown ------------------------------------------------------------

    def stop(self) -> None:
        """Mark node as stopped and clean up ownership keys."""
        self._running = False
        for ws_id in list(self._workstreams):
            self._broker.del_ws_owner(ws_id)
        self._workstreams.clear()

    # -- event publishing helpers --------------------------------------------

    def _publish_global(self, event: OutboundEvent) -> None:
        self._broker.publish_outbound(
            f"{self._prefix}:events:global",
            event.to_json(),
        )

    def _publish_ws(self, ws_id: str, event: OutboundEvent) -> None:
        self._broker.publish_outbound(
            f"{self._prefix}:events:{ws_id}",
            event.to_json(),
        )

    def _publish_cluster(self, event: OutboundEvent) -> None:
        self._broker.publish_outbound(
            f"{self._prefix}:events:cluster",
            event.to_json(),
        )
