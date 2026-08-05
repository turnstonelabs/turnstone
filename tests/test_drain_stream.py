"""Unit tests for ``drain_stream`` — the #831 single non-streaming transport.

Every single-shot lane consumes ``create_streaming`` through this
accumulator, so its semantics ARE the old ``create_completion`` contract:
each case here pins a rule the per-adapter non-streaming methods used to
implement independently (usage max-merge, tool-delta assembly, terminal
provider_blocks, trailing-citation fold).
"""

from __future__ import annotations

import pytest

from turnstone.core.providers import (
    StreamChunk,
    ToolCallDelta,
    UsageInfo,
    drain_stream,
    transport_guarded,
)
from turnstone.core.providers._openai_common import RETRYABLE_ERROR_NAMES
from turnstone.core.providers._protocol import IncompleteStreamError


class TestContentAndReasoning:
    def test_joins_content_deltas_in_order(self):
        result = drain_stream(
            iter(
                [
                    StreamChunk(content_delta="Hello, "),
                    StreamChunk(content_delta="world"),
                    StreamChunk(finish_reason="stop"),
                ]
            )
        )
        assert result.content == "Hello, world"
        assert result.finish_reason == "stop"

    def test_joins_reasoning_deltas_separately_from_content(self):
        result = drain_stream(
            iter(
                [
                    StreamChunk(reasoning_delta="think "),
                    StreamChunk(reasoning_delta="hard"),
                    StreamChunk(content_delta="answer"),
                    StreamChunk(finish_reason="stop"),
                ]
            )
        )
        assert result.reasoning == "think hard"
        assert result.content == "answer"

    def test_inline_tags_segregated_at_the_seam(self):
        # The one-shot splitter runs on the joined content, so EVERY
        # drained consumer receives IR-clean content by construction —
        # the tag arrives split across deltas exactly as a passthrough
        # server streams it.
        result = drain_stream(
            iter(
                [
                    StreamChunk(content_delta="<thi"),
                    StreamChunk(content_delta="nk>plan</think>"),
                    StreamChunk(content_delta="answer"),
                    StreamChunk(finish_reason="stop"),
                ]
            )
        )
        assert result.content == "answer"
        assert result.reasoning == "plan"

    def test_extracted_reasoning_appends_after_server_parsed(self):
        # Segregate, never discard: server-parsed reasoning_delta first,
        # inline-extracted after, with a boundary — two distinct passes
        # must never read as one run-together sentence.
        result = drain_stream(
            iter(
                [
                    StreamChunk(reasoning_delta="parsed."),
                    StreamChunk(content_delta="<think>inline.</think>answer"),
                    StreamChunk(finish_reason="stop"),
                ]
            )
        )
        assert result.reasoning == "parsed.\n\ninline."
        assert result.content == "answer"

    def test_extracted_reasoning_alone_carries_no_separator(self):
        result = drain_stream(
            iter(
                [
                    StreamChunk(content_delta="<think>inline.</think>answer"),
                    StreamChunk(finish_reason="stop"),
                ]
            )
        )
        assert result.reasoning == "inline."

    def test_all_reasoning_turn_drops_citations_footer(self):
        # An all-reasoning turn's footer is sourcing for an answer that
        # does not exist; folding it would hand downstream emptiness
        # checks a truthy, footer-only "answer".
        result = drain_stream(
            iter(
                [
                    StreamChunk(content_delta="<think>all reasoning</think>"),
                    StreamChunk(finish_reason="stop"),
                    StreamChunk(info_delta="Sources:\n- x"),
                ]
            )
        )
        assert result.content == ""
        assert result.reasoning == "all reasoning"

    def test_double_reasoning_shape_logs_chars_only(self, caplog):
        # Inline-extracted text alongside a NATIVE reasoning block has no
        # native lane to land in downstream — observable here, chars-only.
        import logging

        secret = "the plan nobody logs"
        with caplog.at_level(logging.DEBUG):
            result = drain_stream(
                iter(
                    [
                        StreamChunk(content_delta=f"<think>{secret}</think>answer"),
                        StreamChunk(
                            finish_reason="stop",
                            provider_blocks=[{"type": "thinking", "thinking": "native"}],
                        ),
                    ]
                )
            )
        assert result.content == "answer"
        assert "drain.inline_reasoning_alongside_native" in caplog.text
        assert secret not in caplog.text

    def test_routine_reasoning_delta_mirror_does_not_log(self, caplog):
        # reasoning_delta beside a native block is the NORMAL shape on
        # Anthropic/Responses lanes (the delta mirrors the block) — it
        # must not drown the anomaly signal.
        import logging

        with caplog.at_level(logging.DEBUG):
            drain_stream(
                iter(
                    [
                        StreamChunk(reasoning_delta="mirrored"),
                        StreamChunk(content_delta="answer"),
                        StreamChunk(
                            finish_reason="stop",
                            provider_blocks=[{"type": "thinking", "thinking": "mirrored"}],
                        ),
                    ]
                )
            )
        assert "drain.inline_reasoning_alongside_native" not in caplog.text

    def test_trailing_footer_folds_after_split_and_is_never_scanned(self):
        # The citations footer is web-controlled text: it folds onto the
        # ALREADY-split content, so a tag-shaped citation title cannot
        # reclassify the result.
        result = drain_stream(
            iter(
                [
                    StreamChunk(content_delta="<think>x</think>answer"),
                    StreamChunk(finish_reason="stop"),
                    StreamChunk(info_delta="Sources:\n- how </think> works"),
                ]
            )
        )
        assert result.content == "answer\n\nSources:\n- how </think> works"
        assert result.reasoning == "x"

    def test_tool_call_turn_with_in_think_tail(self):
        # A drained tool-call turn whose trailing content is an
        # unterminated think block: the tail is reasoning, the calls ride.
        result = drain_stream(
            iter(
                [
                    StreamChunk(content_delta="<think>pondering tools"),
                    StreamChunk(
                        tool_call_deltas=[ToolCallDelta(index=0, id="c1", name="bash")],
                        finish_reason="tool_calls",
                    ),
                ]
            )
        )
        assert result.content == ""
        assert result.reasoning == "pondering tools"
        assert result.tool_calls is not None and result.tool_calls[0]["id"] == "c1"

    def test_stream_without_finish_reason_raises_incomplete(self):
        # Complete-or-error: every adapter emits a finish reason on a
        # healthy stream, so its absence means the generation died
        # mid-response — partial text must never be stored as a complete
        # result (compaction summary, title).  Typed and retryable.
        assert "IncompleteStreamError" in RETRYABLE_ERROR_NAMES
        with pytest.raises(IncompleteStreamError):
            drain_stream(iter([StreamChunk(content_delta="half a summar")]))

    def test_empty_stream_raises_incomplete(self):
        with pytest.raises(IncompleteStreamError):
            drain_stream(iter([]))


class TestToolCallAssembly:
    def test_merges_deltas_by_index_id_name_once_args_concat(self):
        result = drain_stream(
            iter(
                [
                    StreamChunk(
                        tool_call_deltas=[ToolCallDelta(index=0, id="call_1", name="read_file")]
                    ),
                    StreamChunk(
                        tool_call_deltas=[ToolCallDelta(index=0, arguments_delta='{"path": ')]
                    ),
                    StreamChunk(
                        tool_call_deltas=[ToolCallDelta(index=0, arguments_delta='"x.py"}')]
                    ),
                    StreamChunk(finish_reason="tool_calls"),
                ]
            )
        )
        assert result.tool_calls == [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "read_file", "arguments": '{"path": "x.py"}'},
            }
        ]

    def test_parallel_calls_ordered_by_index(self):
        # Interleaved argument deltas for two calls must not cross-contaminate,
        # and the assembled list is index-ordered regardless of arrival order.
        result = drain_stream(
            iter(
                [
                    StreamChunk(tool_call_deltas=[ToolCallDelta(index=1, id="b", name="beta")]),
                    StreamChunk(tool_call_deltas=[ToolCallDelta(index=0, id="a", name="alpha")]),
                    StreamChunk(
                        tool_call_deltas=[
                            ToolCallDelta(index=0, arguments_delta="{}"),
                            ToolCallDelta(index=1, arguments_delta='{"k": 1}'),
                        ]
                    ),
                    StreamChunk(finish_reason="tool_calls"),
                ]
            )
        )
        assert [tc["id"] for tc in result.tool_calls] == ["a", "b"]
        assert result.tool_calls[1]["function"]["arguments"] == '{"k": 1}'

    def test_blank_id_preserved_for_downstream_repair(self):
        # Google compat can stream blank tool ids — the drain must hand them
        # through untouched so model_turn's pairwise blank-id repair sees them.
        result = drain_stream(
            iter(
                [
                    StreamChunk(tool_call_deltas=[ToolCallDelta(index=0, name="f")]),
                    StreamChunk(finish_reason="tool_calls"),
                ]
            )
        )
        assert result.tool_calls[0]["id"] == ""

    # Index-degenerate parallel-call de-fusion lives in the CHAT ADAPTER's
    # iterator (so the interactive loop is fixed too) — pinned in
    # test_providers.py::TestOpenAIProvider::
    # test_streaming_remaps_index_degenerate_parallel_calls.  The drain
    # accumulates by index verbatim; adapters own index sanity.


class TestUsageMerge:
    def test_anthropic_split_emission_max_merges(self):
        # message_start carries prompt tokens (completion 0); message_delta
        # carries completion tokens (prompt possibly absent → 0).  Neither
        # first-wins nor last-wins sees both — the max-merge does.
        result = drain_stream(
            iter(
                [
                    StreamChunk(
                        usage=UsageInfo(
                            prompt_tokens=120,
                            completion_tokens=0,
                            total_tokens=120,
                            cache_read_tokens=100,
                        )
                    ),
                    StreamChunk(content_delta="hi"),
                    StreamChunk(
                        usage=UsageInfo(prompt_tokens=0, completion_tokens=42, total_tokens=42),
                        finish_reason="stop",
                    ),
                ]
            )
        )
        assert result.usage.prompt_tokens == 120
        assert result.usage.completion_tokens == 42
        assert result.usage.total_tokens == 162
        assert result.usage.cache_read_tokens == 100

    def test_single_terminal_usage_passes_through(self):
        result = drain_stream(
            iter(
                [
                    StreamChunk(content_delta="x"),
                    StreamChunk(finish_reason="stop"),
                    StreamChunk(
                        usage=UsageInfo(prompt_tokens=10, completion_tokens=5, total_tokens=15)
                    ),
                ]
            )
        )
        assert result.usage.total_tokens == 15


class TestFinishAndBlocks:
    def test_finish_reason_last_non_none_wins(self):
        result = drain_stream(
            iter(
                [
                    StreamChunk(finish_reason="tool_calls"),
                    StreamChunk(content_delta="tail"),
                    StreamChunk(finish_reason="stop"),
                ]
            )
        )
        assert result.finish_reason == "stop"

    def test_provider_blocks_taken_from_terminal_emission(self):
        # Every adapter attaches its full block list exactly once (on or
        # after the terminal chunk); replace-on-nonempty keeps the last set.
        blocks = [{"type": "thinking", "thinking": "t", "signature": "s"}]
        result = drain_stream(
            iter(
                [
                    StreamChunk(content_delta="a"),
                    StreamChunk(finish_reason="stop", provider_blocks=blocks),
                ]
            )
        )
        assert result.provider_blocks == blocks


class TestInfoDelta:
    def test_mid_stream_status_pings_dropped(self):
        # "[Searching…]" style transient status — the non-streaming lane
        # never surfaced these, so the drain must not leak them into content.
        result = drain_stream(
            iter(
                [
                    StreamChunk(info_delta="[Searching: quakes]"),
                    StreamChunk(content_delta="answer"),
                    StreamChunk(finish_reason="stop"),
                ]
            )
        )
        assert result.content == "answer"

    def test_trailing_citations_fold_matches_format_citations(self):
        # The chat/responses adapters emit format_citations("", anns).strip()
        # as a final info chunk after the finish reason.  Folding it back as
        # content + "\n\n" + info must byte-match the old non-streaming
        # format_citations(content, anns) append.
        from turnstone.core.providers._openai_common import format_citations

        class _Ann:
            type = "url_citation"
            url = "https://example.com"
            title = "Example"
            url_citation = None

        anns = [_Ann()]
        trailing = format_citations("", anns).strip()
        result = drain_stream(
            iter(
                [
                    StreamChunk(content_delta="body"),
                    StreamChunk(finish_reason="stop"),
                    StreamChunk(info_delta=trailing),
                ]
            )
        )
        assert result.content == format_citations("body", anns)

    def test_trailing_fold_onto_empty_content_is_dropped(self):
        # A turn with NO content (tool-call-only here, all-reasoning in the
        # split case) keeps content empty: a footer-only "answer" would be
        # truthy and defeat every downstream emptiness check.  This pin
        # replaced the old byte-match-the-retired-non-streaming-lane rule
        # (which appended the footer onto the empty base).
        result = drain_stream(
            iter(
                [
                    StreamChunk(finish_reason="tool_calls"),
                    StreamChunk(info_delta="Sources:\n- x"),
                ]
            )
        )
        assert result.content == ""

    def test_finishless_stream_raises_even_with_trailing_info(self):
        # A stream that dies after a status ping must NOT return the ping
        # as content (nor the partial body as a clean result) — the
        # complete-or-error gate turns the whole stream into a retryable
        # error instead of guessing which trailing info was a citation.
        with pytest.raises(IncompleteStreamError):
            drain_stream(
                iter(
                    [
                        StreamChunk(content_delta="body"),
                        StreamChunk(info_delta="[Searching: kubernetes CVEs]"),
                    ]
                )
            )


class TestTransportGuarded:
    """``transport_guarded`` — drain's conversion rule for consumers that
    keep streaming semantics (the interactive loop)."""

    @pytest.mark.parametrize("exc_name", ["ReadError", "RemoteProtocolError"])
    def test_pre_finish_transport_error_becomes_retryable_incomplete(self, exc_name):
        import httpx

        exc_cls = getattr(httpx, exc_name)

        def chunks():
            yield StreamChunk(content_delta="partial")
            raise exc_cls("wire died")

        it = transport_guarded(chunks())
        assert next(it).content_delta == "partial"
        with pytest.raises(IncompleteStreamError, match=exc_name) as excinfo:
            next(it)
        assert isinstance(excinfo.value.__cause__, exc_cls)

    def test_message_byte_matches_drains_shape(self):
        # The wrapper and the drain are the SAME conversion rule; any test
        # or log filter pinned to drain's message must match the wrapper's.
        import httpx

        def chunks():
            yield StreamChunk(content_delta="partial")
            raise httpx.ReadError("[SSL] record layer failure (_ssl.c:2590)")

        with pytest.raises(IncompleteStreamError) as guarded:
            list(transport_guarded(chunks()))
        with pytest.raises(IncompleteStreamError) as drained:
            drain_stream(chunks())
        assert str(guarded.value) == str(drained.value)

    def test_post_finish_blip_ends_stream_cleanly(self, caplog):
        # The generation already completed — the blip only cost trailing
        # metadata, so the stream ends instead of raising.
        import logging

        import httpx

        def chunks():
            yield StreamChunk(content_delta="done")
            yield StreamChunk(finish_reason="stop")
            raise httpx.ReadError("late blip")

        with caplog.at_level(logging.WARNING, logger="turnstone.core.providers._protocol"):
            out = list(transport_guarded(chunks()))
        assert [c.content_delta for c in out] == ["done", ""]
        assert out[-1].finish_reason == "stop"
        blips = [r.message for r in caplog.records if "stream.post_finish_blip" in r.message]
        assert blips
        # usage_captured is the missing-spend attribution signal: this
        # stream never delivered a usage chunk, so the blip must say so.
        assert any("usage_captured" in m and "False" in m for m in blips)

    def test_chunks_pass_through_untouched(self):
        src = [
            StreamChunk(content_delta="a"),
            StreamChunk(reasoning_delta="r"),
            StreamChunk(finish_reason="stop"),
        ]
        out = list(transport_guarded(iter(src)))
        assert all(a is b for a, b in zip(out, src, strict=True))

    def test_exhaustion_without_finish_reason_passes_through(self):
        # No complete-or-error gate here — that stays drain-only; the
        # interactive consumer shows partial output live.
        out = list(transport_guarded(iter([StreamChunk(content_delta="x")])))
        assert len(out) == 1

    def test_non_transport_exception_propagates_verbatim(self):
        def chunks():
            yield StreamChunk(content_delta="x")
            raise ValueError("upstream broke")

        it = transport_guarded(chunks())
        assert next(it).content_delta == "x"
        with pytest.raises(ValueError, match="upstream broke"):
            next(it)


class TestErrorPropagation:
    def test_post_finish_blip_keeps_completed_result(self):
        # The generation completed (finish reason in hand) — a trailing
        # transport blip forfeits only trailing metadata (here: the usage
        # chunk), never the completed result.
        import httpx

        def chunks():
            yield StreamChunk(content_delta="whole answer")
            yield StreamChunk(finish_reason="stop")
            raise httpx.ReadError("late blip")

        result = drain_stream(chunks())
        assert result.content == "whole answer"
        assert result.finish_reason == "stop"
        assert result.usage is None

    def test_httpx_transport_error_becomes_retryable_incomplete(self):
        # Streaming moves the body read out of the SDK's wrapped request:
        # a mid-body wire failure surfaces as a raw httpx.TransportError
        # no retry predicate recognizes.  The drain re-raises it (chained,
        # message preserved) as the retryable IncompleteStreamError.
        import httpx

        def chunks():
            yield StreamChunk(content_delta="partial")
            raise httpx.RemoteProtocolError("peer closed connection")

        with pytest.raises(IncompleteStreamError, match="RemoteProtocolError") as excinfo:
            drain_stream(chunks())
        assert isinstance(excinfo.value.__cause__, httpx.RemoteProtocolError)

    def test_mid_stream_exception_propagates_verbatim(self):
        # Retry/deadline/fallback policy is the caller's — the drain adds
        # no exception translation, exactly like the old transport.
        def chunks():
            yield StreamChunk(content_delta="partial")
            raise RuntimeError("upstream broke")

        with pytest.raises(RuntimeError, match="upstream broke"):
            drain_stream(chunks())


def test_whitespace_only_content_does_not_gain_footer():
    # Blankness, not truthiness: tag-free whitespace-only content is
    # byte-identical at the split (never normalized), and a citations
    # footer folded onto it would read as a truthy footer-only "answer".
    result = drain_stream(
        iter(
            [
                StreamChunk(content_delta="\n\n"),
                StreamChunk(reasoning_delta="all the substance"),
                StreamChunk(finish_reason="stop"),
                StreamChunk(info_delta="Sources:\n- x"),
            ]
        )
    )
    assert result.content == "\n\n"
    assert result.reasoning == "all the substance"


def test_unterminated_think_before_tool_calls_does_not_swallow_answer():
    # Content runs are bounded by interleaving signals, mirroring the
    # interactive consumer's flush-and-reset when tool calls begin: an
    # unterminated <think> before the calls is reasoning, the post-call
    # answer is CONTENT — never swallowed into the open block.
    result = drain_stream(
        iter(
            [
                StreamChunk(content_delta="<think>plan"),
                StreamChunk(tool_call_deltas=[ToolCallDelta(index=0, id="c1", name="bash")]),
                StreamChunk(content_delta="Answer: 42"),
                StreamChunk(finish_reason="stop"),
            ]
        )
    )
    assert result.content == "Answer: 42"
    assert result.reasoning == "plan"
    assert result.tool_calls is not None and result.tool_calls[0]["id"] == "c1"


def test_unterminated_think_before_reasoning_delta_does_not_swallow_answer():
    # The mixed-dialect shape: provider-parsed reasoning interleaving a raw
    # content stream also closes the run (the interactive Path-1 reset).
    result = drain_stream(
        iter(
            [
                StreamChunk(content_delta="<think>plan"),
                StreamChunk(reasoning_delta="parsed."),
                StreamChunk(content_delta="answer"),
                StreamChunk(finish_reason="stop"),
            ]
        )
    )
    assert result.content == "answer"
    assert result.reasoning == "parsed.\n\nplan"


def test_whitespace_only_extraction_does_not_pollute_reasoning():
    # The field-common no-think shape: an empty think body must not append
    # blank text (or a separator) into persisted reasoning.
    result = drain_stream(
        iter(
            [
                StreamChunk(reasoning_delta="parsed."),
                StreamChunk(content_delta="<think>\n\n</think>answer"),
                StreamChunk(finish_reason="stop"),
            ]
        )
    )
    assert result.reasoning == "parsed."
    assert result.content == "answer"


def test_combined_content_and_tool_chunk_keeps_content_in_prior_run():
    # Within a chunk the interactive ordering holds — reasoning, content,
    # THEN the tool-call close: a combined content+tools chunk feeds its
    # content into the pre-boundary run, so a close tag arriving in that
    # chunk still closes the open block instead of stranding as a literal
    # orphan in drained content.
    result = drain_stream(
        iter(
            [
                StreamChunk(content_delta="Let me plan. <think>use bash"),
                StreamChunk(
                    content_delta="</think>Running it.",
                    tool_call_deltas=[ToolCallDelta(index=0, id="c1", name="bash")],
                ),
                StreamChunk(finish_reason="tool_calls"),
            ]
        )
    )
    assert result.content == "Let me plan. Running it."
    assert result.reasoning == "use bash"
    assert result.tool_calls is not None and result.tool_calls[0]["id"] == "c1"


def test_inter_run_paragraph_separator_survives_reasoning_boundary():
    # The edge trim runs ONCE over the joined whole: a genuine paragraph
    # break the model emitted before an interleaving signal is interior
    # after the join and must survive.
    result = drain_stream(
        iter(
            [
                StreamChunk(content_delta="<think>plan</think>First part.\n\n"),
                StreamChunk(reasoning_delta="server-parsed"),
                StreamChunk(content_delta="Second part."),
                StreamChunk(finish_reason="stop"),
            ]
        )
    )
    assert result.content == "First part.\n\nSecond part."
    assert result.reasoning == "server-parsed\n\nplan"


def test_tag_split_across_reasoning_boundary_reassembles():
    """A reasoning delta cannot terminate a tag: a think tag the server
    split across one must reassemble — a partial-tag TAIL is carried into
    the next run (``partial_tag_tail``) instead of the halves passing
    through as visible content."""
    result = drain_stream(
        iter(
            [
                StreamChunk(content_delta="Hello <thi"),
                StreamChunk(reasoning_delta="server-parsed"),
                StreamChunk(content_delta="nk> secret plan"),
                StreamChunk(finish_reason="stop"),
            ]
        )
    )
    assert result.content == "Hello "
    assert result.reasoning == "server-parsed\n\n secret plan"


def test_partial_tag_carry_flushes_when_stream_ends():
    """A carried tail that never completes a tag is CONTENT — the final
    close emits it, byte-preserved."""
    result = drain_stream(
        iter(
            [
                StreamChunk(content_delta="abc <thi"),
                StreamChunk(reasoning_delta="r"),
                StreamChunk(finish_reason="stop"),
            ]
        )
    )
    assert result.content == "abc <thi"
    assert result.reasoning == "r"


def test_scan_off_keeps_content_verbatim_and_reasoning_in_its_own_channel():
    """``server_parses_reasoning`` backends deliver reasoning through
    ``reasoning_delta``, so the drain does not scan content at all: tag
    text stays put (it is prose, not a boundary) and no edge trim fires,
    while the server-parsed lane is unaffected."""
    result = drain_stream(
        iter(
            [
                StreamChunk(reasoning_delta="server-parsed"),
                StreamChunk(content_delta="The `<think>` tag opens a block.\n"),
                StreamChunk(finish_reason="stop"),
            ]
        ),
        scan_inline_reasoning=False,
    )
    assert result.content == "The `<think>` tag opens a block.\n"
    assert result.reasoning == "server-parsed"


def test_inter_run_separator_survives_tool_boundary():
    result = drain_stream(
        iter(
            [
                StreamChunk(content_delta="Narration.<think>x</think>\n\nIntro:\n\n"),
                StreamChunk(tool_call_deltas=[ToolCallDelta(index=0, id="c1", name="bash")]),
                StreamChunk(content_delta="Post-call answer."),
                StreamChunk(finish_reason="stop"),
            ]
        )
    )
    assert result.content == "Narration.\n\nIntro:\n\nPost-call answer."
    assert result.reasoning == "x"
