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
        ssl: bool = False,
        ssl_ca_certs: str | None = None,
        ssl_certfile: str | None = None,
        ssl_keyfile: str | None = None,
    ) -> None:
        import redis

        self._prefix = prefix
        self._response_ttl = response_ttl
        pool_kwargs: dict[str, Any] = {}
        if ssl:
            pool_kwargs["connection_class"] = redis.SSLConnection
            if ssl_ca_certs:
                pool_kwargs["ssl_ca_certs"] = ssl_ca_certs
            if ssl_certfile:
                pool_kwargs["ssl_certfile"] = ssl_certfile
            if ssl_keyfile:
                pool_kwargs["ssl_keyfile"] = ssl_keyfile
        self._pool: _redis_t.ConnectionPool = redis.ConnectionPool(
            host=host,
            port=port,
            db=db,
            password=password,
            decode_responses=True,
            retry_on_timeout=True,
            max_connections=200,
            **pool_kwargs,
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
        # Collect all keys first, then batch-fetch with MGET to avoid
        # N+1 round-trips (1 GET per node).
        keys = list(self._redis.scan_iter(match=pattern, count=100))
        if not keys:
            return []
        values = self._redis.mget(keys)
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


# ---------------------------------------------------------------------------
# CLI helpers (shared across bridge, console, channels)
# ---------------------------------------------------------------------------


def add_redis_args(parser: Any) -> None:
    """Add Redis CLI arguments including TLS options."""
    import os

    parser.add_argument(
        "--redis-host",
        default=os.environ.get("REDIS_HOST", "localhost"),
        help="Redis host (default: $REDIS_HOST or localhost)",
    )
    parser.add_argument(
        "--redis-port",
        type=int,
        default=int(os.environ.get("REDIS_PORT", "6379")),
        help="Redis port (default: %(default)s)",
    )
    parser.add_argument(
        "--redis-password",
        default=os.environ.get("REDIS_PASSWORD"),
        help="Redis password (default: $REDIS_PASSWORD)",
    )
    parser.add_argument(
        "--redis-db",
        type=int,
        default=0,
        help="Redis DB number (default: %(default)s)",
    )
    parser.add_argument("--redis-tls", action="store_true", help="Enable Redis TLS")
    parser.add_argument("--redis-tls-ca", default=None, help="Redis CA cert path")
    parser.add_argument("--redis-tls-cert", default=None, help="Redis client cert path")
    parser.add_argument("--redis-tls-key", default=None, help="Redis client key path")


def _redis_tls_kwargs(args: Any) -> dict[str, Any]:
    """Extract Redis TLS kwargs from parsed args."""
    kwargs: dict[str, Any] = {}
    if getattr(args, "redis_tls", False):
        kwargs["ssl"] = True
        ca = getattr(args, "redis_tls_ca", None)
        if ca:
            kwargs["ssl_ca_certs"] = ca
        cert = getattr(args, "redis_tls_cert", None)
        if cert:
            kwargs["ssl_certfile"] = cert
        key = getattr(args, "redis_tls_key", None)
        if key:
            kwargs["ssl_keyfile"] = key
    return kwargs


def broker_from_args(args: Any) -> RedisBroker:
    """Create a :class:`RedisBroker` from parsed CLI arguments."""
    return RedisBroker(
        host=args.redis_host,
        port=args.redis_port,
        db=args.redis_db,
        password=args.redis_password or None,
        **_redis_tls_kwargs(args),
    )


def async_broker_from_args(args: Any) -> Any:
    """Create an :class:`AsyncRedisBroker` from parsed CLI arguments."""
    from turnstone.mq.async_broker import AsyncRedisBroker

    return AsyncRedisBroker(
        host=args.redis_host,
        port=args.redis_port,
        db=args.redis_db,
        password=args.redis_password or None,
        **_redis_tls_kwargs(args),
    )
