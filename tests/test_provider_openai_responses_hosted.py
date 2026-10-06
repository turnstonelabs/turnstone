"""Hosted tool items (web search, tool search) replay in their native positions."""

from __future__ import annotations

import copy
import json
import logging
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tests.test_provider_openai_astra import _prepare
from tests.test_provider_openai_astra import sdk_boundary as sdk_boundary
from turnstone.core.model_turn import ModelLane, model_turn
from turnstone.core.providers import create_provider
from turnstone.core.providers._openai_common import lookup_openai_capabilities
from turnstone.core.providers._openai_responses import (
    OpenAIResponsesProvider,
    _log_hosted_omission_once,
)
from turnstone.core.providers._xai import XAIProvider
from turnstone.core.trajectory import Turn

BOTH = frozenset({"web_search", "tool_search"})

# Stored shapes are the SDK's model_dump of live output items (gpt-6-astra, 2026-10-05). Input
# shapes carry no ids: the API rejects an id another endpoint minted, and ids are optional.
WEB_SEARCH = {
    "type": "web_search_call",
    "id": "ws_1",
    "status": "completed",
    "action": {"type": "search", "query": "latest python release", "sources": None},
}
WEB_SEARCH_INPUT = {
    "type": "web_search_call",
    "status": "completed",
    "action": {"type": "search", "query": "latest python release"},
}
SEARCH_CALL = {
    "type": "tool_search_call",
    "id": "tsc_1",
    "arguments": {"paths": ["get_weather"]},
    "call_id": None,
    "execution": "server",
    "status": "completed",
    "created_by": None,
    "_reasoning_config": {"model": "gpt-6-astra", "effort": "low"},
}
SEARCH_CALL_INPUT = {
    "type": "tool_search_call",
    "arguments": {"paths": ["get_weather"]},
    "execution": "server",
    "status": "completed",
}
WEATHER_PARAMETERS = {"type": "object", "properties": {"city": {"type": "string"}}}
SEARCH_OUTPUT = {
    "type": "tool_search_output",
    "id": "tso_1",
    "call_id": None,
    "execution": "server",
    "status": "completed",
    "tools": [
        {
            "name": "get_weather",
            "parameters": WEATHER_PARAMETERS,
            "strict": False,
            "type": "function",
            "allowed_callers": None,
            "async_": None,
            "defer_loading": True,
            "description": "Get the current weather for a city.",
            "output_schema": None,
        }
    ],
    "created_by": None,
}
SEARCH_OUTPUT_INPUT = {
    "type": "tool_search_output",
    "execution": "server",
    "status": "completed",
    "tools": [
        {
            "name": "get_weather",
            "parameters": WEATHER_PARAMETERS,
            "strict": False,
            "type": "function",
            "defer_loading": True,
            "description": "Get the current weather for a city.",
        }
    ],
}


def _native_call(call_id, name, arguments, namespace=None):
    return {
        "type": "function_call",
        "id": f"fc_{call_id}",
        "call_id": call_id,
        "name": name,
        "arguments": arguments,
        "namespace": namespace,
        "async_": None,
        "caller": None,
        "status": "completed",
    }


def _call(call_id, name, arguments, namespace=None):
    call = {"type": "function_call", "call_id": call_id, "name": name, "arguments": arguments}
    if namespace:
        call["namespace"] = namespace
    return call


def _reasoning(n):
    return {"type": "reasoning", "id": f"rs_{n}", "summary": [], "encrypted_content": f"enc{n}"}


def _turn(blocks, calls=(), *, content="", producer="openai"):
    message = {"role": "assistant", "content": content, "_provider_content": copy.deepcopy(blocks)}
    if producer is not None:
        message["_producer"] = producer
    if calls:
        message["tool_calls"] = [
            {"id": call_id, "function": {"name": name, "arguments": arguments}}
            for call_id, name, arguments in calls
        ]
    return message


def _result(call_id, text):
    return {"role": "tool", "tool_call_id": call_id, "content": text}


def _output(call_id, text):
    return {"type": "function_call_output", "call_id": call_id, "output": text}


PARIS = ("call_1", "get_weather", '{"city":"Paris"}')
ROME = ("call_2", "get_weather", '{"city":"Rome"}')


def _tool_search_history():
    return [
        {"role": "user", "content": "What is the weather in Paris?"},
        _turn([SEARCH_CALL, SEARCH_OUTPUT, _native_call(*PARIS, "get_weather")], [PARIS]),
        _result("call_1", "Paris: 18C"),
    ]


@pytest.mark.parametrize("replay_reasoning", [False, True])
def test_web_search_items_replay_in_native_order(replay_reasoning):
    blocks = [_reasoning(1), WEB_SEARCH, _reasoning(2), _native_call("call_1", "bash", "{}")]
    messages = [_turn(blocks, [("call_1", "bash", "{}")]), _result("call_1", "done")]
    before = copy.deepcopy(messages)
    _, items = OpenAIResponsesProvider._convert_messages(
        messages, replay_reasoning_to_model=replay_reasoning, hosted_tools=BOTH
    )
    reasoning = [
        {"type": "reasoning", "id": f"rs_{n}", "summary": [], "encrypted_content": f"enc{n}"}
        for n in (1, 2)
    ]
    expected = [
        *([reasoning[0]] if replay_reasoning else []),
        WEB_SEARCH_INPUT,
        *([reasoning[1]] if replay_reasoning else []),
        _call("call_1", "bash", "{}"),
        _output("call_1", "done"),
    ]
    assert items == expected
    assert messages == before


def test_tool_search_replays_its_load_and_the_call_namespace():
    _, items = OpenAIResponsesProvider._convert_messages(_tool_search_history(), hosted_tools=BOTH)
    assert items[1:] == [
        SEARCH_CALL_INPUT,
        SEARCH_OUTPUT_INPUT,
        _call(*PARIS, "get_weather"),
        _output("call_1", "Paris: 18C"),
    ]


def test_loaded_tool_projection_spells_async_as_the_api_does():
    history = _tool_search_history()
    loaded = history[1]["_provider_content"][1]["tools"][0]
    loaded["async_"] = False
    _, items = OpenAIResponsesProvider._convert_messages(history, hosted_tools=BOTH)
    projected = next(item for item in items if item["type"] == "tool_search_output")
    assert projected["tools"][0]["async"] is False
    assert "async_" not in projected["tools"][0]


@pytest.mark.parametrize("producer", ["anthropic", "openai"])
def test_later_call_without_namespace_takes_the_namespace_of_its_load(producer):
    # The API rejects a later call to a loaded tool without its namespace (live 400). A
    # native call made while the tool was undeferred, or another producer's call, has none.
    earlier = [
        {"role": "user", "content": "Weather in Berlin?"},
        _turn([_native_call("call_0", "get_weather", "{}")], [("call_0", "get_weather", "{}")]),
        _result("call_0", "Berlin: 12C"),
    ]
    later = [
        {"role": "user", "content": "And in Rome?"},
        _turn([_native_call(*ROME)], [ROME], producer=producer),
        _result("call_2", "Rome: 24C"),
    ]
    _, items = OpenAIResponsesProvider._convert_messages(
        [*earlier, *_tool_search_history(), *later], hosted_tools=BOTH
    )
    calls = [item for item in items if item["type"] == "function_call"]
    assert [call.get("namespace") for call in calls] == [None, "get_weather", "get_weather"]


def test_native_namespace_survives_compaction_of_its_load():
    # A summary replaced the search turn; the API accepts the namespace without its load.
    messages = [
        {"role": "user", "content": "[summary of the search turn]"},
        _turn([_native_call(*ROME, "get_weather")], [ROME]),
        _result("call_2", "Rome: 24C"),
    ]
    _, items = OpenAIResponsesProvider._convert_messages(messages, hosted_tools=BOTH)
    assert _call(*ROME, "get_weather") in items
    _, without = OpenAIResponsesProvider._convert_messages(messages)
    assert _call(*ROME) in without


def test_namespace_member_takes_the_namespace_name():
    output = copy.deepcopy(SEARCH_OUTPUT)
    output["tools"] = [
        {
            "type": "namespace",
            "name": "crm",
            "description": "CRM tools.",
            "tools": [{"type": "function", "name": "list_open_orders", "async_": None}],
        }
    ]
    orders = ("call_3", "list_open_orders", "{}")
    messages = [
        _turn([SEARCH_CALL, output], content="Loaded."),
        {"role": "user", "content": "List the orders."},
        _turn([], [orders], producer="anthropic"),
        _result("call_3", "none open"),
    ]
    _, items = OpenAIResponsesProvider._convert_messages(messages, hosted_tools=BOTH)
    assert next(item for item in items if item["type"] == "tool_search_output")["tools"] == [
        {
            "type": "namespace",
            "name": "crm",
            "description": "CRM tools.",
            "tools": [{"type": "function", "name": "list_open_orders"}],
        }
    ]
    assert _call(*orders, "crm") in items


def test_native_namespace_wins_over_the_namespace_of_a_load():
    # Loaded on its own, list_open_orders would be its own namespace; the API recorded crm.
    output = copy.deepcopy(SEARCH_OUTPUT)
    output["tools"] = [{"type": "function", "name": "list_open_orders"}]
    orders = ("call_3", "list_open_orders", "{}")
    messages = [
        _turn([SEARCH_CALL, output, _native_call(*orders, "crm")], [orders]),
        _result("call_3", "none open"),
    ]
    _, items = OpenAIResponsesProvider._convert_messages(messages, hosted_tools=BOTH)
    assert _call(*orders, "crm") in items


def test_empty_load_replays_as_the_search_returned_it():
    messages = [_turn([SEARCH_CALL, {**SEARCH_OUTPUT, "tools": []}], content="Nothing found.")]
    _, items = OpenAIResponsesProvider._convert_messages(messages, hosted_tools=BOTH)
    assert items[:2] == [SEARCH_CALL_INPUT, {**SEARCH_OUTPUT_INPUT, "tools": []}]


def _omissions(caplog):
    return [r.getMessage() for r in caplog.records if "hosted_item_omission" in r.getMessage()]


@pytest.mark.parametrize(
    ("block", "omitted"),
    [
        ({**SEARCH_CALL, "arguments": None}, "which has no arguments"),
        ({**SEARCH_OUTPUT, "tools": None}, "which has no tools"),
        ({**SEARCH_OUTPUT, "tools": "get_weather"}, "whose tools are not a list"),
    ],
)
def test_item_the_api_cannot_take_back_is_omitted_and_logged_once(caplog, block, omitted):
    # The API rejects a tool search call without arguments, or an output without tools (live
    # 400), and history keeps the stored item, so the omission recurs on every request.
    _log_hosted_omission_once.cache_clear()
    messages = [_turn([block], content="Searched."), {"role": "user", "content": "Go on."}]
    with caplog.at_level(logging.INFO, logger="turnstone.core.providers._openai_responses"):
        for _ in range(2):
            _, items = OpenAIResponsesProvider._convert_messages(messages, hosted_tools=BOTH)
            assert block["type"] not in [item["type"] for item in items]
    [message] = _omissions(caplog)
    assert omitted in message


def test_another_endpoints_search_replays_without_its_id_or_unknown_action(caplog):
    # A row stored under the openai producer can come from another Responses endpoint: the API
    # rejects an id it did not mint (live 400), so no id goes back, and an action type it lacks
    # is left off the search, which still replays.
    _log_hosted_omission_once.cache_clear()
    foreign = {
        "type": "web_search_call",
        "id": "search_1",
        "status": "completed",
        "action": {"type": "x_search", "query": "python release"},
    }
    with caplog.at_level(logging.INFO, logger="turnstone.core.providers._openai_responses"):
        _, items = OpenAIResponsesProvider._convert_messages(
            [_turn([foreign], content="Searched.")], hosted_tools=BOTH
        )
    assert items[0] == {"type": "web_search_call", "status": "completed"}
    [message] = _omissions(caplog)
    assert "unknown type" in message


@pytest.mark.parametrize(
    ("action", "projected"),
    [
        (
            {
                "type": "search",
                "query": "python release",
                "queries": ["python release", "python 3.14"],
                "sources": [{"type": "url", "url": "https://example.com/a"}],
            },
            {
                "type": "search",
                "query": "python release",
                "queries": ["python release", "python 3.14"],
            },
        ),
        (
            {"type": "open_page", "url": "https://example.com/a"},
            {"type": "open_page", "url": "https://example.com/a"},
        ),
        (
            {"type": "find_in_page", "pattern": "3.14", "url": "https://example.com/a"},
            {"type": "find_in_page", "pattern": "3.14", "url": "https://example.com/a"},
        ),
    ],
)
def test_each_search_action_replays_in_its_input_shape(action, projected):
    # Sources never reach the model on input, so they are not sent back either.
    search = {**WEB_SEARCH, "action": action}
    _, items = OpenAIResponsesProvider._convert_messages(
        [_turn([search], content="Searched.")], hosted_tools=BOTH
    )
    assert items[0] == {"type": "web_search_call", "status": "completed", "action": projected}


@pytest.mark.parametrize("replay_reasoning", [False, True])
def test_turn_without_text_keeps_its_native_order(replay_reasoning):
    # A call made before a search stays before it, and so keeps no namespace from its load.
    run = ("call_0", "run", "{}")
    blocks = [
        _native_call(*run),
        _reasoning(1),
        SEARCH_CALL,
        SEARCH_OUTPUT,
        _native_call(*PARIS, "get_weather"),
    ]
    messages = [_turn(blocks, [run, PARIS]), _result("call_0", "ok"), _result("call_1", "18C")]
    _, items = OpenAIResponsesProvider._convert_messages(
        messages, replay_reasoning_to_model=replay_reasoning, hosted_tools=BOTH
    )
    reasoning = [{"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "enc1"}]
    assert items[: 5 if replay_reasoning else 4] == [
        _call(*run),
        *(reasoning if replay_reasoning else []),
        SEARCH_CALL_INPUT,
        SEARCH_OUTPUT_INPUT,
        _call(*PARIS, "get_weather"),
    ]


def test_edited_layout_keeps_hosted_items_ahead_of_its_text():
    blocks = [
        SEARCH_CALL,
        SEARCH_OUTPUT,
        {
            "type": "message",
            "role": "assistant",
            "phase": "commentary",
            "content": [{"type": "output_text", "text": "Checking.", "annotations": []}],
        },
        _native_call(*PARIS, "get_weather"),
    ]
    messages = [_turn(blocks, [PARIS], content="Edited."), _result("call_1", "Paris: 18C")]
    _, items = OpenAIResponsesProvider._convert_messages(messages, hosted_tools=BOTH)
    assert items == [
        SEARCH_CALL_INPUT,
        SEARCH_OUTPUT_INPUT,
        {"type": "message", "role": "assistant", "content": "Edited."},
        _call(*PARIS, "get_weather"),
        _output("call_1", "Paris: 18C"),
    ]


def test_replay_keeps_the_wire_prefix_as_history_grows():
    history = [
        *_tool_search_history(),
        _turn([WEB_SEARCH, _native_call(*ROME, "get_weather")], [ROME]),
        _result("call_2", "Rome: 24C"),
    ]
    _, shorter = OpenAIResponsesProvider._convert_messages(history[:3], hosted_tools=BOTH)
    _, longer = OpenAIResponsesProvider._convert_messages(history, hosted_tools=BOTH)
    assert longer[: len(shorter)] == shorter
    assert WEB_SEARCH_INPUT in longer


def _kwargs(provider, model, messages, **kwargs):
    return provider._build_kwargs(model, messages, None, 1000, None, None, None, **kwargs)


def _hosted_history():
    return [
        *_tool_search_history(),
        _turn([WEB_SEARCH], content="Searched."),
        {"role": "user", "content": "Thanks."},
    ]


def test_each_hosted_tool_replays_only_for_a_model_that_runs_it():
    # The capability decides, not the request: these requests offer no hosted tool at all.
    astra = _kwargs(OpenAIResponsesProvider(), "gpt-6-astra", _hosted_history())
    assert "tools" not in astra
    assert [item["type"] for item in astra["input"]] == [
        "message",
        "tool_search_call",
        "tool_search_output",
        "function_call",
        "function_call_output",
        "web_search_call",
        "message",
        "message",
    ]
    caps = lookup_openai_capabilities("gpt-6-astra")
    no_web = _kwargs(
        OpenAIResponsesProvider(),
        "gpt-6-astra",
        _hosted_history(),
        capabilities=replace(caps, supports_web_search=False),
    )
    assert "web_search_call" not in [item["type"] for item in no_web["input"]]
    assert "tool_search_output" in [item["type"] for item in no_web["input"]]
    neither = _kwargs(
        OpenAIResponsesProvider(),
        "gpt-6-astra",
        _hosted_history(),
        capabilities=replace(caps, supports_web_search=False, supports_tool_search=False),
    )
    assert [item["type"] for item in neither["input"]] == [
        "message",
        "function_call",
        "function_call_output",
        "message",
        "message",
    ]
    assert not any("namespace" in item for item in neither["input"])


def test_compatible_endpoints_and_xai_do_not_replay_hosted_items():
    astra = lookup_openai_capabilities("gpt-6-astra")
    compat = _kwargs(
        OpenAIResponsesProvider(compat=True), "gpt-6-astra", _hosted_history(), capabilities=astra
    )
    xai_history = _hosted_history()
    for message in xai_history:
        if message["role"] == "assistant":
            message["_producer"] = "xai"
    xai = _kwargs(XAIProvider(), "grok-4.20", xai_history)
    for payload in (compat, xai):
        kinds = [item["type"] for item in payload["input"]]
        assert not {"web_search_call", "tool_search_call", "tool_search_output"} & set(kinds)
        assert not any("namespace" in item for item in payload["input"])


@pytest.mark.parametrize("producer", ["anthropic", "xai"])
def test_hosted_items_of_another_producer_are_not_replayed(producer):
    history = _tool_search_history()
    history[1]["_producer"] = producer
    _, items = OpenAIResponsesProvider._convert_messages(history, hosted_tools=BOTH)
    assert [item["type"] for item in items] == ["message", "function_call", "function_call_output"]
    assert "namespace" not in items[1]


def test_hosted_items_survive_persistence_round_trip(backend):
    history = _tool_search_history()
    assistant = history[1]
    backend.save_message("ws-hosted", "user", history[0]["content"])
    backend.save_message(
        "ws-hosted",
        "assistant",
        assistant["content"],
        provider_data=json.dumps(assistant["_provider_content"]),
        producer="openai",
        tool_calls=json.dumps(assistant["tool_calls"]),
    )
    backend.save_message("ws-hosted", "tool", "Paris: 18C", tool_call_id="call_1")
    loaded = backend.load_messages("ws-hosted", repair=False)
    _, items = OpenAIResponsesProvider._convert_messages(loaded, hosted_tools=BOTH)
    assert items[1:4] == [SEARCH_CALL_INPUT, SEARCH_OUTPUT_INPUT, _call(*PARIS, "get_weather")]


def test_effort_update_lands_after_a_hosted_turn():
    # Response boundaries are recorded before the namespace pass, which rewrites the call it
    # namespaces in place, so the update still follows the hosted turn's own items.
    messages = [
        {"role": "user", "content": "What is the weather in Paris?"},
        _turn([SEARCH_CALL, SEARCH_OUTPUT, _native_call(*PARIS)], [PARIS]),
        _result("call_1", "Paris: 18C"),
    ]
    payload = OpenAIResponsesProvider()._build_kwargs(
        "gpt-6-astra", messages, None, 1000, None, "high", None
    )
    assert payload["reasoning"] == {"effort": "low"}
    assert payload["input"] == [
        {"type": "message", "role": "user", "content": "What is the weather in Paris?"},
        SEARCH_CALL_INPUT,
        SEARCH_OUTPUT_INPUT,
        _call(*PARIS, "get_weather"),
        {"type": "configuration_update", "reasoning": {"effort": "high"}},
        _output("call_1", "Paris: 18C"),
    ]


def test_hosted_replay_through_sdk_and_minted_tool_ids(sdk_boundary):
    client, requests, outputs = sdk_boundary
    provider = create_provider("openai")
    registry = MagicMock()
    registry.get_config.return_value = SimpleNamespace(
        capabilities={}, server_compat={}, replay_reasoning_to_model=False
    )
    lane = ModelLane(
        provider,
        client,
        "gpt-6-astra",
        alias="astra",
        capabilities=provider.get_capabilities("gpt-6-astra"),
        registry=registry,
    )
    loaded = {
        "type": "function",
        "name": "get_weather",
        "description": "Get the current weather for a city.",
        "parameters": WEATHER_PARAMETERS,
        "strict": False,
        "defer_loading": True,
        "async": False,
    }
    outputs.append(
        [
            {
                "type": "web_search_call",
                "id": "ws_test",
                "status": "completed",
                "action": {"type": "search", "query": "paris weather"},
            },
            {
                "type": "tool_search_call",
                "id": "tsc_test",
                "call_id": None,
                "execution": "server",
                "status": "completed",
                "arguments": {"paths": ["get_weather"]},
            },
            {
                "type": "tool_search_output",
                "id": "tso_test",
                "call_id": None,
                "execution": "server",
                "status": "completed",
                "tools": [loaded],
            },
            {
                "type": "function_call",
                "id": "fc_test",
                "call_id": "call_test",
                "name": "get_weather",
                "namespace": "get_weather",
                "arguments": '{"city":"Paris"}',
                "status": "completed",
            },
        ]
    )
    turns = [Turn.system("Base"), Turn.user("Weather in Paris?")]
    wire_id_map = {}
    first = model_turn(
        lane,
        turns,
        mint=lambda original: f"agent::{original}",
        wire_id_map=wire_id_map,
        prepare_wire=_prepare,
    )
    assert first.turn.tool_calls[0].id == "agent::call_test"
    turns.extend([first.turn, Turn.tool("agent::call_test", "Paris: 18C")])
    model_turn(lane, turns, wire_id_map=wire_id_map, prepare_wire=_prepare)
    assert requests[1]["input"] == [
        {"type": "message", "role": "user", "content": "Weather in Paris?"},
        {
            "type": "web_search_call",
            "status": "completed",
            "action": {"type": "search", "query": "paris weather"},
        },
        {
            "type": "tool_search_call",
            "status": "completed",
            "execution": "server",
            "arguments": {"paths": ["get_weather"]},
        },
        {
            "type": "tool_search_output",
            "status": "completed",
            "execution": "server",
            "tools": [loaded],
        },
        {
            "type": "function_call",
            "call_id": "call_test",
            "name": "get_weather",
            "arguments": '{"city":"Paris"}',
            "namespace": "get_weather",
        },
        {"type": "function_call_output", "call_id": "call_test", "output": "Paris: 18C"},
    ]
