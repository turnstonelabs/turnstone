"""Tests for ChatSession.send() multipart-attachment support."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from tests._session_helpers import as_stream, mock_completion_result
from turnstone.core import perception
from turnstone.core.attachments import Attachment
from turnstone.core.memory import (
    get_attachment,
    register_workstream,
)
from turnstone.core.providers._protocol import ModelCapabilities
from turnstone.core.session import ChatSession
from turnstone.core.trajectory import (
    dicts_from_turns,
    materialize_attachments,
    turn_to_dict,
)

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


def _assert_plain_text_turn(d: dict) -> None:
    """A plain-text send (no attachments) must NOT be coerced into the
    multipart/attachment shape: ``content`` stays the plain string and no
    ``_attachments_meta`` is emitted.  The per-user-context feature stamps every
    genuine user turn with a wire-invisible ``_sender`` attribution key (a
    leading-underscore side channel, stripped by ``sanitize_messages`` before
    the model call), deterministically the owner id here — assert its exact
    value so the shape stays pinned, not merely tolerated."""
    assert d["role"] == "user"
    assert d["content"] == "hello"  # plain string, not a multipart list
    assert "_attachments_meta" not in d
    assert set(d) == {"role", "content", "_sender"}
    assert d["_sender"] == "u1"  # owner fallback via _mcp_effective_user_id


class TestPlainTextUnchanged:
    def test_no_attachments_stores_string_content(self, tmp_db, mock_openai_client):
        s = _make_session(mock_openai_client)
        _run_send(s, "hello")
        _assert_plain_text_turn(turn_to_dict(s.messages[-1]))

    def test_empty_attachments_list_stores_string_content(self, tmp_db, mock_openai_client):
        s = _make_session(mock_openai_client)
        _run_send(s, "hello", attachments=[])
        _assert_plain_text_turn(turn_to_dict(s.messages[-1]))


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
        msg = materialize_attachments(dicts_from_turns(s.messages), s._resolve_attachments)[-1]
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
        msg = materialize_attachments(dicts_from_turns(s.messages), s._resolve_attachments)[-1]
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
        msg = materialize_attachments(dicts_from_turns(s.messages), s._resolve_attachments)[-1]
        types = [p["type"] for p in msg["content"]]
        assert types == ["text", "image_url", "document", "document"]
        docs = [p for p in msg["content"] if p["type"] == "document"]
        assert docs[0]["document"]["data"] == "A"
        assert docs[1]["document"]["data"] == "B"

    def test_invalid_utf8_text_falls_back_to_placeholder(self, tmp_db, mock_openai_client):
        s = _make_session(mock_openai_client)
        att = Attachment("a1", "bad.bin", "text/plain", "text", b"\xff\xfe")
        _run_send(s, "read this", attachments=[att])
        parts = materialize_attachments(dicts_from_turns(s.messages), s._resolve_attachments)[-1][
            "content"
        ]
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

        _, converted = AnthropicProvider()._convert_messages(
            materialize_attachments(dicts_from_turns([s.messages[-1]]), s._resolve_attachments)
        )
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
        meta = turn_to_dict(s.messages[-1]).get("_attachments_meta")
        assert meta == [
            {
                "kind": "image",
                "filename": "dog.png",
                "mime_type": "image/png",
                "size_bytes": len(PNG_1x1),
            },
            {"kind": "text", "filename": "notes.md", "mime_type": "text/markdown", "size_bytes": 2},
        ]

    def test_attachments_meta_stripped_before_openai_wire(self, tmp_db, mock_openai_client):
        # OpenAI-compat APIs don't know `_attachments_meta`; sanitize
        # must strip it before the wire call.
        from turnstone.core.providers._openai_common import sanitize_messages

        s = _make_session(mock_openai_client)
        atts = [Attachment("a1", "x.md", "text/markdown", "text", b"x")]
        _run_send(s, "hi", attachments=atts)
        out = sanitize_messages(dicts_from_turns([s.messages[-1]]))
        for k in out[0]:
            assert not k.startswith("_"), f"{k!r} leaked to wire"

    def test_openai_chat_completions_receives_inlined_document(self, tmp_db, mock_openai_client):
        from turnstone.core.providers._openai_common import sanitize_messages

        s = _make_session(mock_openai_client)
        atts = [
            Attachment("a1", "spec.md", "text/markdown", "text", b"DO THE THING"),
        ]
        _run_send(s, "review", attachments=atts)

        out = sanitize_messages(
            materialize_attachments(dicts_from_turns([s.messages[-1]]), s._resolve_attachments)
        )
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
        plain_chars = baseline._msg_char_count(
            materialize_attachments(
                dicts_from_turns(baseline.messages), baseline._resolve_attachments
            )[-1]
        )

        big = "x" * 4000
        with_doc = _make_session(mock_openai_client)
        att = Attachment("a1", "big.md", "text/markdown", "text", big.encode())
        _run_send(with_doc, "hi", attachments=[att])
        doc_chars = with_doc._msg_char_count(
            materialize_attachments(
                dicts_from_turns(with_doc.messages), with_doc._resolve_attachments
            )[-1]
        )

        # The ~4000-char doc lands at the resolved boundary (the per-turn
        # placeholder no longer carries the bytes), well above the budget floor.
        assert doc_chars - plain_chars >= 900

    def test_by_reference_doc_counted_without_materialization(self):
        """R1: a canonical by-reference document turn (NOT yet materialized) must
        count its size in the char budget via ``_attachments_meta``.  Before the
        fix the placeholder carried no bytes, so ``doc_chars`` was 0 and a reloaded
        document conversation under-counted its context (the budget feeds
        compaction / trim decisions)."""
        meta = [
            {"kind": "text", "filename": "big.md", "mime_type": "text/markdown", "size_bytes": 4000}
        ]
        by_ref = {
            "role": "user",
            "content": [
                {"type": "text", "text": "see doc"},
                {"type": "document", "attachment_id": "x"},
            ],
            "_attachments_meta": meta,
        }
        _t, _i, doc_chars = ChatSession._msg_text_chars(by_ref)
        assert doc_chars == 4000  # was 0 before the fix

        # A materialized inline document that still carries meta must count ONCE,
        # not twice — the inline_doc guard suppresses the meta term.
        inline_plus_meta = {
            "role": "user",
            "content": [
                {"type": "document", "document": {"data": "x" * 4000, "name": "", "media_type": ""}}
            ],
            "_attachments_meta": meta,
        }
        _t2, _i2, doc2 = ChatSession._msg_text_chars(inline_plus_meta)
        assert doc2 == 4000


class TestCapabilityGatedFallback:
    """The wire resolver routes each blob to a native part or a client-side
    fallback based on the active model's capabilities — per-kind dispatch, no
    shared 'fallback' machinery."""

    def _att(self, kind, content=b"x", fn="f", mime="application/octet-stream"):
        return {
            "attachment_id": "aX",
            "filename": fn,
            "mime_type": mime,
            "kind": kind,
            "content": content,
        }

    def test_pdf_native_when_supported(self, tmp_db, mock_openai_client):
        s = _make_session(mock_openai_client)
        part = s._wire_content_part(
            self._att("pdf", b"%PDF-1.4 x", "r.pdf", "application/pdf"),
            ModelCapabilities(supports_pdf=True),
        )
        assert part["type"] == "document"
        assert part["document"]["media_type"] == "application/pdf"

    def test_pdf_text_fallback_when_unsupported(self, tmp_db, mock_openai_client, monkeypatch):
        s = _make_session(mock_openai_client)
        monkeypatch.setattr("turnstone.core.pdf.extract_pdf_text", lambda data: "EXTRACTED")
        part = s._wire_content_part(
            self._att("pdf", b"%PDF", "r.pdf", "application/pdf"),
            ModelCapabilities(supports_pdf=False),
        )
        assert part["type"] == "document"
        assert part["document"]["media_type"] == "text/plain"
        assert part["document"]["data"] == "EXTRACTED"
        assert "extracted text" in part["document"]["name"]

    def test_pdf_empty_extract_is_placeholder(self, tmp_db, mock_openai_client, monkeypatch):
        s = _make_session(mock_openai_client)
        monkeypatch.setattr("turnstone.core.pdf.extract_pdf_text", lambda data: "")
        part = s._wire_content_part(
            self._att("pdf", b"%PDF", "scan.pdf", "application/pdf"),
            ModelCapabilities(supports_pdf=False),
        )
        assert part["type"] == "text"
        assert "no extractable text" in part["text"]

    def test_audio_native_when_supported(self, tmp_db, mock_openai_client):
        s = _make_session(mock_openai_client)
        part = s._wire_content_part(
            self._att("audio", b"RIFFxxxxWAVE", "a.wav", "audio/wav"),
            ModelCapabilities(supports_audio_input=True),
        )
        assert part["type"] == "input_audio"
        assert part["input_audio"]["format"] == "wav"

    def test_audio_fallback_no_stt_is_placeholder(self, tmp_db, mock_openai_client):
        # _make_session leaves registry / config_store None -> no STT role.
        s = _make_session(mock_openai_client)
        part = s._wire_content_part(
            self._att("audio", b"RIFF", "a.wav", "audio/wav"),
            ModelCapabilities(supports_audio_input=False),
        )
        assert part["type"] == "text"
        assert "no transcription backend" in part["text"]

    def test_image_not_gated(self, tmp_db, mock_openai_client):
        # Images are unchanged by this work — still emitted as image_url even to
        # a no-vision model (pre-existing behavior, left as-is).
        s = _make_session(mock_openai_client)
        part = s._wire_content_part(
            self._att("image", PNG_1x1, "i.png", "image/png"),
            ModelCapabilities(),
        )
        assert part["type"] == "image_url"

    def test_pdf_rasterize_when_vision(self, tmp_db, mock_openai_client, monkeypatch):
        # Vision-capable but no native PDF → render pages to images (1 -> N).
        s = _make_session(mock_openai_client)
        monkeypatch.setattr("turnstone.core.pdf.rasterize_pdf", lambda data: [b"png-a", b"png-b"])
        parts = s._wire_content_part(
            self._att("pdf", b"%PDF", "r.pdf", "application/pdf"),
            ModelCapabilities(supports_pdf=False, supports_vision=True),
        )
        assert isinstance(parts, list)
        assert len(parts) == 2
        assert all(p["type"] == "image_url" for p in parts)
        assert parts[0]["image_url"]["url"].startswith("data:image/png;base64,")

    def test_pdf_rasterize_empty_falls_back_to_text(self, tmp_db, mock_openai_client, monkeypatch):
        s = _make_session(mock_openai_client)
        monkeypatch.setattr("turnstone.core.pdf.rasterize_pdf", lambda data: [])
        monkeypatch.setattr("turnstone.core.pdf.extract_pdf_text", lambda data: "TXT")
        part = s._wire_content_part(
            self._att("pdf", b"%PDF", "r.pdf", "application/pdf"),
            ModelCapabilities(supports_pdf=False, supports_vision=True),
        )
        assert isinstance(part, dict)
        assert part["type"] == "document"
        assert part["document"]["data"] == "TXT"

    def test_materialize_expands_list_valued_resolution(self):
        # One placeholder resolving to several parts (the PDF-rasterize 1->N case)
        # is spliced in order by resolve_attachment_parts.
        msgs = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "see"},
                    {"type": "pdf", "attachment_id": "a1"},
                ],
            }
        ]

        def resolve(ids):
            return {
                "a1": [
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,BBB"}},
                ]
            }

        out = materialize_attachments(msgs, resolve)
        types = [p["type"] for p in out[0]["content"]]
        assert types == ["text", "image_url", "image_url"]


class TestPerceptionFallback:
    """Universal perception bottom tier: image/PDF/audio for primaries that
    can't ingest them, when a capable perception model is configured."""

    def _att(self, kind, content=b"x", fn="f", mime="application/octet-stream"):
        return {
            "attachment_id": "aP",
            "filename": fn,
            "mime_type": mime,
            "kind": kind,
            "content": content,
        }

    def _with_perception(self, s, *, perc_caps, content="DESCRIPTION"):
        """Wire a stub perception backend onto the session; return the provider mock."""
        perception._clear_perception_cache_for_test()
        prov = MagicMock()
        prov.create_streaming.return_value = as_stream(mock_completion_result(content))
        s._config_store = MagicMock()
        s._config_store.get = lambda k, *a: "omni" if k == "perception.model_alias" else ""
        s._registry = MagicMock()
        s._registry.has_alias = lambda a: a == "omni"
        s._registry.resolve = lambda a: (object(), "omni-model", object())
        s._registry.get_provider = lambda a: prov
        s._resolve_capabilities = lambda *a, **k: perc_caps  # type: ignore[method-assign]
        return prov

    def test_image_perception_when_primary_blind(self, tmp_db, mock_openai_client):
        s = _make_session(mock_openai_client)
        prov = self._with_perception(s, perc_caps=ModelCapabilities(supports_vision=True))
        part = s._wire_content_part(
            self._att("image", PNG_1x1, "i.png", "image/png"),
            ModelCapabilities(),  # primary: no vision
        )
        assert part["type"] == "text"
        assert "DESCRIPTION" in part["text"]
        assert "image attachment 'i.png'" in part["text"]
        prov.create_streaming.assert_called_once()

    def test_image_falls_through_to_native_without_perception(self, tmp_db, mock_openai_client):
        # No perception configured (registry/config_store None) → native image_url:
        # the pre-existing behavior; perception is purely additive.
        s = _make_session(mock_openai_client)
        part = s._wire_content_part(
            self._att("image", PNG_1x1, "i.png", "image/png"),
            ModelCapabilities(),
        )
        assert part["type"] == "image_url"

    def test_pdf_perception_renders_pages(self, tmp_db, mock_openai_client, monkeypatch):
        s = _make_session(mock_openai_client)
        monkeypatch.setattr("turnstone.core.pdf.rasterize_pdf", lambda data: [b"pg1", b"pg2"])
        prov = self._with_perception(s, perc_caps=ModelCapabilities(supports_vision=True))
        part = s._wire_content_part(
            self._att("pdf", b"%PDF", "r.pdf", "application/pdf"),
            ModelCapabilities(supports_pdf=False),  # primary: no pdf, no vision
        )
        assert part["type"] == "text"
        assert "DESCRIPTION" in part["text"]
        # the perception model was handed the rasterized pages, not the raw
        # PDF: the wire carries the prompt + a by-reference placeholder, and
        # the threaded resolver materializes the page parts at the translator.
        sent = prov.create_streaming.call_args.kwargs["messages"][0]["content"]
        assert sent[0]["type"] == "text"
        assert sent[1]["attachment_id"] == "perception-input"
        resolver = prov.create_streaming.call_args.kwargs["resolve_attachments"]
        pages = resolver(["perception-input"])["perception-input"]
        assert [p["type"] for p in pages] == ["image_url", "image_url"]

    def test_audio_perception_when_omni_and_no_stt(self, tmp_db, mock_openai_client):
        s = _make_session(mock_openai_client)
        self._with_perception(s, perc_caps=ModelCapabilities(supports_audio_input=True))
        part = s._wire_content_part(
            self._att("audio", b"RIFFxxxxWAVE", "a.wav", "audio/wav"),
            ModelCapabilities(supports_audio_input=False),
        )
        assert part["type"] == "text"
        assert "DESCRIPTION" in part["text"]

    def test_perception_skipped_when_model_lacks_modality(self, tmp_db, mock_openai_client):
        # Perception model has vision but not audio → audio falls through to the
        # placeholder rather than calling a model that can't hear.
        s = _make_session(mock_openai_client)
        prov = self._with_perception(s, perc_caps=ModelCapabilities(supports_vision=True))
        part = s._wire_content_part(
            self._att("audio", b"RIFF", "a.wav", "audio/wav"),
            ModelCapabilities(supports_audio_input=False),
        )
        assert part["type"] == "text"
        assert "no transcription backend" in part["text"]
        prov.create_streaming.assert_not_called()


class TestResolveAttachmentsCapsThreading:
    """bug-1: the resolver materializes against the caps it is handed (the active
    attempt's), not the primary session model's."""

    def _att(self):
        return {
            "attachment_id": "aT",
            "filename": "r.pdf",
            "mime_type": "application/pdf",
            "kind": "pdf",
            "content": b"%PDF",
        }

    def test_resolver_uses_passed_caps(self, tmp_db, mock_openai_client, monkeypatch):
        s = _make_session(mock_openai_client)
        monkeypatch.setattr("turnstone.core.session.get_attachments", lambda ids: [self._att()])
        monkeypatch.setattr("turnstone.core.pdf.rasterize_pdf", lambda data: [b"pg"])
        # Passed caps (vision, no native PDF) drive rasterize-to-images — not
        # whatever the primary 'test-model' happens to support.
        out = s._resolve_attachments(
            ["aT"], ModelCapabilities(supports_pdf=False, supports_vision=True)
        )
        part = out["aT"]
        assert isinstance(part, list)
        assert all(p["type"] == "image_url" for p in part)

    def test_resolver_native_with_pdf_caps(self, tmp_db, mock_openai_client, monkeypatch):
        s = _make_session(mock_openai_client)
        monkeypatch.setattr("turnstone.core.session.get_attachments", lambda ids: [self._att()])
        out = s._resolve_attachments(["aT"], ModelCapabilities(supports_pdf=True))
        assert out["aT"]["type"] == "document"
        assert out["aT"]["document"]["media_type"] == "application/pdf"


class TestResolveAttachmentsPerSendCache:
    """The per-send wire-part memo collapses the re-fetch + re-rasterize that the
    resolver would otherwise repeat on every agentic round-trip within one send."""

    def _att(self):
        return {
            "attachment_id": "aT",
            "filename": "r.pdf",
            "mime_type": "application/pdf",
            "kind": "pdf",
            "content": b"%PDF",
        }

    def test_cache_collapses_repeat_resolves(self, tmp_db, mock_openai_client, monkeypatch):
        s = _make_session(mock_openai_client)
        fetches = {"n": 0}
        rasters = {"n": 0}

        def _fetch(ids):
            fetches["n"] += 1
            return [self._att()] if ids else []

        monkeypatch.setattr("turnstone.core.session.get_attachments", _fetch)
        monkeypatch.setattr(
            "turnstone.core.pdf.rasterize_pdf",
            lambda data: rasters.__setitem__("n", rasters["n"] + 1) or [b"pg"],
        )
        caps = ModelCapabilities(supports_pdf=False, supports_vision=True)
        s._wire_part_cache = {}  # simulate being inside send()
        first = s._resolve_attachments(["aT"], caps)
        second = s._resolve_attachments(["aT"], caps)
        assert first == second
        assert isinstance(first["aT"], list)
        # Fetched + rasterized once despite two resolver passes.
        assert fetches["n"] == 1
        assert rasters["n"] == 1

    def test_no_cache_outside_send_rematerializes(self, tmp_db, mock_openai_client, monkeypatch):
        s = _make_session(mock_openai_client)
        fetches = {"n": 0}

        def _fetch(ids):
            fetches["n"] += 1
            return [self._att()]

        monkeypatch.setattr("turnstone.core.session.get_attachments", _fetch)
        monkeypatch.setattr("turnstone.core.pdf.rasterize_pdf", lambda data: [b"pg"])
        caps = ModelCapabilities(supports_pdf=False, supports_vision=True)
        assert s._wire_part_cache is None  # default outside a send → no caching
        s._resolve_attachments(["aT"], caps)
        s._resolve_attachments(["aT"], caps)
        assert fetches["n"] == 2

    def test_cache_keyed_by_caps(self, tmp_db, mock_openai_client, monkeypatch):
        s = _make_session(mock_openai_client)
        monkeypatch.setattr("turnstone.core.session.get_attachments", lambda ids: [self._att()])
        monkeypatch.setattr("turnstone.core.pdf.rasterize_pdf", lambda data: [b"pg"])
        s._wire_part_cache = {}
        native = s._resolve_attachments(["aT"], ModelCapabilities(supports_pdf=True))
        rasterized = s._resolve_attachments(
            ["aT"], ModelCapabilities(supports_pdf=False, supports_vision=True)
        )
        # Different caps → different materialization, not a stale same-id hit.
        assert native["aT"]["type"] == "document"
        assert isinstance(rasterized["aT"], list)


class TestByReferenceMediaBudget:
    """bug-2: by-reference pdf/audio are charged a bounded budget — not zero
    (over-context), not the full multi-MB source blob (over-trim)."""

    def test_pdf_and_audio_charged_capped(self):
        msg = {
            "role": "user",
            "content": [],
            "_attachments_meta": [
                {"kind": "pdf", "size_bytes": 32_000_000},
                {"kind": "audio", "size_bytes": 25_000_000},
                {"kind": "text", "size_bytes": 500},
                {"kind": "image", "size_bytes": 99},
            ],
        }
        _text, images, doc_chars = ChatSession._msg_text_chars(msg)
        # pdf + audio each capped at 16_000; text counted in full; image excluded
        # (a real by-reference image is charged a fixed image budget in the
        # content loop, so counting it here too would double-charge).
        assert doc_chars == 16_000 + 16_000 + 500
        assert images == 0
