"""Per-session registry for explicitly backgrounded bash shells (#817).

#816 made the ``bash`` tool terminate its whole process group when the call
returns — no leaked servers, no hangs, but also no way to keep a dev server
alive across calls.  This registry restores that as an explicit opt-in with
the model-facing shape the frontier coding agents converged on: a boolean on
the shell tool, a short ``bash_N`` handle, a delta-output reader that returns
only lines produced since the previous read, and a kill tool.

Lifetime rules (the #816 rule, extended):

* The tracked command defines the shell's lifetime.  When it exits —
  naturally, by ``kill``, or by registry teardown — its whole session group
  is SIGKILLed, so nothing the command backgrounded can outlive it.
* Shells survive generation-cancel (they are deliberately detached) and die
  with the owning session: :meth:`BackgroundShellRegistry.close` runs from
  ``ChatSession.close()``, which every workstream-teardown path funnels
  through.
* Shells spawned inside a task_agent carry that agent's ``owner`` tag; the
  agent's ``finally`` reaps them, and owner-scoped lookup keeps parallel
  agents (and the parent) from touching each other's handles.

Output is buffered per shell as a rolling deque of lines (stderr tagged
``[stderr] `` inline, arrival order) capped by total characters with
drop-oldest semantics — a chatty server cannot grow a session's memory
unbounded.  Reads advance a cursor over the *logical* line stream, so a
line dropped before it was ever read surfaces as an explicit gap count
rather than silently vanishing.
"""

from __future__ import annotations

import contextlib
import itertools
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from turnstone.core.log import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

log = get_logger(__name__)

ShellStatus = Literal["running", "completed", "killed"]

# Live (status == "running") shells per session.  A hard backstop against a
# runaway loop of spawns on a multi-tenant node, not an operator knob.
_DEFAULT_MAX_SHELLS = 8
# Rolling per-shell buffer cap, in characters.  Oldest whole lines drop
# first; the newest line always survives even if it alone exceeds the cap.
_DEFAULT_MAX_BUFFER_CHARS = 200_000
# How long to wait for the drain threads after the group kill forces their
# pipes to EOF.  A grandchild that double-``setsid``-escaped the group can
# hold a pipe open past this — the drain is a daemon thread and leaks
# (logged) until that process dies, same acceptance as the foreground tool.
_DRAIN_JOIN_TIMEOUT_S = 5
# TOTAL join budget for ``kill``/``reap`` across all of a shell's threads
# (not per-thread — a wedged drain must not stack timeouts).
_WAITER_JOIN_TIMEOUT_S = 10
# TOTAL join budget for ``close()`` across ALL shells.  close() runs on the
# workstream-teardown funnel, which the server can reach from an async
# handler — an unbounded (or per-shell-stacking) wait here would freeze the
# node's event loop, not just this workstream.  Threads still alive past the
# budget are daemons: logged and abandoned, they die with their pipes.
_CLOSE_JOIN_BUDGET_S = 5
# Exited records retained per registry (drop-oldest).  Keeps a long-lived
# workstream that backgrounds thousands of short jobs from accumulating
# dead records (each can pin up to ``max_buffer_chars`` of buffer) while
# still letting the model read recently-exited shells' output.
_MAX_EXITED_RECORDS = 32
# Bounds on the model-supplied ``filter`` regex: pattern length, how much of
# each line the pattern sees, and wall-clock for the whole filter pass.  The
# pass runs in a SUBPROCESS, not a thread: CPython's sre engine holds the
# GIL for the entire duration of one ``search`` call, so a catastrophic-
# backtracking pattern freezes every thread in the interpreter — no
# in-process timeout (thread join, signal, anything) can fire.  A child
# process is killable from outside the GIL; on timeout the read errors
# WITHOUT consuming the delta (the cursor only commits on a completed pass).
_MAX_FILTER_PATTERN_CHARS = 512
_FILTER_MAX_LINE_CHARS = 4096
_FILTER_TIMEOUT_S = 2.0

# Runs inside ``sys.executable -c``: reads {pattern, lines} as JSON on
# stdin (lines already truncated parent-side), writes the MATCHING INDEXES
# as JSON on stdout (indexes, not lines — no need to echo a 200K buffer
# back through a pipe).
_FILTER_HELPER_SRC = (
    "import json, re, sys\n"
    "d = json.load(sys.stdin)\n"
    "p = re.compile(d['pattern'])\n"
    "sys.stdout.write(json.dumps([i for i, ln in enumerate(d['lines']) if p.search(ln)]))\n"
)


class UnknownShellError(LookupError):
    """No shell with that id is visible in the caller's owner scope."""


class TooManyShellsError(RuntimeError):
    """The per-session live-shell cap would be exceeded."""


class FilterTimeoutError(ValueError):
    """The ``filter`` regex did not finish within the time bound."""


class FilterExecError(RuntimeError):
    """The filter helper process failed for a non-pattern reason."""


def _filter_lines_bounded(pattern: re.Pattern[str], lines: list[str], shell_id: str) -> list[str]:
    """Apply ``pattern`` per line with a wall-clock bound.

    A catastrophic-backtracking pattern would wedge the (auto-approved)
    tool call — the exact never-returns class #816 removed — and it cannot
    be bounded IN-PROCESS: sre holds the GIL for the whole ``search`` call,
    freezing every interpreter thread including any watchdog.  So the pass
    runs in a small child process (killable from the OS): each line
    truncated PARENT-side to :data:`_FILTER_MAX_LINE_CHARS` before
    serialization (a filter targets log lines; shipping a retained multi-MB
    line through the pipe would spend the time budget on I/O and misreport
    a fine pattern as slow), the whole pass bounded by
    :data:`_FILTER_TIMEOUT_S`, SIGKILL on the child's group past that.

    Raises :class:`FilterTimeoutError` on timeout and
    :class:`FilterExecError` on a helper failure that is NOT the pattern's
    fault (fork/OOM/env) — distinct messages, so the model doesn't
    "simplify" an innocent regex.  Either way the caller consumes nothing.
    The ~tens-of-ms interpreter startup is paid only on filtered reads.

    Threat model for the auto-approved path (``bash_output`` runs without
    operator approval): the only model-controlled inputs are the PATTERN
    and, transitively, the buffered text.  The pattern is compiled
    parent-side before the fork (a non-regex payload fails there), the
    child executes only the fixed ``_FILTER_HELPER_SRC`` — the pattern is
    DATA on stdin, never code —, the child gets a scrubbed environment, no
    shell, read-only work, and a SIGKILL at the time bound.  Worst case a
    hostile pattern buys ~2s of one core.
    """
    from turnstone.core.env import scrubbed_env

    payload = json.dumps(
        {
            "pattern": pattern.pattern,
            "lines": [ln[:_FILTER_MAX_LINE_CHARS] for ln in lines],
        },
        ensure_ascii=False,
    )
    try:
        proc = subprocess.Popen(
            [sys.executable, "-c", _FILTER_HELPER_SRC],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            # Pin BOTH pipe directions to UTF-8: ``text=True`` alone uses
            # the locale encoding, and on a C/POSIX-locale node a single
            # U+FFFD (from the drain's ``errors="replace"``) would raise
            # UnicodeEncodeError out of communicate() — escaping the
            # Timeout/Exec error taxonomy as a generic crash.  The child's
            # own stdio decode is pinned via PYTHONIOENCODING.
            encoding="utf-8",
            errors="replace",
            # scrubbed_env, not os.environ: the helper needs no secrets (it
            # runs only our trusted source over already-buffered text), and
            # every other fork in this codebase strips API keys/tokens —
            # this one must not be the exception.
            env={**scrubbed_env(), "PYTHONIOENCODING": "utf-8"},
            start_new_session=True,
        )
    except OSError as e:
        # Fork pressure (EAGAIN) / exec failure — same containment class as
        # spawn()'s thread-start guard, and by contract NOT the pattern's
        # fault.
        log.warning("bg_shell.filter_helper_spawn_failed", shell_id=shell_id, error=str(e))
        raise FilterExecError(
            "the filter could not be applied (helper failed to start); this "
            "is not a problem with your pattern — no output was consumed; "
            "retry, or read without a filter"
        ) from e
    try:
        out, _ = proc.communicate(payload, timeout=_FILTER_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(OSError, ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=5)
        log.warning("bg_shell.filter_timeout", shell_id=shell_id, pattern=pattern.pattern[:80])
        raise FilterTimeoutError(
            f"filter regex took longer than {_FILTER_TIMEOUT_S:g}s to run; no "
            "output was consumed — simplify the pattern or retry without a filter"
        ) from None
    if proc.returncode != 0:
        # The parent validated the compile, so a child failure is exotic
        # (fork pressure, interpreter env) — NOT the pattern's fault.
        log.warning(
            "bg_shell.filter_helper_failed",
            shell_id=shell_id,
            returncode=proc.returncode,
        )
        raise FilterExecError(
            f"the filter could not be applied (helper exited {proc.returncode}); "
            "this is not a problem with your pattern — no output was consumed; "
            "retry, or read without a filter"
        )
    try:
        indexes = json.loads(out)
    except ValueError:
        log.warning("bg_shell.filter_helper_bad_output", shell_id=shell_id)
        raise FilterExecError(
            "the filter could not be applied (helper returned malformed data); "
            "no output was consumed — retry, or read without a filter"
        ) from None
    return [lines[i] for i in indexes if isinstance(i, int) and 0 <= i < len(lines)]


def drain_pipe_lines(pipe: Any, on_line: Callable[[str], None]) -> None:
    """Read ``pipe`` line-by-line until EOF, forwarding each to ``on_line``.

    The drain half of the shared bash recipe (see :func:`spawn_group_leader`
    for the spawn half): both variants of the tool tolerate the same two
    end-of-stream shapes.  A pipe torn down by the session-group kill is the
    expected end; anything else must not kill the drain silently.  (The
    ``errors="replace"`` on the shared Popen pre-empts UnicodeDecodeError —
    a ValueError that would otherwise end the drain early and drop ALL
    remaining output while reporting a clean success.)
    """
    try:
        for line in pipe:
            on_line(line)
    except (ValueError, OSError):
        log.debug("bash.drain_read_error", exc_info=True)


def spawn_group_leader(
    command: str, *, stop_on_error: bool, env: dict[str, str] | None
) -> tuple[subprocess.Popen[str], int, str]:
    """Write the script, fork the detached group leader, snapshot its pgid.

    THE shared prologue for both runs of the model-facing bash tool — the
    foreground executor (``ChatSession._exec_bash``) and this registry — so
    the two variants of one tool cannot drift: same ``pipefail``/``set -e``
    preamble, same decode policy (``errors="replace"``), same session-group
    discipline.  The script file exists because bash reads scripts lazily
    (robust to quoting/length; unlinking early could truncate a long script
    mid-run) — the CALLER owns the unlink on its own exit path.  On a
    failed fork the script is unlinked here and the error propagates.  The
    pgid snapshot happens while the leader is alive (``start_new_session``
    makes ``pgid == pid``); the microseconds-wide pid-wraparound TOCTOU is
    the same accepted one as always.
    """
    with tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False) as f:
        preamble = "set -o pipefail\n"
        if stop_on_error:
            preamble += "set -e\n"
        f.write(preamble + command)
        script_path = f.name
    try:
        proc = subprocess.Popen(
            ["bash", script_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            start_new_session=True,
            env=env,
        )
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(script_path)
        raise
    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        pgid = proc.pid
    return proc, pgid, script_path


@dataclass
class ShellRead:
    """One delta read: lines since the previous read, plus shell state.

    ``lines`` is post-filter (what the caller shows); ``new_line_count`` is
    the pre-filter delta size — the cursor advanced past all of them, so a
    filtered-out line is consumed, never deferred to a later read.
    ``dropped_lines`` counts lines lost to the buffer cap before they were
    ever read (an explicit gap, not silence).
    """

    shell_id: str
    status: ShellStatus
    exit_code: int | None
    lines: list[str]
    new_line_count: int
    dropped_lines: int
    # Lines in this delta longer than the per-line filter window — their
    # tails were invisible to the pattern.  Only populated on filtered
    # reads; the caller surfaces it so a "none matching" answer over
    # clipped evidence is never silent.
    clipped_lines: int = 0


class BackgroundShell:
    """One detached shell: process handles, rolling buffer, read cursor.

    Mutable state is guarded by ``self.lock`` — the drain threads append
    while reads snapshot; the waiter thread flips ``status`` exactly once.
    """

    def __init__(
        self,
        shell_id: str,
        command: str,
        proc: subprocess.Popen[str],
        pgid: int,
        owner: str | None,
        script_path: str,
        max_buffer_chars: int,
    ) -> None:
        self.shell_id = shell_id
        self.command = command
        self.proc = proc
        self.pid = proc.pid
        self.pgid = pgid
        self.owner = owner
        self.status: ShellStatus = "running"
        self.exit_code: int | None = None
        self.lock = threading.Lock()
        self._script_path = script_path
        self._max_buffer_chars = max_buffer_chars
        # Rolling buffer over the logical line stream: ``_buffer`` holds the
        # retained tail; ``_dropped_total``/``_total_lines`` are absolute
        # line counts so the cursor survives drop-oldest evictions.
        self._buffer: deque[str] = deque()
        self._buffered_chars = 0
        self._dropped_total = 0
        self._total_lines = 0
        self._read_cursor = 0
        # Set (under ``lock``) before the group kill on every deliberate
        # termination path so the waiter can distinguish "killed" from
        # "completed" and suppress the exit callback.
        self._killed = False
        self._threads: list[threading.Thread] = []
        # Serializes whole read passes (snapshot → filter → commit).  The
        # buffer lock alone leaves a window where two concurrent reads of
        # the same shell snapshot the same cursor and BOTH return the delta
        # as new — double-delivering every line.  Held across the filter
        # subprocess too: correctness over parallel reads of one shell.
        self.read_serial = threading.Lock()
        # Monotonic EXIT order (registry-assigned by the waiter), None while
        # running.  Dead-record eviction sorts on this, never on spawn
        # order: a long-lived first-spawned server must not be the first
        # record evicted — least of all by its own exit's prune, which
        # would drop its promised exit notice and crash output unread.
        self._exit_seq: int | None = None

    @property
    def unread_lines(self) -> int:
        """Lines still READABLE that the cursor hasn't consumed — excludes
        lines the buffer cap already evicted, so an exit notice never
        promises more output than ``bash_output`` can actually return."""
        with self.lock:
            return self._total_lines - max(self._read_cursor, self._dropped_total)

    def _append(self, line: str) -> None:
        with self.lock:
            self._buffer.append(line)
            self._buffered_chars += len(line)
            self._total_lines += 1
            # Drop oldest whole lines past the cap, but always keep the
            # newest — a single oversized line must not empty the buffer.
            while self._buffered_chars > self._max_buffer_chars and len(self._buffer) > 1:
                dropped = self._buffer.popleft()
                self._buffered_chars -= len(dropped)
                self._dropped_total += 1

    def _snapshot_delta(self) -> tuple[list[str], int, int, ShellStatus, int | None]:
        """Snapshot unread lines WITHOUT consuming them.

        Returns ``(delta, gap, new_cursor, status, exit_code)``.  The caller
        commits ``new_cursor`` via :meth:`_commit_cursor` only after any
        filtering succeeded — a failed/timed-out filter must not eat output.
        """
        with self.lock:
            start = max(self._read_cursor, self._dropped_total)
            gap = start - self._read_cursor
            delta = list(itertools.islice(self._buffer, start - self._dropped_total, None))
            return delta, gap, self._total_lines, self.status, self.exit_code

    def _commit_cursor(self, new_cursor: int) -> None:
        with self.lock:
            # max(): monotonic under concurrent reads of the same scope.
            self._read_cursor = max(self._read_cursor, new_cursor)


class BackgroundShellRegistry:
    """Session-scoped table of background shells, ``bash_N``-keyed.

    Thread-safe: tool calls (spawn/read/kill), waiter threads (exit
    transitions), and teardown (close/reap) may interleave freely.
    ``on_exit`` fires from the waiter thread on NATURAL exit only — never
    for ``kill``/``reap``/``close`` — after the drains have flushed, so a
    read triggered by the callback sees the complete output.
    """

    def __init__(
        self,
        *,
        max_shells: int = _DEFAULT_MAX_SHELLS,
        max_buffer_chars: int = _DEFAULT_MAX_BUFFER_CHARS,
        max_exited_records: int = _MAX_EXITED_RECORDS,
        on_exit: Callable[[BackgroundShell], None] | None = None,
    ) -> None:
        self._max_shells = max_shells
        self._max_buffer_chars = max_buffer_chars
        self._max_exited_records = max_exited_records
        self._on_exit = on_exit
        self._shells: dict[str, BackgroundShell] = {}
        self._lock = threading.Lock()
        self._counter = 0
        self._exit_counter = 0
        self._closed = False

    # -- Spawning -----------------------------------------------------------

    def spawn(
        self,
        command: str,
        *,
        env: dict[str, str] | None = None,
        owner: str | None = None,
        stop_on_error: bool = False,
    ) -> BackgroundShell:
        """Start ``command`` as a detached shell; return its record.

        Raises ``RuntimeError`` after :meth:`close`, :class:`TooManyShellsError`
        at the live-shell cap, and propagates ``OSError`` from a failed spawn.
        """
        if env is None:
            from turnstone.core.env import scrubbed_env

            env = scrubbed_env()
        # Fast-fail before paying disk + fork; re-checked authoritatively
        # under the lock after the fork (spawn stays lock-free through the
        # slow syscalls so close()/reap() — which serialize on the registry
        # lock with a total time budget — can never be blocked behind a
        # stalled filesystem write or fork).
        with self._lock:
            self._check_capacity_locked(owner)
        # Shared prologue with the foreground bash tool — the waiter unlinks
        # the script after exit.
        proc, pgid, script_path = spawn_group_leader(command, stop_on_error=stop_on_error, env=env)
        try:
            with self._lock:
                # Authoritative re-check: a concurrent spawn/close may have
                # won the race while we were forking.  Refusal lands in the
                # outer handler, which reaps the freshly-forked group —
                # nothing may outlive a failed call (#816 rule).
                self._check_capacity_locked(owner)
                self._counter += 1
                shell = BackgroundShell(
                    shell_id=f"bash_{self._counter}",
                    command=command,
                    proc=proc,
                    pgid=pgid,
                    owner=owner,
                    script_path=script_path,
                    max_buffer_chars=self._max_buffer_chars,
                )
                # Publish, wire and START the threads under the registry
                # lock: close()/reap() take the same lock, so they can never
                # observe a registered shell whose threads aren't started
                # (they would "join" nothing and return while the drains /
                # waiter start up behind them).  Registration is popped on a
                # start failure IN the same hold, so a thread-exhausted node
                # (RLIMIT_NPROC) can't strand an orphan record whose
                # never-started Thread objects would make every later
                # ``join`` — hence every teardown — raise.  The thread
                # bodies only ever take ``shell.lock`` or re-take the
                # registry lock AFTER this hold is released (the waiter's
                # prune), so starting them here cannot deadlock.
                self._shells[shell.shell_id] = shell
                assert proc.stdout is not None and proc.stderr is not None
                out_thread = threading.Thread(
                    target=self._drain,
                    args=(proc.stdout, shell, False),
                    name=f"bg-shell-out-{shell.shell_id}",
                    daemon=True,
                )
                err_thread = threading.Thread(
                    target=self._drain,
                    args=(proc.stderr, shell, True),
                    name=f"bg-shell-err-{shell.shell_id}",
                    daemon=True,
                )
                waiter = threading.Thread(
                    target=self._wait_for_exit,
                    args=(shell, out_thread, err_thread),
                    name=f"bg-shell-wait-{shell.shell_id}",
                    daemon=True,
                )
                shell._threads = [out_thread, err_thread, waiter]
                try:
                    out_thread.start()
                    err_thread.start()
                    waiter.start()
                except BaseException:
                    self._shells.pop(shell.shell_id, None)
                    raise
        except BaseException:
            # Refused post-fork or thread start failed: reap the fresh group
            # (any started drain then EOFs and exits on its own) and surface
            # the original error to the tool layer.
            with contextlib.suppress(OSError, ProcessLookupError):
                os.killpg(pgid, signal.SIGKILL)
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=5)
            with contextlib.suppress(OSError):
                os.unlink(script_path)
            raise
        log.info(
            "bg_shell.spawned",
            shell_id=shell.shell_id,
            pid=shell.pid,
            owner=owner or "",
        )
        return shell

    def _check_capacity_locked(self, owner: str | None) -> None:
        """Raise if closed or at the live-shell cap.  Caller holds the lock."""
        if self._closed:
            raise RuntimeError("background shells unavailable: session is closing")
        live = [s for s in self._shells.values() if s.status == "running"]
        if len(live) < self._max_shells:
            return
        # The cap is registry-wide (it protects the node), but the advice
        # must be scope-honest: kill_shell is owner-scoped, so naming
        # another scope's ids would send the caller in circles.
        mine = [s.shell_id for s in live if s.owner == owner]
        others = len(live) - len(mine)
        if mine:
            detail = f"In your scope: {', '.join(mine)} — stop one with kill_shell"
            if others:
                detail += f"; {others} more belong to other agents"
            detail += "."
        else:
            detail = (
                f"All {others} belong to other agents' scopes and end when "
                "those agents finish; wait and retry."
            )
        raise TooManyShellsError(
            f"Background shell limit reached ({self._max_shells} running). {detail}"
        )

    @staticmethod
    def _drain(pipe: Any, shell: BackgroundShell, is_stderr: bool) -> None:
        drain_pipe_lines(
            pipe, lambda line: shell._append(f"[stderr] {line}" if is_stderr else line)
        )

    def _wait_for_exit(
        self,
        shell: BackgroundShell,
        out_thread: threading.Thread,
        err_thread: threading.Thread,
    ) -> None:
        """Waiter thread: block on the leader, then tear down the group.

        The kill-on-exit is what keeps the #816 guarantee: a child the
        command backgrounded dies with the command, and the drains hit EOF
        promptly instead of hanging on an inherited pipe write-end.
        """
        shell.proc.wait()
        with contextlib.suppress(OSError, ProcessLookupError):
            os.killpg(shell.pgid, signal.SIGKILL)
        out_thread.join(timeout=_DRAIN_JOIN_TIMEOUT_S)
        err_thread.join(timeout=_DRAIN_JOIN_TIMEOUT_S)
        if out_thread.is_alive() or err_thread.is_alive():
            log.warning("bg_shell.drain_leaked", shell_id=shell.shell_id, pid=shell.pid)
        with contextlib.suppress(OSError):
            os.unlink(shell._script_path)
        with shell.lock:
            shell.exit_code = shell.proc.returncode
            shell.status = "killed" if shell._killed else "completed"
            notify = not shell._killed
        with self._lock:
            self._exit_counter += 1
            shell._exit_seq = self._exit_counter
        log.info(
            "bg_shell.exited",
            shell_id=shell.shell_id,
            exit_code=shell.exit_code,
            status=shell.status,
        )
        self._prune_exited()
        if notify and self._on_exit is not None:
            try:
                self._on_exit(shell)
            except Exception:
                log.warning("bg_shell.on_exit_failed", shell_id=shell.shell_id, exc_info=True)

    def _prune_exited(self) -> None:
        """Drop the OLDEST-EXITED records past ``max_exited_records``.

        Exited records are kept so the model can read a finished shell's
        output later, but a workstream that backgrounds thousands of short
        jobs must not accumulate them (each can pin ``max_buffer_chars`` of
        buffer).  Eviction sorts on exit order, NOT spawn order — the shell
        whose exit triggered this prune is by definition the newest-exited
        and therefore never its own victim (its exit notice and unread
        output survive).  An exited shell whose ``_exit_seq`` isn't
        assigned yet (waiter mid-transition) sorts as newest for the same
        reason.
        """
        with self._lock:
            if self._closed:
                return
            exited = sorted(
                (s for s in self._shells.values() if s.status != "running"),
                key=lambda s: s._exit_seq if s._exit_seq is not None else float("inf"),
            )
            for stale in exited[: max(0, len(exited) - self._max_exited_records)]:
                self._shells.pop(stale.shell_id, None)

    # -- Lookup / reads ------------------------------------------------------

    def _get(self, shell_id: str, owner: str | None) -> BackgroundShell:
        with self._lock:
            shell = self._shells.get(shell_id)
            if shell is not None and shell.owner == owner:
                return shell
            visible = [
                f"{s.shell_id} ({s.status})" for s in self._shells.values() if s.owner == owner
            ]
        known = (
            f" Known shells: {', '.join(visible)}." if visible else " No background shells exist."
        )
        raise UnknownShellError(f"No background shell with id '{shell_id}'.{known}")

    def has(self, shell_id: str) -> bool:
        with self._lock:
            return shell_id in self._shells

    def shells(self, owner: str | None = None) -> list[BackgroundShell]:
        """Snapshot of the given scope's shells, in spawn order."""
        with self._lock:
            return [s for s in self._shells.values() if s.owner == owner]

    def read(
        self, shell_id: str, *, owner: str | None = None, filter_pattern: str | None = None
    ) -> ShellRead:
        """Return output produced since the last read of ``shell_id``.

        ``filter_pattern`` (a regex, ``search`` semantics per line — the
        tool-facing ``filter`` arg) narrows what is RETURNED, not what is
        consumed: on a successful read the cursor advances past the whole
        delta.  A failed or timed-out filter consumes NOTHING — the model
        can retry without the filter and still get its output.  Raises
        :class:`UnknownShellError` outside the caller's scope, ``re.error``
        for a bad pattern, and :class:`FilterTimeoutError` for a pattern
        that blows the time bound (catastrophic backtracking).
        """
        shell = self._get(shell_id, owner)
        pattern: re.Pattern[str] | None = None
        if filter_pattern:
            if len(filter_pattern) > _MAX_FILTER_PATTERN_CHARS:
                raise re.error(  # noqa: TRY003 — mirrors re.compile's own error type
                    f"filter pattern too long ({len(filter_pattern)} chars, "
                    f"max {_MAX_FILTER_PATTERN_CHARS})"
                )
            pattern = re.compile(filter_pattern)
        # Serialize the whole pass: concurrent reads of one shell (a
        # parallel tool batch) would otherwise snapshot the same cursor and
        # each return the full delta as "new".
        with shell.read_serial:
            delta, gap, new_cursor, status, exit_code = shell._snapshot_delta()
            clipped = 0
            if pattern is None:
                shown = delta
            else:
                # Raises FilterTimeoutError / FilterExecError BEFORE the
                # commit below — a failed filter consumes nothing.
                shown = _filter_lines_bounded(pattern, delta, shell.shell_id)
                clipped = sum(1 for ln in delta if len(ln) > _FILTER_MAX_LINE_CHARS)
            shell._commit_cursor(new_cursor)
        return ShellRead(
            shell_id=shell.shell_id,
            status=status,
            exit_code=exit_code,
            lines=shown,
            new_line_count=len(delta),
            dropped_lines=gap,
            clipped_lines=clipped,
        )

    # -- Termination ---------------------------------------------------------

    @staticmethod
    def _signal_group(shell: BackgroundShell) -> None:
        """SIGKILL the shell's group IFF its leader is still running.

        The liveness guard is load-bearing: a completed shell's ``pgid`` is
        an hours-stale snapshot the OS may have recycled to an unrelated
        process group — signalling it unconditionally would let the
        auto-approved ``kill_shell`` (or a routine ``close()``) SIGKILL
        another tenant's processes.  A leader that exits between the
        ``poll()`` and the ``killpg`` leaves the same microseconds-wide
        pid-wraparound TOCTOU as the foreground tool — accepted there,
        accepted here.  The guard also keeps a kill racing a natural exit
        honest: the waiter labels the shell ``completed`` (with its real
        exit code and notice) instead of ``killed``.
        """
        with shell.lock:
            if shell.proc.poll() is not None:
                return  # already exited — the waiter's own group kill ran/runs
            shell._killed = True
            # killpg INSIDE the lock: poll-and-signal is atomic wrt our own
            # bookkeeping (nothing can observe _killed without the signal
            # having been attempted).  The lock is never held around other
            # locks, so this cannot deadlock; the OS-level microseconds
            # pid-wraparound TOCTOU is the same accepted one as always.
            with contextlib.suppress(OSError, ProcessLookupError):
                os.killpg(shell.pgid, signal.SIGKILL)

    @staticmethod
    def _join_threads(shells: list[BackgroundShell], budget_s: float) -> bool:
        """Join every shell thread under ONE shared deadline; True if all done.

        The budget is total, not per-thread: teardown latency must not stack
        by shell count (``close()`` can run under the server's async close
        route — see :data:`_CLOSE_JOIN_BUDGET_S`).  Stragglers are daemons;
        the caller logs and abandons them.
        """
        deadline = time.monotonic() + budget_s
        done = True
        for shell in shells:
            for t in shell._threads:
                # suppress: joining a never-started Thread raises
                # RuntimeError.  spawn() unregisters on a start failure, so
                # this is pure belt — teardown must never die on a join.
                with contextlib.suppress(RuntimeError):
                    t.join(timeout=max(0.0, deadline - time.monotonic()))
                done = done and not t.is_alive()
        return done

    def kill(self, shell_id: str, *, owner: str | None = None) -> BackgroundShell:
        """SIGKILL ``shell_id``'s whole group; return its (updated) record.

        Suppresses the exit callback — the caller asked for this exit, so
        there is nothing to announce.  Killing an already-exited shell
        signals nothing (see :meth:`_signal_group`) and returns the record
        unchanged.  On return the record is usually terminal; a leader in
        uninterruptible sleep can still read ``running`` after the join
        budget — callers report that honestly rather than assuming.
        """
        shell = self._get(shell_id, owner)
        self._signal_group(shell)
        if not self._join_threads([shell], _WAITER_JOIN_TIMEOUT_S):
            log.warning("bg_shell.kill_join_timeout", shell_id=shell.shell_id, pid=shell.pid)
        return shell

    def reap(self, *, owner: str | None) -> None:
        """Kill every shell belonging to ``owner`` and drop their records.

        Used by the task_agent teardown: a sub-agent's shells are bound to
        the sub-agent's lifetime (never handed to the parent), and dropping
        the records keeps dead ``bash_N`` handles from cluttering scope
        listings.  Suppresses the exit callback for the shells it kills,
        same as :meth:`kill` — teardown is the caller's own act, there is
        nothing to announce.
        """
        with self._lock:
            mine = [s for s in self._shells.values() if s.owner == owner]
        for shell in mine:
            self._signal_group(shell)
        if not self._join_threads(mine, _WAITER_JOIN_TIMEOUT_S):
            log.warning("bg_shell.reap_join_timeout", owner=owner or "")
        with self._lock:
            for shell in mine:
                self._shells.pop(shell.shell_id, None)

    def signal_all(self) -> None:
        """SIGKILL every live shell's group WITHOUT joining or unregistering.

        The instant half of teardown, separated so multi-session frontends
        can bound their total exit latency: signal every session's groups
        first (microseconds each), then pay the join budgets — or, on an
        impatient Ctrl-C, signal alone still guarantees no process outlives
        the frontend even though the joins are skipped.  Liveness-guarded
        per shell (:meth:`_signal_group`), so completed shells' stale pgids
        are never touched.  Idempotent; :meth:`close` remains the complete
        teardown.
        """
        with self._lock:
            shells = list(self._shells.values())
        for shell in shells:
            self._signal_group(shell)

    def close(self) -> None:
        """Kill everything, join threads under a total budget, refuse spawns.

        Idempotent; called from ``ChatSession.close()`` (the funnel every
        workstream-teardown path runs through).  Signals are issued to all
        groups first (instant), then ONE shared join budget covers every
        thread — a pathological shell (escaped-group grandchild holding the
        pipes, D-state leader) delays teardown by at most
        :data:`_CLOSE_JOIN_BUDGET_S`, with the stragglers logged and left
        to die as daemons.  Records are dropped so a queued exit notice's
        ``valid_until`` predicate (``has(shell_id)``) goes stale and the
        drain discards it — nobody is left to read it.
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            shells = list(self._shells.values())
        for shell in shells:
            self._signal_group(shell)
        if not self._join_threads(shells, _CLOSE_JOIN_BUDGET_S):
            leaked = [t.name for s in shells for t in s._threads if t.is_alive()]
            log.warning("bg_shell.close_join_timeout", leaked=",".join(leaked))
        with self._lock:
            self._shells.clear()
