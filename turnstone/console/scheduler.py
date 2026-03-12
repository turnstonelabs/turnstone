"""Background task scheduler for timed workstream dispatch.

Runs as a daemon thread inside the console process. Checks for due tasks
every ``check_interval`` seconds and dispatches them as
``CreateWorkstreamMessage`` via the MQ broker.

Uses Redis ``SET NX EX`` for distributed locking in multi-console deployments.
"""

from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from turnstone.console.collector import ClusterCollector
    from turnstone.core.storage._protocol import StorageBackend
    from turnstone.mq.broker import RedisBroker

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
        broker: RedisBroker,
        collector: ClusterCollector,
        storage: StorageBackend,
        prefix: str = "turnstone",
        check_interval: float = 15.0,
        lock_ttl: int = 60,
        max_fan_out: int = 20,
    ) -> None:
        self._broker = broker
        self._collector = collector
        self._storage = storage
        self._prefix = prefix
        self._check_interval = check_interval
        self._lock_ttl = lock_ttl
        self._max_fan_out = max_fan_out
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._tick_count = 0
        self._prune_every = 240  # ~1 hour at 15s intervals

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
        log.info("scheduler.stopped")

    def _loop(self) -> None:
        """Main scheduler loop — tick then sleep."""
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception:
                log.exception("scheduler.tick_error")
            self._stop_event.wait(self._check_interval)

    # Lua script for safe lock release — only delete if we still own the lock
    _UNLOCK_SCRIPT = "if redis.call('get',KEYS[1])==ARGV[1] then return redis.call('del',KEYS[1]) else return 0 end"

    def _tick(self) -> None:
        """Single scheduler iteration: acquire lock, query due tasks, dispatch."""
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")

        # Distributed lock with unique owner — prevents releasing another instance's lock
        lock_key = f"{self._prefix}:scheduler:lock"
        lock_value = uuid.uuid4().hex
        acquired = self._broker._redis.set(lock_key, lock_value, nx=True, ex=self._lock_ttl)
        if not acquired:
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
        finally:
            # Only release our own lock (safe even if TTL expired and another took it)
            self._broker._redis.eval(  # type: ignore[no-untyped-call]
                self._UNLOCK_SCRIPT, 1, lock_key, lock_value
            )

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

    def _dispatch_to_node(self, task: dict[str, Any], node_id: str, now: str) -> None:
        """Send a CreateWorkstreamMessage to a specific node."""
        from turnstone.mq.protocol import CreateWorkstreamMessage

        msg = CreateWorkstreamMessage(
            name=task["name"],
            model=task.get("model", ""),
            target_node=node_id,
            initial_message=task["initial_message"],
            auto_approve=bool(task.get("auto_approve", 0)),
            auto_approve_tools=self._parse_tools(task),
            user_id=task.get("created_by", ""),
            template=task.get("template", ""),
        )
        self._broker.push_inbound(msg.to_json(), node_id=node_id)

        self._storage.record_task_run(
            run_id=uuid.uuid4().hex,
            task_id=task["task_id"],
            node_id=node_id,
            ws_id="",
            correlation_id=msg.correlation_id,
            started=now,
            status="dispatched",
            error="",
        )

    def _dispatch_to_pool(self, task: dict[str, Any], now: str) -> None:
        """Send a CreateWorkstreamMessage to the shared pool queue."""
        from turnstone.mq.protocol import CreateWorkstreamMessage

        msg = CreateWorkstreamMessage(
            name=task["name"],
            model=task.get("model", ""),
            initial_message=task["initial_message"],
            auto_approve=bool(task.get("auto_approve", 0)),
            auto_approve_tools=self._parse_tools(task),
            user_id=task.get("created_by", ""),
            template=task.get("template", ""),
        )
        self._broker.push_inbound(msg.to_json())

        self._storage.record_task_run(
            run_id=uuid.uuid4().hex,
            task_id=task["task_id"],
            node_id="pool",
            ws_id="",
            correlation_id=msg.correlation_id,
            started=now,
            status="dispatched",
            error="",
        )

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
