"""A verifier command: a program of the operator's that checks a rollout from outside.

The program is started on the machine the rollout runs on.  Every program here
is a harmless one the test wrote under its own temporary directory, which is
all the suite lets the rollout package start besides the container runtime
(``conftest.py``).

The first classes need no container.  The last runs a rollout in a container
made for the test and has the program read the stopped container with the
runtime's own copy, as a grader does.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import time
from typing import Any

import pytest

from tests._scripted_chat_server import ScriptedChatServer
from tests.test_rollout_container import IMAGE, SMALL, _listed, needs_container, note_containers
from tests.test_rollout_episode import SAMPLED, SCRIPT, _DirSandbox
from turnstone.core import config as config_module
from turnstone.rollout import cli
from turnstone.rollout.container import ContainerSpec
from turnstone.rollout.episode import RolloutConfig, Task, run_episode
from turnstone.rollout.settings import SetupError
from turnstone.rollout.sidecar import Sidecar
from turnstone.rollout.verification import (
    check_verifier_command,
    fill_verifier_command,
    run_verifier_command,
)

NL = chr(10)


@pytest.fixture(autouse=True)
def _no_configuration_file(monkeypatch):
    monkeypatch.setattr(config_module, "_cache", {})


def _program(tmp_path: Any, body: str, name: str = "verifier") -> str:
    """A program of the test's own: *body* is run by this interpreter."""
    path = tmp_path / name
    path.write_text("#!" + sys.executable + NL + "import json, os, sys, time" + NL + body + NL)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


def _says(tmp_path: Any, verdict: Any) -> str:
    return _program(tmp_path, f"print(json.dumps({verdict!r}))")


class TestTheArguments:
    def test_the_placeholders_are_replaced_inside_each_argument(self):
        argv = [
            "grade",
            "tasks.jsonl",
            "{key}",
            "{container}",
            "--state={sidecar}:/work",
            "{other}",
        ]
        filled = fill_verifier_command(argv, container="tsgym-abc", sidecar="tsgym-def", key="t 42")
        assert filled == [
            "grade",
            "tasks.jsonl",
            "t 42",
            "tsgym-abc",
            "--state=tsgym-def:/work",
            "{other}",
        ]

    @pytest.mark.parametrize(
        "key",
        [
            "two words",
            "--help",
            "-x --timeout 0",
            "a; echo no shell",
            "$(id) `id` $HOME",
            "quote ' and \" and a line" + NL + "break",
            "",
        ],
    )
    def test_a_key_is_one_argument_whatever_it_holds(self, key):
        """Put in after the command was split: nothing in a key makes a second
        argument of it, and nothing reads it but the program."""
        filled = fill_verifier_command(
            ["grade", "--", "{key}", "--task={key}"], container="c", sidecar=None, key=key
        )
        assert filled == ["grade", "--", key, "--task=" + key]

    def test_a_key_that_holds_a_placeholders_text_is_not_filled_in_a_second_time(self):
        """The key goes in last, so the text of another placeholder in it stays
        the task's text and does not become a container's name."""
        filled = fill_verifier_command(
            ["grade", "{key}", "{container}"],
            container="tsgym-abc",
            sidecar="tsgym-def",
            key="{container} {sidecar} {key}",
        )
        assert filled == ["grade", "{container} {sidecar} {key}", "tsgym-abc"]

    def test_a_name_there_is_none_of_is_empty(self):
        assert fill_verifier_command(
            ["g", "{container}", "{sidecar}"], container=None, sidecar=None, key="k"
        ) == ["g", "", ""]

    def test_a_command_that_cannot_be_run_as_given_is_refused(self):
        with pytest.raises(SetupError, match="names no program"):
            check_verifier_command([], has_sidecar=False)
        with pytest.raises(SetupError, match="the task has no sidecar"):
            check_verifier_command(["grade", "{sidecar}"], has_sidecar=False)
        check_verifier_command(["grade", "{sidecar}"], has_sidecar=True)
        check_verifier_command(["grade", "{container}"], has_sidecar=False)

    @pytest.mark.parametrize(
        "argv",
        [
            ["{key}", "tasks.jsonl"],
            ["./graders/{key}.sh", "{container}"],
            ["{container}"],
            ["/opt/{sidecar}/grade", "{key}"],
        ],
    )
    def test_the_programs_own_name_takes_no_placeholder(self, argv):
        """A task's key must not choose what is started on this machine,
        whichever way the command is given: the guard was the command line's
        alone, and a command given from Python was started as the key said."""
        with pytest.raises(SetupError, match="placeholder in the program's own name") as refusal:
            check_verifier_command(argv, has_sidecar=True)
        assert f"({argv[0]})" in str(refusal.value)
        # And nothing is ever filled in there.
        filled = fill_verifier_command(argv, container="tsgym-abc", sidecar="tsgym-def", key="k")
        assert filled[0] == argv[0]


class TestRunningOne:
    def test_what_it_prints_is_the_result(self, tmp_path):
        body = "print(json.dumps({'verdict': 'pass', 'tests': 12, 'arguments': sys.argv[1:]}))"
        body += NL + "print('twelve tests ran', file=sys.stderr)"
        program = _program(tmp_path, body)
        result, failure, said = run_verifier_command([program, "one", "two words"], 30)
        assert failure is None
        assert result == {"verdict": "pass", "tests": 12, "arguments": ["one", "two words"]}
        assert said["argv"] == [program, "one", "two words"] and said["exit_code"] == 0
        assert said["timed_out"] is False and said["stderr"] == "twelve tests ran" + NL
        # The output was the verdict, so it is not kept a second time.
        assert "stdout" not in said and said["stdout_bytes"] > 0

    @pytest.mark.parametrize(
        ("printed", "why"),
        [
            ("", "the verifier command did not print one JSON object"),
            ("print('all tests passed')", "the verifier command did not print one JSON object"),
            ("print(json.dumps(['pass']))", "the verifier command did not print one JSON object"),
            (
                "print(json.dumps({'verdict': 'pass'})); print(json.dumps({'verdict': 'fail'}))",
                "the verifier command did not print one JSON object",
            ),
            (
                "print(json.dumps({'verdict': None, 'reason': 'the tests could not be run'}))",
                "the JSON object the verifier command printed holds no verdict",
            ),
            (
                "print(json.dumps({'passed': True}))",
                "the JSON object the verifier command printed holds no verdict",
            ),
        ],
    )
    def test_anything_else_is_no_verdict(self, tmp_path, printed, why):
        result, failure, said = run_verifier_command([_program(tmp_path, printed)], 30)
        assert result is None and failure == why
        # What it printed instead is kept, for whoever looks into it.
        assert isinstance(said["stdout"], str) and said["exit_code"] == 0

    def test_a_status_other_than_zero_is_no_verdict_whatever_it_printed(self, tmp_path):
        body = "print(json.dumps({'verdict': 'pass'})); sys.exit(3)"
        result, failure, said = run_verifier_command([_program(tmp_path, body)], 30)
        assert result is None and failure == "the verifier command ended with exit code 3"
        assert said["exit_code"] == 3 and json.loads(said["stdout"]) == {"verdict": "pass"}

    def test_it_is_stopped_at_its_time_limit_with_what_it_started(self, tmp_path):
        marker = tmp_path / "still-running"
        child = f"import time; time.sleep(3); open({str(marker)!r}, 'w').close()"
        body = f"import subprocess; subprocess.Popen([sys.executable, '-c', {child!r}])"
        body += NL + "print('started', flush=True); time.sleep(30)"
        started = time.monotonic()
        result, failure, said = run_verifier_command([_program(tmp_path, body)], 1)
        assert result is None and failure == "the verifier command did not end within 1 s"
        assert said["timed_out"] is True and said["stdout"] == "started" + NL
        assert time.monotonic() - started < 10
        time.sleep(4)
        assert not marker.exists()

    def test_a_program_that_is_not_there(self, tmp_path):
        result, failure, said = run_verifier_command([str(tmp_path / "missing")], 30)
        assert result is None and failure is not None
        assert failure.startswith("the verifier command could not be started (FileNotFoundError")
        assert said["exit_code"] is None

    def test_only_the_beginning_and_the_end_of_what_it_wrote_are_kept(self, tmp_path):
        body = "for n in range(5000): print('line', n, file=sys.stderr)" + NL + "sys.exit(1)"
        _result, _failure, said = run_verifier_command([_program(tmp_path, body)], 30)
        kept = said["stderr"].split(NL)
        assert kept[0] == "line 0" and kept[-2] == "line 4999"
        assert any("bytes left out" in line for line in kept)
        assert len(said["stderr"]) < 9000 and said["stderr_bytes"] == 48890

    def test_more_output_than_a_verdict_can_be_is_refused(self, tmp_path):
        body = "sys.stdout.write('{\"verdict\": \"' + 'x' * 5_000_000 + '\"}')"
        result, failure, _said = run_verifier_command([_program(tmp_path, body)], 30)
        assert result is None and failure == "the verifier command printed more than 4194304 bytes"


def _checked(server: ScriptedChatServer, command: list[str], **more: Any) -> dict[str, Any]:
    config = RolloutConfig(base_url=server.base_url, model="scripted", **SAMPLED)
    box = more.pop("sandbox", None) or _DirSandbox()
    return run_episode(
        Task(prompt="make a file", key="task 7", setup_files={"seed.txt": "seeded"}),
        config,
        sandbox_factory=lambda _t, _c: box,
        allow_host_execution=True,
        verifier_command=command,
        **more,
    )


class TestAfterARollout:
    def test_the_verdict_is_in_the_record_and_the_model_never_sees_it(self, tmp_path):
        body = (
            "print(json.dumps({'verdict': 'pass', 'key': sys.argv[1], 'container': sys.argv[2]}))"
        )
        program = _program(tmp_path, body)
        with ScriptedChatServer(SCRIPT) as server:
            record = _checked(server, [program, "{key}", "{container}"])
            sent = json.dumps(server.requests)
        checked = record["verification"]
        # The directory stand-in has no name to give.
        assert checked["result"] == {"verdict": "pass", "key": "task 7", "container": ""}
        assert checked["failure"] is None
        assert checked["program"]["argv"] == [program, "task 7", ""]
        assert record["trainable"] is True and "verdict" not in sent

    def test_a_verdict_that_says_fail_is_the_models_outcome(self, tmp_path):
        with ScriptedChatServer(SCRIPT) as server:
            record = _checked(server, [_says(tmp_path, {"verdict": "fail", "failed": ["test_a"]})])
        assert record["verification"]["result"]["verdict"] == "fail"
        assert record["trainable"] is True and record["problems"] == []

    def test_no_verdict_is_the_machinerys_failure_and_the_ending_stays(self, tmp_path):
        with ScriptedChatServer(SCRIPT) as server:
            record = _checked(server, [_says(tmp_path, {"verdict": None, "reason": "no image"})])
        assert record["finish_state"] == "completed" and record["finish_cause"] == "model"
        assert record["trainable"] is False and record["one_sequence"] is False
        assert record["problems"] == [
            "the check after the run failed: the JSON object the verifier command printed "
            "holds no verdict"
        ]
        program = record["verification"]["program"]
        assert json.loads(program["stdout"]) == {"verdict": None, "reason": "no image"}

    def test_its_time_limit_is_the_verifiers(self, tmp_path):
        with ScriptedChatServer(SCRIPT) as server:
            record = _checked(server, [_program(tmp_path, "time.sleep(30)")], verifier_timeout=1)
        assert record["trainable"] is False
        assert record["verification"]["failure"] == "the verifier command did not end within 1 s"

    def test_a_run_that_did_not_start_is_not_checked(self, tmp_path):
        marker = tmp_path / "ran"
        program = _program(tmp_path, f"open({str(marker)!r}, 'w').close()")
        with ScriptedChatServer(SCRIPT) as server:
            record = _checked(server, [program], sandbox=_DirSandbox(fail_start=True))
        assert record["finish_state"] == "sandbox_failed" and record["verification"] is None
        assert not marker.exists()

    def test_a_function_and_a_command_are_not_both_given(self, tmp_path):
        with ScriptedChatServer(SCRIPT) as server, pytest.raises(ValueError, match="not both"):
            _checked(server, [_says(tmp_path, {"verdict": "pass"})], verifier=lambda _c: {})

    def test_a_command_asking_for_a_sidecar_the_task_has_not_is_refused_before_anything_runs(self):
        with ScriptedChatServer(SCRIPT) as server:
            with pytest.raises(SetupError, match="the task has no sidecar"):
                _checked(server, ["grade", "{sidecar}"])
            assert server.requests == []

    def test_a_command_whose_program_is_a_placeholder_is_refused_before_anything_runs(self):
        """From Python as from the command line.  The task's key names no
        program that is there, should the refusal ever fail."""
        with ScriptedChatServer(SCRIPT) as server:
            for command in (["{key}", "tasks.jsonl"], ["./graders/{key}.sh", "{container}"]):
                with pytest.raises(SetupError, match="placeholder in the program's own name"):
                    _checked(server, command)
            assert server.requests == []


class TestTheCommandLine:
    def _main(self, monkeypatch: Any, server: ScriptedChatServer, out: Any, *more: str) -> int:
        box = _DirSandbox()
        monkeypatch.setattr(
            cli,
            "run_episode",
            lambda t, c, **check: run_episode(
                t, c, sandbox_factory=lambda _t, _c: box, allow_host_execution=True, **check
            ),
        )
        return cli.main(
            ["--base-url", server.base_url, "--model", "scripted", "--prompt", "go"]
            + ["--top-k", "20", "--top-p", "0.95", "--context-window", "200000"]
            + ["--sampler-logprobs", "yes", "--key", "task 9", "--out", str(out), *more]
        )

    def test_the_command_is_split_like_a_shell_would_and_no_shell_runs_it(
        self, monkeypatch, tmp_path, capsys
    ):
        out = tmp_path / "episode.json"
        program = _program(tmp_path, "print(json.dumps({'verdict': 'pass', 'got': sys.argv[1:]}))")
        command = f"{program} 'two words' {{key}} '$HOME; echo no shell' {{container}}"
        with ScriptedChatServer(SCRIPT) as server:
            status = self._main(monkeypatch, server, out, "--verifier-command", command)
        record = json.loads(out.read_text(encoding="ascii"))
        assert status == 0, record["problems"]
        assert record["verification"]["result"]["got"] == [
            "two words",
            "task 9",
            "$HOME; echo no shell",
            "",
        ]
        assert "check after the run:" in capsys.readouterr().out

    def test_a_key_that_begins_with_a_dash_reaches_the_program_as_an_argument(
        self, monkeypatch, tmp_path, capsys
    ):
        """A program that takes options reads such a key as one, unless the
        command puts ``--`` before it, as the guide's example does."""
        out = tmp_path / "episode.json"
        body = "import argparse" + NL + "parser = argparse.ArgumentParser()" + NL
        body += "parser.add_argument('key'); parser.add_argument('container')" + NL
        body += "parser.add_argument('--timeout', type=int, default=300)" + NL
        body += "said = parser.parse_args()" + NL
        body += "print(json.dumps({'verdict': 'pass', 'key': said.key, 'timeout': said.timeout}))"
        program = _program(tmp_path, body)
        box = _DirSandbox()
        monkeypatch.setattr(
            cli,
            "run_episode",
            lambda t, c, **check: run_episode(
                t, c, sandbox_factory=lambda _t, _c: box, allow_host_execution=True, **check
            ),
        )
        with ScriptedChatServer(SCRIPT) as server:
            status = cli.main(
                ["--base-url", server.base_url, "--model", "scripted", "--prompt", "go"]
                + ["--top-k", "20", "--top-p", "0.95", "--context-window", "200000"]
                + ["--sampler-logprobs", "yes", "--key=--timeout=0", "--out", str(out)]
                + ["--verifier-command", f"{program} -- {{key}} {{container}}x"]
            )
        record = json.loads(out.read_text(encoding="ascii"))
        assert status == 0, record["problems"]
        assert record["verification"]["result"] == {
            "verdict": "pass",
            "key": "--timeout=0",
            "timeout": 300,
        }
        capsys.readouterr()

    def test_no_verdict_is_the_status_of_a_run_that_cannot_be_trained_on(
        self, monkeypatch, tmp_path, capsys
    ):
        out = tmp_path / "episode.json"
        program = _says(tmp_path, {"verdict": None})
        with ScriptedChatServer(SCRIPT) as server:
            status = self._main(
                monkeypatch, server, out, "--verifier-command", program, "--verifier-timeout", "20"
            )
        record = json.loads(out.read_text(encoding="ascii"))
        assert status == 2 and record["trainable"] is False
        assert record["finish_state"] == "completed"
        capsys.readouterr()

    def test_what_cannot_be_a_command_is_refused(self, monkeypatch, tmp_path, capsys):
        out = tmp_path / "episode.json"
        with ScriptedChatServer(SCRIPT) as server:
            for flags in (
                ["--verifier-command", "grade 'unfinished"],
                ["--verifier-command", "  "],
                ["--verifier-timeout", "30"],
                ["--verifier-command", "grade {sidecar}"],
                # A task's key must not choose what is started on this machine.
                ["--verifier-command", "{key} tasks.jsonl"],
                ["--verifier-command", "./graders/{key}.sh {container}"],
                ["--verifier-command", "'{container}'"],
            ):
                assert self._main(monkeypatch, server, out, *flags) == 3, flags
            assert server.requests == []
        assert not out.exists()
        capsys.readouterr()

    def test_a_placeholder_in_the_programs_name_is_refused_in_words(
        self, monkeypatch, tmp_path, capsys
    ):
        out = tmp_path / "episode.json"
        with ScriptedChatServer(SCRIPT) as server:
            flags = ["--verifier-command", "./graders/{key}.sh {container}"]
            assert self._main(monkeypatch, server, out, *flags) == 3
            assert server.requests == []
        said = capsys.readouterr().err
        assert "the verifier command holds a placeholder in the program's own name" in said
        assert "(./graders/{key}.sh)" in said and "Placeholders go into the arguments" in said


@pytest.fixture(name="containers_made")
def _made(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    return note_containers(monkeypatch)


# A grader in small: it reads paths out of the stopped containers it is named
# with the runtime's own copy, running nothing inside them, and says whether
# they still run.
GRADER = """
import io, subprocess, tarfile

def runtime(*arguments):
    return subprocess.run(["docker", *arguments], capture_output=True, timeout=60)

def read(container, path):
    copied = runtime("cp", container + ":" + path, "-")
    if copied.returncode != 0:
        return None
    with tarfile.open(fileobj=io.BytesIO(copied.stdout)) as archive:
        member = archive.extractfile(archive.getmembers()[0])
        return member.read().decode()

def running(container):
    return runtime("inspect", "--format", "{{.State.Running}}", container).stdout.decode().strip()

key, container = sys.argv[1], sys.argv[2]
said = {"key": key, "running": running(container), "answer": read(container, "/testbed/answer.txt")}
if len(sys.argv) > 3:
    said["sidecar_running"] = running(sys.argv[3])
    said["sidecar_state"] = read(sys.argv[3], "/srv/state.txt")
said["verdict"] = "pass" if said["answer"] == "forty-two" + chr(10) else "fail"
print(json.dumps(said))
"""

# Commands as root over a writable root, with the work in a directory of the
# image's own file system: what is written there is still there once the
# container has stopped.
ON_THE_ROOT = {"user": "0:0", "read_only_root": False, "writable": {}, "workdir": "/testbed"}


@needs_container
class TestReadingTheStoppedContainer:
    SCRIPT: list[dict[str, Any]] = [
        {"tool_calls": [{"name": "bash", "arguments": {"command": "echo forty-two > answer.txt"}}]},
        {"content": "answered"},
    ]

    def test_the_program_reads_what_the_model_left_with_nothing_running(
        self, tmp_path, containers_made
    ):
        program = _program(tmp_path, GRADER)
        task = Task(
            prompt="answer",
            key="code-0001",
            container=ContainerSpec(image=IMAGE, memory="512m", **ON_THE_ROOT),
        )
        with ScriptedChatServer(self.SCRIPT) as server:
            record = run_episode(
                task,
                RolloutConfig(base_url=server.base_url, model="scripted", **SMALL),
                verifier_command=[program, "{key}", "{container}"],
                verifier_timeout=120,
            )
        assert record["finish_state"] == "completed", record["error"]
        checked = record["verification"]
        assert checked["failure"] is None, checked
        name = record["sandbox"]["name"]
        assert checked["result"] == {
            "key": "code-0001",
            "running": "false",
            "answer": "forty-two" + NL,
            "verdict": "pass",
        }
        assert checked["program"]["argv"] == [program, "code-0001", name]
        assert checked["container_stopped"] is True
        assert record["trainable"] is True
        # Everything is removed afterwards, as with a verifier that is a function.
        assert record["sandbox"]["removed"] is True and _listed(name) == []
        assert containers_made == [name]

    def test_it_is_given_the_stopped_sidecar_too(self, tmp_path, containers_made):
        program = _program(tmp_path, GRADER)
        task = Task(
            prompt="answer",
            key="general-0001",
            container=ContainerSpec(image=IMAGE, memory="512m", **ON_THE_ROOT),
            sidecar=Sidecar(
                container=ContainerSpec(
                    image=IMAGE, user="0:0", read_only_root=False, writable={}, memory="512m"
                ),
                files={"/srv/state.txt": b"the task's state"},
            ),
        )
        with ScriptedChatServer(self.SCRIPT) as server:
            record = run_episode(
                task,
                RolloutConfig(base_url=server.base_url, model="scripted", **SMALL),
                verifier_command=[program, "{key}", "{container}", "{sidecar}"],
                verifier_timeout=120,
            )
        assert record["finish_state"] == "completed", record["error"]
        result = record["verification"]["result"]
        assert result["verdict"] == "pass" and result["running"] == "false"
        assert result["sidecar_running"] == "false"
        assert result["sidecar_state"] == "the task's state"
        sidecar = record["sandbox"]["sidecar"]["name"]
        assert record["verification"]["program"]["argv"][3] == sidecar
        assert record["sandbox"]["removed"] is True
        assert [_listed(name) for name in containers_made] == [[], []]

    def test_a_program_that_fails_still_leaves_nothing_behind(self, tmp_path, containers_made):
        program = _program(tmp_path, "print('no verdict here'); sys.exit(2)")
        task = Task(
            prompt="answer", container=ContainerSpec(image=IMAGE, memory="512m", **ON_THE_ROOT)
        )
        with ScriptedChatServer(self.SCRIPT) as server:
            record = run_episode(
                task,
                RolloutConfig(base_url=server.base_url, model="scripted", **SMALL),
                verifier_command=[program, "{container}"],
            )
        assert record["finish_state"] == "completed" and record["trainable"] is False
        assert record["verification"]["failure"] == "the verifier command ended with exit code 2"
        assert record["verification"]["program"]["stdout"] == "no verdict here" + NL
        assert record["sandbox"]["removed"] is True and _listed(record["sandbox"]["name"]) == []
        assert os.path.exists(program)
