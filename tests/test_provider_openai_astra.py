"""Astra's Turn IR, lowering, and real OpenAI SDK boundary (offline)."""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx2
import openai
import pytest

from turnstone.core.lowering import (
    drop_empty_user_turns,
    fold_system_turns,
    repair_wire_messages,
)
from turnstone.core.model_turn import ModelLane, model_turn
from turnstone.core.providers import create_provider, list_known_models, lookup_model_capabilities
from turnstone.core.providers._openai_responses import OpenAIResponsesProvider
from turnstone.core.trajectory import Turn


@pytest.fixture(params=[False, True], ids=["terminal", "streamed"])
def sdk_boundary(request):
    requests = []
    outputs = []
    streamed = request.param

    def handle(request):
        assert request.url.path == "/v1/responses"
        requests.append(json.loads(request.content))
        output = (
            outputs.pop(0)
            if outputs
            else [
                {
                    "type": "message",
                    "id": "msg_test",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": "ok", "annotations": []}],
                }
            ]
        )
        events = []
        if streamed:
            for index, item in enumerate(output):
                if item["type"] == "function_call":
                    events.extend(
                        [
                            {
                                "type": "response.output_item.added",
                                "output_index": index,
                                "item": {**item, "arguments": "", "status": "in_progress"},
                            },
                            {
                                "type": "response.function_call_arguments.delta",
                                "item_id": item["id"],
                                "output_index": index,
                                "delta": item["arguments"],
                            },
                        ]
                    )
                elif item["type"] == "message":
                    for content_index, part in enumerate(item["content"]):
                        events.append(
                            {
                                "type": "response.output_text.delta",
                                "item_id": item["id"],
                                "output_index": index,
                                "content_index": content_index,
                                "delta": part["text"],
                                "logprobs": [],
                            }
                        )
                events.append(
                    {"type": "response.output_item.done", "output_index": index, "item": item}
                )
        events.append(
            {
                "type": "response.completed",
                "response": {
                    "id": "resp_test",
                    "object": "response",
                    "created_at": 0,
                    "model": "gpt-6-astra",
                    "status": "completed",
                    "output": output,
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 10,
                        "total_tokens": 110,
                        "input_tokens_details": {"cached_tokens": 30, "cache_write_tokens": 70},
                    },
                },
            }
        )
        for index, event in enumerate(events):
            event["sequence_number"] = index
        return httpx2.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content="".join("data: " + json.dumps(event) + "\n\n" for event in events).encode(),
        )

    with openai.OpenAI(
        api_key="test-only",
        base_url="https://api.example.com/v1",
        max_retries=0,
        http_client=httpx2.Client(transport=httpx2.MockTransport(handle)),
    ) as client:
        yield client, requests, outputs


def _prepare(messages, lane):
    return repair_wire_messages(
        drop_empty_user_turns(
            fold_system_turns(
                messages,
                supports_mid_conversation_system=lane.capabilities.supports_mid_conversation_system,
                nonce="testnonce",
            )
        )
    )


@pytest.mark.parametrize(
    "effort", [None, "none", "minimal", "low", "medium", "high", "xhigh", "max"]
)
def test_sampling_on_real_responses_sdk(sdk_boundary, effort):
    client, requests, _ = sdk_boundary
    provider = create_provider("openai", api_surface="chat")
    caps = provider.get_capabilities("gpt-6-astra")
    lane = ModelLane(provider, client, "gpt-6-astra", capabilities=caps)

    result = model_turn(lane, [Turn.user("Hello")], temperature=0.7, reasoning_effort=effort)

    sent = requests[0]
    assert result.turn.text == "ok"
    assert not {"temperature", "top_p", "top_logprobs", "reasoning_effort"} & sent.keys()
    if effort in (None, "none"):
        assert "reasoning" not in sent
    else:
        assert sent["reasoning"] == {"effort": "low" if effort == "minimal" else effort}
    assert sent["store"] is False
    assert sent["prompt_cache_options"] == {"ttl": "30m"}
    assert "prompt_cache_retention" not in sent


def test_catalog_and_compatible_lane_isolation():
    assert "gpt-6-astra" in list_known_models("openai")
    caps = lookup_model_capabilities(provider="openai", model="gpt-6-astra")
    assert caps is not None
    assert caps["context_window"] == 1050000
    assert caps["max_output_tokens"] == 128000
    assert caps["supports_vision"] and caps["supports_pdf"] and caps["supports_tool_search"]
    assert caps["server_parses_reasoning"]
    assert lookup_model_capabilities(provider="openai", model="gpt-6-astra-test-snapshot") == caps
    for surface in ("chat", "responses"):
        local = create_provider("openai-compatible", api_surface=surface)
        local_caps = local.get_capabilities("gpt-6-astra")
        assert local_caps.supports_temperature
        assert not local_caps.supports_mid_conversation_system
        assert local_caps.reasoning_effort_values == ()


def test_tool_continuation_preserves_reasoning_and_inline_instructions(sdk_boundary):
    client, requests, outputs = sdk_boundary
    provider = create_provider("openai")
    caps = replace(provider.get_capabilities("gpt-6-astra"), verbosity="low", reasoning_mode="pro")
    cfg = SimpleNamespace(capabilities={}, server_compat={}, replay_reasoning_to_model=True)
    registry = MagicMock()
    registry.get_config.return_value = cfg
    lane = ModelLane(
        provider, client, "gpt-6-astra", alias="astra", capabilities=caps, registry=registry
    )
    tools = [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}]
    outputs.append(
        [
            {"type": "reasoning", "id": "rs_test", "summary": [], "encrypted_content": "opaque"},
            {
                "type": "function_call",
                "id": "fc_test",
                "call_id": "call_test",
                "name": "lookup",
                "arguments": "{}",
                "status": "completed",
            },
        ]
    )
    turns = [Turn.system("Base instructions"), Turn.user("Look it up")]
    first = model_turn(lane, turns, tools=tools, reasoning_effort="max", prepare_wire=_prepare)
    assert first.turn.tool_calls[0].id == "call_test"
    turns.extend(
        [
            first.turn,
            Turn.tool("call_test", "Found it"),
            Turn.system("Use the result carefully", source="tool_advisory"),
        ]
    )
    before = copy.deepcopy(turns)
    second = model_turn(lane, turns, tools=tools, reasoning_effort="max", prepare_wire=_prepare)

    sent = requests[1]
    assert turns == before
    assert sent["instructions"] == requests[0]["instructions"] == "Base instructions"
    assert sent["input"] == [
        {"type": "message", "role": "user", "content": "Look it up"},
        {"type": "reasoning", "id": "rs_test", "summary": [], "encrypted_content": "opaque"},
        {"type": "function_call", "call_id": "call_test", "name": "lookup", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "call_test", "output": "Found it"},
        {"type": "message", "role": "system", "content": "Use the result carefully"},
    ]
    assert sent["include"] == ["reasoning.encrypted_content"]
    assert sent["reasoning"] == {"effort": "max", "mode": "pro"}
    assert sent["text"] == {"verbosity": "low"}
    assert sent["tools"][0]["name"] == "lookup"
    assert "async" not in sent["tools"][0]
    assert second.usage.cache_read_tokens == 30
    assert second.usage.cache_creation_tokens == 70


@pytest.mark.parametrize("replay_reasoning", [False, True])
def test_phased_replay_through_sdk_and_minted_tool_ids(sdk_boundary, replay_reasoning):
    client, requests, outputs = sdk_boundary
    provider = create_provider("openai")
    registry = MagicMock()
    registry.get_config.return_value = SimpleNamespace(
        capabilities={}, server_compat={}, replay_reasoning_to_model=replay_reasoning
    )
    lane = ModelLane(
        provider,
        client,
        "gpt-6-astra",
        alias="astra",
        capabilities=provider.get_capabilities("gpt-6-astra"),
        registry=registry,
    )
    commentary = {
        "type": "message",
        "id": "msg_commentary",
        "role": "assistant",
        "status": "completed",
        "phase": "commentary",
        "content": [{"type": "output_text", "text": "Checking. ", "annotations": []}],
    }
    final = {
        "type": "message",
        "id": "msg_final",
        "role": "assistant",
        "status": "completed",
        "phase": "final_answer",
        "content": [
            {
                "type": "output_text",
                "text": "The answer.",
                "annotations": [
                    {
                        "type": "url_citation",
                        "url": "https://example.com",
                        "title": "Source",
                        "start_index": 0,
                        "end_index": 3,
                    }
                ],
            }
        ],
    }
    outputs.append(
        [
            commentary,
            {"type": "reasoning", "id": "rs_test", "summary": [], "encrypted_content": "opaque"},
            {
                "type": "function_call",
                "id": "fc_test",
                "call_id": "call_test",
                "name": "lookup",
                "arguments": "{}",
                "status": "completed",
            },
            final,
        ]
    )
    turns = [Turn.system("Base"), Turn.user("Find it")]
    wire_id_map = {}
    first = model_turn(
        lane,
        turns,
        mint=lambda original: f"agent::{original}",
        wire_id_map=wire_id_map,
        prepare_wire=_prepare,
    )
    assert first.turn.text == "Checking. The answer.\n\nSources:\n- [Source](https://example.com)"
    assert first.turn.tool_calls[0].id == "agent::call_test"
    assert [b["phase"] for b in first.turn.native.blocks if b["type"] == "message"] == [
        "commentary",
        "final_answer",
    ]
    turns.extend(
        [
            first.turn,
            Turn.tool("agent::call_test", "Found"),
            Turn.system("Use the result carefully", source="tool_advisory"),
        ]
    )
    before = copy.deepcopy(turns)
    model_turn(lane, turns, wire_id_map=wire_id_map, prepare_wire=_prepare)
    assert turns == before
    items = requests[1]["input"]
    expected = [
        {"type": "message", "role": "user", "content": "Find it"},
        {"type": "message", "role": "assistant", "content": "Checking. ", "phase": "commentary"},
        {"type": "reasoning", "id": "rs_test", "summary": [], "encrypted_content": "opaque"},
        {"type": "function_call", "call_id": "call_test", "name": "lookup", "arguments": "{}"},
        {
            "type": "message",
            "role": "assistant",
            "content": "The answer.\n\nSources:\n- [Source](https://example.com)",
            "phase": "final_answer",
        },
        {"type": "function_call_output", "call_id": "call_test", "output": "Found"},
        {"type": "message", "role": "system", "content": "Use the result carefully"},
    ]
    if not replay_reasoning:
        expected = [item for item in expected if item["type"] != "reasoning"]
    assert items == expected


@pytest.mark.parametrize("role", ["system", "developer"])
@pytest.mark.parametrize("native", [False, True])
def test_instruction_position_and_content_parts(role, native):
    messages = [
        {"role": role, "content": "Base"},
        {"role": role, "content": [{"type": "text", "text": "More base"}]},
        {"role": "user", "content": "Hello"},
        {
            "role": role,
            "content": [{"type": "text", "text": "First"}, {"type": "text", "text": "Second"}],
        },
        {"role": role, "content": "Last"},
    ]
    before = copy.deepcopy(messages)
    instructions, items = OpenAIResponsesProvider._convert_messages(
        messages, supports_mid_conversation_system=native
    )
    assert messages == before
    if native:
        assert instructions == "Base\n\nMore base"
        assert items[1:] == [
            {"type": "message", "role": role, "content": "First\n\nSecond"},
            {"type": "message", "role": role, "content": "Last"},
        ]
    else:
        assert instructions == "Base\n\nMore base\n\nFirst\n\nSecond\n\nLast"
        assert len(items) == 1


@pytest.mark.parametrize(
    "provider_name,model,native",
    [
        ("openai", "gpt-6-astra", True),
        ("openai", "gpt-5.6-sol", False),
        ("openai-compatible", "gpt-6-astra", False),
    ],
)
def test_serving_lane_controls_system_fold(sdk_boundary, provider_name, model, native):
    client, requests, _ = sdk_boundary
    provider = create_provider(provider_name, api_surface="responses")
    lane = ModelLane(provider, client, model, capabilities=provider.get_capabilities(model))
    turns = [Turn.user("Hello"), Turn.system("Tail", source="skill_hint")]
    model_turn(lane, turns, prepare_wire=_prepare)
    items = requests[0]["input"]
    if native:
        assert items[-1] == {"type": "message", "role": "system", "content": "Tail"}
    else:
        assert len(items) == 1
        assert "[start system-reminder_testnonce]" in items[0]["content"]
        assert "Tail" in items[0]["content"]
