"""Tests for ChatSession.send() multipart-attachment support."""

from __future__ import annotations

from unittest.mock import MagicMock

from turnstone.core.attachments import Attachment
from turnstone.core.memory import (
    get_attachment,
    list_pending_attachments,
    register_workstream,
    save_attachment,
)
from turnstone.core.session import ChatSession

PNG_1x1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xfc\xcf"
    b"\xc0\xc0\xc0\x00\x00\x00\x05\x00\x01\xa5\xf6E@\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _make_session(mock_client, user_id: str = "u1") -> ChatSession:
    s = ChatSession(
        client=mock_client,
        model="test-model",
        ui=MagicMock(),
        instructions=None,
        temperature=0.5,
        max_tokens=1000,
        tool_timeout=10,
        user_id=user_id,
    )
    register_workstream(s._ws_id)
    # Short-circuit the response loop: patch out the methods send() will call
    # after appending the user message so the test can focus on message shape.
    s._refresh_model_from_registry = lambda: None  # type: ignore[method-assign]
    s._full_messages = lambda: []  # type: ignore[method-assign]
    # Break out of the response loop immediately
    s._check_cancelled = MagicMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("stop after append")
    )
    return s


def _run_send(session: ChatSession, text: str, attachments=None) -> None:
    """Call send() but tolerate the stop-loop sentinel."""
    try:
        session.send(text, attachments=attachments)
    except RuntimeError as e:
        if "stop after append" not in str(e):
            raise


class TestPlainTextUnchanged:
    def test_no_attachments_stores_string_content(self, tmp_db, mock_openai_client):
        s = _make_session(mock_openai_client)
        _run_send(s, "hello")
        assert s.messages[-1] == {"role": "user", "content": "hello"}

    def test_empty_attachments_list_stores_string_content(self, tmp_db, mock_openai_client):
        s = _make_session(mock_openai_client)
        _run_send(s, "hello", attachments=[])
        assert s.messages[-1] == {"role": "user", "content": "hello"}


class TestMultipartBuild:
    def test_image_attachment_becomes_data_uri(self, tmp_db, mock_openai_client):
        s = _make_session(mock_openai_client)
        att = Attachment(
            attachment_id="a1",
            filename="tiny.png",
            mime_type="image/png",
            kind="image",
            content=PNG_1x1,
        )
        _run_send(s, "what is this?", attachments=[att])
        msg = s.messages[-1]
        assert msg["role"] == "user"
        assert isinstance(msg["content"], list)
        assert msg["content"][0] == {"type": "text", "text": "what is this?"}
        img = msg["content"][1]
        assert img["type"] == "image_url"
        assert img["image_url"]["url"].startswith("data:image/png;base64,")

    def test_text_doc_becomes_document_part(self, tmp_db, mock_openai_client):
        s = _make_session(mock_openai_client)
        att = Attachment(
            attachment_id="a1",
            filename="notes.md",
            mime_type="text/markdown",
            kind="text",
            content=b"# hi\n",
        )
        _run_send(s, "summarize", attachments=[att])
        msg = s.messages[-1]
        doc = msg["content"][1]
        assert doc == {
            "type": "document",
            "document": {
                "name": "notes.md",
                "media_type": "text/markdown",
                "data": "# hi\n",
            },
        }

    def test_mixed_attachments_order_preserved(self, tmp_db, mock_openai_client):
        s = _make_session(mock_openai_client)
        atts = [
            Attachment("a1", "img.png", "image/png", "image", PNG_1x1),
            Attachment("a2", "first.md", "text/markdown", "text", b"A"),
            Attachment("a3", "second.md", "text/markdown", "text", b"B"),
        ]
        _run_send(s, "look", attachments=atts)
        types = [p["type"] for p in s.messages[-1]["content"]]
        assert types == ["text", "image_url", "document", "document"]
        docs = [p for p in s.messages[-1]["content"] if p["type"] == "document"]
        assert docs[0]["document"]["data"] == "A"
        assert docs[1]["document"]["data"] == "B"

    def test_invalid_utf8_text_falls_back_to_placeholder(self, tmp_db, mock_openai_client):
        s = _make_session(mock_openai_client)
        att = Attachment("a1", "bad.bin", "text/plain", "text", b"\xff\xfe")
        _run_send(s, "read this", attachments=[att])
        parts = s.messages[-1]["content"]
        assert any(
            p.get("type") == "text" and p.get("text") == "[unreadable attachment: bad.bin]"
            for p in parts
        )


class TestPersistenceAndConsumption:
    def test_db_row_stores_text_only(self, tmp_db, mock_openai_client):
        s = _make_session(mock_openai_client)
        save_attachment(
            "att-persist",
            s._ws_id,
            "u1",
            "note.md",
            "text/markdown",
            5,
            "text",
            b"hello",
        )
        att = Attachment("att-persist", "note.md", "text/markdown", "text", b"hello")
        _run_send(s, "user text", attachments=[att])

        # The conversations row's text content is just the user input —
        # the attachment is linked separately via message_id.
        import sqlalchemy as sa

        from turnstone.core.storage._registry import get_storage
        from turnstone.core.storage._schema import conversations

        with get_storage()._conn() as conn:
            rows = conn.execute(
                sa.select(conversations.c.content, conversations.c.id)
                .where(conversations.c.ws_id == s._ws_id)
                .order_by(conversations.c.id)
            ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "user text"
        msg_id = rows[0][1]

        # Attachment should be consumed and linked to the message
        assert list_pending_attachments(s._ws_id, "u1") == []
        att_row = get_attachment("att-persist")
        assert att_row is not None
        assert att_row["message_id"] == msg_id

    def test_consumption_scoped_to_user(self, tmp_db, mock_openai_client):
        # A session running as user B must not consume user A's attachments
        # even if the id is in the list passed to send().
        s = _make_session(mock_openai_client, user_id="userB")
        save_attachment(
            "att-other",
            s._ws_id,
            "userA",
            "a.md",
            "text/plain",
            1,
            "text",
            b"A",
        )
        # Session constructs multipart content regardless (trust-but-verify),
        # but the DB-level mark is scoped — attachment stays pending for A.
        att = Attachment("att-other", "a.md", "text/plain", "text", b"A")
        _run_send(s, "hi", attachments=[att])
        att_row = get_attachment("att-other")
        assert att_row is not None
        assert att_row["message_id"] is None


class TestProviderIntegration:
    """Verify multipart user messages built by send() survive provider
    translation end-to-end.

    Bridges the unit-level message construction (session) and the
    provider-side conversion (anthropic / openai-common) tested
    separately in test_providers_document_parts.py.
    """

    def test_anthropic_receives_native_document_block(self, tmp_db, mock_openai_client):
        from turnstone.core.providers._anthropic import AnthropicProvider

        s = _make_session(mock_openai_client)
        atts = [
            Attachment("a1", "img.png", "image/png", "image", PNG_1x1),
            Attachment("a2", "notes.md", "text/markdown", "text", b"# hi\n"),
        ]
        _run_send(s, "look at both", attachments=atts)

        _, converted = AnthropicProvider()._convert_messages([s.messages[-1]])
        assert len(converted) == 1
        content = converted[0]["content"]
        types = [p["type"] for p in content]
        assert types == ["text", "image", "document"]
        # Image translated to Anthropic base64 image source
        assert content[1]["source"]["type"] == "base64"
        assert content[1]["source"]["media_type"] == "image/png"
        # Document translated to Anthropic native text-source document
        assert content[2]["source"]["type"] == "text"
        # MIME was coerced to text/plain; original folded into title
        assert content[2]["source"]["media_type"] == "text/plain"
        assert content[2]["title"] == "notes.md (text/markdown)"
        assert content[2]["source"]["data"] == "# hi\n"

    def test_live_send_stashes_attachments_meta_sibling(self, tmp_db, mock_openai_client):
        # Filenames can't be recovered from an image_url data URI, so
        # live send attaches `_attachments_meta` to the user msg; this
        # is what the history endpoint reads (same shape as reloaded).
        s = _make_session(mock_openai_client)
        atts = [
            Attachment("a1", "dog.png", "image/png", "image", PNG_1x1),
            Attachment("a2", "notes.md", "text/markdown", "text", b"hi"),
        ]
        _run_send(s, "desc", attachments=atts)
        meta = s.messages[-1].get("_attachments_meta")
        assert meta == [
            {"kind": "image", "filename": "dog.png", "mime_type": "image/png"},
            {"kind": "text", "filename": "notes.md", "mime_type": "text/markdown"},
        ]

    def test_attachments_meta_stripped_before_openai_wire(self, tmp_db, mock_openai_client):
        # OpenAI-compat APIs don't know `_attachments_meta`; sanitize
        # must strip it before the wire call.
        from turnstone.core.providers._openai_common import sanitize_messages

        s = _make_session(mock_openai_client)
        atts = [Attachment("a1", "x.md", "text/markdown", "text", b"x")]
        _run_send(s, "hi", attachments=atts)
        out = sanitize_messages([s.messages[-1]])
        for k in out[0]:
            assert not k.startswith("_"), f"{k!r} leaked to wire"

    def test_openai_chat_completions_receives_inlined_document(self, tmp_db, mock_openai_client):
        from turnstone.core.providers._openai_common import sanitize_messages

        s = _make_session(mock_openai_client)
        atts = [
            Attachment("a1", "spec.md", "text/markdown", "text", b"DO THE THING"),
        ]
        _run_send(s, "review", attachments=atts)

        out = sanitize_messages([s.messages[-1]])
        parts = out[0]["content"]
        types = [p["type"] for p in parts]
        assert types == ["text", "text"]
        # The user's own text is preserved
        assert parts[0] == {"type": "text", "text": "review"}
        # Document inlined as escaped wrapper text
        assert 'name="spec.md"' in parts[1]["text"]
        assert "DO THE THING" in parts[1]["text"]


class TestQueuedWithAttachments:
    """Queued user turns must carry their attachments through to dequeue."""

    def test_queue_message_stores_attachment_ids(self, tmp_db, mock_openai_client):
        s = _make_session(mock_openai_client)
        # Seed a pending attachment owned by the session user
        save_attachment("a-q1", s._ws_id, "u1", "q.md", "text/markdown", 1, "text", b"q")
        cleaned, priority, msg_id = s.queue_message("queued text", attachment_ids=["a-q1"])
        assert cleaned == "queued text"
        with s._queued_lock:
            entry = s._queued_messages[msg_id]
        # Entry shape is (cleaned, priority, attachment_ids_tuple)
        assert entry[0] == "queued text"
        assert entry[2] == ("a-q1",)

    def test_flush_queued_injects_multipart_user_turn(self, tmp_db, mock_openai_client):
        from turnstone.core.memory import reserve_attachments

        s = _make_session(mock_openai_client)
        save_attachment("a-f1", s._ws_id, "u1", "f.md", "text/markdown", 3, "text", b"DAT")
        _c, _p, msg_id = s.queue_message("please review", attachment_ids=["a-f1"])
        # Server-side would have reserved before queueing; mirror that
        # so consume's token match succeeds on flush.
        reserve_attachments(["a-f1"], msg_id, s._ws_id, "u1")
        s._flush_queued_messages()

        msgs = s.messages
        assert len(msgs) == 1
        msg = msgs[0]
        assert msg["role"] == "user"
        # Multipart shape — text + document parts
        assert isinstance(msg["content"], list)
        assert msg["content"][0] == {"type": "text", "text": "please review"}
        doc = msg["content"][1]
        assert doc["type"] == "document"
        assert doc["document"]["name"] == "f.md"
        assert doc["document"]["data"] == "DAT"
        # And the attachment is now consumed (not pending)
        assert get_attachment("a-f1")["message_id"] is not None
        assert list_pending_attachments(s._ws_id, "u1") == []

    def test_flush_mixed_attachment_and_text_items(self, tmp_db, mock_openai_client):
        # Text-only items should combine into one turn while
        # attachment-bearing items flush as separate multipart turns.
        from turnstone.core.memory import reserve_attachments

        s = _make_session(mock_openai_client)
        save_attachment("a-mx", s._ws_id, "u1", "x.md", "text/markdown", 1, "text", b"x")
        s.queue_message("first plain")
        _c, _p, mid = s.queue_message("with file", attachment_ids=["a-mx"])
        reserve_attachments(["a-mx"], mid, s._ws_id, "u1")
        s.queue_message("another plain")
        s._flush_queued_messages()

        # We expect at least two user messages: one combining the plain
        # items flanking the multipart turn is allowed, but the
        # multipart turn must remain its own message.
        user_msgs = [m for m in s.messages if m.get("role") == "user"]
        multipart = [m for m in user_msgs if isinstance(m["content"], list)]
        assert len(multipart) == 1
        assert "with file" in multipart[0]["content"][0]["text"]

    def test_flush_drops_cross_user_attachment_silently(self, tmp_db, mock_openai_client):
        # A forged attachment_id belonging to another user must not
        # produce an attached part — dequeue resolution re-scopes.
        s = _make_session(mock_openai_client, user_id="u1")
        save_attachment("a-other", s._ws_id, "u2", "other.md", "text/plain", 1, "text", b"o")
        s.queue_message("hi", attachment_ids=["a-other"])
        s._flush_queued_messages()
        # Flushed as plain text-only turn — the forged id was scope-dropped.
        msgs = s.messages
        assert len(msgs) == 1
        assert msgs[0]["content"] == "hi"


class TestQueueReservationLifecycle:
    """session.queue_message + dequeue_message lifecycle with reservations."""

    def test_dequeue_unreserves_attachments(self, tmp_db, mock_openai_client):
        from turnstone.core.memory import get_attachment, reserve_attachments

        s = _make_session(mock_openai_client)
        save_attachment("a-deq", s._ws_id, "u1", "x.md", "text/plain", 1, "text", b"x")
        _cleaned, _priority, msg_id = s.queue_message("queued", attachment_ids=["a-deq"])
        # Simulate the server reserving after queue_message
        reserve_attachments(["a-deq"], msg_id, s._ws_id, "u1")
        assert get_attachment("a-deq")["reserved_for_msg_id"] == msg_id

        # Dequeue (user cancelled the queued send)
        assert s.dequeue_message(msg_id) is True
        # Reservation is released — back to pending
        assert get_attachment("a-deq")["reserved_for_msg_id"] is None
        assert len(list_pending_attachments(s._ws_id, "u1")) == 1

    def test_flush_consumes_reserved_attachment(self, tmp_db, mock_openai_client):
        from turnstone.core.memory import get_attachment, reserve_attachments

        s = _make_session(mock_openai_client)
        save_attachment("a-flush", s._ws_id, "u1", "y.md", "text/plain", 1, "text", b"y")
        _c, _p, msg_id = s.queue_message("go", attachment_ids=["a-flush"])
        reserve_attachments(["a-flush"], msg_id, s._ws_id, "u1")

        # Flush — queue drain must accept the reserved-for-this-msg attachment
        s._flush_queued_messages()
        row = get_attachment("a-flush")
        assert row["message_id"] is not None
        assert row["reserved_for_msg_id"] is None  # cleared on consume
        # And the in-memory message is multipart with the doc attached
        assert isinstance(s.messages[-1]["content"], list)
        assert any(p.get("type") == "document" for p in s.messages[-1]["content"])

    def test_resolve_rejects_reservation_for_other_msg(self, tmp_db, mock_openai_client):
        from turnstone.core.memory import reserve_attachments

        s = _make_session(mock_openai_client)
        save_attachment("a-other", s._ws_id, "u1", "z.md", "text/plain", 1, "text", b"z")
        reserve_attachments(["a-other"], "q-OTHER", s._ws_id, "u1")
        # allow_reserved_for=None (default) → reserved rows are skipped
        assert s._resolve_attachment_ids(["a-other"]) == []
        # allow_reserved_for matches → accepted
        out = s._resolve_attachment_ids(["a-other"], allow_reserved_for="q-OTHER")
        assert [a.attachment_id for a in out] == ["a-other"]


class TestExplicitAttachmentIdsOrderPreserved:
    """session._resolve_attachment_ids must honour request order."""

    def test_resolve_preserves_request_order(self, tmp_db, mock_openai_client):
        s = _make_session(mock_openai_client)
        # Insert in one order, request in the reverse order — resolver
        # must reflect the request, not the DB's INSERT order.
        save_attachment("a-1", s._ws_id, "u1", "first.md", "text/plain", 1, "text", b"1")
        save_attachment("a-2", s._ws_id, "u1", "second.md", "text/plain", 1, "text", b"2")
        save_attachment("a-3", s._ws_id, "u1", "third.md", "text/plain", 1, "text", b"3")

        out = s._resolve_attachment_ids(["a-3", "a-1", "a-2"])
        assert [a.attachment_id for a in out] == ["a-3", "a-1", "a-2"]

    def test_resolve_skips_unknown_and_keeps_order(self, tmp_db, mock_openai_client):
        s = _make_session(mock_openai_client)
        save_attachment("a-k", s._ws_id, "u1", "k.md", "text/plain", 1, "text", b"k")
        out = s._resolve_attachment_ids(["unknown", "a-k", ""])
        assert [a.attachment_id for a in out] == ["a-k"]


class TestTokenAccounting:
    def test_image_adds_image_tokens(self, tmp_db, mock_openai_client):
        baseline = _make_session(mock_openai_client)
        _run_send(baseline, "hello")
        plain_tokens = baseline._msg_tokens[-1]

        with_image = _make_session(mock_openai_client)
        att = Attachment("a1", "x.png", "image/png", "image", PNG_1x1)
        _run_send(with_image, "hello", attachments=[att])
        image_tokens = with_image._msg_tokens[-1]

        # One image injects _IMAGE_TOKENS (1000) worth; plain was ~2
        assert image_tokens - plain_tokens >= ChatSession._IMAGE_TOKENS - 10

    def test_text_doc_adds_text_char_budget(self, tmp_db, mock_openai_client):
        baseline = _make_session(mock_openai_client)
        _run_send(baseline, "hi")
        plain_tokens = baseline._msg_tokens[-1]

        big = "x" * 4000
        with_doc = _make_session(mock_openai_client)
        att = Attachment("a1", "big.md", "text/markdown", "text", big.encode())
        _run_send(with_doc, "hi", attachments=[att])
        doc_tokens = with_doc._msg_tokens[-1]

        # ~4000 chars / 4 chars_per_token ≈ ~1000 tokens added
        assert doc_tokens - plain_tokens >= 900
