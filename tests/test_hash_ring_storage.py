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

    def test_assign_large_list_exceeds_chunk_size(self, storage):
        """Regression: lists larger than chunk_size must not hit param limits."""
        n = 1200  # exceeds SQLite chunk_size (500) and exercises multi-chunk path
        storage.seed_ring_buckets([(i, "node-a") for i in range(n)])
        count = storage.assign_buckets(list(range(n)), "node-b")
        assert count == n
        rows = storage.list_ring_buckets()
        assert all(r["node_id"] == "node-b" for r in rows)

    def test_assign_deduplicates_input(self, storage):
        """Duplicates in the input list should not inflate rowcount."""
        storage.seed_ring_buckets([(0, "node-a"), (1, "node-a")])
        count = storage.assign_buckets([0, 1, 0, 1, 0], "node-b")
        assert count == 2


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

    def test_set_bucket_stat_creates(self, storage):
        """set_bucket_stat upserts a new row."""
        storage.set_bucket_stat(42, 5, 2)
        stats = storage.list_bucket_stats()
        row = next(s for s in stats if s["bucket"] == 42)
        assert row["ws_count"] == 5
        assert row["active_count"] == 2

    def test_set_bucket_stat_overwrites(self, storage):
        """set_bucket_stat overwrites existing values."""
        storage.set_bucket_stat(42, 10, 3)
        storage.set_bucket_stat(42, 2, 0)
        stats = storage.list_bucket_stats()
        row = next(s for s in stats if s["bucket"] == 42)
        assert row["ws_count"] == 2
        assert row["active_count"] == 0

    def test_set_bucket_stat_zero_removes_from_sparse(self, storage):
        """Setting ws_count=0 means list_bucket_stats excludes it (sparse)."""
        storage.set_bucket_stat(42, 5, 1)
        storage.set_bucket_stat(42, 0, 0)
        stats = storage.list_bucket_stats()
        assert not any(s["bucket"] == 42 for s in stats)


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
