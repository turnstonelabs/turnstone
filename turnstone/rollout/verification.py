"""Checking a rollout after the model has finished.

A reward needs a check the model could not have prepared for.  Three things
are here, from the simplest to the general:

- :class:`AfterRunCommand`: run one command in the episode's own container,
  after everything the model left running was ended and before the container is
  stopped.  Only as good as the container: where the model could change the
  programs the command uses (commands run as root, a writable root), it can
  make the command say anything.  Fine for a container whose root is read-only
  and whose commands ran as an unprivileged user.
- a *verifier*: a function the caller supplies.  The harness stops the episode
  container first, so that nothing the model started can change a file any
  more, then calls the function with a :class:`VerificationContext` naming the
  stopped container.  What the function returns is stored in the record.
- helpers such a function can use: :func:`export_paths` reads paths out of a
  stopped container with the runtime's own copy, executing nothing inside it,
  into memory; :meth:`VerificationContext.clean_container` starts a container
  the model never touched, with the same isolation, into which files are put
  and in which commands run.

The model never sees any of it.  Every container started through the context
is removed by the harness, with the removal confirmed, when the function has
returned, raised or run out of time.
"""

from __future__ import annotations

import contextlib
import io
import posixpath
import tarfile
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from turnstone.core.log import get_logger
from turnstone.rollout.container import (
    NAME_PREFIX,
    OWN_WORKDIR,
    Completed,
    Container,
    ContainerSpec,
    run_captured,
)

if TYPE_CHECKING:
    from collections.abc import Callable

log = get_logger(__name__)


@dataclass
class AfterRunCommand:
    """One command run in the episode's container once the model has finished."""

    argv: list[str]
    # Root by default: not the user an unprivileged model's commands ran as, so
    # nothing of theirs is in its way and nothing of its own was theirs to touch.
    user: str = "0:0"
    timeout: int = 300
    max_output_chars: int = 16000
    # Paths read out afterwards (through the container's own archive program).
    collect: list[str] = field(default_factory=list)
    max_collect_bytes: int = 8 * 1024 * 1024


@dataclass
class Entry:
    """One thing found at an exported path.  A link is kept as its target's
    name and anything that is neither a file, a directory nor a link by its
    kind only; nothing is followed and nothing is made on this machine."""

    path: str
    kind: str  # "file", "dir", "symlink", "hardlink" or "other"
    mode: int = 0o644
    data: bytes | None = None
    target: str | None = None


@dataclass
class Exported:
    """Paths read out of a container, held in memory."""

    entries: list[Entry] = field(default_factory=list)
    # More was there than the bound allows.  Whoever wrote that much was the
    # model: an outcome for the check to judge, not a failure of the export.
    too_large: bool = False
    size: int = 0
    # Paths that could not be read although the container was there, each with
    # the reason: nothing at the path, or something above it that is no
    # directory.  What stands at a path is the model's to decide.
    skipped: dict[str, str] = field(default_factory=dict)
    # Why nothing could be read, where the programs that read are the model's
    # to change (commands as root over a writable root).  An outcome too.
    failed: str | None = None

    def files(self) -> dict[str, bytes]:
        return {e.path: e.data for e in self.entries if e.kind == "file" and e.data is not None}

    def summary(self) -> dict[str, Any]:
        kinds: dict[str, int] = {}
        for entry in self.entries:
            kinds[entry.kind] = kinds.get(entry.kind, 0) + 1
        said: dict[str, Any] = {
            "entries": kinds,
            "bytes": self.size,
            "too_large": self.too_large,
        }
        if self.skipped:
            said["skipped"] = dict(self.skipped)
        if self.failed is not None:
            said["failed"] = self.failed
        return said


class VerificationError(RuntimeError):
    """The machinery of a check failed: no verdict exists."""


def read_archive(data: bytes, *, max_bytes: int, strip: str = "") -> Exported:
    """The members of a tar archive, read in memory.  Nothing is written anywhere.

    Raises :class:`VerificationError` for data that cannot be read as one,
    whatever the reading raised: the bytes are whatever the program that made
    them wrote, and where that program is the model's, so are they.
    """
    out = Exported()
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
            for member in archive:
                path = member.name
                if strip and (path == strip or path.startswith(strip + "/")):
                    path = path[len(strip) :].lstrip("/") or "."
                if member.isdir():
                    out.entries.append(Entry(path, "dir", member.mode))
                elif member.issym():
                    out.entries.append(Entry(path, "symlink", member.mode, target=member.linkname))
                elif member.islnk():
                    out.entries.append(Entry(path, "hardlink", member.mode, target=member.linkname))
                elif member.isfile():
                    if out.size + member.size > max_bytes:
                        out.too_large = True
                        break
                    handle = archive.extractfile(member)
                    content = handle.read() if handle is not None else b""
                    out.size += len(content)
                    out.entries.append(Entry(path, "file", member.mode, data=content))
                else:
                    out.entries.append(Entry(path, "other", member.mode))
    except Exception as error:
        # Not only what the archive reader calls its own errors: a number
        # field that holds no number, a size past what memory has, a header
        # that sends the reader in a circle.
        raise VerificationError(
            f"the archive could not be read: {type(error).__name__}: {error}"
        ) from error
    return out


def export_paths(
    container_name: str,
    paths: list[str],
    *,
    max_bytes: int = 64 * 1024 * 1024,
    runtime: str = "docker",
    timeout: float = 300.0,
) -> Exported:
    """Read *paths* out of a container with the runtime's own copy.

    Nothing runs inside the container, so nothing in it can change what is
    read; it works on a stopped container.  The paths come back in memory, at
    most *max_bytes* of file content; more than that sets ``too_large``, as
    soon as it has arrived and without waiting for the rest.

    The runtime's copy reads the container's own file system.  A directory that
    was memory-backed while the container ran is not part of it and is empty
    once the container has stopped; read such a path with an
    :class:`AfterRunCommand` before that.
    """
    out = Exported()
    for path in paths:
        # The archive's own framing comes on top of the content.
        most = max_bytes - out.size + 1024 * 1024
        done = run_captured(
            [runtime, "cp", f"{container_name}:{path}", "-"],
            timeout=timeout,
            limit=most,
            # More than will be read is not waited for: a file of a size no
            # disk holds takes for ever to copy, and is the model's to make.
            stop_after=most,
        )
        if done.stopped_early:
            out.too_large = True
            return out
        if done.timed_out:
            raise VerificationError(f"copying {path} out of the container timed out")
        if done.returncode != 0:
            text = done.stderr_text
            # The container is there and this path cannot be read: nothing
            # stands there, or something above it is no directory.  That is
            # the model's doing, not a failure of the copy.
            if _known(runtime, container_name):
                out.skipped[path] = text[:200]
                continue
            raise VerificationError(f"copying {path} out of the container failed: {text[:300]}")
        if done.dropped:
            out.too_large = True
            return out
        parent = posixpath.dirname(path.rstrip("/"))
        part = read_archive(done.stdout, max_bytes=max_bytes - out.size)
        for entry in part.entries:
            entry.path = posixpath.join(parent, entry.path)
        out.entries += part.entries
        out.size += part.size
        if part.too_large:
            out.too_large = True
            return out
    return out


def _known(runtime: str, container_name: str) -> bool:
    """Whether the runtime answers for *container_name*.  ``False`` also when it
    does not answer at all."""
    try:
        done = run_captured(
            [runtime, "container", "inspect", "--format", "{{.Id}}", container_name],
            timeout=30,
            limit=8192,
        )
    except OSError:
        return False
    return done.returncode == 0 and not done.timed_out


def build_archive(
    files: dict[str, bytes] | None = None,
    tree: Exported | None = None,
    *,
    links: bool = True,
) -> bytes:
    """A tar archive of *files* (path to content) and of *tree*'s files,
    directories and, with *links*, symbolic links.  Paths are made relative.

    An entry of *tree* that lies below something that is not a directory (a
    link, a file) is left out: unpacked, it would land wherever that link
    points.  A tree read out of a file system holds no such entry.
    """
    buffer = io.BytesIO()
    not_directories: set[str] = set()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for entry in tree.entries if tree is not None else []:
            name = posixpath.normpath(entry.path.lstrip("/"))
            if any(above in not_directories for above in _ancestors(name)):
                continue
            if entry.kind != "dir":
                not_directories.add(name)
            info = tarfile.TarInfo(entry.path.lstrip("/"))
            info.mode = entry.mode
            if entry.kind == "dir":
                info.type = tarfile.DIRTYPE
                archive.addfile(info)
            elif entry.kind == "file" and entry.data is not None:
                info.size = len(entry.data)
                archive.addfile(info, io.BytesIO(entry.data))
            elif entry.kind == "symlink" and links and entry.target is not None:
                info.type = tarfile.SYMTYPE
                info.linkname = entry.target
                archive.addfile(info)
        for path, content in (files or {}).items():
            info = tarfile.TarInfo(path.lstrip("/"))
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def _ancestors(path: str) -> list[str]:
    """The directories above a relative *path*, the outermost first."""
    parts = path.split("/")[:-1]
    return ["/".join(parts[: n + 1]) for n in range(len(parts))]


# $1 is the directory the task's files go under.  The other arguments come in
# pairs: ``d`` and a directory that has to be a real directory, ``f`` and a
# path that has to be free for a file.  Whatever else stands there was put in
# with the model's tree and makes way: a link is removed, never followed.
_MAKE_WAY = """
cd -- "$1" || exit 1
shift
while [ "$#" -gt 1 ]; do
  kind=$1
  path=$2
  shift 2
  if [ "$kind" = d ]; then
    if [ -L "$path" ] || { [ -e "$path" ] && [ ! -d "$path" ]; }; then
      rm -rf -- "$path" || exit 1
    fi
    [ -d "$path" ] || mkdir -- "$path" || exit 1
  elif [ -L "$path" ] || [ -d "$path" ]; then
    rm -rf -- "$path" || exit 1
  fi
done
"""


def _way_for(files: dict[str, bytes]) -> list[str]:
    """The arguments of ``_MAKE_WAY`` for the task's *files*."""
    arguments: list[str] = []
    made: set[str] = set()
    for path in files:
        name = posixpath.normpath(path.lstrip("/"))
        if name in (".", "..") or name.startswith("../"):
            raise ValueError(f"a file's path must stay below where it is put: {path!r}")
        for directory in _ancestors(name):
            if directory not in made:
                made.add(directory)
                arguments += ["d", directory]
        arguments += ["f", name]
    return arguments


class CleanContainer:
    """A container the model never touched, for a check to run in."""

    def __init__(self, container: Container) -> None:
        self.container = container
        self.name = container.name

    def put(
        self,
        dest: str,
        *,
        files: dict[str, bytes] | None = None,
        tree: Exported | None = None,
        timeout: float = 300.0,
    ) -> None:
        """Unpack *tree*, then *files*, under *dest*, as root.

        *tree* is what was read out of the model's container; *files* are the
        task's own, which the model never saw.  The task's files win: where the
        tree left a link, a file or a directory in the way of one of them or of
        a directory above it, that makes way.  A link is removed and never
        followed, so nothing of the task's lands where a link of the model's
        points.
        """
        unpack = [
            "sh",
            "-c",
            'mkdir -p -- "$1" && exec tar -x -C "$1" -f -',
            "sh",
            dest,
        ]
        steps: list[tuple[list[str], bytes | None]] = []
        if tree is not None:
            steps.append((unpack, build_archive(None, tree)))
        if files:
            way = _way_for(files)
            steps.append((["sh", "-c", 'mkdir -p -- "$1"', "sh", dest], None))
            # In portions, so that no command line grows past what the system takes.
            for start in range(0, len(way), 400):
                portion = way[start : start + 400]
                steps.append((["sh", "-c", _MAKE_WAY, "sh", dest, *portion], None))
            steps.append((unpack, build_archive(files)))
        for argv, archive in steps:
            done = self.container.exec(argv, stdin_data=archive, timeout=timeout, user="0:0")
            if done.returncode != 0:
                raise VerificationError(
                    f"could not put files under {dest}: {done.stderr_text[:300]}"
                )

    def run(
        self,
        argv: list[str],
        *,
        user: str | None = None,
        timeout: float = 300.0,
        max_output_chars: int = 16000,
    ) -> dict[str, Any]:
        """Run *argv* to its end; its exit status, output and time."""
        return _run_in(self.container, argv, user, timeout, max_output_chars)

    def read(self, paths: list[str], *, max_bytes: int = 8 * 1024 * 1024) -> Exported:
        """Read *paths* out of this container, through its own archive program."""
        return _collect(self.container, paths, max_bytes)


def _run_in(
    container: Container,
    argv: list[str],
    user: str | None,
    timeout: float,
    max_chars: int,
) -> dict[str, Any]:
    started = time.monotonic()
    done = container.exec(argv, timeout=timeout, limit=max_chars, user=user)
    output = (done.stdout + done.stderr).decode("utf-8", errors="replace")
    return {
        "argv": argv,
        "exit_code": done.returncode,
        "timed_out": done.timed_out,
        "output": output[:max_chars],
        "output_dropped": done.dropped,
        "seconds": round(time.monotonic() - started, 2),
    }


class _CutShortError(VerificationError):
    """A read that did not get to its end: it ran out of its time, or its
    archive program was ended before it had finished."""


# The seconds reading out the files an after-run command names may take.
_READ_SECONDS = 120.0


def _collect(container: Container, paths: list[str], max_bytes: int) -> Exported:
    """Read *paths* out of *container* with its own archive program, as root.

    More than will be read is an :class:`Exported` that says ``too_large``.
    Every way the read can fail raises :class:`VerificationError`: its time
    limit, an archive program that was ended or says it failed, and output
    that is no archive.  A path that is not there or cannot be opened is none
    of them: the program passes it over and ends with 0, and writes an archive
    in any case, an empty one at the least.

    The program is started in ``OWN_WORKDIR``, not in the task's working
    directory: where that directory was renamed or removed, which is the
    model's to do where it owns the directory above, the runtime starts
    nothing in it, and the read would fail as the machinery's.  A relative
    path is still read below the working directory, in a call of its own, and
    comes back under the name the task gave it.
    """
    below = container.workdir.rstrip("/")
    as_given = [path for path in paths if path.startswith("/") or not path]
    resolved = [f"{below}/{path}" for path in paths if path and not path.startswith("/")]
    out = Exported()
    deadline = time.monotonic() + _READ_SECONDS
    for group, strip in ((as_given, ""), (resolved, below.lstrip("/"))):
        if not group:
            continue
        part = _archived(
            container, group, max_bytes - out.size, strip, max(deadline - time.monotonic(), 1.0)
        )
        out.entries += part.entries
        out.size += part.size
        if part.too_large:
            out.too_large = True
            break
    return out


def _archived(
    container: Container, paths: list[str], max_bytes: int, strip: str, timeout: float
) -> Exported:
    """One call of the container's archive program for *paths*, read in
    memory, the names of its members without *strip* in front."""
    most = max_bytes + 1024 * 1024
    done = container.exec(
        ["tar", "-c", "-f", "-", "--ignore-failed-read", "--", *paths],
        timeout=timeout,
        limit=most,
        user="0:0",
        # More than will be read is not waited for (see ``export_paths``).
        stop_after=most,
        workdir=OWN_WORKDIR,
    )
    if done.stopped_early or done.dropped:
        return Exported(too_large=True)
    if done.timed_out:
        raise _CutShortError("reading files out of the container timed out")
    code = done.returncode
    if code is None or code >= 128:
        raise _CutShortError(
            "reading files out of the container was cut short: its archive program was "
            f"ended (exit code {code})"
        )
    if code not in (0, 1):
        # 1 is an archive of files that changed while they were read.  Anything
        # else is no archive to go by: the program is not there, or it is not
        # one that takes these options.
        said = done.stderr_text or done.stdout.decode("utf-8", errors="replace").strip()
        raise VerificationError(
            f"the container's archive program ended with exit code {code}: {said[:200]}"
        )
    return read_archive(done.stdout, max_bytes=max_bytes, strip=strip)


def _failed_read(container: Container, error: VerificationError) -> Exported:
    """What a failed read of the files a command names comes to: an
    :class:`Exported` that says why where the failure is the model's doing,
    *error* raised again where it is the machinery's.

    Where the programs that read are the model's to change (commands as root
    over a writable root), it decides what any read does: every failure is
    its outcome.  Elsewhere the archive program is the image's, and what the
    model decides is only how much stands at the paths.  A read that was cut
    short there, by its time limit or by the archive program being ended,
    while the container still answers at once, is taken to be that; any other
    failure is the machinery's.
    """
    # A container that is gone, or a runtime that does not answer, says so
    # itself, with whose doing it is.
    container.ensure_running()
    if container.commands_can_change_programs():
        return Exported(failed=str(error))
    if isinstance(error, _CutShortError):
        probe = container.exec(["true"], timeout=30, user="0:0", workdir=OWN_WORKDIR)
        if probe.returncode == 0 and not probe.timed_out:
            return Exported(
                failed=f"{error}; the container still answers, so what stands at the paths "
                "is more than a read gets through"
            )
    raise error


# How often what is left running in a container is ended and looked for again
# before what is still there is taken to stay, and the seconds all of it may
# take: holding the container still, every list and every helper.  One
# deadline, which each of those calls is given what is left of.
_END_TRIES = 3
_END_SECONDS = 45.0
# No call is begun with less than this left of the step's time: it could not
# answer.  And what is kept back of that time for the list that says what
# stayed, so that no helper is begun that would leave none for it.
_LEAST_SECONDS = 1.0
_LAST_LIST_SECONDS = 5.0
# What removing a helper by its name may take.
_REMOVE_SECONDS = 10.0
# The seconds the runtime is given to hold a container still, and to let it
# run again afterwards: all tries of that together, and each of them.
_HOLD_SECONDS = 20.0
_RESUME_SECONDS = 40.0
_RESUME_TRIES = 3
# Ends every process its user may signal: all of that user's in the process
# namespace, but for itself and the namespace's first process.
_END_SCRIPT = "kill -9 -1"
# What the runtime's list is asked about each process: numbers and a state,
# nothing a process writes itself.  A process's name is its own to write, so
# it is asked apart (``_names``) and only ever shown.
_LIST_COLUMNS = "pid,ruid,rgid,nlwp,stat"
# No number in that list has more digits than this.
_MOST_DIGITS = 20
# User and group numbers are 32 bits wide.  The list prints one above half of
# that range as the negative number of the same bits, and the runtime starts
# a container as no user or group above the half.
_ID_RANGE = 2**32
_MOST_HELPER_ID = 2**31 - 1
# The group of a helper for a process whose own group is none the runtime
# takes.  A signal goes by the user, so the group decides nothing.
_NO_GROUP = 65534


def _ask(container: Container, *words: str, timeout: float, limit: int = 65536) -> Completed:
    """One command of the runtime about *container*, from outside it."""
    return run_captured([container.runtime, *words], timeout=max(timeout, 1.0), limit=limit)


def _unanswered(done: Completed) -> str | None:
    """Why the runtime's answer *done* is none, ``None`` when it is one."""
    if done.timed_out:
        return "the runtime did not answer in time"
    if done.returncode != 0:
        return done.stderr_text[:200] or f"the runtime ended with exit code {done.returncode}"
    return None


def _number(text: str) -> int | None:
    """*text* as a whole number, ``None`` where it is none.

    Its length is looked at first and its characters next; only then is it
    made a number.  Text that came out of a program is never handed to the
    conversion as it is: a number of some thousand digits makes the conversion
    raise, and so does a digit that is none of the ten.
    """
    digits = text[1:] if text.startswith("-") else text
    if not 0 < len(digits) <= _MOST_DIGITS or not (digits.isascii() and digits.isdigit()):
        return None
    return -int(digits) if text.startswith("-") else int(digits)


def _count(text: str) -> int | None:
    """*text* as a count: a whole number that is not negative."""
    number = _number(text)
    return number if number is not None and number >= 0 else None


def _user_number(text: str) -> int | None:
    """A user or group number as the runtime's list prints it, ``None`` for
    text that is none.  A number above 2**31 - 1 is printed as the negative
    number of the same 32 bits; one printed as it is reads the same."""
    number = _number(text)
    if number is not None and -(_ID_RANGE // 2) <= number < 0:
        number += _ID_RANGE
    return number if number is not None and 0 <= number < _ID_RANGE else None


def _held_still(container: Container, timeout: float) -> bool | None:
    """Whether the runtime counts *container* as held still; ``None`` where
    it does not say."""
    asked = _ask(
        container,
        "inspect",
        "--format",
        "{{.State.Paused}}",
        container.name,
        timeout=timeout,
    )
    said = asked.stdout.decode("ascii", errors="replace").strip()
    if _unanswered(asked) is not None or said not in ("true", "false"):
        return None
    return said == "true"


def _let_run_again(container: Container, confirmed: bool) -> str | None:
    """Let *container* run again once the runtime was asked to hold it still;
    why it does not run again, ``None`` when it does.

    Asked for whether or not the runtime confirmed the hold (*confirmed*).
    The runtime carries out a hold it was asked for whatever became of
    whoever asked, so one it did not confirm in time can still come about;
    left alone, the container would stay held and every command after it
    would fail for that.  The runtime takes the two requests one after the
    other, so this one waits for the hold and undoes it.

    A refusal for a container the runtime does not count as held is no
    failure: there was nothing to undo.  Where the hold was never confirmed
    it is asked for once more and then ended: a runtime that gave up on a
    hold half way may have left the container's processes held without
    counting the container as held, and it ends only a hold that it counts.
    """
    deadline = time.monotonic() + _RESUME_SECONDS

    def left() -> float:
        # Past the time for all of it, each question still gets a moment: a
        # container must not stay held for want of asking.
        return min(max(deadline - time.monotonic(), 1.0), 15.0)

    asked_again = confirmed
    why = ""
    for attempt in range(_RESUME_TRIES):
        why = _unanswered(_ask(container, "unpause", container.name, timeout=left())) or ""
        if not why:
            return None
        if _held_still(container, left()) is not False:
            # Still held, or the runtime does not say: once more.
            continue
        if asked_again or attempt == _RESUME_TRIES - 1:
            return None
        # Never without a try after it that ends the hold again.
        asked_again = True
        _ask(container, "pause", container.name, timeout=left())
    return why


def _others(container: Container, timeout: float) -> list[dict[str, Any]]:
    """The processes in *container* besides its main process, as the runtime
    lists them from outside: nothing inside the container is asked, so nothing
    inside it can answer for them.  Each with its process id as the list has
    it (``pid``) and its user and group numbers (``uid``, ``gid``).

    One that has ended and only waits to be collected is left out: its state
    begins with ``Z`` and no thread of it is left.  The state alone does not
    say that.  A process whose first thread has ended while another still runs
    is listed with the same state, and it runs; its count of threads tells the
    two apart.

    A list without the main process, or with a row whose process id or count
    of threads is no number, could not be read: nothing in a container
    decides those.  A user or group that is no number is ``None`` instead.
    Those two columns are a process's own to set where the task lets its
    commands change them, so they must not be what makes a list unreadable.
    """
    asked = _ask(
        container,
        "inspect",
        "--format",
        "{{.State.Pid}}",
        container.name,
        timeout=timeout,
    )
    listed = _ask(
        container,
        "top",
        container.name,
        "-eo",
        _LIST_COLUMNS,
        timeout=timeout,
        limit=4 * 1024 * 1024,
    )
    why = _unanswered(asked) or _unanswered(listed)
    main = asked.stdout.decode("ascii", errors="replace").strip()
    lines = listed.stdout.decode("utf-8", errors="replace").splitlines()[1:]
    rows = [line.split() for line in lines if line.strip()]
    if why is None and _count(main) is None:
        why = "the runtime did not name the main process"
    if why is None and not all(
        len(row) >= 5 and _count(row[0]) is not None and _count(row[3]) is not None for row in rows
    ):
        why = "the runtime's list of processes could not be read"
    if why is None and main not in [row[0] for row in rows]:
        why = "the runtime's list of processes does not hold the main process"
    if why is not None:
        container.ensure_running()
        raise VerificationError(f"what is running in the container could not be listed: {why}")
    return [
        {"pid": row[0], "uid": _user_number(row[1]), "gid": _user_number(row[2])}
        for row in rows
        if row[0] != main and not (row[4].startswith("Z") and (_count(row[3]) or 0) <= 1)
    ]


def _names(container: Container, timeout: float) -> dict[str, str]:
    """The name of each process in *container* by its process id, as the
    runtime lists them; empty where the runtime does not say.  Never raises.

    Only for showing what stayed.  A name is whatever its process wrote
    there, so nothing is decided by one, and no name is a reason to fail.
    """
    try:
        listed = _ask(
            container,
            "top",
            container.name,
            "-eo",
            "pid,comm",
            timeout=timeout,
            limit=4 * 1024 * 1024,
        )
    except OSError:
        return {}
    if _unanswered(listed) is not None:
        return {}
    names: dict[str, str] = {}
    for line in listed.stdout.decode("utf-8", errors="replace").splitlines()[1:]:
        pid, _, name = line.strip().partition(" ")
        if _number(pid) is not None:
            names[pid] = name.strip()[:64]
    return names


def _helper_users(others: list[dict[str, Any]]) -> list[str]:
    """The users to end *others* as: one for each user number among them.

    A signal goes by the user alone, so one helper ends all of a user's
    processes whatever their groups; it is given the group of the first.  A
    group the runtime takes for no container is replaced by ``_NO_GROUP``.  A
    user it takes for no container has no helper: nobody can be asked to end
    that process as its user.
    """
    users: dict[int, str] = {}
    for one in others:
        uid, gid = one["uid"], one["gid"]
        if uid is None or uid > _MOST_HELPER_ID or uid in users:
            continue
        if gid is None or gid > _MOST_HELPER_ID:
            gid = _NO_GROUP
        users[uid] = f"{uid}:{gid}"
    return list(users.values())


def _end_as(container: Container, user: str, timeout: float) -> str:
    """End every process *user* has in *container*'s process namespace, from a
    helper container; what went wrong with the helper, ``""`` when nothing did.

    The helper shares the namespace and nothing else.  It runs as the user
    whose processes it ends, without any capability, so it can do nothing in
    the namespace that this user's own processes could not; it never has more
    rights than what it ends.  Its shell is the image's own, from a file
    system the model never wrote to, and it has limits of its own, so it
    starts also where the container's processes hold every process the
    container may have.

    *timeout* is all it may take, the removal of a helper that did not start
    or did not end included.
    """
    began = time.monotonic()
    helper = _helper_name(container)
    argv = [
        "run",
        "--rm",
        "--name",
        helper,
        "--pid",
        f"container:{container.name}",
        "--pull",
        "never",
        "--label",
        "turnstone.rollout=1",
        "--network",
        "none",
        "--user",
        user,
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--read-only",
        "--memory",
        "64m",
        "--pids-limit",
        "16",
    ]
    for path in container.volumes:
        argv += ["--tmpfs", f"{path}:size=1m"]
    argv += ["--entrypoint", "sh", container.image, "-c", _END_SCRIPT]
    done = _ask(container, *argv, timeout=min(timeout, 30.0))
    if done.timed_out or done.returncode == 125:
        # Not started, or not ended: it must not stay, nor its name be taken
        # from the next one.  It is removed once more when the step is over.
        _remove_helper(container, timeout - (time.monotonic() - began))
        return f"the helper could not be started: {_unanswered(done)}"
    # Its own exit status says only whether it found something to end.
    return ""


def _helper_name(container: Container) -> str:
    """The name every helper of *container* has, one after the other."""
    return NAME_PREFIX + "end-" + container.name.removeprefix(NAME_PREFIX)


def _remove_helper(container: Container, timeout: float) -> None:
    """Remove *container*'s helper by its name, whether or not there is one."""
    _ask(
        container,
        "rm",
        "--force",
        "--volumes",
        _helper_name(container),
        timeout=min(max(timeout, _LEAST_SECONDS), _REMOVE_SECONDS),
    )


def end_what_is_left(container: Container) -> dict[str, Any]:
    """End every process in *container* but its main process, whoever it runs as.

    What a command left running there, the model's or the task's own, would
    otherwise run on beside the check: change a file between two of its
    commands, or under the read that follows.  Root in the container cannot
    end it: the container gives root no right to signal another user's
    processes, and no command is run there with rights the container does not
    have.  Instead:

    - the runtime holds the whole container still, so that nothing in it can
      start another process, end the helper first or hide while it is listed;
    - the processes are listed by the runtime, from outside;
    - for every user that has one, a helper container that shares the process
      namespace ends that user's processes as that user (:func:`_end_as`);
    - they are listed again, and what is still there is ended again, at most
      ``_END_TRIES`` times;
    - the helper is removed by its name, whatever became of it, and the
      container is let run again (:func:`_let_run_again`), also where the
      runtime did not confirm that it holds it still.

    Holding still, listing and ending have ``_END_SECONDS`` together, and
    every call of the runtime is given what is left of them.  When they are
    over no helper is begun any more; the processes are listed once more, in
    time kept back for that, and what that list holds is what stayed.  The
    removal of the helper and letting the container run again come after,
    with bounds of their own: they are owed whatever the time.

    Returns how many processes were found beside the main one, which of them
    are still running, and whether the container could be held still.

    Whose doing one still running is follows from the task's settings, like
    the end of a container's main process.  It is the model's own outcome,
    and comes back in the result, in two kinds of container: where the
    programs are the model's to change (commands as root over a writable
    root), and where the commands can change the user they run as (root with
    the ``SETUID`` capability), because a process can then be given a user
    number the runtime starts no helper as, and there can be more users than
    the step has time for.  Anywhere else it is a failure of the machinery
    (:class:`VerificationError`), as is a runtime that does not list the
    processes or does not let the container run again.

    The size watch of a writable root looks inside the container with a
    process of its own.  None is begun, and none is under way, while the
    container is held still.  A command that is being started at the moment
    a container is held keeps the runtime's list of its processes from
    answering until the container runs again, which would fail this step;
    and a look under way would be counted as left running, and ended.
    """
    with container.without_looks_inside():
        return _end_held(container)


def _end_held(container: Container) -> dict[str, Any]:
    """The step itself (:func:`end_what_is_left`), with nothing of the
    harness's own running in *container*."""
    deadline = time.monotonic() + _END_SECONDS

    def left() -> float:
        return deadline - time.monotonic()

    hold = min(_HOLD_SECONDS, _END_SECONDS)
    frozen = _unanswered(_ask(container, "pause", container.name, timeout=hold)) is None
    found: int | None = None
    others: list[dict[str, Any]] = []
    names: dict[str, str] = {}
    said = ""
    out_of_time = helper_asked = False
    try:
        for attempt in range(_END_TRIES + 1):
            if found is not None and left() < _LEAST_SECONDS:
                # No time for a list: what the last one held is what is known.
                out_of_time = True
                break
            others = _others(container, min(max(left(), _LEAST_SECONDS), 15.0))
            if found is None:
                found = len(others)
            if not others or attempt == _END_TRIES or out_of_time:
                break
            users = _helper_users(others)
            if attempt:
                # Where the runtime's numbers are this machine's and not the
                # container's own, the users the task names still are.
                for user in (container.user, "0:0"):
                    if user not in users:
                        users.append(user)
            for user in users:
                for_helpers = left() - _LAST_LIST_SECONDS
                if for_helpers < _LEAST_SECONDS:
                    out_of_time = True
                    break
                helper_asked = True
                said = _end_as(container, user, for_helpers) or said
        if others and left() >= _LEAST_SECONDS:
            names = _names(container, min(left(), 15.0))
    finally:
        # Both whatever happened, and each with a bound of its own.
        if helper_asked:
            # A helper whose start was cut at its time can still come about
            # afterwards, and nothing else would ever remove it.
            with contextlib.suppress(OSError):
                _remove_helper(container, _REMOVE_SECONDS)
        still_held = _let_run_again(container, frozen)
    if still_held is not None:
        container.ensure_running()
        raise VerificationError(
            "the container was held still to end what was left running in it, and the "
            f"runtime did not let it run again ({still_held})"
        )
    still = [{"uid": one["uid"], "command": names.get(one["pid"], "")} for one in others]
    the_models = container.commands_can_change_programs() or container.commands_can_change_users()
    if still and not the_models:
        container.ensure_running()
        shown = ", ".join(
            f"{one['command'] or 'a process'} "
            f"(user {'unknown' if one['uid'] is None else one['uid']})"
            for one in still[:8]
        )
        when = f"after {_END_TRIES} tries"
        if out_of_time:
            when = f"when the {_END_SECONDS:g} s for it were over"
        raise VerificationError(
            f"what was left running in the container could not be ended: {len(still)} "
            f"still there {when} ({shown})" + (f"; {said}" if said else "")
        )
    return {"found": found or 0, "still_running": still, "frozen": frozen}


def run_after(container: Container, command: AfterRunCommand) -> dict[str, Any]:
    """Run one after-run command in the episode's running container.

    What was left running there is ended first (:func:`end_what_is_left`), so
    the command and the read that follows have the container to themselves.
    The command's exit status, output and time are a result whatever they
    are.  So is a failure to read the files it names where that is the
    model's doing (:func:`_failed_read`): there it must not take the rollout
    out of training.
    """
    leftovers = end_what_is_left(container)
    result = _run_in(
        container, command.argv, command.user, command.timeout, command.max_output_chars
    )
    result["leftovers"] = leftovers
    if command.collect:
        try:
            collected = _collect(container, command.collect, command.max_collect_bytes)
        except VerificationError as error:
            collected = _failed_read(container, error)
        result["collected"] = {
            path: content.decode("utf-8", errors="replace")
            for path, content in collected.files().items()
        }
        result["collected_summary"] = collected.summary()
    return result


@dataclass
class VerificationContext:
    """What a verifier gets."""

    # The episode's task.
    task: Any
    # The stopped episode container, to read from with the runtime's own
    # commands; ``None`` when the sandbox is not a container.
    container_name: str | None
    # Whether it could be stopped, and what the runtime says about how it ended.
    container_stopped: bool
    container_state: dict[str, Any]
    # The after-run commands' results, in order.
    after_run: list[dict[str, Any]]
    # The episode record so far.  To read; the verdict goes in the return value.
    record: dict[str, Any]
    # The task's stopped sidecar, if it has one: its state is what the model's
    # tool calls made of it.
    sidecar_name: str | None = None
    runtime: str = "docker"
    # Seconds a container started through this context is kept at most, for a
    # runner that is killed before it removes it: the verifier's own time
    # limit and a margin.
    lifetime: int = 4200
    _started: list[Container] = field(default_factory=list, repr=False)

    def export(self, paths: list[str], *, max_bytes: int = 64 * 1024 * 1024) -> Exported:
        """Read *paths* out of the stopped episode container (:func:`export_paths`)."""
        if self.container_name is None:
            raise VerificationError("the episode has no container to read from")
        return export_paths(self.container_name, paths, max_bytes=max_bytes, runtime=self.runtime)

    def clean_container(
        self, spec: ContainerSpec, *, max_lifetime: int | None = None
    ) -> CleanContainer:
        """Start a container from *spec* that the model never touched.

        The same isolation as an episode's container.  The harness removes it
        when the verifier is done, whatever the verifier does with it, also
        when the verifier ran out of its time while the container was still
        being started.
        """
        container = Container(
            spec, max_lifetime=max_lifetime or self.lifetime, runtime=self.runtime
        )
        self._started.append(container)
        try:
            container.start()
        except Exception as error:
            raise VerificationError(f"the verification container did not start: {error}") from error
        return CleanContainer(container)

    def remove_started(self) -> bool:
        """Remove every container started through this context; ``True`` when
        every removal is confirmed.  Never raises."""
        return all([container.close() for container in self._started])


def run_verifier(
    verifier: Callable[[VerificationContext], dict[str, Any] | None],
    context: VerificationContext,
    timeout: float,
) -> tuple[dict[str, Any] | None, str | None]:
    """Call *verifier* with its own time limit; ``(result, None)`` or ``(None, why)``.

    Never raises.  A verifier that is still running at the limit is left behind
    on its thread; the containers it started are removed by the caller.
    """
    box: dict[str, Any] = {}

    def call() -> None:
        try:
            box["result"] = verifier(context)
        except BaseException as error:  # reported, never raised into the run
            box["error"] = f"{type(error).__name__}: {str(error)[:500]}"

    worker = threading.Thread(target=call, daemon=True, name="rollout-verifier")
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        return None, f"the verifier did not finish within {timeout:g} s"
    if "error" in box:
        return None, f"the verifier raised {box['error']}"
    result = box.get("result")
    if not isinstance(result, dict) or result.get("verdict") is None:
        return None, "the verifier returned no verdict"
    return result, None
