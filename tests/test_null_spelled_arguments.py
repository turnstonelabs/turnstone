"""A null the model wrote as text reaches the tool as "no value".

A server that parses tool calls out of generated text hands an argument it does
not recognise on as a string, so a model that writes ``None`` for an optional
number delivers the string ``"None"``.  The parameter's schema decides what the
tool gets: the text where it may be a string, a null where null is allowed, no
argument where the parameter is optional, and the text again where it is
required, so that validation answers the model.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from tests._scripted_chat_server import ScriptedChatServer
from turnstone.core.session import ChatSession
from turnstone.core.tools import (
    NULL_SPELLING_NOT_GIVEN,
    NULL_SPELLING_TO_NULL,
    TOOLS,
    apply_null_spellings,
    null_spelled_arguments,
)
from turnstone.eval.core import HeadlessSession

NL = chr(10)


def _tool(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    parameters: dict[str, Any] = {"type": "object", "properties": properties}
    if required is not None:
        parameters["required"] = required
    return {"type": "function", "function": {"name": "t", "parameters": parameters}}


ALLOWS_NULL = [
    {"type": ["integer", "null"]},
    {"anyOf": [{"type": "integer"}, {"type": "null"}]},
    {"oneOf": [{"type": "array"}, {"type": "null"}]},
    {"type": "integer", "nullable": True},
    {"enum": [1, 2, None]},
]
NO_NULL_NO_STRING = [
    {"type": "integer"},
    {"type": "number"},
    {"type": "boolean"},
    {"type": "array", "items": {"type": "string"}},
    {"type": "object"},
    {"type": "integer", "enum": [1, 2, 3]},
]
MAY_BE_STRING = [
    {"type": "string"},
    {"type": ["string", "null"]},
    {"anyOf": [{"type": "string"}, {"type": "integer"}]},
    {"enum": ["None", "all"]},
    {},
    "not a schema",
]


class TestTheFourCases:
    @pytest.mark.parametrize("schema", MAY_BE_STRING)
    @pytest.mark.parametrize("required", [[], ["p"]])
    def test_a_parameter_that_may_be_a_string_keeps_the_text(self, schema, required):
        args = {"p": "None"}
        assert apply_null_spellings(args, _tool({"p": schema}, required)) == {}
        assert args == {"p": "None"}

    @pytest.mark.parametrize("schema", ALLOWS_NULL)
    @pytest.mark.parametrize("required", [[], ["p"]])
    @pytest.mark.parametrize("text", ["None", "null"])
    def test_where_the_schema_allows_null_the_argument_becomes_null(self, schema, required, text):
        args = {"p": text, "q": 1}
        found = apply_null_spellings(args, _tool({"p": schema}, required))
        assert found == {"p": NULL_SPELLING_TO_NULL} and args == {"p": None, "q": 1}

    @pytest.mark.parametrize("schema", NO_NULL_NO_STRING)
    @pytest.mark.parametrize("text", ["None", "null"])
    def test_an_optional_parameter_without_null_counts_as_not_given(self, schema, text):
        for tool in (_tool({"p": schema}), _tool({"p": schema}, ["other"])):
            args = {"p": text, "q": 1}
            assert apply_null_spellings(args, tool) == {"p": NULL_SPELLING_NOT_GIVEN}
            assert args == {"q": 1}

    @pytest.mark.parametrize("schema", NO_NULL_NO_STRING)
    def test_a_required_parameter_without_null_is_left_for_validation(self, schema):
        args = {"p": "None"}
        assert apply_null_spellings(args, _tool({"p": schema}, ["p"])) == {}
        assert args == {"p": "None"}

    def test_only_the_exact_spellings_at_the_top_level(self):
        tool = _tool({"p": {"type": "integer"}, "q": {"type": "object"}})
        for value in ("none", "NULL", " None", "nil", "", "0"):
            assert null_spelled_arguments({"p": value}, tool) == {}
        assert null_spelled_arguments({"q": {"inner": "None"}}, tool) == {}
        assert null_spelled_arguments({"p": None}, tool) == {}

    def test_an_undescribed_parameter_or_an_unknown_tool_is_left_alone(self):
        assert null_spelled_arguments({"other": "None"}, _tool({"p": {"type": "integer"}})) == {}
        assert null_spelled_arguments({"p": "None"}, None) == {}
        assert null_spelled_arguments({"p": "None"}, {"function": {"name": "t"}}) == {}


def _nested(levels: int, leaf: dict[str, Any], key: str = "anyOf") -> dict[str, Any]:
    """*leaf* under *levels* levels of one-branch ``anyOf`` (or ``oneOf``)."""
    schema = leaf
    for _ in range(levels):
        schema = {key: [schema]}
    return schema


class TestASchemaThatIsNotWellFormed:
    """A tool's schema is whatever its server sent, and it is read for every
    call of the tool.  A part of it that is not what a schema holds there says
    nothing; it is never an error, which would fail every call."""

    @pytest.mark.parametrize(
        "required",
        [
            [["p"]],
            [{"name": "p"}],
            [None, 3, 2.5, True],
            [["p"], {"p": 1}, None, 0],
        ],
    )
    def test_what_names_no_parameter_in_the_required_list_is_passed_over(self, required):
        tool = _tool({"p": {"type": "integer"}}, required)
        # A call without the spelling is read like any other.
        assert null_spelled_arguments({"p": 1}, tool) == {}
        # Nothing in the list names ``p``, so it is optional.
        args = {"p": "None", "q": 1}
        assert apply_null_spellings(args, tool) == {"p": NULL_SPELLING_NOT_GIVEN}
        assert args == {"q": 1}

    def test_a_name_beside_such_items_still_counts(self):
        tool = _tool({"p": {"type": "integer"}, "q": {"type": "integer"}}, [["q"], "p", {"q": 1}])
        assert null_spelled_arguments({"p": "None", "q": "None"}, tool) == {
            "q": NULL_SPELLING_NOT_GIVEN
        }

    @pytest.mark.parametrize("required", ["p", {"p": True}, 7, True])
    def test_a_required_that_is_no_list_names_nothing(self, required):
        tool = _tool({"p": {"type": "integer"}})
        tool["function"]["parameters"]["required"] = required
        assert null_spelled_arguments({"p": "None"}, tool) == {"p": NULL_SPELLING_NOT_GIVEN}

    @pytest.mark.parametrize("key", ["anyOf", "oneOf"])
    def test_branches_are_followed_as_deep_as_a_real_schema_goes(self, key):
        allows_null = {"type": ["integer", "null"]}
        tool = _tool({"p": _nested(32, allows_null, key)})
        assert null_spelled_arguments({"p": "None"}, tool) == {"p": NULL_SPELLING_TO_NULL}

    @pytest.mark.parametrize("key", ["anyOf", "oneOf"])
    @pytest.mark.parametrize("levels", [33, 5000])
    def test_deeper_than_that_is_unclear_and_the_text_stays(self, key, levels):
        """5000 levels is far past what the interpreter recurses into."""
        tool = _tool({"p": _nested(levels, {"type": ["integer", "null"]}, key)})
        args = {"p": "None"}
        assert apply_null_spellings(args, tool) == {}
        assert args == {"p": "None"}

    def test_a_schema_that_holds_itself(self):
        loop: dict[str, Any] = {"anyOf": []}
        loop["anyOf"].append(loop)
        assert null_spelled_arguments({"p": "null"}, _tool({"p": loop})) == {}

    @pytest.mark.parametrize(
        "tool",
        [
            "mcp__s__t",
            ["function"],
            7,
            {"function": "t"},
            {"function": ["parameters"]},
            {"function": {"parameters": "object"}},
            {"function": {"parameters": {"properties": ["p"]}}},
            {"function": {"parameters": {"properties": "p", "required": [["p"]]}}},
        ],
    )
    def test_a_definition_that_is_none_describes_nothing(self, tool):
        assert null_spelled_arguments({"p": "None"}, tool) == {}


def _call(call_id: str, name: str, **arguments: Any) -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


BUILT_IN = {t["function"]["name"]: t for t in TOOLS}


class TestBuiltInTools:
    @pytest.fixture
    def session(self, tmp_db):
        made = HeadlessSession(client=MagicMock(), model="eval-model")
        yield made
        made.close()

    def test_the_four_cases_occur_among_them(self):
        read = BUILT_IN["read_file"]
        # Optional numbers: "no value" is leaving them out.
        assert null_spelled_arguments({"offset": "None", "limit": "null"}, read) == {
            "offset": NULL_SPELLING_NOT_GIVEN,
            "limit": NULL_SPELLING_NOT_GIVEN,
        }
        # A file may hold the word; a path may be named it.
        assert not null_spelled_arguments(
            {"path": "None", "content": "None"}, BUILT_IN["write_file"]
        )

    def test_no_required_parameter_of_a_built_in_tool_is_dropped(self):
        for definition in BUILT_IN.values():
            parameters = definition["function"]["parameters"]
            for name in parameters.get("required", []):
                found = null_spelled_arguments({name: "None"}, definition)
                assert found.get(name) != NULL_SPELLING_NOT_GIVEN, (definition, name)

    def test_read_file_reads_the_whole_file_instead_of_refusing(self, session, tmp_path):
        target = tmp_path / "notes.txt"
        target.write_text("one" + NL + "two" + NL)
        results, _ = session._execute_tools(
            [_call("r", "read_file", path=str(target), offset="None", limit="null")]
        )
        assert results[0][1] == "   1" + chr(9) + "one" + NL + "   2" + chr(9) + "two"

    def test_the_prepared_call_has_no_value_for_it(self, session, tmp_path):
        item = session._prepare_tool_item(
            _call("r", "read_file", path=str(tmp_path / "x"), offset="None", limit=3)
        )
        assert item.get("offset") is None and item["limit"] == 3

    def test_a_string_parameter_keeps_the_word(self, session, tmp_path):
        target = tmp_path / "word.txt"
        session._execute_tools([_call("w", "write_file", path=str(target), content="None")])
        assert target.read_text() == "None"

    def test_a_session_without_a_tool_list_knows_only_the_built_in_tools(self):
        """Sessions are built bare elsewhere (``ChatSession.__new__``); looking a
        tool up must not need what ``__init__`` sets."""
        bare = ChatSession.__new__(ChatSession)
        assert bare._tool_definition("no_such_tool") is None
        assert bare._tool_definition("read_file") is not None

    def test_the_tool_log_shows_the_arguments_as_the_tool_got_them(self, tmp_db, tmp_path):
        from openai import OpenAI

        target = tmp_path / "notes.txt"
        target.write_text("one" + NL)
        arguments = {"path": str(target), "offset": "None", "limit": "null"}
        script = [
            {"tool_calls": [{"name": "read_file", "arguments": arguments}]},
            {"content": "done"},
        ]
        with ScriptedChatServer(script) as server:
            client = OpenAI(base_url=server.base_url, api_key="none", max_retries=0)
            session = HeadlessSession(client=client, model="scripted")
            try:
                log = session.send_headless("read it", max_turns=3)
            finally:
                session.close()
                client.close()
        assert [(e["tool"], e["ok"]) for e in log] == [("read_file", True)]
        assert log[0]["args"] == {"path": str(target)}

    @pytest.mark.parametrize(
        ("tool", "arguments"),
        [("read_file", "[1, 2]"), ("read_file", '"notes.txt"'), ("write_file", "5")],
    )
    def test_arguments_that_are_no_object_do_not_stop_the_run(self, tmp_db, tool, arguments):
        """A server may hand on arguments that are valid JSON and no object.  The
        call fails as it always did, the run goes on, and the log shows what came."""
        from openai import OpenAI

        script = [{"tool_calls": [{"name": tool, "arguments": arguments}]}, {"content": "done"}]
        with ScriptedChatServer(script) as server:
            client = OpenAI(base_url=server.base_url, api_key="none", max_retries=0)
            session = HeadlessSession(client=client, model="scripted")
            try:
                log = session.send_headless("go", max_turns=3)
            finally:
                session.close()
                client.close()
            requests = len(server.requests)
        assert requests == 2  # the model was asked again after the failed call
        assert [(e["tool"], e["ok"]) for e in log] == [(tool, False)]
        assert log[0]["args"] == json.loads(arguments)


class TestMcpTools:
    """The same four cases for a tool of a connected server, whose schema the
    session holds in its merged list."""

    TOOL = {
        "type": "function",
        "function": {
            "name": "mcp__ledger__post",
            "parameters": {
                "type": "object",
                "properties": {
                    "memo": {"type": "string"},
                    "amount": {"type": ["number", "null"]},
                    "page": {"type": "integer"},
                    "account": {"type": "integer"},
                },
                "required": ["account"],
            },
        },
    }

    @pytest.fixture
    def session(self, tmp_db):
        made = HeadlessSession(client=MagicMock(), model="eval-model")
        made._tools = [*made._tools, self.TOOL]
        yield made
        made.close()

    def test_the_definition_is_read_from_the_sessions_list(self, session):
        assert session._tool_definition("mcp__ledger__post") == self.TOOL
        assert session._tool_definition("no_such_tool") is None

    def test_what_the_server_is_sent(self, session):
        session._mcp_client = MagicMock()
        session._mcp_client.is_mcp_tool.return_value = True
        item = session._prepare_tool_item(
            _call("p", "mcp__ledger__post", memo="None", amount="None", page="null", account="None")
        )
        assert item["mcp_args"] == {
            "memo": "None",  # a string: the text may be the value
            "amount": None,  # allows null
            # "page" is optional, not a string, without null: not given
            "account": "None",  # required: left for the server's validation
        }


class TestAServersMalformedSchema:
    """A server's schema whose ``required`` list holds what no list of names
    holds.  Every call of the tool reads the schema, twice in a headless run:
    when the call is prepared and when it is logged."""

    TOOL = {
        "type": "function",
        "function": {
            "name": "mcp__ledger__post",
            "parameters": {
                "type": "object",
                "properties": {"page": {"type": "integer"}, "account": {"type": "integer"}},
                "required": [["account"], {"name": "account"}],
            },
        },
    }

    def test_the_call_is_prepared_as_the_readable_part_of_the_schema_says(self, tmp_db):
        session = HeadlessSession(client=MagicMock(), model="eval-model")
        try:
            session._tools = [*session._tools, self.TOOL]
            session._mcp_client = MagicMock()
            session._mcp_client.is_mcp_tool.return_value = True
            item = session._prepare_tool_item(
                _call("p", "mcp__ledger__post", page="None", account=7)
            )
        finally:
            session.close()
        assert item.get("error") is None
        assert item["mcp_args"] == {"account": 7}

    def test_a_headless_run_goes_on_and_logs_the_call(self, tmp_db):
        """The log is written outside the handling of one failed call: an
        error there ended the whole run."""
        from openai import OpenAI

        arguments = {"page": "None", "account": 7}
        script = [
            {"tool_calls": [{"name": "mcp__ledger__post", "arguments": arguments}]},
            {"content": "done"},
        ]
        with ScriptedChatServer(script) as server:
            client = OpenAI(base_url=server.base_url, api_key="none", max_retries=0)
            session = HeadlessSession(client=client, model="scripted")
            session._tools = [*session._tools, self.TOOL]
            try:
                log = session.send_headless("post it", max_turns=3)
            finally:
                session.close()
                client.close()
            requests = len(server.requests)
        assert requests == 2  # the model was asked again after the call
        # No server is connected here, so the call itself fails as any call
        # of a tool that is gone does; the log shows what the tool would get.
        assert [(e["tool"], e["ok"]) for e in log] == [("mcp__ledger__post", False)]
        assert log[0]["args"] == {"account": 7}
