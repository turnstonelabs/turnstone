"""Task-agent context compaction keeps execution truth outside the summary.

The task loop owns a bounded execution journal for cancellation and recall
beside a bounded model context that may be replaced. These tests pin the
cooperative soft warning, hard and reactive compaction paths, prefix
preservation, and the task-local cancellation/read seams.
"""

from __future__ import annotations

import gc
import weakref
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable

from tests._session_helpers import make_result, make_session
from turnstone.core.compaction import CompactionEngine, SummaryResult
from turnstone.core.metacognition import (
    NUDGE_TASK_COMPACTION_RESUME,
    format_nudge,
)
from turnstone.core.providers import UsageInfo
from turnstone.core.session import (
    GenerationCancelled,
    _active_read_files,
    _TaskExecutionJournal,
)
from turnstone.core.trajectory import EffectStatus, Role, ToolCall, Turn

TOOL_NAME = "search"
TOOL_CALL = {
    "id": "call-search",
    "type": "function",
    "function": {"name": TOOL_NAME, "arguments": '{"query":"needle"}'},
}
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": TOOL_NAME,
            "description": "Search files",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    }
]


def _usage(prompt_tokens: int, completion_tokens: int = 1) -> UsageInfo:
    return UsageInfo(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
    )


def _tool_result(*, prompt_tokens: int, content: str = "working"):
    return make_result(
        content,
        tool_calls=[TOOL_CALL],
        finish_reason="tool_calls",
        usage=_usage(prompt_tokens),
    )


def _prepared_tool(tool_call: dict[str, Any], _principal: str):
    call_id = tool_call["id"]
    return {
        "call_id": call_id,
        "func_name": TOOL_NAME,
        "needs_approval": False,
        "execute": lambda _item: (call_id, "x"),
    }


def _run_script(
    session,
    ledger: list[Turn],
    responses: list[Any],
    *,
    window: int = 100,
    tools: list[dict[str, Any]] = TOOLS,
    execution_journal: _TaskExecutionJournal | None = None,
    on_call: Callable[[int, list[Turn]], None] | None = None,
):
    queue = list(responses)
    contexts: list[list[Turn]] = []

    def plant(_lane, turns, **_kwargs):
        contexts.append(list(turns))
        if on_call is not None:
            on_call(len(contexts), list(turns))
        response = queue.pop(0)
        if isinstance(response, BaseException):
            raise response
        if callable(response):
            response = response()
        return response

    with (
        patch.object(session, "_context_window_for_lane", return_value=window),
        patch.object(session, "_prepare_tool_for_principal", side_effect=_prepared_tool),
        patch("turnstone.core.session.model_turn", side_effect=plant),
    ):
        output = session._run_agent(
            ledger,
            label="task",
            tools=tools,
            auto_tools={TOOL_NAME},
            parent_call_id="task-parent",
            principal_id="user-a",
            execution_journal=execution_journal,
        )
    assert queue == []
    return output, contexts


def _texts(turns: list[Turn]) -> list[str]:
    return [turn.text for turn in turns]


def test_soft_crossing_warns_then_compacts_after_wind_down():
    session = make_session(auto_compact_pct=0.8)
    ledger = [Turn.system("immutable task identity"), Turn.user("delegated contract")]
    journal = _TaskExecutionJournal(ledger)
    summary = SummaryResult(text="dense task summary", producer="summary-kernel")

    with patch.object(
        session._compaction_engine,
        "summarize_blocks",
        return_value=summary,
    ) as summarize:
        output, contexts = _run_script(
            session,
            ledger,
            [
                _tool_result(prompt_tokens=82),
                make_result(
                    "Goal recorded; resume by checking the parser.",
                    usage=_usage(86),
                ),
                make_result("implemented and verified", usage=_usage(30)),
            ],
            execution_journal=journal,
        )

    assert output == "implemented and verified"
    assert len(contexts) == 3
    assert format_nudge("compaction_pending") in _texts(contexts[1])
    assert _texts(contexts[2])[:2] == ["immutable task identity", "delegated contract"]
    assert _texts(contexts[2])[-3] == "[Conversation summary]"
    assert _texts(contexts[2])[-2].startswith("dense task summary")
    assert "## Wind-down (verbatim)" in _texts(contexts[2])[-2]
    assert "Goal recorded; resume by checking the parser." in _texts(contexts[2])[-2]
    assert _texts(contexts[2])[-1] == NUDGE_TASK_COMPACTION_RESUME

    # The immutable delegation prefix is reattached, not summarized again.
    summarized_blocks = summarize.call_args.args[0]
    assert not any("immutable task identity" in block for block in summarized_blocks)
    assert not any("delegated contract" in block for block in summarized_blocks)

    # Raw pre-compaction payloads are released. Cancellation/recall truth lives
    # in the bounded journal and never contains synthetic summary/warning turns.
    assert [turn.role for turn in ledger] == [Role.SYSTEM, Role.USER, Role.ASSISTANT]
    assert not any(turn.source == "compaction" for turn in ledger)
    assert journal.project_steps() == [
        {
            "id": "call-search",
            "name": TOOL_NAME,
            "arguments": '{"query":"needle"}',
            "output": "x",
            "is_error": False,
        }
    ]


def test_wind_down_compaction_releases_replaced_native_payload_before_resume():
    session = make_session(auto_compact_pct=0.8)
    ledger = [Turn.system("immutable task identity"), Turn.user("delegated contract")]
    payload_refs: list[weakref.ReferenceType[Any]] = []

    class Payload:
        pass

    def wind_down_result():
        payload = Payload()
        payload_refs.append(weakref.ref(payload))
        return make_result(
            "wind-down recorded",
            usage=_usage(86),
            native_blocks=[{"type": "opaque", "payload": payload}],
        )

    def inspect_resume_call(call_number: int, _turns: list[Turn]) -> None:
        if call_number == 3:
            gc.collect()
            assert len(payload_refs) == 1
            assert payload_refs[0]() is None

    with patch.object(
        session._compaction_engine,
        "summarize_blocks",
        return_value=SummaryResult(text="dense task summary", producer="summary-kernel"),
    ):
        output, _contexts = _run_script(
            session,
            ledger,
            [
                _tool_result(prompt_tokens=82),
                wind_down_result,
                make_result("implemented and verified", usage=_usage(30)),
            ],
            on_call=inspect_resume_call,
        )

    assert output == "implemented and verified"


def test_soft_compaction_rearms_only_after_tool_progress():
    """An irreducible soft-only floor cannot create a summary/model-call loop.

    The 32K immutable prefix leaves every successful replacement above soft but
    below hard.  The post-summary no-tool response must therefore terminate the
    task; another advisory would repeat the same compaction forever without
    advancing the configured tool-turn bound.
    """

    session = make_session(auto_compact_pct=0.8, agent_max_turns=2)
    ledger = [Turn.system("p" * 32_000), Turn.user("delegated contract")]

    with patch.object(
        session._compaction_engine,
        "summarize_blocks",
        return_value=SummaryResult(text="short summary", producer="summary-kernel"),
    ) as summarize:
        output, contexts = _run_script(
            session,
            ledger,
            [
                _tool_result(prompt_tokens=8_200),
                make_result("wind-down recorded", usage=_usage(8_300)),
                make_result("finished after resume", usage=_usage(8_300)),
            ],
            window=10_000,
        )

    assert output == "finished after resume"
    assert len(contexts) == 3
    summarize.assert_called_once()
    assert format_nudge("compaction_pending") in _texts(contexts[1])
    assert format_nudge("compaction_pending") not in _texts(contexts[2])


def test_execution_journal_bounds_completed_raw_payloads():
    journal = _TaskExecutionJournal([])

    for index in range(5_000):
        call_id = f"call-{index}"
        journal.record_assistant(
            Turn.assistant(
                tool_calls=(
                    ToolCall(
                        id=call_id,
                        name="read_file",
                        arguments="a" * 16_000,
                    ),
                )
            )
        )
        journal.mark_started(call_id)
        journal.record_result(
            call_id,
            "x" * 16_000,
            is_error=False,
            effect_status=EffectStatus.COMMITTED,
        )

    steps = journal.project_steps()
    assert journal.retained_step_count == 100
    assert journal.retained_step_chars < 410_000
    assert len(steps) == 101
    assert steps[0]["output"] == "(+4900 earlier steps not retained)"
    assert steps[-1]["id"] == "call-4999"
    assert journal.cancelled_status() is EffectStatus.PARTIAL


def test_execution_journal_bounds_adversarial_ids_and_tool_name_cardinality():
    journal = _TaskExecutionJournal([])

    for index in range(5_000):
        call_id = f"call-{index}-" + "i" * 2_000
        name = f"invented-tool-{index}-" + "n" * 2_000
        journal.record_assistant(
            Turn.assistant(tool_calls=(ToolCall(id=call_id, name=name, arguments="{}"),))
        )
        journal.mark_started(call_id)
        journal.record_result(
            call_id,
            "done",
            is_error=True,
            effect_status=EffectStatus.NONE,
        )

    steps = journal.project_steps()
    disposition = journal.cancelled_disposition("task")
    assert journal.retained_step_count == 100
    assert journal.retained_effect_name_count <= 65  # 64 named keys + overflow bucket
    assert all(len(str(step["id"])) <= 256 for step in steps)
    assert all(len(str(step["name"])) <= 128 for step in steps)
    assert len(disposition) < 12_000
    assert "<other tool names>" in disposition


def test_successful_compaction_publishes_post_swap_context_before_next_call():
    session = make_session(auto_compact_pct=0.8, agent_max_turns=2)
    ledger = [Turn.system("p" * 32_000), Turn.user("delegated contract")]
    observed: dict[str, int] = {}

    def inspect_third_call(call_number: int, _turns: list[Turn]) -> None:
        if call_number != 3:
            return
        ends = [
            call.args[0]
            for call in lifecycle.call_args_list
            if call.args[0].get("phase") == "end" and call.args[0].get("ok")
        ]
        snapshot = session.ui._snapshot_agent_contexts()
        assert len(ends) == 1
        assert len(snapshot) == 1
        observed["event"] = ends[0]["after_tokens"]
        observed["snapshot"] = snapshot[0]["prompt_tokens"]

    with (
        patch.object(
            session._compaction_engine,
            "summarize_blocks",
            return_value=SummaryResult(text="short summary", producer="summary-kernel"),
        ),
        patch.object(session.ui, "on_compaction", wraps=session.ui.on_compaction) as lifecycle,
    ):
        output, _contexts = _run_script(
            session,
            ledger,
            [
                _tool_result(prompt_tokens=8_200),
                make_result("wind-down recorded", usage=_usage(8_300)),
                make_result("finished after resume", usage=_usage(8_300)),
            ],
            window=10_000,
            on_call=inspect_third_call,
        )

    assert output == "finished after resume"
    assert observed["snapshot"] == observed["event"]


def test_raising_context_hook_cannot_reclassify_committed_compaction():
    session = make_session(auto_compact_pct=0.8)
    session.ui.on_agent_context = MagicMock(side_effect=RuntimeError("broken custom UI"))
    ledger = [Turn.user("delegated contract")]

    with (
        patch.object(
            session._compaction_engine,
            "summarize_blocks",
            return_value=SummaryResult(text="private summary", producer="summary-kernel"),
        ),
        patch.object(session.ui, "on_compaction", wraps=session.ui.on_compaction) as lifecycle,
    ):
        output, contexts = _run_script(
            session,
            ledger,
            [
                _tool_result(prompt_tokens=92),
                make_result("done after compaction", usage=_usage(30)),
            ],
        )

    assert output == "done after compaction"
    assert "private summary" in _texts(contexts[1])
    events = [call.args[0] for call in lifecycle.call_args_list]
    assert [event["phase"] for event in events] == ["start", "end"]
    assert events[-1]["ok"] is True


def test_hard_crossing_compacts_before_another_agent_call():
    session = make_session(auto_compact_pct=0.8)
    ledger = [Turn.user("delegated contract")]

    with patch.object(
        session._compaction_engine,
        "summarize_blocks",
        return_value=SummaryResult(text="hard-limit summary", producer="summary-kernel"),
    ) as summarize:
        output, contexts = _run_script(
            session,
            ledger,
            [
                _tool_result(prompt_tokens=92),
                make_result("done after hard compaction", usage=_usage(30)),
            ],
        )

    assert output == "done after hard compaction"
    summarize.assert_called_once()
    assert len(contexts) == 2
    assert format_nudge("compaction_pending") not in _texts(contexts[1])
    assert _texts(contexts[1])[-3:] == [
        "[Conversation summary]",
        "hard-limit summary",
        NUDGE_TASK_COMPACTION_RESUME,
    ]


def test_repeated_task_compactions_have_distinct_targeted_attempt_ids():
    session = make_session(auto_compact_pct=0.8)
    ledger = [Turn.user("delegated contract")]

    with (
        patch.object(
            session._compaction_engine,
            "summarize_blocks",
            side_effect=[
                SummaryResult(text="first summary", producer="summary-kernel"),
                SummaryResult(text="second summary", producer="summary-kernel"),
            ],
        ),
        patch.object(
            session.ui,
            "on_compaction",
            wraps=session.ui.on_compaction,
        ) as on_compaction,
    ):
        output, _contexts = _run_script(
            session,
            ledger,
            [
                _tool_result(prompt_tokens=92),
                _tool_result(prompt_tokens=92),
                make_result("done", usage=_usage(30)),
            ],
        )

    assert output == "done"
    events = [call.args[0] for call in on_compaction.call_args_list]
    assert [event["phase"] for event in events] == ["start", "end", "start", "end"]
    assert [event["compaction_id"] for event in events] == [1, 1, 2, 2]
    assert {event["target"] for event in events} == {"task_agent"}
    assert {event["parent_call_id"] for event in events} == {"task-parent"}
    assert all("summary" not in event for event in events if event["phase"] == "end")


def test_task_summary_stays_private_and_uses_the_shared_compactor_contract():
    session = make_session(auto_compact_pct=0.8)
    ledger = [Turn.user("delegated contract")]
    journal = _TaskExecutionJournal(ledger)
    secret = "TASK_PRIVATE_SUMMARY_SENTINEL_7f0c4e"
    summary_requests: list[list[Turn]] = []

    def summarize(turns: list[Turn], **_kwargs: Any):
        summary_requests.append(list(turns))
        return make_result(secret)

    with (
        patch.object(session, "_utility_completion", side_effect=summarize),
        patch.object(session.ui, "on_compaction", wraps=session.ui.on_compaction) as lifecycle,
        patch("turnstone.core.session.save_message") as save,
    ):
        output, contexts = _run_script(
            session,
            ledger,
            [
                _tool_result(prompt_tokens=40),
                RuntimeError("maximum context length exceeded"),
                make_result("done after private summary", usage=_usage(30)),
            ],
            window=10_000,
            execution_journal=journal,
        )

    assert output == "done after private summary"
    assert len(summary_requests) == 1
    assert summary_requests[0][0].role is Role.SYSTEM
    assert summary_requests[0][0].text == CompactionEngine.COMPACTOR_SYSTEM_PROMPT
    assert secret in _texts(contexts[2])

    # The summary is a provider-facing context replacement, not workstream or
    # task-recall data. Its only observable trace is token/lifecycle metadata.
    assert secret not in repr([call.args[0] for call in lifecycle.call_args_list])
    assert secret not in repr(journal.project_steps())
    assert secret not in _texts(ledger)
    assert secret not in _texts(session.messages)
    save.assert_not_called()


def test_provider_overflow_compacts_once_and_retries_same_step():
    session = make_session(auto_compact_pct=0.8)
    ledger = [Turn.user("delegated contract")]
    overflow = RuntimeError("maximum context length exceeded")

    with patch.object(
        session._compaction_engine,
        "summarize_blocks",
        return_value=SummaryResult(text="overflow summary", producer="summary-kernel"),
    ) as summarize:
        output, contexts = _run_script(
            session,
            ledger,
            [
                _tool_result(prompt_tokens=40),
                overflow,
                make_result("done after retry", usage=_usage(30)),
            ],
        )

    assert output == "done after retry"
    summarize.assert_called_once()
    assert len(contexts) == 3
    assert _texts(contexts[2])[-3:] == [
        "[Conversation summary]",
        "overflow summary",
        NUDGE_TASK_COMPACTION_RESUME,
    ]


def test_second_overflow_returns_partial_execution_without_retry_storm():
    session = make_session(auto_compact_pct=0.8)
    ledger = [Turn.user("delegated contract")]
    overflow = RuntimeError("maximum context length exceeded")

    with patch.object(
        session._compaction_engine,
        "summarize_blocks",
        return_value=SummaryResult(text="overflow summary", producer="summary-kernel"),
    ) as summarize:
        output, contexts = _run_script(
            session,
            ledger,
            [
                _tool_result(prompt_tokens=40, content="useful partial analysis"),
                overflow,
                overflow,
            ],
        )

    assert output == "useful partial analysis"
    summarize.assert_called_once()
    assert len(contexts) == 3


def test_turn_limit_compacts_before_forced_synthesis():
    session = make_session(auto_compact_pct=0.8, agent_max_turns=1)
    # Keep enough message content in the immutable task prefix that the exact
    # tool-free synthesis request is still over soft. This pins the general
    # turn-limit compaction policy without relying on a schema the next request
    # explicitly discards.
    ledger = [Turn.user("delegated contract " * 50)]

    with patch.object(
        session._compaction_engine,
        "summarize_blocks",
        return_value=SummaryResult(text="turn-limit summary", producer="summary-kernel"),
    ) as summarize:
        output, contexts = _run_script(
            session,
            ledger,
            [
                _tool_result(prompt_tokens=82),
                make_result("forced synthesis", usage=_usage(30)),
            ],
        )

    assert output == "forced synthesis"
    summarize.assert_called_once()
    assert len(contexts) == 2
    assert "turn-limit summary" in _texts(contexts[1])
    assert "You have reached the tool call limit" in contexts[1][-1].text


def test_turn_limit_sizes_the_actual_tool_free_synthesis_request():
    """A discarded tool schema alone must not trigger lossy final compaction."""

    session = make_session(auto_compact_pct=0.8, agent_max_turns=1)
    ledger = [Turn.user("delegated contract")]
    large_tools = [
        {
            "type": "function",
            "function": {
                "name": TOOL_NAME,
                "description": "d" * 3_558,
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                },
            },
        }
    ]

    with patch.object(session._compaction_engine, "summarize_blocks") as summarize:
        output, contexts = _run_script(
            session,
            ledger,
            [
                _tool_result(prompt_tokens=900),
                make_result("forced synthesis", usage=_usage(58)),
            ],
            window=1_000,
            tools=large_tools,
        )

    assert output == "forced synthesis"
    summarize.assert_not_called()
    assert len(contexts) == 2
    assert contexts[1][-2].role is Role.TOOL
    assert contexts[1][-2].text == "x"
    assert "You have reached the tool call limit" in contexts[1][-1].text
    assert not any(turn.source == "compaction" for turn in contexts[1])


def test_summary_uses_task_cancel_ref_and_clears_only_task_reads():
    session = make_session(auto_compact_pct=0.8, reasoning_effort="low")
    parent_reads = session._read_files
    parent_reads.add("/parent/read.py")
    task_reads = {"/parent/read.py", "/task/exact.py"}
    token = _active_read_files.set(task_reads)
    seen_cancel_refs: list[object] = []
    seen_efforts: list[str | None] = []

    def summary_completion(_turns, **kwargs):
        seen_cancel_refs.append(kwargs["cancel_ref"])
        seen_efforts.append(kwargs["reasoning_effort"])
        return make_result("task-local summary")

    def summarize(_blocks, runtime):
        result = runtime.complete("summary system", "summary body", 100)
        return SummaryResult(text=result.content, producer=result.producer)

    ledger = [Turn.user("delegated contract")]
    try:
        with (
            patch.object(session._compaction_engine, "summarize_blocks", side_effect=summarize),
            patch.object(session, "_utility_completion", side_effect=summary_completion),
        ):
            output, _contexts = _run_script(
                session,
                ledger,
                [
                    _tool_result(prompt_tokens=92),
                    make_result("done", usage=_usage(30)),
                ],
            )
    finally:
        _active_read_files.reset(token)

    assert output == "done"
    assert len(seen_cancel_refs) == 1
    cancel_ref = seen_cancel_refs[0]
    assert cancel_ref.__class__.__name__ == "StreamAbortRef"
    assert seen_efforts == ["low"]
    assert task_reads == set()
    assert parent_reads == {"/parent/read.py"}


def test_cancelled_summary_retires_event_without_mutating_effect_ledger():
    session = make_session(auto_compact_pct=0.8)
    ledger = [Turn.user("delegated contract")]

    with (
        patch.object(
            session._compaction_engine,
            "summarize_blocks",
            side_effect=GenerationCancelled(),
        ),
        patch.object(
            session.ui,
            "on_compaction",
            wraps=session.ui.on_compaction,
        ) as on_compaction,
        pytest.raises(GenerationCancelled),
    ):
        _run_script(
            session,
            ledger,
            [_tool_result(prompt_tokens=92)],
        )

    events = [call.args[0] for call in on_compaction.call_args_list]
    assert [event["phase"] for event in events] == ["start", "end"]
    assert events[1]["reason"] == "cancelled"
    assert events[1]["notice"] is False
    assert {event["target"] for event in events} == {"task_agent"}
    assert [turn.role for turn in ledger] == [Role.USER, Role.ASSISTANT, Role.TOOL]
    assert not any(turn.source == "compaction" for turn in ledger)


def test_closed_summary_stream_uses_task_scope_cancellation_without_retrying():
    session = make_session(auto_compact_pct=0.8)
    ledger = [Turn.user("delegated contract")]

    def cancelled_summary(_turns, **kwargs):
        kwargs["cancel_ref"].abort()
        raise RuntimeError("summary stream closed by Stop")

    with (
        patch.object(session, "_utility_completion", side_effect=cancelled_summary) as complete,
        patch.object(session, "_stop_retrying", return_value=True),
        patch.object(
            session.ui,
            "on_compaction",
            wraps=session.ui.on_compaction,
        ) as on_compaction,
        pytest.raises(GenerationCancelled),
    ):
        _run_script(
            session,
            ledger,
            [_tool_result(prompt_tokens=92)],
        )

    complete.assert_called_once()
    events = [call.args[0] for call in on_compaction.call_args_list]
    assert events[0]["phase"] == "start"
    assert events[-1]["phase"] == "end"
    assert not any("retry_in" in event for event in events)
    assert events[-1]["reason"] == "cancelled"
    assert events[-1]["notice"] is False
    assert [turn.role for turn in ledger] == [Role.USER, Role.ASSISTANT, Role.TOOL]


def test_policy_boundaries_are_strict_and_shared():
    policy = make_session(auto_compact_pct=0.8)._compaction_policy(100)

    assert policy.over_soft(80) is False
    assert policy.over_soft(81) is True
    assert policy.over_hard(90) is False
    assert policy.over_hard(91) is True
    assert policy.owed(81, advised=False) is False
    assert policy.owed(81, advised=True) is True
    assert policy.owed(91, advised=False) is True


def test_failed_compaction_is_not_retried_without_new_context():
    session = make_session(auto_compact_pct=0.8)
    ledger = [Turn.user("delegated contract")]
    overflow = RuntimeError("maximum context length exceeded")
    failure = RuntimeError("summary backend unavailable")

    with (
        patch.object(
            session._compaction_engine,
            "summarize_blocks",
            side_effect=failure,
        ) as summarize,
        patch.object(session, "_stop_retrying", return_value=True),
        patch.object(session.ui, "on_compaction", wraps=session.ui.on_compaction) as lifecycle,
        patch.object(session.ui, "on_error", wraps=session.ui.on_error) as on_error,
    ):
        output, contexts = _run_script(
            session,
            ledger,
            [
                _tool_result(prompt_tokens=92, content="partial before failure"),
                overflow,
            ],
        )

    assert output == "partial before failure"
    summarize.assert_called_once()
    assert len(contexts) == 2
    events = [call.args[0] for call in lifecycle.call_args_list]
    assert [event["phase"] for event in events] == ["start", "end"]
    assert events[-1]["reason"] == "error"
    assert events[-1]["notice"] is True
    on_error.assert_not_called()


def test_post_summary_exception_emits_one_terminal_end_without_swapping_context():
    session = make_session(auto_compact_pct=0.8)
    ledger = [Turn.user("delegated contract")]

    with (
        patch.object(
            session._compaction_engine,
            "summarize_blocks",
            return_value=SummaryResult(text="valid summary", producer="summary-kernel"),
        ),
        patch(
            "turnstone.core.session.PromptTokenEstimator.invalidate",
            side_effect=RuntimeError("injected estimator failure"),
        ),
        patch.object(session.ui, "on_compaction", wraps=session.ui.on_compaction) as lifecycle,
    ):
        output, contexts = _run_script(
            session,
            ledger,
            [
                _tool_result(prompt_tokens=92),
                make_result("continued on original context", usage=_usage(40)),
            ],
        )

    assert output == "continued on original context"
    events = [call.args[0] for call in lifecycle.call_args_list]
    assert [event["phase"] for event in events] == ["start", "end"]
    assert events[-1]["reason"] == "error"
    assert events[-1]["ok"] is False
    assert _texts(contexts[1])[-2:] == ["working", "x"]
    assert not any(turn.source == "compaction" for turn in contexts[1])


@pytest.mark.parametrize("prompt_tokens", [0, 1])
def test_tiny_provider_usage_does_not_break_estimation(prompt_tokens: int):
    session = make_session(auto_compact_pct=0.8)
    ledger = [Turn.user("delegated contract")]

    output, contexts = _run_script(
        session,
        ledger,
        [make_result("done", usage=_usage(prompt_tokens))],
    )

    assert output == "done"
    assert len(contexts) == 1
