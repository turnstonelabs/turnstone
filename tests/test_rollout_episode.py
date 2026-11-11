"""Whole rollouts through the headless session against the scripted chat server.

No container.  Two stand-ins for the sandbox:

- ``_DirSandbox`` is a temporary directory on this machine.  Its commands run
  here, so a rollout takes it only with ``allow_host_execution=True`` and every
  script given to it holds harmless commands only (``echo``, ``ls``, ``cat``,
  ``sleep``).
- ``_NotingSandbox`` runs nothing: a command is noted and a fixed program is
  started in its place.  A test that needs a command which must not run
  anywhere (to see it refused) uses this one.

The container itself is covered in ``test_rollout_container.py``.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
from typing import Any

import pytest

from tests._scripted_chat_server import (
    ScriptedChatServer,
    fake_logprob,
    render_prompt,
)
from tests.test_exec_env import _MemoryEnvironment
from turnstone.core import config as config_module
from turnstone.core import session as session_module
from turnstone.core.exec_env import ExecutionEnvironment
from turnstone.core.tools import ENVIRONMENT_TOOL_NAMES, INTERACTIVE_TOOL_NAMES
from turnstone.rollout import cli
from turnstone.rollout.episode import (
    HostExecutionError,
    RolloutConfig,
    RolloutSession,
    Task,
    _sub_agents_own_doing,
    run_episode,
)
from turnstone.rollout.settings import SetupError
from turnstone.rollout.tokens import (
    generated_positions,
    per_call_items,
    rebuild_final,
    rebuild_prompts,
    verify_record,
)
from turnstone.rollout.toolset import HOST_SIDE_TOOLS, ROLLOUT_TOOL_NAMES

NL = chr(10)
TAB = chr(9)


class _DirEnvironment(ExecutionEnvironment):
    """The host, with commands and relative paths rooted in one directory.

    What it is given runs on this machine: harmless commands only."""

    def __init__(self, root: str) -> None:
        self.root = root

    def getcwd(self) -> str:
        return self.root

    def abspath(self, path: str) -> str:
        return os.path.normpath(os.path.join(self.root, path))

    def realpath(self, path: str) -> str:
        return os.path.realpath(self.abspath(path))

    def exists(self, path: str) -> bool:
        return os.path.exists(self.abspath(path))

    def spawn(self, command: str, **kwargs: Any) -> Any:
        return super().spawn("cd " + shlex.quote(self.root) + NL + command, **kwargs)

    def popen_search(self, args: list[str], *, env: Any, timeout: float) -> Any:
        return subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self.root,
        )


class _DirSandbox:
    """A sandbox that is a temporary directory; records what the runner did with it."""

    def __init__(self, *, fail_start: bool = False, close_result: bool = True) -> None:
        self.root = ""
        self.fail_start = fail_start
        self.close_result = close_result
        self.closed = 0
        self.lost: str | None = None
        self.lost_callback: Any = None
        self.environment: _DirEnvironment | None = None

    def notify_lost(self, callback: Any) -> None:
        self.lost_callback = callback

    def lose(self) -> None:
        self.lost = "gone"
        self.lost_callback()

    def start(self) -> ExecutionEnvironment:
        if self.fail_start:
            raise RuntimeError("no room at the inn")
        self.root = tempfile.mkdtemp(prefix="turnstone_rollout_test_")
        self.environment = _DirEnvironment(self.root)
        return self.environment

    def put_files(self, files: dict[str, str]) -> None:
        assert self.environment is not None
        for path, content in files.items():
            self.environment.write_text(self.environment.abspath(path), content, make_parents=True)

    def close(self) -> bool:
        self.closed += 1
        if self.root:
            shutil.rmtree(self.root, ignore_errors=True)
        return self.close_result

    def describe(self) -> dict[str, Any]:
        return {"name": "directory", "removed": self.close_result and self.closed > 0}


class _NotingEnvironment(_MemoryEnvironment):
    """Not this machine and no other: a command is noted and never run.

    ``spawn`` starts a fixed local program that prints ``ran`` in place of
    whatever it was asked to start, so a test can hand it a command that must
    not run anywhere and see whether it got as far as a process.  Files live
    in a dict (``_MemoryEnvironment``)."""

    def __init__(self) -> None:
        super().__init__()
        self.commands: list[str] = []

    def command_refusal(self, command: str) -> str | None:
        return None

    def spawn(self, command: str, **_kwargs: Any) -> Any:
        self.commands.append(command)
        proc = subprocess.Popen(
            [sys.executable, "-c", "print('ran')"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        return proc, proc.pid, ""

    def kill_group(self, proc: Any, pgid: int) -> None:
        # The stand-in's own process, and only while it is still there.
        if proc.poll() is None:
            os.killpg(pgid, signal.SIGKILL)


class _NotingSandbox:
    """A sandbox whose environment runs nothing (:class:`_NotingEnvironment`)."""

    def __init__(self) -> None:
        self.environment = _NotingEnvironment()
        self.closed = 0
        self.lost: str | None = None

    def notify_lost(self, callback: Any) -> None:
        return None

    def start(self) -> ExecutionEnvironment:
        return self.environment

    def put_files(self, files: dict[str, str]) -> None:
        for path, content in files.items():
            self.environment.write_text(self.environment.abspath(path), content)

    def close(self) -> bool:
        self.closed += 1
        return True

    def describe(self) -> dict[str, Any]:
        return {"name": "noting", "removed": self.closed > 0}


class _PickyServer(ScriptedChatServer):
    """The scripted server with a template that refuses some conversations.

    A real chat template raises on a conversation it will not render, and the
    server answers such a request with a status and the template's words, on
    its chat route and on its tokenize route alike.  *refuses* is asked about
    the messages of every request that carries messages and returns the words
    to refuse with, or ``None`` to answer as scripted.  A prompt of token IDs
    holds no messages and is never refused: with prompts as token IDs the
    refusal comes where the conversation is rendered, as on a real server.
    """

    def __init__(self, script: list[dict[str, Any]], *, refuses: Any, status: int = 400, **options):
        super().__init__(script, **options)
        self._refuses = refuses
        self._refusal_status = status
        # The route of every request that was refused, in order.
        self.refused: list[str] = []

    def _refused(self, handler: Any, body: dict[str, Any], route: str) -> bool:
        messages = body.get("messages")
        words = self._refuses(messages) if isinstance(messages, list) else None
        if words is None:
            return False
        with self._lock:
            self.refused.append(route)
        data = json.dumps({"error": {"message": words, "type": "BadRequestError"}}).encode("utf-8")
        handler.send_response(self._refusal_status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(data)))
        handler.send_header("Connection", "close")
        handler.end_headers()
        handler.wfile.write(data)
        handler.close_connection = True
        return True

    def _handle_tokenize(self, handler: Any, body: dict[str, Any]) -> None:
        if not self._refused(handler, body, "tokenize"):
            super()._handle_tokenize(handler, body)

    def _handle_chat(self, handler: Any, body: dict[str, Any]) -> None:
        if not self._refused(handler, body, "chat"):
            super()._handle_chat(handler, body)


def _text(message: dict[str, Any]) -> str:
    content = message.get("content")
    return content if isinstance(content, str) else ""


NO_USER_QUERY = "No user query found in messages."


def _no_user_query(messages: list[dict[str, Any]]) -> str | None:
    """What a chat template says of a conversation in which every user message
    is a tool-response block: it finds nothing the user asked."""
    for message in messages:
        text = _text(message).strip()
        tool_response = text.startswith("<tool_response>") and text.endswith("</tool_response>")
        if message.get("role") == "user" and not tool_response:
            return None
    return NO_USER_QUERY


def _status_error(status: int, message: str = "refused", body: Any = None) -> Any:
    """What the SDK client raises for an answer with *status*."""
    import httpx2
    import openai

    request = httpx2.Request("POST", "http://server.invalid/v1/chat/completions")
    said = {"message": message} if body is None else body
    response = httpx2.Response(status, request=request, json={"error": said})
    kinds = {
        400: openai.BadRequestError,
        401: openai.AuthenticationError,
        403: openai.PermissionDeniedError,
        404: openai.NotFoundError,
        409: openai.ConflictError,
        422: openai.UnprocessableEntityError,
        429: openai.RateLimitError,
    }
    kind = kinds.get(status, openai.InternalServerError if status >= 500 else openai.APIStatusError)
    return kind(f"Error code: {status} - {message}", response=response, body=said)


SCRIPT: list[dict[str, Any]] = [
    {
        "reasoning": "I should write the file.",
        "tool_calls": [
            {"name": "write_file", "arguments": {"path": "out/a.txt", "content": "hello"}}
        ],
    },
    {
        "reasoning": "Now look at it.",
        "content": "Checking.",
        "tool_calls": [
            {"name": "bash", "arguments": {"command": "ls out; cat out/a.txt; cat seed.txt"}},
            {"name": "read_file", "arguments": {"path": "out/a.txt"}},
        ],
    },
    {"reasoning": "Finished.", "content": "All done."},
]

# Sampler settings a trainer can reproduce, on a server known to report the
# sampling distribution: what a trainable rollout needs besides sound tokens.
# The context window is named so that no test asks the server for it, and is
# large enough for the framework's default reply limit to be a quarter of it.
SAMPLED: dict[str, Any] = {
    "extra_body": {"top_k": 20, "top_p": 0.95},
    "sampler_logprobs": True,
    "context_window": 200_000,
}


@pytest.fixture(autouse=True)
def _no_configuration_file(monkeypatch):
    """The limits a rollout takes from the framework come from its defaults
    here, not from whatever configuration file this machine has."""
    monkeypatch.setattr(config_module, "_cache", {})


def _run(
    server: ScriptedChatServer,
    *,
    task: Task | None = None,
    sandbox: _DirSandbox | None = None,
    **config: Any,
) -> tuple[dict[str, Any], _DirSandbox]:
    box = sandbox or _DirSandbox()
    task = task or Task(prompt="make a file", key="t1", setup_files={"seed.txt": "seeded"})
    settings: dict[str, Any] = {"base_url": server.base_url, "model": "scripted", **SAMPLED}
    settings.update(config)
    record = run_episode(
        task,
        RolloutConfig(**settings),
        sandbox_factory=lambda _t, _c: box,
        # The directory sandbox acts on this machine; the scripts are the tests' own.
        allow_host_execution=True,
    )
    return record, box


class TestEpisodeRecord:
    def test_a_finished_rollout(self):
        with ScriptedChatServer(SCRIPT) as server:
            record, box = _run(server)
            requests = list(server.requests)

        assert record["finish_state"] == "completed" and record["error"] is None
        assert record["turns"] == 3 and len(requests) == 3
        assert record["final_content"] == "All done."
        assert record["trainable"] is True and record["one_sequence"] is True
        assert record["problems"] == [] and record["warnings"] == [] and box.closed == 1
        assert record["sampling"]["requested"] == {"top_k": 20, "top_p": 0.95, "temperature": 1.0}
        assert record["finish_cause"] == "model"
        assert record["logprobs"] == {
            "sampler_logprobs": True,
            "reflects": {"temperature": True, "top_k": True, "top_p": True},
            "reason": "given",
        }
        assert record["token_id_check"] == "required"
        assert all(c["ids_checked"] for c in record["calls"])

        # Every request was streamed, asked for token data and offered the same tools:
        # the ones that act inside the sandbox, and the sub-agent tool.
        for sent in requests:
            assert sent["stream"] is True and sent["logprobs"] is True
            assert sent["return_token_ids"] is True and sent["top_k"] == 20
            assert {t["function"]["name"] for t in sent["tools"]} == ROLLOUT_TOOL_NAMES

        # The tools ran in the sandbox directory, where the setup file was.
        log = record["tool_log"]
        assert [(e["turn"], e["tool"], e["ok"]) for e in log] == [
            (0, "write_file", True),
            (1, "bash", True),
            (1, "read_file", True),
        ]
        assert log[1]["result"] == NL.join(["a.txt", "helloseeded"])
        assert log[2]["result"] == "   1" + TAB + "hello"
        assert not os.path.exists("out/a.txt")

        roles = [m["role"] for m in record["messages"]]
        assert roles == [
            "system",
            "user",
            "assistant",
            "tool",
            "assistant",
            "tool",
            "tool",
            "assistant",
        ]

    def test_the_token_record_of_a_finished_rollout(self):
        with ScriptedChatServer(SCRIPT) as server:
            record, _box = _run(server)
            requests = list(server.requests)

        tokens = record["tokens"]
        assert record["prefix_ok"] and record["prefix_breaks"] == []
        assert record["prefix_events"] == []
        assert verify_record(tokens) == []
        # The full final sequence: what the server rendered for the last call plus what
        # it generated.  No registry and no model definition were needed for this to hold.
        final = rebuild_final(tokens)
        last_prompt = render_prompt(requests[-1]["messages"], requests[-1]["tools"])
        assert final == last_prompt + tokens["calls"][-1]["token_ids"]
        assert rebuild_prompts(tokens) == [
            render_prompt(r["messages"], r["tools"]) for r in requests
        ]
        # Every generated token has its position in that sequence and its log-probability.
        positions = generated_positions(tokens)
        assert len(positions) == tokens["generated_tokens"] == record["generated_tokens"]
        assert all(final[p["position"]] == p["token_id"] for p in positions)
        for item in tokens["calls"]:
            assert item["in_final"]
            assert item["logprobs"] == [fake_logprob(t, i) for i, t in enumerate(item["token_ids"])]
        assert tokens["calls"][1]["shared"] == tokens["calls"][0]["end"]
        assert record["usage"]["completion"] == tokens["generated_tokens"]

    def test_the_generated_text_is_kept_on_request(self):
        with ScriptedChatServer(SCRIPT) as server:
            record, _box = _run(server, keep_text=True)
            plain, _box2 = _run(server)
        assert plain["text"] is None
        assert len(record["text"]) == 3 and record["text"][2].endswith("All done.<end>")

    def test_the_two_counts_of_usage_are_there_in_a_run_that_did_not_start(self):
        with ScriptedChatServer(SCRIPT) as server:
            record, _box = _run(server, sandbox=_DirSandbox(fail_start=True))
        assert record["finish_state"] == "sandbox_failed"
        assert record["usage"] == {"prompt": 0, "completion": 0}


class TestTwoFacts:
    """``trainable`` (each call is sound) and ``one_sequence`` (the prompts extend)."""

    def test_earlier_context_rewritten_by_the_server_leaves_calls_sound_but_not_one_sequence(self):
        """The session sent a stable prefix, the server rendered an earlier message
        differently, and the token check says where."""
        with ScriptedChatServer(SCRIPT, edit_history_from_call=2) as server:
            record, _box = _run(server)

        assert record["finish_state"] == "completed"
        assert not record["prefix_ok"]
        (found,) = record["tokens"]["prefix"]["breaks"]
        assert found["call"] == 2 and found["region"] == "previous_prompt"
        assert "call 2 does not extend call 1" in record["prefix_breaks"][0]
        assert [c["in_final"] for c in record["tokens"]["calls"]] == [False, False, True]
        assert record["trainable"] is True and record["one_sequence"] is False
        # A trainer cannot take the positions by accident and lose two calls...
        with pytest.raises(ValueError, match="not one sequence"):
            generated_positions(record["tokens"])
        # ...and can train every call from its own prompt.
        assert [i["call"] for i in per_call_items(record["tokens"])] == [0, 1, 2]

    TYPED: list[dict[str, Any]] = [
        {
            "reasoning": "Typed arguments.",
            "tool_calls": [
                {
                    "name": "bash",
                    "arguments": {"command": "echo one", "stop_on_error": True, "timeout": None},
                    "raw": {"stop_on_error": "True", "timeout": "None"},
                },
                {
                    "name": "read_file",
                    "arguments": {"path": "seed.txt", "offset": None, "limit": 5},
                    "raw": {"offset": "null"},
                },
            ],
        },
        {"reasoning": "Done.", "content": "Finished."},
    ]

    def test_typed_arguments_are_sent_back_as_the_model_wrote_them(self):
        """A boolean written ``True`` and a null written ``None``: the text is read
        from the recorded bytes, so the next prompt's tokens match what was generated."""
        with ScriptedChatServer(self.TYPED) as server:
            record, _box = _run(server)
            replayed = next(m for m in server.requests[1]["messages"] if m["role"] == "assistant")

        assert record["prefix_ok"] and record["one_sequence"] is True
        assert record["prefix_events"] == []
        sent = [json.loads(c["function"]["arguments"]) for c in replayed["tool_calls"]]
        assert sent == [
            {"command": "echo one", "stop_on_error": "True", "timeout": "None"},
            {"path": "seed.txt", "offset": "null", "limit": "5"},
        ]
        # The tools ran on the typed values.
        assert record["tool_log"][0]["args"] == {
            "command": "echo one",
            "stop_on_error": True,
            "timeout": None,
        }
        assert [e["ok"] for e in record["tool_log"]] == [True, True]

    def test_without_the_text_the_same_rollout_is_not_one_sequence(self):
        with ScriptedChatServer(self.TYPED) as server:
            record, _box = _run(server, argument_text="none")
        assert not record["prefix_ok"]
        assert record["trainable"] is True and record["one_sequence"] is False


class TestUntrainable:
    def _problems(self, **server_options: Any) -> dict[str, Any]:
        with ScriptedChatServer(SCRIPT, **server_options) as server:
            record, _box = _run(server)
        assert record["finish_state"] == "completed"  # the run itself is never failed by it
        return record

    def test_misaligned_log_probabilities(self):
        """One chunk of the second reply carries token IDs without log-probabilities."""
        record = self._problems(faults={1: "skip_logprobs"})
        assert record["trainable"] is False and record["one_sequence"] is False
        assert any(p.startswith("call 1:") and "one to one" in p for p in record["problems"])
        tokens = record["tokens"]
        # That call keeps its tokens and has no log-probabilities at all, so nothing
        # downstream can pair a value with the wrong token.
        assert [c["logprobs"] is None for c in tokens["calls"]] == [False, True, False]
        with pytest.raises(ValueError, match="not trainable"):
            generated_positions(tokens)

    def test_tokens_missing_against_the_servers_count(self):
        """The last call has no later prompt to be checked against; only the usage
        numbers can show that tokens are missing from it."""
        record = self._problems(faults={2: "drop_tokens"})
        assert record["prefix_ok"]  # nothing later could notice
        assert record["trainable"] is False
        (problem,) = record["problems"]
        assert problem.startswith("call 2: the server counted")

    def test_a_reply_without_usage_numbers(self):
        record = self._problems(faults={0: "no_usage"})
        assert record["trainable"] is False
        assert record["problems"] == [
            "call 0: the server reported no usage to check the token counts against"
        ]

    def test_a_value_the_recorder_cannot_read(self):
        record = self._problems(faults={1: "null_logprob"})
        assert record["turns"] == 3 and record["final_content"] == "All done."
        assert record["trainable"] is False
        assert any("call 1:" in p and "could not be read" in p for p in record["problems"])

    def test_a_server_reporting_the_models_own_log_probabilities(self):
        """Probed, not assumed: the scripted server reports its "model's own" values,
        and the requests reshape the distribution with top-k and top-p."""
        with ScriptedChatServer(SCRIPT) as server:
            record, _box = _run(server, sampler_logprobs=None)
            probes = [r for r in server.requests if r.get("top_logprobs")]
        # A control and one request per sampler setting.
        assert len(probes) == 4
        assert record["logprobs"]["reflects"] == {
            "temperature": False,
            "top_k": False,
            "top_p": False,
        }
        assert record["logprobs"]["sampler_logprobs"] is False
        assert record["trainable"] is False
        (problem,) = record["problems"]
        assert "reports the model's own log-probabilities" in problem
        assert '"top_k": 20' in problem

    def test_the_models_own_log_probabilities_are_fine_when_nothing_reshapes_them(self):
        with ScriptedChatServer(SCRIPT) as server:
            record, _box = _run(
                server, sampler_logprobs=False, extra_body={"top_k": 0, "top_p": 1.0}
            )
        assert record["trainable"] is True and record["problems"] == []

    def test_a_probed_server_that_reports_the_sampling_distribution(self):
        with ScriptedChatServer(SCRIPT, sampler_logprobs=True) as server:
            record, _box = _run(server, sampler_logprobs=None)
            probes = [r for r in server.requests if r.get("top_logprobs")]
        assert record["logprobs"]["sampler_logprobs"] is True
        assert record["trainable"] is True
        # The two cut-offs were asked over several positions, and one more
        # request listed each position's whole distribution: none contradicted
        # what the first position said.
        assert len(probes) == 5 and [p["max_tokens"] for p in probes] == [1, 24, 24, 1, 24]
        positions = record["logprobs"]["positions"]
        assert set(positions) == {"top_k", "top_p", "whole"}
        assert all(p["checked"] > 5 and p["contradicting"] == 0 for p in positions.values())

    def test_a_server_whose_later_positions_contradict_the_first_has_no_verdict(self):
        """The first generated position alone does not speak for a server that takes
        later tokens of a step by another path.  Here the first position is
        reported under the sampling distribution and the rest are the model's own."""
        with ScriptedChatServer(SCRIPT, sampler_logprobs=True, first_position_only=True) as server:
            record, _box = _run(server, sampler_logprobs=None)
        said = record["logprobs"]
        assert said["sampler_logprobs"] is None
        assert said["reflects"]["top_k"] is None and said["reflects"]["top_p"] is None
        assert said["reason"].startswith("the positions after the first contradict it")
        assert said["positions"]["top_k"]["contradicting"] > 0
        assert record["trainable"] is False
        assert any("not known which log-probabilities" in p for p in record["problems"])

    def test_no_verdict_is_not_a_pass(self):
        with ScriptedChatServer(SCRIPT, confident=True) as server:
            record, _box = _run(server, sampler_logprobs=None)
        assert record["logprobs"]["sampler_logprobs"] is None
        assert record["trainable"] is False
        assert any("not known which log-probabilities" in p for p in record["problems"])

    def test_sampler_settings_left_to_the_server(self):
        with ScriptedChatServer(SCRIPT) as server:
            record, _box = _run(server, extra_body={"top_k": 20})
        assert record["trainable"] is False
        assert record["problems"] == [
            "sampling settings left to the server's default, which the record cannot name: top_p"
        ]

    def test_the_command_reports_it_with_its_own_exit_status(self, monkeypatch, tmp_path, capsys):
        with ScriptedChatServer(SCRIPT, faults={5: "skip_logprobs"}) as server:
            box = _DirSandbox()
            real = cli.run_episode
            monkeypatch.setattr(
                cli,
                "run_episode",
                lambda t, c: real(
                    t, c, sandbox_factory=lambda _t, _c: box, allow_host_execution=True
                ),
            )
            out = tmp_path / "episode.json"
            status = cli.main(
                ["--base-url", server.base_url, "--model", "scripted", "--prompt", "go"]
                + ["--top-k", "20", "--top-p", "0.95", "--context-window", "200000"]
                + ["--out", str(out)]
            )

        assert status == 2
        printed = capsys.readouterr().out
        assert "trainable: NO" in printed and "one sequence: NO" in printed
        # The probe took the first four requests; the fault hit the session's second call.
        assert "problem: call 1:" in printed
        assert "reports the model's own log-probabilities" in printed
        assert json.loads(out.read_text())["trainable"] is False


class TestWhereTheToolsAct:
    """A rollout's commands are the model's.  They act in a sandbox, or the caller
    says in so many words that this machine is meant."""

    BASH: list[dict[str, Any]] = [
        {"tool_calls": [{"name": "bash", "arguments": {"command": "echo here"}}]},
        {"content": "done"},
    ]

    def test_a_sandbox_that_acts_on_this_machine_is_refused_before_anything_runs(self):
        box = _DirSandbox()
        started: list[str] = []
        original_start = box.start

        def start_and_watch() -> ExecutionEnvironment:
            environment = original_start()
            environment.spawn = (  # type: ignore[method-assign]
                lambda command, **_kwargs: started.append(command)
            )
            return environment

        box.start = start_and_watch  # type: ignore[method-assign]
        with ScriptedChatServer(self.BASH) as server:
            config = RolloutConfig(base_url=server.base_url, model="scripted", **SAMPLED)
            with pytest.raises(HostExecutionError, match="acts on the machine the rollout runs on"):
                run_episode(Task(prompt="go"), config, sandbox_factory=lambda _t, _c: box)
            assert server.requests == []
        # Nothing was asked of the model, nothing was started, and the sandbox is gone.
        assert started == [] and box.closed == 1

    def test_with_the_callers_say_it_runs_and_the_record_says_where(self):
        with ScriptedChatServer(self.BASH) as server:
            record, _box = _run(server)
        assert record["tool_log"][0]["result"] == "here"
        assert record["acts_on_host"] is True

    def test_a_sandbox_elsewhere_needs_no_say(self):
        box = _NotingSandbox()
        with ScriptedChatServer(self.BASH) as server:
            config = RolloutConfig(base_url=server.base_url, model="scripted", **SAMPLED)
            record = run_episode(Task(prompt="go"), config, sandbox_factory=lambda _t, _c: box)
        assert record["acts_on_host"] is False
        assert box.environment.commands == ["echo here"]
        assert record["tool_log"][0]["result"] == "ran"  # the stand-in's, not the command's

    def test_the_session_itself_refuses_too(self):
        """Whoever builds the session, not only ``run_episode``."""
        for environment in (None, _DirEnvironment("/nowhere")):
            with pytest.raises(HostExecutionError):
                RolloutSession(
                    client=None,
                    model="m",
                    max_subagents=1,
                    last_call_tokens=lambda: None,
                    exec_env=environment,
                )

    def test_an_environment_that_does_not_say_is_taken_to_act_here(self):
        class Silent:
            pass

        with pytest.raises(HostExecutionError):
            RolloutSession(
                client=None,
                model="m",
                max_subagents=1,
                last_call_tokens=lambda: None,
                exec_env=Silent(),
            )


class TestEndings:
    def test_the_turn_limit(self):
        script = [
            {"tool_calls": [{"name": "bash", "arguments": {"command": f"echo {i}"}}]}
            for i in range(6)
        ]
        with ScriptedChatServer(script) as server:
            record, _box = _run(server, task=Task(prompt="loop", max_turns=2))
        assert record["finish_state"] == "max_turns"
        assert record["turns"] == 2 and len(record["tool_log"]) == 2
        assert record["prefix_ok"] and record["trainable"]

    def test_a_reply_said_to_hold_tool_calls_that_holds_none_ends_the_models_turn(self):
        """A server that reads tool calls itself can say a reply holds some and
        hand over none: the model began a call its reader made nothing of.  The
        model wrote that, so it must not end the run as the machinery's."""
        script = [
            {"tool_calls": [{"name": "bash", "arguments": {"command": "echo one"}}]},
            {"content": "Now the second.", "finish_reason": "tool_calls"},
            {"content": "never asked"},
        ]
        with ScriptedChatServer(script) as server:
            record, _box = _run(server)
            requests = len(server.requests)
        assert requests == 2 and record["turns"] == 2
        assert record["calls"][-1]["finish_reason"] == "tool_calls"
        assert not record["messages"][-1].get("tool_calls")
        assert record["finish_state"] == "completed" and record["finish_cause"] == "model"
        assert record["trainable"] is True and record["final_content"] == "Now the second."

    def test_a_finish_reason_the_harness_does_not_know_is_not_the_models_outcome(self):
        """A reason only the server can give, such as a filter of its own."""
        script = [{"content": "Cut by the server.", "finish_reason": "content_filter"}]
        with ScriptedChatServer(script) as server:
            record, _box = _run(server)
        assert record["finish_state"] == "abnormal_reply"
        assert record["finish_cause"] == "machinery" and record["trainable"] is False
        (problem,) = record["problems"]
        assert problem.endswith("(finish reason 'content_filter')")

    def test_a_reply_cut_at_the_token_limit_ends_the_rollout_there(self):
        """Its tool call does not run and nothing follows: sent back, the server would
        close what the model left open, and the prompt would no longer extend."""
        script = [
            {"reasoning": "Cut he", "tool_calls": [CUT_CALL], "finish_reason": "length"},
            {"content": "never asked"},
        ]
        with ScriptedChatServer(script) as server:
            record, _box = _run(server)
            requests = len(server.requests)
        assert record["finish_state"] == "length" and record["turns"] == 1 and requests == 1
        assert record["tool_log"] == []
        assert record["prefix_events"] == [{"event": "ended_at_length", "turn": 0}]
        assert record["trainable"] is True and record["one_sequence"] is True

    def test_the_time_limit_bounds_the_run_and_nothing_is_retried(self):
        with ScriptedChatServer(SCRIPT, delay=3.0) as server:
            record, box = _run(server, task=Task(prompt="slow", timeout=1))
            requests = len(server.requests)

        assert record["finish_state"] == "timeout"
        assert "did not finish within 1 s" in record["error"]
        assert record["turns"] == 0 and requests == 1
        assert record["seconds"] < 3 and record["teardown_seconds"] < 3
        assert box.closed == 1
        # The model's budget ran out while the server was still within its time to
        # answer: an outcome, to be scored, not a record to drop.
        assert record["timeout"]["waiting_on"] == "model"
        assert record["finish_cause"] == "model" and record["trainable"] is True

    def test_the_time_limit_passing_on_a_silent_server_is_not_the_models_outcome(self):
        with ScriptedChatServer(SCRIPT, delay=3.0) as server:
            record, _box = _run(
                server, task=Task(prompt="slow", timeout=1), server_stall_seconds=0.5
            )
        assert record["finish_state"] == "server_stalled"
        assert record["finish_cause"] == "machinery" and record["trainable"] is False
        assert record["timeout"]["server_silence_seconds"] >= 0.5
        assert record["time"]["longest_server_silence"] >= 0.5
        (problem,) = record["problems"]
        assert problem.startswith("the time limit passed while the model server was giving nothing")

    def test_the_time_limit_passing_inside_a_command_is_the_models_outcome(self):
        script = [
            {"tool_calls": [{"name": "bash", "arguments": {"command": "sleep 30"}}]},
            {"content": "never reached"},
        ]
        with ScriptedChatServer(script) as server:
            record, box = _run(server, task=Task(prompt="wait", timeout=2))
        assert record["finish_state"] == "timeout" and record["finish_cause"] == "model"
        assert record["timeout"] == {"waiting_on": "tools"}
        assert record["trainable"] is True and record["turns"] == 1
        assert record["time"]["other_seconds"] > record["time"]["model_seconds"]
        assert box.closed == 1

    def test_a_failed_request_of_the_session_is_not_the_models_outcome(self):
        """The server answers the second request with an error.  The first turn is
        sound, and the run was still cut short by something the model did not do."""
        failing = dict.fromkeys(range(1, 40), "http_500")
        with ScriptedChatServer(SCRIPT, faults=failing) as server:
            record, _box = _run(server)
        assert record["finish_state"] == "error" and record["finish_cause"] == "machinery"
        assert record["turns"] == 1
        assert record["trainable"] is False and record["one_sequence"] is False
        (problem,) = record["problems"]
        assert problem.startswith("a request of the session failed, or the harness raised: ")
        assert "InternalServerError" in problem

    READS: list[dict[str, Any]] = [
        {"tool_calls": [{"name": "read_file", "arguments": {"path": "seed.txt"}}]},
        {"content": "never reached"},
    ]

    @staticmethod
    def _will_not_render(marker: str, words: str = "the template will not render this") -> Any:
        """A template that refuses a conversation holding *marker*, wherever it is."""

        def refuses(messages: list[dict[str, Any]]) -> str | None:
            return words if any(marker in _text(m) for m in messages) else None

        return refuses

    def test_a_first_request_the_server_refuses_is_not_the_models_doing(self):
        """Nothing of the model's is in the first request: a server that refuses it
        refuses the task or the configuration."""
        task = Task(prompt="read the zzyzx file", key="t", setup_files={"seed.txt": "plain"})
        with _PickyServer(self.READS, refuses=self._will_not_render("zzyzx")) as server:
            record, _box = _run(server, task=task)
        assert record["finish_state"] == "error" and record["finish_cause"] == "machinery"
        assert record["trainable"] is False and record["turns"] == 0
        assert record["error"].startswith("BadRequestError: ")

    def test_a_prompt_the_server_finds_too_long_is_not_the_models_doing(self):
        """A run ends by itself before a request that could not fit the window it
        was told.  A server that still finds a prompt too long has another window:
        a property of the configuration."""
        task = Task(prompt="read", key="t", setup_files={"seed.txt": "a line with zzyzx in it"})
        too_long = "This model's maximum context length is 64 tokens."
        with _PickyServer(self.READS, refuses=self._will_not_render("zzyzx", too_long)) as server:
            record, _box = _run(server, task=task)
        assert record["finish_state"] == "error" and record["finish_cause"] == "machinery"
        assert record["trainable"] is False and record["turns"] == 1

    def test_a_conversation_that_fills_the_context_window_ends_as_the_models_outcome(self):
        """The model reads more than fits.  The server would refuse the next request,
        and that would look like a failed request; the run ends before it is sent."""
        script = [
            {"tool_calls": [{"name": "read_file", "arguments": {"path": "seed.txt"}}]},
            {"content": "never reached"},
        ]
        with ScriptedChatServer(script) as server:
            task = Task(prompt="read", key="t", setup_files={"seed.txt": "word " * 400})
            record, _box = _run(server, task=task, context_window=1200, max_tokens=200)
            requests = len(server.requests)
        assert requests == 1
        assert record["finish_state"] == "context_full" and record["finish_cause"] == "model"
        assert "passes the context window of 1,200" in record["error"]
        assert record["trainable"] is True and record["turns"] == 1
        assert record["tool_log"][0]["ok"] is True

    @pytest.mark.parametrize(
        ("tool", "arguments"),
        [("bash", "[1, 2]"), ("read_file", '"seed.txt"'), ("write_file", "5")],
    )
    def test_arguments_that_are_no_object_are_a_failed_call_of_the_models(self, tool, arguments):
        """Valid JSON that is no object, which a server may hand on.  The call
        fails, the model is told, and the rollout is trained on like any other."""
        script = [{"tool_calls": [{"name": tool, "arguments": arguments}]}, {"content": "ok"}]
        box = _NotingSandbox()
        with ScriptedChatServer(script) as server:
            config = RolloutConfig(base_url=server.base_url, model="scripted", **SAMPLED)
            record = run_episode(Task(prompt="go"), config, sandbox_factory=lambda _t, _c: box)
        assert record["finish_state"] == "completed" and record["finish_cause"] == "model"
        assert record["trainable"] is True and record["turns"] == 2
        assert [(e["tool"], e["ok"]) for e in record["tool_log"]] == [(tool, False)]
        assert box.environment.commands == []

    def test_an_unreachable_server_fails_once(self):
        with ScriptedChatServer(SCRIPT) as server:
            url = server.base_url
        made: list[_DirSandbox] = []

        def factory(_task: Task, _config: RolloutConfig) -> _DirSandbox:
            made.append(_DirSandbox())
            return made[-1]

        record = run_episode(
            Task(prompt="nobody home", timeout=30),
            RolloutConfig(base_url=url, model="scripted", **SAMPLED),
            sandbox_factory=factory,
            allow_host_execution=True,
        )
        assert record["finish_state"] == "error" and record["error"]
        assert record["turns"] == 0
        # One attempt, one sandbox, removed.
        assert len(made) == 1 and made[0].closed == 1

    def test_a_sandbox_that_cannot_start_gives_a_record(self):
        with ScriptedChatServer(SCRIPT) as server:
            record, box = _run(server, sandbox=_DirSandbox(fail_start=True))
            assert server.requests == []
        assert record["finish_state"] == "sandbox_failed"
        assert record["finish_cause"] == "machinery" and record["trainable"] is False
        assert record["error"] == "RuntimeError: no room at the inn"
        assert box.closed == 1

    def test_a_sandbox_that_cannot_be_removed_does_not_lose_the_record(self):
        with ScriptedChatServer(SCRIPT) as server:
            record, _box = _run(server, sandbox=_DirSandbox(close_result=False))
        # The rollout is complete and usable; the failed removal is on the record.
        assert record["finish_state"] == "completed" and record["trainable"] is True
        assert record["tokens"]["generated_tokens"] > 0
        assert record["warnings"] == ["the sandbox could not be confirmed removed"]
        assert record["sandbox"]["removed"] is False

    def test_a_lost_sandbox_ends_the_rollout_and_is_not_trainable(self):
        """The sandbox goes away during the first tool call: the run stops before the
        next model call instead of letting the model act on nothing, and what was
        recorded is not to be trained on."""
        box = _DirSandbox()
        script = [
            {"tool_calls": [{"name": "bash", "arguments": {"command": "echo one"}}]},
            {"tool_calls": [{"name": "bash", "arguments": {"command": "echo two"}}]},
            {"content": "never reached"},
        ]
        with ScriptedChatServer(script) as server:
            original_start = box.start

            def start_and_arm() -> ExecutionEnvironment:
                env = original_start()
                real_spawn = env.spawn

                def spawn_then_lose(command: str, **kwargs: Any) -> Any:
                    result = real_spawn(command, **kwargs)
                    box.lose()
                    return result

                env.spawn = spawn_then_lose  # type: ignore[method-assign]
                return env

            box.start = start_and_arm  # type: ignore[method-assign]
            record, _box = _run(server, task=Task(prompt="go"), sandbox=box)
            requests = len(server.requests)

        assert record["finish_state"] == "sandbox_lost"
        assert requests == 1 and record["turns"] == 1
        assert record["finish_cause"] == "machinery"
        assert record["trainable"] is False and record["one_sequence"] is False
        assert record["problems"] == ["the sandbox was lost during the run (gone)"]
        assert box.closed == 1


CUT_CALL = {"name": "bash", "arguments": {"command": "echo cut"}}
SPAWN = {"name": "task_agent", "arguments": {"prompt": "Write the note."}}


class TestSubAgents:
    PARENT: list[dict[str, Any]] = [
        {"reasoning": "Delegate.", "tool_calls": [SPAWN]},
        {"reasoning": "Check.", "tool_calls": [{"name": "read_file", "arguments": {"path": "n"}}]},
        {"reasoning": "Good.", "content": "The note is written."},
    ]
    AGENT: list[dict[str, Any]] = [
        {
            "reasoning": "Write it.",
            "tool_calls": [{"name": "write_file", "arguments": {"path": "n", "content": "note"}}],
        },
        {"reasoning": "Report.", "content": "wrote n"},
    ]

    def test_a_sub_agent_is_a_tool_and_its_conversation_stays_out_of_the_sample(self):
        with ScriptedChatServer(self.PARENT, agent_script=self.AGENT) as server:
            record, box = _run(server, keep_text=True)
            parent_requests = server.parent_requests
            agent_requests = server.agent_requests

        assert record["finish_state"] == "completed"
        assert record["trainable"] is True and record["one_sequence"] is True
        # The parent's call that spawned it is generated and recorded; its result is
        # text from the environment, like any tool result.
        log = record["tool_log"]
        assert [(e["tool"], e["ok"], e["result"]) for e in log] == [
            ("task_agent", True, "wrote n"),
            ("read_file", True, "   1" + TAB + "note"),
        ]
        # The token record and the prefix check hold the parent's three calls only.
        tokens = record["tokens"]
        assert record["turns"] == 3 and len(tokens["calls"]) == 3
        assert rebuild_prompts(tokens) == [
            render_prompt(r["messages"], r["tools"]) for r in parent_requests
        ]
        assert record["prefix_ok"]
        # The sub-agent's two calls are a side record, marked, with its tokens counted
        # apart: the rollout's own length is what the parent generated.
        sub = record["subagents"]
        assert sub["not_for_training"] is True
        # Counts only: the record says that no token data is kept for these calls.
        assert sub["token_data_kept"] is False
        assert all("token_ids" not in call and "logprobs" not in call for call in sub["calls"])
        assert sub["started"] == 1 and sub["model_calls"] == 2 == len(agent_requests)
        assert sub["failures"] == [] and sub["refused_at_limit"] == 0
        assert [c["finish_reason"] for c in sub["calls"]] == ["tool_calls", "stop"]
        assert sub["generated_tokens"] == sum(c["generated_tokens"] for c in sub["calls"]) > 0
        assert record["generated_tokens"] == tokens["generated_tokens"]
        assert record["generated_tokens"] == sum(len(c["token_ids"]) for c in tokens["calls"])
        assert len(sub["text"]) == 2 and "wrote n" in sub["text"][1]
        assert len(record["text"]) == 3
        # It acted in the same sandbox, with the sandbox tools only.
        offered = {t["function"]["name"] for t in agent_requests[0]["tools"]}
        assert offered == ENVIRONMENT_TOOL_NAMES
        assert box.closed == 1

    def test_spawning_one_leaves_the_parents_prompt_and_tools_as_they_were(self):
        with ScriptedChatServer(self.PARENT, agent_script=self.AGENT) as server:
            _record, _box = _run(server)
            first, second, third = server.parent_requests
        assert first["messages"][0] == second["messages"][0] == third["messages"][0]
        assert first["tools"] == second["tools"] == third["tools"]
        assert second["messages"][: len(first["messages"])] == first["messages"]

    @pytest.mark.parametrize("status", [401, 403, 404, 408, 429, 500, 503])
    def test_a_failure_of_the_machinery_is_not_scored_as_the_models(self, status):
        """The sub-agent's request fails for the server's own reasons: an error of
        its own, too many requests, no such model, no right to ask, no answer in
        time.  The model would get a failed tool result and the rollout would be
        judged on it; instead the run ends and is marked."""
        with ScriptedChatServer(
            self.PARENT, agent_script=self.AGENT, agent_status=status
        ) as server:
            record, _box = _run(server)
            parent_requests = len(server.parent_requests)

        assert record["finish_state"] == "subagent_failed"
        assert parent_requests == 1  # the parent was not asked to carry on
        assert record["trainable"] is False and record["one_sequence"] is False
        (failure,) = record["subagents"]["failures"]
        assert record["subagents"]["refused_by_server"] == 0
        assert record["problems"] == [
            f"a sub-agent failed for a reason that is not its work: {failure}"
        ]
        assert record["tool_log"][0]["ok"] is False

    @pytest.mark.parametrize("status", [400, 422])
    def test_a_conversation_the_server_refuses_is_the_sub_agents_own_doing(self, status):
        """The server answers the sub-agent's request by refusing what it holds.
        The conversation begins with the prompt the model wrote, so the model gets
        that as the failed result of its delegation and the run goes on.  Were it
        the machinery's, one tool call would take any episode out of training."""
        with ScriptedChatServer(
            self.PARENT, agent_script=self.AGENT, agent_status=status
        ) as server:
            record, _box = _run(server)
            carried_on = server.parent_requests[1]["messages"]

        said = (
            f"the model server refused the sub-agent's conversation (HTTP {status}): "
            "scripted failure"
        )
        assert record["finish_state"] == "completed" and record["finish_cause"] == "model"
        assert record["trainable"] is True and record["one_sequence"] is True
        assert record["problems"] == [] and record["turns"] == 3
        # The model was told, in the server's words, and went on from there.
        delegation = record["tool_log"][0]
        assert delegation["tool"] == "task_agent" and delegation["ok"] is False
        assert delegation["result"] == "Task error: " + said
        assert [m["content"] for m in carried_on if m["role"] == "tool"] == ["Task error: " + said]
        # The record keeps the reason, apart from a failure of the machinery.
        sub = record["subagents"]
        assert sub["failures"] == [said] and sub["refused_by_server"] == 1
        assert sub["started"] == 1 and sub["model_calls"] == 1
        assert sub["calls"][0]["complete"] is False

    TOOL_RESPONSE = NL.join(["<tool_response>", "status: ok", "</tool_response>"])

    def test_a_sub_agent_that_does_its_task_badly_is_an_ordinary_result(self):
        agent = [{"content": "I could not find anything to write."}]
        with ScriptedChatServer(self.PARENT, agent_script=agent) as server:
            record, _box = _run(server)
        assert record["finish_state"] == "completed" and record["trainable"] is True
        assert record["subagents"]["failures"] == []
        assert record["tool_log"][0]["result"] == "I could not find anything to write."
        assert record["tool_log"][1]["ok"] is False  # the parent's own read of a missing file

    def test_the_number_of_sub_agents_is_capped(self):
        parent = [
            {"tool_calls": [SPAWN, SPAWN]},
            {"tool_calls": [SPAWN]},
            {"content": "Done."},
        ]
        with ScriptedChatServer(parent, agent_script=[{"content": "ok"}]) as server:
            record, _box = _run(server, max_subagents=2)
        results = [e["result"] for e in record["tool_log"]]
        assert results[:2] == ["ok", "ok"]
        assert results[2] == (
            "Error: no sub-agent was started: this run may start 2 sub-agents and has "
            "started them all. Do the work yourself."
        )
        sub = record["subagents"]
        assert sub["started"] == 2 and sub["refused_at_limit"] == 1 and sub["model_calls"] == 2
        # A limit is the environment answering; the rollout stays trainable.
        assert record["finish_state"] == "completed" and record["trainable"] is True

    def test_a_sub_agent_whose_model_returns_nothing_is_an_ordinary_result(self):
        """Ending a sub-agent with nothing is something the model can do.  If that
        took the rollout out of training, it would be a way out of any episode."""
        with ScriptedChatServer(self.PARENT, agent_script=[{}]) as server:
            record, _box = _run(server)
            agent_requests = len(server.agent_requests)
        assert agent_requests > 1  # the session asked again before giving up
        assert record["finish_state"] == "completed" and record["finish_cause"] == "model"
        assert record["trainable"] is True and record["subagents"]["failures"] == []
        first = record["tool_log"][0]
        assert first["tool"] == "task_agent" and first["ok"] is False
        assert "Model returned no answer or tool call" in first["result"]
        assert record["turns"] == 3  # the parent carried on from the failed result

    TEXT_THEN: dict[str, Any] = {
        "content": "Writing the note now.",
        "tool_calls": [{"name": "write_file", "arguments": {"path": "n", "content": "x"}}],
    }

    def test_a_sub_agent_that_wrote_text_and_then_returns_nothing_is_an_ordinary_result(self):
        """Once a sub-agent has written text, the session wraps whatever it raises
        next to hand that text back.  The wrapper must not hide what happened: this
        is still the model's own empty reply."""
        with ScriptedChatServer(self.PARENT, agent_script=[self.TEXT_THEN, {}]) as server:
            record, _box = _run(server)
        assert record["finish_state"] == "completed" and record["finish_cause"] == "model"
        assert record["trainable"] is True and record["subagents"]["failures"] == []
        first = record["tool_log"][0]
        assert first["ok"] is False and "Model returned no answer or tool call" in first["result"]
        assert "earlier partial work" in first["result"]
        assert record["turns"] == 3  # the parent carried on

    def test_a_request_that_fails_after_the_sub_agent_wrote_text_is_the_machinerys(self):
        """The same wrapper around a failed request: still not the model's doing."""
        failing = dict.fromkeys(range(2, 40), "http_500")
        with ScriptedChatServer(
            self.PARENT, agent_script=[self.TEXT_THEN, {"content": "never"}], faults=failing
        ) as server:
            record, _box = _run(server)
            parent_requests = len(server.parent_requests)
        assert record["finish_state"] == "subagent_failed" and record["trainable"] is False
        assert parent_requests == 1
        (failure,) = record["subagents"]["failures"]
        assert "Task could not complete" in failure

    def test_whose_doing_a_sub_agents_failure_is_is_read_from_its_causes(self):
        import types

        from turnstone.core.completion_recovery import EmptyCompletionError
        from turnstone.core.model_turn import ModelContextLimitError

        def wrapped(cause: BaseException, text: str = "earlier work") -> BaseException:
            try:
                raise RuntimeError("Incomplete task; earlier partial work: " + text) from cause
            except RuntimeError as error:
                return error

        # A server's answers, as the client raises them: told apart by their text.
        class BadRequestError(Exception):
            pass

        class InternalServerError(Exception):
            pass

        empty = EmptyCompletionError(types.SimpleNamespace(native_tools_enabled=False))
        too_long = BadRequestError("This model's maximum context length is 64 tokens.")
        # Its own doing, bare or wrapped: an empty reply, a context it filled.
        for own in (empty, ModelContextLimitError("does not fit"), too_long):
            assert _sub_agents_own_doing(own) is True
            assert _sub_agents_own_doing(wrapped(own)) is True
        # The machinery's, bare or wrapped, whatever the model's earlier text says.
        for other in (InternalServerError("boom"), BadRequestError("refused"), KeyError("bug")):
            assert _sub_agents_own_doing(other) is False
            assert _sub_agents_own_doing(wrapped(other, "the context window is small")) is False

    def test_a_refusal_is_told_by_the_kind_of_error_and_its_status_never_by_a_text(self):
        from turnstone.core.completion_recovery import CompletionRecoveryError
        from turnstone.rollout.episode import _refusal, _server_words

        def wrapped(cause: BaseException) -> BaseException:
            try:
                raise RuntimeError("Task could not complete (BadRequestError).") from cause
            except RuntimeError as error:
                return error

        # Refused for what the request holds, bare or under the session's wrapper
        # or the wrapper a recovered call keeps its cause in.
        for status in (400, 422):
            refused = _status_error(status)
            assert _refusal(refused) is refused and _refusal(wrapped(refused)) is refused
            kept = CompletionRecoveryError("failed", native_tools_enabled=False, error=refused)
            assert _refusal(kept) is refused
        # Every other answer says something about the server or the way to it.
        for status in (401, 403, 404, 408, 409, 413, 429, 500, 502, 503, 504):
            other = _status_error(status)
            assert _refusal(other) is None and _refusal(wrapped(other)) is None
        # No status at all: a lost connection, a harness error, and an error whose
        # text or attributes only look the part.
        said = "Error code: 400 - No user query found in messages."
        for other in (ConnectionError(said), RuntimeError(said), KeyError("bug")):
            assert _refusal(other) is None
        named = RuntimeError(said)
        named.error = "BadRequestError: Error code: 400"  # type: ignore[attr-defined]
        named.status_code = 400  # type: ignore[attr-defined]
        assert _refusal(named) is None

        # The server's words: its message, on one line and bounded; without one,
        # what it answered with.
        assert _server_words(_status_error(400, "No user query" + NL + "  found.")) == (
            "No user query found."
        )
        long = _server_words(_status_error(400, "x" * 5000))
        assert long == "x" * 300 + "..."
        detail = {"detail": [{"loc": ["body", "messages"], "msg": "field required"}]}
        assert _server_words(_status_error(422, body=detail)) == json.dumps(detail)

    def test_a_failure_around_a_finished_sub_agent_is_the_machinerys(self, monkeypatch):
        """The session catches what goes wrong around a sub-agent and hands the model
        a task error.  When the sub-agent itself finished, that error is not its work."""
        from turnstone.core.session import ChatSession

        def answers(self: Any, *_args: Any, **_kwargs: Any) -> str:
            return "the sub-agent's answer"

        def loses_it(self: Any, item: dict[str, Any]) -> tuple[str, str]:
            self._run_agent()
            return item["call_id"], "Task error: the answer could not be published"

        with ScriptedChatServer(self.PARENT) as server:
            monkeypatch.setattr(ChatSession, "_run_agent", answers)
            monkeypatch.setattr(ChatSession, "_exec_task", loses_it)
            record, _box = _run(server)
            requests = len(server.requests)
        assert requests == 1  # the parent was not asked to carry on
        assert record["finish_state"] == "subagent_failed"
        assert record["finish_cause"] == "machinery" and record["trainable"] is False
        assert record["subagents"]["failures"] == [
            "after the sub-agent returned: Task error: the answer could not be published"
        ]

    def test_a_task_error_with_no_sub_agent_behind_it_is_the_machinerys(self, monkeypatch):
        from turnstone.core.session import ChatSession

        def never_starts(self: Any, item: dict[str, Any]) -> tuple[str, str]:
            return item["call_id"], "Task error: could not set the sub-agent up"

        with ScriptedChatServer(self.PARENT) as server:
            monkeypatch.setattr(ChatSession, "_exec_task", never_starts)
            record, _box = _run(server)
        assert record["finish_state"] == "subagent_failed" and record["trainable"] is False
        (failure,) = record["subagents"]["failures"]
        assert failure.startswith("no sub-agent ran: Task error:")

    def test_the_sub_agent_limits_are_the_frameworks(self):
        """How many turns a sub-agent may take is the framework's setting; the number
        of sub-agents is the rollout's.  There is no other limit of the harness's own."""
        with ScriptedChatServer(self.PARENT, agent_script=self.AGENT) as server:
            record, _box = _run(server)
        assert record["limits"]["subagent_max_turns"] == {
            "value": -1,
            "source": "the framework's default for tools.agent_max_turns",
        }
        assert "max_subagent_tokens" not in record["task"] and not hasattr(
            RolloutConfig, "max_subagent_tokens"
        )

    def test_the_helper_that_tells_the_calls_apart(self):
        assert session_module.inside_task_agent() is False
        token = session_module._active_task_agent_cancel_scope.set(object())  # type: ignore[arg-type]
        try:
            assert session_module.inside_task_agent() is True
        finally:
            session_module._active_task_agent_cancel_scope.reset(token)


def _reply_limit(request: dict[str, Any]) -> Any:
    return request.get("max_completion_tokens", request.get("max_tokens"))


# A number of the run's own below the least it can be: the command's flag with
# its value, the same as a setting, and what the refusal says.
BELOW_THE_LEAST: list[tuple[str, str, dict[str, Any], str]] = [
    ("--context-window", "0", {"context_window": 0}, "context_window must be at least 1, not 0"),
    ("--context-window", "-8", {"context_window": -8}, "context_window must be at least 1"),
    ("--max-tokens", "0", {"max_tokens": 0}, "max_tokens must be at least 1, not 0"),
    ("--temperature", "-1", {"temperature": -1.0}, "the temperature must be zero or above"),
    ("--max-subagents", "-1", {"max_subagents": -1}, "max_subagents must be at least 0, not -1"),
    ("--tool-timeout", "0", {"tool_timeout": 0}, "tool_timeout must be at least 1, not 0"),
    (
        "--subagent-max-turns",
        "-2",
        {"subagent_max_turns": -2},
        "subagent_max_turns must be at least -1, not -2",
    ),
    (
        "--max-output-chars",
        "-1",
        {"max_output_chars": -1},
        "max_output_chars must be at least 0, not -1",
    ),
    ("--max-read-bytes", "0", {"max_read_bytes": 0}, "max_read_bytes must be at least 1, not 0"),
    (
        "--server-stall-seconds",
        "0",
        {"server_stall_seconds": 0.0},
        "server_stall_seconds must be above zero, not 0.0",
    ),
    (
        "--server-stall-seconds",
        "-1",
        {"server_stall_seconds": -1.0},
        "server_stall_seconds must be above zero, not -1.0",
    ),
]


class TestLimits:
    """The limits come from the framework's settings unless the rollout overrides them."""

    def _with_config(self, monkeypatch: Any, config: dict[str, Any]) -> None:
        monkeypatch.setattr(config_module, "_cache", config)

    @pytest.mark.parametrize(("setting", "said"), [(c[2], c[3]) for c in BELOW_THE_LEAST])
    def test_a_number_below_the_least_it_can_be_is_refused_before_anything_runs(
        self, setting, said
    ):
        """A context window of 0 used to make a reply limit of one token, and
        the run ended as the model's ``length``, trainable: the configuration
        was scored as the model's.  The others started a container and failed,
        or ran as if nothing were wrong."""
        box = _DirSandbox()
        with ScriptedChatServer(SCRIPT) as server:
            with pytest.raises(SetupError, match=said):
                _run(server, sandbox=box, **setting)
            assert server.requests == []
        assert box.root == "" and box.closed == 0

    def test_a_verifier_given_no_time_is_refused_before_anything_runs(self):
        """The whole run used to be made first, and was then lost to a check
        that could not end within 0 s."""
        for seconds in (0, -5.0):
            box = _DirSandbox()
            config = {"base_url": "http://127.0.0.1:9/v1", "model": "scripted", **SAMPLED}
            with pytest.raises(SetupError, match="verifier_timeout must be above zero"):
                run_episode(
                    Task(prompt="p"),
                    RolloutConfig(**config),
                    sandbox_factory=lambda _t, _c, box=box: box,
                    allow_host_execution=True,
                    verifier=lambda _context: {"verdict": "pass"},
                    verifier_timeout=seconds,
                )
            assert box.root == "" and box.closed == 0

    def test_where_the_least_has_a_meaning_it_is_taken(self):
        """No sub-agent at all, the framework's automatic share of a result, a
        sub-agent without a turn limit of its own, the likeliest token."""
        with ScriptedChatServer(SCRIPT) as server:
            record, _box = _run(
                server, max_subagents=0, max_output_chars=0, subagent_max_turns=-1, temperature=0.0
            )
        assert record["finish_state"] == "completed" and record["trainable"] is True
        assert record["limits"]["subagent_max_turns"] == {"value": -1, "source": "rollout config"}
        assert record["limits"]["max_output_chars"] == {"value": 0, "source": "rollout config"}
        assert record["sampling"]["temperature"] == 0.0

    def test_the_defaults_are_the_frameworks(self):
        with ScriptedChatServer(SCRIPT) as server:
            record, _box = _run(server)
            sent = server.requests[0]
        limits = record["limits"]
        assert limits["max_tokens"] == {
            "value": 32768,
            "source": "the framework's default for model.max_tokens",
        }
        assert _reply_limit(sent) == 32768
        assert limits["tool_timeout"]["value"] == 120
        assert limits["max_output_chars"]["value"] == 0
        assert limits["context_window"] == {"value": 200_000, "source": "rollout config"}

    def test_the_models_definition_comes_before_the_configured_setting(self, monkeypatch):
        with ScriptedChatServer(SCRIPT) as server:
            self._with_config(
                monkeypatch,
                {
                    "model": {"max_tokens": 5000, "context_window": 50_000},
                    "tools": {"timeout": 45, "agent_max_turns": 7, "truncation": 9000},
                    "models": {
                        "local": {
                            "model": "scripted",
                            "base_url": server.base_url,
                            "max_tokens": 64_000,
                            "context_window": 262_144,
                        },
                        "other": {"model": "something-else", "max_tokens": 1},
                    },
                },
            )
            record, _box = _run(server, context_window=None)
            sent = server.requests[0]
        limits = record["limits"]
        assert limits["max_tokens"] == {
            "value": 64_000,
            "source": "config.toml [models.local] max_tokens",
        }
        assert _reply_limit(sent) == 64_000
        assert limits["context_window"] == {
            "value": 262_144,
            "source": "config.toml [models.local] context_window",
        }
        assert limits["tool_timeout"] == {"value": 45, "source": "config.toml [tools] timeout"}
        assert limits["subagent_max_turns"]["value"] == 7
        assert limits["max_output_chars"]["value"] == 9000

    def test_the_configured_setting_comes_before_the_default(self, monkeypatch):
        self._with_config(monkeypatch, {"model": {"max_tokens": 5000, "context_window": 50_000}})
        with ScriptedChatServer(SCRIPT) as server:
            record, _box = _run(server, context_window=None)
        assert record["limits"]["max_tokens"] == {
            "value": 5000,
            "source": "config.toml [model] max_tokens",
        }
        assert record["limits"]["context_window"]["source"] == "config.toml [model] context_window"

    def test_the_rollouts_own_value_overrides_them_all(self, monkeypatch):
        self._with_config(monkeypatch, {"model": {"max_tokens": 5000}})
        with ScriptedChatServer(SCRIPT) as server:
            record, _box = _run(server, max_tokens=777, tool_timeout=9, subagent_max_turns=3)
            sent = server.requests[0]
        assert record["limits"]["max_tokens"] == {"value": 777, "source": "rollout config"}
        assert _reply_limit(sent) == 777
        assert record["limits"]["tool_timeout"]["value"] == 9
        assert record["limits"]["subagent_max_turns"]["value"] == 3

    def test_a_context_window_nothing_names_is_refused_not_guessed(self):
        """With a guessed window a run that fills it would be scored as the model's
        outcome for what is a property of the configuration."""
        box = _DirSandbox()
        with ScriptedChatServer(SCRIPT) as server:
            with pytest.raises(SetupError, match="context window of scripted is not known"):
                _run(server, sandbox=box, context_window=None)
            assert server.requests == []
        assert box.root == "" and box.closed == 0  # nothing was started

    def test_the_reply_limit_is_at_most_a_quarter_of_the_window(self):
        """The framework's own defaults for both, where nothing names a window, are
        the same number: reserving all of it for a reply would end every rollout
        after its first turn, as the model's outcome."""
        with ScriptedChatServer(SCRIPT) as server:
            record, _box = _run(server, context_window=32768)
            sent = server.requests[0]
        assert record["limits"]["max_tokens"] == {
            "value": 8192,
            "source": "a quarter of the context window, the most the framework reserves for a "
            "reply (the framework's default for model.max_tokens says 32768)",
        }
        assert _reply_limit(sent) == 8192
        assert record["finish_state"] == "completed" and record["turns"] == 3

    def test_the_sampling_settings_are_the_frameworks_too(self, monkeypatch):
        with ScriptedChatServer(SCRIPT) as server:
            self._with_config(
                monkeypatch,
                {
                    "model": {"temperature": 0.6, "reasoning_effort": "low"},
                    "models": {
                        "local": {
                            "model": "scripted",
                            "base_url": server.base_url,
                            "temperature": 0.8,
                            "reasoning_effort": "high",
                            "capabilities": {
                                "server_compat": {
                                    "extra_body": {
                                        "top_k": 40,
                                        "chat_template_kwargs": {"enable_thinking": True},
                                    }
                                }
                            },
                        }
                    },
                },
            )
            record, _box = _run(server)
            sent = server.requests[0]
        sampling = record["sampling"]
        # The model's definition before the configured setting; the rollout's own
        # extra parameters on top of the definition's, key by key.
        assert sampling["temperature"] == 0.8 and sent["temperature"] == 0.8
        assert sampling["reasoning_effort"] == "high"
        assert sampling["extra_body"] == {
            "top_k": 20,
            "chat_template_kwargs": {"enable_thinking": True},
            "top_p": 0.95,
        }
        assert sent["top_k"] == 20 and sent["chat_template_kwargs"] == {"enable_thinking": True}
        assert sampling["sources"] == {
            "temperature": "config.toml [models.local] temperature",
            "reasoning_effort": "config.toml [models.local] reasoning_effort",
            "extra_body": "config.toml [models.local] capabilities.server_compat.extra_body, "
            "with the rollout's own on top",
        }
        # What the requests carried is what the record trains on.
        assert sampling["requested"] == {"top_k": 20, "top_p": 0.95, "temperature": 0.8}
        assert record["trainable"] is True

    def test_the_configured_sampling_setting_comes_before_the_rollouts_own(self, monkeypatch):
        self._with_config(monkeypatch, {"model": {"temperature": 0.6, "reasoning_effort": "low"}})
        with ScriptedChatServer(SCRIPT) as server:
            configured, _box = _run(server)
            overridden, _box2 = _run(server, temperature=0.3, reasoning_effort="minimal")
        assert configured["sampling"]["temperature"] == 0.6
        assert configured["sampling"]["reasoning_effort"] == "low"
        assert configured["sampling"]["sources"]["temperature"] == "config.toml [model] temperature"
        assert overridden["sampling"]["temperature"] == 0.3
        assert overridden["sampling"]["reasoning_effort"] == "minimal"
        assert set(overridden["sampling"]["sources"].values()) == {"rollout config"}

    def test_a_temperature_nothing_names_is_one(self):
        """Left to the server it would be a value the record cannot name."""
        with ScriptedChatServer(SCRIPT) as server:
            record, _box = _run(server)
            sent = server.requests[0]
        assert record["sampling"]["temperature"] == 1.0 and sent["temperature"] == 1.0
        assert record["sampling"]["reasoning_effort"] is None
        assert record["sampling"]["sources"] == {
            "temperature": "the rollout's own: nothing in the configuration names one",
            "reasoning_effort": "not set: the model's own default",
            "extra_body": "rollout config",
        }


class TestTheChecksBehindTrainable:
    def test_a_server_that_does_not_name_tokens_by_id(self):
        """Only the counts can then be compared.  That is not the check the record
        promises, so the rollout is not trainable unless the caller accepts it."""
        with ScriptedChatServer(SCRIPT, plain_names=True) as server:
            record, _box = _run(server)
            accepted, _box2 = _run(server, require_token_id_check=False)
        assert record["trainable"] is False
        assert len(record["problems"]) == 3
        assert all(
            "did not name each log-probability's token by ID" in p for p in record["problems"]
        )
        assert accepted["trainable"] is True and accepted["token_id_check"] == "not required"
        assert not any(c["ids_checked"] for c in accepted["calls"])

    def test_a_turn_the_session_holds_but_no_call_accounts_for(self, monkeypatch):
        """Every assistant turn must be one recorded call that ended normally."""
        from turnstone.rollout import episode

        real = episode._snapshot

        def one_turn_more(session: Any) -> dict[str, Any]:
            snapshot = real(session)
            snapshot["messages"].append({"role": "assistant", "content": "from nowhere"})
            return snapshot

        with ScriptedChatServer(SCRIPT) as server:
            monkeypatch.setattr(episode, "_snapshot", one_turn_more)
            record, _box = _run(server)
        assert record["trainable"] is False
        assert record["problems"] == [
            "the session holds 4 assistant turns but 3 model calls ended normally"
        ]

    def test_the_sampler_settings_are_read_off_the_requests(self, monkeypatch):
        """A setting the configuration names and the requests did not carry was not
        in force, and the record must not say it was."""
        from turnstone.rollout import recording

        real = recording._Completions.create

        def without_temperature(self: Any, **kwargs: Any) -> Any:
            kwargs.pop("temperature", None)
            return real(self, **kwargs)

        with ScriptedChatServer(SCRIPT) as server:
            monkeypatch.setattr(recording._Completions, "create", without_temperature)
            record, _box = _run(server, temperature=0.7)
            assert all("temperature" not in r for r in server.requests)
        assert "temperature" not in record["sampling"]["requested"]
        assert record["trainable"] is False
        assert record["problems"] == [
            "sampling settings left to the server's default, which the record cannot "
            "name: temperature"
        ]

    def test_a_server_that_reports_after_some_settings_only(self):
        """Temperature applied before the reported values, top-k and top-p after."""
        with ScriptedChatServer(SCRIPT, reflects=("temperature",)) as server:
            hot, _box = _run(server, sampler_logprobs=None, temperature=0.7)
            flat, _box2 = _run(
                server,
                sampler_logprobs=None,
                temperature=0.7,
                extra_body={"top_k": 0, "top_p": 1.0},
            )
        assert hot["logprobs"]["reflects"] == {"temperature": True, "top_k": False, "top_p": False}
        assert hot["logprobs"]["sampler_logprobs"] is None
        assert hot["trainable"] is False
        (problem,) = hot["problems"]
        assert "reports the model's own log-probabilities" in problem
        assert '"top_k": 20' in problem and "temperature" not in problem
        # With top-k and top-p out of the way, what is reported is what was sampled from.
        assert flat["trainable"] is True


class TestToolSet:
    def test_every_interactive_tool_is_placed(self):
        """A tool is one that acts through the execution environment, the sub-agent
        tool, or one with a stated reason for staying out of a rollout.  A new tool
        fails here until it is put in one of them."""
        assert ENVIRONMENT_TOOL_NAMES | {"task_agent"} == ROLLOUT_TOOL_NAMES
        assert set(HOST_SIDE_TOOLS).isdisjoint(ROLLOUT_TOOL_NAMES)
        assert ROLLOUT_TOOL_NAMES | set(HOST_SIDE_TOOLS) >= INTERACTIVE_TOOL_NAMES
        # ``tool_search`` is added to a session on the fly; it has no definition file.
        assert set(HOST_SIDE_TOOLS) - INTERACTIVE_TOOL_NAMES == {"tool_search"}
        assert all(reason.strip() for reason in HOST_SIDE_TOOLS.values())

    def test_the_offered_definitions_are_the_sessions_own(self):
        """Nothing is cut out of a description or a parameter list: the tools behave as
        their definitions say, and the only addition is where they run."""
        from turnstone.core.tools import INTERACTIVE_TOOLS

        own = {t["function"]["name"]: t["function"] for t in INTERACTIVE_TOOLS}
        with ScriptedChatServer([{"content": "ok"}]) as server:
            _record, box = _run(server)
            (request,) = server.requests
        for tool in request["tools"]:
            sent = tool["function"]
            assert sent["parameters"] == own[sent["name"]]["parameters"]
            assert sent["description"].startswith(own[sent["name"]]["description"])
        by_name = {t["function"]["name"]: t["function"]["description"] for t in request["tools"]}
        assert f"Commands run in {box.root}" in by_name["bash"]
        assert "kill_shell" in by_name and "run_in_background" in json.dumps(request["tools"])

    def test_a_host_side_tool_is_refused_in_a_rollout(self):
        script = [
            {
                "tool_calls": [
                    {"name": "web_fetch", "arguments": {"url": "http://example.invalid/"}},
                    {"name": "memory", "arguments": {"action": "list"}},
                    {"name": "notify", "arguments": {"message": "hello"}},
                ]
            },
            {"content": "ok"},
        ]
        with ScriptedChatServer(script) as server:
            record, _box = _run(server)
        log = record["tool_log"]
        assert [e["tool"] for e in log] == ["web_fetch", "memory", "notify"]
        assert all(e["ok"] is False for e in log)
        assert all("is not available in this session" in e["result"] for e in log)
        assert record["finish_state"] == "completed" and record["prefix_ok"]


class TestWhatEndedTheRunInWords:
    """``error`` is ``null`` where the model ended its turn by itself, and says
    what happened in every other ending."""

    LOOP: list[dict[str, Any]] = [
        {"tool_calls": [{"name": "bash", "arguments": {"command": f"echo {i}"}}]} for i in range(4)
    ]

    def _ended(self, script: list[dict[str, Any]], **options: Any) -> dict[str, Any]:
        server_options = {
            k: options.pop(k) for k in ("agent_script", "agent_status") if k in options
        }
        with ScriptedChatServer(script, **server_options) as server:
            record, _box = _run(server, **options)
        return record

    def test_a_run_the_model_ended_by_itself_has_none(self):
        record = self._ended(SCRIPT)
        assert record["finish_state"] == "completed" and record["error"] is None

    def test_the_turn_limit_and_the_reply_limit(self):
        used = self._ended(self.LOOP, task=Task(prompt="loop", max_turns=2))
        assert used["finish_state"] == "max_turns"
        assert used["error"] == "the model used all 2 of its turns"
        cut = self._ended(
            [{"reasoning": "Cut he", "tool_calls": [CUT_CALL], "finish_reason": "length"}]
        )
        assert cut["finish_state"] == "length"
        assert cut["error"] == "the last reply reached the reply limit of 32,768 tokens"

    def test_a_sub_agent_the_machinery_failed(self):
        record = self._ended(
            [{"tool_calls": [SPAWN]}], agent_script=[{"content": "x"}], agent_status=500
        )
        assert record["finish_state"] == "subagent_failed"
        (failure,) = record["subagents"]["failures"]
        assert record["error"] == failure and "InternalServerError" in failure

    def test_a_run_without_a_model_call_and_a_reply_the_server_ended_its_own_way(self):
        none = self._ended(SCRIPT, task=Task(prompt="nothing", max_turns=0))
        assert none["finish_state"] == "no_model_call"
        assert none["error"] == "the run ended before any model call"
        odd = self._ended([{"content": "Cut by the server.", "finish_reason": "content_filter"}])
        assert odd["finish_state"] == "abnormal_reply"
        assert odd["error"] == (
            "the last reply ended in a way the harness does not know "
            "(finish reason 'content_filter')"
        )


class TestCommand:
    def test_text_no_encoding_can_write_does_not_lose_the_record(
        self, monkeypatch, tmp_path, capsys
    ):
        """A tool argument can hold half of a surrogate pair.  The record is written
        after the rollout has run, so writing it must not fail on that."""
        lone = chr(0xD800)
        script = [
            {"tool_calls": [{"name": "bash", "arguments": {"command": "echo " + lone}}]},
            {"content": "ok"},
        ]
        with ScriptedChatServer(script, sampler_logprobs=True) as server:
            box = _DirSandbox()
            real = cli.run_episode
            monkeypatch.setattr(
                cli,
                "run_episode",
                lambda t, c: real(
                    t, c, sandbox_factory=lambda _t, _c: box, allow_host_execution=True
                ),
            )
            out = tmp_path / "episode.json"
            status = cli.main(
                ["--base-url", server.base_url, "--model", "scripted", "--prompt", "go"]
                + ["--top-k", "20", "--top-p", "0.95", "--context-window", "200000"]
                + ["--out", str(out)]
            )

        written = json.loads(out.read_text(encoding="ascii"))
        assert written["tool_log"][0]["args"] == {"command": "echo " + lone}
        assert status == 0, written["problems"]
        capsys.readouterr()
