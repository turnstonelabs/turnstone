"""Execution environment — where a session's shell and file tools act.

The model-facing shell tools (``bash``, its background variant, ``bash_output``,
``kill_shell``) and file tools (``read_file``, ``write_file``, ``edit_file``,
``search``, ``diff_file``) reach processes and files only through an
:class:`ExecutionEnvironment`.  The class itself is this host: every method is
the ``os`` / ``open`` / ``subprocess`` call the tool made before the seam
existed, so a session built without an environment behaves exactly as it did.

A subclass moves all of those tools somewhere else in one step — a per-run
container, a remote worker — while the executors (output capture, time limits,
cancellation, the read-before-edit bookkeeping) stay Turnstone's own.  Both
halves of a tool go through it: the prepare step that resolves and checks a
path, and the execute step that reads or writes it, so a path is never resolved
in one place and opened in another.

What an implementation must keep:

- ``spawn`` returns a local ``Popen`` whose pipes carry the command's output
  and whose exit is the command's exit, like
  :func:`~turnstone.core.background_shells.spawn_group_leader`.
- ``kill_group`` stops the command and everything it started, wherever that
  runs.  The foreground tool calls it after *every* command, also one that
  exited normally, so it must hit exactly that command's process group.
- Errors keep their types: a missing file raises ``FileNotFoundError``,
  anything else an ``OSError`` whose text the tools show to the model.
- Text is read and written the way the built-in text mode does it
  (``read_lines`` splits where ``readlines`` splits), so line numbers and
  edits do not depend on the environment.
"""

from __future__ import annotations

import errno
import os
import shutil
import signal
import subprocess

from turnstone.core.background_shells import signal_group, spawn_group_leader
from turnstone.core.safety import is_command_blocked


class ExecutionEnvironment:
    """The place shell and file tools act.  This base class is the host."""

    # Whether what this environment does takes effect on the machine the
    # session runs on.  The base class is that machine.  A subclass that moves
    # every method somewhere else says ``False``; one that does not say is
    # taken to act here, so a caller that must keep a model's commands off this
    # machine can refuse it without knowing the class.
    acts_on_host: bool = True

    # Bounds on what a tool may build in this process from what the model
    # controls.  The file tools hold a file's text here, wherever the file
    # lives: a replace-all edit multiplies it, and a diff of two files with
    # many repeated lines takes time quadratic in their length.  ``None`` is no
    # bound, which is the host's behaviour.
    #
    # Largest text, in characters, an edit may produce.
    max_text_chars: int | None = None
    # Most lines, both sides together, ``diff_file`` compares.
    max_diff_lines: int | None = None

    # -- shell ---------------------------------------------------------------

    def spawn(
        self,
        command: str,
        *,
        stop_on_error: bool,
        env: dict[str, str] | None,
        stdin: int = subprocess.DEVNULL,
    ) -> tuple[subprocess.Popen[str], int, str]:
        """Start *command* as a script; return ``(process, group id, script path)``.

        The caller unlinks the script path on its own exit path (a path that is
        already gone is tolerated).  *env* is the scrubbed environment of this
        process; an environment that runs commands elsewhere ignores it.
        """
        return spawn_group_leader(command, stop_on_error=stop_on_error, env=env, stdin=stdin)

    def kill_group(self, proc: subprocess.Popen[str], pgid: int) -> None:
        """Kill the process group *spawn* returned for *proc*.

        Raises what the kill raises (``ProcessLookupError`` once the group is
        empty); callers decide what to ignore.  The group is killed also once
        *proc* has ended and been collected, for what the command left running
        in it.  An id that is not that group's to be signalled is never
        signalled and raises the same error: one that is no ``int`` above 1 or
        is this process's own group, one that is not *proc*'s, one that another
        process has by now
        (:func:`~turnstone.core.background_shells.signal_group`).
        """
        if not signal_group(proc, pgid, signal.SIGKILL, after_exit=True):
            raise ProcessLookupError(
                errno.ESRCH, "no process group of this command's is there to signal"
            )

    def process_label(self, proc: subprocess.Popen[str]) -> str:
        """How to name a started command to the model, ``""`` to say nothing.

        The host names the process id.  An environment whose commands run
        elsewhere has only a local stand-in for them, whose id means nothing to
        the model and should not be shown.
        """
        return f"pid {proc.pid}"

    def command_refusal(self, command: str) -> str | None:
        """Why *command* must not be started here, ``None`` when it may.

        Asked when a shell call is prepared, before anything runs; what is
        returned is the tool's result.  The host refuses by its fixed list of
        patterns (:func:`turnstone.core.safety.is_command_blocked`), which
        protects the machine the session runs on.  An environment whose
        commands run elsewhere answers for that place: the list is written for
        a machine somebody owns and refuses ordinary work in a throwaway one.
        """
        return is_command_blocked(command)

    # -- search --------------------------------------------------------------

    def which(self, program: str) -> str | None:
        """Path of *program* where commands run, ``None`` when it is not installed."""
        return shutil.which(program)

    def popen_search(
        self, args: list[str], *, env: dict[str, str] | None, timeout: float
    ) -> subprocess.Popen[bytes]:
        """Start the search tool's subprocess with piped binary output.

        The caller reads a bounded amount, kills the process past its byte cap
        and enforces *timeout* itself; an environment whose kill does not reach
        the real process uses *timeout* to bound it where it runs.
        """
        return subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )

    # -- paths ---------------------------------------------------------------

    def getcwd(self) -> str:
        """The directory commands start in and relative paths resolve against."""
        return os.getcwd()

    def expanduser(self, path: str) -> str:
        return os.path.expanduser(path)

    def abspath(self, path: str) -> str:
        return os.path.abspath(path)

    def realpath(self, path: str) -> str:
        """*path* made absolute with symlinks resolved; the path need not exist."""
        return os.path.realpath(path)

    def exists(self, path: str) -> bool:
        return os.path.exists(path)

    def stat(self, path: str) -> os.stat_result:
        """Mode and size of *path* (symlinks followed), as ``os.stat`` reports them."""
        return os.stat(path)

    # -- content -------------------------------------------------------------

    def read_bytes(self, path: str, limit: int) -> bytes:
        """At most *limit* leading bytes of *path*."""
        with open(path, "rb") as f:
            return f.read(limit)

    def read_text(self, path: str) -> str:
        """The whole of *path* as text."""
        with open(path) as f:
            return f.read()

    def read_lines(self, path: str) -> list[str]:
        """The whole of *path* as text lines, line ends kept."""
        with open(path) as f:
            return f.readlines()

    def write_text(
        self, path: str, content: str, *, append: bool = False, make_parents: bool = False
    ) -> None:
        """Write *content* to *path*, replacing it or appending to it."""
        if make_parents:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a" if append else "w") as f:
            f.write(content)


# The environment of a session that was given none.
HOST_ENVIRONMENT = ExecutionEnvironment()
