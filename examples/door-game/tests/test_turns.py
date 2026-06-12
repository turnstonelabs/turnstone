"""Daily-turn budget and UTC rollover tests.

Covers spend/refuse semantics, the lazy reset when the UTC day advances
(including a 23:59 -> 00:01 crossing on the same Player instance), and the
shared rollover of the bestow pool.
"""

from __future__ import annotations

from tests.conftest import fixed_clock, make_player, utc
from understone.engine.turns import ensure_day, spend_turn


def test_spend_decrements() -> None:
    player = make_player(turns_left=3)
    assert spend_turn(player) is True
    assert player.turns_left == 2


def test_spend_refuses_at_zero_without_mutation() -> None:
    player = make_player(turns_left=0)
    before = player.turns_left
    assert spend_turn(player) is False
    assert player.turns_left == before


def test_ensure_day_resets_on_new_day() -> None:
    day = utc(2026, 6, 12).toordinal()
    player = make_player(turns_left=0, turn_day=day - 1, bestow_spent=20, bestow_day=day - 1)
    reset = ensure_day(player, fixed_clock(utc(2026, 6, 12, 9, 0)), daily_turns=10)
    assert reset is True
    assert player.turns_left == 10
    assert player.turn_day == day
    assert player.bestow_spent == 0
    assert player.bestow_day == day


def test_ensure_day_noop_within_same_day() -> None:
    day = utc(2026, 6, 12).toordinal()
    player = make_player(turns_left=4, turn_day=day, bestow_spent=10, bestow_day=day)
    reset = ensure_day(player, fixed_clock(utc(2026, 6, 12, 23, 0)), daily_turns=10)
    assert reset is False
    assert player.turns_left == 4
    assert player.bestow_spent == 10


def test_midnight_crossing_refreshes_on_same_instance() -> None:
    # Evening of day one: spend down to a low budget.
    player = make_player(turns_left=10, turn_day=0, bestow_spent=0, bestow_day=0)
    evening = utc(2026, 6, 12, 23, 59)
    ensure_day(player, fixed_clock(evening), daily_turns=10)
    for _ in range(8):
        spend_turn(player)
    assert player.turns_left == 2

    # Just past midnight (UTC) the next action refreshes the budget.
    after_midnight = utc(2026, 6, 13, 0, 1)
    reset = ensure_day(player, fixed_clock(after_midnight), daily_turns=10)
    assert reset is True
    assert player.turns_left == 10
    assert player.turn_day == after_midnight.toordinal()


def test_bestow_pool_resets_on_the_same_boundary() -> None:
    player = make_player(bestow_spent=25, bestow_day=utc(2026, 6, 12).toordinal())
    ensure_day(player, fixed_clock(utc(2026, 6, 13, 0, 1)), daily_turns=10)
    assert player.bestow_spent == 0
    assert player.bestow_day == utc(2026, 6, 13).toordinal()
