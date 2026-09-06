"""Completed no-answer responses must not silently park a conversation (#1070)."""

import json
from contextlib import contextmanager
from dataclasses import replace
from unittest.mock import patch

import anthropic
import httpx
import httpx2
import pytest
from openai import APIConnectionError, OpenAI

from tests._session_helpers import (
    NullUI,
    RecordingUI,
    make_registered_session,
    replace_session_lane,
)
from turnstone.core.memory import load_last_error
from turnstone.core.providers import create_provider
from turnstone.core.session import ConversationPersistenceError, GenerationCancelled
from turnstone.core.trajectory import dicts_from_turns
from turnstone.core.workstream import WorkstreamKind

_REASONING = "private reasoning sentinel: inspect the completed child and"
_KINDS = [WorkstreamKind.INTERACTIVE, WorkstreamKind.COORDINATOR]
# Runtime sample for recovery; every shape in _EMPTY_SHAPES is recoverable.
_RECOVERY_SHAPES = [
    ("separate", True),
    ("separate", False),
    ("inline", False),
]
_EMPTY_SHAPES = _RECOVERY_SHAPES + [
    ("unclosed", False),
    ("empty", True),
    ("whitespace", False),
]


class _UsageUI(RecordingUI):
    def on_status(self, usage, context_window, effort):
        self._rec("status", usage)


class _CounterUI(NullUI):
    def __init__(self):
        super().__init__()
        self.stream_ends = 0
        self.stream_discards = 0

    def on_state_change(self, state):
        pass

    def on_stream_end(self):
        self.stream_ends += 1
        super().on_stream_end()

    def on_stream_discarded(self):
        self.stream_discards += 1
        super().on_stream_discarded()


def _wire(shape, *, finish="stop"):
    delta = {"content": ""}
    if shape == "separate":
        delta["reasoning_content"] = _REASONING
    elif shape == "inline":
        delta["content"] = f"<think>{_REASONING}</think>"
    elif shape == "unclosed":
        delta["content"] = f"<think>{_REASONING}"
    elif shape == "whitespace":
        delta["content"] = " \n\t"
    elif shape == "answer":
        delta["content"] = "The work is complete."
    elif shape == "literal":
        delta["content"] = "<think>literal text</think>"
    elif shape == "death":
        delta["reasoning_content"] = _REASONING
    elif shape == "tool":
        delta["tool_calls"] = [
            {
                "index": 0,
                "id": "call-once",
                "type": "function",
                "function": {"name": "lookup", "arguments": "{}"},
            }
        ]
        finish = "tool_calls"
    elif shape == "refusal":
        delta["refusal"] = "Cannot help."
    header = {
        "id": "scope-response",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "served-model",
    }
    chunks = [
        {**header, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
        {
            **header,
            "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
            "usage": {"prompt_tokens": 24038, "completion_tokens": 43, "total_tokens": 24081},
        },
    ]
    if shape == "death":
        chunks = chunks[:1]
    return "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n"


def _native_wire(family, shape):
    text = {
        "answer": "The work is complete.",
        "separate": _REASONING,
        "inline": f"<think>{_REASONING}</think>",
        "unclosed": f"<think>{_REASONING}",
        "whitespace": " \n\t",
        "literal": "<think>literal text</think>",
    }[shape]
    if family == "openai":
        output = (
            {
                "type": "message",
                "id": "msg",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
            if shape != "separate"
            else {
                "type": "reasoning",
                "id": "reason",
                "summary": [{"type": "summary_text", "text": text}],
            }
        )
        events = [
            {
                "type": "response.completed",
                "sequence_number": 1,
                "response": {
                    "id": "response",
                    "object": "response",
                    "created_at": 0,
                    "model": "served-model",
                    "status": "completed",
                    "output": [output],
                    "usage": {"input_tokens": 24038, "output_tokens": 43, "total_tokens": 24081},
                },
            }
        ]
    else:
        kind = "thinking" if shape == "separate" else "text"
        events = [
            {
                "type": "message_start",
                "message": {
                    "id": "message",
                    "type": "message",
                    "role": "assistant",
                    "model": "served-model",
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 24038, "output_tokens": 0},
                },
            },
            {"type": "content_block_start", "index": 0, "content_block": {"type": kind, kind: ""}},
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": kind + "_delta", kind: text},
            },
            {"type": "content_block_stop", "index": 0},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": 43},
            },
            {"type": "message_stop"},
        ]
    return "".join(
        "event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n" for event in events
    )


@contextmanager
def _session(
    kind,
    shapes,
    *,
    server_parses=True,
    native=False,
    finish="stop",
    ui=None,
    family="openai-compatible",
):
    requests = []
    http = httpx if family == "anthropic-compatible" else httpx2
    client_type = anthropic.Anthropic if family == "anthropic-compatible" else OpenAI

    def respond(request):
        endpoint = {
            "openai-compatible": "chat/completions",
            "openai": "responses",
            "anthropic-compatible": "messages",
        }[family]
        assert request.url.path == "/v1/" + endpoint
        requests.append(json.loads(request.content))
        index = len(requests) - 1
        assert index < len(shapes), "unexpected additional provider request"
        return http.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=(
                _wire(shapes[index], finish=finish)
                if family == "openai-compatible"
                else _native_wire(family, shapes[index])
            ),
        )

    ui = ui or _UsageUI()
    with client_type(
        api_key="test-only",
        base_url=(
            "https://provider.example.com"
            if family == "anthropic-compatible"
            else "https://provider.example.com/v1"
        ),
        http_client=http.Client(transport=http.MockTransport(respond)),
        max_retries=0,
    ) as client:
        session = make_registered_session(ui=ui, user_id="empty-response-user", kind=kind)
        session._title_generated = True
        session._RETRY_BASE_DELAY = 0
        provider = create_provider(family)
        replace_session_lane(
            session,
            provider=provider,
            client=client,
            model="served-model",
            capabilities=replace(
                provider.get_capabilities("served-model"),
                server_parses_reasoning=server_parses,
                supports_web_search=native,
            ),
        )
        yield session, ui, requests


@pytest.mark.parametrize("kind", _KINDS)
@pytest.mark.parametrize("shape,server_parses", _EMPTY_SHAPES)
def test_empty_completion_exhaustion(tmp_db, kind, shape, server_parses):
    with (
        _session(kind, [shape] * 3, server_parses=server_parses) as (session, ui, requests),
        pytest.raises(RuntimeError, match="no answer"),
    ):
        session.send("Continue the requested work.")

    assert len(requests) == 3
    assert ui.of("state")[-1] == "error"
    assert not [turn for turn in session.messages if turn.role == "assistant"]
    assert ui.kinds().count("stream_end") == 3
    assert ui.kinds().count("stream_discarded") == 3
    assert ui.kinds().count("turn_committed") == 0
    assert len(ui.of("status")) == 3
    assert sum(usage["completion_tokens"] for usage in ui.of("status")) == 129
    assert {usage["model"] for usage in ui.of("status")} == {"served-model"}
    error = load_last_error(session.ws_id)
    assert error and "no answer" in error
    assert _REASONING not in error


@pytest.mark.parametrize("kind", _KINDS)
@pytest.mark.parametrize("shape,server_parses", _RECOVERY_SHAPES)
def test_empty_completion_recovers_once(tmp_db, kind, shape, server_parses):
    with _session(kind, [shape, "answer"], server_parses=server_parses) as (session, ui, requests):
        session.send("Continue the requested work.")

    assert len(requests) == 2
    assert requests[0] == requests[1]
    assert ui.of("state")[-1] == "idle"
    assistant = [turn for turn in dicts_from_turns(session.messages) if turn["role"] == "assistant"]
    assert len(assistant) == 1
    assert assistant[0]["content"] == "The work is complete."
    assert ui.kinds().count("turn_committed") == 1
    assert ui.kinds().count("stream_discarded") == 1
    assert ui.kinds().count("stream_end") == 2
    assert len(ui.of("status")) == 2
    assert not load_last_error(session.ws_id)


@pytest.mark.parametrize("kind", _KINDS)
def test_stop_in_empty_backoff_preserves_marker(tmp_db, kind):
    with _session(kind, ["separate"]) as (session, ui, requests):
        original_backoff = session._backoff_or_cancelled

        def stop_in_backoff(delay, generation):
            session.cancel()
            original_backoff(delay, generation)

        with patch.object(session, "_backoff_or_cancelled", side_effect=stop_in_backoff):
            session.send("Continue the requested work.")

    assert len(requests) == 1
    assert ui.of("state")[-1] == "idle"
    assistant = [turn for turn in dicts_from_turns(session.messages) if turn["role"] == "assistant"]
    assert len(assistant) == 1
    assert "cancel" in assistant[0]["content"].lower()
    assert _REASONING not in assistant[0]["content"]
    assert len(ui.of("status")) == 1


@pytest.mark.parametrize("kind", _KINDS)
@pytest.mark.parametrize("family", ["openai-compatible", "openai", "anthropic-compatible"])
def test_server_parsed_literal_tags_are_an_answer(tmp_db, kind, family):
    with _session(kind, ["literal"], family=family) as (session, ui, requests):
        session.send("Explain the literal tags.")
    assert len(requests) == 1
    assert session.messages[-1].text == "<think>literal text</think>"
    assert not ui.of("error")


@pytest.mark.parametrize("finish", ["length", "content_filter", "unknown_stop"])
def test_nonordinary_finish_does_not_reissue(tmp_db, finish):
    with _session(WorkstreamKind.INTERACTIVE, ["separate"], finish=finish) as (
        session,
        ui,
        requests,
    ):
        session.send("Continue the requested work.")
    assert len(requests) == 1
    assert "stream_discarded" not in ui.kinds()


@pytest.mark.parametrize("kind", _KINDS)
def test_native_search_empty_completion_is_not_reissued(tmp_db, kind):
    # Keep this tool visible in both kinds; the adapter replaces it with
    # web_search_options={}, whose empty value still enables native search.
    with (
        _session(kind, ["separate"], native=True) as (session, ui, requests),
        patch.object(
            session,
            "_get_active_tools",
            return_value=[
                {
                    "type": "function",
                    "function": {"name": "web_search", "parameters": {}},
                }
            ],
        ),
        pytest.raises(RuntimeError, match="server-side tools"),
    ):
        session.send("Continue the requested work.")
    assert len(requests) == 1
    assert requests[0]["web_search_options"] == {}
    assert ui.of("state")[-1] == "error"
    assert len(ui.of("status")) == 1


@pytest.mark.parametrize("kind", _KINDS)
def test_deferred_client_tools_do_not_disable_empty_recovery(tmp_db, kind):
    with _session(kind, ["separate", "answer"]) as (session, ui, requests):
        replace_session_lane(
            session,
            capabilities=replace(session._get_capabilities(), supports_tool_search=True),
        )
        with (
            patch.object(
                session,
                "_get_active_tools",
                return_value=[
                    {"type": "function", "function": {"name": "lookup", "parameters": {}}}
                ],
            ),
            patch.object(session, "_get_deferred_names", return_value=frozenset({"lookup"})),
        ):
            session.send("Continue the requested work.")
    assert len(requests) == 2
    assert requests[0] == requests[1]
    assert requests[0]["tools"][0]["defer_loading"] is True
    assert session.messages[-1].text == "The work is complete."
    assert ui.kinds().count("stream_end") == 2


def test_empty_recovery_surfaces_recreate_connection_failure(tmp_db):
    with _session(WorkstreamKind.INTERACTIVE, ["separate"]) as (session, ui, requests):
        session._MAX_RETRIES = 0
        provider = session._primary_lane().provider
        create = provider.create_streaming
        error = APIConnectionError(
            request=httpx2.Request("POST", "https://provider.example.com/v1/chat/completions")
        )

        def recreate(**kwargs):
            if requests:
                raise error
            return create(**kwargs)

        with (
            patch.object(provider, "create_streaming", side_effect=recreate) as attempts,
            pytest.raises(APIConnectionError) as caught,
        ):
            session.send("Continue the requested work.")
    assert caught.value is error
    assert attempts.call_count == 2
    assert len(requests) == 1
    assert ui.kinds().count("stream_end") == 1
    assert ui.kinds().count("stream_discarded") == 1
    assert not any("no answer" in message for message in ui.of("error"))


def test_unknown_request_posture_is_not_assumed_safe(tmp_db):
    with _session(WorkstreamKind.INTERACTIVE, ["separate"]) as (session, ui, requests):
        provider = session._primary_lane().provider
        original = provider.create_streaming

        def without_metrics(**kwargs):
            kwargs.pop("request_metrics_ref", None)
            return original(**kwargs)

        with (
            patch.object(provider, "create_streaming", side_effect=without_metrics),
            pytest.raises(RuntimeError, match="server-side tools"),
        ):
            session.send("Continue the requested work.")
    assert len(requests) == 1
    assert ui.of("state")[-1] == "error"


@pytest.mark.parametrize(
    "shapes",
    [
        ["death", "separate", "separate"],
        ["separate", "death", "separate"],
    ],
)
def test_stream_death_and_empty_completion_share_one_budget(tmp_db, shapes):
    with (
        _session(WorkstreamKind.INTERACTIVE, shapes) as (session, ui, requests),
        pytest.raises(RuntimeError, match="no answer"),
    ):
        session.send("Continue the requested work.")
    assert len(requests) == 3
    assert ui.of("state")[-1] == "error"
    assert len(ui.of("status")) == 2


@pytest.mark.parametrize("kind", _KINDS)
def test_empty_recovery_does_not_repeat_committed_tools(tmp_db, kind):
    with (
        _session(kind, ["tool", "separate", "answer"]) as (session, ui, requests),
        patch.object(
            session,
            "_execute_tools",
            return_value=(
                [("call-once", "lookup receipt")],
                "",
            ),
        ) as execute,
    ):
        session.send("Look it up and report back.")
    execute.assert_called_once()
    assert len(requests) == 3
    assert requests[1] == requests[2]
    assert sum(turn.role == "tool" for turn in session.messages) == 1
    assert session.messages[-1].text == "The work is complete."


def test_refusal_survives_the_main_loop(tmp_db):
    with _session(WorkstreamKind.INTERACTIVE, ["refusal"]) as (session, ui, requests):
        session.send("Continue the requested work.")
    assert len(requests) == 1
    assert session.messages[-1].text == "[Refused: Cannot help.]"
    assert ui.of("error")
    assert "stream_discarded" not in ui.kinds()


@pytest.mark.parametrize("kind", _KINDS)
@pytest.mark.parametrize("stage", ["rejection", "usage", "usage_cancelled"])
def test_stop_during_empty_rejection_finalizes_display_and_preserves_marker(tmp_db, stage, kind):
    with _session(kind, ["separate"]) as (session, ui, requests):
        target = session if stage == "rejection" else ui
        method = "_remember_serving_failure_context" if stage == "rejection" else "on_status"
        original = getattr(target, method)

        def stop_at_rejection(*args):
            original(*args)
            session.cancel()
            if stage == "usage_cancelled":
                raise GenerationCancelled()

        with patch.object(target, method, stop_at_rejection):
            session.send("Continue the requested work.")
    assert len(requests) == 1
    assert session.messages[-1].text == "[generation cancelled before completion]"
    assert ui.kinds().count("stream_end") == 1
    assert len(ui.of("status")) == 1
    assert ui.of("status")[0]["total_tokens"] == 24081
    assert not ui.of("error")


@pytest.mark.parametrize("kind", _KINDS)
def test_stop_during_armed_transport_death_records_one_marker(tmp_db, kind):
    with _session(kind, ["death"]) as (session, ui, requests):
        remember = session._remember_serving_failure_context

        def stop_at_death(error, lane):
            remember(error, lane)
            session.cancel()

        with patch.object(session, "_remember_serving_failure_context", stop_at_death):
            session.send("Continue the requested work.")
    assert len(requests) == 1
    assistant = [turn.text for turn in session.messages if turn.role == "assistant"]
    assert assistant == ["[generation cancelled before completion]"]
    assert ui.kinds().count("stream_end") == 1
    assert not ui.of("error")


@pytest.mark.parametrize("error_type", [RuntimeError, KeyboardInterrupt])
@pytest.mark.parametrize("stop", [False, True])
def test_rejected_usage_callback_failure_finalizes_then_reraises(tmp_db, error_type, stop):
    with _session(WorkstreamKind.INTERACTIVE, ["separate"]) as (session, ui, requests):
        error = error_type("status callback failed")

        def fail_status(*args):
            assert session._generation_lock._is_owned()
            if stop:
                session.cancel()
            raise error

        with patch.object(ui, "on_status", fail_status), pytest.raises(error_type) as caught:
            session.send("Continue the requested work.")
    assert caught.value is error
    assert len(requests) == 1
    assert ui.kinds().count("stream_end") == 1
    assert ui.kinds().count("stream_discarded") == 1
    assert not [turn for turn in session.messages if turn.role == "assistant"]
    assert session._cancelled_partial_msg is None


@pytest.mark.parametrize("invalidate", ["supersede", "close"])
def test_rejected_usage_failure_cannot_finalize_a_successor(tmp_db, invalidate):
    with _session(WorkstreamKind.INTERACTIVE, ["separate"]) as (session, ui, requests):
        session._generation = 1
        error = RuntimeError("status callback failed")
        at_invalidation = []

        def fail_status(*args):
            if invalidate == "supersede":
                session._generation += 1
            else:
                session._publication_shutdown = True
            at_invalidation.extend(ui.events)
            raise error

        with patch.object(ui, "on_status", fail_status), pytest.raises(RuntimeError) as caught:
            session._stream_response(1)
    assert caught.value is error
    assert len(requests) == 1
    assert ui.events == at_invalidation
    assert session._cancelled_partial_msg is None


def test_rejected_usage_preserves_conversation_persistence_failure(tmp_db):
    ui = _CounterUI()
    with _session(WorkstreamKind.INTERACTIVE, ["separate"], ui=ui) as (session, _, requests):
        error = ConversationPersistenceError("unresolved conversation boundary")
        remember = session._remember_serving_failure_context

        def poison_at_rejection(exc, lane):
            remember(exc, lane)
            session._conversation_persistence_error = error

        with (
            patch.object(session, "_remember_serving_failure_context", poison_at_rejection),
            pytest.raises(ConversationPersistenceError) as caught,
        ):
            session.send("Continue the requested work.")
    assert caught.value is error
    assert session._conversation_persistence_error is error
    assert len(requests) == 1
    assert ui.stream_ends == ui.stream_discards == 1
    assert not [turn for turn in session.messages if turn.role == "assistant"]
    assert session._cancelled_partial_msg is None


@pytest.mark.parametrize("kind", _KINDS)
def test_rejected_usage_enforces_budget_before_retry_and_next_send(tmp_db, kind):
    with _session(kind, ["separate"]) as (session, ui, requests):
        session._token_budget = 1000
        with pytest.raises(RuntimeError, match="no answer"):
            session.send("Continue the requested work.")
        assert len(requests) == 1
        assert session._budget_warned
        assert session._budget_exhausted
        assert len(ui.of("status")) == 1
        assert not [turn for turn in session.messages if turn.role == "assistant"]
        with patch.object(ui, "approve_tools", return_value=(False, "")) as approve:
            session.send("Try again.")
        assert len(requests) == 1
        approve.assert_called_once()
        assert approve.call_args.args[0][0]["func_name"] == "__budget_override__"


@pytest.mark.parametrize("budget", [0, 30000])
def test_rejected_usage_preserves_per_completion_budget_semantics(tmp_db, budget):
    with _session(WorkstreamKind.INTERACTIVE, ["separate"] * 3) as (session, ui, requests):
        session._token_budget = budget
        with pytest.raises(RuntimeError, match="no answer"):
            session.send("Continue the requested work.")
        assert len(requests) == 3
        assert len(ui.of("status")) == 3
        assert not session._budget_exhausted
        assert session._budget_warned is (budget > 0)


@pytest.mark.parametrize("invalidate", ["supersede", "close"])
def test_invalidated_empty_attempt_cannot_publish_or_retry(tmp_db, invalidate):
    with _session(WorkstreamKind.INTERACTIVE, ["separate"]) as (session, ui, requests):
        session._generation = 1
        remember = session._remember_serving_failure_context
        at_invalidation = []

        def invalidate_at_rejection(error, lane):
            remember(error, lane)
            if invalidate == "supersede":
                session._generation += 1
            else:
                session._publication_shutdown = True
            at_invalidation.extend(ui.events)

        with (
            patch.object(session, "_remember_serving_failure_context", invalidate_at_rejection),
            pytest.raises((GenerationCancelled, RuntimeError)),
        ):
            session._stream_response(1)
    assert len(requests) == 1
    assert ui.events == at_invalidation
    assert not ui.of("status")
    assert session._cancelled_partial_msg is None


@pytest.mark.parametrize("storage_fails", [False, True])
def test_rejected_usage_reaches_live_counters_and_storage_outside_generation_lock(
    tmp_db, storage_fails
):
    from turnstone.core.storage import get_storage

    ui = _CounterUI()
    storage = get_storage()
    write_usage = storage.record_usage_event
    writes = []
    with _session(WorkstreamKind.INTERACTIVE, ["separate", "answer"], ui=ui) as (
        session,
        _,
        requests,
    ):

        def record_usage(**kwargs):
            assert not session._generation_lock._is_owned()
            writes.append(kwargs)
            if storage_fails:
                raise RuntimeError("usage reporting sink failed")
            write_usage(**kwargs)

        with patch.object(storage, "record_usage_event", side_effect=record_usage):
            session.send("Continue the requested work.")
    assert len(requests) == len(writes) == 2
    assert ui.stream_ends == 2
    assert ui.stream_discards == 1
    assert ui._ws_prompt_tokens == 24038 * 2
    assert ui._ws_completion_tokens == 43 * 2
    assert {row["model"] for row in writes} == {"served-model"}


def test_recovery_after_lane_rebind_reports_the_actual_serving_model(tmp_db):
    with _session(WorkstreamKind.INTERACTIVE, ["separate", "answer"]) as (session, ui, requests):
        original_backoff = session._backoff_or_cancelled

        def rebind_in_backoff(delay, generation):
            replace_session_lane(session, model="replacement-model", alias="replacement")
            original_backoff(delay, generation)

        with patch.object(session, "_backoff_or_cancelled", side_effect=rebind_in_backoff):
            session.send("Continue the requested work.")
    assert [request["model"] for request in requests] == ["served-model", "replacement-model"]
    assert [usage["model"] for usage in ui.of("status")] == ["served-model", "replacement-model"]
    assert session.messages[-1].meta.extra["provenance"]["backend_model_id"] == "replacement-model"


@pytest.mark.parametrize("family", ["openai", "anthropic-compatible"])
@pytest.mark.parametrize("kind", _KINDS)
@pytest.mark.parametrize("recovers", [True, False])
@pytest.mark.parametrize(
    "shape,server_parses", [pair for pair in _EMPTY_SHAPES if pair[0] != "empty"]
)
def test_native_reasoning_only_completion_uses_the_same_recovery(
    tmp_db, family, kind, recovers, shape, server_parses
):
    shapes = [shape, "answer"] if recovers else [shape] * 3
    with _session(kind, shapes, family=family, server_parses=server_parses) as (
        session,
        ui,
        requests,
    ):
        if recovers:
            session.send("Continue the requested work.")
        else:
            with pytest.raises(RuntimeError, match="no answer"):
                session.send("Continue the requested work.")
    assert len(requests) == len(shapes)
    assert len(ui.of("status")) == len(shapes)
    assert ui.of("state")[-1] == ("idle" if recovers else "error")
    assert sum(turn.role == "assistant" for turn in session.messages) == int(recovers)
    if recovers:
        assert session.messages[-1].text == "The work is complete."
        assert requests[0] == requests[1]
