"""Shared exact-cap truncation and bounded-capture regressions."""

from __future__ import annotations

from typing import Literal

import pytest

from turnstone.core.truncation import (
    BoundedTextBuffer,
    ProjectedText,
    TextProjectionSource,
    join_projection_sources,
    truncate_text,
)


@pytest.mark.parametrize("limit", range(0, 96))
@pytest.mark.parametrize("mode", ["head", "head_tail"])
def test_truncate_text_never_exceeds_exact_cap(
    limit: int,
    mode: Literal["head", "head_tail"],
) -> None:
    source = "A" * 137 + "Z" * 137
    result = truncate_text(source, limit, mode=mode)

    assert len(result.text) <= limit
    assert result.original_chars == len(source)
    assert result.retained_chars + result.omitted_chars == len(source)
    assert result.truncated is True


def test_one_character_limit_cannot_fail_open_via_negative_zero_slice() -> None:
    result = truncate_text("secret output", 1)

    assert result.text == "…"
    assert result.retained_chars == 0
    assert result.omitted_chars == len("secret output")


def test_small_cap_preserves_source_around_compact_marker() -> None:
    result = truncate_text("abcdefghij", 4)

    assert result.text == "ab…j"
    assert result.retained_chars == 3
    assert result.omitted_chars == 7


def test_marker_omitted_count_matches_retained_source() -> None:
    result = truncate_text("A" * 500 + "Z" * 500, 120)

    assert len(result.text) <= 120
    assert f"[{result.omitted_chars} chars truncated" in result.text
    assert result.text.startswith("A")
    assert result.text.endswith("Z")


def test_bounded_buffer_matches_in_memory_projection() -> None:
    source = "".join(f"line-{i:05d}\n" for i in range(50_000))
    buffer = BoundedTextBuffer(4096)
    for start in range(0, len(source), 997):
        buffer.append(source[start : start + 997])

    buffered = buffer.render(2048)
    direct = truncate_text(source, 2048)

    assert buffered == direct
    assert buffer.source().original_chars == len(source)


def test_bounded_buffer_renders_a_larger_projection_from_its_edges() -> None:
    buffer = BoundedTextBuffer(10)
    buffer.append("x" * 100)

    result = buffer.render(30)

    assert result.text == "x" * 10 + "…" + "x" * 10
    assert result.limit_chars == 30
    assert result.omitted_chars == 80


def test_bounded_buffer_coalesces_many_tiny_fragments() -> None:
    source = "x\n" * 100_000
    buffer = BoundedTextBuffer(8192)
    for char in source:
        buffer.append(char)

    assert buffer.render(4096) == truncate_text(source, 4096)
    # Object overhead is bounded by compaction rather than proportional to
    # the producer's fragment count.
    assert buffer._tail.tell() < 2 * 8192  # noqa: SLF001 - white-box memory regression


def test_projection_survives_concatenation() -> None:
    buf = BoundedTextBuffer(20)
    buf.append("a" * 30 + "b" * 30)
    projected = buf.projected()
    assert isinstance(projected, ProjectedText)

    wrapped = "[pre]" + projected + "[post]"

    assert isinstance(wrapped, ProjectedText)
    assert wrapped.startswith("[pre]") and wrapped.endswith("[post]")
    assert wrapped.source.original_chars == 60 + len("[pre]") + len("[post]")
    result = truncate_text(wrapped, 30)
    assert result.original_chars == wrapped.source.original_chars
    assert result.retained_chars + result.omitted_chars == result.original_chars
    assert result.text.startswith("[pre]") and result.text.endswith("[post]")


def test_asymmetric_edges_never_overdraw_one_side() -> None:
    buf = BoundedTextBuffer(300)
    buf.append("x" * 11_000)
    wrapped = buf.projected() + "!" * 110

    result = truncate_text(wrapped, 3_200)

    assert len(result.text) <= 3_200
    assert result.text.endswith("!" * 110)
    assert result.original_chars == 11_110


def test_known_original_size_survives_a_transformed_rendering() -> None:
    cut = truncate_text("x" * 400, 200, original_chars=5_000)
    assert len(cut.text) <= 200
    assert cut.original_chars == 5_000
    assert cut.retained_chars + cut.omitted_chars == 5_000
    assert f"[{cut.omitted_chars} chars truncated" in cut.text

    kept = truncate_text("x" * 30, 40, original_chars=5_000)
    assert kept.text == "x" * 30
    assert kept.original_chars == 5_000


def test_bounded_buffer_keeps_a_single_copy_until_the_prefix_fills() -> None:
    """An output that never exceeds the retention is held once: the prefix is
    the whole output and serves as both edges, and a render at any limit still
    matches the in-memory projection."""
    buffer = BoundedTextBuffer(100)
    for start in range(0, 60, 7):
        buffer.append(("abcdefghij" * 6)[start : start + 7])
    source = buffer.source()

    assert source.prefix is source.suffix
    assert source.original_chars == 60
    assert buffer.render(100).text == "abcdefghij" * 6
    assert buffer.render(30) == truncate_text("abcdefghij" * 6, 30)


@pytest.mark.parametrize("piece", [1, 5, 20, 100, 101, 250])
def test_bounded_buffer_edges_survive_an_exact_prefix_fill(piece: int) -> None:
    """The suffix edge starts tracking the moment the prefix fills, including
    when one append fills it exactly; the next append must not become the
    whole suffix."""
    text = "".join(chr(97 + i % 26) for i in range(230))
    buffer = BoundedTextBuffer(100)
    for start in range(0, len(text), piece):
        buffer.append(text[start : start + piece])
    source = buffer.source()

    assert source.prefix == text[:100]
    assert source.suffix == text[-100:]
    assert source.original_chars == 230
    assert buffer.render(80) == TextProjectionSource(text[:100], text[-100:], 230).render(80)


@pytest.mark.parametrize(
    ("first_len", "second_len", "separator"),
    [(0, 50, "\n"), (50, 0, "\n"), (3, 120, "\n"), (120, 3, ""), (700, 700, "|"), (40, 40, "\n")],
)
def test_joined_sources_render_like_the_concatenated_document(
    first_len: int, second_len: int, separator: str
) -> None:
    """Two bounded captures joined as edges render exactly as one in-memory
    capture of ``first + separator + second`` would, at every limit."""
    first = "".join(chr(97 + i % 26) for i in range(first_len))
    second = "".join(chr(65 + i % 26) for i in range(second_len))
    retention = 100
    first_buffer, second_buffer = BoundedTextBuffer(retention), BoundedTextBuffer(retention)
    first_buffer.append(first)
    second_buffer.append(second)
    document = first + separator + second
    reference = TextProjectionSource(document[:retention], document[-retention:], len(document))

    joined = join_projection_sources(
        first_buffer.source(),
        second_buffer.source(),
        separator=separator,
        retention_chars=retention,
    )

    assert joined.original_chars == len(document)
    for limit in (0, 1, 50, 100, 200, len(document) + 5):
        assert joined.render(limit) == reference.render(limit)


def test_projected_text_copies_keep_their_source() -> None:
    import copy
    import pickle

    projected = ProjectedText("abc…xyz", TextProjectionSource("abc", "xyz", 100))

    # pickle round-trips an object this test built itself; no untrusted bytes.
    clones = (copy.copy(projected), copy.deepcopy(projected), pickle.loads(pickle.dumps(projected)))
    for clone in clones:
        assert type(clone) is ProjectedText
        assert clone == projected
        assert clone.source == projected.source


def test_result_marker_is_the_embedded_cut() -> None:
    result = truncate_text("A" * 500 + "Z" * 500, 120)

    head, marker, tail = result.text.partition(result.marker)
    assert marker == result.marker
    assert head == "A" * len(head) and tail == "Z" * len(tail)
    assert len(head) + len(tail) == result.retained_chars
    assert truncate_text("short", 120).marker == ""


def test_short_edges_hand_their_leftover_to_the_other_edge() -> None:
    result = TextProjectionSource("P" * 380, "S" * 580, 5000).render(1000)

    assert len(result.text) == 1000
    assert result.text.count("P") == 380
    assert result.limit_chars == 1000
    assert result.retained_chars + result.omitted_chars == 5000


def test_edges_that_cannot_cover_a_small_source_still_mark_omission() -> None:
    result = TextProjectionSource("P" * 7, "S" * 9, 63).render(200)

    assert result.truncated
    assert result.marker and result.marker in result.text
    assert result.retained_chars == 16 and result.omitted_chars == 47


def test_tail_only_projection_with_an_empty_prefix_renders() -> None:
    result = TextProjectionSource("", "Y" * 6, 10).render(5, head_fraction=0.0)

    assert len(result.text) <= 5
    assert result.text.endswith("Y")
    assert result.truncated


@pytest.mark.parametrize("seed", range(4))
def test_bounded_buffer_matches_a_reference_over_random_feeds(seed: int) -> None:
    import random

    rng = random.Random(seed)
    for _ in range(150):
        retention = rng.randint(0, 40)
        buffer = BoundedTextBuffer(retention)
        fed = ""
        for _ in range(rng.randint(0, 12)):
            size = rng.choice([0, 1, 3, 11, 41, 130])
            chunk = "".join(rng.choice("abcdefgh") for _ in range(size))
            buffer.append(chunk)
            fed += chunk
            source = buffer.source()
            assert source.original_chars == len(fed)
            if len(fed) <= retention:
                assert source.prefix == fed and source.suffix == fed
            else:
                assert source.prefix == fed[:retention]
                assert source.suffix == (fed[-retention:] if retention else "")


def test_default_marker_says_retained_when_the_limit_was_not_exceeded() -> None:
    """A limit the output would have fit under was not exceeded: the cut came
    from a producer that retained less than the whole, and the marker says so
    instead of blaming the limit."""
    result = TextProjectionSource("a" * 500, "b" * 500, 1515).render(2048)

    assert result.truncated
    assert "exceeded" not in result.text
    assert (
        f"[{result.omitted_chars} chars truncated — {result.retained_chars} of 1515 chars retained]"
    ) in result.text
    assert truncate_text("x" * 3000, 1000).text.count("exceeded 1000 char limit") == 1


def test_oversized_chunk_becomes_the_newest_edge_without_a_full_copy() -> None:
    """A chunk larger than the retention is kept by its last retention alone,
    so buffering never holds a copy proportional to the producer's chunk."""
    buffer = BoundedTextBuffer(100)
    buffer.append("p" * 10)
    buffer.append("x" * 1_000_000)

    assert buffer._tail.tell() <= 100  # noqa: SLF001 - white-box memory regression
    source = buffer.source()
    assert source.prefix == "p" * 10 + "x" * 90
    assert source.suffix == "x" * 100
    assert source.original_chars == 1_000_010


def test_oversized_chunk_never_costs_a_transient_copy_of_itself() -> None:
    """Whether the prefix is still filling or already frozen, appending a
    chunk far larger than the retention allocates on the order of the
    retention, never of the chunk."""
    import tracemalloc

    chunk = "x" * 8_000_000
    buffer = BoundedTextBuffer(100)
    buffer.append("p" * 10)
    tracemalloc.start()
    try:
        buffer.append(chunk)
        buffer.append(chunk)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert peak < 100_000
    source = buffer.source()
    assert source.prefix == "p" * 10 + "x" * 90
    assert source.suffix == "x" * 100
    assert source.original_chars == 16_000_010
