"""Bounded upload transport and cancellation at the disk boundary."""

from __future__ import annotations

import asyncio
import os
import threading
from unittest.mock import MagicMock

import httpx2
import pytest
from starlette.requests import Request

from tests.test_filesystem import _auth
from tests.test_filesystem import workspace as workspace
from tests.test_server_attachments_endpoints import _TEST_JWT_SECRET
from turnstone.core import filesystem as fs
from turnstone.core.auth import JWT_AUD_CONSOLE
from turnstone.core.web_helpers import BoundedUploadStream, UploadBodyError


def _request(messages, *, headers=()):
    pending = iter(messages)

    async def receive():
        return next(pending)

    return Request(
        {"type": "http", "method": "POST", "path": "/", "headers": list(headers)}, receive
    )


@pytest.mark.anyio
async def test_upload_chunks_are_bounded_and_cannot_be_replayed():
    original = b"a" * (150 * 1024)
    request = _request([{"type": "http.request", "body": original, "more_body": False}])
    stream = BoundedUploadStream(request, max_bytes=len(original))
    chunks = [chunk async for chunk in stream]
    assert b"".join(chunks) == original
    assert max(map(len, chunks)) <= 64 * 1024
    with pytest.raises(RuntimeError, match="cannot be replayed"):
        async for _ in stream:
            pass


@pytest.mark.anyio
async def test_short_declared_body_cannot_publish(workspace):
    request = _request(
        [{"type": "http.request", "body": b"short", "more_body": False}],
        headers=[(b"content-length", b"10")],
    )
    with pytest.raises(UploadBodyError) as exc:
        await fs.stream_upload_file("", "partial.txt", BoundedUploadStream(request, max_bytes=20))
    assert exc.value.status == 400
    assert not list(workspace.iterdir())


@pytest.mark.anyio
async def test_cancellation_waits_for_active_disk_write_before_cleanup(workspace, monkeypatch):
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original_write = os.write
    completed = []

    def slow_write(fd, data):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5), "test did not release disk writer"
        completed.append(original_write(fd, data))
        return completed[-1]

    monkeypatch.setattr(os, "write", slow_write)

    async def chunks():
        yield b"partial"
        pytest.fail("cancelled upload read another chunk")

    task = asyncio.create_task(fs.stream_upload_file("", "cancelled.txt", chunks()))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert len(list(workspace.glob(".file-upload-*/content"))) == 1
        assert not (workspace / "cancelled.txt").exists()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert completed == [len(b"partial")]
    assert not list(workspace.iterdir())


@pytest.mark.anyio
@pytest.mark.parametrize("wrapped", [False, True])
async def test_proxy_preserves_local_size_error_through_transport_exception(monkeypatch, wrapped):
    from turnstone.console.collector import ClusterCollector
    from turnstone.console.server import create_app

    collector = MagicMock(spec=ClusterCollector)
    collector.get_node_detail.return_value = {"server_url": "http://node.test"}
    app = create_app(collector=collector, jwt_secret=_TEST_JWT_SECRET)
    monkeypatch.setattr(fs, "UPLOAD_SIZE_CAP", 4)
    consumed = []

    async def chunks():
        for chunk in (b"123", b"456", b"unread"):
            consumed.append(chunk)
            yield chunk

    class FailingClient:
        timeout = httpx2.Timeout(30)

        async def request(self, method, target, *, content, **kwargs):
            assert not isinstance(content, bytes)
            try:
                async for _ in content:
                    pass
            except UploadBodyError as exc:
                if wrapped:
                    raise httpx2.WriteError("wrapped upload failure") from exc
                raise
            pytest.fail("over-limit upload reached EOF")

    app.state.proxy_client = FailingClient()
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app), base_url="http://console.test"
    ) as client:
        response = await client.post(
            "/node/n1/v1/api/workstreams/ws-A/files/upload?name=too-big.bin",
            content=chunks(),
            headers=_auth(
                "userA", permissions=("files.read", "files.write"), audience=JWT_AUD_CONSOLE
            ),
        )
    assert response.status_code == 413, response.text
    assert response.json()["code"] == "too_large"
    assert consumed == [b"123", b"456"]
