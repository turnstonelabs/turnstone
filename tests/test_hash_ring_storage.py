"""Tests for the hash ring routing storage methods."""

from __future__ import annotations


class TestHashRingBuckets:
    def test_list_empty(self, storage):
        assert storage.list_ring_buckets() == []

    def test_seed_and_list(self, storage):
        storage.seed_ring_buckets([(0, "node-a"), (1, "node-b"), (2, "node-a")])
        rows = storage.list_ring_buckets()
        assert len(rows) == 3
        assert rows[0] == {"bucket": 0, "node_id": "node-a"}
        assert rows[1] == {"bucket": 1, "node_id": "node-b"}
        assert rows[2] == {"bucket": 2, "node_id": "node-a"}

    def test_seed_idempotent(self, storage):
        storage.seed_ring_buckets([(0, "node-a"), (1, "node-b")])
        # Re-seed with conflicting assignment: should keep original
        storage.seed_ring_buckets([(0, "node-x"), (2, "node-c")])
        rows = storage.list_ring_buckets()
        by_bucket = {r["bucket"]: r["node_id"] for r in rows}
        assert by_bucket[0] == "node-a"  # original preserved
        assert by_bucket[1] == "node-b"
        assert by_bucket[2] == "node-c"  # new bucket added

    def test_assign_buckets(self, storage):
        storage.seed_ring_buckets([(0, "node-a"), (1, "node-a"), (2, "node-b")])
        storage.assign_buckets([0, 1], "node-c")
        rows = storage.list_ring_buckets()
        by_bucket = {r["bucket"]: r["node_id"] for r in rows}
        assert by_bucket[0] == "node-c"
        assert by_bucket[1] == "node-c"
        assert by_bucket[2] == "node-b"

    def test_assign_returns_count(self, storage):
        storage.seed_ring_buckets([(0, "node-a"), (1, "node-a")])
        count = storage.assign_buckets([0, 1], "node-b")
        assert count == 2
        # Empty list returns 0
        assert storage.assign_buckets([], "node-x") == 0


class TestBucketStats:
    def test_increment_creates_row(self, storage):
        storage.increment_bucket_count(42)
        stats = storage.list_bucket_stats()
        assert len(stats) == 1
        assert stats[0]["bucket"] == 42
        assert stats[0]["ws_count"] == 1
        assert stats[0]["active_count"] == 0

    def test_increment_active(self, storage):
        storage.increment_bucket_count(10, active=True)
        stats = storage.list_bucket_stats()
        assert stats[0]["ws_count"] == 1
        assert stats[0]["active_count"] == 1
        # Increment again without active
        storage.increment_bucket_count(10)
        stats = storage.list_bucket_stats()
        assert stats[0]["ws_count"] == 2
        assert stats[0]["active_count"] == 1

    def test_decrement(self, storage):
        storage.increment_bucket_count(5, active=True)
        storage.increment_bucket_count(5, active=True)
        storage.decrement_bucket_count(5, active=True)
        stats = storage.list_bucket_stats()
        assert stats[0]["ws_count"] == 1
        assert stats[0]["active_count"] == 1

    def test_decrement_clamps_at_zero(self, storage):
        storage.increment_bucket_count(7)
        storage.decrement_bucket_count(7)
        storage.decrement_bucket_count(7)  # already at 0
        stats = storage.list_bucket_stats()
        # ws_count is 0, so should not appear (filter ws_count > 0)
        assert len(stats) == 0

    def test_adjust_active_only(self, storage):
        storage.increment_bucket_count(20, active=True)
        storage.increment_bucket_count(20, active=True)
        # Decrease active without changing ws_count
        storage.adjust_bucket_active(20, -1)
        stats = storage.list_bucket_stats()
        assert stats[0]["ws_count"] == 2
        assert stats[0]["active_count"] == 1
        # Clamp at zero
        storage.adjust_bucket_active(20, -5)
        stats = storage.list_bucket_stats()
        assert stats[0]["active_count"] == 0

    def test_list_sparse(self, storage):
        storage.increment_bucket_count(100)
        storage.increment_bucket_count(200)
        storage.increment_bucket_count(300)
        # Decrement 200 to zero
        storage.decrement_bucket_count(200)
        stats = storage.list_bucket_stats()
        buckets = [s["bucket"] for s in stats]
        assert 100 in buckets
        assert 200 not in buckets
        assert 300 in buckets


class TestWorkstreamOverrides:
    def test_set_and_list(self, storage):
        storage.set_workstream_override("ws-001", "node-a", reason="affinity")
        overrides = storage.list_workstream_overrides()
        assert len(overrides) == 1
        assert overrides[0]["ws_id"] == "ws-001"
        assert overrides[0]["node_id"] == "node-a"
        assert overrides[0]["reason"] == "affinity"

    def test_upsert(self, storage):
        storage.set_workstream_override("ws-002", "node-a")
        storage.set_workstream_override("ws-002", "node-b", reason="migration")
        overrides = storage.list_workstream_overrides()
        assert len(overrides) == 1
        assert overrides[0]["node_id"] == "node-b"
        assert overrides[0]["reason"] == "migration"

    def test_delete(self, storage):
        storage.set_workstream_override("ws-003", "node-a")
        result = storage.delete_workstream_override("ws-003")
        assert result is True
        assert storage.list_workstream_overrides() == []

    def test_delete_nonexistent(self, storage):
        result = storage.delete_workstream_override("ws-nope")
        assert result is False

    def test_list_empty(self, storage):
        assert storage.list_workstream_overrides() == []
