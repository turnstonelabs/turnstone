"""Abstract message broker and Redis implementation.

The MessageBroker protocol defines the interface for inbound queuing, outbound
pub/sub, per-request response queues, and multi-node routing primitives.
RedisBroker is the default provider.
"""

from __future__ import annotations

import contextlib
import json
from typing import TYPE_CHECKING, Any, Protocol, cast

if TYPE_CHECKING:
    from collections.abc import Callable

    import redis as _redis_t


class MessageBroker(Protocol):
    """Abstract message broker for inbound/outbound communication.

    Implementations must provide:
    - Reliable inbound queue (FIFO, at-least-once delivery)
    - Outbound pub/sub channels (fan-out to all subscribers)
    - Per-request response queues for approval request/response correlation
    - Workstream ownership tracking (ws_id → node_id)
    - Node registry with heartbeat
    """

    def push_inbound(self, message: str, node_id: str = "") -> None:
        """Push a message onto the inbound queue.

        If *node_id* is set, pushes to the per-node queue for directed
        routing.  Otherwise pushes to the shared queue.
        """
        ...

    def pop_inbound(self, timeout: float = 0, node_id: str = "") -> str | None:
        """Pop next message from the inbound queue (bridge side).

        If *node_id* is set, BLPOPs from both the per-node queue (priority)
        and the shared queue.  Otherwise BLPOPs from the shared queue only.
        Returns None on timeout.
        """
        ...

    def publish_outbound(self, channel: str, event: str) -> None:
        """Publish an event to an outbound channel."""
        ...

    def subscribe_outbound(self, channel: str, callback: Callable[[str], None]) -> None:
        """Subscribe to an outbound channel."""
        ...

    def unsubscribe_outbound(self, channel: str) -> None:
        """Unsubscribe from an outbound channel."""
        ...

    def push_response(self, queue_name: str, message: str) -> None:
        """Push a response onto a named response queue."""
        ...

    def pop_response(self, queue_name: str, timeout: float = 300) -> str | None:
        """Pop from a named response queue.  Returns None on timeout."""
        ...

    # -- routing primitives --------------------------------------------------

    def set_ws_owner(self, ws_id: str, node_id: str, ttl: int = 0) -> None:
        """Register which node owns a workstream."""
        ...

    def get_ws_owner(self, ws_id: str) -> str | None:
        """Look up the node that owns a workstream.  Returns None if unowned."""
        ...

    def del_ws_owner(self, ws_id: str) -> None:
        """Remove workstream ownership (on close)."""
        ...

    def register_node(self, node_id: str, metadata: dict[str, Any], ttl: int = 60) -> None:
        """Register or refresh a node's heartbeat with metadata."""
        ...

    def list_nodes(self) -> list[dict[str, Any]]:
        """List all active nodes (those with unexpired heartbeats)."""
        ...

    def subscribe_cluster(self, callback: Callable[[str], None]) -> None:
        """Subscribe to the cluster-wide event channel."""
        ...

    def close(self) -> None:
        """Clean up connections."""
        ...


class RedisBroker:
    """Redis-backed MessageBroker using lists (queues) and pub/sub (events).

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
    ) -> None:
        import redis

        self._prefix = prefix
        self._response_ttl = response_ttl
        self._pool: _redis_t.ConnectionPool = redis.ConnectionPool(
            host=host,
            port=port,
            db=db,
            password=password,
            decode_responses=True,
            retry_on_timeout=True,
        )
        self._redis: _redis_t.Redis[str] = cast(
            "_redis_t.Redis[str]",
            redis.Redis(connection_pool=self._pool),
        )
        self._pubsub = self._redis.pubsub(ignore_subscribe_messages=True)
        self._listener_thread: Any = None
        self._running = True

    # -- inbound queue -------------------------------------------------------

    def push_inbound(self, message: str, node_id: str = "") -> None:
        if node_id:
            self._redis.rpush(f"{self._prefix}:inbound:{node_id}", message)
        else:
            self._redis.rpush(f"{self._prefix}:inbound", message)

    def pop_inbound(self, timeout: float = 0, node_id: str = "") -> str | None:
        t = int(timeout) if timeout > 0 else 0
        if node_id:
            # Per-node queue first (priority), then shared queue
            result = self._redis.blpop(
                [f"{self._prefix}:inbound:{node_id}", f"{self._prefix}:inbound"],
                timeout=t,
            )
        else:
            result = self._redis.blpop(f"{self._prefix}:inbound", timeout=t)
        return result[1] if result else None

    # -- outbound pub/sub ----------------------------------------------------

    def publish_outbound(self, channel: str, event: str) -> None:
        self._redis.publish(channel, event)

    def subscribe_outbound(self, channel: str, callback: Callable[[str], None]) -> None:
        def _handler(msg: dict[str, Any]) -> None:
            callback(msg["data"])

        self._pubsub.subscribe(**{channel: _handler})
        if self._listener_thread is None or not self._listener_thread.is_alive():
            self._listener_thread = self._pubsub.run_in_thread(sleep_time=0.1, daemon=True)

    def unsubscribe_outbound(self, channel: str) -> None:
        self._pubsub.unsubscribe(channel)

    # -- response queues -----------------------------------------------------

    def push_response(self, queue_name: str, message: str) -> None:
        key = f"{self._prefix}:resp:{queue_name}"
        self._redis.rpush(key, message)
        self._redis.expire(key, self._response_ttl)

    def pop_response(self, queue_name: str, timeout: float = 300) -> str | None:
        key = f"{self._prefix}:resp:{queue_name}"
        result = self._redis.blpop(key, timeout=int(timeout))
        return result[1] if result else None

    # -- routing primitives --------------------------------------------------

    def set_ws_owner(self, ws_id: str, node_id: str, ttl: int = 0) -> None:
        key = f"{self._prefix}:ws:{ws_id}"
        if ttl > 0:
            self._redis.set(key, node_id, ex=ttl)
        else:
            self._redis.set(key, node_id)

    def get_ws_owner(self, ws_id: str) -> str | None:
        return self._redis.get(f"{self._prefix}:ws:{ws_id}")

    def del_ws_owner(self, ws_id: str) -> None:
        self._redis.delete(f"{self._prefix}:ws:{ws_id}")

    def register_node(self, node_id: str, metadata: dict[str, Any], ttl: int = 60) -> None:
        key = f"{self._prefix}:node:{node_id}"
        self._redis.set(key, json.dumps(metadata), ex=ttl)

    def list_nodes(self) -> list[dict[str, Any]]:
        pattern = f"{self._prefix}:node:*"
        prefix_len = len(f"{self._prefix}:node:")
        nodes: list[dict[str, Any]] = []
        for key in self._redis.scan_iter(match=pattern, count=100):
            raw = self._redis.get(key)
            if raw:
                try:
                    meta: dict[str, Any] = json.loads(raw)
                except json.JSONDecodeError:
                    meta = {}
                meta["node_id"] = key[prefix_len:]
                nodes.append(meta)
        return nodes

    # -- cluster event channel -----------------------------------------------

    def subscribe_cluster(self, callback: Callable[[str], None]) -> None:
        """Subscribe to the cluster-wide event channel."""
        channel = f"{self._prefix}:events:cluster"
        self.subscribe_outbound(channel, callback)

    # -- lifecycle -----------------------------------------------------------

    def close(self) -> None:
        self._running = False
        if self._listener_thread is not None:
            self._listener_thread.stop()
            self._listener_thread = None
        with contextlib.suppress(Exception):
            self._pubsub.close()
        self._pool.disconnect()
