"""Watch — periodic command polling within a workstream.

A watch periodically runs a shell command and injects results back into the
conversation when a stop condition is met or the output changes.  The
``WatchRunner`` is a server-level daemon thread that polls the database for
due watches, runs their commands, and dispatches results.
"""

from __future__ import annotations

import contextlib
import json
import re
import subprocess
import threading
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from turnstone.core.log import get_logger
from turnstone.core.safety import is_command_blocked, sanitize_command

if TYPE_CHECKING:
    from collections.abc import Callable

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_WATCHES_PER_WS = 5
MIN_INTERVAL = 10  # seconds
MAX_INTERVAL = 86_400  # 24 hours
DEFAULT_MAX_POLLS = 100
MAX_OUTPUT_SIZE = 65_536  # truncate stored/dispatched output at 64 KB

# Safe builtins exposed to condition expressions.
_SAFE_BUILTINS: dict[str, Any] = {
    "len": len,
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
    "abs": abs,
    "min": min,
    "max": max,
    "any": any,
    "all": all,
    "isinstance": isinstance,
    "sorted": sorted,
    "True": True,
    "False": False,
    "None": None,
}

# ---------------------------------------------------------------------------
# Duration parsing
# ---------------------------------------------------------------------------

_DURATION_RE = re.compile(r"(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?\s*(?:(\d+)\s*s)?$", re.IGNORECASE)


def parse_duration(s: str) -> float:
    """Convert a duration string to seconds.

    Supported formats: ``"30s"``, ``"5m"``, ``"1h"``, ``"2h30m"``,
    ``"90"`` (bare number = seconds).

    Raises ``ValueError`` on invalid input.
    """
    s = s.strip()
    if not s:
        raise ValueError("empty duration string")

    # Bare number → seconds
    try:
        val = float(s)
    except ValueError:
        val = None
    if val is not None:
        if val <= 0:
            raise ValueError(f"duration must be positive, got {val}")
        return val

    m = _DURATION_RE.match(s)
    if not m or not any(m.groups()):
        raise ValueError(f"invalid duration format: {s!r}")

    hours = int(m.group(1) or 0)
    minutes = int(m.group(2) or 0)
    seconds = int(m.group(3) or 0)
    total = hours * 3600 + minutes * 60 + seconds
    if total <= 0:
        raise ValueError(f"duration must be positive, got {total}s")
    return float(total)


# ---------------------------------------------------------------------------
# Condition evaluation
# ---------------------------------------------------------------------------


def validate_condition(expr: str) -> str | None:
    """Syntax-check a condition expression.

    Returns an error message string, or ``None`` if the expression is valid.
    """
    try:
        compile(expr, "<watch>", "eval")
    except SyntaxError as exc:
        return f"invalid condition syntax: {exc}"
    return None


def evaluate_condition(
    expr: str | None,
    output: str,
    exit_code: int,
    prev_output: str | None,
) -> tuple[bool, str]:
    """Evaluate a stop condition.

    Returns ``(fired, reason)`` where *fired* is ``True`` when the watch
    should report a result and *reason* is a human-readable explanation.
    """
    changed = output != prev_output

    if expr is None:
        # Default: fire on any change (skip first poll where prev is None)
        if prev_output is None:
            return False, ""
        return changed, "output changed" if changed else ""

    # Build data context
    data: Any = None
    with contextlib.suppress(json.JSONDecodeError, ValueError):
        data = json.loads(output)

    context = {
        "output": output,
        "data": data,
        "exit_code": exit_code,
        "prev_output": prev_output,
        "changed": changed,
    }

    try:
        result = eval(expr, {"__builtins__": _SAFE_BUILTINS}, context)  # noqa: S307
        if result:
            return True, f"condition met: {expr}"
        return False, ""
    except Exception as exc:
        log.warning("watch.condition_error", extra={"expr": expr, "error": str(exc)})
        return False, f"condition error: {exc}"


# ---------------------------------------------------------------------------
# Message formatting
# ---------------------------------------------------------------------------


def format_watch_message(
    name: str,
    command: str,
    output: str,
    poll_count: int,
    max_polls: int,
    elapsed_secs: float,
    stop_on: str | None,
    is_final: bool,
    reason: str,
) -> str:
    """Format a watch result as a synthetic user message."""
    elapsed = format_interval(elapsed_secs)
    lines = [f'[Watch "{name}" \u2014 poll #{poll_count}/{max_polls}, {elapsed} elapsed]']

    # Show the condition so the model knows what this watch was waiting for
    if stop_on:
        lines.append(f"[condition: {stop_on}]")
    else:
        lines.append("[mode: fire on output change]")

    lines.append("")
    lines.append(f"$ {command}")
    lines.append(output)

    if is_final:
        if reason:
            lines.append("")
            lines.append(f"[{reason} \u2014 watch auto-cancelled]")
        else:
            lines.append("")
            lines.append("[max polls reached \u2014 watch auto-cancelled]")

    return "\n".join(lines)


def format_interval(secs: float) -> str:
    """Human-readable duration (e.g. ``'5m'``, ``'1h30m'``)."""
    if secs < 60:
        return f"{secs:.0f}s"
    if secs < 3600:
        return f"{secs / 60:.0f}m"
    hours = int(secs // 3600)
    mins = int((secs % 3600) // 60)
    if mins:
        return f"{hours}h{mins}m"
    return f"{hours}h"


# ---------------------------------------------------------------------------
# WatchRunner — server-level daemon thread
# ---------------------------------------------------------------------------


class WatchRunner:
    """Polls the database for due watches and dispatches results.

    Runs as a daemon thread in the server process, analogous to
    ``TaskScheduler`` in the console.
    """

    def __init__(
        self,
        storage: Any,
        node_id: str,
        *,
        check_interval: float = 15.0,
        tool_timeout: float = 30.0,
        restore_fn: Callable[[str], Callable[[str], None] | None] | None = None,
    ) -> None:
        self._storage = storage
        self._node_id = node_id
        self._check_interval = check_interval
        self._tool_timeout = tool_timeout
        self._restore_fn = restore_fn

        self._dispatch_fns: dict[str, Callable[[str], None]] = {}
        self._dispatch_lock = threading.Lock()

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    # -- Lifecycle -----------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="watch-runner")
        self._thread.start()
        log.info("watch_runner.started", extra={"node_id": self._node_id})

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=self._check_interval + 5)
            self._thread = None
        log.info("watch_runner.stopped")

    # -- Dispatch function registry ------------------------------------------

    def set_dispatch_fn(self, ws_id: str, fn: Callable[[str], None]) -> None:
        with self._dispatch_lock:
            self._dispatch_fns[ws_id] = fn

    def remove_dispatch_fn(self, ws_id: str) -> None:
        with self._dispatch_lock:
            self._dispatch_fns.pop(ws_id, None)

    # -- Main loop -----------------------------------------------------------

    def _run(self) -> None:
        from turnstone.core.storage._registry import StorageUnavailableError

        while not self._stop_event.is_set():
            try:
                self._tick()
            except StorageUnavailableError:
                pass  # already logged by storage layer
            except Exception:
                log.exception("watch_runner.tick_error")
            self._stop_event.wait(self._check_interval)

    def _tick(self) -> None:
        if self._storage is None:
            return
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        due = self._storage.list_due_watches(now)
        for watch_row in due:
            if self._stop_event.is_set():
                break
            # Only poll watches owned by this node
            row_node = watch_row.get("node_id", "")
            if row_node and row_node != self._node_id:
                continue
            try:
                self._poll_watch(watch_row)
            except Exception:
                log.exception(
                    "watch_runner.poll_error",
                    extra={"watch_id": watch_row.get("watch_id")},
                )

    def _poll_watch(self, watch_row: dict[str, Any]) -> None:
        watch_id = watch_row["watch_id"]
        ws_id = watch_row["ws_id"]
        command = watch_row["command"]
        stop_on = watch_row.get("stop_on")
        max_polls = watch_row.get("max_polls", DEFAULT_MAX_POLLS)
        poll_count = watch_row.get("poll_count", 0) + 1
        prev_output = watch_row.get("last_output")
        created = watch_row.get("created", "")

        # Safety check
        blocked = is_command_blocked(command)
        if blocked:
            log.warning(
                "watch_runner.blocked_command", extra={"watch_id": watch_id, "reason": blocked}
            )
            self._deactivate_watch(watch_id)
            return

        # Run command
        output, exit_code = self._run_command(sanitize_command(command))

        # Truncate to avoid unbounded storage / context window usage
        if len(output) > MAX_OUTPUT_SIZE:
            output = output[:MAX_OUTPUT_SIZE] + f"\n[truncated at {MAX_OUTPUT_SIZE} bytes]"

        # Evaluate condition
        fired, reason = evaluate_condition(stop_on, output, exit_code, prev_output)

        # Treat condition evaluation errors as terminal — don't silently
        # loop until max_polls while the user/model never sees the problem.
        if not fired and reason.startswith("condition error:"):
            fired = True

        # Check max polls
        is_final = fired or poll_count >= max_polls
        if not fired and poll_count >= max_polls:
            reason = "max polls reached"
            is_final = True

        now = datetime.now(UTC)
        now_str = now.strftime("%Y-%m-%dT%H:%M:%S")

        # Update DB
        update_fields: dict[str, Any] = {
            "poll_count": poll_count,
            "last_output": output,
            "last_exit_code": exit_code,
            "last_poll": now_str,
        }
        if is_final:
            update_fields["active"] = False
            update_fields["next_poll"] = ""
        else:
            next_poll = now + timedelta(seconds=watch_row["interval_secs"])
            update_fields["next_poll"] = next_poll.strftime("%Y-%m-%dT%H:%M:%S")
        self._storage.update_watch(watch_id, **update_fields)

        # Dispatch result if condition fired or final
        if fired or is_final:
            # Compute elapsed from created time
            elapsed_secs = 0.0
            if created:
                try:
                    created_dt = datetime.fromisoformat(created).replace(tzinfo=UTC)
                    elapsed_secs = (now - created_dt).total_seconds()
                except (ValueError, TypeError):
                    pass  # elapsed stays 0.0

            message = format_watch_message(
                name=watch_row["name"],
                command=command,
                output=output,
                poll_count=poll_count,
                max_polls=max_polls,
                elapsed_secs=elapsed_secs,
                stop_on=stop_on,
                is_final=is_final,
                reason=reason,
            )
            self._dispatch_result(ws_id, message)

        log.debug(
            "watch_runner.polled",
            extra={
                "watch_id": watch_id,
                "poll_count": poll_count,
                "fired": fired,
                "is_final": is_final,
            },
        )

    def _run_command(self, command: str) -> tuple[str, int]:
        """Run a shell command and return (stdout, exit_code)."""
        from turnstone.core.env import scrubbed_env

        try:
            proc = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=self._tool_timeout,
                start_new_session=True,
                env=scrubbed_env(),
            )
            output = proc.stdout
            if proc.stderr:
                output = output + "\n[stderr]\n" + proc.stderr if output else proc.stderr
            return output, proc.returncode
        except subprocess.TimeoutExpired:
            return f"[command timed out after {self._tool_timeout}s]", -1
        except Exception as exc:
            return f"[command failed: {exc}]", -1

    def _dispatch_result(self, ws_id: str, message: str) -> None:
        """Deliver a watch result to the owning workstream."""
        with self._dispatch_lock:
            fn = self._dispatch_fns.get(ws_id)

        if fn is not None:
            try:
                fn(message)
                return
            except Exception:
                log.exception("watch_runner.dispatch_error", extra={"ws_id": ws_id})

        # Workstream may be evicted — try to restore
        if self._restore_fn is not None:
            try:
                restored_fn = self._restore_fn(ws_id)
                if restored_fn is not None:
                    restored_fn(message)
                    return
            except Exception:
                log.exception("watch_runner.restore_error", extra={"ws_id": ws_id})

        log.warning(
            "watch_runner.dispatch_failed",
            extra={"ws_id": ws_id, "reason": "no dispatch function and restore failed"},
        )

    def _deactivate_watch(self, watch_id: str) -> None:
        self._storage.update_watch(watch_id, active=False, next_poll="")
