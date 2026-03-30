"""Tests for turnstone.core.hash_ring."""

from collections import Counter

from turnstone.core.hash_ring import (
    RING_SIZE,
    HashRing,
    RingNode,
    bucket_of,
    fnv1a_32,
)


class TestBucketOf:
    def test_known_vectors(self):
        assert bucket_of("a3f1" + "0" * 28) == 0xA3F1
        assert bucket_of("0000" + "a" * 28) == 0
        assert bucket_of("ffff" + "b" * 28) == 65535

    def test_hex_prefix(self):
        # Only the first 4 hex chars matter — the rest is ignored.
        assert bucket_of("abcd0000") == bucket_of("abcdffff")
        assert bucket_of("abcd0000") == 0xABCD


class TestFnv1a:
    def test_empty(self):
        assert fnv1a_32(b"") == 0x811C9DC5

    def test_known_value(self):
        assert fnv1a_32(b"foobar") == 0xBF9CF968

    def test_deterministic(self):
        assert fnv1a_32(b"hello") == fnv1a_32(b"hello")


class TestHashRing:
    def test_single_node(self):
        node = RingNode(node_id="n1", url="http://n1:8000")
        ring = HashRing([node])
        counts = Counter(ring.owner(b).node_id for b in range(RING_SIZE))  # type: ignore[union-attr]
        assert counts["n1"] == RING_SIZE

    def test_two_equal_nodes(self):
        nodes = [
            RingNode(node_id="n1", url="http://n1:8000"),
            RingNode(node_id="n2", url="http://n2:8000"),
        ]
        ring = HashRing(nodes)
        counts = Counter(ring.owner(b).node_id for b in range(RING_SIZE))  # type: ignore[union-attr]
        assert 25000 <= counts["n1"] <= 40000
        assert 25000 <= counts["n2"] <= 40000

    def test_three_weighted_nodes(self):
        nodes = [
            RingNode(node_id="a", url="http://a:8000", weight=2),
            RingNode(node_id="b", url="http://b:8000", weight=1),
            RingNode(node_id="c", url="http://c:8000", weight=1),
        ]
        ring = HashRing(nodes)
        counts = Counter(ring.owner(b).node_id for b in range(RING_SIZE))  # type: ignore[union-attr]
        total = RING_SIZE
        # weight 2:1:1 → expect ~50:25:25 with +-10% tolerance
        assert 0.40 * total <= counts["a"] <= 0.60 * total
        assert 0.15 * total <= counts["b"] <= 0.35 * total
        assert 0.15 * total <= counts["c"] <= 0.35 * total

    def test_deterministic(self):
        nodes = [
            RingNode(node_id="n1", url="http://n1:8000"),
            RingNode(node_id="n2", url="http://n2:8000"),
        ]
        a = HashRing(nodes).assignments()
        b = HashRing(nodes).assignments()
        assert a == b

    def test_node_addition_stability(self):
        two = [
            RingNode(node_id="n1", url="http://n1:8000"),
            RingNode(node_id="n2", url="http://n2:8000"),
        ]
        three = [
            *two,
            RingNode(node_id="n3", url="http://n3:8000"),
        ]
        old = HashRing(two).assignments()
        new = HashRing(three).assignments()
        moved = sum(1 for (_, o), (_, n) in zip(old, new, strict=True) if o != n)
        # Ideal is ~33% moved; allow up to 40%.
        assert moved < 0.40 * RING_SIZE

    def test_node_removal_stability(self):
        nodes = [
            RingNode(node_id="n1", url="http://n1:8000"),
            RingNode(node_id="n2", url="http://n2:8000"),
            RingNode(node_id="n3", url="http://n3:8000"),
        ]
        before = HashRing(nodes).assignments()
        after = HashRing(nodes[:2]).assignments()
        # Only buckets previously owned by n3 should move.
        for (b, old_owner), (_, new_owner) in zip(before, after, strict=True):
            if old_owner != "n3":
                assert old_owner == new_owner, f"bucket {b} changed from {old_owner} to {new_owner}"

    def test_empty_ring(self):
        ring = HashRing([])
        assert ring.owner(0) is None
        assert ring.owner(42) is None
        assert ring.assignments() == []

    def test_assignments_complete(self):
        nodes = [
            RingNode(node_id="n1", url="http://n1:8000"),
            RingNode(node_id="n2", url="http://n2:8000"),
        ]
        a = HashRing(nodes).assignments()
        assert len(a) == RING_SIZE
        assert all(node_id is not None for _, node_id in a)

    def test_version_changes_on_membership(self):
        r1 = HashRing([RingNode(node_id="n1", url="http://n1:8000")])
        r2 = HashRing(
            [
                RingNode(node_id="n1", url="http://n1:8000"),
                RingNode(node_id="n2", url="http://n2:8000"),
            ]
        )
        assert r1.version != r2.version

    def test_version_same_for_same_membership(self):
        nodes = [
            RingNode(node_id="n1", url="http://n1:8000"),
            RingNode(node_id="n2", url="http://n2:8000"),
        ]
        assert HashRing(nodes).version == HashRing(nodes).version
