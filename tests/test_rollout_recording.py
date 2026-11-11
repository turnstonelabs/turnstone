"""The rollout's recording client, probe and argument reader against the scripted server."""

from __future__ import annotations

from typing import Any

import openai
import pytest

from tests._scripted_chat_server import (
    END,
    ScriptedChatServer,
    assistant_tokens,
    call_text,
    fake_logprob,
    render_prompt,
)
from turnstone.core import session as session_module
from turnstone.rollout.probe import sampler_logprobs_reported
from turnstone.rollout.rawargs import RecordedArguments, parameter_block_calls
from turnstone.rollout.recording import (
    PARENT,
    SUB_AGENT,
    RecordingClient,
    bad_logprob_positions,
)

NL = chr(10)
CALL = {"name": "bash", "arguments": {"command": "echo hi"}}
STEP: dict[str, Any] = {
    "reasoning": "Think first.",
    "content": "Calling a tool.",
    "tool_calls": [CALL],
}
MESSAGES = [{"role": "system", "content": "sys"}, {"role": "user", "content": "go"}]
TOOLS = [
    {
        "type": "function",
        "function": {"name": "bash", "parameters": {"type": "object", "properties": {}}},
    }
]


def expected_generated() -> list[int]:
    return assistant_tokens(STEP["reasoning"], STEP["content"], [("bash", {"command": "echo hi"})])


def make_client(server: ScriptedChatServer) -> openai.OpenAI:
    return openai.OpenAI(base_url=server.base_url, api_key="none", max_retries=0)


def record_one(server: ScriptedChatServer, **create: Any) -> Any:
    recorder = RecordingClient(make_client(server))
    list(
        recorder.chat.completions.create(
            model="m",
            messages=MESSAGES,
            tools=TOOLS,
            stream=True,
            stream_options={"include_usage": True},
            **create,
        )
    )
    recorder.close()
    return recorder.calls[0]


def drain(stream: Any) -> tuple[str, str, list[str], str]:
    reasoning, content, arguments, names = "", "", "", []
    for chunk in stream:
        for choice in chunk.choices:
            delta = choice.delta
            reasoning += getattr(delta, "reasoning", None) or ""
            content += delta.content or ""
            for tool_call in delta.tool_calls or []:
                if tool_call.function and tool_call.function.name:
                    names.append(tool_call.function.name)
                if tool_call.function and tool_call.function.arguments:
                    arguments += tool_call.function.arguments
    return reasoning, content, names, arguments


class TestRecording:
    def test_a_plain_client_gets_no_token_data(self):
        with ScriptedChatServer([STEP]) as server:
            client = make_client(server)
            list(client.chat.completions.create(model="m", messages=MESSAGES, stream=True))
            client.close()
            assert "logprobs" not in server.requests[0]
            assert "return_token_ids" not in server.requests[0]

    def test_a_streamed_call_is_recorded_and_passed_through_unchanged(self):
        with ScriptedChatServer([STEP]) as server:
            recorder = RecordingClient(make_client(server))
            stream = recorder.chat.completions.create(
                model="m",
                messages=MESSAGES,
                tools=TOOLS,
                stream=True,
                stream_options={"include_usage": True},
            )
            reasoning, content, names, arguments = drain(stream)
            recorder.close()
            sent = server.requests[0]
        # The session still sees the whole reply.
        assert (reasoning, content, names) == (STEP["reasoning"], STEP["content"], ["bash"])
        assert arguments == '{"command": "echo hi"}'
        # The request asked for token data although the caller did not.
        assert sent["logprobs"] is True and sent["return_token_ids"] is True
        assert sent["return_tokens_as_token_ids"] is True

        record = recorder.calls[0]
        generated = expected_generated()
        assert record.streamed and record.complete and record.error is None
        assert record.scope == PARENT
        assert record.prompt_token_ids == render_prompt(MESSAGES, TOOLS)
        assert record.token_ids == generated and record.token_ids[-1] == END
        assert record.logprobs == [fake_logprob(t, i) for i, t in enumerate(generated)]
        assert record.finish_reason == "tool_calls"
        # Both channels were compared token by token, and the counts with the server's.
        assert record.ids_checked is True
        assert record.usage == {
            "prompt_tokens": len(record.prompt_token_ids),
            "completion_tokens": len(generated),
            "total_tokens": len(record.prompt_token_ids) + len(generated),
        }
        assert record.problems() == []

    def test_the_generated_text_is_kept_as_the_model_wrote_it(self):
        typed = {
            "name": "bash",
            "arguments": {"command": "x", "run_in_background": True},
            "raw": {"run_in_background": "True"},
        }
        with ScriptedChatServer([{"reasoning": "r", "tool_calls": [typed]}]) as server:
            record = record_one(server)
        text = record.raw_text
        assert text.startswith("r</think>" + NL + NL + "<tool_call>")
        assert "<parameter=run_in_background>" + NL + "True" + NL + "</parameter>" in text
        assert text.endswith("</tool_call><end>")

    def test_a_non_streamed_call_is_recorded(self):
        with ScriptedChatServer([STEP]) as server:
            recorder = RecordingClient(make_client(server))
            response = recorder.chat.completions.create(model="m", messages=MESSAGES, tools=TOOLS)
            recorder.close()
        assert response.choices[0].message.content == STEP["content"]
        record = recorder.calls[0]
        generated = expected_generated()
        assert not record.streamed and record.complete
        assert record.token_ids == generated
        assert record.logprobs == [fake_logprob(t, i) for i, t in enumerate(generated)]
        assert record.problems() == []

    def test_extra_body_is_merged_with_the_callers(self):
        with ScriptedChatServer([STEP]) as server:
            recorder = RecordingClient(
                make_client(server),
                extra_body={"top_k": 20, "chat_template_kwargs": {"enable_thinking": True}},
            )
            list(
                recorder.chat.completions.create(
                    model="m",
                    messages=MESSAGES,
                    stream=True,
                    extra_body={"chat_template_kwargs": {"reasoning_effort": "low"}, "min_p": 0.1},
                )
            )
            recorder.close()
            sent = server.requests[0]
        assert sent["top_k"] == 20 and sent["min_p"] == 0.1
        assert sent["chat_template_kwargs"] == {"reasoning_effort": "low", "enable_thinking": True}

    def test_an_abandoned_stream_is_marked_incomplete(self):
        with ScriptedChatServer([STEP]) as server:
            recorder = RecordingClient(make_client(server))
            stream = recorder.chat.completions.create(model="m", messages=MESSAGES, stream=True)
            for count, _chunk in enumerate(stream):
                if count == 3:
                    break
            stream.close()
            recorder.close()
        record = recorder.calls[0]
        assert not record.complete and record.finish_reason is None
        assert 0 < len(record.token_ids) < len(expected_generated())
        assert "the reply did not end normally" in record.problems()

    def test_a_failed_request_is_recorded_and_raised(self):
        with ScriptedChatServer([STEP]) as server:
            url = server.base_url
        recorder = RecordingClient(openai.OpenAI(base_url=url, api_key="none", max_retries=0))
        with pytest.raises(openai.APIConnectionError):
            recorder.chat.completions.create(model="m", messages=MESSAGES, stream=True)
        recorder.close()
        assert recorder.calls[0].error and not recorder.calls[0].complete

    def test_a_per_call_copy_shares_the_record_list(self):
        with ScriptedChatServer([STEP]) as server:
            recorder = RecordingClient(make_client(server), extra_body={"top_k": 5})
            copy = recorder.with_options(api_key="other")
            list(copy.chat.completions.create(model="m", messages=MESSAGES, stream=True))
            recorder.close()
            assert server.requests[0]["top_k"] == 5
        assert len(recorder.calls) == 1 and recorder.calls is copy.calls
        assert str(recorder.base_url).startswith("http://127.0.0.1")

    def test_a_call_made_inside_a_sub_agent_is_marked(self):
        with ScriptedChatServer([STEP]) as server:
            recorder = RecordingClient(make_client(server))
            token = session_module._active_task_agent_cancel_scope.set(object())  # type: ignore[arg-type]
            try:
                list(recorder.chat.completions.create(model="m", messages=MESSAGES, stream=True))
            finally:
                session_module._active_task_agent_cancel_scope.reset(token)
            list(recorder.chat.completions.create(model="m", messages=MESSAGES, stream=True))
            recorder.close()
        assert [c.scope for c in recorder.calls] == [SUB_AGENT, PARENT]
        assert recorder.last_parent_call() is recorder.calls[1]
        assert recorder.scoped(SUB_AGENT) == [recorder.calls[0]]


class TestWhatMakesACallUntrustworthy:
    def test_token_ids_without_log_probabilities(self):
        """Every later value would be paired with the wrong token, so none is kept."""
        with ScriptedChatServer([STEP], faults={0: "skip_logprobs"}) as server:
            record = record_one(server)
        assert record.complete
        assert record.token_ids == expected_generated()  # the tokens are all there
        assert record.aligned is False and record.misaligned_chunks == 1
        assert record.logprobs == [] and record.to_dict()["logprobs"] is None
        assert any("did not arrive one to one" in p for p in record.problems())

    def test_tokens_missing_from_both_channels_are_caught_by_the_usage_count(self):
        """Equal counts of IDs and values say nothing about a gap in both; the
        server's own count of what it generated does."""
        with ScriptedChatServer([STEP], faults={0: "drop_tokens"}) as server:
            record = record_one(server)
        assert record.complete and record.aligned  # the two channels agree with each other
        generated = len(expected_generated())
        assert len(record.token_ids) == len(record.logprobs) < generated
        assert record.problems() == [
            f"the server counted {generated} generated tokens, {len(record.token_ids)} "
            "were recorded"
        ]

    def test_log_probabilities_named_for_other_tokens(self):
        with ScriptedChatServer([STEP], faults={0: "wrong_ids"}) as server:
            record = record_one(server)
        assert record.ids_checked is True and record.aligned is False
        assert record.logprobs == []
        assert any("did not arrive one to one" in p for p in record.problems())

    def test_no_usage_means_no_check(self):
        with ScriptedChatServer([STEP], faults={0: "no_usage"}) as server:
            record = record_one(server)
        assert record.problems() == [
            "the server reported no usage to check the token counts against"
        ]

    def test_a_value_the_recorder_cannot_read_never_reaches_the_session(self):
        """A null log-probability used to raise out of the stream the provider was
        reading and fail the model call.  The reply goes through whole; the record
        says it cannot be trusted."""
        with ScriptedChatServer([STEP], faults={0: "null_logprob"}) as server:
            recorder = RecordingClient(make_client(server))
            stream = recorder.chat.completions.create(
                model="m",
                messages=MESSAGES,
                tools=TOOLS,
                stream=True,
                stream_options={"include_usage": True},
            )
            reasoning, content, names, _arguments = drain(stream)
            recorder.close()
        assert (reasoning, content, names) == (STEP["reasoning"], STEP["content"], ["bash"])
        record = recorder.calls[0]
        assert record.complete and record.error is None
        assert record.unreadable is not None and record.aligned is False
        assert any("could not be read" in p for p in record.problems())

    def test_impossible_log_probabilities_are_found(self):
        assert bad_logprob_positions([-0.5, 0.0, 1e-7]) == []
        assert bad_logprob_positions([-0.5, 0.3, -9999.0, float("nan"), float("-inf")]) == [
            1,
            2,
            3,
            4,
        ]


class TestProbe:
    def test_a_server_reporting_the_models_own_distribution(self):
        with ScriptedChatServer([{"content": "one two"}]) as server:
            client = make_client(server)
            verdict = sampler_logprobs_reported(client, "m", extra_body={"top_k": 20, "x": 1})
            client.close()
            control, by_top_k, by_top_p, by_temperature = server.requests
        assert verdict["sampler_logprobs"] is False
        assert verdict["reflects"] == {"temperature": False, "top_k": False, "top_p": False}
        # The control leaves the distribution as it is and asks for the alternatives;
        # each other request changes one setting.  The rollout's other arguments ride along.
        neutral = {"temperature": 1.0, "top_k": 0, "top_p": 1.0}
        assert {k: control[k] for k in neutral} == neutral
        assert control["top_logprobs"] == 20 and control["x"] == 1
        assert {k: by_top_k[k] for k in neutral} == {**neutral, "top_k": 1}
        assert {k: by_top_p[k] for k in neutral} == {**neutral, "top_p": 0.001}
        assert {k: by_temperature[k] for k in neutral} == {**neutral, "temperature": 0.5}
        (tried,) = verdict["tried"]
        assert tried["used"] and tried["likeliest"] < -0.05
        assert tried["top_k"] == tried["top_p"] == tried["temperature"] == tried["likeliest"]

    def test_a_server_reporting_the_sampling_distribution(self):
        with ScriptedChatServer([{"content": "one two"}], sampler_logprobs=True) as server:
            client = make_client(server)
            verdict = sampler_logprobs_reported(client, "m")
            client.close()
        assert verdict["sampler_logprobs"] is True
        assert verdict["reflects"] == {"temperature": True, "top_k": True, "top_p": True}
        tried = verdict["tried"][0]
        assert tried["top_k"] == tried["top_p"] == 0.0
        # Sharpened by the probe's temperature: nearer to zero than the model's own value.
        assert tried["likeliest"] < tried["temperature"] < 0.0

    @pytest.mark.parametrize("reflected", [("temperature",), ("top_k", "top_p"), ("top_p",)])
    def test_each_setting_is_measured_by_itself(self, reflected):
        """A server may apply some settings before the values it reports and others
        after.  A verdict for one setting must not stand for the others."""
        with ScriptedChatServer([{"content": "one two"}], reflects=reflected) as server:
            client = make_client(server)
            verdict = sampler_logprobs_reported(client, "m")
            client.close()
        assert verdict["reflects"] == {
            name: name in reflected for name in ("temperature", "top_k", "top_p")
        }
        assert verdict["sampler_logprobs"] is None
        assert "after some sampler settings and before others" in verdict["reason"]

    def test_a_model_certain_of_everything_gives_no_verdict(self):
        """Its own log-probabilities are near zero too, so zeros prove nothing: the
        probe must not pass such a server as reporting the sampling distribution."""
        with ScriptedChatServer([{"content": "one two"}], confident=True) as server:
            client = make_client(server)
            verdict = sampler_logprobs_reported(client, "m")
            client.close()
            requests = len(server.requests)
        assert verdict["sampler_logprobs"] is None
        assert "certain" in verdict["reason"]
        assert requests == 3 and all(not t["used"] for t in verdict["tried"])


class TestArgumentText:
    def test_values_are_read_as_written(self):
        text = (
            "thinking</think>"
            + NL
            + NL
            + "<tool_call>"
            + call_text(
                "bash",
                {"command": "a" + NL + "b", "flag": True, "none": None, "blank": "", "n": 5},
                {"flag": "True", "none": "None"},
            )
            + "</tool_call><tool_call>"
            + call_text("read_file", {"path": "x"})
            + "</tool_call><end>"
        )
        assert parameter_block_calls(text) == [
            (
                "bash",
                {"command": "a" + NL + "b", "flag": "True", "none": "None", "blank": "", "n": "5"},
            ),
            ("read_file", {"path": "x"}),
        ]

    def test_a_call_with_no_arguments_and_text_with_no_calls(self):
        assert parameter_block_calls("<tool_call>" + call_text("ls", {}) + "</tool_call>") == [
            ("ls", {})
        ]
        assert parameter_block_calls("just text") == []

    @pytest.mark.parametrize(
        "block",
        [
            NL
            + "<function=bash>"
            + NL
            + "<parameter=a>value</parameter>"
            + NL
            + "</function>"
            + NL,
            NL + "<function=bash>" + NL + "<parameter=a>" + NL + "1" + NL + "</function>" + NL,
            NL + "<function=bash>" + NL + "stray text" + NL + "</function>" + NL,
            NL + "no function here" + NL,
            NL
            + "<function=bash>"
            + NL
            + ("<parameter=a>" + NL + "1" + NL + "</parameter>" + NL) * 2
            + "</function>"
            + NL,
        ],
    )
    def test_a_call_of_another_shape_is_not_guessed_at(self, block):
        assert parameter_block_calls("<tool_call>" + block + "</tool_call>") == [None]
        assert parameter_block_calls("<tool_call>" + block) == [None]

    def test_an_argument_written_twice_is_not_guessed_at(self):
        """A server keeps one of the two values, and which one is its business.  The
        text cannot say what the call meant, so it gives no account of it."""
        twice = (
            "<function=bash>"
            + NL
            + "<parameter=command>"
            + NL
            + "echo one"
            + NL
            + "</parameter>"
            + NL
            + "<parameter=command>"
            + NL
            + "echo two"
            + NL
            + "</parameter>"
            + NL
            + "</function>"
            + NL
        )
        once = twice.replace("<parameter=command>" + NL + "echo two" + NL + "</parameter>" + NL, "")
        assert parameter_block_calls("<tool_call>" + NL + once + "</tool_call>") == [
            ("bash", {"command": "echo one"})
        ]
        assert parameter_block_calls("<tool_call>" + NL + twice + "</tool_call>") == [None]

    def test_the_source_reads_the_sessions_latest_own_call(self):
        typed = {
            "name": "bash",
            "arguments": {"command": "x", "run_in_background": True},
            "raw": {"run_in_background": "True"},
        }
        with ScriptedChatServer([{"tool_calls": [typed, CALL]}]) as server:
            recorder = RecordingClient(make_client(server))
            source = RecordedArguments(recorder)
            assert source([{"function": {"name": "bash"}}]) is None  # nothing recorded yet
            list(
                recorder.chat.completions.create(
                    model="m",
                    messages=MESSAGES,
                    stream=True,
                    stream_options={"include_usage": True},
                )
            )
            recorder.close()
        calls = [{"function": {"name": "bash"}}, {"function": {"name": "bash"}}]
        assert source(calls) == [
            {"command": "x", "run_in_background": "True"},
            {"command": "echo hi"},
        ]
        # Another count of calls, or another function, is not accounted for.
        assert source(calls[:1]) is None
        assert source([{"function": {"name": "bash"}}, {"function": {"name": "other"}}]) == [
            {"command": "x", "run_in_background": "True"},
            None,
        ]
