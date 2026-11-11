"""Prompts sent as token IDs: the check of the server, the assembly of each
prompt, the reply read on this side, and whole rollouts.  Scripted server, no
container; the sandbox stand-ins are those of ``test_rollout_episode.py`` and
every script holds harmless commands only."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import openai
import pytest

from tests._scripted_chat_server import (
    CALL,
    CALL_END,
    END,
    MERGED,
    OTHER_END,
    OWN_CHOICE,
    START,
    THINK,
    THINK_END,
    ScriptedChatServer,
    assistant_tokens,
    encode,
    generation_prompt,
    no_user_query,
    render_prompt,
)
from tests.test_rollout_episode import SCRIPT, SPAWN, _no_configuration_file, _run  # noqa: F401
from turnstone.core import session as session_module
from turnstone.rollout import cli
from turnstone.rollout.idprompt import (
    PromptAssembly,
    PromptAssemblyError,
    TokenIdRoute,
    check_token_id_route,
    completion_request,
    template_arguments,
)
from turnstone.rollout.probe import sampler_logprobs_reported
from turnstone.rollout.recording import PARENT, SUB_AGENT, RecordingClient
from turnstone.rollout.settings import SetupError
from turnstone.rollout.tokens import rebuild_final, rebuild_prompts

NL = chr(10)
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}, "timeout": {"type": "integer"}},
            },
        },
    }
]
MESSAGES = [{"role": "system", "content": "sys"}, {"role": "user", "content": "go"}]
STEP: dict[str, Any] = {
    "reasoning": "Think first.",
    "content": "Calling a tool.",
    "tool_calls": [{"name": "bash", "arguments": {"command": "echo hi", "timeout": 5}}],
}
ROUTE = TokenIdRoute(
    format="parameter_blocks",
    marker_ids={
        "reasoning_open": THINK,
        "reasoning_close": THINK_END,
        "call_open": CALL,
        "call_close": CALL_END,
    },
    text_markers=(),
    closing=(END, *encode(NL)),
    generation_prompt=tuple(generation_prompt()),
    in_reasoning=True,
)


def make_client(server: ScriptedChatServer) -> openai.OpenAI:
    return openai.OpenAI(base_url=server.base_url, api_key="none", max_retries=0)


def recorder_for(server: ScriptedChatServer, **kwargs: Any) -> RecordingClient:
    return RecordingClient(make_client(server), id_prompts=PromptAssembly(ROUTE), **kwargs)


def ask(recorder: RecordingClient, messages: list[dict[str, Any]], **kwargs: Any) -> list[Any]:
    return list(
        recorder.chat.completions.create(
            model="m",
            messages=messages,
            tools=TOOLS,
            stream=True,
            stream_options={"include_usage": True},
            max_tokens=500,
            temperature=1.0,
            **kwargs,
        )
    )


def turn_of(chunks: list[Any]) -> dict[str, Any]:
    """The assistant message a session would store for a reply, as it is sent back."""
    delta = chunks[0].choices[0].delta
    message: dict[str, Any] = {"role": "assistant", "content": delta.content or ""}
    if getattr(delta, "reasoning", None):
        message["reasoning"] = delta.reasoning
    if delta.tool_calls:
        message["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.function.name, "arguments": call.function.arguments},
            }
            for call in delta.tool_calls
        ]
    return message


def result_of(chunks: list[Any], text: str) -> dict[str, Any]:
    call_id = chunks[0].choices[0].delta.tool_calls[0].id
    return {"role": "tool", "tool_call_id": call_id, "content": text}


class TestTheCheck:
    def test_a_server_that_can(self) -> None:
        with ScriptedChatServer([STEP]) as server:
            verdict = check_token_id_route(make_client(server), "m", extra_body={"top_k": 20})
            chat = [r for r in server.requests if "messages" in r]
        assert verdict["ok"] is True and verdict["reason"] is None
        route = TokenIdRoute.from_verdict(verdict)
        assert route == ROUTE
        assert verdict["checks"]["chat_route_renders_the_same"] is True
        checks = verdict["checks"]
        assert set(checks["markers_kept_in_text"].values()) == {True}
        assert checks["completions_route"]["token_ids"] == [END]
        # Everything was seen, on the tokens the model wrote by itself.
        assert checks["end_of_turn_token_ends_the_reply"] is True
        assert checks["not_observed"] == {}
        assert checks["produced_by"] == dict.fromkeys([*ROUTE.marker_ids, "end_of_turn"], "model")
        # The chat route was asked once, for one token, only to compare renderings.
        assert len(chat) == 1 and chat[0]["max_tokens"] == 1
        assert json.loads(json.dumps(verdict)) == verdict

    @pytest.mark.parametrize(
        ("fault", "reason"),
        [
            ({"tokenize_status": 404}, "tokenize route does not render"),
            ({"tokenize_differs": True}, "differently from the chat route"),
            ({"unstable_rendering": True}, "turn is a prefix of the longer rendering"),
            ({"drop_marker_text": True}, "does not keep the markers"),
            ({"hide_stop_token": True}, "cannot be trained on"),
            ({"runs_past_end": True}, "did not end with a stop"),
            ({"completions_status": 404}, "does not take a prompt of token IDs"),
            ({"echo_other_prompt": True}, "another prompt than the token IDs it was sent"),
        ],
    )
    def test_a_server_that_cannot_is_told_why(self, fault: dict[str, Any], reason: str) -> None:
        with ScriptedChatServer([STEP], **fault) as server:
            verdict = check_token_id_route(make_client(server), "m")
        assert verdict["ok"] is False and reason in verdict["reason"]
        with pytest.raises(ValueError, match="cannot be sent token-ID prompts"):
            TokenIdRoute.from_verdict(verdict)

    def test_a_model_that_would_write_other_tokens_is_no_fault_of_the_server(self) -> None:
        """The check has the server generate each marker and the end-of-turn
        token to see how it reports them.  A model whose own likeliest token
        is another one at every one of those places, as a later checkpoint's
        can be, does not fail it: the server is steered to the token."""
        with ScriptedChatServer([STEP], other_choice=True) as server:
            verdict = check_token_id_route(make_client(server), "m", extra_body={"top_k": 20})
            steered = [r["logit_bias"] for r in server.requests if r.get("logit_bias")]
        assert verdict["ok"] is True and verdict["reason"] is None
        assert TokenIdRoute.from_verdict(verdict) == ROUTE
        checks = verdict["checks"]
        places = [*ROUTE.marker_ids, "end_of_turn"]
        assert checks["produced_by"] == dict.fromkeys(places, "logit_bias")
        assert checks["not_observed"] == {}
        assert set(checks["markers_kept_in_text"].values()) == {True}
        assert checks["end_of_turn_token_ends_the_reply"] is True
        # The reply the route's soundness is read off is the model's own.
        assert checks["completions_route"]["token_ids"] == [OWN_CHOICE]
        assert checks["completions_route"]["problems"] == []
        # One request for each place, each naming the one token it is to be.
        wanted = [*ROUTE.marker_ids.values(), END]
        assert steered == [{str(token): 100} for token in wanted]

    def test_a_server_that_refuses_the_first_way_is_steered_the_second(self) -> None:
        """A server that drafts several tokens in a step may refuse a bias on
        a logit and still take a restriction of the choice to one token."""
        with ScriptedChatServer([STEP], other_choice=True, logit_bias="refused") as server:
            verdict = check_token_id_route(make_client(server), "m")
            biased = [r for r in server.requests if r.get("logit_bias")]
            restricted = [
                r["allowed_token_ids"] for r in server.requests if "allowed_token_ids" in r
            ]
        assert verdict["ok"] is True and TokenIdRoute.from_verdict(verdict) == ROUTE
        checks = verdict["checks"]
        places = [*ROUTE.marker_ids, "end_of_turn"]
        assert checks["produced_by"] == dict.fromkeys(places, "allowed_token_ids")
        assert checks["not_observed"] == {}
        # The way that was refused was tried once, not at every place.
        assert len(biased) == 1
        assert restricted == [[token] for token in [*ROUTE.marker_ids.values(), END]]

    @pytest.mark.parametrize("answer", ["ignored", "refused"])
    def test_what_a_server_cannot_be_brought_to_generate_is_left_unseen(self, answer: str) -> None:
        """A server that takes neither way cannot be steered.  What the model
        did not write by itself was then not seen: the verdict says which and
        why, and the server passes."""
        deaf = {"logit_bias": answer, "allowed_token_ids": answer}
        with ScriptedChatServer([STEP], other_choice=True, **deaf) as server:
            verdict = check_token_id_route(make_client(server), "m")
            steered = [r for r in server.requests if "logit_bias" in r or "allowed_token_ids" in r]
        # Each way was tried once.
        assert len(steered) == 2
        assert verdict["ok"] is True and verdict["reason"] is None
        assert TokenIdRoute.from_verdict(verdict) == ROUTE
        checks = verdict["checks"]
        assert checks["markers_kept_in_text"] == dict.fromkeys(ROUTE.marker_ids)
        assert checks["end_of_turn_token_ends_the_reply"] is None
        assert checks["produced_by"] == {}
        assert sorted(checks["not_observed"]) == sorted([*ROUTE.marker_ids, "end_of_turn"])
        why = checks["not_observed"]["end_of_turn"]
        assert f"the model wrote token [{OWN_CHOICE}] there" in why
        said = "not followed" if answer == "ignored" else "refused"
        assert f"logit_bias {said}" in why and f"allowed_token_ids {said}" in why
        assert json.loads(json.dumps(verdict)) == verdict

    @pytest.mark.parametrize(
        ("fault", "reason"),
        [
            ({"drop_marker_text": True}, "does not keep the markers"),
            ({"hide_stop_token": True}, "cannot be trained on"),
            ({"runs_past_end": True}, "did not end with a stop"),
        ],
    )
    def test_what_a_steered_server_does_wrong_is_still_found(
        self, fault: dict[str, Any], reason: str
    ) -> None:
        with ScriptedChatServer([STEP], other_choice=True, **fault) as server:
            verdict = check_token_id_route(make_client(server), "m")
        assert verdict["ok"] is False and reason in verdict["reason"]

    @pytest.mark.parametrize(
        ("fault", "found"),
        [
            ({"drop_marker_text": True}, "marker tokens of the reply came without their text"),
            ({"runs_past_end": True}, "went on generating after the template's end-of-turn token"),
            ({"hide_stop_token": True}, "the server counted"),
        ],
    )
    def test_what_the_check_could_not_see_is_found_on_the_first_reply(
        self, fault: dict[str, Any], found: str
    ) -> None:
        """A server that does one of these things wrong and cannot be steered
        passes the check with a model that would write other tokens.  The run
        goes ahead, and its first reply shows the fault: the record is the
        machinery's, never the model's outcome."""
        deaf = {"logit_bias": "ignored", "allowed_token_ids": "ignored"}
        with ScriptedChatServer(SCRIPT, other_choice=True, **deaf, **fault) as server:
            verdict = check_token_id_route(make_client(server), "scripted")
            assert verdict["ok"] is True and len(verdict["checks"]["not_observed"]) == 5
            record, _box = _run(server, token_id_prompts=True, token_id_route=verdict)
        assert record["trainable"] is False
        if "hide_stop_token" in fault:
            # The reply could be read; its token data cannot be trained on.
            assert any(found in line for line in record["problems"])
        else:
            assert record["finish_state"] == "error" and record["finish_cause"] == "machinery"
            assert "PromptAssemblyError" in record["error"] and found in record["error"]
            assert record["turns"] == 0 and record["tool_log"] == []

    def test_a_run_on_a_server_that_cannot_does_not_start(self) -> None:
        with ScriptedChatServer(SCRIPT, tokenize_status=404) as server:
            with pytest.raises(SetupError, match="prompts cannot be sent as token IDs"):
                _run(server, token_id_prompts=True)
            # Nothing of the task was asked of the model, on either route.
            assert server.requests == []

    def test_the_template_arguments_a_request_carries(self) -> None:
        request = {"extra_body": {"chat_template_kwargs": {"a": 1}, "top_k": 5}}
        assert template_arguments(request) == {"a": 1}
        request["reasoning_effort"] = "low"
        assert template_arguments(request) == {
            "a": 1,
            "reasoning_effort": "low",
            "enable_thinking": True,
        }
        sent = completion_request({**request, "model": "m", "max_completion_tokens": 9}, [1, 2])
        assert sent["extra_body"] == {
            "top_k": 5,
            "return_token_ids": True,
            "return_tokens_as_token_ids": True,
            "skip_special_tokens": True,
        }
        assert sent["prompt"] == [1, 2] and sent["max_tokens"] == 9 and sent["logprobs"] == 0


class TestATemplateThatRefuses:
    """A real chat template refuses some conversations, and the server answers
    with the template's words wherever it renders: on the chat route for a run
    that sends messages, on the tokenize route for one that sends token IDs."""

    REFUSED = [
        MESSAGES[0],
        {"role": "user", "content": "<tool_response>" + NL + "ok" + NL + "</tool_response>"},
    ]

    def test_a_conversation_without_a_users_words_is_refused_where_it_is_rendered(self) -> None:
        with ScriptedChatServer([STEP], refuses=no_user_query) as server:
            with pytest.raises(openai.BadRequestError, match="No user query found"):
                ask(recorder_for(server), self.REFUSED)
            with pytest.raises(openai.BadRequestError, match="No user query found"):
                ask(RecordingClient(make_client(server)), self.REFUSED)
            # Nothing was generated for either, and each was refused on its route.
            assert server.refused == ["tokenize", "chat"]
            assert server.requests == [] and server.tokenize_requests == []
            # A conversation the template renders goes through as before.
            ask(recorder_for(server), MESSAGES)
            assert len(server.requests) == 1 and server.refused == ["tokenize", "chat"]

    def test_the_status_of_a_refusal_can_be_chosen(self) -> None:
        with (
            ScriptedChatServer([STEP], refuses=no_user_query, refusal_status=422) as server,
            pytest.raises(openai.UnprocessableEntityError, match="No user query found"),
        ):
            ask(RecordingClient(make_client(server)), self.REFUSED)

    def test_what_the_template_is_asked_about(self) -> None:
        asked = "<tool_response>x</tool_response>"
        assert no_user_query([{"role": "user", "content": " " + asked + NL}]) is not None
        assert no_user_query([{"role": "system", "content": "s"}]) is not None
        assert no_user_query(MESSAGES) is None
        assert no_user_query([{"role": "user", "content": asked}, MESSAGES[1]]) is None
        assert no_user_query([{"role": "user", "content": asked + " and more"}]) is None


class TestOneCall:
    def test_the_reply_is_read_here_and_recorded_like_any_other(self) -> None:
        with ScriptedChatServer([STEP]) as server:
            recorder = recorder_for(server, extra_body={"top_k": 20})
            chunks = ask(recorder, MESSAGES)
            sent = server.requests[0]
        delta = chunks[0].choices[0].delta
        assert (delta.reasoning, delta.content) == ("Think first.", "Calling a tool.")
        assert chunks[0].choices[0].finish_reason == "tool_calls"
        (call,) = delta.tool_calls
        # The arguments are typed by the tool's schema, as a server would type them.
        assert (call.function.name, call.function.arguments) == (
            "bash",
            '{"command": "echo hi", "timeout": 5}',
        )
        assert chunks[1].usage.completion_tokens == len(recorder.calls[0].token_ids)
        record = recorder.calls[0]
        assert record.route == "completions" and record.complete and record.ended_by_itself
        assert record.prompt_token_ids == render_prompt(MESSAGES, TOOLS)
        assert record.token_ids == assistant_tokens(
            "Think first.", "Calling a tool.", [("bash", STEP["tool_calls"][0]["arguments"])]
        )
        assert record.ids_checked and len(record.logprobs) == len(record.token_ids)
        assert record.problems() == []
        # What went out: the prompt as token IDs, the sampler settings, no messages.
        assert sent["prompt"] == record.prompt_token_ids and "messages" not in sent
        assert (sent["top_k"], sent["temperature"], sent["max_tokens"]) == (20, 1.0, 500)
        assert record.request["sampling"] == {"temperature": 1.0, "max_tokens": 500}
        assert record.request["extra_body"]["top_k"] == 20

    def test_a_call_that_is_not_streamed(self) -> None:
        with ScriptedChatServer([STEP]) as server:
            recorder = recorder_for(server)
            reply = recorder.chat.completions.create(model="m", messages=MESSAGES, tools=TOOLS)
        message = reply.choices[0].message
        assert (
            message.content == "Calling a tool." and reply.choices[0].finish_reason == "tool_calls"
        )
        assert message.tool_calls[0].function.name == "bash"
        assert recorder.calls[0].complete and recorder.calls[0].problems() == []

    def test_a_reply_cut_at_the_limit_says_so(self) -> None:
        with ScriptedChatServer([STEP], keep_reply_limit=True) as server:
            recorder = recorder_for(server)
            chunks = list(
                recorder.chat.completions.create(
                    model="m", messages=MESSAGES, tools=TOOLS, stream=True, max_tokens=40
                )
            )
        assert chunks[0].choices[0].finish_reason == "length"
        record = recorder.calls[0]
        assert record.complete and record.ended_by_itself is False
        assert len(record.token_ids) == 40

    @pytest.mark.parametrize(
        ("fault", "problem"),
        [
            ("skip_logprobs", "did not arrive one to one"),
            ("wrong_ids", "did not arrive one to one"),
            ("drop_tokens", "the server counted"),
            ("no_usage", "reported no usage"),
        ],
    )
    def test_the_checks_behind_trainable_hold_on_this_route(self, fault: str, problem: str) -> None:
        with ScriptedChatServer([STEP], faults={0: fault}) as server:
            recorder = recorder_for(server)
            ask(recorder, MESSAGES)
        assert any(problem in line for line in recorder.calls[0].problems())

    @pytest.mark.parametrize(
        ("fault", "found"),
        [
            ({"drop_marker_text": True}, "3 marker tokens of the reply came without their text"),
            ({"runs_past_end": True}, "went on generating after the template's end-of-turn token"),
        ],
    )
    def test_a_reply_the_server_did_not_deliver_as_the_reading_needs_it(
        self, fault: dict[str, Any], found: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nothing a model writes makes a marker token come without its text,
        or a reply go on after the end-of-turn token: such a reply is not read
        as if it were the model's, whoever's call it is."""
        with ScriptedChatServer([STEP], **fault) as server:
            recorder = recorder_for(server)
            with pytest.raises(PromptAssemblyError, match=found):
                ask(recorder, MESSAGES)
            monkeypatch.setattr("turnstone.rollout.recording.inside_task_agent", lambda: True)
            with pytest.raises(PromptAssemblyError, match=found):
                ask(recorder, MESSAGES)
        assert [c.scope for c in recorder.calls] == [PARENT, SUB_AGENT]
        assert all(found in c.error and not c.complete for c in recorder.calls)
        assert recorder.id_prompts.events == []

    def test_a_prompt_the_server_reports_differently(self) -> None:
        with ScriptedChatServer([STEP], echo_other_prompt=True) as server:
            recorder = recorder_for(server)
            ask(recorder, MESSAGES)
        assert any("another prompt" in line for line in recorder.calls[0].problems())

    def test_the_gate_cuts_the_reply_limit_on_this_route_too(self) -> None:
        class Gate:
            def admit(self, call: Any, request: dict[str, Any]) -> None:
                request["max_tokens"] = 7

        with ScriptedChatServer([STEP], keep_reply_limit=True) as server:
            recorder = recorder_for(server, gate=Gate())
            chunks = ask(recorder, MESSAGES)
            assert server.requests[0]["max_tokens"] == 7
        assert chunks[0].choices[0].finish_reason == "length"

    def test_a_sub_agents_call_gets_the_servers_rendering_of_its_messages(
        self, monkeypatch
    ) -> None:
        steps = [STEP, {"content": "done"}]
        with ScriptedChatServer(steps) as server:
            recorder = recorder_for(server)
            first = ask(recorder, MESSAGES)
            longer = [*MESSAGES, turn_of(first), result_of(first, "hi")]
            monkeypatch.setattr(session_module, "inside_task_agent", lambda: True)
            monkeypatch.setattr("turnstone.rollout.recording.inside_task_agent", lambda: True)
            ask(recorder, longer)
        assert [c.scope for c in recorder.calls] == [PARENT, SUB_AGENT]
        assert recorder.calls[1].prompt_token_ids == render_prompt(longer, TOOLS)


class TestAMarkerBesideItsOwnText:
    """A server may hand on several tokens a part.  A part can then hold a
    marker's token and, beside it, the same characters spelled with ordinary
    tokens.  The same tokens are read the same however they were cut into
    parts, wherever the order of a part's tokens says which is the marker."""

    # The reasoning ends, and the answer spells the closing tag out.
    AFTER = {"reasoning": "Think first.", "content": "</think> closes the reasoning."}
    # The reasoning spells the tag out, then ends.
    BEFORE = {"reasoning": "It ends with </think>", "content": "Done."}

    def read(self, step: dict[str, Any], size: int) -> tuple[Any, RecordingClient]:
        with ScriptedChatServer([step], part_tokens=size) as server:
            recorder = recorder_for(server)
            chunks = ask(recorder, MESSAGES)
        return chunks[0].choices[0].delta, recorder

    @pytest.mark.parametrize("size", [1, 2, 3, 12, 13])
    def test_the_marker_token_first_or_alone_in_its_part(self, size: int) -> None:
        """The marker's token is the thirteenth: in parts of 12 it is the first
        of a part that also holds the spelled-out tag, in parts of 13 the last
        of its part."""
        delta, recorder = self.read(self.AFTER, size)
        assert delta.reasoning == "Think first."
        assert delta.content == NL + NL + "</think> closes the reasoning."
        assert recorder.id_prompts.events == []

    @pytest.mark.parametrize("size", [1, 11, 22])
    def test_the_marker_token_last_in_its_part(self, size: int) -> None:
        """The marker's token is the twenty-second: in parts of 22 it ends a
        part that holds the spelled-out tag before it."""
        delta, recorder = self.read(self.BEFORE, size)
        assert delta.reasoning == "It ends with </think>"
        assert delta.content == NL + NL + "Done."
        assert recorder.id_prompts.events == []

    def test_between_other_tokens_the_first_is_taken_and_the_record_is_told(self) -> None:
        """The whole reply in one part: other tokens before the marker's and
        after it.  The part does not say which occurrence the token is, the
        first is taken, and an event says that the reading rests on that."""
        noted = [{"event": "marker_place_uncertain", "call": 0, "tokens": 1}]
        delta, recorder = self.read(self.AFTER, 1000)
        assert (delta.reasoning, delta.content) == (
            "Think first.",
            NL + NL + "</think> closes the reasoning.",
        )
        assert recorder.id_prompts.events == noted
        # The spelled-out tag first: it is taken for the marker, and the
        # marker's own text is left in the answer.
        delta, recorder = self.read(self.BEFORE, 1000)
        assert (delta.reasoning, delta.content) == ("It ends with ", "</think>" + NL + NL + "Done.")
        assert recorder.id_prompts.events == noted

    def test_what_is_noted_of_a_sub_agents_reply_is_kept_apart(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nothing of a sub-agent is among the session's events."""
        monkeypatch.setattr("turnstone.rollout.recording.inside_task_agent", lambda: True)
        _delta, recorder = self.read(self.AFTER, 1000)
        (call,) = recorder.calls
        assert call.scope == SUB_AGENT
        assert recorder.id_prompts.events == []
        assert recorder.id_prompts.subagent_events == [
            {"event": "marker_place_uncertain", "call": call.index, "tokens": 1}
        ]

    @pytest.mark.parametrize("size", [1, 4, 1000])
    def test_a_rollout_reads_the_same_whatever_the_size_of_the_parts(self, size: int) -> None:
        with ScriptedChatServer(SCRIPT) as server:
            usual, _box = _run(server, token_id_prompts=True)
        with ScriptedChatServer(SCRIPT, part_tokens=size) as server:
            record, _box = _run(server, token_id_prompts=True)
        assert record["finish_state"] == "completed" and record["one_sequence"] is True
        assert record["prefix_events"] == []
        assert record["tool_log"] == usual["tool_log"]
        assert rebuild_final(record["tokens"]) == rebuild_final(usual["tokens"])
        assert [m.get("content") for m in record["messages"]] == [
            m.get("content") for m in usual["messages"]
        ]


# The one prompt the stand-in below renders.  It ends with the generation
# prompt the route was checked with, as a first prompt must.
REPLAYED_PROMPT = [START, *ROUTE.generation_prompt]


class _Replaying:
    """An SDK client stand-in: its tokenize route gives one prompt, and its
    completions route the parts of a reply that were made beforehand."""

    base_url = "http://stand-in.invalid/v1"

    def __init__(self, parts: list[Any]) -> None:
        self.completions = SimpleNamespace(create=lambda **request: iter(parts))
        self.chat = SimpleNamespace(completions=SimpleNamespace())

    def post(self, path: str, **request: Any) -> dict[str, Any]:
        return {"tokens": list(REPLAYED_PROMPT)}


def _part(text: str, ids: list[int], **more: Any) -> SimpleNamespace:
    """One part of a completions reply, as the SDK hands it on."""
    choice = SimpleNamespace(
        index=0,
        text=text,
        token_ids=ids,
        logprobs=SimpleNamespace(
            token_logprobs=[-0.1] * len(ids), tokens=[f"token_id:{i}" for i in ids]
        ),
        finish_reason=more.get("finish"),
        prompt_token_ids=more.get("prompt"),
    )
    return SimpleNamespace(choices=[choice], usage=None)


class TestTheReadersTime:
    def test_a_reply_made_to_keep_the_reader_busy_is_read_at_once(self) -> None:
        """The reply is read inside the model call, so the reader's time is
        the call's.  A model can write an argument tag it never closes and
        fill the rest of its reply limit with white space, 128 spaces a token:
        the reader took hours over that once.  One token a part; no server."""
        opened = NL + "<function=bash>" + NL + "<parameter="
        head = [
            _part("ok", encode("ok"), prompt=list(REPLAYED_PROMPT)),
            _part("</think>", [THINK_END]),
            _part("<tool_call>", [CALL]),
            _part(opened, encode(opened)),
        ]
        spaces = " " * 128
        # Smallest first: a reader that takes quadratic time fails after
        # seconds, at a size a linear one does not notice.
        for tokens, bound in ((32, 0.5), (512, 0.5), (4096, 0.5), (32_768, 5.0)):
            generated = sum(len(p.choices[0].token_ids) for p in head) + tokens
            usage = SimpleNamespace(
                choices=[],
                usage={
                    "prompt_tokens": len(REPLAYED_PROMPT),
                    "completion_tokens": generated,
                    "total_tokens": generated + len(REPLAYED_PROMPT),
                },
            )
            run = [_part(spaces, [MERGED])] * (tokens - 1)
            last = _part(spaces, [MERGED], finish="length")
            client = _Replaying([*head, *run, last, usage])
            recorder = RecordingClient(client, id_prompts=PromptAssembly(ROUTE))
            chunks = ask(recorder, MESSAGES)
            (call,) = recorder.calls
            assert call.seconds < bound, f"{tokens} tokens of white space took {call.seconds:.2f} s"
            assert call.complete and call.problems() == []
            assert len(call.token_ids) == generated
            choice = chunks[0].choices[0]
            # A reply cut inside a call: the call, without the argument that never ended.
            assert choice.finish_reason == "length"
            assert [(c.function.name, c.function.arguments) for c in choice.delta.tool_calls] == [
                ("bash", "{}")
            ]


class TestTheChain:
    def two_turns(
        self, steps: list[dict[str, Any]], **server_options: Any
    ) -> tuple[RecordingClient, list[dict[str, Any]]]:
        """Two calls of one conversation; the recorder and the second request's messages."""
        with ScriptedChatServer(steps, **server_options) as server:
            recorder = recorder_for(server)
            first = ask(recorder, MESSAGES)
            longer = [*MESSAGES, turn_of(first), result_of(first, "hi")]
            ask(recorder, longer)
        return recorder, longer

    def test_a_prompt_is_the_previous_one_what_was_generated_and_the_tail(self) -> None:
        recorder, _longer = self.two_turns([STEP, {"content": "done"}])
        first, second = recorder.calls
        tail = [END, *encode(NL)]  # the turn's closing, less the token the model generated
        assert second.prompt_token_ids[: len(first.prompt_token_ids)] == first.prompt_token_ids
        start = len(first.prompt_token_ids) + len(first.token_ids)
        assert second.prompt_token_ids[len(first.prompt_token_ids) : start] == first.token_ids
        assert second.prompt_token_ids[start : start + 1] == tail[1:]
        assert second.prompt_token_ids[-len(ROUTE.generation_prompt) :] == list(
            ROUTE.generation_prompt
        )
        assert recorder.id_prompts.events == []

    def test_a_split_the_template_never_writes_stays_in_the_sequence(self) -> None:
        """The model generated one token where the template writes two: sent as
        messages the next prompt differs there, sent as token IDs it does not."""
        steps = [{**STEP, "content": "a cab drives", "merge": True}, {"content": "done"}]
        recorder, longer = self.two_turns(steps)
        first, second = recorder.calls
        assert MERGED in first.token_ids
        whole = first.prompt_token_ids + first.token_ids
        assert second.prompt_token_ids[: len(whole)] == whole
        # The server's own rendering of the same conversation has the other split.
        with ScriptedChatServer(steps) as server:
            plain = RecordingClient(make_client(server))
            ask(plain, MESSAGES)
            ask(plain, longer)
        rendered = plain.calls[1].prompt_token_ids
        assert rendered[: len(whole)] != whole and MERGED not in rendered
        # The record is told: this prompt is not the chat route's rendering.
        merged_at = len(first.prompt_token_ids) + first.token_ids.index(MERGED)
        assert recorder.id_prompts.events == [
            {
                "event": "prompt_differs_from_rendering",
                "call": 1,
                "prompt_tokens": len(second.prompt_token_ids),
                "rendered_tokens": len(rendered),
                "same_tokens": merged_at,
            }
        ]
        assert len(rendered) == len(second.prompt_token_ids) + 1

    def test_a_turn_that_ended_on_another_token_gets_the_whole_closing(self) -> None:
        recorder, _longer = self.two_turns([{**STEP, "end_token": OTHER_END}, {"content": "done"}])
        first, second = recorder.calls
        assert first.token_ids[-1] == OTHER_END and first.ended_by_itself
        start = len(first.prompt_token_ids) + len(first.token_ids)
        assert second.prompt_token_ids[:start] == first.prompt_token_ids + first.token_ids
        assert second.prompt_token_ids[start : start + 2] == [END, *encode(NL)]
        assert recorder.id_prompts.events == [
            {
                "event": "turn_ended_on_other_token",
                "turn": 0,
                "token_id": OTHER_END,
                "end_of_turn_token_id": END,
            },
            # The template would not have written the other token: one token more.
            {
                "event": "prompt_differs_from_rendering",
                "call": 1,
                "prompt_tokens": len(second.prompt_token_ids),
                "rendered_tokens": len(second.prompt_token_ids) - 1,
                "same_tokens": start - 1,
            },
        ]

    # The model generated one token where the template writes two, so the
    # second prompt is not the chat route's rendering.
    MERGED_STEPS = [{**STEP, "content": "a cab drives", "merge": True}, {"content": "done"}]

    def test_a_prompt_that_differs_is_noted_when_its_reply_has_ended(self) -> None:
        """A request that goes out and is not answered is no call of the token
        record either.  The same request again gets the same prompt, and the
        event is noted once, for the call that returned a reply."""
        with ScriptedChatServer(self.MERGED_STEPS, faults={1: "http_500"}) as server:
            recorder = recorder_for(server)
            first = ask(recorder, MESSAGES)
            longer = [*MESSAGES, turn_of(first), result_of(first, "hi")]
            with pytest.raises(openai.InternalServerError):
                ask(recorder, longer)
            assert recorder.id_prompts.events == []
            ask(recorder, longer)
            prompts = [r["prompt"] for r in server.requests]
        assert prompts[1] == prompts[2] == recorder.calls[2].prompt_token_ids
        (noted,) = recorder.id_prompts.events
        # The second call of the token record: the attempt that failed is none.
        assert noted["event"] == "prompt_differs_from_rendering" and noted["call"] == 1
        assert noted["prompt_tokens"] == len(prompts[2])

    def test_a_reply_to_a_prompt_the_server_did_not_report_leaves_no_event(self) -> None:
        """A call whose prompt the server did not report is none of the token
        record's, whatever was found in its reply: here a marker token between
        other tokens, in a part that spells the marker out once more."""
        twice = "so</think>the tag </think> again"
        tokens = [*encode("so"), THINK_END, *encode("the tag </think> again")]
        for reported, noted in ((list(REPLAYED_PROMPT), 1), (None, 0)):
            parts = [_part(twice, tokens, prompt=reported), _part("", [END], finish="stop")]
            recorder = RecordingClient(_Replaying(parts), id_prompts=PromptAssembly(ROUTE))
            ask(recorder, MESSAGES)
            (call,) = recorder.calls
            assert call.complete and call.prompt_token_ids == reported
            events = recorder.id_prompts.events
            assert [e["event"] for e in events] == ["marker_place_uncertain"] * noted

    def test_the_same_request_again_after_a_failed_attempt_gets_the_same_prompt(self) -> None:
        with ScriptedChatServer([STEP], faults={0: "http_500"}) as server:
            recorder = recorder_for(server)
            with pytest.raises(openai.InternalServerError):
                ask(recorder, MESSAGES)
            ask(recorder, MESSAGES)
            prompts = [r["prompt"] for r in server.requests]
        assert prompts[0] == prompts[1] == recorder.calls[1].prompt_token_ids
        assert recorder.calls[0].error and recorder.calls[1].complete

    def test_a_request_that_does_not_extend_the_previous_one_is_not_sent(self) -> None:
        with ScriptedChatServer([STEP, {"content": "done"}]) as server:
            recorder = recorder_for(server)
            first = ask(recorder, MESSAGES)
            changed = [{"role": "system", "content": "other"}, MESSAGES[1], turn_of(first)]
            with pytest.raises(PromptAssemblyError, match="does not begin with the messages"):
                ask(recorder, changed)
            with pytest.raises(PromptAssemblyError, match="not the assistant turn"):
                ask(recorder, [*MESSAGES, {"role": "user", "content": "more"}])
            assert len(server.requests) == 1
        assert "PromptAssemblyError" in recorder.calls[1].error

    def test_a_cut_turn_is_not_continued(self) -> None:
        with ScriptedChatServer([STEP, {"content": "done"}], keep_reply_limit=True) as server:
            recorder = recorder_for(server)
            first = list(
                recorder.chat.completions.create(
                    model="m", messages=MESSAGES, tools=TOOLS, stream=True, max_tokens=30
                )
            )
            with pytest.raises(PromptAssemblyError, match="did not end by itself"):
                ask(recorder, [*MESSAGES, turn_of(first)])

    def test_a_rendering_that_changes_once_messages_follow_is_refused(self) -> None:
        with ScriptedChatServer([STEP, {"content": "done"}]) as server:
            recorder = recorder_for(server)
            first = ask(recorder, MESSAGES)
            server.unstable_rendering = True
            with pytest.raises(PromptAssemblyError, match="differently once more messages follow"):
                ask(recorder, [*MESSAGES, turn_of(first), result_of(first, "hi")])
            assert len(server.requests) == 1

    def test_a_closing_other_than_the_one_found_before_the_run_is_refused(self) -> None:
        other = TokenIdRoute(**{**ROUTE.__dict__, "closing": (END, *encode("!"))})
        with ScriptedChatServer([STEP, {"content": "done"}]) as server:
            recorder = RecordingClient(make_client(server), id_prompts=PromptAssembly(other))
            first = ask(recorder, MESSAGES)
            with pytest.raises(PromptAssemblyError, match="does not end with the closing"):
                ask(recorder, [*MESSAGES, turn_of(first), result_of(first, "hi")])


class TestTheProbe:
    def test_it_asks_on_the_completions_route(self) -> None:
        with ScriptedChatServer([{"content": "probe"}], sampler_logprobs=True) as server:
            verdict = sampler_logprobs_reported(make_client(server), "m", route="completions")
            asked = list(server.requests)
        assert verdict["sampler_logprobs"] is True and verdict["route"] == "completions"
        assert asked and all(isinstance(r.get("prompt"), list) for r in asked)

    def test_a_server_reporting_the_models_own_values_there(self) -> None:
        with ScriptedChatServer([{"content": "probe"}]) as server:
            verdict = sampler_logprobs_reported(make_client(server), "m", route="completions")
        assert verdict["sampler_logprobs"] is False

    def test_the_chat_route_is_the_default(self) -> None:
        with ScriptedChatServer([{"content": "probe"}], sampler_logprobs=True) as server:
            verdict = sampler_logprobs_reported(make_client(server), "m")
            assert all("messages" in r for r in server.requests)
        assert verdict["route"] == "chat"

    # A reply whose second token the toy model is unsure of in the way a
    # measurement needs.
    OPENS_THE_SAME = [{"reasoning": "We need a note.", "content": "done"}]

    @pytest.mark.parametrize("reflects", [("temperature", "top_k", "top_p"), ()])
    def test_a_model_certain_of_its_first_token_is_measured_at_a_later_one(
        self, reflects: tuple[str, ...]
    ) -> None:
        with ScriptedChatServer(
            self.OPENS_THE_SAME, reflects=reflects, confident_first=1
        ) as server:
            verdict = sampler_logprobs_reported(make_client(server), "m", route="completions")
            asked = list(server.requests)
        assert verdict["sampler_logprobs"] is bool(reflects) and verdict["route"] == "completions"
        assert all(isinstance(r.get("prompt"), list) for r in asked)
        assert [r["max_tokens"] for r in asked][:6] == [1, 24, 24, 24, 24, 2]
        measured = verdict["tried"][1]
        assert measured["position"] == 1 and set(measured["first"].values()) == {bool(reflects)}

    def test_a_model_certain_of_every_token_has_no_verdict_there_either(self) -> None:
        with ScriptedChatServer(
            self.OPENS_THE_SAME, sampler_logprobs=True, confident=True
        ) as server:
            verdict = sampler_logprobs_reported(make_client(server), "m", route="completions")
            asked = len(server.requests)
        assert verdict["sampler_logprobs"] is None and asked == 3
        assert "certain of every token" in verdict["reason"]

    def test_the_command_probing_alone_asks_on_the_route_its_rollouts_will_use(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A batch probes once (``--probe-only``) and passes the verdict on: with
        prompts as token IDs that probe has to be made on the completions route."""
        with ScriptedChatServer(SCRIPT, sampler_logprobs=True) as server:
            base = ["--base-url", server.base_url, "--model", "scripted", "--probe-only"]
            base += ["--top-k", "20", "--top-p", "0.95"]
            assert cli.main([*base, "--token-id-prompts"]) == 0
            asked = list(server.requests)
        verdict = json.loads(capsys.readouterr().out)
        assert verdict["route"] == "completions" and verdict["sampler_logprobs"] is True
        assert asked and all(isinstance(r.get("prompt"), list) for r in asked)


class TestWholeRollouts:
    def test_a_rollout_with_prompts_as_token_ids(self) -> None:
        with ScriptedChatServer(SCRIPT, sampler_logprobs=True) as server:
            record, box = _run(server, token_id_prompts=True, sampler_logprobs=None, keep_text=True)
            task_requests = [r for r in server.requests if r.get("max_tokens") != 1][-3:]
        assert record["finish_state"] == "completed"
        assert record["trainable"] is True and record["one_sequence"] is True
        assert record["problems"] == [] and record["prompt_mode"] == "token_ids"
        assert record["token_id_route"]["ok"] is True
        assert record["logprobs"]["route"] == "completions"
        # Every request of the task went out as token IDs, and the session got its
        # tool calls and ran them.
        assert all(isinstance(r.get("prompt"), list) for r in task_requests)
        assert [e["tool"] for e in record["tool_log"]] == ["write_file", "bash", "read_file"]
        assert all(e["ok"] for e in record["tool_log"])
        tokens = record["tokens"]
        assert rebuild_prompts(tokens) == [r["prompt"] for r in task_requests]
        assert len(rebuild_final(tokens)) == tokens["final_len"]
        assert [c["route"] for c in record["calls"]] == ["completions"] * 3
        assert [c["finish_reason"] for c in record["calls"]] == ["tool_calls", "tool_calls", "stop"]
        assert record["sampling"]["requested"] == {"top_k": 20, "top_p": 0.95, "temperature": 1.0}
        assert box.closed == 1

    def test_the_same_rollout_as_messages_says_so(self) -> None:
        with ScriptedChatServer(SCRIPT) as server:
            record, _box = _run(server)
        assert record["prompt_mode"] == "messages" and record["token_id_route"] is None
        assert [c["route"] for c in record["calls"]] == ["chat"] * 3

    def test_a_split_that_breaks_the_sequence_as_messages_does_not_as_token_ids(self) -> None:
        script = [{**SCRIPT[0], "content": "a cab", "merge": True}, *SCRIPT[1:]]
        with ScriptedChatServer(script) as server:
            as_messages, _box = _run(server)
        with ScriptedChatServer(script) as server:
            as_ids, _box = _run(server, token_id_prompts=True)
        assert as_messages["trainable"] is True and as_messages["one_sequence"] is False
        assert as_ids["trainable"] is True and as_ids["one_sequence"] is True
        assert as_ids["prefix_ok"] and not as_messages["prefix_ok"]
        # As token IDs the run stays one sequence, and its record says that
        # the prompts after that turn are not what the chat route renders:
        # one event for each of the two later calls, from the same place on.
        noted = [
            e for e in as_ids["prefix_events"] if e["event"] == "prompt_differs_from_rendering"
        ]
        calls = as_ids["tokens"]["calls"]
        assert [e["call"] for e in noted] == [1, 2]
        assert [e["prompt_tokens"] for e in noted] == [c["prompt_len"] for c in calls[1:]]
        assert [e["rendered_tokens"] - e["prompt_tokens"] for e in noted] == [1, 1]
        assert noted[0]["same_tokens"] == noted[1]["same_tokens"] < calls[1]["prompt_len"]
        assert as_messages["prefix_events"] == []

    def test_a_turn_ending_on_another_token_is_noted_and_stays_one_sequence(self) -> None:
        script = [{**SCRIPT[0], "end_token": OTHER_END}, *SCRIPT[1:]]
        with ScriptedChatServer(script) as server:
            record, _box = _run(server, token_id_prompts=True)
        assert record["one_sequence"] is True and record["finish_state"] == "completed"
        assert {"event": "turn_ended_on_other_token", "turn": 0}.items() <= (
            record["prefix_events"][0].items()
        )

    def test_a_reply_cut_at_the_limit_ends_the_rollout(self) -> None:
        script = [{**SCRIPT[0], "finish_reason": "length"}, *SCRIPT[1:]]
        with ScriptedChatServer(script) as server:
            record, _box = _run(server, token_id_prompts=True)
        assert record["finish_state"] == "length" and record["turns"] == 1
        assert record["trainable"] is True and record["tool_log"] == []

    def test_half_a_surrogate_pair_the_model_wrote_does_not_end_the_run(self) -> None:
        """A model can write half of a surrogate pair as an escape inside a
        typed argument, and write the argument twice, so that the session
        sends the call back from the values that were read.  No request body
        can carry half a pair: the next prompt could not be rendered, and the
        run ended as the machinery's over a text the model chose.  What the
        reader hands on can always be carried."""
        half = '"' + chr(92) + 'ud800"'
        twice = (
            "<parameter=command>" + NL + "echo hi" + NL + "</parameter>" + NL
            + "<parameter=timeout>" + NL + "5" + NL + "</parameter>" + NL
            + "<parameter=timeout>" + NL + half + NL + "</parameter>"
        )  # fmt: skip
        script = [
            {"reasoning": "Twice.", "tool_calls": [{"name": "bash", "arguments": twice}]},
            {"reasoning": "Finished.", "content": "All done."},
        ]
        with ScriptedChatServer(script) as server:
            record, _box = _run(server, token_id_prompts=True)
        assert record["finish_state"] == "completed" and record["finish_cause"] == "model"
        assert record["trainable"] is True and record["one_sequence"] is True
        assert record["turns"] == 2 and record["error"] is None
        # The call was sent back from what was read of it, with the half replaced.
        sent_back = record["messages"][2]["tool_calls"][0]["function"]["arguments"]
        assert json.loads(sent_back)["timeout"] == chr(0xFFFD)
        assert "raw_arguments_unrecovered" in [e["event"] for e in record["prefix_events"]]

    def test_a_failure_of_the_assembly_ends_the_run_as_the_machinerys(self) -> None:
        with ScriptedChatServer(SCRIPT) as server:
            verdict = check_token_id_route(make_client(server), "scripted")
            server.unstable_rendering = True
            record, _box = _run(server, token_id_prompts=True, token_id_route=verdict)
        assert record["finish_state"] == "error" and record["finish_cause"] == "machinery"
        assert record["trainable"] is False
        assert "PromptAssemblyError" in record["error"]
        assert "differently once more messages follow" in record["error"]

    def test_a_given_verdict_spares_the_check(self) -> None:
        with ScriptedChatServer(SCRIPT) as server:
            verdict = check_token_id_route(make_client(server), "scripted")
            before = len(server.requests) + len(server.tokenize_requests)
            record, _box = _run(server, token_id_prompts=True, token_id_route=verdict)
            # Three calls of the task; each later one renders the conversation twice.
            assert len(server.requests) + len(server.tokenize_requests) - before == 3 + 1 + 2 + 2
        assert record["one_sequence"] is True
        with ScriptedChatServer(SCRIPT) as server, pytest.raises(SetupError, match="reply format"):
            _run(server, token_id_prompts=True, token_id_route={**verdict, "format": "other"})

    def test_a_sub_agent_uses_the_same_route(self) -> None:
        parent = [
            {"reasoning": "Delegate.", "tool_calls": [SPAWN]},
            {"reasoning": "Good.", "content": "The note is written."},
        ]
        agent = [
            {
                "reasoning": "Write it.",
                "tool_calls": [
                    {"name": "write_file", "arguments": {"path": "n", "content": "note"}}
                ],
            },
            {"reasoning": "Report.", "content": "wrote n"},
        ]
        with ScriptedChatServer(parent, agent_script=agent) as server:
            record, _box = _run(server, token_id_prompts=True)
            agent_requests = server.agent_requests
            parent_requests = [r for r in server.parent_requests if r.get("max_tokens") != 1]
        assert record["finish_state"] == "completed" and record["one_sequence"] is True
        assert [(e["tool"], e["ok"]) for e in record["tool_log"]] == [("task_agent", True)]
        assert record["tool_log"][0]["result"].strip() == "wrote n"
        sub = record["subagents"]
        assert sub["started"] == 1 and sub["model_calls"] == 2 and sub["failures"] == []
        assert all(isinstance(r.get("prompt"), list) for r in agent_requests + parent_requests[-2:])
        assert len(record["tokens"]["calls"]) == 2
