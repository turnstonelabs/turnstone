"""Background task scheduler for timed workstream dispatch.

Runs as a daemon thread inside the console process. Checks for due tasks
every ``check_interval`` seconds and dispatches them to server nodes via
the :class:`~turnstone.sdk.server.TurnstoneServer` SDK client.

Uses a ``system_settings`` row for distributed locking in multi-console
deployments.
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import TYPE_CHECKING, Any

import httpx
import structlog

from turnstone.console.schedule_timing import TS_FMT, compute_next_run, no_next_run_reason
from turnstone.sdk._types import TurnstoneAPIError
from turnstone.sdk.server import TurnstoneServer

if TYPE_CHECKING:
    from turnstone.console.collector import ClusterCollector
    from turnstone.core.auth import ServiceTokenManager
    from turnstone.core.storage._protocol import StorageBackend

log = structlog.get_logger(__name__)

# The system_settings row of the firings held for retry, beside the
# scheduler_lock row: one JSON object, task id to hold.
_HOLDS_KEY = "scheduler_holds"

# Raised before the request is sent, so nothing reached the node: a
# connection that never opened, a pool or proxy that refused it, a URL httpx
# will not send to.  A failure after sending may have made the workstream.
_NEVER_SENT = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    httpx.ProxyError,
    httpx.UnsupportedProtocol,
    httpx.InvalidURL,
)


class _Outcome(Enum):
    """What one dispatch attempt established about the workstream."""

    CREATED = "created"
    NOT_CREATED = "not_created"
    """Certainly not: no node to send to, a connection that never opened,
    or a node's 4xx answer.  Another attempt may do better."""
    UNKNOWN = "unknown"
    """The request left and no answer says: a lost reply, a connection
    dropped mid-request, a 5xx.  Another attempt could create a second."""


@dataclass
class _Hold:
    """A firing kept due for retry: which firing (its due time), when its
    first attempt failed, and its latest."""

    due: str
    since: datetime
    last_attempt: datetime


def _pick_best_node(collector: ClusterCollector) -> str:
    """Select the reachable node with the most available capacity."""
    nodes, _ = collector.get_nodes(sort_by="activity", limit=1000, offset=0)
    best_id = ""
    best_headroom = -1
    for n in nodes:
        if not n.get("reachable", False):
            continue
        headroom = n.get("max_ws", 10) - n.get("ws_total", 0)
        if headroom > best_headroom:
            best_headroom = headroom
            best_id = n["node_id"]
    return best_id


class TaskScheduler:
    """Background scheduler for dispatching timed workstreams."""

    def __init__(
        self,
        collector: ClusterCollector,
        storage: StorageBackend,
        check_interval: float = 15.0,
        lock_ttl: int = 60,
        max_fan_out: int = 20,
        retry_window: float = 300.0,
        retry_interval: float = 60.0,
        api_token: str = "",
        token_manager: ServiceTokenManager | None = None,
    ) -> None:
        self._collector = collector
        self._storage = storage
        self._check_interval = check_interval
        self._lock_ttl = lock_ttl
        self._max_fan_out = max_fan_out
        self._retry_window = timedelta(seconds=retry_window)
        self._retry_interval = timedelta(seconds=retry_interval)
        # The firings held for retry, by task id: loaded from storage at the
        # start of each tick and written back once at its end, under the
        # lock, so every console paces and times a hold the same way and a
        # restart does not restart its window.
        self._holds: dict[str, _Hold] = {}
        self._holds_dirty = False
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._tick_count = 0
        self._prune_every = 240  # ~1 hour at 15s intervals
        self._lock_owner = uuid.uuid4().hex
        self._api_token = api_token
        self._token_manager = token_manager
        self._sdk_clients: dict[str, TurnstoneServer] = {}
        self._last_token: str = ""

    def start(self) -> None:
        """Start the scheduler daemon thread."""
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="scheduler")
        self._thread.start()
        log.info("scheduler.started", check_interval=self._check_interval)

    def stop(self) -> None:
        """Stop the scheduler and wait for the thread to finish."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        for client in self._sdk_clients.values():
            client.close()
        self._sdk_clients.clear()
        log.info("scheduler.stopped")

    def _loop(self) -> None:
        """Main scheduler loop — tick then sleep."""
        from turnstone.core.storage._registry import StorageUnavailableError

        while not self._stop_event.is_set():
            try:
                self._tick()
            except StorageUnavailableError:
                pass  # already logged by storage layer
            except Exception:
                log.exception("scheduler.tick_error")
            self._stop_event.wait(self._check_interval)

    def _try_acquire_lock(self) -> bool:
        """Try to acquire the scheduler lock via system_settings.

        Uses a row with key ``scheduler_lock``.  The value is a JSON
        object ``{"owner": "<id>", "acquired": "<iso>"}``.  Another
        instance's lock is considered expired when its timestamp is
        older than ``_lock_ttl`` seconds.

        To reduce the TOCTOU window of a read-then-write approach, this
        method writes unconditionally and reads back to verify ownership.
        If two schedulers race, one write wins and the loser sees the
        winner's value on read-back.  The race window is microseconds
        (write + read-back) which is acceptable for 15s tick intervals.
        """
        now = datetime.now(UTC)
        now_str = now.strftime(TS_FMT)

        # Check if another instance holds a non-expired lock before
        # attempting to overwrite it.
        existing = self._storage.get_system_setting("scheduler_lock")
        if existing is not None:
            try:
                lock_data = json.loads(existing.get("value", "{}"))
            except (json.JSONDecodeError, TypeError):
                lock_data = {}
            owner = lock_data.get("owner", "")
            acquired_str = lock_data.get("acquired", "")
            if owner != self._lock_owner and acquired_str:
                try:
                    acquired_dt = datetime.strptime(acquired_str, TS_FMT).replace(tzinfo=UTC)
                    if (now - acquired_dt).total_seconds() < self._lock_ttl:
                        return False  # Another instance holds a valid lock
                except ValueError:
                    pass  # Malformed timestamp — take the lock

        # Write our lock and read back to verify we won any concurrent race.
        lock_value = json.dumps({"owner": self._lock_owner, "acquired": now_str})
        self._storage.upsert_system_setting("scheduler_lock", lock_value)
        return self._owns_lock()

    def _owns_lock(self) -> bool:
        """Whether the lock row still names this instance.  A tick that has
        outlived the TTL finds another owner here, and must not write."""
        existing = self._storage.get_system_setting("scheduler_lock")
        if existing is None:
            return False
        try:
            lock_data = json.loads(existing.get("value", "{}"))
        except (json.JSONDecodeError, TypeError):
            return False
        return bool(lock_data.get("owner") == self._lock_owner)

    def _release_lock(self) -> None:
        """Release the scheduler lock if we still own it."""
        if self._owns_lock():
            self._storage.delete_system_setting("scheduler_lock")

    def _tick(self) -> None:
        """Single scheduler iteration: acquire lock, query due tasks, dispatch."""
        now = datetime.now(UTC).strftime(TS_FMT)

        if not self._try_acquire_lock():
            return

        try:
            loaded = self._load_holds()
            if loaded is not None:
                self._holds = loaded
            self._holds_dirty = False
            due_tasks = self._storage.list_due_tasks(now)
            for task in due_tasks:
                try:
                    self._dispatch_task(task, now)
                except Exception:
                    # One bad row must not starve the rest of the tick: an
                    # unadvanced next_run would make it the earliest due
                    # task again next tick, and every tick after.
                    log.exception("scheduler.dispatch_error", task_id=task.get("task_id", ""))
            self._prune_holds({t["task_id"] for t in due_tasks})
            if self._holds_dirty:
                self._save_holds()

            # Periodic run history pruning (~once per hour)
            self._tick_count += 1
            if self._tick_count % self._prune_every == 0:
                pruned = self._storage.prune_task_runs(retention_days=90)
                if pruned:
                    log.info("scheduler.pruned_runs", count=pruned)
                try:
                    usage_pruned = self._storage.prune_usage_events(retention_days=90)
                    if usage_pruned:
                        log.info("scheduler.pruned_usage", count=usage_pruned)
                except Exception:
                    log.warning("scheduler.prune_usage_error", exc_info=True)
                try:
                    audit_pruned = self._storage.prune_audit_events(retention_days=365)
                    if audit_pruned:
                        log.info("scheduler.pruned_audit", count=audit_pruned)
                except Exception:
                    log.warning("scheduler.prune_audit_error", exc_info=True)
                # Prune SDK clients for nodes no longer in the cluster
                if self._sdk_clients and self._collector:
                    live_urls = {n.get("server_url", "") for n in self._collector.get_all_nodes()}
                    stale = [u for u in self._sdk_clients if u not in live_urls]
                    for url in stale:
                        self._sdk_clients.pop(url).close()
                    if stale:
                        log.info("scheduler.pruned_sdk_clients", count=len(stale))
        finally:
            self._release_lock()

    def _dispatch_task(self, task: dict[str, Any], now: str) -> None:
        """Dispatch a single task as one or more CreateWorkstreamMessages,
        then advance its schedule — or hold it for a retry."""
        target_mode = task["target_mode"]
        task_id = task["task_id"]
        now_dt = datetime.strptime(now, TS_FMT)

        hold = self._holds.get(task_id)
        if hold is not None and hold.due != task["next_run"]:
            hold = None  # re-timed since: this is a different firing
        # Held, and not yet time for the next attempt.  A clock behind the
        # console that wrote the hold sees a negative wait, and attempts
        # rather than waiting for a moment that already passed.
        if hold is not None and timedelta(0) <= now_dt - hold.last_attempt < self._retry_interval:
            return

        if target_mode == "all":
            outcome, error = self._dispatch_to_all(task, now)
        elif target_mode in ("auto", "pool"):
            outcome, error = self._dispatch_to_pool(task, now)
        else:
            # Specific node_id
            outcome, error = self._dispatch_to_node(task, target_mode, now)

        if error:
            if outcome is _Outcome.UNKNOWN:
                error += (
                    "; whether the workstream was created is not known, "
                    "so the firing was not retried"
                )
            self._record_failure(task, now, error)

        if outcome is _Outcome.NOT_CREATED:
            # next_run stays at the due time, so the firing stays due and is
            # attempted again: right for a node mid-restart.  The hold paces
            # the attempts (retry_interval) and bounds them: retry_window
            # after the first failure the firing is given up, so a node that
            # never comes back is not attempted for ever.  The window is
            # judged after an attempt, never instead of one, so a console
            # back from a long gap still tries the node once before giving
            # up on it.
            if hold is None:
                hold = _Hold(due=task["next_run"], since=now_dt, last_attempt=now_dt)
            else:
                hold.last_attempt = now_dt
            self._holds[task_id] = hold
            self._holds_dirty = True
            if now_dt - hold.since < self._retry_window:
                return
            log.warning(
                "scheduler.firing_abandoned",
                task_id=task_id,
                retry_window=self._retry_window.total_seconds(),
            )

        self._drop_hold(task_id)
        if outcome is _Outcome.UNKNOWN:
            # Retrying could run the job twice; a firing possibly missed is
            # the lesser harm.  The failed row says so.
            log.warning("scheduler.dispatch_unresolved", task_id=task_id)
        self._advance(task, now, outcome)

    def _advance(self, task: dict[str, Any], now: str, outcome: _Outcome) -> None:
        """Move the schedule past this firing.  last_run only when a node
        created the workstream; otherwise nothing is known to have run.
        Either way the next firing is walked from the clock, as it always
        was."""
        task_id = task["task_id"]
        dispatched = outcome is _Outcome.CREATED
        ran: dict[str, Any] = {"last_run": now} if dispatched else {}
        reason = ""
        if task["schedule_type"] == "at":
            next_run = ""
            if outcome is _Outcome.NOT_CREATED:
                reason = "the firing was given up; set a new time to run it again"
            elif outcome is _Outcome.UNKNOWN:
                # Not an invitation to re-arm: the job may have run.
                reason = (
                    "whether the firing ran is not known; check the node before setting a new time"
                )
        else:
            next_run = self._compute_next_run(task)
            if not next_run:
                # The cron walk found no next firing — a zone this host can
                # no longer resolve, or an expression with no future date.
                # Disable the schedule so the shelf shows it stopped, and say
                # why in its run history; re-enabling it re-validates the
                # stored timing.  The state change comes first: it is what
                # stops the re-dispatch, so it must not wait on the history
                # write.  A transient fault in reading the zone database
                # disables too: leaving next_run alone would re-create the
                # workstream every tick for as long as the fault lasts, and
                # no other advance is computable without the zone.
                # Re-enabling is one click once the host is right.
                reason = no_next_run_reason(
                    task.get("cron_expr", ""), task.get("timezone") or "UTC"
                )
        if next_run:
            self._storage.update_scheduled_task(task_id, next_run=next_run, **ran)
        else:
            self._storage.update_scheduled_task(task_id, next_run="", enabled=False, **ran)
            if reason:
                self._record_disabled(task, now, reason)

        if not dispatched:
            return
        log_kw: dict[str, Any] = {
            "task_id": task_id,
            "target_mode": task["target_mode"],
            "schedule_type": task["schedule_type"],
            "created_by": task.get("created_by", ""),
        }
        if task.get("auto_approve", 0):
            log_kw["auto_approve"] = True
            log_kw["auto_approve_tools"] = task.get("auto_approve_tools", "")
            log.warning("scheduler.task_dispatched_auto_approve", **log_kw)
        else:
            log.info("scheduler.task_dispatched", **log_kw)

    # -- holds ---------------------------------------------------------------

    def _load_holds(self) -> dict[str, _Hold] | None:
        """The held firings, from storage.  No row is no holds.  A row that
        cannot be read is None: the caller keeps what it has in memory, so
        a broken row costs this console its shared view of the holds, not
        the bound on its own attempts."""
        row = self._storage.get_system_setting(_HOLDS_KEY)
        if row is None:
            return {}
        try:
            data = json.loads(row.get("value", "{}"))
            return {
                task_id: _Hold(
                    due=str(h["due"]),
                    since=datetime.strptime(h["since"], TS_FMT),
                    last_attempt=datetime.strptime(h["last_attempt"], TS_FMT),
                )
                for task_id, h in data.items()
            }
        except (AttributeError, KeyError, TypeError, ValueError):
            log.warning("scheduler.holds_unreadable", exc_info=True)
            return None

    def _save_holds(self) -> None:
        """Write the held firings back, once per tick.  Only while this
        instance still holds the lock: a tick that outlived the TTL would
        otherwise overwrite the holds another console has since advanced.
        A lost write costs another attempt or two, never a firing, so it is
        logged rather than raised; the copy in memory stays."""
        if not self._owns_lock():
            log.warning("scheduler.holds_write_skipped", reason="lock lost")
            return
        try:
            if not self._holds:
                self._storage.delete_system_setting(_HOLDS_KEY)
                return
            self._storage.upsert_system_setting(
                _HOLDS_KEY,
                json.dumps(
                    {
                        task_id: {
                            "due": h.due,
                            "since": h.since.strftime(TS_FMT),
                            "last_attempt": h.last_attempt.strftime(TS_FMT),
                        }
                        for task_id, h in self._holds.items()
                    }
                ),
            )
        except Exception:
            log.warning("scheduler.holds_write_failed", exc_info=True)
            return
        self._holds_dirty = False

    def _drop_hold(self, task_id: str) -> None:
        if self._holds.pop(task_id, None) is not None:
            self._holds_dirty = True

    def _prune_holds(self, seen: set[str]) -> None:
        """Drop the holds of firings that are over: the task deleted,
        disabled or re-timed since.  Checked against the task row rather
        than the due page, whose limit can leave a still-held task unlisted
        for a tick."""
        stale = []
        for task_id, hold in self._holds.items():
            if task_id in seen:
                continue
            task = self._storage.get_scheduled_task(task_id)
            if task is None or not task.get("enabled") or task.get("next_run") != hold.due:
                stale.append(task_id)
        for task_id in stale:
            del self._holds[task_id]
            self._holds_dirty = True

    # -- dispatch ------------------------------------------------------------

    @staticmethod
    def _parse_tools(task: dict[str, Any]) -> list[str]:
        raw = task.get("auto_approve_tools", "")
        return [t.strip() for t in raw.split(",") if t.strip()]

    def _get_sdk_client(self, node_url: str) -> TurnstoneServer:
        """Return a cached :class:`TurnstoneServer` for *node_url*.

        When a :class:`ServiceTokenManager` is configured, the client is
        re-created whenever the token rotates so that fresh JWTs are used.
        """
        token = self._api_token
        if self._token_manager is not None:
            token = self._token_manager.token

        if token != self._last_token:
            # Token rotated — close all stale clients.
            for client in self._sdk_clients.values():
                client.close()
            self._sdk_clients.clear()
            self._last_token = token

        if node_url not in self._sdk_clients:
            self._sdk_clients[node_url] = TurnstoneServer(
                base_url=node_url,
                token=token,
            )
        return self._sdk_clients[node_url]

    def _get_node_url(self, node_id: str) -> str:
        """Resolve a node_id to its server URL via the collector."""
        detail = self._collector.get_node_detail(node_id)
        if detail:
            url: str = detail.get("server_url", "")
            return url
        return ""

    def _dispatch_to_node(
        self, task: dict[str, Any], node_id: str, now: str
    ) -> tuple[_Outcome, str]:
        """Dispatch a workstream to a specific node via the SDK client.

        Returns the outcome and, short of created, what happened, for the
        run history.  The line between the two failure outcomes is whether
        the request left: a connection that never opened and a node's 4xx
        answer both mean nothing was made, while anything after that may
        have made the workstream.
        """
        server_url = self._get_node_url(node_id)
        if not server_url:
            return _Outcome.NOT_CREATED, f"No URL for node {node_id}"

        try:
            client = self._get_sdk_client(server_url)
        except Exception as exc:
            # A URL httpx will not take, or a token that will not mint:
            # nothing was sent.  Classified rather than raised, so a fan-out
            # keeps the nodes that already created their workstream.
            log.warning("scheduler.sdk_client_failed", node_id=node_id, exc_info=True)
            return _Outcome.NOT_CREATED, f"{node_id}: no client ({type(exc).__name__})"

        correlation_id = uuid.uuid4().hex
        try:
            resp = client.create_workstream(
                name=task["name"],
                required_node_id=(
                    node_id if task.get("target_mode", "auto") not in {"auto", "pool"} else None
                ),
                model=task.get("model", ""),
                initial_message=task["initial_message"],
                auto_approve=bool(task.get("auto_approve", 0)),
                auto_approve_tools=",".join(self._parse_tools(task)),
                user_id=task.get("created_by", ""),
                skill=task.get("skill", ""),
                persona=task.get("persona", ""),
                project_id=task.get("project_id", ""),
                notify_targets=task.get("notify_targets", "[]"),
                # Mark the resulting ChatSession as non-interactive-for-
                # consent so OAuth-MCP errors get persisted to
                # ``mcp_pending_consent`` for later dashboard surfacing,
                # rather than relying on an in-flight SSE redirect the
                # absent user can't complete.
                client_type="scheduled",
            )
            ws_id = resp.ws_id
        except _NEVER_SENT as exc:
            log.warning("scheduler.sdk_dispatch_failed", node_id=node_id, exc_info=True)
            return _Outcome.NOT_CREATED, f"{node_id} could not be reached ({type(exc).__name__})"
        except TurnstoneAPIError as exc:
            log.warning("scheduler.sdk_dispatch_failed", node_id=node_id, exc_info=True)
            if exc.status_code < 500:
                return _Outcome.NOT_CREATED, f"{node_id} answered {exc}"
            return _Outcome.UNKNOWN, f"{node_id} answered {exc}"
        except Exception as exc:
            log.warning("scheduler.sdk_dispatch_failed", node_id=node_id, exc_info=True)
            return _Outcome.UNKNOWN, f"{node_id} gave no answer ({type(exc).__name__})"

        self._write_run(
            task_id=task["task_id"],
            node_id=node_id,
            ws_id=ws_id,
            correlation_id=correlation_id,
            started=now,
            status="dispatched",
            error="",
        )
        return _Outcome.CREATED, ""

    def _dispatch_to_pool(self, task: dict[str, Any], now: str) -> tuple[_Outcome, str]:
        """Dispatch to the reachable node with the most headroom (the auto
        and pool modes)."""
        node_id = _pick_best_node(self._collector)
        if not node_id:
            return _Outcome.NOT_CREATED, "No reachable nodes"
        return self._dispatch_to_node(task, node_id, now)

    def _dispatch_to_all(self, task: dict[str, Any], now: str) -> tuple[_Outcome, str]:
        """Fan out to every reachable node, up to max_fan_out.  One run row
        per attempt names every node that failed."""
        nodes, _ = self._collector.get_nodes(sort_by="activity", limit=1000, offset=0)
        reachable = [n["node_id"] for n in nodes if n.get("reachable", False)]
        if not reachable:
            return _Outcome.NOT_CREATED, "No reachable nodes for fan-out"
        if len(reachable) > self._max_fan_out:
            log.warning(
                "scheduler.fan_out_capped", task_id=task["task_id"], max_fan_out=self._max_fan_out
            )
        outcomes: list[_Outcome] = []
        errors: list[str] = []
        for node_id in reachable[: self._max_fan_out]:
            outcome, error = self._dispatch_to_node(task, node_id, now)
            outcomes.append(outcome)
            if error:
                errors.append(error)
        # Created anywhere is created.  Otherwise one node whose answer is
        # unknown makes the firing's unknown: another attempt could hand
        # that node a second workstream.
        if _Outcome.CREATED in outcomes:
            outcome = _Outcome.CREATED
        elif _Outcome.UNKNOWN in outcomes:
            outcome = _Outcome.UNKNOWN
        else:
            outcome = _Outcome.NOT_CREATED
        return outcome, "; ".join(errors)

    def _write_run(
        self,
        *,
        task_id: str,
        node_id: str,
        ws_id: str,
        correlation_id: str,
        started: str,
        status: str,
        error: str,
    ) -> None:
        """Append a run-history row.  A row that fails to write is logged,
        never raised: the schedule's advance must not wait on history, or a
        dispatched workstream would be re-created on every tick."""
        try:
            self._storage.record_task_run(
                run_id=uuid.uuid4().hex,
                task_id=task_id,
                node_id=node_id,
                ws_id=ws_id,
                correlation_id=correlation_id,
                started=started,
                status=status,
                error=error,
            )
        except Exception:
            log.warning(
                "scheduler.record_run_failed", task_id=task_id, status=status, exc_info=True
            )

    def _record_failure(self, task: dict[str, Any], now: str, error: str) -> None:
        """Record a failed dispatch attempt."""
        self._write_run(
            task_id=task["task_id"],
            node_id="",
            ws_id="",
            correlation_id="",
            started=now,
            status="failed",
            error=error,
        )
        log.warning("scheduler.dispatch_failed", task_id=task["task_id"], error=error)

    def _record_disabled(self, task: dict[str, Any], now: str, reason: str) -> None:
        """Record in the run history that a schedule was disabled at
        dispatch, and why.  Its own status: the schedule stopping is neither
        a dispatch nor one more failed attempt."""
        log.warning("scheduler.schedule_disabled", task_id=task["task_id"], reason=reason)
        self._write_run(
            task_id=task["task_id"],
            node_id="",
            ws_id="",
            correlation_id="",
            started=now,
            status="disabled",
            error=reason,
        )

    @staticmethod
    def _compute_next_run(task: dict[str, Any]) -> str:
        """The next run time; empty when the cron walk finds no next firing."""
        return compute_next_run(
            task["schedule_type"],
            task.get("cron_expr", ""),
            task.get("at_time", ""),
            task.get("timezone") or "UTC",
        )
