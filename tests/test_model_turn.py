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

import pytest

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
    temperature: float | None = None,
) -> MagicMock:
    cfg = SimpleNamespace(
        capabilities=capabilities or {},
        server_compat=server_compat or {},
        replay_reasoning_to_model=replay,
        temperature=temperature,
    )
    reg = MagicMock()
    reg.get_config.return_value = cfg
    return reg


def _lane(provider: _FakeProvider, **kw: Any) -> ModelLane:
    return ModelLane(provider=provider, client=object(), model="m", **kw)


def _real_semantics_store(**stored: Any) -> SimpleNamespace:
    """A ConfigStore fake with the REAL ``get()`` semantics.

    A stored key returns its value; a never-stored key returns the
    SETTINGS registry default — which for the sampling keys IS the unset
    sentinel (``None`` / ``""``).  The old fakes returned ``None`` on any
    miss, which masked the default-on-miss collision the round-2 review
    caught: never fake a store rung more forgiving than the real one.
    """
    from turnstone.core.settings_registry import SETTINGS

    def _get(key: str, default: Any = ...) -> Any:
        if key in stored:
            return stored[key]
        if default is not ...:
            return default
        defn = SETTINGS.get(key)
        return defn.default if defn else None

    return SimpleNamespace(get=_get)


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


def test_blank_ids_repair_native_lane_pairwise() -> None:
    # Google-compat shape: blank id on BOTH the mirror and the raw fidelity
    # block.  The manufactured uuid lands in both (positional pairing), so
    # the thought_signature-bearing block SURVIVES instead of the turn
    # degrading to loose reasoning text — the unblock for the Gemini judge
    # evidence loop on blank-id compat responses.
    provider = _FakeProvider(
        [
            CompletionResult(
                content="",
                tool_calls=[
                    {"id": "", "type": "function", "function": {"name": "f", "arguments": "{}"}}
                ],
                provider_blocks=[
                    {
                        "id": "",
                        "type": "function",
                        "function": {"name": "f", "arguments": "{}"},
                        "thought_signature": "sig123",
                    }
                ],
                reasoning="thought",
            )
        ]
    )
    result = model_turn(_lane(provider), [Turn.user("x")])

    manufactured = result.tool_calls[0]["id"]
    assert manufactured.startswith("call_")
    assert result.turn.native is not None
    blocks = list(result.turn.native.blocks)
    # The fidelity block survives, id-agreeing with the mirror, signature
    # untouched; the loose reasoning still synthesizes alongside it.
    assert blocks[0]["id"] == manufactured
    assert blocks[0]["thought_signature"] == "sig123"
    assert blocks[-1]["type"] == "reasoning_text"


def test_blank_id_repair_never_rewrites_nonblank_ids() -> None:
    provider = _FakeProvider(
        [
            CompletionResult(
                content="",
                tool_calls=[
                    {"id": "call_7", "type": "function", "function": {"name": "a"}},
                    {"id": "", "type": "function", "function": {"name": "b"}},
                ],
                provider_blocks=[
                    {"id": "call_7", "type": "function", "function": {"name": "a"}},
                    {"id": "", "type": "function", "function": {"name": "b"}},
                ],
            )
        ]
    )
    result = model_turn(_lane(provider), [Turn.user("x")])
    assert result.turn.native is not None
    blocks = list(result.turn.native.blocks)
    # Provider-assigned id untouched (it may sit under a signature) …
    assert blocks[0]["id"] == "call_7"
    # … only the blank one was manufactured, agreeing with its mirror twin.
    assert blocks[1]["id"] == result.tool_calls[1]["id"]
    assert blocks[1]["id"].startswith("call_")


def test_blank_id_pairing_mismatch_falls_back_to_reasoning_text_drop() -> None:
    # Two mirror calls but only one client block: no trustworthy pairing —
    # the total drop rule (the #825-converged fallback) keeps only the
    # loose-text reasoning synth.
    provider = _FakeProvider(
        [
            CompletionResult(
                content="",
                tool_calls=[
                    {"id": "", "type": "function", "function": {"name": "a"}},
                    {"id": "", "type": "function", "function": {"name": "b"}},
                ],
                provider_blocks=[{"type": "function", "id": "", "function": {"name": "a"}}],
                reasoning="thought",
            )
        ]
    )
    result = model_turn(_lane(provider), [Turn.user("x")])
    assert result.tool_calls[0]["id"].startswith("call_")
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


def test_mint_without_wire_id_map_raises() -> None:
    provider = _FakeProvider([])
    with pytest.raises(ValueError, match="wire_id_map"):
        model_turn(_lane(provider), [Turn.user("x")], mint=lambda o: f"p::{o}")
    # Nothing reached the provider — the guard fires before lowering.
    assert provider.calls == []


def test_temperature_inherits_lane_value_when_caller_omits() -> None:
    provider = _FakeProvider([CompletionResult(content="")])
    lane = _lane(provider, temperature=1.3)
    model_turn(lane, [Turn.user("x")])
    assert provider.calls[0]["temperature"] == 1.3


def test_temperature_caller_value_wins_over_lane() -> None:
    provider = _FakeProvider([CompletionResult(content="")])
    lane = _lane(provider, temperature=1.3)
    model_turn(lane, [Turn.user("x")], temperature=0.9)
    assert provider.calls[0]["temperature"] == 0.9


def test_temperature_unresolved_passes_none_and_wire_omits_it() -> None:
    # No caller value, no lane value → model_turn passes temperature=None,
    # and the PROVIDER layer omits the field from the wire so the server
    # default applies (house rule: code never pins one).  Both halves are
    # pinned: a Python-signature default of 0.5 anywhere on this path is a
    # hidden universal pin — the exact bug the second xhigh review caught.
    provider = _FakeProvider([CompletionResult(content="")])
    model_turn(_lane(provider), [Turn.user("x")])
    assert provider.calls[0]["temperature"] is None

    from turnstone.core.providers._openai_common import apply_temperature

    kwargs: dict[str, Any] = {}
    apply_temperature(kwargs, ModelCapabilities(), None, "medium")
    assert "temperature" not in kwargs  # None never reaches the wire
    apply_temperature(kwargs, ModelCapabilities(), 1.0, "medium")
    assert kwargs["temperature"] == 1.0  # a real value still does


def test_resolve_lane_global_config_store_rung() -> None:
    # The global rung fires only when the operator actually STORED a
    # value; the registry default is the unset sentinel (None), so an
    # untouched install resolves None → the wire omits the field.
    provider = _FakeProvider([])
    registry = _fake_registry(temperature=None)
    store = _real_semantics_store(**{"model.temperature": 1.0})
    lane = resolve_lane(provider, object(), "m", alias="ali", registry=registry, config_store=store)
    assert lane.temperature == 1.0
    # The per-model value wins over the global rung.
    registry2 = _fake_registry(temperature=0.3)
    lane2 = resolve_lane(
        provider, object(), "m", alias="ali", registry=registry2, config_store=store
    )
    assert lane2.temperature == 0.3
    # Never-stored global → None (the round-2 headline: ConfigStore.get
    # must NOT manufacture a wire value on a miss).
    lane3 = resolve_lane(
        provider,
        object(),
        "m",
        alias="ali",
        registry=_fake_registry(temperature=None),
        config_store=_real_semantics_store(),
    )
    assert lane3.temperature is None


def test_resolve_lane_reasoning_effort_operator_rungs() -> None:
    # The lane carries the OPERATOR rungs only: per-model config → stored
    # global setting → None.  The in-code model definition (caps default)
    # applies at the model_turn call, so an operator-silent lane stays
    # None — the assignment scheme's "if not set, we don't send it".
    provider = _FakeProvider([])
    # Per-model config wins.
    reg = _fake_registry()
    reg.get_config.return_value.reasoning_effort = "high"
    lane = resolve_lane(provider, object(), "m", alias="ali", registry=reg)
    assert lane.reasoning_effort == "high"
    # Stored global setting rung.
    reg2 = _fake_registry()
    reg2.get_config.return_value.reasoning_effort = None
    store = _real_semantics_store(**{"model.reasoning_effort": "low"})
    lane2 = resolve_lane(provider, object(), "m", alias="ali", registry=reg2, config_store=store)
    assert lane2.reasoning_effort == "low"
    # Empty string at any rung is the unset sentinel (a valid settings
    # choice meaning fall through) — with a never-stored global (registry
    # default IS "") the lane stays operator-silent.
    reg3 = _fake_registry()
    reg3.get_config.return_value.reasoning_effort = ""
    lane3 = resolve_lane(
        provider, object(), "m", alias="ali", registry=reg3, config_store=_real_semantics_store()
    )
    assert lane3.reasoning_effort is None
    # Bare lane (no registry, no store): None.
    assert resolve_lane(provider, object(), "m").reasoning_effort is None


def test_model_turn_effort_lower_rungs() -> None:
    # Below the lane's operator rungs, model_turn applies: caller
    # request-shaped default → in-code model definition (caps) → None
    # (wire omission — no hidden "medium" constant anywhere).
    # Bare hand-built lane: nothing anywhere → the provider receives None.
    provider = _FakeProvider([CompletionResult(content="")])
    model_turn(_lane(provider), [Turn.user("x")])
    assert provider.calls[0]["reasoning_effort"] is None

    # In-code model definition rung: a declared caps default applies.
    caps = ModelCapabilities(default_reasoning_effort="high")
    provider2 = _FakeProvider([CompletionResult(content="")])
    model_turn(_lane(provider2, capabilities=caps), [Turn.user("x")])
    assert provider2.calls[0]["reasoning_effort"] == "high"

    # A caller request-shaped default (budget-coupled lanes: utility,
    # output guard) beats the model-generic caps default…
    provider3 = _FakeProvider([CompletionResult(content="")])
    model_turn(
        _lane(provider3, capabilities=caps), [Turn.user("x")], default_reasoning_effort="low"
    )
    assert provider3.calls[0]["reasoning_effort"] == "low"

    # …loses to an operator value on the lane…
    provider4 = _FakeProvider([CompletionResult(content="")])
    model_turn(
        _lane(provider4, capabilities=caps, reasoning_effort="xhigh"),
        [Turn.user("x")],
        default_reasoning_effort="low",
    )
    assert provider4.calls[0]["reasoning_effort"] == "xhigh"

    # …and to an explicit relay (the "none" knob stays distinct from unset).
    provider5 = _FakeProvider([CompletionResult(content="")])
    model_turn(
        _lane(provider5, capabilities=caps),
        [Turn.user("x")],
        reasoning_effort="none",
        default_reasoning_effort="low",
    )
    assert provider5.calls[0]["reasoning_effort"] == "none"


def test_model_turn_fetches_config_once_per_call() -> None:
    # ONE get_config per plant call feeds both live flags (replay + vLLM
    # attach) — a hot-reload between them cannot mix config generations
    # within a single request.
    registry = _fake_registry(replay=True)
    provider = _FakeProvider([CompletionResult(content="")])
    lane = _lane(provider, alias="ali", registry=registry)
    model_turn(lane, [Turn.user("x")])
    assert registry.get_config.call_count == 1


def test_resolve_lane_inherits_config_temperature() -> None:
    provider = _FakeProvider([])
    registry = _fake_registry(temperature=0.7)
    lane = resolve_lane(provider, object(), "m", alias="ali", registry=registry)
    assert lane.temperature == 0.7
    # Exactly ONE config fetch feeds caps + extra_params + temperature —
    # no cross-generation mixing on a registry hot-reload.
    assert registry.get_config.call_count == 1


def test_resolve_lane_survives_get_config_raise() -> None:
    provider = _FakeProvider([])
    registry = MagicMock()
    registry.get_config.side_effect = ValueError("Unknown model alias")
    lane = resolve_lane(provider, object(), "m", alias="gone", registry=registry)
    # Every facet degrades to its miss behavior instead of raising into a
    # caller's constructor (the judge alias-resolution abort case).
    assert lane.capabilities is not None
    assert lane.extra_params is None
    assert lane.temperature is None


def test_resolve_capabilities_survives_get_config_raise() -> None:
    from turnstone.core.model_turn import resolve_capabilities

    provider = _FakeProvider([])
    registry = MagicMock()
    registry.get_config.side_effect = KeyError("gone")
    caps = resolve_capabilities(provider, "m", "gone", registry)
    assert caps == ModelCapabilities()
