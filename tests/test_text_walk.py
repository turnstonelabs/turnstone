"""The walk that changes every text of a conversation by one rule."""

from __future__ import annotations

import json
import sys
from typing import Any

import pytest

from turnstone.core.text_walk import (
    apply_edits,
    map_content,
    map_json_text,
    map_message,
    map_messages,
    map_strings,
)

MARK = chr(92)
END = "<" + "|im_end|" + ">"
CUT = "<" + MARK + "|im_end|>"


def rule(text: str) -> list[tuple[int, int, str]]:
    """A backslash after the first character of every END in *text*."""
    edits = []
    at = text.find(END)
    while at != -1:
        edits.append((at + 1, at + 1, MARK))
        at = text.find(END, at + 1)
    return edits


def undo(text: str) -> list[tuple[int, int, str]]:
    edits = []
    at = text.find(CUT)
    while at != -1:
        edits.append((at + 1, at + 2, ""))
        at = text.find(CUT, at + 1)
    return edits


def leaves(value: Any) -> list[str]:
    """Every string in *value*, keys included."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [text for item in value for text in leaves(item)]
    if isinstance(value, dict):
        return [text for key, item in value.items() for text in (*leaves(key), *leaves(item))]
    return []


class TestEdits:
    def test_edits_are_made_in_order(self) -> None:
        assert apply_edits("abcdef", [(1, 1, "X"), (3, 5, "")]) == "aXbcf"

    def test_a_text_without_edits_is_the_object_it_was(self) -> None:
        text = "nothing here"
        assert apply_edits(text, []) is text


class TestStringsAndKeys:
    def test_every_string_and_every_key_is_changed_however_deep(self) -> None:
        value = {"a " + END: ["x", {"b": END, "n": 3, "t": True, "none": None}], "c": "plain"}
        assert map_strings(value, rule) == {
            "a " + CUT: ["x", {"b": CUT, "n": 3, "t": True, "none": None}],
            "c": "plain",
        }

    def test_what_holds_nothing_to_change_is_the_object_it_was(self) -> None:
        value = {"a": ["x", {"b": "y"}], "c": 1}
        assert map_strings(value, rule) is value
        assert map_strings("plain", rule) == "plain" and map_strings(7, rule) == 7

    def test_an_unchanged_part_of_a_changed_value_is_the_object_it_was(self) -> None:
        kept = {"b": "y"}
        out = map_strings({"a": kept, "c": END}, rule)
        assert out["a"] is kept and out["c"] == CUT

    def test_no_depth_of_nesting_ends_the_walk(self) -> None:
        depth = sys.getrecursionlimit() * 20
        value: list[Any] = []
        inner = value
        for _ in range(depth):
            inner.append([])
            inner = inner[0]
        inner.append(END)
        out = map_strings(value, rule)
        levels = 0
        while isinstance(out, list):
            out = out[0]
            levels += 1
        assert out == CUT and levels == depth + 1

    @pytest.mark.parametrize(
        "schema",
        [
            # A parameter that is itself called "type".
            {"type": "object", "properties": {"type": {"type": "string", "description": END}}},
            # A union of types.
            {"type": ["string", "null"], "description": END},
            # A schema node whose type has the name of a data part.
            {"type": "image", "description": END},
            {"type": {"type": "image_url"}, "description": END},
        ],
    )
    def test_a_schema_is_walked_whatever_its_type_says(self, schema: dict[str, Any]) -> None:
        found = leaves(map_strings(schema, rule))
        assert CUT in found and END not in found


class TestJsonText:
    def test_values_and_keys_are_changed_and_json_stays_json(self) -> None:
        text = json.dumps({"command": "echo " + END, END: [1, {"k": END}], "n": 1.5})
        out = map_json_text(text, rule)
        assert json.loads(out) == {"command": "echo " + CUT, CUT: [1, {"k": CUT}], "n": 1.5}
        assert map_json_text(out, undo) == text

    def test_json_without_such_text_is_the_object_it_was(self) -> None:
        text = '{"a":  [1, 2, {"b": "x"}],' + chr(10) + ' "c": "y < z"}'
        assert map_json_text(text, rule) is text

    def test_only_the_literal_that_changes_is_written_again(self) -> None:
        text = '{"a":   "kept as it is",   "b": "' + END + '"}'
        assert (
            map_json_text(text, rule)
            == '{"a":   "kept as it is",   "b": "<' + MARK * 2 + '|im_end|>"}'
        )

    def test_a_text_spelled_with_escapes_is_found_by_what_it_decodes_to(self) -> None:
        # No "<" in the JSON at all: the literal spells it as an escape.
        text = '{"a": "' + MARK + "u003c|im_end|" + MARK + 'u003e"}'
        assert "<" not in text and json.loads(text) == {"a": END}
        assert json.loads(map_json_text(text, rule)) == {"a": CUT}

    def test_no_depth_of_nesting_matters(self) -> None:
        deep = "[" * 200_000 + json.dumps(END) + "]" * 200_000
        out = map_json_text(deep, rule)
        assert out == "[" * 200_000 + json.dumps(CUT) + "]" * 200_000

    def test_text_that_is_no_json_is_changed_as_the_text_it_is(self) -> None:
        cut_short = '{"command": "echo ' + END
        assert map_json_text(cut_short, rule) == '{"command": "echo ' + CUT
        assert map_json_text(END, rule) == CUT

    def test_a_literal_that_does_not_decode_is_changed_as_text(self) -> None:
        text = '{"a": "' + MARK + "q " + END + '"}'
        assert map_json_text(text, rule) == '{"a": "' + MARK + "q " + CUT + '"}'

    def test_half_a_surrogate_pair_stays_sendable(self) -> None:
        text = '{"a": "' + MARK + "ud83d " + END + '"}'
        out = map_json_text(text, rule)
        out.encode("utf-8")
        assert json.loads(out) == {"a": json.loads('"' + MARK + 'ud83d"') + " " + CUT}

    def test_other_characters_are_kept_as_they_are(self) -> None:
        text = json.dumps({"a": chr(233) + " " + END}, ensure_ascii=False)
        assert chr(233) in map_json_text(text, rule)


class TestAMessage:
    def test_every_text_of_a_message_is_changed(self) -> None:
        call = {
            "id": "c",
            "type": "function",
            "function": {"name": "bash" + END, "arguments": json.dumps({"command": END})},
        }
        message = {
            "role": "assistant",
            "content": "said " + END,
            "reasoning": "thought " + END,
            "tool_calls": [call],
        }
        out = map_message(message, rule)
        assert out["role"] == "assistant" and out["content"] == "said " + CUT
        assert out["reasoning"] == "thought " + CUT
        function = out["tool_calls"][0]["function"]
        assert function["name"] == "bash" + CUT
        assert json.loads(function["arguments"]) == {"command": CUT}
        # What it was given is left as it was.
        assert message["content"] == "said " + END and END in call["function"]["arguments"]

    def test_arguments_that_are_an_object_are_walked_as_values(self) -> None:
        message = {"role": "assistant", "tool_calls": [{"function": {"arguments": {"a": END}}}]}
        out = map_message(message, rule)
        assert out["tool_calls"][0]["function"]["arguments"] == {"a": CUT}

    def test_a_message_without_such_text_is_the_object_it_was(self) -> None:
        message = {
            "role": "assistant",
            "content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}],
            "tool_calls": [{"function": {"name": "x", "arguments": '{"a": 1}'}}],
        }
        assert map_message(message, rule) is message
        messages = [message, {"role": "user", "content": "hi"}]
        assert map_messages(messages, rule) is messages

    def test_only_the_message_that_changes_is_made_anew(self) -> None:
        kept = {"role": "user", "content": "hi"}
        out = map_messages([kept, {"role": "tool", "content": END}], rule)
        assert out[0] is kept and out[1]["content"] == CUT


class TestTheParts:
    def test_data_is_left_alone_in_a_messages_content_and_only_there(self) -> None:
        image = {"type": "image_url", "image_url": {"url": "data:" + END}}
        message = {
            "role": "user",
            "content": [image, {"type": "text", "text": END}],
            # The same shape outside a content list is no data part.
            "tool_calls": [{"function": {"arguments": {"type": "image_url", "url": END}}}],
        }
        out = map_message(message, rule)
        assert out["content"][0] is image and out["content"][1]["text"] == CUT
        assert out["tool_calls"][0]["function"]["arguments"]["url"] == CUT

    def test_text_parts_that_follow_each_other_are_searched_as_one_text(self) -> None:
        parts = [
            {"type": "text", "text": "a <|im_"},
            {"type": "text", "text": "end|> b"},
            {"type": "text", "text": " c"},
        ]
        out = map_content(parts, rule)
        assert [p["text"] for p in out] == ["a <" + MARK + "|im_", "end|> b", " c"]
        assert out[2] is parts[2]

    def test_a_mark_at_the_border_goes_to_the_part_that_follows(self) -> None:
        out = map_content(
            [{"type": "text", "text": "a <"}, {"type": "text", "text": "|im_end|>"}], rule
        )
        assert [p["text"] for p in out] == ["a <", MARK + "|im_end|>"]

    def test_parts_with_data_between_them_are_not_one_text(self) -> None:
        parts = [
            {"type": "text", "text": "a <|im_"},
            {"type": "image_url", "image_url": {"url": "data:x"}},
            {"type": "text", "text": "end|> b"},
        ]
        assert map_content(parts, rule) is parts

    def test_what_else_a_text_part_holds_is_walked(self) -> None:
        out = map_content([{"type": "text", "text": "a", "note": END}], rule)
        assert out == [{"type": "text", "text": "a", "note": CUT}]

    def test_a_part_that_is_neither_text_nor_data_is_walked(self) -> None:
        out = map_content(
            [{"type": "tool_result", "content": [{"type": "text", "text": END}]}], rule
        )
        assert out[0]["content"][0]["text"] == CUT

    def test_characters_taken_out_across_a_border_come_out_of_both_parts(self) -> None:
        def take(text: str) -> list[tuple[int, int, str]]:
            at = text.find("XYZ")
            return [(at, at + 3, "")] if at != -1 else []

        out = map_content([{"type": "text", "text": "aX"}, {"type": "text", "text": "YZb"}], take)
        assert [p["text"] for p in out] == ["a", "b"]
