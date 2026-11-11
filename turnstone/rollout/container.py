"""One container per rollout, and the execution environment that acts inside it.

:class:`ContainerSpec` says what one task's container is: its image, working
directory, the user its commands run as, what is writable, its capabilities,
environment and limits.  :class:`Container` owns the lifecycle: a container
with no network, holding nothing of this machine's but the three files the
runtime itself mounts into every container (``RUNTIME_FILES``), never the
cause of an image being fetched, removed afterwards, its volumes with it, and
the removal confirmed.

:class:`ContainerEnvironment` is the session's
:class:`~turnstone.core.exec_env.ExecutionEnvironment` for that container:
every shell command and every file read or write of the session's tools is a
``exec`` into it.  The session itself runs outside.
"""

from __future__ import annotations

import contextlib
import ctypes
import errno
import io
import json
import os
import posixpath
import signal
import subprocess
import tarfile
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from turnstone.core.background_shells import signal_group
from turnstone.core.exec_env import ExecutionEnvironment
from turnstone.core.log import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

log = get_logger(__name__)

_NL = chr(10)
NAME_PREFIX = "tsgym-"

# Exit codes the helper scripts below use for what they checked themselves, so
# an error maps to an exception without reading a localized message.
_RC_MISSING = 44
_RC_DIRECTORY = 45
_RC_UNREADABLE = 46
_RC_TOO_LARGE = 47
_RC_NOT_REGULAR = 48
_RC_NO_PARENT = 49

# Text of the runtime's own failure to reach a container (as opposed to the
# command's output), checked before the container's state is looked up.
_RUNTIME_ERROR_MARKS = ("Error response from daemon", "No such container", "is not running")

# $1 path, $2 seconds, $3 "head" or "whole", $4 byte count (head) or cap (whole).
_READ_SCRIPT = """
[ -e "$1" ] || exit 44
[ -d "$1" ] && exit 45
[ -r "$1" ] || exit 46
if [ "$3" = head ]; then
  exec timeout -s KILL "$2" head -c "$4" -- "$1"
fi
[ -f "$1" ] || exit 48
size=$(stat -L -c %s -- "$1") || exit 46
[ "$size" -le "$4" ] || exit 47
exec timeout -s KILL "$2" cat -- "$1"
"""

# $1 path, $2 seconds, $3 "w" or "a", $4 "parents" to create missing directories.
_WRITE_SCRIPT = """
[ -d "$1" ] && exit 45
dir=$(dirname -- "$1")
if [ "$4" = parents ]; then
  mkdir -p -- "$dir" || exit 49
fi
[ -d "$dir" ] || exit 44
if [ "$3" = a ]; then
  exec timeout -s KILL "$2" sh -c 'cat >> "$1"' sh "$1"
fi
exec timeout -s KILL "$2" sh -c 'cat > "$1"' sh "$1"
"""

# $1 script path inside the container, $2 shell.  The script arrives on standard
# input; it then runs as the leader of its own session and process group, whose
# id is left next to it for the kill.  When the script ends, whatever it left
# running in that group is killed at once, as the host's shell tool does, so the
# output streams close and the caller is not kept waiting for them.
_SPAWN_SCRIPT = """
cat > "$1" || exit 125
(exec setsid "$2" "$1" < /dev/null) &
pgid=$!
echo "$pgid" > "$1.pgid"
wait "$pgid" 2>/dev/null
rc=$?
kill -s KILL -- "-$pgid" 2>/dev/null
exit "$rc"
"""

# $1 script path.  Kills exactly the group the spawn recorded, then tidies up.
_KILL_SCRIPT = """
i=0
while [ ! -s "$1.pgid" ] && [ "$i" -lt 20 ]; do sleep 0.05; i=$((i + 1)); done
pgid=$(cat "$1.pgid" 2>/dev/null) && kill -s KILL -- "-$pgid" 2>/dev/null
rm -f -- "$1" "$1.pgid"
"""

# What the check of the shell tool prints when a command ran as it should.
_SHELL_CHECK = "turnstone-shell-tool-check"


def _script(command: str, stop_on_error: bool) -> str:
    """*command* as the script the shell tool runs: behind the preamble the
    host's shell tool puts in front of a command."""
    return "set -o pipefail" + _NL + ("set -e" + _NL if stop_on_error else "") + command


# Prints the device of the root file system, then device, links, inode and size
# of every file a process holds open.  A file with no link left was deleted
# while open: no listing of the file system shows it, and it takes room.
_OPEN_FILES_SCRIPT = (
    "stat -c 'root %d' / && stat -L -c '%d %h %i %s' /proc/[0-9]*/fd/* 2>/dev/null; true"
)

# Failures a helper's own message names; reported with their usual number and text.
_KNOWN_ERRNOS = (
    errno.ENOSPC,
    errno.EROFS,
    errno.EACCES,
    errno.ENOTDIR,
    errno.EISDIR,
    errno.ENAMETOOLONG,
    errno.EDQUOT,
)

# What the image must provide for the tools to work.
_REQUIRED_PROGRAMS = ("setsid", "timeout", "head", "cat", "stat", "mkdir", "dirname", "grep")

# The size a writable root is bounded to where a task names none.  A writable
# root is never without a bound: what is written there goes to this machine's
# disk.
DEFAULT_ROOT_SIZE = "10g"

# The files the runtime itself keeps on this machine's disk and mounts into
# every container.  Over a read-only root it mounts them read-only.  Over a
# writable root the root user can write them, and they are part of neither the
# container's own file system nor any size a storage driver enforces, so their
# sizes are asked of the runtime and counted (``Container._runtime_file_bytes``).
RUNTIME_FILES = ("/etc/hosts", "/etc/hostname", "/etc/resolv.conf")
# How much of the runtime's copy of one such file is read.  The archive's first
# header says the file's size; its content is not waited for.
_ARCHIVE_HEAD_BYTES = 64 * 1024
# Where the harness starts what it runs in a container for itself: the look for
# files deleted while open, the end of a command that ran out of its time, the
# read of the files an after-run command names.  Not the task's working
# directory, which its user can rename or remove where it owns the directory
# above it: the runtime then starts nothing there.
OWN_WORKDIR = "/"
# What a runtime says when nothing stands at a path it was asked to copy.
_NOTHING_THERE_MARKS = ("could not find the file", "could not be found on container")


class ContainerError(RuntimeError):
    """The container could not be created, or the runtime could not be asked about it."""


class ContainerLostError(OSError):
    """The container stopped while the run still needed it."""


@dataclass
class Completed:
    """The outcome of one command run to completion with bounded capture."""

    returncode: int | None
    stdout: bytes
    stderr: bytes
    timed_out: bool
    # Bytes the command wrote to standard output, also those not kept.
    stdout_total: int
    # The command was stopped because it had written more than it may.
    stopped_early: bool = False

    @property
    def stderr_text(self) -> str:
        return self.stderr.decode("utf-8", errors="replace").strip()


# Whether a child can be waited for without being collected.  Until it is
# collected its process id is still its own, and with it the id of the process
# group it leads, so that group can be signalled without reaching anything else.
_CAN_WAIT_UNCOLLECTED = hasattr(os, "waitid") and hasattr(os, "WNOWAIT")
# Seconds a command that was killed is given to be gone, and seconds its output
# is still read after it has ended.
_COLLECT_SECONDS = 10.0
_DRAIN_SECONDS = 5.0


def _ended_within(
    proc: subprocess.Popen[Any], timeout: float, collecting: threading.Lock
) -> bool | None:
    """Wait until *proc* has ended, for at most *timeout* seconds; ``False``
    when it has not.  Where the system allows, the process is not collected;
    where it does not, it is collected under *collecting*, the lock its group
    is signalled under.

    ``None`` when the system says that *proc* is not this process's to wait
    for: somebody else collected it, or it never was a child.  Its id then
    names nothing that is certain to be the command's.
    """
    deadline = time.monotonic() + timeout
    delay = 0.0005
    while True:
        if _CAN_WAIT_UNCOLLECTED:
            try:
                ended = os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG)
            except ChildProcessError:
                return None
            if ended is not None:
                return True
        else:
            with collecting:
                if proc.poll() is not None:
                    return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        delay = min(delay * 2, remaining, 0.05)
        time.sleep(delay)


def run_captured(
    argv: list[str],
    *,
    stdin_data: bytes | None = None,
    timeout: float,
    limit: int,
    stop_after: int | None = None,
) -> Completed:
    """Run *argv* and keep at most *limit* leading bytes of each output stream.

    Output past the limit is read and dropped, so the command never blocks on a
    full pipe and this process never holds more than the limit.  At *timeout*
    the local process group is killed.  With *stop_after* it is also killed as
    soon as it has written more than that many bytes to standard output: a
    caller that will refuse more than a bound need not wait for the rest, which
    may never end.

    *timeout* bounds the whole call, not only the command.  When the command
    has ended, whatever it left running in its process group is killed, as the
    shell tool does with what a command leaves behind: such a process holds
    the command's output open, and waiting for it would hold this call for as
    long as it lives.  A process the command moved out of its group is not
    this call's to end; its output is read a moment longer and then left.
    """
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    kept: dict[str, bytearray] = {"out": bytearray(), "err": bytearray()}
    totals = {"out": 0, "err": 0}
    stopped_early = threading.Event()
    # Held while the group is signalled and while the command is collected.
    collecting = threading.Lock()
    # Set once a wait has said that the command is not this process's to wait
    # for.  From then on its id is signalled no more.
    not_ours = threading.Event()

    def end_group() -> bool:
        """Kill the command's process group; ``False`` when it was not signalled.

        The command leads a session and a group of its own.  Only while it has
        not been collected is the group's id certain to be its own.
        """
        with collecting:
            if not_ours.is_set():
                return False
            try:
                return signal_group(proc, proc.pid, signal.SIGKILL)
            except OSError:
                return False

    def drain(pipe: Any, key: str) -> None:
        try:
            while True:
                # What has arrived, not a full buffer: a pipe that stays open
                # must not keep back what was already written to it.
                data = pipe.read1(65536)
                if not data:
                    return
                totals[key] += len(data)
                room = limit - len(kept[key])
                if room > 0:
                    kept[key].extend(data[:room])
                if key == "out" and stop_after is not None and totals[key] > stop_after:
                    stopped_early.set()
                    end_group()
                    return
        except (OSError, ValueError):
            return
        finally:
            # By the thread that reads it: closing a pipe that another thread
            # is still reading would wait for that read.
            with contextlib.suppress(OSError, ValueError):
                pipe.close()

    threads = [
        threading.Thread(target=drain, args=(proc.stdout, "out"), daemon=True),
        threading.Thread(target=drain, args=(proc.stderr, "err"), daemon=True),
    ]
    if stdin_data is not None and proc.stdin is not None:
        # On its own thread, so a command that never reads its input cannot
        # hold this call past *timeout*: the kill below closes the pipe.
        threads.append(threading.Thread(target=_feed, args=(proc.stdin, stdin_data), daemon=True))
    for thread in threads:
        thread.start()
    ended = _ended_within(proc, timeout, collecting)
    if ended is None:
        not_ours.set()
    timed_out = ended is False
    # At its time limit, everything the command started; after it has ended by
    # itself, whatever it left running in its group.
    if not end_group() and timed_out:
        with contextlib.suppress(OSError):
            proc.kill()
    with collecting, contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=_COLLECT_SECONDS)
    # What the command wrote is in the pipes or read already.  A pipe that
    # something outside its group still holds open is not waited for: the
    # thread that reads it goes on, and ends when the pipe does.
    deadline = time.monotonic() + _DRAIN_SECONDS
    for thread in threads:
        thread.join(timeout=max(deadline - time.monotonic(), 0.0))
    return Completed(
        returncode=proc.returncode,
        stdout=bytes(kept["out"]),
        stderr=bytes(kept["err"]),
        timed_out=timed_out and not stopped_early.is_set(),
        stdout_total=totals["out"],
        stopped_early=stopped_early.is_set(),
    )


def _feed(pipe: Any, data: bytes) -> None:
    try:
        pipe.write(data)
        pipe.close()
    except (OSError, ValueError):
        # The command ended, or was killed, without reading its input.
        return


# The capabilities a task may name for its container.  What root needs for
# ordinary work on files and users it owns; none of them reaches outside the
# container or its main process.
ALLOWED_CAPABILITIES = frozenset(
    {
        "CHOWN",
        "DAC_OVERRIDE",
        "FOWNER",
        "FSETID",
        "KILL",
        "SETGID",
        "SETUID",
        "NET_BIND_SERVICE",
        "SYS_CHROOT",
    }
)
# Refused by name, with the reason, because each would undo something the
# container is relied on for.  Anything not allowed above is refused as well.
REFUSED_CAPABILITIES = {
    "SYS_BOOT": "restarting ends the container's main process from inside",
    "SYS_PTRACE": "the container's main process could be taken over from inside",
    "SYS_ADMIN": "it lifts most of the container's confinement",
    "SYS_RESOURCE": "the container's limits could be raised from inside",
    "SYS_TIME": "the clock is the host's",
    "SYS_MODULE": "kernel modules are the host's",
    "SYS_RAWIO": "it reaches the host's devices",
    "MKNOD": "device files reach the host's devices",
    "NET_ADMIN": "a container has no network to administer",
    "NET_RAW": "a container has no network",
    "DAC_READ_SEARCH": "file handles opened with it can reach outside the container",
}


@dataclass
class ContainerSpec:
    """What one task's container is.  The defaults are the contained case: an
    unprivileged user, a read-only root and a memory-backed working directory.

    A task that needs otherwise says so: its own image and working directory,
    commands run as root, a writable root, a few capabilities.  Whatever it
    says, the container has no network, no setting mounts anything of this
    machine's into it, its image is never fetched for it, and its main process
    is out of its commands' reach as far as these settings allow (see
    :class:`Container`).
    """

    image: str = "python:3.14-slim"
    # Where commands start and relative paths resolve.
    workdir: str = "/workspace"
    # The user the model's commands run as; the image's root is ``"0:0"``.
    user: str = "65534:65534"
    # ``False`` makes the image's whole file system writable.  The writes then
    # go to this machine's disk, up to ``root_size``.
    read_only_root: bool = True
    # Memory-backed writable directories and their sizes.  ``None``: the working
    # directory (512m) and /tmp (256m) over a read-only root, nothing over a
    # writable one.
    writable: dict[str, str] | None = None
    # What a writable root may hold, the three files the runtime mounts into
    # the container included (``RUNTIME_FILES``).  ``None`` is
    # ``DEFAULT_ROOT_SIZE``: a writable root always has a size.
    root_size: str | None = None
    # Size of each directory the image declares as a volume.  The runtime would
    # put such a directory on this machine's disk, writable and without a size,
    # whatever the root is; it becomes a memory-backed directory instead, empty
    # like the others.  A path that is also under ``writable`` has that size.
    volume_size: str = "64m"
    shm_size: str = "64m"
    # Capabilities the commands may use, from ``ALLOWED_CAPABILITIES``.
    capabilities: tuple[str, ...] = ()
    environment: dict[str, str] = field(default_factory=dict)
    cpus: float = 1.0
    # Covers the processes and every memory-backed directory.
    memory: str = "2g"
    pids_limit: int = 256

    def writable_areas(self) -> dict[str, str]:
        if self.writable is not None:
            return dict(self.writable)
        return {self.workdir: "512m", "/tmp": "256m"} if self.read_only_root else {}

    def runs_as_root(self) -> bool:
        return self.user.split(":")[0] in ("0", "root")

    def validate(self) -> None:
        """Raise ``ValueError`` for a setting a container must not have, and
        for one it cannot have.

        What can be seen to be wrong without asking the runtime is refused
        here, before anything is started: a value the runtime would refuse,
        and one it would take to mean "no limit".  What only the runtime can
        say is found when the container is started: an image that is not
        there, a user name the image does not hold, a limit this machine
        cannot give.
        """
        sizes = {"memory": self.memory, "shm_size": self.shm_size, "volume_size": self.volume_size}
        areas = self.writable or {}
        sizes.update({f"the size of {path}": given for path, given in areas.items()})
        texts = [("image", self.image), ("workdir", self.workdir), ("user", self.user)]
        texts += list(sizes.items())
        texts += [("a writable directory", path) for path in areas]
        for name, value in self.environment.items():
            texts += [("an environment variable's name", name), (f"the value of {name}", value)]
        for what, value in texts:
            if not isinstance(value, str) or chr(0) in value:
                raise ValueError(f"{what} must be text without a NUL character: {value!r}")
        if not self.image or self.image[0] == "-" or any(c.isspace() for c in self.image):
            raise ValueError(f"image must name an image: {self.image!r}")
        problem = _user_problem(self.user)
        if problem is not None:
            raise ValueError(
                "user must be a name or a number, with a group after a colon if it names "
                f"one: {self.user!r} ({problem})"
            )
        for name, value in self.environment.items():
            if not name:
                raise ValueError(f"an environment variable needs a name: {'=' + value!r}")
            if "=" in name:
                raise ValueError(f"an environment variable's name cannot hold '=': {name!r}")
        for path in areas:
            if not posixpath.isabs(path) or ":" in path:
                raise ValueError(
                    f"a writable directory must be an absolute path without a colon: {path!r}"
                )
        for what, given in sizes.items():
            if (_runtime_size(given) or 0) <= 0:
                raise ValueError(f"{what} must be a size above zero, like 2g or 512m: {given!r}")
        cpus = self.cpus
        if isinstance(cpus, bool) or not isinstance(cpus, (int, float)) or not 0 < cpus < _NO_LIMIT:
            raise ValueError(f"cpus must be a number above zero: {cpus!r}")
        if type(self.pids_limit) is not int or self.pids_limit < 1:
            raise ValueError(f"pids_limit must be a whole number above zero: {self.pids_limit!r}")
        for name in self.capabilities:
            plain = name.upper().removeprefix("CAP_")
            if plain in REFUSED_CAPABILITIES:
                raise ValueError(f"capability {plain} is refused: {REFUSED_CAPABILITIES[plain]}")
            if plain not in ALLOWED_CAPABILITIES:
                raise ValueError(
                    f"capability {plain} is not one a task may name "
                    f"({', '.join(sorted(ALLOWED_CAPABILITIES))})"
                )
        if not posixpath.isabs(self.workdir):
            raise ValueError(f"the working directory must be absolute: {self.workdir!r}")
        if self.root_size is not None:
            if self.read_only_root:
                raise ValueError("root_size is for a writable root")
            try:
                size = size_bytes(self.root_size)
            except (AttributeError, ValueError, OverflowError):
                size = 0
            if size <= 0:
                raise ValueError(
                    f"root_size must be a size above zero, like 8g or 512m: {self.root_size!r}"
                )


# The units of a size as the runtime reads one, in bytes.
_SIZE_UNITS = {"k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4, "p": 1024**5}
# What the name of a user or a group is made of.
_NAME_CHARACTERS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-")
# The largest number the kernel takes for a user or a group.
_MAX_USER_NUMBER = 2**32 - 2
_NO_LIMIT = float("inf")


def _runtime_size(text: Any) -> int | None:
    """*text* in bytes where it is a size as the runtime reads one: a number,
    whole or with a decimal point, then a unit or none (``512m``, ``1.5g``,
    ``1048576``, ``64mb``).  ``None`` for anything else."""
    if not isinstance(text, str) or not text.isascii():
        return None
    rest = text.lower().removesuffix("b").removesuffix("i")
    factor = 1
    if rest[-1:] in _SIZE_UNITS:
        factor, rest = _SIZE_UNITS[rest[-1]], rest[:-1].removesuffix(" ")
    whole, point, fraction = rest.partition(".")
    if not whole.isdigit() or (point and not fraction.isdigit()):
        return None
    try:
        return int(float(rest) * factor)
    except (OverflowError, ValueError):
        return None


def _user_problem(user: str) -> str | None:
    """What is wrong with *user* as the runtime takes one: a name or a number,
    and after a colon a group, likewise; ``None`` when nothing is.  Whether
    the image holds a user of that name only the runtime can say."""
    parts = user.split(":")
    if len(parts) > 2:
        return "more than one colon"
    for part in parts:
        if part.isascii() and part.isdigit():
            if len(part) > 10 or int(part) > _MAX_USER_NUMBER:
                return f"{part} is above the largest number a user can have"
            continue
        name = part.removesuffix("$")
        if not name or name[0] == "-" or not set(name) <= _NAME_CHARACTERS:
            return f"{part!r} is neither a name nor a number"
    return None


# The users the container's main process can run as.  Never the user a task's
# commands run as: one process may signal another, set its limits, trace it
# (where the machine allows tracing at all) and write its ``/proc`` files only
# as the same user, or with a capability no task may name.
_MAIN_USERS = ("65533:65533", "65532:65532")

# The container's main process.  It is the first process of its process
# namespace, to which the kernel delivers no signal from inside that it has no
# handler for, the kill and stop signals included.
_MAIN_TRAPS = "trap '' HUP INT QUIT ALRM TERM USR1 USR2 PIPE TSTP TTIN TTOU"

# It should be ``sleep`` itself, with the signal for an ended child ignored:
# the kernel then collects every orphaned process handed to it, and the main
# process never runs again.  It starts no program, reads no file and uses no
# processor time after this script, so neither a limit set on it nor a change
# to the files it came from can end it.  A shell cannot ignore that signal for
# what it starts unless it is one that lets a script do so, hence the choice
# of four ways; the image's own ``sh`` is not always one.
_SLEEP_AS_MAIN = """
if command -v bash >/dev/null 2>&1; then
  exec bash -c 'trap "" CHLD; exec sleep 2147483647'
fi
if env --ignore-signal=CHLD true >/dev/null 2>&1; then
  exec env --ignore-signal=CHLD sleep 2147483647
fi
if command -v python3 >/dev/null 2>&1; then
  exec python3 -c 'import os, signal
signal.signal(signal.SIGCHLD, signal.SIG_IGN)
os.execvp("sleep", ["sleep", "2147483647"])'
fi
if command -v perl >/dev/null 2>&1; then
  exec perl -e '$SIG{CHLD} = "IGNORE"; exec "sleep", "2147483647"'
fi
"""

# Where the image offers none of them the shell itself stays as the main
# process: it waits for one ``sleep`` and collects the orphans while it waits.
#
# A shell that cannot start a process gives up, and at the container's process
# limit, which a task's commands can hold, it cannot.  So this one must not
# have to start anything while commands run.  It waits for a ``sleep`` that
# does not end by itself, and it starts that only once it has itself been
# spared from the out-of-memory killer, so that what it waits for is spared
# too and no kill for memory inside the container takes it.  It starts
# ``sleep`` again, from the image's files, only if something ended it, which
# takes a setting of the task's: see ``Container._reach``.
_SHELL_AS_MAIN = """
sleep=$(command -v sleep)
n=0
while read -r score < /proc/self/oom_score_adj && [ "$score" != -1000 ] && [ "$n" -lt 100 ]; do
  "$sleep" 0.1 2>/dev/null || "$sleep" 1
  n=$((n + 1))
done
while :; do "$sleep" 2147483647 || "$sleep" 3600; done
"""
_MAIN_PROCESS = _MAIN_TRAPS + _SLEEP_AS_MAIN + _SHELL_AS_MAIN

# Lowers the main process's out-of-memory score.  Run once, by a helper that
# shares the container's process namespace and may do this one thing.
_PROTECT_MAIN_PROCESS = "echo -1000 > /proc/1/oom_score_adj"

# What the container says about itself once it runs, in ``key=value`` lines
# and with the shell alone: the user of a command, the main process's user,
# name, ignored signals and out-of-memory score, whether a command's user
# could replace the program a waiting shell starts, and every program of
# "$@" that is missing.
_START_CHECK = """
while read -r key value rest; do
  case "$key" in Uid:) echo "uid=$value" ;; esac
done < /proc/self/status
while read -r key value rest; do
  case "$key" in Uid:) echo "main_uid=$value" ;; SigIgn:) echo "ignored=$value" ;; esac
done < /proc/1/status
read -r comm < /proc/1/comm; echo "comm=$comm"
read -r score < /proc/1/oom_score_adj; echo "score=$score"
sleep=$(command -v sleep)
if [ -w "$sleep" ] || [ -w "${sleep%/*}" ]; then echo "sleep_writable=1"; fi
for p in "$@"; do command -v "$p" >/dev/null 2>&1 || echo "missing=$p"; done
"""
_SIGCHLD_BIT = 1 << (signal.SIGCHLD - 1)

# An out-of-memory kill this close before the main process ended took it.
_OOM_AT_DEATH_SECONDS = 2.0
# The runtime being told to end the container this close before its main
# process ended is what ended it.
_ASKED_BEFORE_DEATH_SECONDS = 15.0

# The kernel's request "signal this process when the thread that started it
# ends", where there is one.
_SIGNAL_AT_STARTERS_END = 1
try:
    _PROCESS_CONTROL: Any = ctypes.CDLL(None, use_errno=True).prctl
except (AttributeError, OSError, TypeError):
    _PROCESS_CONTROL = None


def _ends_with_its_starter() -> Callable[[], None] | None:
    """What a child of this process runs before it becomes its program, so that
    the kernel kills it when the thread that started it ends: with the whole
    process at the latest, however that ends, a kill that cannot be caught
    included.  ``None`` where the kernel offers no such request.

    The request counts for the starting thread, not for the process, so a
    child that has to live as long as something else does is started on a
    thread that lives that long.
    """
    if _PROCESS_CONTROL is None:
        return None
    starter = os.getpid()

    def arrange() -> None:
        _PROCESS_CONTROL(_SIGNAL_AT_STARTERS_END, int(signal.SIGKILL), 0, 0, 0)
        if os.getppid() != starter:
            # The starter was gone before the kernel was asked.
            os._exit(127)

    return arrange


# $1 seconds, $2 the runtime's command, $3 the container's name.  After the
# time: remove the container with its volumes, and keep trying for a while
# when it is still there or the runtime cannot say.  The only program besides
# the runtime that this module starts on this machine.
_LIFETIME_SCRIPT = (
    'sleep "$1"; n=0; while [ "$n" -lt 30 ]; do '
    '"$2" rm --force --volumes "$3" >/dev/null 2>&1; '
    'said=$("$2" container inspect --format x "$3" 2>&1) || '
    'case "$said" in *"o such"*) exit 0 ;; esac; '
    "n=$((n + 1)); sleep 10; done"
)


class _Lifetime:
    """Removes a container after a time, from outside it.

    A process of its own on this machine, in its own session, so that it
    outlives a runner that is killed, and out of the container's reach.  It
    removes the container's volumes with it, and keeps trying for some minutes
    when the container is still there afterwards or the runtime cannot say.
    """

    def __init__(self, runtime: str, name: str) -> None:
        self._runtime = runtime
        self._name = name
        self._proc: subprocess.Popen[bytes] | None = None
        self._lock = threading.Lock()

    def keep_for(self, seconds: float) -> None:
        """Have the container removed *seconds* from now, whatever was set before.

        The new keeper is started before the old one is stopped, so that there
        is never a moment without one.
        """
        argv = ["sh", "-c", _LIFETIME_SCRIPT, "sh", str(max(int(seconds), 0))]
        proc = subprocess.Popen(
            [*argv, self._runtime, self._name],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        with self._lock:
            old, self._proc = self._proc, proc
        self._stop(old)

    def cancel(self) -> None:
        with self._lock:
            proc, self._proc = self._proc, None
        self._stop(proc)

    @staticmethod
    def _stop(proc: subprocess.Popen[bytes] | None) -> None:
        if proc is None:
            return
        # Its own session and group, signalled only while it has not been
        # collected: until then the group's id is certain to be its own.
        with contextlib.suppress(OSError):
            signal_group(proc, proc.pid, signal.SIGKILL)
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            proc.wait(timeout=5)


class _StopWatch:
    """Listens to what the runtime reports about one container: the
    out-of-memory kills inside it, when the runtime was told to end it, and
    when its main process ended.

    The runtime's own state says that an out-of-memory kill happened in the
    container at some time; only the events say whether one came right before
    the main process ended, and whether somebody outside asked for the end.

    The runtime's client that carries the events must not outlive the runner.
    It is started on the thread that reads it and ends with that thread, which
    is with the runner at the latest, however the runner ends
    (:func:`_ends_with_its_starter`).  Where the kernel offers no such
    request, the client ends when the container is removed: it then writes
    that event into a pipe nobody reads any more.
    """

    def __init__(self, runtime: str, name: str) -> None:
        self._name = name
        self._argv = [
            runtime,
            "events",
            "--filter",
            f"container={name}",
            "--filter",
            "event=die",
            "--filter",
            "event=oom",
            "--filter",
            "event=kill",
            # Not read for its own sake: it is what ends a client whose reader
            # is gone, once the container is removed.
            "--filter",
            "event=destroy",
            # The runtime's filter takes every container whose name begins with
            # this one's, and the helper that spares the main process is such a
            # container.  So each line names the container it is about.
            "--format",
            "{{.Action}} {{.Actor.Attributes.name}}",
        ]
        self._proc: subprocess.Popen[str] | None = None
        self._lock = threading.Lock()
        self._closed = False
        self._died = threading.Event()
        self._heard = False
        self.oom_kills = 0
        self._last_oom_at: float | None = None
        self._last_asked_at: float | None = None
        self._oom_at_death = False
        self._asked_at_death = False

    def start(self) -> None:
        """Start listening.  Returns once the runtime's client runs, or could
        not be started."""
        running = threading.Event()
        threading.Thread(target=self._listen, args=(running,), daemon=True).start()
        running.wait(30)

    def _listen(self, running: threading.Event) -> None:
        try:
            proc = subprocess.Popen(
                self._argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                errors="replace",
                start_new_session=True,
                # Started here, on the thread that reads it, to end with it.
                preexec_fn=_ends_with_its_starter(),
            )
        except (OSError, subprocess.SubprocessError):
            running.set()
            return
        with self._lock:
            self._proc = proc
            closed = self._closed
        running.set()
        if closed:
            # Closed while the client was being started: nobody else ends it.
            self._end(proc)
            return
        self._read(proc)

    def _read(self, proc: subprocess.Popen[str]) -> None:
        assert proc.stdout is not None
        with contextlib.suppress(OSError, ValueError):
            for line in proc.stdout:
                action, _, name = line.strip().partition(" ")
                if name != self._name:
                    # Another container's, whose name begins with this one's.
                    continue
                now = time.monotonic()
                self._heard = True
                if action == "oom":
                    self.oom_kills += 1
                    self._last_oom_at = now
                elif action == "kill":
                    # The runtime was asked, from outside, to signal the container.
                    self._last_asked_at = now
                elif action == "die":
                    self._oom_at_death = (
                        self._last_oom_at is not None
                        and now - self._last_oom_at <= _OOM_AT_DEATH_SECONDS
                    )
                    self._asked_at_death = (
                        self._last_asked_at is not None
                        and now - self._last_asked_at <= _ASKED_BEFORE_DEATH_SECONDS
                    )
                    self._died.set()

    def oom_at_death(self, oom_killed: bool | None, wait: float = 3.0) -> bool:
        """Whether an out-of-memory kill came right before the main process
        ended.  *oom_killed* is the runtime's own flag, used when no event was
        heard at all."""
        self._died.wait(wait)
        return self._oom_at_death if self._heard else bool(oom_killed)

    def ended_from_outside(self, wait: float = 3.0) -> bool:
        """Whether the runtime was told to end the container right before its
        main process ended."""
        self._died.wait(wait)
        return self._asked_at_death

    def close(self) -> None:
        with self._lock:
            self._closed = True
            proc, self._proc = self._proc, None
        if proc is not None:
            self._end(proc)

    @staticmethod
    def _end(proc: subprocess.Popen[str]) -> None:
        with contextlib.suppress(OSError):
            proc.kill()
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            proc.wait(timeout=5)
        if proc.stdout is not None:
            with contextlib.suppress(OSError):
                proc.stdout.close()


def size_bytes(text: str) -> int:
    """A size as the runtime writes it (``"512m"``, ``"2g"``, ``"1048576"``) in bytes."""
    units = {"k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}
    text = text.strip().lower().removesuffix("b")
    if text and text[-1] in units:
        return int(float(text[:-1]) * units[text[-1]])
    return int(float(text))


# The most digits a file's size has: nineteen say every size up to 2**63.
_MOST_SIZE_DIGITS = 19


def _printed_size(text: str) -> int | None:
    """A file's size as a program in the container printed it, ``None`` for
    text that cannot be one.

    Its length is looked at first, then its characters, and only then is it
    made a number.  The program may be the model's, and the conversion raises
    for what the usual test for digits lets through: a number of some
    thousand digits, and digits that are none of the ten.
    """
    if not 0 < len(text) <= _MOST_SIZE_DIGITS or not (text.isascii() and text.isdigit()):
        return None
    return int(text)


def _archived_size(data: bytes) -> int | None:
    """The size of the file that the archive *data* begins with, as its header
    says it; ``None`` when *data* does not begin with a regular file's header.

    Only the header is read, so *data* may end anywhere after it: the runtime's
    copy of a file is cut off long before a large file's content has arrived.
    """
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
            member = archive.next()
    except Exception:
        # Whatever the archive module makes of bytes that are no archive.
        return None
    if member is None or not member.isreg() or member.size < 0:
        return None
    return member.size


class Container:
    """A sandbox container for one rollout, as its :class:`ContainerSpec` says.

    Whatever the spec says: no network, nothing of this machine's mounted into
    it but the three files the runtime itself mounts into every container
    (``RUNTIME_FILES``), an image that is never fetched for it, limits on CPU,
    memory, processes and open files, no core files.

    Every write is bounded.  Memory-backed directories (every directory in the
    spec's ``writable``, every directory the image declares as a volume, and
    shared memory) have a size, and their contents count toward the memory
    limit.  A writable root has a size too, the spec's or
    ``DEFAULT_ROOT_SIZE``: the host's storage driver enforces it where it can,
    and this class watches it where the driver cannot (:attr:`root_bound`).
    The runtime's three files are read-only over a read-only root; over a
    writable root they are asked of the runtime at every look and counted
    toward the root's size, because no storage driver counts them.  A writable
    root that cannot be measured is not started.

    A rollout whose container is lost is not trained on, so ending the
    container must not be something a model can do.  What keeps it from that:

    - The main process is the first process of the container's process
      namespace, to which the kernel delivers no signal from inside that it
      has no handler for.
    - It runs as a user of its own, never the one the commands run as.  One
      process may set another's limits, trace it and write its ``/proc`` files
      only as the same user or with a capability no task may name
      (``REFUSED_CAPABILITIES``).
    - It is ``sleep``, which never runs again once it has started: it starts
      no program, reads no file and uses no processor time, and the kernel
      collects the orphaned processes handed to it.  So neither a limit set
      on it nor a change to the files it came from ends it.
    - It alone is spared by the out-of-memory killer.  A command cannot spare
      itself: what uses up the memory is what is taken.

    What a task's own settings give back (:meth:`_reach`): commands that run
    as root with the capability to become another user can act as the main
    process's user; and in an image that offers no way to start ``sleep`` as
    described, the main process is a shell that waits for a program from the
    image's files, which a writable root lets the commands replace, and which
    root with the capability to signal other users' processes can end, after
    which the shell has to start it again and at the process limit cannot.  In
    such a container the main process's end, when nothing outside explains it,
    is taken to be the model's doing, and :attr:`lost` says why.  Everywhere
    else it is the runtime's or the machine's.
    """

    def __init__(
        self,
        spec: ContainerSpec | str,
        *,
        max_lifetime: int = 3600,
        runtime: str = "docker",
        confirm_timeout: float = 30.0,
    ) -> None:
        self.spec = ContainerSpec(image=spec) if isinstance(spec, str) else spec
        self.spec.validate()
        self.image = self.spec.image
        self.workdir = self.spec.workdir
        self.user = self.spec.user
        self.name = NAME_PREFIX + uuid.uuid4().hex[:12]
        self.max_lifetime = max_lifetime
        self.runtime = runtime
        # Seconds to wait for a removed container to be gone.
        self.confirm_timeout = confirm_timeout
        # After a start that timed out, how long to wait before looking again
        # for a container the runtime may still have been creating.
        self.settle_seconds = 2.0
        # After a removal that could not be confirmed, how long until the
        # keeper outside tries again.
        self.retry_removal_seconds = 30.0
        self._creation_uncertain = False
        self.container_id = ""
        self.started = False
        # ``True`` only once a successful listing showed the container absent.
        self.removed = False
        self.removal_error: str | None = None
        # Why the container is gone although nobody removed it, and whose
        # doing that is (``"model"`` or ``"machinery"``).
        self.lost: str | None = None
        self.lost_cause: str | None = None
        self.exit_code: int | None = None
        # Called once, when the container is first found gone.
        self.on_lost: Callable[[], None] | None = None
        # The user the main process runs as: never the commands' user.
        named = self.user.split(":")[0]
        self.main_user = next(u for u in _MAIN_USERS if u.split(":")[0] != named)
        # What the main process turned out to be: ``"sleep"``, which never
        # runs again, or ``"shell"`` where the image offered no way to that.
        self.main_process: str | None = None
        # The numeric user of the commands, as the container reports it.
        self.task_uid: int | None = None
        self._sleep_writable = False
        # The directories the image declares as volumes, made memory-backed.
        self.volumes: list[str] = []
        self._watch = _StopWatch(self.runtime, self.name)
        self._lifetime = _Lifetime(self.runtime, self.name)
        # What a writable root may hold, and whether the task named that size
        # (``"task"``) or it is the default (``"default"``); both ``None`` for
        # a read-only root.
        self.root_size: str | None = None
        self.root_size_source: str | None = None
        if not self.spec.read_only_root:
            self.root_size = self.spec.root_size or DEFAULT_ROOT_SIZE
            self.root_size_source = "task" if self.spec.root_size else "default"
        # How the size of the root's own file system is kept: ``"enforced"``
        # by the host's storage driver, ``"watched"`` by this class where the
        # driver cannot (the container is stopped once it is over; between two
        # looks it can go over by what it writes in ``size_check_seconds``),
        # ``None`` for a read-only root.  The runtime's three files are
        # watched either way.
        self.root_bound: str | None = "enforced" if self.root_size is not None else None
        self.size_check_seconds = 2.0
        # How long the runtime may fail to say how much a running container
        # holds before the container is stopped: a root that is not measured
        # is not bounded.
        self.size_unknown_seconds = 30.0
        self._size_watch: threading.Thread | None = None
        self._closing = threading.Event()
        # Why this class stopped the container, or is about to: it held more
        # than its root may, the runtime no longer said how much it holds,
        # or the watch over its size was found ended.
        self._over_size: str | None = None
        # What the runtime's three files held when the container started.
        self._runtime_bytes_at_start = 0
        # Held while the size watch looks inside the container, and by
        # whoever holds the container still to list and end what runs in it
        # (``without_looks_inside``).  The look is a process of the
        # container: one under way then would be counted among what was
        # left running and ended with it, and one that is being started at
        # that moment keeps the runtime's list of processes from answering
        # for as long as the container is held.
        self._looking = threading.Lock()
        # What the last look from inside counted.
        self._unlinked_seen = 0

    # -- lifecycle -----------------------------------------------------------

    def _memory_backed(self) -> dict[str, str]:
        """Every memory-backed directory and its size: the image's volumes at
        the spec's volume size, then what the spec names."""
        areas = dict.fromkeys(self.volumes, self.spec.volume_size)
        areas.update(self.spec.writable_areas())
        return areas

    def run_argv(self) -> list[str]:
        """The command that creates the container."""
        spec = self.spec
        argv = [
            self.runtime,
            "run",
            "--detach",
            # Not removed by the runtime when it stops: a stopped container is
            # still read from (what the model left, why it stopped).  It is
            # removed by ``close``, or by its lifetime if the runner is gone.
            # Never fetch an image: a run uses what is there or fails.
            "--pull",
            "never",
            # The main process runs as a user of its own, whatever user the
            # commands have.
            "--user",
            self.main_user,
            # Nothing in the container may lower its own out-of-memory score
            # below this.  The main process's is lowered from outside
            # (``_protect_main_process``).
            "--oom-score-adj",
            "0",
            "--name",
            self.name,
            "--label",
            "turnstone.rollout=1",
            "--network",
            "none",
            "--cpus",
            str(spec.cpus),
            "--memory",
            spec.memory,
            "--memory-swap",
            spec.memory,
            "--pids-limit",
            str(spec.pids_limit),
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--shm-size",
            spec.shm_size,
            "--ulimit",
            "nofile=4096:4096",
            "--ulimit",
            "core=0",
            "--ulimit",
            "memlock=65536:65536",
            "--workdir",
            spec.workdir,
        ]
        for name in spec.capabilities:
            argv += ["--cap-add", name.upper().removeprefix("CAP_")]
        if spec.read_only_root:
            argv.append("--read-only")
        elif self.root_bound == "enforced":
            argv += ["--storage-opt", f"size={self.root_size}"]
        environment = dict(spec.environment)
        if not spec.runs_as_root():
            # A home the user can write to.
            environment.setdefault("HOME", spec.workdir)
        for key, value in environment.items():
            argv += ["--env", f"{key}={value}"]
        for path, size in self._memory_backed().items():
            argv += ["--tmpfs", f"{path}:rw,exec,nosuid,size={size},mode=1777"]
        # The image's own entry point is not used: the main process is ours.
        argv += ["--entrypoint", "sh", self.image, "-c", _MAIN_PROCESS]
        return argv

    def _image_volumes(self) -> list[str]:
        """The directories the image declares as volumes.

        Raises :class:`ContainerError` when the runtime does not say: a volume
        that went unnoticed would be a writable directory on this machine's
        disk, without a size.
        """
        done = run_captured(
            [self.runtime, "image", "inspect", "--format", "{{json .Config.Volumes}}", self.image],
            timeout=60,
            limit=1024 * 1024,
        )
        if done.returncode != 0 or done.timed_out:
            reason = "timed out" if done.timed_out else done.stderr_text[:300]
            raise ContainerError(
                f"could not start the container: the image {self.image} could not be read "
                f"({reason}); an image is never fetched for a run"
            )
        try:
            declared = json.loads(done.stdout or b"")
        except (ValueError, RecursionError) as error:
            raise ContainerError(
                f"could not start the container: what the runtime says about the volumes of "
                f"{self.image} cannot be read ({error})"
            ) from error
        return sorted(declared) if isinstance(declared, dict) else []

    def protect_argv(self) -> list[str]:
        """The command that spares the main process from the out-of-memory killer.

        A helper container that shares this one's process namespace, with the
        two capabilities the one write takes and nothing else: no network, a
        read-only root, gone when its command has run.  The container itself
        never holds these capabilities, so no command in it can spare itself
        or make the main process a candidate again.
        """
        argv = [
            self.runtime,
            "run",
            "--rm",
            "--name",
            f"{self.name}-main",
            "--pid",
            f"container:{self.name}",
            "--pull",
            "never",
            "--label",
            "turnstone.rollout=1",
            "--network",
            "none",
            "--user",
            "0:0",
            "--cap-drop",
            "ALL",
            "--cap-add",
            "SYS_RESOURCE",
            "--cap-add",
            "DAC_OVERRIDE",
            "--security-opt",
            "no-new-privileges",
            "--read-only",
            "--memory",
            "64m",
            "--pids-limit",
            "16",
        ]
        for path in self.volumes:
            argv += ["--tmpfs", f"{path}:size=1m"]
        return [*argv, "--entrypoint", "sh", self.image, "-c", _PROTECT_MAIN_PROCESS]

    def _protect_main_process(self) -> None:
        done = run_captured(self.protect_argv(), timeout=120, limit=8192)
        if done.returncode != 0 or done.timed_out:
            run_captured(
                [self.runtime, "rm", "--force", "--volumes", f"{self.name}-main"],
                timeout=60,
                limit=8192,
            )
            reason = "timed out" if done.timed_out else done.stderr_text[:300]
            raise ContainerError(
                "could not start the container: its main process could not be spared from "
                f"the out-of-memory killer ({reason})"
            )

    def _check_start(self) -> None:
        """Read what the container says about itself, and refuse one a rollout
        cannot rely on."""
        done = self.exec(["sh", "-c", _START_CHECK, "sh", *_REQUIRED_PROGRAMS], timeout=30)
        said: dict[str, list[str]] = {}
        for line in done.stdout.decode("utf-8", errors="replace").splitlines():
            key, _, value = line.partition("=")
            said.setdefault(key, []).append(value)
        if done.returncode != 0 or not {"uid", "main_uid", "comm", "score"} <= set(said):
            raise ContainerError(
                f"could not start the container: it did not answer ({done.stderr_text[:200]})"
            )
        if said.get("missing"):
            raise ContainerError(
                f"image {self.image} lacks what the tools need: {', '.join(said['missing'])}"
            )
        if said["uid"][0] == said["main_uid"][0]:
            raise ContainerError(
                f"could not start the container: its commands would run as user "
                f"{said['uid'][0]}, the user of its main process; name another user"
            )
        if said["score"][0] != "-1000":
            raise ContainerError(
                "could not start the container: its main process is not spared from the "
                f"out-of-memory killer (score {said['score'][0]})"
            )
        try:
            self.task_uid = int(said["uid"][0])
            ignored = int(said.get("ignored", ["0"])[0], 16)
        except ValueError as error:
            raise ContainerError(f"could not start the container: {error}") from error
        asleep = said["comm"][0] == "sleep" and bool(ignored & _SIGCHLD_BIT)
        self.main_process = "sleep" if asleep else "shell"
        self._sleep_writable = "sleep_writable" in said

    def start(self) -> Container:
        self.volumes = self._image_volumes()
        self._watch.start()
        # Kept from outside, so that nothing inside can shorten or stretch it
        # and a killed runner does not leave the container for good.
        self._lifetime.keep_for(self.max_lifetime)
        done = run_captured(self.run_argv(), timeout=120, limit=8192)
        if done.returncode != 0 and not done.timed_out and self.root_bound == "enforced":
            # As a rule this host's storage driver cannot bound a container,
            # which every runtime says in words of its own.  So the start is
            # tried once more without the driver's bound, whatever was said,
            # and the size is watched instead; a start that failed for another
            # reason fails again and says why.
            run_captured(
                [self.runtime, "rm", "--force", "--volumes", self.name], timeout=60, limit=8192
            )
            self.root_bound = "watched"
            done = run_captured(self.run_argv(), timeout=120, limit=8192)
        if done.returncode != 0:
            reason = "timed out" if done.timed_out else done.stderr_text[:500]
            # After a timeout the runtime may still be creating the container.
            self._creation_uncertain = done.timed_out
            self.close()
            raise ContainerError(f"could not start the container: {reason}")
        self.container_id = done.stdout.decode("ascii", errors="replace").strip()
        self.started = True
        try:
            self._abandon_if_closed()
            self._protect_main_process()
            self._check_start()
            self._check_bound()
            self._abandon_if_closed()
        except (ContainerError, ContainerLostError) as error:
            self.close()
            if isinstance(error, ContainerError):
                raise
            raise ContainerError(f"could not start the container: {self.lost}") from error
        if self.root_size is not None:
            self._size_watch = threading.Thread(target=self._watch_size, daemon=True)
            self._size_watch.start()
        return self

    def _abandon_if_closed(self) -> None:
        """Remove a container that was closed while it was still being started.

        Whoever closed it looked for it before the runtime had made it, found
        nothing and stopped the keeper.  Without this the container the runtime
        then made would stay for good.
        """
        if not self._closing.is_set():
            return
        self.removed = False
        gone = False
        with contextlib.suppress(ContainerError, OSError):
            gone = self._remove()
        self.removed = gone
        if not gone:
            with contextlib.suppress(OSError):
                self._lifetime.keep_for(self.retry_removal_seconds)
        raise ContainerError("could not start the container: it was closed while it started")

    def keep_for(self, seconds: float) -> None:
        """Have the keeper outside remove the container *seconds* from now
        instead of when it would have.  For a caller that has more to do with
        the container than its lifetime foresaw."""
        if not self.removed:
            self._lifetime.keep_for(seconds)

    def _unlinked_bytes(self) -> int:
        """Bytes in files of the root file system that were deleted while a
        process holds them open, as far as a look from inside shows them.

        No listing of the file system holds such a file, and it takes room all
        the same.  The look uses the container's own programs, as the
        commands' user and never with more rights than the commands have:
        over a writable root those programs may be the model's.  So a root
        user who replaced them, or a file held open as another user, is not
        seen.

        What comes back is whatever those programs printed.  A line counts
        only where its last field can be a size (``_printed_size``); any other
        line is passed over like one about another device.

        No look is begun while the container is held still to end what was
        left running in it (``without_looks_inside``): what the last look
        counted is given again.
        """
        if not self._looking.acquire(blocking=False):
            return self._unlinked_seen
        try:
            self._unlinked_seen = self._look_inside()
            return self._unlinked_seen
        finally:
            self._looking.release()

    @contextlib.contextmanager
    def without_looks_inside(self, wait: float = 40.0) -> Iterator[None]:
        """For as long as this is entered the size watch begins no look from
        inside the container, and none is under way: one that is, is waited
        for first, for at most *wait* seconds, which is longer than a look
        may take.

        For whoever holds the container still and lists its processes.
        Everything else the watch asks is asked of the runtime, which
        answers for a container it holds still as for any other, so the root
        stays measured and bounded meanwhile.
        """
        held = self._looking.acquire(timeout=wait)
        try:
            yield
        finally:
            if held:
                self._looking.release()

    def _look_inside(self) -> int:
        """One look from inside for :meth:`_unlinked_bytes`."""
        try:
            done = run_captured(
                self.exec_argv(["sh", "-c", _OPEN_FILES_SCRIPT], workdir=OWN_WORKDIR),
                timeout=30,
                limit=1024 * 1024,
            )
        except OSError:
            return 0
        lines = done.stdout.decode("ascii", errors="replace").splitlines()
        if not lines or not lines[0].startswith("root "):
            return 0
        root = lines[0].split()[-1]
        total = 0
        counted: set[str] = set()
        for line in lines[1:]:
            device, links, inode, size = (line.split() + ["", "", "", ""])[:4]
            held = _printed_size(size)
            if device != root or links != "0" or inode in counted or held is None:
                continue
            counted.add(inode)
            total += held
        return total

    def _listed_bytes(self) -> int | str:
        """What the container's own file system holds, as the runtime lists
        it; a text instead when the runtime did not say."""
        try:
            done = run_captured(
                [self.runtime, "inspect", "--size", "--format", "{{.SizeRw}}", self.name],
                timeout=60,
                limit=8192,
            )
        except OSError as error:
            return str(error)[:200]
        if done.timed_out:
            return "the runtime's answer timed out"
        if done.returncode != 0:
            return done.stderr_text[:200] or f"exit code {done.returncode}"
        try:
            return max(int(done.stdout.strip()), 0)
        except ValueError:
            return f"no size in the runtime's answer ({done.stdout[:40]!r})"

    def _runtime_file_bytes(self) -> int | str:
        """What the files the runtime mounts into the container hold
        (``RUNTIME_FILES``); a text instead when the runtime did not say.

        Asked of the runtime, which reads its own files on this machine: the
        size is in the first header of its copy of each file.  Nothing runs
        inside the container, whose programs a root user there may have
        replaced, and nothing is given more rights than the commands have.
        """
        total = 0
        for path in RUNTIME_FILES:
            try:
                done = run_captured(
                    [self.runtime, "cp", f"{self.name}:{path}", "-"],
                    timeout=30,
                    limit=_ARCHIVE_HEAD_BYTES,
                    # The header is at the start; a large file's content is
                    # not waited for.
                    stop_after=_ARCHIVE_HEAD_BYTES,
                )
            except OSError as error:
                return f"{path}: {str(error)[:200]}"
            if done.timed_out:
                return f"{path}: the runtime's copy timed out"
            if done.returncode != 0 and not done.stopped_early:
                said = done.stderr_text
                if any(mark in said.lower() for mark in _NOTHING_THERE_MARKS):
                    # This runtime mounts no such file, so nothing of this
                    # machine's is written through that path.
                    continue
                return f"{path}: {said[:200] or f'exit code {done.returncode}'}"
            size = _archived_size(done.stdout)
            if size is None:
                return f"{path}: the runtime's copy does not begin with the file"
            total += size
        return total

    def _held(self) -> tuple[int, int, int] | str:
        """What counts toward the root's size, in bytes: what the runtime
        lists in the container's own file system, what is in files there that
        were deleted while open, and what the runtime's three files hold.  A
        text instead when the runtime did not say.

        Where the storage driver enforces the size of the container's own file
        system, only the three files are left to count.
        """
        outside = self._runtime_file_bytes()
        if isinstance(outside, str):
            return outside
        if self.root_bound != "watched":
            return 0, 0, outside
        listed = self._listed_bytes()
        if isinstance(listed, str):
            return f"its file system: {listed}"
        return listed, self._unlinked_bytes(), outside

    def _check_bound(self) -> None:
        """Refuse a writable root that cannot be bounded: the runtime has to
        say how much the container holds, now as at every later look.

        And one that holds more than its size before any command has run in
        it.  That size is the task's mistake, a size typed without its unit
        for one.  Started all the same, the container is stopped at the
        watch's first look, and a run that had done nothing but start is
        scored as the model's own destroyed sandbox.
        """
        if self.root_size is None:
            return
        held = self._held()
        if isinstance(held, str):
            raise ContainerError(
                "could not start the container: its writable root cannot be bounded, because "
                f"the runtime does not say how much it holds ({held})"
            )
        self._runtime_bytes_at_start = held[2]
        limit = size_bytes(self.root_size)
        if sum(held) > limit:
            raise ContainerError(
                f"could not start the container: its writable root may hold {limit:,} bytes "
                f"(root_size {self.root_size!r}) and holds {sum(held):,} when it starts, before "
                "any command has run in it; give the task a root_size above what its "
                "container starts with"
            )

    def _watch_size(self) -> None:
        """Stop the container once it holds more than its writable root may,
        or once it could not be measured for too long.

        Nothing bounds a watched root once this has ended, so it ends with the
        container and for no other reason: when the container is closed, or
        once it has stopped it.  A look that raises, whatever it raises, is a
        look that could not measure.  A stop that did not work is tried again
        at the next round.
        """
        limit = size_bytes(self.root_size or "0")
        unknown_since: float | None = None
        while not self._closing.wait(self.size_check_seconds):
            try:
                if self._over_size is None:
                    held: tuple[int, int, int] | str
                    try:
                        held = self._held()
                    except Exception as error:
                        held = f"a look at it failed: {type(error).__name__}: {str(error)[:200]}"
                    if isinstance(held, str):
                        now = time.monotonic()
                        if unknown_since is None:
                            unknown_since = now
                        unknown_for = now - unknown_since
                        if unknown_for < self.size_unknown_seconds or self._closing.is_set():
                            continue
                        # Only a container that runs, on a runtime that
                        # answers: one that has stopped writes nothing more,
                        # and a runtime that says nothing at all is not the
                        # container's doing.
                        if self.state()["running"] is not True:
                            continue
                        self._over_size = (
                            f"the runtime could not say for {unknown_for:.0f} s how much it "
                            f"holds ({held}), and a root that cannot be measured is not bounded"
                        )
                    else:
                        unknown_since = None
                        self._over_size = self._over(held, limit)
                if self._over_size is not None and self.stop():
                    return
            except Exception as error:
                # Asking for the state, or stopping: again at the next round.
                log.warning(
                    "rollout.size_watch_failed",
                    name=self.name,
                    error=f"{type(error).__name__}: {str(error)[:200]}",
                )

    def _over(self, held: tuple[int, int, int], limit: int) -> str | None:
        """By how much what the container holds is over *limit*, in words;
        ``None`` when it is not.  *held* is what :meth:`_held` gave."""
        listed, unlinked, outside = held
        total = listed + unlinked + outside
        if total <= limit:
            return None
        where = []
        if unlinked:
            where.append(f"{unlinked:,} of them in files deleted while open")
        if outside > self._runtime_bytes_at_start:
            where.append(
                f"{outside:,} of them in {', '.join(RUNTIME_FILES)}, which the runtime keeps "
                "on this machine's disk"
            )
        return (
            f"it wrote {total:,} bytes to its file system, more than the {limit:,} it may hold"
            + (f" ({'; '.join(where)})" if where else "")
        )

    def _unwatched(self) -> bool:
        """Whether the watch over a writable root's size has ended although
        the container was neither closed nor stopped by it."""
        watch = self._size_watch
        return (
            watch is not None
            and not watch.is_alive()
            and not self._closing.is_set()
            and self._over_size is None
        )

    def stop(self) -> bool:
        """End the container's processes and keep the container; ``True`` once
        it is not running.  Never raises."""
        try:
            run_captured([self.runtime, "kill", self.name], timeout=60, limit=8192)
            deadline = time.monotonic() + 30
            while self.is_running():
                if time.monotonic() > deadline:
                    return False
                time.sleep(0.2)
        except (OSError, ContainerError):
            return False
        return True

    def state(self) -> dict[str, Any]:
        """What the runtime says about the container's main process: whether it
        runs, its exit code, whether an out-of-memory kill happened inside.

        ``running`` is ``None`` when the runtime did not answer: a question
        that went unanswered is not "stopped".  A container the runtime does
        not know is not running and has no exit code.
        """
        unknown = {"running": None, "exit_code": None, "oom_killed": None}
        try:
            done = run_captured(
                [
                    self.runtime,
                    "inspect",
                    "--format",
                    "{{.State.Running}} {{.State.ExitCode}} {{.State.OOMKilled}}",
                    self.name,
                ],
                timeout=15,
                limit=8192,
            )
        except OSError as error:
            return {**unknown, "unanswered": str(error)[:200]}
        parts = done.stdout.decode("ascii", errors="replace").split()
        if done.returncode == 0 and not done.timed_out and len(parts) == 3:
            try:
                code: int | None = int(parts[1])
            except ValueError:
                code = None
            return {
                "running": parts[0] == "true",
                "exit_code": code,
                "oom_killed": parts[2] == "true",
            }
        if not done.timed_out and "no such" in done.stderr_text.lower():
            return {**unknown, "running": False}
        reason = "timed out" if done.timed_out else done.stderr_text[:200]
        return {**unknown, "unanswered": reason or "no answer"}

    def _answered_state(self, tries: int = 3) -> dict[str, Any]:
        """:meth:`state`, asked again when the runtime did not answer.

        Raises :class:`ContainerError` when it still has not after *tries*.
        """
        state = self.state()
        for _ in range(tries - 1):
            if state["running"] is not None:
                break
            time.sleep(1.0)
            state = self.state()
        if state["running"] is None:
            raise ContainerError(
                f"could not ask about the container: {state.get('unanswered', 'no answer')}"
            )
        return state

    def exists(self) -> bool:
        """Whether the runtime knows the container, running or not.

        Asked by inspecting the container by name, which every runtime with
        this command line answers the same way: success when it is there, a
        "no such" error when it is not.  Raises :class:`ContainerError` for any
        other outcome: an unanswered question is not "absent".
        """
        try:
            done = run_captured(
                [self.runtime, "container", "inspect", "--format", "{{.Id}}", self.name],
                timeout=30,
                limit=8192,
            )
        except OSError as exc:
            raise ContainerError(f"could not run {self.runtime}: {exc}") from exc
        if done.returncode == 0 and not done.timed_out:
            return True
        if not done.timed_out and "no such" in done.stderr_text.lower():
            return False
        reason = "timed out" if done.timed_out else done.stderr_text[:300]
        raise ContainerError(f"could not ask about the container: {reason}")

    def _remove(self) -> bool:
        """One removal and its confirmation; ``True`` once the container is gone."""
        run_captured(
            [self.runtime, "rm", "--force", "--volumes", self.name], timeout=60, limit=8192
        )
        deadline = time.monotonic() + self.confirm_timeout
        while self.exists():
            if time.monotonic() > deadline:
                self.removal_error = f"still there {self.confirm_timeout:g} s after removal"
                return False
            time.sleep(0.2)
        return True

    def close(self) -> bool:
        """Remove the container and its volumes; ``True`` once its absence is
        confirmed.

        Never raises: a finished run must not be lost to its teardown.  When the
        removal cannot be confirmed ``removed`` stays ``False``,
        ``removal_error`` says why, and the keeper outside is left to try again
        shortly: it is stopped only once the container is known to be gone.
        Bounded: the removal command gets 60 s and the confirmation
        ``confirm_timeout``.
        """
        if self.removed:
            return True
        self._closing.set()
        self._watch.close()
        try:
            gone = self._remove()
            if gone and self._creation_uncertain:
                # The start timed out: the runtime may finish creating the
                # container after the first look.  Look once more.
                time.sleep(self.settle_seconds)
                gone = self._remove()
            if gone:
                self.removed = True
                self.removal_error = None
        except (ContainerError, OSError) as exc:
            self.removal_error = str(exc)
        if self.removed:
            self._lifetime.cancel()
        else:
            log.warning("rollout.container_not_removed", name=self.name, error=self.removal_error)
            with contextlib.suppress(OSError):
                self._lifetime.keep_for(self.retry_removal_seconds)
        return self.removed

    def __enter__(self) -> Container:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- running things inside ----------------------------------------------

    def exec_argv(
        self,
        argv: list[str],
        *,
        stdin: bool = False,
        user: str | None = None,
        workdir: str | None = None,
    ) -> list[str]:
        """The command that runs *argv* inside the container, in the working
        directory (or in *workdir*), as the commands' user (or as *user*).

        The task's working directory is for what the task and the model run.
        Where its user owns the directory above it, the model can rename or
        remove it, and the runtime then starts nothing there.  What the
        harness runs for itself is started in ``OWN_WORKDIR``, which no
        command of a task can take away.
        """
        base = [self.runtime, "exec"]
        if stdin:
            base.append("--interactive")
        where = workdir or self.workdir
        return [*base, "--user", user or self.user, "--workdir", where, self.name, *argv]

    def exec(
        self,
        argv: list[str],
        *,
        timeout: float,
        stdin_data: bytes | None = None,
        limit: int = 65536,
        user: str | None = None,
        stop_after: int | None = None,
        workdir: str | None = None,
    ) -> Completed:
        """Run *argv* inside the container to completion.

        Raises :class:`ContainerLostError` when the command could not run
        because the container is gone.
        """
        done = run_captured(
            self.exec_argv(argv, stdin=stdin_data is not None, user=user, workdir=workdir),
            stdin_data=stdin_data,
            timeout=timeout,
            limit=limit,
            stop_after=stop_after,
        )
        if done.returncode != 0 and any(m in done.stderr_text for m in _RUNTIME_ERROR_MARKS):
            self.ensure_running()
        return done

    def is_running(self) -> bool:
        """Whether the container runs.  Raises :class:`ContainerError` when the
        runtime does not answer."""
        return bool(self._answered_state()["running"])

    def _reach(self) -> str | None:
        """How this task's commands can end the main process without the
        runtime seeing why; ``None`` when they cannot.

        Three settings give that back.  Commands that run as root and may
        become another user can act as the main process's user, and then trace
        it where the machine allows tracing.  Where the main process is a
        shell that starts ``sleep`` from the image's files, commands that can
        replace that program run their own in its place, as its user.  And
        where it is such a shell, commands that run as root and may signal
        other users' processes can end the ``sleep`` it waits for; the shell
        then has to start another, and gives up when it cannot, which at the
        process limit is in the commands' hands.
        """
        capabilities = {c.upper().removeprefix("CAP_") for c in self.spec.capabilities}
        root = self.task_uid == 0 if self.task_uid is not None else self.spec.runs_as_root()
        if root and "SETUID" in capabilities:
            return (
                "this task's commands run as root with the SETUID capability, so they can "
                "act as the main process's user"
            )
        replaceable = not self.spec.read_only_root and (root or self._sleep_writable)
        if self.main_process == "shell" and replaceable:
            return (
                "this image's main process is a shell that starts programs from a file "
                "system this task's commands can change"
            )
        if self._ends_what_a_shell_waits_for():
            return (
                "this image's main process is a shell that waits for a program, which this "
                "task's commands, run as root with the KILL capability, can end; at the "
                "process limit the shell cannot start it again"
            )
        return None

    def _ends_what_a_shell_waits_for(self) -> bool:
        """Whether this task's commands can end the main process without acting
        as its user: it is a shell, and they may signal what it waits for."""
        capabilities = {c.upper().removeprefix("CAP_") for c in self.spec.capabilities}
        root = self.task_uid == 0 if self.task_uid is not None else self.spec.runs_as_root()
        return self.main_process == "shell" and root and "KILL" in capabilities

    def commands_can_change_programs(self) -> bool:
        """Whether this task's commands can change the programs in the
        container: root over a writable root.  What a program does there
        afterwards is the model's doing."""
        root = self.task_uid == 0 if self.task_uid is not None else self.spec.runs_as_root()
        return root and not self.spec.read_only_root

    def _can_make_candidate(self) -> bool:
        """Whether this task's commands can make the main process a candidate
        for the out-of-memory killer again: by writing its score, which takes
        its user or the capability to pass file permissions.  Every reach but
        the one by a signal lets them act as its user."""
        capabilities = {c.upper().removeprefix("CAP_") for c in self.spec.capabilities}
        root = self.task_uid == 0 if self.task_uid is not None else self.spec.runs_as_root()
        as_its_user = self._reach() is not None and not self._ends_what_a_shell_waits_for()
        return as_its_user or (root and "DAC_OVERRIDE" in capabilities)

    def _why_stopped(self, state: dict[str, Any]) -> tuple[str, str]:
        """Why the container stopped although nobody here stopped it, and whose
        doing that is: ``"model"`` when the task's settings let its commands
        end the main process and nothing outside explains the end,
        ``"machinery"`` otherwise.  *state* is the runtime's answer."""
        if self._over_size is not None:
            return f"the container was stopped: {self._over_size}", "model"
        code = state["exit_code"]
        if code is None:
            return "the container is gone; the runtime gave no reason", "machinery"
        said = f"the container's main process ended with exit code {code}"
        if self._watch.ended_from_outside():
            return (
                said + ": the runtime was told to end it, from outside the container",
                "machinery",
            )
        reach = self._reach()
        if self._watch.oom_at_death(state["oom_killed"]):
            if self._can_make_candidate():
                return (
                    said + ": the out-of-memory killer took it, which takes making it a "
                    "candidate from inside, as this task's commands can",
                    "model",
                )
            if reach is None:
                return (
                    said + ": the out-of-memory killer took it, although nothing inside this "
                    "container can make it a candidate",
                    "machinery",
                )
        if reach is not None:
            return said + f": nothing outside the container explains it, and {reach}", "model"
        return said, "machinery"

    def ensure_running(self) -> None:
        """Raise :class:`ContainerLostError` unless the container is running.

        A container whose writable root is no longer watched is stopped
        first: the watch ends with the container and for nothing else, and a
        root that is not watched is not bounded.
        """
        if self.lost is None:
            if self._unwatched():
                self._over_size = (
                    "the watch over its size had ended, and a root that is not watched is "
                    "not bounded"
                )
                self.stop()
            try:
                state = self._answered_state()
            except ContainerError as error:
                state = None
                self.lost, self.lost_cause = f"the runtime did not answer ({error})", "machinery"
            if state is not None and not state["running"]:
                self.lost, self.lost_cause = self._why_stopped(state)
                self.exit_code = state["exit_code"]
            if self.lost is not None:
                log.warning(
                    "rollout.container_lost",
                    name=self.name,
                    reason=self.lost,
                    cause=self.lost_cause,
                )
                if self.on_lost is not None:
                    self.on_lost()
        if self.lost is not None:
            raise ContainerLostError(
                errno.EIO,
                "the sandbox has stopped: no further command or file operation can run in it",
            )

    def describe(self) -> dict[str, Any]:
        spec = self.spec
        return {
            "name": self.name,
            "image": self.image,
            "container_id": self.container_id,
            "network": "none",
            "workdir": spec.workdir,
            "user": spec.user,
            "read_only_root": spec.read_only_root,
            "capabilities": list(spec.capabilities),
            "cpus": spec.cpus,
            "memory": spec.memory,
            "pids_limit": spec.pids_limit,
            "writable": {**self._memory_backed(), "/dev/shm": spec.shm_size},
            # The image's volumes, which are among the memory-backed directories.
            "volumes": list(self.volumes),
            # The main process: its user, what it is (``sleep``, or a shell
            # where the image offered no way to that), and how this task's
            # commands could end it unseen, if its settings let them.
            "main_user": self.main_user,
            "main_process": self.main_process,
            "main_process_reach": self._reach(),
            "removed": self.removed,
            "removal_error": self.removal_error,
            "lost": self.lost,
            "lost_cause": self.lost_cause,
            # What the runtime reported: out-of-memory kills inside (a command
            # of the model's, as a rule) and the main process's exit code if it
            # ended.
            "oom_kills": self._watch.oom_kills,
            "exit_code": self.exit_code,
            # A writable root: the size it was bounded to, whether the task
            # named it or it is the default, and how it was kept.
            "root_size": self.root_size,
            "root_size_source": self.root_size_source,
            "root_bound": self.root_bound,
        }


class ContainerEnvironment(ExecutionEnvironment):
    """The shell and file tools of a session, acting inside one container.

    ``op_timeout`` bounds each file operation where it runs, inside the
    container, because stopping the local client does not stop the command
    there.  ``max_file_bytes`` is the largest file a whole-file read returns;
    a larger one is an error that says so, never a silently shortened text.

    Building one runs a command in the container the way the shell tool will
    run the model's (:meth:`_check_shell_tool`).  A container in which that
    does not work is refused with :class:`ContainerError`.
    """

    # Every command and file operation is an ``exec`` into the container.
    acts_on_host = False

    def __init__(
        self,
        container: Container,
        *,
        op_timeout: int = 30,
        max_file_bytes: int = 8 * 1024 * 1024,
    ) -> None:
        self.container = container
        self.op_timeout = op_timeout
        self.max_file_bytes = max_file_bytes
        home = container.exec(["sh", "-c", 'printf %s "$HOME"'], timeout=30)
        self.home = home.stdout.decode("utf-8", errors="replace") or "/"
        self.shell = "bash" if self.which("bash") else "sh"
        # What the file tools may build in the session's process from a file
        # here: an edit's result, and the lines a diff compares.
        self.max_text_chars = max_file_bytes
        self.max_diff_lines = 4000
        # Seconds one kill inside the container may take.
        self.kill_timeout = 10
        # Local client pid -> the script's path inside the container.
        self._scripts: dict[int, str] = {}
        # Kills inside the container that are still under way.
        self._kills: list[threading.Thread] = []
        self._lock = threading.Lock()
        self._check_shell_tool()

    # -- shell ---------------------------------------------------------------

    @staticmethod
    def _script_path() -> str:
        """Where one command's script is put inside the container."""
        return f"/tmp/.turnstone-{uuid.uuid4().hex}.sh"

    def _spawn_argv(self, inside: str) -> list[str]:
        """What runs one command inside the container: its script arrives on
        standard input, is written to *inside* and run by the shell."""
        return ["sh", "-c", _SPAWN_SCRIPT, "sh", inside, self.shell]

    def _check_shell_tool(self) -> None:
        """Run one command the way :meth:`spawn` runs every command of the
        model's, and refuse a container in which that does not work.

        The same script with the same preamble, written to the same place as
        the commands' user and run by the same shell, then tidied up as after
        every command.  In a container where this fails every shell call of
        the model's would fail, and the run would be scored as the model's: a
        task whose ``writable`` leaves ``/tmp`` read-only, a shell that does
        not know the preamble.  Raises :class:`ContainerError`.
        """
        inside = self._script_path()
        script = _script("echo " + _SHELL_CHECK, stop_on_error=False)
        try:
            done = self.container.exec(
                self._spawn_argv(inside), stdin_data=script.encode("utf-8"), timeout=30
            )
        finally:
            self._kill_inside(inside)
        if done.returncode == 0 and done.stdout.strip() == _SHELL_CHECK.encode("ascii"):
            return
        if done.timed_out:
            reason = "the command did not end within 30 s"
        elif done.returncode != 0:
            reason = done.stderr_text[:300] or f"exit code {done.returncode}"
        else:
            reason = f"the command printed {done.stdout[:80]!r}"
        raise ContainerError(
            "could not start the container: the shell tool cannot run in it as user "
            f"{self.container.user} ({reason}). The tool writes each command to /tmp as a "
            "script, so /tmp has to be writable for that user"
        )

    def spawn(
        self,
        command: str,
        *,
        stop_on_error: bool,
        env: dict[str, str] | None,
        stdin: int = subprocess.DEVNULL,
    ) -> tuple[subprocess.Popen[str], int, str]:
        # *env* is this process's environment and *stdin* a local descriptor;
        # neither belongs in the container.
        # What an earlier command left is gone before this one starts.
        self.settle()
        self.container.ensure_running()
        with tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False) as f:
            script_path = f.name
            try:
                f.write(_script(command, stop_on_error))
                # Here, where a failure is caught, not when the file is closed.
                f.flush()
            except BaseException:
                # A command no encoding can write (half of a surrogate pair),
                # or no room for it: the tool reports it; the file must not
                # stay behind.
                os.unlink(script_path)
                raise
        inside = self._script_path()
        try:
            with open(script_path, "rb") as script:
                proc = subprocess.Popen(
                    self.container.exec_argv(self._spawn_argv(inside), stdin=True),
                    stdin=script,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    errors="replace",
                    start_new_session=True,
                )
        except BaseException:
            os.unlink(script_path)
            raise
        with self._lock:
            self._scripts[proc.pid] = inside
        try:
            pgid = os.getpgid(proc.pid)
        except OSError:
            pgid = proc.pid
        return proc, pgid, script_path

    def kill_group(self, proc: subprocess.Popen[str], pgid: int) -> None:
        """Stop the command *proc* stands for, inside the container and here.

        Returns at once.  The kill inside the container is a command of its
        own and can take as long as the runtime takes to answer, and callers
        hold locks across this call, so it runs on a thread; :meth:`settle`
        waits for it, and the next command does that before it starts.
        """
        with self._lock:
            inside = self._scripts.pop(proc.pid, None)
        if inside is not None:
            worker = threading.Thread(target=self._kill_inside, args=(inside,), daemon=True)
            with self._lock:
                self._kills.append(worker)
            worker.start()
        # Only a client that has not been collected: once it has, its
        # process-group id may belong to something else.
        signal_group(proc, pgid, signal.SIGKILL)

    def _kill_inside(self, inside: str) -> None:
        # A container that is gone has nothing left to kill; ``lost`` records it.
        # The harness's own: the kill has to start whatever the command it
        # ends did to the working directory.
        with contextlib.suppress(ContainerLostError, OSError):
            self.container.exec(
                ["sh", "-c", _KILL_SCRIPT, "sh", inside],
                timeout=self.kill_timeout,
                workdir=OWN_WORKDIR,
            )

    def settle(self, timeout: float = 10.0) -> bool:
        """Wait for the kills still under way; ``True`` when none is left."""
        deadline = time.monotonic() + timeout
        with self._lock:
            pending = list(self._kills)
        for worker in pending:
            worker.join(timeout=max(0.0, deadline - time.monotonic()))
        with self._lock:
            self._kills = [w for w in self._kills if w.is_alive()]
            return not self._kills

    def process_label(self, proc: subprocess.Popen[str]) -> str:
        # The local client's process id is this machine's, not the command's.
        return ""

    # -- search --------------------------------------------------------------

    def which(self, program: str) -> str | None:
        done = self.container.exec(["sh", "-c", 'command -v "$1"', "sh", program], timeout=30)
        found = done.stdout.decode("utf-8", errors="replace").strip()
        return found if done.returncode == 0 and found else None

    def popen_search(
        self, args: list[str], *, env: dict[str, str] | None, timeout: float
    ) -> subprocess.Popen[bytes]:
        self.container.ensure_running()
        # Bounded where it runs, one second after the caller's own deadline.
        inner = ["timeout", "-s", "KILL", str(int(timeout) + 1), *args]
        return subprocess.Popen(
            self.container.exec_argv(inner),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    # -- paths ---------------------------------------------------------------

    def getcwd(self) -> str:
        return self.container.workdir

    def expanduser(self, path: str) -> str:
        if path == "~" or path.startswith("~/"):
            return self.home.rstrip("/") + path[1:]
        return path

    def abspath(self, path: str) -> str:
        return posixpath.normpath(posixpath.join(self.container.workdir, path))

    def realpath(self, path: str) -> str:
        # Path questions never fail on a stopped container, like their host
        # counterparts never raise; the read or write that follows reports it.
        try:
            done = self.container.exec(
                ["sh", "-c", 'realpath -m -- "$1" 2>/dev/null || readlink -f -- "$1"', "sh", path],
                timeout=self.op_timeout,
            )
        except ContainerLostError:
            return self.abspath(path)
        resolved = done.stdout.decode("utf-8", errors="surrogateescape")
        if resolved.endswith(_NL):
            resolved = resolved[:-1]
        if done.returncode != 0 or not resolved.startswith("/"):
            # Neither program could resolve it (a path whose directories do not
            # exist yet, on an image without the lenient mode).
            return self.abspath(path)
        return resolved

    def exists(self, path: str) -> bool:
        try:
            done = self.container.exec(["test", "-e", path], timeout=self.op_timeout)
        except ContainerLostError:
            return False
        return done.returncode == 0

    def stat(self, path: str) -> os.stat_result:
        done = self.container.exec(
            ["sh", "-c", '[ -e "$1" ] || exit 44; exec stat -L -c "%f %s" -- "$1"', "sh", path],
            timeout=self.op_timeout,
        )
        self._raise_for(done, path)
        mode, size = done.stdout.split()
        return os.stat_result((int(mode, 16), 0, 0, 1, 0, 0, int(size), 0, 0, 0))

    # -- content -------------------------------------------------------------

    def _raise_for(self, done: Completed, path: str) -> None:
        """Turn a failed helper into the exception the host call would raise."""
        code = done.returncode
        if code == 0 and not done.timed_out:
            return
        if code == _RC_MISSING:
            raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), path)
        if code == _RC_DIRECTORY:
            raise IsADirectoryError(errno.EISDIR, os.strerror(errno.EISDIR), path)
        if code == _RC_UNREADABLE:
            raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), path)
        if code == _RC_TOO_LARGE:
            raise OSError(
                errno.EFBIG,
                f"file is larger than {self.max_file_bytes} bytes; read part of it with a "
                "shell command",
                path,
            )
        if code == _RC_NOT_REGULAR:
            raise OSError(errno.EINVAL, "not a regular file", path)
        if code == _RC_NO_PARENT:
            raise OSError(errno.EACCES, "could not create the directory", path)
        if done.timed_out or code == 128 + signal.SIGKILL:
            raise TimeoutError(
                errno.ETIMEDOUT, f"operation did not finish within {self.op_timeout} s", path
            )
        text = done.stderr_text
        for number in _KNOWN_ERRNOS:
            if os.strerror(number) in text:
                raise OSError(number, os.strerror(number), path)
        raise OSError(errno.EIO, text[:300] or f"exit code {code}", path)

    def _read(self, path: str, mode: str, count: int) -> bytes:
        done = self.container.exec(
            ["sh", "-c", _READ_SCRIPT, "sh", path, str(self.op_timeout), mode, str(count)],
            timeout=self.op_timeout + 15,
            limit=count + 1,
        )
        self._raise_for(done, path)
        if mode == "whole" and done.stdout_total > count:
            # It grew between the size check and the read.
            raise OSError(errno.EFBIG, "file is growing while it is read", path)
        return done.stdout[:count] if mode == "head" else done.stdout

    def read_bytes(self, path: str, limit: int) -> bytes:
        return self._read(path, "head", limit)

    def read_text(self, path: str) -> str:
        data = self._read(path, "whole", self.max_file_bytes)
        return io.TextIOWrapper(io.BytesIO(data), encoding="utf-8").read()

    def read_lines(self, path: str) -> list[str]:
        data = self._read(path, "whole", self.max_file_bytes)
        return io.TextIOWrapper(io.BytesIO(data), encoding="utf-8").readlines()

    def put_files(self, files: dict[str, str]) -> None:
        """Write a task's setup files; relative paths land in the working directory."""
        for path, content in files.items():
            self.write_text(path, content, make_parents=True)

    def write_text(
        self, path: str, content: str, *, append: bool = False, make_parents: bool = False
    ) -> None:
        done = self.container.exec(
            [
                "sh",
                "-c",
                _WRITE_SCRIPT,
                "sh",
                path,
                str(self.op_timeout),
                "a" if append else "w",
                "parents" if make_parents else "",
            ],
            stdin_data=content.encode("utf-8"),
            timeout=self.op_timeout + 15,
        )
        self._raise_for(done, path)
