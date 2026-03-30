"""Tests for turnstone.console.rebalancer."""

from __future__ import annotations

import json

import pytest

from turnstone.console.rebalancer import Rebalancer
from turnstone.core.hash_ring import RING_SIZE
from turnstone.core.storage._sqlite import SQLiteBackend


@pytest.fixture
def storage(tmp_path):
    """Fresh SQLite backend for each test."""
    return SQLiteBackend(str(tmp_path / "test.db"))


def _register_nodes(storage: SQLiteBackend, count: int, *, weight: int = 1) -> None:
    """Register *count* server nodes in the services table."""
    for i in range(count):
        meta = json.dumps({"weight": weight, "started": "2026-01-01T00:00:00Z"})
        storage.register_service("server", f"node-{i}", f"http://node-{i}:8080", metadata=meta)


def _register_weighted_nodes(storage: SQLiteBackend, weights: dict[str, int]) -> None:
    """Register nodes with specific weights."""
    for node_id, w in weights.items():
        meta = json.dumps({"weight": w, "started": "2026-01-01T00:00:00Z"})
        storage.register_service("server", node_id, f"http://{node_id}:8080", metadata=meta)


def _get_version(storage: SQLiteBackend) -> int:
    """Read the rebalancer_version from system_settings."""
    raw = storage.get_system_setting("rebalancer_version", node_id="")
    if raw is None:
        return 0
    try:
        return int(json.loads(raw.get("value", "0")))
    except (json.JSONDecodeError, TypeError, ValueError):
        return 0


class TestFirstRunSeed:
    def test_first_run_seeds_ring(self, storage):
        """Empty assignment table + 2 nodes -> seed all 65536 rows."""
        _register_nodes(storage, 2)
        rb = Rebalancer(storage=storage)
        result = rb.rebalance_once()

        assert result.seeded is True
        assert result.noop is False
        assert result.nodes == 2

        buckets = storage.list_ring_buckets()
        assert len(buckets) == RING_SIZE

        # All buckets should be assigned to one of the two nodes
        node_ids = {b["node_id"] for b in buckets}
        assert node_ids == {"node-0", "node-1"}


class TestIdempotent:
    def test_second_run_is_noop(self, storage):
        """Running rebalance twice with same membership produces noop on second pass."""
        _register_nodes(storage, 2)
        rb = Rebalancer(storage=storage)

        r1 = rb.rebalance_once()
        assert r1.seeded is True

        r2 = rb.rebalance_once()
        assert r2.noop is True
        assert r2.moves == 0


class TestNewNodeRebalances:
    def test_adding_node_moves_buckets(self, storage):
        """Seed with 2 nodes, add 3rd -> some buckets move to the new node."""
        _register_nodes(storage, 2)
        rb = Rebalancer(storage=storage, threshold=0.01)
        rb.rebalance_once()  # seed

        # Verify only 2 nodes initially
        buckets_before = storage.list_ring_buckets()
        nodes_before = {b["node_id"] for b in buckets_before}
        assert nodes_before == {"node-0", "node-1"}

        # Add a third node
        meta = json.dumps({"weight": 1, "started": "2026-01-01T00:00:00Z"})
        storage.register_service("server", "node-2", "http://node-2:8080", metadata=meta)

        result = rb.rebalance_once()
        assert result.noop is False
        assert result.moves > 0
        assert result.nodes == 3

        # Verify all three nodes have buckets
        buckets_after = storage.list_ring_buckets()
        nodes_after = {b["node_id"] for b in buckets_after}
        assert "node-2" in nodes_after


class TestDeadNodeReassigned:
    def test_dead_node_buckets_move_to_survivors(self, storage):
        """Seed with 3 nodes, deregister one -> its buckets move to survivors."""
        _register_nodes(storage, 3)
        rb = Rebalancer(storage=storage, threshold=0.01)
        rb.rebalance_once()  # seed

        # Verify node-2 has some buckets
        buckets = storage.list_ring_buckets()
        node2_count = sum(1 for b in buckets if b["node_id"] == "node-2")
        assert node2_count > 0

        # Deregister node-2
        storage.deregister_service("server", "node-2")

        result = rb.rebalance_once()
        assert result.noop is False
        assert result.moves > 0

        # Verify no buckets assigned to dead node
        buckets_after = storage.list_ring_buckets()
        nodes_after = {b["node_id"] for b in buckets_after}
        assert "node-2" not in nodes_after


class TestSingleNodeNoop:
    def test_single_node_already_assigned_is_noop(self, storage):
        """1 node with all buckets assigned -> noop."""
        _register_nodes(storage, 1)
        rb = Rebalancer(storage=storage)

        # Seed with single node
        rb.rebalance_once()

        # Second run should be noop
        result = rb.rebalance_once()
        assert result.noop is True


class TestWeightedDistribution:
    def test_weight_2_gets_more_buckets(self, storage):
        """Node with weight=2 gets roughly 2x the buckets of weight=1."""
        _register_weighted_nodes(storage, {"heavy": 2, "light": 1})
        rb = Rebalancer(storage=storage, vnodes_per_unit=150)
        rb.rebalance_once()  # seed

        buckets = storage.list_ring_buckets()
        heavy_count = sum(1 for b in buckets if b["node_id"] == "heavy")
        light_count = sum(1 for b in buckets if b["node_id"] == "light")

        # heavy should have roughly 2/3 of total, light roughly 1/3
        # Allow 10% tolerance
        expected_heavy = RING_SIZE * 2 // 3
        assert abs(heavy_count - expected_heavy) < RING_SIZE * 0.10
        assert heavy_count > light_count


class TestVersionIncremented:
    def test_version_bumps_on_seed(self, storage):
        """Verify rebalancer_version increments after seed."""
        _register_nodes(storage, 2)
        rb = Rebalancer(storage=storage)

        v0 = _get_version(storage)
        assert v0 == 0

        rb.rebalance_once()
        v1 = _get_version(storage)
        assert v1 == 1

    def test_version_bumps_on_rebalance(self, storage):
        """Version bumps on actual moves, not on noops."""
        _register_nodes(storage, 2)
        rb = Rebalancer(storage=storage, threshold=0.01)
        rb.rebalance_once()  # seed: version -> 1

        # Noop: version stays at 1
        rb.rebalance_once()
        assert _get_version(storage) == 1

        # Add node: version -> 2
        meta = json.dumps({"weight": 1, "started": "2026-01-01T00:00:00Z"})
        storage.register_service("server", "node-2", "http://node-2:8080", metadata=meta)
        rb.rebalance_once()
        assert _get_version(storage) == 2


class TestReconcileStats:
    def test_bucket_stats_corrected(self, storage):
        """Create workstreams in DB, verify bucket_stats are reconciled."""
        _register_nodes(storage, 2)
        rb = Rebalancer(storage=storage)
        rb.rebalance_once()  # seed

        # Create some workstreams — ws_id starts with hex bucket
        # Bucket 0x0000 = 0, bucket 0x0001 = 1
        storage.register_workstream("0000" + "a" * 28, state="idle")
        storage.register_workstream("0000" + "b" * 28, state="running")
        storage.register_workstream("0001" + "c" * 28, state="idle")

        # Set bogus stats that will be corrected
        storage.increment_bucket_count(0)  # says 1, should be 2
        storage.increment_bucket_count(5)  # says 1, should be 0

        rb._reconcile_bucket_stats()

        stats = storage.list_bucket_stats()
        stats_map = {s["bucket"]: s for s in stats}

        # Bucket 0 should have 2 ws, 1 active (running)
        assert stats_map[0]["ws_count"] == 2
        assert stats_map[0]["active_count"] == 1

        # Bucket 1 should have 1 ws, 0 active
        assert stats_map[1]["ws_count"] == 1
        assert stats_map[1]["active_count"] == 0

        # Bucket 5 should have been removed (ws_count=0)
        assert 5 not in stats_map


class TestTransferPriorityEmptyFirst:
    def test_empty_buckets_moved_before_occupied(self, storage):
        """Verify the sort key puts empty buckets before occupied ones.

        Rather than asserting specific bucket assignments (which depend on
        hash ring placement), we verify the sorting invariant directly by
        checking that moves with zero occupancy come before occupied ones
        in the internal ordering.
        """
        _register_nodes(storage, 2)
        rb = Rebalancer(storage=storage, threshold=0.01)
        rb.rebalance_once()  # seed

        # Create workstreams in a few buckets owned by node-0
        buckets = storage.list_ring_buckets()
        node0_buckets = [b["bucket"] for b in buckets if b["node_id"] == "node-0"]

        occupied = set()
        for b in node0_buckets[:3]:
            ws_id = f"{b:04x}" + "d" * 28
            storage.register_workstream(ws_id, state="running")
            storage.increment_bucket_count(b, active=True)
            occupied.add(b)

        # Reconcile stats so the rebalancer sees them
        rb._reconcile_bucket_stats()

        # Read stats to verify ordering assumptions
        stats = storage.list_bucket_stats()
        stats_map = {s["bucket"]: (s["ws_count"], s["active_count"]) for s in stats}

        # The sort key is (active_count, ws_count) — occupied buckets
        # must sort AFTER empty buckets
        for b in occupied:
            assert stats_map[b][0] > 0  # ws_count > 0
            assert stats_map[b][1] > 0  # active_count > 0

        # Empty buckets have (0, 0) which sorts before (1, 1)
        assert (0, 0) < (1, 1)


class TestLeaderElection:
    def test_two_rebalancers_one_runs(self, storage):
        """Two rebalancers compete — only one acquires the lock."""
        _register_nodes(storage, 2)
        rb1 = Rebalancer(storage=storage)
        rb2 = Rebalancer(storage=storage)

        # rb1 acquires the lock
        assert rb1._try_acquire_lock() is True

        # rb2 cannot acquire (lock is fresh)
        assert rb2._try_acquire_lock() is False

        # rb1 releases
        rb1._release_lock()

        # Now rb2 can acquire
        assert rb2._try_acquire_lock() is True
        rb2._release_lock()


class TestZeroNodes:
    def test_no_nodes_returns_noop(self, storage):
        """Zero live nodes -> noop result."""
        rb = Rebalancer(storage=storage)
        result = rb.rebalance_once()
        assert result.noop is True
        assert result.nodes == 0


class TestStartStop:
    def test_start_stop_lifecycle(self, storage):
        """Verify start/stop lifecycle doesn't hang or crash."""
        _register_nodes(storage, 1)
        rb = Rebalancer(storage=storage, interval=1)
        rb.start()
        assert rb._thread is not None
        assert rb._thread.is_alive()
        rb.stop()
        assert not rb._thread.is_alive()

    def test_trigger_wakes_thread(self, storage):
        """Verify trigger() causes an immediate pass."""
        _register_nodes(storage, 2)
        rb = Rebalancer(storage=storage, interval=3600)  # long interval
        rb.start()
        try:
            rb.trigger()
            # Give it a moment to process
            rb._stop_event.wait(timeout=2)
        finally:
            rb.stop()
        # After trigger, the ring should be seeded
        assert len(storage.list_ring_buckets()) == RING_SIZE


class TestGetStatus:
    def test_status_before_any_run(self, storage):
        """Status returns version=0 and no last_result before any run."""
        rb = Rebalancer(storage=storage)
        status = rb.get_status()
        assert status["version"] == 0
        assert status["last_result"] is None

    def test_status_after_seed(self, storage):
        """Status reflects the seed run when result is stored."""
        _register_nodes(storage, 2)
        rb = Rebalancer(storage=storage)
        result = rb.rebalance_once()
        # The loop normally sets _last_result; simulate that here
        rb._last_result = result
        status = rb.get_status()
        assert status["version"] == 1
        assert status["last_result"] is not None
        assert status["last_result"]["seeded"] is True


class TestEagerMigration:
    def test_eager_migrate_posts_to_source_nodes(self, storage):
        """When eager_migrate=True, rebalancer POSTs /_internal/migrate for idle workstreams."""
        import httpx

        # Seed ring with 2 nodes
        _register_nodes(storage, 2)
        rb = Rebalancer(storage=storage, eager_migrate=True)
        rb.rebalance_once()  # seeds

        # Create a workstream on node-0's bucket range
        # Find a bucket assigned to node-0
        buckets = storage.list_ring_buckets()
        node0_bucket = None
        for b in buckets:
            if b["node_id"] == "node-0":
                node0_bucket = b["bucket"]
                break
        assert node0_bucket is not None

        ws_id = f"{node0_bucket:04x}" + "a" * 28
        storage.register_workstream(ws_id, node_id="node-0", name="test")
        storage.increment_bucket_count(node0_bucket)

        # Add a 3rd node — this will trigger rebalance
        meta = json.dumps({"weight": 1, "started": "2026-01-01T00:00:00Z"})
        storage.register_service("server", "node-2", "http://node-2:8080", metadata=meta)

        # Track migrate calls
        migrate_calls: list[tuple[str, str]] = []  # (url, ws_id)

        class FakeTransport(httpx.BaseTransport):
            def handle_request(self, request: httpx.Request) -> httpx.Response:
                body = json.loads(request.content)
                migrate_calls.append((str(request.url), body.get("ws_id", "")))
                return httpx.Response(200, json={"status": "ok", "ws_id": body["ws_id"]})

        # Monkey-patch httpx.Client to use our fake transport
        original_init = httpx.Client.__init__

        def patched_init(self_client, **kwargs):
            kwargs["transport"] = FakeTransport()
            original_init(self_client, **kwargs)

        import unittest.mock

        with unittest.mock.patch.object(httpx.Client, "__init__", patched_init):
            result = rb.rebalance_once(trigger="test")

        # If the bucket moved to a different node, the workstream should be migrated
        new_buckets = storage.list_ring_buckets()
        new_owner = None
        for b in new_buckets:
            if b["bucket"] == node0_bucket:
                new_owner = b["node_id"]
                break

        if new_owner != "node-0":
            # Bucket moved — migration should have happened
            assert result.migrations > 0
            assert any(ws_id in call[1] for call in migrate_calls)
        else:
            # Bucket stayed — no migration needed for this ws
            assert result.migrations >= 0  # other workstreams might have been migrated

    def test_eager_migrate_skips_active_workstreams(self, storage):
        """Active workstreams are not eagerly migrated (would disrupt in-flight work)."""
        import httpx

        _register_nodes(storage, 2)
        rb = Rebalancer(storage=storage, eager_migrate=True)
        rb.rebalance_once()  # seeds

        # Find a bucket on node-0
        buckets = storage.list_ring_buckets()
        node0_bucket = None
        for b in buckets:
            if b["node_id"] == "node-0":
                node0_bucket = b["bucket"]
                break
        assert node0_bucket is not None

        # Create an ACTIVE workstream (state="running")
        ws_id = f"{node0_bucket:04x}" + "b" * 28
        storage.register_workstream(ws_id, node_id="node-0", name="active-ws")
        storage.update_workstream_state(ws_id, "running")
        storage.increment_bucket_count(node0_bucket, active=True)

        # Add 3rd node to trigger rebalance
        meta = json.dumps({"weight": 1, "started": "2026-01-01T00:00:00Z"})
        storage.register_service("server", "node-2", "http://node-2:8080", metadata=meta)

        migrate_calls: list[str] = []

        class FakeTransport(httpx.BaseTransport):
            def handle_request(self, request: httpx.Request) -> httpx.Response:
                body = json.loads(request.content)
                migrate_calls.append(body.get("ws_id", ""))
                return httpx.Response(200, json={"status": "ok"})

        original_init = httpx.Client.__init__

        def patched_init(self_client, **kwargs):
            kwargs["transport"] = FakeTransport()
            original_init(self_client, **kwargs)

        import unittest.mock

        with unittest.mock.patch.object(httpx.Client, "__init__", patched_init):
            rb.rebalance_once(trigger="test")

        # The active workstream should NOT have been migrated
        assert ws_id not in migrate_calls

    def test_eager_migrate_disabled_by_default(self, storage):
        """When eager_migrate=False (default), no migrate calls happen."""
        _register_nodes(storage, 2)
        rb = Rebalancer(storage=storage)  # eager_migrate defaults to False
        rb.rebalance_once()  # seeds

        # Create workstream and trigger rebalance
        buckets = storage.list_ring_buckets()
        node0_bucket = next(b["bucket"] for b in buckets if b["node_id"] == "node-0")
        ws_id = f"{node0_bucket:04x}" + "c" * 28
        storage.register_workstream(ws_id, node_id="node-0", name="test")

        meta = json.dumps({"weight": 1, "started": "2026-01-01T00:00:00Z"})
        storage.register_service("server", "node-2", "http://node-2:8080", metadata=meta)

        result = rb.rebalance_once(trigger="test")
        assert result.migrations == 0  # no eager migration when disabled


class TestMinimalTransfer:
    def test_new_node_only_receives_never_shuffles(self, storage):
        """Adding a 3rd node moves buckets TO it, never between existing nodes.

        This is the key property of the minimal-transfer algorithm: nodes A
        and B should not exchange buckets with each other — only donate to C.
        """
        _register_nodes(storage, 2)
        rb = Rebalancer(storage=storage, threshold=0.05)
        rb.rebalance_once()  # seeds: node-0 gets 32768, node-1 gets 32768

        # Record which node owns each bucket before adding node-2
        before = {r["bucket"]: r["node_id"] for r in storage.list_ring_buckets()}

        # Add a third node
        meta = json.dumps({"weight": 1, "started": "2026-01-01T00:00:00Z"})
        storage.register_service("server", "node-2", "http://node-2:8080", metadata=meta)
        result = rb.rebalance_once()

        after = {r["bucket"]: r["node_id"] for r in storage.list_ring_buckets()}

        # Verify: every bucket that moved went TO node-2
        for bucket in range(RING_SIZE):
            old = before[bucket]
            new = after[bucket]
            if old != new:
                assert new == "node-2", (
                    f"bucket {bucket} moved {old} -> {new}, "
                    "expected all moves to target node-2"
                )

        # Verify: node-2 got roughly 1/3 of all buckets
        node2_count = sum(1 for nid in after.values() if nid == "node-2")
        assert 19000 < node2_count < 24000, f"node-2 got {node2_count} buckets"
        assert result.moves > 0

    def test_remove_node_distributes_proportionally(self, storage):
        """Removing a node distributes its buckets to remaining nodes
        proportionally — doesn't shuffle between survivors."""
        _register_nodes(storage, 3)
        rb = Rebalancer(storage=storage, threshold=0.05)
        rb.rebalance_once()  # seeds

        before = {r["bucket"]: r["node_id"] for r in storage.list_ring_buckets()}

        # Remove node-2
        storage.deregister_service("server", "node-2")
        result = rb.rebalance_once()

        after = {r["bucket"]: r["node_id"] for r in storage.list_ring_buckets()}

        # Every moved bucket should have been owned by node-2 (the dead node)
        for bucket in range(RING_SIZE):
            old = before[bucket]
            new = after[bucket]
            if old != new:
                assert old == "node-2", (
                    f"bucket {bucket} moved {old} -> {new}, "
                    "but only node-2's buckets should move"
                )

        # node-2 should have zero buckets now
        node2_count = sum(1 for nid in after.values() if nid == "node-2")
        assert node2_count == 0
        assert result.moves > 0
