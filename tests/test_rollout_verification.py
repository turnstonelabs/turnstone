"""Checking a rollout after the model has finished.

The container tests need a container runtime and the test image, like
``tests/test_rollout_container.py``; the others need neither.
"""

from __future__ import annotations

import io
import os
import stat
import tarfile
import time
from typing import Any

import pytest

from tests._scripted_chat_server import ScriptedChatServer
from tests.test_rollout_container import (
    IMAGE,
    SMALL,
    _listed,
    _runtime,
    needs_container,
    note_containers,
)
from turnstone.core import config as config_module
from turnstone.rollout.container import (
    Completed,
    Container,
    ContainerError,
    ContainerLostError,
    ContainerSpec,
    run_captured,
)
from turnstone.rollout.episode import RolloutConfig, Task, run_episode
from turnstone.rollout.verification import (
    AfterRunCommand,
    Entry,
    Exported,
    VerificationContext,
    VerificationError,
    _way_for,
    build_archive,
    end_what_is_left,
    export_paths,
    read_archive,
    run_after,
    run_verifier,
)

NL = chr(10)


@pytest.fixture(autouse=True)
def _no_configuration_file(monkeypatch):
    monkeypatch.setattr(config_module, "_cache", {})


@pytest.fixture(name="containers_made")
def _made(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    return note_containers(monkeypatch)


def _archive() -> bytes:
    """A file, a directory, a symbolic link out of the tree and a named pipe."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        folder = tarfile.TarInfo("tree")
        folder.type = tarfile.DIRTYPE
        archive.addfile(folder)
        note = tarfile.TarInfo("tree/note.txt")
        note.size = 5
        archive.addfile(note, io.BytesIO(b"hello"))
        link = tarfile.TarInfo("tree/link")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        archive.addfile(link)
        pipe = tarfile.TarInfo("tree/pipe")
        pipe.type = tarfile.FIFOTYPE
        archive.addfile(pipe)
    return buffer.getvalue()


def _sparse(first_line: bytes) -> bytes:
    """An archive of one file kept as a map of its data, the map's first line
    (how many pieces follow) being *first_line*."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
        info = tarfile.TarInfo("out.txt")
        data = first_line + NL.encode() + b"0" + NL.encode() + b"5" + NL.encode() + b"hello"
        data += bytes(512 - len(data) % 512)
        info.size = len(data)
        info.pax_headers = {
            "GNU.sparse.major": "1",
            "GNU.sparse.minor": "0",
            "GNU.sparse.name": "out.txt",
            "GNU.sparse.realsize": "5",
        }
        archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


class TestArchives:
    def test_links_and_special_files_are_noted_not_followed_or_made(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        read = read_archive(_archive(), max_bytes=1000)
        assert [(e.path, e.kind) for e in read.entries] == [
            ("tree", "dir"),
            ("tree/note.txt", "file"),
            ("tree/link", "symlink"),
            ("tree/pipe", "other"),
        ]
        assert read.files() == {"tree/note.txt": b"hello"}
        assert read.entries[2].target == "/etc/passwd" and read.entries[2].data is None
        assert read.summary() == {
            "entries": {"dir": 1, "file": 1, "symlink": 1, "other": 1},
            "bytes": 5,
            "too_large": False,
        }
        assert os.listdir(tmp_path) == []  # nothing was made on this machine

    def test_more_than_the_bound_is_said_not_read(self):
        read = read_archive(_archive(), max_bytes=4)
        assert read.too_large is True and read.files() == {}

    def test_what_is_put_back_holds_files_directories_and_links_only(self):
        tree = Exported(
            [
                Entry("src", "dir", 0o755),
                Entry("src/a.py", "file", 0o644, data=b"print(1)"),
                Entry("src/b", "symlink", target="a.py"),
                Entry("src/dev", "other"),
            ]
        )
        again = read_archive(build_archive({"/harness/test.sh": b"exit 0"}, tree), max_bytes=1000)
        assert [(e.path, e.kind) for e in again.entries] == [
            ("src", "dir"),
            ("src/a.py", "file"),
            ("src/b", "symlink"),
            ("harness/test.sh", "file"),
        ]

    def test_an_archive_that_cannot_be_read_is_a_failure_of_the_machinery(self):
        with pytest.raises(VerificationError, match="could not be read"):
            read_archive(b"not an archive at all" * 100, max_bytes=1000)
        with pytest.raises(VerificationError, match="could not be read"):
            read_archive(b"", max_bytes=1000)

    def test_whatever_the_reader_raises_over_one_is_that_same_failure(self):
        """The reader raises its own errors for most of what is wrong with an
        archive, and others for the rest: a count that is no number is a
        ``ValueError``.  Whoever writes the archive decides which, so none may
        get past as something else."""
        assert read_archive(_sparse(b"1"), max_bytes=1000).files() == {"out.txt": bytes(5)}
        with pytest.raises(VerificationError, match="could not be read: ValueError"):
            read_archive(_sparse(b"one"), max_bytes=1000)


class TestTheTasksFilesWin:
    """What the model left in the way of a file the task holds makes way for it."""

    def test_what_makes_way_for_them(self):
        files = {"/work/tests/test_a.py": b"", "work/tests/sub/b.py": b"", "top.txt": b""}
        assert _way_for(files) == [
            "d",
            "work",
            "d",
            "work/tests",
            "f",
            "work/tests/test_a.py",
            "d",
            "work/tests/sub",
            "f",
            "work/tests/sub/b.py",
            "f",
            "top.txt",
        ]

    @pytest.mark.parametrize("path", ["../outside", "a/../../outside", ".."])
    def test_a_path_that_leaves_the_place_it_is_put_under_is_refused(self, path):
        with pytest.raises(ValueError, match="must stay below"):
            _way_for({path: b""})

    def test_an_entry_below_something_that_is_no_directory_is_left_out(self):
        """Unpacked, it would land wherever the link above it points."""
        tree = Exported(
            [
                Entry("a", "symlink", target="/etc"),
                Entry("a/passwd", "file", data=b"x"),
                Entry("b", "file", data=b"y"),
                Entry("b/c", "file", data=b"z"),
                Entry("d", "dir", 0o755),
                Entry("d/e", "file", data=b"kept"),
            ]
        )
        again = read_archive(build_archive(None, tree), max_bytes=1000)
        assert [e.path for e in again.entries] == ["a", "b", "d", "d/e"]


def _context() -> VerificationContext:
    return VerificationContext(
        task=None,
        container_name=None,
        container_stopped=True,
        container_state={},
        after_run=[],
        record={},
    )


class TestTheVerifierCall:
    def test_a_verdict_comes_back(self):
        result, failure = run_verifier(lambda _c: {"verdict": "fail", "tests": 3}, _context(), 5)
        assert result == {"verdict": "fail", "tests": 3} and failure is None

    @pytest.mark.parametrize("returned", [None, {}, {"verdict": None}, "pass"])
    def test_no_verdict_is_a_failure(self, returned):
        result, failure = run_verifier(lambda _c: returned, _context(), 5)
        assert result is None and failure == "the verifier returned no verdict"

    def test_a_verifier_that_raises(self):
        def broken(_context: VerificationContext) -> dict[str, Any]:
            raise KeyError("reward")

        result, failure = run_verifier(broken, _context(), 5)
        assert result is None and failure == "the verifier raised KeyError: 'reward'"

    @pytest.mark.allow_thread_leak
    def test_a_verifier_that_runs_out_of_time(self):
        started = time.monotonic()
        result, failure = run_verifier(
            lambda _c: time.sleep(5) or {"verdict": "pass"}, _context(), 0.3
        )
        assert result is None and failure == "the verifier did not finish within 0.3 s"
        assert time.monotonic() - started < 3


class _AtTheRead:
    """Stands in for a container when the files a command names are read: its
    archive program gives back what the test says."""

    workdir = "/workspace"

    def __init__(
        self, read: Completed, *, programs_are_the_models: bool, answers: bool = True
    ) -> None:
        self.read = read
        self.programs_are_the_models = programs_are_the_models
        self.answers = answers
        self.gone = False
        self.asked: list[list[str]] = []
        # How each was asked: the user, the directory it is started in.
        self.how: list[dict[str, Any]] = []

    def commands_can_change_programs(self) -> bool:
        return self.programs_are_the_models

    def ensure_running(self) -> None:
        if self.gone:
            raise ContainerLostError(5, "the sandbox has stopped")

    def exec(self, argv: list[str], **options: Any) -> Completed:
        self.asked.append(argv)
        self.how.append(options)
        if argv[0] == "tar":
            return self.read
        return Completed(0 if self.answers else None, b"", b"", not self.answers, 0)


def _read(
    code: int | None, out: bytes = b"", err: bytes = b"", timed_out: bool = False
) -> Completed:
    return Completed(code, out, err, timed_out, len(out))


# Every way a read can fail, by what the archive program's call came back with.
CUT_SHORT = {
    "its time ran out": _read(None, timed_out=True),
    "the archive program was ended": _read(137),
}
NO_ARCHIVE = {
    "there is no archive program": _read(127, b"exec failed: executable file not found"),
    "the archive program says it failed": _read(2, err=b"tar: something is wrong"),
    "what it wrote is no archive": _read(0, b"not an archive at all" * 100),
    "it wrote nothing": _read(0),
    "the reader raises over what it wrote": _read(0, _sparse(b"one")),
}
FAILED = {**CUT_SHORT, **NO_ARCHIVE}


class TestReadingTheFilesACommandNames:
    COMMAND = AfterRunCommand(["true"], collect=["/work/out.txt"])

    @pytest.fixture(autouse=True)
    def _nothing_left_running(self, monkeypatch):
        """These are about the read; ending what is left running has its own tests."""
        import turnstone.rollout.verification as verification

        nothing = {"found": 0, "still_running": [], "frozen": True}
        monkeypatch.setattr(verification, "end_what_is_left", lambda _container: nothing)

    def test_what_was_read_comes_back(self):
        container = _AtTheRead(_read(0, _archive()), programs_are_the_models=False)
        result = run_after(container, self.COMMAND)  # type: ignore[arg-type]
        assert result["collected"] == {"tree/note.txt": "hello"}
        assert "failed" not in result["collected_summary"]
        # A file that changed while it was read is an archive all the same.
        container = _AtTheRead(_read(1, _archive()), programs_are_the_models=False)
        assert run_after(container, self.COMMAND)["collected"]  # type: ignore[arg-type]

    @pytest.mark.parametrize("how", FAILED)
    def test_where_the_programs_are_the_models_a_failed_read_is_a_result(self, how):
        """Root over a writable root decides what the archive program does, in
        every one of these ways.  A read that fails there must not take the
        rollout out of training."""
        container = _AtTheRead(FAILED[how], programs_are_the_models=True)
        result = run_after(container, self.COMMAND)  # type: ignore[arg-type]
        assert result["collected"] == {}
        assert isinstance(result["collected_summary"]["failed"], str)
        assert result["collected_summary"]["failed"]

    @pytest.mark.parametrize("how", NO_ARCHIVE)
    def test_elsewhere_it_is_a_failure_of_the_machinery(self, how):
        """The archive program is the image's there: the model cannot have
        made it fail like this."""
        container = _AtTheRead(NO_ARCHIVE[how], programs_are_the_models=False)
        with pytest.raises(VerificationError):
            run_after(container, self.COMMAND)  # type: ignore[arg-type]

    @pytest.mark.parametrize("how", CUT_SHORT)
    def test_a_read_cut_short_elsewhere_is_the_models_while_the_container_answers(self, how):
        """What the model does decide there is how much stands at the paths:
        more than a read gets through in its time or in the container's
        memory.  The container answers at once afterwards."""
        container = _AtTheRead(CUT_SHORT[how], programs_are_the_models=False)
        result = run_after(container, self.COMMAND)  # type: ignore[arg-type]
        assert result["collected"] == {}
        assert "the container still answers" in result["collected_summary"]["failed"]
        assert container.asked[-1] == ["true"]

    @pytest.mark.parametrize("how", CUT_SHORT)
    def test_and_the_machinerys_when_it_does_not(self, how):
        container = _AtTheRead(CUT_SHORT[how], programs_are_the_models=False, answers=False)
        with pytest.raises(VerificationError, match="timed out|cut short"):
            run_after(container, self.COMMAND)  # type: ignore[arg-type]

    @pytest.mark.parametrize("programs_are_the_models", [True, False])
    def test_a_container_that_is_gone_says_so_itself(self, programs_are_the_models):
        """Whose doing that is, the container's own state says; it is no result
        of the read in either kind of container."""
        container = _AtTheRead(_read(137), programs_are_the_models=programs_are_the_models)
        container.gone = True
        with pytest.raises(ContainerLostError):
            run_after(container, self.COMMAND)  # type: ignore[arg-type]

    def test_more_than_will_be_read_is_said_whoever_owns_the_programs(self):
        more = Completed(None, b"x", b"", False, 10, stopped_early=True)
        result = run_after(_AtTheRead(more, programs_are_the_models=False), self.COMMAND)  # type: ignore[arg-type]
        assert result["collected_summary"]["too_large"] is True

    def test_the_read_does_not_start_in_the_tasks_working_directory(self):
        """That directory is the model's to rename or remove where it owns
        the directory above it, and the runtime starts nothing in a
        directory that is not there: the read failed as the machinery's.
        It is started in the root directory, as root, and so is the
        question whether the container still answers."""
        container = _AtTheRead(_read(0, _archive()), programs_are_the_models=False)
        run_after(container, self.COMMAND)  # type: ignore[arg-type]
        asked = zip(container.asked, container.how, strict=True)
        (read,) = [how for argv, how in asked if argv[0] == "tar"]
        assert (read["workdir"], read["user"]) == ("/", "0:0")
        container = _AtTheRead(_read(137), programs_are_the_models=False)
        run_after(container, self.COMMAND)  # type: ignore[arg-type]
        assert container.asked[-1] == ["true"]
        assert (container.how[-1]["workdir"], container.how[-1]["user"]) == ("/", "0:0")

    def test_a_relative_path_is_read_below_the_working_directory_all_the_same(self):
        """In a call of its own, by its whole path; what it holds comes back
        under the name the task gave it, as when the read started there."""
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            for name in ("workspace/out", "workspace/out/a.txt", "workspace/b.txt"):
                info = tarfile.TarInfo(name)
                if name.endswith("out"):
                    info.type = tarfile.DIRTYPE
                    archive.addfile(info)
                else:
                    info.size = 2
                    archive.addfile(info, io.BytesIO(b"hi"))
        container = _AtTheRead(_read(0, buffer.getvalue()), programs_are_the_models=False)
        command = AfterRunCommand(["true"], collect=["out", "/etc/hostname", "b.txt"])
        result = run_after(container, command)  # type: ignore[arg-type]
        reads = [argv[argv.index("--") + 1 :] for argv in container.asked if argv[0] == "tar"]
        assert reads == [["/etc/hostname"], ["/workspace/out", "/workspace/b.txt"]]
        # The stand-in gives the same archive to both calls: the first keeps
        # the names of absolute paths, the second those the task gave.
        assert set(result["collected"]) == {"workspace/out/a.txt", "workspace/b.txt"} | {
            "out/a.txt",
            "b.txt",
        }


def _stand_in_runtime(tmp_path: Any) -> str:
    """A stand-in for the runtime's command that plays a container being held
    still, listed and let run again.  It notes what it is asked and starts
    nothing; the files beside it say what it answers.

    The list of processes is the file ``top.<n>`` the n-th time it is asked
    and ``top.last`` from then on.  Their names are asked apart: the file
    ``names``, and a refusal where that file is missing.

    Held still, the container has the file ``paused``, and the runtime says
    so when asked.  Like the runtime, the stand-in refuses to let a
    container run again that it does not count as held.  With the file
    ``pause-seconds`` it holds the container still at once and answers only
    that many seconds later; with ``no-pause`` or ``no-unpause`` it refuses
    the one or the other, and with ``no-pause-once`` the first hold only.

    A helper takes the seconds in the file ``run-seconds`` and ends with the
    status in ``run-status``."""
    path = tmp_path / "runtime"
    lines = [
        "#!/bin/sh",
        f'dir="{tmp_path}"',
        'echo "$*" >> "$dir/calls"',
        'case "$1" in',
        '  pause|unpause) if [ -e "$dir/no-$1" ]; then echo "cannot $1" >&2; exit 1; fi ;;',
        "esac",
        'if [ "$1" = pause ] && [ -e "$dir/no-pause-once" ]; then',
        '  rm "$dir/no-pause-once"; echo "cannot pause: gave up half way" >&2; exit 1',
        "fi",
        'case "$1" in',
        "  pause)",
        '    touch "$dir/paused"',
        '    if [ -e "$dir/pause-seconds" ]; then sleep "$(cat "$dir/pause-seconds")"; fi ;;',
        "  unpause)",
        '    if [ ! -e "$dir/paused" ]; then echo "Container $2 is not paused" >&2; exit 1; fi',
        '    rm -f "$dir/paused" ;;',
        "  inspect)",
        '    case "$*" in',
        "      *State.Pid*) echo 4242 ;;",
        '      *State.Paused*) if [ -e "$dir/paused" ]; then echo true; else echo false; fi ;;',
        '      *) if [ -e "$dir/gone" ]; then echo false 137 false; else echo true 0 false; fi ;;',
        "    esac ;;",
        "  top)",
        '    if [ -e "$dir/gone" ]; then echo "Error response from daemon: not running" >&2; exit 1; fi',
        '    case "$*" in',
        '      *pid,comm) cat "$dir/names" || exit 1 ;;',
        "      *)",
        '        n=$(cat "$dir/tops" 2>/dev/null || echo 0)',
        '        echo $((n + 1)) > "$dir/tops"',
        '        if [ -e "$dir/top.$n" ]; then cat "$dir/top.$n"; else cat "$dir/top.last"; fi ;;',
        "    esac ;;",
        '  run) if [ -e "$dir/run-seconds" ]; then sleep "$(cat "$dir/run-seconds")"; fi',
        '    exit "$(cat "$dir/run-status" 2>/dev/null || echo 0)" ;;',
        "esac",
        "exit 0",
    ]
    path.write_text(NL.join(lines) + NL)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


MAIN = "4242 65533 65533 1 Ss"
HEADER = "PID RUID RGID NLWP STAT"
# Root over a read-only root whose commands may change the user they run as.
MAY_CHANGE_USERS = ContainerSpec(image=IMAGE, user="0:0", capabilities=("SETUID", "SETGID"))


class TestEndingWhatIsLeftRunning:
    """The step before an after-run command, against a stand-in for the
    runtime: what it asks of the runtime, in which order, and what it makes of
    the answers.  Nothing is started but the stand-in."""

    def _container(
        self,
        tmp_path: Any,
        *,
        root_over_writable_root: bool = False,
        spec: ContainerSpec | None = None,
    ) -> Container:
        spec = spec or ContainerSpec(image=IMAGE)
        if root_over_writable_root:
            spec = ContainerSpec(image=IMAGE, user="0:0", read_only_root=False)
        container = Container(spec, runtime=_stand_in_runtime(tmp_path))
        container.task_uid = 0 if spec.runs_as_root() else 65534
        # Nothing was heard about its end: do not wait for that.
        container._watch._died.set()
        return container

    def _listing(self, tmp_path: Any, name: str, *rows: str) -> None:
        (tmp_path / name).write_text(NL.join([HEADER, MAIN, *rows]) + NL)

    def _names(self, tmp_path: Any, *rows: str) -> None:
        (tmp_path / "names").write_text(NL.join(["PID COMMAND", "4242 sleep", *rows]) + NL)

    def _calls(self, tmp_path: Any) -> list[str]:
        return (tmp_path / "calls").read_text().splitlines()

    def _helper_users(self, tmp_path: Any) -> list[str]:
        """The user each helper was asked for, in order."""
        helpers = [call.split() for call in self._calls(tmp_path) if call.startswith("run ")]
        return [words[words.index("--user") + 1] for words in helpers]

    def test_nothing_left_is_found_while_the_container_is_held_still(self, tmp_path):
        container = self._container(tmp_path)
        self._listing(tmp_path, "top.last")
        assert end_what_is_left(container) == {"found": 0, "still_running": [], "frozen": True}
        asked = [call.split()[0] for call in self._calls(tmp_path)]
        assert asked == ["pause", "inspect", "top", "unpause"]

    def test_each_users_processes_are_ended_as_that_user_and_no_more(self, tmp_path):
        container = self._container(tmp_path)
        self._listing(tmp_path, "top.0", "5000 65534 65534 1 S", "5001 0 0 1 S")
        self._listing(tmp_path, "top.last")
        assert end_what_is_left(container) == {"found": 2, "still_running": [], "frozen": True}
        calls = self._calls(tmp_path)
        helpers = [call.split() for call in calls if call.startswith("run ")]
        assert [words[words.index("--user") + 1] for words in helpers] == ["65534:65534", "0:0"]
        for words in helpers:
            # In the container's process namespace and nowhere else, without a
            # right the user does not have, from the image's own file system.
            assert words[words.index("--pid") + 1] == f"container:{container.name}"
            assert words[words.index("--cap-drop") + 1] == "ALL"
            assert "--privileged" not in words and "--cap-add" not in words
            assert words[words.index("--name") + 1].startswith("tsgym-end-")
            assert words[-6:] == ["sh", IMAGE, "-c", "kill", "-9", "-1"]
        # Never an exec into the container, and it runs again at the end.
        assert not [call for call in calls if call.startswith("exec ")]
        assert calls[0].startswith("pause ") and calls[-1].startswith("unpause ")

    def test_the_list_holds_numbers_and_a_state_and_nothing_a_process_writes(self, tmp_path):
        """Each process's count of threads is among them.  Its name is not: a
        process writes its own, and nothing is decided by one."""
        container = self._container(tmp_path)
        self._listing(tmp_path, "top.last")
        end_what_is_left(container)
        (listing,) = [call for call in self._calls(tmp_path) if call.startswith("top ")]
        assert listing == f"top {container.name} -eo pid,ruid,rgid,nlwp,stat"

    @pytest.mark.parametrize("threads", ["1", "0"])
    def test_one_that_has_ended_and_waits_to_be_collected_is_not_left(self, tmp_path, threads):
        """Its state begins with ``Z`` and no thread of it is left: it is over,
        and only its parent can take it out of the list."""
        container = self._container(tmp_path)
        self._listing(tmp_path, "top.last", f"5000 65534 65534 {threads} Z")
        assert end_what_is_left(container) == {"found": 0, "still_running": [], "frozen": True}
        assert not [call for call in self._calls(tmp_path) if call.startswith("run ")]

    def test_one_whose_first_thread_has_ended_while_another_runs_is_left(self, tmp_path):
        """The list gives such a process the state of one that is over.  It
        runs: it is counted, and ended like any other."""
        container = self._container(tmp_path)
        self._listing(tmp_path, "top.0", "5000 65534 65534 2 Zsl")
        self._listing(tmp_path, "top.last")
        assert end_what_is_left(container) == {"found": 1, "still_running": [], "frozen": True}
        (helper,) = [call.split() for call in self._calls(tmp_path) if call.startswith("run ")]
        assert helper[helper.index("--user") + 1] == "65534:65534"

    def test_such_a_process_that_stays_is_no_list_of_nothing(self, tmp_path):
        """Left out of the list, it ran on beside every after-run command."""
        container = self._container(tmp_path)
        self._listing(tmp_path, "top.last", "5000 65534 65534 2 Zsl")
        with pytest.raises(VerificationError, match="1 still there after 3 tries"):
            end_what_is_left(container)

    def test_a_list_without_a_count_of_threads_cannot_be_read(self, tmp_path):
        """A row whose count is no number says nothing about whether the
        process runs."""
        container = self._container(tmp_path)
        self._listing(tmp_path, "top.last", "5000 65534 65534 - Z")
        with pytest.raises(VerificationError, match="could not be listed"):
            end_what_is_left(container)

    def test_what_is_still_there_after_the_tries_is_the_machinerys_failure(self, tmp_path):
        """An unprivileged model over a read-only root has no means to keep a
        process alive through this; if one is, the step did not work."""
        container = self._container(tmp_path)
        self._listing(tmp_path, "top.last", "5000 65534 65534 1 S")
        self._names(tmp_path, "5000 sleep")
        said = r"could not be ended: 1 still there after 3 tries \(sleep \(user 65534\)\)"
        with pytest.raises(VerificationError, match=said):
            end_what_is_left(container)
        calls = self._calls(tmp_path)
        asked = [call.split()[-1] for call in calls if call.startswith("top ")]
        # Four times the list, and once, for what stayed, the names.
        assert asked == ["pid,ruid,rgid,nlwp,stat"] * 4 + ["pid,comm"]
        # First as the user the list names; then also as the users the task names.
        assert self._helper_users(tmp_path) == [
            "65534:65534",
            "65534:65534",
            "0:0",
            "65534:65534",
            "0:0",
        ]
        assert calls[-2].startswith("unpause ")  # then it is asked whether it still runs

    def test_where_the_programs_are_the_models_it_is_its_outcome_and_is_said(self, tmp_path):
        container = self._container(tmp_path, root_over_writable_root=True)
        self._listing(tmp_path, "top.last", "5000 0 0 1 S", "5001 1000 1000 3 Rl")
        self._names(tmp_path, "5000 not sleeping", "5001 sh")
        assert end_what_is_left(container) == {
            "found": 2,
            "still_running": [
                {"uid": 0, "command": "not sleeping"},
                {"uid": 1000, "command": "sh"},
            ],
            "frozen": True,
        }

    def test_what_stayed_is_named_as_far_as_the_runtime_names_it(self, tmp_path):
        """The names are only shown.  A runtime that does not give them, or
        gives something else, changes nothing about what was found."""
        container = self._container(tmp_path, root_over_writable_root=True)
        self._listing(tmp_path, "top.last", "5000 0 0 1 S", "5001 0 0 1 S")
        # No file of names: the stand-in refuses.
        assert end_what_is_left(container)["still_running"] == [
            {"uid": 0, "command": ""},
            {"uid": 0, "command": ""},
        ]
        # A name with blanks in it, a row that is no row, a process without one.
        (tmp_path / "names").write_text(NL.join(["PID COMMAND", "5000 a b  c", "what", ""]))
        assert end_what_is_left(container)["still_running"] == [
            {"uid": 0, "command": "a b  c"},
            {"uid": 0, "command": ""},
        ]

    def test_one_helper_ends_all_processes_of_one_user_whatever_their_groups(self, tmp_path):
        """A signal goes by the user alone.  A helper for each pair of user
        and group would let a task that may change groups ask for as many
        helpers as it has processes."""
        container = self._container(tmp_path)
        rows = ["5000 0 5 1 S", "5001 0 6 1 S", "5002 7 5 1 S", "5003 0 7 1 S"]
        self._listing(tmp_path, "top.0", *rows)
        self._listing(tmp_path, "top.last")
        assert end_what_is_left(container) == {"found": 4, "still_running": [], "frozen": True}
        assert self._helper_users(tmp_path) == ["0:5", "7:5"]

    def test_a_group_number_the_runtime_takes_for_no_container_costs_nothing(self, tmp_path):
        """The runtime starts a container as no group above 2147483647, and
        the list prints such a number as a negative one.  The helper is that
        user under another group, which ends the process all the same."""
        container = self._container(tmp_path)
        self._listing(tmp_path, "top.0", "5000 60001 -2147483648 1 S", "5001 60002 what 1 S")
        self._listing(tmp_path, "top.last")
        assert end_what_is_left(container) == {"found": 2, "still_running": [], "frozen": True}
        assert self._helper_users(tmp_path) == ["60001:65534", "60002:65534"]

    # One process under a user number above 2147483647, as the list prints it.
    PAST_THE_HALF = "5000 -1294967296 60000 1 S"

    def test_a_user_number_the_runtime_takes_for_no_container_is_read(self, tmp_path):
        """Where the commands may change their user, a process can run under
        any number, also one the runtime starts no helper as.  The whole list
        used to count as unreadable for it, which is the machinery's failure:
        one call of the model's took its rollout out of training.  It is read,
        nobody is asked to be that user, and it is the model's own outcome."""
        container = self._container(tmp_path, spec=MAY_CHANGE_USERS)
        assert not container.commands_can_change_programs()
        self._listing(tmp_path, "top.0", self.PAST_THE_HALF, "5001 2001 60000 1 S")
        self._listing(tmp_path, "top.last", self.PAST_THE_HALF)
        self._names(tmp_path, "5000 sleep")
        assert end_what_is_left(container) == {
            "found": 2,
            "still_running": [{"uid": 3000000000, "command": "sleep"}],
            "frozen": True,
        }
        # The other user's helper; then, as on every later try, the task's own
        # user.  Never a number the runtime would refuse, and no negative one.
        assert self._helper_users(tmp_path) == ["2001:60000", "0:0", "0:0"]

    def test_where_the_commands_cannot_change_their_user_it_is_the_machinerys(self, tmp_path):
        """No command of such a task made that process: the runtime's numbers
        are not the container's own there, or the step did not work."""
        for spec in (
            ContainerSpec(image=IMAGE),
            ContainerSpec(image=IMAGE, capabilities=("SETUID", "SETGID")),
            ContainerSpec(image=IMAGE, user="0:0", capabilities=("SETGID", "KILL")),
        ):
            container = self._container(tmp_path, spec=spec)
            self._listing(tmp_path, "top.last", self.PAST_THE_HALF)
            said = r"1 still there after 3 tries \(a process \(user 3000000000\)\)"
            with pytest.raises(VerificationError, match=said):
                end_what_is_left(container)

    @pytest.mark.parametrize(
        "text",
        ["3000000+", "nobody", "-2147483649", "4294967296", pytest.param("1" * 5000, id="long")],
    )
    def test_a_user_the_list_does_not_give_as_a_number_is_no_way_out_either(self, tmp_path, text):
        """Whatever a list prints in that column where the commands may change
        their user: the row is one process nobody can be asked to end, the
        model's own outcome, and never a list that could not be read."""
        container = self._container(tmp_path, spec=MAY_CHANGE_USERS)
        self._listing(tmp_path, "top.last", f"5000 {text} 60000 1 S")
        assert end_what_is_left(container) == {
            "found": 1,
            "still_running": [{"uid": None, "command": ""}],
            "frozen": True,
        }
        assert self._helper_users(tmp_path) == ["0:0", "0:0"]
        # And the machinery's where no command could have brought it about.
        elsewhere = self._container(tmp_path, spec=ContainerSpec(image=IMAGE))
        with pytest.raises(VerificationError, match=r"\(user unknown\)"):
            end_what_is_left(elsewhere)

    def _asked(self, tmp_path: Any, *of: str) -> list[str]:
        """The first word of each call that begins with one of *of*, in order."""
        return [call.split()[0] for call in self._calls(tmp_path) if call.split()[0] in of]

    def test_a_runtime_that_cannot_hold_a_container_still(self, tmp_path):
        """It is asked to let the container run all the same, and its refusal
        for a container it does not count as held is no failure.  It is also
        asked once more for the hold, to end one it may have left half done."""
        container = self._container(tmp_path)
        (tmp_path / "no-pause").write_text("")
        self._listing(tmp_path, "top.0", "5000 65534 65534 1 S")
        self._listing(tmp_path, "top.last")
        assert end_what_is_left(container) == {"found": 1, "still_running": [], "frozen": False}
        assert self._asked(tmp_path, "pause", "unpause") == [
            "pause",
            "unpause",
            "pause",
            "unpause",
        ]
        assert not (tmp_path / "paused").exists()

    def test_a_hold_the_runtime_confirms_too_late_is_undone(self, tmp_path, monkeypatch):
        """The runtime holds the container still and says so only after the
        time it was given.  Nobody asked it to let the container run again,
        the container stayed held, and every command after that failed with
        the runtime's refusal as its own exit status: the model's, to the record."""
        import turnstone.rollout.verification as verification

        monkeypatch.setattr(verification, "_HOLD_SECONDS", 1.0)
        container = self._container(tmp_path)
        (tmp_path / "pause-seconds").write_text("30")
        self._listing(tmp_path, "top.0", "5000 65534 65534 1 S")
        self._listing(tmp_path, "top.last")
        started = time.monotonic()
        assert end_what_is_left(container) == {"found": 1, "still_running": [], "frozen": False}
        assert time.monotonic() - started < 15
        assert self._asked(tmp_path, "pause", "unpause") == ["pause", "unpause"]
        assert not (tmp_path / "paused").exists()

    def test_a_hold_given_up_half_way_is_asked_for_again_and_ended(self, tmp_path):
        """A runtime that gave up on a hold may have left the processes held
        without counting the container as held, and it lets run only a
        container it counts as held.  Held once more, it can be let run."""
        container = self._container(tmp_path)
        (tmp_path / "no-pause-once").write_text("")
        self._listing(tmp_path, "top.last")
        assert end_what_is_left(container) == {"found": 0, "still_running": [], "frozen": False}
        assert self._asked(tmp_path, "pause", "unpause") == [
            "pause",
            "unpause",
            "pause",
            "unpause",
        ]
        assert not (tmp_path / "paused").exists()

    def test_a_container_that_is_not_let_run_again(self, tmp_path):
        """Still held after the tries, it is of no use to anything that
        follows: the machinery's failure, in the runtime's own words."""
        container = self._container(tmp_path)
        (tmp_path / "no-unpause").write_text("")
        self._listing(tmp_path, "top.last")
        said = r"did not let it run again \(cannot unpause\)"
        with pytest.raises(VerificationError, match=said):
            end_what_is_left(container)
        assert self._asked(tmp_path, "pause", "unpause") == ["pause"] + ["unpause"] * 3
        assert (tmp_path / "paused").exists()

    def test_one_held_after_a_hold_that_was_never_confirmed_is_the_same(
        self, tmp_path, monkeypatch
    ):
        import turnstone.rollout.verification as verification

        monkeypatch.setattr(verification, "_HOLD_SECONDS", 1.0)
        container = self._container(tmp_path)
        (tmp_path / "pause-seconds").write_text("30")
        (tmp_path / "no-unpause").write_text("")
        self._listing(tmp_path, "top.last")
        with pytest.raises(VerificationError, match="did not let it run again"):
            end_what_is_left(container)
        assert self._asked(tmp_path, "unpause") == ["unpause"] * 3

    @pytest.mark.parametrize(
        "listing", ["", HEADER + NL, HEADER + NL + "5000 65534 65534 1 S" + NL, "nonsense"]
    )
    def test_a_list_that_cannot_be_read_is_no_list_of_nothing(self, tmp_path, listing):
        """Also one without the main process in it: an empty list must not pass
        for a container with nothing left in it."""
        container = self._container(tmp_path)
        (tmp_path / "top.last").write_text(listing)
        with pytest.raises(VerificationError, match="could not be listed"):
            end_what_is_left(container)
        assert self._calls(tmp_path)[-1].startswith("unpause ")

    def test_a_container_that_is_gone_says_so_itself(self, tmp_path):
        container = self._container(tmp_path)
        (tmp_path / "gone").write_text("")
        with pytest.raises(ContainerLostError):
            end_what_is_left(container)
        assert container.lost is not None

    def _short_of_time(self, monkeypatch: Any, seconds: float, last_list: float) -> None:
        """Give the step *seconds* in all, *last_list* of them kept back for
        the list that says what stayed."""
        import turnstone.rollout.verification as verification

        monkeypatch.setattr(verification, "_END_SECONDS", seconds)
        monkeypatch.setattr(verification, "_LAST_LIST_SECONDS", last_list)

    def test_the_step_has_one_deadline_and_begins_no_helper_past_it(self, tmp_path, monkeypatch):
        """More users than the step has time for, each with a helper of its
        own, which takes commands that can change their user.  Past the
        step's time every call still got a second, one helper after the
        other, for as many users as there were.  Now no helper is begun that
        would leave no time to list what stayed; that list is the outcome,
        the model's own in such a container."""
        self._short_of_time(monkeypatch, seconds=4.0, last_list=1.5)
        container = self._container(tmp_path, spec=MAY_CHANGE_USERS)
        rows = [f"{5000 + n} {2000 + n} 60000 1 S" for n in range(12)]
        self._listing(tmp_path, "top.last", *rows)
        (tmp_path / "run-seconds").write_text("0.6")
        started = time.monotonic()
        result = end_what_is_left(container)
        took = time.monotonic() - started
        assert result["found"] == 12 and result["frozen"] is True
        assert [one["uid"] for one in result["still_running"]] == list(range(2000, 2012))
        # Some users were reached and not all of them, and the whole step
        # was over with its time.
        assert 2 <= len(self._helper_users(tmp_path)) < 12
        assert took < 4.0 + 2.0
        words = [call.split()[0] for call in self._calls(tmp_path)]
        last_helper = max(index for index, word in enumerate(words) if word == "run")
        # Then: what stayed (the main process is asked for, then the list),
        # its names, the helper's removal by name, and the release.
        assert words[last_helper + 1 :] == ["inspect", "top", "top", "rm", "unpause"]

    def test_at_the_deadline_what_no_command_could_have_made_is_the_machinerys(
        self, tmp_path, monkeypatch
    ):
        """A runtime too busy for its helpers, in a container whose commands
        have one user: no command made the step run out of its time.  The
        helper that was cut at its time is removed by its name at once, and
        once more when the step is over, whatever came of it meanwhile."""
        self._short_of_time(monkeypatch, seconds=3.0, last_list=1.0)
        container = self._container(tmp_path)
        self._listing(tmp_path, "top.last", "5000 65534 65534 1 S", "5001 0 0 1 S")
        (tmp_path / "run-seconds").write_text("30")
        started = time.monotonic()
        said = (
            r"2 still there when the 3 s for it were over \(.*\); the helper could not be "
            r"started: the runtime did not answer in time"
        )
        with pytest.raises(VerificationError, match=said):
            end_what_is_left(container)
        assert time.monotonic() - started < 3.0 + 3.0
        calls = self._calls(tmp_path)
        assert self._helper_users(tmp_path) == ["65534:65534"]
        removed = [call for call in calls if call.startswith("rm ")]
        name = "tsgym-end-" + container.name.removeprefix("tsgym-")
        assert removed == [f"rm --force --volumes {name}"] * 2
        assert calls[-2].startswith("unpause ")

    def test_the_helper_is_removed_by_its_name_when_the_step_is_over(self, tmp_path):
        """Also when every helper came and went as it should: one whose start
        the runtime carries out late would be left behind, never started,
        with nothing to remove it."""
        container = self._container(tmp_path)
        self._listing(tmp_path, "top.0", "5000 65534 65534 1 S")
        self._listing(tmp_path, "top.last")
        end_what_is_left(container)
        calls = self._calls(tmp_path)
        name = "tsgym-end-" + container.name.removeprefix("tsgym-")
        assert calls[-2:] == [f"rm --force --volumes {name}", f"unpause {container.name}"]
        assert len([call for call in calls if call.startswith("rm ")]) == 1

    def test_the_size_watch_looks_inside_at_no_time_of_the_step(self, tmp_path, monkeypatch):
        """Its look is a process of the container: begun during the step it
        would be listed, counted as left running and ended.  From before the
        hold until the container runs again none is begun."""
        import turnstone.rollout.verification as verification

        container = self._container(tmp_path)
        self._listing(tmp_path, "top.0", "5000 65534 65534 1 S")
        self._listing(tmp_path, "top.last")
        real = verification._ask
        asked: list[tuple[str, bool, int]] = []

        def noting(container: Container, *words: str, **how: Any) -> Completed:
            # A look asked for now is given what the last one counted.
            container._unlinked_seen = 123
            asked.append((words[0], container._looking.locked(), container._unlinked_bytes()))
            return real(container, *words, **how)

        monkeypatch.setattr(verification, "_ask", noting)
        assert end_what_is_left(container)["found"] == 1
        assert asked[0][0] == "pause" and asked[-1][0] == "unpause" and len(asked) >= 6
        assert all(locked and seen == 123 for _word, locked, seen in asked)
        assert not container._looking.locked()

    def test_a_helper_that_cannot_be_started_is_removed_and_named(self, tmp_path):
        container = self._container(tmp_path)
        (tmp_path / "run-status").write_text("125")
        self._listing(tmp_path, "top.last", "5000 65534 65534 1 S")
        with pytest.raises(VerificationError, match="the helper could not be started"):
            end_what_is_left(container)
        removed = [call for call in self._calls(tmp_path) if call.startswith("rm ")]
        assert removed and all(call.split()[-1].startswith("tsgym-end-") for call in removed)


class TestTheNumbersOfTheRuntimesList:
    """A user or group number is 32 bits wide.  The list prints one above half
    of that range as a negative number, found by leaving a process under each
    in a container."""

    def test_a_number_is_read_whichever_half_of_its_range_it_is_in(self):
        from turnstone.rollout.verification import _user_number

        assert _user_number("0") == 0 and _user_number("65534") == 65534
        assert _user_number("2147483647") == 2147483647
        assert _user_number("-2147483648") == 2147483648
        assert _user_number("-1294967296") == 3000000000
        assert _user_number("-2") == 4294967294
        # A list that prints such a number as it is reads the same.
        assert _user_number("3000000000") == 3000000000
        assert _user_number("4294967295") == 4294967295

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "-",
            "+5",
            "5 ",
            "1e3",
            "0x10",
            "3000000+",
            "nobody",
            # Outside the 32 bits, on either side.
            "-2147483649",
            "4294967296",
            # Digits to the test that asks for digits, and none of the ten:
            # the conversion raises for the first and reads the second as 3.
            chr(0xB2),
            chr(0x663),
            pytest.param("1" * 21, id="21 digits"),
            # More digits than the conversion takes: it raises.
            pytest.param("9" * 5000, id="5000 digits"),
            pytest.param("-" + "9" * 5000, id="5000 digits below zero"),
        ],
    )
    def test_anything_else_is_no_number_and_raises_nothing(self, text):
        from turnstone.rollout.verification import _count, _number, _user_number

        assert _user_number(text) is None
        if text not in ("-2147483649", "4294967296"):
            assert _number(text) is None and _count(text) is None

    def test_a_count_is_never_below_zero(self):
        from turnstone.rollout.verification import _count

        assert _count("0") == 0 and _count("12") == 12 and _count("-1") is None


class _KeptSandbox:
    """Stands in for a sandbox: notes for how long it is told to be kept."""

    lost: str | None = None
    name = None

    def __init__(self) -> None:
        self.kept: list[float] = []

    def keep_for(self, seconds: float) -> None:
        self.kept.append(seconds)

    def after_run(self, command: AfterRunCommand) -> dict[str, Any]:
        return {"exit_code": 0}

    def stop(self) -> bool:
        return True


class TestTheContainersLifetimeCoversTheCheck:
    def test_the_time_is_set_for_each_step_of_it(self):
        """The after-run commands and the verifier come after the run, and can take
        longer than anything worked out when the run began."""
        from turnstone.rollout.episode import _AFTER_RUN_OVERHEAD, _verify

        task = Task(
            prompt="x",
            after_run=[AfterRunCommand(["a"], timeout=30), AfterRunCommand(["b"], timeout=300)],
        )
        sandbox = _KeptSandbox()
        out = _verify(task, sandbox, {}, lambda _c: {"verdict": "pass"}, 50)  # type: ignore[arg-type]
        assert out is not None and out["failure"] is None
        # Each command's own limit and what goes with it, then the verifier's.
        each = _AFTER_RUN_OVERHEAD
        assert sandbox.kept == [(30 + each) + (300 + each) + 600, 120 + 50 + 600]

    def test_what_goes_with_an_after_run_command_covers_every_part_of_it(self):
        """The time a command is given on top of its own limit is what its parts
        may take together.  Were it less, a container whose check takes as long
        as it may would be removed from outside under the check."""
        import turnstone.rollout.verification as verification
        from turnstone.rollout.episode import _AFTER_RUN_OVERHEAD

        parts = (
            12  # the ends of commands still under way (the kill's limit and a little)
            + 40  # a look of the size watch that is under way
            + verification._END_SECONDS  # ending what is left running
            + verification._REMOVE_SECONDS  # the helper, by its name
            + verification._RESUME_SECONDS  # letting the container run again
            + 5  # the last questions of that, each given a moment past its time
            + 30  # whether the container answers
            + verification._READ_SECONDS  # the files the command names
        )
        assert parts <= _AFTER_RUN_OVERHEAD

    def test_the_ends_of_commands_are_waited_for_before_an_after_run_command(self, monkeypatch):
        """The end of a command past its time is a command in the container.  The
        after-run command, which holds the container still first, waits for as
        long as such an end may take, not for a part of it."""
        from turnstone.rollout import episode

        order: list[Any] = []

        class Environment:
            kill_timeout = 10

            def settle(self, timeout: float) -> bool:
                order.append(("settle", timeout))
                return True

        def ran(container: Any, command: AfterRunCommand) -> dict[str, Any]:
            order.append(("after-run", command.argv))
            return {}

        monkeypatch.setattr(episode, "run_after", ran)
        sandbox = object.__new__(episode.ContainerSandbox)
        sandbox.container = object()  # type: ignore[assignment]
        sandbox.environment = Environment()  # type: ignore[assignment]
        assert sandbox.after_run(AfterRunCommand(["a"], timeout=5)) == {}
        assert [step for step, _ in order] == ["settle", "after-run"]
        assert order[0][1] >= Environment.kill_timeout

    def test_a_clean_container_is_kept_for_the_verifiers_time(self, monkeypatch):
        import turnstone.rollout.verification as verification

        context = _context()
        context.lifetime = 650
        seen: list[int] = []

        class Noted(Container):
            def start(self) -> Container:
                seen.append(self.max_lifetime)
                raise ContainerError("not started in this test")

        monkeypatch.setattr(verification, "Container", Noted)
        with pytest.raises(VerificationError, match="did not start"):
            context.clean_container(ContainerSpec(image=IMAGE))
        with pytest.raises(VerificationError):
            context.clean_container(ContainerSpec(image=IMAGE), max_lifetime=99)
        assert seen == [650, 99]


class TestASandboxEndedDuringTheCheck:
    """After the model's last turn, something it left running can still end its
    container, where its settings let it.  That is the same outcome as ending
    it during the run, not a failure of the check.  The model here has run a
    command first: one that never ran anything left nothing behind."""

    def _run(self, cause: str) -> dict[str, Any]:
        from tests.test_rollout_episode import SAMPLED, _NotingSandbox

        class Ended(_NotingSandbox):
            lost_cause: str | None = None

            def after_run(self, command: AfterRunCommand) -> dict[str, Any]:
                self.lost, self.lost_cause = "the container's main process ended", cause
                raise ContainerLostError(5, "the sandbox has stopped")

        script = [
            {"tool_calls": [{"name": "bash", "arguments": {"command": "echo started"}}]},
            {"content": "done"},
        ]
        with ScriptedChatServer(script) as server:
            config = RolloutConfig(base_url=server.base_url, model="scripted", **SAMPLED)
            task = Task(prompt="say done", after_run=[AfterRunCommand(["true"])])
            return run_episode(task, config, sandbox_factory=lambda _t, _c: Ended())

    def test_ended_by_the_model_it_is_the_models_outcome(self):
        record = self._run("model")
        assert record["finish_state"] == "sandbox_destroyed" and record["finish_cause"] == "model"
        assert record["trainable"] is True and record["problems"] == []
        check = record["verification"]
        assert check["failure"] is None and check["commands"] == []
        assert check["commands_not_run"] == "the container's main process ended"

    def test_ended_by_anything_else_it_is_the_machinerys_failure(self):
        record = self._run("machinery")
        assert record["finish_state"] == "completed" and record["trainable"] is False
        (problem,) = record["problems"]
        assert problem.startswith("the check after the run failed: an after-run command could not")


class TestAnEpisodeWithoutAContainer:
    """The record's side of it, with a sandbox that runs nothing."""

    def _run(self, verifier: Any) -> dict[str, Any]:
        from tests.test_rollout_episode import SAMPLED, _NotingSandbox

        with ScriptedChatServer([{"content": "done"}]) as server:
            config = RolloutConfig(base_url=server.base_url, model="scripted", **SAMPLED)
            return run_episode(
                Task(prompt="say done"),
                config,
                sandbox_factory=lambda _t, _c: _NotingSandbox(),
                verifier=verifier,
            )

    def test_the_verdict_is_stored_and_the_model_never_saw_it(self):
        seen: dict[str, Any] = {}

        def verifier(context: VerificationContext) -> dict[str, Any]:
            seen["final"] = context.record["final_content"]
            seen["name"] = context.container_name
            return {"verdict": "pass", "score": 1.0}

        record = self._run(verifier)
        assert seen == {"final": "done", "name": None}
        assert record["verification"]["result"] == {"verdict": "pass", "score": 1.0}
        assert record["trainable"] is True
        assert "score" not in str(record["messages"])

    def test_without_a_verdict_there_is_no_reward_to_train_on(self):
        record = self._run(lambda _context: {"notes": "looked fine"})
        assert record["verification"]["failure"] == "the verifier returned no verdict"
        assert record["trainable"] is False and record["finish_state"] == "completed"
        assert record["problems"] == [
            "the check after the run failed: the verifier returned no verdict"
        ]

    def test_no_check_asked_for_leaves_the_key_empty(self):
        assert self._run(None)["verification"] is None


SCRIPT = [
    {"tool_calls": [{"name": "write_file", "arguments": {"path": "answer.txt", "content": "41"}}]},
    {"content": "done"},
]
# The check a task holds and the model never sees.
TEST_SCRIPT = b"grep -qx 42 /testbed/answer.txt && echo PASSED || { echo FAILED; exit 1; }" + b"\n"
ROOT = ContainerSpec(
    image=IMAGE, user="0:0", read_only_root=False, writable={}, workdir="/testbed", memory="512m"
)


def _grade(context: VerificationContext) -> dict[str, Any]:
    """What a grader does with the helpers: take the model's files out of the
    stopped container, put them and the task's own test into a clean one, run it."""
    work = context.export(["/testbed"], max_bytes=1024 * 1024)
    if work.too_large:
        return {"verdict": "fail", "why": "the model wrote more than a check will read"}
    clean = context.clean_container(ROOT)
    clean.put("/", tree=work)
    clean.put("/testbed", files={"run_tests.sh": TEST_SCRIPT})
    ran = clean.run(["sh", "/testbed/run_tests.sh"], timeout=60)
    return {
        "verdict": "pass" if ran["exit_code"] == 0 else "fail",
        "output": ran["output"].strip(),
        "exported": work.summary(),
        "clean_container": clean.name,
    }


def _episode(script: list[dict[str, Any]], **options: Any) -> dict[str, Any]:
    with ScriptedChatServer(script) as server:
        task = Task(
            prompt="write the answer",
            container=ROOT,
            after_run=[AfterRunCommand(["sh", "/testbed/run_tests.sh"], timeout=30)],
            setup_files={"run_tests.sh": TEST_SCRIPT.decode()},
        )
        config = RolloutConfig(base_url=server.base_url, model="scripted", **SMALL)
        return run_episode(task, config, **options)


def _all_gone(record: dict[str, Any], made: list[str]) -> None:
    """Every container the harness made for this test is gone.  Only those are
    looked for: other rollouts may be running on the same machine."""
    assert record["sandbox"]["removed"] is True
    assert record["sandbox"]["name"] in made
    assert [name for name in made if _listed(name)] == []


@needs_container
class TestCheckingARootRollout:
    def test_an_honest_wrong_answer_fails_both_ways(self, containers_made):
        record = _episode(SCRIPT, verifier=_grade)
        check = record["verification"]
        assert check["commands"][0]["exit_code"] == 1 and "FAILED" in check["commands"][0]["output"]
        assert check["result"]["verdict"] == "fail" and check["result"]["output"] == "FAILED"
        assert check["failure"] is None and check["container_stopped"] is True
        # A check that ran and failed is the model's outcome.
        assert record["trainable"] is True and record["finish_cause"] == "model"
        _all_gone(record, containers_made)

    def test_a_right_answer_passes(self, containers_made):
        right = [
            {"tool_calls": [{"name": "bash", "arguments": {"command": "echo 42 > answer.txt"}}]},
            {"content": "done"},
        ]
        record = _episode(right, verifier=_grade)
        assert record["verification"]["result"]["verdict"] == "pass"
        assert record["verification"]["commands"][0]["exit_code"] == 0
        _all_gone(record, containers_made)

    def test_a_model_that_rewrites_the_test_and_the_shell_fools_only_its_own_container(
        self, containers_made
    ):
        """Root in a writable container can change anything a check there relies on:
        here the test script and the shell itself.  The command run in that container
        reports success.  The clean container, which the model never touched and
        which gets the task's own test, reports what is true."""
        hostile = [
            {
                "tool_calls": [
                    {
                        "name": "bash",
                        "arguments": {
                            "command": "echo 41 > answer.txt; echo 'echo PASSED' > run_tests.sh; "
                            "ln -sf /bin/true /bin/sh"
                        },
                    }
                ]
            },
            {"content": "all tests pass"},
        ]
        record = _episode(hostile, verifier=_grade)
        check = record["verification"]
        fooled = check["commands"][0]
        assert fooled["exit_code"] == 0 and "FAILED" not in fooled["output"]
        assert check["result"]["verdict"] == "fail" and check["result"]["output"] == "FAILED"
        assert record["trainable"] is True
        _all_gone(record, containers_made)

    def test_more_than_the_export_will_read_is_the_models_outcome(self, containers_made):
        big = [
            {
                "tool_calls": [
                    {
                        "name": "bash",
                        "arguments": {
                            "command": "head -c 3000000 /dev/urandom > big; echo 42 > answer.txt"
                        },
                    }
                ]
            },
            {"content": "done"},
        ]
        record = _episode(big, verifier=_grade)
        assert record["verification"]["result"] == {
            "verdict": "fail",
            "why": "the model wrote more than a check will read",
        }
        assert record["trainable"] is True and record["verification"]["failure"] is None
        _all_gone(record, containers_made)

    def test_a_verifier_that_fails_leaves_no_reward_and_no_container(self, containers_made):
        started: list[str] = []

        def breaks(context: VerificationContext) -> dict[str, Any]:
            started.append(context.clean_container(ROOT).name)
            raise RuntimeError("the grader has a bug")

        record = _episode(SCRIPT, verifier=breaks)
        assert record["verification"]["failure"] == (
            "the verifier raised RuntimeError: the grader has a bug"
        )
        assert record["trainable"] is False and record["one_sequence"] is False
        assert record["problems"] == [
            "the check after the run failed: the verifier raised RuntimeError: the grader has a bug"
        ]
        assert record["verification"]["helpers_removed"] is True
        assert _listed(started[0]) == []
        _all_gone(record, containers_made)

    @pytest.mark.allow_thread_leak
    def test_a_verifier_out_of_time_is_left_behind_and_its_container_removed(self, containers_made):
        started: list[str] = []

        def slow(context: VerificationContext) -> dict[str, Any]:
            started.append(context.clean_container(ROOT).name)
            time.sleep(20)
            return {"verdict": "pass"}

        record = _episode(SCRIPT, verifier=slow, verifier_timeout=4)
        assert record["verification"]["failure"] == "the verifier did not finish within 4 s"
        assert record["trainable"] is False
        assert _listed(started[0]) == []
        _all_gone(record, containers_made)

    def test_the_verifier_is_given_a_stopped_container_to_read(self, containers_made):
        seen: dict[str, Any] = {}

        def look(context: VerificationContext) -> dict[str, Any]:
            name = context.container_name
            seen["running"] = _runtime("inspect", "--format", "{{.State.Running}}", name).stdout
            seen["diff"] = _runtime("diff", name).stdout
            seen["stopped"] = context.container_stopped
            return {"verdict": "pass"}

        record = _episode(SCRIPT, verifier=look)
        assert seen["running"].strip() == "false" and seen["stopped"] is True
        assert "A /testbed/answer.txt" in seen["diff"]
        _all_gone(record, containers_made)


@needs_container
class TestWhatTheModelLeavesForTheCheck:
    """Files of a size no disk holds, a link where the task's files go, a file
    where a directory was expected: each is something a model can leave, so
    each is an outcome for the check to judge and none is a failure of it."""

    def test_a_file_of_enormous_apparent_size_is_too_large_at_once(self, containers_made):
        """The default container: the task reads its result file out after the run.
        The read stops when more than it will take has arrived; it used to wait for
        the whole file, past its own time limit."""
        script = [
            {"tool_calls": [{"name": "bash", "arguments": {"command": "truncate -s 50G out.txt"}}]},
            {"content": "done"},
        ]
        check = AfterRunCommand(
            ["sh", "-c", "test -e /workspace/out.txt"],
            collect=["/workspace/out.txt"],
            max_collect_bytes=1024 * 1024,
        )
        with ScriptedChatServer(script) as server:
            task = Task(
                prompt="write out.txt",
                container=ContainerSpec(image=IMAGE, memory="512m"),
                after_run=[check],
            )
            config = RolloutConfig(base_url=server.base_url, model="scripted", **SMALL)
            record = run_episode(task, config)
        (ran,) = record["verification"]["commands"]
        assert ran["exit_code"] == 0 and ran["collected"] == {}
        assert ran["collected_summary"]["too_large"] is True
        assert record["verification"]["failure"] is None and record["trainable"] is True
        assert record["verification"]["seconds"] < 60
        _all_gone(record, containers_made)

    def test_an_export_of_such_a_file_is_too_large_at_once(self, containers_made):
        script = [
            {
                "tool_calls": [
                    {"name": "bash", "arguments": {"command": "truncate -s 50G /testbed/huge"}}
                ]
            },
            {"content": "done"},
        ]
        seen: dict[str, Any] = {}

        def look(context: VerificationContext) -> dict[str, Any]:
            started = time.monotonic()
            huge = context.export(["/testbed/huge"], max_bytes=1024 * 1024)
            seen["seconds"] = time.monotonic() - started
            return {"verdict": "fail" if huge.too_large else "pass", "export": huge.summary()}

        with ScriptedChatServer(script) as server:
            config = RolloutConfig(base_url=server.base_url, model="scripted", **SMALL)
            record = run_episode(
                Task(prompt="work", container=ROOT), config, verifier=look, verifier_timeout=120
            )
        assert record["verification"]["result"]["export"]["too_large"] is True
        assert record["verification"]["failure"] is None and record["trainable"] is True
        assert seen["seconds"] < 60
        _all_gone(record, containers_made)

    @pytest.mark.parametrize(
        "in_the_way",
        ["ln -s /etc tests", "ln -s /nonexistent tests", "echo not-a-directory > tests"],
    )
    def test_the_tasks_files_win_over_what_stands_in_their_way(self, containers_made, in_the_way):
        """The flow a verifier is told to use: the model's files and the task's own
        test into a clean container.  The model left something where the test's
        directory goes.  A link there is removed, never followed."""
        script = [
            {"tool_calls": [{"name": "bash", "arguments": {"command": in_the_way}}]},
            {"content": "done"},
        ]
        hidden = {"testbed/tests/test_hidden.py": b"assert True" + NL.encode()}
        placed = "test -f /testbed/tests/test_hidden.py && test ! -L /testbed/tests "
        placed += "&& test ! -e /etc/test_hidden.py && echo placed"

        def grade(context: VerificationContext) -> dict[str, Any]:
            work = context.export(["/testbed"], max_bytes=1024 * 1024)
            clean = context.clean_container(ROOT)
            clean.put("/", tree=work, files=hidden)
            together = clean.run(["sh", "-c", placed])["output"].strip()
            # And in two steps, the model's tree first and the task's files after.
            again = context.clean_container(ROOT)
            again.put("/", tree=work)
            again.put("/testbed", files={"tests/test_hidden.py": hidden[next(iter(hidden))]})
            apart = again.run(["sh", "-c", placed])["output"].strip()
            return {"verdict": "pass", "together": together, "apart": apart}

        with ScriptedChatServer(script) as server:
            config = RolloutConfig(base_url=server.base_url, model="scripted", **SMALL)
            record = run_episode(Task(prompt="work", container=ROOT), config, verifier=grade)
        check = record["verification"]
        assert check["failure"] is None, check["failure"]
        assert check["result"] == {"verdict": "pass", "together": "placed", "apart": "placed"}
        assert record["trainable"] is True
        _all_gone(record, containers_made)

    def test_a_path_that_cannot_be_read_is_skipped_not_raised(self, containers_made):
        """A file where the task expected a directory: nothing can stand below it."""
        script = [
            {"tool_calls": [{"name": "bash", "arguments": {"command": "echo 41 > results"}}]},
            {"content": "done"},
        ]

        def look(context: VerificationContext) -> dict[str, Any]:
            read = context.export(["/testbed/results/summary.json", "/testbed/results"])
            return {
                "verdict": "fail",
                "skipped": sorted(read.skipped),
                "files": sorted(read.files()),
            }

        with ScriptedChatServer(script) as server:
            config = RolloutConfig(base_url=server.base_url, model="scripted", **SMALL)
            record = run_episode(Task(prompt="work", container=ROOT), config, verifier=look)
        check = record["verification"]
        assert check["failure"] is None, check["failure"]
        assert check["result"]["skipped"] == ["/testbed/results/summary.json"]
        assert check["result"]["files"] == ["/testbed/results"]
        assert record["trainable"] is True
        _all_gone(record, containers_made)

    def test_a_container_that_is_gone_is_still_a_failure_of_the_export(self):
        with pytest.raises(VerificationError, match="out of the container failed"):
            export_paths("tsgym-no-such-container", ["/testbed/answer.txt"])

    def test_the_container_outlives_a_check_longer_than_the_runs_own_margin(
        self, containers_made, monkeypatch
    ):
        """The keeper outside is given the check's time when the check starts.  With
        the time worked out at the start of the run, a long check found its
        container removed under it."""
        from turnstone.rollout import episode

        monkeypatch.setattr(episode, "_KEEP_MARGIN", 3)

        def slow(context: VerificationContext) -> dict[str, Any]:
            time.sleep(9)  # longer than the run's limit and its margin together
            answer = context.export(["/testbed/answer.txt"]).files()
            return {"verdict": "pass" if answer else "fail"}

        with ScriptedChatServer(SCRIPT) as server:
            config = RolloutConfig(base_url=server.base_url, model="scripted", **SMALL)
            task = Task(prompt="write the answer", container=ROOT, timeout=2)
            record = run_episode(task, config, verifier=slow, verifier_timeout=60)
        assert record["verification"]["failure"] is None
        assert record["verification"]["result"] == {"verdict": "pass"}
        _all_gone(record, containers_made)


DETACHED = "setsid nohup sleep {seconds} >/dev/null 2>&1 & sleep 0.3"


def _processes(name: str) -> list[str]:
    """The names of the container's processes as the runtime lists them from
    outside: nothing in the container is asked."""
    listed = _runtime("top", name, "-eo", "pid,comm").stdout.splitlines()[1:]
    return [line.split(None, 1)[1].strip() for line in listed if line.strip()]


@needs_container
class TestWhatIsLeftRunningIsEnded:
    """In real containers: whoever a process runs as, and whatever it does to
    stay, the runtime lists nothing but the main process afterwards."""

    def _left(self, container: Container, user: str, seconds: int) -> None:
        done = container.exec(["sh", "-c", DETACHED.format(seconds=seconds)], timeout=30, user=user)
        assert done.returncode == 0, done.stderr_text

    def test_detached_processes_of_every_user(self):
        """The commands' own user, root, and a user nothing names.  Root in the
        container may not signal the others, which is why its own kill left
        them running."""
        with Container(ContainerSpec(image=IMAGE, memory="512m")) as container:
            self._left(container, "65534:65534", 600)
            self._left(container, "0:0", 601)
            self._left(container, "12345:54321", 602)
            assert sorted(_processes(container.name)) == ["sleep"] * 4
            assert end_what_is_left(container) == {
                "found": 3,
                "still_running": [],
                "frozen": True,
            }
            assert _processes(container.name) == ["sleep"]  # the main process
            # It runs again, nobody asked the runtime to end it, and it works.
            state = _runtime(
                "inspect", "--format", "{{.State.Paused}} {{.State.Running}}", container.name
            )
            assert state.stdout.split() == ["false", "true"]
            assert container._watch._last_asked_at is None and container.lost is None
            assert container.exec(["true"], timeout=30).returncode == 0
            # And with nothing left there is nothing to do.
            assert end_what_is_left(container)["found"] == 0
        assert _listed(container.name) == []
        assert _listed("tsgym-end-" + container.name.removeprefix("tsgym-")) == []

    def test_one_that_ends_every_new_process_of_its_user(self):
        """A kill sent in as that same user would be one of the processes this
        ends first.  Held still, it ends nothing."""
        with Container(ContainerSpec(image=IMAGE, memory="512m")) as container:
            self._left(container, "65534:65534", 600)
            loop = "setsid nohup sh -c 'while :; do kill -9 -1; done' >/dev/null 2>&1 & sleep 0.3"
            container.exec(["sh", "-c", loop], timeout=30)
            assert "sh" in _processes(container.name)
            assert end_what_is_left(container)["still_running"] == []
            assert _processes(container.name) == ["sleep"]
        assert _listed(container.name) == []

    def test_with_every_process_the_container_may_have_held(self):
        """Nothing can be started in the container then, a kill included; the
        helper is no process of the container's."""
        spec = ContainerSpec(image=IMAGE, memory="512m", pids_limit=64)
        with Container(spec) as container:
            fill = (
                "i=0; while [ $i -lt 80 ]; do "
                "(setsid nohup sleep 900 >/dev/null 2>&1 &) 2>/dev/null; i=$((i+1)); done; sleep 0.5"
            )
            container.exec(["sh", "-c", fill], timeout=60)
            assert len(_processes(container.name)) > 50
            refused = run_captured(container.exec_argv(["true"]), timeout=30, limit=4000)
            assert refused.returncode != 0  # the container takes no new process
            result = end_what_is_left(container)
            assert result["found"] > 50 and result["still_running"] == []
            assert _processes(container.name) == ["sleep"]
            assert container.exec(["true"], timeout=30).returncode == 0
        assert _listed(container.name) == []

    # A program whose first thread ends by itself, with the system's plain
    # exit call, while a second thread sleeps on: the process runs, and its
    # first thread is what a process list calls ended.
    FIRST_THREAD_ENDS = NL.join(
        [
            "import ctypes, platform, threading, time",
            "threading.Thread(target=time.sleep, args=(600,)).start()",
            "time.sleep(0.2)",
            "number = {'x86_64': 60, 'aarch64': 93}[platform.machine()]",
            "ctypes.CDLL(None).syscall(number, 0)",
        ]
    )
    # A program that sleeps and never collects a child of its own that ended.
    CHILD_NOT_COLLECTED = NL.join(
        ["import os, time", "if os.fork() == 0:", "    os._exit(0)", "time.sleep(600)"]
    )

    def test_a_process_whose_first_thread_has_ended_while_another_runs(self):
        """The runtime lists it with the state of a process that is over, so it
        was left out, never ended, and ran on beside every after-run command:
        in the default container, with nothing but the image's interpreter.
        Its count of threads tells it from one that is over, which is neither
        counted nor in the way."""
        leave = 'setsid python3 -c "$1" >/dev/null 2>&1 & sleep 0.3'
        with Container(ContainerSpec(image=IMAGE, memory="512m")) as container:
            for program in (self.FIRST_THREAD_ENDS, self.CHILD_NOT_COLLECTED):
                done = container.exec(["sh", "-c", leave, "sh", program], timeout=30)
                assert done.returncode == 0, done.stderr_text
            deadline = time.monotonic() + 30
            while True:
                listed = _runtime("top", container.name, "-eo", "pid,nlwp,stat,comm").stdout
                rows = [line.split() for line in listed.splitlines()[1:] if line.strip()]
                ended_first = [row for row in rows if row[2].startswith("Z") and row[1] != "1"]
                if ended_first and len(rows) >= 3:
                    break
                assert time.monotonic() < deadline, listed
                time.sleep(0.2)
            assert [row[1] for row in ended_first] == ["2"]
            # That process and the sleeping parent; not the child that is over.
            assert end_what_is_left(container) == {
                "found": 2,
                "still_running": [],
                "frozen": True,
            }
            assert _processes(container.name) == ["sleep"]
            assert container.exec(["true"], timeout=30).returncode == 0
        assert _listed(container.name) == []

    def test_the_size_watchs_own_look_is_never_among_what_was_left_running(self):
        """A writable root is watched with a look from inside, as the
        commands' user: a process of the container.  One under way when the
        container was held still was listed, counted in what was found and
        ended.  Here the watch looks without a pause between two looks, and
        nothing is ever found in a container that nothing was left in."""
        container = Container(ROOT)
        container.size_check_seconds = 0.01
        with container:
            if container.root_bound != "watched":
                pytest.skip("this machine's storage driver enforces the size itself")
            nothing = {"found": 0, "still_running": [], "frozen": True}
            assert [end_what_is_left(container) for _ in range(15)] == [nothing] * 15
            assert container._size_watch is not None and container._size_watch.is_alive()
            assert container.lost is None and container._unlinked_bytes() == 0
        assert _listed(container.name) == []

    def test_letting_a_container_run_whether_or_not_it_is_held(self):
        """What the runtime itself says: it refuses to let a container run
        that it does not count as held, which is no failure; and one held
        again and let run, as after a hold that was never confirmed, runs."""
        from turnstone.rollout.verification import _held_still, _let_run_again

        with Container(ContainerSpec(image=IMAGE, memory="512m")) as container:
            assert _held_still(container, 30) is False
            refused = _runtime("unpause", container.name)
            assert refused.returncode != 0 and "is not paused" in refused.stderr
            assert _let_run_again(container, confirmed=True) is None
            assert _let_run_again(container, confirmed=False) is None
            assert _held_still(container, 30) is False
            assert container.exec(["true"], timeout=30).returncode == 0
            # Held by a request nobody waited for, as one confirmed too late.
            assert _runtime("pause", container.name).returncode == 0
            assert _held_still(container, 30) is True
            assert _let_run_again(container, confirmed=False) is None
            assert _held_still(container, 30) is False
            assert container.exec(["true"], timeout=30).returncode == 0
            assert container._watch._last_asked_at is None and container.lost is None
        assert _listed(container.name) == []

    def test_under_user_and_group_numbers_the_runtime_takes_for_no_container(self):
        """Root that may change its user and group, over a read-only root.  A
        group above 2147483647 costs nothing: the helper is that user under
        another group.  A user above it has no helper, since the runtime
        starts a container as no such user; that process stays, and is the
        model's own outcome here, where a command can make one."""
        spec = ContainerSpec(
            image=IMAGE, user="0:0", capabilities=("SETUID", "SETGID"), memory="512m"
        )
        users = ["2001:60000", "2001:60001", "60001:3000000000", "3000000000:60000"]
        with Container(spec) as container:
            done = container.exec(
                ["python3", "-c", UNDER_USERS, *users, "4294967294:7"], timeout=30
            )
            assert done.returncode == 0, done.stderr_text
            assert sorted(_processes(container.name)) == ["sleep"] * 6
            result = end_what_is_left(container)
            assert result["found"] == 5 and result["frozen"] is True
            assert sorted(result["still_running"], key=lambda one: one["uid"]) == [
                {"uid": 3000000000, "command": "sleep"},
                {"uid": 4294967294, "command": "sleep"},
            ]
            assert sorted(_processes(container.name)) == ["sleep"] * 3
            # It runs again and works; the two are found again the next time.
            assert container.exec(["true"], timeout=30).returncode == 0
            assert end_what_is_left(container)["found"] == 2
        assert _listed(container.name) == []
        assert _listed("tsgym-end-" + container.name.removeprefix("tsgym-")) == []


# Leaves one sleeping process under each user and group given as ``uid:gid``.
UNDER_USERS = NL.join(
    [
        "import os, sys",
        "for word in sys.argv[1:]:",
        "    uid, gid = (int(part) for part in word.split(':'))",
        "    if os.fork() == 0:",
        "        os.setsid()",
        "        null = os.open('/dev/null', os.O_RDWR)",
        "        for fd in (0, 1, 2):",
        "            os.dup2(null, fd)",
        "        os.setgid(gid)",
        "        os.setuid(uid)",
        "        os.execvp('sleep', ['sleep', '600'])",
    ]
)


@needs_container
class TestAWorkingDirectoryItsUserCanTakeAway:
    """Over a writable root, a user who owns the directory above its working
    directory can rename or remove the working directory, and the runtime
    then starts nothing there.  The default container's is a mount point of
    its own that its user can neither rename nor remove."""

    SPEC = ContainerSpec(
        image=IMAGE,
        read_only_root=False,
        writable={},
        workdir="/home/u/work",
        capabilities=("CHOWN",),
        memory="512m",
    )

    def _with_files(self, container: Container) -> None:
        """The directories are the user's, as a task's image or its before-run
        command would have them, with files of the user's in them."""
        owned = container.exec(["chown", "-R", "65534:65534", "/home/u"], timeout=30, user="0:0")
        assert owned.returncode == 0, owned.stderr_text
        files = "echo out > out.txt; mkdir sub; echo a > sub/a.txt"
        assert container.exec(["sh", "-c", files], timeout=30).returncode == 0

    def test_what_is_read_keeps_the_names_the_task_gave(self):
        with Container(self.SPEC) as container:
            self._with_files(container)
            paths = ["out.txt", "/etc/hostname", "sub"]
            result = run_after(container, AfterRunCommand(["cat", "out.txt"], collect=paths))
            assert result["exit_code"] == 0 and result["output"] == "out" + NL
            # A relative path by the name it was given, an absolute one by its
            # place below the root: as when the read started in that directory.
            assert set(result["collected"]) == {"out.txt", "sub/a.txt", "etc/hostname"}
            assert result["collected"]["sub/a.txt"] == "a" + NL
            assert result["collected_summary"]["entries"] == {"file": 3, "dir": 1}
        assert _listed(container.name) == []

    def test_renamed_by_its_user_the_read_still_starts(self):
        """The task's own command starts in the working directory and cannot:
        that is its exit status, the model's outcome.  The read started there
        as well and failed as the machinery's, which let a model take its
        rollout out of training by renaming a directory.  It starts, reads
        what is there and passes over what is not."""
        with Container(self.SPEC) as container:
            self._with_files(container)
            renamed = container.exec(
                ["sh", "-c", "cd / && mv /home/u/work /home/u/gone"], timeout=30
            )
            assert renamed.returncode == 0, renamed.stderr_text
            paths = ["out.txt", "/home/u/gone/out.txt", "/etc/hostname"]
            result = run_after(container, AfterRunCommand(["cat", "out.txt"], collect=paths))
            assert result["exit_code"] not in (0, None) and result["timed_out"] is False
            assert set(result["collected"]) == {"home/u/gone/out.txt", "etc/hostname"}
            assert result["collected"]["home/u/gone/out.txt"] == "out" + NL
            assert "failed" not in result["collected_summary"]
            assert container.lost is None
            # So does the size watch's look from inside, and the end of a
            # command that ran out of its time: both are the harness's own.
            assert container.exec(["true"], timeout=30, workdir="/").returncode == 0
            assert container.exec(["true"], timeout=30).returncode != 0
        assert _listed(container.name) == []


@needs_container
class TestAfterRunInTheContainedCase:
    def test_a_command_and_the_files_it_names(self):
        """An unprivileged model over a read-only root cannot change the programs a
        command there uses.  Its working directory is memory-backed, so it is read
        while the container still runs."""
        script = [
            {
                "tool_calls": [
                    {"name": "bash", "arguments": {"command": "echo 42 > answer.txt; sleep 300 &"}}
                ]
            },
            {"content": "done"},
        ]
        with ScriptedChatServer(script) as server:
            task = Task(
                prompt="write the answer",
                container=ContainerSpec(image=IMAGE, memory="512m"),
                after_run=[
                    AfterRunCommand(
                        [
                            "sh",
                            "-c",
                            "id -u; cat /proc/[0-9]*/comm | grep -c sleep; cat answer.txt",
                        ],
                        collect=["/workspace/answer.txt"],
                    )
                ],
            )
            config = RolloutConfig(base_url=server.base_url, model="scripted", **SMALL)
            record = run_episode(task, config)
        (ran,) = record["verification"]["commands"]
        # As root, with what the model left running ended (the one sleep is the
        # container's own), and the file read out.
        assert ran["output"].split() == ["0", "1", "42"] and ran["exit_code"] == 0
        assert ran["collected"] == {"workspace/answer.txt": "42" + NL}
        assert record["verification"]["result"] is None and record["trainable"] is True
        assert _listed(record["sandbox"]["name"]) == []
