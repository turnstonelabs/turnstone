"""Leaderboard ordering.

Adventurers are ranked by level (desc), then XP (desc), then name (asc)
so ties break deterministically and alphabetically. The Hall of Legends is a
separate, append-only roll of completed runs (Wyrm kills), ordered newest
first by the store.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RankEntry:
    """One row of the leaderboard.

    ``wins`` is the number of times the adventurer has slain the Wyrm Below
    (each shown as a ★ beside the name); it does not affect ordering.
    """

    name: str
    level: int
    xp: int
    gold: int
    wins: int = 0


@dataclass(frozen=True, slots=True)
class HallEntry:
    """One immortalised run in the Hall of Legends (a Wyrm slain)."""

    name: str
    win_ts: str
    run_days: int
    level_at_win: int


def leaderboard(entries: list[RankEntry], limit: int = 10) -> list[RankEntry]:
    """Return the top ``limit`` entries in leaderboard order."""
    ordered = sorted(entries, key=lambda e: (-e.level, -e.xp, e.name))
    return ordered[:limit]
