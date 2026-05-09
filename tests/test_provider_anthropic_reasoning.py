"""Tests for ``AnthropicProvider.extract_reasoning_text``.

Phase 1 of the optional-reasoning-persistence feature: provider-side
extractor that walks stored ``provider_blocks`` and returns the
concatenated thinking text, capped at the operator-friendly UI display
size.

These tests drive through the real ``AnthropicProvider`` instance — no
mocks of the extractor itself — using fixture-shaped blocks that match
what ``_iter_anthropic_stream`` actually accumulates at
``_anthropic.py:713-724`` (``thinking_delta`` + ``signature_delta``
combined into ``{"type": "thinking", "thinking": <text>, "signature":
<sig>}``).
"""

from __future__ import annotations

import pytest

from turnstone.core.providers._anthropic import AnthropicProvider
from turnstone.core.providers._openai_chat import OpenAIChatCompletionsProvider
from turnstone.core.providers._openai_responses import OpenAIResponsesProvider
from turnstone.core.providers._protocol import (
    MAX_REASONING_DISPLAY_CHARS as _MAX_REASONING_DISPLAY_CHARS,
)


@pytest.fixture
def anthropic() -> AnthropicProvider:
    return AnthropicProvider()


class TestExtractReasoningText:
    def test_none_input_returns_empty_string(self, anthropic: AnthropicProvider) -> None:
        assert anthropic.extract_reasoning_text(None) == ""

    def test_empty_list_returns_empty_string(self, anthropic: AnthropicProvider) -> None:
        assert anthropic.extract_reasoning_text([]) == ""

    def test_no_thinking_blocks_returns_empty(self, anthropic: AnthropicProvider) -> None:
        blocks: list[dict[str, object]] = [
            {"type": "text", "text": "hello"},
            {"type": "tool_use", "id": "t1", "name": "x", "input": {}},
        ]
        assert anthropic.extract_reasoning_text(blocks) == ""

    def test_single_thinking_block_returns_text(self, anthropic: AnthropicProvider) -> None:
        blocks = [{"type": "thinking", "thinking": "Let me think about this.", "signature": "abc"}]
        assert anthropic.extract_reasoning_text(blocks) == "Let me think about this."

    def test_multiple_thinking_blocks_joined_with_newline(
        self, anthropic: AnthropicProvider
    ) -> None:
        blocks = [
            {"type": "thinking", "thinking": "first thought", "signature": "s1"},
            {"type": "thinking", "thinking": "second thought", "signature": "s2"},
        ]
        assert anthropic.extract_reasoning_text(blocks) == "first thought\nsecond thought"

    def test_mixed_blocks_extracts_only_thinking(self, anthropic: AnthropicProvider) -> None:
        blocks = [
            {"type": "thinking", "thinking": "reason A", "signature": "s"},
            {"type": "text", "text": "visible answer"},
            {"type": "tool_use", "id": "t1", "name": "x", "input": {}},
            {"type": "thinking", "thinking": "reason B", "signature": "s"},
        ]
        assert anthropic.extract_reasoning_text(blocks) == "reason A\nreason B"

    def test_thinking_block_without_thinking_field_skipped(
        self, anthropic: AnthropicProvider
    ) -> None:
        blocks = [{"type": "thinking", "signature": "s"}]
        assert anthropic.extract_reasoning_text(blocks) == ""

    def test_thinking_block_with_empty_text_skipped(self, anthropic: AnthropicProvider) -> None:
        blocks = [{"type": "thinking", "thinking": "", "signature": "s"}]
        assert anthropic.extract_reasoning_text(blocks) == ""

    def test_truncation_at_64kib_cap(self, anthropic: AnthropicProvider) -> None:
        long_text = "x" * (_MAX_REASONING_DISPLAY_CHARS + 1024)
        blocks = [{"type": "thinking", "thinking": long_text, "signature": "s"}]
        result = anthropic.extract_reasoning_text(blocks)
        assert len(result) == _MAX_REASONING_DISPLAY_CHARS

    def test_just_under_cap_not_truncated(self, anthropic: AnthropicProvider) -> None:
        text = "y" * (_MAX_REASONING_DISPLAY_CHARS - 1)
        blocks = [{"type": "thinking", "thinking": text, "signature": "s"}]
        assert anthropic.extract_reasoning_text(blocks) == text

    def test_malformed_block_entry_skipped(self, anthropic: AnthropicProvider) -> None:
        # A defensive sanity check — we should not crash if some
        # entry isn't a dict (e.g. a corrupted JSON payload).
        blocks = [
            "not a dict",  # type: ignore[list-item]
            {"type": "thinking", "thinking": "good one", "signature": "s"},
        ]
        assert anthropic.extract_reasoning_text(blocks) == "good one"  # type: ignore[arg-type]

    def test_non_list_input_returns_empty(self, anthropic: AnthropicProvider) -> None:
        # Defensive against a corrupted provider_data payload.
        assert anthropic.extract_reasoning_text("not a list") == ""  # type: ignore[arg-type]
        assert anthropic.extract_reasoning_text({"type": "thinking"}) == ""  # type: ignore[arg-type]


class TestOtherProvidersDefault:
    """Non-Anthropic providers return "" for the same fixture shapes."""

    def test_openai_chat_returns_empty(self) -> None:
        provider = OpenAIChatCompletionsProvider()
        blocks = [{"type": "thinking", "thinking": "would-be-text", "signature": "s"}]
        assert provider.extract_reasoning_text(blocks) == ""

    def test_openai_responses_returns_empty(self) -> None:
        provider = OpenAIResponsesProvider()
        blocks = [{"type": "thinking", "thinking": "would-be-text", "signature": "s"}]
        assert provider.extract_reasoning_text(blocks) == ""

    def test_openai_responses_extracts_reasoning_summary(self) -> None:
        # Phase 3: extractor now walks reasoning items captured via
        # include=["reasoning.encrypted_content"] and returns the
        # summary[*].text concatenation.  Pre-Phase-3 this returned
        # "" — the stub was replaced once the wire path landed.
        provider = OpenAIResponsesProvider()
        blocks = [{"type": "reasoning", "summary": [{"type": "summary_text", "text": "x"}]}]
        assert provider.extract_reasoning_text(blocks) == "x"
