"""Attachment data types + upload-classification helpers for user-uploaded files
bound to a workstream turn.

The image-sniff / text-classify / per-(ws,user) upload-lock helpers
live here (rather than in ``turnstone/server.py``) so the console
process can wire them into the lifted attachment endpoints for the
coordinator surface without depending on the node-side server module.
The classification policy is intentionally kind-agnostic — the same
type allowlist applies on both processes.
"""

from __future__ import annotations

import collections
import os
import threading
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from starlette.responses import JSONResponse

# Byte caps — enforced by the server layer at upload time.  The
# constants live here so the session / tests share the same definitions.
IMAGE_SIZE_CAP: int = 4 * 1024 * 1024
TEXT_DOC_SIZE_CAP: int = 512 * 1024
# Cap on simultaneously-pending attachments for a single (ws, user).
# Once reserved for a queued message the row no longer counts against
# this budget, so the name reflects the pending-pool limit rather than
# a per-message limit.
MAX_PENDING_ATTACHMENTS_PER_USER_WS: int = 10

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
# Per-(ws, user) upload lock cache
# ---------------------------------------------------------------------------
# Soft cap on the upload-lock cache. Locks are evicted opportunistically
# when an upload completes (see ``upload_lock``); a held lock means an
# upload is in flight, never evicted.
_ATTACHMENT_UPLOAD_LOCKS_MAX: int = 1024
_attachment_upload_locks: collections.OrderedDict[tuple[str, str], threading.Lock] = (
    collections.OrderedDict()
)
_attachment_upload_locks_mx: threading.Lock = threading.Lock()


def upload_lock(ws_id: str, user_id: str) -> threading.Lock:
    """Return (and track) the per-(ws, user) upload mutex.

    Called at the start of every attachment upload to serialize the
    pending-cap check + save sequence per (ws, user) — concurrent
    uploads can't both pass a check that sees ``count == cap-1``.
    Process-local cache; bounded eviction skips held locks.
    """
    key = (ws_id, user_id)
    with _attachment_upload_locks_mx:
        lock = _attachment_upload_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _attachment_upload_locks[key] = lock
        else:
            # Touch for LRU
            _attachment_upload_locks.move_to_end(key)
        # Opportunistic eviction once we exceed the soft cap. Skip
        # held locks (an upload is in flight under that key).
        if len(_attachment_upload_locks) > _ATTACHMENT_UPLOAD_LOCKS_MAX:
            for stale_key in list(_attachment_upload_locks):
                if len(_attachment_upload_locks) <= _ATTACHMENT_UPLOAD_LOCKS_MAX:
                    break
                if stale_key == key:
                    continue  # never evict the lock we're handing out
                stale = _attachment_upload_locks[stale_key]
                # threading.Lock has no public locked() — use the
                # non-blocking acquire-and-release probe instead.
                if stale.acquire(blocking=False):
                    stale.release()
                    del _attachment_upload_locks[stale_key]
        return lock


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
    """Classify + save a list of ``(filename, claimed_mime, data)`` tuples.

    Applies the same validation rules as ``upload_attachment`` (magic-byte
    image sniffing, UTF-8 text decode, per-kind size cap, per-(ws,user)
    pending cap) under the shared :func:`upload_lock`.

    Kind-agnostic: both interactive and coordinator create-with-attachments
    paths call into this helper from the lifted ``make_create_handler``
    factory (Stage 2 ``create`` verb lift). The helper does not consult
    any kind-specific config — the storage layer is kind-agnostic by
    design (P1.5).

    Returns ``(attachment_ids, None)`` on success or ``(ids_saved_so_far,
    JSONResponse)`` on the first failure so the caller can roll back any
    partial state.
    """
    from starlette.responses import JSONResponse as _JSONResponse

    from turnstone.core.memory import list_pending_attachments, save_attachment

    saved_ids: list[str] = []
    if not files:
        return saved_ids, None

    lock = upload_lock(ws_id, user_id)
    with lock:
        pending_count = len(list_pending_attachments(ws_id, user_id))
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

            if pending_count + 1 > MAX_PENDING_ATTACHMENTS_PER_USER_WS:
                return saved_ids, _JSONResponse(
                    {
                        "error": (
                            f"Too many pending attachments "
                            f"(max {MAX_PENDING_ATTACHMENTS_PER_USER_WS} pending per workstream)"
                        ),
                        "code": "too_many",
                    },
                    status_code=409,
                )
            attachment_id = uuid.uuid4().hex
            save_attachment(
                attachment_id,
                ws_id,
                user_id,
                filename,
                mime,
                len(data),
                kind,
                data,
            )
            saved_ids.append(attachment_id)
            pending_count += 1
    return saved_ids, None


def reserve_and_resolve_attachments(
    requested_ids: list[str],
    send_id: str,
    ws_id: str,
    user_id: str,
) -> tuple[list[Attachment], list[str], list[str]]:
    """Reserve attachment ids for ``send_id`` and resolve to Attachment objects.

    Returns ``(resolved, ordered_reserved, dropped)``. ``dropped`` is the
    subset of *requested_ids* that could not be reserved (already consumed,
    lost a race, or cross-scope).

    Kind-agnostic: both interactive and coordinator create-with-attachments
    paths call into this helper from their respective ``post_install``
    callbacks (Stage 2 ``create`` verb lift). The reservation token
    (``send_id``) scopes both the soft-lock and the eventual consume —
    the worker calling ``ChatSession.send(..., send_id=...)`` matches
    the lock and converts pending → consumed; failure paths
    ``unreserve_attachments(send_id, ws_id, user_id)`` to release the
    rows back to pending.
    """
    from turnstone.core.memory import get_attachments as _get_attachments
    from turnstone.core.memory import reserve_attachments as _reserve

    if not requested_ids:
        return [], [], []

    reserved_ids: list[str] = _reserve(requested_ids, send_id, ws_id, user_id)
    reserved_set = set(reserved_ids)
    ordered_reserved: list[str] = [aid for aid in requested_ids if aid in reserved_set]
    dropped: list[str] = [aid for aid in requested_ids if aid not in reserved_set]

    resolved: list[Attachment] = []
    if ordered_reserved:
        rows = _get_attachments(ordered_reserved)
        rows_by_id = {str(r["attachment_id"]): r for r in rows}
        for aid in ordered_reserved:
            r = rows_by_id.get(aid)
            if not r:
                continue
            if (
                r.get("ws_id") != ws_id
                or r.get("user_id") != user_id
                or r.get("message_id") is not None
                or r.get("reserved_for_msg_id") != send_id
            ):
                continue
            content = r.get("content")
            if not isinstance(content, bytes):
                continue
            resolved.append(
                Attachment(
                    attachment_id=str(r["attachment_id"]),
                    filename=str(r.get("filename") or ""),
                    mime_type=str(r.get("mime_type") or "application/octet-stream"),
                    kind=str(r.get("kind") or ""),
                    content=content,
                )
            )
    return resolved, ordered_reserved, dropped


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
