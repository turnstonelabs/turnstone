"""Tests for turnstone.core.rendezvous (HRW routing primitive)."""

from __future__ import annotations

import pytest

from turnstone.core.rendezvous import NoAvailableNodeError, NodeRef, fnv1a_32, select, select_all


class TestFnv1aVectors:
    """Pin the FNV-1a-32 implementation against the documented test
    vectors so cross-language readers (Go, TS) stay in sync."""

    def test_empty_input(self) -> None:
        assert fnv1a_32(b"") == 0x811C9DC5  # basis

    def test_foobar(self) -> None:
        assert fnv1a_32(b"foobar") == 0xBF9CF968

    def test_single_byte(self) -> None:
        # Hand-computed: (basis ^ 0x61) * prime, masked to 32 bits.
        expected = ((0x811C9DC5 ^ 0x61) * 0x01000193) & 0xFFFFFFFF
        assert fnv1a_32(b"a") == expected


class TestSelect:
    def test_empty_node_list_raises(self) -> None:
        with pytest.raises(NoAvailableNodeError):
            select("any-key", [])

    def test_single_node_always_wins(self) -> None:
        only = NodeRef("solo", "http://solo")
        for key in ("a", "b", "00ff" + "0" * 28):
            assert select(key, [only]) is only

    def test_deterministic(self) -> None:
        nodes = [NodeRef(f"n{i}", f"http://n{i}") for i in range(5)]
        key = "deadbeef" * 4
        first = select(key, nodes)
        for _ in range(20):
            assert select(key, nodes) is first

    def test_independent_of_node_list_order(self) -> None:
        nodes = [NodeRef(f"n{i}", f"http://n{i}") for i in range(5)]
        key = "feedface" * 4
        forward = select(key, nodes)
        backward = select(key, list(reversed(nodes)))
        assert forward.node_id == backward.node_id

    def test_distribution_roughly_uniform(self) -> None:
        nodes = [NodeRef(f"n{i}", f"http://n{i}") for i in range(4)]
        counts = {n.node_id: 0 for n in nodes}
        # Use sequential keys — 32 hex chars is what the router actually
        # passes in.  Sequential isn't a problem because FNV-1a smears.
        for i in range(4000):
            key = f"{i:08x}" + "0" * 24
            counts[select(key, nodes).node_id] += 1
        # Each node should win ~25% (1000); allow ±15% drift.
        for c in counts.values():
            assert 850 < c < 1150, counts


class TestMinimalMoves:
    def test_join_only_moves_to_new_node(self) -> None:
        old = [NodeRef(f"n{i}", f"http://n{i}") for i in range(3)]
        new = [*old, NodeRef("n3", "http://n3")]
        moved_correctly = 0
        moved_incorrectly = 0
        for i in range(2000):
            key = f"{i:08x}" + "0" * 24
            before = select(key, old).node_id
            after = select(key, new).node_id
            if before == after:
                continue
            if after == "n3":
                moved_correctly += 1
            else:
                moved_incorrectly += 1
        # Strict invariant: a join must never move a key between two
        # surviving nodes.
        assert moved_incorrectly == 0
        # Sanity: some keys did move.
        assert moved_correctly > 0

    def test_leave_does_not_disturb_surviving_nodes(self) -> None:
        old = [NodeRef(f"n{i}", f"http://n{i}") for i in range(4)]
        new = old[:-1]  # n3 leaves
        for i in range(2000):
            key = f"{i:08x}" + "0" * 24
            before = select(key, old).node_id
            after = select(key, new).node_id
            if before == "n3":
                # Must rehome to a survivor.
                assert after in {"n0", "n1", "n2"}
            else:
                # Must not move.
                assert after == before


class TestWeights:
    def test_higher_weight_wins_more_often(self) -> None:
        nodes = [
            NodeRef("light", "http://l", weight=1),
            NodeRef("heavy", "http://h", weight=4),
        ]
        on_heavy = 0
        for i in range(5000):
            key = f"{i:08x}" + "0" * 24
            if select(key, nodes).node_id == "heavy":
                on_heavy += 1
        # Heavy gets clearly more than half; tolerance for the simple
        # hash×weight formulation is wide.
        assert on_heavy / 5000 > 0.65

    def test_zero_weight_clamped_to_one(self) -> None:
        # A weight-0 node still participates as if weight 1 — defended
        # at both NodeRef construction and _score().  Use the public
        # surface to sanity check.
        nodes = [
            NodeRef("a", "http://a", weight=0),
            NodeRef("b", "http://b", weight=0),
        ]
        # Just confirms it doesn't divide-by-zero or score to 0.
        winner = select("any-key", nodes)
        assert winner.node_id in {"a", "b"}


class TestSelectAll:
    def test_returns_all_nodes_in_score_order(self) -> None:
        nodes = [NodeRef(f"n{i}", f"http://n{i}") for i in range(4)]
        ranked = select_all("some-key", nodes)
        assert len(ranked) == 4
        assert {n.node_id for n in ranked} == {"n0", "n1", "n2", "n3"}
        # Top of the ranked list matches the single-select winner.
        assert ranked[0] is select("some-key", nodes)

    def test_empty_list_returns_empty(self) -> None:
        assert select_all("any-key", []) == []
