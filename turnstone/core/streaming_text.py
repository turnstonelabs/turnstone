"""Streaming think-tag splitting for interactive content streams."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable


class ThinkTagSplitter:
    """Split streamed content into content vs reasoning around think tags.

    Local-model servers without a reasoning parser emit reasoning inline
    as ``<think>``/``<reasoning>`` blocks inside the content stream, and
    a tag can arrive split across chunk boundaries.  This class owns the
    carry buffer and the in-tag state: :meth:`feed` buffers a chunk and
    emits every span that provably cannot be part of an unresolved tag;
    the trailing :data:`MAX_TAG_LEN` chars are held until a later chunk
    (or a final :meth:`flush_pending`) resolves them.

    Emission goes through the *emit* callback ``(text, is_reasoning)`` —
    spans arrive in stream order, never empty, exactly once.  The
    consumer owns dispatch (UI callbacks, accumulation) and may
    read/write :attr:`in_think` directly to mirror out-of-band
    transitions (the provider-normalized ``reasoning_delta`` path,
    tool-call starts).

    Tag selection: at each step the EARLIEST occurrence wins among the
    tag variants for the current state (open tags outside a block, close
    tags inside).
    """

    OPEN_TAGS: tuple[str, ...] = ("<think>", "<reasoning>")
    CLOSE_TAGS: tuple[str, ...] = ("</think>", "</reasoning>")
    MAX_TAG_LEN = max(len(t) for t in OPEN_TAGS + CLOSE_TAGS)

    def __init__(self, emit: Callable[[str, bool], None]) -> None:
        self._emit = emit
        self.pending = ""
        self.in_think = False

    def feed(self, text: str) -> None:
        """Buffer *text* and emit every tag-resolved span."""
        self.pending += text
        self._drain()

    def flush_pending(self) -> None:
        """Emit the raw carry buffer at the current state, with no tag scan.

        For stream boundaries where a partial tag is no longer possible:
        end of stream, tool calls beginning, cancellation.
        """
        if self.pending:
            self._emit(self.pending, self.in_think)
            self.pending = ""

    def _drain(self) -> None:
        while self.pending:
            tags = self.CLOSE_TAGS if self.in_think else self.OPEN_TAGS
            best_idx, best_tag = None, None
            for tag in tags:
                idx = self.pending.find(tag)
                if idx != -1 and (best_idx is None or idx < best_idx):
                    best_idx, best_tag = idx, tag

            if best_idx is not None:
                assert best_tag is not None
                if best_idx:
                    self._emit(self.pending[:best_idx], self.in_think)
                self.pending = self.pending[best_idx + len(best_tag) :]
                self.in_think = not self.in_think
                continue

            # No tag in sight — everything but a possible partial-tag tail
            # is provably safe to emit now (live streaming beats holding
            # the whole buffer for a tag that may never come).
            safe = len(self.pending) - self.MAX_TAG_LEN
            if safe > 0:
                self._emit(self.pending[:safe], self.in_think)
                self.pending = self.pending[safe:]
            break
