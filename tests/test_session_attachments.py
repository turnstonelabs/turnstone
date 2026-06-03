"""Tests for ChatSession.send() multipart-attachment support."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from turnstone.core.attachments import Attachment
from turnstone.core.memory import (
    get_attachment,
    register_workstream,
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
    def test_db_row_stores_text_only_and_records_ref_list(self, tmp_db, mock_openai_client):
        import hashlib

        s = _make_session(mock_openai_client)
        content = b"hello"
        aid = hashlib.sha256(content).hexdigest()  # the content hash is the id
        att = Attachment(aid, "note.md", "text/markdown", "text", content)
        _run_send(s, "user text", attachments=[att])

        # The conversations row's text content is just the user input; the
        # attachment is linked via the ``attachments`` ref-list column.
        import json

        import sqlalchemy as sa

        from turnstone.core.storage._registry import get_storage
        from turnstone.core.storage._schema import conversations

        with get_storage()._conn() as conn:
            rows = conn.execute(
                sa.select(conversations.c.content, conversations.c.id, conversations.c.attachments)
                .where(conversations.c.ws_id == s._ws_id)
                .order_by(conversations.c.id)
            ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "user text"
        assert json.loads(rows[0][2]) == [aid]

        # The blob was written content-addressed at refcount 1, origin upload.
        att_row = get_attachment(aid)
        assert att_row is not None
        assert att_row["content"] == content
        assert att_row["refcount"] == 1
        assert att_row["origin"] == "upload"

    def test_send_drains_the_upload_buffer(self, tmp_db, mock_openai_client):
        # Bytes staged in the per-node buffer are drained (discarded) once the
        # send commits them content-addressed — they don't linger as pending.
        from turnstone.core.attachment_buffer import get_attachment_buffer

        s = _make_session(mock_openai_client)
        buf = get_attachment_buffer()
        staged = buf.stage(
            ws_id=s._ws_id,
            user_id=s._user_id,
            filename="note.md",
            mime_type="text/markdown",
            kind="text",
            content=b"buffered",
        )
        assert buf.get(staged.attachment_id, ws_id=s._ws_id, user_id=s._user_id) is not None
        att = Attachment(staged.attachment_id, "note.md", "text/markdown", "text", b"buffered")
        _run_send(s, "user text", attachments=[att])
        # Drained from the buffer post-commit.
        assert buf.get(staged.attachment_id, ws_id=s._ws_id, user_id=s._user_id) is None

    def test_reload_reconstructs_multipart(self, tmp_db, mock_openai_client):
        import hashlib

        from turnstone.core.memory import load_messages

        s = _make_session(mock_openai_client)
        content = b"# doc\n"
        aid = hashlib.sha256(content).hexdigest()
        att = Attachment(aid, "d.md", "text/markdown", "text", content)
        _run_send(s, "see doc", attachments=[att])

        msgs = load_messages(s._ws_id, repair=False)
        assert msgs[0]["role"] == "user"
        parts = msgs[0]["content"]
        assert isinstance(parts, list)
        assert parts[0] == {"type": "text", "text": "see doc"}
        assert parts[1]["type"] == "document"
        assert parts[1]["document"]["data"] == "# doc\n"


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


class TestQueuedAttachmentsRejected:
    """Queued user messages can't carry attachments — see
    :class:`AttachmentsNotQueueableError` for the role-ordering reason
    (an attachment-bearing queued item would have to be appended as a
    separate user turn, injecting ``user`` between
    ``assistant(tool_calls)`` and ``tool``)."""

    def test_queue_message_rejects_attachments(self, tmp_db, mock_openai_client):
        from turnstone.core.session import AttachmentsNotQueueableError

        s = _make_session(mock_openai_client)
        # Rejection is on the ``attachment_ids`` argument alone — no row need
        # exist (the buffer is the pending store; queueing never touches it).
        with pytest.raises(AttachmentsNotQueueableError):
            s.queue_message("queued text", attachment_ids=["a-q1"])
        # Queue stayed empty — nothing partially committed.
        assert s._queued_messages == {}

    def test_queue_message_accepts_text_only(self, tmp_db, mock_openai_client):
        s = _make_session(mock_openai_client)
        cleaned, priority, msg_id = s.queue_message("plain text")
        assert cleaned == "plain text"
        with s._queued_lock:
            assert s._queued_messages[msg_id] == ("plain text", priority)


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
