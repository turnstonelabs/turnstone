"""Eager channel recovery spends platform requests only on live sessions."""

from __future__ import annotations

import asyncio
from collections import Counter
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from turnstone.channels._routing import ChannelRouter
from turnstone.sdk.server import AsyncTurnstoneServer

pytestmark = pytest.mark.filterwarnings(
    "ignore:.*'asyncio.iscoroutinefunction' is deprecated:DeprecationWarning"
)


@pytest.mark.anyio
@pytest.mark.parametrize("platform", ["discord", "slack"])
@pytest.mark.parametrize("initial_state", ["empty", "live", "unavailable"])
async def test_startup_only_subscribes_live_routes_and_retains_inactive_associations(
    db, platform, initial_state
):
    if platform == "discord":
        discord = pytest.importorskip("discord")
        from tests.test_channel_discord import _make_real_bot

        bot = _make_real_bot()
        thread = MagicMock(spec=discord.Thread)
        thread.parent_id = 222
        fetch = AsyncMock(return_value=thread)
        platform_patch = patch.object(bot._bot, "fetch_channel", new=fetch)
        cache_patch = patch.object(bot._bot, "get_channel", return_value=None)
    else:
        from contextlib import nullcontext

        from tests.test_channel_slack import _make_bot

        bot, _, _ = _make_bot()
        platform_patch = cache_patch = nullcontext()

    await bot.router.aclose()
    bot.storage = db
    bot.subscribe_ws = AsyncMock()
    for index in range(20):
        channel_id = str(1000 + index) if platform == "discord" else f"C1:U{index}:1000.000001"
        db.create_channel_route(platform, channel_id, f"ws-{index}", channel_user_id="111")

    state = initial_state
    list_calls = 0

    def handler(request):
        nonlocal list_calls
        assert request.method == "GET" and request.url.path == "/v1/api/workstreams"
        list_calls += 1
        if state == "unavailable":
            return httpx.Response(503, json={"error": "node unavailable"})
        workstreams = []
        if state == "live":
            workstreams = [
                {"ws_id": "ws-0", "name": "live", "state": "idle"},
                {"ws_id": "ws-1", "name": "pending", "state": "creating"},
            ]
        return httpx.Response(200, json={"workstreams": workstreams})

    router = ChannelRouter("http://server.example", db)
    await router.aclose()
    bot.router = router
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://server.example"
    ) as http:
        router._server = AsyncTurnstoneServer(httpx_client=http)
        try:
            with platform_patch, cache_patch:
                expected = 0
                for _ in range(2):
                    await bot._recover_routes()
                    expected += int(state == "live")
                    assert bot.subscribe_ws.await_count == expected
                    if platform == "discord":
                        assert fetch.await_count == expected
                    else:
                        assert len(bot._channel_sessions) == 20
                    assert len(db.list_channel_routes_by_type(platform)) == 20
                    if state == "unavailable":
                        state = "live"
                assert list_calls == 2
        finally:
            await bot.stop()


@pytest.mark.anyio
async def test_console_startup_discovery_bounds_time_and_concurrency(db, monkeypatch):
    monkeypatch.setattr("turnstone.channels._routing._STARTUP_PROBE_CONCURRENCY", 2)
    monkeypatch.setattr("turnstone.channels._routing._STARTUP_PROBE_TIMEOUT", 0.05)
    router = ChannelRouter("http://server.example", db, console_url="http://console.example")
    active = maximum = 0
    calls = Counter()

    async def probe(ws_id):
        nonlocal active, maximum
        calls[ws_id] += 1
        active += 1
        maximum = max(maximum, active)
        try:
            if ws_id == "blocked":
                await asyncio.Event().wait()
            if ws_id == "error":
                raise RuntimeError("node unavailable")
            return MagicMock(live=ws_id == "live")
        finally:
            active -= 1

    router._console.route_workstream_live = probe
    try:
        found = await asyncio.wait_for(
            router.get_live_workstream_ids(["blocked", "live", "error", "idle", "live"]), 1
        )
        assert found == {"live"}
        assert maximum == 2
        assert active == 0
        assert calls == {"blocked": 1, "live": 1, "error": 1, "idle": 1}
    finally:
        await router.aclose()
