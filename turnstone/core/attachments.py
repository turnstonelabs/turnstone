"""Attachment data types for user-uploaded files bound to a workstream turn."""

from __future__ import annotations

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
