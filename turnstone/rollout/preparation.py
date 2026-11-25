"""Preparing a rollout's container before the model starts.

A task's files are put in when its container is up.  Some tasks need more
than files before the first model call: build leftovers and package caches
purged, a service started, a checkout brought to a known state.
:class:`BeforeRunCommand` is one such command, and ``Task.before_run`` lists
them; they are the counterpart of the after-run commands
(:class:`turnstone.rollout.verification.AfterRunCommand`).

They run in the episode's own container once the task's files are in place and
before any model call, in order, as root unless the command names another
user, each with its own time limit.  They are the task's commands, not the
model's:

- nothing of them reaches the model.  They are no tool calls: they are not in
  the messages, not in the tool log and not in the token record, and the
  pattern rules a rollout enforces on the model's tool calls are not asked;
- their time is start-up time.  It is neither the task's time limit nor one
  of the model's budgets;
- their results are in the record (``before_run``): exit status, the beginning
  and the end of what each wrote, and the seconds each took.

A command that ends with another status than 0, or does not end within its
time, means the run does not start: the container is not what the task
describes, and a model that worked in it would be scored for that.  The
commands after it are not run, no model call is made, and the record ends as
``before_run_failed``, a failure of the machinery.

A command must return.  What it leaves running stays, as long as that does
not hold the command's own output open (send a service's output to a file).
A command that runs out of time is left to the removal of the container.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from turnstone.rollout.container import Container


@dataclass
class BeforeRunCommand:
    """One command run in the episode's container before the first model call."""

    argv: list[str]
    # Root by default: what a task prepares is as a rule not the model's user's
    # to change.  It has the container's capabilities and no others.
    user: str = "0:0"
    timeout: int = 300
    # The most the record keeps of each output stream: its beginning and its
    # end, half of this each, with what lies between counted.
    max_output_chars: int = 16000


def run_before(container: Container, command: BeforeRunCommand) -> dict[str, Any]:
    """Run one before-run command in the episode's running container.

    Its exit status, output and time come back whatever they are; whether the
    run may start on them is the caller's to decide.  Raises
    :class:`~turnstone.rollout.container.ContainerLostError` when the container
    is gone.
    """
    half = max(command.max_output_chars // 2, 1)
    started = time.monotonic()
    done = container.exec(
        command.argv, timeout=command.timeout, limit=half, tail=half, user=command.user
    )
    return {
        "argv": list(command.argv),
        "user": command.user,
        "exit_code": done.returncode,
        "timed_out": done.timed_out,
        "seconds": round(time.monotonic() - started, 2),
        "stdout": done.shown("stdout"),
        "stderr": done.shown("stderr"),
        "stdout_bytes": done.stdout_total,
        "stderr_bytes": done.stderr_total,
    }


def before_run_failure(number: int, count: int, timeout: int, result: dict[str, Any]) -> str | None:
    """Why the run cannot start after before-run command *number* of *count*
    gave *result*; ``None`` when it can."""
    which = f"before-run command {number} of {count}"
    if result.get("timed_out"):
        return f"{which} did not end within {timeout} s"
    if result.get("exit_code") != 0:
        return f"{which} ended with exit code {result.get('exit_code')}"
    return None
