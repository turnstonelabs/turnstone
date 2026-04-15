"""Tests for workstream_attachments storage layer."""

from __future__ import annotations

import uuid

import pytest


def _aid() -> str:
    return uuid.uuid4().hex


PNG_1x1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xfc\xcf"
    b"\xc0\xc0\xc0\x00\x00\x00\x05\x00\x01\xa5\xf6E@\x00\x00\x00\x00IEND\xaeB`\x82"
)


class TestSaveMessageReturnsId:
    def test_returns_autoincrement_id(self, backend):
        backend.register_workstream("ws-ret")
        m1 = backend.save_message("ws-ret", "user", "hello")
        m2 = backend.save_message("ws-ret", "assistant", "world")
        assert isinstance(m1, int)
        assert isinstance(m2, int)
        assert m1 > 0
        assert m2 > m1


class TestAttachmentCRUD:
    def test_save_then_list_pending(self, backend):
        backend.register_workstream("ws-a")
        aid = _aid()
        backend.save_attachment(
            aid, "ws-a", "user-1", "hello.txt", "text/plain", 5, "text", b"hello"
        )
        pending = backend.list_pending_attachments("ws-a", "user-1")
        assert len(pending) == 1
        row = pending[0]
        assert row["attachment_id"] == aid
        assert row["filename"] == "hello.txt"
        assert row["mime_type"] == "text/plain"
        assert row["size_bytes"] == 5
        assert row["kind"] == "text"
        # bytes must not leak into the pending-listing payload
        assert "content" not in row

    def test_list_pending_isolates_users(self, backend):
        backend.register_workstream("ws-iso")
        a1 = _aid()
        a2 = _aid()
        backend.save_attachment(a1, "ws-iso", "user-A", "a.txt", "text/plain", 1, "text", b"A")
        backend.save_attachment(a2, "ws-iso", "user-B", "b.txt", "text/plain", 1, "text", b"B")
        a_pending = backend.list_pending_attachments("ws-iso", "user-A")
        b_pending = backend.list_pending_attachments("ws-iso", "user-B")
        assert [r["attachment_id"] for r in a_pending] == [a1]
        assert [r["attachment_id"] for r in b_pending] == [a2]

    def test_get_attachments_bulk_returns_bytes(self, backend):
        backend.register_workstream("ws-b")
        a1 = _aid()
        a2 = _aid()
        backend.save_attachment(a1, "ws-b", "u", "one.txt", "text/plain", 3, "text", b"one")
        backend.save_attachment(
            a2, "ws-b", "u", "img.png", "image/png", len(PNG_1x1), "image", PNG_1x1
        )
        rows = backend.get_attachments([a1, a2])
        by_id = {r["attachment_id"]: r for r in rows}
        assert by_id[a1]["content"] == b"one"
        assert by_id[a2]["content"] == PNG_1x1
        assert by_id[a2]["kind"] == "image"

    def test_get_attachments_empty_input(self, backend):
        assert backend.get_attachments([]) == []

    def test_get_attachment_missing_returns_none(self, backend):
        assert backend.get_attachment("no-such-id") is None

    def test_delete_pending(self, backend):
        backend.register_workstream("ws-d")
        aid = _aid()
        backend.save_attachment(aid, "ws-d", "u", "x.txt", "text/plain", 1, "text", b"x")
        assert backend.delete_attachment(aid, "ws-d", "u") is True
        assert backend.list_pending_attachments("ws-d", "u") == []

    def test_delete_wrong_user_is_noop(self, backend):
        backend.register_workstream("ws-perm")
        aid = _aid()
        backend.save_attachment(aid, "ws-perm", "owner", "o.txt", "text/plain", 1, "text", b"o")
        assert backend.delete_attachment(aid, "ws-perm", "intruder") is False
        assert len(backend.list_pending_attachments("ws-perm", "owner")) == 1

    def test_delete_after_consumed_is_noop(self, backend):
        backend.register_workstream("ws-con")
        aid = _aid()
        backend.save_attachment(aid, "ws-con", "u", "c.txt", "text/plain", 1, "text", b"c")
        msg_id = backend.save_message("ws-con", "user", "hi")
        backend.mark_attachments_consumed([aid], msg_id, "ws-con", "u")
        assert backend.delete_attachment(aid, "ws-con", "u") is False
        row = backend.get_attachment(aid)
        assert row is not None
        assert row["message_id"] == msg_id


class TestConsumptionLinkage:
    def test_mark_consumed_links_message(self, backend):
        backend.register_workstream("ws-link")
        aid = _aid()
        backend.save_attachment(aid, "ws-link", "u", "f.txt", "text/plain", 1, "text", b"f")
        msg_id = backend.save_message("ws-link", "user", "with attach")
        backend.mark_attachments_consumed([aid], msg_id, "ws-link", "u")

        # No longer listed as pending
        assert backend.list_pending_attachments("ws-link", "u") == []
        # Second mark is a no-op (won't re-link to a different message)
        other_msg_id = backend.save_message("ws-link", "user", "another")
        backend.mark_attachments_consumed([aid], other_msg_id, "ws-link", "u")
        row = backend.get_attachment(aid)
        assert row is not None
        assert row["message_id"] == msg_id

    def test_mark_consumed_empty_input(self, backend):
        backend.mark_attachments_consumed([], 0, "ws", "u")  # must not raise

    def test_mark_consumed_wrong_user_is_noop(self, backend):
        backend.register_workstream("ws-scope")
        aid = _aid()
        backend.save_attachment(aid, "ws-scope", "owner", "o.txt", "text/plain", 1, "text", b"o")
        msg_id = backend.save_message("ws-scope", "user", "hi")
        # Different user tries to consume — must not link
        backend.mark_attachments_consumed([aid], msg_id, "ws-scope", "intruder")
        row = backend.get_attachment(aid)
        assert row is not None
        assert row["message_id"] is None

    def test_mark_consumed_wrong_ws_is_noop(self, backend):
        backend.register_workstream("ws-scope2")
        backend.register_workstream("ws-other")
        aid = _aid()
        backend.save_attachment(aid, "ws-scope2", "u", "x.txt", "text/plain", 1, "text", b"x")
        msg_id = backend.save_message("ws-other", "user", "hi")
        # Try to link to a message in a different ws — must not succeed
        backend.mark_attachments_consumed([aid], msg_id, "ws-other", "u")
        row = backend.get_attachment(aid)
        assert row is not None
        assert row["message_id"] is None


class TestLoadMessagesReconstructsMultipart:
    def test_user_message_with_image_and_text_doc(self, backend):
        backend.register_workstream("ws-multi")
        msg_id = backend.save_message("ws-multi", "user", "look at these")

        img_id = _aid()
        doc_id = _aid()
        backend.save_attachment(
            img_id,
            "ws-multi",
            "u",
            "tiny.png",
            "image/png",
            len(PNG_1x1),
            "image",
            PNG_1x1,
        )
        backend.save_attachment(
            doc_id,
            "ws-multi",
            "u",
            "notes.md",
            "text/markdown",
            5,
            "text",
            b"# hi\n",
        )
        backend.mark_attachments_consumed([img_id, doc_id], msg_id, "ws-multi", "u")

        msgs = backend.load_messages("ws-multi")
        assert len(msgs) == 1
        user_msg = msgs[0]
        assert user_msg["role"] == "user"
        content = user_msg["content"]
        assert isinstance(content, list)
        assert content[0] == {"type": "text", "text": "look at these"}
        # Image part: base64 data URI
        kinds = [p["type"] for p in content[1:]]
        assert "image_url" in kinds
        assert "document" in kinds
        img_part = next(p for p in content if p["type"] == "image_url")
        assert img_part["image_url"]["url"].startswith("data:image/png;base64,")
        doc_part = next(p for p in content if p["type"] == "document")
        assert doc_part["document"]["name"] == "notes.md"
        assert doc_part["document"]["media_type"] == "text/markdown"
        assert doc_part["document"]["data"] == "# hi\n"

    def test_user_message_without_attachments_stays_string(self, backend):
        backend.register_workstream("ws-plain")
        backend.save_message("ws-plain", "user", "plain text")
        msgs = backend.load_messages("ws-plain")
        assert msgs[0]["content"] == "plain text"

    def test_invalid_utf8_text_attachment_shows_placeholder(self, backend):
        backend.register_workstream("ws-bad")
        msg_id = backend.save_message("ws-bad", "user", "oops")
        aid = _aid()
        backend.save_attachment(aid, "ws-bad", "u", "bad.txt", "text/plain", 2, "text", b"\xff\xfe")
        backend.mark_attachments_consumed([aid], msg_id, "ws-bad", "u")
        msgs = backend.load_messages("ws-bad")
        # Undecodable text → placeholder so the user sees the attachment existed
        content = msgs[0]["content"]
        assert isinstance(content, list)
        assert content[0] == {"type": "text", "text": "oops"}
        assert content[1] == {"type": "text", "text": "[unreadable attachment: bad.txt]"}


class TestDeleteWorkstreamCascade:
    def test_attachments_removed_on_workstream_delete(self, backend):
        backend.register_workstream("ws-cas")
        aid = _aid()
        backend.save_attachment(aid, "ws-cas", "u", "a.txt", "text/plain", 1, "text", b"a")
        msg_id = backend.save_message("ws-cas", "user", "hi")
        backend.mark_attachments_consumed([aid], msg_id, "ws-cas", "u")

        assert backend.delete_workstream("ws-cas") is True
        assert backend.get_attachment(aid) is None

    def test_pending_attachments_also_cascade(self, backend):
        backend.register_workstream("ws-cas2")
        pending = _aid()
        consumed = _aid()
        backend.save_attachment(pending, "ws-cas2", "u", "p.txt", "text/plain", 1, "text", b"p")
        backend.save_attachment(consumed, "ws-cas2", "u", "c.txt", "text/plain", 1, "text", b"c")
        msg_id = backend.save_message("ws-cas2", "user", "hi")
        backend.mark_attachments_consumed([consumed], msg_id, "ws-cas2", "u")

        assert backend.delete_workstream("ws-cas2") is True
        assert backend.get_attachment(pending) is None
        assert backend.get_attachment(consumed) is None


class TestReconstructMetaSibling:
    def test_reconstructed_user_msg_carries_attachments_meta(self, backend):
        backend.register_workstream("ws-meta")
        aid = _aid()
        backend.save_attachment(aid, "ws-meta", "u", "doc.md", "text/markdown", 2, "text", b"hi")
        mid = backend.save_message("ws-meta", "user", "see this")
        backend.mark_attachments_consumed([aid], mid, "ws-meta", "u")

        msgs = backend.load_messages("ws-meta")
        assert len(msgs) == 1
        meta = msgs[0].get("_attachments_meta")
        assert isinstance(meta, list) and len(meta) == 1
        assert meta[0] == {
            "kind": "text",
            "filename": "doc.md",
            "mime_type": "text/markdown",
        }


class TestReservation:
    def test_reserve_excludes_from_pending_listing(self, backend):
        backend.register_workstream("ws-res1")
        aid = _aid()
        backend.save_attachment(aid, "ws-res1", "u", "a.md", "text/plain", 1, "text", b"a")
        assert len(backend.list_pending_attachments("ws-res1", "u")) == 1
        reserved = backend.reserve_attachments([aid], "q-1", "ws-res1", "u")
        assert reserved == [aid]
        # Reserved row must be hidden from the pending list
        assert backend.list_pending_attachments("ws-res1", "u") == []
        # And from the with-content variant used by auto-consume
        assert backend.get_pending_attachments_with_content("ws-res1", "u") == []

    def test_reserve_blocks_delete(self, backend):
        backend.register_workstream("ws-res2")
        aid = _aid()
        backend.save_attachment(aid, "ws-res2", "u", "a.md", "text/plain", 1, "text", b"a")
        backend.reserve_attachments([aid], "q-1", "ws-res2", "u")
        # Reserved attachment cannot be deleted — the user must dequeue
        # the queued message first.
        assert backend.delete_attachment(aid, "ws-res2", "u") is False
        assert backend.get_attachment(aid) is not None

    def test_reserve_twice_is_idempotent_first_wins(self, backend):
        backend.register_workstream("ws-res3")
        aid = _aid()
        backend.save_attachment(aid, "ws-res3", "u", "a.md", "text/plain", 1, "text", b"a")
        assert backend.reserve_attachments([aid], "q-1", "ws-res3", "u") == [aid]
        # Second reservation for a different queue msg must not steal
        assert backend.reserve_attachments([aid], "q-2", "ws-res3", "u") == []
        row = backend.get_attachment(aid)
        assert row["reserved_for_msg_id"] == "q-1"

    def test_unreserve_returns_to_pending(self, backend):
        backend.register_workstream("ws-res4")
        aid = _aid()
        backend.save_attachment(aid, "ws-res4", "u", "a.md", "text/plain", 1, "text", b"a")
        backend.reserve_attachments([aid], "q-1", "ws-res4", "u")
        backend.unreserve_attachments("q-1", "ws-res4", "u")
        # Back to pending — delete and listing work again
        assert len(backend.list_pending_attachments("ws-res4", "u")) == 1
        row = backend.get_attachment(aid)
        assert row["reserved_for_msg_id"] is None

    def test_consume_clears_reservation(self, backend):
        backend.register_workstream("ws-res5")
        aid = _aid()
        backend.save_attachment(aid, "ws-res5", "u", "a.md", "text/plain", 1, "text", b"a")
        backend.reserve_attachments([aid], "q-1", "ws-res5", "u")
        mid = backend.save_message("ws-res5", "user", "go")
        backend.mark_attachments_consumed([aid], mid, "ws-res5", "u")
        row = backend.get_attachment(aid)
        # Transition reserved → consumed clears the reservation
        assert row["message_id"] == mid
        assert row["reserved_for_msg_id"] is None

    def test_reserve_scoped_to_owner(self, backend):
        backend.register_workstream("ws-res6")
        aid = _aid()
        backend.save_attachment(aid, "ws-res6", "owner", "a.md", "text/plain", 1, "text", b"a")
        # An intruder user_id cannot reserve someone else's attachment
        assert backend.reserve_attachments([aid], "q-x", "ws-res6", "intruder") == []
        row = backend.get_attachment(aid)
        assert row["reserved_for_msg_id"] is None


class TestGetAttachmentsRobustness:
    def test_mixed_known_and_unknown_ids(self, backend):
        backend.register_workstream("ws-mix")
        known = _aid()
        unknown = _aid()
        backend.save_attachment(known, "ws-mix", "u", "k.txt", "text/plain", 1, "text", b"k")
        rows = backend.get_attachments([known, unknown, "definitely-not-an-id"])
        assert len(rows) == 1
        assert rows[0]["attachment_id"] == known


class TestRewindTruncationCascadesAttachments:
    def test_delete_messages_after_removes_linked_attachments(self, backend):
        backend.register_workstream("ws-rewind")
        # Two user turns, each with an attachment.  A rewind that keeps
        # only the first turn's messages must also drop the second
        # turn's attachment rather than leak the BLOB.
        a1 = _aid()
        a2 = _aid()
        backend.save_attachment(a1, "ws-rewind", "u", "keep.md", "text/plain", 1, "text", b"k")
        m1 = backend.save_message("ws-rewind", "user", "turn1")
        backend.mark_attachments_consumed([a1], m1, "ws-rewind", "u")

        backend.save_attachment(a2, "ws-rewind", "u", "drop.md", "text/plain", 1, "text", b"d")
        m2 = backend.save_message("ws-rewind", "user", "turn2")
        backend.mark_attachments_consumed([a2], m2, "ws-rewind", "u")

        # Keep only the first conversation row
        backend.delete_messages_after("ws-rewind", 1)

        # Kept attachment survives
        assert backend.get_attachment(a1) is not None
        # Doomed attachment is gone — no orphan BLOB
        assert backend.get_attachment(a2) is None

    def test_delete_messages_after_preserves_pending(self, backend):
        # Pending (un-consumed) attachments must not be touched by a
        # truncation — they have no message_id and shouldn't be swept
        # up by the cascade.
        backend.register_workstream("ws-rewind2")
        pending = _aid()
        consumed = _aid()
        backend.save_attachment(pending, "ws-rewind2", "u", "p.md", "text/plain", 1, "text", b"p")
        backend.save_attachment(consumed, "ws-rewind2", "u", "c.md", "text/plain", 1, "text", b"c")
        m1 = backend.save_message("ws-rewind2", "user", "turn1")
        backend.mark_attachments_consumed([consumed], m1, "ws-rewind2", "u")

        backend.delete_messages_after("ws-rewind2", 0)  # drop everything

        # Pending survives (no message_id → no cascade match)
        assert backend.get_attachment(pending) is not None
        # Consumed is dropped with its parent message
        assert backend.get_attachment(consumed) is None


@pytest.mark.parametrize("kind", ["image", "text"])
class TestParametrizedKind:
    def test_roundtrip_content_bytes(self, backend, kind):
        backend.register_workstream(f"ws-p-{kind}")
        aid = _aid()
        payload = PNG_1x1 if kind == "image" else b"x" * 42
        mime = "image/png" if kind == "image" else "text/plain"
        backend.save_attachment(
            aid, f"ws-p-{kind}", "u", f"f.{kind}", mime, len(payload), kind, payload
        )
        rows = backend.get_attachments([aid])
        assert len(rows) == 1
        assert rows[0]["content"] == payload
        assert rows[0]["kind"] == kind
