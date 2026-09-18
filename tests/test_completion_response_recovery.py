"""HTTP-200 error responses share product recovery without changing raw callers."""

from __future__ import annotations

import json
import threading
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from typing import Any
from unittest.mock import MagicMock, patch

import httpx2
import pytest
from openai import InternalServerError, OpenAI

from tests._session_helpers import NullUI, make_registered_session, replace_session_lane
from tests.test_empty_completion import _native_wire, _wire
from tests.test_sdk_stream_boundary import _BlockingJsonStream
from turnstone.core.completion_recovery import (
    CompletionRecoveryError,
    ModelTurnLocalError,
    completion_cause,
)
from turnstone.core.deadline import DeadlineCancelledError, StreamAbortRef
from turnstone.core.judge import JudgeConfig
from turnstone.core.model_turn import model_turn
from turnstone.core.providers import IncompleteStreamError
from turnstone.core.providers._openai_chat import OpenAIChatCompletionsProvider
from turnstone.core.providers._openai_common import (
    UpstreamRateLimitError,
    UpstreamResponseError,
    UpstreamTransientError,
)
from turnstone.core.providers._openai_responses import OpenAIResponsesProvider
from turnstone.core.session import ChatSession
from turnstone.core.trajectory import Turn

_ANSWER = "The work is complete."


class _UI(NullUI):
    def __init__(self) -> None:
        super().__init__()
        self.aux_usage: list[dict[str, Any]] = []
        self.task_results: list[tuple[str, bool]] = []

    def on_state_change(self, state: str) -> None:
        pass

    def on_aux_usage(self, usage: dict[str, Any]) -> None:
        self.aux_usage.append(dict(usage))
        super().on_aux_usage(usage)

    def on_tool_result(self, call_id, name, output, *, is_error=False, preview=None):
        if name == "task_agent":
            self.task_results.append((output, is_error))
        super().on_tool_result(call_id, name, output, is_error=is_error, preview=preview)


class _BrokenJsonBody(httpx2.SyncByteStream):
    def __init__(self) -> None:
        self.closed = False

    def __iter__(self):
        yield b'{"error":{"message":"'
        raise httpx2.ReadError("JSON body connection lost")

    def close(self) -> None:
        self.closed = True


class _Replies:
    def __init__(self, surface: str, replies: list[str | httpx2.SyncByteStream]) -> None:
        self.surface = surface
        self.replies = replies
        self.requests: list[dict[str, Any]] = []
        self.responses: list[httpx2.Response] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        expected_path = "/v1/chat/completions" if self.surface == "chat" else "/v1/responses"
        assert request.url.path == expected_path
        self.requests.append(json.loads(request.content))
        index = len(self.requests) - 1
        assert index < len(self.replies), "unexpected additional provider request"
        reply = self.replies[index]
        if isinstance(reply, httpx2.SyncByteStream):
            response = httpx2.Response(
                200,
                headers={"content-type": "application/json"},
                stream=reply,
                request=request,
            )
        elif reply == "connect":
            raise httpx2.ConnectError("connection unavailable", request=request)
        elif reply in {"overloaded", "rate-limit", "invalid", "http503", "overflow"}:
            error_type = {
                "overloaded": "overloaded_error",
                "rate-limit": "rate_limit_error",
                "invalid": "invalid_request_error",
                "http503": "overloaded_error",
                "overflow": "context_length_exceeded",
            }[reply]
            response = httpx2.Response(
                503 if reply == "http503" else 200,
                headers={"content-type": "application/json"},
                json={
                    "error": {
                        "type": error_type,
                        "message": "maximum number of tokens allowed per minute"
                        if reply == "rate-limit"
                        else (
                            "maximum context length exceeded"
                            if reply == "overflow"
                            else "backend cannot serve this request"
                        ),
                    }
                },
                request=request,
            )
        else:
            response = httpx2.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text=_wire(reply) if self.surface == "chat" else _native_wire("openai", reply),
                request=request,
            )
        self.responses.append(response)
        return response

    def assert_finished(self) -> None:
        assert len(self.requests) == len(self.replies)
        # Check while the SDK client is still open, before its teardown can hide
        # a response leaked by the provider's invalid-body or recovery path.
        assert all(response.is_closed for response in self.responses)


@pytest.fixture(autouse=True)
def no_recovery_wait(monkeypatch):
    monkeypatch.setattr("turnstone.core.model_turn._DRAIN_RETRY_BASE_DELAY", 0.0)


@contextmanager
def _session(replies, *, surface="chat", native=False, sdk_retries=0):
    transport = _Replies(surface, replies)
    provider = (
        OpenAIChatCompletionsProvider()
        if surface == "chat"
        else OpenAIResponsesProvider(compat=True)
    )
    with (
        OpenAI(
            api_key="offline-test-only",
            base_url="https://provider.invalid/v1",
            http_client=httpx2.Client(transport=httpx2.MockTransport(transport)),
            max_retries=sdk_retries,
        ) as client,
        ExitStack() as stack,
    ):
        stack.enter_context(patch.object(client, "_calculate_retry_timeout", return_value=0))
        ui = _UI()
        session = make_registered_session(
            client=client,
            ui=ui,
            user_id="response-recovery-user",
            context_window=100_000,
            max_tokens=256,
            compact_max_tokens=256,
            judge_config=JudgeConfig(enabled=False, output_guard=False),
        )
        lane = replace_session_lane(session, provider=provider, client=client, model="served-model")
        session._title_generated = True
        session._RETRY_BASE_DELAY = 0
        session._agent_prompt_components = ()
        session._task_tools = []
        if native is True:
            if surface == "chat":
                lane = replace(lane, extra_params={"web_search_options": {}})
            else:
                # Responses ignores extra_params. Its main envelope advertises
                # web_search, which this capability converts to native search.
                lane = replace(
                    lane, capabilities=replace(lane.capabilities, supports_web_search=True)
                )
            session._model_binding = replace(session._model_binding, lane=lane)
        elif native is None:
            # An older adapter can omit request metrics. Keep the real adapter,
            # SDK, and wire path; omit only its posture report for this case.
            create_streaming = provider.create_streaming

            def without_metrics(**kwargs):
                kwargs["request_metrics_ref"] = None
                return create_streaming(**kwargs)

            stack.enter_context(
                patch.object(provider, "create_streaming", side_effect=without_metrics)
            )
        yield session, ui, transport


@contextmanager
def _fallbacks(session, aliases, *, degraded=()):
    binding = session._model_binding
    primary_alias = session._model_alias
    trackers = {alias: MagicMock() for alias in [primary_alias, *aliases]}
    for alias, tracker in trackers.items():
        tracker.is_degraded = alias in degraded
    registry = MagicMock(fallback=aliases)
    health = MagicMock()
    health.get_tracker_for_alias.side_effect = lambda registry, alias: trackers[alias]

    def resolve(registry, alias, **kwargs):
        lane = replace(
            binding.lane,
            alias=alias,
            model=alias,
            capabilities=replace(binding.lane.capabilities, context_window=200_000),
            extra_params=None,
        )
        return replace(binding, lane=lane)

    with (
        patch.object(session, "_registry", registry),
        patch.object(session, "_health_registry", health),
        patch.object(session, "_get_health_tracker", return_value=trackers[primary_alias]),
        patch.object(session, "_refresh_model_from_registry"),
        patch("turnstone.core.session.resolve_model_binding", side_effect=resolve),
    ):
        yield trackers[primary_alias], trackers


def _invoke(session, caller):
    if caller == "main":
        session.send("Complete the work.")
        return next(turn.text for turn in reversed(session.messages) if turn.role == "assistant")
    if caller == "task":
        _, result = session._exec_task({"call_id": "task-response", "prompt": "Complete the work."})
        return result
    if caller == "summary":
        runtime = session._build_summary_runtime(
            session._primary_lane(), continuation_overhead_tokens=0
        )
        return session._compaction_engine.summarize_batch(
            "Summarize the conversation.", ["Earlier useful work."], 0, runtime
        ).text
    return session._utility_completion([Turn.user("Complete the work.")]).content


@pytest.mark.parametrize("surface", ["chat", "responses"])
@pytest.mark.parametrize("caller,error", [("task", "overloaded"), ("summary", "rate-limit")])
def test_product_callers_recover_json_errors_without_replaying_their_operation(
    tmp_db, surface, caller, error
):
    with _session([error, "answer"], surface=surface, sdk_retries=2) as (session, ui, transport):
        assert _invoke(session, caller) == _ANSWER
        transport.assert_finished()
        assert transport.requests[0] == transport.requests[1]
        assert len(ui.aux_usage) == 1
        assert ui.aux_usage[0]["completion_tokens"] == 43
        if caller == "task":
            assert ui.task_results == [(_ANSWER, False)]


@pytest.mark.parametrize("caller", ["task", "summary"])
@pytest.mark.parametrize(
    "replies", [["overloaded", "empty", "death"], ["empty", "rate-limit", "overloaded"]]
)
def test_json_empty_and_drain_failures_share_one_allowance(tmp_db, caller, replies):
    with _session(replies) as (session, ui, transport):
        if caller == "task":
            result = _invoke(session, caller)
            assert result.startswith("Task error:")
            assert ui.task_results == [(result, True)]
        else:
            with pytest.raises(CompletionRecoveryError):
                _invoke(session, caller)
        transport.assert_finished()
        assert len(transport.requests) == 3
        assert transport.requests[0] == transport.requests[1] == transport.requests[2]
        # Only the completed empty attempt has usage. Error bodies and
        # incomplete streams cannot create a fabricated completed charge.
        assert len(ui.aux_usage) == 1
        assert ui.aux_usage[0]["completion_tokens"] == 43


@pytest.mark.parametrize(
    "caller,surface,native",
    [("utility", "chat", True), ("utility", "responses", None), ("task", "chat", True)],
)
def test_json_rejection_reissues_regardless_of_native_posture(tmp_db, caller, surface, native):
    """A JSON error body arrives before any stream exists, so nothing ran and
    the request's tool posture is no replay hazard: nonstreaming callers spend
    their shared allowance on it like any transient failure."""
    with _session(["overloaded", "answer"], surface=surface, native=native, sdk_retries=2) as (
        session,
        ui,
        transport,
    ):
        assert _invoke(session, caller) == _ANSWER
        transport.assert_finished()
        assert transport.requests[0] == transport.requests[1]
        assert len(ui.aux_usage) == 1
        if native and surface == "chat":
            assert transport.requests[0]["web_search_options"] == {}
        elif native:
            assert {"type": "web_search"} in transport.requests[0]["tools"]


@pytest.mark.parametrize("native", [True, None, False])
def test_main_json_rejection_is_a_creation_failure(tmp_db, native):
    """The streaming owner keeps its creation ladder for a JSON error body at
    any tool posture: the primary lane is retried, its health failure is
    recorded once, and the fallback walk runs before the error surfaces."""
    replies = ["overloaded"] * (ChatSession._MAX_RETRIES + 1)
    with _session(replies, native=native) as (session, ui, transport):
        tracker = MagicMock()
        session._registry = MagicMock(fallback=["spare"])
        with (
            patch.object(session, "_get_health_tracker", return_value=tracker),
            patch.object(session, "_try_fallback_lane", return_value=None) as fallback,
            pytest.raises(UpstreamTransientError),
        ):
            _invoke(session, "main")
        transport.assert_finished()
        assert len(transport.requests) == ChatSession._MAX_RETRIES + 1
        tracker.record_failure.assert_called_once()
        fallback.assert_called_once()
        assert ui.aux_usage == []


def test_main_json_rejection_walks_fallbacks_after_the_ladder(tmp_db):
    replies = [*(["overloaded"] * (ChatSession._MAX_RETRIES + 1)), "answer"]
    with _session(replies) as (session, ui, transport):
        with _fallbacks(session, ["spare"]) as (primary, trackers):
            assert _invoke(session, "main") == _ANSWER
        assert [request["model"] for request in transport.requests] == [
            *(["served-model"] * (ChatSession._MAX_RETRIES + 1)),
            "spare",
        ]
        primary.record_failure.assert_called_once()
        primary.record_success.assert_not_called()
        trackers["spare"].record_success.assert_called_once()
        trackers["spare"].record_failure.assert_not_called()
        transport.assert_finished()


def test_main_json_overflow_rejection_compacts(tmp_db):
    """A context rejection delivered as an HTTP-200 JSON body reaches the send
    loop's compact-and-retry arm like any other request-time overflow."""
    with _session(["overflow", "answer"]) as (session, ui, transport):
        with patch.object(session, "_compact_messages") as compact:
            assert _invoke(session, "main") == _ANSWER
        compact.assert_called_once()
        transport.assert_finished()
        assert len(transport.requests) == 2


def test_main_json_rejection_health_fault_is_local(tmp_db):
    replies = ["overloaded"] * (ChatSession._MAX_RETRIES + 1)
    with _session(replies) as (session, ui, transport):
        with _fallbacks(session, ["spare"]) as (primary, trackers):
            primary.record_failure.side_effect = ValueError("private health state")
            with pytest.raises(ModelTurnLocalError):
                _invoke(session, "main")
        trackers["spare"].record_failure.assert_not_called()
        trackers["spare"].record_success.assert_not_called()
        transport.assert_finished()


@pytest.mark.parametrize("surface", ["chat", "responses"])
@pytest.mark.parametrize(
    "reply,error_type",
    [("overloaded", UpstreamTransientError), ("rate-limit", UpstreamRateLimitError)],
)
def test_raw_call_keeps_original_json_error_and_single_request(tmp_db, surface, reply, error_type):
    with _session([reply], surface=surface, sdk_retries=2) as (session, _ui, transport):
        with pytest.raises(error_type) as failure:
            model_turn(session._primary_lane(), [Turn.user("Continue")], product_recovery=False)
        assert not isinstance(failure.value, CompletionRecoveryError)
        assert failure.value.status_code == 200
        transport.assert_finished()


def test_unclassified_json_error_remains_terminal(tmp_db):
    with _session(["invalid"]) as (session, ui, transport):
        with pytest.raises(CompletionRecoveryError) as failure:
            _invoke(session, "utility")
        assert type(completion_cause(failure.value)) is UpstreamResponseError
        transport.assert_finished()
        assert ui.aux_usage == []


def test_http503_retains_sdk_retry_ownership(tmp_db):
    with _session(["http503"] * 3, sdk_retries=2) as (session, ui, transport):
        with pytest.raises(InternalServerError) as failure:
            _invoke(session, "utility")
        assert failure.value.status_code == 503
        transport.assert_finished()
        assert len(transport.requests) == 3
        assert ui.aux_usage == []


@pytest.mark.parametrize("surface", ["chat", "responses"])
def test_json_body_read_failure_recovers_and_closes_the_response(tmp_db, surface):
    body = _BrokenJsonBody()
    with _session([body, "answer"], surface=surface, sdk_retries=2) as (session, ui, transport):
        assert _invoke(session, "utility") == _ANSWER
        transport.assert_finished()
        assert body.closed
        assert len(ui.aux_usage) == 1


def test_raw_json_body_read_failure_is_not_retried(tmp_db):
    body = _BrokenJsonBody()
    with _session([body], sdk_retries=2) as (session, _ui, transport):
        with pytest.raises(IncompleteStreamError) as failure:
            model_turn(session._primary_lane(), [Turn.user("Continue")], product_recovery=False)
        assert isinstance(failure.value.__cause__, httpx2.ReadError)
        transport.assert_finished()
        assert body.closed


def test_cancelling_json_body_read_closes_response_without_reissue(tmp_db):
    body = _BlockingJsonStream()
    cancel_ref = StreamAbortRef()
    errors: list[BaseException] = []
    with _session([body]) as (session, ui, transport):

        def invoke():
            try:
                session._utility_completion([Turn.user("Continue")], cancel_ref=cancel_ref)
            except BaseException as error:
                errors.append(error)

        worker = threading.Thread(target=invoke)
        worker.start()
        try:
            assert body.read_started.wait(2)
            cancel_ref.abort()
            worker.join(2)
            assert not worker.is_alive()
            assert len(errors) == 1
            assert isinstance(errors[0], DeadlineCancelledError)
            assert body.closed.is_set()
            transport.assert_finished()
            assert ui.aux_usage == []
        finally:
            cancel_ref.abort()
            worker.join(2)


def test_main_fallback_walk_dials_healthy_lanes_before_degraded_ones(tmp_db):
    ladder = ChatSession._MAX_RETRIES + 1
    replies = [*(["overloaded"] * ladder), *(["connect"] * ladder), "answer"]
    with _session(replies) as (session, ui, transport):
        notices: list[str] = []
        with (
            _fallbacks(session, ["degraded", "healthy"], degraded={"degraded"}),
            patch.object(ui, "on_info", side_effect=notices.append),
        ):
            assert _invoke(session, "main") == _ANSWER
        assert [request["model"] for request in transport.requests] == [
            *(["served-model"] * ladder),
            *(["healthy"] * ladder),
            "degraded",
        ]
        healthy_notice = notices.index("[Primary model failed, falling back to healthy]")
        degraded_notice = notices.index("[Fallback degraded is degraded, trying anyway]")
        assert healthy_notice < degraded_notice
        transport.assert_finished()
