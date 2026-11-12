"""A sub-agent and the tools of the task's own servers, and what of a sub-agent
reaches the training sample: nothing.

No container.  The task's servers are a stand-in for the MCP client a sidecar
brings: a ledger kept in memory.  The sandbox is the one that starts nothing
(``test_rollout_episode.py``), so no command of any script here runs anywhere.
The same things against a real sidecar are in ``test_rollout_sidecar.py``.
"""

from __future__ import annotations

import json
import threading
from typing import Any
from unittest.mock import MagicMock

import mcp.types as mcp_types
import pytest

from tests._scripted_chat_server import ScriptedChatServer, decode, encode, render_prompt
from tests.test_rollout_episode import (
    SAMPLED,
    _no_configuration_file,  # noqa: F401
    _NotingSandbox,
    _run,
)
from tests.test_rollout_rules import SHIPPED
from turnstone.core.mcp_client import MCPClientManager
from turnstone.core.rule_registry import HeuristicRuleDef
from turnstone.core.tools import ENVIRONMENT_TOOL_NAMES
from turnstone.rollout import cli
from turnstone.rollout.episode import run_episode
from turnstone.rollout.idprompt import check_token_id_route
from turnstone.rollout.tokens import generated_positions, rebuild_final, rebuild_prompts

POST_ENTRY, LIST_ENTRIES = "mcp__ledger__post_entry", "mcp__ledger__list_entries"
TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": POST_ENTRY,
            "description": "Post an amount to the ledger.",
            "parameters": {
                "type": "object",
                "properties": {"amount": {"type": "integer"}, "memo": {"type": "string"}},
                "required": ["amount"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": LIST_ENTRIES,
            "description": "List what the ledger holds.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]
NAMES = {POST_ENTRY, LIST_ENTRIES}


class _Ledger:
    """A stand-in for the MCP client of a task's sidecar: a ledger kept in memory.

    With *together* above one a posting waits until that many are under way at
    once, and each gets a number of its own; a caller left alone fails."""

    def __init__(self, *, listing: str = "", together: int = 1) -> None:
        self.entries: list[int] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.listing = listing
        self._meet = threading.Barrier(together) if together > 1 else None
        self._lock = threading.Lock()
        self.client = MagicMock(spec=MCPClientManager)
        self.client.get_tools.return_value = TOOLS
        self.client.get_resources.return_value = []
        self.client.get_prompts.return_value = []
        self.client.is_mcp_tool.side_effect = lambda name, **_kw: name in NAMES
        self.client.call_tool_sync.side_effect = self._call

    def _call(self, name: str, arguments: dict[str, Any], **_kw: Any) -> mcp_types.CallToolResult:
        with self._lock:
            self.calls.append((name, dict(arguments)))
        if name == LIST_ENTRIES:
            text = self.listing
        else:
            met = "" if self._meet is None else f" as number {self._meet.wait(timeout=20)}"
            with self._lock:
                self.entries.append(arguments["amount"])
            text = f"posted {arguments['amount']}{met}"
        return mcp_types.CallToolResult(
            content=[mcp_types.TextContent(type="text", text=text)], is_error=False
        )


class _ServedSandbox(_NotingSandbox):
    """The sandbox that starts nothing, with the MCP client of a task's own servers."""

    def __init__(self, ledger: _Ledger) -> None:
        super().__init__()
        self.mcp_client = ledger.client


def _delegate(prompt: str) -> dict[str, Any]:
    return {"name": "task_agent", "arguments": {"prompt": prompt}}


# What the sub-agent's model writes carries words nothing else holds, so that
# each can be looked for in the token record.
THINKS, ARGUMENT, HANDS_BACK = "zebra-thinks", "quokka-memo", "okapi-hands-back"
ASKED = "Post five to the ledger."
PARENT: list[dict[str, Any]] = [
    {"reasoning": "Delegate the posting.", "tool_calls": [_delegate(ASKED)]},
    {"reasoning": "Now my own.", "tool_calls": [{"name": POST_ENTRY, "arguments": {"amount": 3}}]},
    {"reasoning": "Done.", "content": "Both are posted."},
]
AGENT: list[dict[str, Any]] = [
    {
        "reasoning": f"A {THINKS} step.",
        "tool_calls": [{"name": POST_ENTRY, "arguments": {"amount": 5, "memo": ARGUMENT}}],
    },
    {"reasoning": f"Another {THINKS} step.", "content": f"Posted five ({HANDS_BACK})."},
]


def _served(ledger: _Ledger, server: ScriptedChatServer, **config: Any) -> dict[str, Any]:
    record, _box = _run(server, sandbox=_ServedSandbox(ledger), **config)  # type: ignore[arg-type]
    return record


def _offered(request: dict[str, Any]) -> set[str]:
    return {t["function"]["name"] for t in request["tools"]}


class TestTheTasksOwnServersInASubAgent:
    def test_a_sub_agent_is_offered_them_and_every_call_it_makes_is_in_the_record(self):
        ledger = _Ledger()
        with ScriptedChatServer(PARENT, agent_script=AGENT) as server:
            record = _served(ledger, server)
            first_parent, first_agent = server.parent_requests[0], server.agent_requests[0]

        assert record["finish_state"] == "completed", record["error"]
        assert record["trainable"] is True and record["one_sequence"] is True
        # Offered to the session and, by default, to its sub-agent.
        assert _offered(first_parent) >= NAMES
        assert _offered(first_agent) == ENVIRONMENT_TOOL_NAMES | NAMES
        assert record["subagents"]["sidecar_tools"] == "offered"
        # The sub-agent's call reached the task's server, before the session's own.
        assert ledger.entries == [5, 3]
        # The session's log holds its own two calls; the delegation says to whom.
        delegation, own = record["tool_log"]
        assert delegation["tool"] == "task_agent" and delegation["agent"] == 0
        assert delegation["args"] == {"prompt": ASKED}
        assert own["tool"] == POST_ENTRY and own["args"] == {"amount": 3} and "agent" not in own
        # The sub-agent's call: its arguments, its outcome, and no rule.
        (call,) = record["subagents"]["tool_log"]
        parent_id = record["messages"][2]["tool_calls"][0]["id"]
        assert call["call_id"].startswith(parent_id + "::")
        assert {key: value for key, value in call.items() if key != "call_id"} == {
            "agent": 0,
            "turn": 0,
            "tool": POST_ENTRY,
            "args": {"amount": 5, "memo": ARGUMENT},
            "result": "posted 5",
            "ok": True,
        }

    def test_a_run_can_withhold_them(self):
        ledger = _Ledger()
        with ScriptedChatServer(PARENT, agent_script=AGENT) as server:
            record = _served(ledger, server, subagent_sidecar_tools=False)
            first_parent, first_agent = server.parent_requests[0], server.agent_requests[0]

        assert record["finish_state"] == "completed", record["error"]
        assert _offered(first_parent) >= NAMES
        assert _offered(first_agent) == ENVIRONMENT_TOOL_NAMES
        assert record["subagents"]["sidecar_tools"] == "withheld"
        # The call the sub-agent made anyway never reached the server, and is in the log.
        assert ledger.entries == [3] and [name for name, _args in ledger.calls] == [POST_ENTRY]
        (attempt,) = record["subagents"]["tool_log"]
        assert attempt["tool"] == POST_ENTRY and attempt["ok"] is False
        assert "not available" in attempt["result"]
        assert record["trainable"] is True

    def test_a_task_without_servers_has_nothing_to_offer_or_withhold(self):
        with ScriptedChatServer([{"content": "nothing to do"}]) as server:
            record, _box = _run(server, sandbox=_NotingSandbox())  # type: ignore[arg-type]
        assert record["subagents"]["sidecar_tools"] is None

    def test_the_rules_are_asked_about_a_sub_agents_call_to_one(self):
        no_memo = HeuristicRuleDef(
            name="no-quokka-memo",
            risk_level="high",
            confidence=0.9,
            recommendation="deny",
            tool_pattern=POST_ENTRY,
            arg_patterns=["quokka"],
            intent_template="Post with a memo: {arg_snippet}",
            reasoning_template="this task does not allow that memo",
            tier="high",
        )
        ledger = _Ledger()
        with ScriptedChatServer(PARENT, agent_script=AGENT) as server:
            record = _served(ledger, server, rules=SHIPPED.with_rules([no_memo], name="no-quokka"))

        assert record["finish_state"] == "completed", record["error"]
        # Refused before it reached the server; the session's own call has no memo.
        assert ledger.entries == [3]
        (refused,) = record["subagents"]["tool_log"]
        assert refused["rule"] == "no-quokka-memo" and refused["rule_says"] == "deny"
        assert refused["ok"] is False and refused["result"].startswith("Refused by the rule")
        (event,) = record["rules"]["events"]
        assert event["scope"] == "sub_agent" and event["refused"] is True
        assert event["tool"] == POST_ENTRY and event["call_id"] == refused["call_id"]
        assert all("rule" not in entry for entry in record["tool_log"])
        assert record["trainable"] is True

    def test_what_one_returns_is_bounded_in_the_sub_agent_and_on_the_way_back(self):
        """Inside a sub-agent the framework's own bound holds, as for any of its
        results; the rollout's bound is on what the sub-agent hands back."""
        listing = "".join(f"entry {n}; " for n in range(5000))
        parent = [{"tool_calls": [_delegate("List the ledger.")]}, {"content": "listed"}]
        agent = [
            {"tool_calls": [{"name": LIST_ENTRIES, "arguments": {}}]},
            {"content": "finding " * 1500},
        ]
        ledger = _Ledger(listing=listing)
        with ScriptedChatServer(parent, agent_script=agent) as server:
            record = _served(
                ledger,
                server,
                max_tool_result_chars=2000,
                # The scripted server counts a token for every character.
                context_window=4_000_000,
                max_tokens=1000,
            )
            (inside,) = [
                m["content"] for m in server.agent_requests[-1]["messages"] if m["role"] == "tool"
            ]
            (handed_back,) = [
                m["content"] for m in server.parent_requests[-1]["messages"] if m["role"] == "tool"
            ]

        assert record["finish_state"] == "completed", record["error"]
        assert len(listing) > 50_000
        assert inside.startswith("entry 0; entry 1; ") and len(inside) <= 16_000
        assert len(handed_back) <= 2000 and "Ask the sub-agent for a shorter" in handed_back
        # Only what entered the session's conversation is counted as cut.
        assert record["tool_results"] == {
            "cut": 1,
            "cuts": [{"tool": "task_agent", "chars": 12000}],
        }
        (call,) = record["subagents"]["tool_log"]
        assert call["tool"] == LIST_ENTRIES and call["ok"] is True
        assert call["result"] == listing[:500]

    def test_two_sub_agents_reach_the_one_client_at_the_same_time(self):
        """Both postings are under way at once, each on its sub-agent's own thread:
        the session does not queue them, so the client has to take both."""
        parent = [
            {"tool_calls": [_delegate("Post five."), _delegate("Post five as well.")]},
            {"content": "Both are posted."},
        ]
        agent = [
            {"tool_calls": [{"name": POST_ENTRY, "arguments": {"amount": 5}}]},
            {"content": "posted"},
        ]
        ledger = _Ledger(together=2)
        with ScriptedChatServer(parent, agent_script=agent) as server:
            record = _served(ledger, server)

        assert record["finish_state"] == "completed", record["error"]
        calls = record["subagents"]["tool_log"]
        assert sorted(c["agent"] for c in calls) == [0, 1] and all(c["ok"] for c in calls)
        # Each caller got an answer of its own.
        assert sorted(c["result"] for c in calls) == [
            "posted 5 as number 0",
            "posted 5 as number 1",
        ]
        assert ledger.entries == [5, 5] and record["subagents"]["started"] == 2
        assert record["trainable"] is True

    def test_the_command_has_the_switch(self, monkeypatch, tmp_path, capsys):
        ledger = _Ledger()
        box = _ServedSandbox(ledger)
        monkeypatch.setattr(
            cli,
            "run_episode",
            lambda task, config, **check: run_episode(
                task,
                config,
                sandbox_factory=lambda _t, _c: box,  # type: ignore[arg-type,return-value]
                allow_host_execution=True,
                **check,
            ),
        )
        out = tmp_path / "episode.json"
        with ScriptedChatServer(PARENT, agent_script=AGENT) as server:
            status = cli.main(
                ["--base-url", server.base_url, "--model", "scripted", "--prompt", "post"]
                + ["--top-k", "20", "--top-p", "0.95", "--context-window", "200000"]
                + ["--sampler-logprobs", "yes", "--out", str(out)]
                + ["--no-subagent-sidecar-tools"]
            )
        record = json.loads(out.read_text(encoding="ascii"))
        assert status == 0, record["problems"]
        assert record["subagents"]["sidecar_tools"] == "withheld" and ledger.entries == [3]
        assert "tools of the task's own servers: withheld to sub-agents" in capsys.readouterr().out


def _find(sequence: list[int], part: list[int]) -> int:
    """Where *part* begins in *sequence*, -1 when it is not there."""
    for start in range(len(sequence) - len(part) + 1):
        if sequence[start : start + len(part)] == part:
            return start
    return -1


def _token_id_route(server: ScriptedChatServer) -> dict[str, Any]:
    """The verdict of the check before a run, made on *server* itself: a
    verdict speaks for the server it was made on.  The server then forgets
    the check's requests, so that the rollouts below show nothing but their
    own, counted from the first.
    """
    import openai

    with openai.OpenAI(base_url=server.base_url, api_key="none", max_retries=0) as client:
        verdict = check_token_id_route(client, "scripted", extra_body=SAMPLED["extra_body"])
    assert verdict["ok"] is True
    server.forget()
    return verdict


def _mode(name: str, server: ScriptedChatServer) -> dict[str, Any]:
    if name == "messages":
        return {}
    return {"token_id_prompts": True, "token_id_route": _token_id_route(server)}


def _keys(value: Any) -> set[str]:
    """Every key that appears anywhere inside *value*."""
    if isinstance(value, dict):
        return set(value) | {key for item in value.values() for key in _keys(item)}
    if isinstance(value, list):
        return {key for item in value for key in _keys(item)}
    return set()


@pytest.mark.parametrize("mode", ["messages", "token_ids"])
class TestNothingOfASubAgentIsInTheSample:
    """For training a sub-agent is a tool.  The sample holds the session's own
    call that starts it, generated, with its log-probabilities, and the result
    it hands back, as text from the environment.  Nothing else of it: no token
    and no log-probability of its model calls, whichever way the prompts go."""

    def test_the_sample_holds_the_delegation_and_what_came_back_and_nothing_else(self, mode):
        ledger = _Ledger()
        with ScriptedChatServer(PARENT, agent_script=AGENT) as server:
            record = _served(ledger, server, **_mode(mode, server))
            parent_requests = server.parent_requests
            agent_requests = server.agent_requests

        assert record["finish_state"] == "completed", record["error"]
        assert record["prompt_mode"] == mode
        assert record["trainable"] is True and record["one_sequence"] is True
        assert len(agent_requests) == 2 and ledger.entries == [5, 3]

        # The token record holds the session's own three calls, each with the
        # prompt the session's own request had.
        tokens = record["tokens"]
        assert record["turns"] == 3 and len(tokens["calls"]) == 3
        if mode == "token_ids":
            sent = [r["prompt"] for r in parent_requests]
            assert all(isinstance(r.get("prompt"), list) for r in agent_requests)
        else:
            sent = [render_prompt(r["messages"], r["tools"]) for r in parent_requests]
        assert rebuild_prompts(tokens) == sent

        # Nothing the sub-agent's model generated is anywhere in the sequence:
        # not its reasoning, not the arguments of its call.
        final = rebuild_final(tokens)
        text = decode(final)
        assert THINKS not in text and ARGUMENT not in text
        # What it handed back is there once, as context: no generated token and
        # no log-probability lies in it.
        generated = {entry["position"] for entry in generated_positions(tokens)}
        at = _find(final, encode(HANDS_BACK))
        assert at >= 0 and text.count(HANDS_BACK) == 1
        assert not generated & set(range(at, at + len(HANDS_BACK)))
        assert all(HANDS_BACK not in decode(call["token_ids"]) for call in tokens["calls"])
        # The session's own call that started it was generated, every token of it.
        asked = _find(final, encode(ASKED))
        assert asked >= 0 and set(range(asked, asked + len(ASKED))) <= generated
        assert ASKED in decode(tokens["calls"][0]["token_ids"])
        assert len(tokens["calls"][0]["logprobs"]) == len(tokens["calls"][0]["token_ids"])

        # The rollout's length and the server's counts are the session's own.
        own = sum(len(call["token_ids"]) for call in tokens["calls"])
        assert record["generated_tokens"] == tokens["generated_tokens"] == own
        assert record["usage"]["completion"] == own
        assert [call["scope"] for call in record["calls"]] == ["parent"] * 3
        assert record["discarded_calls"] == []

        # The side record says what it is, and holds counts, summaries and the
        # tool log: no token ID and no log-probability under any key.
        sub = record["subagents"]
        assert sub["not_for_training"] is True and sub["token_data_kept"] is False
        assert sub["model_calls"] == 2 and sub["generated_tokens"] > 0
        assert not _keys(sub) & {"token_ids", "logprobs", "prompt_token_ids", "prompt_tail"}
        assert all(
            set(call)
            == {
                "index",
                "scope",
                "complete",
                "finish_reason",
                "error",
                "prompt_tokens",
                "generated_tokens",
                "seconds",
            }
            and call["scope"] == "sub_agent"
            for call in sub["calls"]
        )
        assert [call["tool"] for call in sub["tool_log"]] == [POST_ENTRY]

    @pytest.mark.parametrize("fault", ["wrong_ids", "skip_logprobs", "null_logprob", "no_usage"])
    def test_token_data_of_a_sub_agent_that_is_unsound_does_not_decide_trainable(self, mode, fault):
        """None of the checks behind ``trainable`` is made on a sub-agent's calls:
        the same fault that makes the session's own call unusable changes nothing
        on a sub-agent's."""
        # Requests arrive in this order: the session's first, the sub-agent's
        # two, the session's second and third.  The faults are set once the
        # check before the run is over: they are the rollout's.
        with ScriptedChatServer(PARENT, agent_script=AGENT) as server:
            settings = _mode(mode, server)
            server.faults = {1: fault, 2: fault}
            on_the_sub_agent = _served(_Ledger(), server, **settings)
            assert [server.is_agent_request(r) for r in server.requests] == [
                False,
                True,
                True,
                False,
                False,
            ]
        assert on_the_sub_agent["finish_state"] == "completed", on_the_sub_agent["error"]
        assert on_the_sub_agent["problems"] == []
        assert on_the_sub_agent["trainable"] is True and on_the_sub_agent["one_sequence"] is True

        with ScriptedChatServer(PARENT, agent_script=AGENT) as server:
            settings = _mode(mode, server)
            server.faults = {3: fault}
            on_the_session = _served(_Ledger(), server, **settings)
        assert on_the_session["trainable"] is False and on_the_session["problems"]

    def test_a_sub_agents_prompts_are_no_part_of_the_sequence(self, mode):
        ledger = _Ledger()
        with ScriptedChatServer(PARENT, agent_script=AGENT) as server:
            record = _served(ledger, server, **_mode(mode, server))
            if mode == "token_ids":
                agent_prompts = [r["prompt"] for r in server.agent_requests]
            else:
                agent_prompts = [
                    render_prompt(r["messages"], r["tools"]) for r in server.agent_requests
                ]

            marker = server.agent_marker

        final = rebuild_final(record["tokens"])
        # A sub-agent starts from a system prompt of its own, which the rollout's
        # sequence does not hold; none of its prompts is a part of that sequence.
        assert marker not in decode(final)
        for prompt in agent_prompts:
            assert marker in decode(prompt)
            assert final[: len(prompt)] != prompt and _find(final, prompt) == -1
        assert record["prefix_ok"] is True and record["one_sequence"] is True
