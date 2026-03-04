"""Tests for turnstone.core.providers — protocol, OpenAI provider, Anthropic provider."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from turnstone.core.providers._openai import OpenAIProvider
from turnstone.core.providers._protocol import (
    CompletionResult,
    LLMProvider,
    StreamChunk,
    ToolCallDelta,
    UsageInfo,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _openai_stream_chunk(
    *,
    content: str | None = None,
    reasoning: str | None = None,
    reasoning_content: str | None = None,
    tool_calls: list[MagicMock] | None = None,
    finish_reason: str | None = None,
    usage: MagicMock | None = None,
    empty_choices: bool = False,
) -> MagicMock:
    """Build a mock OpenAI streaming chunk."""
    chunk = MagicMock()
    if empty_choices:
        chunk.choices = []
        chunk.usage = usage
        return chunk

    delta = MagicMock()
    delta.content = content
    delta.tool_calls = tool_calls

    # Reasoning attributes accessed via getattr
    type(delta).reasoning = PropertyMock(return_value=reasoning)
    type(delta).reasoning_content = PropertyMock(return_value=reasoning_content)

    choice = MagicMock()
    choice.delta = delta
    choice.finish_reason = finish_reason

    chunk.choices = [choice]
    chunk.usage = usage
    return chunk


def _openai_tool_call_delta(
    *,
    index: int = 0,
    tc_id: str | None = None,
    name: str | None = None,
    arguments: str | None = None,
) -> MagicMock:
    """Build a mock OpenAI tool call delta within a streaming chunk."""
    tcd = MagicMock()
    tcd.index = index
    tcd.id = tc_id
    tcd.function = MagicMock()
    tcd.function.name = name
    tcd.function.arguments = arguments
    return tcd


def _anthropic_event(
    event_type: str,
    **kwargs: Any,
) -> MagicMock:
    """Build a mock Anthropic streaming event."""
    event = MagicMock()
    event.type = event_type

    if event_type == "content_block_start":
        block = MagicMock()
        block.type = kwargs.get("block_type", "text")
        block.id = kwargs.get("block_id", "")
        block.name = kwargs.get("block_name", "")
        event.content_block = block
        event.index = kwargs.get("index", 0)

    elif event_type == "content_block_delta":
        delta = MagicMock()
        delta.type = kwargs.get("delta_type", "text_delta")
        delta.text = kwargs.get("text", "")
        delta.thinking = kwargs.get("thinking", "")
        delta.partial_json = kwargs.get("partial_json", "")
        event.delta = delta
        event.index = kwargs.get("index", 0)

    elif event_type == "message_delta":
        if "usage_output_tokens" in kwargs:
            usage = MagicMock()
            usage.input_tokens = kwargs.get("usage_input_tokens", 0)
            usage.output_tokens = kwargs.get("usage_output_tokens", 0)
            event.usage = usage
        else:
            event.usage = None
        stop_delta = MagicMock()
        stop_delta.stop_reason = kwargs.get("stop_reason")
        event.delta = stop_delta

    elif event_type == "message_start":
        msg = MagicMock()
        if "usage_input_tokens" in kwargs:
            msg_usage = MagicMock()
            msg_usage.input_tokens = kwargs.get("usage_input_tokens", 0)
            msg.usage = msg_usage
        else:
            msg.usage = None
        event.message = msg

    return event


# ===========================================================================
# TestOpenAIProvider
# ===========================================================================


class TestOpenAIProvider:
    """Tests for the OpenAI-compatible provider adapter."""

    def setup_method(self) -> None:
        self.provider = OpenAIProvider()

    def test_provider_name(self) -> None:
        assert self.provider.provider_name == "openai"

    def test_convert_tools_passthrough(self) -> None:
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "Read a file",
                    "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
                },
            }
        ]
        assert self.provider.convert_tools(tools) is tools

    def test_streaming_content(self) -> None:
        chunks = [
            _openai_stream_chunk(content="Hello"),
            _openai_stream_chunk(content=" world"),
        ]
        client = MagicMock()
        client.chat.completions.create.return_value = iter(chunks)

        results = list(
            self.provider.create_streaming(
                client=client,
                model="gpt-4o",
                messages=[{"role": "user", "content": "hi"}],
            )
        )
        assert len(results) == 2
        assert results[0].content_delta == "Hello"
        assert results[1].content_delta == " world"

    def test_streaming_reasoning(self) -> None:
        chunks = [
            _openai_stream_chunk(reasoning_content="thinking..."),
            _openai_stream_chunk(reasoning_content="more thought"),
        ]
        client = MagicMock()
        client.chat.completions.create.return_value = iter(chunks)

        results = list(
            self.provider.create_streaming(
                client=client,
                model="qwen3-32b",
                messages=[{"role": "user", "content": "hi"}],
            )
        )
        assert len(results) == 2
        assert results[0].reasoning_delta == "thinking..."
        assert results[1].reasoning_delta == "more thought"

    def test_streaming_tool_calls(self) -> None:
        tc1 = _openai_tool_call_delta(index=0, tc_id="call_1", name="read_file")
        tc2 = _openai_tool_call_delta(index=0, arguments='{"path":')
        tc3 = _openai_tool_call_delta(index=0, arguments='"foo.py"}')

        chunks = [
            _openai_stream_chunk(tool_calls=[tc1]),
            _openai_stream_chunk(tool_calls=[tc2]),
            _openai_stream_chunk(tool_calls=[tc3]),
        ]
        client = MagicMock()
        client.chat.completions.create.return_value = iter(chunks)

        results = list(
            self.provider.create_streaming(
                client=client,
                model="gpt-4o",
                messages=[{"role": "user", "content": "read a file"}],
            )
        )
        assert len(results) == 3
        assert results[0].tool_call_deltas[0].id == "call_1"
        assert results[0].tool_call_deltas[0].name == "read_file"
        assert results[1].tool_call_deltas[0].arguments_delta == '{"path":'
        assert results[2].tool_call_deltas[0].arguments_delta == '"foo.py"}'

    def test_streaming_usage(self) -> None:
        usage = MagicMock()
        usage.prompt_tokens = 10
        usage.completion_tokens = 20
        usage.total_tokens = 30

        chunks = [
            _openai_stream_chunk(content="Hi"),
            _openai_stream_chunk(empty_choices=True, usage=usage),
        ]
        client = MagicMock()
        client.chat.completions.create.return_value = iter(chunks)

        results = list(
            self.provider.create_streaming(
                client=client,
                model="gpt-4o",
                messages=[{"role": "user", "content": "hi"}],
            )
        )
        # Last yielded chunk should carry usage
        usage_chunk = [r for r in results if r.usage is not None]
        assert len(usage_chunk) == 1
        assert usage_chunk[0].usage is not None
        assert usage_chunk[0].usage.prompt_tokens == 10
        assert usage_chunk[0].usage.completion_tokens == 20
        assert usage_chunk[0].usage.total_tokens == 30

    def test_streaming_finish_reason(self) -> None:
        chunks = [
            _openai_stream_chunk(content="done"),
            _openai_stream_chunk(finish_reason="stop"),
        ]
        client = MagicMock()
        client.chat.completions.create.return_value = iter(chunks)

        results = list(
            self.provider.create_streaming(
                client=client,
                model="gpt-4o",
                messages=[{"role": "user", "content": "hi"}],
            )
        )
        finish_chunks = [r for r in results if r.finish_reason is not None]
        assert len(finish_chunks) == 1
        assert finish_chunks[0].finish_reason == "stop"

    def test_streaming_finish_reason_tool_calls(self) -> None:
        tc = _openai_tool_call_delta(index=0, tc_id="call_1", name="fn")
        chunks = [
            _openai_stream_chunk(tool_calls=[tc]),
            _openai_stream_chunk(finish_reason="tool_calls"),
        ]
        client = MagicMock()
        client.chat.completions.create.return_value = iter(chunks)

        results = list(
            self.provider.create_streaming(
                client=client,
                model="gpt-4o",
                messages=[{"role": "user", "content": "hi"}],
            )
        )
        finish_chunks = [r for r in results if r.finish_reason is not None]
        assert finish_chunks[0].finish_reason == "tool_calls"

    def test_streaming_is_first(self) -> None:
        chunks = [
            _openai_stream_chunk(content="A"),
            _openai_stream_chunk(content="B"),
            _openai_stream_chunk(content="C"),
        ]
        client = MagicMock()
        client.chat.completions.create.return_value = iter(chunks)

        results = list(
            self.provider.create_streaming(
                client=client,
                model="gpt-4o",
                messages=[{"role": "user", "content": "hi"}],
            )
        )
        assert results[0].is_first is True
        assert results[1].is_first is False
        assert results[2].is_first is False

    def test_completion_basic(self) -> None:
        response = MagicMock()
        response.choices = [MagicMock()]
        response.choices[0].message.content = "Hello world"
        response.choices[0].message.tool_calls = None
        response.choices[0].finish_reason = "stop"
        response.usage.prompt_tokens = 10
        response.usage.completion_tokens = 5
        response.usage.total_tokens = 15

        client = MagicMock()
        client.chat.completions.create.return_value = response

        result = self.provider.create_completion(
            client=client,
            model="gpt-4o",
            messages=[{"role": "user", "content": "hi"}],
        )
        assert isinstance(result, CompletionResult)
        assert result.content == "Hello world"
        assert result.tool_calls is None
        assert result.finish_reason == "stop"

    def test_completion_with_tools(self) -> None:
        tc = MagicMock()
        tc.id = "call_abc"
        tc.function.name = "read_file"
        tc.function.arguments = '{"path": "foo.py"}'

        response = MagicMock()
        response.choices = [MagicMock()]
        response.choices[0].message.content = None
        response.choices[0].message.tool_calls = [tc]
        response.choices[0].finish_reason = "tool_calls"
        response.usage.prompt_tokens = 8
        response.usage.completion_tokens = 12
        response.usage.total_tokens = 20

        client = MagicMock()
        client.chat.completions.create.return_value = response

        result = self.provider.create_completion(
            client=client,
            model="gpt-4o",
            messages=[{"role": "user", "content": "read"}],
        )
        assert result.content == ""
        assert result.tool_calls is not None
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0]["id"] == "call_abc"
        assert result.tool_calls[0]["type"] == "function"
        assert result.tool_calls[0]["function"]["name"] == "read_file"
        assert result.tool_calls[0]["function"]["arguments"] == '{"path": "foo.py"}'
        assert result.finish_reason == "tool_calls"

    def test_completion_usage(self) -> None:
        response = MagicMock()
        response.choices = [MagicMock()]
        response.choices[0].message.content = "ok"
        response.choices[0].message.tool_calls = None
        response.choices[0].finish_reason = "stop"
        response.usage.prompt_tokens = 100
        response.usage.completion_tokens = 50
        response.usage.total_tokens = 150

        client = MagicMock()
        client.chat.completions.create.return_value = response

        result = self.provider.create_completion(
            client=client,
            model="gpt-4o",
            messages=[{"role": "user", "content": "hi"}],
        )
        assert result.usage is not None
        assert result.usage.prompt_tokens == 100
        assert result.usage.completion_tokens == 50
        assert result.usage.total_tokens == 150

    def test_retryable_errors(self) -> None:
        errors = self.provider.retryable_error_names
        assert isinstance(errors, frozenset)
        assert "APIError" in errors
        assert "APIConnectionError" in errors
        assert "RateLimitError" in errors
        assert "Timeout" in errors
        assert "APITimeoutError" in errors


# ===========================================================================
# TestAnthropicProvider
# ===========================================================================


class TestAnthropicProvider:
    """Tests for the Anthropic native provider adapter."""

    def setup_method(self) -> None:
        from turnstone.core.providers._anthropic import AnthropicProvider

        self.provider = AnthropicProvider()

    def test_provider_name(self) -> None:
        assert self.provider.provider_name == "anthropic"

    def test_convert_tools(self) -> None:
        openai_tools = [
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "Read a file from disk",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "write_file",
                    "description": "Write a file",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "content": {"type": "string"},
                        },
                    },
                },
            },
        ]
        result = self.provider.convert_tools(openai_tools)
        assert len(result) == 2
        assert result[0]["name"] == "read_file"
        assert result[0]["description"] == "Read a file from disk"
        assert result[0]["input_schema"]["type"] == "object"
        assert "path" in result[0]["input_schema"]["properties"]
        # No "type": "function" wrapper
        assert "function" not in result[0]
        assert "type" not in result[0]

        assert result[1]["name"] == "write_file"

    def test_message_conversion_basic(self) -> None:
        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
            {"role": "user", "content": "How are you?"},
        ]
        system, converted = self.provider._convert_messages(messages)
        assert system == "You are helpful."
        assert len(converted) == 3
        assert converted[0]["role"] == "user"
        assert converted[0]["content"] == "Hello"
        assert converted[1]["role"] == "assistant"
        assert converted[1]["content"] == [{"type": "text", "text": "Hi there!"}]
        assert converted[2]["role"] == "user"
        assert converted[2]["content"] == "How are you?"

    def test_message_conversion_tool_calls(self) -> None:
        messages = [
            {
                "role": "assistant",
                "content": "Let me check that.",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "function": {
                            "name": "read_file",
                            "arguments": '{"path": "foo.py"}',
                        },
                    }
                ],
            }
        ]
        _, converted = self.provider._convert_messages(messages)
        assert len(converted) == 1
        blocks = converted[0]["content"]
        assert len(blocks) == 2
        assert blocks[0] == {"type": "text", "text": "Let me check that."}
        assert blocks[1]["type"] == "tool_use"
        assert blocks[1]["id"] == "call_1"
        assert blocks[1]["name"] == "read_file"
        assert blocks[1]["input"] == {"path": "foo.py"}

    def test_message_conversion_tool_results(self) -> None:
        messages = [
            {"role": "tool", "tool_call_id": "call_1", "content": "file contents here"},
            {"role": "tool", "tool_call_id": "call_2", "content": "another result"},
        ]
        _, converted = self.provider._convert_messages(messages)
        assert len(converted) == 1
        assert converted[0]["role"] == "user"
        blocks = converted[0]["content"]
        assert len(blocks) == 2
        assert blocks[0]["type"] == "tool_result"
        assert blocks[0]["tool_use_id"] == "call_1"
        assert blocks[0]["content"] == "file contents here"
        assert blocks[1]["type"] == "tool_result"
        assert blocks[1]["tool_use_id"] == "call_2"
        assert blocks[1]["content"] == "another result"

    def test_message_conversion_alternating_merge(self) -> None:
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "user", "content": "Are you there?"},
            {"role": "assistant", "content": "Yes"},
            {"role": "assistant", "content": "I am here"},
        ]
        _, converted = self.provider._convert_messages(messages)
        assert len(converted) == 2
        # First merged user message
        assert converted[0]["role"] == "user"
        assert converted[0]["content"] == [
            {"type": "text", "text": "Hello"},
            {"type": "text", "text": "Are you there?"},
        ]
        # Second merged assistant message
        assert converted[1]["role"] == "assistant"
        assert converted[1]["content"] == [
            {"type": "text", "text": "Yes"},
            {"type": "text", "text": "I am here"},
        ]

    def test_message_conversion_developer_role_as_system(self) -> None:
        messages = [
            {"role": "developer", "content": "System prompt via developer role."},
            {"role": "user", "content": "Hi"},
        ]
        system, converted = self.provider._convert_messages(messages)
        assert system == "System prompt via developer role."
        assert len(converted) == 1
        assert converted[0]["role"] == "user"

    def test_message_conversion_multiple_system(self) -> None:
        messages = [
            {"role": "system", "content": "Part 1."},
            {"role": "system", "content": "Part 2."},
            {"role": "user", "content": "Go."},
        ]
        system, _ = self.provider._convert_messages(messages)
        assert system == "Part 1.\n\nPart 2."

    def test_reasoning_params_mapping(self) -> None:
        assert self.provider._reasoning_params("low", None, max_tokens=32768) == {
            "thinking": {"type": "enabled", "budget_tokens": 1024}
        }
        assert self.provider._reasoning_params("medium", None, max_tokens=32768) == {
            "thinking": {"type": "enabled", "budget_tokens": 4096}
        }
        assert self.provider._reasoning_params("high", None, max_tokens=32768) == {
            "thinking": {"type": "enabled", "budget_tokens": 16384}
        }

    def test_reasoning_params_override(self) -> None:
        result = self.provider._reasoning_params(
            "low", {"thinking_budget_tokens": 8192}, max_tokens=32768
        )
        assert result == {"thinking": {"type": "enabled", "budget_tokens": 8192}}

    def test_reasoning_params_unknown_effort(self) -> None:
        # Unknown effort falls back to 4096
        result = self.provider._reasoning_params("turbo", None, max_tokens=32768)
        assert result == {"thinking": {"type": "enabled", "budget_tokens": 4096}}

    def test_reasoning_params_budget_clamped(self) -> None:
        # Budget >= max_tokens gets clamped to leave room for response
        result = self.provider._reasoning_params("high", None, max_tokens=4096)
        assert result == {"thinking": {"type": "enabled", "budget_tokens": 3072}}

    def test_finish_reason_normalization(self) -> None:
        from turnstone.core.providers._anthropic import _normalize_finish_reason

        assert _normalize_finish_reason("end_turn") == "stop"
        assert _normalize_finish_reason("tool_use") == "tool_calls"
        assert _normalize_finish_reason("max_tokens") == "length"
        assert _normalize_finish_reason("other_reason") == "other_reason"

    @patch("turnstone.core.providers._anthropic._ensure_anthropic")
    def test_completion_basic(self, mock_ensure: MagicMock) -> None:
        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = "Hello world"

        response = MagicMock()
        response.content = [text_block]
        response.stop_reason = "end_turn"
        response.usage = MagicMock()
        response.usage.input_tokens = 10
        response.usage.output_tokens = 5

        client = MagicMock()
        client.messages.create.return_value = response

        result = self.provider.create_completion(
            client=client,
            model="claude-sonnet-4-20250514",
            messages=[{"role": "user", "content": "hi"}],
        )
        assert isinstance(result, CompletionResult)
        assert result.content == "Hello world"
        assert result.tool_calls is None
        assert result.finish_reason == "stop"

    @patch("turnstone.core.providers._anthropic._ensure_anthropic")
    def test_completion_with_tool_use(self, mock_ensure: MagicMock) -> None:
        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = "Let me read that."

        tool_block = MagicMock()
        tool_block.type = "tool_use"
        tool_block.id = "toolu_abc"
        tool_block.name = "read_file"
        tool_block.input = {"path": "foo.py"}

        response = MagicMock()
        response.content = [text_block, tool_block]
        response.stop_reason = "tool_use"
        response.usage = MagicMock()
        response.usage.input_tokens = 15
        response.usage.output_tokens = 20

        client = MagicMock()
        client.messages.create.return_value = response

        result = self.provider.create_completion(
            client=client,
            model="claude-sonnet-4-20250514",
            messages=[{"role": "user", "content": "read foo.py"}],
        )
        assert result.content == "Let me read that."
        assert result.finish_reason == "tool_calls"
        assert result.tool_calls is not None
        assert len(result.tool_calls) == 1
        tc = result.tool_calls[0]
        assert tc["id"] == "toolu_abc"
        assert tc["type"] == "function"
        assert tc["function"]["name"] == "read_file"
        assert json.loads(tc["function"]["arguments"]) == {"path": "foo.py"}

    @patch("turnstone.core.providers._anthropic._ensure_anthropic")
    def test_completion_usage(self, mock_ensure: MagicMock) -> None:
        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = "ok"

        response = MagicMock()
        response.content = [text_block]
        response.stop_reason = "end_turn"
        response.usage = MagicMock()
        response.usage.input_tokens = 100
        response.usage.output_tokens = 50

        client = MagicMock()
        client.messages.create.return_value = response

        result = self.provider.create_completion(
            client=client,
            model="claude-sonnet-4-20250514",
            messages=[{"role": "user", "content": "hi"}],
        )
        assert result.usage is not None
        assert result.usage.prompt_tokens == 100
        assert result.usage.completion_tokens == 50
        assert result.usage.total_tokens == 150

    @patch("turnstone.core.providers._anthropic._ensure_anthropic")
    def test_streaming_text_delta(self, mock_ensure: MagicMock) -> None:
        events = [
            _anthropic_event("content_block_delta", delta_type="text_delta", text="Hello"),
            _anthropic_event("content_block_delta", delta_type="text_delta", text=" world"),
        ]
        stream_ctx = MagicMock()
        stream_ctx.__enter__ = MagicMock(return_value=iter(events))
        stream_ctx.__exit__ = MagicMock(return_value=False)

        client = MagicMock()
        client.messages.stream.return_value = stream_ctx

        results = list(
            self.provider.create_streaming(
                client=client,
                model="claude-sonnet-4-20250514",
                messages=[{"role": "user", "content": "hi"}],
            )
        )
        assert len(results) == 2
        assert results[0].content_delta == "Hello"
        assert results[0].is_first is True
        assert results[1].content_delta == " world"
        assert results[1].is_first is False

    @patch("turnstone.core.providers._anthropic._ensure_anthropic")
    def test_streaming_thinking_delta(self, mock_ensure: MagicMock) -> None:
        events = [
            _anthropic_event(
                "content_block_delta",
                delta_type="thinking_delta",
                thinking="reasoning step 1",
            ),
            _anthropic_event(
                "content_block_delta",
                delta_type="thinking_delta",
                thinking="reasoning step 2",
            ),
        ]
        stream_ctx = MagicMock()
        stream_ctx.__enter__ = MagicMock(return_value=iter(events))
        stream_ctx.__exit__ = MagicMock(return_value=False)

        client = MagicMock()
        client.messages.stream.return_value = stream_ctx

        results = list(
            self.provider.create_streaming(
                client=client,
                model="claude-sonnet-4-20250514",
                messages=[{"role": "user", "content": "think"}],
            )
        )
        assert len(results) == 2
        assert results[0].reasoning_delta == "reasoning step 1"
        assert results[1].reasoning_delta == "reasoning step 2"

    @patch("turnstone.core.providers._anthropic._ensure_anthropic")
    def test_streaming_tool_use(self, mock_ensure: MagicMock) -> None:
        events = [
            _anthropic_event(
                "content_block_start",
                block_type="tool_use",
                block_id="toolu_123",
                block_name="read_file",
                index=0,
            ),
            _anthropic_event(
                "content_block_delta",
                delta_type="input_json_delta",
                partial_json='{"path":',
                index=0,
            ),
            _anthropic_event(
                "content_block_delta",
                delta_type="input_json_delta",
                partial_json='"foo.py"}',
                index=0,
            ),
        ]
        stream_ctx = MagicMock()
        stream_ctx.__enter__ = MagicMock(return_value=iter(events))
        stream_ctx.__exit__ = MagicMock(return_value=False)

        client = MagicMock()
        client.messages.stream.return_value = stream_ctx

        results = list(
            self.provider.create_streaming(
                client=client,
                model="claude-sonnet-4-20250514",
                messages=[{"role": "user", "content": "read a file"}],
            )
        )
        assert len(results) == 3
        # First chunk: content_block_start with tool id and name
        assert results[0].tool_call_deltas[0].id == "toolu_123"
        assert results[0].tool_call_deltas[0].name == "read_file"
        assert results[0].tool_call_deltas[0].index == 0
        # Subsequent chunks: argument fragments
        assert results[1].tool_call_deltas[0].arguments_delta == '{"path":'
        assert results[2].tool_call_deltas[0].arguments_delta == '"foo.py"}'

    @patch("turnstone.core.providers._anthropic._ensure_anthropic")
    def test_streaming_message_delta_usage(self, mock_ensure: MagicMock) -> None:
        events = [
            _anthropic_event("content_block_delta", delta_type="text_delta", text="Hi"),
            _anthropic_event(
                "message_delta",
                stop_reason="end_turn",
                usage_input_tokens=0,
                usage_output_tokens=12,
            ),
        ]
        stream_ctx = MagicMock()
        stream_ctx.__enter__ = MagicMock(return_value=iter(events))
        stream_ctx.__exit__ = MagicMock(return_value=False)

        client = MagicMock()
        client.messages.stream.return_value = stream_ctx

        results = list(
            self.provider.create_streaming(
                client=client,
                model="claude-sonnet-4-20250514",
                messages=[{"role": "user", "content": "hi"}],
            )
        )
        # The message_delta event should carry usage and finish_reason
        delta_chunks = [r for r in results if r.finish_reason is not None]
        assert len(delta_chunks) == 1
        assert delta_chunks[0].finish_reason == "stop"
        assert delta_chunks[0].usage is not None
        assert delta_chunks[0].usage.completion_tokens == 12

    @patch("turnstone.core.providers._anthropic._ensure_anthropic")
    def test_streaming_message_start_usage(self, mock_ensure: MagicMock) -> None:
        events = [
            _anthropic_event("message_start", usage_input_tokens=42),
            _anthropic_event("content_block_delta", delta_type="text_delta", text="Hi"),
        ]
        stream_ctx = MagicMock()
        stream_ctx.__enter__ = MagicMock(return_value=iter(events))
        stream_ctx.__exit__ = MagicMock(return_value=False)

        client = MagicMock()
        client.messages.stream.return_value = stream_ctx

        results = list(
            self.provider.create_streaming(
                client=client,
                model="claude-sonnet-4-20250514",
                messages=[{"role": "user", "content": "hi"}],
            )
        )
        # message_start with usage should be yielded
        start_chunks = [r for r in results if r.usage is not None and r.usage.prompt_tokens == 42]
        assert len(start_chunks) == 1
        assert start_chunks[0].usage is not None
        assert start_chunks[0].usage.prompt_tokens == 42

    def test_retryable_errors(self) -> None:
        errors = self.provider.retryable_error_names
        assert isinstance(errors, frozenset)
        assert "RateLimitError" in errors
        assert "APITimeoutError" in errors
        assert "APIConnectionError" in errors
        assert "InternalServerError" in errors
        assert "APIError" in errors
        assert "OverloadedError" in errors


# ===========================================================================
# TestAnthropicHelpers
# ===========================================================================


class TestAnthropicHelpers:
    """Tests for Anthropic module-level helper functions."""

    def test_merge_consecutive(self) -> None:
        from turnstone.core.providers._anthropic import _merge_consecutive

        messages = [
            {"role": "user", "content": "A"},
            {"role": "user", "content": "B"},
            {"role": "assistant", "content": "C"},
            {"role": "user", "content": "D"},
        ]
        merged = _merge_consecutive(messages)
        assert len(merged) == 3
        assert merged[0]["role"] == "user"
        assert merged[0]["content"] == [
            {"type": "text", "text": "A"},
            {"type": "text", "text": "B"},
        ]
        assert merged[1]["role"] == "assistant"
        assert merged[2]["role"] == "user"

    def test_merge_consecutive_empty(self) -> None:
        from turnstone.core.providers._anthropic import _merge_consecutive

        assert _merge_consecutive([]) == []

    def test_merge_consecutive_no_duplicates(self) -> None:
        from turnstone.core.providers._anthropic import _merge_consecutive

        messages = [
            {"role": "user", "content": "A"},
            {"role": "assistant", "content": "B"},
            {"role": "user", "content": "C"},
        ]
        merged = _merge_consecutive(messages)
        assert len(merged) == 3

    def test_to_blocks_string(self) -> None:
        from turnstone.core.providers._anthropic import _to_blocks

        result = _to_blocks("hello")
        assert result == [{"type": "text", "text": "hello"}]

    def test_to_blocks_list(self) -> None:
        from turnstone.core.providers._anthropic import _to_blocks

        blocks = [{"type": "text", "text": "already a block"}]
        result = _to_blocks(blocks)
        assert result == blocks

    def test_to_blocks_other(self) -> None:
        from turnstone.core.providers._anthropic import _to_blocks

        result = _to_blocks(42)
        assert result == [{"type": "text", "text": "42"}]

    def test_capabilities_lookup_exact(self) -> None:
        from turnstone.core.providers._anthropic import AnthropicProvider

        provider = AnthropicProvider()
        caps = provider.get_capabilities("claude-opus-4-6")
        assert caps.context_window == 200000
        assert caps.max_output_tokens == 128000
        assert caps.thinking_mode == "adaptive"
        assert caps.supports_effort is True

    def test_capabilities_lookup_prefix(self) -> None:
        from turnstone.core.providers._anthropic import AnthropicProvider

        provider = AnthropicProvider()
        # Prefix match: "claude-sonnet-4" matches dated variants
        caps = provider.get_capabilities("claude-sonnet-4-20260101")
        assert caps.context_window == 200000
        assert caps.token_param == "max_tokens"
        assert caps.thinking_mode == "manual"

    def test_capabilities_lookup_unknown(self) -> None:
        from turnstone.core.providers._anthropic import AnthropicProvider

        provider = AnthropicProvider()
        caps = provider.get_capabilities("unknown-model-xyz")
        # Falls back to default
        assert caps.context_window == 200000
        assert caps.thinking_mode == "manual"
        assert caps.token_param == "max_tokens"


# ===========================================================================
# TestProviderFactory
# ===========================================================================


class TestProviderFactory:
    """Tests for create_provider and create_client factory functions."""

    def test_create_provider_openai(self) -> None:
        from turnstone.core.providers import create_provider

        provider = create_provider("openai")
        assert isinstance(provider, OpenAIProvider)
        assert provider.provider_name == "openai"

    def test_create_provider_anthropic(self) -> None:
        from turnstone.core.providers import create_provider

        provider = create_provider("anthropic")
        assert provider.provider_name == "anthropic"

    def test_create_provider_unknown(self) -> None:
        from turnstone.core.providers import create_provider

        with pytest.raises(ValueError, match="Unknown provider"):
            create_provider("gemini")

    @patch("openai.OpenAI")
    def test_create_client_openai(self, mock_openai_cls: MagicMock) -> None:
        from turnstone.core.providers import create_client

        mock_openai_cls.return_value = MagicMock()
        client = create_client("openai", base_url="http://localhost:8000/v1", api_key="test-key")
        mock_openai_cls.assert_called_once_with(
            base_url="http://localhost:8000/v1", api_key="test-key"
        )
        assert client is mock_openai_cls.return_value

    def test_create_client_unknown(self) -> None:
        from turnstone.core.providers import create_client

        with pytest.raises(ValueError, match="Unknown provider"):
            create_client("gemini", base_url="http://x", api_key="k")

    def test_is_llm_provider(self) -> None:
        """Verify runtime_checkable protocol works with isinstance."""
        provider = OpenAIProvider()
        assert isinstance(provider, LLMProvider)

    def test_non_provider_not_instance(self) -> None:
        """A plain object should not satisfy LLMProvider protocol check."""

        class NotAProvider:
            pass

        assert not isinstance(NotAProvider(), LLMProvider)

    def test_create_provider_returns_singleton(self) -> None:
        from turnstone.core.providers import create_provider

        p1 = create_provider("openai")
        p2 = create_provider("openai")
        assert p1 is p2


# ===========================================================================
# TestDataclasses
# ===========================================================================


class TestDataclasses:
    """Tests for protocol dataclass construction and defaults."""

    def test_stream_chunk_defaults(self) -> None:
        sc = StreamChunk()
        assert sc.content_delta == ""
        assert sc.reasoning_delta == ""
        assert sc.tool_call_deltas == []
        assert sc.usage is None
        assert sc.finish_reason is None
        assert sc.is_first is False

    def test_tool_call_delta_defaults(self) -> None:
        tcd = ToolCallDelta(index=0)
        assert tcd.index == 0
        assert tcd.id == ""
        assert tcd.name == ""
        assert tcd.arguments_delta == ""

    def test_usage_info(self) -> None:
        u = UsageInfo(prompt_tokens=10, completion_tokens=5, total_tokens=15)
        assert u.prompt_tokens == 10
        assert u.completion_tokens == 5
        assert u.total_tokens == 15

    def test_completion_result_defaults(self) -> None:
        cr = CompletionResult(content="hello")
        assert cr.content == "hello"
        assert cr.tool_calls is None
        assert cr.finish_reason == "stop"
        assert cr.usage is None


# ===========================================================================
# TestParameterGating — model capability parameter gating
# ===========================================================================


class TestOpenAIParameterGating:
    """Verify _apply_model_params gates temperature and reasoning_effort correctly."""

    def setup_method(self) -> None:
        self.provider = OpenAIProvider()

    def test_unknown_model_no_reasoning_effort(self) -> None:
        """Unknown/local models should NOT receive top-level reasoning_effort."""
        caps = self.provider.get_capabilities("my-local-model")
        kwargs: dict[str, Any] = {}
        self.provider._apply_model_params(kwargs, caps, temperature=0.7, reasoning_effort="medium")
        assert "reasoning_effort" not in kwargs
        assert kwargs["temperature"] == 0.7

    def test_gpt5_no_temperature_has_reasoning_effort(self) -> None:
        """GPT-5 base: no temperature, reasoning_effort sent."""
        caps = self.provider.get_capabilities("gpt-5")
        kwargs: dict[str, Any] = {}
        self.provider._apply_model_params(kwargs, caps, temperature=0.7, reasoning_effort="high")
        assert "temperature" not in kwargs
        assert kwargs["reasoning_effort"] == "high"

    def test_gpt51_temperature_when_effort_none(self) -> None:
        """GPT-5.1: temperature only when reasoning_effort='none'."""
        caps = self.provider.get_capabilities("gpt-5.1")
        kwargs: dict[str, Any] = {}
        self.provider._apply_model_params(kwargs, caps, temperature=0.7, reasoning_effort="none")
        assert kwargs["temperature"] == 0.7
        assert "reasoning_effort" not in kwargs  # "none" is skipped

    def test_gpt51_no_temperature_when_reasoning_active(self) -> None:
        """GPT-5.1: no temperature when reasoning is active."""
        caps = self.provider.get_capabilities("gpt-5.1")
        kwargs: dict[str, Any] = {}
        self.provider._apply_model_params(kwargs, caps, temperature=0.7, reasoning_effort="high")
        assert "temperature" not in kwargs
        assert kwargs["reasoning_effort"] == "high"

    def test_o_series_no_temperature_no_reasoning_effort(self) -> None:
        """O-series: no temperature, no reasoning_effort."""
        caps = self.provider.get_capabilities("o3")
        kwargs: dict[str, Any] = {}
        self.provider._apply_model_params(kwargs, caps, temperature=0.7, reasoning_effort="medium")
        assert "temperature" not in kwargs
        assert "reasoning_effort" not in kwargs


class TestAnthropicReasoningNone:
    """Verify 'none' effort disables thinking for manual-thinking models."""

    def setup_method(self) -> None:
        from turnstone.core.providers._anthropic import AnthropicProvider

        self.provider = AnthropicProvider()

    def test_none_effort_disables_thinking(self) -> None:
        result = self.provider._reasoning_params("none", None, max_tokens=4096)
        assert result == {}

    def test_empty_effort_disables_thinking(self) -> None:
        result = self.provider._reasoning_params("", None, max_tokens=4096)
        assert result == {}

    def test_low_effort_enables_thinking(self) -> None:
        result = self.provider._reasoning_params("low", None, max_tokens=4096)
        assert "thinking" in result
        assert result["thinking"]["budget_tokens"] == 1024
