"""Every signal the rollout package and the core's shell tools send to a process group.

One function sends them all (``turnstone.core.background_shells.signal_group``),
and it sends none unless the id is certain to be the group of a child this
process started and has not collected.  A signal to the group id 1 reaches every
process the user may signal, the id 0 is the caller's own group, and an id that
was freed can be another group's a moment later.

What a test here does, and what it never does: ids and objects that stand in
for a process are judged as data, by the functions that only judge and send
nothing.  Where a refusal is asked of the function that signals, the signal is
0, which delivers nothing whatever the id.  A real signal goes only to a
``sleep`` the test itself started in a session of its own, by the id taken from
it while it has not been collected.
"""

from __future__ import annotations

import ast
import logging
import operator
import os
import pathlib
import signal
import stat
import subprocess
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

import turnstone
from turnstone.core import background_shells
from turnstone.core.background_shells import (
    group_id_refusal,
    group_signal_refusal,
    signal_group,
)
from turnstone.rollout import container as container_module
from turnstone.rollout.container import run_captured

NL = chr(10)

needs_uncollected_wait = pytest.mark.skipif(
    not background_shells._CAN_ASK_UNCOLLECTED,
    reason="this system cannot ask about a child without collecting it",
)


@pytest.fixture(name="sleeper")
def _sleeper() -> Any:
    """A ``sleep`` of the test's own, leader of a session and group of its own."""
    proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
    yield proc
    if proc.poll() is None:
        proc.kill()
    proc.wait(timeout=10)


# A shell that starts a sleep, prints its id and waits for it.
LEAVES_A_SLEEP = ["sh", "-c", "sleep 30 & echo $!; wait"]


def _stand_in(pid: Any, returncode: Any = None) -> SimpleNamespace:
    """An object that only stands in for a process: it claims an id, and with
    ``returncode=None`` that it still runs."""
    return SimpleNamespace(pid=pid, returncode=returncode, poll=lambda: returncode)


class _Converts:
    """Converts to 1 wherever a number is asked for, as a stand-in object does."""

    def __index__(self) -> int:
        return 1

    def __int__(self) -> int:
        return 1


# No process group of a command's, each for a reason of its own.
NOT_AN_ID = [
    pytest.param(True, id="True"),
    pytest.param(False, id="False"),
    pytest.param(0, id="0"),
    pytest.param(1, id="1"),
    pytest.param(-1, id="-1"),
    pytest.param(-4242, id="negative"),
    pytest.param(4242.0, id="float"),
    pytest.param("4242", id="text"),
    pytest.param(None, id="None"),
    pytest.param(signal.SIGKILL, id="a-number-of-another-type"),
    pytest.param(_Converts(), id="converts-to-1"),
    pytest.param(MagicMock(), id="stand-in-object"),
    pytest.param(MagicMock().pid, id="pid-of-a-stand-in-object"),
]


class TestAnIdThatIsNoGroupOfACommands:
    def test_an_object_that_stands_in_for_a_process_converts_to_one(self):
        """Why only an ``int`` counts: handed to the system as a group id, the
        ``pid`` of such an object is 1, every process the user may signal."""
        assert operator.index(MagicMock().pid) == 1
        assert operator.index(True) == 1

    @pytest.mark.parametrize("pgid", NOT_AN_ID)
    def test_it_is_refused_whatever_claims_it(self, pgid):
        assert group_id_refusal(pgid) is not None
        for leader in (_stand_in(pgid), MagicMock(), None):
            assert group_signal_refusal(leader, pgid) is not None
            assert group_signal_refusal(leader, pgid, after_exit=True) is not None

    @pytest.mark.parametrize("pgid", NOT_AN_ID)
    def test_asked_to_signal_it_the_function_sends_nothing(self, pgid):
        """With signal 0, which delivers nothing: were the refusal gone, the
        call would report that it had signalled."""
        assert signal_group(_stand_in(pgid), pgid, 0) is False
        assert signal_group(_stand_in(pgid), pgid, 0, after_exit=True) is False

    def test_the_callers_own_group_is_refused(self, sleeper, monkeypatch):
        # Where the leader of that group is outside this process's namespace
        # of ids, the system gives its id as 0.
        own = os.getpgrp()
        assert group_id_refusal(own) is not None
        assert group_signal_refusal(_stand_in(own), own) is not None
        assert group_signal_refusal(_stand_in(own), own, after_exit=True) is not None
        assert signal_group(_stand_in(own), own, 0) is False
        # Whatever the id is: here the group of this test's sleep is passed
        # off as the caller's own.
        assert group_signal_refusal(sleeper, sleeper.pid) is None
        monkeypatch.setattr(os, "getpgrp", lambda: sleeper.pid)
        assert "own process group" in (group_signal_refusal(sleeper, sleeper.pid) or "")
        assert signal_group(sleeper, sleeper.pid, 0) is False

    def test_a_refusal_for_the_id_is_logged(self, caplog):
        """An id at fault is a defect somewhere, so the log says it.  A leader
        that was simply collected is the ordinary end of a command, and silent."""
        with caplog.at_level(logging.WARNING, logger=background_shells.__name__):
            assert signal_group(_stand_in(1), 1, 0) is False
            refused = [r for r in caplog.records if "signal_refused" in r.getMessage()]
            assert len(refused) == 1
            caplog.clear()
            proc = subprocess.Popen(["true"], start_new_session=True)
            proc.wait(timeout=10)
            assert signal_group(proc, proc.pid, 0) is False
            assert [r for r in caplog.records if "signal_refused" in r.getMessage()] == []


class TestAGroupThatIsNotCertainToBeTheCommands:
    def test_the_id_must_be_the_leaders(self, sleeper):
        other = subprocess.Popen(["sleep", "30"], start_new_session=True)
        try:
            assert group_signal_refusal(sleeper, sleeper.pid) is None
            said = group_signal_refusal(sleeper, other.pid)
            assert said is not None and "not the id of the process" in said
            assert group_signal_refusal(MagicMock(), other.pid) is not None
            assert group_signal_refusal(None, other.pid) is not None
        finally:
            other.kill()
            other.wait(timeout=10)

    def test_a_collected_process(self):
        proc = subprocess.Popen(["true"], start_new_session=True)
        proc.wait(timeout=10)
        said = group_signal_refusal(proc, proc.pid)
        assert said is not None and "collected" in said
        assert signal_group(proc, proc.pid, 0) is False

    @needs_uncollected_wait
    def test_a_process_somebody_else_collected(self):
        """The object still says that it runs; the system says that there is no
        such child."""
        proc = subprocess.Popen(["true"], start_new_session=True)
        os.waitpid(proc.pid, 0)
        try:
            assert proc.returncode is None
            assert group_signal_refusal(proc, proc.pid) is not None
            assert signal_group(proc, proc.pid, 0) is False
        finally:
            proc.wait(timeout=10)

    @needs_uncollected_wait
    def test_an_object_that_claims_the_id_of_a_process_that_is_no_child_of_this_one(self):
        """Judged only: the id is a process's, here a ``sleep`` that a shell of
        the test's own started, and nothing is sent to it."""
        shell = subprocess.Popen(LEAVES_A_SLEEP, start_new_session=True, stdout=subprocess.PIPE)
        try:
            assert shell.stdout is not None
            grandchild = int(shell.stdout.readline())
            claims = _stand_in(grandchild)
            assert group_signal_refusal(claims, grandchild) is not None
            assert group_signal_refusal(claims, grandchild, after_exit=True) is not None
        finally:
            # The shell and its sleep: the group of a child that is not collected.
            signal_group(shell, shell.pid, signal.SIGKILL)
            shell.wait(timeout=10)
            shell.stdout.close()

    @needs_uncollected_wait
    def test_one_that_has_ended_and_is_not_collected_may_still_be_signalled(self):
        """Its id is its own until it is collected, and what it left running in
        its group is ended through that id."""
        proc = subprocess.Popen(["true"], start_new_session=True)
        try:
            os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOWAIT)
            assert proc.returncode is None
            assert group_signal_refusal(proc, proc.pid) is None
            assert signal_group(proc, proc.pid, 0) is True
        finally:
            proc.wait(timeout=10)

    def test_after_its_leader_was_collected_an_id_another_process_has_is_refused(self, sleeper):
        """The host's shell tool ends what a command left in its group after
        the command was collected.  A process that has the id by then shows that
        the group it named is gone.  Here the "collected" leader is an object
        that claims the id of this test's own ``sleep``."""
        collected = _stand_in(sleeper.pid, returncode=0)
        said = group_signal_refusal(collected, sleeper.pid, after_exit=True)
        assert said is not None and "another process's" in said
        assert signal_group(collected, sleeper.pid, signal.SIGKILL, after_exit=True) is False
        assert signal_group(collected, sleeper.pid, signal.SIGKILL) is False
        assert sleeper.poll() is None

    def test_after_its_leader_was_collected_an_id_nobody_has_is_not_refused(self):
        """Judged only.  The group, if anything is left in it, is the command's."""
        proc = subprocess.Popen(["true"], start_new_session=True)
        proc.wait(timeout=10)
        assert group_signal_refusal(proc, proc.pid, after_exit=True) is None
        assert group_signal_refusal(proc, proc.pid) is not None


class TestTheSignal:
    def test_a_sleep_of_the_tests_own_is_ended(self, sleeper):
        assert group_signal_refusal(sleeper, sleeper.pid) is None
        assert signal_group(sleeper, sleeper.pid, signal.SIGKILL) is True
        assert sleeper.wait(timeout=10) == -signal.SIGKILL
        # Collected now: the same call sends nothing more.
        assert signal_group(sleeper, sleeper.pid, 0) is False

    def test_what_the_kill_raises_is_raised(self, sleeper, monkeypatch):
        """A group that emptied between the look and the signal: the caller
        hears it from the system, as before."""

        def gone(pgid: int, sig: int) -> None:
            raise ProcessLookupError(3, "No such process")

        monkeypatch.setattr(os, "killpg", gone)
        with pytest.raises(ProcessLookupError):
            signal_group(sleeper, sleeper.pid, signal.SIGKILL)


def _program(tmp_path: Any, body: str) -> str:
    """A harmless program of the test's own, standing in for the runtime's command."""
    path = tmp_path / "program"
    path.write_text("#!/bin/sh" + NL + body + NL)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


class TestACommandThatIsNotThisProcesssToWaitFor:
    @needs_uncollected_wait
    def test_its_id_is_not_signalled(self, tmp_path, monkeypatch):
        """The wait says that the command is no child of this process: somebody
        else collected it, or the object only stands in for one.  Its id then
        names nothing that is certain to be the command's.  The system's
        signal is replaced by a note for this test, so nothing is sent."""
        signalled: list[int] = []

        def no_such_child(*_args: Any) -> None:
            raise ChildProcessError(10, "No child processes")

        monkeypatch.setattr(os, "killpg", lambda pgid, _sig: signalled.append(pgid))
        monkeypatch.setattr(os, "waitid", no_such_child)
        done = run_captured([_program(tmp_path, "echo out")], timeout=30, limit=100)
        assert signalled == []
        assert not done.timed_out and done.stdout == b"out" + NL.encode()


class TestWhereTheSystemCannotWaitWithoutCollecting:
    """There the command is collected under the lock its group is signalled
    under, and a command that still runs is told by asking the object."""

    @pytest.fixture(autouse=True)
    def _such_a_system(self, monkeypatch):
        monkeypatch.setattr(container_module, "_CAN_WAIT_UNCOLLECTED", False)
        monkeypatch.setattr(background_shells, "_CAN_ASK_UNCOLLECTED", False)

    def test_a_command_past_its_time_is_still_killed(self, tmp_path):
        started = time.monotonic()
        program = _program(tmp_path, "echo begin; exec sleep 30")
        done = run_captured([program], timeout=1, limit=100)
        assert done.timed_out and done.stdout == b"begin" + NL.encode()
        assert done.returncode == -signal.SIGKILL and time.monotonic() - started < 10

    def test_a_command_that_ends_by_itself_is_collected_and_not_signalled(
        self, tmp_path, monkeypatch
    ):
        signalled: list[int] = []
        monkeypatch.setattr(os, "killpg", lambda pgid, _sig: signalled.append(pgid))
        done = run_captured([_program(tmp_path, "echo out")], timeout=30, limit=100)
        assert done.returncode == 0 and done.stdout == b"out" + NL.encode()
        assert signalled == []

    def test_a_process_that_runs_may_be_signalled_and_a_collected_one_not(self, sleeper):
        assert group_signal_refusal(sleeper, sleeper.pid) is None
        assert signal_group(sleeper, sleeper.pid, signal.SIGKILL) is True
        assert sleeper.wait(timeout=10) == -signal.SIGKILL
        assert group_signal_refusal(sleeper, sleeper.pid) is not None
        assert group_signal_refusal(_stand_in(sleeper.pid, returncode=0), sleeper.pid) is not None


PACKAGE = pathlib.Path(turnstone.__file__).parent
# The rollout package, and the two files of the core through which a session's
# shell tools end processes.
SOURCES = [
    *sorted((PACKAGE / "rollout").glob("*.py")),
    PACKAGE / "core" / "exec_env.py",
    PACKAGE / "core" / "background_shells.py",
]
SIGNALS_BY_ID = ("kill", "killpg")


def _signals_by_id(source: str) -> list[tuple[str, str]]:
    """Every place in *source* that reaches for the system's signal to a
    process id or a group id, as ``(enclosing function, name)``: an attribute of
    the ``os`` module under any name it was imported by, the same asked for by
    name, and an import of the function itself."""
    tree = ast.parse(source)
    modules = {"os"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(a.asname or a.name for a in node.names if a.name == "os")
    found: list[tuple[str, str]] = []

    def visit(node: ast.AST, function: str) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            function = node.name
        if isinstance(node, ast.ImportFrom) and node.module == "os":
            found.extend((function, a.name) for a in node.names if a.name in SIGNALS_BY_ID)
        if (
            isinstance(node, ast.Attribute)
            and node.attr in SIGNALS_BY_ID
            and isinstance(node.value, ast.Name)
            and node.value.id in modules
        ):
            found.append((function, node.attr))
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id in modules
        ):
            name = node.args[1]
            asked = name.value if isinstance(name, ast.Constant) else "?"
            if asked in (*SIGNALS_BY_ID, "?"):
                found.append((function, str(asked)))
        for child in ast.iter_child_nodes(node):
            visit(child, function)

    visit(tree, "")
    return found


class TestNoOtherCodeSignalsAnId:
    def test_one_function_holds_the_only_signal(self):
        found = {
            (path.name, function, name)
            for path in SOURCES
            for function, name in _signals_by_id(path.read_text(encoding="utf-8"))
        }
        assert found == {("background_shells.py", "signal_group", "killpg")}, (
            "a signal to a process id or a group id is sent by "
            "turnstone.core.background_shells.signal_group and nowhere else"
        )
        helper = (PACKAGE / "core" / "background_shells.py").read_text(encoding="utf-8")
        assert len(_signals_by_id(helper)) == 1

    def test_the_files_read_are_the_package_and_the_two_core_files(self):
        names = {path.name for path in SOURCES}
        assert {"container.py", "episode.py", "exec_env.py", "background_shells.py"} <= names
        assert all(path.is_file() for path in SOURCES)

    def test_the_reading_sees_every_spelling(self):
        """The source below is read, never run."""
        source = NL.join(
            [
                "import os",
                "import os as system",
                "from os import killpg, getpid",
                "def plain(pid):",
                "    os.kill(pid, 9)",
                "def renamed(pgid):",
                "    system.killpg(pgid, 9)",
                "def handed_on(pgid):",
                "    return probe(os.killpg, pgid)",
                "def by_name(pid, which):",
                "    getattr(os, 'kill')(pid, 9)",
                "    getattr(system, which)(pid, 9)",
                "def not_a_signal_by_id(proc):",
                "    proc.kill()",
                "    proc.send_signal(9)",
                "    os.getpgid(proc.pid)",
            ]
        )
        assert _signals_by_id(source) == [
            ("", "killpg"),
            ("plain", "kill"),
            ("renamed", "killpg"),
            ("handed_on", "killpg"),
            ("by_name", "kill"),
            ("by_name", "?"),
        ]


def test_this_file_starts_nothing_but_sleep_and_true():
    """What the tests above start themselves, read off this file: a change that
    starts anything else is looked at before it is run."""
    tree = ast.parse(pathlib.Path(__file__).read_text(encoding="utf-8"))
    started = [
        ast.unparse(node.args[0])
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "Popen"
    ]
    assert set(started) == {"['sleep', '30']", "['true']", "LEAVES_A_SLEEP"}
