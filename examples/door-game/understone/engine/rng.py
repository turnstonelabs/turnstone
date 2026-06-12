"""Randomness with deterministic test injection.

A single master ``GameRNG`` is seeded once at startup (from ``os.urandom``
in production). Per-encounter child generators are derived from the master
so a fight's rolls are reproducible given the same child seed. No tool
argument ever carries a seed — randomness is server-authoritative.
"""

from __future__ import annotations

import os
import random


class GameRNG:
    """A thin wrapper over ``random.Random`` with child-RNG derivation."""

    def __init__(self, seed: int | None = None) -> None:
        if seed is None:
            seed = int.from_bytes(os.urandom(8), "big")
        self._random = random.Random(seed)

    def chance(self, probability: float) -> bool:
        """Return ``True`` with the given probability in ``[0.0, 1.0]``."""
        if probability <= 0.0:
            return False
        if probability >= 1.0:
            return True
        return self._random.random() < probability

    def randint(self, lo: int, hi: int) -> int:
        """Return a random integer in the inclusive range ``[lo, hi]``."""
        return self._random.randint(lo, hi)

    def choice_index(self, count: int) -> int:
        """Return a random index in ``[0, count)``."""
        return self._random.randrange(count)

    def child(self) -> GameRNG:
        """Derive an independent child RNG seeded from the master stream."""
        return GameRNG(self._random.getrandbits(64))
