"""Phase 1 (spine) tests for PDF + audio attachment kinds.

Pure-function coverage for the provider-neutral plumbing: magic-byte sniffers,
``Attachment`` kind predicates, and the internal content-part shapes the wire
builder emits.  No DB / provider wiring yet (Phase 2) — these pin the shapes the
later phases translate.
"""

from __future__ import annotations

import base64

from turnstone.core.attachments import (
    AUDIO_MIME_TO_FORMAT,
    IMAGE_SIZE_CAP,
    Attachment,
    classify_upload,
    sniff_audio_mime,
    sniff_pdf_mime,
)
from turnstone.core.storage._utils import attachment_to_content_part

# --- sample bytes (just enough magic for the sniffers) --------------------- #
PDF = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj\n"
WAV = b"RIFF\x24\x00\x00\x00WAVEfmt "
MP3_ID3 = b"ID3\x04\x00\x00\x00\x00\x00\x00\x00\x00"
MP3_SYNC = b"\xff\xfb\x90\x00" + b"\x00" * 8
OGG = b"OggS\x00\x02" + b"\x00" * 8
FLAC = b"fLaC\x00\x00\x00\x22" + b"\x00" * 8
M4A = b"\x00\x00\x00\x20ftypM4A \x00\x00\x00\x00"
WEBM = b"\x1aE\xdf\xa3" + b"\x00" * 8
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8


class TestSniffPdf:
    def test_pdf_magic(self) -> None:
        assert sniff_pdf_mime(PDF) == "application/pdf"

    def test_rejects_non_pdf(self) -> None:
        assert sniff_pdf_mime(PNG) is None
        assert sniff_pdf_mime(b"not a pdf at all") is None

    def test_too_short(self) -> None:
        assert sniff_pdf_mime(b"%PD") is None
        assert sniff_pdf_mime(b"") is None


class TestSniffAudio:
    def test_each_format(self) -> None:
        assert sniff_audio_mime(WAV) == "audio/wav"
        assert sniff_audio_mime(MP3_ID3) == "audio/mpeg"
        assert sniff_audio_mime(MP3_SYNC) == "audio/mpeg"
        assert sniff_audio_mime(OGG) == "audio/ogg"
        assert sniff_audio_mime(FLAC) == "audio/flac"
        assert sniff_audio_mime(M4A) == "audio/mp4"
        assert sniff_audio_mime(WEBM) == "audio/webm"

    def test_rejects_non_audio(self) -> None:
        assert sniff_audio_mime(PNG) is None
        assert sniff_audio_mime(PDF) is None

    def test_too_short(self) -> None:
        assert sniff_audio_mime(b"RIFF") is None
        assert sniff_audio_mime(b"") is None


class TestAttachmentKindPredicates:
    def _att(self, kind: str) -> Attachment:
        return Attachment(
            attachment_id="a",
            filename="f",
            mime_type="application/octet-stream",
            kind=kind,
            content=b"x",
        )

    def test_pdf(self) -> None:
        a = self._att("pdf")
        assert a.is_pdf and not (a.is_image or a.is_text or a.is_audio)

    def test_audio(self) -> None:
        a = self._att("audio")
        assert a.is_audio and not (a.is_image or a.is_text or a.is_pdf)

    def test_existing_kinds_unaffected(self) -> None:
        assert self._att("image").is_image
        assert self._att("text").is_text


class TestContentPartBuilder:
    def test_pdf_part_is_base64_document(self) -> None:
        raw = PDF
        part = attachment_to_content_part(
            {"kind": "pdf", "content": raw, "mime_type": "application/pdf", "filename": "doc.pdf"}
        )
        assert part is not None
        assert part["type"] == "document"
        doc = part["document"]
        assert doc["name"] == "doc.pdf"
        assert doc["media_type"] == "application/pdf"
        # base64 (not utf-8 text) — round-trips to the original bytes.
        assert base64.b64decode(doc["data"]) == raw

    def test_audio_part_is_input_audio(self) -> None:
        raw = WAV
        part = attachment_to_content_part(
            {"kind": "audio", "content": raw, "mime_type": "audio/wav", "filename": "a.wav"}
        )
        assert part is not None
        assert part["type"] == "input_audio"
        ia = part["input_audio"]
        assert ia["format"] == "wav"
        assert base64.b64decode(ia["data"]) == raw

    def test_audio_format_falls_back_to_codec_token(self) -> None:
        part = attachment_to_content_part(
            {
                "kind": "audio",
                "content": b"\x00" * 16,
                "mime_type": "audio/x-exotic",
                "filename": "x",
            }
        )
        assert part is not None
        assert part["input_audio"]["format"] == "x-exotic"

    def test_unknown_kind_returns_none(self) -> None:
        assert attachment_to_content_part({"kind": "weird", "content": b"x"}) is None


class TestAudioFormatMap:
    def test_known_mimes_map_to_codec_tokens(self) -> None:
        assert AUDIO_MIME_TO_FORMAT["audio/mpeg"] == "mp3"
        assert AUDIO_MIME_TO_FORMAT["audio/wav"] == "wav"
        assert AUDIO_MIME_TO_FORMAT["audio/mp4"] == "m4a"


class TestClassifyUpload:
    def test_image(self) -> None:
        assert classify_upload("x.png", "image/png", PNG) == ("image", "image/png", None)

    def test_pdf(self) -> None:
        assert classify_upload("d.pdf", "application/pdf", PDF) == (
            "pdf",
            "application/pdf",
            None,
        )

    def test_audio(self) -> None:
        assert classify_upload("a.wav", "audio/wav", WAV) == ("audio", "audio/wav", None)

    def test_text(self) -> None:
        assert classify_upload("notes.md", "text/markdown", b"# hi") == (
            "text",
            "text/markdown",
            None,
        )

    def test_unsupported_binary_rejected(self) -> None:
        kind, _mime, rej = classify_upload(
            "blob.bin", "application/octet-stream", b"\x00\x01\x02\x03"
        )
        assert kind is None
        assert rej is not None and rej.code == "unsupported" and rej.status == 400

    def test_oversize_rejected(self) -> None:
        big = PNG + b"\x00" * IMAGE_SIZE_CAP  # > image cap
        kind, _mime, rej = classify_upload("big.png", "image/png", big)
        assert kind is None
        assert rej is not None and rej.code == "too_large" and rej.status == 413
