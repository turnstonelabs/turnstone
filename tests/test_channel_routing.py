"""Tests for turnstone.channels._routing.ChannelRouter."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from turnstone.channels._routing import ChannelRouter


@pytest.fixture
def mock_broker() -> AsyncMock:
    """Return a mock AsyncRedisBroker."""
    broker = AsyncMock()
    broker._prefix = "test"
    broker.push_inbound = AsyncMock()
    broker.push_response = AsyncMock()
    broker.subscribe = AsyncMock()
    broker.unsubscribe = AsyncMock()
    return broker


@pytest.fixture
def mock_storage() -> MagicMock:
    """Return a mock StorageBackend."""
    storage = MagicMock()
    storage.get_channel_user = MagicMock(return_value=None)
    storage.get_channel_route = MagicMock(return_value=None)
    storage.get_channel_route_by_ws = MagicMock(return_value=None)
    storage.create_channel_route = MagicMock()
    storage.delete_channel_route = MagicMock(return_value=True)
    return storage


@pytest.fixture
def router(mock_broker: AsyncMock, mock_storage: MagicMock) -> ChannelRouter:
    return ChannelRouter(broker=mock_broker, storage=mock_storage)


class TestResolveUser:
    @pytest.mark.anyio
    async def test_linked_user(self, router: ChannelRouter, mock_storage: MagicMock) -> None:
        mock_storage.get_channel_user.return_value = {"user_id": "usr-1", "channel_user_id": "d-42"}
        result = await router.resolve_user("discord", "d-42")
        assert result == "usr-1"
        mock_storage.get_channel_user.assert_called_once_with("discord", "d-42")

    @pytest.mark.anyio
    async def test_unlinked_user(self, router: ChannelRouter, mock_storage: MagicMock) -> None:
        mock_storage.get_channel_user.return_value = None
        result = await router.resolve_user("slack", "s-99")
        assert result is None


class TestSendMessage:
    @pytest.mark.anyio
    async def test_pushes_send_message(self, router: ChannelRouter, mock_broker: AsyncMock) -> None:
        cid = await router.send_message("ws-1", "hello world")
        assert isinstance(cid, str)
        assert len(cid) > 0
        mock_broker.push_inbound.assert_awaited_once()
        raw = mock_broker.push_inbound.call_args[0][0]
        payload = json.loads(raw)
        assert payload["type"] == "send"
        assert payload["ws_id"] == "ws-1"
        assert payload["message"] == "hello world"
        assert payload["correlation_id"] == cid


class TestSendApproval:
    @pytest.mark.anyio
    async def test_pushes_to_response_queue(
        self, router: ChannelRouter, mock_broker: AsyncMock
    ) -> None:
        await router.send_approval("ws-1", "corr-abc", approved=True, feedback="ok")
        mock_broker.push_response.assert_awaited_once()
        queue_name = mock_broker.push_response.call_args[0][0]
        assert queue_name == "corr-abc"
        raw = mock_broker.push_response.call_args[0][1]
        payload = json.loads(raw)
        assert payload["type"] == "approve"
        assert payload["approved"] is True
        assert payload["ws_id"] == "ws-1"


class TestSendPlanFeedback:
    @pytest.mark.anyio
    async def test_pushes_to_response_queue(
        self, router: ChannelRouter, mock_broker: AsyncMock
    ) -> None:
        await router.send_plan_feedback("ws-2", "corr-xyz", "looks good")
        mock_broker.push_response.assert_awaited_once()
        raw = mock_broker.push_response.call_args[0][1]
        payload = json.loads(raw)
        assert payload["type"] == "plan_feedback"
        assert payload["feedback"] == "looks good"


class TestDeleteRoute:
    @pytest.mark.anyio
    async def test_calls_storage_delete(
        self, router: ChannelRouter, mock_storage: MagicMock
    ) -> None:
        await router.delete_route("discord", "ch-123")
        mock_storage.delete_channel_route.assert_called_once_with("discord", "ch-123")
