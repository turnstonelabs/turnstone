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
)


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
                ]
            )
        )
        assert result.reasoning == "think hard"
        assert result.content == "answer"

    def test_empty_stream_yields_defaults(self):
        result = drain_stream(iter([]))
        assert result.content == ""
        assert result.reasoning == ""
        assert result.tool_calls is None
        assert result.finish_reason == "stop"
        assert result.usage is None
        assert result.provider_blocks == []


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
                ]
            )
        )
        assert [tc["id"] for tc in result.tool_calls] == ["a", "b"]
        assert result.tool_calls[1]["function"]["arguments"] == '{"k": 1}'

    def test_blank_id_preserved_for_downstream_repair(self):
        # Google compat can stream blank tool ids — the drain must hand them
        # through untouched so model_turn's pairwise blank-id repair sees them.
        result = drain_stream(
            iter([StreamChunk(tool_call_deltas=[ToolCallDelta(index=0, name="f")])])
        )
        assert result.tool_calls[0]["id"] == ""


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

    def test_trailing_fold_with_empty_content_matches_too(self):
        result = drain_stream(
            iter(
                [
                    StreamChunk(finish_reason="tool_calls"),
                    StreamChunk(info_delta="Sources:\n- x"),
                ]
            )
        )
        assert result.content == "\n\nSources:\n- x"


class TestErrorPropagation:
    def test_mid_stream_exception_propagates_verbatim(self):
        # Retry/deadline/fallback policy is the caller's — the drain adds
        # no exception translation, exactly like the old transport.
        def chunks():
            yield StreamChunk(content_delta="partial")
            raise RuntimeError("upstream broke")

        with pytest.raises(RuntimeError, match="upstream broke"):
            drain_stream(chunks())
