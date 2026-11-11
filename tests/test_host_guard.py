"""The suite's guard on what is started on the machine the tests run on
(``tests/_host_guard.py``, installed for every test by ``conftest.py``).

Nothing here hands a destructive command to anything.  A guard is shown to
refuse with a harmless program (``true``, ``echo``, ``touch`` of a file under
the test's temporary directory, ``mv --version``), and the list of destructive
shapes is made, for one test, to refuse such a harmless command.  If a guard
did not refuse, what would run is that harmless program.  Command lines that
name something destructive appear only as data for the function that reads a
start, which starts nothing.

No signal is sent here but signal 0, which delivers nothing, and real
signals to a ``sleep`` the test started itself.  A signal to an id of 1 or
below, to a stand-in object, or to a process the test did not start is shown
refused on the function that judges, as data, and with signal 0.
"""

from __future__ import annotations

import asyncio
import builtins
import contextlib
import functools
import operator
import os
import pathlib
import posix
import shutil
import signal
import stat
import subprocess
import sys
import types
from typing import Any
from unittest.mock import MagicMock

import pytest

from tests import _host_guard
from tests._host_guard import (
    ABSENT,
    OTHER,
    OURS,
    PROCESS_MARK,
    UNKNOWN,
    DestructiveCommandError,
    ProcessView,
    Start,
    signal_refusal,
    start_shape,
    started_program,
)
from turnstone.core import background_shells
from turnstone.core.watch import WatchRunner

NL = chr(10)
ROOT = pathlib.Path(__file__).resolve().parent.parent
TRUE = shutil.which("true") or "/bin/true"
TOUCH = shutil.which("touch") or "/usr/bin/touch"
SLEEP = shutil.which("sleep") or "/bin/sleep"
# The host's process start, held the way a module holds it that imported the
# function by name: taken before any test wrapped the module's attribute.
SPAWN_BY_NAME = background_shells.spawn_group_leader

# A module that starts a process in every way there is.  ``os.system`` and
# ``shell=True`` are here on purpose: the guard has to stand in front of
# exactly these calls.  What they are handed in the tests is a fixed harmless
# command of the test's own, never outside input.
PROBE = """
import os
import subprocess
from subprocess import Popen, call, check_call, check_output, getoutput, getstatusoutput, run


def popen(argv):
    return subprocess.Popen(argv).wait()


def popen_by_name(argv):
    return Popen(argv).wait()


def run_(argv):
    return subprocess.run(argv).returncode


def run_by_name(argv):
    return run(argv).returncode


def call_(argv):
    return subprocess.call(argv)


def call_by_name(argv):
    return call(argv)


def check_call_(argv):
    return subprocess.check_call(argv)


def check_call_by_name(argv):
    return check_call(argv)


def check_output_(argv):
    return subprocess.check_output(argv)


def check_output_by_name(argv):
    return check_output(argv)


def getoutput_(argv):
    return subprocess.getoutput(" ".join(argv))


def getoutput_by_name(argv):
    return getoutput(" ".join(argv))


def getstatusoutput_(argv):
    return subprocess.getstatusoutput(" ".join(argv))


def getstatusoutput_by_name(argv):
    return getstatusoutput(" ".join(argv))


def shell(argv):
    return subprocess.run(" ".join(argv), shell=True).returncode


def executable(argv):
    return subprocess.run(["another-name", *argv[1:]], executable=argv[0]).returncode


def system(argv):
    return os.system(" ".join(argv))


def os_popen(argv):
    return os.popen(" ".join(argv)).read()


def spawnv(argv):
    return os.spawnv(os.P_WAIT, argv[0], argv)


def spawnve(argv):
    return os.spawnve(os.P_WAIT, argv[0], argv, dict(os.environ))


def spawnvp(argv):
    return os.spawnvp(os.P_WAIT, argv[0], argv)


def spawnvpe(argv):
    return os.spawnvpe(os.P_WAIT, argv[0], argv, dict(os.environ))


def spawnl(argv):
    return os.spawnl(os.P_WAIT, argv[0], *argv)


def spawnle(argv):
    return os.spawnle(os.P_WAIT, argv[0], *argv, dict(os.environ))


def spawnlp(argv):
    return os.spawnlp(os.P_WAIT, argv[0], *argv)


def spawnlpe(argv):
    return os.spawnlpe(os.P_WAIT, argv[0], *argv, dict(os.environ))


def posix_spawn(argv):
    return os.waitpid(os.posix_spawn(argv[0], argv, dict(os.environ)), 0)[1]


def posix_spawnp(argv):
    return os.waitpid(os.posix_spawnp(argv[0], argv, dict(os.environ)), 0)[1]


def execv(argv):
    os.execv(argv[0], argv)


def execve(argv):
    os.execve(argv[0], argv, dict(os.environ))


def execvp(argv):
    os.execvp(argv[0], argv)


def execvpe(argv):
    os.execvpe(argv[0], argv, dict(os.environ))


def execl(argv):
    os.execl(argv[0], *argv)


def execle(argv):
    os.execle(argv[0], *argv, dict(os.environ))


def execlp(argv):
    os.execlp(argv[0], *argv)


def execlpe(argv):
    os.execlpe(argv[0], *argv, dict(os.environ))
"""
# The ways that come back when the program has run, so that a guard that did
# not refuse would show as a file the program made.
RETURNING = [
    "popen",
    "popen_by_name",
    "run_",
    "run_by_name",
    "call_",
    "call_by_name",
    "check_call_",
    "check_call_by_name",
    "check_output_",
    "check_output_by_name",
    "getoutput_",
    "getoutput_by_name",
    "getstatusoutput_",
    "getstatusoutput_by_name",
    "shell",
    "executable",
    "system",
    "os_popen",
    "spawnv",
    "spawnve",
    "spawnvp",
    "spawnvpe",
    "spawnl",
    "spawnle",
    "spawnlp",
    "spawnlpe",
    "posix_spawn",
    "posix_spawnp",
]
# Those of them that hand a command line to the system's shell.
THROUGH_A_SHELL = [
    "getoutput_",
    "getoutput_by_name",
    "getstatusoutput_",
    "getstatusoutput_by_name",
    "shell",
    "system",
    "os_popen",
]
# The ways that replace the process that asks.  They are tried in an
# interpreter of their own.
REPLACING = ["execv", "execve", "execvp", "execvpe", "execl", "execle", "execlp", "execlpe"]


def probe_module(name: str) -> types.ModuleType:
    """The probe as a module called *name*; it is not put among the loaded modules."""
    module = types.ModuleType(name)
    exec(compile(PROBE, f"<{name}>", "exec"), module.__dict__)
    return module


def own_program(tmp_path: Any, body: str, name: str = "program") -> str:
    """A harmless program of the test's own, under its temporary directory."""
    path = tmp_path / name
    path.write_text("#!/bin/sh" + NL + body + NL)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


class TestEveryWayToStartAProgramAsksFirst:
    """The checks stand in ``subprocess.Popen`` and in the ``os`` module's own
    functions, so they are asked whoever starts a program, by whichever call,
    and however the caller came by the function."""

    @pytest.fixture
    def asked(self, _every_process_start_is_judged) -> list[Start]:
        """What code of ``example.package`` asked to start in this test.  Each
        was refused."""
        seen: list[Start] = []

        def refuse(start: Start) -> None:
            if start.caller.startswith("example.package"):
                seen.append(start)
                raise DestructiveCommandError("this test says so")

        _every_process_start_is_judged.append(refuse)
        return seen

    @pytest.mark.parametrize("way", RETURNING)
    def test_each_call_is_refused_before_anything_starts(self, tmp_path, asked, way):
        module = probe_module("example.package.module")
        marker = tmp_path / "marker"
        with pytest.raises(DestructiveCommandError, match="this test says so"):
            getattr(module, way)([TOUCH, str(marker)])
        assert not marker.exists()
        # The check was told who asked, the words, and the program the system
        # would have started for them.
        (start,) = asked
        assert start.caller == "example.package.module"
        if way in THROUGH_A_SHELL:
            assert start.argv == ("/bin/sh", "-c", f"{TOUCH} {marker}")
            assert start.program == "/bin/sh"
        elif way == "executable":
            assert start.argv == ("another-name", str(marker)) and start.program == TOUCH
        else:
            assert start.argv == (TOUCH, str(marker)) and start.program == TOUCH

    @pytest.mark.parametrize("way", RETURNING)
    def test_a_start_no_check_refuses_happens(self, asked, way):
        module = probe_module("another.package.module")
        assert getattr(module, way)([TRUE]) is not None
        assert asked == []

    def test_the_calls_that_replace_the_process_ask_too(self, tmp_path):
        """Tried in an interpreter of their own: one that was let through would
        replace it with ``true``, and its line below would be missing."""
        script = tmp_path / "replacing.py"
        lines = [
            "import types",
            "from _pytest.monkeypatch import MonkeyPatch",
            "from tests import _host_guard",
            f"probe = {PROBE!r}",
            "module = types.ModuleType('example.package.module')",
            "exec(compile(probe, '<probe>', 'exec'), module.__dict__)",
            "asked = []",
            "def refuse(start):",
            "    asked.append(start)",
            "    raise _host_guard.DestructiveCommandError('this test says so')",
            "_host_guard.judge_every_start(MonkeyPatch(), [refuse])",
            f"for way in {REPLACING!r}:",
            "    try:",
            f"        getattr(module, way)([{TRUE!r}])",
            "    except _host_guard.DestructiveCommandError:",
            "        start = asked[-1]",
            "        print('refused', way, start.caller, start.program, *start.argv, flush=True)",
        ]
        script.write_text(NL.join(lines) + NL)
        done = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True,
            text=True,
            timeout=60,
            env={**os.environ, "PYTHONPATH": str(ROOT)},
        )
        assert done.stdout.splitlines() == [
            f"refused {way} example.package.module {TRUE} {TRUE}" for way in REPLACING
        ], done.stderr

    def test_one_start_is_asked_about_once(self, _every_process_start_is_judged):
        """``subprocess`` may spawn through ``os``, and ``os.execlp`` tries one
        directory after the other; the outermost call is the one that asks."""
        seen: list[Start] = []
        _every_process_start_is_judged.append(seen.append)
        subprocess.run([TRUE], close_fds=False)
        os.spawnlp(os.P_WAIT, "true", "true")
        assert [(s.how, s.argv) for s in seen] == [
            ("subprocess.Popen", (TRUE,)),
            ("os.spawnlp", ("true",)),
        ]

    def test_arguments_a_call_does_not_take_are_left_to_it(self):
        with pytest.raises(TypeError):
            subprocess.Popen()  # type: ignore[call-overload]
        with pytest.raises(TypeError):
            subprocess.run([TRUE], no_such_option=True)  # type: ignore[call-overload]


class TestTheProgramTheSystemWouldStart:
    def test_a_bare_name_is_looked_for_along_the_search_path(self, tmp_path, monkeypatch):
        found = started_program("sh")
        assert os.path.isabs(found) and os.path.basename(found) == "sh"
        # Not in the working directory, whatever file of that name is there.
        own_program(tmp_path, "true", name="sh")
        monkeypatch.chdir(tmp_path)
        assert started_program("sh") == found
        assert started_program("./sh") == str(tmp_path / "sh")

    def test_the_search_path_is_the_one_the_program_is_started_with(self, tmp_path):
        own = own_program(tmp_path, "true", name="sh")
        assert started_program("sh", env={"PATH": str(tmp_path)}) == own
        assert started_program("sh", env={"PATH": str(tmp_path / "nothing-here")}) == ""
        assert started_program("no-such-program-anywhere") == ""
        # A file there that cannot be run is passed over, as the system does.
        (tmp_path / "bin").mkdir()
        (tmp_path / "bin" / "sh").write_text("")
        both = os.pathsep.join([str(tmp_path / "bin"), str(tmp_path)])
        assert started_program("sh", env={"PATH": both}) == own

    def test_a_name_with_a_directory_is_read_from_where_the_program_starts(self, tmp_path):
        assert started_program("bin/tool", cwd=str(tmp_path)) == str(tmp_path / "bin" / "tool")
        assert started_program("/usr/bin/env") == "/usr/bin/env"
        # A call that takes a path does not search.
        assert started_program("tool", cwd=str(tmp_path), search=False) == str(tmp_path / "tool")


class TestNoDestructiveCommandIsStartedHere:
    """The list of destructive shapes stands where a process is started, so it
    holds for every caller.  Shown with a harmless command the list is made to
    refuse for one test: nothing destructive is handed to anything."""

    @pytest.fixture
    def listed(self, tmp_path, monkeypatch, _no_destructive_command_on_this_machine):
        """A marker file whose ``touch`` the list refuses for this test, and
        what the guard noted."""
        marker = tmp_path / "marker"

        def says(text: Any) -> str | None:
            return "this test says so" if "marker" in str(text) else None

        monkeypatch.setattr(_host_guard, "destructive_shape", says)
        monkeypatch.setattr(_host_guard, "_segment_shape", says)
        return marker, _no_destructive_command_on_this_machine

    def _refused(self, listed: Any, start: Any) -> None:
        marker, tripped = listed
        with pytest.raises(DestructiveCommandError, match="this test says so"):
            start(f"touch {marker}")
        assert not marker.exists()
        # Noted, to fail the test at its end; this one is the test's own.
        assert len(tripped) == 1
        tripped.clear()

    def test_a_caller_that_holds_the_hosts_start_under_another_name(self, listed):
        """The module attribute is wrapped; a module that imported the function
        by name holds the function itself, and meets the guard where the
        function starts its process."""
        assert SPAWN_BY_NAME is not background_shells.spawn_group_leader
        self._refused(listed, lambda command: SPAWN_BY_NAME(command, stop_on_error=False, env=None))

    def test_the_watch_runners_shell(self, listed):
        marker, tripped = listed
        runner = WatchRunner(
            storage=MagicMock(), node_id="test-node", check_interval=0.1, tool_timeout=5
        )
        output, code = runner._run_command(f"touch {marker}")
        # The runner reports a command it could not run; the guard has noted it.
        assert code == -1 and "this test says so" in output
        assert not marker.exists() and len(tripped) == 1
        tripped.clear()
        assert runner._run_command("echo fine") == ("fine" + NL, 0)

    @pytest.mark.parametrize(
        "start",
        [
            lambda command: subprocess.run(command, shell=True),
            lambda command: subprocess.Popen(command, shell=True),
            lambda command: subprocess.call(["sh", "-c", command]),
            lambda command: subprocess.check_call(["/bin/sh", "-c", command]),
            lambda command: subprocess.check_output(
                ["bash", "-e", "-o", "pipefail", "-c", command]
            ),
            lambda command: subprocess.run(["sh", "-ec", command]),
            lambda command: subprocess.run(["env", "sh", "-c", command]),
            lambda command: subprocess.run(["timeout", "5", "sh", "-c", command]),
            lambda command: subprocess.getoutput(command),
            lambda command: os.system(command),
            lambda command: os.popen(command),
            lambda command: os.spawnlp(os.P_WAIT, "sh", "sh", "-c", command),
            lambda command: os.posix_spawnp("sh", ["sh", "-c", command], dict(os.environ)),
            lambda command: asyncio.run(asyncio.create_subprocess_shell(command)),
            lambda command: asyncio.run(asyncio.create_subprocess_exec("sh", "-c", command)),
        ],
        ids=[
            "run-shell",
            "popen-shell",
            "call-sh-c",
            "check_call-path-sh-c",
            "check_output-bash-options-c",
            "run-sh-ec",
            "run-env-sh-c",
            "run-timeout-sh-c",
            "getoutput",
            "os.system",
            "os.popen",
            "os.spawnlp",
            "os.posix_spawnp",
            "event-loop-shell",
            "event-loop-exec",
        ],
    )
    def test_a_shell_given_a_listed_command_by_any_call(self, listed, start):
        self._refused(listed, start)

    def test_a_shell_given_a_script_file_that_holds_one(self, listed, tmp_path):
        script = tmp_path / "script.sh"

        def write_and_start(command: str) -> Any:
            script.write_text("set -e" + NL + command + NL)
            return subprocess.run(["sh", str(script)])

        self._refused(listed, write_and_start)
        self._refused(listed, lambda _c: subprocess.run(["bash", "script.sh"], cwd=tmp_path))

    def test_a_script_started_as_a_program_that_names_a_shell(self, listed, tmp_path):
        for first_line in ("#!/bin/sh", "#!/usr/bin/env bash", "#! /bin/bash -e"):

            def write_and_start(command: str, first_line: str = first_line) -> Any:
                path = tmp_path / "tool"
                path.write_text(first_line + NL + command + NL)
                path.chmod(0o755)
                return subprocess.run([str(path)])

            self._refused(listed, write_and_start)

    def test_a_listed_program_started_without_a_shell(self, listed):
        marker, tripped = listed
        # ``mv --version`` only prints; the list is made to refuse it here.
        with pytest.raises(DestructiveCommandError, match="this test says so"):
            subprocess.run(["mv", "--version", "marker"], capture_output=True)
        with pytest.raises(DestructiveCommandError, match="this test says so"):
            subprocess.run(["env", "mv", "--version", "marker"], capture_output=True)
        assert len(tripped) == 2
        tripped.clear()
        # A program that is none of them is not asked about its words.
        assert subprocess.run(["echo", "marker"], capture_output=True).returncode == 0
        assert tripped == []

    def test_what_tests_ordinarily_start_passes(self, tmp_path):
        fine = b"fine" + NL.encode()
        assert subprocess.run(["true"]).returncode == 0
        assert subprocess.run("echo fine", shell=True, capture_output=True).stdout == fine
        assert subprocess.run(["sh", "-c", "echo fine"], capture_output=True).stdout == fine
        assert os.system("true") == 0
        script = tmp_path / "script.sh"
        script.write_text("echo fine" + NL)
        assert subprocess.run(["sh", str(script)], capture_output=True).stdout == fine
        assert (
            subprocess.run([own_program(tmp_path, "echo fine")], capture_output=True).stdout == fine
        )
        assert (
            subprocess.run([sys.executable, "-c", "print(1)"], capture_output=True).returncode == 0
        )

    @pytest.mark.parametrize(
        "argv",
        [
            ("kill", "-9", "-1"),
            ("/usr/bin/kill", "-s", "TERM", "1"),
            ("rm", "-rf", "/"),
            ("env", "rm", "-rf", "/usr"),
            ("timeout", "5", "kill", "-9", "-1"),
            ("sudo", "true"),
            ("shutdown", "-h", "now"),
            ("mkfs.ext4", "/dev/sdb1"),
            ("git", "clean", "-fdx"),
            ("sh", "-c", "kill -9 -1"),
            ("bash", "-lc", "rm -rf ~"),
            ("env", "bash", "-c", "shutdown -h now"),
            ("/bin/sh", "-c", "true; kill -9 -1"),
        ],
        ids=" ".join,
    )
    def test_the_list_knows_these_starts(self, argv):
        """As data for the function that reads a start: nothing is started."""
        assert start_shape(Start("subprocess.Popen", argv, "", "tests.somewhere")) is not None

    @pytest.mark.parametrize(
        "argv",
        [
            ("kill", "-9", "4242"),
            ("rm", "-rf", "build"),
            ("git", "status"),
            ("echo", "rm", "-rf", "/"),
            (sys.executable, "-c", "print(1)"),
            ("sh", "-c", "echo fine"),
            ("bash", "-c", "ls out; cat out/a.txt"),
            # Carried into a container by a runtime: not started here.
            ("docker", "exec", "some-container", "kill", "-9", "-1"),
            ("docker", "exec", "some-container", "sh", "-c", "kill -9 -1"),
            ("docker", "rm", "--force", "--volumes", "some-container"),
            ("docker", "kill", "some-container"),
        ],
        ids=" ".join,
    )
    def test_and_lets_these_pass(self, argv):
        assert start_shape(Start("subprocess.Popen", argv, "", "tests.somewhere")) is None


# The test process, its group and its session, for the judgement as data.
ME, GROUP, SESSION = 4000, 3990, 3980
HOWS = ["os.kill", "os.killpg"]


def _judge(
    how: str,
    target: Any,
    signum: Any,
    whose: dict[int, str] | None = None,
    members: Any = None,
) -> str | None:
    said = whose or {}
    return signal_refusal(
        how,
        target,
        signum,
        me=ME,
        group=GROUP,
        session=SESSION,
        whose=lambda pid: said.get(pid, ABSENT),
        members=members if members is not None else (lambda group: []),
    )


def _own_sleep(**kwargs: Any) -> subprocess.Popen[bytes]:
    """A ``sleep`` of the test's own: the one kind of process a test here
    sends a real signal to."""
    return subprocess.Popen(
        [SLEEP, "30"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **kwargs,
    )


def _end(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is None:
        proc.kill()
    proc.wait(timeout=10)


def _command_line(pid: int) -> list[str]:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as line:
            return [part.decode("utf-8", "replace") for part in line.read().split(bytes(1)) if part]
    except OSError:
        return []


class TestWhichSignalsTheGuardLetsThrough:
    """The judgement itself, on ids, signals and what the system says as
    data: nothing is signalled."""

    @pytest.mark.parametrize("how", HOWS)
    @pytest.mark.parametrize(
        "target",
        [MagicMock(), MagicMock().pid, True, False, 4242.0, "4242", None],
        ids=["stand-in", "stand-in-id", "true", "false", "float", "text", "none"],
    )
    @pytest.mark.parametrize("signum", [0, signal.SIGKILL])
    def test_a_target_that_is_not_an_int(self, how, target, signum):
        assert "only an int is taken" in str(_judge(how, target, signum, {4242: OURS}))

    def test_a_stand_ins_id_converts_to_the_first_process(self):
        """Why: with a stand-in in place of a process object,
        ``os.killpg(proc.pid, ...)`` would be ``kill(-1, ...)``."""
        assert operator.index(MagicMock().pid) == 1

    @pytest.mark.parametrize("how", HOWS)
    @pytest.mark.parametrize("target", [1, 0, -1, -2, -ME])
    @pytest.mark.parametrize("signum", [0, signal.SIGTERM, signal.SIGKILL])
    def test_an_id_of_one_or_below_whatever_the_signal(self, how, target, signum):
        why = _judge(how, target, signum, {1: OURS, ME: OURS}, lambda group: [ME])
        assert str(why).startswith(f"{how}({target}) signals ")

    @pytest.mark.parametrize("target", [GROUP, SESSION])
    @pytest.mark.parametrize("signum", [0, signal.SIGKILL])
    def test_the_test_processs_own_group_or_session(self, target, signum):
        why = _judge("os.killpg", target, signum, {ME: OURS}, lambda group: [ME])
        assert "own group or session" in str(why)

    def test_the_test_process_and_a_process_it_started(self):
        assert _judge("os.kill", ME, signal.SIGINT) is None
        assert _judge("os.kill", 4242, signal.SIGKILL, {4242: OURS}) is None

    def test_a_process_the_test_did_not_start(self):
        why = _judge("os.kill", 4242, signal.SIGKILL, {4242: OTHER})
        assert "4242, which the test did not start" in str(why)
        why = _judge("os.kill", 4242, signal.SIGTERM, {4242: UNKNOWN})
        assert "does not say whose process 4242 is" in str(why)

    def test_signal_0_may_ask_about_any_other_id(self):
        assert _judge("os.kill", 4242, 0, {4242: OTHER}) is None
        assert _judge("os.kill", 4242, 0, {4242: UNKNOWN}) is None
        assert _judge("os.killpg", 4242, 0, members=lambda group: None) is None

    def test_only_an_int_itself_is_taken_as_signal_0(self):
        """A subclass of ``int`` that says 0 when asked is 9 to the system: it is
        judged as a real signal.  As data only; it is never sent."""

        class SaysZero(int):
            def __int__(self) -> int:
                return 0

        disguised = SaysZero(9)
        assert int(disguised) == 0 and disguised == 9
        why = _judge("os.kill", 4242, disguised, {4242: OTHER})
        assert "4242, which the test did not start" in str(why)
        why = _judge("os.killpg", 4242, disguised, {4242: OTHER}, lambda group: [4242])
        assert "4242, which the test did not start" in str(why)
        assert "did not start" in str(_judge("os.kill", 4242, False, {4242: OTHER}))
        assert _judge("os.kill", 4242, 0, {4242: OTHER}) is None

    def test_an_id_that_no_process_has(self):
        assert _judge("os.kill", 4242, signal.SIGKILL) is None
        assert _judge("os.killpg", 4242, signal.SIGKILL) is None

    def test_a_group_is_judged_by_every_process_in_it(self):
        mine = {4242: OURS, 4243: OURS}
        both = [4242, 4243]
        assert _judge("os.killpg", 4242, signal.SIGKILL, mine, lambda group: both) is None
        mixed = {4242: OURS, 4243: OTHER}
        why = _judge("os.killpg", 4242, signal.SIGKILL, mixed, lambda group: both)
        assert "process 4243, which the test did not start" in str(why)
        why = _judge("os.killpg", 4242, signal.SIGKILL, mine, lambda group: None)
        assert "does not say which processes the group holds" in str(why)


class TestNoSignalButToTheTestsOwnProcesses:
    """The guard where ``os.kill`` and ``os.killpg`` are called."""

    @pytest.fixture
    def tripped(self, _no_signal_but_to_the_tests_own_processes) -> list[str]:
        """What the guard noted in this test.  A test that made it refuse on
        purpose empties the list, or the guard fails the test."""
        noted: list[str] = _no_signal_but_to_the_tests_own_processes
        return noted

    def test_it_stands_in_both_calls(self):
        assert os.kill is not posix.kill and os.kill.__wrapped__ is posix.kill  # type: ignore[attr-defined]
        assert os.killpg is not posix.killpg
        assert os.killpg.__wrapped__ is posix.killpg  # type: ignore[attr-defined]

    @pytest.mark.parametrize(
        "send",
        [
            lambda: os.kill(-1, 0),
            lambda: os.kill(0, 0),
            lambda: os.kill(1, 0),
            lambda: os.kill(-2, 0),
            lambda: os.killpg(0, 0),
            lambda: os.killpg(1, 0),
            lambda: os.killpg(os.getpgrp(), 0),
            lambda: os.killpg(os.getsid(0), 0),
            lambda: os.kill(MagicMock().pid, 0),
            lambda: os.killpg(MagicMock().pid, 0),
            lambda: os.kill(True, 0),
        ],
        ids=[
            "kill-every-process",
            "kill-own-group",
            "kill-first-process",
            "kill-a-group",
            "killpg-own-group",
            "killpg-every-process",
            "killpg-own-group-by-id",
            "killpg-own-session-by-id",
            "kill-stand-in-id",
            "killpg-stand-in-id",
            "kill-bool",
        ],
    )
    def test_is_refused_before_anything_is_sent(self, tripped, send):
        """With signal 0: were the guard not there, nothing would be delivered."""
        with pytest.raises(DestructiveCommandError, match="must never be sent here"):
            send()
        # Noted, to fail the test at its end; this one is the test's own.
        assert len(tripped) == 1
        tripped.clear()

    def test_a_refusal_the_code_swallows_still_fails_the_test(self, tripped):
        # As code under test may.
        with contextlib.suppress(Exception):
            os.killpg(0, 0)
        assert len(tripped) == 1
        tripped.clear()

    def test_a_signal_to_a_process_the_test_started(self, tripped, _processes_this_test_started):
        child = _own_sleep()
        leader = _own_sleep(start_new_session=True)
        try:
            assert {child.pid, leader.pid} <= set(_processes_this_test_started)
            os.kill(child.pid, signal.SIGTERM)
            assert child.wait(timeout=10) == -signal.SIGTERM
            os.killpg(leader.pid, signal.SIGKILL)
            assert leader.wait(timeout=10) == -signal.SIGKILL
            # Asking with 0 about one that is gone is the system's to answer.
            with pytest.raises(ProcessLookupError):
                os.kill(child.pid, 0)
        finally:
            _end(child)
            _end(leader)
        assert tripped == []

    @pytest.mark.parametrize("way", ["os.kill", "os.killpg", "kill", "terminate", "send_signal"])
    def test_one_the_system_does_not_call_the_tests_is_not_signalled(
        self, tripped, monkeypatch, way
    ):
        """The test's own ``sleep``, with the system made to say otherwise: were
        the guard not there, only that ``sleep`` would end.  A process object's
        ``kill``, ``terminate`` and ``send_signal`` meet the guard in
        ``os.kill``."""
        child = _own_sleep(start_new_session=True)
        sends = {
            "os.kill": lambda: os.kill(child.pid, signal.SIGKILL),
            "os.killpg": lambda: os.killpg(child.pid, signal.SIGKILL),
            "kill": child.kill,
            "terminate": child.terminate,
            "send_signal": lambda: child.send_signal(signal.SIGTERM),
        }
        try:
            with monkeypatch.context() as patch:
                patch.setattr(ProcessView, "whose", lambda _view, _pid: OTHER)
                with pytest.raises(DestructiveCommandError, match="which the test did not start"):
                    sends[way]()
            assert child.poll() is None
            assert len(tripped) == 1
            tripped.clear()
        finally:
            _end(child)

    def test_nor_through_an_event_loop(self, tripped, monkeypatch):
        async def start_and_kill() -> None:
            proc = await asyncio.create_subprocess_exec(SLEEP, "30")
            try:
                with monkeypatch.context() as patch:
                    patch.setattr(ProcessView, "whose", lambda _view, _pid: OTHER)
                    with pytest.raises(DestructiveCommandError):
                        proc.kill()
                assert proc.returncode is None
            finally:
                proc.kill()
                await proc.wait()

        asyncio.run(start_and_kill())
        assert len(tripped) == 1
        tripped.clear()

    def test_every_way_to_start_a_process_notes_it(self, _processes_this_test_started):
        started = _processes_this_test_started
        done = subprocess.run([TRUE])
        assert done.returncode == 0 and len(started) == 1
        pid = os.posix_spawn(TRUE, [TRUE], dict(os.environ))
        os.waitpid(pid, 0)
        assert pid in started
        pid = os.spawnv(os.P_NOWAIT, TRUE, [TRUE])
        os.waitpid(pid, 0)
        assert pid in started and len(started) == 3
        assert os.spawnv(os.P_WAIT, TRUE, [TRUE]) == 0 and len(started) == 3


class TestWhatTheSystemSaysOfAProcess:
    """The view the guard asks, on real processes: it only reads."""

    def test_the_test_process_and_its_child(self, _processes_this_test_started):
        view = ProcessView(_processes_this_test_started)
        assert view.whose(os.getpid()) == OURS
        child = _own_sleep()
        try:
            assert view.whose(child.pid) == OURS
            # Parent by parent, without the note of the start as well.
            assert ProcessView({}).whose(child.pid) == OURS
        finally:
            _end(child)

    def test_a_process_the_test_did_not_start(self, _processes_this_test_started):
        view = ProcessView(_processes_this_test_started)
        assert view.whose(os.getppid()) == OTHER

    def test_a_tests_stand_in_for_what_it_asks_changes_nothing(self, monkeypatch):
        """What the guard asks the system with is taken when its module is
        imported, not where a test may stand in for it."""
        assert (_host_guard._getpid, _host_guard._getpgrp, _host_guard._getsid) == (
            posix.getpid,
            posix.getpgrp,
            posix.getsid,
        )
        assert _host_guard._listdir is posix.listdir and _host_guard._open is builtins.open
        child = _own_sleep(start_new_session=True)
        try:
            with monkeypatch.context() as patch:
                patch.setattr(os, "getpid", lambda: 2**22 + 7)
                patch.setattr(os, "listdir", lambda *_args: [])
                view = ProcessView({})
                assert view.whose(child.pid) == OURS
                assert view.members(child.pid) == [child.pid]
        finally:
            _end(child)

    def test_an_id_no_process_has(self, _processes_this_test_started):
        with open("/proc/sys/kernel/pid_max") as limit:
            unused = int(limit.read())  # ids stay below it
        assert ProcessView(_processes_this_test_started).whose(unused) == ABSENT

    def test_one_whose_parent_ended_is_known_by_the_group_the_test_made(
        self, _processes_this_test_started
    ):
        """Its parent is gone and it was handed to another; its group is still
        the one the process the test started made."""
        code = (
            "import subprocess; "
            "print(subprocess.Popen(['sleep', '30'], stdout=subprocess.DEVNULL).pid)"
        )
        leader = subprocess.Popen(
            [sys.executable, "-c", code],
            stdout=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        out, _ = leader.communicate(timeout=30)
        orphan = int(out)
        try:
            assert leader.pid in _processes_this_test_started
            view = ProcessView(_processes_this_test_started)
            assert view.whose(orphan) == OURS
            assert view.members(leader.pid) == [orphan]
            # Nothing else ties it to the test.
            assert ProcessView({}).whose(orphan) == OTHER
        finally:
            # This test's own sleep, by the id its own child printed.
            if _command_line(orphan) == ["sleep", "30"]:
                os.kill(orphan, signal.SIGKILL)

    def test_one_that_also_left_its_group_is_known_by_the_mark(self):
        """The test's environment carries a mark of this test's, which what it
        starts inherits."""
        code = (
            "import subprocess; print(subprocess.Popen(['sleep', '30'], "
            "stdout=subprocess.DEVNULL, start_new_session=True).pid)"
        )
        leader = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
        out, _ = leader.communicate(timeout=30)
        orphan = int(out)
        try:
            mark = f"{PROCESS_MARK}={os.environ[PROCESS_MARK]}"
            assert ProcessView({}, mark=mark).whose(orphan) == OURS
            assert ProcessView({leader.pid: None}).whose(orphan) == OTHER
            assert ProcessView({}, mark=PROCESS_MARK + "=another").whose(orphan) == OTHER
        finally:
            # This test's own sleep, by the id its own child printed.
            if _command_line(orphan) == ["sleep", "30"]:
                os.kill(orphan, signal.SIGKILL)


class TestTheViewOnATableOfProcesses:
    """The same view on a process table the test writes: whose a process is
    by its parents, by the group or session a noted start made, and where
    the table does not say."""

    @staticmethod
    def _table(tmp_path: Any, rows: dict[int, tuple[int, int, int, int]]) -> str:
        """A table in the system's own format, with the test process in it;
        each row is a process's parent, group, session and start time."""
        proc = tmp_path / "proc"
        (proc / "self").mkdir(parents=True)
        for pid, (parent, group, session, start) in {os.getpid(): (1, 1, 1, 7), **rows}.items():
            (proc / str(pid)).mkdir()
            rest = " ".join(["0"] * 15)
            (proc / str(pid) / "stat").write_text(
                f"{pid} (a (name)) S {parent} {group} {session} {rest} {start}" + NL
            )
        return str(proc)

    def test_by_its_parents(self, tmp_path):
        me = os.getpid()
        proc = self._table(tmp_path, {501: (me, 501, 501, 10), 502: (501, 501, 501, 11)})
        view = ProcessView({}, proc)
        assert view.whose(502) == OURS and view.whose(501) == OURS and view.whose(me) == OURS
        assert view.whose(503) == ABSENT

    def test_by_the_group_or_session_a_noted_start_made(self, tmp_path):
        # 502 was handed to the first process when 501, which the test started, ended.
        proc = self._table(tmp_path, {502: (1, 501, 501, 11), 600: (1, 600, 600, 5)})
        assert ProcessView({501: 10}, proc).whose(502) == OURS
        assert ProcessView({}, proc).whose(502) == OTHER
        assert ProcessView({501: 10}, proc).whose(600) == OTHER
        assert ProcessView({501: 10}, proc).members(501) == [502]

    def test_a_note_whose_id_another_process_holds_now_is_not_counted(self, tmp_path):
        """501 is a process that started at another time than the one noted."""
        rows = {501: (1, 501, 501, 99), 502: (1, 501, 501, 100)}
        proc = self._table(tmp_path, rows)
        assert ProcessView({501: 10}, proc).whose(502) == OTHER
        assert ProcessView({501: 99}, proc).whose(502) == OURS
        # Where the start of the noted one is not known, it is not counted.
        assert ProcessView({501: None}, proc).whose(502) == OTHER

    def test_by_the_mark_its_environment_carries(self, tmp_path):
        """502 left the group and session it was started in, and its parent ended."""
        proc = self._table(tmp_path, {502: (1, 502, 502, 11)})
        entries = [b"PATH=/bin", b"MARK=this-test", b""]
        (tmp_path / "proc" / "502" / "environ").write_bytes(bytes(1).join(entries))
        assert ProcessView({}, proc, mark="MARK=this-test").whose(502) == OURS
        assert ProcessView({}, proc, mark="MARK=another-test").whose(502) == OTHER
        assert ProcessView({}, proc, mark="MARK=this").whose(502) == OTHER
        assert ProcessView({}, proc).whose(502) == OTHER
        (tmp_path / "proc" / "502" / "environ").unlink()
        assert ProcessView({}, proc, mark="MARK=this-test").whose(502) == OTHER

    def test_where_the_table_does_not_say(self, tmp_path):
        me = os.getpid()
        proc = self._table(tmp_path, {502: (501, 502, 502, 11), 503: (me, 503, 503, 12)})
        # Its parent is not in the table.
        assert ProcessView({}, proc).whose(502) == UNKNOWN
        (tmp_path / "proc" / "504").mkdir()
        (tmp_path / "proc" / "504" / "stat").write_text("not a line of the table")
        assert ProcessView({}, proc).whose(504) == UNKNOWN
        assert ProcessView({}, proc).members(503) is None
        # Without its own entry the table says nothing of a missing id either.
        (tmp_path / "proc" / "self").rmdir()
        assert ProcessView({}, proc).whose(505) == UNKNOWN


class TestATestsOwnPatchesLeaveTheGuardsInPlace:
    """The guards patch with contexts of their own: a test's ``monkeypatch``,
    undone in the test or after it, puts back what it patched and nothing of
    theirs.  The three tests run in this order."""

    def test_an_undo_in_the_test_reaches_no_guard(
        self, monkeypatch, _every_process_start_is_judged, _no_signal_but_to_the_tests_own_processes
    ):
        tripped = _no_signal_but_to_the_tests_own_processes
        monkeypatch.setattr(os, "killpg", lambda _group, _signum: None)
        monkeypatch.setattr(background_shells, "spawn_group_leader", lambda *_a, **_k: None)
        monkeypatch.undo()
        # The guard on signals: refused, with signal 0.
        with pytest.raises(DestructiveCommandError):
            os.killpg(0, 0)
        assert len(tripped) == 1
        tripped.clear()
        # The guard on starts still asks every check.
        seen: list[Start] = []
        _every_process_start_is_judged.append(seen.append)
        subprocess.run([TRUE])
        assert [start.argv for start in seen] == [(TRUE,)]
        # The host's own start is still wrapped, and the mark still set.
        assert background_shells.spawn_group_leader.__wrapped__ is SPAWN_BY_NAME  # type: ignore[attr-defined]
        assert os.environ.get(PROCESS_MARK)

    def test_a_test_patches_over_the_guards(self, monkeypatch):
        """Undone after the test before the guards' own patches are: what it
        puts back is theirs, and theirs is then put back to the real one."""
        killpg, init = os.killpg, subprocess.Popen.__init__
        monkeypatch.setattr(os, "killpg", functools.wraps(killpg)(lambda *a: killpg(*a)))
        monkeypatch.setattr(
            subprocess.Popen, "__init__", functools.wraps(init)(lambda *a, **k: init(*a, **k))
        )
        monkeypatch.setattr(background_shells, "spawn_group_leader", lambda *_a, **_k: None)
        monkeypatch.setenv(PROCESS_MARK, "this-test")

    def test_and_in_the_next_test_each_guard_stands_once(self):
        from turnstone.core import exec_env

        assert os.kill.__wrapped__ is posix.kill  # type: ignore[attr-defined]
        assert os.killpg.__wrapped__ is posix.killpg  # type: ignore[attr-defined]
        assert not hasattr(subprocess.Popen.__init__.__wrapped__, "__wrapped__")  # type: ignore[attr-defined]
        assert background_shells.spawn_group_leader.__wrapped__ is SPAWN_BY_NAME  # type: ignore[attr-defined]
        assert not hasattr(exec_env.spawn_group_leader.__wrapped__, "__wrapped__")  # type: ignore[attr-defined]
        assert os.environ.get(PROCESS_MARK) not in (None, "this-test")
