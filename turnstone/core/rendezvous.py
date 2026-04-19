"""Rendezvous (Highest Random Weight, HRW) routing.

Pure function over ``(ws_id, live_nodes)`` → ``NodeRef``.  Liveness is
sourced from the ``services`` table; routing requires no extra
persistent state.

Properties (per the standard HRW result):

- **Determinism** — every reader given the same membership list produces
  the same answer, no coordination required.
- **Minimal moves** — when a node joins, only the keys for which it now
  scores highest move to it (~``1/N`` for equal weights).  When a node
  leaves, only the keys it was winning fail over, distributed across
  the remaining nodes proportionally to their weight.  Other keys do
  not move.
- **Cross-language compatible** — the FNV-1a hash spec matches
  ``docs/design/consistent-hash-ring.md`` so Go or TypeScript clients
  can compute the same routes.

Cost: O(N) per lookup where N is the live-node count, dominated by
N FNV-1a hash computes.  Negligible compared with any downstream HTTP
round-trip.

Weighting: hash value is multiplied by the node weight rather than
using the Skeena ``-weight / ln(uniform)`` formulation.  The simpler
form gives indistinguishable distribution at typical weight ranges
(1-3) and avoids floating-point comparisons that would be a
cross-language portability hazard.  Switch to Skeena if a node ever
ships with weight ≥ 10 alongside weight-1 peers.
"""

from __future__ import annotations

from dataclasses import dataclass

_FNV_BASIS = 0x811C9DC5
_FNV_PRIME = 0x01000193
_MASK_32 = 0xFFFFFFFF


def fnv1a_32(data: bytes) -> int:
    """FNV-1a 32-bit hash.

    Reference impl from ``docs/design/consistent-hash-ring.md`` — kept
    bit-identical so cross-language clients can compute the same routes.
    Test vectors:

    >>> hex(fnv1a_32(b""))
    '0x811c9dc5'
    >>> hex(fnv1a_32(b"foobar"))
    '0xbf9cf968'
    """
    h = _FNV_BASIS
    for b in data:
        h ^= b
        h = (h * _FNV_PRIME) & _MASK_32
    return h


@dataclass(frozen=True, slots=True)
class NodeRef:
    """A live node eligible for routing."""

    node_id: str
    url: str
    weight: int = 1


class NoAvailableNodeError(Exception):
    """Raised when the live-node list is empty."""


def _score(node_id: str, key: str, weight: int) -> int:
    """HRW score — higher wins.

    Hash key is ``"{node_id}\\x00{key}"`` — the NUL separator prevents
    boundary collisions (e.g. ``("ab", "cd")`` vs ``("a", "bcd")``).
    """
    payload = f"{node_id}\x00{key}".encode()
    return fnv1a_32(payload) * max(weight, 1)


def select(key: str, nodes: list[NodeRef]) -> NodeRef:
    """Pick the highest-scoring node for *key*.

    Tie-breaks on ``node_id`` lexicographically descending (the
    higher-sorted ``node_id`` wins) so behavior stays deterministic if
    two nodes happen to score identically — vanishingly rare with
    32-bit hashes but worth pinning for tests.
    """
    if not nodes:
        raise NoAvailableNodeError("no live nodes")
    return max(
        nodes,
        key=lambda n: (_score(n.node_id, key, n.weight), n.node_id),
    )


def select_all(key: str, nodes: list[NodeRef]) -> list[NodeRef]:
    """Return *all* nodes ranked by score, highest first.

    Tie-break matches ``select`` — lexicographically descending on
    ``node_id`` so the top of the list always equals ``select(...)``.
    Used by callers that want a pre-computed fail-over list — e.g. a
    retry policy that, on connect failure to the primary, falls through
    to the second-highest scorer without re-running selection.
    """
    if not nodes:
        return []
    return sorted(
        nodes,
        key=lambda n: (_score(n.node_id, key, n.weight), n.node_id),
        reverse=True,
    )
