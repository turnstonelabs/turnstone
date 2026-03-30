"""Hash ring rebalancer — maintains bucket-to-node assignments.

Runs as a daemon thread inside the console process, following the same
lifecycle pattern as ClusterCollector and TaskScheduler.
"""

from __future__ import annotations

import contextlib
import json
import threading
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

from turnstone.core.hash_ring import RING_SIZE, RingNode, bucket_of

if TYPE_CHECKING:
    from turnstone.console.collector import ClusterCollector
    from turnstone.console.metrics import ConsoleMetrics
    from turnstone.console.router import ConsoleRouter
    from turnstone.core.storage._protocol import StorageBackend

log = structlog.get_logger(__name__)

# States considered "active" for bucket stat reconciliation
_ACTIVE_STATES = frozenset({"running", "thinking", "attention"})


@dataclass
class RebalanceResult:
    """Summary of a single rebalance pass."""

    moves: int = 0
    migrations: int = 0
    trigger: str = "periodic"
    duration_ms: float = 0.0
    nodes: int = 0
    seeded: bool = False
    noop: bool = True


class Rebalancer:
    """Background daemon thread that maintains hash ring bucket assignments.

    Uses the same lifecycle pattern as TaskScheduler: daemon thread, DB-based
    leader lock, periodic wake or event-driven trigger.
    """

    def __init__(
        self,
        storage: StorageBackend,
        router: ConsoleRouter | None = None,
        collector: ClusterCollector | None = None,
        console_metrics: ConsoleMetrics | None = None,
        interval: int = 60,
        threshold: float = 0.10,
        vnodes_per_unit: int = 150,
        lock_ttl: int = 120,
        eager_migrate: bool = False,
        api_token: str = "",
    ) -> None:
        self._storage = storage
        self._router = router
        self._collector = collector
        self._console_metrics = console_metrics
        self._interval = interval
        self._threshold = threshold
        self._vnodes_per_unit = vnodes_per_unit
        self._lock_ttl = lock_ttl
        self._eager_migrate = eager_migrate
        self._api_token = api_token
        self._stop_event = threading.Event()
        self._trigger_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock_owner = uuid.uuid4().hex
        self._last_result: RebalanceResult | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the rebalancer daemon thread."""
        self._stop_event.clear()
        self._trigger_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="rebalancer")
        self._thread.start()
        log.info("rebalancer.started", interval=self._interval)

    def stop(self) -> None:
        """Stop the rebalancer and wait for the thread to finish."""
        self._stop_event.set()
        self._trigger_event.set()  # wake the thread so it exits promptly
        if self._thread is not None:
            self._thread.join(timeout=5)
        log.info("rebalancer.stopped")

    def trigger(self) -> None:
        """Wake the rebalancer for an immediate check."""
        self._trigger_event.set()

    def get_status(self) -> dict[str, Any]:
        """Return current rebalancer status for the admin API."""
        raw = self._storage.get_system_setting("rebalancer_version", node_id="")
        version = 0
        if raw is not None:
            with contextlib.suppress(json.JSONDecodeError, TypeError, ValueError):
                version = int(json.loads(raw.get("value", "0")))
        result: dict[str, Any] = {
            "version": version,
            "is_leader": False,
            "last_result": None,
        }
        if self._last_result is not None:
            lr = self._last_result
            result["last_result"] = {
                "moves": lr.moves,
                "trigger": lr.trigger,
                "duration_ms": lr.duration_ms,
                "nodes": lr.nodes,
                "seeded": lr.seeded,
                "noop": lr.noop,
            }
        return result

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def _loop(self) -> None:
        """Main rebalancer loop — sleep or wait for trigger, then rebalance."""
        while not self._stop_event.is_set():
            self._trigger_event.wait(timeout=self._interval)
            if self._stop_event.is_set():
                break
            trigger = "triggered" if self._trigger_event.is_set() else "periodic"
            self._trigger_event.clear()
            if not self._try_acquire_lock():
                continue
            try:
                result = self.rebalance_once(trigger=trigger)
                self._last_result = result
                self._record_result_metrics(result)
            except Exception:
                log.exception("rebalancer.error")
            finally:
                self._release_lock()

    def _record_result_metrics(self, result: RebalanceResult) -> None:
        """Push rebalance result counters to the console metrics collector."""
        if self._console_metrics is None:
            return
        if result.seeded:
            self._console_metrics.record_rebalance("seeded")
        elif not result.noop:
            self._console_metrics.record_rebalance("rebalanced")
        else:
            self._console_metrics.record_rebalance("noop")
        if result.migrations > 0:
            self._console_metrics.record_migrations(result.migrations)

    # ------------------------------------------------------------------
    # Leader lock (same pattern as TaskScheduler)
    # ------------------------------------------------------------------

    def _try_acquire_lock(self) -> bool:
        """Try to acquire the rebalancer lock via system_settings.

        Uses a row with key ``rebalancer_lock``.  The value is a JSON
        object ``{"owner": "<id>", "acquired": "<iso>"}``.  Another
        instance's lock is considered expired when its timestamp is
        older than ``_lock_ttl`` seconds.

        To reduce the TOCTOU window of a read-then-write approach, this
        method writes unconditionally and reads back to verify ownership.
        If two rebalancers race, one write wins and the loser sees the
        winner's value on read-back.
        """
        now = datetime.now(UTC)
        now_str = now.strftime("%Y-%m-%dT%H:%M:%S")

        existing = self._storage.get_system_setting("rebalancer_lock")
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

        lock_value = json.dumps({"owner": self._lock_owner, "acquired": now_str})
        self._storage.upsert_system_setting("rebalancer_lock", lock_value)
        stored = self._storage.get_system_setting("rebalancer_lock")
        if stored is not None:
            try:
                data = json.loads(stored.get("value", "{}"))
            except (json.JSONDecodeError, TypeError):
                return False
            return bool(data.get("owner") == self._lock_owner)
        return False

    def _release_lock(self) -> None:
        """Release the rebalancer lock if we still own it."""
        existing = self._storage.get_system_setting("rebalancer_lock")
        if existing is not None:
            try:
                lock_data = json.loads(existing.get("value", "{}"))
            except (json.JSONDecodeError, TypeError):
                lock_data = {}
            if lock_data.get("owner") == self._lock_owner:
                self._storage.delete_system_setting("rebalancer_lock")

    # ------------------------------------------------------------------
    # Rebalance algorithm
    # ------------------------------------------------------------------

    def rebalance_once(self, trigger: str = "periodic") -> RebalanceResult:
        """Execute a single rebalance pass.

        Returns a RebalanceResult describing what happened.
        """
        t0 = time.monotonic()
        result = RebalanceResult(trigger=trigger)

        # 1. Read live server nodes
        nodes_raw = self._storage.list_services("server", max_age_seconds=120)
        if not nodes_raw:
            result.duration_ms = (time.monotonic() - t0) * 1000
            return result

        ring_nodes = _build_ring_nodes(nodes_raw)
        result.nodes = len(ring_nodes)

        # 2. Read current bucket assignments
        current_rows = self._storage.list_ring_buckets()

        # 3. If table is empty — first run, seed all 65536 buckets
        if not current_rows:
            assignments = _weight_based_assignments(ring_nodes)
            self._storage.seed_ring_buckets(assignments)
            self._bump_version()
            if self._router is not None:
                self._router.refresh_cache()
            result.seeded = True
            result.noop = False
            result.duration_ms = (time.monotonic() - t0) * 1000
            log.info(
                "rebalancer.seeded",
                nodes=len(ring_nodes),
                buckets=len(assignments),
            )
            return result

        # 4. Build current assignment map and per-node bucket lists
        current_map: dict[int, str] = {r["bucket"]: r["node_id"] for r in current_rows}
        live_ids = {n.node_id for n in ring_nodes}

        # Single node with all buckets assigned — noop
        if len(live_ids) == 1 and all(nid in live_ids for nid in current_map.values()):
            result.duration_ms = (time.monotonic() - t0) * 1000
            return result

        # 5. Reconcile bucket_stats before computing transfer costs
        self._reconcile_bucket_stats()
        stats_rows = self._storage.list_bucket_stats()
        stats_map: dict[int, tuple[int, int]] = {}
        for s in stats_rows:
            stats_map[s["bucket"]] = (s["ws_count"], s["active_count"])

        # 6. Group buckets by current owner
        buckets_by_node: dict[str, list[int]] = defaultdict(list)
        for bucket, nid in current_map.items():
            buckets_by_node[nid].append(bucket)

        # 7. Compute ideal bucket count per node from weights
        total_weight = sum(n.weight for n in ring_nodes)
        ideal_counts: dict[str, int] = {}
        remainder_pool: list[str] = []
        assigned_ideal = 0
        for n in ring_nodes:
            ideal_n = int((n.weight / total_weight) * RING_SIZE)
            ideal_counts[n.node_id] = ideal_n
            assigned_ideal += ideal_n
            remainder_pool.append(n.node_id)
        # Distribute remainder buckets (rounding error) to heaviest nodes
        leftover = RING_SIZE - assigned_ideal
        remainder_pool.sort(key=lambda nid: ideal_counts[nid], reverse=True)
        for i in range(leftover):
            ideal_counts[remainder_pool[i % len(remainder_pool)]] += 1

        # 8. Always reassign dead-node buckets first (unconditional)
        dead_node_ids = {nid for nid in buckets_by_node if nid not in live_ids}
        filtered_moves: list[tuple[int, str, str]] = []  # (bucket, from, to)

        if dead_node_ids:
            # Dead nodes are implicit donors — all their buckets must move.
            # Distribute to the most underloaded live nodes.
            dead_buckets: list[int] = []
            for nid in dead_node_ids:
                dead_buckets.extend(buckets_by_node.pop(nid))
            # Sort by cost (cheapest first)
            dead_buckets.sort(key=lambda b: stats_map.get(b, (0, 0)))
            # Assign to live nodes that are most below their ideal
            for bucket in dead_buckets:
                # Pick the node with the largest deficit
                best = min(
                    live_ids,
                    key=lambda nid: len(buckets_by_node.get(nid, [])) - ideal_counts.get(nid, 0),
                )
                filtered_moves.append((bucket, "", best))
                buckets_by_node[best].append(bucket)

        # 9. Identify donors and recipients among live nodes
        actual_counts = {nid: len(bkts) for nid, bkts in buckets_by_node.items()}
        donors: list[str] = []
        recipients: list[str] = []
        for nid in live_ids:
            actual = actual_counts.get(nid, 0)
            ideal = ideal_counts.get(nid, 0)
            if ideal > 0 and actual > ideal * (1 + self._threshold):
                donors.append(nid)
            elif ideal > 0 and actual < ideal * (1 - self._threshold):
                recipients.append(nid)

        # 10. Transfer from donors to recipients — minimal moves only
        if donors and recipients:
            # Sort donors by excess descending, recipients by deficit descending
            donors.sort(key=lambda nid: actual_counts[nid] - ideal_counts[nid], reverse=True)
            recipients.sort(
                key=lambda nid: ideal_counts[nid] - actual_counts.get(nid, 0),
                reverse=True,
            )

            for donor_id in donors:
                donor_excess = len(buckets_by_node[donor_id]) - ideal_counts[donor_id]
                if donor_excess <= 0:
                    continue
                # Sort this donor's buckets by cost (cheapest to move first)
                donor_buckets = sorted(
                    buckets_by_node[donor_id],
                    key=lambda b: stats_map.get(b, (0, 0)),
                )
                moved_from_donor = 0
                for recipient_id in recipients:
                    recipient_deficit = ideal_counts[recipient_id] - len(
                        buckets_by_node.get(recipient_id, [])
                    )
                    if recipient_deficit <= 0:
                        continue
                    # Transfer min(donor_excess - moved, recipient_deficit) buckets
                    to_move = min(donor_excess - moved_from_donor, recipient_deficit)
                    for _ in range(to_move):
                        if not donor_buckets:
                            break
                        bucket = donor_buckets.pop(0)
                        filtered_moves.append((bucket, donor_id, recipient_id))
                        buckets_by_node[donor_id].remove(bucket)
                        buckets_by_node.setdefault(recipient_id, []).append(bucket)
                        moved_from_donor += 1
                    if moved_from_donor >= donor_excess:
                        break

        if not filtered_moves:
            result.duration_ms = (time.monotonic() - t0) * 1000
            return result

        # 11. Execute moves: group by target node
        by_target: dict[str, list[int]] = defaultdict(list)
        for bucket, _from, to in filtered_moves:
            by_target[to].append(bucket)

        total_moved = 0
        for target_node_id, bucket_list in by_target.items():
            total_moved += self._storage.assign_buckets(bucket_list, target_node_id)

        # 12. Bump version and refresh cache
        self._bump_version()
        if self._router is not None:
            self._router.refresh_cache()

        # 13. Eager migration: evict workstreams on moved buckets from source nodes
        migrations = 0
        if self._eager_migrate and filtered_moves:
            migrations = self._eager_migrate_workstreams(
                filtered_moves,
                nodes_raw,
            )

        result.moves = total_moved
        result.migrations = migrations
        result.noop = False
        result.duration_ms = (time.monotonic() - t0) * 1000

        log.info(
            "rebalancer.rebalanced",
            moves=total_moved,
            migrations=migrations,
            nodes=len(ring_nodes),
            trigger=trigger,
            duration_ms=round(result.duration_ms, 1),
        )
        return result

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _bump_version(self) -> None:
        """Increment the rebalancer_version counter in system_settings.

        The read-then-write is safe because this method is only called while
        the leader lock is held (``_try_acquire_lock`` succeeded).  Concurrent
        writers are prevented by the lock, so no CAS or timestamp trick is
        needed.
        """
        raw = self._storage.get_system_setting("rebalancer_version", node_id="")
        version = 0
        if raw is not None:
            with contextlib.suppress(json.JSONDecodeError, TypeError, ValueError):
                version = int(json.loads(raw.get("value", "0")))
        self._storage.upsert_system_setting(
            "rebalancer_version", json.dumps(version + 1), node_id=""
        )

    def _reconcile_bucket_stats(self) -> None:
        """Reconcile bucket_stats against actual workstream table data.

        Self-heals counter drift from server crashes (a crashed server
        can't decrement its counters).
        """
        ws_data = self._storage.list_workstream_routing_data()

        # Compute actual per-bucket counts
        actual: dict[int, tuple[int, int]] = {}  # bucket -> (ws_count, active_count)
        for ws_id, state in ws_data:
            if len(ws_id) < 4:
                continue
            bucket = bucket_of(ws_id)
            ws_count, active_count = actual.get(bucket, (0, 0))
            ws_count += 1
            if state in _ACTIVE_STATES:
                active_count += 1
            actual[bucket] = (ws_count, active_count)

        # Load current stats
        stats_rows = self._storage.list_bucket_stats()
        stored: dict[int, tuple[int, int]] = {}
        for s in stats_rows:
            stored[s["bucket"]] = (s["ws_count"], s["active_count"])

        # All buckets that appear in either set
        all_buckets = set(actual.keys()) | set(stored.keys())

        for bucket in all_buckets:
            act = actual.get(bucket, (0, 0))
            sto = stored.get(bucket, (0, 0))
            if act != sto:
                # Pass current stored values to avoid re-querying the DB
                self._reset_bucket_stat(
                    bucket,
                    act[0],
                    act[1],
                    current_ws=sto[0],
                    current_active=sto[1],
                )

    def _reset_bucket_stat(
        self,
        bucket: int,
        ws_count: int,
        active_count: int,
        current_ws: int = 0,
        current_active: int = 0,
    ) -> None:
        """Reset a bucket_stats row to exact values via single upsert."""
        if (ws_count, active_count) == (current_ws, current_active):
            return  # no change
        self._storage.set_bucket_stat(bucket, ws_count, active_count)

    def _eager_migrate_workstreams(
        self,
        moves: list[tuple[int, str, str]],
        nodes_raw: list[dict[str, str]],
    ) -> int:
        """POST /_internal/migrate to source nodes for workstreams on moved buckets.

        Only migrates idle workstreams — active ones would be disrupted.
        Returns the number of successful migrations.
        """
        import httpx

        # Build node URL map from the services data already loaded
        node_urls: dict[str, str] = {s["service_id"]: s["url"] for s in nodes_raw}

        # Moved buckets grouped by source node
        moved_buckets: dict[str, set[int]] = defaultdict(set)
        for bucket, from_node, _to_node in moves:
            moved_buckets[from_node].add(bucket)

        # Find workstreams on moved buckets (idle only — don't disrupt active work)
        ws_data = self._storage.list_workstream_routing_data()
        to_migrate: list[tuple[str, str]] = []  # (ws_id, source_node_url)
        for ws_id, state in ws_data:
            if len(ws_id) < 4 or state in _ACTIVE_STATES:
                continue
            bucket = bucket_of(ws_id)
            for node_id, buckets in moved_buckets.items():
                if bucket in buckets:
                    url = node_urls.get(node_id)
                    if url:
                        to_migrate.append((ws_id, url))
                    break

        if not to_migrate:
            return 0

        headers: dict[str, str] = {}
        if self._api_token:
            headers["Authorization"] = f"Bearer {self._api_token}"

        migrated = 0
        with httpx.Client(timeout=10, headers=headers) as client:
            for ws_id, source_url in to_migrate:
                try:
                    resp = client.post(
                        f"{source_url}/v1/api/_internal/migrate",
                        json={"ws_id": ws_id},
                    )
                    if resp.status_code == 200:
                        migrated += 1
                    elif resp.status_code == 409:
                        log.debug(
                            "rebalancer.migrate.refused",
                            ws_id=ws_id[:8],
                            reason="last_workstream",
                        )
                    # 404 = already gone, that's fine
                except httpx.HTTPError:
                    log.warning(
                        "rebalancer.migrate.failed",
                        ws_id=ws_id[:8],
                        source=source_url,
                        exc_info=True,
                    )

        if migrated:
            log.info("rebalancer.migrations", count=migrated, total=len(to_migrate))
        return migrated


def _weight_based_assignments(nodes: list[RingNode]) -> list[tuple[int, str]]:
    """Compute bucket assignments proportional to node weights.

    Distributes all 65536 buckets across nodes proportionally to their
    weights, with deterministic rounding. Used for seeding — produces
    an exact weight-proportional split that the donor/recipient
    algorithm won't try to "correct" on the next run.
    """
    total_weight = sum(n.weight for n in nodes)
    assignments: list[tuple[int, str]] = []
    # Sort nodes for determinism
    sorted_nodes = sorted(nodes, key=lambda n: n.node_id)
    bucket = 0
    for i, node in enumerate(sorted_nodes):
        if i == len(sorted_nodes) - 1:
            # Last node gets the remainder (avoids rounding gaps)
            count = RING_SIZE - bucket
        else:
            count = round((node.weight / total_weight) * RING_SIZE)
        for _ in range(count):
            if bucket >= RING_SIZE:
                break
            assignments.append((bucket, node.node_id))
            bucket += 1
    return assignments


def _build_ring_nodes(services: list[dict[str, str]]) -> list[RingNode]:
    """Convert service registry rows into RingNode instances."""
    nodes: list[RingNode] = []
    for svc in services:
        meta_str = svc.get("metadata", "{}")
        try:
            meta = json.loads(meta_str)
        except (json.JSONDecodeError, TypeError):
            meta = {}
        weight = int(meta.get("weight", 1))
        if weight < 1:
            weight = 1
        nodes.append(RingNode(node_id=svc["service_id"], url=svc["url"], weight=weight))
    return nodes
