# Consistent Hash Ring — Reference Design

**Status**: Reference (not currently in the hot path)
**Date**: 2026-03-30

## Overview

This document describes a consistent hash ring algorithm evaluated during
the design of the direct HTTP transport routing system. The current
implementation uses weight-proportional bucket assignment with a
donor/recipient rebalancing algorithm (see `direct-http-transport.md`).
The consistent hash ring is documented here as a reference for future
scalability work — if the cluster grows beyond the point where the
weight-proportional approach is sufficient, the ring provides a
proven alternative with stronger stability guarantees.

## When to consider the ring approach

The current weight-proportional seeding + donor/recipient rebalancer works
well when:
- Cluster size is moderate (< 50 nodes)
- Nodes join/leave infrequently
- The rebalancer runs centrally (in the console)

The consistent hash ring becomes advantageous when:
- Cluster size grows large (50+ nodes) and frequent membership changes
  cause the donor/recipient algorithm to churn
- Decentralized routing is needed (each node computes the ring locally,
  no central console required)
- Cross-language determinism is important (multiple implementations must
  agree on the same assignment without sharing state)

## Algorithm

### Hash function: FNV-1a (32-bit)

```python
def fnv1a_32(data: bytes) -> int:
    """FNV-1a 32-bit hash.

    Basis: 0x811C9DC5, Prime: 0x01000193.
    XOR each byte, then multiply by prime (masked to 32 bits).
    """
    h = 0x811C9DC5
    for b in data:
        h ^= b
        h = (h * 0x01000193) & 0xFFFFFFFF
    return h
```

Known test vectors:
- `fnv1a_32(b"")` = `0x811C9DC5` (basis value)
- `fnv1a_32(b"foobar")` = `0xBF9CF968`

Cross-language implementations:
- **Python**: loop above (no dependencies)
- **Go**: same algorithm with `uint32` arithmetic
- **TypeScript**: same algorithm with `>>> 0` for unsigned 32-bit

### Virtual nodes

Each physical node with weight `w` gets `w * 150` virtual positions on a
16-bit ring (65536 positions). Virtual node `i` of physical node `N` is
placed at:

```
position = fnv1a_32(f"{N.node_id}:{i}".encode()) % 65536
```

With 150 vnodes per unit weight:
- 2 equal-weight nodes: ~50/50 split (measured: 38-62% range due to
  hash variance, stddev ~3% with large vnode counts)
- 3 nodes at weights 2:1:1: ~50/25/25 (within 10% tolerance)

### Lookup

```python
def owner(bucket: int) -> str:
    """O(log n) bisect-right walk to find the next virtual node clockwise."""
    idx = bisect_right(positions, bucket)
    if idx >= len(positions):
        idx = 0  # wrap around
    return vnode_map[positions[idx]]
```

### Stability properties

The consistent hash ring guarantees:
- **Node addition**: adding a node moves at most `1/N` of buckets (where N
  is the new node count). Other nodes' buckets are unaffected.
- **Node removal**: only the removed node's buckets are reassigned. Buckets
  owned by surviving nodes don't move.
- **Determinism**: same membership list always produces the same ring.
  No coordination needed between processes.

### Full assignment precomputation

```python
def assignments() -> list[tuple[int, str]]:
    """Compute all 65536 bucket-to-node mappings."""
    return [(b, owner(b)) for b in range(65536)]
```

This produces a complete assignment table that can be loaded into a flat
array for O(1) request-time lookup. The ring itself is never consulted
on the hot path.

## Data structures

```python
@dataclass(frozen=True, slots=True)
class RingNode:
    node_id: str
    url: str
    weight: int = 1

class HashRing:
    """Immutable consistent hash ring. Thread-safe (no mutable state)."""

    def __init__(self, nodes: Sequence[RingNode], vnodes_per_unit: int = 150):
        # Validate no duplicate node_ids
        # Build sorted array of (position, node_id) tuples
        # positions[i] = fnv1a_32(f"{node_id}:{i}".encode()) % RING_SIZE

    def owner(self, bucket: int) -> RingNode | None:
        # bisect_right + wrap

    @property
    def version(self) -> int:
        # Deterministic hash of membership: fnv1a_32 of sorted node_id:weight pairs

    def assignments(self) -> list[tuple[int, str]]:
        # Precompute all 65536 bucket assignments
```

## Comparison with current approach

| Aspect | Weight-proportional (current) | Consistent hash ring |
|--------|------------------------------|---------------------|
| Seeding | Exact weight split, deterministic | Hash-based, ~3% variance |
| Node addition | Donor/recipient moves only excess | Ring moves ~1/N buckets |
| Node removal | Dead buckets → most underloaded | Ring redistributes to clockwise neighbors |
| Cross-node churn | Zero (only donor→recipient) | Zero (ring stability guarantee) |
| Decentralized | No (needs central rebalancer) | Yes (each node computes locally) |
| Complexity | Simple weight arithmetic | Virtual node construction + bisect |

## Test vectors

For cross-language implementation validation:

```json
{
  "fnv1a_32": [
    {"input": "", "output": 2166136261},
    {"input": "foobar", "output": 3215766888}
  ],
  "bucket_of": [
    {"ws_id": "a3f100000000000000000000000000000", "bucket": 41969},
    {"ws_id": "00000000000000000000000000000000", "bucket": 0},
    {"ws_id": "ffff0000000000000000000000000000", "bucket": 65535}
  ],
  "ring_single_node": {
    "nodes": [{"node_id": "n1", "weight": 1}],
    "vnodes_per_unit": 150,
    "expected_n1_buckets": 65536
  }
}
```
