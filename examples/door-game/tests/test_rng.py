"""GameRNG tests — the deterministic randomness seam.

Covers the v0.2 ``weighted_index`` helper: that a fixed seed reproduces the
same stream, that the cumulative-sum mapping honours the weights' proportions,
and that every index of a crafted table is reachable.
"""

from __future__ import annotations

from collections import Counter

from understone.engine.rng import GameRNG


def test_weighted_index_is_deterministic_under_seed() -> None:
    """Two RNGs at the same seed yield the identical weighted-index stream."""
    weights = [55, 8, 7, 5, 5, 5, 5, 3, 3, 4]
    a = GameRNG(seed=2026)
    b = GameRNG(seed=2026)
    draws_a = [a.weighted_index(weights) for _ in range(50)]
    draws_b = [b.weighted_index(weights) for _ in range(50)]
    assert draws_a == draws_b


def test_weighted_index_every_index_reachable() -> None:
    """With equal weights, a crafted table sees every index appear."""
    weights = [1, 1, 1, 1, 1]
    rng = GameRNG(seed=7)
    seen = {rng.weighted_index(weights) for _ in range(500)}
    assert seen == set(range(len(weights)))


def test_weighted_index_single_entry_always_zero() -> None:
    """A one-row table can only ever pick index 0."""
    rng = GameRNG(seed=1)
    assert all(rng.weighted_index([9]) == 0 for _ in range(20))


def test_weighted_index_respects_proportions() -> None:
    """A heavily-weighted index dominates the empirical distribution."""
    weights = [90, 5, 5]
    rng = GameRNG(seed=99)
    counts = Counter(rng.weighted_index(weights) for _ in range(4000))
    # Index 0 carries 90% of the mass; it must be by far the most common.
    assert counts[0] > counts[1] + counts[2]
    # And the rare indices still occur (no off-by-one swallowing the tail).
    assert counts[1] > 0 and counts[2] > 0
