"""The reply reader: recorded replies with what a server of the format read in
them, and the rules one by one.  No live server."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from turnstone.rollout.replyformat import (
    PARAMETER_BLOCKS,
    Marker,
    ParameterBlocks,
    ParsedCall,
    Piece,
    pieces_from_chunks,
    pieces_from_tokens,
    reply_format,
    schema_types,
    typed_value,
)

NL = chr(10)
FIXTURES = json.loads(
    (Path(__file__).parent / "data" / "rollout_reply_fixtures.json").read_text(encoding="ascii")
)
THINK, THINK_END = Marker("reasoning_open"), Marker("reasoning_close")
CALL, CALL_END = Marker("call_open"), Marker("call_close")
IDS = {"reasoning_open": 3, "reasoning_close": 4, "call_open": 5, "call_close": 6}
TEXT = PARAMETER_BLOCKS.token_markers


def tool(name: str, **properties: Any) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {"name": name, "parameters": {"type": "object", "properties": properties}},
    }


TOOLS = [
    tool("bash", command={"type": "string"}, timeout={"type": "integer"}),
    tool("ping"),
]


def block(name: str, **arguments: str) -> str:
    """One function block as the format's template writes it."""
    out = "<function=" + name + ">" + NL
    for key, value in arguments.items():
        out += "<parameter=" + key + ">" + NL + value + NL + "</parameter>" + NL
    return out + "</function>" + NL


def read(*pieces: Piece, tools: Any = TOOLS, in_reasoning: bool = True) -> Any:
    return PARAMETER_BLOCKS.read(list(pieces), tools=tools, in_reasoning=in_reasoning)


def pieces_of(case: dict[str, Any]) -> list[Piece]:
    return [Marker(p["marker"]) if isinstance(p, dict) else p for p in case["pieces"]]


@pytest.mark.parametrize("case", FIXTURES["cases"], ids=[c["name"] for c in FIXTURES["cases"]])
def test_a_recorded_reply_is_read_as_the_server_read_it(case: dict[str, Any]) -> None:
    tools = FIXTURES["tools"][case["tools"]] if case["tools"] else None
    reply = PARAMETER_BLOCKS.read(pieces_of(case), tools=tools, in_reasoning=case["in_reasoning"])
    expected = case["expected"]
    assert reply.reasoning == expected["reasoning"]
    assert reply.content == expected["content"]
    # The arguments are compared as text: key order, spacing and the type of
    # every value are part of what the session is handed.
    assert [[c.name, c.arguments] for c in reply.calls] == expected["calls"]


def test_the_fixtures_cover_what_they_are_there_for() -> None:
    cases = FIXTURES["cases"]
    live = [c for c in cases if c["name"].startswith("live reply")]
    assert len(live) >= 40 and len(cases) >= 150
    assert any(len(c["expected"]["calls"]) > 1 for c in live)
    assert any(c["expected"]["reasoning"] is None for c in live)
    assert any(not c["in_reasoning"] for c in live)
    assert any("server_reads" in c for c in cases)
    typed = {
        type(value).__name__
        for c in live
        for _name, arguments in c["expected"]["calls"]
        for value in json.loads(arguments).values()
    }
    assert {"str", "int", "float", "bool", "NoneType", "list", "dict"} <= typed


class TestReading:
    def test_a_plain_answer(self) -> None:
        reply = read("Short thought." + NL, THINK_END, NL + NL + "The answer.")
        assert (reply.reasoning, reply.content, reply.calls) == (
            "Short thought." + NL,
            NL + NL + "The answer.",
            (),
        )

    def test_a_reply_cut_inside_the_reasoning_is_all_reasoning(self) -> None:
        reply = read("Thinking about it and never")
        assert (reply.reasoning, reply.content, reply.calls) == (
            "Thinking about it and never",
            None,
            (),
        )

    def test_reasoning_that_is_empty_is_none_and_white_space_stays(self) -> None:
        assert read(THINK_END, "x").reasoning is None
        assert read(NL, THINK_END, "x").reasoning == NL

    def test_a_reply_that_does_not_begin_in_reasoning_has_none(self) -> None:
        reply = read("The answer.", in_reasoning=False)
        assert (reply.reasoning, reply.content) == (None, "The answer.")

    def test_a_call_with_the_text_before_it_stripped(self) -> None:
        reply = read(
            "r",
            THINK_END,
            NL + NL + "I look." + NL + NL,
            CALL,
            NL + block("bash", command="ls -la", timeout="30"),
            CALL_END,
        )
        assert reply.content == "I look."
        assert reply.calls == (ParsedCall("bash", '{"command": "ls -la", "timeout": 30}'),)

    def test_white_space_alone_before_a_call_is_no_content(self) -> None:
        reply = read("r", THINK_END, NL + NL, CALL, NL + block("ping"), CALL_END)
        assert reply.content is None
        assert reply.calls == (ParsedCall("ping", "{}"),)

    def test_several_calls_and_the_text_between_and_after_them_dropped(self) -> None:
        reply = read(
            "r",
            THINK_END,
            NL + NL,
            CALL,
            NL + block("ping"),
            CALL_END,
            NL + "and" + NL,
            CALL,
            NL + block("bash", command="pwd"),
            CALL_END,
            NL + "Done.",
        )
        assert reply.content is None
        assert [c.name for c in reply.calls] == ["ping", "bash"]

    def test_a_call_straight_out_of_the_reasoning_ends_it(self) -> None:
        reply = read("r", CALL, NL + block("ping"), CALL_END)
        assert (reply.reasoning, reply.content) == ("r", None)
        assert reply.calls == (ParsedCall("ping", "{}"),)

    def test_a_call_cut_at_the_limit_keeps_its_finished_arguments(self) -> None:
        cut = "<function=bash>" + NL + "<parameter=command>" + NL + "ls" + NL + "</parameter>"
        reply = read("r", THINK_END, CALL, NL + cut + NL + "<parameter=timeout>" + NL + "3")
        assert reply.calls == (ParsedCall("bash", '{"command": "ls"}'),)

    def test_a_call_cut_inside_its_name_is_a_call_without_arguments(self) -> None:
        assert read("r", THINK_END, CALL, NL + "<function=ba").calls == (ParsedCall("ba", "{}"),)

    def test_a_function_block_without_the_call_marker_is_a_call(self) -> None:
        reply = read("r", THINK_END, "x" + NL + block("ping"))
        assert (reply.content, reply.calls) == ("x", (ParsedCall("ping", "{}"),))

    def test_an_argument_written_twice_counts_as_written_last(self) -> None:
        twice = block("bash", command="ls")[: -len("</function>" + NL)]
        twice += "<parameter=command>" + NL + "pwd" + NL + "</parameter>" + NL + "</function>"
        assert read("r", THINK_END, CALL, NL + twice, CALL_END).calls == (
            ParsedCall("bash", '{"command": "pwd"}'),
        )

    def test_a_value_keeps_its_inner_line_breaks_and_loses_one_at_each_end(self) -> None:
        value = NL + "a" + NL + NL + "b" + NL
        reply = read("r", THINK_END, CALL, NL + block("bash", command=value), CALL_END)
        assert json.loads(reply.calls[0].arguments) == {"command": value}

    def test_an_unknown_tool_and_an_unknown_argument_stay_text(self) -> None:
        reply = read("r", THINK_END, CALL, NL + block("other", n="3"), CALL_END)
        assert reply.calls == (ParsedCall("other", '{"n": "3"}'),)
        reply = read("r", THINK_END, CALL, NL + block("bash", n="3"), CALL_END)
        assert reply.calls == (ParsedCall("bash", '{"n": "3"}'),)

    def test_a_request_without_tools_is_given_no_calls(self) -> None:
        reply = read("r", THINK_END, "x", CALL, NL + block("ping"), CALL_END, tools=None)
        assert (reply.content, reply.calls) == ("x", ())

    def test_a_marker_spelled_out_in_text_is_text(self) -> None:
        # The same characters, not generated as the marker's own token.
        spelled = "print " + TEXT["call_open"] + " and " + TEXT["reasoning_close"]
        reply = read("r", THINK_END, CALL, NL + block("bash", command=spelled), CALL_END)
        assert json.loads(reply.calls[0].arguments) == {"command": spelled}
        assert read(TEXT["reasoning_close"] + " still thinking").content is None

    def test_a_marker_the_server_has_no_token_for_is_taken_from_the_text(self) -> None:
        text = "r" + TEXT["reasoning_close"] + TEXT["call_open"] + NL + block("ping")
        reply = PARAMETER_BLOCKS.read(
            [text + TEXT["call_close"]],
            tools=TOOLS,
            in_reasoning=True,
            text_markers=("reasoning_close", "call_open", "call_close"),
        )
        assert (reply.reasoning, reply.calls) == ("r", (ParsedCall("ping", "{}"),))

    def test_a_closing_reasoning_marker_after_the_reasoning_is_dropped(self) -> None:
        reply = read(
            "r",
            THINK_END,
            "a",
            THINK_END,
            "b",
            CALL,
            NL,
            "<function=ba",
            THINK_END,
            "sh>",
            CALL_END,
        )
        assert (reply.content, reply.calls) == ("ab", (ParsedCall("bash", "{}"),))

    def test_an_opening_reasoning_marker_is_dropped_inside_and_text_outside(self) -> None:
        reply = read(THINK, "r", THINK_END, "a", THINK, "b")
        assert (reply.reasoning, reply.content) == ("r", "a" + TEXT["reasoning_open"] + "b")

    def test_nameless_calls_leave_the_text_after_them(self) -> None:
        reply = read("r", THINK_END, "before ", CALL, CALL_END, "after")
        assert (reply.content, reply.calls) == ("before after", ())


class TestTypes:
    @pytest.mark.parametrize(
        ("types", "text", "value"),
        [
            (["string"], "null", "null"),
            (["string"], "12", "12"),
            (["integer"], "12", 12),
            (["integer"], "007", 7),
            (["integer"], " 5 ", 5),
            (["integer"], "1e3", 1000.0),
            (["integer"], "3.0", 3.0),
            (["integer"], "x", "x"),
            (["number"], "3", 3),
            (["number"], "3.0", 3),
            (["number"], "3.5", 3.5),
            (["number"], "1e3", 1000),
            (["number"], "NaN", "NaN"),
            (["number"], "1e999", "1e999"),
            (["boolean"], "True", True),
            (["boolean"], " false ", False),
            (["boolean"], "1", True),
            (["boolean"], "yes", "yes"),
            (["null"], "NULL", None),
            (["string", "null"], "null", None),
            (["string", "null"], "None", "None"),
            (["array"], "[1, 2]", [1, 2]),
            (["array"], "a,b", "a,b"),
            (["array"], "(1, 2)", "(1, 2)"),
            (["object"], '{"a": 1}', {"a": 1}),
            (["object"], "{'a': 1}", "{'a': 1}"),
            (["object"], "[1e999]", "[1e999]"),
            (["object"], "5", 5),
            (["boolean", "integer"], "1", 1),
            (["number", "string"], "x", "x"),
            (["float"], "2", 2),
            (["int"], "2", 2),
            (["bool"], "true", True),
            (["str"], "true", "true"),
            (["unheard-of"], "[1]", [1]),
            (["unheard-of"], "plain", "plain"),
        ],
    )
    def test_a_text_becomes_the_first_type_that_fits(
        self, types: list[str], text: str, value: Any
    ) -> None:
        typed = typed_value(text, types)
        assert typed == value and type(typed) is type(value)

    def test_a_number_of_more_digits_than_the_interpreter_converts_stays_text(self) -> None:
        """Converting digits to an integer takes more than linear time, so the
        interpreter refuses beyond a number of digits; the reader's time rests
        on that, and what it reads there is the text."""
        short, long = "7" * 4300, "7" * 4301
        assert typed_value(short, ["integer"]) == int(short)
        assert typed_value(long, ["integer"]) == long
        assert typed_value(long, ["number"]) == long
        assert typed_value("[" + long + "]", ["array"]) == "[" + long + "]"
        assert typed_value('{"n": ' + long + "}", ["object"]) == '{"n": ' + long + "}"

    def test_what_is_read_can_be_carried_by_a_request(self) -> None:
        """JSON a model wrote can spell half of a surrogate pair as an escape.
        No request body can carry the character, so the session could not
        send such a call back.  Each half is replaced, wherever JSON is read."""
        escape = chr(92) + "ud800"
        gone = chr(0xFFFD)

        def calls(body: str, tools: Any = TOOLS) -> list[tuple[str, Any]]:
            reply = read("r", THINK_END, CALL, NL + body, CALL_END, tools=tools)
            for call in reply.calls:
                call.name.encode("utf-8")
                call.arguments.encode("utf-8")
            return [(c.name, json.loads(c.arguments)) for c in reply.calls]

        # A value typed by the tool's schema, alone and inside a list.
        assert calls(block("bash", timeout='"' + escape + '"')) == [("bash", {"timeout": gone})]
        listed = [tool("f", n={"type": "array"})]
        assert calls(block("f", n='["a' + escape + '"]'), listed) == [("f", {"n": ["a" + gone]})]
        # A call written as a JSON object: its arguments, and its name.

        def as_json(text: str) -> str:
            return "<function= >" + NL + text + NL + "</function>"

        written = '{"name": "bash", "arguments": {"command": "' + escape + '"}}'
        assert calls(as_json(written)) == [("bash", {"command": gone})]
        named = '{"name": "ba' + escape + 'sh", "arguments": {}}'
        assert calls(as_json(named)) == [("ba" + gone + "sh", {})]
        # Both halves written as two escapes are the character they spell,
        # and a string argument keeps the escape as the text it is.
        pair = chr(92) + "ud83d" + chr(92) + "ude00"
        assert calls(block("bash", timeout='"' + pair + '"')) == [
            ("bash", {"timeout": chr(0x1F600)})
        ]
        assert calls(block("bash", command=escape)) == [("bash", {"command": escape})]

    def test_the_types_a_schema_allows(self) -> None:
        assert schema_types({"type": "integer"}) == ["integer"]
        assert sorted(schema_types({"type": ["string", "null"]})) == ["null", "string"]
        assert sorted(schema_types({"enum": ["a", 1, None, True, 1.5, [], {}]})) == [
            "array",
            "boolean",
            "integer",
            "null",
            "number",
            "object",
            "string",
        ]
        assert sorted(schema_types({"anyOf": [{"type": "integer"}, {"type": "null"}]})) == [
            "integer",
            "null",
        ]
        assert schema_types({"description": "anything"}) == ["string"]
        assert schema_types(None) == ["string"]

    def test_the_first_tool_of_a_name_gives_the_types(self) -> None:
        tools = [tool("f", n={"type": "integer"}), tool("f", n={"type": "string"})]
        reply = read("r", THINK_END, CALL, NL + block("f", n="3"), CALL_END, tools=tools)
        assert reply.calls == (ParsedCall("f", '{"n": 3}'),)


class TestPieces:
    def test_from_tokens_whose_bytes_are_known(self) -> None:
        # A character split over two tokens is put together before decoding.
        euro = chr(0x20AC).encode()
        pieces = pieces_from_tokens(
            [b"a", euro[:1], euro[1:], b"", b"b", b""], [10, 11, 12, 4, 13, 5], IDS
        )
        assert pieces == ["a" + chr(0x20AC), THINK_END, "b", CALL]

    def test_from_parts_with_their_token_ids(self) -> None:
        parts = [
            ("thought" + TEXT["reasoning_close"], [10, 4]),
            (NL + NL, [11]),
            (TEXT["call_open"] + NL + "<fun", [5, 12, 13]),
            ("ction=ping>" + TEXT["call_close"], [14, 15, 6]),
        ]
        pieces, missing, uncertain = pieces_from_chunks(parts, IDS, TEXT)
        assert (missing, uncertain) == (0, 0)
        assert pieces == ["thought", THINK_END, NL + NL, CALL, NL + "<function=ping>", CALL_END]

    def test_a_marker_spelled_out_across_parts_stays_text(self) -> None:
        marker = TEXT["call_open"]
        parts = [("say " + marker[:5], [10, 11]), (marker[5:] + " now", [12, 13])]
        assert pieces_from_chunks(parts, IDS, TEXT) == (["say " + marker + " now"], 0, 0)

    def test_a_marker_spelled_out_in_a_part_without_its_token_stays_text(self) -> None:
        marker = TEXT["call_close"]
        assert pieces_from_chunks([("see " + marker, [10, 11, 12])], IDS, TEXT) == (
            ["see " + marker],
            0,
            0,
        )

    def test_a_marker_token_without_its_text_is_counted(self) -> None:
        assert pieces_from_chunks([("abc", [10, 6])], IDS, TEXT) == (["abc"], 1, 0)
        # One text for two tokens: one of them has none.
        close = TEXT["call_close"]
        assert pieces_from_chunks([(close, [6, 6])], IDS, TEXT) == ([CALL_END], 1, 0)


class TestAMarkerIsBoundToItsToken:
    """A part of a reply can hold a marker's token and, beside it, the same
    characters spelled with ordinary tokens.  The order of the part's token
    IDs says which of the two the marker is, wherever that order decides it:
    the same tokens are then read the same however the server cut the reply
    into parts."""

    CLOSE = TEXT["reasoning_close"]
    ANSWER = (
        NL + NL + "The closing tag is " + TEXT["reasoning_close"] + " and it ends the reasoning."
    )
    # The answer's tokens: any IDs that are no marker's.
    WORDS = list(range(100, 115))

    def read_parts(self, parts: list[tuple[str, list[int]]]) -> tuple[Any, int, int]:
        pieces, missing, uncertain = pieces_from_chunks(parts, IDS, TEXT)
        reply = PARAMETER_BLOCKS.read(pieces, tools=None, in_reasoning=True)
        return (reply.reasoning, reply.content), missing, uncertain

    def test_the_token_first_in_its_part_is_the_first_occurrence(self) -> None:
        """The reasoning ends, and the answer spells the closing tag out: one
        part holds the marker's token and, after it, the same characters as
        ordinary tokens.  The marker's token is the first of its part."""
        thought = ("I think." + NL, [40, 41, 42, 43])
        together = [thought, (self.CLOSE + self.ANSWER, [4, *self.WORDS])]
        apart = [thought, (self.CLOSE, [4]), (self.ANSWER, self.WORDS)]
        expected = (("I think." + NL, self.ANSWER), 0, 0)
        assert self.read_parts(together) == expected
        assert self.read_parts(apart) == expected

    def test_the_token_last_in_its_part_is_the_last_occurrence(self) -> None:
        """The other way round: the reasoning spells the tag out, then ends."""
        thought = "It ends with " + self.CLOSE + "."
        together = [(thought + self.CLOSE, [*self.WORDS, 4]), ("Done.", [50, 51])]
        apart = [(thought, self.WORDS), (self.CLOSE, [4]), ("Done.", [50, 51])]
        expected = ((thought, "Done."), 0, 0)
        assert self.read_parts(together) == expected
        assert self.read_parts(apart) == expected

    def test_marker_tokens_side_by_side_stand_side_by_side(self) -> None:
        close, call = TEXT["reasoning_close"], TEXT["call_open"]
        # Two marker tokens end the part: the last ends its text, the other
        # stands right before it, whatever the text before them spells.
        spelled = "about " + close + call + " "
        pieces, missing, uncertain = pieces_from_chunks(
            [(spelled + close + call, [*self.WORDS, 4, 5])], IDS, TEXT
        )
        assert (pieces, missing, uncertain) == ([spelled, THINK_END, CALL], 0, 0)
        # Two marker tokens begin the part: the occurrences at its start.
        pieces, missing, uncertain = pieces_from_chunks(
            [(close + call + " then " + close + call, [4, 5, *self.WORDS])], IDS, TEXT
        )
        assert (pieces, missing, uncertain) == ([THINK_END, CALL, " then " + close + call], 0, 0)

    def test_two_tokens_of_one_marker_leave_the_third_text_between_them(self) -> None:
        close = TEXT["call_close"]
        # First and last of the part are the tokens, so the middle one is text.
        pieces, missing, uncertain = pieces_from_chunks(
            [(close + " a " + close + " b " + close, [6, *self.WORDS, 6])], IDS, TEXT
        )
        assert (pieces, missing, uncertain) == ([CALL_END, " a " + close + " b ", CALL_END], 0, 0)

    def test_a_token_between_other_tokens_is_taken_for_the_first_and_counted(self) -> None:
        """Other tokens before the marker's and after it, and its text twice:
        the part alone does not say which is the token.  The first is taken,
        and the count says that one binding is not certain."""
        text = "a " + self.CLOSE + " b " + self.CLOSE + " c"
        pieces, missing, uncertain = pieces_from_chunks([(text, [10, 11, 4, 12, 13])], IDS, TEXT)
        assert (pieces, missing, uncertain) == (["a ", THINK_END, " b " + self.CLOSE + " c"], 0, 1)
        # With its text once in the part nothing is uncertain.
        once = [("a " + self.CLOSE + " b", [10, 11, 4, 12])]
        assert pieces_from_chunks(once, IDS, TEXT) == (["a ", THINK_END, " b"], 0, 0)

    def test_text_that_was_held_back_before_the_first_token_does_not_mislead(self) -> None:
        # A character completed late can stand before a marker whose token is
        # the first of its part.
        late = chr(0xFFFD)
        pieces, missing, uncertain = pieces_from_chunks(
            [(late + self.CLOSE + " x " + self.CLOSE, [4, *self.WORDS])], IDS, TEXT
        )
        assert (pieces, missing, uncertain) == ([late, THINK_END, " x " + self.CLOSE], 0, 0)


# A reply at the reply limit of a training run, in tokens, and the longest run
# of spaces that is one token for the models that use the format.
REPLY_TOKENS = 32_768
SPACES = " " * 128
# A reply as long as a server's whole context window.
WINDOW_TOKENS = 8 * REPLY_TOKENS
TIME_TOOLS = [
    tool(
        "bash",
        command={"type": "string"},
        timeout={"type": "integer"},
        ratio={"type": "number"},
        items={"type": "array"},
        options={"type": "object"},
    )
]
HEAD = NL + "<function=bash>" + NL
ARGUMENT = "<parameter=command>" + NL + "ls" + NL + "</parameter>" + NL


def in_call(body: str) -> list[Piece]:
    """A reply whose reasoning is over and whose call holds *body*."""
    return [THINK_END, CALL, HEAD + body]


def nameless(body: str) -> list[Piece]:
    """A call whose function tag names nothing: a name is looked for in *body*."""
    return [THINK_END, CALL, NL + "<function=>" + body]


# Replies a model can write that a reader could take more than linear time
# over, each for a reply of *n* tokens.  White space is counted at 128
# characters a token and a tag at one token, which is more than a model gets.
SHAPES: dict[str, Any] = {
    # A long run of white space after each part of each tag.
    "white space after <": lambda n: in_call("<" + SPACES * n),
    "white space after <parameter": lambda n: in_call("<parameter" + SPACES * n),
    "white space after <parameter=": lambda n: in_call("<parameter=" + SPACES * n),
    "white space after <parameter=k": lambda n: in_call("<parameter=k" + SPACES * n),
    "white space after <parameter=k>": lambda n: in_call("<parameter=k>" + SPACES * n),
    "white space after <parameter=k>v<": lambda n: in_call("<parameter=k>v<" + SPACES * n),
    "white space after <parameter=k>v</": lambda n: in_call("<parameter=k>v</" + SPACES * n),
    "white space after <parameter=k>v</parameter": lambda n: in_call(
        "<parameter=k>v</parameter" + SPACES * n
    ),
    "white space after <parameter= and a value that never ends": lambda n: in_call(
        "<parameter=" + SPACES * n + ">" + "x" * 1000
    ),
    "white space inside every tag of many arguments": lambda n: in_call(
        (
            ("<" + SPACES + "parameter" + SPACES + "=" + SPACES + "k>v")
            + ("<" + SPACES + "/" + SPACES + "parameter" + SPACES + ">")
        )
        * (n // 6)
    ),
    "<parameter= and white space, many times": lambda n: in_call(("<parameter=" + SPACES) * n),
    "<parameter and white space, many times": lambda n: in_call(("<parameter" + SPACES) * n),
    "< and white space, many times": lambda n: in_call(("<" + SPACES) * n),
    "</ and white space, many times, in a value": lambda n: in_call(
        "<parameter=k>" + ("</" + SPACES) * n
    ),
    "</parameter and white space, many times, in a value": lambda n: in_call(
        "<parameter=k>" + ("</parameter" + SPACES) * n
    ),
    'white space after "name"': lambda n: nameless('"name"' + SPACES * n),
    'white space after "name":': lambda n: nameless('"name":' + SPACES * n),
    'a name that never ends after "name":"': lambda n: nameless('"name":"' + SPACES * n),
    '"name" and white space, many times': lambda n: nameless(('"name"' + SPACES) * n),
    '"name": and white space, many times': lambda n: nameless(('"name":' + SPACES) * n),
    # Many openings that are never closed.
    "<parameter= many times": lambda n: in_call("<parameter=" * n),
    "<parameter= many times, then white space": lambda n: in_call(
        "<parameter=" * (n // 2) + SPACES * (n // 2)
    ),
    "<parameter= many times, then one closing bracket": lambda n: in_call("<parameter=" * n + ">"),
    "<parameter=k> many times": lambda n: in_call("<parameter=k>" * n),
    "<parameter=k> and white space, many times": lambda n: in_call(("<parameter=k>" + SPACES) * n),
    "<function= many times": lambda n: [THINK_END, "<function=" * n],
    "<function=f> many times": lambda n: [THINK_END, "<function=f>" * n],
    "call markers, none closed": lambda n: [THINK_END, *([CALL] * n)],
    "call markers, each with a function that is never closed": lambda n: [
        THINK_END,
        *([CALL, HEAD + "<parameter=command>" + NL + "ls"] * (n // 2)),
    ],
    # A marker repeated thousands of times.
    "closing reasoning markers": lambda n: ["r", *([THINK_END] * n)],
    "opening reasoning markers inside the reasoning": lambda n: [*([THINK, "r"] * (n // 2))],
    "whole calls": lambda n: [
        THINK_END,
        *([CALL, HEAD + ARGUMENT + "</function>" + NL, CALL_END] * (n // 3)),
    ],
    "</parameter> many times": lambda n: in_call("</parameter>" * n),
    "</function> many times": lambda n: in_call("</function>" * n),
    "a reasoning marker spelled out, many times": lambda n: [TEXT["reasoning_close"] * n],
    "a call marker spelled out, many times": lambda n: [THINK_END, TEXT["call_open"] * n],
    # Values as long as a reply.
    "an integer": lambda n: in_call(
        "<parameter=timeout>" + NL + "7" * (16 * n) + NL + "</parameter>"
    ),
    "a number": lambda n: in_call("<parameter=ratio>" + NL + "7" * (16 * n) + NL + "</parameter>"),
    "a list": lambda n: in_call(
        "<parameter=items>" + NL + "[" + "1, " * (4 * n) + "1]" + NL + "</parameter>"
    ),
    "nothing but opening brackets": lambda n: in_call(
        "<parameter=options>" + NL + "[" * (16 * n) + NL + "</parameter>"
    ),
    "a text": lambda n: in_call("<parameter=command>" + NL + SPACES * n + NL + "</parameter>"),
}
# Replies whose text arrives in very many pieces.  A reader that puts a text
# together piece by piece copies it once per piece; that is quadratic too, with
# a small factor, so these go on to the length of a whole window.
MANY_PIECES: dict[str, Any] = {
    "white space, then closing brackets, in a call's body": lambda n: in_call(
        SPACES * (n // 2) + "> " * (n // 2)
    ),
    "white space and closing brackets in turn, in a call's body": lambda n: in_call(
        (SPACES + ">") * (n // 2)
    ),
    "white space, then closing tags, in a call's name": lambda n: [
        THINK_END,
        CALL,
        NL + "<function=" + SPACES * (n // 2) + "</parameter> " * (n // 2),
    ],
}
# The sizes a shape is read at, smallest first, each with the seconds it may
# take.  A reader that is quadratic fails at one of the small sizes, after
# seconds; only a linear one gets to the sizes that would take it hours.
SMALL = [(REPLY_TOKENS // 1024, 0.4), (REPLY_TOKENS // 64, 0.4), (REPLY_TOKENS // 8, 0.4)]
BOUND = 3.0
# A reply as long as the window is eight replies long, and a linear reader
# takes eight times as long over it: on a slow machine that traces every
# line, more than a reply's own bound.
WINDOW_BOUND = BOUND * (WINDOW_TOKENS // REPLY_TOKENS)


def seconds_to(read_it: Any) -> float:
    started = time.perf_counter()
    read_it()
    return time.perf_counter() - started


class TestTime:
    """Reading takes time in proportion to the reply, whatever the model wrote.

    The reply is read inside the model call with the interpreter held, so a
    reader that takes quadratic time over a reply stalls the run for as long
    as the model likes.  The patterns this reader had took about nine hours
    over a reply that is white space after ``<parameter=``; it now takes
    hundredths of a second.
    """

    @pytest.mark.parametrize("shape", list(SHAPES))
    def test_a_reply_at_the_reply_limit_is_read_at_once(self, shape: str) -> None:
        for tokens, bound in [*SMALL, (REPLY_TOKENS, BOUND)]:
            pieces = SHAPES[shape](tokens)
            took = seconds_to(
                lambda p=pieces: PARAMETER_BLOCKS.read(p, tools=TIME_TOOLS, in_reasoning=True)
            )
            assert took < bound, f"{tokens} tokens took {took:.2f} s"

    @pytest.mark.parametrize("shape", list(MANY_PIECES))
    def test_a_text_in_very_many_pieces_is_put_together_once(self, shape: str) -> None:
        for tokens, bound in [*SMALL, (REPLY_TOKENS, BOUND), (WINDOW_TOKENS, WINDOW_BOUND)]:
            pieces = MANY_PIECES[shape](tokens)
            took = seconds_to(
                lambda p=pieces: PARAMETER_BLOCKS.read(p, tools=TIME_TOOLS, in_reasoning=True)
            )
            assert took < bound, f"{tokens} tokens took {took:.2f} s"

    @pytest.mark.parametrize(
        ("text", "token_ids"),
        [
            # One token of white space a part; marker tokens whose text never
            # came; and one part holding every token of the reply.
            (SPACES, [10]),
            ("x", [4, 5, 6, 4]),
            (TEXT["reasoning_close"] + SPACES + TEXT["call_open"], [4, 10, 5]),
        ],
    )
    def test_the_parts_of_a_reply_are_put_together_once(
        self, text: str, token_ids: list[int]
    ) -> None:
        for tokens, bound in [*SMALL, (REPLY_TOKENS, BOUND), (WINDOW_TOKENS, WINDOW_BOUND)]:
            parts = [(text, token_ids)] * tokens
            took = seconds_to(lambda p=parts: pieces_from_chunks(p, IDS, TEXT))
            assert took < bound, f"{tokens} parts took {took:.2f} s"
            one = [(text * tokens, token_ids * tokens)]
            took = seconds_to(lambda p=one: pieces_from_chunks(p, IDS, TEXT))
            assert took < bound, f"one part of {tokens} times the tokens took {took:.2f} s"

    @pytest.mark.parametrize(
        "text",
        [
            TEXT["call_open"],
            TEXT["call_open"] + TEXT["call_close"],
            TEXT["call_open"] + HEAD + "<parameter=k>" + SPACES + TEXT["call_close"],
            TEXT["call_open"] + HEAD + ARGUMENT + ARGUMENT + "</function>" + TEXT["call_close"],
            TEXT["call_open"] + HEAD + "</function>" + SPACES + "<parameter=",
        ],
    )
    def test_the_arguments_as_written_are_found_in_linear_time(self, text: str) -> None:
        for tokens, bound in [*SMALL, (REPLY_TOKENS, BOUND)]:
            whole = text * tokens
            took = seconds_to(lambda t=whole: PARAMETER_BLOCKS.written_calls(t))
            assert took < bound, f"{tokens} times took {took:.2f} s"

    def test_what_the_shapes_are_read_as(self) -> None:
        """The shapes are replies, not noise: each is read as what it says."""

        def calls(shape: str, table: dict[str, Any] = SHAPES) -> list[tuple[str, Any]]:
            reply = PARAMETER_BLOCKS.read(table[shape](48), tools=TIME_TOOLS, in_reasoning=True)
            return [(c.name, json.loads(c.arguments)) for c in reply.calls]

        assert calls("white space after <parameter=") == [("bash", {})]
        assert calls("white space after <parameter=k>") == [("bash", {})]
        assert calls("white space inside every tag of many arguments") == [("bash", {"k": "v"})]
        assert calls("<parameter=k> many times") == [("bash", {"k": ""})]
        assert calls("<parameter=k> and white space, many times") == [("bash", {"k": SPACES})]
        assert calls("<parameter= many times, then one closing bracket") == [("bash", {})]
        assert calls("whole calls") == [("bash", {"command": "ls"})] * 16
        assert calls("a text") == [("bash", {"command": SPACES * 48})]
        assert calls("a list") == [("bash", {"items": [1] * 193})]
        # An integer of more digits than the interpreter reads stays text.
        assert calls("an integer", {"an integer": lambda n: SHAPES["an integer"](400)}) == [
            ("bash", {"timeout": "7" * 6400})
        ]
        # A call marker inside a call's body is text of the argument it is in.
        assert calls("call markers, each with a function that is never closed") == [
            ("bash", {"command": "ls" + TEXT["call_open"] + NL + "<function=bash>"})
        ]
        assert calls("white space, then closing brackets, in a call's body", MANY_PIECES) == [
            ("bash", {})
        ]


class TestFormat:
    def test_where_a_reply_begins(self) -> None:
        opens = PARAMETER_BLOCKS.opens_in_reasoning
        assert opens(["reasoning_open"])
        assert not opens(["reasoning_open", "reasoning_close"])
        assert not opens([])
        assert opens(["call_close", "reasoning_close", "reasoning_open", "call_open"])

    def test_a_format_is_asked_for_by_name(self) -> None:
        assert reply_format("parameter_blocks") is PARAMETER_BLOCKS
        with pytest.raises(ValueError, match="parameter_blocks"):
            reply_format("other")

    def test_a_tag_is_read_with_white_space_between_its_parts(self) -> None:
        spaced = "< parameter = command >" + NL + "ls" + NL + "<" + NL + "/ parameter" + NL + ">"
        reply = read("r", THINK_END, CALL, NL + "<function=bash>" + NL + spaced, CALL_END)
        # The key begins after the white space and keeps what follows it.
        assert reply.calls == (ParsedCall("bash", '{"command ": "ls"}'),)

    def test_a_marker_with_white_space_at_the_start_of_a_part_is_refused(self) -> None:
        """The white space allowed between the parts of a tag is taken whole,
        so a part that began with white space could never match after it."""
        with pytest.raises(ValueError, match="white space at the start"):
            ParameterBlocks(parameter_open="< parameter=")
        with pytest.raises(ValueError, match="white space at the start"):
            ParameterBlocks(parameter_close="</ parameter>")
        # White space inside a part, or at its end, is part of the text.
        spaced = ParameterBlocks(parameter_open="<the parameter =")
        body = "<function=bash>" + NL + "<the parameter =command>" + NL + "ls" + NL + "</parameter>"
        reply = spaced.read([THINK_END, CALL, NL + body], tools=TOOLS, in_reasoning=True)
        assert reply.calls == (ParsedCall("bash", '{"command": "ls"}'),)
