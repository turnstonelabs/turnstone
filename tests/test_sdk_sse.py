"""Tests for _BaseClient._stream_sse — SSE stream parsing."""

from __future__ import annotations

import functools
import json

import httpx2
import pytest

from turnstone.sdk._base import _BaseClient
from turnstone.sdk.events import HistoryResyncEvent, UserTurnEvent
from turnstone.sdk.server import AsyncTurnstoneServer


def _sse_response(*events: str) -> httpx2.Response:
    """Build a mock SSE response from data strings."""
    body = ""
    for event in events:
        body += f"data: {event}\n\n"
    return httpx2.Response(
        200,
        content=body.encode(),
        headers={"content-type": "text/event-stream"},
    )


@pytest.mark.anyio
async def test_stream_sse_yields_json():
    """SSE stream with valid JSON payloads yields parsed dicts."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        return _sse_response(
            '{"type": "content", "text": "hello"}',
            '{"type": "stream_end"}',
        )

    transport = httpx2.MockTransport(handler)
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as hc:
        client = _BaseClient(httpx_client=hc)
        events = []
        async for data in client._stream_sse("/v1/api/workstreams/ws1/events"):
            events.append(data)
        assert len(events) == 2
        assert events[0]["type"] == "content"
        assert events[1]["type"] == "stream_end"


@pytest.mark.anyio
async def test_stream_sse_skips_malformed_json():
    """Malformed JSON in SSE data is silently skipped."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        return _sse_response(
            "not-json",
            '{"type": "content", "text": "ok"}',
        )

    transport = httpx2.MockTransport(handler)
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as hc:
        client = _BaseClient(httpx_client=hc)
        events = []
        async for data in client._stream_sse("/test"):
            events.append(data)
        assert len(events) == 1
        assert events[0]["type"] == "content"


@pytest.mark.anyio
async def test_stream_sse_skips_overlong_integer_and_continues():
    from tests._deep_json import overlong_integer

    raw = '{"count":' + overlong_integer() + "}"
    transport = httpx2.MockTransport(
        lambda request: _sse_response(raw, '{"type":"content","text":"ok"}')
    )
    async with httpx2.AsyncClient(transport=transport, base_url="https://node.example.com") as hc:
        client = _BaseClient(httpx_client=hc)
        events = [data async for data in client._stream_sse("/test")]

    assert events == [{"type": "content", "text": "ok"}]


@pytest.mark.anyio
async def test_stream_sse_skips_empty_data():
    """SSE frames with empty data field are skipped."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        # Build response with an empty data line
        body = 'data: \n\ndata: {"type": "info", "message": "ok"}\n\n'
        return httpx2.Response(
            200,
            content=body.encode(),
            headers={"content-type": "text/event-stream"},
        )

    transport = httpx2.MockTransport(handler)
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as hc:
        client = _BaseClient(httpx_client=hc)
        events = []
        async for data in client._stream_sse("/test"):
            events.append(data)
        # Empty " " data is not valid JSON, so skipped; only "info" event remains
        assert len(events) == 1
        assert events[0]["type"] == "info"


@pytest.mark.anyio
async def test_stream_sse_multiple_events():
    """Multiple SSE events are yielded in order."""
    event_data = [
        '{"type": "connected", "model": "gpt-5"}',
        '{"type": "content", "text": "word1 "}',
        '{"type": "content", "text": "word2"}',
        '{"type": "status", "total_tokens": 10}',
        '{"type": "stream_end"}',
    ]

    def handler(request: httpx2.Request) -> httpx2.Response:
        return _sse_response(*event_data)

    transport = httpx2.MockTransport(handler)
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as hc:
        client = _BaseClient(httpx_client=hc)
        events = []
        async for data in client._stream_sse("/test"):
            events.append(data)
        assert len(events) == 5
        types = [e["type"] for e in events]
        assert types == ["connected", "content", "content", "status", "stream_end"]


@pytest.mark.anyio
async def test_stream_events_forwards_initial_history_handoff_hints():
    captured: dict[str, str] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        captured.update(dict(request.url.params))
        return _sse_response('{"type":"history_resync","ws_id":"ws1","reason":"handoff_mismatch"}')

    transport = httpx2.MockTransport(handler)
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as hc:
        client = AsyncTurnstoneServer(httpx_client=hc)
        events = [
            event
            async for event in client.stream_events(
                "ws1",
                last_event_id=0,
                history_token="epoch.7",
            )
        ]

    assert captured == {
        "user_turn": "1",
        "last_event_id": "0",
        "history_token": "epoch.7",
    }
    assert len(events) == 1
    assert isinstance(events[0], HistoryResyncEvent)
    assert events[0].reason == "handoff_mismatch"


@pytest.mark.anyio
async def test_stream_events_decodes_canonical_user_turn_wire_identity():
    """The capable SDK receives the payload identity, not only SSE transport id."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.params["user_turn"] == "1"
        return _sse_response(
            '{"type":"user_turn","ws_id":"ws1","content":"hello peer",'
            '"sender":"alice","client_send_ids":["send-1"],"_event_id":17}'
        )

    transport = httpx2.MockTransport(handler)
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as hc:
        client = AsyncTurnstoneServer(httpx_client=hc)
        events = [event async for event in client.stream_events("ws1", last_event_id=16)]

    assert events == [
        UserTurnEvent(
            ws_id="ws1",
            content="hello peer",
            sender="alice",
            client_send_ids=["send-1"],
            _event_id=17,
        )
    ]


@pytest.mark.anyio
async def test_a_static_token_reaches_the_event_stream(monkeypatch):
    # A static token lives only in the owned client's default headers, so the event stream gets
    # it only through httpx2 merging them into the request.
    seen = []

    def handler(request):
        seen.append(request.headers.get("authorization"))
        return _sse_response('{"type": "stream_end"}')

    monkeypatch.setattr(
        "turnstone.sdk._base.httpx2.AsyncClient",
        functools.partial(httpx2.AsyncClient, transport=httpx2.MockTransport(handler)),
    )
    async with _BaseClient(base_url="http://node.example", token="static-token") as client:
        [_ async for _ in client._stream_sse("/v1/api/workstreams/ws1/events")]
    assert seen == ["Bearer static-token"]


@pytest.mark.anyio
async def test_stream_sse_takes_an_event_over_httpx2s_default_cap():
    """An event past httpx2's 1 MiB default arrives: the client passes MAX_SSE_EVENT_SIZE."""
    text = "x" * (2 * 1024 * 1024)

    def handler(request: httpx2.Request) -> httpx2.Response:
        return _sse_response(json.dumps({"type": "content", "text": text}))

    transport = httpx2.MockTransport(handler)
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as hc:
        client = _BaseClient(httpx_client=hc)
        events = [data async for data in client._stream_sse("/v1/api/workstreams/ws1/events")]
    assert [event["text"] for event in events] == [text]


@pytest.mark.anyio
async def test_stream_sse_ends_at_an_event_over_the_cap():
    def handler(request: httpx2.Request) -> httpx2.Response:
        return _sse_response("x" * (16 * 1024 * 1024 + 1))

    transport = httpx2.MockTransport(handler)
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as hc:
        client = _BaseClient(httpx_client=hc)
        with pytest.raises(httpx2.SSEError):
            async for _ in client._stream_sse("/v1/api/workstreams/ws1/events"):
                pass
