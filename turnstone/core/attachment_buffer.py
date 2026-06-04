"""Per-node in-memory buffer for pending (uploaded-but-unsent) attachments.

In the content-addressed attachment model, blob bytes are written to
``workstream_attachments`` only at send-commit, so every *stored* blob is born
referenced (refcount >= 1). Between the upload request and the send that references it,
the bytes live here — content-addressed by their sha256 hash (stored ONCE, mirroring
the committed store) with the per-scope ``(ws_id, user_id)`` references that staged them
held alongside, and bounded by a TTL plus a total-size ceiling so a flood of unsent
uploads can't exhaust a node. ws->node affinity (HRW routing) guarantees the upload and
the later send hit the same node, so this buffer is process-local.

Keying the blob by hash alone (not by scope) means identical bytes staged from two tabs /
workstreams dedupe to one copy, yet each scope keeps its own reference — so one scope's
send can't drop another's pending upload (the references are tracked independently, the
bytes shared). A blob is evicted only when its last reference is dropped or expires.

Losing an unsent upload on a crash / restart / node re-route is acceptable: it is
pre-commit transient state, never committed data — the user re-uploads. This is the
deliberate simplification that replaces the persisted pending/reserved/consumed
lifecycle (and its orphan-sweep) the old upload flow carried.
"""

from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

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


@dataclass(frozen=True, slots=True)
class _Ref:
    """Per-scope metadata for one reference to a shared staged blob.

    The same bytes staged from different ``(ws_id, user_id)`` scopes — or under
    different filenames — share one blob but carry their own ``_Ref`` (filename /
    mime / kind differ per upload; ``staged_at`` drives that reference's TTL).
    """

    filename: str
    mime_type: str
    kind: str
    staged_at: float


@dataclass(slots=True)
class _StagedBlob:
    """Content-addressed bytes (stored once) + the per-scope references to them."""

    content: bytes
    refs: dict[tuple[str, str], _Ref] = field(default_factory=dict)  # (ws_id, user_id) -> _Ref


class AttachmentBuffer:
    """Thread-safe, size- and TTL-bounded store of pending uploads, content-addressed.

    A single lock guards the whole structure: the critical sections are tiny
    in-memory dict mutations with no I/O, so one lock is both correct and (under
    the GIL) as concurrent as per-entry locking would be, without the
    creation-race / ordering complexity.
    """

    def __init__(
        self,
        *,
        max_total_bytes: int = _DEFAULT_MAX_TOTAL_BYTES,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._lock = threading.Lock()
        # content hash -> the deduped blob (bytes once) + its per-scope references.
        self._blobs: dict[str, _StagedBlob] = {}
        self._max_total_bytes = max_total_bytes
        self._ttl = ttl_seconds
        self._clock = clock

    def stage(
        self, *, ws_id: str, user_id: str, filename: str, mime_type: str, kind: str, content: bytes
    ) -> StagedAttachment:
        """Hold *content* until a send commits it; returns the staged entry.

        The id is the content hash, so identical bytes dedupe to one blob (and to
        the eventual content-addressed store).  Re-staging from a different scope
        adds a reference to the existing blob rather than overwriting it, so each
        scope's send resolves its own upload independently.
        """
        handle = hashlib.sha256(content).hexdigest()
        ref = _Ref(filename=filename, mime_type=mime_type, kind=kind, staged_at=self._clock())
        with self._lock:
            self._evict_expired_locked()
            blob = self._blobs.get(handle)
            if blob is None:
                blob = _StagedBlob(content=content)
                self._blobs[handle] = blob
            blob.refs[(ws_id, user_id)] = ref
            self._evict_over_capacity_locked()
        return StagedAttachment(
            attachment_id=handle,
            ws_id=ws_id,
            user_id=user_id,
            filename=filename,
            mime_type=mime_type,
            kind=kind,
            content=content,
            staged_at=ref.staged_at,
        )

    def get(self, handle: str, *, ws_id: str, user_id: str) -> StagedAttachment | None:
        """Fetch a pending upload, enforcing the uploader's scope (else ``None``)."""
        with self._lock:
            blob = self._blobs.get(handle)
            if blob is None:
                return None
            ref = blob.refs.get((ws_id, user_id))
            if ref is None:
                return None
            return _to_staged(handle, ws_id, user_id, blob, ref)

    def list_for(self, *, ws_id: str, user_id: str) -> list[StagedAttachment]:
        """Pending uploads for ``(ws_id, user_id)``."""
        with self._lock:
            self._evict_expired_locked()
            out: list[StagedAttachment] = []
            for handle, blob in self._blobs.items():
                ref = blob.refs.get((ws_id, user_id))
                if ref is not None:
                    out.append(_to_staged(handle, ws_id, user_id, blob, ref))
            return out

    def discard(self, handle: str, *, ws_id: str, user_id: str) -> bool:
        """Drop one scope's reference (scope-checked); ``True`` if it existed.

        The shared blob is evicted only when its last reference goes — so a
        committing send draining its own reference leaves another scope's pending
        upload of the same bytes intact.
        """
        with self._lock:
            blob = self._blobs.get(handle)
            if blob is None or (ws_id, user_id) not in blob.refs:
                return False
            del blob.refs[(ws_id, user_id)]
            if not blob.refs:
                del self._blobs[handle]
            return True

    def clear(self) -> None:
        """Drop all pending uploads (every reference and blob).

        For node shutdown / test isolation — the buffer holds only transient
        pre-commit state, so a full reset discards nothing committed.
        """
        with self._lock:
            self._blobs.clear()

    # -- eviction (caller holds the lock) ------------------------------------
    def _evict_expired_locked(self) -> None:
        if self._ttl <= 0:
            return
        cutoff = self._clock() - self._ttl
        for handle in list(self._blobs):
            blob = self._blobs[handle]
            stale = [scope for scope, ref in blob.refs.items() if ref.staged_at < cutoff]
            for scope in stale:
                del blob.refs[scope]
            if not blob.refs:
                del self._blobs[handle]

    def _evict_over_capacity_locked(self) -> None:
        # Deduped: each blob's bytes count once regardless of how many scopes
        # reference it — the size ceiling bounds the bytes actually resident.
        total = sum(len(b.content) for b in self._blobs.values())
        if total <= self._max_total_bytes:
            return
        # Evict whole blobs oldest-first, ordered by each blob's earliest
        # outstanding reference, until under the ceiling.
        ordered = sorted(
            self._blobs.items(),
            key=lambda kv: min(ref.staged_at for ref in kv[1].refs.values()),
        )
        for handle, blob in ordered:
            if total <= self._max_total_bytes:
                break
            total -= len(blob.content)
            del self._blobs[handle]


def _to_staged(
    handle: str, ws_id: str, user_id: str, blob: _StagedBlob, ref: _Ref
) -> StagedAttachment:
    """Project a shared blob + a per-scope reference into the public view."""
    return StagedAttachment(
        attachment_id=handle,
        ws_id=ws_id,
        user_id=user_id,
        filename=ref.filename,
        mime_type=ref.mime_type,
        kind=ref.kind,
        content=blob.content,
        staged_at=ref.staged_at,
    )


_BUFFER = AttachmentBuffer()


def get_attachment_buffer() -> AttachmentBuffer:
    """The per-node pending-upload buffer singleton."""
    return _BUFFER
