"""Hash ring routing primitives.

Constants, helpers, and data types for the bucket-based routing system.
The consistent hash ring algorithm itself is documented in
``docs/design/consistent-hash-ring.md`` as a reference design for future
scalability work.  The current rebalancer uses weight-proportional
distribution instead (see ``turnstone/console/rebalancer.py``).
"""

from __future__ import annotations

from dataclasses import dataclass

RING_SIZE = 65536  # 16-bit bucket space


def bucket_of(ws_id: str) -> int:
    """Extract the bucket from a workstream UUID.

    ws_ids are hex strings (``secrets.token_hex``).  The first 4 hex
    characters give us 16 bits = 65536 buckets.  Since UUIDs are random,
    this is already uniformly distributed — no hash function needed.
    """
    return int(ws_id[:4], 16)


@dataclass(frozen=True, slots=True)
class RingNode:
    """A physical node participating in the cluster."""

    node_id: str
    url: str
    weight: int = 1


class NoAvailableNodeError(Exception):
    """Raised when routing fails (no nodes registered, bucket not assigned)."""
