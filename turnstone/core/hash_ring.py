"""Consistent hash ring for workstream-to-node routing.

The ring is used internally by the rebalancer to compute ideal bucket-to-node
assignments.  At request time, routing is a flat array lookup — the ring is
never consulted on the hot path.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

RING_SIZE = 65536  # 16-bit bucket space
DEFAULT_VNODES = 150  # virtual nodes per unit weight


def bucket_of(ws_id: str) -> int:
    """Extract the bucket from a workstream UUID.

    ws_ids are hex strings (``secrets.token_hex``).  The first 4 hex
    characters give us 16 bits = 65536 buckets.  Since UUIDs are random,
    this is already uniformly distributed — no hash function needed.
    """
    return int(ws_id[:4], 16)


def fnv1a_32(data: bytes) -> int:
    """FNV-1a 32-bit hash."""
    h = 0x811C9DC5
    for b in data:
        h ^= b
        h = (h * 0x01000193) & 0xFFFFFFFF
    return h


@dataclass(frozen=True, slots=True)
class RingNode:
    """A physical node on the ring."""

    node_id: str
    url: str
    weight: int = 1


class NoAvailableNodeError(Exception):
    """Raised when routing fails (no nodes in ring, bucket not assigned)."""


class HashRing:
    """Consistent hash ring with virtual nodes.

    Immutable after construction — create a new ring on membership change.
    Thread-safe (no mutable state).
    """

    def __init__(
        self,
        nodes: Sequence[RingNode],
        vnodes_per_unit: int = DEFAULT_VNODES,
    ) -> None:
        self._nodes = tuple(nodes)
        self._node_map: dict[str, RingNode] = {n.node_id: n for n in nodes}

        # Build sorted array of (position, node_id) tuples.
        ring: list[tuple[int, str]] = []
        for node in nodes:
            count = node.weight * vnodes_per_unit
            for i in range(count):
                pos = fnv1a_32(f"{node.node_id}:{i}".encode()) % RING_SIZE
                ring.append((pos, node.node_id))
        ring.sort()

        self._positions = [p for p, _ in ring]
        self._ring = ring

    def owner(self, bucket: int) -> RingNode | None:
        """Return the node that owns the given bucket.  O(log n) lookup."""
        if not self._ring:
            return None
        idx = bisect_right(self._positions, bucket)
        if idx >= len(self._positions):
            idx = 0
        return self._node_map[self._ring[idx][1]]

    @property
    def nodes(self) -> tuple[RingNode, ...]:
        """All physical nodes, sorted by node_id."""
        return tuple(sorted(self._nodes, key=lambda n: n.node_id))

    @property
    def version(self) -> int:
        """Hash of the membership list.  Changes when nodes join/leave."""
        return hash(tuple(sorted((n.node_id, n.weight) for n in self._nodes)))

    def assignments(self) -> list[tuple[int, str]]:
        """Precompute all 65536 bucket-to-node assignments.

        Returns a list of ``(bucket, node_id)`` tuples.  Used by the
        rebalancer to seed/update the assignment table.
        """
        if not self._ring:
            return []
        return [(b, self.owner(b).node_id) for b in range(RING_SIZE)]  # type: ignore[union-attr]
