"""Simulation scenarios — workload patterns for cluster testing."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Protocol

from turnstone.mq.broker import RedisBroker
from turnstone.mq.protocol import SendMessage
from turnstone.sim.config import SimConfig
from turnstone.sim.metrics import MetricsCollector

if TYPE_CHECKING:
    from turnstone.sim.cluster import SimCluster

log = logging.getLogger("turnstone.sim.scenario")


class Scenario(Protocol):
    async def run(
        self,
        cluster: SimCluster,
        config: SimConfig,
        metrics: MetricsCollector,
    ) -> None: ...


class SteadyStateScenario:
    """Inject messages at a constant rate for the configured duration."""

    async def run(
        self,
        cluster: SimCluster,
        config: SimConfig,
        metrics: MetricsCollector,
    ) -> None:
        broker = _make_broker(config)
        interval = 1.0 / max(0.01, config.messages_per_second)
        deadline = time.monotonic() + config.duration
        count = 0

        try:
            while time.monotonic() < deadline:
                count += 1
                msg = SendMessage(
                    message=f"Steady-state message {count}",
                    auto_approve=True,
                )
                broker.push_inbound(msg.to_json())
                metrics.record_inject()
                await asyncio.sleep(interval)
        finally:
            # Allow in-flight turns to finish
            await asyncio.sleep(min(10, config.llm_latency_mean * 3))
            broker.close()
            log.info("Steady-state scenario complete: %d messages injected", count)


class BurstScenario:
    """Inject burst_size messages as fast as possible, then wait."""

    async def run(
        self,
        cluster: SimCluster,
        config: SimConfig,
        metrics: MetricsCollector,
    ) -> None:
        broker = _make_broker(config)

        try:
            for i in range(config.burst_size):
                msg = SendMessage(
                    message=f"Burst message {i}",
                    auto_approve=True,
                )
                broker.push_inbound(msg.to_json())
                metrics.record_inject()

            log.info("Burst injected: %d messages", config.burst_size)
            # Wait for processing to complete
            await asyncio.sleep(config.duration)
        finally:
            broker.close()


class NodeFailureScenario:
    """Steady-state load with periodic node kills."""

    async def run(
        self,
        cluster: SimCluster,
        config: SimConfig,
        metrics: MetricsCollector,
    ) -> None:
        # Start steady injection in background
        steady = SteadyStateScenario()
        load_task = asyncio.create_task(steady.run(cluster, config, metrics))

        # Periodically kill nodes
        killed = 0
        max_kills = config.num_nodes // 2  # never kill more than half
        node_ids = list(cluster.nodes.keys())

        try:
            while killed < max_kills:
                await asyncio.sleep(config.node_kill_interval)
                for _ in range(config.node_kill_count):
                    if killed < len(node_ids):
                        await cluster.kill_node(node_ids[killed])
                        killed += 1
        finally:
            await load_task
            log.info("Node-failure scenario complete: %d nodes killed", killed)


class DirectedScenario:
    """Send messages targeted to specific nodes."""

    async def run(
        self,
        cluster: SimCluster,
        config: SimConfig,
        metrics: MetricsCollector,
    ) -> None:
        broker = _make_broker(config)
        node_ids = list(cluster.nodes.keys())
        count = min(config.burst_size, len(node_ids))

        try:
            for i in range(count):
                target = node_ids[i % len(node_ids)]
                msg = SendMessage(
                    message=f"Directed message to {target}",
                    auto_approve=True,
                    target_node=target,
                )
                broker.push_inbound(msg.to_json(), node_id=target)
                metrics.record_inject()

            log.info("Directed scenario: %d messages sent to specific nodes", count)
            await asyncio.sleep(config.duration)
        finally:
            broker.close()


class LifecycleScenario:
    """Create, use, and close workstreams across nodes."""

    async def run(
        self,
        cluster: SimCluster,
        config: SimConfig,
        metrics: MetricsCollector,
    ) -> None:
        from turnstone.mq.protocol import (
            CloseWorkstreamMessage,
            CreateWorkstreamMessage,
        )

        broker = _make_broker(config)
        ws_ids: list[str] = []

        try:
            # Phase 1: Create workstreams
            create_count = min(50, config.num_nodes * 2)
            for i in range(create_count):
                msg = CreateWorkstreamMessage(
                    name=f"lifecycle-ws-{i}",
                    auto_approve=True,
                )
                broker.push_inbound(msg.to_json())
                metrics.record_inject()
                await asyncio.sleep(0.05)

            # Let creations settle
            await asyncio.sleep(3)

            # Phase 2: Send messages to shared queue (will be routed to nodes
            # that own workstreams)
            for i in range(create_count):
                msg = SendMessage(
                    message=f"Lifecycle message {i}",
                    auto_approve=True,
                )
                broker.push_inbound(msg.to_json())
                metrics.record_inject()
                await asyncio.sleep(0.1)

            # Let turns complete
            await asyncio.sleep(min(15, config.llm_latency_mean * 5))

            # Phase 3: Close half the workstreams
            # Collect ws_ids from nodes
            for node in cluster.nodes.values():
                for ws_id in list(node._workstreams.keys()):
                    ws_ids.append(ws_id)

            close_count = len(ws_ids) // 2
            for ws_id in ws_ids[:close_count]:
                owner = broker.get_ws_owner(ws_id)
                msg = CloseWorkstreamMessage(ws_id=ws_id)
                broker.push_inbound(msg.to_json(), node_id=owner or "")
                await asyncio.sleep(0.05)

            await asyncio.sleep(2)
            log.info(
                "Lifecycle scenario complete: created %d, closed %d",
                create_count,
                close_count,
            )
        finally:
            broker.close()


def _make_broker(config: SimConfig) -> RedisBroker:
    """Create a RedisBroker for scenario message injection."""
    return RedisBroker(
        host=config.redis_host,
        port=config.redis_port,
        db=config.redis_db,
        prefix=config.prefix,
        password=config.redis_password,
    )


SCENARIOS: dict[str, type] = {
    "steady": SteadyStateScenario,
    "burst": BurstScenario,
    "node_failure": NodeFailureScenario,
    "directed": DirectedScenario,
    "lifecycle": LifecycleScenario,
}
