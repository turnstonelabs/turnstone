"""Tests for Phase 2 wire-build replay flag + shape filter on AnthropicProvider.

Phase 2 of optional reasoning persistence wraps the verbatim
``_provider_content`` replay path at ``_anthropic.py:_convert_messages``
with two gates:

1. ``ANTHROPIC_VALID_BLOCK_TYPES`` shape filter — foreign-shaped
   blocks (OpenAI Responses ``type="reasoning"`` after Phase 3 lands,
   Gemini thought parts, anything else) fall through to the
   text+tool_calls rebuild path rather than 400-ing the API.  Closes
   a pre-existing latent bug.
2. ``replay_reasoning_to_model`` operator flag — when False (the
   ``model_definitions`` server_default), thinking blocks are
   stripped before the wire payload is built.  Tool_use /
   server_tool_use / web_search_tool_result blocks (which carry
   web-search ``encrypted_content``) intentionally survive — the
   strip predicate is narrow by design.

Drives through the real ``AnthropicProvider._convert_messages`` with
fixture-shaped messages, no mocks of the converter.  Edge cases come
from the briefing's "Edges & validation memo" sections.
"""

from __future__ import annotations

import pytest

from turnstone.core.providers._anthropic import (
    ANTHROPIC_REASONING_BLOCK_TYPES,
    ANTHROPIC_VALID_BLOCK_TYPES,
    AnthropicProvider,
)


@pytest.fixture
def provider() -> AnthropicProvider:
    return AnthropicProvider()


def _assistant_with_thinking(content: str = "Final answer.") -> dict[str, object]:
    """Build an assistant message with a thinking + text + tool_use shape
    matching what the streaming layer captures at ``_anthropic.py:713-724``."""
    return {
        "role": "assistant",
        "content": content,
        "_provider_content": [
            {"type": "thinking", "thinking": "let me think", "signature": "sig"},
            {"type": "text", "text": content},
            {
                "type": "tool_use",
                "id": "call_abc",
                "name": "search",
                "input": {"q": "x"},
            },
        ],
        "tool_calls": [
            {
                "id": "call_abc",
                "type": "function",
                "function": {"name": "search", "arguments": '{"q": "x"}'},
            }
        ],
    }


class TestReplayFlagStripsThinking:
    """``replay_reasoning_to_model=False`` strips thinking; ``True`` preserves."""

    def test_replay_true_preserves_thinking_block(self, provider: AnthropicProvider) -> None:
        msg = _assistant_with_thinking()
        _, converted = provider._convert_messages([msg], replay_reasoning_to_model=True)
        assistant = next(m for m in converted if m["role"] == "assistant")
        types_present = [b["type"] for b in assistant["content"]]
        assert "thinking" in types_present
        assert "text" in types_present
        assert "tool_use" in types_present

    def test_replay_false_strips_thinking_block(self, provider: AnthropicProvider) -> None:
        msg = _assistant_with_thinking()
        _, converted = provider._convert_messages([msg], replay_reasoning_to_model=False)
        assistant = next(m for m in converted if m["role"] == "assistant")
        types_present = [b["type"] for b in assistant["content"]]
        assert "thinking" not in types_present
        assert "text" in types_present  # final answer survives
        assert "tool_use" in types_present  # tool dispatch survives

    def test_replay_false_strips_redacted_thinking_too(self, provider: AnthropicProvider) -> None:
        # Anthropic emits redacted_thinking blocks when the safety system
        # rewrites a thinking block.  Phase 2 strip predicate must include
        # both shapes.
        msg = {
            "role": "assistant",
            "content": "Answer.",
            "_provider_content": [
                {"type": "redacted_thinking", "data": "redacted-blob"},
                {"type": "text", "text": "Answer."},
            ],
        }
        _, converted = provider._convert_messages([msg], replay_reasoning_to_model=False)
        assistant = next(m for m in converted if m["role"] == "assistant")
        types_present = [b["type"] for b in assistant["content"]]
        assert "redacted_thinking" not in types_present
        assert "text" in types_present

    def test_default_kwarg_preserves_existing_behaviour(self, provider: AnthropicProvider) -> None:
        """Pre-Phase-2 callers that don't pass the kwarg get the verbatim
        replay (default True), matching the behaviour all production
        Anthropic-with-thinking turns shipped with for months."""
        msg = _assistant_with_thinking()
        _, converted = provider._convert_messages([msg])  # no kwarg
        assistant = next(m for m in converted if m["role"] == "assistant")
        types_present = [b["type"] for b in assistant["content"]]
        assert "thinking" in types_present


class TestWebSearchBlocksSurviveStrip:
    """Edge 14: Anthropic web-search ``encrypted_content`` rides on
    ``server_tool_use`` / ``web_search_tool_result`` blocks (NOT
    thinking blocks).  Strip predicate is intentionally narrow."""

    def test_server_tool_use_survives(self, provider: AnthropicProvider) -> None:
        msg = {
            "role": "assistant",
            "content": "From search: ...",
            "_provider_content": [
                {"type": "thinking", "thinking": "I should search", "signature": "s"},
                {
                    "type": "server_tool_use",
                    "id": "stu_1",
                    "name": "web_search",
                    "input": {"query": "turnstone bird"},
                },
                {
                    "type": "web_search_tool_result",
                    "tool_use_id": "stu_1",
                    "content": [{"type": "web_search_result", "url": "https://e.com"}],
                    "encrypted_content": "abc123encrypted",
                    "encrypted_index": "idx456encrypted",
                },
                {"type": "text", "text": "From search: ..."},
            ],
        }
        _, converted = provider._convert_messages([msg], replay_reasoning_to_model=False)
        assistant = next(m for m in converted if m["role"] == "assistant")
        types_present = [b["type"] for b in assistant["content"]]
        assert "thinking" not in types_present  # stripped
        assert "server_tool_use" in types_present  # survives
        assert "web_search_tool_result" in types_present  # survives
        assert "text" in types_present  # survives
        # encrypted_content rides through intact — required for round-trip continuity
        wsr = next(b for b in assistant["content"] if b["type"] == "web_search_tool_result")
        assert wsr["encrypted_content"] == "abc123encrypted"

    def test_tool_use_block_survives(self, provider: AnthropicProvider) -> None:
        # Plain tool_use (not server-side) — used by client-side function
        # tools.  Strip predicate must not touch these.
        msg = {
            "role": "assistant",
            "content": "Calling tool",
            "_provider_content": [
                {"type": "thinking", "thinking": "I should call tool", "signature": "s"},
                {"type": "tool_use", "id": "tu_1", "name": "f", "input": {"a": 1}},
            ],
            "tool_calls": [
                {
                    "id": "tu_1",
                    "type": "function",
                    "function": {"name": "f", "arguments": '{"a": 1}'},
                }
            ],
        }
        # Provide the tool result so orphan-tool detection doesn't synthesize
        msgs = [
            msg,
            {"role": "tool", "tool_call_id": "tu_1", "content": "ok"},
        ]
        _, converted = provider._convert_messages(msgs, replay_reasoning_to_model=False)
        assistant = next(m for m in converted if m["role"] == "assistant")
        types_present = [b["type"] for b in assistant["content"]]
        assert "thinking" not in types_present
        assert "tool_use" in types_present

    def test_orphan_tool_use_synthesized_after_strip(self, provider: AnthropicProvider) -> None:
        """Pin the post-strip orphan-tool branch at _anthropic.py:397-433.

        The implementation comment specifically calls out reading
        ``provider_content`` (not ``wire_blocks``) for the orphan-tool
        ID walk after the strip — keeping the read on the source-of-
        truth list so a future refactor that swapped them would still
        get the same set of tool_use IDs.  This test exercises that
        branch end-to-end: replay=False strips the thinking block,
        AND the message has a tool_use whose result is missing.  The
        converter must synthesize a 'cancelled' tool_result for the
        orphaned tool_use ID (matching the existing pre-Phase-2
        behaviour for the verbatim path).
        """
        msg = {
            "role": "assistant",
            "content": "Calling tool",
            "_provider_content": [
                {"type": "thinking", "thinking": "let me think", "signature": "s"},
                {"type": "tool_use", "id": "orphan_tu", "name": "f", "input": {"a": 1}},
            ],
            "tool_calls": [
                {
                    "id": "orphan_tu",
                    "type": "function",
                    "function": {"name": "f", "arguments": '{"a": 1}'},
                }
            ],
        }
        # NO tool result follows — orphan branch must synthesize one.
        _, converted = provider._convert_messages([msg], replay_reasoning_to_model=False)
        # Synthetic tool_result lands as a user-role message immediately
        # after the assistant turn (per existing behaviour at
        # _anthropic.py:421-430).
        assistant = next(m for m in converted if m["role"] == "assistant")
        # Stripped: thinking gone, tool_use survives.
        a_types = [b["type"] for b in assistant["content"]]
        assert "thinking" not in a_types
        assert "tool_use" in a_types
        # Synthesized: cancelled tool_result for orphan_tu attached to a
        # following user-role message.
        user_msgs_after = [m for m in converted if m["role"] == "user"]
        assert user_msgs_after, (
            "Expected a synthetic user message carrying the cancelled "
            "tool_result for the orphaned tool_use"
        )
        flat_results = [
            block
            for um in user_msgs_after
            if isinstance(um["content"], list)
            for block in um["content"]
            if isinstance(block, dict) and block.get("type") == "tool_result"
        ]
        synth = next(
            (b for b in flat_results if b.get("tool_use_id") == "orphan_tu"),
            None,
        )
        assert synth is not None, f"Expected synthetic tool_result for orphan_tu in {flat_results}"
        assert synth.get("is_error") is True
        assert "cancelled" in synth.get("content", "").lower()


class TestShapeFilterFallthrough:
    """Foreign / empty / mixed-shape ``_provider_content`` falls through
    to the text+tool_calls rebuild path rather than reaching the wire
    as a malformed block."""

    def test_foreign_shape_openai_reasoning_falls_through(
        self, provider: AnthropicProvider
    ) -> None:
        # OpenAI Responses style block (Phase 3 will land this shape into
        # _provider_content via include=["reasoning.encrypted_content"]).
        # Mid-workstream model switch from OpenAI -> Anthropic must NOT
        # reach the API with an OpenAI-shaped block (which would 400).
        msg = {
            "role": "assistant",
            "content": "Final answer from openai turn.",
            "_provider_content": [
                {
                    "type": "reasoning",
                    "summary": [{"type": "summary_text", "text": "I reasoned..."}],
                    "encrypted_content": "openai-encrypted",
                }
            ],
            "tool_calls": [],
        }
        _, converted = provider._convert_messages([msg], replay_reasoning_to_model=True)
        assistant = next(m for m in converted if m["role"] == "assistant")
        # Rebuilt from text — no foreign block reached the wire.
        for b in assistant["content"]:
            assert b.get("type") in ANTHROPIC_VALID_BLOCK_TYPES, (
                f"Foreign block type leaked through: {b}"
            )
        # And the foreign block specifically is NOT present.
        types_present = [b["type"] for b in assistant["content"]]
        assert "reasoning" not in types_present

    def test_mixed_shape_one_foreign_block_falls_through(self, provider: AnthropicProvider) -> None:
        # Even a single foreign block in a mostly-Anthropic payload
        # forces fall-through (the shape predicate is "all blocks
        # match", not "majority match").
        msg = {
            "role": "assistant",
            "content": "Mixed.",
            "_provider_content": [
                {"type": "thinking", "thinking": "anth shape", "signature": "s"},
                {"type": "text", "text": "Mixed."},
                {"type": "reasoning", "summary": []},  # foreign
            ],
        }
        _, converted = provider._convert_messages([msg], replay_reasoning_to_model=True)
        assistant = next(m for m in converted if m["role"] == "assistant")
        for b in assistant["content"]:
            assert b.get("type") in ANTHROPIC_VALID_BLOCK_TYPES

    def test_empty_provider_content_falls_through(self, provider: AnthropicProvider) -> None:
        msg = {
            "role": "assistant",
            "content": "Plain text answer.",
            "_provider_content": [],
        }
        _, converted = provider._convert_messages([msg])
        assistant = next(m for m in converted if m["role"] == "assistant")
        # Falls through to text rebuild
        assert assistant["content"] == [{"type": "text", "text": "Plain text answer."}]

    def test_none_provider_content_falls_through(self, provider: AnthropicProvider) -> None:
        msg = {
            "role": "assistant",
            "content": "Plain text answer.",
            "_provider_content": None,
        }
        _, converted = provider._convert_messages([msg])
        assistant = next(m for m in converted if m["role"] == "assistant")
        assert assistant["content"] == [{"type": "text", "text": "Plain text answer."}]

    def test_provider_content_not_a_list_falls_through(self, provider: AnthropicProvider) -> None:
        # Defensive against a corrupted provider_data deserialization.
        msg = {
            "role": "assistant",
            "content": "Plain.",
            "_provider_content": "not a list",
        }
        _, converted = provider._convert_messages([msg])
        assistant = next(m for m in converted if m["role"] == "assistant")
        assert assistant["content"] == [{"type": "text", "text": "Plain."}]


class TestLegacyAnthropicRowsNoRegression:
    """Critical property: rows persisted before Phase 2 carry valid
    Anthropic-shape _provider_content (only Anthropic captured this lane
    historically).  They must stay in the verbatim path and keep their
    thinking context across the migration boundary when replay=True
    (the legacy default).
    """

    def test_legacy_thinking_row_preserved_with_default_kwarg(
        self, provider: AnthropicProvider
    ) -> None:
        """No kwarg passed (matches the pre-Phase-2 production call site)."""
        msg = {
            "role": "assistant",
            "content": "Old answer from months ago.",
            "_provider_content": [
                {"type": "thinking", "thinking": "old reasoning", "signature": "s"},
                {"type": "text", "text": "Old answer from months ago."},
            ],
        }
        _, converted = provider._convert_messages([msg])
        assistant = next(m for m in converted if m["role"] == "assistant")
        types_present = [b["type"] for b in assistant["content"]]
        assert "thinking" in types_present  # preserved -> no regression
        # The thinking block IS the same dict as the source (verbatim path).
        assert assistant["content"][0]["thinking"] == "old reasoning"

    def test_replay_false_only_strips_when_explicitly_requested(
        self, provider: AnthropicProvider
    ) -> None:
        # Operator flips persist+replay flags off.  Strip fires.
        # Pinning that the strip is gated on the explicit flag value,
        # not silently triggered by some other condition.
        msg = {
            "role": "assistant",
            "content": "Answer.",
            "_provider_content": [
                {"type": "thinking", "thinking": "stripped", "signature": "s"},
                {"type": "text", "text": "Answer."},
            ],
        }
        _, converted_default = provider._convert_messages([msg])
        _, converted_strip = provider._convert_messages([msg], replay_reasoning_to_model=False)
        default_types = [b["type"] for b in converted_default[0]["content"]]
        strip_types = [b["type"] for b in converted_strip[0]["content"]]
        assert "thinking" in default_types
        assert "thinking" not in strip_types


class TestStripAllBlocksFallthrough:
    """When the message is 100% thinking (no text, no tool_use) and
    replay=False strips everything, the message falls through to the
    text+tool_calls rebuild path.  If both are also empty, the assistant
    turn is silently skipped — correct: stripped reasoning has nothing
    to replay."""

    def test_only_thinking_strip_falls_to_rebuild_with_text(
        self, provider: AnthropicProvider
    ) -> None:
        # Provider_content = only thinking; msg.content has the spoken text.
        # Strip drops thinking; rebuild path picks up the content as a
        # text block.  No information lost.
        msg = {
            "role": "assistant",
            "content": "Spoken answer.",
            "_provider_content": [
                {"type": "thinking", "thinking": "internal", "signature": "s"},
            ],
        }
        _, converted = provider._convert_messages([msg], replay_reasoning_to_model=False)
        assistant = next(m for m in converted if m["role"] == "assistant")
        assert assistant["content"] == [{"type": "text", "text": "Spoken answer."}]

    def test_only_thinking_strip_with_no_content_skips_message(
        self, provider: AnthropicProvider
    ) -> None:
        # Edge: provider_content was 100% thinking AND msg.content is
        # empty AND no tool_calls.  The rebuild path sees nothing to
        # emit — assistant turn silently skipped.  Anthropic's API
        # would reject an empty assistant content array anyway.
        msg = {
            "role": "assistant",
            "content": "",
            "_provider_content": [
                {"type": "thinking", "thinking": "only", "signature": "s"},
            ],
        }
        _, converted = provider._convert_messages([msg], replay_reasoning_to_model=False)
        # Assistant turn skipped — no entry for it in `converted`.
        assert all(m["role"] != "assistant" for m in converted)


class TestConstants:
    """Pin the constant contents so a future edit doesn't accidentally
    widen the strip set or narrow the valid set."""

    def test_reasoning_block_types_is_narrow(self) -> None:
        # Strip predicate MUST cover only reasoning shapes.  Adding
        # tool_use here would break web-search round-trip.
        assert frozenset({"thinking", "redacted_thinking"}) == ANTHROPIC_REASONING_BLOCK_TYPES

    def test_valid_block_types_includes_web_search(self) -> None:
        # Without server_tool_use / web_search_tool_result, Anthropic
        # web-search results would fall through to the rebuild path
        # and lose their encrypted_content.
        assert "server_tool_use" in ANTHROPIC_VALID_BLOCK_TYPES
        assert "web_search_tool_result" in ANTHROPIC_VALID_BLOCK_TYPES
        assert "tool_use" in ANTHROPIC_VALID_BLOCK_TYPES
        assert "tool_result" in ANTHROPIC_VALID_BLOCK_TYPES

    def test_reasoning_subset_of_valid(self) -> None:
        # The strip set must be a subset of the valid set — otherwise
        # the strip predicate would never match anything (we only
        # strip after shape validity passes).
        assert ANTHROPIC_REASONING_BLOCK_TYPES.issubset(ANTHROPIC_VALID_BLOCK_TYPES)
