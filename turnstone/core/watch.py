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
import time
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
# Cap on delivery re-attempts for a fire whose workstream can't be
# reached (evicted + transiently unrestorable — all restore slots busy).
# On exhaustion the held reminder is dropped and one poll is charged to
# the watch's own ``max_polls`` budget: with budget left the watch stays
# ACTIVE and the fire cycle repeats on its normal cadence; with the
# budget spent it deactivates (loudly) — so a persistently unreachable
# workstream can never re-run its command forever.  A PERMANENT failure
# (:class:`WatchWorkstreamUnrestorable`, e.g. corrupt persona stamp)
# deactivates immediately without waiting out either budget.
MAX_DELIVERY_ATTEMPTS = 5
# Cap on the held-reminder retry delay.  Re-delivery is a cheap in-memory
# dispatch (never a command re-run), so it retries on a short cadence —
# ``min(interval_secs, this)`` — rather than the watch's own interval: a
# daily watch whose fire hit a busy restore slot must not sit on its
# reminder for 24 h when the cause clears in seconds.
DELIVERY_RETRY_CAP_SECS = 60
# Cap on concurrent restore paths in flight across the poll pool.  Kept
# below ``max_concurrent_polls`` so a burst of evicted-workstream fires
# can never occupy every poll slot on slow restores (leaving normal polls
# unserved) and can't drain the shared DB connection pool.  A poll that
# would exceed the cap DEFERS (holds its reminder, releases its slot)
# instead of blocking.
MAX_CONCURRENT_RESTORES = 2
# Age past which an in-flight restore's admission entry is presumed
# wedged inside ``restore_fn`` and ALERTED on (error log at every
# refused admission).  Ten minutes exceeds every configured storage /
# MCP timeout by an order of magnitude, so a genuine restore never
# trips it.  Deliberately detection-ONLY — the entry is never evicted:
# the wedged poll thread's pool slot is never released, so reclaiming
# its admission would just readmit a restore that can wedge ANOTHER
# pool thread on the same cause, converting this capped degraded state
# (restores blocked, normal polling intact) into total poll-pool
# collapse, one slot per threshold period.  Recovery from a genuine
# wedge is a process restart; the loud log is what tells the operator.
RESTORE_STALL_ALERT_SECS = 600.0
# Cap on ``stop()``'s in-flight-poll drain.  Shutdown runs under an
# external deadline (systemd's stop timeout defaults to 90 s), and the
# teardown steps AFTER the watch runner — state-writer drain, node
# deregistration — must still get their turn, so the drain waits
# ``min(tool_timeout, this) + 5`` rather than a full ``tool_timeout``
# (default 120 s).  An abandoned poll is loud and safe: its row stays
# due and re-polls on the next boot.
STOP_DRAIN_CAP_SECS = 30.0

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


# The structured fields that ride the ``watch_triggered`` system turn's
# ``_source_meta`` (delivered to the FE for the watch-result card).  ``output``
# is the raw (sanitized) shell output so the card body renders it alone, without
# re-showing the header / command lines that ``format_watch_message`` bakes into
# the turn's text ``content`` (which is what the model reads on the wire).
WATCH_REMINDER_OPTIONAL_KEYS = (
    "watch_name",
    "command",
    "output",
    "poll_count",
    "max_polls",
    "is_final",
)


def build_watch_reminder(
    name: str,
    command: str,
    output: str,
    poll_count: int,
    max_polls: int,
    elapsed_secs: float,
    stop_on: str | None,
    is_final: bool,
    reason: str,
) -> dict[str, Any]:
    """Build a structured ``watch_triggered`` reminder dict.

    Returns a dict with ``{type, text, watch_name, command, output,
    poll_count, max_polls, is_final}``.  The ``text`` field is the formatted body
    (same content :func:`format_watch_message` produces) and becomes the
    ``content`` of the first-class ``{"role": "system", "_source":
    "watch_triggered"}`` turn the drain seam emits — the model-facing prose, with
    the watch header / ``$ command`` / output all baked in.  The remaining fields
    ride as the turn's structured ``_source_meta`` so the FE rebuilds the
    watch-result card from them; ``output`` is carried separately so the card
    body shows the raw shell output alone (without re-printing the header /
    command the chrome already renders).  Both ``text`` and the structured fields
    derive from the same inputs, so they cannot drift.  Compaction / channel
    adapters keep seeing the human-readable shell output via the ``text`` field.
    """
    return {
        "type": "watch_triggered",
        "text": format_watch_message(
            name=name,
            command=command,
            output=output,
            poll_count=poll_count,
            max_polls=max_polls,
            elapsed_secs=elapsed_secs,
            stop_on=stop_on,
            is_final=is_final,
            reason=reason,
        ),
        "watch_name": name,
        "command": command,
        "output": output,
        "poll_count": poll_count,
        "max_polls": max_polls,
        "is_final": is_final,
    }


def _iso_in(seconds: float) -> str:
    """``now + seconds`` in the storage layer's naive-UTC second format."""
    return (datetime.now(UTC) + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S")


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


class WatchWorkstreamUnrestorable(Exception):  # noqa: N818
    """Raised by a ``restore_fn`` when a watch's workstream can NEVER be
    restored (e.g. a corrupt persona stamp the operator must fix), as
    opposed to a transient failure (all restore slots busy) which returns
    ``None``.  Signals :class:`WatchRunner` to stop retrying delivery and
    deactivate the watch immediately rather than burning the whole
    attempt budget on a cause that can't clear on its own.
    """


class WatchRunner:
    """Polls the database for due watches and dispatches results.

    Runs as a daemon thread in the server process, analogous to
    ``TaskScheduler`` in the console.

    Poll execution is CONCURRENT and bounded: the tick thread only
    enumerates due rows and hands each to a short-lived daemon thread
    gated by a semaphore (``max_concurrent_polls``), so one hung
    command — itself bounded by ``tool_timeout`` — delays at most one
    slot instead of head-of-line blocking every watch on the node.  A
    per-``watch_id`` in-flight set prevents double-polling rows that
    keep appearing in ``list_due_watches`` while their (slow) poll is
    still running.  When a fire can't reach its workstream, the built
    reminder is HELD and re-delivered on a short retry cadence
    (``min(interval_secs, DELIVERY_RETRY_CAP_SECS)``, never re-running
    the command); transient failures retry up to
    :data:`MAX_DELIVERY_ATTEMPTS`, after which one poll is charged to the
    watch's ``max_polls`` budget and the fire cycle repeats on its normal
    cadence — or, with the budget spent, the watch deactivates loudly.  A
    permanent :class:`WatchWorkstreamUnrestorable` deactivates it at
    once.  Restore is admission-controlled (per-ws_id
    dedup + a :data:`MAX_CONCURRENT_RESTORES` cap, both without blocking a
    poll slot) so two watches on one evicted workstream can't each spawn a
    live session and a restore burst can't starve the poll pool.
    """

    def __init__(
        self,
        storage: Any,
        node_id: str,
        *,
        check_interval: float = 15.0,
        tool_timeout: float = 30.0,
        max_concurrent_polls: int = 4,
        restore_fn: Callable[[str], Callable[[dict[str, Any], str], None] | None] | None = None,
    ) -> None:
        self._storage = storage
        self._node_id = node_id
        self._check_interval = check_interval
        self._tool_timeout = tool_timeout
        self._restore_fn = restore_fn
        # Bounded poll concurrency (see class docstring).  The slot is
        # acquired on the tick thread and released in ``_poll_one``'s
        # ``finally``; the in-flight set is keyed by watch_id and holds
        # entries for exactly the lifetime of their poll thread.
        self._poll_slots = threading.BoundedSemaphore(max_concurrent_polls)
        self._in_flight: set[str] = set()
        self._in_flight_lock = threading.Lock()

        self._dispatch_fns: dict[str, Callable[[dict[str, Any], str], None]] = {}
        self._dispatch_lock = threading.Lock()

        # Restore admission control.  The restore path (``manager.create``
        # + ``session.resume``) must not run twice for one ws_id, or two
        # watches on the same evicted workstream — polled on separate pool
        # threads — would each spawn a live auto-approved session racing
        # writes into one conversation history.  ``_restoring`` tracks the
        # ws_ids with a restore in flight; ``_restore_lock`` guards it but
        # is held only for the fast admit/reject check, NEVER across the
        # slow restore (which would pin the caller's poll slot and starve
        # the pool).  A poll is admitted only when its ws_id isn't already
        # restoring AND fewer than :data:`MAX_CONCURRENT_RESTORES` restores
        # are in flight; otherwise it DEFERS (holds its reminder, releases
        # its slot) and re-delivers on the capped retry cadence — by which
        # point the winning restore has registered a dispatch fn.  Values
        # are ``time.monotonic()`` admission stamps, used ONLY to alert on
        # wedged restores (:data:`RESTORE_STALL_ALERT_SECS`); entries are
        # removed solely by their own restore's ``finally``.
        self._restoring: dict[str, float] = {}
        self._restore_lock = threading.Lock()

        # Held reminders whose delivery failed, keyed by watch_id.  Value:
        # ``{"reminder", "update_fields", "attempts"}``.  Held deliveries
        # are always TERMINAL fires (``fired ⟹ is_final``, and reminders
        # are only built for final polls), so committing ``update_fields``
        # always deactivates the row.  A later tick re-DELIVERS the
        # reminder (never re-runs the command, so a transient stop_on
        # match survives) up to :data:`MAX_DELIVERY_ATTEMPTS`.  Access is
        # guarded, though the per-watch_id in-flight gate already
        # serialises pollers for a given watch_id.
        self._pending_delivery: dict[str, dict[str, Any]] = {}
        self._pending_delivery_lock = threading.Lock()

        # Watch ids whose terminal reminder has already been dispatched
        # but whose row write has not yet been confirmed.  Populated
        # between ``_dispatch_result`` and ``update_watch`` in
        # :meth:`_poll_watch`; on a subsequent tick the same row will
        # still appear in ``list_due_watches`` (active=1, next_poll
        # unchanged) — the guard at the top of ``_poll_watch`` retries
        # the row write WITHOUT re-dispatching.  Bounded by transient
        # storage failure depth (~MAX_WATCHES_PER_WS × num_ws).
        self._terminal_dispatched: set[str] = set()
        self._terminal_dispatched_lock = threading.Lock()

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
        # Drain in-flight polls so their storage writes land before
        # teardown.  A poll's command can block up to ``tool_timeout``, so
        # bound the wait on THAT (not the tick cadence): a poll that
        # started just before ``stop()`` may legitimately still be running
        # its command, and a ``check_interval``-based deadline would
        # abandon it mid-run and skip its commit.  The wait is capped at
        # :data:`STOP_DRAIN_CAP_SECS` though — shutdown itself runs under
        # an external deadline (systemd stop timeout), and the teardown
        # steps queued after us must still run.  On the deadline we
        # abandon loudly; the daemon threads die with the process and the
        # abandoned rows stay due (re-polled on next boot).
        deadline = time.monotonic() + min(self._tool_timeout, STOP_DRAIN_CAP_SECS) + 5
        while time.monotonic() < deadline:
            with self._in_flight_lock:
                if not self._in_flight:
                    break
            time.sleep(0.05)
        else:
            with self._in_flight_lock:
                leftover = len(self._in_flight)
            # Only warn on a genuine abandonment — the last poll can drain
            # in the same window the deadline is crossed, and a count=0
            # warning would be a false alarm for log-based monitoring.
            if leftover:
                log.warning("watch_runner.stop_abandoned_polls count=%d", leftover)
        log.info("watch_runner.stopped")

    # -- Dispatch function registry ------------------------------------------

    def set_dispatch_fn(self, ws_id: str, fn: Callable[[dict[str, Any], str], None]) -> None:
        """Register a per-workstream dispatch fn.

        The fn signature is ``(reminder, watch_id)``.  ``reminder`` is
        the structured dict returned by :func:`build_watch_reminder` —
        ``text`` carries the formatted body, the remaining fields ride
        as queue-entry metadata → sibling keys on the ``watch_triggered``
        system turn, surfaced in the operator bubble.  ``watch_id`` is passed for
        closures that need per-watch metadata in their queue plumbing
        (e.g. correlating a fire back to the originating row in logs);
        do NOT use it to gate delivery against
        ``storage.is_watch_active(watch_id)`` — see
        :meth:`ChatSession.set_watch_runner` for why that pattern
        races :meth:`_poll_watch`'s commit of ``active=False`` and
        drops fires the model was meant to see.
        """
        with self._dispatch_lock:
            self._dispatch_fns[ws_id] = fn

    def remove_dispatch_fn(
        self, ws_id: str, owner: Callable[[dict[str, Any], str], None] | None = None
    ) -> None:
        """Remove the registration for ``ws_id`` — with ``owner`` given,
        ONLY if the registered fn IS that closure.  Multiple live
        sessions can transiently serve one ws_id (a watch-restore shell
        vs a reopened pane; an in-session ``/resume`` of an id open in
        another pane), and a blind removal from one session's teardown
        would silently unregister the OTHER, still-live session — its
        next fire would then take the restore path and spawn a duplicate
        auto-approved session onto the live conversation.
        """
        with self._dispatch_lock:
            if owner is not None and self._dispatch_fns.get(ws_id) is not owner:
                return
            self._dispatch_fns.pop(ws_id, None)

    def get_dispatch_fn(self, ws_id: str) -> Callable[[dict[str, Any], str], None] | None:
        """Public accessor for the registered dispatch fn (used by
        server-side restore paths to thread the closure through
        ``WatchRunner.restore_fn`` after the workstream is rehydrated).
        Returns ``None`` if no dispatch fn is registered for ``ws_id``.
        """
        with self._dispatch_lock:
            return self._dispatch_fns.get(ws_id)

    def forget_terminal_dispatched(self, watch_id: str) -> None:
        """Discard ``watch_id`` from the runner's per-watch transient
        state (terminal-dispatched set AND any held pending delivery).
        Called by paths that take a watch out of
        :meth:`StorageBackend.list_due_watches` view independent of
        the runner's own poll (most importantly the user-cancel path
        in :meth:`ChatSession._exec_watch`).  Without this, a
        ``_poll_watch`` whose row write failed AFTER dispatch would
        leak ``watch_id`` in ``_terminal_dispatched`` indefinitely —
        the user-cancel writes ``next_poll=''`` which excludes the
        row from ``list_due_watches``, so the retry-deactivate branch
        at the top of :meth:`_poll_watch` never fires to clear the
        entry.  Held reminders are dropped for the same reason: a
        cancelled watch's row leaves the due view, so its pending
        re-delivery would never be retried and would leak.

        Call this AFTER the row write that takes the watch out of the
        active view: the delivery paths re-check ``is_watch_active``
        before stashing or dispatching, so with the write already
        visible a racing poll thread drops its own hold instead of
        re-stashing behind this clear.  (The residual
        check-before-write / stash-after-clear interleaving is mopped
        up by :meth:`_sweep_cancelled_holds` within one tick.)
        """
        with self._terminal_dispatched_lock:
            self._terminal_dispatched.discard(watch_id)
        self._clear_pending_delivery(watch_id)

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
        self._sweep_cancelled_holds()
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        due = self._storage.list_due_watches(now)
        for watch_row in due:
            if self._stop_event.is_set():
                break
            # Only poll watches owned by this node
            row_node = watch_row.get("node_id", "")
            if row_node and row_node != self._node_id:
                continue
            watch_id = str(watch_row.get("watch_id", ""))
            with self._in_flight_lock:
                if watch_id in self._in_flight:
                    # Still polling from a previous tick (slow command) —
                    # the row keeps listing as due until its update
                    # commits; don't double-poll it.
                    continue
                self._in_flight.add(watch_id)
            if not self._poll_slots.acquire(blocking=False):
                # Pool saturated: the remaining due rows stay due and the
                # next tick retries them — nothing is dropped, delivery is
                # just deferred by up to ``check_interval``.
                with self._in_flight_lock:
                    self._in_flight.discard(watch_id)
                log.debug("watch_runner.poll_slots_saturated")
                break
            try:
                threading.Thread(
                    target=self._poll_one,
                    args=(watch_row,),
                    daemon=True,
                    name=f"watch-poll-{watch_id[:8]}",
                ).start()
            except Exception:
                # Spawn failure (e.g. OS thread exhaustion) must not leak
                # the slot or the in-flight entry, and must NOT abort the
                # rest of this tick — the failed watch stays due and
                # retries next tick while its siblings still get polled
                # (a raise here would unwind out of the un-guarded
                # due-row loop and skip every remaining due watch).
                with self._in_flight_lock:
                    self._in_flight.discard(watch_id)
                self._poll_slots.release()
                log.exception("watch_runner.poll_spawn_failed", extra={"watch_id": watch_id})
                continue

    def _poll_one(self, watch_row: dict[str, Any]) -> None:
        """Run one ``_poll_watch`` on a pool thread.

        Always releases the slot and the in-flight entry, even on an
        unexpected raise — a leaked slot would shrink the pool for the
        rest of the process lifetime.
        """
        try:
            self._poll_watch(watch_row)
        except Exception:
            log.exception(
                "watch_runner.poll_error",
                extra={"watch_id": watch_row.get("watch_id")},
            )
        finally:
            with self._in_flight_lock:
                self._in_flight.discard(str(watch_row.get("watch_id", "")))
            self._poll_slots.release()

    def _poll_watch(self, watch_row: dict[str, Any]) -> None:
        watch_id = watch_row["watch_id"]
        ws_id = watch_row["ws_id"]
        command = watch_row["command"]
        stop_on = watch_row.get("stop_on")
        max_polls = watch_row.get("max_polls", DEFAULT_MAX_POLLS)
        poll_count = watch_row.get("poll_count", 0) + 1
        prev_output = watch_row.get("last_output")
        created = watch_row.get("created", "")

        # Re-poll of a row whose terminal reminder already shipped but
        # whose ``active=False`` write didn't land — retry just the row
        # write so the row stops appearing in ``list_due_watches``; do
        # NOT re-dispatch the reminder, which the model already saw.
        with self._terminal_dispatched_lock:
            already_dispatched = watch_id in self._terminal_dispatched
        if already_dispatched:
            try:
                self._storage.update_watch(watch_id, active=False, next_poll="")
                with self._terminal_dispatched_lock:
                    self._terminal_dispatched.discard(watch_id)
                # Belt-and-braces: no current path leaves an id in both
                # ``_terminal_dispatched`` AND ``_pending_delivery``
                # (``_redeliver_pending`` clears the hold BEFORE its
                # commit), but this branch deactivates the row — after
                # which it never re-lists — so any hold that ever DID
                # coexist with the terminal mark would leak forever
                # without this clear.  The mark means the reminder was
                # delivered; a coexisting hold is by definition stale.
                self._clear_pending_delivery(watch_id)
            except Exception:
                log.exception("watch_runner.retry_deactivate_failed", extra={"watch_id": watch_id})
            return

        # Held-reminder re-delivery: a prior poll fired but couldn't reach
        # this watch's workstream.  Re-DELIVER the stashed reminder without
        # re-running the command (so a stop_on match that was momentarily
        # true isn't lost to a fresh run), bounded by MAX_DELIVERY_ATTEMPTS.
        # The per-watch_id in-flight gate in _tick serialises POLLERS for
        # this watch_id; the user-cancel path's forget_terminal_dispatched
        # runs on a worker thread and CAN race this peek — which is why
        # _redeliver_pending re-checks the row's active state before
        # dispatching, the hold paths re-check before stashing, and
        # _sweep_cancelled_holds mops up any stash that still lands after
        # a cancel's clear.
        with self._pending_delivery_lock:
            pending = self._pending_delivery.get(watch_id)
        if pending is not None:
            self._redeliver_pending(watch_row, pending)
            return

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

        # Build the row update this poll intends to commit, UP FRONT, so a
        # delivery failure can stash it verbatim alongside the reminder
        # (see :meth:`_redeliver_pending`) instead of recomputing it at
        # re-delivery time.
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

        # Dispatch before committing the row update.  Belt-and-braces
        # given the rest of the fix (closure no longer wires a
        # ``valid_until`` predicate, cancel-by-name uses
        # :meth:`find_watch_by_name` which ignores the ``active``
        # filter): either order would deliver the reminder today, but
        # this ordering preserves the invariant against re-wiring an
        # ``is_watch_active`` predicate or adding a new
        # ``active``-filtered read on this hot path.  Combined with the
        # ``_terminal_dispatched`` guard above it also bounds the
        # duplicate-fire blast radius if the row write fails after the
        # reminder shipped.
        #
        # ``fired ⟹ is_final`` (see the assignment above), so reminders
        # are built ONLY for terminal polls — the held-delivery machinery
        # relies on that (a committed ``update_fields`` always
        # deactivates).
        if is_final:
            # Compute elapsed from created time
            elapsed_secs = 0.0
            if created:
                try:
                    created_dt = datetime.fromisoformat(created).replace(tzinfo=UTC)
                    elapsed_secs = (now - created_dt).total_seconds()
                except (ValueError, TypeError):
                    pass  # elapsed stays 0.0

            reminder = build_watch_reminder(
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
            try:
                delivered = self._dispatch_result(ws_id, reminder, watch_id)
            except WatchWorkstreamUnrestorable:
                # Permanent: the workstream can never be restored (corrupt
                # persona stamp / history gone).  Stash BEFORE the abandon
                # write — not for delivery (there is nowhere to deliver),
                # but so a failing deactivation write routes the next tick
                # into the redeliver path (which retries the WRITE) instead
                # of the still-active row re-listing into a fresh command
                # run every tick with the budget never advancing.  On a
                # successful write, _abandon_delivery clears the stash
                # immediately (write-then-clear).
                self._stash_pending_delivery(watch_id, reminder, update_fields, attempts=1)
                self._abandon_delivery(watch_id, ws_id, update_fields, reason="unrestorable")
                return
            if not delivered:
                # Transiently undeliverable (ws evicted + slots busy, or
                # restore admission deferred).  HOLD the built reminder +
                # its intended row update for re-delivery on the capped
                # retry cadence, advancing next_poll and durably charging
                # this fire's poll — no baseline advance and no command
                # re-run (which would lose a transient stop_on match);
                # see :meth:`_hold_delivery` for why the charge commits.
                self._hold_delivery(watch_row, reminder, update_fields, attempts=1)
                return
            self._commit_terminal_update(watch_id, update_fields)
        else:
            self._storage.update_watch(watch_id, **update_fields)

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

    def _try_dispatch_fn(self, ws_id: str, reminder: dict[str, Any], watch_id: str) -> bool | None:
        """Deliver via the registered dispatch fn, if one exists.

        Returns ``True`` (delivered), ``False`` (a fn is registered but it
        raised — the ws is live, so the caller must NOT restore, which
        would spawn a duplicate session), or ``None`` (no fn registered —
        the ws may be evicted and the caller should try to restore).
        """
        with self._dispatch_lock:
            fn = self._dispatch_fns.get(ws_id)
        if fn is None:
            return None
        try:
            fn(reminder, watch_id)
            return True
        except Exception:
            log.exception("watch_runner.dispatch_error", extra={"ws_id": ws_id})
            return False

    def _dispatch_result(self, ws_id: str, reminder: dict[str, Any], watch_id: str) -> bool:
        """Deliver a watch result to the owning workstream.

        ``reminder`` is the structured dict produced by
        :func:`build_watch_reminder` — ``text`` is the formatted body
        (matched by the dispatch closure's :func:`sanitize_payload`
        pass) and the optional fields ride as queue-entry metadata.

        Returns ``True`` iff a dispatch closure ran without raising — the
        registered one, or the one the restore path produced.  ``False``
        means the reminder was not delivered for a TRANSIENT reason (no fn
        + slots busy, restore admission deferred, or a live fn raised); the
        caller HOLDS the reminder and re-delivers on the capped retry
        cadence.  Raises
        :class:`WatchWorkstreamUnrestorable` for a PERMANENT failure so the
        caller can deactivate the watch instead of retrying.
        """
        delivered = self._try_dispatch_fn(ws_id, reminder, watch_id)
        if delivered is not None:
            return delivered

        # No dispatch fn — the workstream may be evicted.  Admit a restore
        # only when this ws isn't already restoring and the concurrent-
        # restore cap has room; hold ``_restore_lock`` for that fast check
        # ONLY (never across the slow restore, which would pin this poll
        # slot).  A rejected poll defers (holds + re-delivers on the
        # capped retry cadence).
        if self._restore_fn is None:
            log.warning(
                "watch_runner.dispatch_failed",
                extra={"ws_id": ws_id, "reason": "no dispatch fn and no restore_fn"},
            )
            return False

        deliver_after_admission = False
        with self._restore_lock:
            # Re-check: a restore that completed while we waited for the
            # lock may already have registered a fn for this ws_id.  This
            # is a PRESENCE check only — the dispatch closure itself can
            # block (it takes ``ws._lock`` and may spawn a wake thread),
            # and running it here would serialise every restore admission
            # on the node behind one delivery.  (Lock order: _restore_lock
            # → _dispatch_lock via get_dispatch_fn; nothing takes them in
            # the reverse order.)
            if self.get_dispatch_fn(ws_id) is not None:
                deliver_after_admission = True
            else:
                # Alert on admission entries past the stall threshold —
                # a restore wedged inside ``restore_fn`` (its poll thread
                # is blocked, so the release in the ``finally`` below can
                # never run) holds this capacity for the process
                # lifetime.  Detection ONLY: see
                # :data:`RESTORE_STALL_ALERT_SECS` for why reclaiming the
                # entry would make the failure strictly worse.
                now = time.monotonic()
                for rid, started in self._restoring.items():
                    if now - started > RESTORE_STALL_ALERT_SECS:
                        log.error(
                            "watch_runner.restore_admission_wedged",
                            extra={"ws_id": rid, "stalled_secs": round(now - started, 1)},
                        )
                if ws_id in self._restoring or len(self._restoring) >= MAX_CONCURRENT_RESTORES:
                    # Same-ws restore already running, or the pool is at
                    # its restore cap — defer rather than block a poll
                    # slot.
                    return False
                self._restoring[ws_id] = now
        if deliver_after_admission:
            # The fn appeared while we waited: deliver OUTSIDE the lock.
            # ``None`` (it vanished again — an eviction race) defers like
            # any other transient.
            return bool(self._try_dispatch_fn(ws_id, reminder, watch_id))

        try:
            restored_fn = self._restore_fn(ws_id)  # may raise WatchWorkstreamUnrestorable
        except WatchWorkstreamUnrestorable:
            raise  # permanent — caller deactivates the watch
        except Exception:
            log.exception("watch_runner.restore_error", extra={"ws_id": ws_id})
            return False
        finally:
            with self._restore_lock:
                self._restoring.pop(ws_id, None)

        if restored_fn is not None:
            try:
                restored_fn(reminder, watch_id)
                return True
            except Exception:
                log.exception("watch_runner.restore_dispatch_error", extra={"ws_id": ws_id})
                return False

        log.warning(
            "watch_runner.dispatch_failed",
            extra={"ws_id": ws_id, "reason": "restore produced no dispatch fn"},
        )
        return False

    # -- Held-reminder re-delivery -------------------------------------------
    #
    # Everything held here is a TERMINAL fire (``fired ⟹ is_final`` in
    # ``_poll_watch``, and reminders are built only for final polls), so a
    # committed ``update_fields`` always deactivates the row.  The delivery
    # outcome state machine — commit-with-terminal-mark, abandon, hold —
    # lives in the three helpers below so ``_poll_watch`` (fresh fire) and
    # ``_redeliver_pending`` can't drift apart on the fragile ordering.

    def _watch_still_active(self, watch_id: str) -> bool:
        """``True`` unless the row has CLEANLY left the active view (user
        cancel / deletion).  A storage error biases toward ``True``:
        delivery paths keep retrying on a blip — bounded by their own
        attempt and poll budgets — rather than dropping a fire.
        """
        try:
            return bool(self._storage.is_watch_active(watch_id))
        except Exception:
            return True

    def _stash_pending_delivery(
        self,
        watch_id: str,
        reminder: dict[str, Any],
        update_fields: dict[str, Any],
        *,
        attempts: int,
    ) -> None:
        """Hold ``reminder`` + its intended row update for re-delivery."""
        with self._pending_delivery_lock:
            self._pending_delivery[watch_id] = {
                "reminder": reminder,
                "update_fields": update_fields,
                "attempts": attempts,
            }

    def _clear_pending_delivery(self, watch_id: str) -> None:
        with self._pending_delivery_lock:
            self._pending_delivery.pop(watch_id, None)

    def _commit_terminal_update(self, watch_id: str, update_fields: dict[str, Any]) -> None:
        """Commit a DELIVERED terminal fire's row update with the
        ``_terminal_dispatched`` mark held across the write: if the write
        raises, the next tick routes into the retry-deactivate branch at
        the top of :meth:`_poll_watch` instead of re-firing a reminder the
        model already saw.
        """
        with self._terminal_dispatched_lock:
            self._terminal_dispatched.add(watch_id)
        self._storage.update_watch(watch_id, **update_fields)
        # Row write committed; the retry-deactivate branch will never be
        # reached for this watch_id.
        with self._terminal_dispatched_lock:
            self._terminal_dispatched.discard(watch_id)

    def _abandon_delivery(
        self,
        watch_id: str,
        ws_id: str,
        update_fields: dict[str, Any],
        *,
        reason: str,
        attempts: int | None = None,
    ) -> None:
        """Give up on delivering this fire: commit the intended row
        update (deactivating the terminal watch), then drop any held
        reminder.  Write-then-clear: a failed commit leaves the hold in
        place, so the next tick retries via the REDELIVER path (dispatch
        → same terminal outcome → retry this write) without re-running
        the command — clearing first would let the row re-list into a
        fresh command run every attempt-budget cycle, forever, whenever
        storage can read but not write (e.g. disk-full SQLite), with the
        poll budget never advancing.  The clear itself is pure in-memory
        and cannot fail after a successful commit, so no ordering leaks
        the hold.
        """
        self._storage.update_watch(watch_id, **update_fields)
        self._clear_pending_delivery(watch_id)
        extra: dict[str, Any] = {"watch_id": watch_id, "ws_id": ws_id, "reason": reason}
        if attempts is not None:
            extra["attempts"] = attempts
        log.error("watch_runner.delivery_abandoned", extra=extra)

    def _hold_delivery(
        self,
        watch_row: dict[str, Any],
        reminder: dict[str, Any],
        update_fields: dict[str, Any],
        *,
        attempts: int,
    ) -> None:
        """Stash the reminder + intended update and advance ``next_poll``
        (by the capped retry delay) so the row re-lists for re-delivery
        soon — without advancing its baseline or running its command.
        Re-delivery is a cheap in-memory dispatch, so it retries on
        ``min(interval_secs, DELIVERY_RETRY_CAP_SECS)`` rather than the
        watch's own interval: a daily watch must not sit on its fired
        reminder for 24 h because a restore slot was briefly busy.

        The poll charge (``update_fields["poll_count"]``) is committed
        alongside ``next_poll``: the hold itself lives only in this
        process, so a restart mid-hold re-lists the row and re-runs the
        (possibly side-effectful) command — with the charge durable those
        re-runs stay bounded by the watch's own ``max_polls``, matching
        the in-memory exhaustion path, plus at most ONE regeneration run
        per restart when the held fire had already spent the budget (the
        re-list runs the command before the cap check so the lost
        reminder is regenerated rather than silently dropped).  The
        baseline (``last_output``) stays uncommitted so a delta-style
        ``stop_on`` re-detects the change the model never saw.

        Dropped instead when the row has left the active view: the user
        cancelled while this fire was in flight, and a stash landing
        after the cancel path's :meth:`forget_terminal_dispatched` would
        leak the hold forever (an inactive row never re-lists to retry
        it) and violate that method's drop guarantee.
        """
        watch_id = watch_row["watch_id"]
        ws_id = watch_row["ws_id"]
        if not self._watch_still_active(watch_id):
            self._clear_pending_delivery(watch_id)
            log.info(
                "watch_runner.hold_dropped_cancelled",
                extra={"watch_id": watch_id, "ws_id": ws_id, "attempts": attempts},
            )
            return
        retry_secs = min(int(watch_row["interval_secs"]), DELIVERY_RETRY_CAP_SECS)
        self._stash_pending_delivery(watch_id, reminder, update_fields, attempts=attempts)
        retry_poll = _iso_in(retry_secs)
        self._storage.update_watch(
            watch_id,
            next_poll=retry_poll,
            poll_count=int(update_fields["poll_count"]),
        )
        log.warning(
            "watch_runner.delivery_deferred",
            extra={
                "watch_id": watch_id,
                "ws_id": ws_id,
                "attempts": attempts,
                "next_retry": retry_poll,
            },
        )

    def _redeliver_pending(self, watch_row: dict[str, Any], pending: dict[str, Any]) -> None:
        """Re-attempt delivery of a held reminder WITHOUT re-running the
        command.

        Five outcomes:

        * **Watch cancelled** — the user-cancel path raced the due
          listing.  Drop the hold and deliver nothing: cancel's
          :meth:`forget_terminal_dispatched` promises the held reminder
          is dropped, and delivering here could even RESTORE a session
          for a watch the user just cancelled.
        * **Delivered** — commit the row update stashed at fire time (which
          deactivates the terminal watch) and clear the hold.  The hold is
          cleared BEFORE the commit so that a commit failure can't strand
          it (the row then re-lists and the ``already_dispatched`` guard
          finishes deactivation).
        * **Permanent failure** (:class:`WatchWorkstreamUnrestorable`) —
          deactivate the watch now and drop the reminder; it can never be
          delivered.
        * **Transient failure past** :data:`MAX_DELIVERY_ATTEMPTS`, poll
          budget remaining — drop the held reminder, charge ONE poll to the
          watch's ``max_polls`` budget, and leave it ACTIVE on its normal
          cadence: a temporary cause (restore slots saturated under load)
          doesn't silently turn the watch off, it re-fires and re-attempts
          delivery next interval.  The baseline (``last_output``) is
          deliberately NOT committed, so a delta-style ``stop_on`` re-fires
          on the same change the model never saw.
        * **Transient exhaustion with the poll budget spent** — commit the
          held update (deactivates).  Without this bound a persistently
          unreachable workstream would re-run its command every interval
          forever, past the user's own ``max_polls``.
        """
        watch_id = watch_row["watch_id"]
        ws_id = watch_row["ws_id"]
        reminder = pending["reminder"]
        update_fields = pending["update_fields"]

        if not self._watch_still_active(watch_id):
            self._clear_pending_delivery(watch_id)
            log.info(
                "watch_runner.redelivery_dropped_cancelled",
                extra={"watch_id": watch_id, "ws_id": ws_id},
            )
            return

        try:
            delivered = self._dispatch_result(ws_id, reminder, watch_id)
        except WatchWorkstreamUnrestorable:
            self._abandon_delivery(watch_id, ws_id, update_fields, reason="unrestorable")
            return

        if delivered:
            # Clear the hold FIRST: if the commit below raises, the row
            # re-lists and the ``already_dispatched`` branch deactivates it
            # — with the hold already gone there's nothing to leak.
            self._clear_pending_delivery(watch_id)
            self._commit_terminal_update(watch_id, update_fields)
            log.info(
                "watch_runner.delivery_recovered",
                extra={"watch_id": watch_id, "ws_id": ws_id},
            )
            return

        attempts = int(pending["attempts"]) + 1
        if attempts >= MAX_DELIVERY_ATTEMPTS:
            poll_count = int(update_fields.get("poll_count", 0))
            max_polls = int(watch_row.get("max_polls", DEFAULT_MAX_POLLS))
            if poll_count >= max_polls:
                # Poll budget spent — deactivate rather than re-run the
                # command forever against an unreachable workstream.
                self._abandon_delivery(
                    watch_id,
                    ws_id,
                    update_fields,
                    reason="poll_budget_exhausted",
                    attempts=attempts,
                )
                return
            # Budget remains: charge this cycle's poll, then drop the hold
            # and let the watch re-fire on its own cadence.  next_poll uses
            # the FULL interval (a fresh command cycle, not a cheap
            # re-delivery) and the baseline stays uncommitted so the fire
            # re-detects.  Write-then-clear, mirroring _abandon_delivery: a
            # failed charge commit keeps the hold, so the next tick retries
            # THIS branch instead of the row re-listing into a fresh
            # command run with the budget never advancing.
            self._storage.update_watch(
                watch_id,
                poll_count=poll_count,
                next_poll=_iso_in(int(watch_row["interval_secs"])),
            )
            self._clear_pending_delivery(watch_id)
            log.warning(
                "watch_runner.delivery_abandoned",
                extra={
                    "watch_id": watch_id,
                    "ws_id": ws_id,
                    "attempts": attempts,
                    "reason": "transient_exhausted",
                    "watch_active": True,
                    "poll_count": poll_count,
                    "max_polls": max_polls,
                },
            )
            return

        self._hold_delivery(watch_row, reminder, update_fields, attempts=attempts)

    def _sweep_cancelled_holds(self) -> None:
        """Drop held deliveries whose rows have left the active view.

        The cancel paths call :meth:`forget_terminal_dispatched`, but a
        poll thread that already passed its own active re-check can
        re-stash a hold microseconds AFTER that clear — no ordering
        between the cancel's row write and the in-memory stash can
        prevent it without a per-watch lock spanning storage I/O.  An
        inactive row never re-lists, so nothing else would ever retry or
        drop such an entry; this tick-time sweep bounds the leak (and
        any post-cancel redelivery) to one ``check_interval``.  Iterates
        only currently-held ids — holds are rare and short-lived — so
        the steady-state per-tick cost is zero storage reads.
        """
        with self._pending_delivery_lock:
            held_ids = list(self._pending_delivery)
        for watch_id in held_ids:
            if not self._watch_still_active(watch_id):
                self._clear_pending_delivery(watch_id)
                log.info("watch_runner.hold_swept_cancelled", extra={"watch_id": watch_id})

    def _deactivate_watch(self, watch_id: str) -> None:
        self._storage.update_watch(watch_id, active=False, next_poll="")
