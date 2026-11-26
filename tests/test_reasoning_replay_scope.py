"""``model.reasoning_replay_scope = "turn"``: replayed reasoning is limited to the turn in progress."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from tests._session_helpers import make_session
from turnstone.core.history_decoration import (
    attach_vllm_chat_reasoning_field,
    limit_reasoning_to_current_turn,
)
from turnstone.core.providers._protocol import ModelCapabilities


def _thought(text: str) -> dict[str, Any]:
    return {"type": "reasoning_text", "text": text, "source": "vllm"}


def _assistant(text: str, *blocks: dict[str, Any], calls: bool = False) -> dict[str, Any]:
    msg: dict[str, Any] = {"role": "assistant", "content": text}
    if blocks:
        msg["_provider_content"] = list(blocks)
    if calls:
        msg["tool_calls"] = [
            {"id": "c1", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}
        ]
    return msg


def _history() -> list[dict[str, Any]]:
    return [
        {"role": "system", "content": "prompt"},
        {"role": "user", "content": "first question"},
        _assistant("", _thought("old plan"), calls=True),
        {"role": "tool", "tool_call_id": "c1", "content": "old result"},
        _assistant("first answer", _thought("old wrap-up")),
        {"role": "user", "content": "second question"},
        _assistant("", _thought("current plan"), calls=True),
        {"role": "tool", "tool_call_id": "c1", "content": "current result"},
        {
            "role": "system",
            "content": "User message: also check the fans",
            "_source": "user_interjection",
        },
        _assistant("", _thought("current step two"), calls=True),
        {"role": "tool", "tool_call_id": "c1", "content": "second result"},
    ]


def _replayed(messages: list[dict[str, Any]]) -> list[str]:
    return [m["reasoning"] for m in attach_vllm_chat_reasoning_field(messages) if "reasoning" in m]


def test_reasoning_before_the_last_user_message_is_left_out() -> None:
    history = _history()
    assert _replayed(history) == ["old plan", "old wrap-up", "current plan", "current step two"]

    scoped = limit_reasoning_to_current_turn(history)

    # A queued interjection is a system row: it does not end the turn.
    assert _replayed(scoped) == ["current plan", "current step two"]
    # Nothing but the reasoning changed, and the input was not touched.
    assert [m["role"] for m in scoped] == [m["role"] for m in history]
    assert scoped[2]["tool_calls"] == history[2]["tool_calls"]
    assert scoped[4]["content"] == "first answer"
    assert "_provider_content" in history[2] and "_provider_content" not in scoped[2]
    for index in (0, 1, 3, 5, 6, 7, 8, 9, 10):
        assert scoped[index] is history[index]


def test_a_first_turn_or_a_history_without_old_reasoning_is_returned_as_is() -> None:
    first_turn = _history()[5:]
    assert limit_reasoning_to_current_turn(first_turn) is first_turn
    no_user: list[dict[str, Any]] = [_assistant("x", _thought("t"))]
    assert limit_reasoning_to_current_turn(no_user) is no_user
    plain = [
        {"role": "user", "content": "a"},
        _assistant("b"),
        {"role": "user", "content": "c"},
    ]
    assert limit_reasoning_to_current_turn(plain) is plain
    assert limit_reasoning_to_current_turn([]) == []


def test_other_blocks_of_an_earlier_message_are_kept() -> None:
    thinking = {"type": "thinking", "thinking": "old", "signature": "sig"}
    tool_use = {"type": "tool_use", "id": "c1", "name": "lookup", "input": {}}
    text = {"type": "text", "text": "calling"}
    responses_item = {"type": "reasoning", "id": "rs_1", "encrypted_content": "..."}
    history = [
        {"role": "user", "content": "q1"},
        _assistant("calling", thinking, text, tool_use),
        {"role": "tool", "tool_call_id": "c1", "content": "r"},
        _assistant("done", responses_item),
        {"role": "user", "content": "q2"},
    ]

    scoped = limit_reasoning_to_current_turn(history)

    assert scoped[1]["_provider_content"] == [text, tool_use]
    # An OpenAI Responses reasoning item is referenced by its siblings: left alone.
    assert scoped[3] is history[3]


@pytest.mark.parametrize(
    ("scope", "expected"),
    [
        ("turn", ["current plan", "current step two"]),
        ("all", ["old plan", "old wrap-up", "current plan", "current step two"]),
        (None, ["old plan", "old wrap-up", "current plan", "current step two"]),
    ],
)
def test_the_session_applies_the_setting_before_lowering(tmp_db, scope: Any, expected: Any) -> None:
    session = make_session()
    store = MagicMock()
    store.get.side_effect = lambda key: scope if key == "model.reasoning_replay_scope" else None
    session._config_store = store

    wire = session._prepare_lowered_wire_messages(_history(), caps=ModelCapabilities())

    assert _replayed(wire) == expected


def test_a_session_without_a_store_or_with_a_failing_one_replays_everything(tmp_db) -> None:
    session = make_session()
    session._config_store = None
    assert session._scope_replayed_reasoning(_history()) == _history()

    broken = MagicMock()
    broken.get.side_effect = RuntimeError("store down")
    session._config_store = broken
    assert len(_replayed(session._scope_replayed_reasoning(_history()))) == 4
