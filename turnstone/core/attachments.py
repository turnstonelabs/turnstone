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

ALLOWED_IMAGE_MIMES: frozenset[str] = frozenset(
    {"image/png", "image/jpeg", "image/gif", "image/webp"}
)


@dataclass(frozen=True)
class Attachment:
    """An attachment resolved from storage, ready for injection into a turn.

    ``kind`` is ``"image"`` or ``"text"``.  ``content`` is raw bytes — for
    text attachments, UTF-8 decoded at the point of content-part
    construction.
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
        sniffed_image = sniff_image_mime(data)
        if sniffed_image is not None:
            if len(data) > IMAGE_SIZE_CAP:
                return saved_ids, _JSONResponse(
                    {
                        "error": (
                            f"Image too large ({len(data):,} bytes); "
                            f"cap is {IMAGE_SIZE_CAP:,} bytes."
                        ),
                        "code": "too_large",
                    },
                    status_code=413,
                )
            kind = "image"
            mime = sniffed_image
        else:
            if len(data) > TEXT_DOC_SIZE_CAP:
                return saved_ids, _JSONResponse(
                    {
                        "error": (
                            f"Text document too large ({len(data):,} bytes); "
                            f"cap is {TEXT_DOC_SIZE_CAP:,} bytes."
                        ),
                        "code": "too_large",
                    },
                    status_code=413,
                )
            mime_or_err = classify_text_attachment(filename, claimed_mime, data)
            if mime_or_err[0] is None:
                return saved_ids, _JSONResponse(
                    {"error": mime_or_err[1], "code": "unsupported"},
                    status_code=400,
                )
            kind = "text"
            mime = mime_or_err[0]

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
