"""Unit tests for the ``model_turn`` plant-call primitive (#827).

The agent-path tests in ``test_session.py`` exercise ``model_turn`` through
``_run_agent`` (native-lane replay, blank-id gate, minted-id nesting); these
pin the module's own contract directly so the judges (phase 1b) and the
single-shot lanes (phase 2) can build on it without re-deriving semantics.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from turnstone.core.model_turn import (
    ModelLane,
    finalize_provider_blocks,
    maybe_attach_vllm_chat_reasoning,
    model_turn,
    resolve_lane,
    synth_reasoning_block,
)
from turnstone.core.providers._protocol import (
    CompletionResult,
    ModelCapabilities,
    UsageInfo,
)
from turnstone.core.trajectory import Role, ToolCall, Turn


class _FakeProvider:
    """Records every ``create_completion`` call; replays scripted results."""

    provider_name = "openai-compatible"

    def __init__(self, results: list[CompletionResult]) -> None:
        self.results = list(results)
        self.calls: list[dict[str, Any]] = []

    def get_capabilities(self, model: str) -> ModelCapabilities:
        return ModelCapabilities()

    def create_completion(self, **kwargs: Any) -> CompletionResult:
        self.calls.append(kwargs)
        return self.results.pop(0)


def _fake_registry(
    *,
    capabilities: dict[str, Any] | None = None,
    server_compat: dict[str, Any] | None = None,
    replay: bool = False,
) -> MagicMock:
    cfg = SimpleNamespace(
        capabilities=capabilities or {},
        server_compat=server_compat or {},
        replay_reasoning_to_model=replay,
    )
    reg = MagicMock()
    reg.get_config.return_value = cfg
    return reg


def _lane(provider: _FakeProvider, **kw: Any) -> ModelLane:
    return ModelLane(provider=provider, client=object(), model="m", **kw)


def test_model_turn_lowers_turns_and_threads_lane_config() -> None:
    caps = ModelCapabilities(max_output_tokens=1234)
    extra = {"chat_template_kwargs": {"enable_thinking": True}}
    provider = _FakeProvider([CompletionResult(content="hi")])
    lane = _lane(provider, capabilities=caps, extra_params=extra)

    result = model_turn(
        lane,
        [Turn.user("x")],
        tools=[{"type": "function", "function": {"name": "f", "parameters": {}}}],
        max_tokens=99,
        temperature=0.1,
        reasoning_effort="low",
    )

    (call,) = provider.calls
    assert call["messages"][0]["role"] == "user"
    assert call["messages"][0]["content"] == "x"
    assert call["capabilities"] is caps
    assert call["extra_params"] is extra
    assert call["max_tokens"] == 99
    assert call["temperature"] == 0.1
    assert call["reasoning_effort"] == "low"
    # No registry on the lane → the operator replay flag resolves False.
    assert call["replay_reasoning_to_model"] is False
    assert result.turn.role is Role.ASSISTANT
    assert result.content == "hi"
    assert result.finish_reason == "stop"


def test_model_turn_returns_usage_verbatim() -> None:
    usage = UsageInfo(prompt_tokens=9, completion_tokens=1, total_tokens=10)
    provider = _FakeProvider([CompletionResult(content="", usage=usage)])
    result = model_turn(_lane(provider), [Turn.user("x")])
    assert result.usage is usage


def test_mint_rewrites_mirror_records_map_and_native_keeps_original() -> None:
    provider = _FakeProvider(
        [
            CompletionResult(
                content="",
                tool_calls=[
                    {
                        "id": "call_0",
                        "type": "function",
                        "function": {"name": "f", "arguments": "{}"},
                    }
                ],
                provider_blocks=[{"type": "tool_use", "id": "call_0", "name": "f"}],
            )
        ]
    )
    wire_id_map: dict[str, str] = {}
    result = model_turn(
        _lane(provider),
        [Turn.user("x")],
        mint=lambda original: f"parent::r1s1::{original}",
        wire_id_map=wire_id_map,
    )

    # The mirror (execution view) and the Turn both carry the minted id …
    assert result.tool_calls[0]["id"] == "parent::r1s1::call_0"
    assert result.turn.tool_calls[0].id == "parent::r1s1::call_0"
    # … the map records the recovery path …
    assert wire_id_map == {"parent::r1s1::call_0": "call_0"}
    # … and the native block keeps the provider-original id verbatim (it may
    # sit under a reasoning signature and is never rewritten).
    assert result.turn.native is not None
    assert result.turn.native.blocks[0]["id"] == "call_0"
    assert result.turn.native.producer == "openai-compatible"


def test_restore_maps_minted_ids_back_on_the_wire() -> None:
    minted = "parent::r1s1::call_0"
    provider = _FakeProvider([CompletionResult(content="done")])
    turns = [
        Turn.user("go"),
        Turn.assistant("", tool_calls=(ToolCall(id=minted, name="f", arguments="{}"),)),
        Turn.tool(minted, "result"),
    ]

    model_turn(_lane(provider), turns, wire_id_map={minted: "call_0"})

    (call,) = provider.calls
    assistant = next(m for m in call["messages"] if m["role"] == "assistant")
    tool = next(m for m in call["messages"] if m["role"] == "tool")
    assert assistant["tool_calls"][0]["id"] == "call_0"
    assert tool["tool_call_id"] == "call_0"


def test_blank_ids_backfill_and_reduce_native_lane_to_reasoning_text() -> None:
    provider = _FakeProvider(
        [
            CompletionResult(
                content="",
                tool_calls=[{"id": "", "type": "function", "function": {"name": "f"}}],
                provider_blocks=[{"type": "tool_use", "id": "", "name": "f"}],
                reasoning="thought",
            )
        ]
    )
    result = model_turn(_lane(provider), [Turn.user("x")])

    # uuid back-fill reaches the mirror …
    assert result.tool_calls[0]["id"].startswith("call_")
    # … while the desynced native blocks are dropped down to the loose-text
    # reasoning synth (the blank-id mirror gate).
    assert result.turn.native is not None
    assert [b["type"] for b in result.turn.native.blocks] == ["reasoning_text"]
    assert result.turn.native.blocks[0]["text"] == "thought"


def test_orphan_client_tool_blocks_stripped_when_no_tool_calls() -> None:
    provider = _FakeProvider(
        [
            CompletionResult(
                content="truncated",
                tool_calls=None,
                provider_blocks=[{"type": "tool_use", "id": "x", "name": "f"}],
            )
        ]
    )
    result = model_turn(_lane(provider), [Turn.user("x")])
    # A tool_use with no mirrored call would replay with no matching
    # tool_result — the finalize gate strips it, leaving no lane at all.
    assert result.turn.native is None


def test_live_operator_flags_reresolve_per_call() -> None:
    registry = _fake_registry(replay=False)
    provider = _FakeProvider([CompletionResult(content="a"), CompletionResult(content="b")])
    lane = _lane(provider, alias="ali", registry=registry)

    model_turn(lane, [Turn.user("x")])
    # Operator flips the toggle mid-session (admin write → registry reload).
    registry.get_config.return_value.replay_reasoning_to_model = True
    model_turn(lane, [Turn.user("x")])

    first, second = provider.calls
    assert first["replay_reasoning_to_model"] is False
    assert second["replay_reasoning_to_model"] is True


def test_resolve_lane_respects_preresolved_values() -> None:
    provider = _FakeProvider([])
    caps = ModelCapabilities(max_output_tokens=7)
    lane = resolve_lane(provider, object(), "m", capabilities=caps, extra_params={"k": "v"})
    assert lane.capabilities is caps
    assert lane.extra_params == {"k": "v"}
    # Explicit None is a valid resolved value, distinct from "resolve for me".
    lane_none = resolve_lane(provider, object(), "m", capabilities=caps, extra_params=None)
    assert lane_none.extra_params is None


def test_resolve_lane_merges_registry_capability_overrides() -> None:
    provider = _FakeProvider([])
    registry = _fake_registry(capabilities={"max_output_tokens": 42, "not_a_field": 1})
    lane = resolve_lane(provider, object(), "m", alias="ali", registry=registry)
    assert lane.capabilities is not None
    assert lane.capabilities.max_output_tokens == 42


def test_vllm_attach_gates() -> None:
    from turnstone.core.providers._openai_chat import OpenAIChatCompletionsProvider

    msgs = [
        {"role": "user", "content": "q"},
        {
            "role": "assistant",
            "content": "a",
            "_provider_content": [{"type": "reasoning_text", "text": "cot"}],
        },
    ]
    # Non-Chat-Completions provider: untouched (identity).
    assert maybe_attach_vllm_chat_reasoning(msgs, _FakeProvider([]), None, "ali") is msgs  # type: ignore[arg-type]

    chat = OpenAIChatCompletionsProvider()
    # All three gates open → reasoning field attached.
    on = _fake_registry(server_compat={"server_type": "vllm"}, replay=True)
    out = maybe_attach_vllm_chat_reasoning(msgs, chat, on, "ali")
    assert out[1]["reasoning"] == "cot"
    # Operator flag off → untouched.
    off = _fake_registry(server_compat={"server_type": "vllm"}, replay=False)
    assert maybe_attach_vllm_chat_reasoning(msgs, chat, off, "ali") is msgs
    # Wrong server type → untouched.
    sglang = _fake_registry(server_compat={"server_type": "sglang"}, replay=True)
    assert maybe_attach_vllm_chat_reasoning(msgs, chat, sglang, "ali") is msgs


def test_synth_reasoning_block_appends_with_source_and_skips_native() -> None:
    registry = _fake_registry(server_compat={"server_type": "vllm"})
    fidelity = [{"type": "tool_calls", "raw": True}]
    out = synth_reasoning_block(fidelity, ["thought"], registry=registry, alias="ali")
    # Appends (Google fidelity blocks survive) and tags the source server.
    assert out[0] is fidelity[0]
    assert out[1] == {"type": "reasoning_text", "text": "thought", "source": "vllm"}
    # A native reasoning-bearing block suppresses synthesis (identity return).
    native = [{"type": "thinking", "thinking": "t", "signature": "s"}]
    assert synth_reasoning_block(native, ["thought"]) is native


def test_finalize_keeps_full_lane_with_tool_calls_and_clean_ids() -> None:
    blocks = [
        {"type": "thinking", "thinking": "t", "signature": "s"},
        {"type": "tool_use", "id": "toolu_1", "name": "f"},
    ]
    out = finalize_provider_blocks(blocks, [""], has_tool_calls=True)
    assert out == blocks
