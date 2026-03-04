"""Tests for turnstone.mq.async_broker.AsyncRedisBroker."""

from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from turnstone.mq.async_broker import AsyncRedisBroker


@pytest.fixture
def broker() -> AsyncRedisBroker:
    return AsyncRedisBroker(host="localhost", port=6379, db=0, prefix="test", response_ttl=120)


@pytest.fixture
def mock_redis() -> AsyncMock:
    """Return a mock Redis client with common async methods."""
    r = AsyncMock()
    r.rpush = AsyncMock()
    r.publish = AsyncMock()
    r.expire = AsyncMock()
    r.get = AsyncMock(return_value=None)
    r.set = AsyncMock()
    r.delete = AsyncMock()
    r.blpop = AsyncMock(return_value=None)
    ps = AsyncMock()
    ps.subscribe = AsyncMock()
    ps.unsubscribe = AsyncMock()
    ps.close = AsyncMock()
    ps.get_message = AsyncMock(return_value=None)
    r.pubsub = MagicMock(return_value=ps)
    return r


def _inject_redis(broker: AsyncRedisBroker, mock_redis: AsyncMock) -> None:
    """Inject a mock Redis client into the broker, simulating connect()."""
    broker._redis = mock_redis
    broker._pubsub = mock_redis.pubsub()


class TestConstructor:
    def test_stores_config(self) -> None:
        b = AsyncRedisBroker(host="h", port=1234, db=2, prefix="pfx", password="pw")
        assert b._host == "h"
        assert b._port == 1234
        assert b._db == 2
        assert b._prefix == "pfx"
        assert b._password == "pw"
        assert b._redis is None

    def test_defaults(self) -> None:
        b = AsyncRedisBroker()
        assert b._host == "localhost"
        assert b._port == 6379
        assert b._prefix == "turnstone"


class TestConnect:
    @pytest.mark.anyio
    async def test_creates_connection(self) -> None:
        b = AsyncRedisBroker()
        mock_r = AsyncMock()
        mock_r.pubsub = MagicMock(return_value=AsyncMock())
        with patch("redis.asyncio.Redis", return_value=mock_r):
            await b.connect()
            assert b._redis is mock_r
            assert b._pubsub is not None

    @pytest.mark.anyio
    async def test_connect_idempotent(
        self, broker: AsyncRedisBroker, mock_redis: AsyncMock
    ) -> None:
        _inject_redis(broker, mock_redis)
        old = broker._redis
        await broker.connect()
        assert broker._redis is old


class TestPushInbound:
    @pytest.mark.anyio
    async def test_shared_queue(self, broker: AsyncRedisBroker, mock_redis: AsyncMock) -> None:
        _inject_redis(broker, mock_redis)
        await broker.push_inbound('{"type":"send"}')
        mock_redis.rpush.assert_awaited_once_with("test:inbound", '{"type":"send"}')

    @pytest.mark.anyio
    async def test_per_node_queue(self, broker: AsyncRedisBroker, mock_redis: AsyncMock) -> None:
        _inject_redis(broker, mock_redis)
        await broker.push_inbound('{"type":"send"}', node_id="node-1")
        mock_redis.rpush.assert_awaited_once_with("test:inbound:node-1", '{"type":"send"}')


class TestPublishOutbound:
    @pytest.mark.anyio
    async def test_publishes(self, broker: AsyncRedisBroker, mock_redis: AsyncMock) -> None:
        _inject_redis(broker, mock_redis)
        await broker.publish_outbound("test:events:global", '{"event":"data"}')
        mock_redis.publish.assert_awaited_once_with("test:events:global", '{"event":"data"}')


class TestPushResponse:
    @pytest.mark.anyio
    async def test_rpush_and_expire(self, broker: AsyncRedisBroker, mock_redis: AsyncMock) -> None:
        _inject_redis(broker, mock_redis)
        await broker.push_response("req-123", '{"ok":true}')
        mock_redis.rpush.assert_awaited_once_with("test:resp:req-123", '{"ok":true}')
        mock_redis.expire.assert_awaited_once_with("test:resp:req-123", 120)


class TestSubscribe:
    @pytest.mark.anyio
    async def test_creates_task(self, broker: AsyncRedisBroker, mock_redis: AsyncMock) -> None:
        _inject_redis(broker, mock_redis)
        await broker.subscribe("test:events:global", lambda msg: None)
        assert "test:events:global" in broker._callbacks
        assert broker._listener_task is not None
        assert isinstance(broker._listener_task, asyncio.Task)
        # Clean up.
        broker._listener_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await broker._listener_task


class TestUnsubscribe:
    @pytest.mark.anyio
    async def test_cancels_task(self, broker: AsyncRedisBroker, mock_redis: AsyncMock) -> None:
        _inject_redis(broker, mock_redis)
        await broker.subscribe("test:events:ch", lambda msg: None)
        assert "test:events:ch" in broker._callbacks
        await broker.unsubscribe("test:events:ch")
        assert "test:events:ch" not in broker._callbacks


class TestRoutingPrimitives:
    @pytest.mark.anyio
    async def test_get_ws_owner(self, broker: AsyncRedisBroker, mock_redis: AsyncMock) -> None:
        _inject_redis(broker, mock_redis)
        mock_redis.get.return_value = "node-1"
        result = await broker.get_ws_owner("ws-abc")
        mock_redis.get.assert_awaited_once_with("test:ws:ws-abc")
        assert result == "node-1"

    @pytest.mark.anyio
    async def test_set_ws_owner(self, broker: AsyncRedisBroker, mock_redis: AsyncMock) -> None:
        _inject_redis(broker, mock_redis)
        await broker.set_ws_owner("ws-abc", "node-2")
        mock_redis.set.assert_awaited_once_with("test:ws:ws-abc", "node-2")

    @pytest.mark.anyio
    async def test_set_ws_owner_with_ttl(
        self, broker: AsyncRedisBroker, mock_redis: AsyncMock
    ) -> None:
        _inject_redis(broker, mock_redis)
        await broker.set_ws_owner("ws-abc", "node-2", ttl=300)
        mock_redis.set.assert_awaited_once_with("test:ws:ws-abc", "node-2", ex=300)

    @pytest.mark.anyio
    async def test_del_ws_owner(self, broker: AsyncRedisBroker, mock_redis: AsyncMock) -> None:
        _inject_redis(broker, mock_redis)
        await broker.del_ws_owner("ws-abc")
        mock_redis.delete.assert_awaited_once_with("test:ws:ws-abc")


class TestClose:
    @pytest.mark.anyio
    async def test_cancels_tasks_and_closes(
        self, broker: AsyncRedisBroker, mock_redis: AsyncMock
    ) -> None:
        _inject_redis(broker, mock_redis)
        await broker.subscribe("ch1", lambda m: None)
        assert len(broker._callbacks) == 1
        assert broker._listener_task is not None
        await broker.close()
        assert len(broker._callbacks) == 0
        assert broker._listener_task is None
        assert broker._redis is None
        assert broker._pubsub is None
