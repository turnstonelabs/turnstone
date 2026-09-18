"""Product callers share recovery without replaying their enclosing operations."""

import json
import sqlite3
import threading
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx2
import pytest
from openai import APIConnectionError

from tests.test_empty_completion import _REASONING, _session, _wire
from turnstone.core.admission import ModelAdmission
from turnstone.core.attachments import Attachment
from turnstone.core.completion_recovery import (
    CompletionRecoveryError,
    EmptyCompletionError,
    ModelTurnLocalError,
    is_context_overflow,
)
from turnstone.core.deadline import (
    DeadlineCancelledError,
    StreamAbortRef,
    run_abortable_with_deadline,
)
from turnstone.core.judge import IntentJudge, JudgeConfig
from turnstone.core.model_turn import ModelContextLimitError, ResolvedModelBinding, model_turn
from turnstone.core.output_guard_judge import OutputGuardJudge
from turnstone.core.ratelimit import TokenBucket
from turnstone.core.trajectory import AttachmentRef, Role, Turn
from turnstone.core.workstream import WorkstreamKind


@pytest.fixture(autouse=True)
def no_recovery_wait(monkeypatch):
    monkeypatch.setattr("turnstone.core.model_turn._DRAIN_RETRY_BASE_DELAY", 0.0)


@pytest.mark.parametrize("cancel_during_sleep", [False, True])
def test_product_backoff_uses_elapsed_time_and_rechecks_cancellation(
    tmp_db, monkeypatch, cancel_during_sleep
):
    ref = StreamAbortRef()
    now = [0.0]
    sleeps = []

    def oversleep(delay):
        sleeps.append(delay)
        now[0] += 1.25
        if cancel_during_sleep:
            ref.abort()

    monkeypatch.setattr("turnstone.core.model_turn._DRAIN_RETRY_BASE_DELAY", 1.0)
    monkeypatch.setattr(
        "turnstone.core.model_turn.time",
        SimpleNamespace(monotonic=lambda: now[0], sleep=oversleep),
    )
    monkeypatch.setattr("turnstone.core.model_turn.random", SimpleNamespace(random=lambda: 0.5))
    replies = ["empty"] if cancel_during_sleep else ["empty", "answer"]
    with _session(WorkstreamKind.INTERACTIVE, replies) as (session, ui, requests):
        if cancel_during_sleep:
            with pytest.raises(DeadlineCancelledError):
                model_turn(
                    session._primary_lane(),
                    [Turn.user("Continue")],
                    product_recovery=True,
                    cancel_ref=ref,
                )
        else:
            result = model_turn(
                session._primary_lane(),
                [Turn.user("Continue")],
                product_recovery=True,
                cancel_ref=ref,
            )
            assert result.content == "The work is complete."
        assert len(requests) == len(replies)
        assert sleeps == [0.05]


def _call(session, caller):
    turns = [Turn.system("Finish the delegated work."), Turn.user("Continue.")]
    if caller == "utility":
        return session._utility_completion(turns).content
    session.agent_max_turns = 0 if caller == "final" else 2
    return session._run_agent(
        turns,
        label="task",
        tools=[],
    )


@pytest.mark.parametrize("caller", ["utility", "task", "final"])
@pytest.mark.parametrize("family", ["openai-compatible", "openai", "anthropic-compatible"])
def test_product_recovery_accounts_rejected_and_accepted_sdk_attempts(tmp_db, caller, family):
    with _session(WorkstreamKind.INTERACTIVE, ["separate", "answer"], family=family) as (
        session,
        ui,
        requests,
    ):
        with patch.object(session, "_record_aux_usage") as usage:
            assert _call(session, caller) == "The work is complete."
        assert len(requests) == 2
        assert requests[0] == requests[1]
        assert usage.call_count == 2
        assert [call.args[0].completion_tokens for call in usage.call_args_list] == [43, 43]
        assert all(call.kwargs["model"] == "served-model" for call in usage.call_args_list)
        assert _REASONING not in str(session.messages)
        assert session._last_usage is None


@pytest.mark.parametrize("caller", ["utility", "task", "final"])
@pytest.mark.parametrize(
    "shapes",
    [["empty"] * 3, ["death"] * 3, ["empty", "death", "empty"], ["death", "empty", "death"]],
)
def test_product_failures_share_one_allowance(tmp_db, caller, shapes):
    with _session(WorkstreamKind.INTERACTIVE, shapes) as (session, ui, requests):
        with patch.object(session, "_record_aux_usage") as usage, pytest.raises(RuntimeError):
            _call(session, caller)
        assert len(requests) == 3
        assert usage.call_count == shapes.count("empty")
        assert not any("done]" in str(event) for event in ui.events)


@pytest.mark.parametrize("shape", ["empty", "death"])
def test_native_request_extras_prohibit_product_replay(tmp_db, shape):
    with _session(WorkstreamKind.INTERACTIVE, [shape]) as (session, ui, requests):
        lane = replace(session._primary_lane(), extra_params={"web_search_options": {}})
        with pytest.raises(CompletionRecoveryError) as failure:
            model_turn(lane, [Turn.user("Continue")], tools=[], product_recovery=True)
        assert failure.value.native_tools_enabled is True
        assert requests[0]["web_search_options"] == {}
        assert len(requests) == 1


def test_observer_fault_is_local_even_when_named_like_a_transport_error(tmp_db):
    fault = APIConnectionError(request=httpx2.Request("POST", "https://example.com/v1"))
    with _session(WorkstreamKind.INTERACTIVE, ["empty"]) as (session, ui, requests):
        with (
            patch.object(session, "_record_aux_usage", side_effect=fault) as usage,
            pytest.raises(ModelTurnLocalError) as failure,
        ):
            _call(session, "task")
        assert failure.value.__cause__ is fault
        assert usage.call_count == 1
        assert len(requests) == 1


def test_completed_billing_precedes_stop_and_no_reissue_follows(tmp_db):
    ref = StreamAbortRef()
    completed = []

    def observe(usage):
        completed.append(usage)
        ref.abort()

    with _session(WorkstreamKind.INTERACTIVE, ["empty"]) as (session, ui, requests):
        with pytest.raises(DeadlineCancelledError):
            model_turn(
                session._primary_lane(),
                [Turn.user("Continue")],
                product_recovery=True,
                cancel_ref=ref,
                on_completed=observe,
            )
        assert len(completed) == len(requests) == 1
        assert completed[0].completion_tokens == 43


def test_local_chunk_failure_cannot_become_a_transport_retry(tmp_db):
    fault = APIConnectionError(request=httpx2.Request("POST", "https://example.com/v1"))

    def consume(_chunk):
        raise fault

    with _session(WorkstreamKind.INTERACTIVE, ["answer"]) as (session, ui, requests):
        with pytest.raises(ModelTurnLocalError) as failure:
            model_turn(
                session._primary_lane(),
                [Turn.user("Continue")],
                product_recovery=True,
                on_chunk=consume,
            )
        assert failure.value.__cause__ is fault
        assert len(requests) == 1


@pytest.mark.parametrize("boundary", ["on_chunk", "validate_wire", "admit_reissue"])
def test_local_boundaries_preserve_an_existing_typed_fault(tmp_db, boundary):
    fault = ModelTurnLocalError(KeyError(_REASONING), stage="completion ingestion")

    def fail(*_args):
        raise fault

    shape = "empty" if boundary == "admit_reissue" else "answer"
    with _session(WorkstreamKind.INTERACTIVE, [shape]) as (session, ui, requests):
        with pytest.raises(ModelTurnLocalError) as failure:
            model_turn(
                session._primary_lane(),
                [Turn.user("Continue")],
                product_recovery=True,
                **{boundary: fail},
            )
        assert failure.value is fault
        assert "completion ingestion failed (KeyError)" in str(fault)
        assert _REASONING not in str(fault)
        assert len(requests) == (0 if boundary == "validate_wire" else 1)


@pytest.mark.parametrize("fault_kind", ["storage", "transport"])
def test_attachment_faults_do_not_retry_or_degrade_the_model_backend(tmp_db, fault_kind):
    fault = (
        sqlite3.OperationalError("database is locked")
        if fault_kind == "storage"
        else APIConnectionError(request=httpx2.Request("GET", "https://storage.example.com/blobs"))
    )
    with _session(WorkstreamKind.INTERACTIVE, []) as (session, ui, requests):
        tracker = MagicMock()
        session._registry = MagicMock(fallback=["spare"])
        with (
            patch("turnstone.core.session.get_attachments", side_effect=fault) as read,
            patch.object(session, "_get_health_tracker", return_value=tracker),
            patch.object(session, "_try_fallback_lane", return_value=None) as fallback,
            pytest.raises(ModelTurnLocalError) as failure,
        ):
            session.send(
                "Read image",
                attachments=[
                    Attachment(
                        attachment_id="image",
                        kind="image",
                        mime_type="image/png",
                        filename="image.png",
                        content=b"image bytes",
                    )
                ],
            )
        assert failure.value.__cause__ is fault
        assert failure.value.stage == "attachment materialization"
        assert read.call_count == 1
        assert requests == []
        tracker.record_failure.assert_not_called()
        fallback.assert_not_called()

        # Direct raw/eval callers retain their original exception contract.
        def resolve(_ids):
            raise fault

        with pytest.raises(type(fault)) as raw_failure:
            model_turn(
                session._primary_lane(),
                [
                    Turn(
                        role=Role.USER,
                        content=(AttachmentRef(attachment_id="image", kind="image"),),
                    )
                ],
                resolve_attachments=resolve,
            )
        assert raw_failure.value is fault
        assert requests == []


def test_stream_admission_fault_closes_native_response_without_replay(tmp_db):
    fault = RuntimeError("maximum context length exceeded in local health observer")
    with _session(WorkstreamKind.INTERACTIVE, ["answer"], native=True) as (session, ui, requests):
        tracker = MagicMock()
        tracker.record_success.side_effect = fault
        completions = session._primary_lane().client.chat.completions
        create = completions.create
        handles = []

        def capture(**kwargs):
            stream = create(**kwargs)
            stream.close = MagicMock(wraps=stream.close)
            handles.append(stream)
            return stream

        with (
            patch.object(completions, "create", side_effect=capture),
            patch.object(session, "_get_health_tracker", return_value=tracker),
            patch.object(session, "_compact_messages") as compact,
            pytest.raises(ModelTurnLocalError) as failure,
        ):
            session.send("Continue")
        assert failure.value.__cause__ is fault
        assert failure.value.stage == "stream admission"
        assert len(requests) == len(handles) == 1
        assert "web_search_options" in requests[0]
        handles[0].close.assert_called_once()
        compact.assert_not_called()
        tracker.record_failure.assert_not_called()


@pytest.mark.parametrize(
    "fault", [DeadlineCancelledError("stopped"), ModelContextLimitError("full")]
)
def test_attachment_control_flow_keeps_its_domain_type(tmp_db, fault):
    def resolve(_ids):
        raise fault

    with _session(WorkstreamKind.INTERACTIVE, []) as (session, ui, requests):
        with pytest.raises(type(fault)) as failure:
            model_turn(
                session._primary_lane(),
                [
                    Turn(
                        role=Role.USER,
                        content=(AttachmentRef(attachment_id="image", kind="image"),),
                    )
                ],
                resolve_attachments=resolve,
                product_recovery=True,
            )
        assert failure.value is fault
        assert requests == []


@pytest.mark.parametrize("native", [True, None, False])
def test_accepted_overflow_classifies_at_any_tool_posture(native):
    # Compaction shrinks the next request rather than replaying the failed one,
    # so the wrapper's replay hazard never hides an overflow from the shrink sites.
    failure = CompletionRecoveryError(
        "Stream failed", native_tools_enabled=native, error=RuntimeError("context window exceeded")
    )
    assert is_context_overflow(failure)
    assert not is_context_overflow(ModelTurnLocalError(RuntimeError("context window exceeded")))


def test_raw_default_still_returns_empty_for_excluded_callers(tmp_db):
    with _session(WorkstreamKind.INTERACTIVE, ["separate"]) as (session, ui, requests):
        result = model_turn(session._primary_lane(), [Turn.user("Continue")])
        assert result.content == ""
        assert len(requests) == 1


def test_rejected_diagnostics_keep_counts_without_reasoning_payload(tmp_db, caplog):
    with _session(WorkstreamKind.INTERACTIVE, ["separate"] * 3) as (session, ui, requests):
        with pytest.raises(EmptyCompletionError) as failure:
            _call(session, "utility")
        assert failure.value.result.reasoning_chars == len(_REASONING)
        assert "model_turn.recovery_stopped" in caplog.text
        assert _REASONING not in caplog.text


_VERDICT = json.dumps(
    {"risk_level": "low", "recommendation": "approve", "flags": [], "confidence": 0.95}
)


def _verdict_wire(shape, **kwargs):
    return _wire(shape, **kwargs).replace(json.dumps("The work is complete."), json.dumps(_VERDICT))


def _judge(session, kind):
    lane = session._primary_lane()
    binding = ResolvedModelBinding(lane=lane, config=None, registry_generation=0)
    config = JudgeConfig(read_only_tools=False, output_guard_llm=True)
    if kind == "intent":
        judge = IntentJudge(config, binding)

        def call():
            return judge._evaluate_single(
                {"func_name": "read_file", "func_args": {"path": "README.md"}, "call_id": "read"},
                [{"role": "user", "content": "Read the README."}],
                None,
                lane.client,
            )

    else:
        judge = OutputGuardJudge(config, binding)
        judge._create_client = lambda: lane.client

        def call():
            return judge.evaluate("readme text", func_name="read_file", call_id="read")

    return judge, call


@pytest.mark.parametrize("kind", ["intent", "output"])
@pytest.mark.parametrize("shapes", [["empty", "answer"], ["empty", "death", "empty"]])
def test_judges_use_shared_recovery_without_prompt_nudges(tmp_db, kind, shapes):
    with (
        patch("tests.test_empty_completion._wire", side_effect=_verdict_wire),
        _session(WorkstreamKind.INTERACTIVE, shapes) as (session, ui, requests),
    ):
        _judge_instance, evaluate = _judge(session, kind)
        verdict = evaluate()
        assert len(requests) == len(shapes)
        assert all(request == requests[0] for request in requests)
        if shapes[-1] == "answer":
            assert verdict is not None
            assert verdict.risk_level == "low"
        elif kind == "intent":
            assert verdict is None
        else:
            assert not verdict.succeeded
            assert verdict.error == "empty_response"


@pytest.mark.parametrize("tokens", [1, 3])
def test_output_judge_charges_captured_limiter_for_each_dispatch(tmp_db, tokens):
    with (
        patch("tests.test_empty_completion._wire", side_effect=_verdict_wire),
        _session(WorkstreamKind.INTERACTIVE, ["empty", "empty", "answer"]) as (
            session,
            ui,
            requests,
        ),
    ):
        judge, _evaluate = _judge(session, "output")
        session._output_guard_judge = judge
        session._output_guard_judge_cancel = threading.Event()
        limiter = session._output_guard_judge_rl = TokenBucket(rate=0, burst=tokens)
        with patch.object(session, "_ensure_output_guard_judge", return_value=judge):
            verdict = session._invoke_output_guard_judge("read", "readme text", "read_file")
        assert len(requests) == tokens
        assert limiter.tokens == 0
        assert verdict is not None
        assert verdict.succeeded is (tokens == 3)
        if tokens == 1:
            assert verdict.error == "admission_denied"


def test_output_judge_reports_the_provider_cause_after_recovery_exhaustion(tmp_db):
    with _session(WorkstreamKind.INTERACTIVE, ["death"] * 3) as (session, ui, requests):
        _judge_instance, evaluate = _judge(session, "output")
        verdict = evaluate()
        assert len(requests) == 3
        assert verdict.error == "provider_error: IncompleteStreamError"


@pytest.mark.parametrize("retirement", ["generation", "judge", "cancel_event"])
def test_output_judge_retirement_blocks_reissue_without_spending_quota(tmp_db, retirement):
    from turnstone.core.model_turn import _ingest_completion

    with _session(WorkstreamKind.INTERACTIVE, ["empty"]) as (session, ui, requests):
        judge, _evaluate = _judge(session, "output")
        session._output_guard_judge = judge
        session._output_guard_judge_cancel = threading.Event()
        session._generation = 1
        limiter = session._output_guard_judge_rl = TokenBucket(rate=0, burst=3)

        def finish(*args, **kwargs):
            result = _ingest_completion(*args, **kwargs)
            with session._output_guard_judge_lock:
                if retirement == "generation":
                    session._generation = 2
                elif retirement == "judge":
                    session._output_guard_judge, _unused = _judge(session, "output")
                else:
                    session._output_guard_judge_cancel.set()
            return result

        with (
            patch.object(session, "_ensure_output_guard_judge", return_value=judge),
            patch("turnstone.core.model_turn._ingest_completion", side_effect=finish),
        ):
            verdict = session._invoke_output_guard_judge(
                "read", "readme text", "read_file", my_generation=1
            )
        assert verdict.error == "cancelled"
        assert len(requests) == 1
        assert limiter.tokens == 2


@pytest.mark.parametrize("kind", ["intent", "output"])
def test_judge_recovery_stays_inside_original_deadline(tmp_db, monkeypatch, kind):
    monkeypatch.setattr("turnstone.core.model_turn._DRAIN_RETRY_BASE_DELAY", 1.0)
    finished = threading.Event()
    refs = []

    def bounded(call, **kwargs):
        def tracked(ref):
            refs.append(ref)
            try:
                return call(ref)
            finally:
                finished.set()

        return run_abortable_with_deadline(tracked, **{**kwargs, "timeout": 0.1})

    module = "judge" if kind == "intent" else "output_guard_judge"
    with (
        _session(WorkstreamKind.INTERACTIVE, ["empty"]) as (session, ui, requests),
        patch(f"turnstone.core.{module}.run_abortable_with_deadline", side_effect=bounded) as run,
    ):
        _judge_instance, evaluate = _judge(session, kind)
        verdict = evaluate()
        assert finished.wait(2)
        assert len(requests) == len(refs) == run.call_count == 1
        assert refs[0].aborted
        if kind == "intent":
            assert verdict is None
        else:
            assert verdict.error == "timeout"


@pytest.mark.parametrize("site", ["finish", "rejection_usage", "retry_notice"])
def test_main_local_publication_fault_cannot_trigger_compaction(tmp_db, site):
    fault = RuntimeError("maximum context length exceeded in local callback")
    target = {
        "finish": "_finalize_stream_result",
        "rejection_usage": "_update_token_budget",
        "retry_notice": "on_info",
    }[site]
    shapes = ["answer" if site == "finish" else "empty"]
    with _session(WorkstreamKind.INTERACTIVE, shapes) as (session, ui, requests):
        owner = ui if site == "retry_notice" else session
        with (
            patch.object(owner, target, side_effect=fault),
            patch.object(session, "_compact_messages") as compact,
            pytest.raises(ModelTurnLocalError),
        ):
            session.send("Continue.")
        assert len(requests) == 1
        compact.assert_not_called()


def test_reissue_cannot_inherit_previous_requests_safe_posture(tmp_db):
    with _session(WorkstreamKind.INTERACTIVE, ["empty", "empty"]) as (session, ui, requests):
        lane = session._primary_lane()
        create = lane.provider.create_streaming

        def lose_second_metric(**kwargs):
            chunks = create(**kwargs)
            if len(requests) == 2:
                kwargs["request_metrics_ref"].clear()
            return chunks

        with (
            patch.object(lane.provider, "create_streaming", side_effect=lose_second_metric),
            pytest.raises(EmptyCompletionError) as failure,
        ):
            model_turn(lane, [Turn.user("Continue")], product_recovery=True)
        assert len(requests) == 2
        assert failure.value.native_tools_enabled is None


@pytest.mark.parametrize("caller", ["utility", "task", "final"])
def test_creation_failure_after_empty_cannot_restart_outer_recovery(tmp_db, caller):
    fault = APIConnectionError(request=httpx2.Request("POST", "https://example.com/v1"))
    with _session(WorkstreamKind.INTERACTIVE, ["empty"]) as (session, ui, requests):
        lane = session._primary_lane()
        create = lane.provider.create_streaming

        def fail_reissue(**kwargs):
            if requests:
                raise fault
            return create(**kwargs)

        with (
            patch.object(lane.provider, "create_streaming", side_effect=fail_reissue) as dispatch,
            pytest.raises(APIConnectionError) as failure,
        ):
            _call(session, caller)
        assert failure.value is fault
        assert dispatch.call_count == 2
        assert len(requests) == 1


@pytest.mark.parametrize("finish", ["length", "content_filter"])
@pytest.mark.parametrize("shape", ["answer", "empty"])
def test_final_synthesis_preserves_nonblank_output_and_guards_once(tmp_db, finish, shape):
    with _session(WorkstreamKind.INTERACTIVE, [shape], finish=finish) as (session, ui, requests):
        with patch.object(
            session, "_guard_subagent_synthesis", side_effect=lambda text, *a, **k: text
        ) as guard:
            output = _call(session, "final")
        assert len(requests) == guard.call_count == 1
        if shape == "answer":
            assert output == "The work is complete."
        else:
            assert output == {"length": "(truncated)", "content_filter": "(content filter)"}[finish]


@pytest.mark.parametrize("transport_shaped", [False, True])
def test_main_local_admission_fault_cannot_fall_back_or_compact(tmp_db, transport_shaped):
    fault = (
        APIConnectionError(request=httpx2.Request("POST", "https://example.com/v1"))
        if transport_shaped
        else RuntimeError("maximum context length exceeded in local callback")
    )
    with _session(WorkstreamKind.INTERACTIVE, []) as (session, ui, requests):
        with (
            patch.object(session, "_admit_memory_index_request", side_effect=fault),
            patch.object(session, "_try_fallback_lane") as fallback,
            patch.object(session, "_compact_messages") as compact,
            pytest.raises(ModelTurnLocalError) as failure,
        ):
            session.send("Continue.")
        assert failure.value.__cause__ is fault
        assert not requests
        fallback.assert_not_called()
        compact.assert_not_called()


@pytest.mark.parametrize("product_recovery", [False, True])
def test_ingestion_fault_is_local_after_billing_and_capacity_release(tmp_db, product_recovery):
    fault = RuntimeError("maximum context length exceeded in local id callback")
    seen = []
    admission = ModelAdmission("observed", limit=1)

    def observe(usage):
        assert admission.snapshot().in_flight == 0
        seen.append(usage)

    def mint(_original):
        assert len(seen) == 1
        raise fault

    with _session(WorkstreamKind.INTERACTIVE, ["tool"]) as (session, ui, requests):
        with pytest.raises(RuntimeError) as failure:
            model_turn(
                replace(session._primary_lane(), admission=admission),
                [Turn.user("Continue")],
                product_recovery=product_recovery,
                mint=mint,
                wire_id_map={},
                on_completed=observe,
            )
        assert len(requests) == len(seen) == 1
        assert seen[0].completion_tokens == 43
        if product_recovery:
            assert isinstance(failure.value, ModelTurnLocalError)
            assert failure.value.__cause__ is fault
            assert not is_context_overflow(failure.value)
        else:
            assert failure.value is fault


def test_main_ingestion_fault_cannot_reopen_native_tool_replay(tmp_db):
    fault = RuntimeError("maximum context length exceeded in local finalization")
    with _session(WorkstreamKind.INTERACTIVE, ["answer"], native=True) as (session, ui, requests):
        with (
            patch("turnstone.core.model_turn.finalize_provider_blocks", side_effect=fault),
            patch.object(session, "_compact_messages") as compact,
            pytest.raises(ModelTurnLocalError) as failure,
        ):
            session.send("Continue.")
        assert failure.value.__cause__ is fault
        assert len(requests) == 1
        assert "web_search_options" in requests[0]
        compact.assert_not_called()


@pytest.mark.parametrize("shape", ["empty", "death"])
def test_main_budget_stop_blocks_both_replay_shapes(tmp_db, shape):
    with _session(WorkstreamKind.INTERACTIVE, [shape]) as (session, ui, requests):
        session._budget_exhausted = True
        with pytest.raises(CompletionRecoveryError):
            session._stream_response()
        assert len(requests) == 1


def test_empty_summary_exhaustion_bails_as_empty_summary_not_error(tmp_db):
    # A summary model that returns blank stops exhausts the shared allowance;
    # the lifecycle owner then reports the documented ``empty_summary`` bail
    # (informational, history kept), never a generic compaction error.
    with _session(WorkstreamKind.INTERACTIVE, ["empty"] * 3) as (session, ui, requests):
        session.compact_max_tokens = 100
        session._system_tokens = 0
        session.messages = [
            Turn.user("do the thing"),
            Turn.assistant("working on it"),
            Turn.user("and the next thing"),
        ]
        session._msg_tokens = [5, 5, 5]
        before = list(session.messages)
        with patch.object(
            session, "_compaction_bailed", wraps=session._compaction_bailed
        ) as bailed:
            assert session._compact_messages(auto=True) is False
        assert bailed.call_args.args[0] == "empty_summary"
        assert session.messages == before
        assert len(requests) == 3
        assert not ui.of("error")
