"""Tests for turnstone.channels._routing.ChannelRouter."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from turnstone.api.console_schemas import RouteCreateResponse
from turnstone.channels._routing import ChannelRouter
from turnstone.sdk._types import TurnstoneAPIError


@pytest.fixture
def mock_storage() -> MagicMock:
    """Return a mock StorageBackend."""
    storage = MagicMock()
    storage.get_channel_user = MagicMock(return_value=None)
    storage.get_channel_route = MagicMock(return_value=None)
    storage.get_channel_route_by_ws = MagicMock(return_value=None)
    storage.get_workstream = MagicMock(side_effect=lambda ws_id: {"ws_id": ws_id})
    storage.create_channel_route = MagicMock(return_value=True)
    storage.replace_channel_route = MagicMock(return_value=True)
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
            ws_id="ws-1", approved=True, feedback="ok", always=False, cycle_id="corr-abc"
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
            ws_id="ws-1", approved=False, feedback=None, always=False, cycle_id="corr-abc"
        )

    @pytest.mark.anyio
    async def test_calls_console_route_approve(
        self, console_router: ChannelRouter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert console_router._console is not None
        mock_approve = AsyncMock()
        monkeypatch.setattr(console_router._console, "route_approve", mock_approve)
        await console_router.send_approval("ws-1", "corr-abc", approved=True, always=True)
        mock_approve.assert_awaited_once_with(
            ws_id="ws-1", approved=True, feedback="", always=True, cycle_id="corr-abc"
        )


class TestDeleteRoute:
    @pytest.mark.anyio
    async def test_calls_storage_delete(
        self, router: ChannelRouter, mock_storage: MagicMock
    ) -> None:
        await router.delete_route("discord", "ch-123")
        mock_storage.delete_channel_route.assert_called_once_with(
            "discord", "ch-123", expected_ws_id=None
        )


class TestWorkstreamLiveness:
    @pytest.mark.anyio
    async def test_direct_mode_uses_manager_authoritative_active_list(
        self,
        router: ChannelRouter,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        assert router._server is not None
        mock_list = AsyncMock(
            return_value=MagicMock(
                workstreams=[
                    MagicMock(ws_id="other", state="idle"),
                    MagicMock(ws_id="ws-live", state="running"),
                    MagicMock(ws_id="ws-creating", state="creating"),
                ]
            )
        )
        monkeypatch.setattr(router._server, "list_workstreams", mock_list)

        assert await router._is_ws_live("ws-live") is True
        assert await router._is_ws_live("ws-cold") is False
        assert await router._is_ws_live("ws-creating") is False
        assert mock_list.await_count == 3

    @pytest.mark.anyio
    async def test_console_mode_uses_routed_live_probe(
        self,
        console_router: ChannelRouter,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        assert console_router._console is not None
        mock_live = AsyncMock(side_effect=[MagicMock(live=True), MagicMock(live=False)])
        monkeypatch.setattr(console_router._console, "route_workstream_live", mock_live)

        assert await console_router._is_ws_live("ws-live") is True
        assert await console_router._is_ws_live("ws-cold") is False
        assert [item.args[0] for item in mock_live.await_args_list] == ["ws-live", "ws-cold"]


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
        mock_storage.create_channel_route.assert_called_once_with(
            "discord", "ch-1", "ws-new", channel_user_id=""
        )
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
            return_value=RouteCreateResponse(
                ws_id="ws-new",
                name="test",
                node_url="http://node1:8080/v1",
                node_id="node-1",
                routing_strategy="rendezvous",
            )
        )
        monkeypatch.setattr(console_router._console, "route_create_workstream", mock_create)
        ws_id, is_new = await console_router.get_or_create_workstream(
            "discord", "ch-1", name="test"
        )
        assert ws_id == "ws-new"
        assert is_new is True
        mock_storage.create_channel_route.assert_called_once_with(
            "discord", "ch-1", "ws-new", channel_user_id=""
        )

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
        monkeypatch.setattr(router, "_is_ws_live", AsyncMock(return_value=True))
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
        # The durable source exists but is no longer loaded on the node.
        monkeypatch.setattr(router, "_is_ws_live", AsyncMock(return_value=False))
        # Server create returns a resumed workstream.
        assert router._server is not None
        mock_create = AsyncMock()
        mock_create.return_value = MagicMock(ws_id="ws-resumed", name="test")
        monkeypatch.setattr(router._server, "create_workstream", mock_create)

        ws_id, is_new = await router.get_or_create_workstream("discord", "ch-1", name="test")
        assert ws_id == "ws-resumed"
        assert is_new is True
        mock_storage.delete_channel_route.assert_not_called()
        mock_storage.replace_channel_route.assert_called_once_with(
            "discord", "ch-1", "ws-stale", "ws-resumed"
        )
        # The create call should include resume_ws pointing at the old ws.
        mock_create.assert_awaited_once()
        call_kwargs = mock_create.call_args[1]
        assert call_kwargs["resume_ws"] == "ws-stale"

    @pytest.mark.anyio
    async def test_missing_stale_source_retries_fresh_via_server(
        self,
        router: ChannelRouter,
        mock_storage: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        mock_storage.get_channel_route.return_value = {
            "ws_id": "ws-pruned",
            "channel_type": "discord",
            "channel_id": "ch-1",
        }
        mock_storage.get_workstream.side_effect = None
        mock_storage.get_workstream.return_value = None
        assert router._server is not None
        mock_create = AsyncMock(
            side_effect=[
                TurnstoneAPIError(404, "Workstream not found"),
                MagicMock(ws_id="ws-fresh", name="test"),
            ]
        )
        mock_send = AsyncMock()
        monkeypatch.setattr(router._server, "create_workstream", mock_create)
        monkeypatch.setattr(router._server, "send", mock_send)

        ws_id, is_new = await router.get_or_create_workstream(
            "discord",
            "ch-1",
            name="test",
            initial_message="hello",
        )

        assert (ws_id, is_new) == ("ws-fresh", True)
        assert [item.kwargs["resume_ws"] for item in mock_create.await_args_list] == [
            "ws-pruned",
            "",
        ]
        mock_send.assert_awaited_once_with("hello", "ws-fresh")
        mock_storage.replace_channel_route.assert_called_once_with(
            "discord", "ch-1", "ws-pruned", "ws-fresh"
        )

    @pytest.mark.anyio
    async def test_missing_stale_source_retries_fresh_via_console(
        self,
        console_router: ChannelRouter,
        mock_storage: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        mock_storage.get_channel_route.return_value = {
            "ws_id": "ws-pruned",
            "channel_type": "slack",
            "channel_id": "ch-1",
        }
        mock_storage.get_workstream.side_effect = None
        mock_storage.get_workstream.return_value = None
        assert console_router._console is not None
        mock_create = AsyncMock(
            side_effect=[
                TurnstoneAPIError(404, "Workstream not found"),
                RouteCreateResponse(
                    ws_id="ws-fresh",
                    name="test",
                    node_url="http://node2:8080/v1",
                    node_id="node-2",
                    routing_strategy="rendezvous",
                ),
            ]
        )
        mock_send = AsyncMock()
        monkeypatch.setattr(console_router._console, "route_create_workstream", mock_create)
        monkeypatch.setattr(console_router._console, "route_send", mock_send)

        ws_id, is_new = await console_router.get_or_create_workstream(
            "slack",
            "ch-1",
            name="test",
            initial_message="hello",
        )

        assert (ws_id, is_new) == ("ws-fresh", True)
        assert [item.kwargs["resume_ws"] for item in mock_create.await_args_list] == [
            "ws-pruned",
            "",
        ]
        mock_send.assert_awaited_once_with("hello", "ws-fresh")
        mock_storage.replace_channel_route.assert_called_once_with(
            "slack", "ch-1", "ws-pruned", "ws-fresh"
        )

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        ("status_code", "message"),
        [
            (404, "Workstream not found"),
            (403, "Forbidden"),
            (503, "Storage unavailable"),
            (409, "Fork source is no longer available"),
            (404, "Project not found"),
        ],
        ids=["masked-acl", "forbidden", "operational", "conflict", "other-not-found"],
    )
    @pytest.mark.parametrize("via_console", [False, True], ids=["server", "console"])
    async def test_stale_source_does_not_retry_other_failures(
        self,
        router: ChannelRouter,
        console_router: ChannelRouter,
        mock_storage: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
        status_code: int,
        message: str,
        via_console: bool,
    ) -> None:
        selected = console_router if via_console else router
        mock_storage.get_channel_route.return_value = {
            "ws_id": "ws-stale",
            "channel_type": "discord",
            "channel_id": "ch-1",
        }
        monkeypatch.setattr(selected, "_is_ws_live", AsyncMock(return_value=False))
        mock_create = AsyncMock(side_effect=TurnstoneAPIError(status_code, message))
        if selected._console is not None:
            monkeypatch.setattr(selected._console, "route_create_workstream", mock_create)
        else:
            assert selected._server is not None
            monkeypatch.setattr(selected._server, "create_workstream", mock_create)

        with pytest.raises(TurnstoneAPIError) as exc_info:
            await selected.get_or_create_workstream("discord", "ch-1")

        assert exc_info.value.status_code == status_code
        assert exc_info.value.message == message
        mock_create.assert_awaited_once()
        mock_storage.delete_channel_route.assert_not_called()
        mock_storage.create_channel_route.assert_not_called()

    @pytest.mark.anyio
    async def test_fresh_retry_is_attempted_only_once(
        self,
        router: ChannelRouter,
        mock_storage: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        mock_storage.get_channel_route.return_value = {
            "ws_id": "ws-pruned",
            "channel_type": "discord",
            "channel_id": "ch-1",
        }
        mock_storage.get_workstream.side_effect = None
        mock_storage.get_workstream.return_value = None
        assert router._server is not None
        error = TurnstoneAPIError(404, "Workstream not found")
        mock_create = AsyncMock(side_effect=[error, error])
        monkeypatch.setattr(router._server, "create_workstream", mock_create)

        with pytest.raises(TurnstoneAPIError, match="Workstream not found"):
            await router.get_or_create_workstream("discord", "ch-1")

        assert [item.kwargs["resume_ws"] for item in mock_create.await_args_list] == [
            "ws-pruned",
            "",
        ]
        mock_storage.create_channel_route.assert_not_called()

    @pytest.mark.anyio
    async def test_storage_lookup_failure_preserves_route(
        self,
        router: ChannelRouter,
        mock_storage: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        mock_storage.get_channel_route.return_value = {
            "ws_id": "ws-existing",
            "channel_type": "discord",
            "channel_id": "ch-1",
        }
        mock_storage.get_workstream.side_effect = RuntimeError("storage offline")
        assert router._server is not None
        mock_create = AsyncMock()
        monkeypatch.setattr(router._server, "create_workstream", mock_create)

        with pytest.raises(RuntimeError, match="storage offline"):
            await router.get_or_create_workstream("discord", "ch-1")

        mock_storage.delete_channel_route.assert_not_called()
        mock_create.assert_not_awaited()

    @pytest.mark.anyio
    @pytest.mark.parametrize("via_console", [False, True], ids=["server", "console"])
    async def test_live_probe_failure_preserves_route_without_creating(
        self,
        router: ChannelRouter,
        console_router: ChannelRouter,
        mock_storage: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
        via_console: bool,
    ) -> None:
        selected = console_router if via_console else router
        mock_storage.get_channel_route.return_value = {
            "ws_id": "ws-existing",
            "channel_type": "discord",
            "channel_id": "ch-1",
        }
        probe_error = TurnstoneAPIError(503, "route uncertain")
        monkeypatch.setattr(selected, "_is_ws_live", AsyncMock(side_effect=probe_error))
        mock_create = AsyncMock()
        if selected._console is not None:
            monkeypatch.setattr(selected._console, "route_create_workstream", mock_create)
        else:
            assert selected._server is not None
            monkeypatch.setattr(selected._server, "create_workstream", mock_create)

        with pytest.raises(TurnstoneAPIError, match="route uncertain"):
            await selected.get_or_create_workstream("discord", "ch-1")

        mock_storage.delete_channel_route.assert_not_called()
        mock_create.assert_not_awaited()

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
    @pytest.mark.parametrize("status", [403, 409, 503])
    async def test_close_refusal_propagates(self, router, monkeypatch, status):
        monkeypatch.setattr(
            router._server,
            "close_workstream",
            AsyncMock(side_effect=TurnstoneAPIError(status, "not closed")),
        )
        with pytest.raises(TurnstoneAPIError, match="not closed"):
            await router.close_workstream("ws-1")

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
    async def test_reconnect_refreshes_address_and_propagates_failures(
        self, console_router, monkeypatch
    ):
        lookup = AsyncMock(
            side_effect=[
                {"node_url": "http://old.example/"},
                {"node_url": "http://new.example/"},
                TurnstoneAPIError(503, "unavailable"),
                {},
            ]
        )
        monkeypatch.setattr(console_router._console, "route_lookup", lookup)
        assert await console_router.get_node_url("ws-1") == "http://old.example"
        assert await console_router.get_node_url("ws-1") == "http://new.example"
        with pytest.raises(TurnstoneAPIError, match="unavailable"):
            await console_router.get_node_url("ws-1")
        with pytest.raises(RuntimeError, match="no node URL"):
            await console_router.get_node_url("ws-1")

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
