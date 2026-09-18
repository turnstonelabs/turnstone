"""Recovery failures preserve public task history and settle overflow owners."""

from __future__ import annotations

import copy
import json
from collections import deque
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import httpx2
import pytest
from openai import APIConnectionError

from tests._session_helpers import (
    NullUI,
    fake_chat_stream,
    make_registered_session,
    replace_session_lane,
)
from tests.test_workstream_endpoints import _build_history_app
from turnstone.core.completion_recovery import ModelTurnLocalError
from turnstone.core.judge import JudgeConfig
from turnstone.core.memory import load_last_error
from turnstone.core.providers import ContextWindowExceededError
from turnstone.core.providers._openai_chat import OpenAIChatCompletionsProvider
from turnstone.core.storage import get_storage
from turnstone.core.trajectory import Turn

_TASK_ID = "task-public"
_PRIOR = "I completed the file write; analysis remains unfinished."
_GUARDED = "The file was written; this is guarded partial work."
_FILE_CONTENT = "one completed child write\n"
_SUMMARY = "private summary that must never be committed"
_SUMMARY_TOKENS = 73


class _ObservedUI(NullUI):
    def __init__(self) -> None:
        super().__init__()
        self.events: list[dict[str, Any]] = []
        self.aux_usage: list[dict[str, Any]] = []
        self.summary_fault: Exception | None = None

    def _enqueue(self, data: dict[str, Any]) -> int:
        event_id = super()._enqueue(data)
        self.events.append(copy.deepcopy(data))
        return event_id

    def approve_tools(self, items: list[dict[str, Any]]) -> tuple[bool, str | None]:
        return True, None

    def on_state_change(self, state: str) -> None:
        self._enqueue({"type": "state", "state": state})

    def on_aux_usage(self, usage: dict[str, Any]) -> None:
        self.aux_usage.append(dict(usage))
        if usage["completion_tokens"] == _SUMMARY_TOKENS and self.summary_fault is not None:
            raise self.summary_fault
        super().on_aux_usage(usage)


class _SDKStream:
    def __init__(self, script: dict[str, Any]) -> None:
        self.chunks = iter(fake_chat_stream(**script))
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        try:
            return next(self.chunks)
        except StopIteration:
            # The SDK closes its response when its own iterator is exhausted.
            self.close()
            raise

    def close(self) -> None:
        self.closed = True


class _StrictSDK:
    """Real adapter input, with no repeating response to hide extra requests."""

    def __init__(self, *scripts: dict[str, Any] | Exception) -> None:
        self.remaining = deque(scripts)
        self.calls: list[dict[str, Any]] = []
        self.streams: list[_SDKStream] = []

    def __call__(self, **kwargs: Any) -> _SDKStream:
        self.calls.append(copy.deepcopy(kwargs))
        assert self.remaining, "unexpected provider request after the terminal failure"
        script = self.remaining.popleft()
        if isinstance(script, Exception):
            raise script
        stream = _SDKStream(script)
        self.streams.append(stream)
        return stream

    def assert_finished(self, requests: int) -> None:
        assert not self.remaining
        assert len(self.calls) == requests
        assert all(stream.closed for stream in self.streams)


@pytest.fixture
def session(tmp_db, monkeypatch):
    monkeypatch.setattr("turnstone.core.model_turn._DRAIN_RETRY_BASE_DELAY", 0.0)
    result = make_registered_session(
        user_id="test-user",
        ui=_ObservedUI(),
        context_window=100_000,
        max_tokens=256,
        compact_max_tokens=256,
        judge_config=JudgeConfig(enabled=False, output_guard=True),
    )
    replace_session_lane(result, provider=OpenAIChatCompletionsProvider())
    result._title_generated = True
    result._RETRY_BASE_DELAY = 0
    result.auto_approve = True
    result._agent_prompt_components = ()
    result._task_tools = [
        tool for tool in result._task_tools if tool["function"]["name"] == "write_file"
    ]
    assert len(result._task_tools) == 1
    return result


def _install_sdk(session, *scripts: dict[str, Any] | Exception) -> _StrictSDK:
    sdk = _StrictSDK(*scripts)
    session._primary_lane().client.chat.completions.create = sdk
    return sdk


def _child_write(path) -> dict[str, Any]:
    return {
        "content": _PRIOR,
        "tool_calls": [
            {
                "id": "child-write",
                "name": "write_file",
                "arguments": json.dumps(
                    {"path": str(path), "content": _FILE_CONTENT, "mode": "append"}
                ),
            }
        ],
        "finish_reason": "tool_calls",
        "prompt_tokens": 40,
    }


def _parent_results(ui) -> list[dict[str, Any]]:
    return [
        event
        for event in ui.events
        if event.get("type") == "tool_result"
        and event.get("call_id") == _TASK_ID
        and not event.get("accepted")
    ]


def _assert_written_once(path, ui) -> list[dict[str, Any]]:
    assert path.read_text() == _FILE_CONTENT
    steps = ui.get_agent_trajectory(_TASK_ID)
    assert steps is not None and len(steps) == 1
    assert steps[0]["name"] == "write_file"
    assert steps[0]["is_error"] is False
    assert "Appended" in steps[0]["output"]
    # Ordinary successful child receipts omit the optional effect annotation.
    assert steps[0].get("effect_status") in (None, "committed")
    assert ui._snapshot_agent_contexts() == []
    assert ui._snapshot_agent_compactions() == []
    return steps


def test_failed_task_partial_survives_parent_fold_and_warm_cold_history(session, tmp_path):
    path = tmp_path / "completed-effect.txt"
    sdk = _install_sdk(
        session,
        {
            "tool_calls": [
                {
                    "id": _TASK_ID,
                    "name": "task_agent",
                    "arguments": json.dumps({"prompt": "Write the file and finish the analysis."}),
                }
            ],
            "finish_reason": "tool_calls",
        },
        _child_write(path),
        {"content": ""},
        {"content": "  "},
        {"content": ""},
        {"content": "The task failed after writing the file."},
    )

    def guard(_call_id, output, func_name, **_kwargs):
        if func_name == "task_agent_synthesis":
            assert output == _PRIOR
            return _GUARDED, None
        return output, None

    with patch.object(session, "_evaluate_output", side_effect=guard) as evaluate:
        session.send("Delegate the file write and analysis.")

    sdk.assert_finished(6)
    guarded = [call for call in evaluate.call_args_list if call.args[2] == "task_agent_synthesis"]
    assert len(guarded) == 1
    terminal = _parent_results(session.ui)
    assert len(terminal) == 1
    assert terminal[0]["is_error"] is True
    output = terminal[0]["output"]
    assert output.startswith("Task error:")
    assert "Incomplete task" in output
    assert _GUARDED in output
    assert _PRIOR not in output
    accepted = [
        event
        for event in session.ui.events
        if event.get("type") == "tool_result"
        and event.get("call_id") == _TASK_ID
        and event.get("accepted")
    ]
    assert len(accepted) == 1
    assert accepted[0]["is_error"] is True
    assert accepted[0]["output"] == output
    steps = _assert_written_once(path, session.ui)

    tool_turns = [
        turn for turn in session.messages if turn.role == "tool" and turn.tool_call_id == _TASK_ID
    ]
    assert len(tool_turns) == 1
    assert tool_turns[0].is_error is True
    assert tool_turns[0].text == output
    storage = get_storage()
    durable = [
        row
        for row in storage.load_messages(session.ws_id, repair=False)
        if row.get("tool_call_id") == _TASK_ID
    ]
    assert len(durable) == 1
    assert durable[0]["is_error"] is True
    assert durable[0]["content"] == output

    manager = MagicMock()
    manager.get.return_value = SimpleNamespace(id=session.ws_id, session=session, ui=session.ui)
    with _build_history_app(manager, storage) as client:
        warm = client.get(f"/v1/api/workstreams/{session.ws_id}/history")
        assert warm.status_code == 200
        # A genuinely cold workstream, not a live session with an empty stash.
        manager.get.return_value = None
        cold = client.get(f"/v1/api/workstreams/{session.ws_id}/history")
        assert cold.status_code == 200

    for response, retained in ((warm, True), (cold, False)):
        messages = response.json()["messages"]
        tool = next(row for row in messages if row.get("tool_call_id") == _TASK_ID)
        assert tool["is_error"] is True
        assert tool["content"] == output
        parent_call = next(
            call for row in messages for call in row.get("tool_calls", []) if call["id"] == _TASK_ID
        )
        if retained:
            assert parent_call["agent_steps"] == steps
        else:
            assert "agent_steps" not in parent_call


@pytest.fixture(params=["ordinary", "transport", "overflow"])
def observer_fault(request):
    if request.param == "transport":
        return APIConnectionError(request=httpx2.Request("POST", "https://example.invalid/v1"))
    if request.param == "overflow":
        return ContextWindowExceededError("maximum context length exceeded in local observer")
    return RuntimeError("summary usage observer failed")


def _summary_response() -> dict[str, Any]:
    return {"content": _SUMMARY, "completion_tokens": _SUMMARY_TOKENS}


def _assert_failed_compaction(ui, target: str) -> None:
    events = [event for event in ui.events if event.get("type") == "compaction"]
    starts = [event for event in events if event["phase"] == "start"]
    ends = [event for event in events if event["phase"] == "end"]
    assert len(starts) == len(ends) == 1
    assert starts[0]["compaction_id"] == ends[0]["compaction_id"]
    assert all(event.get("target", "workstream") == target for event in events)
    assert ends[0]["ok"] is False
    assert ends[0]["reason"] == "error"
    if target == "task_agent":
        assert all(event["parent_call_id"] == _TASK_ID for event in events)
    assert sum(usage["completion_tokens"] == _SUMMARY_TOKENS for usage in ui.aux_usage) == 1
    assert _SUMMARY not in str(ui.events)


def test_main_overflow_summary_observer_fault_stops_the_owner(session, observer_fault):
    session.messages = [Turn.user("Earlier request"), Turn.assistant("Earlier answer")]
    session._msg_tokens = [5, 5]
    session._system_tokens = 0
    storage = get_storage()
    storage.save_message(session.ws_id, "user", "Earlier request")
    storage.save_message(session.ws_id, "assistant", "Earlier answer")
    session.ui.summary_fault = observer_fault
    sdk = _install_sdk(
        session,
        ContextWindowExceededError("maximum context length exceeded"),
        _summary_response(),
    )

    with pytest.raises(ModelTurnLocalError) as failure:
        session.send("Continue the earlier request")

    assert failure.value.__cause__ is observer_fault
    sdk.assert_finished(2)
    _assert_failed_compaction(session.ui, "workstream")
    assert _SUMMARY not in str(session.messages)
    assert _SUMMARY not in str(storage.load_messages(session.ws_id))
    errors = [event for event in session.ui.events if event.get("type") == "error"]
    assert len(errors) == 1
    last_error = load_last_error(session.ws_id)
    assert last_error == str(failure.value)
    assert "Local model-call completion accounting failed" in last_error
    assert "Context window exceeded" not in last_error


def test_task_overflow_summary_observer_fault_fails_without_salvage(
    session, tmp_path, observer_fault
):
    path = tmp_path / "effect-before-overflow.txt"
    session.ui.summary_fault = observer_fault
    sdk = _install_sdk(
        session,
        _child_write(path),
        ContextWindowExceededError("maximum context length exceeded"),
        _summary_response(),
    )

    with (
        patch.object(
            session, "_guard_subagent_synthesis", wraps=session._guard_subagent_synthesis
        ) as synthesis,
        patch.object(session.ui, "begin_agent_scope", wraps=session.ui.begin_agent_scope) as begin,
        patch.object(session.ui, "end_agent_scope", wraps=session.ui.end_agent_scope) as end,
    ):
        call_id, output = session._exec_task(
            {"call_id": _TASK_ID, "prompt": "Write the file and finish the analysis."}
        )

    assert call_id == _TASK_ID
    assert output.startswith("Task error: Local model-call completion accounting failed")
    assert _PRIOR not in output
    assert _SUMMARY not in output
    synthesis.assert_not_called()
    begin.assert_called_once()
    end.assert_called_once()
    sdk.assert_finished(3)
    _assert_failed_compaction(session.ui, "task_agent")
    _assert_written_once(path, session.ui)
    terminal = _parent_results(session.ui)
    assert len(terminal) == 1
    assert terminal[0]["is_error"] is True
    assert terminal[0]["output"] == output
    assert _SUMMARY not in str(get_storage().load_messages(session.ws_id))
