"""Guards that keep a test from acting on the machine it runs on.

A test that hands a destructive command to an execution environment and counts
on the code under test to refuse it runs that command for real on the day the
code under test is broken: the developer's machine, or the CI runner, takes it
before any assertion can fail.  The guards here do not depend on the code under
test, so they hold on that day too.

They stand where a process is started, so that every caller meets them,
however it came by the function it calls.  :func:`judge_every_start` puts a
check in front of each way this interpreter starts a program:
``subprocess.Popen``, which ``run``, ``call``, ``check_call``,
``check_output``, ``os.popen`` and an event loop's subprocesses all go
through, and ``os.system``, ``os.exec*``, ``os.spawn*`` and
``os.posix_spawn*``.  A check is handed one :class:`Start`: the words the
program is given, the file the system would start for them, and the module
whose code asked.  ``conftest.py`` installs its checks for every test.

:func:`refuse_destructive_starts` knows a short list of command shapes that
hurt the machine they run on (:func:`destructive_shape`).  A shell that would
be started here with such a command, as its ``-c`` text or in the script file
it is given, is refused before a process exists, and so is such a program
started without a shell; the test fails.  ``conftest.py`` also wraps the
host's own process start with :func:`guarded_spawn`, which refuses the same
commands before their script is written.

:func:`judge_every_signal` stands in ``os.kill`` and ``os.killpg``, which a
process object's ``kill``, ``terminate`` and ``send_signal`` send through:
a signal that could reach a process the test did not start is refused
before it is sent (:func:`signal_refusal`).

The list is deliberately wider than what is dangerous: a test that trips it
with a harmless command picks another command.  It is not a safety mechanism
for anything but tests.

:func:`refuse_all_but_the_runtime` is for a package whose commands must all
run somewhere else, through one program (a container runtime).  Whatever
module of that package asks for a process, and by whichever call, the program
is started only when it is the runtime, used on a container of the package's
own (:func:`runtime_refusal`), the package's keeper in the one shape it is
started in (:func:`rollout_refusal`), or a program the test wrote under its
temporary directory.  A command line that lost its "run this inside the container"
prefix fails instead of running here.  :func:`only_these_programs` is the
same check standing in for the ``subprocess`` module inside a module of the
package.

What the guards do not see:

- what a process the test starts does in its turn: a child interpreter,
  whether it is given code as text, a script file or a module, and a
  program the test wrote under its temporary directory, which
  :func:`rollout_refusal` lets through with any arguments;
- the commands a shell is fed through its standard input (``sh -s``, or a
  shell started without a command and without a script file), a shell the
  list does not name (``fish``, ``csh``, a copy of a shell under another
  name), and a file a shell reads before its command (``BASH_ENV``,
  ``--rcfile``);
- a program not on the list that starts what its arguments spell
  (``flock``, ``nsenter``, ``unshare``, ``chroot``, ``script``,
  ``find -exec``, ``env -S``);
- a process started without a new program (``os.fork``,
  ``multiprocessing``), by a C extension, or by a ``preexec_fn``;
- for :func:`refuse_all_but_the_runtime`, a start made for the package by
  other code: by the framework's shell tool where the package's
  environment is the host's, or from a thread that runs only
  standard-library code, which is judged as that code's, so that only the
  list applies;
- a signal sent other than through ``os.kill`` and ``os.killpg``
  (``posix.kill``, ``signal.pidfd_send_signal``, a function taken by name
  before the test began, a C extension), a signal a started process sends,
  and an id that a process gives up and another takes between the check
  and the signal;
- an id taken again within one test: once a process the test started has
  ended and its group or session has emptied, an unrelated process can
  take the freed id, make a group or session of its own with it and end,
  and the members it leaves then count as the test's (:class:`ProcessView`);
- a test's own stand-in for ``os.kill``, ``os.killpg`` or
  ``Popen.__init__``, which takes that guard's place for as long as it
  stands, and what session- and module-scoped fixtures and the collection
  of the tests do, which run outside the guards;
- a file operation this interpreter performs itself.

What no guard sees of signals and files is answered by running the suite
in a process namespace of its own, with the file system read-only but for
a temporary directory.  The container runtime is not answered so: its
socket stays reachable there, and a runtime command from a path no guard
judges can still act on another container or mount a directory of the
machine into one.
"""

from __future__ import annotations

import functools
import inspect
import os
import re
import subprocess
import sys
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

# Paths whose recursive removal or change of ownership nobody wants on the
# machine a test runs on: the root, a home, a top-level directory or one
# directly below it.
_TOP = "bin|boot|dev|etc|home|lib|lib32|lib64|mnt|opt|proc|root|run|sbin|srv|sys|tmp|usr|var"
_BROAD_PATH = re.compile(
    r"^(?:/|/[*]|~|~/|[$]HOME/?|[$][{]HOME[}]/?|/(?:" + _TOP + r")(?:/[^/]*)?/?)$"
)
# Words a shell puts before the program of a simple command.
_BEFORE_PROGRAM = frozenset(
    {"sudo", "exec", "nohup", "time", "command", "then", "do", "else", "!", "{", "("}
)
_SIGNAL_PROGRAMS = frozenset({"kill", "pkill", "killall"})
_RECURSIVE_PROGRAMS = frozenset({"rm", "chown", "chgrp", "chmod"})
_POWER_PROGRAMS = frozenset({"shutdown", "reboot", "halt", "poweroff", "kexec"})
_POWER_UNITS = frozenset({"poweroff", "reboot", "halt", "suspend", "hibernate", "kexec"})
_DISK_PROGRAMS = frozenset({"wipefs", "fdisk", "sfdisk", "sgdisk", "parted", "blkdiscard"})
_PRIVILEGE_PROGRAMS = frozenset({"sudo", "su", "doas", "pkexec"})
_HARMLESS_DEVICES = ("null", "zero", "full", "stdout", "stderr", "tty", "fd/")
_FORK_BOMB = re.compile(r":\s*[(]\s*[)]\s*[{]")
_DEVICE_REDIRECT = re.compile(r">\s*/dev/(?:sd|nvme|vd|xvd|hd|dm-|md|loop|disk|mapper)")


class DestructiveCommandError(AssertionError):
    """A test was about to start a destructive command on this machine."""


def _recursive(word: str) -> bool:
    if word in ("--recursive", "--no-preserve-root"):
        return True
    return word.startswith("-") and not word.startswith("--") and ("r" in word or "R" in word)


def _segment_shape(words: list[str]) -> str | None:
    """What is destructive about one simple command, given as its words."""
    names = [w.rsplit("/", 1)[-1] for w in words]
    program = next((n for n in names if n not in _BEFORE_PROGRAM), "")
    if any(n in _PRIVILEGE_PROGRAMS for n in names):
        return "it asks for another user's rights"
    if "killall5" in names:
        return "it signals every process"
    if any(n in _SIGNAL_PROGRAMS for n in names) and any(w in ("-1", "1") for w in words):
        return "it signals every process, or the first one"
    changes_a_tree = any(n in _RECURSIVE_PROGRAMS for n in names) and any(
        _recursive(w) for w in words
    )
    if changes_a_tree and any(_BROAD_PATH.match(w) for w in words):
        return "it changes a whole directory tree of this machine"
    if "mv" in names:
        operands = [w for w in words[names.index("mv") + 1 :] if not w.startswith("-")]
        if operands and _BROAD_PATH.match(operands[0]):
            return "it moves a whole directory tree of this machine"
    if program in _POWER_PROGRAMS:
        return "it turns the machine off or restarts it"
    if "systemctl" in names and any(w in _POWER_UNITS for w in words):
        return "it turns the machine off or restarts it"
    if program in ("init", "telinit") and any(w in ("0", "6") for w in words):
        return "it turns the machine off or restarts it"
    if any(n in _DISK_PROGRAMS or n.startswith("mkfs") for n in names):
        return "it writes to a disk device"
    for word in words:
        if word.startswith("of=/dev/") and not word[8:].startswith(_HARMLESS_DEVICES):
            return "it writes to a disk device"
    if "git" in names:
        rest = words[names.index("git") + 1 :]
        if "clean" in rest or ("reset" in rest and "--hard" in rest):
            return "it discards work in a git checkout"
        if "push" in rest and any(w in ("--force", "-f") for w in rest):
            return "it overwrites a git remote"
    return None


def destructive_shape(command: str) -> str | None:
    """Why *command* must not be started on a machine a test runs on, ``None``
    when nothing in the list matches it."""
    if _FORK_BOMB.search(command):
        return "it starts processes without end"
    if _DEVICE_REDIRECT.search(command):
        return "it writes to a disk device"
    for segment in re.split(r"[;&|" + chr(10) + r"]+", command):
        # Quotes, parentheses and backticks around a word do not hide it.
        words = [w.strip("'\"()`{}") for w in segment.split()]
        shape = _segment_shape([w for w in words if w])
        if shape is not None:
            return shape
    return None


# -- every process start ------------------------------------------------------

_NL = chr(10)
# Programs that run a command given to them as text or as a script file.
_SHELLS = frozenset({"sh", "bash", "dash", "ash", "zsh", "ksh", "mksh"})
# Programs that start, on this machine, the command their arguments spell.
_STARTS_ITS_ARGUMENTS = frozenset(
    {
        "env",
        "nohup",
        "nice",
        "ionice",
        "timeout",
        "setsid",
        "stdbuf",
        "time",
        "chrt",
        "taskset",
        "xargs",
        "busybox",
    }
)
# Programs that are destructive without a shell around them, by their own
# arguments.
_DIRECT_PROGRAMS = (
    _SIGNAL_PROGRAMS
    | _RECURSIVE_PROGRAMS
    | _POWER_PROGRAMS
    | _DISK_PROGRAMS
    | _PRIVILEGE_PROGRAMS
    | {"killall5", "mv", "dd", "systemctl", "init", "telinit", "git"}
)
# Options of a shell that are followed by a value of their own.
_SHELL_OPTIONS_WITH_VALUE = frozenset({"-o", "+o", "-O", "+O", "--rcfile", "--init-file"})
# The most of a script file that is read to judge it.
_SCRIPT_BYTES = 1024 * 1024


@dataclass(frozen=True)
class Start:
    """One program some code is about to start on this machine."""

    # The call that asked: ``"subprocess.Popen"``, ``"os.system"``, ...
    how: str
    # The words the program is given, its own name first.
    argv: tuple[str, ...]
    # The file the system would start for them, as an absolute path; ``""``
    # when it would find none.
    program: str
    # The module whose code asked.
    caller: str
    # The directory the program starts in, where the caller named one.
    cwd: str | None = None

    def shown(self) -> str:
        return " ".join(self.argv)[:200] or f"({self.how}, arguments that could not be read)"


def _text(value: Any) -> str:
    """*value* as the system takes it for one word of a command line."""
    try:
        return os.fsdecode(value)
    except TypeError:
        return str(value)


def started_program(
    name: str,
    *,
    cwd: str | None = None,
    env: Mapping[Any, Any] | None = None,
    search: bool = True,
) -> str:
    """The file the system starts for *name*, as an absolute path; ``""`` when
    it finds none.

    A name with a directory in it is that file, read from the directory the
    program starts in.  A bare name is looked for along the search path (the
    ``PATH`` of *env* where the caller gave an environment), unless the call
    takes a path and does not *search*.  It is not read against the working
    directory: there a bare ``sh`` would be whatever file of that name a
    test's own directory holds, while the system starts the shell.
    """
    try:
        base = os.path.abspath(cwd) if cwd is not None else os.getcwd()
    except OSError:
        base = os.sep
    if os.path.dirname(name) or not search:
        return os.path.normpath(os.path.join(base, name))
    try:
        directories = os.get_exec_path(env)
    except (TypeError, ValueError, AttributeError):
        return ""
    for directory in directories:
        candidate = os.path.normpath(os.path.join(base, _text(directory), name))
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return ""


def _caller() -> str:
    """The module whose code asked for a process: the innermost caller that is
    neither the standard library's nor this module's."""
    frame = sys._getframe(1)
    while frame is not None:
        name = str(frame.f_globals.get("__name__", ""))
        if name != __name__ and name.partition(".")[0] not in sys.stdlib_module_names:
            return name
        frame = frame.f_back
    return ""


_POPEN_PARAMETERS = inspect.signature(subprocess.Popen.__init__)


def popen_start(
    how: str, popenargs: tuple[Any, ...], kwargs: dict[str, Any], caller: str
) -> Start | None:
    """What ``subprocess.Popen`` would start, given what it was called with.

    ``None`` for arguments it does not take: it refuses those itself, before
    anything starts.
    """
    known = {key: value for key, value in kwargs.items() if key in _POPEN_PARAMETERS.parameters}
    try:
        given = _POPEN_PARAMETERS.bind_partial(None, *popenargs, **known).arguments
    except TypeError:
        return None
    if "args" not in given:
        return None
    args = given["args"]
    if isinstance(args, (str, bytes, os.PathLike)):
        words = [args]
    else:
        try:
            words = list(args)
        except TypeError:
            return None
    argv = [_text(word) for word in words]
    executable = given.get("executable")
    if given.get("shell"):
        # The system's shell is given the first word as its command.
        argv = ["/bin/sh", "-c", *argv]
        if executable:
            argv[0] = _text(executable)
    if not argv:
        return None
    cwd = given.get("cwd")
    where = _text(cwd) if cwd is not None else None
    program = started_program(
        _text(executable) if executable else argv[0], cwd=where, env=given.get("env")
    )
    return Start(how, tuple(argv), program, caller, where)


# How each of ``os``'s ways to start a program takes its arguments: whether a
# mode comes first, whether the words come one by one, whether an environment
# comes last, and whether the program is looked for along the search path.
_OS_STARTS: dict[str, tuple[bool, bool, bool, bool]] = {
    "execv": (False, False, False, False),
    "execve": (False, False, True, False),
    "execvp": (False, False, False, True),
    "execvpe": (False, False, True, True),
    "execl": (False, True, False, False),
    "execle": (False, True, True, False),
    "execlp": (False, True, False, True),
    "execlpe": (False, True, True, True),
    "spawnv": (True, False, False, False),
    "spawnve": (True, False, True, False),
    "spawnvp": (True, False, False, True),
    "spawnvpe": (True, False, True, True),
    "spawnl": (True, True, False, False),
    "spawnle": (True, True, True, False),
    "spawnlp": (True, True, False, True),
    "spawnlpe": (True, True, True, True),
    "posix_spawn": (False, False, True, False),
    "posix_spawnp": (False, False, True, True),
}


def os_start(how: str, args: tuple[Any, ...], kwargs: dict[str, Any], caller: str) -> Start:
    """What one of ``os``'s calls would start, given what it was called with.

    Arguments that cannot be read make a start of nothing known, which a check
    that lets through only what it knows refuses.
    """
    name = how.removeprefix("os.")
    words = list(args)
    try:
        if name == "system":
            command = words[0] if words else kwargs["command"]
            return Start(how, ("/bin/sh", "-c", _text(command)), "/bin/sh", caller)
        mode_first, one_by_one, env_last, search = _OS_STARTS[name]
        if mode_first:
            words = words[1:]
        # By position, as these calls are made; then by the names they know.
        words += [kwargs[key] for key in ("path", "file", "argv", "args", "env") if key in kwargs]
        file, rest = _text(words[0]), words[1:]
        env = None
        if one_by_one:
            if env_last:
                env = rest.pop()
            argv = rest
        else:
            argv = list(rest[0])
            if env_last:
                env = rest[1]
        if name == "posix_spawnp":
            # It searches along the caller's own path, not the new environment's.
            env = None
        program = started_program(file, env=env, search=search)
        return Start(how, tuple(_text(word) for word in argv) or (file,), program, caller)
    except (TypeError, KeyError, IndexError):
        return Start(how, (), "", caller)


_judging = threading.local()
# The modes in which ``os.spawn*`` do not wait for the process they start.
_NO_WAIT = frozenset(getattr(os, name) for name in ("P_NOWAIT", "P_NOWAITO") if hasattr(os, name))


def judge_every_start(
    monkeypatch: Any,
    judges: list[Callable[[Start], None]],
    started: dict[int, int | None] | None = None,
) -> None:
    """Put *judges* in front of every way this interpreter starts a program,
    until *monkeypatch* undoes it.

    Each is called with the :class:`Start` before anything of it exists, and
    raises to refuse it.  The check stands in ``subprocess.Popen`` itself and
    in the ``os`` module's own functions, not in a module that uses them, so a
    caller meets it whatever it imported and under whatever name.  One start
    is judged once: where one of these calls makes another (``subprocess``
    spawning through ``os``, ``os.execlp`` trying one directory after the
    other), the outermost is the one that asks.  Each process that was
    started is noted in *started*, with when it started (:func:`started_at`).
    """

    def guarded(real: Any, how: str, read: Any, pid_of: Any) -> Any:
        @functools.wraps(real)
        def start(*args: Any, **kwargs: Any) -> Any:
            depth = getattr(_judging, "depth", 0)
            if depth == 0:
                asked = read(how, args, kwargs, _caller())
                if asked is not None:
                    for judge in list(judges):
                        judge(asked)
            _judging.depth = depth + 1
            try:
                result = real(*args, **kwargs)
            finally:
                _judging.depth = depth
            pid = pid_of(args, result)
            if started is not None and type(pid) is int and pid > 0:
                started[pid] = started_at(pid)
            return result

        return start

    def read_popen(
        how: str, args: tuple[Any, ...], kwargs: dict[str, Any], caller: str
    ) -> Start | None:
        return popen_start(how, args[1:], kwargs, caller)  # without the instance

    def popen_pid(args: tuple[Any, ...], _result: Any) -> Any:
        return getattr(args[0], "pid", None) if args else None

    def spawned_pid(args: tuple[Any, ...], result: Any) -> Any:
        # Without waiting, ``os.spawn*`` answer the id of the process.
        return result if args and args[0] in _NO_WAIT else None

    def returned_pid(_args: tuple[Any, ...], result: Any) -> Any:
        return result

    def no_pid(_args: tuple[Any, ...], _result: Any) -> Any:
        return None

    monkeypatch.setattr(
        subprocess.Popen,
        "__init__",
        guarded(subprocess.Popen.__init__, "subprocess.Popen", read_popen, popen_pid),
    )
    for name in ("system", *_OS_STARTS):
        real = getattr(os, name, None)
        if real is None:
            continue
        if name.startswith("posix_spawn"):
            pid_of = returned_pid
        elif name.startswith("spawn"):
            pid_of = spawned_pid
        else:
            pid_of = no_pid
        monkeypatch.setattr(os, name, guarded(real, "os." + name, os_start, pid_of))


# -- what a start would run in a shell ----------------------------------------


def _names(start: Start) -> set[str]:
    """The names the program of *start* goes by: as it was written, as the
    system finds it, and what that file really is."""
    names = {os.path.basename(start.argv[0])} if start.argv else set()
    if start.program:
        names.add(os.path.basename(start.program))
        names.add(os.path.basename(os.path.realpath(start.program)))
    return names


def _file_text(path: str) -> str | None:
    """The beginning of the regular file at *path* as text, ``None`` when it
    is no such file or cannot be read."""
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "rb") as handle:
            return handle.read(_SCRIPT_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return None


def _given_to_a_shell(words: list[str], cwd: str | None) -> list[str]:
    """The commands a shell runs that is started with *words* after its name:
    the text that follows ``-c``, or what the script file it is given holds."""
    command = False
    operand: str | None = None
    rest = iter(words)
    for word in rest:
        if word in _SHELL_OPTIONS_WITH_VALUE:
            next(rest, None)
        elif word == "--":
            operand = next(rest, None)
            break
        elif len(word) > 1 and word[0] in "-+":
            # ``-c`` alone or among other letters (``-ec``).
            command = command or (word[0] == "-" and word[1] != "-" and "c" in word)
        else:
            operand = word
            break
    if operand is None:
        return []
    if command:
        return [operand]
    content = _file_text(os.path.join(cwd, operand) if cwd else operand)
    return [] if content is None else [content]


def _shell_script(path: str) -> str | None:
    """What the file at *path* holds, where the system would run it through a
    shell because its first line names one; ``None`` for any other file."""
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "rb") as handle:
            marked = handle.read(2) == b"#!"
    except OSError:
        return None
    content = _file_text(path) if marked else None
    if content is None:
        return None
    interpreter = content.split(_NL, 1)[0][2:].split()
    if interpreter and os.path.basename(interpreter[0]) == "env":
        interpreter = [w for w in interpreter[1:] if not w.startswith("-") and "=" not in w]
    if not interpreter or os.path.basename(interpreter[0]) not in _SHELLS:
        return None
    return content


def shell_texts(start: Start) -> list[str]:
    """The shell commands *start* would run on this machine: the text after a
    shell's ``-c``, what a script file holds that a shell is given, and what a
    started file holds that names a shell as its interpreter."""
    names = _names(start)
    if names & _SHELLS:
        return _given_to_a_shell(list(start.argv[1:]), start.cwd)
    if names & _STARTS_ITS_ARGUMENTS:
        for index, word in enumerate(start.argv):
            if index and os.path.basename(word) in _SHELLS:
                return _given_to_a_shell(list(start.argv[index + 1 :]), start.cwd)
    script = _shell_script(start.program) if start.program else None
    return [] if script is None else [script]


def start_shape(start: Start) -> str | None:
    """Why *start* must not happen on a machine a test runs on, ``None`` when
    nothing in the list matches it.

    Judged by what it would run in a shell here (:func:`shell_texts`) and,
    for a program that is destructive without a shell around it, by its own
    words.  A program that carries its arguments somewhere else (a container
    runtime) is neither.
    """
    for text in shell_texts(start):
        shape = destructive_shape(text)
        if shape is not None:
            return shape
    names = _names(start)
    if names & (_DIRECT_PROGRAMS | _STARTS_ITS_ARGUMENTS) or any(
        name.startswith("mkfs") for name in names
    ):
        return _segment_shape(list(start.argv))
    return None


def refuse_destructive_starts(tripped: list[str]) -> Callable[[Start], None]:
    """A check for :func:`judge_every_start`: a start the list knows
    (:func:`start_shape`) is noted in *tripped* and refused."""

    def judge(start: Start) -> None:
        shape = start_shape(start)
        if shape is not None:
            shown = start.shown()
            tripped.append(f"{shown!r}: {shape}")
            raise DestructiveCommandError(
                f"a test was about to run a command on this machine that must never run "
                f"here ({shape}): {shown!r}"
            )

    return judge


def guarded_spawn(real: Any, tripped: list[str]) -> Any:
    """*real* (a function whose first argument is a shell command) behind the
    list: a destructive command is noted in *tripped* and refused."""

    @functools.wraps(real)
    def spawn(command: str, *args: Any, **kwargs: Any) -> Any:
        shape = destructive_shape(command)
        if shape is not None:
            tripped.append(f"{command[:200]!r}: {shape}")
            raise DestructiveCommandError(
                f"a test was about to run a command on this machine that must never run "
                f"here ({shape}): {command[:200]!r}"
            )
        return real(command, *args, **kwargs)

    return spawn


# -- signals ------------------------------------------------------------------

# What the system says of a process id, for :func:`signal_refusal`.
ABSENT = "absent"  # no process has it
OURS = "ours"  # the test process, a process it started, or one of theirs
OTHER = "other"  # a process that is none of those
UNKNOWN = "unknown"  # the system does not say
# How many parents up a process is followed to the test process at most.
_GENERATIONS = 4096
# The variable of the test's environment whose value marks the processes the
# test starts (:class:`ProcessView`).
PROCESS_MARK = "TURNSTONE_TEST_PROCESS_MARK"
# What the signal guard asks the system with, taken when this module is
# imported: a test's own stand-in for one of them changes nothing it sees.
_getpid, _getpgrp, _getsid = os.getpid, os.getpgrp, os.getsid
_listdir, _isdir, _open = os.listdir, os.path.isdir, open


def _is_probe(signum: object) -> bool:
    """Whether *signum* is 0, which asks whether a target exists and may be
    signalled, and delivers nothing.  Only an ``int`` itself is taken as 0:
    a subclass may say 0 when asked and be another number to the system."""
    return type(signum) is int and signum == 0


def signal_refusal(
    how: str,
    target: object,
    signum: object,
    *,
    me: int,
    group: int,
    session: int,
    whose: Callable[[int], str],
    members: Callable[[int], list[int] | None],
) -> str | None:
    """Why the test process may not send *signum* to *target* by *how*
    (``"os.kill"`` or ``"os.killpg"``), ``None`` when it may.

    *me* is the test process, *group* and *session* the ids of its own
    process group and session.  *whose* says of a process id whether no
    process has it (:data:`ABSENT`), whether the process is :data:`OURS` or
    :data:`OTHER`, or that the system does not say (:data:`UNKNOWN`);
    *members* lists the processes of a group, ``None`` where the system does
    not say.  Refused:

    - a target that is not an ``int``: a ``bool``, or anything that merely
      converts to one, as a stand-in object's id does (to 1);
    - an id of 1 or below, whatever the signal, 0 included: for ``os.kill``
      -1 is every process the test may signal, 0 the test process's own
      group, any other negative id a whole group, 1 the first process; for
      ``os.killpg`` 1 is every process and 0 the test process's own group;
    - the test process's own group or its session's, as a group;
    - a signal other than 0 to a process that is not :data:`OURS`, or to a
      group that holds one, and wherever the system does not say.

    Signal 0 delivers nothing, and asking whether a process exists is what
    it is for; a signal to an id that no process or group has reaches
    nothing, and the system answers so.
    """
    if type(target) is not int:
        return (
            f"{how} was given {target!r}, a {type(target).__name__}, as the process to "
            "signal: only an int is taken as it is"
        )
    if target <= 1:
        if how == "os.killpg":
            whom = "every process" if target == 1 else "the test process's own group"
        elif target == -1:
            whom = "every process"
        elif target == 0:
            whom = "the test process's own group"
        elif target == 1:
            whom = "the first process"
        else:
            whom = f"the whole process group {-target}"
        return f"{how}({target}) signals {whom}"
    if how == "os.killpg" and target in (group, session):
        return f"{how}({target}) signals the test process's own group or session"
    if _is_probe(signum):
        return None
    if how == "os.killpg":
        found = members(target)
        if found is None:
            return f"{how}({target}): the system does not say which processes the group holds"
    else:
        found = [target]
    for pid in found:
        if pid == me:
            continue
        said = whose(pid)
        if said == UNKNOWN:
            return f"{how}({target}): the system does not say whose process {pid} is"
        if said not in (ABSENT, OURS):
            return f"{how}({target}) signals process {pid}, which the test did not start"
    return None


def _stat(pid: int, proc: str = "/proc") -> tuple[int, int, int, int] | str:
    """The parent, group, session and start time of process *pid*, as the
    system says them; :data:`ABSENT` or :data:`UNKNOWN` where it does not."""
    try:
        with _open(f"{proc}/{pid}/stat", "rb") as handle:
            data = handle.read()
    except (FileNotFoundError, ProcessLookupError):
        return ABSENT if _isdir(f"{proc}/self") else UNKNOWN
    except OSError:
        return UNKNOWN
    try:
        # After the name in parentheses: state, parent, group, session, ...
        # and the start time as the 20th of them.
        fields = data[data.rindex(b")") + 2 :].split()
        return int(fields[1]), int(fields[2]), int(fields[3]), int(fields[19])
    except (ValueError, IndexError):
        return UNKNOWN


def started_at(pid: int) -> int | None:
    """When process *pid* started, in the system's clock ticks since it
    booted; ``None`` where the system does not say."""
    stat = _stat(pid)
    return None if isinstance(stat, str) else stat[3]


class ProcessView:
    """What the system says of processes, for :func:`signal_refusal`.

    A process is the test's when it is the test process or comes from it
    parent by parent; when its group or session is one that a process the
    test started made (*started*: each process the test process started,
    with when it started); or when the environment it was started with
    carries *mark*, a ``NAME=value`` the test put in its own environment
    for this test alone.  A process whose parent ended is handed to another
    parent, but keeps its group and session, and the system gives the id of
    a group or session that still has members to no other process; one that
    also left them keeps the environment it inherited.  A noted start whose
    id another process holds now, one that started at another time, is not
    counted.
    """

    def __init__(
        self, started: Mapping[int, int | None], proc: str = "/proc", mark: str | None = None
    ) -> None:
        self._started = started
        self._proc = proc
        self._mark = os.fsencode(mark) if mark else None

    def _carries_the_mark(self, pid: int) -> bool:
        if self._mark is None:
            return False
        try:
            with _open(f"{self._proc}/{pid}/environ", "rb") as handle:
                return self._mark in handle.read().split(bytes(1))
        except OSError:
            return False

    def _made_by_the_test(self, ident: int) -> bool:
        if ident not in self._started:
            return False
        holder = _stat(ident, self._proc)
        if holder == ABSENT:
            # Its maker has ended; the id stays the group's while it has members.
            return True
        recorded = self._started[ident]
        return not isinstance(holder, str) and recorded is not None and holder[3] == recorded

    def whose(self, pid: int) -> str:
        """:data:`ABSENT`, :data:`OURS`, :data:`OTHER` or :data:`UNKNOWN`."""
        me = _getpid()
        stat = _stat(pid, self._proc)
        if isinstance(stat, str):
            return stat
        if pid == me:
            return OURS
        parent, group, session, _start = stat
        if self._made_by_the_test(group) or self._made_by_the_test(session):
            return OURS
        if self._carries_the_mark(pid):
            return OURS
        for _ in range(_GENERATIONS):
            if parent == me:
                return OURS
            if parent <= 1:
                return OTHER
            above = _stat(parent, self._proc)
            if isinstance(above, str):
                return UNKNOWN
            parent = above[0]
        return UNKNOWN

    def members(self, group: int) -> list[int] | None:
        """The processes of *group*, ``None`` where the system does not say."""
        try:
            names = _listdir(self._proc)
        except OSError:
            return None
        found: list[int] = []
        for name in names:
            if not name.isdigit():
                continue
            stat = _stat(int(name), self._proc)
            if stat == UNKNOWN:
                return None
            if not isinstance(stat, str) and stat[1] == group:
                found.append(int(name))
        return found


def judge_every_signal(monkeypatch: Any, tripped: list[str], view: ProcessView) -> None:
    """Put :func:`signal_refusal` in front of ``os.kill`` and ``os.killpg``,
    until *monkeypatch* undoes it: a refused signal is noted in *tripped* and
    raised before anything is sent.

    ``subprocess.Popen``'s ``send_signal``, ``terminate`` and ``kill`` send
    through ``os.kill``, and so do an event loop's subprocesses, through
    them.
    """

    def guarded(real: Any, how: str) -> Any:
        @functools.wraps(real)
        def send(target: Any, signum: Any) -> Any:
            why = signal_refusal(
                how,
                target,
                signum,
                me=_getpid(),
                group=_getpgrp(),
                session=_getsid(0),
                whose=view.whose,
                members=view.members,
            )
            if why is not None:
                tripped.append(f"{how}({target!r}, {signum!r}): {why}")
                raise DestructiveCommandError(
                    f"a test was about to send a signal that must never be sent here: {why}"
                )
            return real(target, signum)

        return send

    for name in ("kill", "killpg"):
        monkeypatch.setattr(os, name, guarded(getattr(os, name), "os." + name))


# -- a package whose commands go through a container runtime ------------------

# Subcommands of the runtime that only read, and name nothing they act on.
_RUNTIME_READS = frozenset({"ps", "version", "info", "images", "events"})
# Subcommands whose first operand is the container; what follows is theirs.
# With each, its options that stand alone: any other option of a subcommand
# is followed by its value.
_FIRST_IS_THE_CONTAINER: dict[str, frozenset[str]] = {
    "exec": frozenset({"-d", "--detach", "-i", "--interactive", "-t", "--tty", "--privileged"}),
    "logs": frozenset({"-f", "--follow", "-t", "--timestamps", "--details"}),
    "port": frozenset(),
    "top": frozenset(),
}
# Subcommands whose every operand is a container.
_EVERY_OPERAND_IS_A_CONTAINER: dict[str, frozenset[str]] = {
    "attach": frozenset({"--no-stdin", "--sig-proxy"}),
    "diff": frozenset(),
    "inspect": frozenset({"-s", "--size"}),
    "kill": frozenset(),
    "pause": frozenset(),
    "rename": frozenset(),
    "restart": frozenset(),
    "rm": frozenset({"-f", "--force", "-v", "--volumes", "-l", "--link"}),
    "start": frozenset({"-a", "--attach", "-i", "--interactive"}),
    "stats": frozenset({"-a", "--all", "--no-stream", "--no-trunc"}),
    "stop": frozenset(),
    "unpause": frozenset(),
    "update": frozenset(),
    "wait": frozenset(),
}
_CP_ALONE = frozenset({"-a", "--archive", "-L", "--follow-link", "-q", "--quiet"})
# The options the rollout package gives ``run``; no other is let through.
# Written out here, read from ``Container.run_argv``, ``Container.protect_argv``
# and the helper that ends what is left in a container.  Those that stand alone:
_RUN_FLAGS = frozenset({"--detach", "--rm", "--read-only"})
# Those whose value can give the container no more than leaving the option out
# does: its name (judged below), labels, limits, its own environment, user and
# working directory, memory-backed directories, the program it starts.
_RUN_ANY_VALUE = frozenset(
    {
        "--name",
        "--label",
        "--user",
        "--cpus",
        "--memory",
        "--memory-swap",
        "--pids-limit",
        "--shm-size",
        "--workdir",
        "--env",
        "--tmpfs",
        "--entrypoint",
        "--cap-drop",
    }
)
# Those whose value could give the container more, with the values the package
# gives them.  The capabilities are those a task may name for its container,
# and the one the helper that spares the main process from the out-of-memory
# killer takes besides; a test holds them equal to the package's.
_RUN_VALUES: dict[str, frozenset[str]] = {
    "--pull": frozenset({"never"}),
    "--network": frozenset({"none"}),
    "--security-opt": frozenset({"no-new-privileges"}),
    "--oom-score-adj": frozenset({"0"}),
    "--ulimit": frozenset({"nofile=4096:4096", "core=0", "memlock=65536:65536"}),
    "--cap-add": frozenset(
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
            "SYS_RESOURCE",
        }
    ),
}
# Those whose value the package makes of a fixed beginning and a size.
_RUN_VALUE_BEGINS = {"--storage-opt": "size="}
# A capability only a container takes that shares the process namespace of
# one of the harness's: the helper that sets the main process's score.
_HELPER_CAPABILITIES = frozenset({"SYS_RESOURCE"})


def _read_words(
    words: list[str], alone: frozenset[str], *, first_operand_ends: bool = False
) -> tuple[list[tuple[str, str | None]], list[str]]:
    """The options, each with its value, and the operands among the words
    after a subcommand.

    An option not in *alone* is followed by its value.  With
    *first_operand_ends*, everything after the first operand is an operand
    too: the command a container is to run, with options of its own.
    """
    options: list[tuple[str, str | None]] = []
    operands: list[str] = []
    rest = iter(words)
    for word in rest:
        if word == "--":
            operands.extend(rest)
        elif word.startswith("--"):
            name, given, value = word.partition("=")
            if given:
                options.append((name, value))
            elif name in alone:
                options.append((name, None))
            else:
                options.append((name, next(rest, "")))
        elif word.startswith("-") and len(word) > 1:
            letters = ["-" + letter for letter in word[1:]]
            if all(letter in alone for letter in letters):
                options.extend((letter, None) for letter in letters)
            elif len(word) > 2:
                # The value follows its letter at once.
                options.append((word[:2], word[2:]))
            else:
                options.append((word, next(rest, "")))
        else:
            operands.append(word)
            if first_operand_ends:
                operands.extend(rest)
    return options, operands


def _run_refusal(words: list[str], prefix: str) -> str | None:
    """Why ``run`` (or ``create``) with *words* makes no container of the
    harness's kind: one of its name, given no option but those the package
    gives ``run``, and those of them that could give the container more only
    with the values the package gives them.  A process namespace is shared
    only with another container of its name."""
    options, operands = _read_words(words, _RUN_FLAGS, first_operand_ends=True)
    if not operands:
        return "run names no image"
    names = [value or "" for name, value in options if name == "--name"]
    if len(names) != 1 or not names[0].startswith(prefix):
        return f"run must give its container a name of the harness's ({prefix}*)"
    shares = False
    for name, value in options:
        if name in _RUN_FLAGS:
            if value is not None:
                return f"run {name}={value}: the harness gives {name} no value"
        elif name == "--pid":
            if not (value or "").startswith("container:" + prefix):
                return f"run --pid {value} shares with a container that is not the harness's"
            shares = True
        elif name in _RUN_VALUES:
            if value not in _RUN_VALUES[name]:
                known = ", ".join(sorted(_RUN_VALUES[name]))
                return f"run {name} {value}: the harness gives {name} only {known}"
        elif name in _RUN_VALUE_BEGINS:
            begins = _RUN_VALUE_BEGINS[name]
            if not (value or "").startswith(begins):
                return f"run {name} {value}: the harness gives {name} only {begins}..."
        elif name not in _RUN_ANY_VALUE:
            return f"run {name} is no option the harness gives run"
    added = {value for name, value in options if name == "--cap-add"}
    if added & _HELPER_CAPABILITIES and not shares:
        helper_only = ", ".join(sorted(added & _HELPER_CAPABILITIES))
        return (
            f"run --cap-add {helper_only} is for a container that shares the process "
            "namespace of one of the harness's"
        )
    return None


def runtime_refusal(words: list[str], *, prefix: str, own: Callable[[str], bool]) -> str | None:
    """Why a container runtime's command line is not one to let through,
    ``None`` when it is.  *words* are the ones after the runtime's name.

    Let through: a subcommand that names containers (``exec``, ``rm``,
    ``kill``, ``stop``, ``cp``, ``inspect`` and the others in the tables
    above) when every container it names begins with *prefix*; ``run`` and
    ``create`` for a container of that name with only the options the
    package gives ``run`` (:func:`_run_refusal`); ``cp`` between such a
    container and a
    stream or a path that *own* says is the test's; and the few subcommands
    that only read.  Anything else is refused, also a subcommand this does
    not know: one that a package comes to use is added here, with what it
    names.
    """
    if not words or words[0].startswith("-"):
        return "the runtime must be given its subcommand first"
    sub, rest = words[0], words[1:]
    if sub == "container":
        # The long spelling of the same subcommands.
        if not rest:
            return "container is given no subcommand"
        sub, rest = rest[0], rest[1:]
        if sub in ("ls", "list", "ps"):
            return None
        if sub == "prune":
            return "container prune removes containers that are not the harness's"
    elif sub == "image":
        if rest[:1] in (["inspect"], ["ls"]):
            return None
        return f"image {' '.join(rest[:1])} is no read of an image"
    elif sub in _RUNTIME_READS:
        return None
    if sub in ("run", "create"):
        return _run_refusal(rest, prefix)
    if sub == "cp":
        _options, operands = _read_words(rest, _CP_ALONE)
        inside = [o for o in operands if ":" in o and not o.startswith(("/", "."))]
        outside = [o for o in operands if o not in inside]
        if len(operands) != 2 or len(inside) != 1:
            return "cp must copy between one container and this machine"
        if not inside[0].startswith(prefix):
            return f"cp names {inside[0].split(':')[0]!r}, which is no container of the harness"
        if outside[0] != "-" and not own(outside[0]):
            return (
                f"cp reads or writes {outside[0]!r} on this machine: a stream (-) or a path "
                "under the test's own directory is all it may"
            )
        return None
    if sub in _FIRST_IS_THE_CONTAINER:
        options, operands = _read_words(rest, _FIRST_IS_THE_CONTAINER[sub], first_operand_ends=True)
        if sub == "exec" and any(name == "--privileged" for name, _value in options):
            return "exec --privileged gives a command in the container this machine's rights"
        named = operands[:1]
    elif sub in _EVERY_OPERAND_IS_A_CONTAINER:
        _options, named = _read_words(rest, _EVERY_OPERAND_IS_A_CONTAINER[sub])
        if sub == "stats" and not named:
            return None
    else:
        return f"the subcommand {sub!r} is not one the guard knows the package to use"
    if not named:
        return f"{sub} names no container"
    for name in named:
        if not name.startswith(prefix):
            return f"{sub} names {name!r}, which is no container of the harness ({prefix}*)"
    return None


def _is_the_runtime(word: str, program: str, runtime: str) -> bool:
    """Whether *word*, which the system starts as the file *program*, is the
    container *runtime*: that name, and the file the system finds for it."""
    if os.path.basename(word) != runtime:
        return False
    found = started_program(runtime)
    return program == found or bool(
        program and found and os.path.realpath(program) == os.path.realpath(found)
    )


def rollout_refusal(
    start: Start, *, scratch: str, prefix: str, keeper: str | None, runtime: str = "docker"
) -> str | None:
    """Why a package whose commands go through a container runtime may not
    start *start* on this machine, ``None`` when it may.

    It may start: a program the test wrote under *scratch*, its temporary
    directory (a stand-in for the runtime, a verifier program); the runtime
    itself, for what :func:`runtime_refusal` lets through; and its keeper,
    which removes one container of the harness after a time, in exactly the
    shape the package starts it: ``sh -c <keeper> sh SECONDS RUNTIME NAME``,
    seven words, where *keeper* is the script's text, RUNTIME is the runtime
    or a program the test wrote, and NAME, the argument the script removes,
    begins with *prefix*.  A program is the file the system would start,
    with links followed, so a bare name is never taken for a file of the
    same name in the test's directory.
    """

    def own(path: str) -> bool:
        return bool(path) and os.path.realpath(path).startswith(scratch)

    if own(start.program):
        return None
    argv = list(start.argv)
    if not argv:
        return "what it would start could not be read"
    if _is_the_runtime(argv[0], start.program, runtime):
        return runtime_refusal(argv[1:], prefix=prefix, own=own)
    if keeper is not None and argv[:4] == ["sh", "-c", keeper, "sh"]:
        if len(argv) != 7:
            return f"the keeper is given {len(argv) - 4} arguments, not its 3"
        removes, program = argv[6], argv[5]
        if not removes.startswith(prefix):
            return f"the keeper would remove {removes!r}, which is no container of the harness"
        found = started_program(program, cwd=start.cwd)
        if not _is_the_runtime(program, found, runtime) and not own(found):
            return (
                f"the keeper would run {program!r}, which is neither the container runtime "
                "nor a program the test wrote"
            )
        return None
    return "it is neither the container runtime nor a program the test wrote"


def _refuse_program(start: Start, why: str, tripped: list[str]) -> None:
    shown = start.shown()
    tripped.append(f"{shown} ({why})")
    raise DestructiveCommandError(
        f"a test was about to start a program on this machine that is not the one "
        f"its commands go through: {shown!r} ({why})"
    )


def refuse_all_but_the_runtime(
    tripped: list[str], *, package: str, refusal: Callable[[Start], str | None]
) -> Callable[[Start], None]:
    """A check for :func:`judge_every_start`: a start asked for by a module of
    *package*, whichever, is noted in *tripped* and refused when *refusal*
    gives a reason for it.  Other callers are not this check's to judge."""

    def judge(start: Start) -> None:
        if start.caller != package and not start.caller.startswith(package + "."):
            return
        why = refusal(start)
        if why is not None:
            _refuse_program(start, why, tripped)

    return judge


class _OnlyThesePrograms:
    """The ``subprocess`` module as one module of a package holds it, with
    every way it starts a process asking first."""

    def __init__(
        self, real: Any, refusal: Callable[[Start], str | None], tripped: list[str], caller: str
    ) -> None:
        self._real = real
        self._refusal = refusal
        self._tripped = tripped
        self._caller = caller

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    def _ask(self, how: str, popenargs: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
        start = popen_start(how, popenargs, kwargs, self._caller)
        if start is None:
            return  # arguments the call does not take: it refuses them itself
        why = self._refusal(start)
        if why is not None:
            _refuse_program(start, why, self._tripped)

    def Popen(self, *popenargs: Any, **kwargs: Any) -> Any:  # noqa: N802
        self._ask("subprocess.Popen", popenargs, kwargs)
        return self._real.Popen(*popenargs, **kwargs)

    def run(self, *popenargs: Any, **kwargs: Any) -> Any:
        self._ask("subprocess.run", popenargs, kwargs)
        return self._real.run(*popenargs, **kwargs)

    def call(self, *popenargs: Any, **kwargs: Any) -> Any:
        self._ask("subprocess.call", popenargs, kwargs)
        return self._real.call(*popenargs, **kwargs)

    def check_call(self, *popenargs: Any, **kwargs: Any) -> Any:
        self._ask("subprocess.check_call", popenargs, kwargs)
        return self._real.check_call(*popenargs, **kwargs)

    def check_output(self, *popenargs: Any, **kwargs: Any) -> Any:
        self._ask("subprocess.check_output", popenargs, kwargs)
        return self._real.check_output(*popenargs, **kwargs)

    def getoutput(self, cmd: Any, **kwargs: Any) -> Any:
        self._ask("subprocess.getoutput", (cmd,), {"shell": True})
        return self._real.getoutput(cmd, **kwargs)

    def getstatusoutput(self, cmd: Any, **kwargs: Any) -> Any:
        self._ask("subprocess.getstatusoutput", (cmd,), {"shell": True})
        return self._real.getstatusoutput(cmd, **kwargs)


def only_these_programs(
    real: Any, refusal: Callable[[Start], str | None], tripped: list[str], caller: str
) -> Any:
    """A stand-in for the ``subprocess`` module *real* inside the module
    *caller*: a program is started only when *refusal* gives no reason
    against it; anything else is noted in *tripped* and refused."""
    return _OnlyThesePrograms(real, refusal, tripped, caller)
