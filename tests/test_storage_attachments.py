"""Tests for the content-addressed, refcounted workstream_attachments store.

The pre-cutover persisted pending/reserved/consumed lifecycle (message_id /
reserved_* + the per-user upload cap) is gone — pending uploads now live in the
per-node in-memory buffer (see ``test_attachment_buffer.py``), and storage holds
only committed blobs: written content-addressed at send-commit, deduped by
content hash, and reference-counted via the ``conversations.attachments``
ref-list.  These tests pin that model at the storage boundary.
"""

from __future__ import annotations

import hashlib

import pytest

PNG_1x1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xfc\xcf"
    b"\xc0\xc0\xc0\x00\x00\x00\x05\x00\x01\xa5\xf6E@\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _hash(content: bytes) -> str:
    """The content-addressed id: bytes' sha256 hex (what the buffer computes)."""
    return hashlib.sha256(content).hexdigest()


class TestSaveMessageReturnsId:
    def test_returns_autoincrement_id(self, backend):
        backend.register_workstream("ws-ret")
        m1 = backend.save_message("ws-ret", "user", "hello")
        m2 = backend.save_message("ws-ret", "assistant", "world")
        assert isinstance(m1, int)
        assert isinstance(m2, int)
        assert m1 > 0
        assert m2 > m1


class TestContentAddressedWrite:
    def test_save_writes_blob_at_refcount_one(self, backend):
        backend.register_workstream("ws-ca")
        aid = _hash(b"hello")
        backend.save_attachment(aid, "hello.txt", "text/plain", 5, "text", b"hello")
        row = backend.get_attachment(aid)
        assert row is not None
        assert row["attachment_id"] == aid
        assert row["content"] == b"hello"
        assert row["refcount"] == 1
        assert row["origin"] == "upload"

    def test_origin_tool_recorded(self, backend):
        backend.register_workstream("ws-origin")
        aid = _hash(PNG_1x1)
        backend.save_attachment(aid, "t.png", "image/png", len(PNG_1x1), "image", PNG_1x1, "tool")
        row = backend.get_attachment(aid)
        assert row is not None
        assert row["origin"] == "tool"
        assert row["refcount"] == 1

    def test_identical_bytes_dedup_and_bump_refcount(self, backend):
        backend.register_workstream("ws-dedup")
        aid = _hash(b"same")
        backend.save_attachment(aid, "a.txt", "text/plain", 4, "text", b"same")
        # A second reference to identical bytes does not duplicate the row —
        # it bumps the refcount (e.g. two messages reference the same blob).
        backend.save_attachment(aid, "b.txt", "text/plain", 4, "text", b"same")
        rows = backend.get_attachments([aid])
        assert len(rows) == 1
        assert rows[0]["refcount"] == 2
        # First-writer metadata wins (INSERT-OR-IGNORE on the blob).
        assert rows[0]["filename"] == "a.txt"

    def test_distinct_bytes_are_distinct_blobs(self, backend):
        backend.register_workstream("ws-distinct")
        a1 = _hash(b"one")
        a2 = _hash(b"two")
        backend.save_attachment(a1, "1.txt", "text/plain", 3, "text", b"one")
        backend.save_attachment(a2, "2.txt", "text/plain", 3, "text", b"two")
        assert a1 != a2
        rows = {r["attachment_id"]: r for r in backend.get_attachments([a1, a2])}
        assert rows[a1]["content"] == b"one"
        assert rows[a2]["content"] == b"two"


class TestGetAttachments:
    def test_bulk_returns_bytes(self, backend):
        backend.register_workstream("ws-b")
        a1 = _hash(b"one")
        a2 = _hash(PNG_1x1)
        backend.save_attachment(a1, "one.txt", "text/plain", 3, "text", b"one")
        backend.save_attachment(a2, "img.png", "image/png", len(PNG_1x1), "image", PNG_1x1)
        by_id = {r["attachment_id"]: r for r in backend.get_attachments([a1, a2])}
        assert by_id[a1]["content"] == b"one"
        assert by_id[a2]["content"] == PNG_1x1
        assert by_id[a2]["kind"] == "image"

    def test_empty_input(self, backend):
        assert backend.get_attachments([]) == []

    def test_mixed_known_and_unknown_ids(self, backend):
        backend.register_workstream("ws-mix")
        known = _hash(b"k")
        backend.save_attachment(known, "k.txt", "text/plain", 1, "text", b"k")
        rows = backend.get_attachments([known, _hash(b"nope"), "definitely-not-an-id"])
        assert len(rows) == 1
        assert rows[0]["attachment_id"] == known

    def test_get_attachment_missing_returns_none(self, backend):
        assert backend.get_attachment("no-such-id") is None


class TestSetMessageAttachments:
    def test_records_ordered_ref_list(self, backend):
        import json

        import sqlalchemy as sa

        from turnstone.core.storage._schema import conversations

        backend.register_workstream("ws-ref")
        mid = backend.save_message("ws-ref", "user", "hi")
        a1, a2 = _hash(b"x"), _hash(b"y")
        backend.save_attachment(a1, "x.txt", "text/plain", 1, "text", b"x")
        backend.save_attachment(a2, "y.txt", "text/plain", 1, "text", b"y")
        backend.set_message_attachments("ws-ref", mid, [a2, a1])  # order matters
        with backend._conn() as conn:
            raw = conn.execute(
                sa.select(conversations.c.attachments).where(conversations.c.id == mid)
            ).scalar_one()
        assert json.loads(raw) == [a2, a1]

    def test_empty_input_is_noop(self, backend):
        backend.register_workstream("ws-ref2")
        mid = backend.save_message("ws-ref2", "user", "hi")
        backend.set_message_attachments("ws-ref2", mid, [])  # must not raise
        # Column stays NULL → load yields a plain string message.
        assert backend.load_messages("ws-ref2")[0]["content"] == "hi"

    def test_scoped_to_ws(self, backend):
        # A cross-ws message id is not written (defense-in-depth).
        import sqlalchemy as sa

        from turnstone.core.storage._schema import conversations

        backend.register_workstream("ws-a")
        backend.register_workstream("ws-b")
        mid = backend.save_message("ws-a", "user", "hi")
        aid = _hash(b"x")
        backend.save_attachment(aid, "x.txt", "text/plain", 1, "text", b"x")
        backend.set_message_attachments("ws-b", mid, [aid])  # wrong ws
        with backend._conn() as conn:
            raw = conn.execute(
                sa.select(conversations.c.attachments).where(conversations.c.id == mid)
            ).scalar_one()
        assert raw is None


class TestLoadMessagesReconstructsMultipart:
    def test_user_message_with_image_and_text_doc(self, backend):
        backend.register_workstream("ws-multi")
        msg_id = backend.save_message("ws-multi", "user", "look at these")
        img_id = _hash(PNG_1x1)
        doc_id = _hash(b"# hi\n")
        backend.save_attachment(img_id, "tiny.png", "image/png", len(PNG_1x1), "image", PNG_1x1)
        backend.save_attachment(doc_id, "notes.md", "text/markdown", 5, "text", b"# hi\n")
        backend.set_message_attachments("ws-multi", msg_id, [img_id, doc_id])

        msgs = backend.load_messages("ws-multi")
        assert len(msgs) == 1
        user_msg = msgs[0]
        assert user_msg["role"] == "user"
        content = user_msg["content"]
        assert isinstance(content, list)
        assert content[0] == {"type": "text", "text": "look at these"}
        kinds = [p["type"] for p in content[1:]]
        assert "image_url" in kinds
        assert "document" in kinds
        img_part = next(p for p in content if p["type"] == "image_url")
        assert img_part["image_url"]["url"].startswith("data:image/png;base64,")
        doc_part = next(p for p in content if p["type"] == "document")
        assert doc_part["document"]["name"] == "notes.md"
        assert doc_part["document"]["media_type"] == "text/markdown"
        assert doc_part["document"]["data"] == "# hi\n"

    def test_ref_list_order_preserved(self, backend):
        backend.register_workstream("ws-order")
        mid = backend.save_message("ws-order", "user", "ordered")
        a, b = _hash(b"AAA"), _hash(b"BBB")
        backend.save_attachment(a, "a.md", "text/markdown", 3, "text", b"AAA")
        backend.save_attachment(b, "b.md", "text/markdown", 3, "text", b"BBB")
        # Record b before a — reconstruction must follow the ref-list order.
        backend.set_message_attachments("ws-order", mid, [b, a])
        docs = [
            p for p in backend.load_messages("ws-order")[0]["content"] if p["type"] == "document"
        ]
        assert [d["document"]["data"] for d in docs] == ["BBB", "AAA"]

    def test_user_message_without_attachments_stays_string(self, backend):
        backend.register_workstream("ws-plain")
        backend.save_message("ws-plain", "user", "plain text")
        assert backend.load_messages("ws-plain")[0]["content"] == "plain text"

    def test_invalid_utf8_text_attachment_shows_placeholder(self, backend):
        backend.register_workstream("ws-bad")
        msg_id = backend.save_message("ws-bad", "user", "oops")
        aid = _hash(b"\xff\xfe")
        backend.save_attachment(aid, "bad.txt", "text/plain", 2, "text", b"\xff\xfe")
        backend.set_message_attachments("ws-bad", msg_id, [aid])
        content = backend.load_messages("ws-bad")[0]["content"]
        assert isinstance(content, list)
        assert content[0] == {"type": "text", "text": "oops"}
        assert content[1] == {"type": "text", "text": "[unreadable attachment: bad.txt]"}

    def test_missing_blob_is_skipped(self, backend):
        # A ref-list id whose blob was pruned (refcount hit 0 via another
        # message's GC) reconstructs as plain text, not a crash.
        backend.register_workstream("ws-missing")
        mid = backend.save_message("ws-missing", "user", "gone")
        backend.set_message_attachments("ws-missing", mid, [_hash(b"never-written")])
        assert backend.load_messages("ws-missing")[0]["content"] == "gone"


class TestToolImageReconstruction:
    def test_tool_row_with_image_rebuilds_multipart(self, backend):
        """Tool vision output is persisted content-addressed + referenced on the
        tool row, so a reload rebuilds the multipart [text, image_url] content
        (role-agnostic reconstruction) rather than the flattened text alone."""
        backend.register_workstream("ws-tool")
        # assistant(tool_calls) → tool row, mirroring a real read_image turn.
        import json

        backend.save_message(
            "ws-tool",
            "assistant",
            None,
            tool_calls=json.dumps(
                [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": "{}"},
                    }
                ]
            ),
        )
        tool_mid = backend.save_message(
            "ws-tool", "tool", "Image file: dog.png", "read_file", tool_call_id="c1"
        )
        img_id = _hash(PNG_1x1)
        backend.save_attachment(
            img_id,
            "read_file-image.png",
            "image/png",
            len(PNG_1x1),
            "image",
            PNG_1x1,
            "tool",
        )
        backend.set_message_attachments("ws-tool", tool_mid, [img_id])

        msgs = backend.load_messages("ws-tool")
        tool_msg = next(m for m in msgs if m["role"] == "tool")
        assert tool_msg["tool_call_id"] == "c1"
        content = tool_msg["content"]
        assert isinstance(content, list)
        assert content[0] == {"type": "text", "text": "Image file: dog.png"}
        assert content[1]["type"] == "image_url"
        assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")

    def test_tool_row_without_attachments_stays_string(self, backend):
        backend.register_workstream("ws-tool2")
        backend.save_message("ws-tool2", "tool", "plain output", "bash", tool_call_id="c9")
        tool_msg = backend.load_messages("ws-tool2", repair=False)[0]
        assert tool_msg["content"] == "plain output"


class TestReconstructMetaSibling:
    def test_reconstructed_user_msg_carries_attachments_meta(self, backend):
        backend.register_workstream("ws-meta")
        aid = _hash(b"hi")
        backend.save_attachment(aid, "doc.md", "text/markdown", 2, "text", b"hi")
        mid = backend.save_message("ws-meta", "user", "see this")
        backend.set_message_attachments("ws-meta", mid, [aid])
        meta = backend.load_messages("ws-meta")[0].get("_attachments_meta")
        assert isinstance(meta, list) and len(meta) == 1
        assert meta[0] == {
            "kind": "text",
            "filename": "doc.md",
            "mime_type": "text/markdown",
            "size_bytes": 2,
        }


class TestRefcountGC:
    def test_delete_messages_after_decrements_and_prunes(self, backend):
        backend.register_workstream("ws-rewind")
        # Two turns, each referencing a distinct blob.  A rewind that keeps
        # only the first turn must prune the second's blob (refcount → 0) and
        # keep the first's.
        a1, a2 = _hash(b"keep"), _hash(b"drop")
        m1 = backend.save_message("ws-rewind", "user", "turn1")
        backend.save_attachment(a1, "keep.md", "text/plain", 4, "text", b"keep")
        backend.set_message_attachments("ws-rewind", m1, [a1])

        m2 = backend.save_message("ws-rewind", "user", "turn2")
        backend.save_attachment(a2, "drop.md", "text/plain", 4, "text", b"drop")
        backend.set_message_attachments("ws-rewind", m2, [a2])

        backend.delete_messages_after("ws-rewind", 1)  # keep only turn1's row

        assert backend.get_attachment(a1) is not None  # still referenced
        assert backend.get_attachment(a2) is None  # pruned at refcount 0

    def test_deduped_blob_survives_partial_delete(self, backend):
        """A blob referenced by two messages survives deleting one of them —
        refcount drops 2 → 1, the blob stays until the last reference goes."""
        backend.register_workstream("ws-shared")
        shared = _hash(b"shared-bytes")
        m1 = backend.save_message("ws-shared", "user", "first")
        backend.save_attachment(shared, "s.txt", "text/plain", 12, "text", b"shared-bytes")
        backend.set_message_attachments("ws-shared", m1, [shared])
        m2 = backend.save_message("ws-shared", "user", "second")
        backend.save_attachment(shared, "s.txt", "text/plain", 12, "text", b"shared-bytes")
        backend.set_message_attachments("ws-shared", m2, [shared])

        assert backend.get_attachment(shared)["refcount"] == 2
        backend.delete_messages_after("ws-shared", 1)  # drop the 2nd turn
        row = backend.get_attachment(shared)
        assert row is not None
        assert row["refcount"] == 1

    def test_delete_workstream_prunes_referenced_blobs(self, backend):
        backend.register_workstream("ws-cas")
        aid = _hash(b"a")
        m = backend.save_message("ws-cas", "user", "hi")
        backend.save_attachment(aid, "a.txt", "text/plain", 1, "text", b"a")
        backend.set_message_attachments("ws-cas", m, [aid])

        assert backend.delete_workstream("ws-cas") is True
        assert backend.get_attachment(aid) is None

    def test_delete_workstream_keeps_blob_shared_with_other_ws(self, backend):
        """Content-addressed ids are global: a blob referenced from two
        workstreams must only be decremented (not blanket-deleted) when one
        workstream is removed."""
        backend.register_workstream("ws-one")
        backend.register_workstream("ws-two")
        shared = _hash(b"cross-ws")
        m1 = backend.save_message("ws-one", "user", "a")
        backend.save_attachment(shared, "s.txt", "text/plain", 8, "text", b"cross-ws")
        backend.set_message_attachments("ws-one", m1, [shared])
        m2 = backend.save_message("ws-two", "user", "b")
        backend.save_attachment(shared, "s.txt", "text/plain", 8, "text", b"cross-ws")
        backend.set_message_attachments("ws-two", m2, [shared])
        assert backend.get_attachment(shared)["refcount"] == 2

        backend.delete_workstream("ws-one")
        row = backend.get_attachment(shared)
        assert row is not None, "blob still referenced by ws-two must survive"
        assert row["refcount"] == 1
        backend.delete_workstream("ws-two")
        assert backend.get_attachment(shared) is None


class TestOwnershipGate:
    def test_referenced_in_ws_true_for_referencing_row(self, backend):
        backend.register_workstream("ws-own")
        aid = _hash(b"owned")
        m = backend.save_message("ws-own", "user", "hi")
        backend.save_attachment(aid, "o.txt", "text/plain", 5, "text", b"owned")
        backend.set_message_attachments("ws-own", m, [aid])
        assert backend.attachment_referenced_in_ws(aid, "ws-own") is True

    def test_referenced_in_ws_false_for_other_ws(self, backend):
        # The blob is global, but the OTHER workstream has no row referencing
        # it → the get_content ownership gate denies it there.
        backend.register_workstream("ws-own2")
        backend.register_workstream("ws-stranger")
        aid = _hash(b"owned2")
        m = backend.save_message("ws-own2", "user", "hi")
        backend.save_attachment(aid, "o.txt", "text/plain", 6, "text", b"owned2")
        backend.set_message_attachments("ws-own2", m, [aid])
        assert backend.attachment_referenced_in_ws(aid, "ws-stranger") is False

    def test_referenced_in_ws_false_when_unreferenced(self, backend):
        # A blob written but not yet recorded on any row (shouldn't happen in
        # the live flow, but the gate must be closed-by-default).
        backend.register_workstream("ws-own3")
        aid = _hash(b"orphan")
        backend.save_attachment(aid, "o.txt", "text/plain", 6, "text", b"orphan")
        assert backend.attachment_referenced_in_ws(aid, "ws-own3") is False


@pytest.mark.parametrize("kind", ["image", "text"])
class TestParametrizedKind:
    def test_roundtrip_content_bytes(self, backend, kind):
        backend.register_workstream(f"ws-p-{kind}")
        payload = PNG_1x1 if kind == "image" else b"x" * 42
        mime = "image/png" if kind == "image" else "text/plain"
        aid = _hash(payload)
        backend.save_attachment(aid, f"f.{kind}", mime, len(payload), kind, payload)
        rows = backend.get_attachments([aid])
        assert len(rows) == 1
        assert rows[0]["content"] == payload
        assert rows[0]["kind"] == kind


class TestGetAttachmentsExcludeKinds:
    def test_exclude_kinds_filters_at_the_query(self, backend):
        """Preview-pane blobs ride ref-lists only for GC + the serving gate;
        the reconstruct loader excludes them so a history load never pulls
        their multi-MB content just to discard it."""
        backend.register_workstream("ws-ex")
        blob = _hash(b"<html>big page</html>")
        img = _hash(PNG_1x1)
        backend.save_attachment(
            blob,
            "preview-web",
            "text/html; charset=utf-8",
            21,
            "preview",
            b"<html>big page</html>",
            "tool",
        )
        backend.save_attachment(
            img, "shot.png", "image/png", len(PNG_1x1), "image", PNG_1x1, "tool"
        )
        rows = backend.get_attachments([blob, img], exclude_kinds=("preview",))
        assert [r["attachment_id"] for r in rows] == [img]
        # Default stays unfiltered — the serving route still resolves previews.
        assert {r["attachment_id"] for r in backend.get_attachments([blob, img])} == {blob, img}
