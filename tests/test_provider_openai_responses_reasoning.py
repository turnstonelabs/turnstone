"""Tests for OpenAI Responses reasoning capture + replay (Phase 3 path 2).

Phase 3 wires:
1. ``include=["reasoning.encrypted_content"]`` on the request when
   the operator flag AND the model capability both allow.
2. ``_convert_messages`` round-tripping stored reasoning items as
   ``ResponseReasoningItemParam`` input items on subsequent turns.
3. ``OpenAIResponsesProvider.extract_reasoning_text`` walking
   reasoning items and returning concatenated summary + content text.

All tests drive through the real ``OpenAIResponsesProvider`` — no
mocks of the converter/build_kwargs themselves; only the SDK boundary
is mocked where relevant.
"""

from __future__ import annotations

import pytest

from turnstone.core.providers._openai_responses import (
    OpenAIResponsesProvider,
    _reasoning_item_for_input,
)
from turnstone.core.providers._protocol import (
    MAX_REASONING_DISPLAY_CHARS as _MAX_REASONING_DISPLAY_CHARS,
)
from turnstone.core.providers._protocol import ModelCapabilities


@pytest.fixture
def provider() -> OpenAIResponsesProvider:
    return OpenAIResponsesProvider()


def _capable_caps() -> ModelCapabilities:
    """Capability fixture for a reasoning-replay-capable model."""
    return ModelCapabilities(
        context_window=400000,
        max_output_tokens=128000,
        supports_temperature=False,
        reasoning_effort_values=("low", "medium", "high"),
        default_reasoning_effort="medium",
        supports_reasoning_replay=True,
    )


def _incapable_caps() -> ModelCapabilities:
    return ModelCapabilities(
        context_window=128000,
        supports_reasoning_replay=False,
    )


class TestExtractReasoningText:
    def test_none_returns_empty(self, provider: OpenAIResponsesProvider) -> None:
        assert provider.extract_reasoning_text(None) == ""

    def test_empty_list_returns_empty(self, provider: OpenAIResponsesProvider) -> None:
        assert provider.extract_reasoning_text([]) == ""

    def test_no_reasoning_items_returns_empty(self, provider: OpenAIResponsesProvider) -> None:
        blocks = [
            {"type": "message", "role": "assistant", "content": "hi"},
            {"type": "function_call", "call_id": "c1", "name": "x", "arguments": "{}"},
        ]
        assert provider.extract_reasoning_text(blocks) == ""

    def test_summary_text_extracted(self, provider: OpenAIResponsesProvider) -> None:
        # Per ResponseReasoningItem (response_reasoning_item.py:31-62):
        # summary is always present; content is optional.
        blocks = [
            {
                "type": "reasoning",
                "id": "r_1",
                "summary": [
                    {"type": "summary_text", "text": "I considered X"},
                    {"type": "summary_text", "text": "then Y"},
                ],
            }
        ]
        assert provider.extract_reasoning_text(blocks) == "I considered X\nthen Y"

    def test_content_text_extracted_alongside_summary(
        self, provider: OpenAIResponsesProvider
    ) -> None:
        blocks = [
            {
                "type": "reasoning",
                "id": "r_1",
                "summary": [{"type": "summary_text", "text": "summary line"}],
                "content": [{"type": "reasoning_text", "text": "raw reasoning"}],
            }
        ]
        # Order: summary first, then content (matches the order the SDK
        # surfaces them via streaming events).
        result = provider.extract_reasoning_text(blocks)
        assert "summary line" in result
        assert "raw reasoning" in result

    def test_truncation_at_64kib_cap(self, provider: OpenAIResponsesProvider) -> None:
        long_text = "x" * (_MAX_REASONING_DISPLAY_CHARS + 1024)
        blocks = [
            {
                "type": "reasoning",
                "id": "r_1",
                "summary": [{"type": "summary_text", "text": long_text}],
            }
        ]
        result = provider.extract_reasoning_text(blocks)
        assert len(result) == _MAX_REASONING_DISPLAY_CHARS

    def test_malformed_summary_entry_skipped(self, provider: OpenAIResponsesProvider) -> None:
        blocks = [
            {
                "type": "reasoning",
                "id": "r_1",
                "summary": [
                    "not a dict",
                    {"type": "summary_text"},  # missing text
                    {"type": "summary_text", "text": ""},  # empty text
                    {"type": "summary_text", "text": "good"},
                ],
            }
        ]
        assert provider.extract_reasoning_text(blocks) == "good"

    def test_non_list_input_returns_empty(self, provider: OpenAIResponsesProvider) -> None:
        assert provider.extract_reasoning_text("not a list") == ""  # type: ignore[arg-type]

    def test_other_block_types_skipped_in_walk(self, provider: OpenAIResponsesProvider) -> None:
        # Mixed payload: only the reasoning block contributes.
        blocks = [
            {"type": "message", "role": "assistant", "content": "hi"},
            {
                "type": "reasoning",
                "id": "r_1",
                "summary": [{"type": "summary_text", "text": "thought"}],
            },
            {"type": "function_call", "call_id": "c1", "name": "x", "arguments": "{}"},
        ]
        assert provider.extract_reasoning_text(blocks) == "thought"


class TestReasoningItemForInput:
    """``_reasoning_item_for_input`` projects a stored ``ResponseReasoningItem``
    dict into ``ResponseReasoningItemParam`` shape (drops server-only
    ``status``)."""

    def test_minimal_item_round_trip(self) -> None:
        stored = {
            "type": "reasoning",
            "id": "r_1",
            "summary": [{"type": "summary_text", "text": "x"}],
            "status": "completed",
        }
        result = _reasoning_item_for_input(stored)
        assert result["type"] == "reasoning"
        assert result["id"] == "r_1"
        assert result["summary"] == [{"type": "summary_text", "text": "x"}]
        # status NOT round-tripped (server-only field per
        # ResponseReasoningItemParam at response_reasoning_item_param.py).
        assert "status" not in result

    def test_encrypted_content_round_trips_when_present(self) -> None:
        stored = {
            "type": "reasoning",
            "id": "r_1",
            "summary": [{"type": "summary_text", "text": "x"}],
            "encrypted_content": "opaque-blob",
        }
        result = _reasoning_item_for_input(stored)
        assert result["encrypted_content"] == "opaque-blob"

    def test_encrypted_content_omitted_when_absent(self) -> None:
        stored = {
            "type": "reasoning",
            "id": "r_1",
            "summary": [{"type": "summary_text", "text": "x"}],
        }
        result = _reasoning_item_for_input(stored)
        assert "encrypted_content" not in result

    def test_content_round_trips_when_present(self) -> None:
        stored = {
            "type": "reasoning",
            "id": "r_1",
            "summary": [{"type": "summary_text", "text": "s"}],
            "content": [{"type": "reasoning_text", "text": "raw"}],
        }
        result = _reasoning_item_for_input(stored)
        assert result["content"] == [{"type": "reasoning_text", "text": "raw"}]


class TestBuildKwargsInclude:
    """``_build_kwargs`` adds ``include=["reasoning.encrypted_content"]``
    only when the operator flag AND the model capability both allow."""

    def test_include_added_when_flag_and_capability_true(
        self, provider: OpenAIResponsesProvider
    ) -> None:
        kwargs = provider._build_kwargs(
            model="gpt-5",
            messages=[{"role": "user", "content": "hi"}],
            tools=None,
            max_tokens=1024,
            temperature=0.5,
            reasoning_effort="medium",
            deferred_names=None,
            capabilities=_capable_caps(),
            replay_reasoning_to_model=True,
        )
        assert kwargs.get("include") == ["reasoning.encrypted_content"]

    def test_include_omitted_when_flag_false(self, provider: OpenAIResponsesProvider) -> None:
        kwargs = provider._build_kwargs(
            model="gpt-5",
            messages=[{"role": "user", "content": "hi"}],
            tools=None,
            max_tokens=1024,
            temperature=0.5,
            reasoning_effort="medium",
            deferred_names=None,
            capabilities=_capable_caps(),
            replay_reasoning_to_model=False,
        )
        assert "include" not in kwargs

    def test_include_omitted_when_capability_false(self, provider: OpenAIResponsesProvider) -> None:
        # Defends against operator flipping the flag on a non-reasoning
        # model — the capability gate prevents the include= from being
        # sent (silently no-op'd).
        kwargs = provider._build_kwargs(
            model="gpt-4o",
            messages=[{"role": "user", "content": "hi"}],
            tools=None,
            max_tokens=1024,
            temperature=0.5,
            reasoning_effort="medium",
            deferred_names=None,
            capabilities=_incapable_caps(),
            replay_reasoning_to_model=True,
        )
        assert "include" not in kwargs

    def test_include_omitted_by_default(self, provider: OpenAIResponsesProvider) -> None:
        # When neither flag nor capability is passed, replay defaults
        # True (kwarg) but capability defaults False — net: no include.
        kwargs = provider._build_kwargs(
            model="gpt-4o",
            messages=[{"role": "user", "content": "hi"}],
            tools=None,
            max_tokens=1024,
            temperature=0.5,
            reasoning_effort="medium",
            deferred_names=None,
        )
        assert "include" not in kwargs


class TestConvertMessagesReasoningReplay:
    """``_convert_messages`` round-trips stored reasoning items as input."""

    def test_reasoning_item_emitted_before_assistant_when_replay_true(
        self, provider: OpenAIResponsesProvider
    ) -> None:
        messages = [
            {"role": "user", "content": "explain"},
            {
                "role": "assistant",
                "content": "Final answer.",
                "_provider_content": [
                    {
                        "type": "reasoning",
                        "id": "r_1",
                        "summary": [{"type": "summary_text", "text": "I thought"}],
                        "encrypted_content": "abc",
                    }
                ],
            },
            {"role": "user", "content": "follow up"},
        ]
        _, items = provider._convert_messages(messages, replay_reasoning_to_model=True)
        # Find the reasoning input item.
        types = [it.get("type") for it in items]
        # Expected: user, reasoning, message (assistant), user.
        assert types == ["message", "reasoning", "message", "message"]
        reasoning_idx = types.index("reasoning")
        r_item = items[reasoning_idx]
        assert r_item["id"] == "r_1"
        assert r_item["encrypted_content"] == "abc"
        # And the reasoning item appears immediately BEFORE the
        # assistant message it belongs to.
        assert items[reasoning_idx + 1]["role"] == "assistant"

    def test_reasoning_item_dropped_when_replay_false(
        self, provider: OpenAIResponsesProvider
    ) -> None:
        messages = [
            {
                "role": "assistant",
                "content": "Answer.",
                "_provider_content": [
                    {
                        "type": "reasoning",
                        "id": "r_1",
                        "summary": [{"type": "summary_text", "text": "thought"}],
                    }
                ],
            },
        ]
        _, items = provider._convert_messages(messages, replay_reasoning_to_model=False)
        types = [it.get("type") for it in items]
        assert "reasoning" not in types

    def test_no_reasoning_items_when_provider_content_lacks_reasoning(
        self, provider: OpenAIResponsesProvider
    ) -> None:
        # Anthropic-shaped _provider_content reaching OpenAI Responses
        # (cross-provider — operator switch from Anthropic to GPT-5):
        # no type=="reasoning" items, so nothing emitted.
        messages = [
            {
                "role": "assistant",
                "content": "x",
                "_provider_content": [
                    {"type": "thinking", "thinking": "anth", "signature": "s"},
                ],
            },
        ]
        _, items = provider._convert_messages(messages, replay_reasoning_to_model=True)
        types = [it.get("type") for it in items]
        assert "reasoning" not in types

    def test_default_replay_reasoning_false_omits_reasoning(
        self, provider: OpenAIResponsesProvider
    ) -> None:
        # Pre-Phase-3 callers (no kwarg) get the back-compat behaviour:
        # reasoning items are silently dropped (sanitize_messages was
        # already stripping _provider_content anyway).
        messages = [
            {
                "role": "assistant",
                "content": "x",
                "_provider_content": [
                    {
                        "type": "reasoning",
                        "id": "r_1",
                        "summary": [{"type": "summary_text", "text": "x"}],
                    }
                ],
            },
        ]
        _, items = provider._convert_messages(messages)  # no kwarg
        types = [it.get("type") for it in items]
        assert "reasoning" not in types
