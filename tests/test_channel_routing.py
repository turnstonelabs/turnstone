"""Tests for turnstone.channels._routing.ChannelRouter."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from turnstone.channels._routing import ChannelRouter


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


def _ok_response(json_data: object = None) -> httpx.Response:
    """Build a mock 200 response with optional JSON body."""
    import json

    content = json.dumps(json_data or {"status": "ok"}).encode()
    return httpx.Response(
        200,
        content=content,
        headers={"content-type": "application/json"},
        request=httpx.Request("POST", "http://test"),
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
    async def test_posts_to_server(
        self, router: ChannelRouter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock_post = AsyncMock(return_value=_ok_response())
        monkeypatch.setattr(router, "_post", mock_post)
        await router.send_message("ws-1", "hello world")
        mock_post.assert_awaited_once_with("/api/send", {"ws_id": "ws-1", "message": "hello world"})


class TestSendApproval:
    @pytest.mark.anyio
    async def test_posts_to_server(
        self, router: ChannelRouter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock_post = AsyncMock(return_value=_ok_response())
        monkeypatch.setattr(router, "_post", mock_post)
        await router.send_approval("ws-1", "corr-abc", approved=True, feedback="ok")
        mock_post.assert_awaited_once_with(
            "/api/approve",
            {"ws_id": "ws-1", "approved": True, "always": False, "feedback": "ok"},
        )

    @pytest.mark.anyio
    async def test_omits_empty_feedback(
        self, router: ChannelRouter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock_post = AsyncMock(return_value=_ok_response())
        monkeypatch.setattr(router, "_post", mock_post)
        await router.send_approval("ws-1", "corr-abc", approved=False)
        mock_post.assert_awaited_once_with(
            "/api/approve",
            {"ws_id": "ws-1", "approved": False, "always": False},
        )


class TestSendPlanFeedback:
    @pytest.mark.anyio
    async def test_posts_to_server(
        self, router: ChannelRouter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock_post = AsyncMock(return_value=_ok_response())
        monkeypatch.setattr(router, "_post", mock_post)
        await router.send_plan_feedback("ws-2", "corr-xyz", "looks good")
        mock_post.assert_awaited_once_with(
            "/api/plan",
            {"ws_id": "ws-2", "feedback": "looks good"},
        )


class TestDeleteRoute:
    @pytest.mark.anyio
    async def test_calls_storage_delete(
        self, router: ChannelRouter, mock_storage: MagicMock
    ) -> None:
        await router.delete_route("discord", "ch-123")
        mock_storage.delete_channel_route.assert_called_once_with("discord", "ch-123")


class TestGetOrCreateWorkstream:
    @pytest.mark.anyio
    async def test_creates_new_workstream(
        self,
        router: ChannelRouter,
        mock_storage: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        mock_post = AsyncMock(
            return_value=_ok_response({"ws_id": "ws-new", "name": "test", "resumed": False}),
        )
        monkeypatch.setattr(router, "_post", mock_post)
        ws_id, is_new = await router.get_or_create_workstream("discord", "ch-1", name="test")
        assert ws_id == "ws-new"
        assert is_new is True
        mock_storage.create_channel_route.assert_called_once_with("discord", "ch-1", "ws-new")

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
        # POST to create returns a new ws_id.
        create_resp = _ok_response({"ws_id": "ws-resumed", "name": "test", "resumed": True})
        captured: list[dict[str, Any]] = []

        async def _fake_post(path: str, body: dict[str, Any]) -> httpx.Response:
            captured.append({"path": path, "body": body})
            return create_resp

        monkeypatch.setattr(router, "_post", _fake_post)
        ws_id, is_new = await router.get_or_create_workstream("discord", "ch-1", name="test")
        assert ws_id == "ws-resumed"
        assert is_new is True
        # Should have deleted the stale route and created a new one.
        mock_storage.delete_channel_route.assert_called_once_with("discord", "ch-1")
        mock_storage.create_channel_route.assert_called_once_with("discord", "ch-1", "ws-resumed")
        # The create body should include resume_ws pointing at the old ws.
        create_call = captured[0]
        assert create_call["body"]["resume_ws"] == "ws-stale"


class TestCloseWorkstream:
    @pytest.mark.anyio
    async def test_posts_to_server(
        self, router: ChannelRouter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock_post = AsyncMock(return_value=_ok_response())
        monkeypatch.setattr(router, "_post", mock_post)
        await router.close_workstream("ws-1")
        mock_post.assert_awaited_once_with(
            "/api/workstreams/close",
            {"ws_id": "ws-1"},
        )


class TestAclose:
    @pytest.mark.anyio
    async def test_closes_client(
        self, router: ChannelRouter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock_close = AsyncMock()
        monkeypatch.setattr(router._client, "aclose", mock_close)
        await router.aclose()
        mock_close.assert_awaited_once()
