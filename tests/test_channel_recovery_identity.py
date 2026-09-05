"""Channel recovery cannot substitute another conversation's identity."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from tests._helpers import wait_until
from tests.test_server_authz import _auth
from tests.test_server_authz import app_client as app_client
from turnstone.core.storage import get_storage
from turnstone.sdk.server import AsyncTurnstoneServer

pytestmark = pytest.mark.filterwarnings(
    "ignore:.*'asyncio.iscoroutinefunction' is deprecated:DeprecationWarning"
)


@pytest.mark.parametrize("exact", [False, True])
def test_exact_resume_is_opt_in_for_public_alias_api(app_client, exact):
    client, manager = app_client
    alias = "f" * 32
    source = client.post("/v1/api/workstreams/new", json={}, headers=_auth("owner")).json()["ws_id"]
    manager.close(source)
    response = client.post(
        f"/v1/api/workstreams/{source}/title", json={"title": alias}, headers=_auth("owner")
    )
    assert response.status_code == 200
    response = client.post(
        "/v1/api/workstreams/new",
        json={"resume_ws": alias, "resume_ws_exact": exact},
        headers=_auth("owner"),
    )
    assert response.status_code == (404 if exact else 200), response.text
    if not exact:
        assert response.json()["resumed"] is True


@pytest.mark.parametrize(
    ("value", "error"),
    [("true", "resume_ws_exact must be a boolean"), (True, "resume_ws_exact requires resume_ws")],
)
def test_exact_resume_validates_input(app_client, value, error):
    client, _ = app_client
    response = client.post(
        "/v1/api/workstreams/new", json={"resume_ws_exact": value}, headers=_auth("owner")
    )
    assert response.status_code == 400
    assert response.json() == {"error": error}


@pytest.mark.anyio
@pytest.mark.parametrize("substitution", ["alias-before-message", "alias-before-create", "same-id"])
async def test_retained_channel_cannot_adopt_another_users_workstream(app_client, substitution):
    discord = pytest.importorskip("discord")
    from tests.test_channel_discord import _make_message
    from turnstone.channels.discord.bot import TurnstoneBot
    from turnstone.channels.discord.cog import MessageCog
    from turnstone.channels.discord.config import DiscordConfig

    client, manager = app_client
    storage = get_storage()
    assert storage is not None
    service = _auth("channel-gateway", scopes=frozenset({"read", "write", "approve", "service"}))
    source = client.post("/v1/api/workstreams/new", json={}, headers=service).json()["ws_id"]
    attacker = client.post("/v1/api/workstreams/new", json={}, headers=_auth("attacker")).json()[
        "ws_id"
    ]
    storage.save_message(attacker, "user", "ATTACKER-CONTROLLED SAVED HISTORY")
    storage.create_channel_user("discord", "111", "victim")
    storage.create_channel_route("discord", "555", source, channel_user_id="111")
    manager.close(source)
    manager.close(attacker)

    def delete_source():
        response = client.post(f"/v1/api/workstreams/{source}/delete", headers=service)
        assert response.status_code == 200, response.text

    def install_alias():
        delete_source()
        response = client.post(
            f"/v1/api/workstreams/{attacker}/title",
            json={"title": source},
            headers=_auth("attacker"),
        )
        assert response.status_code == 200, response.text

    bot = TurnstoneBot(DiscordConfig(allowed_channels=[222]), "http://server.example", storage)
    await bot.router.aclose()
    await bot._http_client.aclose()
    bot.subscribe_ws, bot.unsubscribe_ws = AsyncMock(), AsyncMock()
    thread = MagicMock(spec=discord.Thread)
    thread.id, thread.parent_id, thread.owner_id, thread.name = 555, 222, 99999, "conversation"
    thread.send = AsyncMock()
    message = _make_message(guild=True, channel=thread)
    message.author.id, message.content = 111, "Victim next message"
    cog = MessageCog(bot._bot)
    create_bodies = []

    async def before_request(request):
        if request.url.path.endswith("/workstreams/new"):
            body = json.loads(request.content)
            create_bodies.append(body)
            if substitution == "alias-before-create" and body.get("resume_ws") == source:
                assert storage.get_workstream(source) is not None
                install_alias()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=client.app),
        base_url="http://server.example",
        headers=service,
        event_hooks={"request": [before_request]},
    ) as http:
        bot._http_client, bot.router._server = http, AsyncTurnstoneServer(httpx_client=http)
        try:
            await asyncio.wait_for(bot._sse_listener(source, thread), 2)
            assert storage.get_channel_route("discord", "555")["ws_id"] == source
            if substitution == "alias-before-message":
                install_alias()
            elif substitution == "same-id":
                delete_source()
                response = client.post(
                    "/v1/api/workstreams/new",
                    json={"ws_id": source},
                    headers=_auth("attacker"),
                )
                assert response.status_code == 409, response.text
                assert storage.get_workstream(source) is None

            await cog._on_message(message)
            route = storage.get_channel_route("discord", "555")
            destination = route["ws_id"]
            assert destination not in {source, attacker}
            assert route["channel_user_id"] == "111"
            assert create_bodies[0]["resume_ws"] == source
            assert create_bodies[0]["resume_ws_exact"] is True
            assert "resume_ws" not in create_bodies[1]
            session = manager.get(destination).session
            await asyncio.to_thread(wait_until, lambda: bool(session.sends))
            assert session.sends[0][0] == "Victim next message"
            assert not session.fork_calls
            assert all(
                item["content"] != "ATTACKER-CONTROLLED SAVED HISTORY"
                for item in storage.load_messages(destination)
            )
        finally:
            await bot.router.aclose()
            await bot._bot.close()
            for ws in manager.list_all():
                manager.close(ws.id)
