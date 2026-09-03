"""Honest, exact-cap text truncation primitives.

The mechanics in this module are deliberately policy-free.  Callers choose
whether a head or head-and-tail projection is useful, and may supply
surface-specific marker wording, but all of them receive the same guarantees:

* the returned text never exceeds the requested positive character limit;
* truncation is reported structurally instead of inferred from string length;
* tiny limits cannot fail open (notably Python's ``text[-0:]`` trap); and
* streaming producers can retain bounded edges without first allocating their
  complete output; and
* a lossy rendering can carry its source edges along, so a later cut reports
  omission against the true size instead of against the earlier rendering.

Context-exhaustion policy remains in :class:`turnstone.core.session.ChatSession`:
its zero-budget receipt intentionally replaces source text with a controller
error that may be longer than the zero-character source allowance.
"""

from __future__ import annotations

import io
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

TruncationMode = Literal["head", "head_tail"]
TruncationMarker = Callable[[int, int, int], str]


def default_truncation_marker(omitted: int, original: int, limit: int) -> str:
    """The canonical general-purpose tool-output omission marker.

    An output larger than the limit exceeded it.  One the limit could have
    held was cut only because its producer retained less than the whole, and
    the marker says so instead of blaming a limit that was never exceeded.
    """

    if original > limit:
        return f"\n\n... [{omitted} chars truncated — output exceeded {limit} char limit] ...\n\n"
    return (
        f"\n\n... [{omitted} chars truncated — {original - omitted} of {original} chars "
        "retained] ...\n\n"
    )


@dataclass(frozen=True, slots=True)
class TruncationResult:
    """One bounded text projection and explicit source-coverage metadata.

    ``marker`` is the omission marker this rendering embedded, empty when it
    embedded none (nothing was omitted, or an already-cut rendering was
    reported against its true size), so a consumer that transforms the
    rendering can still find the cut and re-project the edges around it.
    """

    text: str
    original_chars: int
    limit_chars: int
    omitted_chars: int
    marker: str = ""

    @property
    def retained_chars(self) -> int:
        return self.original_chars - self.omitted_chars

    @property
    def truncated(self) -> bool:
        return self.omitted_chars > 0


@dataclass(frozen=True, slots=True)
class TextProjectionSource:
    """Retained source edges plus the true size they were cut from.

    A producer that cannot hold its complete output keeps ``prefix`` and
    ``suffix`` and counts the rest.  Every later consumer renders from these
    edges, so each projection reports omission against ``original_chars``
    rather than against an earlier rendering.
    """

    prefix: str
    suffix: str
    original_chars: int

    def render(
        self,
        limit_chars: int,
        *,
        mode: TruncationMode = "head_tail",
        head_fraction: float = 0.5,
        marker_factory: TruncationMarker = default_truncation_marker,
    ) -> TruncationResult:
        return truncate_edges(
            prefix=self.prefix,
            suffix=self.suffix,
            original_chars=self.original_chars,
            limit_chars=limit_chars,
            mode=mode,
            head_fraction=head_fraction,
            marker_factory=marker_factory,
        )

    def projected(
        self,
        limit_chars: int,
        *,
        mode: TruncationMode = "head_tail",
        head_fraction: float = 0.5,
        marker_factory: TruncationMarker = default_truncation_marker,
    ) -> str:
        """Render, keeping this source attached whenever the rendering is lossy."""

        result = self.render(
            limit_chars,
            mode=mode,
            head_fraction=head_fraction,
            marker_factory=marker_factory,
        )
        if not result.truncated:
            return result.text
        return ProjectedText(result.text, self)


class ProjectedText(str):
    """A rendered projection that still carries its rerenderable source.

    It reads as the plain rendering for every consumer that only wants text.
    :func:`truncate_text` renders from the attached source instead, so a second
    cut reports omission against the true size rather than the first cut.
    Concatenation with plain text extends the matching edge, so controller
    text added before or after a rendering never discards its source; the
    other operand contributes its rendering only.
    """

    source: TextProjectionSource

    def __new__(cls, text: str, source: TextProjectionSource) -> ProjectedText:
        projected = super().__new__(cls, text)
        projected.source = source
        return projected

    def __getnewargs__(self) -> tuple[str, TextProjectionSource]:  # type: ignore[override]
        # ``str`` reconstructs a subclass through ``cls(text)`` when copied or
        # pickled; that call would lack ``source``.  Copies keep it instead.
        return (str(self), self.source)

    def __add__(self, other: str, /) -> ProjectedText:
        source = self.source
        tail = str(other)
        return ProjectedText(
            str(self) + tail,
            TextProjectionSource(
                source.prefix, source.suffix + tail, source.original_chars + len(tail)
            ),
        )

    def __radd__(self, other: str, /) -> ProjectedText:
        source = self.source
        head = str(other)
        return ProjectedText(
            head + str(self),
            TextProjectionSource(
                head + source.prefix, source.suffix, source.original_chars + len(head)
            ),
        )


def source_chars(text: str) -> int:
    """The producer's size behind ``text``: its source's when it carries one,
    otherwise its own length."""

    if isinstance(text, ProjectedText):
        return text.source.original_chars
    return len(text)


def _split_retained(
    retained: int,
    *,
    prefix_chars: int,
    suffix_chars: int,
    mode: TruncationMode,
    head_fraction: float,
) -> tuple[int, int]:
    """Divide ``retained`` source characters between the two edges.

    An edge shorter than its share hands the leftover to the other edge, so
    asymmetric edges still fill the allowance as far as they reach.
    """

    if mode == "head":
        return min(retained, prefix_chars), 0
    if mode != "head_tail":  # pragma: no cover - type checkers constrain callers.
        raise ValueError(f"unknown truncation mode: {mode!r}")
    fraction = min(1.0, max(0.0, float(head_fraction)))
    head_floor = 1 if fraction > 0 else 0
    head_chars = min(retained, max(head_floor, int(retained * fraction + 0.5)))
    tail_chars = retained - head_chars
    if head_chars > prefix_chars:
        head_chars = prefix_chars
        tail_chars = min(retained - head_chars, suffix_chars)
    elif tail_chars > suffix_chars:
        tail_chars = suffix_chars
        head_chars = min(retained - tail_chars, prefix_chars)
    return head_chars, tail_chars


def truncate_edges(
    *,
    prefix: str,
    suffix: str,
    original_chars: int,
    limit_chars: int,
    mode: TruncationMode = "head_tail",
    head_fraction: float = 0.5,
    marker_factory: TruncationMarker = default_truncation_marker,
) -> TruncationResult:
    """Project a source from already-retained edges.

    The edges are used as far as they reach: a producer that retained fewer
    characters than the limit allows still gets an honest marker, and the
    omitted count always includes the marker's own cost.  Digit-width changes
    can alter the marker length (999 -> 1,000 omitted), so the marker and the
    split are iterated to a fixed point.  A custom marker whose length never
    settles still fits the limit; only its omitted count may lag one pass.
    This lower-level form lets streaming producers share the exact same
    rendering logic as :func:`truncate_text`.
    """

    limit = max(0, int(limit_chars))
    original = max(0, int(original_chars))
    edge_chars = len(prefix) + len(suffix)
    if original <= limit and original <= edge_chars:
        # The edges meet or overlap: the complete source is recoverable.
        if len(prefix) >= original:
            text = prefix[:original]
        else:
            text = prefix + suffix[edge_chars - original :]
        return TruncationResult(text, original, limit, 0)
    if limit == 0:
        return TruncationResult("", original, 0, original)

    def split(retained: int) -> tuple[int, int]:
        return _split_retained(
            retained,
            prefix_chars=len(prefix),
            suffix_chars=len(suffix),
            mode=mode,
            head_fraction=head_fraction,
        )

    omitted = original
    marker = marker_factory(omitted, original, limit)
    head_chars, tail_chars = split(max(0, limit - len(marker)))
    for _ in range(12):
        next_omitted = original - head_chars - tail_chars
        next_marker = marker_factory(next_omitted, original, limit)
        if next_omitted == omitted and next_marker == marker:
            break
        omitted, marker = next_omitted, next_marker
        head_chars, tail_chars = split(max(0, limit - len(marker)))
    else:
        marker = marker_factory(original - head_chars - tail_chars, original, limit)
        head_chars, tail_chars = split(max(0, limit - len(marker)))

    if len(marker) > limit or head_chars + tail_chars <= 0:
        # Fall back to the smallest honest marker instead of spending a
        # moderate-but-marker-too-small cap entirely on one ellipsis.  Prefix
        # and/or suffix plus ``…`` still signals incompleteness; at a true
        # one-character cap the marker necessarily consumes the whole budget.
        marker = "…"[:limit]
        head_chars, tail_chars = split(max(0, limit - len(marker)))
        if head_chars + tail_chars == 0:
            return TruncationResult(marker, original, limit, original, marker)

    head = prefix[:head_chars]
    tail = suffix[len(suffix) - tail_chars :] if tail_chars else ""
    return TruncationResult(
        head + marker + tail, original, limit, original - head_chars - tail_chars, marker
    )


def truncate_text(
    text: str,
    limit_chars: int,
    *,
    mode: TruncationMode = "head_tail",
    head_fraction: float = 0.5,
    marker_factory: TruncationMarker = default_truncation_marker,
    original_chars: int | None = None,
) -> TruncationResult:
    """Return an honestly marked projection no longer than ``limit_chars``.

    ``original_chars`` lets a caller that transformed an earlier lossy
    rendering (redaction, an inserted notice) keep reporting omission against
    the producer's true size once the edges can no longer be re-rendered.  It
    is ignored when ``text`` still carries its source.
    """

    if isinstance(text, ProjectedText):
        return text.source.render(
            limit_chars,
            mode=mode,
            head_fraction=head_fraction,
            marker_factory=marker_factory,
        )
    if original_chars is not None and original_chars > len(text):
        limit = max(0, int(limit_chars))
        if len(text) <= limit:
            return TruncationResult(text, original_chars, limit, original_chars - len(text))
        return truncate_edges(
            prefix=text,
            suffix=text,
            original_chars=original_chars,
            limit_chars=limit,
            mode=mode,
            head_fraction=head_fraction,
            marker_factory=marker_factory,
        )
    return truncate_edges(
        prefix=text,
        suffix=text,
        original_chars=len(text),
        limit_chars=limit_chars,
        mode=mode,
        head_fraction=head_fraction,
        marker_factory=marker_factory,
    )


def join_projection_sources(
    first: TextProjectionSource,
    second: TextProjectionSource,
    *,
    separator: str = "",
    retention_chars: int,
) -> TextProjectionSource:
    """Edges of ``first + separator + second`` built from the parts' edges.

    Each part must retain at least ``min(its size, retention_chars)``
    characters at both edges, which is what a :class:`BoundedTextBuffer` of
    that retention guarantees.  The joined source retains ``retention_chars``
    at each edge and reports the concatenation's true size, so two bounded
    captures can be presented as one document without either being rendered
    first.
    """

    retention = max(0, int(retention_chars))
    total = first.original_chars + len(separator) + second.original_chars
    if first.original_chars >= retention:
        prefix = first.prefix[:retention]
    else:
        prefix = (first.prefix + separator + second.prefix)[:retention]
    if second.original_chars >= retention:
        suffix = second.suffix[-retention:] if retention else ""
    else:
        joined_tail = first.suffix + separator + second.suffix
        suffix = joined_tail[-retention:] if retention else ""
    return TextProjectionSource(prefix, suffix, total)


class BoundedTextBuffer:
    """Thread-safe streaming text accumulator retaining bounded source edges.

    The oldest edge is written once and frozen when it fills.  The newest edge
    accumulates in one buffer that is compacted to its last retention whenever
    it holds twice that, so the buffer keeps at most about three retentions of
    text however much the producer writes (about four while a compaction
    copies), and tiny producer fragments coalesce inside it, so bounded
    character retention also means bounded object overhead.
    """

    def __init__(self, retention_chars: int) -> None:
        self._retention = max(0, int(retention_chars))
        self._prefix: io.StringIO | None = io.StringIO()
        self._prefix_text = ""
        self._tail = io.StringIO()
        self._total_chars = 0
        self._lock = threading.Lock()

    def append(self, text: str) -> None:
        if not text:
            return
        with self._lock:
            self._total_chars += len(text)
            retention = self._retention
            start = 0
            if self._prefix is not None:
                room = retention - self._prefix.tell()
                if len(text) < room:
                    # The prefix still holds the complete output with room to
                    # spare: an output that never exceeds the retention is
                    # stored exactly once.
                    self._prefix.write(text)
                    return
                # The prefix just filled.  From here on the tail tracks the
                # newest edge, and it starts as the prefix itself.
                self._prefix.write(text[:room])
                self._prefix_text = self._prefix.getvalue()
                self._prefix = None
                self._tail.write(self._prefix_text)
                start = room
            if len(text) - start >= retention:
                # The rest of this chunk alone is the whole newest edge: keep
                # its last retention without ever copying the rest of the
                # chunk, so a chunk far larger than the retention costs no
                # transient copy of itself.
                self._tail = io.StringIO()
                self._tail.write(text[len(text) - retention :])
                return
            self._tail.write(text[start:])
            if self._tail.tell() >= 2 * retention:
                kept = self._tail.getvalue()
                self._tail = io.StringIO()
                self._tail.write(kept[len(kept) - retention :])

    def source(self) -> TextProjectionSource:
        """Snapshot the retained edges and the true size behind them."""

        with self._lock:
            total = self._total_chars
            if self._prefix is not None:
                # Nothing was ever dropped: the prefix is the whole output and
                # serves as both edges without a second copy.
                prefix = self._prefix.getvalue()
                return TextProjectionSource(prefix, prefix, total)
            tail = self._tail.getvalue()
            return TextProjectionSource(
                self._prefix_text, tail[len(tail) - self._retention :], total
            )

    def render(
        self,
        limit_chars: int | None = None,
        *,
        mode: TruncationMode = "head_tail",
        head_fraction: float = 0.5,
        marker_factory: TruncationMarker = default_truncation_marker,
    ) -> TruncationResult:
        """Render the retained edges; the default limit is the retention."""

        return self.source().render(
            self._retention if limit_chars is None else limit_chars,
            mode=mode,
            head_fraction=head_fraction,
            marker_factory=marker_factory,
        )

    def projected(
        self,
        limit_chars: int | None = None,
        *,
        mode: TruncationMode = "head_tail",
        head_fraction: float = 0.5,
        marker_factory: TruncationMarker = default_truncation_marker,
    ) -> str:
        """Render, keeping the source attached whenever the rendering is lossy."""

        return self.source().projected(
            self._retention if limit_chars is None else limit_chars,
            mode=mode,
            head_fraction=head_fraction,
            marker_factory=marker_factory,
        )
