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
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

from turnstone.sdk.server import TurnstoneServer

if TYPE_CHECKING:
    from turnstone.console.collector import ClusterCollector
    from turnstone.core.auth import ServiceTokenManager
    from turnstone.core.storage._protocol import StorageBackend

log = structlog.get_logger(__name__)


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
        api_token: str = "",
        token_manager: ServiceTokenManager | None = None,
    ) -> None:
        self._collector = collector
        self._storage = storage
        self._check_interval = check_interval
        self._lock_ttl = lock_ttl
        self._max_fan_out = max_fan_out
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
        now_str = now.strftime("%Y-%m-%dT%H:%M:%S")

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
                    acquired_dt = datetime.strptime(acquired_str, "%Y-%m-%dT%H:%M:%S").replace(
                        tzinfo=UTC
                    )
                    if (now - acquired_dt).total_seconds() < self._lock_ttl:
                        return False  # Another instance holds a valid lock
                except ValueError:
                    pass  # Malformed timestamp — take the lock

        # Write our lock and read back to verify we won any concurrent race.
        lock_value = json.dumps({"owner": self._lock_owner, "acquired": now_str})
        self._storage.upsert_system_setting("scheduler_lock", lock_value)
        stored = self._storage.get_system_setting("scheduler_lock")
        if stored is not None:
            try:
                data = json.loads(stored.get("value", "{}"))
            except (json.JSONDecodeError, TypeError):
                return False
            return bool(data.get("owner") == self._lock_owner)
        return False

    def _release_lock(self) -> None:
        """Release the scheduler lock if we still own it."""
        existing = self._storage.get_system_setting("scheduler_lock")
        if existing is not None:
            try:
                lock_data = json.loads(existing.get("value", "{}"))
            except (json.JSONDecodeError, TypeError):
                lock_data = {}
            if lock_data.get("owner") == self._lock_owner:
                self._storage.delete_system_setting("scheduler_lock")

    def _tick(self) -> None:
        """Single scheduler iteration: acquire lock, query due tasks, dispatch."""
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")

        if not self._try_acquire_lock():
            return

        try:
            due_tasks = self._storage.list_due_tasks(now)
            for task in due_tasks:
                self._dispatch_task(task, now)

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
        """Dispatch a single task as one or more CreateWorkstreamMessages."""
        target_mode = task["target_mode"]
        task_id = task["task_id"]
        dispatched = False

        if target_mode == "all":
            nodes, _ = self._collector.get_nodes(sort_by="activity", limit=1000, offset=0)
            fan_count = 0
            for n in nodes:
                if n.get("reachable", False):
                    if fan_count >= self._max_fan_out:
                        log.warning(
                            "scheduler.fan_out_capped",
                            task_id=task_id,
                            max_fan_out=self._max_fan_out,
                        )
                        break
                    self._dispatch_to_node(task, n["node_id"], now)
                    fan_count += 1
                    dispatched = True
            if not dispatched:
                self._record_failure(task, now, "No reachable nodes for fan-out")
        elif target_mode == "pool":
            self._dispatch_to_pool(task, now)
            dispatched = True
        elif target_mode == "auto":
            node_id = _pick_best_node(self._collector)
            if node_id:
                self._dispatch_to_node(task, node_id, now)
                dispatched = True
            else:
                self._record_failure(task, now, "No reachable nodes")
        else:
            # Specific node_id
            self._dispatch_to_node(task, target_mode, now)
            dispatched = True

        if not dispatched:
            return  # Don't advance schedule on failure

        # Update last_run and compute next_run
        next_run = self._compute_next_run(task)
        if task["schedule_type"] == "at":
            self._storage.update_scheduled_task(task_id, last_run=now, next_run="", enabled=False)
        else:
            self._storage.update_scheduled_task(task_id, last_run=now, next_run=next_run)

        log_kw: dict[str, Any] = {
            "task_id": task_id,
            "target_mode": target_mode,
            "schedule_type": task["schedule_type"],
            "created_by": task.get("created_by", ""),
        }
        if task.get("auto_approve", 0):
            log_kw["auto_approve"] = True
            log_kw["auto_approve_tools"] = task.get("auto_approve_tools", "")
            log.warning("scheduler.task_dispatched_auto_approve", **log_kw)
        else:
            log.info("scheduler.task_dispatched", **log_kw)

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

    def _dispatch_to_node(self, task: dict[str, Any], node_id: str, now: str) -> None:
        """Dispatch a workstream to a specific node via the SDK client."""
        server_url = self._get_node_url(node_id)
        if not server_url:
            self._record_failure(task, now, f"No URL for node {node_id}")
            return

        correlation_id = uuid.uuid4().hex
        try:
            client = self._get_sdk_client(server_url)
            resp = client.create_workstream(
                name=task["name"],
                model=task.get("model", ""),
                initial_message=task["initial_message"],
                auto_approve=bool(task.get("auto_approve", 0)),
                auto_approve_tools=",".join(self._parse_tools(task)),
                user_id=task.get("created_by", ""),
                skill=task.get("skill", ""),
            )
            ws_id = resp.ws_id
        except Exception:
            self._record_failure(task, now, f"SDK dispatch to {node_id} failed")
            log.warning("scheduler.sdk_dispatch_failed", node_id=node_id, exc_info=True)
            return

        self._storage.record_task_run(
            run_id=uuid.uuid4().hex,
            task_id=task["task_id"],
            node_id=node_id,
            ws_id=ws_id,
            correlation_id=correlation_id,
            started=now,
            status="dispatched",
            error="",
        )

    def _dispatch_to_pool(self, task: dict[str, Any], now: str) -> None:
        """Dispatch to any available server node (pool mode)."""
        node_id = _pick_best_node(self._collector)
        if not node_id:
            self._record_failure(task, now, "No reachable nodes for pool dispatch")
            return
        self._dispatch_to_node(task, node_id, now)

    def _record_failure(self, task: dict[str, Any], now: str, error: str) -> None:
        """Record a failed dispatch attempt."""
        self._storage.record_task_run(
            run_id=uuid.uuid4().hex,
            task_id=task["task_id"],
            node_id="",
            ws_id="",
            correlation_id="",
            started=now,
            status="failed",
            error=error,
        )
        log.warning("scheduler.dispatch_failed", task_id=task["task_id"], error=error)

    @staticmethod
    def _compute_next_run(task: dict[str, Any]) -> str:
        """Compute the next run time. Returns empty string for one-shot tasks."""
        from turnstone.console.server import _compute_next_run

        return _compute_next_run(
            task["schedule_type"], task.get("cron_expr", ""), task.get("at_time", "")
        )
