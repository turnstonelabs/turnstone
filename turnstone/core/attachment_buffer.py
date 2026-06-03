"""Per-node in-memory buffer for pending (uploaded-but-unsent) attachments.

In the content-addressed attachment model, blob bytes are written to
``workstream_attachments`` only at send-commit, so every *stored* blob is born
referenced (refcount >= 1). Between the upload request and the send that references it,
the bytes live here — keyed by their content hash, scoped to ``(ws_id, user_id)``, and
bounded by a TTL plus a total-size ceiling so a flood of unsent uploads can't exhaust a
node. ws->node affinity (HRW routing) guarantees the upload and the later send hit the
same node, so this buffer is process-local.

Losing an unsent upload on a crash / restart / node re-route is acceptable: it is
pre-commit transient state, never committed data — the user re-uploads. This is the
deliberate simplification that replaces the persisted pending/reserved/consumed
lifecycle (and its orphan-sweep) the old upload flow carried.
"""

from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

# OOM-safety backstops (per node), not a product policy — the per-user upload cap was
# removed.  Generous: the goal is only to bound a pathological flood of unsent uploads.
_DEFAULT_MAX_TOTAL_BYTES = 1024 * 1024 * 1024  # 1 GiB
_DEFAULT_TTL_SECONDS = 3600.0  # unsent uploads expire after an hour


@dataclass(frozen=True, slots=True)
class StagedAttachment:
    """A pending upload held in memory until the send that references it commits."""

    attachment_id: str  # content hash (sha256 hex) — also the content-addressed blob id
    ws_id: str
    user_id: str
    filename: str
    mime_type: str
    kind: str  # 'image' | 'text'
    content: bytes
    staged_at: float

    @property
    def size_bytes(self) -> int:
        return len(self.content)


class AttachmentBuffer:
    """Thread-safe, size- and TTL-bounded store of pending uploads, keyed by content hash."""

    def __init__(
        self,
        *,
        max_total_bytes: int = _DEFAULT_MAX_TOTAL_BYTES,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, StagedAttachment] = {}
        self._max_total_bytes = max_total_bytes
        self._ttl = ttl_seconds
        self._clock = clock

    def stage(
        self, *, ws_id: str, user_id: str, filename: str, mime_type: str, kind: str, content: bytes
    ) -> StagedAttachment:
        """Hold *content* until a send commits it; returns the staged entry.

        The id is the content hash, so re-uploading identical bytes is idempotent (and
        dedupes against the eventual content-addressed blob).
        """
        handle = hashlib.sha256(content).hexdigest()
        entry = StagedAttachment(
            attachment_id=handle,
            ws_id=ws_id,
            user_id=user_id,
            filename=filename,
            mime_type=mime_type,
            kind=kind,
            content=content,
            staged_at=self._clock(),
        )
        with self._lock:
            self._evict_expired_locked()
            self._entries[handle] = entry
            self._evict_over_capacity_locked()
        return entry

    def get(self, handle: str, *, ws_id: str, user_id: str) -> StagedAttachment | None:
        """Fetch a pending upload, enforcing the uploader's scope (else ``None``)."""
        with self._lock:
            entry = self._entries.get(handle)
            if entry is None or entry.ws_id != ws_id or entry.user_id != user_id:
                return None
            return entry

    def list_for(self, *, ws_id: str, user_id: str) -> list[StagedAttachment]:
        """Pending uploads for ``(ws_id, user_id)``."""
        with self._lock:
            self._evict_expired_locked()
            return [e for e in self._entries.values() if e.ws_id == ws_id and e.user_id == user_id]

    def discard(self, handle: str, *, ws_id: str, user_id: str) -> bool:
        """Drop a pending upload (scope-checked); ``True`` if it existed."""
        with self._lock:
            entry = self._entries.get(handle)
            if entry is None or entry.ws_id != ws_id or entry.user_id != user_id:
                return False
            del self._entries[handle]
            return True

    def take(self, handles: Iterable[str], *, ws_id: str) -> list[StagedAttachment]:
        """Pop the staged entries for *handles* at send-commit (ws-scoped).

        Missing / out-of-scope handles are skipped — the send proceeds with whatever
        committed (a buffer eviction or crash between upload and send drops the upload).
        """
        with self._lock:
            taken: list[StagedAttachment] = []
            for handle in handles:
                entry = self._entries.get(handle)
                if entry is not None and entry.ws_id == ws_id:
                    taken.append(entry)
                    del self._entries[handle]
            return taken

    # -- eviction (caller holds the lock) ------------------------------------
    def _evict_expired_locked(self) -> None:
        if self._ttl <= 0:
            return
        cutoff = self._clock() - self._ttl
        stale = [h for h, e in self._entries.items() if e.staged_at < cutoff]
        for handle in stale:
            del self._entries[handle]

    def _evict_over_capacity_locked(self) -> None:
        total = sum(e.size_bytes for e in self._entries.values())
        if total <= self._max_total_bytes:
            return
        # Evict oldest-first until under the ceiling.
        for handle, entry in sorted(self._entries.items(), key=lambda kv: kv[1].staged_at):
            if total <= self._max_total_bytes:
                break
            total -= entry.size_bytes
            del self._entries[handle]


_BUFFER = AttachmentBuffer()


def get_attachment_buffer() -> AttachmentBuffer:
    """The per-node pending-upload buffer singleton."""
    return _BUFFER
