"""Tests for turnstone.channels._routing.ChannelRouter."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from turnstone.channels._routing import ChannelRouter
from turnstone.sdk._types import TurnstoneAPIError


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
def router(mock_storage: MagicMock) -> ChannelRouter:
    return ChannelRouter(
        server_url="http://localhost:8080/v1",
        storage=mock_storage,
    )


@pytest.fixture
def console_router(mock_storage: MagicMock) -> ChannelRouter:
    return ChannelRouter(
        server_url="http://localhost:8080/v1",
        storage=mock_storage,
        console_url="http://localhost:8081/v1",
        api_token="tok-test",
    )


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
    async def test_calls_server_send(
        self, router: ChannelRouter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert router._server is not None
        mock_send = AsyncMock()
        monkeypatch.setattr(router._server, "send", mock_send)
        await router.send_message("ws-1", "hello world")
        mock_send.assert_awaited_once_with("hello world", "ws-1")

    @pytest.mark.anyio
    async def test_calls_console_route_send(
        self, console_router: ChannelRouter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert console_router._console is not None
        mock_send = AsyncMock()
        monkeypatch.setattr(console_router._console, "route_send", mock_send)
        await console_router.send_message("ws-1", "hello world")
        mock_send.assert_awaited_once_with("hello world", "ws-1")


class TestSendApproval:
    @pytest.mark.anyio
    async def test_calls_server_approve(
        self, router: ChannelRouter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert router._server is not None
        mock_approve = AsyncMock()
        monkeypatch.setattr(router._server, "approve", mock_approve)
        await router.send_approval("ws-1", "corr-abc", approved=True, feedback="ok")
        mock_approve.assert_awaited_once_with(
            ws_id="ws-1", approved=True, feedback="ok", always=False
        )

    @pytest.mark.anyio
    async def test_omits_empty_feedback(
        self, router: ChannelRouter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert router._server is not None
        mock_approve = AsyncMock()
        monkeypatch.setattr(router._server, "approve", mock_approve)
        await router.send_approval("ws-1", "corr-abc", approved=False)
        mock_approve.assert_awaited_once_with(
            ws_id="ws-1", approved=False, feedback=None, always=False
        )

    @pytest.mark.anyio
    async def test_calls_console_route_approve(
        self, console_router: ChannelRouter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert console_router._console is not None
        mock_approve = AsyncMock()
        monkeypatch.setattr(console_router._console, "route_approve", mock_approve)
        await console_router.send_approval("ws-1", "corr-abc", approved=True, always=True)
        mock_approve.assert_awaited_once_with(ws_id="ws-1", approved=True, feedback="", always=True)


class TestSendPlanFeedback:
    @pytest.mark.anyio
    async def test_calls_server_plan_feedback(
        self, router: ChannelRouter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert router._server is not None
        mock_plan = AsyncMock()
        monkeypatch.setattr(router._server, "plan_feedback", mock_plan)
        await router.send_plan_feedback("ws-2", "corr-xyz", "looks good")
        mock_plan.assert_awaited_once_with(ws_id="ws-2", feedback="looks good")

    @pytest.mark.anyio
    async def test_calls_console_route_plan_feedback(
        self, console_router: ChannelRouter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert console_router._console is not None
        mock_plan = AsyncMock()
        monkeypatch.setattr(console_router._console, "route_plan_feedback", mock_plan)
        await console_router.send_plan_feedback("ws-2", "corr-xyz", "looks good")
        mock_plan.assert_awaited_once_with(ws_id="ws-2", feedback="looks good")


class TestDeleteRoute:
    @pytest.mark.anyio
    async def test_calls_storage_delete(
        self, router: ChannelRouter, mock_storage: MagicMock
    ) -> None:
        await router.delete_route("discord", "ch-123")
        mock_storage.delete_channel_route.assert_called_once_with("discord", "ch-123")


class TestGetOrCreateWorkstream:
    @pytest.mark.anyio
    async def test_creates_new_workstream_via_server(
        self,
        router: ChannelRouter,
        mock_storage: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        assert router._server is not None
        mock_create = AsyncMock()
        mock_create.return_value = MagicMock(ws_id="ws-new", name="test")
        monkeypatch.setattr(router._server, "create_workstream", mock_create)
        ws_id, is_new = await router.get_or_create_workstream("discord", "ch-1", name="test")
        assert ws_id == "ws-new"
        assert is_new is True
        mock_storage.create_channel_route.assert_called_once_with("discord", "ch-1", "ws-new")
        mock_create.assert_awaited_once()

    @pytest.mark.anyio
    async def test_creates_new_workstream_via_console(
        self,
        console_router: ChannelRouter,
        mock_storage: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        assert console_router._console is not None
        mock_create = AsyncMock(
            return_value={"ws_id": "ws-new", "name": "test", "node_url": "http://node1:8080/v1"}
        )
        monkeypatch.setattr(console_router._console, "route_create_workstream", mock_create)
        ws_id, is_new = await console_router.get_or_create_workstream(
            "discord", "ch-1", name="test"
        )
        assert ws_id == "ws-new"
        assert is_new is True
        mock_storage.create_channel_route.assert_called_once_with("discord", "ch-1", "ws-new")
        # Node URL should be cached.
        assert console_router._node_urls["ws-new"] == "http://node1:8080/v1"

    @pytest.mark.anyio
    async def test_returns_existing_alive_workstream(
        self,
        router: ChannelRouter,
        mock_storage: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        mock_storage.get_channel_route.return_value = {
            "ws_id": "ws-old",
            "channel_type": "discord",
            "channel_id": "ch-1",
        }
        monkeypatch.setattr(router, "_is_ws_alive", AsyncMock(return_value=True))
        ws_id, is_new = await router.get_or_create_workstream("discord", "ch-1")
        assert ws_id == "ws-old"
        assert is_new is False

    @pytest.mark.anyio
    async def test_resumes_stale_workstream(
        self,
        router: ChannelRouter,
        mock_storage: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        mock_storage.get_channel_route.return_value = {
            "ws_id": "ws-stale",
            "channel_type": "discord",
            "channel_id": "ch-1",
        }
        # Alive check returns False — ws is not alive.
        monkeypatch.setattr(router, "_is_ws_alive", AsyncMock(return_value=False))
        # Server create returns a resumed workstream.
        assert router._server is not None
        mock_create = AsyncMock()
        mock_create.return_value = MagicMock(ws_id="ws-resumed", name="test")
        monkeypatch.setattr(router._server, "create_workstream", mock_create)

        ws_id, is_new = await router.get_or_create_workstream("discord", "ch-1", name="test")
        assert ws_id == "ws-resumed"
        assert is_new is True
        # Should have deleted the stale route and created a new one.
        mock_storage.delete_channel_route.assert_called_once_with("discord", "ch-1")
        mock_storage.create_channel_route.assert_called_once_with("discord", "ch-1", "ws-resumed")
        # The create call should include resume_ws pointing at the old ws.
        mock_create.assert_awaited_once()
        call_kwargs = mock_create.call_args[1]
        assert call_kwargs["resume_ws"] == "ws-stale"

    @pytest.mark.anyio
    async def test_sends_initial_message_for_new_workstream(
        self,
        router: ChannelRouter,
        mock_storage: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        assert router._server is not None
        mock_create = AsyncMock()
        mock_create.return_value = MagicMock(ws_id="ws-new", name="test")
        monkeypatch.setattr(router._server, "create_workstream", mock_create)
        mock_send = AsyncMock()
        monkeypatch.setattr(router._server, "send", mock_send)

        await router.get_or_create_workstream("discord", "ch-1", name="test", initial_message="hi")
        mock_send.assert_awaited_once_with("hi", "ws-new")


class TestCloseWorkstream:
    @pytest.mark.anyio
    async def test_calls_server_close(
        self, router: ChannelRouter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert router._server is not None
        mock_close = AsyncMock()
        monkeypatch.setattr(router._server, "close_workstream", mock_close)
        await router.close_workstream("ws-1")
        mock_close.assert_awaited_once_with("ws-1")

    @pytest.mark.anyio
    async def test_catches_api_error(
        self, router: ChannelRouter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert router._server is not None
        mock_close = AsyncMock(side_effect=TurnstoneAPIError(404, "not found"))
        monkeypatch.setattr(router._server, "close_workstream", mock_close)
        # Should not raise.
        await router.close_workstream("ws-1")

    @pytest.mark.anyio
    async def test_calls_console_route_close(
        self, console_router: ChannelRouter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert console_router._console is not None
        mock_close = AsyncMock()
        monkeypatch.setattr(console_router._console, "route_close", mock_close)
        await console_router.close_workstream("ws-1")
        mock_close.assert_awaited_once_with("ws-1")


class TestAclose:
    @pytest.mark.anyio
    async def test_closes_server_client(
        self, router: ChannelRouter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert router._server is not None
        mock_close = AsyncMock()
        monkeypatch.setattr(router._server, "aclose", mock_close)
        await router.aclose()
        mock_close.assert_awaited_once()

    @pytest.mark.anyio
    async def test_closes_console_client(
        self, console_router: ChannelRouter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert console_router._console is not None
        mock_close = AsyncMock()
        monkeypatch.setattr(console_router._console, "aclose", mock_close)
        await console_router.aclose()
        mock_close.assert_awaited_once()


class TestGetNodeUrl:
    @pytest.mark.anyio
    async def test_returns_cached_url(self, router: ChannelRouter) -> None:
        router._node_urls["ws-1"] = "http://node1:8080/v1"
        url = await router.get_node_url("ws-1")
        assert url == "http://node1:8080/v1"

    @pytest.mark.anyio
    async def test_falls_back_to_server_url(self, router: ChannelRouter) -> None:
        url = await router.get_node_url("ws-unknown")
        assert url == "http://localhost:8080/v1"

    @pytest.mark.anyio
    async def test_queries_console_route_lookup(
        self, console_router: ChannelRouter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert console_router._console is not None
        mock_lookup = AsyncMock(return_value={"node_url": "http://node2:8080/v1", "node_id": "n2"})
        monkeypatch.setattr(console_router._console, "route_lookup", mock_lookup)
        url = await console_router.get_node_url("ws-1")
        assert url == "http://node2:8080/v1"
        mock_lookup.assert_awaited_once_with("ws-1")
        # Should be cached now.
        assert console_router._node_urls["ws-1"] == "http://node2:8080/v1"
