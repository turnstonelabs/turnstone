"""Leaderboard ordering.

Adventurers are ranked by level (desc), then XP (desc), then name (asc)
so ties break deterministically and alphabetically.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RankEntry:
    """One row of the leaderboard."""

    name: str
    level: int
    xp: int
    gold: int


def leaderboard(entries: list[RankEntry], limit: int = 10) -> list[RankEntry]:
    """Return the top ``limit`` entries in leaderboard order."""
    ordered = sorted(entries, key=lambda e: (-e.level, -e.xp, e.name))
    return ordered[:limit]
