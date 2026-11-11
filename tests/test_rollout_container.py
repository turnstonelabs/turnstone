"""The rollout container and the execution environment that acts inside it.

The first class needs no container runtime: it drives :class:`Container` with a
scripted stand-in for the runtime's command, to pin how removal is confirmed.

The rest needs a local container runtime and a small public image
(``python:3.14-slim`` unless ``TURNSTONE_ROLLOUT_TEST_IMAGE`` names another, as
CI does by digest) and is skipped when either is missing; nothing is pulled.
With ``TURNSTONE_ROLLOUT_REQUIRE_CONTAINER=1`` a missing runtime or image fails
instead.  Only containers named ``tsgym-*`` are created, and every test checks
that its own are gone.

Some commands here are destructive on purpose: signals to every process, memory
used up, a disk filled.  They run inside a container the test made, through the
runtime's ``exec``, or not at all: for these files the suite lets the rollout
package start nothing on this machine but the runtime (``conftest.py``), so a
command line that lost its ``exec`` into the container fails before it starts.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import threading
import time
import uuid
from typing import Any
from unittest.mock import MagicMock

import pytest

import turnstone
from tests._scripted_chat_server import ScriptedChatServer
from turnstone.core import config as config_module
from turnstone.core.exec_env import ExecutionEnvironment
from turnstone.eval.core import HeadlessSession
from turnstone.rollout import container as container_module
from turnstone.rollout.container import (
    DEFAULT_ROOT_SIZE,
    NAME_PREFIX,
    RUNTIME_FILES,
    Completed,
    Container,
    ContainerEnvironment,
    ContainerError,
    ContainerLostError,
    ContainerSpec,
    run_captured,
)
from turnstone.rollout.episode import ContainerSandbox, RolloutConfig, Task, run_episode

IMAGE = os.environ.get("TURNSTONE_ROLLOUT_TEST_IMAGE", "python:3.14-slim")
NL = chr(10)
TAB = chr(9)


def _runtime(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=60)


def _image_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return _runtime("image", "inspect", IMAGE).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


# CI sets this, so that a missing runtime or image fails the lane instead of
# quietly skipping every test that needs a container.
REQUIRED = os.environ.get("TURNSTONE_ROLLOUT_REQUIRE_CONTAINER") == "1"
AVAILABLE = _image_available()

needs_container = pytest.mark.skipif(
    not AVAILABLE and not REQUIRED, reason=f"no container runtime or no local image {IMAGE}"
)


def test_the_container_tests_can_run_where_they_are_required():
    if REQUIRED:
        assert AVAILABLE, f"TURNSTONE_ROLLOUT_REQUIRE_CONTAINER is set but {IMAGE} cannot be run"


def _listed(name: str) -> list[str]:
    out = _runtime("ps", "--all", "--filter", f"name=^/{name}$", "--format", "{{.Names}}").stdout
    return [line for line in out.split(NL) if line]


def note_containers(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The name of every container the harness makes from here on, so that a
    test can check that its own are gone without looking at anybody else's."""
    names: list[str] = []
    real = Container.__init__

    def noting(self: Container, *args: Any, **kwargs: Any) -> None:
        real(self, *args, **kwargs)
        names.append(self.name)

    monkeypatch.setattr(Container, "__init__", noting)
    return names


@pytest.fixture(name="containers_made")
def _made(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    return note_containers(monkeypatch)


@pytest.fixture(autouse=True)
def _no_keeper_outlives_its_test(monkeypatch: pytest.MonkeyPatch) -> Any:
    """End the keepers a test left on duty.

    A keeper stays until its container is known to be gone, which is the point
    of it.  A test whose stand-in for the runtime never says so would leave it
    trying for minutes after the test, a process on this machine that nothing
    waits for.  The keepers are this test's own processes."""
    made: list[Any] = []
    real = container_module._Lifetime.__init__

    def noting(self: Any, *args: Any, **kwargs: Any) -> None:
        real(self, *args, **kwargs)
        made.append(self)

    monkeypatch.setattr(container_module._Lifetime, "__init__", noting)
    yield
    for keeper in made:
        keeper.cancel()


ABSENT = "echo Error: no such container >&2; exit 1"
# What a container that has started says about itself.
STARTED = ["uid=65534", "main_uid=65533", "comm=sleep", "ignored=10000", "score=-1000"]


def _scripted_runtime(tmp_path: Any, asked: str) -> str:
    """A stand-in for the runtime command.  *asked* is what it does when asked about
    the container by name (``container inspect``)."""
    path = tmp_path / "runtime"
    lines = [
        "#!/bin/sh",
        f'echo "$*" >> "{tmp_path}/calls"',
        'case "$1" in',
        "  image) echo null ;;",
        "  run) echo 0123456789abcdef ;;",
        "  exec) " + "; ".join("echo " + line for line in STARTED) + " ;;",
        f"  container) {asked} ;;",
        "  inspect) echo true 0 false ;;",
        "esac",
        "exit 0",
    ]
    path.write_text(NL.join(lines) + NL)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


class TestRemovalIsConfirmed:
    def test_an_absent_container_is_removed(self, tmp_path):
        container = Container(IMAGE, runtime=_scripted_runtime(tmp_path, ABSENT)).start()
        assert container.exists() is False
        assert container.close() is True
        assert container.removed is True and container.removal_error is None

    def test_a_question_that_fails_is_not_read_as_absent(self, tmp_path):
        runtime = _scripted_runtime(tmp_path, "echo cannot reach the service >&2; exit 1")
        container = Container(IMAGE, runtime=runtime).start()

        with pytest.raises(ContainerError, match="could not ask about the container"):
            container.exists()
        # close() does not raise and does not claim a removal it could not confirm.
        assert container.close() is False
        assert container.removed is False
        assert "cannot reach the service" in (container.removal_error or "")
        assert container.describe()["removed"] is False

    def test_a_container_that_is_still_there_is_not_removed(self, tmp_path):
        runtime = _scripted_runtime(tmp_path, "echo 0123456789ab")
        container = Container(IMAGE, runtime=runtime, confirm_timeout=0.3).start()
        assert container.exists() is True
        assert container.close() is False
        assert container.removal_error == "still there 0.3 s after removal"

    def test_after_a_start_that_timed_out_it_looks_a_second_time(self, tmp_path):
        """The runtime may finish creating the container after the first look found
        nothing.  Here it is absent once and there from then on."""
        marker = tmp_path / "asked"
        appears = f'if [ -e "{marker}" ]; then echo 0123; else touch "{marker}"; {ABSENT}; fi'
        container = Container(
            IMAGE, runtime=_scripted_runtime(tmp_path, appears), confirm_timeout=0.3
        )
        container._creation_uncertain = True
        container.settle_seconds = 0.0
        assert container.close() is False
        assert container.removal_error == "still there 0.3 s after removal"

    def test_a_missing_runtime_is_reported_not_raised(self, tmp_path):
        container = Container(IMAGE, runtime=str(tmp_path / "no-such-runtime"))
        with pytest.raises(OSError):
            container.start()
        assert container.close() is False
        assert container.removed is False and container.removal_error

    def test_the_command_that_creates_it(self):
        spec = ContainerSpec(
            image=IMAGE, cpus=0.5, memory="1g", writable={"/workspace": "64m", "/tmp": "32m"}
        )
        argv = Container(spec).run_argv()
        joined = " ".join(argv)
        assert "--network none" in joined and "--read-only" in joined
        assert "--cap-drop ALL" in joined and "--memory 1g --memory-swap 1g" in joined
        assert "--tmpfs /workspace:rw,exec,nosuid,size=64m,mode=1777" in joined
        assert "--tmpfs /tmp:rw,exec,nosuid,size=32m,mode=1777" in joined
        # Not root, an explicit size for shared memory, limits of its own instead of
        # the service's defaults, and no image fetched behind the caller's back.
        # The main process is a user of its own, which no command runs as, and
        # nothing in the container may lower its own out-of-memory score.
        assert "--user 65533:65533" in joined and "--env HOME=/workspace" in joined
        assert "--oom-score-adj 0" in joined
        # Its main process is ours, whatever entry point the image has, and the
        # runtime keeps the container when it stops.
        assert argv[-5:-1] == ["--entrypoint", "sh", IMAGE, "-c"]
        assert "exec sleep 2147483647" in argv[-1] and 'trap "" CHLD' in argv[-1]
        assert "--rm" not in argv and "--init" not in argv
        container = Container(IMAGE)
        assert container.exec_argv(["true"]) == [
            "docker",
            "exec",
            "--user",
            "65534:65534",
            "--workdir",
            "/workspace",
            container.name,
            "true",
        ]
        assert "--shm-size 64m" in joined and "--pull never" in joined
        assert "--ulimit nofile=4096:4096" in joined and "--ulimit core=0" in joined
        assert "--volume" not in joined and "--gpus" not in joined
        assert argv[argv.index("--name") + 1].startswith(NAME_PREFIX)


def _program(tmp_path: Any, body: str) -> str:
    """A harmless program of the test's own, standing in for the runtime's command."""
    path = tmp_path / "program"
    path.write_text("#!/bin/sh" + NL + body + NL)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


class TestBoundedCapture:
    def test_only_the_limit_is_kept_and_the_rest_is_counted(self, tmp_path):
        program = _program(tmp_path, "head -c 300000 /dev/zero")
        done = run_captured([program], timeout=30, limit=1000)
        assert done.returncode == 0 and len(done.stdout) == 1000
        assert done.stdout_total == 300000

    def test_input_a_command_never_reads_cannot_hold_the_call(self, tmp_path):
        """More input than a pipe holds, to a command that does not read it."""
        started = time.monotonic()
        program = _program(tmp_path, "exec sleep 30")
        done = run_captured([program], stdin_data=b"x" * 2_000_000, timeout=1, limit=10)
        assert done.timed_out and time.monotonic() - started < 10

    def test_a_command_past_its_time_is_killed(self, tmp_path):
        started = time.monotonic()
        program = _program(tmp_path, "echo begin; exec sleep 30")
        done = run_captured([program], timeout=1, limit=1000)
        assert done.timed_out and done.stdout == b"begin" + NL.encode()
        assert time.monotonic() - started < 10

    @pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")
    def test_what_a_command_leaves_running_ends_with_it(self, tmp_path):
        """A program that ends while a child of its own still holds its output
        open, as a verifier program may.  The call used to wait for that child
        without bound, whatever its time limit.  The child here is a sleep that
        ends by itself soon."""
        program = _program(tmp_path, "sleep 40 &" + NL + 'echo "$!"')
        started = time.monotonic()
        done = run_captured([program], timeout=30, limit=1000)
        took = time.monotonic() - started
        child = int(done.stdout)
        try:
            assert done.returncode == 0 and not done.timed_out and not done.stopped_early
            assert took < 10
            assert _gone_within(child, 5)
        finally:
            # Still there only where what is under test failed; this test's own.
            if _command_line(child) == ["sleep", "40"]:
                os.kill(child, signal.SIGKILL)

    @pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")
    def test_output_held_open_outside_the_commands_group_is_not_waited_for(self, tmp_path):
        """A child the program moved to a session of its own is not in its
        process group: it is the program's to end, not this call's.  The call
        returns all the same, with what the program itself wrote."""
        # The program ends only once its child has left its group.
        program = _program(tmp_path, "setsid sleep 40 &" + NL + 'sleep 0.5; echo "$!"')
        started = time.monotonic()
        done = run_captured([program], timeout=30, limit=1000)
        took = time.monotonic() - started
        child = int(done.stdout)
        try:
            assert done.returncode == 0 and not done.timed_out
            assert done.stdout == f"{child}{NL}".encode()
            assert took < 20
            assert _alive(child)
        finally:
            self._end_the_child(child)

    def test_the_time_limit_bounds_the_call_when_a_child_outlives_the_kill(self, tmp_path):
        """Past its time the command's group is killed.  A child outside that
        group lives on and holds the output open; the call still returns."""
        program = _program(tmp_path, "setsid sleep 40 &" + NL + 'echo "$!"' + NL + "exec sleep 41")
        started = time.monotonic()
        done = run_captured([program], timeout=1, limit=1000)
        took = time.monotonic() - started
        child = int(done.stdout)
        try:
            assert done.timed_out and took < 20
            assert _alive(child)
        finally:
            self._end_the_child(child)

    @staticmethod
    def _end_the_child(child: int) -> None:
        """End the sleep a test's own program left in a session of its own, by
        the id the program printed, and let the reading of its output end."""
        if _command_line(child) == ["sleep", "40"]:
            os.kill(child, signal.SIGKILL)
        for thread in threading.enumerate():
            if thread.name.endswith("(drain)"):
                thread.join(timeout=10)


class TestNothingStartsHereButTheRuntime:
    """The guard these files run under (``conftest.py``): the rollout package's
    process start takes the runtime, a test's own stand-in for it, and the
    package's lifetime keeper."""

    def test_a_command_line_without_the_runtime_in_front_is_refused(self, tmp_path):
        from tests._host_guard import DestructiveCommandError

        marker = tmp_path / "marker"
        with pytest.raises(DestructiveCommandError, match="not the one its commands go through"):
            run_captured(["touch", str(marker)], timeout=5, limit=10)
        assert not marker.exists()
        # Noted, to fail the test at its end; this one is the test's own.
        tripped = container_module.subprocess._tripped
        assert len(tripped) == 1
        tripped.clear()

    def test_an_exec_must_name_a_container_of_the_harness(self):
        from tests._host_guard import DestructiveCommandError

        with pytest.raises(DestructiveCommandError):
            run_captured(["docker", "exec", "someone-elses", "true"], timeout=5, limit=10)
        container_module.subprocess._tripped.clear()

    def test_the_environment_says_it_does_not_act_here(self):
        assert ContainerEnvironment.acts_on_host is False

    def test_what_runs_inside_starts_with_the_runtimes_exec_into_the_container(self):
        container = Container(IMAGE)
        for command in (["sh", "-c", "kill -9 -1"], ["true"]):
            argv = container.exec_argv(command, stdin=True, user="0:0")
            # The runtime's exec, its options, the container, and only then the command.
            assert argv[:2] == ["docker", "exec"]
            assert argv[-len(command) - 1 :] == [container.name, *command]
            assert not set(command) & set(argv[: -len(command) - 1])


def _left_to_the_host(environment: type, base: type = ExecutionEnvironment) -> list[str]:
    """The methods of *base* that *environment* does not replace with its own:
    plain, static and class methods and properties alike."""
    left = []
    for name, member in vars(base).items():
        kinds = (staticmethod, classmethod, property)
        if name.startswith("__") or not (callable(member) or isinstance(member, kinds)):
            continue
        owner = next(cls for cls in environment.__mro__ if name in vars(cls))
        if issubclass(base, owner):
            left.append(name)
    return sorted(left)


class TestNoMethodOfTheEnvironmentIsLeftToTheHost:
    """The base class of every execution environment is the host.  A method the
    container's environment does not replace acts on the machine the rollout
    runs on, quietly; so would a method added to the base class later."""

    def test_the_containers_environment_replaces_every_method(self):
        assert ContainerEnvironment.acts_on_host is False
        assert _left_to_the_host(ContainerEnvironment) == [], (
            "these methods of ExecutionEnvironment act on the host through a rollout: give "
            "ContainerEnvironment its own"
        )

    def test_the_check_sees_what_is_left(self):
        every = _left_to_the_host(ExecutionEnvironment)
        assert {"spawn", "kill_group", "which", "popen_search", "realpath", "write_text"} <= set(
            every
        )

        class OnlyTheFiles(ExecutionEnvironment):
            """Keeps its files elsewhere; its commands would still run here."""

            acts_on_host = False

            def read_text(self, path: str) -> str:
                return ""

            def write_text(
                self, path: str, content: str, *, append: bool = False, make_parents: bool = False
            ) -> None:
                return None

        left = _left_to_the_host(OnlyTheFiles)
        assert "spawn" in left and "read_bytes" in left
        assert "read_text" not in left and "write_text" not in left

    def test_a_method_added_to_the_base_class_later_is_noticed_whatever_its_kind(self):
        class Grown(ExecutionEnvironment):
            """The base class, some methods later."""

            def chmod(self, path: str, mode: int) -> None:
                os.chmod(path, mode)

            def chown(self, path: str, user: int) -> None:
                os.chown(path, user, -1)

            @staticmethod
            def umask() -> int:
                return 0o022

            @classmethod
            def host(cls) -> str:
                return os.uname().nodename

            @property
            def temporary_directory(self) -> str:
                return "/tmp"

            not_a_method = 3

        class Elsewhere(Grown):
            def chmod(self, path: str, mode: int) -> None:
                return None

        assert _left_to_the_host(Elsewhere, Grown) == [
            "chown",
            "host",
            "temporary_directory",
            "umask",
        ]


def _lost(
    spec: ContainerSpec,
    *,
    code: int | None = 137,
    oom: bool = False,
    asked: bool = False,
    main: str = "sleep",
    sleep_writable: bool = False,
) -> tuple[str, str]:
    """What a container of *spec* makes of its main process ending, given what
    the runtime reported.  No container is started."""
    container = Container(spec)
    container.main_process = main
    container.task_uid = 0 if spec.runs_as_root() else 65534
    container._sleep_writable = sleep_writable
    watch = container._watch
    watch._heard = True
    watch._oom_at_death = oom
    watch._asked_at_death = asked
    watch._died.set()
    return container._why_stopped({"running": False, "exit_code": code, "oom_killed": oom})


def _root(*capabilities: str, read_only_root: bool = False) -> ContainerSpec:
    return ContainerSpec(
        image=IMAGE, user="0:0", read_only_root=read_only_root, capabilities=capabilities
    )


class TestWhoseDoingALostContainerIs:
    """A main process that ended and nobody here ended it.  It is the model's
    doing only where the task's own settings let its commands end it; everywhere
    else something outside the container did, whatever the exit code."""

    PLAIN = "the container's main process ended with exit code 137"

    def test_where_the_commands_cannot_reach_it_the_end_is_the_machinerys(self):
        assert _lost(ContainerSpec(image=IMAGE)) == (self.PLAIN, "machinery")
        # Root alone, over a writable root, does not reach a main process that is
        # another user and never runs again.
        assert _lost(_root()) == (self.PLAIN, "machinery")
        assert _lost(_root("KILL", "CHOWN", "FOWNER", "SETGID"))[1] == "machinery"
        # Also when it ended as if it had crashed.
        assert _lost(_root(), code=139)[1] == "machinery"

    def test_root_that_can_become_another_user_reaches_it(self):
        why, whose = _lost(_root("SETUID"))
        assert whose == "model"
        assert why == (
            self.PLAIN + ": nothing outside the container explains it, and this task's "
            "commands run as root with the SETUID capability, so they can act as the main "
            "process's user"
        )
        # The capability is of no use to a user that is not root.
        unprivileged = ContainerSpec(image=IMAGE, capabilities=("SETUID",))
        assert _lost(unprivileged) == (self.PLAIN, "machinery")

    def test_an_end_the_runtime_was_asked_for_is_never_the_models(self):
        why, whose = _lost(_root("SETUID"), asked=True)
        assert whose == "machinery" and why.endswith("from outside the container")

    def test_an_out_of_memory_kill_of_it_takes_writing_its_score(self):
        """It is spared unless something inside made it a candidate again, which
        takes its user or the capability to pass file permissions."""
        for spec in (_root("DAC_OVERRIDE"), _root("SETUID")):
            why, whose = _lost(spec, oom=True)
            assert whose == "model" and "out-of-memory killer took it" in why
        why, whose = _lost(_root(), oom=True)
        assert whose == "machinery" and "nothing inside this container can make it" in why
        # Without the kill, passing file permissions reaches nothing.
        assert _lost(_root("DAC_OVERRIDE")) == (self.PLAIN, "machinery")

    def test_a_main_process_that_is_a_shell_is_reached_through_its_files(self):
        """Where the image offers no way to a main process that never runs again,
        it starts a program from the image's files now and then."""
        why, whose = _lost(_root(), main="shell")
        assert whose == "model" and "is a shell that starts programs" in why
        assert _lost(_root(read_only_root=True), main="shell")[1] == "machinery"
        other = ContainerSpec(image=IMAGE, read_only_root=False)
        assert _lost(other, main="shell")[1] == "machinery"
        assert _lost(other, main="shell", sleep_writable=True)[1] == "model"

    def test_a_shell_that_waits_is_reached_by_root_that_may_signal_what_it_waits_for(self):
        """The shell has to start that program again, and a shell that cannot
        start a process gives up: at the process limit, which the commands can
        hold.  Over a read-only root, where nothing can be replaced."""
        why, whose = _lost(_root("KILL", read_only_root=True), main="shell", code=2)
        assert whose == "model"
        assert why == (
            "the container's main process ended with exit code 2: nothing outside the "
            "container explains it, and this image's main process is a shell that waits for "
            "a program, which this task's commands, run as root with the KILL capability, "
            "can end; at the process limit the shell cannot start it again"
        )
        # Not a main process that is sleep itself, which starts nothing again.
        assert _lost(_root("KILL", read_only_root=True), code=2)[1] == "machinery"
        # Not root without that capability, and not another user with it.
        assert _lost(_root(read_only_root=True), main="shell", code=2)[1] == "machinery"
        unprivileged = ContainerSpec(image=IMAGE, capabilities=("KILL",))
        assert _lost(unprivileged, main="shell", code=2)[1] == "machinery"
        # A kill for memory inside, right before, does not turn it into the
        # machinery's: such commands cannot make the shell a candidate, and they
        # can end it all the same.
        why, whose = _lost(_root("KILL", read_only_root=True), main="shell", code=2, oom=True)
        assert whose == "model" and "is a shell that waits for a program" in why

    def test_a_container_the_runtime_no_longer_knows(self):
        assert _lost(_root("SETUID"), code=None) == (
            "the container is gone; the runtime gave no reason",
            "machinery",
        )


class TestWhatATasksCommandsCanChange:
    """Two settings decide whose doing it is when something a check relies on
    is not as the harness left it.  No container is started."""

    def test_the_programs_take_root_over_a_writable_root(self):
        assert Container(_root()).commands_can_change_programs() is True
        assert Container(_root(read_only_root=True)).commands_can_change_programs() is False
        unprivileged = ContainerSpec(image=IMAGE, read_only_root=False)
        assert Container(unprivileged).commands_can_change_programs() is False

    def test_their_user_takes_root_with_the_capability_for_it(self):
        """Tried in containers: root with ``SETUID`` runs a process as any
        user; root without it keeps its own, as does another user named it;
        ``SETGID`` changes groups only."""
        assert Container(_root("SETUID", read_only_root=True)).commands_can_change_users()
        assert Container(_root("cap_setuid", "KILL")).commands_can_change_users() is True
        assert Container(_root(read_only_root=True)).commands_can_change_users() is False
        every_other = ("CHOWN", "DAC_OVERRIDE", "FOWNER", "FSETID", "KILL", "SETGID", "SYS_CHROOT")
        assert Container(_root(*every_other)).commands_can_change_users() is False
        unprivileged = ContainerSpec(image=IMAGE, capabilities=("SETUID", "SETGID"))
        assert Container(unprivileged).commands_can_change_users() is False
        # What the container says about its commands' user decides, where it has.
        named = Container(ContainerSpec(image=IMAGE, user="app", capabilities=("SETUID",)))
        assert named.commands_can_change_users() is False
        named.task_uid = 0
        assert named.commands_can_change_users() is True


class TestARuntimeThatDoesNotAnswer:
    """An unanswered question is neither "stopped" nor "gone"."""

    def _container(self, tmp_path: Any, inspect: str) -> Container:
        path = tmp_path / "runtime"
        lines = ["#!/bin/sh", f'echo "$*" >> "{tmp_path}/calls"', 'case "$1" in']
        lines += ["  image) echo null ;;", "  run) echo 0123456789abcdef ;;"]
        lines += ["  exec) " + "; ".join("echo " + line for line in STARTED) + " ;;"]
        lines += [f"  inspect) {inspect} ;;", f"  container) {ABSENT} ;;", "esac", "exit 0"]
        path.write_text(NL.join(lines) + NL)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return Container(IMAGE, runtime=str(path)).start()

    def test_it_is_asked_again_before_anything_is_concluded(self, tmp_path):
        marker = tmp_path / "asked-once"
        once = f'if [ -e "{marker}" ]; then echo true 0 false; else touch "{marker}"; '
        once += "echo cannot reach the service >&2; exit 1; fi"
        container = self._container(tmp_path, once)
        assert container.state()["running"] is None  # the first, unanswered
        marker.unlink()
        container.ensure_running()  # asks, is not answered, asks again
        assert container.lost is None
        assert container.close() is True

    def test_a_runtime_that_stays_silent_is_the_machinerys_failure(self, tmp_path):
        container = self._container(tmp_path, "echo cannot reach the service >&2; exit 1")
        state = container.state()
        assert state["running"] is None and "cannot reach the service" in state["unanswered"]
        with pytest.raises(ContainerError, match="could not ask about the container"):
            container.is_running()
        with pytest.raises(ContainerLostError):
            container.ensure_running()
        assert container.lost_cause == "machinery"
        assert "the runtime did not answer" in (container.lost or "")
        assert container.stop() is False
        assert container.close() is True

    def test_a_container_the_runtime_does_not_know_is_not_running(self, tmp_path):
        container = self._container(tmp_path, ABSENT)
        assert container.state() == {"running": False, "exit_code": None, "oom_killed": None}
        with pytest.raises(ContainerLostError):
            container.ensure_running()
        assert container.lost == "the container is gone; the runtime gave no reason"
        assert container.close() is True


class TestTheKeeperOutside:
    """The process on this machine that removes a container nobody removed."""

    def test_a_removal_that_failed_is_tried_again_by_the_keeper(self, tmp_path, monkeypatch):
        """A keeper stopped before the removal is confirmed would leave a
        container whose removal failed for good."""
        allowed = tmp_path / "rm-works-now"
        gone = tmp_path / "gone"
        path = tmp_path / "runtime"
        lines = ["#!/bin/sh", f'echo "$*" >> "{tmp_path}/calls"', 'case "$1" in']
        lines += ["  image) echo null ;;", "  run) echo 0123456789abcdef ;;"]
        lines += ["  exec) " + "; ".join("echo " + line for line in STARTED) + " ;;"]
        lines += [f'  rm) if [ -e "{allowed}" ]; then touch "{gone}"; else exit 1; fi ;;']
        lines += [f'  container) if [ -e "{gone}" ]; then {ABSENT}; else echo 0123; fi ;;']
        lines += ["  inspect) echo true 0 false ;;", "esac", "exit 0"]
        path.write_text(NL.join(lines) + NL)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        container = Container(IMAGE, runtime=str(path), confirm_timeout=0.3).start()
        container.retry_removal_seconds = 1.0
        try:
            assert container.close() is False and container.removed is False
            keeper = container._lifetime._proc
            assert keeper is not None and keeper.poll() is None  # still on duty
            allowed.touch()
            deadline = time.monotonic() + 20
            while not gone.exists() and time.monotonic() < deadline:
                time.sleep(0.2)
            assert gone.exists()
            assert keeper.wait(timeout=20) == 0  # it saw the container gone and left
            removals = [c for c in (tmp_path / "calls").read_text().split(NL) if c[:2] == "rm"]
            assert all(c == f"rm --force --volumes {container.name}" for c in removals)
            # The keeper was collected just now, so its id may be another
            # group's: stopping it signals nothing.  A signal would be noted
            # here, not sent.
            signalled: list[int] = []
            with monkeypatch.context() as noting:
                noting.setattr(os, "killpg", lambda pgid, _sig: signalled.append(pgid))
                container._lifetime.cancel()
            assert signalled == [] and container._lifetime._proc is None
        finally:
            container._lifetime.cancel()

    def test_a_container_closed_while_it_starts_is_removed_once_it_exists(self, tmp_path):
        """Whoever closes it then looks for it before the runtime has made it, finds
        nothing and stops the keeper.  The start itself has to remove what the
        runtime goes on to make, or it stays for good."""
        created, gone = tmp_path / "created", tmp_path / "gone"
        path = tmp_path / "runtime"
        lines = ["#!/bin/sh", 'case "$1" in', "  image) echo null ;;"]
        lines += [f'  run) sleep 1.5; touch "{created}"; echo 0123456789abcdef ;;']
        lines += [f'  rm) if [ -e "{created}" ]; then touch "{gone}"; fi ;;']
        present = f'[ -e "{created}" ] && [ ! -e "{gone}" ]'
        lines += [f"  container) if {present}; then echo 0123; else {ABSENT}; fi ;;"]
        lines += ["esac", "exit 0"]
        path.write_text(NL.join(lines) + NL)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        container = Container(IMAGE, runtime=str(path))
        failed: list[str] = []

        def start() -> None:
            try:
                container.start()
            except ContainerError as error:
                failed.append(str(error))

        starting = threading.Thread(target=start)
        starting.start()
        time.sleep(0.5)
        assert container.close() is True  # nothing there yet
        starting.join(timeout=20)
        assert failed == ["could not start the container: it was closed while it started"]
        assert created.exists() and gone.exists()
        assert container.removed is True and container._lifetime._proc is None

    def test_the_time_can_be_set_again(self, tmp_path):
        """Whoever has more to do with a container than its lifetime foresaw says
        so; there is a keeper at every moment in between."""
        container = Container(IMAGE, runtime=_scripted_runtime(tmp_path, ABSENT), max_lifetime=600)
        container.start()
        try:
            first = container._lifetime._proc
            container.keep_for(1200)
            second = container._lifetime._proc
            assert first is not None and second is not None and first is not second
            assert first.poll() is not None and second.poll() is None
            assert second.args[4:] == ["1200", container.runtime, container.name]
        finally:
            assert container.close() is True
        assert container._lifetime._proc is None and second.poll() is not None


def _alive(pid: int) -> bool:
    """Whether a process with this id runs.  One that has ended and only waits
    to be collected does not."""
    try:
        with open(f"/proc/{pid}/stat") as status:
            return status.read().rpartition(")")[2].split()[0] != "Z"
    except OSError:
        return False


def _command_line(pid: int) -> list[str]:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as line:
            return [part.decode("utf-8", "replace") for part in line.read().split(bytes(1)) if part]
    except OSError:
        return []


def _gone_within(pid: int, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    return not _alive(pid)


# Where the package under test is imported from, for a process a test starts.
PACKAGE_PATH = os.path.dirname(os.path.dirname(os.path.abspath(turnstone.__file__)))


def _runner(tmp_path: Any, lines: list[str], *arguments: str) -> subprocess.Popen[str]:
    """A small process of the test's own that uses the package as a runner does
    and then waits to be killed: the one process such a test signals."""
    script = tmp_path / "runner.py"
    script.write_text(NL.join(lines) + NL)
    return subprocess.Popen(
        [sys.executable, str(script), *arguments],
        stdout=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONPATH": PACKAGE_PATH},
    )


class TestTheEventsOfOneContainer:
    def test_another_containers_events_are_not_taken_for_its_own(self, tmp_path):
        """The runtime's filter takes every container whose name begins with this
        one's, and the helper that spares the main process is such a container.
        Its end, right after the start, is not the container's end: taken for
        it, nobody would wait any more for the events that say why the container
        itself ended."""
        from turnstone.rollout.container import _StopWatch

        body = [
            'case "$1" in events)',
            "  echo die tsgym-one-main; echo destroy tsgym-one-main; sleep 1",
            "  echo oom tsgym-one; echo kill tsgym-one; echo die tsgym-one; exec sleep 30 ;;",
            "esac",
        ]
        watch = _StopWatch(_program(tmp_path, NL.join(body)), "tsgym-one")
        watch.start()
        try:
            time.sleep(0.5)
            assert not watch._died.is_set() and watch._heard is False and watch.oom_kills == 0
            # Waited for, and then read: asked from outside, after a kill for memory.
            assert watch.ended_from_outside(wait=10) is True
            assert watch.oom_at_death(None) is True and watch.oom_kills == 1
        finally:
            watch.close()
        assert watch._proc is None

    def test_a_watch_closed_before_its_client_runs_leaves_no_client(self, tmp_path):
        from turnstone.rollout.container import _StopWatch

        watch = _StopWatch(_program(tmp_path, "exec sleep 30"), "tsgym-one")
        watch.close()
        started = threading.Event()
        watch._listen(started)  # returns at once: the client is ended, not read
        assert started.is_set() and watch._proc is not None
        assert watch._proc.poll() is not None


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")
class TestWhatAKilledRunnerLeaves:
    """A pool ends a runner with a kill that cannot be caught, so nothing in the
    runner tidies up.  What is left on this machine must end by itself: the
    keeper when it has removed the container, everything else at once.

    Each test starts a small runner of its own and kills that one process."""

    def test_the_events_client_ends_with_the_runner(self, tmp_path):
        # Stands in for the runtime's client, which by itself never ends.
        runtime = _program(tmp_path, 'case "$1" in events) exec sleep 60 ;; esac')
        lines = [
            "import sys, time",
            "from turnstone.rollout.container import _StopWatch",
            "watch = _StopWatch(sys.argv[1], 'tsgym-no-such-container')",
            "watch.start()",
            "print(watch._proc.pid, flush=True)",
            "time.sleep(120)",
        ]
        runner = _runner(tmp_path, lines, runtime)
        client = 0
        try:
            assert runner.stdout is not None
            client = int(runner.stdout.readline())
            assert _alive(client)
            runner.kill()
            runner.wait(timeout=10)
            assert _gone_within(client, 10)
        finally:
            if runner.poll() is None:
                runner.kill()
                runner.wait()
            if runner.stdout is not None:
                runner.stdout.close()
            # Still there only where what is under test failed.  It is this
            # test's own stand-in, and its command line is checked first.
            line = _command_line(client) if client else []
            if line == ["sleep", "60"] or line[1:3] == [runtime, "events"]:
                os.kill(client, signal.SIGKILL)

    @needs_container
    def test_of_a_real_container_only_the_keeper_stays_until_it_has_removed_it(self, tmp_path):
        lines = [
            "import sys, time",
            "from turnstone.rollout.container import Container, ContainerSpec",
            "spec = ContainerSpec(image=sys.argv[1], memory='64m')",
            "container = Container(spec, max_lifetime=15).start()",
            "watch, keeper = container._watch._proc, container._lifetime._proc",
            "print(container.name, watch.pid, keeper.pid, flush=True)",
            "time.sleep(120)",
        ]
        runner = _runner(tmp_path, lines, IMAGE)
        name, client, keeper = "", 0, 0
        try:
            assert runner.stdout is not None
            name, *pids = runner.stdout.readline().split()
            client, keeper = (int(pid) for pid in pids)
            assert name.startswith(NAME_PREFIX) and _listed(name) == [name]
            assert _alive(client) and _alive(keeper)
            runner.kill()
            runner.wait(timeout=10)
            # The runtime's events client: at once.  It did not wait for the
            # container's removal, which is still to come.
            assert _gone_within(client, 5)
            assert _listed(name) == [name] and _alive(keeper)
            # The keeper: it removes the container when its time is up, and leaves.
            deadline = time.monotonic() + 60
            while (_listed(name) or _alive(keeper)) and time.monotonic() < deadline:
                time.sleep(0.5)
            assert _listed(name) == [] and not _alive(keeper)
        finally:
            if runner.poll() is None:
                runner.kill()
                runner.wait()
            if runner.stdout is not None:
                runner.stdout.close()
            if name.startswith(NAME_PREFIX):
                # The container this test's own runner made, by its name.
                _runtime("rm", "--force", "--volumes", name)
                for pid in (client, keeper):
                    if any(name in part for part in _command_line(pid)):
                        os.kill(pid, signal.SIGKILL)


def _archive(name: str, size: int, kind: bytes = tarfile.REGTYPE) -> bytes:
    """The start of the runtime's copy of a file of *size* bytes: the archive's
    headers, without the content that would follow."""
    entry = tarfile.TarInfo(name)
    entry.size, entry.type = size, kind
    return entry.tobuf(tarfile.PAX_FORMAT)


WRITABLE_ROOT = ContainerSpec(image=IMAGE, user="0:0", read_only_root=False, writable={})


class TestTheSizeOfAWritableRoot:
    """What is written to a writable root goes to this machine's disk, so it
    always has a size."""

    def test_a_task_that_names_none_gets_the_default(self):
        container = Container(WRITABLE_ROOT)
        assert DEFAULT_ROOT_SIZE == "10g"
        assert (container.root_size, container.root_size_source) == ("10g", "default")
        # Asked of the storage driver first; watched where it cannot.
        assert container.root_bound == "enforced"
        argv = container.run_argv()
        assert argv[argv.index("--storage-opt") + 1] == "size=10g"
        described = container.describe()
        assert (described["root_size"], described["root_size_source"]) == ("10g", "default")

    def test_a_task_can_name_another_size(self):
        spec = ContainerSpec(image=IMAGE, read_only_root=False, root_size="20m")
        container = Container(spec)
        assert (container.root_size, container.root_size_source) == ("20m", "task")
        assert "--storage-opt size=20m" in " ".join(container.run_argv())

    def test_a_read_only_root_has_none(self):
        container = Container(IMAGE)
        described = container.describe()
        assert (described["root_size"], described["root_size_source"]) == (None, None)
        assert described["root_bound"] is None
        assert "--storage-opt" not in container.run_argv()

    @pytest.mark.parametrize("size", ["0", "0g", "-1g", "lots", "", "nan", "inf", "1e400g"])
    def test_there_is_no_way_to_ask_for_no_size(self, size):
        spec = ContainerSpec(image=IMAGE, read_only_root=False, root_size=size)
        with pytest.raises(ValueError, match="root_size must be a size above zero"):
            spec.validate()
        with pytest.raises(ValueError, match="root_size must be a size above zero"):
            Container(spec)

    def test_a_size_is_for_a_writable_root(self):
        with pytest.raises(ValueError, match="root_size is for a writable root"):
            ContainerSpec(image=IMAGE, root_size="1g").validate()

    def test_a_files_size_is_read_from_the_start_of_the_runtimes_copy(self):
        """The first header says it; the content is never waited for."""
        from turnstone.rollout.container import _archived_size

        assert _archived_size(_archive("hosts", 148)) == 148
        # Also with content after it, whole or cut off anywhere.
        assert _archived_size(_archive("hosts", 148) + b"127.0.0.1 localhost") == 148
        # A size the header's own field cannot hold is written in a record before it.
        huge = 9 * 1024**3
        assert _archived_size(_archive("resolv.conf", huge)) == huge
        assert _archived_size(_archive("hosts", 0)) == 0
        # Anything else is no answer, never a size of nothing.
        assert _archived_size(b"") is None
        assert _archived_size(b"Error response from daemon") is None
        assert _archived_size(_archive("hosts", 148)[:300]) is None
        assert _archived_size(_archive("hosts", 0, tarfile.DIRTYPE)) is None
        assert _archived_size(_archive("hosts", 0, tarfile.SYMTYPE)) is None


def _sizing_runtime(tmp_path: Any, *, enforces: bool = False) -> str:
    """A stand-in for the runtime that answers what a writable root is asked.

    The listed size of the container's own file system is the content of the
    file ``listed``; the copy of each of the runtime's three files is the file
    of its name under ``copies``, a refusal where that file is missing, and
    "nothing stands there" where ``<name>.absent`` exists.  ``kill`` leaves the
    file ``killed``, after which the container is not running; with the file
    ``silent`` the runtime answers no question about the container's state.
    The look from inside for files deleted while open is answered with the
    file ``opened``, as the container's own programs would print it, and
    with nothing where that file is missing.  Without *enforces* it refuses
    a size for the storage driver, in words of its own.
    """
    (tmp_path / "copies").mkdir()
    (tmp_path / "listed").write_text("1000" + NL)
    for path, size in zip(RUNTIME_FILES, (148, 13, 273), strict=True):
        name = path.rsplit("/", 1)[-1]
        (tmp_path / "copies" / name).write_bytes(_archive(name, size))
    refusal = "" if enforces else "echo this driver takes no size >&2; exit 125"
    lines = [
        "#!/bin/sh",
        f'echo "$*" >> "{tmp_path}/calls"',
        'case "$1" in',
        "  image) echo null ;;",
        f'  run) case "$*" in *--storage-opt*) {refusal} ;; esac; echo 0123456789abcdef ;;',
        '  exec) case "$*" in',
        f'      *"root %d"*) cat "{tmp_path}/opened" 2>/dev/null ;;',
        "      *) echo uid=0; echo main_uid=65533; echo comm=sleep; echo ignored=10000; "
        "echo score=-1000 ;;",
        "    esac ;;",
        "  cp) name=${2##*/}",
        f'    if [ -e "{tmp_path}/copies/$name.absent" ]; then',
        '      echo "Error response from daemon: Could not find the file $2" >&2; exit 1',
        f'    elif [ -e "{tmp_path}/copies/$name" ]; then cat "{tmp_path}/copies/$name"',
        "    else echo Error response from daemon: cannot mount >&2; exit 1; fi ;;",
        f'  kill) touch "{tmp_path}/killed" ;;',
        '  inspect) case "$*" in',
        f'      *SizeRw*) cat "{tmp_path}/listed" ;;',
        f'      *) if [ -e "{tmp_path}/silent" ]; then echo cannot reach the service >&2; exit 1',
        f'         elif [ -e "{tmp_path}/killed" ]; then echo false 137 false',
        "         else echo true 0 false; fi ;;",
        "    esac ;;",
        f"  container) {ABSENT} ;;",
        "esac",
        "exit 0",
    ]
    path = tmp_path / "runtime"
    path.write_text(NL.join(lines) + NL)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


def _wait_for(path: Any, seconds: float = 10.0) -> bool:
    deadline = time.monotonic() + seconds
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    return bool(path.exists())


class TestAWritableRootIsBounded:
    """The size counts what the container's own file system holds and what the
    three files hold that the runtime mounts into it from this machine's disk.
    Driven with a stand-in for the runtime; the same in real containers below."""

    def _container(self, tmp_path: Any, size: str = "1m", **runtime: Any) -> Container:
        spec = ContainerSpec(
            image=IMAGE, user="0:0", read_only_root=False, writable={}, root_size=size
        )
        container = Container(spec, runtime=_sizing_runtime(tmp_path, **runtime))
        container.size_check_seconds = 0.1
        return container

    def _calls(self, tmp_path: Any) -> list[str]:
        return [line for line in (tmp_path / "calls").read_text().split(NL) if line]

    def test_what_the_three_files_hold_counts(self, tmp_path):
        container = self._container(tmp_path).start()
        try:
            # The driver refused a size, in words this harness has never seen:
            # the start was tried again without it, and the size is watched.
            assert container.root_bound == "watched"
            runs = [call for call in self._calls(tmp_path) if call.startswith("run --detach ")]
            assert len(runs) == 2 and "--storage-opt size=1m" in runs[0]
            assert "--storage-opt" not in runs[1]
            # Each of the three was asked of the runtime before the start was over.
            copies = [call for call in self._calls(tmp_path) if call.startswith("cp ")]
            assert copies[:3] == [f"cp {container.name}:{path} -" for path in RUNTIME_FILES]
            time.sleep(0.5)
            assert not (tmp_path / "killed").exists()  # 1000 + 148 + 13 + 273 bytes
            # Listed and the files together, not either alone.
            (tmp_path / "listed").write_text("600000" + NL)
            (tmp_path / "copies" / "hostname").write_bytes(_archive("hostname", 500_000))
            assert _wait_for(tmp_path / "killed")
            with pytest.raises(ContainerLostError):
                container.ensure_running()
            assert container.lost_cause == "model"
            assert container.lost == (
                "the container was stopped: it wrote 1,100,421 bytes to its file system, "
                "more than the 1,048,576 it may hold (500,421 of them in /etc/hosts, "
                "/etc/hostname, /etc/resolv.conf, which the runtime keeps on this machine's "
                "disk)"
            )
        finally:
            assert container.close() is True

    def test_where_the_driver_enforces_the_size_the_three_files_are_watched_all_the_same(
        self, tmp_path
    ):
        """No storage driver counts them: they are not in the container's own
        file system."""
        container = self._container(tmp_path, enforces=True).start()
        try:
            assert container.root_bound == "enforced"
            (tmp_path / "listed").write_text("999999999" + NL)  # the driver's to refuse
            time.sleep(0.5)
            assert not (tmp_path / "killed").exists()
            (tmp_path / "copies" / "resolv.conf").write_bytes(_archive("resolv.conf", 2_000_000))
            assert _wait_for(tmp_path / "killed")
            with pytest.raises(ContainerLostError):
                container.ensure_running()
            assert container.lost_cause == "model"
            assert "more than the 1,048,576 it may hold" in (container.lost or "")
            assert not any("SizeRw" in call for call in self._calls(tmp_path))
        finally:
            assert container.close() is True

    @pytest.mark.parametrize("broken", ["no copy", "no archive", "no listed size"])
    def test_a_root_that_cannot_be_measured_is_not_started(self, tmp_path, broken):
        container = self._container(tmp_path)
        if broken == "no copy":
            (tmp_path / "copies" / "hostname").unlink()
        elif broken == "no archive":
            (tmp_path / "copies" / "hosts").write_bytes(b"127.0.0.1 localhost" + NL.encode())
        else:
            (tmp_path / "listed").write_text("<nil>" + NL)
        with pytest.raises(ContainerError, match="its writable root cannot be bounded"):
            container.start()
        # What had been made was removed again.
        assert f"rm --force --volumes {container.name}" in self._calls(tmp_path)
        assert container.removed is True and container._lifetime._proc is None

    def test_a_root_over_its_size_before_anything_ran_in_it_is_not_started(self, tmp_path):
        """A size below what the container holds when it starts is the task's
        mistake: typed without its unit, for one.  Started all the same, the
        container was stopped at the first look and the run scored as the
        model's own destroyed sandbox, trainable, though the model wrote nothing."""
        container = self._container(tmp_path, size="1433")
        said = (
            r"its writable root may hold 1,433 bytes \(root_size '1433'\) and holds 1,434 when "
            r"it starts, before any command has run in it"
        )
        with pytest.raises(ContainerError, match=said):
            container.start()
        # What had been made was removed again, and nothing was stopped as
        # if a command had written it.
        assert f"rm --force --volumes {container.name}" in self._calls(tmp_path)
        assert container.removed is True and container._lifetime._proc is None
        assert container._over_size is None and not (tmp_path / "killed").exists()

    def test_one_that_holds_exactly_its_size_when_it_starts_is(self, tmp_path):
        container = self._container(tmp_path, size="1434").start()  # 1000 + 148 + 13 + 273
        try:
            time.sleep(0.4)
            assert not (tmp_path / "killed").exists() and container.lost is None
        finally:
            assert container.close() is True

    def test_where_the_driver_enforces_the_size_the_three_files_alone_are_held_against_it(
        self, tmp_path
    ):
        container = self._container(tmp_path, size="433", enforces=True)
        with pytest.raises(ContainerError, match="may hold 433 bytes .* and holds 434 when"):
            container.start()
        assert container.removed is True

    def test_a_file_the_runtime_does_not_mount_holds_nothing(self, tmp_path):
        container = self._container(tmp_path)
        (tmp_path / "copies" / "hostname").unlink()
        (tmp_path / "copies" / "hostname.absent").touch()
        container.start()
        try:
            assert container._runtime_file_bytes() == 148 + 273
        finally:
            assert container.close() is True

    def test_one_that_can_no_longer_be_measured_while_it_runs_is_stopped(self, tmp_path):
        """Measured when it started, so what changed is in the container: there
        the commands run as root over a writable root."""
        container = self._container(tmp_path)
        container.size_unknown_seconds = 0.3
        container.start()
        try:
            time.sleep(0.3)
            assert not (tmp_path / "killed").exists()
            (tmp_path / "copies" / "hosts").unlink()
            assert _wait_for(tmp_path / "killed")
            with pytest.raises(ContainerLostError):
                container.ensure_running()
            assert container.lost_cause == "model"
            assert "the runtime could not say for" in (container.lost or "")
            assert "/etc/hosts: Error response from daemon: cannot mount" in (container.lost or "")
        finally:
            assert container.close() is True

    def test_no_look_runs_the_containers_programs_with_more_rights_than_the_commands(
        self, tmp_path
    ):
        """Over a writable root the programs in the container may be the model's.
        The look for files deleted while open runs them, so it runs them as the
        commands' user and as nobody else."""
        spec = ContainerSpec(image=IMAGE, read_only_root=False, writable={}, root_size="1m")
        container = Container(spec, runtime=_sizing_runtime(tmp_path))
        container.size_check_seconds = 0.1
        container.start()
        try:
            time.sleep(0.6)
        finally:
            assert container.close() is True
        inside = [call.split() for call in self._calls(tmp_path) if call.startswith("exec ")]
        assert len(inside) >= 3  # the check at the start, and a look or more
        assert {call[call.index("--user") + 1] for call in inside} == {"65534:65534"}
        assert not any("--privileged" in call for call in inside)

    def test_the_look_from_inside_does_not_start_in_the_tasks_working_directory(self, tmp_path):
        """A working directory its user renamed or removed starts nothing, and
        a look that cannot start sees no file deleted while open.  The
        harness's own commands start in the root directory."""
        container = self._container(tmp_path)
        container.start()
        try:
            time.sleep(0.4)
        finally:
            assert container.close() is True
        looks = [call.split() for call in self._calls(tmp_path) if "root %d" in call]
        assert len(looks) >= 2
        assert {call[call.index("--workdir") + 1] for call in looks} == {"/"}
        # What is the task's own starts where the task says.
        assert container.exec_argv(["true"])[4:6] == ["--workdir", "/workspace"]
        assert container.exec_argv(["true"], workdir="/")[4:6] == ["--workdir", "/"]

    def test_a_runtime_that_answers_nothing_at_all_is_not_the_containers_doing(self, tmp_path):
        container = self._container(tmp_path)
        container.size_unknown_seconds = 0.3
        container.start()
        try:
            (tmp_path / "silent").touch()
            (tmp_path / "copies" / "hosts").unlink()
            time.sleep(1.5)
            assert not (tmp_path / "killed").exists()
            assert container._over_size is None
        finally:
            (tmp_path / "silent").unlink()
            assert container.close() is True

    def _opened(self, tmp_path: Any, *lines: bytes | str) -> None:
        """What the look from inside is answered with: the device of the root,
        then device, links, inode and size of every file a process holds open."""
        as_bytes = [line.encode() if isinstance(line, str) else line for line in lines]
        (tmp_path / "opened").write_bytes(NL.encode().join(as_bytes) + NL.encode())

    def test_a_file_deleted_while_open_counts_as_the_look_from_inside_shows_it(self, tmp_path):
        """A file of the root's own device with no link left, once, however
        many processes hold it; neither one that still has a link nor one on
        another device."""
        container = self._container(tmp_path)
        answer = ["root 64", "64 0 11 500000", "64 1 12 900000", "65 0 13 900000", "64 0 11 500000"]
        self._opened(tmp_path, *answer)
        container.start()
        try:
            assert container._held() == (1000, 500000, 434)
            time.sleep(0.5)
            assert not (tmp_path / "killed").exists()
            self._opened(tmp_path, *answer, "64 0 14 600000")
            assert _wait_for(tmp_path / "killed")
            with pytest.raises(ContainerLostError):
                container.ensure_running()
            assert container.lost_cause == "model"
            assert container.lost == (
                "the container was stopped: it wrote 1,101,434 bytes to its file system, "
                "more than the 1,048,576 it may hold (1,100,000 of them in files deleted "
                "while open)"
            )
        finally:
            assert container.close() is True

    # What a program in the container can print where a size belongs.
    NO_SIZE: dict[str, bytes] = {
        "five thousand digits": b"9" * 5000,
        "digits that are none of the ten": (chr(0xB2) + chr(0xB3)).encode(),
        "below zero": b"-5",
        "a number with a point": b"1e9",
        "words": b"lots",
        "nothing": b"",
        "twenty digits": b"1" * 20,
    }

    @pytest.mark.parametrize("what", NO_SIZE)
    def test_whatever_the_look_from_inside_is_answered_the_watch_goes_on(self, tmp_path, what):
        """Over a writable root the programs that answer may be the model's.
        A size of five thousand digits passed for a number and its conversion
        raised; nothing caught that, the watch ended, and from then on nothing
        bounded the root while the record still said it was watched.  Only
        what can be a size is taken for one."""
        container = self._container(tmp_path)
        self._opened(tmp_path, "root 64", b"64 0 11 " + self.NO_SIZE[what], "64 0 12 300")
        container.start()
        try:
            time.sleep(0.5)  # several looks
            assert container._size_watch is not None and container._size_watch.is_alive()
            assert container._unlinked_bytes() == 300
            assert not (tmp_path / "killed").exists()
            # And the root is still bounded.
            (tmp_path / "listed").write_text("2000000" + NL)
            assert _wait_for(tmp_path / "killed")
            with pytest.raises(ContainerLostError):
                container.ensure_running()
            assert container.lost_cause == "model"
            assert "more than the 1,048,576 it may hold" in (container.lost or "")
        finally:
            assert container.close() is True

    @pytest.mark.parametrize(
        "answer",
        [b"", b"root", b"root ", bytes(range(256)) * 8, b"no root line" + NL.encode() + b"1 0 1 5"],
        ids=["nothing", "no device", "a blank device", "no text", "another first line"],
    )
    def test_an_answer_that_is_no_answer_counts_nothing(self, tmp_path, answer):
        container = self._container(tmp_path)
        (tmp_path / "opened").write_bytes(answer)
        container.start()
        try:
            assert container._unlinked_bytes() == 0
            time.sleep(0.3)
            assert container._size_watch is not None and container._size_watch.is_alive()
        finally:
            assert container.close() is True

    def test_while_the_container_is_held_still_no_look_from_inside_is_begun(self, tmp_path):
        """The look is a process of the container.  One begun while the
        container is held still to end what was left running in it would be
        counted among that, and ended with it.  What the last look counted
        is given again, and everything else is asked of the runtime as
        before: held still, a root is neither unmeasured nor unbounded."""
        container = self._container(tmp_path)
        container.size_unknown_seconds = 0.3
        self._opened(tmp_path, "root 64", "64 0 11 700")
        container.start()
        try:
            time.sleep(0.4)
            with container.without_looks_inside():
                looks = len([call for call in self._calls(tmp_path) if "root %d" in call])
                assert container._held() == (1000, 700, 434)
                time.sleep(1.0)  # longer than a root may go unmeasured
                began = len([call for call in self._calls(tmp_path) if "root %d" in call])
                assert began == looks
                assert not (tmp_path / "killed").exists() and container._over_size is None
                # Over its size it is stopped all the same.
                (tmp_path / "listed").write_text("2000000" + NL)
                assert _wait_for(tmp_path / "killed")
            # And the looks go on afterwards.
            before = len([call for call in self._calls(tmp_path) if "root %d" in call])
            assert container._unlinked_bytes() == 700
            after = len([call for call in self._calls(tmp_path) if "root %d" in call])
            assert after == before + 1
        finally:
            assert container.close() is True

    def test_a_look_under_way_is_waited_for_before_the_container_is_held(self, tmp_path):
        """Not for ever: a look has a time limit, and so has the wait."""
        container = self._container(tmp_path)
        assert container._looking.acquire(blocking=False)  # a look is under way
        try:
            started = time.monotonic()
            with container.without_looks_inside(wait=0.3):
                waited = time.monotonic() - started
            assert 0.3 <= waited < 3
            assert container._looking.locked()  # still the look's own
        finally:
            container._looking.release()

    def test_a_look_that_raises_is_a_look_that_could_not_measure(self, tmp_path, monkeypatch):
        """Whatever it raises.  The watch does not end over it: the root is
        unmeasured from then on, and after its time the container is stopped
        like any other whose root can no longer be measured."""
        container = self._container(tmp_path)
        container.size_unknown_seconds = 0.5
        container.start()
        try:
            looked: list[float] = []

            def broken() -> int:
                looked.append(time.monotonic())
                raise RuntimeError("no look today")

            monkeypatch.setattr(container, "_unlinked_bytes", broken)
            assert _wait_for(tmp_path / "killed")
            assert len(looked) >= 3 and looked[-1] - looked[0] >= 0.5  # not at the first
            with pytest.raises(ContainerLostError):
                container.ensure_running()
            assert container.lost_cause == "model"
            assert "the runtime could not say for" in (container.lost or "")
            assert "a look at it failed: RuntimeError: no look today" in (container.lost or "")
        finally:
            assert container.close() is True

    def test_the_watch_goes_on_until_the_container_is_stopped(self, tmp_path, monkeypatch):
        """A stop that did not work, or raised, left a container over its size
        running with nothing looking at it any more."""
        container = self._container(tmp_path)
        real_stop = container.stop
        tried: list[int] = []

        def stop() -> bool:
            tried.append(len(tried))
            if len(tried) == 1:
                return False
            if len(tried) == 2:
                raise RuntimeError("the runtime is away")
            return real_stop()

        monkeypatch.setattr(container, "stop", stop)
        container.start()
        try:
            (tmp_path / "listed").write_text("2000000" + NL)
            assert _wait_for(tmp_path / "killed")
            assert len(tried) == 3
            with pytest.raises(ContainerLostError):
                container.ensure_running()
            assert "more than the 1,048,576 it may hold" in (container.lost or "")
        finally:
            assert container.close() is True

    def test_a_watch_found_ended_while_the_container_runs_stops_it(self, tmp_path, monkeypatch):
        """Nothing should end the watch but the container's end.  Should it
        be found ended all the same, the root is not bounded any more: the
        container is stopped where it is next asked for, before another
        command runs in it."""
        monkeypatch.setattr(Container, "_watch_size", lambda _self: None)
        container = self._container(tmp_path)
        container.start()
        try:
            assert container._size_watch is not None
            container._size_watch.join(timeout=5)
            assert not container._size_watch.is_alive()
            with pytest.raises(ContainerLostError):
                container.ensure_running()
            assert (tmp_path / "killed").exists()
            assert container.lost_cause == "model"
            assert container.lost == (
                "the container was stopped: the watch over its size had ended, and a root "
                "that is not watched is not bounded"
            )
        finally:
            assert container.close() is True

    def test_a_watch_that_ended_with_the_container_is_nothing_to_act_on(self, tmp_path):
        """Closed, or stopped by the watch itself."""
        container = self._container(tmp_path)
        container.start()
        assert container.close() is True
        assert container._size_watch is not None
        container._size_watch.join(timeout=5)
        assert container._unwatched() is False


# What the check of the shell tool gets back from a container in which it works.
SHELL_WORKS = Completed(0, b"turnstone-shell-tool-check" + NL.encode(), b"", False, 27)


class _SlowContainer:
    """Stands in for a container whose runtime takes its time over a kill.

    The check of the shell tool, made when an environment is built, gets
    *shell_says*; tidying up after that check takes no time and is not noted
    among the kills."""

    workdir = "/workspace"
    user = "65534:65534"
    lost = None

    def __init__(self, kill_seconds: float, shell_says: Completed = SHELL_WORKS) -> None:
        self.kill_seconds = kill_seconds
        self.kills: list[list[str]] = []
        self.shell_says = shell_says
        # Everything that was run, with how; and the scripts of the checks.
        self.ran: list[tuple[list[str], dict[str, Any]]] = []
        self.checked: list[str] = []

    def exec(self, argv: list[str], **kwargs: Any) -> Completed:
        self.ran.append((argv, kwargs))
        if argv[2] == container_module._SPAWN_SCRIPT:
            self.checked.append(argv[4])
            return self.shell_says
        if argv[2] == container_module._KILL_SCRIPT and argv[-1] not in self.checked:
            time.sleep(self.kill_seconds)
            self.kills.append(argv)
        return Completed(0, b"", b"", False, 0)

    def ensure_running(self) -> None:
        return None


class TestTheShellToolIsTriedBeforeTheRun:
    """In a container where the model's commands cannot run, every shell call
    fails and the run is scored as the model's.  So one command is run the way
    the tool runs them when the tools are set up, and a container in which
    that fails does not start."""

    def test_one_command_is_run_the_way_every_command_of_the_models_is(self):
        container = _SlowContainer(kill_seconds=0)
        environment = ContainerEnvironment(container)  # type: ignore[arg-type]
        spawns = [(a, how) for a, how in container.ran if a[2] == container_module._SPAWN_SCRIPT]
        ((argv, how),) = spawns
        (inside,) = container.checked
        # The script the tool runs commands with, at the place it puts them,
        # by the shell it uses ...
        assert argv == environment._spawn_argv(inside) and argv[-1] == environment.shell
        assert inside.startswith("/tmp/.turnstone-") and inside.endswith(".sh")
        # ... behind the tool's own preamble, as the commands' user: no other
        # user is named.
        script = "set -o pipefail" + NL + "echo turnstone-shell-tool-check"
        assert how["stdin_data"] == script.encode() and "user" not in how
        # And tidied up as after every command.
        tidied = [a[-1] for a, _how in container.ran if a[2] == container_module._KILL_SCRIPT]
        assert tidied == [inside]

    def test_a_command_of_the_models_is_written_the_same_way(self, tmp_path, monkeypatch):
        """The check and the tool share what they run, so the tool cannot move
        somewhere the check does not look.  A program of this test's own stands
        in for the runtime and notes what it is given."""
        import tempfile

        monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
        noted = tmp_path / "noted.json"
        lines = [
            f"#!{sys.executable}",
            "import json, sys",
            f"json.dump([sys.argv[1:], sys.stdin.read()], open({str(noted)!r}, 'w'))",
        ]
        program = tmp_path / "stand-in"
        program.write_text(NL.join(lines) + NL)
        program.chmod(program.stat().st_mode | stat.S_IXUSR)
        container = _SlowContainer(kill_seconds=0)
        container.exec_argv = lambda argv, **_how: [str(program), *argv]  # type: ignore[attr-defined]
        container.ensure_running = lambda: None  # type: ignore[attr-defined]
        environment = ContainerEnvironment(container)  # type: ignore[arg-type]
        proc, _pgid, script_path = environment.spawn("echo hi", stop_on_error=True, env=None)
        try:
            assert proc.wait(timeout=30) == 0
            argv, script = json.loads(noted.read_text())
        finally:
            for pipe in (proc.stdout, proc.stderr):
                if pipe is not None:
                    pipe.close()
            os.unlink(script_path)
        inside = argv[4]
        assert argv == environment._spawn_argv(inside)
        assert inside.startswith("/tmp/.turnstone-") and inside not in container.checked
        assert script == "set -o pipefail" + NL + "set -e" + NL + "echo hi"

    @pytest.mark.parametrize(
        ("says", "why"),
        [
            (
                Completed(
                    125, b"", b"sh: 2: cannot create /tmp/x.sh: Read-only file system", False, 0
                ),
                "Read-only file system",
            ),
            (
                Completed(2, b"", b"sh: 1: set: Illegal option -o pipefail", False, 0),
                "Illegal option",
            ),
            (Completed(1, b"", b"", False, 0), "exit code 1"),
            (
                Completed(0, b"something else", b"", False, 14),
                "the command printed b'something else'",
            ),
            (Completed(-9, b"", b"", True, 0), "did not end within 30 s"),
        ],
    )
    def test_a_container_in_which_it_fails_is_refused(self, says, why):
        container = _SlowContainer(kill_seconds=0, shell_says=says)
        with pytest.raises(ContainerError, match="the shell tool cannot run in it") as refused:
            ContainerEnvironment(container)  # type: ignore[arg-type]
        assert "as user 65534:65534" in str(refused.value) and why in str(refused.value)
        assert "/tmp has to be writable for that user" in str(refused.value)
        # Tidied up all the same.
        assert [a[-1] for a, _how in container.ran if a[2] == container_module._KILL_SCRIPT] == (
            container.checked
        )


class TestKillDoesNotBlock:
    """Callers hold locks across ``kill_group`` (a background shell's, while every
    shell of a session is stopped one after another), so it must return at once
    however long the runtime takes."""

    def test_it_returns_before_the_kill_inside_has_finished(self):
        container = _SlowContainer(kill_seconds=1.5)
        environment = ContainerEnvironment(container)  # type: ignore[arg-type]
        proc = subprocess.Popen(["sleep", "30"], start_new_session=True, text=True)
        environment._scripts[proc.pid] = "/tmp/.turnstone-x.sh"
        try:
            started = time.monotonic()
            environment.kill_group(proc, os.getpgid(proc.pid))
            assert time.monotonic() - started < 0.5
            assert container.kills == []  # still under way
            proc.wait(timeout=5)  # the local stand-in was killed at once
            assert environment.settle(10) is True
            assert len(container.kills) == 1 and container.kills[0][-1] == "/tmp/.turnstone-x.sh"
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()

    def test_settle_reports_a_kill_that_is_still_under_way(self):
        container = _SlowContainer(kill_seconds=1.0)
        environment = ContainerEnvironment(container)  # type: ignore[arg-type]
        proc = subprocess.Popen(["true"], start_new_session=True, text=True)
        proc.wait()
        environment._scripts[proc.pid] = "/tmp/.turnstone-y.sh"
        environment.kill_group(proc, proc.pid)
        assert environment.settle(0.1) is False
        assert environment.settle(10) is True

    def test_a_stand_in_that_has_exited_is_not_signalled(self, monkeypatch):
        """Once the local process has exited and been collected, its group id may
        belong to something else."""
        signalled: list[int] = []
        monkeypatch.setattr(os, "killpg", lambda pgid, _sig: signalled.append(pgid))
        environment = ContainerEnvironment(_SlowContainer(kill_seconds=0))  # type: ignore[arg-type]
        proc = subprocess.Popen(["true"], start_new_session=True, text=True)
        pgid = proc.pid
        proc.wait()
        environment.kill_group(proc, pgid)
        environment.settle(5)
        assert signalled == []

    def test_the_end_of_a_command_does_not_start_in_the_tasks_working_directory(self):
        """The command it ends may have renamed or removed that directory."""
        container = _SlowContainer(kill_seconds=0)
        environment = ContainerEnvironment(container)  # type: ignore[arg-type]
        environment._kill_inside("/tmp/.turnstone-z.sh")
        kills = [how for argv, how in container.ran if argv[2] == container_module._KILL_SCRIPT]
        assert kills and all(how["workdir"] == "/" for how in kills)

    def test_the_model_is_not_shown_this_machines_process_id(self):
        environment = ContainerEnvironment(_SlowContainer(kill_seconds=0))  # type: ignore[arg-type]
        proc = subprocess.Popen(["true"], start_new_session=True, text=True)
        proc.wait()
        assert environment.process_label(proc) == ""


class TestACommandNoEncodingCanWrite:
    def test_it_is_reported_and_leaves_no_file_behind(self, tmp_path, monkeypatch):
        """Half of a surrogate pair, which a model can write as an escape."""
        import tempfile

        monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
        environment = ContainerEnvironment(_SlowContainer(kill_seconds=0))  # type: ignore[arg-type]
        with pytest.raises(UnicodeEncodeError):
            environment.spawn("echo " + chr(0xD800), stop_on_error=False, env=None)
        assert os.listdir(tmp_path) == []


def _call(call_id: str, name: str, **arguments: Any) -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def _spec(**settings: Any) -> ContainerSpec:
    """The contained default with small memory-backed directories."""
    settings.setdefault("writable", {"/workspace": "16m", "/tmp": "16m"})
    return ContainerSpec(image=IMAGE, **settings)


@pytest.fixture
def box(tmp_db):
    """A started container, its environment and a session whose tools act in it."""
    container = Container(_spec(memory="512m"))
    session = None
    try:
        container.start()
        environment = ContainerEnvironment(container, op_timeout=3, max_file_bytes=200_000)
        session = HeadlessSession(
            client=MagicMock(), model="eval-model", exec_env=environment, tool_timeout=20
        )

        def run(name: str, **arguments: Any) -> str:
            results, _feedback = session._execute_tools([_call("c", name, **arguments)])
            output = results[0][1]
            return output if isinstance(output, str) else json.dumps(output)

        yield container, environment, session, run
    finally:
        if session is not None:
            session.close()
        container.close()
        assert container.removed and _listed(container.name) == []


@needs_container
class TestAContainerThatCannotRunTheShellTool:
    """A task whose writable directories leave ``/tmp`` out, over a read-only
    root: the tool cannot write a command's script."""

    NO_TMP = ContainerSpec(image=IMAGE, memory="256m", writable={"/workspace": "16m"})

    def test_it_is_refused_when_the_tools_are_set_up(self):
        container = Container(self.NO_TMP)
        try:
            # The container as such starts: one that only runs given programs,
            # like a verifier's clean container, needs no such directory.
            container.start()
            with pytest.raises(ContainerError, match="the shell tool cannot run in it") as refused:
                ContainerEnvironment(container)
            assert "as user 65534:65534" in str(refused.value)
            assert "Read-only file system" in str(refused.value)
        finally:
            assert container.close() is True

    def test_a_rollout_in_it_does_not_start(self):
        """It used to start, every shell call failed, and the record was the
        model's own outcome, trainable."""
        script = [
            {"tool_calls": [{"name": "bash", "arguments": {"command": "echo hi"}}]},
            {"content": "done"},
        ]
        with ScriptedChatServer(script) as server:
            record = run_episode(
                Task(prompt="work", container=self.NO_TMP),
                RolloutConfig(base_url=server.base_url, model="scripted", **SMALL),
            )
            requests = len(server.requests)
        assert record["finish_state"] == "sandbox_failed" and record["finish_cause"] == "machinery"
        assert record["trainable"] is False and requests == 0
        assert "the shell tool cannot run in it as user 65534:65534" in record["error"]
        assert record["sandbox"]["removed"] is True and _listed(record["sandbox"]["name"]) == []


@needs_container
class TestContainer:
    def test_it_is_isolated_limited_and_removed(self):
        container = Container(_spec(cpus=0.5, memory="256m", pids_limit=64))
        try:
            container.start()
            assert _listed(container.name) == [container.name]
            host = json.loads(_runtime("inspect", container.name).stdout)[0]["HostConfig"]
            assert host["NetworkMode"] == "none"
            assert host["Memory"] == 256 * 1024 * 1024 and host["MemorySwap"] == host["Memory"]
            assert host["NanoCpus"] == 500_000_000
            assert host["PidsLimit"] == 64
            assert host["ReadonlyRootfs"] is True
            assert host["CapDrop"] == ["ALL"]
            assert not host["Binds"] and not host.get("DeviceRequests")
            assert set(host["Tmpfs"]) == {"/workspace", "/tmp"}
            assert "size=16m" in host["Tmpfs"]["/workspace"]
            assert host["AutoRemove"] is False
            assert host["ShmSize"] == 64 * 1024 * 1024
            limits = {u["Name"]: (u["Soft"], u["Hard"]) for u in host["Ulimits"]}
            assert limits == {
                "nofile": (4096, 4096),
                "core": (0, 0),
                "memlock": (65536, 65536),
            }
            config = json.loads(_runtime("inspect", container.name).stdout)[0]["Config"]
            assert config["User"] == "65533:65533" and host["OomScoreAdj"] == 0
            # A command is another user than the main process, and only the main
            # process is spared by the out-of-memory killer.
            script = "id -u; cat /proc/self/oom_score_adj /proc/1/oom_score_adj"
            who = container.exec(["sh", "-c", script], timeout=20)
            assert who.stdout.split() == [b"65534", b"0", b"-1000"]
            described = container.describe()
            assert described["main_process"] == "sleep" and described["main_user"] == "65533:65533"
            assert described["main_process_reach"] is None and described["volumes"] == []
            # The helper that spared the main process has ended by now, under a
            # name that begins with the container's.  That is not its own end.
            time.sleep(0.5)
            assert not container._watch._died.is_set() and container._watch._heard is False
            assert not json.loads(_runtime("inspect", container.name).stdout)[0]["Mounts"]
        finally:
            assert container.close() is True
        assert container.removed and _listed(container.name) == []

    def test_an_image_that_is_not_there_is_not_fetched(self):
        container = Container("this-image-does-not-exist-locally:0")
        with pytest.raises(ContainerError, match="could not start the container"):
            container.start()
        assert _listed(container.name) == []
        assert _runtime("image", "inspect", "this-image-does-not-exist-locally:0").returncode != 0


# A task that runs its commands as root over a writable root, with the
# capabilities root's ordinary work takes.
ROOT_TASK: dict[str, Any] = {
    "user": "0:0",
    "read_only_root": False,
    "writable": {},
    "workdir": "/testbed",
    "memory": "256m",
    "environment": {"TASK_NAME": "sample"},
    "capabilities": ("CHOWN", "DAC_OVERRIDE", "FOWNER", "KILL", "SETGID", "SETUID"),
}


def _sh(container: Container, script: str, *, timeout: float = 60) -> str:
    """Run *script* in *container*.  Scripts given here are destructive in places;
    the suite's guard lets the command line start only as the runtime's ``exec``."""
    done = container.exec(["sh", "-c", script], timeout=timeout)
    return (done.stdout + done.stderr).decode("utf-8", errors="replace").strip()


class TestWhatATaskMayAsk:
    @pytest.mark.parametrize("name", ["SYS_BOOT", "SYS_PTRACE", "SYS_ADMIN", "cap_sys_resource"])
    def test_a_capability_that_would_undo_the_container_is_refused(self, name):
        with pytest.raises(ValueError, match="is refused"):
            Container(ContainerSpec(image=IMAGE, capabilities=(name,)))

    def test_an_unknown_capability_is_refused_too(self):
        with pytest.raises(ValueError, match="not one a task may name"):
            Container(ContainerSpec(image=IMAGE, capabilities=("BPF",)))

    def test_no_setting_adds_a_network_or_a_host_mount(self):
        argv = Container(ContainerSpec(image=IMAGE, **ROOT_TASK)).run_argv()
        joined = " ".join(argv)
        assert "--network none" in joined and "--pull never" in joined
        assert "--volume" not in joined and "--mount" not in joined and "--read-only" not in argv
        assert "--cap-drop ALL" in joined and "--cap-add KILL" in joined
        assert "--env TASK_NAME=sample" in joined and "--workdir /testbed" in joined
        assert "HOME=" not in joined  # root keeps the image's home


# Settings that can be seen to be wrong without asking the runtime, and a part
# of what the refusal says.
COULD_NOT_BE: list[tuple[dict[str, Any], str]] = [
    ({"environment": {"": "x"}}, "an environment variable needs a name: '=x'"),
    ({"environment": {"A=B": "c"}}, "cannot hold '='"),
    ({"environment": {"A": "x" + chr(0) + "y"}}, "without a NUL character"),
    ({"environment": {"A": 3}}, "the value of A must be text"),
    ({"memory": "junk"}, "memory must be a size above zero, like 2g or 512m: 'junk'"),
    ({"memory": ""}, "memory must be a size above zero"),
    ({"memory": "0"}, "memory must be a size above zero"),
    ({"memory": "0.0g"}, "memory must be a size above zero"),
    ({"memory": "-1g"}, "memory must be a size above zero"),
    ({"memory": "1e3"}, "memory must be a size above zero"),
    ({"memory": "1_000"}, "memory must be a size above zero"),
    ({"memory": " 2g"}, "memory must be a size above zero"),
    ({"memory": "2  g"}, "memory must be a size above zero"),
    ({"memory": ".5g"}, "memory must be a size above zero"),
    ({"memory": "1.g"}, "memory must be a size above zero"),
    ({"memory": "2gg"}, "memory must be a size above zero"),
    ({"memory": "9" * 400}, "memory must be a size above zero"),
    ({"memory": 2}, "memory must be text"),
    ({"shm_size": "lots"}, "shm_size must be a size above zero"),
    ({"volume_size": "0m"}, "volume_size must be a size above zero"),
    ({"writable": {"/tmp": "big"}}, "the size of /tmp must be a size above zero"),
    ({"writable": {"tmp": "8m"}}, "must be an absolute path without a colon: 'tmp'"),
    ({"writable": {"/a:b": "8m"}}, "must be an absolute path without a colon"),
    ({"user": "no such"}, "user must be a name or a number"),
    ({"user": ""}, "'' is neither a name nor a number"),
    ({"user": "0:"}, "'' is neither a name nor a number"),
    ({"user": ":0"}, "'' is neither a name nor a number"),
    ({"user": "a:b:c"}, "more than one colon"),
    ({"user": "-x"}, "'-x' is neither a name nor a number"),
    ({"user": "$"}, "'$' is neither a name nor a number"),
    ({"user": "who/ever"}, "is neither a name nor a number"),
    ({"user": "4294967295"}, "above the largest number a user can have"),
    ({"user": "0:" + "9" * 5000}, "above the largest number a user can have"),
    ({"image": ""}, "image must name an image"),
    ({"image": "--privileged"}, "image must name an image"),
    ({"image": "an image"}, "image must name an image"),
    ({"cpus": 0}, "cpus must be a number above zero"),
    ({"cpus": -1.0}, "cpus must be a number above zero"),
    ({"cpus": float("nan")}, "cpus must be a number above zero"),
    ({"cpus": float("inf")}, "cpus must be a number above zero"),
    ({"cpus": True}, "cpus must be a number above zero"),
    ({"cpus": "1"}, "cpus must be a number above zero"),
    ({"pids_limit": 0}, "pids_limit must be a whole number above zero"),
    ({"pids_limit": -1}, "pids_limit must be a whole number above zero"),
    ({"pids_limit": 2.5}, "pids_limit must be a whole number above zero"),
    ({"pids_limit": True}, "pids_limit must be a whole number above zero"),
]

# Settings the runtime takes, whatever it then finds in the image.
COULD_BE: list[dict[str, Any]] = [
    {"memory": size}
    for size in ("512m", "2g", "1.5g", "1048576", "64mb", "1 g", "1gib", "8M", "3T", "6291456b")
]
COULD_BE += [
    {"user": user}
    for user in ("0:0", "root", "1000", "nobody:nogroup", "www-data", "svc$", "65534:65534")
]
COULD_BE += [
    {"user": "4294967294:4294967294"},
    {"user": "_apt"},
    {"user": "first.last"},
    {"environment": {"A": "", "PATH": "/x:/y"}},
    {"environment": {"with space": "a=b=c"}},
    {"shm_size": "1g", "volume_size": "16m", "writable": {"/data": "1.5g", "/tmp": "64mb"}},
    {"cpus": 0.25},
    {"cpus": 4},
    {"pids_limit": 1},
    {"image": "registry.example.invalid:5000/team/image@sha256:" + "a" * 64},
]


class TestAContainerThatCouldNotBe:
    """A setting that can be seen to be wrong without asking the runtime is
    refused before anything is started: a value the runtime would refuse, and
    one it would take to mean that there is no limit."""

    @pytest.mark.parametrize(("settings", "said"), COULD_NOT_BE, ids=range(len(COULD_NOT_BE)))
    def test_it_is_refused_before_anything_is_started(self, settings, said, tmp_path):
        spec = ContainerSpec(**{"image": IMAGE, **settings})
        with pytest.raises(ValueError) as refused:
            spec.validate()
        assert said in str(refused.value)
        # Making the container refuses it too, and asks the runtime nothing.
        with pytest.raises(ValueError):
            Container(spec, runtime=_scripted_runtime(tmp_path, ABSENT))
        assert not (tmp_path / "calls").exists()

    @pytest.mark.parametrize("settings", COULD_BE, ids=range(len(COULD_BE)))
    def test_what_the_runtime_takes_is_not_refused(self, settings):
        ContainerSpec(**{"image": IMAGE, **settings}).validate()

    def test_the_defaults_and_a_root_task_pass(self):
        ContainerSpec().validate()
        ContainerSpec(image=IMAGE, **ROOT_TASK).validate()

    @needs_container
    def test_a_user_name_the_image_does_not_hold_is_for_the_runtime_to_say(self):
        """A name is a name until the image is asked: the container is made,
        cannot run a command as that user, and is removed again."""
        spec = ContainerSpec(image=IMAGE, user="nosuchuser", memory="256m")
        spec.validate()
        container = Container(spec)
        with pytest.raises(ContainerError, match="could not start the container"):
            container.start()
        assert container.removed is True and _listed(container.name) == []


@needs_container
class TestARootContainer:
    """A task whose commands run as root.  Nothing may rest on the commands
    being another user than the container's main process."""

    @pytest.fixture
    def root(self):
        container = Container(ContainerSpec(image=IMAGE, **ROOT_TASK))
        try:
            yield container.start()
        finally:
            assert container.close() is True and _listed(container.name) == []

    def test_the_settings_are_the_tasks(self, root):
        shown = _sh(root, "id -u; pwd; echo $TASK_NAME; touch /usr/local/made-here && echo wrote")
        assert shown.split() == ["0", "/testbed", "sample", "wrote"]
        assert _sh(root, "touch new && stat -c %U new") == "root"

    def test_no_signal_from_a_root_shell_ends_it(self, root):
        _sh(root, "for n in $(seq 1 64); do kill -$n 1 2>/dev/null; done; echo sent")
        assert root.is_running()
        # Every process root may signal, which with the KILL capability is all of them.
        _sh(root, "kill -9 -1")
        time.sleep(0.5)
        assert root.is_running() and root.state()["exit_code"] == 0
        assert _sh(root, "echo still here") == "still here"

    def test_orphaned_processes_are_collected(self, root):
        """Processes that outlive what started them are handed to the main process;
        left uncollected they would pile up against the process limit."""
        before = int(_sh(root, "ls /proc | grep -c '^[0-9]'"))
        _sh(root, "for i in $(seq 40); do (setsid sh -c 'sleep 0.2' &); done")
        time.sleep(1.5)
        count = "z=0; for f in /proc/[0-9]*/stat; do s=$(cut -d' ' -f3 $f 2>/dev/null); "
        count += '[ "$s" = Z ] && z=$((z+1)); done; echo $z'
        assert _sh(root, count) == "0"
        assert int(_sh(root, "ls /proc | grep -c '^[0-9]'")) <= before + 1

    def test_it_ends_after_its_lifetime_with_nobody_there(self):
        """The runner is killed, say.  The lifetime is kept outside the container,
        where nothing inside can stretch or shorten it."""
        container = Container(ContainerSpec(image=IMAGE, **ROOT_TASK), max_lifetime=3).start()
        try:
            assert container.exists() is True
            deadline = time.monotonic() + 30
            while container.exists() and time.monotonic() < deadline:
                time.sleep(0.5)
            assert container.exists() is False
        finally:
            assert container.close() is True

    def test_ending_it_takes_a_deliberate_act_and_is_recorded_as_the_models(self, root):
        """This task's root may pass file permissions, so it can write the main
        process's out-of-memory score, make it the killer's first choice and then
        use up the memory in pieces smaller than that choice.  A task's own
        settings give that back; it is told apart from the runtime failing by what
        the runtime reports, and is the model's own outcome."""
        piece = "b = bytearray(12 * 1024 * 1024); import time; time.sleep(60)"
        _sh(
            root,
            "echo 1000 > /proc/1/oom_score_adj; "
            f"for i in $(seq 40); do python3 -c '{piece}' & done; wait",
        )
        with pytest.raises(ContainerLostError):
            for _ in range(50):
                root.ensure_running()
                time.sleep(0.2)
        assert root.lost_cause == "model" and root.exit_code == 137
        assert "out-of-memory killer took it" in (root.lost or "")
        # It is still there to be read from until it is removed.
        assert root.exists() is True

    def test_a_writable_root_that_outgrows_its_size_is_stopped(self):
        spec = ContainerSpec(image=IMAGE, **{**ROOT_TASK, "root_size": "20m"})
        container = Container(spec)
        container.size_check_seconds = 0.5
        try:
            container.start()
            # Enforced by the storage driver where it can; watched where it cannot.
            assert container.root_bound in ("enforced", "watched")
            _sh(container, "head -c 60000000 /dev/urandom > /testbed/big; echo rc=$?")
            if container.root_bound == "watched":
                with pytest.raises(ContainerLostError):
                    for _ in range(100):
                        container.ensure_running()
                        time.sleep(0.2)
                assert container.lost_cause == "model"
                assert "more than the 20,971,520 it may hold" in (container.lost or "")
            else:
                assert int(_sh(container, "stat -c %s /testbed/big")) < 60000000
        finally:
            assert container.close() is True


@needs_container
class TestTheMainProcess:
    """What a task's commands can do to the container's main process, measured in
    containers: nothing, unless the task's own settings give it back."""

    def _container(self, **settings: Any) -> Container:
        settings.setdefault("memory", "256m")
        return Container(ContainerSpec(image=IMAGE, **settings))

    def test_it_is_sleep_under_a_user_of_its_own(self):
        with self._container() as container:
            facts = _sh(container, "cat /proc/1/comm; grep '^Uid' /proc/1/status; id -u")
            assert facts.split() == ["sleep", "Uid:", "65533", "65533", "65533", "65533", "65534"]
            described = container.describe()
            assert described["main_process"] == "sleep"
            assert described["main_process_reach"] is None
        # A task whose commands run as that very user gets another one for it.
        clash = self._container(user="65533:65533")
        assert clash.main_user == "65532:65532"
        with clash:
            assert _sh(clash, "grep '^Uid' /proc/1/status; id -u").split()[1::4] == [
                "65532",
                "65533",
            ]

    def test_root_without_capabilities_cannot_limit_it_or_write_its_score(self):
        """What ended a container before: a root command lowering the main
        process's processor limit.  The main process is another user now."""
        limit = "import resource; resource.prlimit(1, resource.RLIMIT_CPU, (1, 1))"
        with self._container(user="0:0", read_only_root=False, writable={}) as container:
            refused = _sh(container, f'python3 -c "{limit}"; echo rc=$?')
            assert "Operation not permitted" in refused and "rc=1" in refused
            written = _sh(container, "echo 1000 > /proc/1/oom_score_adj; cat /proc/1/oom_score_adj")
            assert "Permission denied" in written and "-1000" in written.split()
            assert container.describe()["main_process_reach"] is None
            assert container.is_running()

    def test_a_limit_on_it_ends_nothing_even_set_as_its_own_user(self):
        """Root with the capability to become the main process's user can set its
        limits.  A processor limit ends a process that uses the processor; this
        one never runs again."""
        attack = NL.join(
            [
                "import os, resource, time",
                "os.setgid(65533); os.setuid(65533)",
                "resource.prlimit(1, resource.RLIMIT_CPU, (1, 1))",
                "print(resource.prlimit(1, resource.RLIMIT_CPU))",
                "end = time.time() + 4",
                "while time.time() < end:",
                "    os.kill(1, 9)",
            ]
        )
        container = Container(ContainerSpec(image=IMAGE, **ROOT_TASK))
        ticks = "cut -d' ' -f14,15 /proc/1/stat"
        with container:
            before = _sh(container, ticks)
            assert _sh(container, f"python3 -c '{attack}'") == "(1, 1)"
            time.sleep(0.5)
            assert container.is_running() and container.state()["exit_code"] == 0
            # Not one tick of processor time since it started, in user or kernel,
            # and what its start took is far from the second the limit allows.
            assert _sh(container, ticks) == before
            assert sum(int(t) for t in before.split()) < 50
            # The record says that this task's settings reach the main process.
            assert "SETUID" in container.describe()["main_process_reach"]

    def test_a_command_cannot_spare_itself_from_the_out_of_memory_killer(self):
        """Only the main process is spared.  A command that could spare itself
        would be left standing with all the memory, and nothing could run."""
        hog = "b = []" + NL + "while True: b.append(bytearray(16 * 1024 * 1024))"
        spare = "echo -1000 > /proc/self/oom_score_adj; cat /proc/self/oom_score_adj"
        with self._container(user="0:0", read_only_root=False, writable={}) as container:
            for user in ("0:0", "65534:65534"):
                done = container.exec(["sh", "-c", spare], timeout=30, user=user)
                assert done.stdout.split() == [b"0"] and done.stderr.strip()
            taken = _sh(container, f"echo -1000 > /proc/self/oom_score_adj; python3 -c '{hog}'")
            assert "Killed" in taken
            assert container.is_running() and container.lost is None
            assert _sh(container, "echo and here") == "and here"
            assert container.describe()["oom_kills"] >= 1

    # What the main process waits for: every process whose parent it is, with
    # its command line and its out-of-memory score.
    WAITED_FOR = (
        "for s in /proc/[0-9]*/status; do if grep -q '^PPid:.1$' $s 2>/dev/null; then "
        'd=${s%/status}; echo "${d#/proc/} $(xargs -0 < $d/cmdline) $(cat $d/oom_score_adj)"; '
        "fi; done"
    )

    def test_where_the_image_offers_no_way_to_it_a_shell_stays_and_collects(self, monkeypatch):
        """An image without any of the ways to a ``sleep`` that ignores its ended
        children: the shell itself is the main process, as the record says."""
        only_the_shell = container_module._MAIN_TRAPS + container_module._SHELL_AS_MAIN
        monkeypatch.setattr(container_module, "_MAIN_PROCESS", only_the_shell)
        with self._container(user="0:0", read_only_root=False, writable={}) as container:
            described = container.describe()
            assert described["main_process"] == "shell"
            assert "is a shell that starts programs" in described["main_process_reach"]
            assert _sh(container, "cat /proc/1/oom_score_adj") == "-1000"
            _sh(container, "for i in $(seq 20); do (setsid sh -c 'sleep 0.2' &); done")
            time.sleep(1.5)
            count = "z=0; for f in /proc/[0-9]*/stat; do s=$(cut -d' ' -f3 $f 2>/dev/null); "
            count += '[ "$s" = Z ] && z=$((z+1)); done; echo $z'
            assert _sh(container, count) == "0"
        with self._container() as contained:
            # Over a read-only root nobody replaces what it starts.
            assert contained.describe()["main_process"] == "shell"
            assert contained.describe()["main_process_reach"] is None

    def test_such_a_shell_starts_nothing_again_while_commands_run(self, monkeypatch):
        """A shell that cannot start a process gives up, and at the process limit
        it cannot.  One that started ``sleep`` anew every hour ended the container
        whenever the commands held the limit at that moment, as a failure of the
        machinery.  It waits for one ``sleep`` that does not end, spared from the
        out-of-memory killer like the shell itself."""
        only_the_shell = container_module._MAIN_TRAPS + container_module._SHELL_AS_MAIN
        monkeypatch.setattr(container_module, "_MAIN_PROCESS", only_the_shell)
        with self._container(pids_limit=32) as container:
            assert container.describe()["main_process"] == "shell"
            deadline = time.monotonic() + 20
            waited_for = _sh(container, self.WAITED_FOR)
            while not waited_for.endswith("sleep 2147483647 -1000"):
                assert time.monotonic() < deadline, waited_for
                time.sleep(0.2)
                waited_for = _sh(container, self.WAITED_FOR)
            assert len(waited_for.split(NL)) == 1
            # The commands' user takes every process the limit leaves, again
            # and again, for a few seconds.  Nothing else could start then.
            hold = NL.join(
                [
                    "import os, time",
                    "held = refused = 0",
                    "end = time.time() + 3",
                    "while time.time() < end:",
                    "    try:",
                    "        pid = os.fork()",
                    "    except OSError:",
                    "        refused += 1",
                    "        continue",
                    "    if pid == 0:",
                    "        time.sleep(max(end - time.time(), 0))",
                    "        os._exit(0)",
                    "    held += 1",
                    "print(held > 20 and refused > 100)",
                ]
            )
            assert _sh(container, f'python3 -c "{hold}"') == "True"
            time.sleep(1.0)
            assert container.is_running() and container.lost is None
            # The same process as before: it was never started again.
            assert _sh(container, self.WAITED_FOR) == waited_for


@pytest.fixture
def image_with_volumes():
    """The test image with two directories declared as volumes."""
    unique = uuid.uuid4().hex[:10]
    tag = "tsgym-test-volumes:" + unique
    created = "tsgym-test-volumes-" + unique
    made = _runtime("create", "--name", created, "--pull", "never", IMAGE, "true")
    assert made.returncode == 0, made.stderr
    try:
        changed = ["--change", "VOLUME /data", "--change", "VOLUME /var/lib/extra"]
        committed = _runtime("commit", *changed, created, tag)
        assert committed.returncode == 0, committed.stderr
        yield tag
    finally:
        _runtime("rm", "--force", created)
        _runtime("rmi", tag)


@needs_container
class TestAnImagesVolumes:
    """A directory an image declares as a volume is, left to the runtime, a
    writable directory on this machine's disk, without a size and still there
    when the container is gone."""

    def test_they_are_memory_backed_with_a_size_and_leave_nothing(self, image_with_volumes):
        spec = ContainerSpec(image=image_with_volumes, memory="256m", volume_size="8m")
        container = Container(spec)
        try:
            container.start()
            assert container.volumes == ["/data", "/var/lib/extra"]
            inspected = json.loads(_runtime("inspect", container.name).stdout)[0]
            # No volume of the runtime's, and a size on each directory.
            assert inspected["Mounts"] == []
            assert "size=8m" in inspected["HostConfig"]["Tmpfs"]["/data"]
            assert "size=8m" in inspected["HostConfig"]["Tmpfs"]["/var/lib/extra"]
            filled = _sh(container, "head -c 50000000 /dev/zero > /data/fill; echo rc=$?")
            assert "rc=1" in filled and "No space left on device" in filled
            described = container.describe()
            assert described["volumes"] == ["/data", "/var/lib/extra"]
            assert described["writable"]["/data"] == "8m"
        finally:
            assert container.close() is True
        assert _listed(container.name) == []

    def test_a_size_the_task_names_for_the_path_wins(self, image_with_volumes):
        spec = ContainerSpec(
            image=image_with_volumes, memory="256m", writable={"/data": "24m", "/tmp": "8m"}
        )
        container = Container(spec)
        container.volumes = ["/data", "/var/lib/extra"]
        joined = " ".join(container.run_argv())
        assert "--tmpfs /data:rw,exec,nosuid,size=24m,mode=1777" in joined
        assert "--tmpfs /var/lib/extra:rw,exec,nosuid,size=64m,mode=1777" in joined

    def test_removal_takes_the_volumes_along(self, tmp_path):
        """Whatever volume a container has after all goes with it."""
        container = Container(IMAGE, runtime=_scripted_runtime(tmp_path, ABSENT)).start()
        assert container.close() is True
        calls = (tmp_path / "calls").read_text().split(NL)
        assert f"rm --force --volumes {container.name}" in calls

    def test_an_image_whose_volumes_cannot_be_read_is_not_started(self, tmp_path):
        """Unnoticed, a volume would be an unbounded directory on this machine."""
        path = tmp_path / "runtime"
        path.write_text("#!/bin/sh" + NL + f'echo "$*" >> "{tmp_path}/calls"' + NL + "exit 1" + NL)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        container = Container(IMAGE, runtime=str(path))
        with pytest.raises(ContainerError, match="could not be read"):
            container.start()
        calls = (tmp_path / "calls").read_text().split(NL)
        assert not any(call.startswith("run ") for call in calls)


@needs_container
class TestAWatchedRootSize:
    def test_a_file_deleted_while_open_counts(self):
        """No listing of the file system shows it, and it takes room all the same."""
        spec = ContainerSpec(image=IMAGE, **{**ROOT_TASK, "root_size": "20m"})
        container = Container(spec)
        container.size_check_seconds = 0.5
        try:
            container.start()
            if container.root_bound != "watched":
                pytest.skip("this machine's storage driver enforces the size itself")
            hold = "exec 3>/testbed/big; rm /testbed/big; head -c 60000000 /dev/zero >&3; "
            hold += "exec sleep 600"
            _sh(container, f"setsid sh -c '{hold}' </dev/null >/dev/null 2>&1 & sleep 1")
            with pytest.raises(ContainerLostError):
                for _ in range(100):
                    container.ensure_running()
                    time.sleep(0.2)
            assert container.lost_cause == "model"
            assert "more than the 20,971,520 it may hold" in (container.lost or "")
            assert "in files deleted while open" in (container.lost or "")
        finally:
            assert container.close() is True

    def test_held_still_by_the_runtime_the_root_is_measured_all_the_same(self):
        """While the runtime holds a container still it answers what the
        watch asks of it; only the look from inside is refused, and counts
        nothing for that look.  So holding a container still, as before
        every after-run command, never makes its root one that can no
        longer be measured, which would stop it as the model's doing."""
        spec = ContainerSpec(image=IMAGE, **{**ROOT_TASK, "root_size": "20m"})
        container = Container(spec)
        container.size_check_seconds = 0.2
        container.size_unknown_seconds = 1.0
        try:
            container.start()
            assert _runtime("pause", container.name).returncode == 0
            try:
                held = container._held()
                assert isinstance(held, tuple) and held[1] == 0 and held[2] > 0
                time.sleep(2.5)  # looks went on all the while
                assert container._over_size is None
            finally:
                assert _runtime("unpause", container.name).returncode == 0
            assert container.is_running() and container.lost is None
            assert container._size_watch is not None and container._size_watch.is_alive()
        finally:
            assert container.close() is True


# File systems that are memory or the kernel's own: nothing of this machine's disk.
NOT_ON_DISK = {"proc", "sysfs", "tmpfs", "devtmpfs", "devpts", "mqueue", "cgroup", "cgroup2"}


def _mounts(container: Container) -> dict[str, tuple[str, str]]:
    """Every mount in *container*: where it is, with its file system's kind and
    whether it is mounted read-only or writable."""
    found: dict[str, tuple[str, str]] = {}
    for line in _sh(container, "cat /proc/self/mountinfo").split(NL):
        before, _, after = line.partition(" - ")
        fields = before.split()
        found[fields[4]] = (after.split()[0], fields[5].split(",")[0])
    return found


@needs_container
class TestTheRuntimesOwnFiles:
    """``/etc/hosts``, ``/etc/hostname`` and ``/etc/resolv.conf`` are files the
    runtime keeps on this machine's disk and mounts into every container: the
    only thing of this machine's a container holds.  Nothing a command can
    write may be outside every bound, so over a writable root they count toward
    its size."""

    @pytest.mark.parametrize("path", RUNTIME_FILES)
    def test_what_root_appends_to_one_of_them_counts_toward_the_roots_size(self, path):
        """The root's own file system stays empty: a bound on that alone would
        let the file grow until this machine's disk is full."""
        spec = ContainerSpec(image=IMAGE, **{**ROOT_TASK, "root_size": "20m"})
        container = Container(spec)
        container.size_check_seconds = 0.5
        try:
            container.start()
            assert container.root_bound in ("enforced", "watched")
            # More than the root may hold, into a container made for this test.
            _sh(container, f"head -c 30000000 /dev/zero >> {path}; echo rc=$?")
            with pytest.raises(ContainerLostError):
                for _ in range(100):
                    container.ensure_running()
                    time.sleep(0.2)
            assert container.lost_cause == "model"
            assert "more than the 20,971,520 it may hold" in (container.lost or "")
            assert "which the runtime keeps on this machine's disk" in (container.lost or "")
            assert container.is_running() is False
        finally:
            assert container.close() is True
        assert _listed(container.name) == []

    def test_a_writable_root_without_a_size_of_its_own_has_the_default(self):
        container = Container(ContainerSpec(image=IMAGE, **ROOT_TASK))
        try:
            container.start()
            described = container.describe()
            assert (described["root_size"], described["root_size_source"]) == ("10g", "default")
            assert described["root_bound"] in ("enforced", "watched")
            # Small writes are nobody's business.
            assert _sh(container, "echo 127.0.0.2 other >> /etc/hosts; echo rc=$?") == "rc=0"
            time.sleep(container.size_check_seconds + 1.0)
            assert container.is_running() and container.lost is None
        finally:
            assert container.close() is True

    def test_only_root_can_write_them_and_only_over_a_writable_root(self):
        write = "; ".join(f"echo x >> {path}" for path in RUNTIME_FILES)
        unprivileged = ContainerSpec(image=IMAGE, read_only_root=False, writable={}, memory="256m")
        with Container(unprivileged) as container:
            assert _sh(container, write).count("Permission denied") == 3
        over_a_read_only_root = _spec(user="0:0", memory="256m")
        with Container(over_a_read_only_root) as container:
            assert _sh(container, write).count("Read-only file system") == 3
            mounts = _mounts(container)
            assert [mounts[path][1] for path in RUNTIME_FILES] == ["ro", "ro", "ro"]

    @pytest.mark.parametrize("settings", [{}, ROOT_TASK], ids=["default", "root"])
    def test_nothing_else_of_this_machine_is_in_a_container(self, settings):
        """Besides its own root: memory, the kernel's own file systems, and the
        runtime's three files."""
        with Container(ContainerSpec(image=IMAGE, **{"memory": "256m", **settings})) as container:
            mounts = _mounts(container)
        on_disk = {where for where, (kind, _mode) in mounts.items() if kind not in NOT_ON_DISK}
        assert on_disk == {"/", *RUNTIME_FILES}


@needs_container
class TestToolsInside:
    def test_commands_and_files_are_the_containers(self, box):
        container, _env, _session, run = box
        shown = run("bash", command="cat /etc/hostname; test -f /.dockerenv && echo inside; pwd")
        assert shown.split(NL) == [container.container_id[:12], "inside", "/workspace"]
        # Not root, with a home it can write to.
        assert run("bash", command="id -u; echo $HOME; touch ~/.probe && echo ok") == NL.join(
            ["65534", "/workspace", "ok"]
        )

        assert run("write_file", path="src/a.py", content="x = 1" + NL + "y = 2" + NL) == (
            "Wrote 12 chars to src/a.py"
        )
        assert run("read_file", path="src/a.py") == f"   1{TAB}x = 1{NL}   2{TAB}y = 2"
        assert run("edit_file", path="src/a.py", old_string="y = 2", new_string="y = 3") == (
            "Edited src/a.py: applied 1 edit"
        )
        assert "+y = 3" in run("diff_file", path_a="src/a.py", content_b="x = 1" + NL + "y = 2")
        assert "src/a.py:2:y = 3" in run("search", query="y = ", path=".")
        # Also when the search is given the one file and runs its fallback program.
        assert "src/a.py:2:y = 3" in run("search", query="y = ", path="src/a.py")
        assert run("bash", command="cat src/a.py") == "x = 1" + NL + "y = 3"
        # Nothing of it exists on this machine.
        assert not os.path.exists("src/a.py") and not os.path.exists("/workspace/src/a.py")

    def test_errors_read_like_the_hosts(self, box):
        _container, _env, _session, run = box
        assert run("read_file", path="missing.txt") == "Error: missing.txt not found"
        assert (
            run("read_file", path="/etc") == "Error reading /etc: [Errno 21] Is a directory: '/etc'"
        )
        assert run("write_file", path="/etc/nope", content="x") == (
            "Error writing /etc/nope: [Errno 30] Read-only file system: '/etc/nope'"
        )

    def test_text_with_wide_characters_comes_back_whole(self, box):
        _container, _env, _session, run = box
        text = (chr(233) * 79 + NL) * 400  # 2 bytes per character, 64,000 bytes
        run("write_file", path="wide.txt", content=text)
        read = run("read_file", path="wide.txt")
        assert chr(0xFFFD) not in read
        assert read.count(chr(233)) == 79 * 400

    def test_a_file_too_large_to_read_whole_is_an_error_not_a_shortened_text(self, box):
        _container, _env, _session, run = box
        run("bash", command="yes line | head -c 300000 > big.txt")
        read = run("read_file", path="big.txt")
        assert read.startswith("Error reading big.txt: [Errno 27] file is larger than 200000 bytes")
        assert "omitted" not in read and TAB not in read

    def test_a_read_that_never_ends_is_stopped_inside_the_container(self, box):
        _container, _env, _session, run = box
        run("bash", command="mkfifo pipe")
        started = time.monotonic()
        read = run("read_file", path="pipe")
        assert "did not finish within 3 s" in read
        assert time.monotonic() - started < 15
        left = run("bash", command="for p in /proc/[0-9]*; do cat $p/comm; done | grep -cx head")
        assert left.split(NL)[0] == "0"

    def test_writes_are_bounded(self, box):
        _container, _env, _session, run = box
        filled = run("bash", command="head -c 100000000 /dev/zero > fill; echo rc=$?")
        assert "rc=1" in filled and "No space left on device" in filled
        assert run("write_file", path="more.txt", content="z" * 100_000) == (
            "Error writing more.txt: [Errno 28] No space left on device: '/workspace/more.txt'"
        )
        assert "Read-only file system" in run("bash", command="touch /usr/x")
        # Shared memory is the one other writable place, and it has its size too.
        shm = run("bash", command="head -c 100000000 /dev/zero > /dev/shm/fill; echo rc=$?")
        assert "rc=1" in shm and "No space left on device" in shm

    def test_the_session_does_not_build_what_the_model_multiplies(self, box):
        """A replace-all edit and a diff are worked out in the session's process, not
        in the container, so the container's limits do not bound them."""
        _container, _env, _session, run = box
        run("bash", command="head -c 150000 /dev/zero | tr '" + chr(92) + "0' a > a.txt")
        run("read_file", path="a.txt")
        edit = run("edit_file", path="a.txt", old_string="a", new_string="b" * 50, replace_all=True)
        assert edit.startswith("Error: the edit would make a.txt 7,500,000 characters long")
        assert run("bash", command="wc -c < a.txt") == "150000"

        run("bash", command="seq 1 2500 > one.txt; seq 2 2501 > two.txt")
        diff = run("diff_file", path_a="one.txt", path_b="two.txt")
        assert diff.startswith("Error: diff_file compares up to 4,000 lines here")

    def test_there_is_no_network(self, box):
        _container, _env, _session, run = box
        probe = (
            "python3 -c 'import socket; socket.create_connection((" + '"192.0.2.1"' + ", 80), 2)'"
        )
        assert "unreachable" in run("bash", command=probe).lower()

    def test_what_a_command_leaves_running_ends_with_it(self, box):
        _container, _env, _session, run = box
        started = time.monotonic()
        assert run("bash", command="sleep 300 &" + NL + "echo returned") == "returned"
        assert time.monotonic() - started < 5
        timed_out = run("bash", command="sleep 300 & echo begin; sleep 300", timeout=2)
        assert timed_out.startswith("Command timed out after 2s") and "begin" in timed_out
        sleeping = run(
            "bash", command="for p in /proc/[0-9]*; do cat $p/comm; done | grep -cx sleep"
        )
        assert sleeping.split(NL)[0] == "1"  # the container's own, nothing else

    def test_a_background_shell_runs_inside_and_can_be_killed(self, box):
        _container, _env, _session, run = box
        started = run("bash", command="echo up; sleep 300", run_in_background=True)
        # No process id of this machine in what the model reads.
        assert started.startswith("Started background shell bash_1. Read new output")
        deadline = time.monotonic() + 10
        output = ""
        while "up" not in output and time.monotonic() < deadline:
            output += run("bash_output", id="bash_1")
            time.sleep(0.1)
        assert "up" in output
        began = time.monotonic()
        assert "Killed background shell bash_1" in run("kill_shell", id="bash_1")
        assert time.monotonic() - began < 5
        sleeping = run(
            "bash", command="for p in /proc/[0-9]*; do cat $p/comm; done | grep -cx sleep"
        )
        assert sleeping.split(NL)[0] == "1"

    def test_nothing_a_command_does_ends_the_container(self, box):
        """Ending the container must not be something the model can do: a rollout
        whose sandbox is lost is not trained on, and the model would learn to
        leave the episodes that go badly."""
        container, _env, _session, run = box
        # Every process the user may signal, the common clean-up idiom.
        run("bash", command="kill -9 -1")
        assert run("bash", command="echo still here") == "still here"
        # The main process itself.
        assert "not permitted" in run("bash", command="kill -9 1")
        # All the memory there is: the command is taken, not the container.
        hog = "b = []" + NL + "while True: b.append(bytearray(16 * 1024 * 1024))"
        run("bash", command=f"python3 -c '{hog}'; echo rc=$?")
        assert container.is_running() and container.lost is None
        assert run("bash", command="echo and here") == "and here"
        assert container.describe()["oom_kills"] >= 1

    def test_a_stopped_container_is_reported_not_passed_off_as_an_exit_code(self, box):
        container, _env, _session, run = box
        _runtime("kill", container.name)  # the runtime ends it; nothing inside can

        after = run("bash", command="echo after")
        read = run("read_file", path="anything.txt")
        assert after == (
            "Error executing command: [Errno 5] the sandbox has stopped: no further command "
            "or file operation can run in it"
        )
        assert "the sandbox has stopped" in read
        assert container.name not in after + read
        assert container.lost == (
            "the container's main process ended with exit code 137: the runtime was told "
            "to end it, from outside the container"
        )
        assert container.lost_cause == "machinery" and container.exit_code == 137


@pytest.fixture(autouse=True)
def _no_configuration_file(monkeypatch):
    """The limits a rollout takes from the framework come from its defaults
    here, not from whatever configuration file this machine has."""
    monkeypatch.setattr(config_module, "_cache", {})


# A small container, and sampling a trainer can reproduce on a server known to
# report the sampling distribution.
SMALL: dict[str, Any] = {
    "context_window": 100_000,
    "extra_body": {"top_k": 20, "top_p": 0.95},
    "sampler_logprobs": True,
}


@needs_container
class TestRolloutInAContainer:
    def test_a_sub_agent_acts_in_the_same_container(self):
        parent = [
            {"tool_calls": [{"name": "task_agent", "arguments": {"prompt": "Name the host."}}]},
            {"tool_calls": [{"name": "bash", "arguments": {"command": "cat by-agent.txt"}}]},
            {"content": "finished"},
        ]
        agent = [
            {
                "tool_calls": [
                    {
                        "name": "bash",
                        "arguments": {"command": "cat /etc/hostname | tee by-agent.txt; id -u"},
                    }
                ]
            },
            {"content": "named"},
        ]
        with ScriptedChatServer(parent, agent_script=agent) as server:
            record = run_episode(
                Task(prompt="delegate", container=_spec(memory="512m")),
                RolloutConfig(base_url=server.base_url, model="scripted", **SMALL),
            )
            agent_second = server.agent_requests[1]

        assert record["finish_state"] == "completed", record["error"]
        short_id = record["sandbox"]["container_id"][:12]
        # The sub-agent's command printed the container's own name, as an unprivileged
        # user, and the file it left is the one the parent then read there.
        (agent_result,) = [m["content"] for m in agent_second["messages"] if m["role"] == "tool"]
        assert agent_result == NL.join([short_id, "65534"])
        assert [e["result"] for e in record["tool_log"]] == ["named", short_id]
        assert record["subagents"]["started"] == 1 and record["subagents"]["model_calls"] == 2
        assert record["trainable"] and record["one_sequence"]
        assert record["sandbox"]["removed"] is True and _listed(record["sandbox"]["name"]) == []

    def test_tool_calls_run_inside_and_the_container_is_gone_afterwards(self, containers_made):
        script = [
            {
                "reasoning": "Look around.",
                "tool_calls": [
                    {
                        "name": "bash",
                        "arguments": {
                            "command": "cat /etc/hostname; cat seed.txt; echo made > made.txt"
                        },
                    }
                ],
            },
            {
                "reasoning": "Change it.",
                "tool_calls": [
                    {"name": "read_file", "arguments": {"path": "made.txt"}},
                    {
                        "name": "edit_file",
                        "arguments": {
                            "path": "made.txt",
                            "old_string": "made",
                            "new_string": "done",
                        },
                    },
                ],
            },
            {"tool_calls": [{"name": "bash", "arguments": {"command": "cat made.txt"}}]},
            {"content": "finished"},
        ]
        with ScriptedChatServer(script) as server:
            record = run_episode(
                Task(
                    prompt="look around",
                    setup_files={"seed.txt": "seeded" + NL},
                    container=_spec(memory="512m"),
                ),
                RolloutConfig(base_url=server.base_url, model="scripted", **SMALL),
            )

        assert record["finish_state"] == "completed", record["error"]
        sandbox = record["sandbox"]
        log = record["tool_log"]
        assert [e["tool"] for e in log] == ["bash", "read_file", "edit_file", "bash"]
        assert log[0]["result"] == NL.join([sandbox["container_id"][:12], "seeded"])
        assert log[3]["result"] == "done"
        assert all(e["ok"] for e in log)
        assert record["prefix_ok"] and record["trainable"] and record["warnings"] == []
        assert sandbox["network"] == "none" and sandbox["removed"] is True
        assert sandbox["name"].startswith(NAME_PREFIX) and _listed(sandbox["name"]) == []
        # A read-only root has no size to keep.
        assert (sandbox["root_size"], sandbox["root_size_source"]) == (None, None)
        assert sandbox["root_bound"] is None
        # One container was made for the rollout, and no other.
        assert containers_made == [sandbox["name"]]

    def test_the_record_says_the_size_a_writable_root_was_bounded_to(self):
        """A task that names no size: the default, and the record says that it is."""
        script = [
            {
                "tool_calls": [
                    {"name": "bash", "arguments": {"command": "echo made > /usr/local/made; id -u"}}
                ]
            },
            {"content": "finished"},
        ]
        with ScriptedChatServer(script) as server:
            record = run_episode(
                Task(prompt="write", container=ContainerSpec(image=IMAGE, **ROOT_TASK)),
                RolloutConfig(base_url=server.base_url, model="scripted", **SMALL),
            )
        assert record["finish_state"] == "completed", record["error"]
        assert record["trainable"] and record["tool_log"][0]["result"] == "0"
        sandbox = record["sandbox"]
        assert (sandbox["root_size"], sandbox["root_size_source"]) == ("10g", "default")
        assert sandbox["root_bound"] in ("enforced", "watched")
        # The task is recorded as it was given: it named no size.
        assert record["task"]["container"]["root_size"] is None
        assert sandbox["removed"] is True and _listed(sandbox["name"]) == []

    def test_a_command_that_kills_everything_is_the_models_outcome(self):
        """What used to end the container, and with it take the rollout out of
        training: now an ordinary command with an ordinary result."""
        script = [
            {"tool_calls": [{"name": "bash", "arguments": {"command": "kill -9 -1"}}]},
            {"tool_calls": [{"name": "bash", "arguments": {"command": "echo still here"}}]},
            {"content": "carried on"},
        ]
        with ScriptedChatServer(script) as server:
            record = run_episode(
                Task(prompt="break it", container=_spec(memory="512m")),
                # Without the rule that would refuse it: the command really runs.
                RolloutConfig(base_url=server.base_url, model="scripted", rules=None, **SMALL),
            )
        assert record["finish_state"] == "completed" and record["finish_cause"] == "model"
        assert record["trainable"] is True and record["problems"] == []
        assert record["tool_log"][1]["result"] == "still here"
        assert record["sandbox"]["lost"] is None and record["sandbox"]["removed"] is True

    def test_a_rollout_whose_container_the_runtime_stops_ends_as_lost(self):
        script = [
            {"tool_calls": [{"name": "bash", "arguments": {"command": "sleep 20"}}]},
            {"tool_calls": [{"name": "bash", "arguments": {"command": "echo still here"}}]},
            {"content": "never reached"},
        ]
        made: list[ContainerSandbox] = []

        def factory(task: Task, config: RolloutConfig) -> ContainerSandbox:
            made.append(ContainerSandbox(task, config))
            return made[-1]

        def stop_it() -> None:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline and not (made and made[0].container.started):
                time.sleep(0.1)
            time.sleep(1.5)
            _runtime("kill", made[0].container.name)

        with ScriptedChatServer(script) as server:
            stopper = threading.Thread(target=stop_it, daemon=True)
            stopper.start()
            record = run_episode(
                Task(prompt="work", container=_spec(memory="512m")),
                RolloutConfig(base_url=server.base_url, model="scripted", **SMALL),
                sandbox_factory=factory,
            )
            stopper.join(timeout=30)
            requests = len(server.requests)

        assert record["finish_state"] == "sandbox_lost" and record["finish_cause"] == "machinery"
        assert requests == 1 and record["turns"] == 1
        assert record["trainable"] is False
        assert record["problems"] == [
            "the sandbox was lost during the run (the container's main process ended with "
            "exit code 137: the runtime was told to end it, from outside the container)"
        ]
        assert record["sandbox"]["exit_code"] == 137
        assert record["sandbox"]["removed"] is True
        assert _listed(record["sandbox"]["name"]) == []

    def test_the_container_is_removed_when_the_rollout_times_out(self):
        with ScriptedChatServer([{"content": "late"}], delay=4.0) as server:
            record = run_episode(
                Task(prompt="slow", timeout=2, container=_spec(memory="512m")),
                RolloutConfig(base_url=server.base_url, model="scripted", **SMALL),
            )
        assert record["finish_state"] == "timeout"
        assert record["sandbox"]["removed"] is True
        assert _listed(record["sandbox"]["name"]) == []
