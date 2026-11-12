"""Where a rollout's settings meet: prompts as token IDs with a task file, a
setup step, the server's version, a bound on tool results, a verifier command
and budgets, all from the command line; one check of the server for a batch;
and a record that is whole however the run ended.

No container.  The sandboxes are the stand-ins of ``test_rollout_episode.py``
and ``test_rollout_preparation.py``; every script holds harmless commands only,
and the one program started here is a verifier the test wrote under its own
temporary directory.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from tests._scripted_chat_server import ScriptedChatServer
from tests.test_rollout_episode import (
    SCRIPT,
    _DirSandbox,
    _no_configuration_file,  # noqa: F401
    _run,
)
from tests.test_rollout_preparation import _answer, _PreparedSandbox
from tests.test_rollout_subagent_tools import (
    LIST_ENTRIES,
    _delegate,
    _Ledger,
    _served,
    _token_id_route,
)
from tests.test_rollout_verifier_command import _program
from turnstone.rollout import cli, server_version
from turnstone.rollout.episode import Task, run_episode
from turnstone.rollout.preparation import BeforeRunCommand

BASE = ["--top-k", "20", "--top-p", "0.95", "--context-window", "200000"]
REFLECTS = '{"temperature": true, "top_k": true, "top_p": true}'


@pytest.fixture(autouse=True)
def _no_pause_between_tries(monkeypatch):
    monkeypatch.setattr(server_version, "_PAUSE_SECONDS", 0.0)


def _never(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    raise AssertionError("a rollout was run")


def _in(monkeypatch: pytest.MonkeyPatch, box: Any) -> None:
    """Have the command run its rollout in *box* instead of a container."""
    monkeypatch.setattr(
        cli,
        "run_episode",
        lambda task, config, **check: run_episode(
            task,
            config,
            sandbox_factory=lambda _t, _c: box,
            # The directory sandbox acts on this machine; the scripts are the tests' own.
            allow_host_execution=True,
            **check,
        ),
    )


class TestOneCheckForABatch:
    """A caller with many rollouts asks the server once and passes on what it said."""

    def test_the_probe_alone_also_checks_the_routes_with_prompts_as_token_ids(self, capsys):
        with ScriptedChatServer(SCRIPT, sampler_logprobs=True) as server:
            base = ["--base-url", server.base_url, "--model", "scripted", *BASE, "--probe-only"]
            assert cli.main([*base, "--token-id-prompts"]) == 0
            as_token_ids = json.loads(capsys.readouterr().out)
            assert cli.main(base) == 0
            as_messages = json.loads(capsys.readouterr().out)

        assert as_token_ids["sampler_logprobs"] is True and as_token_ids["route"] == "completions"
        route = as_token_ids["token_id_route"]
        assert route["ok"] is True and route["format"] == "parameter_blocks"
        assert route["closing"] and route["generation_prompt"]
        # A batch that sends messages has no such check, and nothing is printed for it.
        assert as_messages["route"] == "chat" and "token_id_route" not in as_messages

    def test_what_it_printed_spares_every_rollout_the_check_and_the_probe(
        self, monkeypatch, tmp_path, capsys
    ):
        verdict = tmp_path / "probe.json"
        out = tmp_path / "episode.json"
        with ScriptedChatServer(SCRIPT, sampler_logprobs=True) as server:
            base = ["--base-url", server.base_url, "--model", "scripted", *BASE]
            base += ["--token-id-prompts"]
            assert cli.main([*base, "--probe-only", "--out", str(verdict)]) == 0
            asked, rendered = len(server.requests), len(server.tokenize_requests)
            _in(monkeypatch, _DirSandbox())
            status = cli.main(
                [*base, "--prompt", "go", "--out", str(out)]
                + ["--sampler-logprobs", f"@{verdict}", "--token-id-route", f"@{verdict}"]
            )
            # The session's three calls and the renderings their prompts are built
            # from: one for the first, two for each later one.  Nothing else.
            assert len(server.requests) == asked + 3
            assert len(server.tokenize_requests) == rendered + 1 + 2 + 2

        record = json.loads(out.read_text(encoding="ascii"))
        assert status == 0, record["problems"]
        assert record["prompt_mode"] == "token_ids" and record["one_sequence"] is True
        given = json.loads(verdict.read_text(encoding="ascii"))
        assert record["token_id_route"] == given["token_id_route"]
        # The verdict that was handed in is the one in force; no probe was made.
        assert record["logprobs"]["sampler_logprobs"] is True
        assert record["logprobs"]["reflects"] == {"temperature": True, "top_k": True, "top_p": True}
        capsys.readouterr()

    def test_a_server_that_cannot_be_sent_token_ids_has_another_status(self, capsys):
        with ScriptedChatServer(SCRIPT, sampler_logprobs=True, tokenize_status=404) as server:
            base = ["--base-url", server.base_url, "--model", "scripted", *BASE]
            assert cli.main([*base, "--probe-only", "--token-id-prompts"]) == 2
        route = json.loads(capsys.readouterr().out)["token_id_route"]
        assert route["ok"] is False and "tokenize route" in route["reason"]

    @pytest.mark.parametrize(
        ("flags", "why"),
        [
            (["--token-id-route", "{}"], "--token-id-route is for --token-id-prompts"),
            (["--token-id-prompts", "--token-id-route", "maybe"], "the verdict of the check"),
            (
                ["--token-id-prompts", "--token-id-route", '{"token_id_route": null}'],
                "the verdict of the check",
            ),
            (["--token-id-prompts", "--token-id-route", "@/no/such/file"], "cannot be read"),
            (
                ["--token-id-prompts"]
                + ["--sampler-logprobs", '{"route": "chat", "reflects": ' + REFLECTS + "}"],
                "probed on the chat route",
            ),
            (
                ["--sampler-logprobs", '{"route": "completions", "reflects": ' + REFLECTS + "}"],
                "probed on the completions route",
            ),
        ],
    )
    def test_a_verdict_that_is_not_this_runs_is_refused(self, monkeypatch, capsys, flags, why):
        monkeypatch.setattr(cli, "run_episode", _never)
        base = ["--base-url", "http://127.0.0.1:9/v1", "--model", "m", "--prompt", "p"]
        assert cli.main([*base, *flags]) == 3
        assert why in capsys.readouterr().err

    def test_a_check_made_with_other_template_arguments_is_refused(
        self, monkeypatch, tmp_path, capsys
    ):
        """What a template writes at the end of a turn and at the start of a reply
        can depend on its arguments, so a verdict holds for the ones it was made with."""
        verdict = tmp_path / "probe.json"
        box = _DirSandbox()
        with ScriptedChatServer(SCRIPT, sampler_logprobs=True) as server:
            base = ["--base-url", server.base_url, "--model", "scripted", *BASE]
            base += ["--token-id-prompts"]
            probe = [*base, "--probe-only", "--effort", "low", "--out", str(verdict)]
            assert cli.main(probe) == 0
            asked = len(server.requests)
            _in(monkeypatch, box)
            run = [*base, "--prompt", "go", "--sampler-logprobs", "yes"]
            run += ["--token-id-route", f"@{verdict}"]
            assert cli.main([*run, "--effort", "high"]) == 3
            assert len(server.requests) == asked and box.root == ""
            assert "template arguments" in capsys.readouterr().err
            # The same arguments as the check's: taken.
            assert cli.main([*run, "--effort", "low"]) == 0
        capsys.readouterr()


class TestEverySettingFromTheCommand:
    def test_a_task_file_and_every_flag_of_a_training_run_together(
        self, monkeypatch, tmp_path, capsys
    ):
        """Prompts as token IDs, a named window, the bound on a tool result, the
        server's version, a setup step, budgets and a verifier command in one run,
        each of them in force in the record."""
        task = tmp_path / "task.json"
        task.write_text(
            json.dumps(
                {
                    "prompt": "count",
                    "key": "together-1",
                    "files": {"seed.txt": "seeded"},
                    "max_turns": 6,
                    "max_generated_tokens": 5000,
                    "max_command_seconds": 60,
                    "before_run": [{"argv": ["true"], "timeout": 30}],
                }
            ),
            encoding="utf-8",
        )
        program = _program(
            tmp_path, "print(json.dumps({'verdict': 'pass', 'key': sys.argv[1]}))", "grade"
        )
        out = tmp_path / "episode.json"
        script = [
            {"tool_calls": [{"name": "bash", "arguments": {"command": "seq 1 5000"}}]},
            {"content": "counted"},
        ]
        with ScriptedChatServer(
            script, sampler_logprobs=True, version_answers=[{"revision": 7}]
        ) as server:
            box = _PreparedSandbox([_answer()], server)
            _in(monkeypatch, box)
            status = cli.main(
                ["--base-url", server.base_url, "--model", "scripted", "--task", str(task)]
                + ["--top-k", "20", "--top-p", "0.95", "--token-id-prompts"]
                + ["--context-window", "200000", "--max-tokens", "1000"]
                + ["--max-tool-result-chars", "1200", "--min-tokens-per-second", "1"]
                + ["--server-version-url", server.version_url]
                + ["--server-version-field", "revision"]
                + ["--verifier-command", program + " {key}", "--verifier-timeout", "20"]
                + ["--out", str(out)]
            )
            requests = list(server.requests)
            first_question, last_question = server.version_requests

        record = json.loads(out.read_text(encoding="ascii"))
        assert status == 0, record["problems"]
        assert record["finish_state"] == "completed" and record["key"] == "together-1"
        assert record["trainable"] is True and record["one_sequence"] is True
        # Prompts as token IDs, inside a window the command named.
        assert record["prompt_mode"] == "token_ids" and record["token_id_route"]["ok"] is True
        assert [call["route"] for call in record["calls"]] == ["completions"] * 2
        assert record["limits"]["context_window"]["value"] == 200_000
        assert all(0 < call["window_left"] < 200_000 for call in record["calls"])
        assert record["logprobs"]["route"] == "completions"
        # One tool result was longer than the bound, and was cut to it.
        assert record["limits"]["max_tool_result_chars"]["value"] == 1200
        assert record["tool_results"] == {"cut": 1, "cuts": [{"tool": "bash", "chars": 23892}]}
        assert record["tool_log"][0]["cut_from"] == 23892
        # One version of the server, asked before anything else and after everything.
        assert record["server_version"]["first"] == record["server_version"]["last"] == 7
        assert record["server_version"]["same"] is True
        assert first_question["chat_requests_before"] == 0
        assert first_question["tokenize_requests_before"] == 0
        assert last_question["chat_requests_before"] == len(requests)
        # The setup step ran once the check and the probe were done, and before
        # the session's own first request.
        (prepared,) = box.asked
        assert prepared["argv"] == ["true"] and prepared["files_in_place"] is True
        own = [i for i, r in enumerate(requests) if r.get("max_tokens") == 1000]
        assert len(own) == 2 and prepared["model_requests"] == own[0] > 0
        assert record["before_run"]["failure"] is None
        assert record["task"]["before_run"][0]["argv"] == ["true"]
        # The model's budgets, with the time limit worked out from them.
        budgets = record["budgets"]
        assert budgets["max_generated_tokens"] == 5000 and budgets["max_command_seconds"] == 60
        assert budgets["wall_clock"] == "backstop for the machinery"
        # The operator's program gave the verdict.
        assert record["verification"]["result"] == {"verdict": "pass", "key": "together-1"}
        assert record["verification"]["program"]["exit_code"] == 0
        capsys.readouterr()


# What a record holds since the two sets of features were put together.
ADDED = {"before_run", "server_version", "tool_results", "prompt_mode", "token_id_route"}


class TestTheRecordIsWhole:
    def test_every_record_has_the_same_keys_however_the_run_ended(self):
        cut = [{**SCRIPT[0], "finish_reason": "length"}, *SCRIPT[1:]]
        failing = dict.fromkeys(range(40), "http_500")
        prepared = Task(prompt="p", before_run=[BeforeRunCommand(["true"])])
        records: dict[str, dict[str, Any]] = {}
        with ScriptedChatServer(SCRIPT) as server:
            records["completed as messages"], _box = _run(server)
        with ScriptedChatServer(SCRIPT) as server:
            records["completed as token IDs"], _box = _run(server, token_id_prompts=True)
        with ScriptedChatServer(cut) as server:
            records["length"], _box = _run(server, token_id_prompts=True)
        with ScriptedChatServer(SCRIPT, faults=failing) as server:
            records["error"], _box = _run(server)
        with ScriptedChatServer(SCRIPT) as server:
            # This server has no address that tells its version.
            records["server_version_unknown"], _box = _run(
                server, server_version_url=server.version_url, token_id_prompts=True
            )
        with ScriptedChatServer(SCRIPT) as server:
            records["sandbox_failed"], _box = _run(server, sandbox=_DirSandbox(fail_start=True))
        with ScriptedChatServer(SCRIPT) as server:
            records["before_run_failed"], _box = _run(
                server, task=prepared, sandbox=_PreparedSandbox([_answer(3)], server)
            )

        assert {name: record["finish_state"] for name, record in records.items()} == {
            "completed as messages": "completed",
            "completed as token IDs": "completed",
            "length": "length",
            "error": "error",
            "server_version_unknown": "server_version_unknown",
            "sandbox_failed": "sandbox_failed",
            "before_run_failed": "before_run_failed",
        }
        keys = set(records["completed as messages"])
        assert keys >= ADDED
        for name, record in records.items():
            assert set(record) == keys, name
            # Every one of them can be written as it is.
            assert json.loads(json.dumps(record)) == record, name
            assert record["prompt_mode"] in ("messages", "token_ids"), name
            assert set(record["subagents"]) == set(records["completed as messages"]["subagents"])
        # A run that did not start still says which way its prompts were to go.
        assert records["server_version_unknown"]["prompt_mode"] == "token_ids"
        assert records["server_version_unknown"]["token_id_route"]["ok"] is None
        for name in ("server_version_unknown", "sandbox_failed", "before_run_failed"):
            assert records[name]["verification"] is None and records[name]["turns"] == 0
            assert records[name]["trainable"] is False


class TestASubAgentWithPromptsAsTokenIds:
    def test_the_bounds_on_its_results_and_its_tool_log_are_the_same_there(self):
        """A sub-agent goes the same route as the session; what bounds its results,
        and what the record keeps of its tool calls, does not depend on the route."""
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
                token_id_prompts=True,
                token_id_route=_token_id_route(server),
                max_tool_result_chars=2000,
                # The scripted server counts a token for every character.
                context_window=4_000_000,
                max_tokens=1000,
            )
            agent_requests = server.agent_requests
            # What the server rendered for the sub-agent's second request.
            inside = [
                message["content"]
                for request in server.tokenize_requests
                for message in request.get("messages") or []
                if message["role"] == "tool" and message["content"].startswith("entry 0; ")
            ]

        assert record["finish_state"] == "completed", record["error"]
        assert record["prompt_mode"] == "token_ids" and record["one_sequence"] is True
        assert len(agent_requests) == 2
        assert all(isinstance(request.get("prompt"), list) for request in agent_requests)
        assert inside and all(len(text) <= 16_000 for text in inside)
        (handed_back,) = [m["content"] for m in record["messages"] if m["role"] == "tool"]
        assert len(handed_back) <= 2000 and "Ask the sub-agent for a shorter" in handed_back
        # The reply was read here, as written: the scripted template's two line
        # breaks before the text are part of what the sub-agent handed back.
        assert record["tool_results"] == {
            "cut": 1,
            "cuts": [{"tool": "task_agent", "chars": 12002}],
        }
        (call,) = record["subagents"]["tool_log"]
        assert call["tool"] == LIST_ENTRIES and call["ok"] is True
        assert call["result"] == listing[:500]
        # Nothing was noted of the sub-agent's replies; what is, is kept here
        # and not among the session's events.
        assert record["subagents"]["events"] == [] and record["prefix_events"] == []
