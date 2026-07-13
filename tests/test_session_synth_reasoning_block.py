"""Tests for the synthetic ``reasoning_text`` block stamping (Phase 3 path 3).

Path 3 covers OpenAI Chat Completions endpoints — vLLM with
``--reasoning-parser``, llama.cpp with ``reasoning_format``, Gemini's
``/v1beta/openai/`` endpoint, and any other server that surfaces
``delta.reasoning_content`` Pydantic extras.  These have no native
provider_blocks shape on the wire, so the captured reasoning text is
stamped onto ``_provider_content`` as a synthetic
``{type: "reasoning_text"}`` block at the end of the turn by
``model_turn.synth_reasoning_block`` — the one synthesizer every lane
runs (the main loop reaches it through ``ChatSession._stream_response``
→ ``_finalize_provider_blocks``; agents and judges through
``model_turn``).

These tests pin:
1. The synthesizer fires only when no native blocks were emitted AND
   reasoning was captured (Anthropic + OpenAI Responses bypass it).
2. ``source`` field is tagged with the active model's server_type
   (informational; pulled from ``server_compat.server_type``).
3. ``OpenAIChatCompletionsProvider.extract_reasoning_text`` round-trips
   the synthetic block on history rehydration.
4. The synthetic shape is NOT in ``ANTHROPIC_VALID_BLOCK_TYPES`` so
   cross-model resumption (local-model → Anthropic) falls through
   cleanly to the text+tool_calls rebuild path.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from tests._session_helpers import make_session as _make_session
from turnstone.core.model_turn import _server_type_of, synth_reasoning_block
from turnstone.core.providers._anthropic import (
    ANTHROPIC_VALID_BLOCK_TYPES,
    AnthropicProvider,
)
from turnstone.core.providers._openai_chat import OpenAIChatCompletionsProvider


class TestSynthReasoningBlock:
    """Direct unit tests for ``model_turn.synth_reasoning_block`` — the one
    synthesizer every lane runs (main loop via the session's finalize
    wrapper; agents and judges via ``model_turn``)."""

    def test_no_synth_when_provider_blocks_present(self) -> None:
        # Anthropic / OpenAI Responses path — native blocks already
        # carry the reasoning, no synth needed.
        existing = [{"type": "thinking", "thinking": "x"}]
        out = synth_reasoning_block(existing, ["should not be added"])
        assert out is existing

    def test_no_synth_when_reasoning_parts_empty(self) -> None:
        assert synth_reasoning_block([], []) == []

    def test_no_synth_when_reasoning_parts_only_whitespace(self) -> None:
        assert synth_reasoning_block([], ["   ", "\n\t"]) == []

    def test_synth_creates_reasoning_text_block(self) -> None:
        out = synth_reasoning_block([], ["thought ", "process"])
        assert len(out) == 1
        assert out[0]["type"] == "reasoning_text"
        assert out[0]["text"] == "thought process"

    def test_synth_omits_source_when_no_server_type(self) -> None:
        # No registry / no server_compat → source field omitted.
        out = synth_reasoning_block([], ["text"])
        assert "source" not in out[0]

    def test_synth_includes_source_when_server_type_resolvable(self) -> None:
        registry = SimpleNamespace(
            get_config=lambda alias: SimpleNamespace(
                capabilities={},
                server_compat={"server_type": "vllm"},
            )
        )
        out = synth_reasoning_block([], ["text"], registry=registry, alias="qwen3-32b")
        assert out[0]["source"] == "vllm"

    def test_synth_handles_registry_exception(self) -> None:
        # The defensive config fetch degrades to None on any lookup error
        # — synth still fires but omits the source field.
        class BrokenRegistry:
            def get_config(self, alias: str) -> Any:
                raise KeyError(alias)

        out = synth_reasoning_block([], ["text"], registry=BrokenRegistry(), alias="missing")
        assert out[0]["text"] == "text"
        assert "source" not in out[0]

    def test_synth_appends_when_provider_blocks_are_non_reasoning(self) -> None:
        # GoogleProvider attaches raw tool_call dicts as provider_blocks
        # on the finish chunk (for thought_signature round-trip).  When
        # the same turn streamed reasoning_delta (Gemini's reasoning_
        # content extra), the synthesizer must APPEND the synthetic
        # reasoning block rather than skip synthesis — otherwise the
        # reasoning text is shown live but lost on page reload.
        existing = [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "search", "arguments": "{}"},
                "thought_signature": "sig123",
            }
        ]
        out = synth_reasoning_block(existing, ["I should search"])
        assert len(out) == 2
        assert out[0] is existing[0]  # tool_call fidelity block survives intact
        assert out[1]["type"] == "reasoning_text"
        assert out[1]["text"] == "I should search"

    def test_no_synth_when_openai_responses_reasoning_already_present(self) -> None:
        # OpenAI Responses native reasoning item — synth must NOT fire
        # even though provider_blocks contains ALSO non-reasoning items
        # (e.g. message blocks).  The reasoning-bearing block satisfies
        # the persistence contract on its own.
        existing = [
            {"type": "reasoning", "summary": [{"text": "openai reasoning"}]},
            {"type": "message", "role": "assistant", "content": "answer"},
        ]
        out = synth_reasoning_block(existing, ["live reasoning text"])
        assert out is existing

    def test_no_synth_when_non_reasoning_blocks_but_reasoning_parts_empty(self) -> None:
        # Google tool_calls with no reasoning streamed — return as-is.
        existing = [
            {"id": "call_1", "type": "function", "function": {"name": "f", "arguments": "{}"}}
        ]
        out = synth_reasoning_block(existing, [])
        assert out is existing


class TestSyntheticBlockShapeContract:
    """The synthetic block shape MUST stay outside Anthropic's valid
    block types so cross-model resumption falls through cleanly."""

    def test_reasoning_text_not_in_anthropic_valid_types(self) -> None:
        # If this assertion ever fails, the cross-model resumption
        # safety story breaks: a synthetic block from a local-model
        # session would reach Anthropic's wire as a malformed block.
        assert "reasoning_text" not in ANTHROPIC_VALID_BLOCK_TYPES

    def test_synthetic_block_falls_through_anthropic_shape_filter(self) -> None:
        # Cross-model resumption regression: turn 1 was on a local
        # model (synthetic block stamped), then the operator switched
        # to Anthropic. The shape filter must reject the synthetic
        # block and fall through to text+tool_calls rebuild.
        provider = AnthropicProvider()
        msg = {
            "role": "assistant",
            "content": "spoken answer",
            "_provider_content": [
                {"type": "reasoning_text", "text": "synth thought", "source": "vllm"},
            ],
        }
        _, converted = provider._convert_messages([msg])
        assistant = next(m for m in converted if m["role"] == "assistant")
        block_types = [b.get("type") for b in assistant["content"] if isinstance(b, dict)]
        # Foreign block did NOT reach Anthropic's wire.  Rebuilt from
        # text only.
        assert "reasoning_text" not in block_types
        assert assistant["content"] == [{"type": "text", "text": "spoken answer"}]


class TestOpenAIChatExtractReasoningText:
    """``OpenAIChatCompletionsProvider.extract_reasoning_text`` reads
    the synthetic block back out for UI rehydration."""

    def test_reads_synthetic_reasoning_text_block(self) -> None:
        provider = OpenAIChatCompletionsProvider()
        blocks = [{"type": "reasoning_text", "text": "captured thought"}]
        assert provider.extract_reasoning_text(blocks) == "captured thought"

    def test_concatenates_multiple_blocks(self) -> None:
        provider = OpenAIChatCompletionsProvider()
        blocks = [
            {"type": "reasoning_text", "text": "first"},
            {"type": "reasoning_text", "text": "second"},
        ]
        assert provider.extract_reasoning_text(blocks) == "first\nsecond"

    def test_skips_other_block_types(self) -> None:
        provider = OpenAIChatCompletionsProvider()
        blocks = [
            {"type": "thinking", "thinking": "anth"},
            {"type": "reasoning", "summary": [{"text": "openai"}]},
            {"type": "reasoning_text", "text": "chat"},
        ]
        assert provider.extract_reasoning_text(blocks) == "chat"

    def test_handles_empty_text_field(self) -> None:
        provider = OpenAIChatCompletionsProvider()
        blocks = [
            {"type": "reasoning_text", "text": ""},
            {"type": "reasoning_text", "text": "kept"},
        ]
        assert provider.extract_reasoning_text(blocks) == "kept"

    def test_handles_missing_text_field(self) -> None:
        provider = OpenAIChatCompletionsProvider()
        blocks = [
            {"type": "reasoning_text"},  # no text
            {"type": "reasoning_text", "text": "kept"},
        ]
        assert provider.extract_reasoning_text(blocks) == "kept"

    def test_returns_empty_for_no_synth_blocks(self) -> None:
        provider = OpenAIChatCompletionsProvider()
        blocks = [{"type": "thinking", "thinking": "x"}]
        assert provider.extract_reasoning_text(blocks) == ""


class TestStreamResponseSynthBlockIntegration:
    """Integration test: drives a fake reasoning-emitting stream
    through ``ChatSession._stream_response`` and asserts the
    synthesizer wires up correctly.  Pins the call site at
    ``session.py`` (where ``_maybe_synth_reasoning_block`` is invoked
    on the assembled provider_blocks before stamping ``_provider_content``)
    — without this, a future refactor that drops the synthesizer call
    would silently break path-3 capture (vLLM/llama.cpp/Gemini-compat
    reasoning would be visible live but invisible on history reload).
    """

    def _make_stream(self, content: str, reasoning: str) -> Any:
        """Build an iterator of StreamChunks that mimic a path-3
        capture (reasoning_delta chunks, content chunks, no
        provider_blocks emitted).
        """
        from turnstone.core.providers._protocol import StreamChunk, UsageInfo

        chunks = []
        # Reasoning first (matches live SSE order).
        if reasoning:
            chunks.append(StreamChunk(reasoning_delta=reasoning, is_first=True))
        # Content next.
        if content:
            chunks.append(
                StreamChunk(
                    content_delta=content,
                    is_first=not reasoning,
                )
            )
        # Final chunk with finish_reason + usage.
        chunks.append(
            StreamChunk(
                finish_reason="stop",
                usage=UsageInfo(prompt_tokens=10, completion_tokens=20, total_tokens=30),
            )
        )
        return iter(chunks)

    def test_stream_response_stamps_synth_block_when_path3_reasoning_captured(
        self,
    ) -> None:
        """Drive a fake stream emitting reasoning_delta chunks (no
        native provider_blocks) through ``_stream_response``; assert
        the resulting assistant_msg carries a synthetic reasoning_text
        block stamped onto ``_provider_content``."""
        session = _make_session()
        # No registry → source field omitted from synth block.
        stream = self._make_stream(content="Final answer.", reasoning="path-3 reasoning")
        msg = session._stream_response(stream)
        assert msg["role"] == "assistant"
        assert msg["content"] == "Final answer."
        # Synthetic block should be stamped onto _provider_content.
        provider_content = msg.get("_provider_content")
        assert isinstance(provider_content, list)
        assert len(provider_content) == 1
        assert provider_content[0]["type"] == "reasoning_text"
        assert provider_content[0]["text"] == "path-3 reasoning"

    def test_stream_response_no_synth_when_no_reasoning_captured(self) -> None:
        """Stream emits only content (no reasoning_delta).  No synth
        block stamped — _provider_content key absent on assistant_msg."""
        session = _make_session()
        stream = self._make_stream(content="just content", reasoning="")
        msg = session._stream_response(stream)
        assert msg["content"] == "just content"
        # No synth block (and no native blocks either) → key absent.
        assert "_provider_content" not in msg

    def test_stream_response_synth_block_carries_source_when_server_type_resolvable(
        self,
    ) -> None:
        """When the active model has server_compat.server_type set,
        the synth block carries it as the ``source`` field."""
        session = _make_session()
        session._registry = SimpleNamespace(
            get_config=lambda alias: SimpleNamespace(
                capabilities={},
                server_compat={"server_type": "vllm"},
            )
        )
        session._model_alias = "qwen3-32b"
        stream = self._make_stream(content="answer", reasoning="reasoning text")
        msg = session._stream_response(stream)
        provider_content = msg.get("_provider_content")
        assert isinstance(provider_content, list)
        assert provider_content[0]["source"] == "vllm"


class TestServerTypeOf:
    """Direct unit tests for ``model_turn._server_type_of``, the one
    reader of ``server_compat.server_type`` (the Phase 5 vLLM gate and
    the synth-block source tagging both go through it)."""

    def test_reads_server_type_when_present(self) -> None:
        # Mirrors production ModelConfig shape: server_compat lives at
        # the top-level dataclass field, NOT inside capabilities.  Both
        # model_registry loader paths pop("server_compat") out of caps
        # before construction (see model_registry.py:401, 485).
        cfg = SimpleNamespace(
            capabilities={},
            server_compat={"server_type": "llama.cpp"},
        )
        assert _server_type_of(cfg) == "llama.cpp"

    def test_returns_empty_when_server_compat_missing(self) -> None:
        cfg = SimpleNamespace(
            capabilities={"context_window": 32768},
            server_compat={},
        )
        assert _server_type_of(cfg) == ""

    def test_returns_empty_on_non_dict_server_compat(self) -> None:
        assert _server_type_of(SimpleNamespace(server_compat=None)) == ""
        assert _server_type_of(SimpleNamespace()) == ""


class TestFinalizeProviderBlocks:
    """Direct unit tests for the shared native-lane builder
    ``ChatSession._finalize_provider_blocks`` — in particular the
    ``had_blank_ids`` gate (a uuid back-fill reaches only the tool_calls
    mirror, so blocks that would replay the blank id must be dropped while
    the reasoning lane survives)."""

    def test_passthrough_without_blank_ids(self) -> None:
        session = _make_session()
        blocks = [
            {"type": "thinking", "thinking": "x", "signature": "s"},
            {"type": "tool_use", "id": "toolu_1", "name": "f", "input": {}},
        ]
        out = session._finalize_provider_blocks(blocks, [], has_tool_calls=True)
        assert out is blocks

    def test_no_tool_calls_strips_orphan_client_blocks(self) -> None:
        session = _make_session()
        blocks = [
            {"type": "thinking", "thinking": "x", "signature": "s"},
            {"type": "tool_use", "id": "toolu_1", "name": "f", "input": {}},
        ]
        out = session._finalize_provider_blocks(blocks, [], has_tool_calls=False)
        assert [b["type"] for b in out] == ["thinking"]

    def test_blank_ids_drop_messages_shaped_lane_entirely(self) -> None:
        # Anthropic-shaped lane with a blank-id tool_use: only reasoning_text
        # may survive a blank-id turn, so the whole Messages-shaped lane goes
        # — on that translator a surviving native lane REPLACES the rebuilt
        # content, so a lane missing its tool_use would orphan the mirror's
        # calls.
        session = _make_session()
        blocks = [
            {"type": "thinking", "thinking": "x", "signature": "s"},
            {"type": "text", "text": "using f"},
            {"type": "tool_use", "id": "", "name": "f", "input": {}},
        ]
        out = session._finalize_provider_blocks(blocks, [], has_tool_calls=True, had_blank_ids=True)
        assert out == []

    def test_blank_ids_drop_asymmetric_thinking_lane_without_tool_blocks(self) -> None:
        # Asymmetric capture (thinking/text present, tool_use absent, mirror
        # blank-id): the rule is total — no client block needs to be present
        # for the Messages-shaped lane to be dropped on a blank-id turn.
        session = _make_session()
        blocks = [
            {"type": "thinking", "thinking": "x", "signature": "s"},
            {"type": "text", "text": "t"},
        ]
        out = session._finalize_provider_blocks(blocks, [], has_tool_calls=True, had_blank_ids=True)
        assert out == []

    def test_blank_ids_drop_responses_reasoning_items(self) -> None:
        # Responses reasoning items pair with their original sibling items;
        # on a blank-id turn the function_call siblings are rebuilt from the
        # back-filled mirror, so the reasoning items must go too.
        session = _make_session()
        blocks = [
            {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "enc"},
            {"type": "function_call", "call_id": "", "name": "f", "arguments": "{}"},
        ]
        out = session._finalize_provider_blocks(blocks, [], has_tool_calls=True, had_blank_ids=True)
        assert out == []

    def test_blank_ids_keep_only_reasoning_text(self) -> None:
        # Google-shaped lane: the raw function dict (blank id) is dropped;
        # the synthesized reasoning_text block survives — it carries no id
        # and is shape-invalid on the Messages translator by design, and the
        # Google swap simply finds no function blocks and keeps the
        # sanitized mirror.
        session = _make_session()
        blocks = [
            {"id": "", "type": "function", "function": {"name": "f", "arguments": "{}"}},
        ]
        out = session._finalize_provider_blocks(
            blocks, ["thinking text"], has_tool_calls=True, had_blank_ids=True
        )
        assert [b["type"] for b in out] == ["reasoning_text"]
        assert out[0]["text"] == "thinking text"

    def test_blank_ids_without_client_blocks_keep_the_lane(self) -> None:
        # llama.cpp / older vLLM: blank tool ids AND loose reasoning text,
        # but no client tool blocks at all — nothing can desync, so the
        # synthesized reasoning lane must be kept (the over-drop case).
        session = _make_session()
        out = session._finalize_provider_blocks(
            [], ["step by step"], has_tool_calls=True, had_blank_ids=True
        )
        assert [b["type"] for b in out] == ["reasoning_text"]
