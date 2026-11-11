"""The tool calls of a rollout's sub-agents, kept in the record apart from the session's.

No container.  Where a script holds only harmless commands it runs in the
directory stand-in of ``test_rollout_episode.py``; where a call is to be refused
the sandbox is the one that starts nothing.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests._scripted_chat_server import ScriptedChatServer
from tests.test_rollout_episode import SAMPLED, _NotingSandbox, _run
from tests.test_rollout_rules import NO_TOUCH, SHIPPED
from turnstone.core import config as config_module
from turnstone.core.trajectory import ToolCall, Turn
from turnstone.rollout.episode import RolloutConfig, Task, run_episode
from turnstone.rollout.subagent_log import SubAgentLog

TAB = chr(9)


@pytest.fixture(autouse=True)
def _no_configuration_file(monkeypatch):
    monkeypatch.setattr(config_module, "_cache", {})


def _delegate(prompt: str) -> dict[str, Any]:
    return {"name": "task_agent", "arguments": {"prompt": prompt}}


def _bash(command: str) -> dict[str, Any]:
    return {"name": "bash", "arguments": {"command": command}}


PARENT: list[dict[str, Any]] = [
    {"reasoning": "Delegate.", "tool_calls": [_delegate("Write the note, then say so.")]},
    {"reasoning": "Check.", "tool_calls": [{"name": "read_file", "arguments": {"path": "n"}}]},
    {"reasoning": "Good.", "content": "The note is written."},
]
AGENT: list[dict[str, Any]] = [
    {"tool_calls": [{"name": "write_file", "arguments": {"path": "n", "content": "note"}}]},
    {"tool_calls": [_bash("echo from the sub-agent"), _bash("cat n")]},
    {"content": "wrote n"},
]


def _parents(record: dict[str, Any]) -> list[str]:
    """The ids of the session's own ``task_agent`` calls, in the order made."""
    return [
        call["id"]
        for message in record["messages"]
        if message["role"] == "assistant"
        for call in message.get("tool_calls") or []
        if call["function"]["name"] == "task_agent"
    ]


class TestInTheRecord:
    def test_a_sub_agents_calls_are_kept_apart_from_the_sessions_own(self):
        with ScriptedChatServer(PARENT, agent_script=AGENT) as server:
            record, _box = _run(server)

        assert record["finish_state"] == "completed" and record["trainable"] is True
        (parent,) = _parents(record)
        sub = record["subagents"]
        assert sub["not_for_training"] is True
        calls = sub["tool_log"]
        assert [(c["agent"], c["turn"], c["tool"], c["ok"]) for c in calls] == [
            (0, 0, "write_file", True),
            (0, 1, "bash", True),
            (0, 1, "bash", True),
        ]
        assert calls[0]["args"] == {"path": "n", "content": "note"}
        assert calls[1]["args"] == {"command": "echo from the sub-agent"}
        assert calls[1]["result"] == "from the sub-agent" and calls[2]["result"] == "note"
        # Each call is the sub-agent's, under the session's call that started it.
        assert all(c["call_id"].startswith(parent + "::") for c in calls)
        assert len({c["call_id"] for c in calls}) == 3
        assert all("rule" not in c for c in calls)
        # The session's own log holds its own calls and no others.
        log = record["tool_log"]
        assert [e["tool"] for e in log] == ["task_agent", "read_file"]
        assert log[1]["result"] == "   1" + TAB + "note"

    def test_the_sessions_entry_of_a_delegation_says_what_was_asked_and_of_whom(self):
        with ScriptedChatServer(PARENT, agent_script=AGENT) as server:
            record, _box = _run(server)
        delegation = record["tool_log"][0]
        assert delegation["tool"] == "task_agent"
        assert delegation["args"] == {"prompt": "Write the note, then say so."}
        assert delegation["result"] == "wrote n" and delegation["ok"] is True
        # The number its sub-agent's calls carry.
        assert delegation["agent"] == 0
        assert "agent" not in record["tool_log"][1]

    def test_several_sub_agents_are_told_apart(self):
        parent = [
            {"tool_calls": [_delegate("first"), _delegate("second")]},
            {"tool_calls": [_delegate("third")]},
            {"content": "Done."},
        ]
        agent = [{"tool_calls": [_bash("echo working")]}, {"content": "ok"}]
        with ScriptedChatServer(parent, agent_script=agent) as server:
            record, _box = _run(server)

        parents = _parents(record)
        delegations = [e for e in record["tool_log"] if e["tool"] == "task_agent"]
        assert [e["args"]["prompt"] for e in delegations] == ["first", "second", "third"]
        # Numbered in the order they were started; two of one turn start side by side.
        assert sorted(e["agent"] for e in delegations[:2]) == [0, 1]
        assert delegations[2]["agent"] == 2
        calls = record["subagents"]["tool_log"]
        assert [c["agent"] for c in calls] == [0, 1, 2]
        for delegation, parent_id in zip(delegations, parents, strict=True):
            (call,) = [c for c in calls if c["agent"] == delegation["agent"]]
            assert call["call_id"].startswith(parent_id + "::")
            assert call["tool"] == "bash" and call["result"] == "working"
        assert record["subagents"]["started"] == 3

    def test_a_call_a_rule_refused_names_the_rule(self):
        agent = [{"tool_calls": [_bash("touch shortcut"), _bash("echo fine")]}, {"content": "ok"}]
        box = _NotingSandbox()
        with ScriptedChatServer(PARENT, agent_script=agent) as server:
            rules = SHIPPED.with_rules([NO_TOUCH], name="no-touch")
            config = RolloutConfig(
                base_url=server.base_url, model="scripted", rules=rules, **SAMPLED
            )
            record = run_episode(Task(prompt="go"), config, sandbox_factory=lambda _t, _c: box)

        refused, ran = record["subagents"]["tool_log"]
        assert refused["tool"] == "bash" and refused["args"] == {"command": "touch shortcut"}
        assert refused["ok"] is False and refused["rule"] == "no-touch"
        assert refused["rule_says"] == "deny"
        assert refused["result"].startswith("Refused by the rule 'no-touch'")
        assert ran["ok"] is True and "rule" not in ran
        # Only the one that was not refused reached the sandbox.
        assert box.environment.commands == ["echo fine"]
        # The same event the rules part of the record holds.
        (event,) = record["rules"]["events"]
        assert event["scope"] == "sub_agent" and event["call_id"] == refused["call_id"]

    def test_a_call_the_sub_agents_loop_answers_itself_is_there_too(self):
        """A tool the sub-agent is not offered, and a sub-agent of its own: nothing
        runs, and the attempt is on the record."""
        agent = [
            {
                "tool_calls": [
                    _delegate("do it for me"),
                    {"name": "web_fetch", "arguments": {"url": "http://example.invalid/"}},
                ]
            },
            {"content": "could not"},
        ]
        with ScriptedChatServer(PARENT, agent_script=agent) as server:
            record, _box = _run(server)
        nested, fetch = record["subagents"]["tool_log"]
        assert (nested["tool"], nested["ok"]) == ("task_agent", False)
        assert nested["args"] == {"prompt": "do it for me"}
        assert "cannot spawn further agents" in nested["result"]
        assert (fetch["tool"], fetch["ok"]) == ("web_fetch", False)
        assert fetch["args"] == {"url": "http://example.invalid/"}
        assert record["subagents"]["started"] == 1 and record["trainable"] is True

    def test_a_call_the_run_was_cut_in_is_there_too(self):
        """The time limit passes while a sub-agent's command runs.  The call is on
        the record, with what the session noted of it when it was stopped."""
        agent = [{"tool_calls": [_bash("sleep 30")]}, {"content": "never"}]
        with ScriptedChatServer(PARENT, agent_script=agent) as server:
            record, _box = _run(server, task=Task(prompt="wait", timeout=2))
        assert record["finish_state"] == "timeout"
        (cut,) = record["subagents"]["tool_log"]
        assert cut["tool"] == "bash" and cut["args"] == {"command": "sleep 30"}
        assert cut["ok"] is False and "interrupted" in cut["result"]

    def test_a_run_without_sub_agents_has_none(self):
        script = [{"tool_calls": [_bash("echo alone")]}, {"content": "done"}]
        with ScriptedChatServer(script) as server:
            record, _box = _run(server)
        assert record["subagents"]["tool_log"] == []
        assert all("agent" not in entry for entry in record["tool_log"])

    def test_a_delegation_refused_at_the_limit_started_no_sub_agent(self):
        parent = [
            {"tool_calls": [_delegate("one")]},
            {"tool_calls": [_delegate("two")]},
            {"content": "Done."},
        ]
        agent = [{"tool_calls": [_bash("echo working")]}, {"content": "ok"}]
        with ScriptedChatServer(parent, agent_script=agent) as server:
            record, _box = _run(server, max_subagents=1)
        first, second = record["tool_log"]
        assert first["agent"] == 0 and "agent" not in second
        assert second["ok"] is False and "no sub-agent was started" in second["result"]
        assert [c["agent"] for c in record["subagents"]["tool_log"]] == [0]


class TestTheLog:
    """The log by itself: what it makes of the turns a sub-agent's loop adds."""

    def _log(self) -> SubAgentLog:
        return SubAgentLog(lambda _name: None)

    def test_calls_come_with_assistant_turns_and_results_with_tool_turns(self):
        log = self._log()
        turns = log.watch([Turn.system("be a sub-agent"), Turn.user("do it")], "parent-1")
        call = ToolCall(id="c1", name=" bash ", arguments='{"command": "ls"}')
        turns.append(Turn.assistant("", tool_calls=(call,)))
        (entry,) = log.entries()
        assert entry == {
            "agent": 0,
            "turn": 0,
            "call_id": "c1",
            "tool": "bash",
            "args": {"command": "ls"},
            "result": None,
            "ok": None,
        }
        # First a placeholder, then the result put in its place.
        turns.append(Turn.tool("c1", "(processing)", is_error=False))
        turns[len(turns) - 1] = Turn.tool("c1", "a.txt", is_error=True)
        (entry,) = log.entries()
        assert entry["result"] == "a.txt" and entry["ok"] is False
        assert log.agents() == ["parent-1"]

    def test_the_turn_is_the_sub_agents_own_count(self):
        log = self._log()
        turns = log.watch([], "p")
        turns.append(Turn.assistant("thinking aloud"))
        turns.append(Turn.assistant("", tool_calls=(ToolCall("c1", "bash", "{}"),)))
        # The turns before a summary of them replaced them: nothing to note.
        turns[:] = []
        turns.append(Turn.assistant("", tool_calls=(ToolCall("c2", "bash", "{}"),)))
        assert [(e["call_id"], e["turn"]) for e in log.entries()] == [("c1", 1), ("c2", 2)]

    def test_arguments_that_are_no_object_are_kept_as_they_came(self):
        log = self._log()
        turns = log.watch([], "p")
        calls = (ToolCall("c1", "bash", "ls -la"), ToolCall("c2", "bash", "[1, 2]"))
        turns.append(Turn.assistant("", tool_calls=calls))
        assert [e["args"] for e in log.entries()] == [{"_raw": "ls -la"}, {"_raw": [1, 2]}]

    def test_a_result_is_kept_to_its_first_500_characters(self):
        log = self._log()
        turns = log.watch([], "p")
        turns.append(Turn.assistant("", tool_calls=(ToolCall("c1", "bash", "{}"),)))
        turns.append(Turn.tool("c1", "x" * 2000))
        assert len(log.entries()[0]["result"]) == 500

    def test_a_turn_that_cannot_be_read_does_not_stop_the_sub_agent(self):
        log = self._log()
        turns = log.watch([], "p")
        turns.append(object())  # no turn at all
        turns.append(Turn.assistant("", tool_calls=(ToolCall("c1", "bash", "{}"),)))
        assert len(turns) == 2 and [e["call_id"] for e in log.entries()] == ["c1"]
