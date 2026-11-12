"""The execution environment seam: shell and file tools act where the session says.

``_MemoryEnvironment`` holds its files in a dict and nowhere else, so a file
tool that bypassed the seam (an ``open`` or ``os.path`` call on this machine)
could neither find what a test stored nor leave its result where the test
looks.  ``_RecordingHost`` is the host environment with a call log, for the
shell tools, whose commands have to run somewhere: they run on this machine, so
a test gives it harmless commands only.  A refusal is tested with
``_UnstartedHost``, which answers like the host and starts nothing.
"""

from __future__ import annotations

import io
import json
import os
import posixpath
import stat
import subprocess
import sys
from typing import Any
from unittest.mock import MagicMock

import pytest

from tests._helpers import wait_until
from tests._host_guard import DestructiveCommandError, destructive_shape, guarded_spawn
from turnstone.core.exec_env import HOST_ENVIRONMENT, ExecutionEnvironment
from turnstone.core.tools import ENVIRONMENT_TOOL_NAMES, INTERACTIVE_TOOL_NAMES
from turnstone.eval.core import HeadlessSession

NL = chr(10)
TAB = chr(9)


class _MemoryEnvironment(ExecutionEnvironment):
    """Files in a dict under ``/work``; no process and no disk behind it."""

    acts_on_host = False

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.search_requests: list[list[str]] = []
        self.search_output = ""

    def getcwd(self) -> str:
        return "/work"

    def expanduser(self, path: str) -> str:
        return "/home/agent" + path[1:] if path.startswith("~") else path

    def abspath(self, path: str) -> str:
        return posixpath.normpath(posixpath.join(self.getcwd(), path))

    def realpath(self, path: str) -> str:
        return self.abspath(path)

    def exists(self, path: str) -> bool:
        return path in self.files

    def _data(self, path: str) -> bytes:
        if path not in self.files:
            raise FileNotFoundError(2, "No such file or directory", path)
        return self.files[path]

    def stat(self, path: str) -> os.stat_result:
        size = len(self._data(path))
        return os.stat_result((stat.S_IFREG | 0o644, 0, 0, 1, 0, 0, size, 0, 0, 0))

    def read_bytes(self, path: str, limit: int) -> bytes:
        return self._data(path)[:limit]

    def read_text(self, path: str) -> str:
        return io.TextIOWrapper(io.BytesIO(self._data(path)), encoding="utf-8").read()

    def read_lines(self, path: str) -> list[str]:
        return io.TextIOWrapper(io.BytesIO(self._data(path)), encoding="utf-8").readlines()

    def write_text(
        self, path: str, content: str, *, append: bool = False, make_parents: bool = False
    ) -> None:
        old = self.files.get(path, b"") if append else b""
        self.files[path] = old + content.encode("utf-8")

    def which(self, program: str) -> str | None:
        return None

    def popen_search(
        self, args: list[str], *, env: dict[str, str] | None, timeout: float
    ) -> subprocess.Popen[bytes]:
        self.search_requests.append(args)
        return subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.stdout.write(sys.argv[1])", self.search_output],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def spawn(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("this environment runs no commands")


class _RecordingHost(ExecutionEnvironment):
    """The host, with every shell call noted.  What it is given runs on this
    machine: harmless commands only."""

    def __init__(self) -> None:
        self.spawned: list[str] = []
        self.killed: list[int] = []

    def spawn(self, command: str, **kwargs: Any) -> Any:
        self.spawned.append(command)
        return super().spawn(command, **kwargs)

    def kill_group(self, proc: Any, pgid: int) -> None:
        self.killed.append(pgid)
        super().kill_group(proc, pgid)


class _UnstartedHost(ExecutionEnvironment):
    """The host's answers, and no process: ``spawn`` notes what it was asked to
    start and starts nothing.  For testing what the host refuses, where a
    refusal that failed must not run the command."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    def spawn(self, command: str, **kwargs: Any) -> Any:
        self.asked.append(command)
        raise RuntimeError("not started: this environment only notes the command")


def _call(call_id: str, name: str, **arguments: Any) -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def _run(session: HeadlessSession, *calls: dict[str, Any]) -> list[str]:
    results, _feedback = session._execute_tools(list(calls))
    return [output if isinstance(output, str) else json.dumps(output) for _id, output in results]


@pytest.fixture
def memory(tmp_db):
    env = _MemoryEnvironment()
    session = HeadlessSession(client=MagicMock(), model="eval-model", exec_env=env)
    yield env, session
    session.close()


@pytest.fixture
def recording(tmp_db):
    env = _RecordingHost()
    session = HeadlessSession(client=MagicMock(), model="eval-model", exec_env=env)
    yield env, session
    session.close()


@pytest.fixture
def unstarted(tmp_db):
    env = _UnstartedHost()
    session = HeadlessSession(client=MagicMock(), model="eval-model", exec_env=env)
    yield env, session
    session.close()


class TestDefault:
    def test_a_session_without_an_environment_uses_the_host(self, tmp_db):
        session = HeadlessSession(client=MagicMock(), model="eval-model")
        try:
            assert session._exec_env is HOST_ENVIRONMENT
            assert session._background_shells._env is HOST_ENVIRONMENT
            assert session._offered_tool_names is None
            # Every headless tool is offered, as before.
            offered = {t["function"]["name"] for t in session._eval_tools}
            assert offered == INTERACTIVE_TOOL_NAMES - {"read_resource", "use_prompt"}
        finally:
            session.close()

    def test_the_host_has_no_bounds(self):
        assert HOST_ENVIRONMENT.max_text_chars is None
        assert HOST_ENVIRONMENT.max_diff_lines is None

    def test_an_environment_acts_on_this_machine_unless_it_says_otherwise(self):
        """A caller that must keep commands off this machine asks the environment.
        A subclass that forgets to answer is taken to act here."""
        assert HOST_ENVIRONMENT.acts_on_host is True
        assert _RecordingHost().acts_on_host is True
        assert _UnstartedHost().acts_on_host is True
        assert _MemoryEnvironment().acts_on_host is False

    def test_a_sub_agents_tools_name_the_environments_directory(self, memory):
        """The directory notes of the task agent's tool list follow the environment,
        not this process, and name no host workspace."""
        _env, session = memory
        by_name = {t["function"]["name"]: t["function"]["description"] for t in session._task_tools}
        assert "Commands run in /work" in by_name["bash"]
        assert "Relative paths resolve against /work." in by_name["read_file"]
        assert "workspace directory" not in by_name["bash"]

    def test_the_host_environment_is_the_plain_calls(self, tmp_path):
        target = tmp_path / "sub" / "a.txt"
        HOST_ENVIRONMENT.write_text(str(target), "one" + NL + "two" + NL, make_parents=True)
        HOST_ENVIRONMENT.write_text(str(target), "three" + NL, append=True)

        assert target.read_text() == "one" + NL + "two" + NL + "three" + NL
        assert HOST_ENVIRONMENT.read_lines(str(target)) == ["one" + NL, "two" + NL, "three" + NL]
        assert HOST_ENVIRONMENT.read_bytes(str(target), 3) == b"one"
        assert HOST_ENVIRONMENT.stat(str(target)).st_size == 14
        assert HOST_ENVIRONMENT.realpath(str(target)) == os.path.realpath(target)
        assert HOST_ENVIRONMENT.getcwd() == os.getcwd()
        with pytest.raises(FileNotFoundError):
            HOST_ENVIRONMENT.read_text(str(tmp_path / "missing"))


class TestACommandThatCannotBeWritten:
    def test_the_hosts_shell_start_leaves_no_script_behind(self, tmp_path, monkeypatch):
        """Half of a surrogate pair, which a model can write as an escape: no
        encoding writes it.  The start fails before a process exists, and used
        to leave an empty script file in the temporary directory every time."""
        import tempfile

        from turnstone.core.background_shells import spawn_group_leader

        monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
        command = "echo " + chr(0xD800)
        with pytest.raises(UnicodeEncodeError):
            spawn_group_leader(command, stop_on_error=False, env=None)
        assert os.listdir(tmp_path) == []
        # The same through the environment a session without one of its own uses.
        with pytest.raises(UnicodeEncodeError):
            ExecutionEnvironment().spawn(command, stop_on_error=True, env=None)
        assert os.listdir(tmp_path) == []

    def test_a_command_that_can_be_written_still_runs(self, tmp_path, monkeypatch):
        import tempfile

        from turnstone.core.background_shells import spawn_group_leader

        monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
        proc, _pgid, script_path = spawn_group_leader("echo written", stop_on_error=True, env=None)
        try:
            out, _err = proc.communicate(timeout=30)
            assert proc.returncode == 0 and out == "written" + NL
            # The script is the caller's to remove, as before.
            assert os.listdir(tmp_path) == [os.path.basename(script_path)]
        finally:
            os.unlink(script_path)


class TestFileTools:
    def test_write_then_read_lives_only_in_the_environment(self, memory):
        env, session = memory
        written, read = _run(
            session,
            _call("w", "write_file", path="notes/a.txt", content="alpha" + NL + "beta" + NL),
        ) + _run(session, _call("r", "read_file", path="notes/a.txt"))

        assert written == "Wrote 11 chars to notes/a.txt"
        assert read == f"   1{TAB}alpha{NL}   2{TAB}beta"
        # Relative paths resolved against the environment's directory, not this process's.
        assert env.files == {"/work/notes/a.txt": b"alpha" + NL.encode() + b"beta" + NL.encode()}
        assert not os.path.exists("notes/a.txt")

    def test_home_expands_to_the_environments_home(self, memory):
        env, session = memory
        _run(session, _call("w", "write_file", path="~/x.txt", content="x"))
        assert env.files == {"/home/agent/x.txt": b"x"}

    def test_append_and_a_range_read(self, memory):
        env, session = memory
        env.files["/work/log"] = ("".join(f"line {i}{NL}" for i in range(1, 6))).encode()
        _run(session, _call("w", "write_file", path="log", content="line 6" + NL, mode="append"))
        (read,) = _run(session, _call("r", "read_file", path="/work/log", offset=5, limit=2))
        assert read == f"   5{TAB}line 5{NL}   6{TAB}line 6"

    def test_a_missing_or_binary_file_is_reported_from_the_environment(self, memory):
        env, session = memory
        env.files["/work/blob"] = b"ab" + bytes([0]) + b"cd"
        missing, binary = _run(
            session, _call("m", "read_file", path="nope.txt"), _call("b", "read_file", path="blob")
        )
        assert missing == "Error: nope.txt not found"
        assert "appears to be a binary file" in binary

    def test_edit_needs_a_read_and_changes_the_environments_file(self, memory):
        env, session = memory
        env.files["/work/code.py"] = b"x = 1" + NL.encode() + b"y = 2" + NL.encode()
        edit = _call("e", "edit_file", path="code.py", old_string="y = 2", new_string="y = 3")

        (refused,) = _run(session, edit)
        assert refused == "Error: must read_file code.py before editing it"
        _run(session, _call("r", "read_file", path="code.py"))
        (edited,) = _run(session, edit)

        assert edited == "Edited code.py: applied 1 edit"
        assert env.files["/work/code.py"] == b"x = 1" + NL.encode() + b"y = 3" + NL.encode()

    def test_replace_all_and_a_batch_edit(self, memory):
        env, session = memory
        env.files["/work/t"] = b"a b a" + NL.encode() + b"c" + NL.encode()
        _run(session, _call("r", "read_file", path="t"))
        (replaced,) = _run(
            session,
            _call("e1", "edit_file", path="t", old_string="a", new_string="A", replace_all=True),
        )
        (batch,) = _run(
            session,
            _call(
                "e2",
                "edit_file",
                path="t",
                edits=[
                    {"old_string": "b", "new_string": "B"},
                    {"old_string": "c", "new_string": "C"},
                ],
            ),
        )
        assert replaced == "Edited t: replaced 2 occurrences"
        assert batch == "Edited t: applied 2 edits"
        assert env.files["/work/t"] == b"A B A" + NL.encode() + b"C" + NL.encode()

    def test_an_edit_that_would_outgrow_the_environments_bound_is_refused(self, memory):
        """A replace-all multiplies a file by a factor the model chooses, in this
        process.  The size is worked out and refused before the text is built."""
        env, session = memory
        env.max_text_chars = 2_000
        env.files["/work/a"] = b"a" * 500
        _run(session, _call("r", "read_file", path="a"))
        (refused,) = _run(
            session,
            _call(
                "e", "edit_file", path="a", old_string="a", new_string="b" * 50, replace_all=True
            ),
        )
        assert refused.startswith("Error: the edit would make a 25,000 characters long")
        assert env.files["/work/a"] == b"a" * 500

        env.files["/work/b"] = b"start" + NL.encode() + b"end" + NL.encode()
        _run(session, _call("r2", "read_file", path="b"))
        (batch,) = _run(
            session,
            _call(
                "e2",
                "edit_file",
                path="b",
                edits=[
                    {"old_string": "start", "new_string": "s" * 1500},
                    {"old_string": "end", "new_string": "e" * 1500},
                ],
            ),
        )
        assert batch.startswith("Error: the edit would make b 3,002 characters long")
        (fits,) = _run(
            session, _call("e3", "edit_file", path="b", old_string="start", new_string="s" * 100)
        )
        assert fits == "Edited b: applied 1 edit"

    def test_a_diff_past_the_environments_bound_is_refused(self, memory):
        env, session = memory
        env.max_diff_lines = 10
        env.files["/work/a"] = (("x" + NL) * 6).encode()
        env.files["/work/b"] = (("y" + NL) * 6).encode()
        (refused,) = _run(session, _call("d", "diff_file", path_a="a", path_b="b"))
        assert refused.startswith("Error: diff_file compares up to 10 lines here")
        assert "these have 12" in refused
        env.max_diff_lines = 12
        assert "-x" in _run(session, _call("d2", "diff_file", path_a="a", path_b="b"))[0]

    def test_overwriting_an_unread_file_is_seen_in_the_environment(self, memory):
        """The overwrite check asks the environment whether the file exists."""
        env, session = memory
        env.files["/work/old"] = b"keep"
        item = session._prepare_write_file("c", {"path": "old", "content": "new"})
        fresh = session._prepare_write_file("c", {"path": "brand-new", "content": "new"})
        assert item["approval_label"] == "overwrite_file"
        assert fresh["approval_label"] == "write_file"
        assert item["resolved"] == "/work/old"

    def test_diff_reads_both_sides_from_the_environment(self, memory):
        env, session = memory
        env.files["/work/a"] = b"one" + NL.encode() + b"two" + NL.encode()
        env.files["/work/b"] = b"one" + NL.encode() + b"2" + NL.encode()
        (diff,) = _run(session, _call("d", "diff_file", path_a="a", path_b="b"))
        assert "-two" in diff and "+2" in diff and "--- a" in diff

    def test_an_image_is_sized_through_the_environment(self, memory):
        env, session = memory
        env.files["/work/pic.png"] = b"not really a picture"
        (described,) = _run(session, _call("i", "read_file", path="pic.png"))
        assert described.startswith("Binary image file: pic.png (20 bytes)")

    def test_search_runs_the_environments_process_and_program(self, memory):
        env, session = memory
        env.search_output = f"/work/a.py:3:needle here{NL}"
        (found,) = _run(session, _call("s", "search", query="needle", path="src"))

        assert "needle here" in found and "a.py" in found
        (args,) = env.search_requests
        # The preferred search program is not in this environment, so the fallback
        # one, with the caller's path.
        assert args[0] == "grep" and args[-2:] == ["needle", "src"]


class TestShellTools:
    def test_foreground_bash_spawns_and_kills_through_the_environment(self, recording):
        env, session = recording
        (out,) = _run(session, _call("b", "bash", command="echo through-the-seam"))

        assert out == "through-the-seam"
        assert env.spawned == ["echo through-the-seam"]
        # The group is killed after every command, also one that exited normally.
        assert len(env.killed) == 1

    def test_a_background_shell_and_its_tools_use_the_same_environment(self, recording):
        env, session = recording
        (started,) = _run(
            session,
            _call("b", "bash", command="echo started; sleep 30", run_in_background=True),
        )
        assert "Started background shell bash_1" in started
        assert env.spawned == ["echo started; sleep 30"]

        wait_until(lambda: "started" in _run(session, _call("o", "bash_output", id="bash_1"))[0])
        (killed,) = _run(session, _call("k", "kill_shell", id="bash_1"))

        assert "Killed background shell bash_1" in killed
        assert env.killed, "the kill went around the environment"

    def test_the_host_refuses_by_its_fixed_list_and_another_environment_answers_for_itself(
        self, unstarted
    ):
        """The list protects the machine the session runs on.  What it says is not
        changed by the seam; an environment elsewhere is asked instead of it.

        Nothing is started here: were the refusal broken, the command would be
        noted, not run."""
        from turnstone.core.safety import is_command_blocked

        commands = ["chown -R root:root /srv", "grep -rn shutdown src/", "rm -rf /tmp/build"]
        for command in commands:
            assert HOST_ENVIRONMENT.command_refusal(command) == is_command_blocked(command)
            assert is_command_blocked(command)
        assert HOST_ENVIRONMENT.command_refusal("echo fine") is None

        env, session = unstarted
        # ``unstarted`` answers as the host does: the list still applies.
        (refused,) = _run(session, _call("b", "bash", command="chown -R root:root /srv"))
        assert refused == "Blocked: command matches dangerous pattern 'chown -R '"
        assert env.asked == []

        env.command_refusal = lambda command: (  # type: ignore[method-assign]
            "Refused here: no echo" if "echo" in command else None
        )
        allowed, own = _run(
            session,
            _call("c", "bash", command="true # chown -R nobody ."),
            _call("d", "bash", command="echo hello"),
        )
        # The environment's own answer let the first through to the start of a
        # process and refused the second before it.
        assert "this environment only notes the command" in allowed
        assert own == "Refused here: no echo"
        assert env.asked == ["true # chown -R nobody ."]

    def test_the_host_names_the_process_and_another_environment_need_not(self, recording):
        env, session = recording
        (named,) = _run(session, _call("b", "bash", command="sleep 30", run_in_background=True))
        assert "Started background shell bash_1 (pid " in named
        _run(session, _call("k", "kill_shell", id="bash_1"))

        env.process_label = lambda proc: ""  # type: ignore[method-assign]
        (unnamed,) = _run(session, _call("b2", "bash", command="sleep 30", run_in_background=True))
        assert unnamed.startswith("Started background shell bash_2. Read new output")
        _run(session, _call("k2", "kill_shell", id="bash_2"))

    def test_cancel_kills_through_the_environment(self, recording):
        env, session = recording
        proc, pgid, script = env.spawn("sleep 30", stop_on_error=False, env=None)
        session._active_procs.add(proc)
        try:
            session.cancel()
            proc.wait(timeout=5)
        finally:
            session._active_procs.discard(proc)
            os.unlink(script)
        assert env.killed == [pgid]


def _group_of(pid: int) -> int | None:
    """The process group of the running process *pid*, ``None`` when there is
    none: one that has ended and only waits to be collected does not run."""
    try:
        with open(f"/proc/{pid}/stat") as status:
            fields = status.read().rpartition(")")[2].split()
    except OSError:
        return None
    return None if fields[0] == "Z" else int(fields[2])


class TestTheHostsGroupKill:
    """The host ends a command's process group through one function, which
    sends no signal to an id that is not certain to be that group's
    (``turnstone.core.background_shells.signal_group``)."""

    @pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")
    def test_what_a_command_left_running_is_ended_after_the_command_was_collected(self):
        """The shell tool kills the group after every command, also one that
        ended by itself and was collected: what the command left running there
        holds the group's id, so it is still that group's."""
        proc, pgid, script = HOST_ENVIRONMENT.spawn(
            "sleep 30 >/dev/null 2>&1 & echo $!", stop_on_error=False, env=None
        )
        try:
            out, _err = proc.communicate(timeout=30)
            left = int(out)
            # Collected, and its sleep is still in the group it led.
            assert proc.returncode == 0 and _group_of(left) == pgid == proc.pid
            HOST_ENVIRONMENT.kill_group(proc, pgid)
            wait_until(lambda: _group_of(left) is None)
        finally:
            os.unlink(script)

    def test_an_id_another_process_has_by_then_is_not_signalled(self):
        """Once the command has been collected its id can be given out again.
        Shown with an object that claims to be a collected command with the id
        of this test's own ``sleep``: the refusal reads like an empty group."""
        from types import SimpleNamespace

        sleeper = subprocess.Popen(["sleep", "30"], start_new_session=True)
        try:
            collected = SimpleNamespace(pid=sleeper.pid, returncode=0)
            with pytest.raises(ProcessLookupError, match="no process group of this command's"):
                HOST_ENVIRONMENT.kill_group(collected, sleeper.pid)  # type: ignore[arg-type]
            assert sleeper.poll() is None
            # The command's own group, while it runs, is signalled as ever.
            HOST_ENVIRONMENT.kill_group(sleeper, sleeper.pid)  # type: ignore[arg-type]
            assert sleeper.wait(timeout=10) < 0
        finally:
            if sleeper.poll() is None:
                sleeper.kill()
            sleeper.wait(timeout=10)

    @pytest.mark.parametrize("pgid", [True, 0, 1, -1, "4242", None, MagicMock().pid])
    def test_an_id_that_is_no_group_of_a_commands_is_refused(self, pgid):
        """Judged only, by the function the kill asks first: the id 1 is every
        process the user may signal, 0 the caller's own group, and an object
        that stands in for a process converts to 1."""
        from types import SimpleNamespace

        from turnstone.core.background_shells import group_signal_refusal

        for leader in (SimpleNamespace(pid=pgid, returncode=None), MagicMock()):
            assert group_signal_refusal(leader, pgid, after_exit=True) is not None
        own = os.getpgrp()
        claims = SimpleNamespace(pid=own, returncode=None)
        assert group_signal_refusal(claims, own, after_exit=True) is not None


class TestOfferedTools:
    def test_environment_tools_are_the_shell_and_file_tools(self):
        assert (
            frozenset(
                {
                    "bash",
                    "bash_output",
                    "kill_shell",
                    "read_file",
                    "write_file",
                    "edit_file",
                    "search",
                    "diff_file",
                }
            )
            == ENVIRONMENT_TOOL_NAMES
        )
        assert ENVIRONMENT_TOOL_NAMES <= INTERACTIVE_TOOL_NAMES

    def test_a_session_elsewhere_offers_only_environment_tools(self, memory):
        _env, session = memory
        offered = {t["function"]["name"] for t in session._eval_tools}
        assert offered == ENVIRONMENT_TOOL_NAMES

    def test_descriptions_name_the_environments_directory(self, memory):
        _env, session = memory
        by_name = {t["function"]["name"]: t["function"]["description"] for t in session._eval_tools}
        assert "Commands run in /work" in by_name["bash"]
        assert "Relative paths resolve against /work." in by_name["read_file"]

    def test_a_tool_that_was_not_offered_does_not_run(self, memory):
        """Dispatch is by name: without the gate ``web_fetch`` and ``memory`` would
        act on this machine although the session never offered them."""
        _env, session = memory
        session._prepare_web_fetch = MagicMock(side_effect=AssertionError("ran"))  # type: ignore[method-assign]
        session._prepare_memory = MagicMock(side_effect=AssertionError("ran"))  # type: ignore[method-assign]

        fetch, remember = _run(
            session,
            _call("f", "web_fetch", url="http://example.invalid/", question="x"),
            _call("m", "memory", action="save", key="n", content="c"),
        )

        for text in (fetch, remember):
            assert "is not available in this session. Tools offered: bash, bash_output" in text
        assert session._tool_error_flags == {"f": True, "m": True}

    def test_the_caller_can_name_the_tools(self, tmp_db):
        session = HeadlessSession(
            client=MagicMock(), model="eval-model", tool_names={"bash", "web_search"}
        )
        try:
            assert {t["function"]["name"] for t in session._eval_tools} == {"bash", "web_search"}
            (refused,) = _run(session, _call("r", "read_file", path="/etc/hostname"))
            assert "Tool 'read_file' is not available in this session" in refused
        finally:
            session.close()

    def test_an_unknown_tool_name_is_refused_at_construction(self, tmp_db):
        with pytest.raises(ValueError, match="no_such_tool"):
            HeadlessSession(client=MagicMock(), model="eval-model", tool_names={"no_such_tool"})

    def test_a_refused_construction_leaves_nothing_registered(self, tmp_db):
        """The session registers with its MCP client before the names are looked at;
        nobody gets a session to close when the constructor raises."""
        mcp = MagicMock()
        mcp.get_tools.return_value = []
        with pytest.raises(ValueError, match="no_such_tool"):
            HeadlessSession(
                client=MagicMock(),
                model="eval-model",
                tool_names={"no_such_tool"},
                mcp_client=mcp,
            )
        assert mcp.add_listener.call_count == 1
        assert mcp.remove_listener.call_count == 1


class TestSubAgentOfALimitedRun:
    """A task agent spawned by a limited run acts where the run acts, with its tools."""

    PARENT: list[dict[str, Any]] = [
        {"tool_calls": [{"name": "task_agent", "arguments": {"prompt": "Write the note."}}]},
        {"content": "Done."},
    ]
    AGENT: list[dict[str, Any]] = [
        {
            "tool_calls": [
                {
                    "name": "write_file",
                    "arguments": {"path": "note.txt", "content": "by the agent"},
                },
                {
                    "name": "web_fetch",
                    "arguments": {"url": "http://example.invalid/", "question": "?"},
                },
                {"name": "notify", "arguments": {"message": "hello"}},
                {"name": "bash", "arguments": {"command": "echo on the host"}},
            ]
        },
        {"tool_calls": [{"name": "read_file", "arguments": {"path": "note.txt"}}]},
        {"content": "note written"},
    ]

    def test_nothing_it_does_runs_outside_the_environment(self, tmp_db):
        from openai import OpenAI

        from tests._scripted_chat_server import ScriptedChatServer

        env = _MemoryEnvironment()
        with ScriptedChatServer(self.PARENT, agent_script=self.AGENT) as server:
            client = OpenAI(base_url=server.base_url, api_key="none", max_retries=0)
            session = HeadlessSession(
                client=client,
                model="scripted",
                system_prompt_override="You are a test.",
                exec_env=env,
                tool_names=ENVIRONMENT_TOOL_NAMES | {"task_agent"},
            )
            # Any host-side executor a sub-agent could reach fails the test if it runs.
            for name in (
                "_exec_web_fetch",
                "_exec_notify",
                "_prepare_web_fetch",
                "_prepare_notify",
            ):
                setattr(session, name, MagicMock(side_effect=AssertionError(name + " ran")))
            try:
                log = session.send_headless("go", max_turns=3)
            finally:
                session.close()
                client.close()
            agent_requests = server.agent_requests

        assert [(e["tool"], e["ok"], e["result"]) for e in log] == [
            ("task_agent", True, "note written")
        ]
        # The file exists in the environment and nowhere else.
        assert env.files == {"/work/note.txt": b"by the agent"}
        assert not os.path.exists("note.txt")
        # The agent was offered the run's tools only, with the environment's directory...
        offered = {t["function"]["name"] for t in agent_requests[0]["tools"]}
        assert offered == ENVIRONMENT_TOOL_NAMES
        bash = next(t for t in agent_requests[0]["tools"] if t["function"]["name"] == "bash")
        assert "Commands run in /work" in bash["function"]["description"]
        # ...and its calls outside them were refused; the shell call reached the
        # environment (which runs no commands) and not this machine.
        results = [m["content"] for m in agent_requests[1]["messages"] if m["role"] == "tool"]
        assert results[0] == "Wrote 12 chars to note.txt"
        assert "tool 'web_fetch' is not available in agent mode" in results[1]
        assert "tool 'notify' is not available in agent mode" in results[2]
        assert "this environment runs no commands" in results[3]
        assert "on the host" not in results[3]

    def test_a_sub_agent_cannot_spawn_another(self, tmp_db):
        from openai import OpenAI

        from tests._scripted_chat_server import ScriptedChatServer

        agent = [
            {"tool_calls": [{"name": "task_agent", "arguments": {"prompt": "go deeper"}}]},
            {"content": "stayed at depth one"},
        ]
        with ScriptedChatServer(self.PARENT, agent_script=agent) as server:
            client = OpenAI(base_url=server.base_url, api_key="none", max_retries=0)
            session = HeadlessSession(
                client=client,
                model="scripted",
                system_prompt_override="You are a test.",
                exec_env=_MemoryEnvironment(),
                tool_names=ENVIRONMENT_TOOL_NAMES | {"task_agent"},
            )
            try:
                session.send_headless("go", max_turns=3)
            finally:
                session.close()
                client.close()
            first, second = server.agent_requests

        assert "task_agent" not in {t["function"]["name"] for t in first["tools"]}
        (result,) = [m["content"] for m in second["messages"] if m["role"] == "tool"]
        assert result == "Error: agents cannot spawn further agents"
        assert len(server.agent_requests) == 2  # one sub-agent, no second conversation


class TestOwnMcpTools:
    """A run that brings its own MCP servers offers their tools, and no others."""

    TOOL = {
        "type": "function",
        "function": {
            "name": "mcp__ledger__post_entry",
            "description": "Post an entry to the ledger.",
            "parameters": {"type": "object", "properties": {"amount": {"type": "number"}}},
        },
    }

    def _manager(self) -> MagicMock:
        from turnstone.core.mcp_client import MCPClientManager

        manager = MagicMock(spec=MCPClientManager)
        manager.get_tools.return_value = [self.TOOL]
        manager.get_resources.return_value = []
        manager.get_prompts.return_value = []
        manager.is_mcp_tool.side_effect = lambda name, **_kw: name == "mcp__ledger__post_entry"
        return manager

    def test_the_sessions_own_mcp_tools_are_offered_beside_the_environment_tools(self, tmp_db):
        session = HeadlessSession(
            client=MagicMock(),
            model="eval-model",
            exec_env=_MemoryEnvironment(),
            mcp_client=self._manager(),
            mcp_tools=True,
        )
        try:
            offered = [t["function"]["name"] for t in session._eval_tools]
            assert set(offered) == ENVIRONMENT_TOOL_NAMES | {"mcp__ledger__post_entry"}
            assert offered[-1] == "mcp__ledger__post_entry"  # built-in tools come first
            assert session._eval_tools[-1] == self.TOOL

            session._exec_mcp_tool = MagicMock(return_value=("p", "posted"))  # type: ignore[method-assign]
            posted, other = _run(
                session,
                _call("p", "mcp__ledger__post_entry", amount=3),
                _call("o", "mcp__payroll__run", month=1),
            )
            assert posted == "posted"
            # A tool of a server this run did not bring is not reachable.
            assert "Tool 'mcp__payroll__run' is not available in this session" in other
        finally:
            session.close()

    def test_a_client_that_cannot_list_its_tools_leaves_nothing_registered(self, tmp_db):
        """The read that fixes the offered list comes after the session has registered
        with the client; nobody gets a session to close when it fails."""
        working = self._manager()
        HeadlessSession(
            client=MagicMock(), model="eval-model", mcp_client=working, mcp_tools=True
        ).close()
        reads = working.get_tools.call_count  # the last of them is the option's own

        manager = self._manager()
        answers: list[Any] = [[self.TOOL]] * (reads - 1) + [RuntimeError("the server went away")]
        manager.get_tools.side_effect = answers
        with pytest.raises(RuntimeError, match="went away"):
            HeadlessSession(
                client=MagicMock(), model="eval-model", mcp_client=manager, mcp_tools=True
            )
        assert manager.add_listener.call_count == manager.remove_listener.call_count == 1

    def test_without_the_option_no_mcp_tool_is_offered_or_run(self, tmp_db):
        session = HeadlessSession(
            client=MagicMock(),
            model="eval-model",
            exec_env=_MemoryEnvironment(),
            mcp_client=self._manager(),
        )
        try:
            assert {t["function"]["name"] for t in session._eval_tools} == ENVIRONMENT_TOOL_NAMES
            session._exec_mcp_tool = MagicMock(side_effect=AssertionError("ran"))  # type: ignore[method-assign]
            (refused,) = _run(session, _call("p", "mcp__ledger__post_entry", amount=3))
            assert "is not available in this session" in refused
        finally:
            session.close()

    def test_the_caller_can_pick_among_them_and_needs_a_client(self, tmp_db):
        session = HeadlessSession(
            client=MagicMock(),
            model="eval-model",
            mcp_client=self._manager(),
            mcp_tools=True,
            tool_names={"bash", "mcp__ledger__post_entry"},
        )
        try:
            assert [t["function"]["name"] for t in session._eval_tools] == [
                "bash",
                "mcp__ledger__post_entry",
            ]
        finally:
            session.close()
        with pytest.raises(ValueError, match="mcp_client"):
            HeadlessSession(client=MagicMock(), model="eval-model", mcp_tools=True)


class TestNoDestructiveCommandRunsHere:
    """The suite's own guard (``tests/_host_guard.py``, installed for every test by
    ``conftest.py``): the host's process start refuses a list of destructive shapes
    whatever the code under test does."""

    @pytest.mark.parametrize(
        "command",
        [
            "kill -9 -1",
            "touch before; kill -9 -1",
            "kill -s TERM -- -1",
            "for n in $(seq 1 64); do kill -$n 1 2>/dev/null; done",
            "killall5 -9",
            "rm -rf /",
            "rm -rf /*",
            "rm -rf ~",
            "rm -rf $HOME",
            "cd /tmp && rm -fr /usr",
            "rm -r /home/someone",
            "chown -R root:root /srv",
            "chmod -R 777 /",
            "mv / /old",
            ":(){ :|:& };:",
            "shutdown -h now",
            "make test && reboot",
            "systemctl poweroff",
            "dd if=/dev/zero of=/dev/sda bs=1M",
            "cat image > /dev/nvme0n1",
            "mkfs.ext4 /dev/sdb1",
            "sudo true",
            "git clean -fdx; echo ran",
            "git reset --hard HEAD~1",
        ],
    )
    def test_the_list_knows_what_must_not_run_on_this_machine(self, command):
        assert destructive_shape(command) is not None

    @pytest.mark.parametrize(
        "command",
        [
            "echo fine",
            "ls out; cat out/a.txt; cat seed.txt",
            "sleep 30",
            "echo started; sleep 30",
            "true # chown -R nobody .",
            "kill -9 4242",
            "kill %1",
            "pkill -f pytest",
            "rm -rf build/",
            "rm -rf /tmp/pytest-of-someone/pytest-0/test_x0/build",
            "chmod -R a+rwX .",
            "grep -rn shutdown src/",
            "cat docs/reboot_notes.md",
            "dd if=/dev/zero of=blob bs=1M count=1",
            "echo x > /dev/null",
            "git status",
            "python -m pytest tests/test_shutdown.py",
        ],
    )
    def test_what_tests_ordinarily_run_passes(self, command):
        assert destructive_shape(command) is None

    def test_a_listed_command_is_refused_before_anything_starts(self):
        started: list[str] = []
        tripped: list[str] = []
        spawn = guarded_spawn(lambda command, **_kwargs: started.append(command), tripped)
        with pytest.raises(DestructiveCommandError, match="signals every process"):
            spawn("touch before; kill -9 -1", stop_on_error=False, env=None)
        assert started == [] and len(tripped) == 1
        spawn("echo fine", stop_on_error=False, env=None)
        assert started == ["echo fine"]

    def test_the_hosts_process_start_is_behind_it(
        self, tmp_path, monkeypatch, _no_destructive_command_on_this_machine
    ):
        """Shown with a harmless command the list is made to refuse for this test:
        nothing destructive is handed to the host, guard or no guard."""
        from tests import _host_guard

        tripped = _no_destructive_command_on_this_machine
        marker = tmp_path / "marker"
        monkeypatch.setattr(
            _host_guard,
            "destructive_shape",
            lambda command: "this test says so" if "marker" in command else None,
        )
        with pytest.raises(DestructiveCommandError, match="this test says so"):
            HOST_ENVIRONMENT.spawn(f"touch {marker}", stop_on_error=False, env=None)
        assert not marker.exists()
        # It was noted, to fail the test at its end; this one is the test's own.
        assert len(tripped) == 1
        tripped.clear()
