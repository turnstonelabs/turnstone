"""Tests for _BaseClient._stream_sse — SSE stream parsing."""

from __future__ import annotations

import httpx
import pytest

from turnstone.sdk._base import _BaseClient


def _sse_response(*events: str) -> httpx.Response:
    """Build a mock SSE response from data strings."""
    body = ""
    for event in events:
        body += f"data: {event}\n\n"
    return httpx.Response(
        200,
        content=body.encode(),
        headers={"content-type": "text/event-stream"},
    )


@pytest.mark.anyio
async def test_stream_sse_yields_json():
    """SSE stream with valid JSON payloads yields parsed dicts."""

    def handler(request: httpx.Request) -> httpx.Response:
        return _sse_response(
            '{"type": "content", "text": "hello"}',
            '{"type": "stream_end"}',
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as hc:
        client = _BaseClient(httpx_client=hc)
        events = []
        async for data in client._stream_sse("/v1/api/events", params={"ws_id": "ws1"}):
            events.append(data)
        assert len(events) == 2
        assert events[0]["type"] == "content"
        assert events[1]["type"] == "stream_end"


@pytest.mark.anyio
async def test_stream_sse_skips_malformed_json():
    """Malformed JSON in SSE data is silently skipped."""

    def handler(request: httpx.Request) -> httpx.Response:
        return _sse_response(
            "not-json",
            '{"type": "content", "text": "ok"}',
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as hc:
        client = _BaseClient(httpx_client=hc)
        events = []
        async for data in client._stream_sse("/test"):
            events.append(data)
        assert len(events) == 1
        assert events[0]["type"] == "content"


@pytest.mark.anyio
async def test_stream_sse_skips_empty_data():
    """SSE frames with empty data field are skipped."""

    def handler(request: httpx.Request) -> httpx.Response:
        # Build response with an empty data line
        body = 'data: \n\ndata: {"type": "info", "message": "ok"}\n\n'
        return httpx.Response(
            200,
            content=body.encode(),
            headers={"content-type": "text/event-stream"},
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as hc:
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

    def handler(request: httpx.Request) -> httpx.Response:
        return _sse_response(*event_data)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as hc:
        client = _BaseClient(httpx_client=hc)
        events = []
        async for data in client._stream_sse("/test"):
            events.append(data)
        assert len(events) == 5
        types = [e["type"] for e in events]
        assert types == ["connected", "content", "content", "status", "stream_end"]
