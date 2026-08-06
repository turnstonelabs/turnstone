"""Think-tag splitting: streaming (interactive) and one-shot (drained) forms.

:class:`ThinkTagSplitter` is the ONE tag-semantics engine — vocabulary,
earliest-tag-wins selection, any-close-closes-any-open, the partial-tag
carry.  :func:`split_inline_reasoning` is its drained form: the same
engine applied to a complete text (feed + flush), plus the residue
whitespace rule.  All tag selection happens inside the class; the
one-shot's fast path tests only tag PRESENCE, never position."""

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

    *scan_tags* ``False`` turns the tag scan OFF for backends that
    segregate reasoning themselves (``capabilities.server_parses_reasoning``
    — a vLLM launched with a reasoning parser, a commercial provider):
    spans pass straight through at the current state, so prose that merely
    QUOTES a tag can no longer be misrouted, and no carry is held.  The
    state machine stays live either way — :attr:`in_think` still mirrors
    the out-of-band transitions its consumer writes.
    """

    OPEN_TAGS: tuple[str, ...] = ("<think>", "<reasoning>")
    CLOSE_TAGS: tuple[str, ...] = ("</think>", "</reasoning>")
    ALL_TAGS: tuple[str, ...] = OPEN_TAGS + CLOSE_TAGS
    MAX_TAG_LEN = max(len(t) for t in ALL_TAGS)

    def __init__(self, emit: Callable[[str, bool], None], *, scan_tags: bool = True) -> None:
        self._emit = emit
        self._scan_tags = scan_tags
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

    def close_run(self) -> str:
        """Close the current run at an out-of-band interleave signal.

        For a provider-parsed ``reasoning_delta`` arriving mid-stream:
        everything decided emits at the CURRENT state, and a possible
        partial-tag tail (:func:`partial_tag_tail`) is RETURNED to the
        caller.  The drain splits its runs at the same boundary by the
        same rule, so the displayed and committed readings of one stream
        agree.

        The carry belongs to the run owner, not to :attr:`pending`: the
        tail was cut in the closing run's state, while ``pending`` is
        read under whatever state later flushes hit (``in_think`` flips
        across the reasoning block), which would relabel a content-state
        carry as reasoning.  The caller re-feeds it when content resumes
        — reassembling a tag the server split across the block — or
        flushes it at its original state at a terminal boundary.
        """
        if not self.pending:
            return ""
        tail = partial_tag_tail(self.pending)
        closeable = self.pending[: len(self.pending) - len(tail)] if tail else self.pending
        if closeable:
            self._emit(closeable, self.in_think)
        self.pending = ""
        return tail

    def _drain(self) -> None:
        if not self._scan_tags:
            # Nothing to resolve, so nothing to hold: a tag-free contract
            # makes every span immediately safe — the same emit
            # ``flush_pending`` performs at a stream boundary.
            self.flush_pending()
            return
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


def partial_tag_tail(text: str) -> str:
    """The longest suffix of *text* that could still grow into a tag.

    Tag-vocabulary knowledge for boundary handling: a consumer that must
    finalize a span at an interleaving signal (``drain_stream`` closing a
    content run at a ``reasoning_delta``) uses this to hold back ONLY a
    possible partial tag for the next span — reassembling a tag the
    server split across the signal — while everything decided emits with
    the span it arrived in.  Returns ``""`` when no suffix is a proper
    prefix of any tag (a complete tag is not a partial one).
    """
    limit = min(len(text), ThinkTagSplitter.MAX_TAG_LEN - 1)
    for size in range(limit, 0, -1):
        suffix = text[-size:]
        # Proper prefix only: without the length check a complete tag
        # shorter than the longest one self-matches and gets carried as
        # a "partial".
        if any(
            len(suffix) < len(tag) and tag.startswith(suffix) for tag in ThinkTagSplitter.ALL_TAGS
        ):
            return suffix
    return ""


def split_inline_reasoning(text: str, *, scan_tags: bool = True) -> tuple[str, str]:
    """Split a complete drained text into ``(content, reasoning)``.

    *scan_tags* ``False`` (the backend segregates reasoning itself —
    ``capabilities.server_parses_reasoning``) returns the text unsplit:
    there is no inline reasoning to find, and scanning could only
    misroute prose that quotes a tag.

    The one-shot form of :class:`ThinkTagSplitter` for non-streaming
    consumers (``drain_stream``): a plain feed-and-flush of ONE content
    run — the interactive lane's per-run rule, no more.  The caller owns
    run boundaries (``drain_stream`` closes a run when tool-call deltas
    or provider-parsed reasoning interleave, mirroring the interactive
    consumer's flush-and-reset at those signals); a partial tag never
    spans a TOOL boundary, and across a reasoning delta the caller
    carries a possible partial-tag tail into the next run
    (:func:`partial_tag_tail`) so a tag the server split there still
    reassembles.  Balanced
    blocks land in the reasoning lane; an unterminated open sends the
    tail to reasoning; an orphan CLOSE tag (no prior open) stays in
    content untouched.  That last case is deliberate: a close tag whose
    open never arrived is indistinguishable from prose that merely
    QUOTES the tag, and drained lanes routinely quote third-party text
    (a web-fetch answer citing a page about reasoning models, a guard
    verdict echoing judged content) — reclassifying everything before
    it would let that text destroy the result.  Display-string lanes
    that want stricter cosmetic peeling (the title) own it locally as
    formatting, not segregation.

    The split is RAW: tag residue (the ``"\\n\\n"`` a leading block
    leaves behind) stays in the returned content.  Exactly ONE trim
    policy exists in the tree and ``drain_stream`` owns it — it joins
    the per-run splits and applies :func:`strip_blank_edge_lines` once
    over the whole when any run consumed a tag (a run edge may be
    INTERIOR after joining, where a genuine paragraph separator must
    survive).  With no tag present anywhere the input returns
    byte-identical (fast path), so tag-free lanes cannot drift.
    """
    if not scan_tags or not any(tag in text for tag in ThinkTagSplitter.ALL_TAGS):
        return text, ""

    content_parts: list[str] = []
    reasoning_parts: list[str] = []

    def _collect(span: str, is_reasoning: bool) -> None:
        (reasoning_parts if is_reasoning else content_parts).append(span)

    splitter = ThinkTagSplitter(_collect)
    splitter.feed(text)
    splitter.flush_pending()
    content = "".join(content_parts)
    if len(content) == len(text):
        # Only orphan close tags were present — nothing was consumed;
        # the text passes through byte-identical.
        return text, ""
    return content, "".join(reasoning_parts)


def strip_blank_edge_lines(text: str) -> str:
    """Remove leading/trailing lines that are entirely whitespace.

    The residue-trim unit ``drain_stream`` applies once over its joined
    per-run splits: kills the separator lines a consumed tag leaves
    behind while preserving the first surviving line's significant
    indentation — ``.strip()`` would delete it and silently reformat
    whitespace-significant output.
    """
    lines = text.split("\n")
    start, end = 0, len(lines)
    while start < end and not lines[start].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    return "\n".join(lines[start:end])
