"""Daily action budget and the UTC-day rollover.

Turns refresh lazily: the first action on a new UTC day resets the
budget rather than relying on a scheduled job. The same rollover resets
the per-player bestow pool, so both daily allowances share one boundary.
The clock is injected so tests can cross midnight deterministically.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from understone.engine.models import Player


def _utc_ordinal(clock: Callable[[], datetime]) -> int:
    """Return today's proleptic-Gregorian ordinal in UTC."""
    return clock().toordinal()


def ensure_day(player: Player, clock: Callable[[], datetime], daily_turns: int) -> bool:
    """Refresh daily allowances if the UTC day has advanced.

    Returns ``True`` when a reset occurred. Resets both the turn budget
    (to *daily_turns*) and the bestow pool (to empty), stamping the current
    UTC ordinal onto both day markers.
    """
    today = _utc_ordinal(clock)
    reset = False
    if player.turn_day != today:
        player.turns_left = daily_turns
        player.turn_day = today
        reset = True
    if player.bestow_day != today:
        player.bestow_spent = 0
        player.bestow_day = today
        reset = True
    return reset


def spend_turn(player: Player) -> bool:
    """Consume one daily turn.

    Returns ``True`` and decrements when a turn is available; returns
    ``False`` and leaves state untouched when the budget is exhausted.
    """
    if player.turns_left <= 0:
        return False
    player.turns_left -= 1
    return True
