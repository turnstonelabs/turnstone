"""Cluster orchestration — manages N SimNodes, dispatchers, and metrics."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from concurrent.futures import ThreadPoolExecutor

import redis

from turnstone.mq.broker import RedisBroker
from turnstone.sim.config import SimConfig
from turnstone.sim.metrics import MetricsCollector
from turnstone.sim.node import SimNode

log = logging.getLogger("turnstone.sim.cluster")

# How many node queues a single dispatcher watches via one BLPOP call.
NODES_PER_DISPATCHER = 50


class PooledBroker(RedisBroker):
    """RedisBroker that uses a shared external ConnectionPool."""

    def __init__(
        self,
        pool: redis.ConnectionPool,
        prefix: str = "turnstone",
        response_ttl: int = 600,
    ):
        # Bypass RedisBroker.__init__ — set up manually with the shared pool.
        import threading

        self._prefix = prefix
        self._response_ttl = response_ttl
        self._pool = pool
        self._redis = redis.Redis(connection_pool=pool)
        self._pubsub = self._redis.pubsub(ignore_subscribe_messages=True)
        self._listener_thread: threading.Thread | None = None
        self._running = True

    def close(self) -> None:
        """No-op — the shared pool is managed by SimCluster."""
        self._running = False


class InboundDispatcher:
    """Watches batches of node queues via a single BLPOP call.

    Instead of one BLPOP per node (which would exhaust Redis connections at
    1000 nodes), a dispatcher batches ~50 node queues into a single BLPOP
    on multiple keys.  This keeps total Redis connections bounded.
    """

    def __init__(
        self,
        redis_client: redis.Redis,
        node_ids: list[str],
        nodes: dict[str, SimNode],
        prefix: str,
    ):
        self._redis = redis_client
        self._node_ids = node_ids
        self._nodes = nodes
        self._prefix = prefix
        self._running = True

        # Build BLPOP key list: per-node queues first (priority), shared last
        self._keys = [f"{prefix}:inbound:{nid}" for nid in node_ids]
        self._keys.append(f"{prefix}:inbound")

        # Pre-compute key → node_id mapping
        self._key_to_node: dict[str, str] = {
            f"{prefix}:inbound:{nid}": nid for nid in node_ids
        }

    async def run(self) -> None:
        while self._running:
            # Snapshot keys to avoid race with remove_node() during BLPOP
            keys = list(self._keys)
            if not keys:
                await asyncio.sleep(0.5)
                continue
            result = await asyncio.to_thread(
                self._redis.blpop,
                keys,
                timeout=1,
            )
            if result is None:
                continue

            queue_key, raw = result
            if isinstance(queue_key, bytes):
                queue_key = queue_key.decode()
            if isinstance(raw, bytes):
                raw = raw.decode()

            node = self._resolve_target(queue_key)
            if node and node._running:
                await node.handle_message(raw)

    def _resolve_target(self, queue_key: str) -> SimNode | None:
        """Determine which SimNode should handle this message."""
        node_id = self._key_to_node.get(queue_key)
        if node_id:
            return self._nodes.get(node_id)

        # Shared queue — pick running node with fewest workstreams and capacity
        if self._nodes:
            candidates = [
                n
                for n in self._nodes.values()
                if n._running and n.workstream_count < n._config.max_ws_per_node
            ]
            if candidates:
                return min(candidates, key=lambda n: n.workstream_count)
            # Fall back to any running node if all at capacity
            running = [n for n in self._nodes.values() if n._running]
            if running:
                return min(running, key=lambda n: n.workstream_count)
        return None

    def stop(self) -> None:
        self._running = False

    def remove_node(self, node_id: str) -> None:
        """Remove a node from this dispatcher (for kill simulation)."""
        self._nodes.pop(node_id, None)
        key = f"{self._prefix}:inbound:{node_id}"
        self._key_to_node.pop(key, None)
        if key in self._keys:
            self._keys.remove(key)


class SimCluster:
    """Orchestrates N SimNodes, dispatchers, heartbeats, and metrics.

    Usage::

        cluster = SimCluster(config)
        await cluster.start()
        await cluster.run_scenario()
        report = cluster.report()
        await cluster.stop()
    """

    def __init__(self, config: SimConfig):
        self._config = config
        self._metrics = MetricsCollector()
        self._nodes: dict[str, SimNode] = {}
        self._node_order: list[str] = []
        self._dispatchers: list[InboundDispatcher] = []
        self._tasks: list[asyncio.Task] = []
        self._pool: redis.ConnectionPool | None = None
        self._redis_client: redis.Redis | None = None
        self._running = True

    @property
    def metrics(self) -> MetricsCollector:
        return self._metrics

    @property
    def nodes(self) -> dict[str, SimNode]:
        return self._nodes

    @property
    def config(self) -> SimConfig:
        return self._config

    async def start(self) -> None:
        """Create connection pool, nodes, dispatchers; start all tasks."""
        self._executor = ThreadPoolExecutor(max_workers=64)

        # Shared Redis pool
        self._pool = redis.ConnectionPool(
            host=self._config.redis_host,
            port=self._config.redis_port,
            db=self._config.redis_db,
            password=self._config.redis_password,
            decode_responses=True,
            retry_on_timeout=True,
            max_connections=64,
        )
        self._redis_client = redis.Redis(connection_pool=self._pool)

        # Create nodes
        for i in range(self._config.num_nodes):
            node_id = f"sim-{i:04d}"
            broker = PooledBroker(
                self._pool,
                prefix=self._config.prefix,
            )
            node = SimNode(node_id, broker, self._config, self._metrics)
            self._nodes[node_id] = node
            self._node_order.append(node_id)

        # Create dispatchers (batches of NODES_PER_DISPATCHER)
        all_ids = list(self._nodes.keys())
        num_dispatchers = max(1, math.ceil(len(all_ids) / NODES_PER_DISPATCHER))
        for i in range(num_dispatchers):
            start = i * NODES_PER_DISPATCHER
            batch_ids = all_ids[start : start + NODES_PER_DISPATCHER]
            # Each dispatcher gets its own Redis client from the shared pool
            client = redis.Redis(connection_pool=self._pool)
            dispatcher = InboundDispatcher(
                client,
                batch_ids,
                dict(self._nodes),
                self._config.prefix,
            )
            self._dispatchers.append(dispatcher)
            self._tasks.append(asyncio.create_task(dispatcher.run()))

        # Start heartbeat task
        self._tasks.append(asyncio.create_task(self._heartbeat_loop()))

        # Start utilization snapshot task
        self._tasks.append(asyncio.create_task(self._utilization_loop()))

        # Wait for all nodes to register
        await self._wait_for_nodes()
        log.info(
            "Cluster started: %d nodes, %d dispatchers",
            len(self._nodes),
            len(self._dispatchers),
        )

    async def _heartbeat_loop(self) -> None:
        """Register heartbeats for all running nodes concurrently."""
        interval = max(1, self._config.heartbeat_ttl // 2)
        loop = asyncio.get_running_loop()
        while self._running:
            tasks = [
                loop.run_in_executor(self._executor, node.heartbeat_once)
                for node in self._nodes.values()
                if node._running
            ]
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.sleep(interval)

    async def _utilization_loop(self) -> None:
        """Periodically snapshot workstream utilization."""
        while self._running:
            await asyncio.sleep(self._config.metrics_interval)
            counts = {
                nid: node.workstream_count
                for nid, node in self._nodes.items()
                if node._running
            }
            self._metrics.snapshot_utilization(counts)

    async def _wait_for_nodes(self) -> None:
        """Do an initial heartbeat and confirm registration."""
        loop = asyncio.get_running_loop()
        tasks = [
            loop.run_in_executor(self._executor, node.heartbeat_once)
            for node in self._nodes.values()
        ]
        await asyncio.gather(*tasks, return_exceptions=True)

        registered = 0
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            keys = await loop.run_in_executor(
                self._executor,
                self._redis_client.keys,
                f"{self._config.prefix}:node:sim-*",
            )
            registered = len(keys)
            if registered >= self._config.num_nodes:
                return
            await asyncio.sleep(0.5)
        raise TimeoutError(
            f"Only {registered}/{self._config.num_nodes} nodes registered",
        )

    async def run_scenario(self) -> None:
        """Run the configured scenario."""
        from turnstone.sim.scenario import SCENARIOS

        scenario_cls = SCENARIOS.get(self._config.scenario)
        if scenario_cls is None:
            raise ValueError(f"Unknown scenario: {self._config.scenario!r}")
        scenario = scenario_cls()
        await scenario.run(self, self._config, self._metrics)

    async def kill_node(self, node_id: str) -> None:
        """Simulate a node failure: stop heartbeat, stop processing."""
        node = self._nodes.get(node_id)
        if node and node._running:
            node.stop()
            self._metrics.record_node_kill(node_id)
            # Remove from dispatchers
            for d in self._dispatchers:
                d.remove_node(node_id)
            log.info("Killed node %s", node_id)

    def report(self) -> dict:
        """Generate final metrics report."""
        return self._metrics.summary()

    async def stop(self) -> None:
        """Shutdown all nodes and cancel tasks."""
        self._running = False
        for node in self._nodes.values():
            node.stop()
        for d in self._dispatchers:
            d.stop()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if hasattr(self, "_executor"):
            self._executor.shutdown(wait=False)
        if self._pool:
            self._pool.disconnect()
        log.info("Cluster stopped")
