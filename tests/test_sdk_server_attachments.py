"""Tests for the attachment surface of turnstone.sdk.server (async + sync).

Uses ``httpx.MockTransport`` to record what the SDK sends so we can
assert on multipart bodies, the auto-generated ws_id, etc.
"""

from __future__ import annotations

import json
import re

import httpx
import pytest

from turnstone.sdk._types import AttachmentUpload
from turnstone.sdk.server import AsyncTurnstoneServer

PNG_1x1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xfc\xcf"
    b"\xc0\xc0\xc0\x00\x00\x00\x05\x00\x01\xa5\xf6E@\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _capturing_transport(response: httpx.Response) -> tuple[httpx.MockTransport, list]:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return response

    return httpx.MockTransport(handler), captured


# ---------------------------------------------------------------------------
# upload / list / get_content / delete
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_upload_attachment_sends_multipart():
    response = httpx.Response(
        200,
        json={
            "attachment_id": "att-1",
            "filename": "tiny.png",
            "mime_type": "image/png",
            "size_bytes": len(PNG_1x1),
            "kind": "image",
        },
    )
    transport, captured = _capturing_transport(response)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as hc:
        client = AsyncTurnstoneServer(httpx_client=hc)
        result = await client.upload_attachment("ws-X", "tiny.png", PNG_1x1, mime_type="image/png")
    assert result.attachment_id == "att-1"
    assert result.kind == "image"
    assert len(captured) == 1
    req = captured[0]
    assert req.method == "POST"
    assert req.url.path == "/v1/api/workstreams/ws-X/attachments"
    ct = req.headers.get("content-type", "")
    assert ct.startswith("multipart/form-data")
    body = bytes(req.content)
    assert b"tiny.png" in body
    assert PNG_1x1 in body


@pytest.mark.anyio
async def test_list_attachments_returns_pending():
    response = httpx.Response(
        200,
        json={
            "attachments": [
                {
                    "attachment_id": "att-1",
                    "filename": "a.txt",
                    "mime_type": "text/plain",
                    "size_bytes": 5,
                    "kind": "text",
                }
            ]
        },
    )
    transport, captured = _capturing_transport(response)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as hc:
        client = AsyncTurnstoneServer(httpx_client=hc)
        result = await client.list_attachments("ws-X")
    assert len(result.attachments) == 1
    assert result.attachments[0].attachment_id == "att-1"
    assert captured[0].method == "GET"


@pytest.mark.anyio
async def test_get_attachment_content_returns_bytes():
    response = httpx.Response(
        200,
        content=b"hello world",
        headers={"Content-Type": "text/plain; charset=utf-8"},
    )
    transport, captured = _capturing_transport(response)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as hc:
        client = AsyncTurnstoneServer(httpx_client=hc)
        data = await client.get_attachment_content("ws-X", "att-1")
    assert data == b"hello world"
    assert captured[0].url.path == "/v1/api/workstreams/ws-X/attachments/att-1/content"


@pytest.mark.anyio
async def test_delete_attachment():
    response = httpx.Response(200, json={"status": "deleted"})
    transport, captured = _capturing_transport(response)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as hc:
        client = AsyncTurnstoneServer(httpx_client=hc)
        result = await client.delete_attachment("ws-X", "att-1")
    assert result.status == "deleted"
    assert captured[0].method == "DELETE"


# ---------------------------------------------------------------------------
# send(attachment_ids=...)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_send_with_attachment_ids():
    response = httpx.Response(200, json={"status": "ok"})
    transport, captured = _capturing_transport(response)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as hc:
        client = AsyncTurnstoneServer(httpx_client=hc)
        await client.send("hi", "ws-X", attachment_ids=["a1", "a2"])
    body = json.loads(bytes(captured[0].content))
    assert body["attachment_ids"] == ["a1", "a2"]
    assert body["message"] == "hi"


@pytest.mark.anyio
async def test_send_omits_attachment_ids_when_none():
    response = httpx.Response(200, json={"status": "ok"})
    transport, captured = _capturing_transport(response)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as hc:
        client = AsyncTurnstoneServer(httpx_client=hc)
        await client.send("hi", "ws-X")
    body = json.loads(bytes(captured[0].content))
    assert "attachment_ids" not in body


# ---------------------------------------------------------------------------
# create_workstream(attachments=...)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_create_workstream_with_attachments_sends_multipart():
    response = httpx.Response(
        200,
        json={
            "ws_id": "00ff" + "0" * 28,
            "name": "demo",
            "resumed": False,
            "message_count": 0,
            "attachment_ids": ["att-1"],
        },
    )
    transport, captured = _capturing_transport(response)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as hc:
        client = AsyncTurnstoneServer(httpx_client=hc)
        resp = await client.create_workstream(
            name="demo",
            initial_message="describe",
            attachments=[AttachmentUpload(filename="hi.png", data=PNG_1x1, mime_type="image/png")],
        )
    assert resp.ws_id
    assert resp.attachment_ids == ["att-1"]
    req = captured[0]
    assert req.method == "POST"
    assert req.url.path == "/v1/api/workstreams/new"
    ct = req.headers.get("content-type", "")
    assert ct.startswith("multipart/form-data")

    body = bytes(req.content)
    # `meta` field carries the JSON metadata including the auto-generated ws_id
    meta_match = re.search(rb'name="meta"\r\n\r\n(\{[^}]*\})', body)
    assert meta_match, body
    meta = json.loads(meta_match.group(1))
    assert meta["name"] == "demo"
    assert meta["initial_message"] == "describe"
    assert re.fullmatch(r"[0-9a-f]{32}", meta["ws_id"])
    # PNG bytes appear in the body as a file part
    assert PNG_1x1 in body


@pytest.mark.anyio
async def test_create_workstream_caller_supplied_ws_id_used():
    response = httpx.Response(
        200,
        json={
            "ws_id": "deadbeef" * 4,
            "name": "demo",
            "resumed": False,
            "message_count": 0,
            "attachment_ids": [],
        },
    )
    transport, captured = _capturing_transport(response)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as hc:
        client = AsyncTurnstoneServer(httpx_client=hc)
        await client.create_workstream(
            name="demo",
            ws_id="deadbeef" * 4,
            attachments=[AttachmentUpload(filename="a.txt", data=b"hi")],
        )
    body = bytes(captured[0].content)
    meta_match = re.search(rb'name="meta"\r\n\r\n(\{[^}]*\})', body)
    assert meta_match
    meta = json.loads(meta_match.group(1))
    assert meta["ws_id"] == "deadbeef" * 4


@pytest.mark.anyio
async def test_create_workstream_without_attachments_uses_json():
    """Back-compat: callers that don't pass attachments still get the JSON path."""
    response = httpx.Response(200, json={"ws_id": "ws-json", "name": "j", "attachment_ids": []})
    transport, captured = _capturing_transport(response)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as hc:
        client = AsyncTurnstoneServer(httpx_client=hc)
        await client.create_workstream(name="j")
    req = captured[0]
    assert req.headers.get("content-type", "").startswith("application/json")
