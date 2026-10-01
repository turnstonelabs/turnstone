"""Effort changes preserve the Responses input prefix through the real SDK."""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tests.test_provider_openai_astra import sdk_boundary as sdk_boundary
from turnstone.core.model_turn import ModelLane, is_empty_completion, model_turn
from turnstone.core.providers._openai_responses import OpenAIResponsesProvider
from turnstone.core.trajectory import (
    ProviderNative,
    Turn,
    assistant_meta_envelope,
    turn_from_dict,
    turn_to_dict,
)


def _lane(client, model="gpt-6-astra", *, caps=None, compat=False):
    provider = OpenAIResponsesProvider(compat=compat)
    return ModelLane(provider, client, model, capabilities=caps or provider.get_capabilities(model))


def _sample(lane, turns, effort):
    result = model_turn(lane, turns, reasoning_effort=effort)
    turns.append(result.turn)
    return result.turn


def _updates(request):
    return [item for item in request["input"] if item["type"] == "configuration_update"]


def _config(turn):
    return turn.native.blocks[0].get("_reasoning_config") if turn.native else None


@pytest.mark.parametrize("model", ["gpt-6-astra", "gpt-6-sol", "gpt-6-luna", "gpt-6.1-sol"])
def test_effort_changes_replay_at_original_positions(sdk_boundary, model):
    client, requests, _ = sdk_boundary
    lane = _lane(client, model)
    turns = [Turn.user("first")]
    _sample(lane, turns, "low")
    turns.append(Turn.user("second"))
    _sample(lane, turns, "high")
    turns.append(Turn.user("third"))
    _sample(lane, turns, "high")
    turns.append(Turn.user("fourth"))
    _sample(lane, turns, "low")

    assert [request["reasoning"] for request in requests] == [{"effort": "low"}] * 4
    assert _updates(requests[0]) == []
    update_high = {"type": "configuration_update", "reasoning": {"effort": "high"}}
    update_low = {"type": "configuration_update", "reasoning": {"effort": "low"}}
    assert _updates(requests[1]) == [update_high]
    assert _updates(requests[2]) == [update_high]
    assert _updates(requests[3]) == [update_high, update_low]
    assert requests[2]["input"][: len(requests[1]["input"])] == requests[1]["input"]
    assert requests[3]["input"][: len(requests[2]["input"])] == requests[2]["input"]
    assert requests[1]["input"][-2:] == [
        update_high,
        {"type": "message", "role": "user", "content": "second"},
    ]
    assert requests[3]["input"][-2:] == [
        update_low,
        {"type": "message", "role": "user", "content": "fourth"},
    ]
    assert [_config(turn)["effort"] for turn in turns if turn.role.value == "assistant"] == [
        "low",
        "high",
        "high",
        "low",
    ]


def test_capture_uses_resolved_effort_and_bridge_roundtrip(sdk_boundary):
    client, requests, _ = sdk_boundary
    turns = [Turn.user("first")]
    assistant = _sample(_lane(client), turns, "minimal")
    expected = {"model": "gpt-6-astra", "effort": "low"}
    assert _config(assistant) == expected
    assert _config(turn_from_dict(turn_to_dict(assistant))) == expected

    turns.append(Turn.user("second"))
    _sample(_lane(client), turns, "low")
    assert requests[-1]["reasoning"] == {"effort": "low"}
    assert _updates(requests[-1]) == []
    assert "_reasoning_config" not in json.dumps(requests[-1])


def test_initial_omission_stays_omitted_and_unknown_default_resets(sdk_boundary):
    client, requests, _ = sdk_boundary
    turns = [Turn.user("first")]
    lane = _lane(client)
    _sample(lane, turns, None)
    turns.append(Turn.user("second"))
    _sample(lane, turns, "high")
    assert "reasoning" not in requests[0]
    assert "reasoning" not in requests[1]
    assert _updates(requests[1]) == [
        {"type": "configuration_update", "reasoning": {"effort": "high"}}
    ]

    turns.append(Turn.user("third"))
    _sample(lane, turns, "none")
    assert "reasoning" not in requests[-1]
    assert _updates(requests[-1]) == []
    turns.append(Turn.user("fourth"))
    _sample(lane, turns, "low")
    assert "reasoning" not in requests[-1]
    assert _updates(requests[-1]) == [
        {"type": "configuration_update", "reasoning": {"effort": "low"}}
    ]


@pytest.mark.parametrize("barrier", ["legacy", "foreign", "compaction", "corrupt"])
def test_history_barriers_start_a_new_baseline(sdk_boundary, barrier):
    client, requests, _ = sdk_boundary
    lane = _lane(client)
    turns = [Turn.user("first")]
    _sample(lane, turns, "low")
    if barrier == "foreign":
        turns.append(Turn.user("another model"))
        _sample(_lane(client, "gpt-6-luna"), turns, "high")
    else:
        assistant = Turn.assistant("summary" if barrier == "compaction" else "older reply")
        if barrier == "corrupt":
            assistant.native = ProviderNative(
                "openai", ({"type": "message", "_reasoning_config": {"effort": "high"}},)
            )
        if barrier == "compaction":
            turns = [Turn.user("conversation summary")]
            assistant.source = "compaction"
        turns.append(assistant)
    turns.append(Turn.user("next"))
    _sample(lane, turns, "high")
    assert requests[-1]["reasoning"] == {"effort": "high"}
    assert _updates(requests[-1]) == []
    turns.append(Turn.user("last"))
    _sample(lane, turns, "low")
    assert requests[-1]["reasoning"] == {"effort": "high"}
    assert _updates(requests[-1]) == [
        {"type": "configuration_update", "reasoning": {"effort": "low"}}
    ]


def test_pro_mode_keeps_request_level_effort(sdk_boundary):
    client, requests, _ = sdk_boundary
    lane = _lane(client)
    turns = [Turn.user("first")]
    _sample(lane, turns, "low")
    turns.append(Turn.user("second"))
    pro = replace(lane, capabilities=replace(lane.capabilities, reasoning_mode="pro"))
    assistant = _sample(pro, turns, "high")
    assert requests[-1]["reasoning"] == {"effort": "high", "mode": "pro"}
    assert _updates(requests[-1]) == []
    assert _config(assistant) is None
    turns.append(Turn.user("third"))
    _sample(lane, turns, "low")
    assert requests[-1]["reasoning"] == {"effort": "low"}
    assert _updates(requests[-1]) == []


@pytest.mark.parametrize("model,compat", [("gpt-5.6", False), ("gpt-6-astra", True)])
def test_other_lanes_keep_request_level_effort(sdk_boundary, model, compat):
    client, requests, _ = sdk_boundary
    turns = [Turn.user("first")]
    caps = OpenAIResponsesProvider().get_capabilities(model)
    lane = _lane(client, model, caps=caps, compat=compat)
    _sample(lane, turns, "low")
    turns.append(Turn.user("second"))
    assistant = _sample(lane, turns, "high")
    assert requests[-1]["reasoning"] == {"effort": "high"}
    assert _updates(requests[-1]) == []
    assert _config(assistant) is None


def test_effort_timeline_survives_storage_and_fork(sdk_boundary, storage_backend):
    client, requests, _ = sdk_boundary
    lane = _lane(client)
    turns = [Turn.user("first")]
    _sample(lane, turns, "low")
    turns.append(Turn.user("second"))
    _sample(lane, turns, "high")
    backend = storage_backend
    backend.register_workstream("source", user_id="alice", kind="interactive")
    backend.register_workstream(
        "destination", user_id="alice", state="creating", kind="interactive"
    )
    for turn in turns:
        backend.save_message(
            "source",
            turn.role.value,
            turn.text,
            provider_data=json.dumps(list(turn.native.blocks)) if turn.native else None,
            producer=turn.native.producer if turn.native else None,
            meta=json.dumps(assistant_meta_envelope(turn))
            if turn.role.value == "assistant"
            else None,
        )
    resumed = backend.load_message_turns("source")
    assert [_config(t) for t in resumed] == [_config(t) for t in turns]
    assert [assistant_meta_envelope(t) for t in resumed] == [
        assistant_meta_envelope(t) for t in turns
    ]
    backend.clone_workstream("source", "destination", principal_id="alice")
    forked = backend.load_message_turns("destination")
    assert [t.native for t in forked] == [t.native for t in resumed]
    assert [assistant_meta_envelope(t) for t in forked] == [
        assistant_meta_envelope(t) for t in resumed
    ]
    before = copy.deepcopy(forked)
    forked.append(Turn.user("third"))
    _sample(_lane(client), forked, "low")
    assert forked[:-2] == before
    assert requests[-1]["reasoning"] == {"effort": "low"}
    assert _updates(requests[-1]) == [
        {"type": "configuration_update", "reasoning": {"effort": "high"}},
        {"type": "configuration_update", "reasoning": {"effort": "low"}},
    ]


def test_effort_change_between_tool_rounds(sdk_boundary):
    client, requests, outputs = sdk_boundary
    outputs.append(
        [
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
    lane = _lane(client)
    turns = [Turn.user("look up the answer")]
    assistant = _sample(lane, turns, "low")
    assert assistant.tool_calls[0].id == "call_test"
    turns.append(Turn.tool("call_test", "found it"))
    _sample(lane, turns, "high")
    assert requests[-1]["reasoning"] == {"effort": "low"}
    assert requests[-1]["input"][-2:] == [
        {"type": "configuration_update", "reasoning": {"effort": "high"}},
        {"type": "function_call_output", "call_id": "call_test", "output": "found it"},
    ]
    turns.append(Turn.user("continue"))
    _sample(lane, turns, "high")
    assert requests[-1]["input"][: len(requests[-2]["input"])] == requests[-2]["input"]


def test_updates_never_become_adjacent_when_reasoning_is_not_replayed(sdk_boundary):
    client, requests, outputs = sdk_boundary
    lane = _lane(client)
    turns = [Turn.user("first")]
    _sample(lane, turns, "low")
    for effort in ("high", "medium"):
        outputs.append(
            [
                {
                    "type": "reasoning",
                    "id": "rs_test",
                    "summary": [],
                    "encrypted_content": "opaque",
                    "status": "completed",
                },
            ]
        )
        _sample(lane, turns, effort)
    _sample(lane, turns, "xhigh")
    assert requests[-1]["reasoning"] == {"effort": "low"}
    assert _updates(requests[-1]) == [
        {"type": "configuration_update", "reasoning": {"effort": "xhigh"}},
    ]
    assert not any(
        a["type"] == b["type"] == "configuration_update"
        for a, b in zip(requests[-1]["input"], requests[-1]["input"][1:], strict=False)
    )


def test_model_default_is_replayed_when_the_operator_clears_effort(sdk_boundary):
    client, requests, _ = sdk_boundary
    lane = _lane(client, "gpt-6.1-sol")
    turns = [Turn.user("first")]
    _sample(lane, turns, None)
    turns.append(Turn.user("second"))
    _sample(lane, turns, "high")
    turns.append(Turn.user("third"))
    _sample(lane, turns, None)
    assert requests[-1]["reasoning"] == {"effort": "medium"}
    assert _updates(requests[-1]) == [
        {"type": "configuration_update", "reasoning": {"effort": "high"}},
        {"type": "configuration_update", "reasoning": {"effort": "medium"}},
    ]


@pytest.mark.parametrize("temperature", [None, 0.7])
def test_reasoning_none_preserves_sampling_contract(sdk_boundary, temperature):
    client, requests, _ = sdk_boundary
    lane = replace(_lane(client, "gpt-6-sol"), temperature=temperature)
    turns = [Turn.user("first")]
    _sample(lane, turns, "low")
    turns.append(Turn.user("second"))
    assistant = _sample(lane, turns, "none")
    if temperature is None:
        assert requests[-1]["reasoning"] == {"effort": "low"}
        assert _updates(requests[-1]) == [
            {"type": "configuration_update", "reasoning": {"effort": "none"}},
        ]
        assert _config(assistant)["effort"] == "none"
    else:
        assert requests[-1]["reasoning"] == {"effort": "none"}
        assert requests[-1]["temperature"] == temperature
        assert _updates(requests[-1]) == []
        assert _config(assistant) is None
    turns.append(Turn.user("third"))
    _sample(lane, turns, "high")
    assert "temperature" not in requests[-1]
    if temperature is not None:
        assert requests[-1]["reasoning"] == {"effort": "high"}
        assert _updates(requests[-1]) == []


@pytest.mark.parametrize("enabled", [False, True])
def test_capability_gates_updates_independently_of_model_name(sdk_boundary, enabled):
    client, requests, _ = sdk_boundary
    caps = replace(
        OpenAIResponsesProvider().get_capabilities("gpt-6-astra"),
        supports_reasoning_config_updates=enabled,
    )
    lane = _lane(client, "declared-model", caps=caps)
    turns = [Turn.user("first")]
    _sample(lane, turns, "low")
    turns.append(Turn.user("second"))
    _sample(lane, turns, "high")
    assert requests[-1]["reasoning"] == {"effort": "low" if enabled else "high"}
    assert bool(_updates(requests[-1])) is enabled


@pytest.mark.parametrize(
    "bad",
    [
        None,
        [],
        {},
        {"model": "gpt-6-astra"},
        {"model": "gpt-6-astra", "effort": False},
        {"model": "gpt-6-astra", "effort": []},
        {"model": "gpt-6-astra", "effort": "unsupported"},
    ],
)
def test_corrupt_effort_metadata_is_ignored(sdk_boundary, bad):
    client, requests, _ = sdk_boundary
    turn = Turn.assistant(
        "ok",
        native=ProviderNative("openai", ({"type": "message", "_reasoning_config": bad},)),
    )
    _sample(_lane(client), [Turn.user("first"), turn, Turn.user("second")], "high")
    assert requests[-1]["reasoning"] == {"effort": "high"}
    assert _updates(requests[-1]) == []
    assert "_reasoning_config" not in json.dumps(requests[-1])


@pytest.mark.parametrize(
    "output",
    [
        [],
        [
            {
                "type": "message",
                "id": "msg_empty",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": "", "annotations": []}],
            }
        ],
        [
            {
                "type": "reasoning",
                "id": "rs_empty",
                "summary": [],
                "encrypted_content": "opaque",
                "status": "completed",
            }
        ],
    ],
)
def test_local_replay_metadata_cannot_make_an_empty_completion_usable(sdk_boundary, output):
    client, _, outputs = sdk_boundary
    outputs.append(output)
    result = model_turn(_lane(client), [Turn.user("answer")], reasoning_effort="low")
    assert is_empty_completion(result)
    assert "_reasoning_config" not in json.dumps(output)


def test_reasoning_replay_preserves_encrypted_content_without_local_metadata(sdk_boundary):
    client, requests, outputs = sdk_boundary
    reasoning = {
        "type": "reasoning",
        "id": "rs_test",
        "summary": [],
        "encrypted_content": "opaque-exact-bytes",
        "status": "completed",
    }
    outputs.append(
        [
            reasoning,
            {
                "type": "message",
                "id": "msg_test",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": "ok", "annotations": []}],
            },
        ]
    )
    registry = MagicMock()
    registry.get_config.return_value = SimpleNamespace(
        replay_reasoning_to_model=True,
        server_compat={},
    )
    lane = replace(_lane(client), registry=registry, alias="target")
    turns = [Turn.user("first")]
    _sample(lane, turns, "low")
    turns.append(Turn.user("second"))
    _sample(lane, turns, "high")
    sent_reasoning = next(item for item in requests[-1]["input"] if item["type"] == "reasoning")
    assert sent_reasoning == {key: value for key, value in reasoning.items() if key != "status"}
    assert "_reasoning_config" not in json.dumps(requests[-1])
    assert _updates(requests[-1]) == [
        {"type": "configuration_update", "reasoning": {"effort": "high"}},
    ]
    assert "_reasoning_config" not in reasoning
