"""``HeadlessSession(stable_prefix=True)``: every request extends the previous one.

Each test drives the real headless loop, the real ``model_turn`` and the real
chat-completions provider against a scripted server, and asserts on what the
server received.  The server renders prompts with a toy tokenizer, so "the
prompt extends" is checked on token IDs as a recording caller would see them,
not only on the message list.

Tool execution is stubbed where a test is not about tools, so nothing runs on
the machine; the refusal tests use the real dispatch.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from openai import OpenAI

from tests._scripted_chat_server import ScriptedChatServer, assistant_tokens, render_prompt
from turnstone.core.trajectory import Role, Turn
from turnstone.eval.core import (
    _MAX_TOOL_CALLS_PER_TURN,
    HeadlessSession,
    StablePrefixError,
    raw_argument_json,
    tool_call_limit_result,
)

SYSTEM = "You are a test."


def _bash(command: str) -> dict[str, Any]:
    return {"name": "bash", "arguments": {"command": command}}


SCRIPT: list[dict[str, Any]] = [
    {"reasoning": "First look.", "tool_calls": [_bash("ls")]},
    {"reasoning": "Then count.", "content": "Counting.", "tool_calls": [_bash("wc -l a")]},
    {"reasoning": "Enough.", "content": "Done."},
]


class _Run:
    """One session against one scripted server, torn down by ``close``."""

    def __init__(
        self,
        script: list[dict[str, Any]],
        *,
        stable: bool,
        stub_tools: bool = True,
        agent_script: list[dict[str, Any]] | None = None,
        **session_kwargs: Any,
    ) -> None:
        self.server = ScriptedChatServer(script, agent_script=agent_script).start()
        self.client = OpenAI(base_url=self.server.base_url, api_key="none", max_retries=0)
        self.session = HeadlessSession(
            client=self.client,
            model="scripted",
            system_prompt_override=SYSTEM,
            stable_prefix=stable,
            **session_kwargs,
        )
        self.executed: list[list[str]] = []
        self.executed_arguments: list[Any] = []
        self.before_tools: list[Any] = []
        if stub_tools:
            self.session._execute_tools = self._execute  # type: ignore[method-assign]

    def _execute(self, tool_calls: list[dict[str, Any]], **_kw: Any) -> Any:
        for hook in self.before_tools:
            hook(len(self.executed))
        self.executed.append([tc["function"]["name"] for tc in tool_calls])
        for tc in tool_calls:
            try:
                self.executed_arguments.append(json.loads(tc["function"]["arguments"]))
            except ValueError:
                self.executed_arguments.append(tc["function"]["arguments"])
        return [(tc["id"], f"output of {tc['function']['name']}") for tc in tool_calls], None

    @property
    def requests(self) -> list[dict[str, Any]]:
        return list(self.server.requests)

    def prompts(self) -> list[list[int]]:
        return [render_prompt(r["messages"], r.get("tools")) for r in self.server.parent_requests]

    def close(self) -> None:
        self.session.close()
        self.client.close()
        self.server.stop()


@pytest.fixture
def run(tmp_db):
    made: list[_Run] = []

    def _make(script: list[dict[str, Any]], **kwargs: Any) -> _Run:
        made.append(_Run(script, **kwargs))
        return made[-1]

    yield _make
    for item in made:
        item.close()


def _generated(step: dict[str, Any]) -> list[int]:
    calls = [(c["name"], c["arguments"], c.get("raw")) for c in step.get("tool_calls") or []]
    return assistant_tokens(step.get("reasoning") or "", step.get("content") or "", calls)


def _extends(prompts: list[list[int]], script: list[dict[str, Any]]) -> list[bool]:
    """Per later request: does its prompt start with the earlier prompt plus its reply?"""
    out = []
    for i in range(1, len(prompts)):
        expected = prompts[i - 1] + _generated(script[i - 1])
        out.append(prompts[i][: len(expected)] == expected)
    return out


class TestReasoningReplay:
    def test_the_default_drops_earlier_reasoning(self, run):
        """With the option off and no model definition, a replayed assistant turn has
        no reasoning, so the later prompt is not the earlier one plus its reply."""
        r = run(SCRIPT, stable=False)
        r.session.send_headless("go", max_turns=5)

        assistant = [m for m in r.requests[1]["messages"] if m["role"] == "assistant"]
        assert "reasoning" not in assistant[0]
        assert _extends(r.prompts(), SCRIPT) == [False, False]
        assert r.session.prefix_events == []

    def test_stable_prefix_replays_it_without_a_model_definition(self, run):
        r = run(SCRIPT, stable=True)
        r.session.send_headless("go", max_turns=5)

        assert len(r.requests) == 3
        first, second = (m for m in r.requests[2]["messages"] if m["role"] == "assistant")
        assert first["reasoning"] == "First look."
        assert second["reasoning"] == "Then count."
        # On tokens: every prompt is the previous prompt plus the previous reply, then more.
        assert _extends(r.prompts(), SCRIPT) == [True, True]

    def test_every_request_begins_with_the_previous_one(self, run):
        r = run(SCRIPT, stable=True)
        r.session.send_headless("go", max_turns=5)

        for earlier, later in zip(r.requests, r.requests[1:], strict=False):
            sent = earlier["messages"]
            assert later["messages"][: len(sent)] == sent
            assert later["messages"][len(sent)]["role"] == "assistant"
            assert later["tools"] == earlier["tools"]

    def test_a_second_send_extends_the_first_run(self, run):
        script = [*SCRIPT, {"reasoning": "Again.", "content": "Still done."}]
        r = run(script, stable=True)
        r.session.send_headless("go", max_turns=5)
        r.session.send_headless("and again", max_turns=5)

        assert len(r.requests) == 4
        assert r.requests[3]["messages"][-1] == {"role": "user", "content": "and again"}
        assert _extends(r.prompts(), script) == [True, True, True]


class TestToolCallLimit:
    CALLS = _MAX_TOOL_CALLS_PER_TURN + 2
    SCRIPT: list[dict[str, Any]] = [
        {"reasoning": "Many.", "tool_calls": [_bash(f"echo {i}") for i in range(12)]},
        {"reasoning": "Fine.", "content": "Done."},
    ]

    def test_the_default_drops_the_calls_over_the_limit_and_the_reasoning(self, run):
        r = run(self.SCRIPT, stable=False)
        log = r.session.send_headless("go", max_turns=3)

        assert len(log) == _MAX_TOOL_CALLS_PER_TURN
        stored = next(t for t in r.session.messages if t.role is Role.ASSISTANT)
        assert len(stored.tool_calls) == _MAX_TOOL_CALLS_PER_TURN
        assert stored.native is None

    def test_stable_prefix_keeps_every_call_and_answers_the_ones_it_did_not_run(self, run):
        r = run(self.SCRIPT, stable=True)
        log = r.session.send_headless("go", max_turns=3)

        # Ten ran; the stored turn still holds all twelve, with its reasoning.
        assert r.executed == [["bash"] * _MAX_TOOL_CALLS_PER_TURN]
        stored = next(t for t in r.session.messages if t.role is Role.ASSISTANT)
        assert len(stored.tool_calls) == self.CALLS
        assert stored.native is not None

        not_run = tool_call_limit_result(self.CALLS, _MAX_TOOL_CALLS_PER_TURN)
        assert [e["ok"] for e in log] == [True] * _MAX_TOOL_CALLS_PER_TURN + [False, False]
        assert [e["result"] for e in log[-2:]] == [not_run, not_run]
        assert log[-1]["args"] == {"command": "echo 11"}

        # The next request carries all twelve calls, each with one result, in order.
        messages = r.requests[1]["messages"]
        assistant = next(m for m in messages if m["role"] == "assistant")
        assert assistant["reasoning"] == "Many."
        call_ids = [c["id"] for c in assistant["tool_calls"]]
        results = [m for m in messages if m["role"] == "tool"]
        assert [m["tool_call_id"] for m in results] == call_ids and len(call_ids) == self.CALLS
        assert [m["content"] for m in results[-2:]] == [not_run, not_run]
        assert _extends(r.prompts(), self.SCRIPT) == [True]
        assert r.session.prefix_events == [
            {
                "event": "tool_calls_over_limit",
                "turn": 0,
                "calls": self.CALLS,
                "limit": _MAX_TOOL_CALLS_PER_TURN,
            }
        ]


class TestMalformedArguments:
    SCRIPT: list[dict[str, Any]] = [
        {"reasoning": "Oops.", "tool_calls": [{"name": "bash", "arguments": '{"command": "ls'}]},
        {"reasoning": "Retry.", "tool_calls": [_bash("ls")]},
        {"content": "Done."},
    ]

    def test_the_legalized_call_is_replayed_the_same_way_every_time(self, run):
        r = run(self.SCRIPT, stable=True)
        r.session.send_headless("go", max_turns=5)

        assert len(r.requests) == 3
        replayed = [
            next(m for m in request["messages"] if m["role"] == "assistant")
            for request in r.requests[1:]
        ]
        # What went out is valid JSON, not what the model wrote, and it never changes.
        assert replayed[0] == replayed[1]
        assert replayed[0]["tool_calls"][0]["function"]["arguments"] != '{"command": "ls'
        (event,) = r.session.prefix_events
        assert event["event"] == "arguments_legalized" and event["turn"] == 0
        assert event["call_ids"] == [replayed[0]["tool_calls"][0]["id"]]
        # The messages stay stable; the tokens of that one turn cannot, which is what
        # the event tells a recording caller.
        assert _extends(r.prompts(), self.SCRIPT) == [False, True]


class TestFrozenRequestParts:
    def test_a_recomposed_system_prompt_does_not_reach_later_requests(self, run):
        r = run(SCRIPT, stable=True)
        r.before_tools.append(lambda turn: r.session._init_system_messages())
        r.session.send_headless("go", max_turns=5)

        # The session's own copy was recomposed (the override is gone from it)...
        assert r.session.system_messages[0]["content"] != SYSTEM
        # ...and no request saw that.
        assert [req["messages"][0]["content"] for req in r.requests] == [SYSTEM] * 3
        assert _extends(r.prompts(), SCRIPT) == [True, True]

    def test_the_default_sends_the_recomposed_prompt(self, run):
        r = run(SCRIPT, stable=False)
        r.before_tools.append(lambda turn: r.session._init_system_messages())
        r.session.send_headless("go", max_turns=5)

        assert r.requests[0]["messages"][0]["content"] == SYSTEM
        assert r.requests[1]["messages"][0]["content"] != SYSTEM

    def test_the_tool_list_and_the_effort_are_taken_once(self, run):
        r = run(SCRIPT, stable=True, reasoning_effort="low")

        def change(turn: int) -> None:
            r.session._eval_tools = r.session._eval_tools[:1]
            r.session.reasoning_effort = "high"

        r.before_tools.append(change)
        r.session.send_headless("go", max_turns=5)

        assert all(req["tools"] == r.requests[0]["tools"] for req in r.requests)
        assert len(r.requests[0]["tools"]) > 1
        assert [req["reasoning_effort"] for req in r.requests] == ["low"] * 3

    def test_the_default_follows_the_session(self, run):
        r = run(SCRIPT, stable=False, reasoning_effort="low")

        def change(turn: int) -> None:
            r.session._eval_tools = r.session._eval_tools[:1]
            r.session.reasoning_effort = "high"

        r.before_tools.append(change)
        r.session.send_headless("go", max_turns=5)

        assert len(r.requests[1]["tools"]) == 1
        assert r.requests[1]["reasoning_effort"] == "high"


class TestRefusals:
    SCRIPT: list[dict[str, Any]] = [
        {
            "tool_calls": [
                {"name": "skills", "arguments": {"action": "load", "name": "anything"}},
                {"name": "tool_search", "arguments": {"query": "anything"}},
            ]
        },
        {"content": "Done."},
    ]

    def test_tools_that_change_the_prompt_or_the_tool_list_are_refused(self, run):
        r = run(self.SCRIPT, stable=True, stub_tools=False)
        log = r.session.send_headless("go", max_turns=3)

        assert [(e["tool"], e["ok"]) for e in log] == [("skills", False), ("tool_search", False)]
        assert "the system prompt is fixed for the whole run" in log[0]["result"]
        assert "the tool list is fixed for the whole run" in log[1]["result"]
        # The model is told so in the tool results it gets back.
        results = [m["content"] for m in r.requests[1]["messages"] if m["role"] == "tool"]
        assert results == [log[0]["result"], log[1]["result"]]
        assert r.requests[1]["messages"][0]["content"] == SYSTEM

    def test_other_skill_actions_and_the_default_are_not_refused_here(self, run):
        script = [
            {"tool_calls": [{"name": "skills", "arguments": {"action": "find", "query": "x"}}]},
            {"content": "Done."},
        ]
        r = run(script, stable=True, stub_tools=False)
        log = r.session.send_headless("go", max_turns=3)
        assert "fixed for the whole run" not in log[0]["result"]

        d = run(self.SCRIPT, stable=False, stub_tools=False)
        default_log = d.session.send_headless("go", max_turns=3)
        assert all("fixed for the whole run" not in e["result"] for e in default_log)

    def test_compaction_does_nothing(self, run):
        r = run(SCRIPT, stable=True)
        r.session.send_headless("go", max_turns=5)
        before = list(r.session.messages)

        assert r.session._compact_messages(auto=True) is False
        assert r.session._do_auto_compact("mid-turn") is False
        assert r.session.messages == before
        assert r.session.prefix_events == [{"event": "compaction_skipped"}] * 2
        assert len(r.requests) == 3  # no summary request went out


class TestDispatchCheck:
    def test_a_rewritten_earlier_message_stops_the_run_before_the_request(self, run):
        r = run(SCRIPT, stable=True)

        def rewrite(turn: int) -> None:
            if turn == 1:
                r.session.messages[0] = Turn.user("something else")

        r.before_tools.append(rewrite)
        with pytest.raises(StablePrefixError) as raised:
            r.session.send_headless("go", max_turns=5)

        text = str(raised.value)
        assert "message 1" in text and "'user'" in text and "content" in text
        assert len(r.requests) == 2  # the third request was never sent

    def test_a_dropped_earlier_message_stops_the_run(self, run):
        r = run(SCRIPT, stable=True)

        def drop(turn: int) -> None:
            if turn == 1:
                del r.session.messages[:3]

        r.before_tools.append(drop)
        with pytest.raises(StablePrefixError):
            r.session.send_headless("go", max_turns=5)
        assert len(r.requests) == 2

    def test_the_default_sends_the_rewritten_history(self, run):
        r = run(SCRIPT, stable=False)

        def rewrite(turn: int) -> None:
            if turn == 1:
                r.session.messages[0] = Turn.user("something else")

        r.before_tools.append(rewrite)
        r.session.send_headless("go", max_turns=5)

        assert len(r.requests) == 3
        assert r.requests[2]["messages"][1]["content"] == "something else"


NL = chr(10)

# One call with every kind of argument a model spells its own way.  ``arguments`` is
# what the server parsed; ``raw`` is the text the model wrote where that differs from
# the template's spelling.  ``later`` arrived as the text "None" because the server did
# not recognise the model's null.
TYPED_CALL: dict[str, Any] = {
    "name": "bash",
    "arguments": {
        "command": "sleep 1",
        "run_in_background": True,
        "stop_on_error": False,
        "timeout": None,
        "later": "None",
        "blank": "",
        "count": 5,
        "ratio": 0.5,
        "options": {"depth": [1, 2], "deep": {"on": True}},
        "text": "line one" + NL + "  line two" + NL,
    },
    "raw": {
        "run_in_background": "True",
        "stop_on_error": "false",
        "timeout": "null",
        "options": '{"depth":[1,2], "deep": {"on": true}}',
    },
}
TYPED_SCRIPT: list[dict[str, Any]] = [
    {"reasoning": "Typed.", "tool_calls": [TYPED_CALL]},
    {"reasoning": "Again.", "tool_calls": [_bash("ls")]},
    {"content": "Done."},
]


def _texts(call: dict[str, Any]) -> dict[str, str]:
    """The text the scripted model wrote for each argument of *call*."""
    raw = call.get("raw") or {}
    return {
        name: raw.get(name, value if isinstance(value, str) else json.dumps(value))
        for name, value in call["arguments"].items()
    }


class _TextSource:
    """Stands in for a caller that can read the generated text."""

    def __init__(self, script: list[dict[str, Any]]) -> None:
        self.script = script
        self.turn = 0
        self.asked: list[list[str]] = []

    def __call__(self, tool_calls: list[dict[str, Any]]) -> list[dict[str, str] | None]:
        self.asked.append([tc["function"]["name"] for tc in tool_calls])
        step = self.script[self.turn]
        self.turn += 1
        return [_texts(call) for call in step["tool_calls"]]


class TestRawArgumentJson:
    def test_every_value_becomes_the_text_the_model_wrote(self):
        replay = raw_argument_json(json.dumps(TYPED_CALL["arguments"]), _texts(TYPED_CALL))
        assert replay is not None
        assert json.loads(replay) == {
            "command": "sleep 1",
            "run_in_background": "True",
            "stop_on_error": "false",
            "timeout": "null",
            "later": "None",
            "blank": "",
            "count": "5",
            "ratio": "0.5",
            "options": '{"depth":[1,2], "deep": {"on": true}}',
            "text": "line one" + NL + "  line two" + NL,
        }
        assert list(json.loads(replay)) == list(TYPED_CALL["arguments"])  # order kept

    @pytest.mark.parametrize(
        ("value", "text"),
        [
            (True, "True"),
            (True, "true"),
            (False, "False"),
            (None, "None"),
            (None, "null"),
            ("None", "None"),
            ("", ""),
            (5, "5"),
            (5.0, "5.0"),
            # A whole number is the same value however the server typed it.
            (5.0, "5"),
            (5, "5.0"),
            ([1, "a"], '[1, "a"]'),
            ({"a": {"b": None}}, '{"a":{"b":null}}'),
        ],
    )
    def test_a_text_that_means_the_parsed_value(self, value, text):
        assert raw_argument_json(json.dumps({"p": value}), {"p": text}) == json.dumps({"p": text})

    @pytest.mark.parametrize(
        ("value", "text"),
        [
            (True, "1"),
            (True, "yes"),
            (1, "True"),
            (1.0, "True"),
            (5, "6"),
            (None, ""),
            ({"a": 1, "b": 2}, '{"b": 2, "a": 1}'),
            ([1, 2], "not json"),
        ],
    )
    def test_a_text_that_does_not_mean_it_is_refused(self, value, text):
        assert raw_argument_json(json.dumps({"p": value}), {"p": text}) is None

    def test_a_string_is_sent_back_as_parsed_whatever_its_text_says(self):
        """The template writes a string as it is, so the parsed value already is the
        model's spelling.  A text that differs (here a character the recorded bytes
        did not give back whole) costs nothing, and the typed argument beside it
        still goes back as written."""
        broken = "caf" + chr(0xFFFD) + chr(0xFFFD)
        arguments = json.dumps({"name": "caf" + chr(0xE9), "force": True, "word": "None"})
        replay = raw_argument_json(arguments, {"name": broken, "force": "True", "word": "null"})
        assert replay is not None
        assert json.loads(replay) == {"name": "caf" + chr(0xE9), "force": "True", "word": "None"}

    def test_text_no_request_can_carry_is_escaped(self):
        lone = chr(0xD800)
        replay = raw_argument_json(json.dumps({"command": "echo " + lone}), {"command": "?"})
        assert replay is not None
        replay.encode("utf-8")  # does not raise
        assert json.loads(replay) == {"command": "echo " + lone}

    def test_the_arguments_and_their_order_must_match(self):
        arguments = json.dumps({"a": 1, "b": "x"})
        assert raw_argument_json(arguments, None) is None
        assert raw_argument_json(arguments, {"a": "1"}) is None
        assert raw_argument_json(arguments, {"b": "x", "a": "1"}) is None
        assert raw_argument_json(arguments, {"a": "1", "b": "x", "c": "extra"}) is None
        assert raw_argument_json("{not json", {"a": "1"}) is None
        assert raw_argument_json("[1]", {}) is None


class TestRawArgumentReplay:
    def test_typed_arguments_break_the_token_prefix_when_replayed_from_their_values(self, run):
        """What a recording caller saw before: the messages are stable, and the turn
        with a boolean the model wrote as ``True`` still comes back as ``true``."""
        r = run(TYPED_SCRIPT, stable=True)
        r.session.send_headless("go", max_turns=5)

        assert _extends(r.prompts(), TYPED_SCRIPT) == [False, True]
        assert r.session.prefix_events == []

    def test_replayed_as_written_the_prompt_extends(self, run):
        source = _TextSource(TYPED_SCRIPT)
        r = run(TYPED_SCRIPT, stable=True, raw_arguments=source)
        r.session.send_headless("go", max_turns=5)

        assert _extends(r.prompts(), TYPED_SCRIPT) == [True, True]
        assert r.session.prefix_events == []
        assert source.asked == [["bash"], ["bash"]]
        # The wire carries each argument as text, in the model's spelling and order...
        replayed = next(m for m in r.requests[1]["messages"] if m["role"] == "assistant")
        sent = json.loads(replayed["tool_calls"][0]["function"]["arguments"])
        assert sent == _texts(TYPED_CALL) and list(sent) == list(TYPED_CALL["arguments"])
        # ...and the tool ran on the parsed, typed values.
        assert r.executed_arguments[0] == TYPED_CALL["arguments"]
        assert r.executed_arguments[0]["run_in_background"] is True
        assert r.executed_arguments[0]["timeout"] is None
        # Later requests send that turn the same way again.
        again = next(m for m in r.requests[2]["messages"] if m["role"] == "assistant")
        assert again == replayed

    def test_a_call_the_text_does_not_account_for_is_flagged_and_replayed_from_its_values(
        self, run
    ):
        def wrong(tool_calls: list[dict[str, Any]]) -> list[dict[str, str] | None]:
            texts = _texts(TYPED_CALL)
            texts["run_in_background"] = "maybe"
            return [texts]

        r = run(TYPED_SCRIPT[:1] + [{"content": "Done."}], stable=True, raw_arguments=wrong)
        r.session.send_headless("go", max_turns=5)

        (event,) = r.session.prefix_events
        assert event["event"] == "raw_arguments_unrecovered" and event["turn"] == 0
        replayed = next(m for m in r.requests[1]["messages"] if m["role"] == "assistant")
        assert event["call_ids"] == [replayed["tool_calls"][0]["id"]]
        sent = json.loads(replayed["tool_calls"][0]["function"]["arguments"])
        assert sent == TYPED_CALL["arguments"]  # the parsed values, not a guess

    @pytest.mark.parametrize("answer", ["none", "short", "raise"])
    def test_a_source_that_cannot_answer_never_fails_the_run(self, run, answer):
        def source(tool_calls: list[dict[str, Any]]) -> Any:
            if answer == "raise":
                raise RuntimeError("no text")
            return None if answer == "none" else []

        r = run(TYPED_SCRIPT, stable=True, raw_arguments=source)
        r.session.send_headless("go", max_turns=5)

        assert len(r.requests) == 3
        assert [e["event"] for e in r.session.prefix_events] == ["raw_arguments_unrecovered"] * 2

    def test_without_the_option_the_source_is_not_used(self, run):
        source = _TextSource(TYPED_SCRIPT)
        r = run(TYPED_SCRIPT, stable=False, raw_arguments=source)
        r.session.send_headless("go", max_turns=5)

        assert source.asked == []
        replayed = next(m for m in r.requests[1]["messages"] if m["role"] == "assistant")
        sent = json.loads(replayed["tool_calls"][0]["function"]["arguments"])
        assert sent == TYPED_CALL["arguments"]


class TestCutReply:
    SCRIPT: list[dict[str, Any]] = [
        {"reasoning": "Cut he", "tool_calls": [_bash("ls")], "finish_reason": "length"},
        {"content": "never asked"},
    ]

    def test_a_reply_cut_at_the_token_limit_ends_a_stable_run(self, run):
        r = run(self.SCRIPT, stable=True)
        log = r.session.send_headless("go", max_turns=5)

        assert len(r.requests) == 1 and log == [] and r.executed == []
        assert r.session.prefix_events == [{"event": "ended_at_length", "turn": 0}]

    def test_the_default_goes_on(self, run):
        r = run(self.SCRIPT, stable=False)
        r.session.send_headless("go", max_turns=5)
        assert len(r.requests) == 2 and r.executed == [["bash"]]


class TestSubAgent:
    def test_spawning_one_changes_neither_the_system_prompt_nor_the_tool_list(self, run):
        script = [
            {
                "reasoning": "Delegate.",
                "tool_calls": [{"name": "task_agent", "arguments": {"prompt": "Count to three."}}],
            },
            {"reasoning": "Use it.", "content": "Done."},
        ]
        r = run(script, stable=True, stub_tools=False, agent_script=[{"content": "one two three"}])
        log = r.session.send_headless("go", max_turns=3)

        assert [(e["tool"], e["ok"], e["result"]) for e in log] == [
            ("task_agent", True, "one two three")
        ]
        first, second = r.server.parent_requests
        (agent,) = r.server.agent_requests
        assert (
            second["messages"][0] == first["messages"][0] == {"role": "system", "content": SYSTEM}
        )
        assert second["tools"] == first["tools"]
        assert _extends(r.prompts(), script) == [True]
        # The sub-agent had its own conversation: its own system prompt, the task as the
        # user turn, and nothing of the parent's.
        assert agent["messages"][0]["content"] != SYSTEM
        assert agent["messages"][-1] == {"role": "user", "content": "Count to three."}
