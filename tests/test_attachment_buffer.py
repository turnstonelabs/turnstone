"""Tests for the per-node pending-upload buffer (turnstone.core.attachment_buffer)."""

from __future__ import annotations

import hashlib

from turnstone.core.attachment_buffer import (
    AttachmentBuffer,
    StagedAttachment,
    get_attachment_buffer,
)


def _stage(
    buf: AttachmentBuffer,
    *,
    content: bytes = b"hi",
    ws: str = "ws1",
    user: str = "u1",
    filename: str = "f.txt",
    mime: str = "text/plain",
    kind: str = "text",
) -> StagedAttachment:
    return buf.stage(
        ws_id=ws, user_id=user, filename=filename, mime_type=mime, kind=kind, content=content
    )


def test_stage_returns_content_hash_id_and_size() -> None:
    buf = AttachmentBuffer()
    entry = _stage(buf, content=b"hello")
    assert entry.attachment_id == hashlib.sha256(b"hello").hexdigest()
    assert entry.size_bytes == 5


def test_stage_is_idempotent_for_identical_bytes() -> None:
    buf = AttachmentBuffer()
    a = _stage(buf, content=b"same")
    b = _stage(buf, content=b"same")
    assert a.attachment_id == b.attachment_id
    assert len(buf.list_for(ws_id="ws1", user_id="u1")) == 1  # deduped by content hash


def test_get_enforces_scope() -> None:
    buf = AttachmentBuffer()
    entry = _stage(buf, ws="ws1", user="u1")
    assert buf.get(entry.attachment_id, ws_id="ws1", user_id="u1") is not None
    assert buf.get(entry.attachment_id, ws_id="ws2", user_id="u1") is None  # wrong ws
    assert buf.get(entry.attachment_id, ws_id="ws1", user_id="u2") is None  # wrong user


def test_list_for_scopes_by_ws_and_user() -> None:
    buf = AttachmentBuffer()
    _stage(buf, content=b"a", ws="ws1", user="u1")
    _stage(buf, content=b"b", ws="ws1", user="u1")
    _stage(buf, content=b"c", ws="ws2", user="u1")
    assert len(buf.list_for(ws_id="ws1", user_id="u1")) == 2
    assert len(buf.list_for(ws_id="ws2", user_id="u1")) == 1


def test_discard_is_scope_checked() -> None:
    buf = AttachmentBuffer()
    entry = _stage(buf)
    assert buf.discard(entry.attachment_id, ws_id="ws2", user_id="u1") is False
    assert buf.discard(entry.attachment_id, ws_id="ws1", user_id="u1") is True
    assert buf.get(entry.attachment_id, ws_id="ws1", user_id="u1") is None


def test_ttl_eviction_on_access() -> None:
    clock = [0.0]
    buf = AttachmentBuffer(ttl_seconds=10.0, clock=lambda: clock[0])
    _stage(buf, content=b"x")
    clock[0] = 11.0  # past the TTL
    assert buf.list_for(ws_id="ws1", user_id="u1") == []


def test_size_cap_evicts_oldest_first() -> None:
    clock = [0.0]
    buf = AttachmentBuffer(max_total_bytes=10, clock=lambda: clock[0])
    clock[0] = 1.0
    a = _stage(buf, content=b"aaaaa")  # 5 bytes
    clock[0] = 2.0
    b = _stage(buf, content=b"bbbbb")  # +5 → 10, at the ceiling
    clock[0] = 3.0
    c = _stage(buf, content=b"ccccc")  # +5 → 15 > 10 → evict oldest (a)
    ids = {e.attachment_id for e in buf.list_for(ws_id="ws1", user_id="u1")}
    assert a.attachment_id not in ids
    assert {b.attachment_id, c.attachment_id} <= ids


def test_singleton_getter_is_stable() -> None:
    assert get_attachment_buffer() is get_attachment_buffer()
