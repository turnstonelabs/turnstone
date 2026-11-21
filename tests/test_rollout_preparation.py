"""A task's before-run commands: run in the episode's container before the model starts.

The first class needs no container: the sandbox is the directory stand-in of
``test_rollout_episode.py`` with a before-run step that runs nothing.  Each
command is noted, with what was there at that moment, and answered from a
script.

The rest runs the commands for real, in a container made for the test (see
``test_rollout_container.py`` for what that needs).
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

import pytest

from tests._scripted_chat_server import ScriptedChatServer, decode
from tests.test_rollout_container import (
    SMALL,
    _listed,
    _program,
    _spec,
    needs_container,
    note_containers,
)
from tests.test_rollout_episode import SCRIPT, _DirSandbox, _run
from tests.test_rollout_rules import NO_TOUCH, SHIPPED
from turnstone.core import config as config_module
from turnstone.rollout.container import Container, ContainerLostError, run_captured
from turnstone.rollout.episode import RolloutConfig, Task, run_episode
from turnstone.rollout.preparation import BeforeRunCommand, before_run_failure, run_before
from turnstone.rollout.tokens import rebuild_final
from turnstone.rollout.verification import AfterRunCommand

NL = chr(10)


@pytest.fixture(autouse=True)
def _no_configuration_file(monkeypatch):
    monkeypatch.setattr(config_module, "_cache", {})


def _answer(exit_code: int = 0, *, timed_out: bool = False, stdout: str = "") -> dict[str, Any]:
    return {
        "argv": ["scripted"],
        "user": "0:0",
        "exit_code": exit_code,
        "timed_out": timed_out,
        "seconds": 0.01,
        "stdout": stdout,
        "stderr": "",
        "stdout_bytes": len(stdout),
        "stderr_bytes": 0,
    }


class _PreparedSandbox(_DirSandbox):
    """The directory sandbox with a before-run step that starts nothing."""

    def __init__(self, answers: list[Any], server: ScriptedChatServer, pause: float = 0.0) -> None:
        super().__init__()
        self.answers = list(answers)
        self.server = server
        self.pause = pause
        # What each command was and what was there when it was asked for.
        self.asked: list[dict[str, Any]] = []
        self.kept: list[float] = []
        self.lost_cause: str | None = None

    def keep_for(self, seconds: float) -> None:
        self.kept.append(seconds)

    def before_run(self, command: BeforeRunCommand) -> dict[str, Any]:
        self.asked.append(
            {
                "argv": command.argv,
                "user": command.user,
                "files_in_place": os.path.exists(os.path.join(self.root, "seed.txt")),
                "model_requests": len(self.server.requests),
                "kept_for": list(self.kept),
            }
        )
        time.sleep(self.pause)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return dict(answer)


def _task(*commands: BeforeRunCommand, **settings: Any) -> Task:
    return Task(
        prompt="make a file",
        key="prepared",
        setup_files={"seed.txt": "seeded"},
        before_run=list(commands),
        **settings,
    )


PURGE = BeforeRunCommand(["sh", "-c", "echo purge build leftovers"], timeout=40)
WARM = BeforeRunCommand(["make", "warm"], user="1000:1000", timeout=20)


class TestBeforeTheRun:
    def test_they_run_after_the_files_and_before_any_model_call(self):
        with ScriptedChatServer(SCRIPT) as server:
            box = _PreparedSandbox([_answer(stdout="purged"), _answer()], server)
            record, _box = _run(server, task=_task(PURGE, WARM), sandbox=box)

        assert [(a["argv"], a["user"]) for a in box.asked] == [
            (["sh", "-c", "echo purge build leftovers"], "0:0"),
            (["make", "warm"], "1000:1000"),
        ]
        assert all(a["files_in_place"] and a["model_requests"] == 0 for a in box.asked)
        assert record["finish_state"] == "completed" and record["trainable"] is True
        said = record["before_run"]
        assert said["failure"] is None and said["seconds"] >= 0
        assert said["commands"] == [_answer(stdout="purged"), _answer()]
        # The task in the record says what was asked for.
        assert [c["argv"] for c in record["task"]["before_run"]] == [PURGE.argv, WARM.argv]

    def test_nothing_of_them_reaches_the_model(self):
        with ScriptedChatServer(SCRIPT) as server:
            box = _PreparedSandbox([_answer(stdout="only-the-task-knows")], server)
            record, _box = _run(server, task=_task(PURGE), sandbox=box)
            sent = json.dumps(server.requests)

        # Not the messages, not the tool log, not the token record.
        assert "only-the-task-knows" not in sent and "purge build leftovers" not in sent
        assert "only-the-task-knows" not in json.dumps(record["messages"])
        assert [e["tool"] for e in record["tool_log"]] == ["write_file", "bash", "read_file"]
        assert "only-the-task-knows" not in decode(rebuild_final(record["tokens"]))
        assert record["one_sequence"] is True

    def test_the_rules_on_the_models_calls_are_not_asked(self):
        """The same word in a command of the task's and in one of the model's: the
        rule refuses the model's, and nobody asks it about the task's."""
        script = [
            {"tool_calls": [{"name": "bash", "arguments": {"command": "touch mine"}}]},
            {"content": "ok"},
        ]
        rules = SHIPPED.with_rules([NO_TOUCH], name="no-touch")
        with ScriptedChatServer(script) as server:
            box = _PreparedSandbox([_answer()], server)
            task = _task(BeforeRunCommand(["sh", "-c", "touch prepared"]))
            record, _box = _run(server, task=task, sandbox=box, rules=rules)
        assert len(box.asked) == 1 and record["before_run"]["failure"] is None
        (event,) = record["rules"]["events"]
        assert event["rule"] == "no-touch" and event["scope"] == "parent"
        assert record["tool_log"][0]["rule"] == "no-touch"

    def test_a_command_that_fails_means_the_run_does_not_start(self):
        task = _task(PURGE, WARM, PURGE, after_run=[AfterRunCommand(["true"])])
        with ScriptedChatServer(SCRIPT) as server:
            box = _PreparedSandbox([_answer(), _answer(3), _answer()], server)
            record, _box = _run(server, task=task, sandbox=box)
            assert server.requests == []

        assert record["finish_state"] == "before_run_failed"
        assert record["finish_cause"] == "machinery" and record["trainable"] is False
        assert record["error"] == "before-run command 2 of 3 ended with exit code 3"
        assert record["problems"] == [
            "a before-run command of the task failed, so the run did not start: "
            "before-run command 2 of 3 ended with exit code 3"
        ]
        # The third was not run, and nothing is checked afterwards.
        assert len(box.asked) == 2 and len(record["before_run"]["commands"]) == 2
        assert record["before_run"]["failure"] == record["error"]
        assert record["verification"] is None
        assert record["turns"] == 0 and box.closed == 1

    def test_a_command_that_runs_out_of_time_does_too(self):
        with ScriptedChatServer(SCRIPT) as server:
            box = _PreparedSandbox([_answer(-9, timed_out=True)], server)
            record, _box = _run(server, task=_task(PURGE), sandbox=box)
            assert server.requests == []
        assert record["finish_state"] == "before_run_failed"
        assert record["error"] == "before-run command 1 of 1 did not end within 40 s"

    def test_a_sandbox_that_cannot_run_them_does_not_start_either(self):
        with ScriptedChatServer(SCRIPT) as server:
            record, box = _run(server, task=_task(PURGE), sandbox=_DirSandbox())
            assert server.requests == []
        assert record["finish_state"] == "before_run_failed"
        assert record["error"] == "the sandbox cannot run commands before the run"
        assert record["before_run"]["commands"] == [] and box.closed == 1

    def test_a_container_lost_during_them_is_not_the_models_doing(self):
        """Whatever the container's settings say about who could have ended it."""
        with ScriptedChatServer(SCRIPT) as server:
            box = _PreparedSandbox([ContainerLostError(5, "the sandbox has stopped")], server)
            box.lost, box.lost_cause = "its main process ended", "model"
            record, _box = _run(server, task=_task(PURGE), sandbox=box)
            assert server.requests == []
        assert record["finish_state"] == "before_run_failed"
        assert record["finish_cause"] == "machinery" and record["trainable"] is False
        assert record["error"].startswith(
            "before-run command 1 of 1 could not be run: ContainerLostError:"
        )

    def test_their_time_is_start_up_time_not_the_tasks(self):
        """Longer than the task's whole time limit, and the run still has all of it."""
        with ScriptedChatServer(SCRIPT) as server:
            box = _PreparedSandbox([_answer()], server, pause=1.5)
            record, _box = _run(server, task=_task(PURGE, timeout=1), sandbox=box)
        assert record["finish_state"] == "completed" and record["turns"] == 3
        assert record["before_run"]["seconds"] >= 1.5
        assert record["startup_seconds"] >= 1.5
        spent = record["time"]
        assert spent["model_seconds"] + spent["command_seconds"] + spent["harness_seconds"] < 1.5

    def test_the_sandbox_is_kept_for_as_long_as_they_may_take(self):
        with ScriptedChatServer(SCRIPT) as server:
            box = _PreparedSandbox([_answer(), _answer()], server)
            _run(server, task=_task(PURGE, WARM), sandbox=box)
        # Both time limits, what reaching the container takes, and the margin:
        # set before the first command, for the whole step.
        assert box.asked[0]["kept_for"] == [(40 + 30) + (20 + 30) + 600]

    def test_a_task_without_them_has_none(self):
        with ScriptedChatServer(SCRIPT) as server:
            record, _box = _run(server)
        assert record["before_run"] is None and record["task"]["before_run"] == []

    def test_what_stops_a_run_from_starting(self):
        assert before_run_failure(1, 2, 30, {"exit_code": 0, "timed_out": False}) is None
        assert before_run_failure(2, 2, 30, {"exit_code": 1, "timed_out": False}) == (
            "before-run command 2 of 2 ended with exit code 1"
        )
        assert before_run_failure(1, 1, 30, {"exit_code": None, "timed_out": True}) == (
            "before-run command 1 of 1 did not end within 30 s"
        )


class TestHowOutputIsKept:
    """A failing program says why in its last lines, so the bounded capture can
    keep the end of a stream as well as its beginning."""

    def test_the_beginning_and_the_end_with_the_rest_counted(self, tmp_path):
        program = _program(tmp_path, "seq 1 3000; seq 1 3000 >&2; echo the end >&2")
        done = run_captured([program], timeout=30, limit=20, tail=20)
        assert done.stdout_total == 13893 and done.stderr_total == 13893 + 8
        assert done.stdout == NL.join(str(n) for n in range(1, 11)).encode()
        out = done.shown("stdout").split(NL)
        assert out[:3] == ["1", "2", "3"] and out[-3:] == ["2999", "3000", ""]
        assert "[... 13853 bytes left out ...]" in out
        assert done.shown("stderr").endswith("3000" + NL + "the end" + NL)

    def test_output_within_the_limit_comes_back_as_it_is(self, tmp_path):
        program = _program(tmp_path, "echo all of it")
        done = run_captured([program], timeout=30, limit=100, tail=100)
        assert done.shown("stdout") == "all of it" + NL and done.stdout_tail == b""
        assert done.shown("stderr") == ""

    def test_without_a_tail_nothing_but_the_beginning_is_held(self, tmp_path):
        program = _program(tmp_path, "seq 1 3000")
        done = run_captured([program], timeout=30, limit=20)
        assert done.stdout_tail == b"" and len(done.stdout) == 20
        assert done.shown("stdout").endswith("[... 13873 bytes left out ...]" + NL)


@pytest.fixture(name="containers_made")
def _made(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    return note_containers(monkeypatch)


@pytest.fixture
def container():
    """A started container with the contained defaults: an unprivileged user,
    a read-only root, a memory-backed working directory."""
    made = Container(_spec(memory="512m"))
    try:
        yield made.start()
    finally:
        assert made.close() is True and _listed(made.name) == []


@needs_container
class TestInAContainer:
    def test_a_command_runs_as_root_in_the_working_directory(self, container):
        done = run_before(container, BeforeRunCommand(["sh", "-c", "id -u; pwd; echo said >&2"]))
        assert done["exit_code"] == 0 and done["timed_out"] is False
        assert done["stdout"] == "0" + NL + "/workspace" + NL
        assert done["stderr"] == "said" + NL
        assert done["user"] == "0:0" and done["stdout_bytes"] == 13

    def test_a_command_can_name_its_user(self, container):
        done = run_before(container, BeforeRunCommand(["id", "-u"], user="65534:65534"))
        assert done["stdout"].strip() == "65534"

    def test_its_exit_status_is_its_own(self, container):
        done = run_before(container, BeforeRunCommand(["sh", "-c", "echo no >&2; exit 3"]))
        assert done["exit_code"] == 3 and done["stderr"] == "no" + NL

    def test_it_is_stopped_at_its_time_limit(self, container):
        started = time.monotonic()
        done = run_before(container, BeforeRunCommand(["sleep", "30"], timeout=1))
        assert done["timed_out"] is True and time.monotonic() - started < 10

    def test_the_record_keeps_how_its_output_began_and_how_it_ended(self, container):
        done = run_before(
            container, BeforeRunCommand(["sh", "-c", "seq 1 20000"], max_output_chars=200)
        )
        lines = done["stdout"].split(NL)
        assert lines[:3] == ["1", "2", "3"] and lines[-2:] == ["20000", ""]
        assert "[... 108694 bytes left out ...]" in done["stdout"]
        assert len(done["stdout"]) < 300 and done["stdout_bytes"] == 108894

    def test_what_a_command_leaves_running_stays(self, container):
        started = time.monotonic()
        leave = "nohup sleep 300 > /dev/null 2>&1 & echo started"
        done = run_before(container, BeforeRunCommand(["sh", "-c", leave]))
        assert done["stdout"] == "started" + NL and time.monotonic() - started < 10
        # The container's main process is one ``sleep``; this is the other.
        count = "n=0; for f in /proc/[0-9]*/comm; do read -r c < $f; "
        count += '[ "$c" = sleep ] && n=$((n+1)); done; echo $n'
        seen = container.exec(["sh", "-c", count], timeout=30)
        assert seen.stdout.decode().strip() == "2"


@needs_container
class TestARolloutWithThem:
    SCRIPT: list[dict[str, Any]] = [
        {"tool_calls": [{"name": "bash", "arguments": {"command": "cat prepared.txt; id -u"}}]},
        {"content": "finished"},
    ]

    def test_the_model_finds_what_they_left_and_sees_nothing_of_them(self, containers_made):
        prepare = "cat seed.txt > prepared.txt; id -u >> prepared.txt; echo only-the-task-knows"
        task = Task(
            prompt="look",
            setup_files={"seed.txt": "seeded" + NL},
            before_run=[BeforeRunCommand(["sh", "-c", prepare])],
            container=_spec(memory="512m"),
        )
        with ScriptedChatServer(self.SCRIPT) as server:
            record = run_episode(
                task, RolloutConfig(base_url=server.base_url, model="scripted", **SMALL)
            )
            sent = json.dumps(server.requests)

        assert record["finish_state"] == "completed", record["error"]
        (done,) = record["before_run"]["commands"]
        assert done["exit_code"] == 0 and done["stdout"] == "only-the-task-knows" + NL
        # Root prepared it; the model's command, as its own user, read it.
        assert record["tool_log"][0]["result"] == NL.join(["seeded", "0", "65534"])
        assert "only-the-task-knows" not in sent and "prepared.txt; id -u >>" not in sent
        assert record["trainable"] is True and record["one_sequence"] is True
        assert record["startup_seconds"] >= record["before_run"]["seconds"]
        assert record["sandbox"]["removed"] is True and _listed(record["sandbox"]["name"]) == []
        assert containers_made == [record["sandbox"]["name"]]

    def test_a_failing_command_ends_the_run_before_it_starts(self, containers_made):
        task = Task(
            prompt="look",
            before_run=[
                BeforeRunCommand(["sh", "-c", "echo cannot prepare >&2; exit 7"]),
                BeforeRunCommand(["sh", "-c", "echo never > never.txt"]),
            ],
            container=_spec(memory="512m"),
        )
        with ScriptedChatServer(self.SCRIPT) as server:
            record = run_episode(
                task, RolloutConfig(base_url=server.base_url, model="scripted", **SMALL)
            )
            assert server.requests == []

        assert record["finish_state"] == "before_run_failed"
        assert record["finish_cause"] == "machinery" and record["trainable"] is False
        (done,) = record["before_run"]["commands"]
        assert done["exit_code"] == 7 and done["stderr"] == "cannot prepare" + NL
        assert record["error"] == "before-run command 1 of 2 ended with exit code 7"
        assert record["sandbox"]["removed"] is True and _listed(record["sandbox"]["name"]) == []
        assert containers_made == [record["sandbox"]["name"]]
