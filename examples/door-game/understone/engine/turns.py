"""Daily action budget and the UTC-day rollover.

Turns refresh lazily: the first action on a new UTC day resets the
budget rather than relying on a scheduled job. The same rollover resets
the per-player bestow pool and the social daily caps (posts left, dice
played), so every daily allowance shares one boundary. The clock is
injected so tests can cross midnight deterministically.
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

    Returns ``True`` when a reset occurred. On a new UTC day this resets the
    turn budget (to *daily_turns*), the bestow pool, the daily post count, and
    the daily dice count — each back to its baseline — stamping the current UTC
    ordinal onto every day marker. Each counter is reset independently so a
    stale stamp on one never suppresses the refresh of another.
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
    if player.post_day != today:
        player.posts_sent = 0
        player.post_day = today
        reset = True
    if player.gamble_day != today:
        player.gambles = 0
        player.gamble_day = today
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
