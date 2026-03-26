"""Async Redis message broker.

Provides :class:`AsyncRedisBroker`, an asyncio-native counterpart to
:class:`~turnstone.mq.broker.RedisBroker`.  Uses ``redis.asyncio`` for all I/O
and manages pub/sub listeners as :class:`asyncio.Task` instances.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    import redis.asyncio as _aredis_t


class AsyncRedisBroker:
    """Async Redis-backed message broker using lists (queues) and pub/sub.

    This is the asyncio equivalent of :class:`~turnstone.mq.broker.RedisBroker`.
    All methods are coroutines and must be awaited.

    Queue keys:
        ``{prefix}:inbound``             — shared inbound command queue
        ``{prefix}:inbound:{node_id}``   — per-node directed queue
        ``{prefix}:resp:{request_id}``   — per-request response queues

    Routing keys:
        ``{prefix}:ws:{ws_id}``          — workstream ownership (string)
        ``{prefix}:node:{node_id}``      — node heartbeat + metadata (string/JSON)

    Pub/sub channels:
        ``{prefix}:events:global``       — global event channel
        ``{prefix}:events:{ws_id}``      — per-workstream event channel
        ``{prefix}:events:cluster``      — cluster-wide state changes
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = 6379,
        db: int = 0,
        prefix: str = "turnstone",
        password: str | None = None,
        response_ttl: int = 600,
        ssl: bool = False,
        ssl_ca_certs: str | None = None,
        ssl_certfile: str | None = None,
        ssl_keyfile: str | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._db = db
        self._password = password
        self._prefix = prefix
        self._response_ttl = response_ttl
        self._ssl_kwargs: dict[str, Any] = {}
        if ssl:
            self._ssl_kwargs["ssl"] = True
            if ssl_ca_certs:
                self._ssl_kwargs["ssl_ca_certs"] = ssl_ca_certs
            if ssl_certfile:
                self._ssl_kwargs["ssl_certfile"] = ssl_certfile
            if ssl_keyfile:
                self._ssl_kwargs["ssl_keyfile"] = ssl_keyfile
        self._redis: _aredis_t.Redis[str] | None = None
        self._pubsub: _aredis_t.client.PubSub | None = None
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._callbacks: dict[str, Callable[[str], Any]] = {}
        self._queues: dict[str, asyncio.Queue[str]] = {}
        self._workers: dict[str, asyncio.Task[None]] = {}
        self._listener_task: asyncio.Task[None] | None = None

    # -- connection ----------------------------------------------------------

    async def connect(self) -> None:
        """Create the async Redis connection.

        This is called lazily before first use if the connection has not yet
        been established.
        """
        if self._redis is not None:
            return

        import redis.asyncio as aioredis

        self._redis = aioredis.Redis(
            host=self._host,
            port=self._port,
            db=self._db,
            password=self._password,
            decode_responses=True,
            retry_on_timeout=True,
            **self._ssl_kwargs,
            max_connections=200,
        )
        self._pubsub = self._redis.pubsub(ignore_subscribe_messages=True)

    async def _ensure_connected(self) -> None:
        """Ensure the Redis connection is established."""
        if self._redis is None:
            await self.connect()

    @property
    def _r(self) -> _aredis_t.Redis[str]:
        """Return the Redis client, assuming it is connected."""
        if self._redis is None:
            msg = "Broker not connected — call connect() first"
            raise RuntimeError(msg)
        return self._redis

    @property
    def _ps(self) -> _aredis_t.client.PubSub:
        """Return the pub/sub client, assuming it is connected."""
        if self._pubsub is None:
            msg = "Broker not connected — call connect() first"
            raise RuntimeError(msg)
        return self._pubsub

    # -- inbound queue -------------------------------------------------------

    async def push_inbound(self, message: str, node_id: str = "") -> None:
        """Push a message onto the inbound queue.

        If *node_id* is set, pushes to the per-node queue for directed
        routing.  Otherwise pushes to the shared queue.
        """
        await self._ensure_connected()
        if node_id:
            await self._r.rpush(f"{self._prefix}:inbound:{node_id}", message)
        else:
            await self._r.rpush(f"{self._prefix}:inbound", message)

    # -- outbound pub/sub ----------------------------------------------------

    async def publish_outbound(self, channel: str, event: str) -> None:
        """Publish an event to an outbound channel."""
        await self._ensure_connected()
        await self._r.publish(channel, event)

    async def subscribe(self, channel: str, callback: Callable[[str], Any]) -> None:
        """Subscribe to a pub/sub channel.

        The *callback* receives the message string for each published event.
        It may be a regular function or an async coroutine.

        All subscriptions share a single listener task that dispatches
        messages to the correct callback based on the channel name.
        """
        await self._ensure_connected()
        await self._ps.subscribe(channel)
        self._callbacks[channel] = callback

        # Per-channel queue + worker ensures ordered delivery within a channel
        # while allowing different channels to process concurrently.
        q: asyncio.Queue[str] = asyncio.Queue()
        self._queues[channel] = q
        self._workers[channel] = asyncio.create_task(self._channel_worker(channel, q))

        # Start the shared listener task if not already running.
        if self._listener_task is None or self._listener_task.done():
            self._listener_task = asyncio.create_task(self._dispatch_loop())

    async def _dispatch_loop(self) -> None:
        """Single listener that routes pub/sub messages to per-channel queues.

        Each channel has its own queue + worker task, ensuring ordered
        delivery within a channel while allowing different channels to
        process concurrently.
        """
        import logging

        _log = logging.getLogger("turnstone.mq.async_broker")
        try:
            while self._callbacks:
                msg = await self._ps.get_message(
                    ignore_subscribe_messages=True,
                    timeout=0.1,
                )
                if msg is None:
                    # Yield control so cancellation can be delivered.
                    await asyncio.sleep(0)
                    continue
                if msg["type"] == "message":
                    ch = msg.get("channel", "")
                    q = self._queues.get(ch)
                    if q is not None:
                        q.put_nowait(msg["data"])
            _log.debug("Dispatch loop exiting — no active callbacks")
        except asyncio.CancelledError:
            return

    async def _channel_worker(self, channel: str, q: asyncio.Queue[str]) -> None:
        """Process messages for a single channel sequentially."""
        import logging

        _log = logging.getLogger("turnstone.mq.async_broker")
        try:
            while True:
                data = await q.get()
                cb = self._callbacks.get(channel)
                if cb is not None:
                    try:
                        result = cb(data)
                        if asyncio.iscoroutine(result):
                            await result
                    except Exception:
                        _log.exception("Listener callback error on %s", channel)
        except asyncio.CancelledError:
            return

    async def unsubscribe(self, channel: str) -> None:
        """Unsubscribe from a channel and cancel its worker."""
        await self._ensure_connected()
        self._callbacks.pop(channel, None)
        self._queues.pop(channel, None)
        worker = self._workers.pop(channel, None)
        if worker is not None:
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker
        # Legacy per-channel task cleanup (in case any remain).
        task = self._tasks.pop(channel, None)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self._ps.unsubscribe(channel)

    # -- response queues -----------------------------------------------------

    async def push_response(self, queue_name: str, message: str) -> None:
        """Push a response onto a named response queue."""
        await self._ensure_connected()
        key = f"{self._prefix}:resp:{queue_name}"
        await self._r.rpush(key, message)
        await self._r.expire(key, self._response_ttl)

    async def pop_response(self, queue_name: str, timeout: float = 300) -> str | None:
        """Pop from a named response queue.  Returns ``None`` on timeout."""
        await self._ensure_connected()
        key = f"{self._prefix}:resp:{queue_name}"
        result = await self._r.blpop(key, timeout=int(timeout))
        return result[1] if result else None

    # -- routing primitives --------------------------------------------------

    async def get_ws_owner(self, ws_id: str) -> str | None:
        """Look up the node that owns a workstream."""
        await self._ensure_connected()
        return await self._r.get(f"{self._prefix}:ws:{ws_id}")

    async def set_ws_owner(self, ws_id: str, node_id: str, ttl: int = 0) -> None:
        """Register which node owns a workstream."""
        await self._ensure_connected()
        key = f"{self._prefix}:ws:{ws_id}"
        if ttl > 0:
            await self._r.set(key, node_id, ex=ttl)
        else:
            await self._r.set(key, node_id)

    async def del_ws_owner(self, ws_id: str) -> None:
        """Remove workstream ownership."""
        await self._ensure_connected()
        await self._r.delete(f"{self._prefix}:ws:{ws_id}")

    async def register_node(self, node_id: str, metadata: dict[str, Any], ttl: int = 60) -> None:
        """Register or refresh a node's heartbeat with metadata."""
        await self._ensure_connected()
        key = f"{self._prefix}:node:{node_id}"
        await self._r.set(key, json.dumps(metadata), ex=ttl)

    async def list_nodes(self) -> list[dict[str, Any]]:
        """List all active nodes (those with unexpired heartbeats)."""
        await self._ensure_connected()
        pattern = f"{self._prefix}:node:*"
        prefix_len = len(f"{self._prefix}:node:")
        # Collect all keys first, then batch-fetch with MGET to avoid
        # N+1 round-trips (1 GET per node).
        keys: list[str] = []
        async for key in self._r.scan_iter(match=pattern, count=100):
            keys.append(key)
        if not keys:
            return []
        values = await self._r.mget(keys)
        nodes: list[dict[str, Any]] = []
        for key, raw in zip(keys, values, strict=True):
            if raw:
                try:
                    meta: dict[str, Any] = json.loads(raw)
                except json.JSONDecodeError:
                    meta = {}
                meta["node_id"] = key[prefix_len:]
                nodes.append(meta)
        return nodes

    # -- lifecycle -----------------------------------------------------------

    async def close(self) -> None:
        """Cancel all listener tasks and close the Redis connection."""
        self._callbacks.clear()
        self._queues.clear()
        # Cancel per-channel workers.
        for worker in self._workers.values():
            worker.cancel()
        for worker in self._workers.values():
            with contextlib.suppress(asyncio.CancelledError):
                await worker
        self._workers.clear()
        # Cancel the shared dispatch loop.
        if self._listener_task is not None:
            if not self._listener_task.done():
                self._listener_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._listener_task
            self._listener_task = None
        # Legacy per-channel tasks.
        for task in self._tasks.values():
            task.cancel()
        for task in self._tasks.values():
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()

        if self._pubsub is not None:
            with contextlib.suppress(Exception):
                await self._pubsub.close()
            self._pubsub = None

        if self._redis is not None:
            await self._redis.close()
            self._redis = None
