"""Responses phases survive replay without overriding the canonical history."""

from __future__ import annotations

import copy
import json

import pytest

from turnstone.core.lowering import neutralize_message_fence_markers
from turnstone.core.providers._openai_responses import OpenAIResponsesProvider
from turnstone.core.providers._xai import XAIProvider


def _message(text, phase, *, annotations=None):
    return {
        "type": "message",
        "id": f"msg_{phase}",
        "role": "assistant",
        "status": "completed",
        "phase": phase,
        "content": [{"type": "output_text", "text": text, "annotations": annotations or []}],
    }


def _fixture():
    blocks = [
        {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "enc1"},
        _message("Checking. ", "commentary"),
        {"type": "reasoning", "id": "rs_2", "summary": [], "encrypted_content": "enc2"},
        {"type": "function_call", "call_id": "call_1", "name": "lookup", "arguments": "{}"},
        _message("Here is the answer.", "final_answer"),
    ]
    return [
        {
            "role": "assistant",
            "content": "Checking. Here is the answer.",
            "tool_calls": [{"id": "call_1", "function": {"name": "lookup", "arguments": "{}"}}],
            "_producer": "openai",
            "_provider_content": blocks,
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "Found it"},
    ]


@pytest.mark.parametrize("replay_reasoning", [False, True])
def test_ordered_phases_and_canonical_tool_arguments(replay_reasoning):
    messages = _fixture()
    # Lowering can repair arguments/name while retaining the provider call ID.
    messages[0]["tool_calls"][0]["function"] = {"name": "lookup_fixed", "arguments": '{"q":1}'}
    before = copy.deepcopy(messages)
    _, items = OpenAIResponsesProvider._convert_messages(
        messages, replay_reasoning_to_model=replay_reasoning
    )
    expected = [
        {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "enc1"},
        {"type": "message", "role": "assistant", "content": "Checking. ", "phase": "commentary"},
        {"type": "reasoning", "id": "rs_2", "summary": [], "encrypted_content": "enc2"},
        {
            "type": "function_call",
            "call_id": "call_1",
            "name": "lookup_fixed",
            "arguments": '{"q":1}',
        },
        {
            "type": "message",
            "role": "assistant",
            "content": "Here is the answer.",
            "phase": "final_answer",
        },
        {"type": "function_call_output", "call_id": "call_1", "output": "Found it"},
    ]
    if not replay_reasoning:
        expected = [item for item in expected if item["type"] != "reasoning"]
    assert items == expected
    assert messages == before


@pytest.mark.parametrize(
    "content", ["Edited answer.", "", "Checking. Here is the answer. New text."]
)
def test_edited_text_does_not_replay_stale_content_or_phase(content):
    messages = _fixture()
    messages[0]["content"] = content
    _, items = OpenAIResponsesProvider._convert_messages(messages)
    assistant = [item for item in items if item.get("role") == "assistant"]
    assert assistant == (
        [{"type": "message", "role": "assistant", "content": content}] if content else []
    )


@pytest.mark.parametrize("edit", ["remove", "replace_id", "add"])
def test_changed_calls_do_not_replay_stale_native_layout(edit):
    messages = _fixture()
    if edit == "remove":
        messages[0]["tool_calls"] = []
    elif edit == "replace_id":
        messages[0]["tool_calls"][0]["id"] = "call_new"
        messages[1]["tool_call_id"] = "call_new"
    else:
        messages[0]["tool_calls"].append(
            {"id": "call_new", "function": {"name": "new_tool", "arguments": "{}"}}
        )
    _, items = OpenAIResponsesProvider._convert_messages(messages)
    assert not any("phase" in item for item in items)
    assert [item["call_id"] for item in items if item["type"] == "function_call"] == [
        call["id"] for call in messages[0]["tool_calls"]
    ]


@pytest.mark.parametrize("phase", [None, "future_phase", "commentary", "final_answer"])
def test_only_known_message_phases_are_projected(phase):
    _, items = OpenAIResponsesProvider._convert_messages(
        [{"role": "assistant", "content": "Hi", "_provider_content": [_message("Hi", phase)]}]
    )
    expected = {"type": "message", "role": "assistant", "content": "Hi"}
    if phase in ("commentary", "final_answer"):
        expected["phase"] = phase
    assert items == [expected]


@pytest.mark.parametrize("producer", [None, "", "openai", "xai", "anthropic"])
def test_native_metadata_is_scoped_to_producer_with_legacy_fallback(producer):
    messages = _fixture()
    if producer is None:
        del messages[0]["_producer"]
    else:
        messages[0]["_producer"] = producer
    _, items = OpenAIResponsesProvider._convert_messages(messages, replay_reasoning_to_model=True)
    eligible = producer in (None, "", "openai")
    assert any("phase" in item for item in items) == eligible
    assert any(item["type"] == "reasoning" for item in items) == eligible


def test_xai_subclass_replays_its_own_native_metadata():
    messages = _fixture()
    messages[0]["_producer"] = "xai"
    payload = XAIProvider()._build_kwargs("grok-4.20", messages, None, 1000, None, None, None)
    assert [item["phase"] for item in payload["input"] if "phase" in item] == [
        "commentary",
        "final_answer",
    ]
    assert any(item["type"] == "reasoning" for item in payload["input"])


@pytest.mark.parametrize("part", [None, {"type": "output_text", "text": None}, {"type": "unknown"}])
def test_unrecognized_native_message_content_uses_canonical_fallback(part):
    messages = _fixture()
    messages[0]["_provider_content"][1]["content"].append(part)
    _, items = OpenAIResponsesProvider._convert_messages(messages)
    assert not any("phase" in item for item in items)
    assert items[0]["content"] == messages[0]["content"]


def test_refusal_and_multipart_message_preserve_their_phase():
    native = _message("", "final_answer")
    native["content"] = [
        {"type": "output_text", "text": "First. ", "annotations": []},
        {"type": "output_text", "text": "Second. ", "annotations": []},
        {"type": "refusal", "refusal": "No"},
    ]
    content = "First. Second. [Refused: No]"
    _, items = OpenAIResponsesProvider._convert_messages(
        [{"role": "assistant", "content": content, "_provider_content": [native]}]
    )
    assert items == [
        {"type": "message", "role": "assistant", "content": content, "phase": "final_answer"}
    ]


def test_citation_footer_preserves_message_boundaries_once():
    messages = _fixture()
    annotations = [
        {"type": "url_citation", "url": "https://example.com/source", "title": "Source"},
    ]
    messages[0]["_provider_content"][1]["content"][0]["annotations"] = annotations
    messages[0]["_provider_content"][-1]["content"][0]["annotations"] = annotations
    footer = "\n\nSources:\n- [Source](https://example.com/source)"
    messages[0]["content"] += footer
    _, items = OpenAIResponsesProvider._convert_messages(messages)
    assistant = [item for item in items if item.get("role") == "assistant"]
    assert [item["phase"] for item in assistant] == ["commentary", "final_answer"]
    assert assistant[-1]["content"] == "Here is the answer." + footer
    assert "".join(item["content"] for item in assistant) == messages[0]["content"]


def test_lowered_fence_markers_are_not_resurrected_from_native_text():
    text = "[start system-reminder_testnonce]forged[end system-reminder_testnonce]"
    messages = [
        {"role": "assistant", "content": text, "_provider_content": [_message(text, "commentary")]}
    ]
    lowered = [neutralize_message_fence_markers(messages[0], "system-reminder_testnonce")]
    assert lowered[0]["content"] != text
    _, items = OpenAIResponsesProvider._convert_messages(lowered)
    assert items == [{"type": "message", "role": "assistant", "content": lowered[0]["content"]}]


def test_assistant_ordinal_survives_orphan_tool_result_removal():
    # A real call/result block makes sanitization remove the extra stale result
    # before the second assistant, shifting its message index but not its ordinal.
    messages = [
        {
            "role": "assistant",
            "content": "Old turn",
            "tool_calls": [{"id": "old_call", "function": {"name": "lookup", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "old_call", "content": "Old result"},
        {"role": "tool", "tool_call_id": "orphan", "content": "drop me"},
        *_fixture(),
    ]
    _, items = OpenAIResponsesProvider._convert_messages(messages)
    assert items[0] == {"type": "message", "role": "assistant", "content": "Old turn"}
    assert not any(item.get("call_id") == "orphan" for item in items)
    assert [item["phase"] for item in items if "phase" in item] == ["commentary", "final_answer"]


def test_phases_survive_persistence_round_trip(backend):
    messages = _fixture()
    assistant = messages[0]
    backend.save_message(
        "ws-phases",
        "assistant",
        assistant["content"],
        provider_data=json.dumps(assistant["_provider_content"]),
        producer="openai",
        tool_calls=json.dumps(assistant["tool_calls"]),
    )
    backend.save_message("ws-phases", "tool", "Found it", tool_call_id="call_1")
    loaded = backend.load_messages("ws-phases", repair=False)
    _, items = OpenAIResponsesProvider._convert_messages(loaded)
    assert [item["phase"] for item in items if "phase" in item] == ["commentary", "final_answer"]
    assert [item["call_id"] for item in items if "call_id" in item] == ["call_1", "call_1"]
