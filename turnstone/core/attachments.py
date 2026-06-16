"""Attachment data types + upload-classification helpers for user-uploaded files
bound to a workstream turn.

The image-sniff / text-classify helpers live here (rather than in
``turnstone/server.py``) so the console process can wire them into the lifted
attachment endpoints for the coordinator surface without depending on the
node-side server module.  The classification policy is intentionally
kind-agnostic — the same type allowlist applies on both processes.

In the content-addressed model an upload is *staged* in the per-node in-memory
``attachment_buffer`` (keyed by content hash) until the send that references
it commits, at which point the bytes are written content-addressed +
reference-counted into ``workstream_attachments``.  Staging is thread-safe and
idempotent on the content hash, so the old per-(ws,user) upload lock + pending
cap (which only existed to serialize a DB count-check) are gone — the buffer's
own size/TTL ceilings bound a flood instead.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from starlette.responses import JSONResponse

# Byte caps — enforced by the server layer at upload time.  The
# constants live here so the session / tests share the same definitions.
IMAGE_SIZE_CAP: int = 4 * 1024 * 1024
TEXT_DOC_SIZE_CAP: int = 512 * 1024
PDF_SIZE_CAP: int = 32 * 1024 * 1024
AUDIO_SIZE_CAP: int = 25 * 1024 * 1024

ALLOWED_IMAGE_MIMES: frozenset[str] = frozenset(
    {"image/png", "image/jpeg", "image/gif", "image/webp"}
)

# Audio MIMEs accepted as chat attachments (sniffed by magic bytes; the
# client-claimed Content-Type is never trusted alone).  ``AUDIO_MIME_TO_FORMAT``
# maps each to the OpenAI ``input_audio.format`` token the wire builder emits.
ALLOWED_AUDIO_MIMES: frozenset[str] = frozenset(
    {
        "audio/wav",
        "audio/x-wav",
        "audio/mpeg",
        "audio/mp3",
        "audio/ogg",
        "audio/flac",
        "audio/mp4",
        "audio/aac",
        "audio/webm",
    }
)

AUDIO_MIME_TO_FORMAT: dict[str, str] = {
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/ogg": "ogg",
    "audio/flac": "flac",
    "audio/mp4": "m4a",
    "audio/aac": "aac",
    "audio/webm": "webm",
}


@dataclass(frozen=True)
class Attachment:
    """An attachment resolved from storage, ready for injection into a turn.

    ``kind`` is ``"image"``, ``"text"``, ``"pdf"``, or ``"audio"``.  ``content``
    is raw bytes — text attachments are UTF-8 decoded at content-part
    construction; image/pdf/audio are base64-encoded at the wire boundary.
    """

    attachment_id: str
    filename: str
    mime_type: str
    kind: str
    content: bytes

    @property
    def is_image(self) -> bool:
        return self.kind == "image"

    @property
    def is_text(self) -> bool:
        return self.kind == "text"

    @property
    def is_pdf(self) -> bool:
        return self.kind == "pdf"

    @property
    def is_audio(self) -> bool:
        return self.kind == "audio"


# ---------------------------------------------------------------------------
# Upload classification
# ---------------------------------------------------------------------------
_TEXT_ATTACHMENT_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".c",
        ".conf",
        ".cpp",
        ".css",
        ".go",
        ".h",
        ".hpp",
        ".html",
        ".ini",
        ".java",
        ".js",
        ".json",
        ".jsx",
        ".md",
        ".py",
        ".rs",
        ".sh",
        ".sql",
        ".toml",
        ".ts",
        ".tsx",
        ".txt",
        ".xml",
        ".yaml",
        ".yml",
    }
)


def sniff_image_mime(data: bytes) -> str | None:
    """Return a canonical image MIME type by inspecting magic bytes.

    Returns ``None`` if the bytes don't match any supported image
    format. Do not trust the client-provided ``Content-Type`` alone.
    """
    if len(data) < 12:
        return None
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def sniff_pdf_mime(data: bytes) -> str | None:
    """Return ``"application/pdf"`` if ``data`` starts with the PDF magic, else None."""
    return "application/pdf" if data[:5] == b"%PDF-" else None


def sniff_audio_mime(data: bytes) -> str | None:
    """Return a canonical audio MIME type by inspecting magic bytes.

    Covers WAV, MP3 (ID3 tag or MPEG frame sync), AAC (ADTS), OGG, FLAC,
    ISO-BMFF audio (m4a / m4b — video brands like mp4 / mov are rejected), and
    WebM/Matroska.  Returns ``None`` on no match — the client-provided
    ``Content-Type`` is never trusted alone.
    """
    if len(data) < 12:
        return None
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "audio/wav"
    if data[:3] == b"ID3" or data[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return "audio/mpeg"
    # ADTS AAC frame sync (0xFFF...): 0xF1 = MPEG-4, 0xF9 = MPEG-2 (no CRC).
    # Distinct from the MP3 syncs above (FB/F3/F2).  audio/aac is allowed +
    # format-mapped but was never sniffed, so a raw .aac upload always failed.
    if data[:2] in (b"\xff\xf1", b"\xff\xf9"):
        return "audio/aac"
    if data[:4] == b"OggS":
        return "audio/ogg"
    if data[:4] == b"fLaC":
        return "audio/flac"
    # ISO-BMFF: the ``ftyp`` box is shared by MP4/MOV *video* and M4A/M4B
    # *audio*.  Accept only audio brands (major, or an audio brand in the
    # compatible-brands list) so a video file can't masquerade as audio.
    if data[4:8] == b"ftyp" and (
        data[8:12] in (b"M4A ", b"M4B ", b"F4A ", b"F4B ")
        or b"M4A " in data[16:40]
        or b"M4B " in data[16:40]
    ):
        return "audio/mp4"
    if data[:4] == b"\x1aE\xdf\xa3":
        return "audio/webm"
    return None


def classify_text_attachment(
    filename: str, claimed_mime: str, data: bytes
) -> tuple[str | None, str | None]:
    """Return ``(canonical_mime, error)`` for a candidate text upload.

    Accepts MIMEs starting with ``text/`` or in an application allowlist,
    OR a filename with a known text-file extension. The payload must
    decode as UTF-8. Returns ``(None, error_message)`` on rejection.
    """
    allowed_app_mimes = {
        "application/json",
        "application/xml",
        "application/x-yaml",
        "application/yaml",
        "application/toml",
    }
    mime_ok = claimed_mime.startswith("text/") or claimed_mime in allowed_app_mimes
    ext_ok = os.path.splitext(filename)[1].lower() in _TEXT_ATTACHMENT_EXTENSIONS
    if not (mime_ok or ext_ok):
        return None, (
            f"Unsupported file type: {claimed_mime or 'unknown'} (filename: {filename!r})"
        )
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return None, "Text attachment is not valid UTF-8"
    # Normalize MIME — prefer the claimed one if sensible, else text/plain.
    if mime_ok and claimed_mime:
        return claimed_mime, None
    return "text/plain", None


@dataclass(frozen=True)
class UploadRejection:
    """A rejected upload: client-facing message, machine code, HTTP status.

    The single rejection shape both upload paths (the single-file endpoint and
    the create-with-attachments batch) render into a JSON error response.
    """

    message: str
    code: str
    status: int


def _too_large(label: str, size: int, cap: int) -> UploadRejection:
    return UploadRejection(
        f"{label} too large ({size:,} bytes); cap is {cap:,} bytes.", "too_large", 413
    )


def classify_upload(
    filename: str, claimed_mime: str, data: bytes
) -> tuple[str | None, str | None, UploadRejection | None]:
    """Classify one non-empty upload into ``(kind, canonical_mime, rejection)``.

    The single attachment-policy point, shared by the upload endpoint and the
    create-with-attachments batch.  Sniff order is image → pdf → audio (magic
    bytes; the client-claimed ``Content-Type`` is never trusted), then UTF-8
    text by MIME/extension allowlist.  Each kind enforces its own byte cap.
    Returns ``(kind, mime, None)`` on success, or ``(None, None, rejection)`` on
    the first failure.  Callers pre-check for empty data.
    """
    sniffed_image = sniff_image_mime(data)
    if sniffed_image is not None:
        if len(data) > IMAGE_SIZE_CAP:
            return None, None, _too_large("Image", len(data), IMAGE_SIZE_CAP)
        return "image", sniffed_image, None
    if sniff_pdf_mime(data) is not None:
        if len(data) > PDF_SIZE_CAP:
            return None, None, _too_large("PDF", len(data), PDF_SIZE_CAP)
        return "pdf", "application/pdf", None
    sniffed_audio = sniff_audio_mime(data)
    if sniffed_audio is not None:
        if len(data) > AUDIO_SIZE_CAP:
            return None, None, _too_large("Audio", len(data), AUDIO_SIZE_CAP)
        return "audio", sniffed_audio, None
    if len(data) > TEXT_DOC_SIZE_CAP:
        return None, None, _too_large("Text document", len(data), TEXT_DOC_SIZE_CAP)
    mime, err = classify_text_attachment(filename, claimed_mime, data)
    if mime is None:
        return None, None, UploadRejection(err or "Unsupported file type", "unsupported", 400)
    return "text", mime, None


def validate_and_save_uploaded_files(
    files: list[tuple[str, str, bytes]],
    ws_id: str,
    user_id: str,
) -> tuple[list[str], JSONResponse | None]:
    """Classify + stage a list of ``(filename, claimed_mime, data)`` tuples.

    Applies the same validation rules as the upload endpoint (magic-byte image
    sniffing, UTF-8 text decode, per-kind size cap) and stages each file in the
    per-node :class:`~turnstone.core.attachment_buffer.AttachmentBuffer`.  The
    returned ids are the content hashes the buffer computed — re-uploading
    identical bytes is idempotent (same id).  The per-(ws,user) pending cap and
    its lock are gone; the buffer's own size/TTL ceilings bound a flood.

    Kind-agnostic: both interactive and coordinator create-with-attachments
    paths call into this helper from the lifted ``make_create_handler``
    factory.

    Returns ``(attachment_ids, None)`` on success or ``(ids_staged_so_far,
    JSONResponse)`` on the first failure so the caller can roll back any
    partial state.
    """
    from starlette.responses import JSONResponse as _JSONResponse

    from turnstone.core.attachment_buffer import get_attachment_buffer

    saved_ids: list[str] = []
    if not files:
        return saved_ids, None

    buffer = get_attachment_buffer()
    for filename, claimed_mime, data in files:
        if not data:
            return saved_ids, _JSONResponse({"error": "Empty file"}, status_code=400)
        kind, mime, rejection = classify_upload(filename, claimed_mime, data)
        if rejection is not None:
            return saved_ids, _JSONResponse(
                {"error": rejection.message, "code": rejection.code},
                status_code=rejection.status,
            )
        assert kind is not None and mime is not None  # success ⟹ both set
        staged = buffer.stage(
            ws_id=ws_id,
            user_id=user_id,
            filename=filename,
            mime_type=mime,
            kind=kind,
            content=data,
        )
        saved_ids.append(staged.attachment_id)
    return saved_ids, None


def resolve_staged_attachments(
    requested_ids: list[str],
    ws_id: str,
    user_id: str,
) -> tuple[list[Attachment], list[str], list[str]]:
    """Resolve staged uploads for *requested_ids* to Attachment objects.

    Returns ``(resolved, taken, dropped)``.  ``taken`` is the subset of
    *requested_ids* present in the buffer for ``(ws_id, user_id)`` (in request
    order); ``dropped`` is the rest (buffer-evicted, never staged, or out of
    scope).

    This is a *peek*, not a drain: the entries stay in the buffer so a send
    that resolves them but doesn't commit (e.g. the queue rejects an
    attachment-bearing turn → ``attachments_busy``, and the client retries) can
    still find them.  The committing path drains them at write time via
    :meth:`ChatSession._append_user_turn` (``buffer.discard`` per persisted
    id); anything left over expires on the buffer's TTL.

    Kind-agnostic: both create-with-attachments and ``/send`` paths call this.
    The old ``send_id`` reservation token is gone — the buffer is the pending
    store and scoping is ``(ws_id, user_id)`` on the staged entry itself.
    """
    from turnstone.core.attachment_buffer import get_attachment_buffer

    if not requested_ids:
        return [], [], []

    buffer = get_attachment_buffer()
    resolved: list[Attachment] = []
    taken: list[str] = []
    dropped: list[str] = []
    for aid in requested_ids:
        s = buffer.get(aid, ws_id=ws_id, user_id=user_id)
        if s is None:
            dropped.append(aid)
            continue
        taken.append(aid)
        resolved.append(
            Attachment(
                attachment_id=s.attachment_id,
                filename=s.filename,
                mime_type=s.mime_type or "application/octet-stream",
                kind=s.kind,
                content=s.content,
            )
        )
    return resolved, taken, dropped


def unreadable_placeholder(filename: str) -> dict[str, Any]:
    """Return a content-part placeholder used when an attachment can't be
    decoded for a given turn.

    Shared between live injection (session.send) and history replay
    (storage._utils) so the wording stays canonical.
    """
    return {
        "type": "text",
        "text": f"[unreadable attachment: {filename or 'attachment'}]",
    }
