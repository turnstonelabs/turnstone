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
from dataclasses import dataclass
from typing import Any

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
