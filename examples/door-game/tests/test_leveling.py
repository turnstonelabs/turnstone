"""XP curve, level-up, and restorative-maths tests.

Pins the threshold edges (at / just below / just above), a multi-level
jump from a single award, the exact growth table, the inn's flat-rate
full heal with affordability gating, and the healer's per-HP cost maths.
"""

from __future__ import annotations

from tests.conftest import DEFAULT_SETTINGS, make_player, make_settings
from understone.engine.leveling import apply_xp, heal, rest, xp_for_level

# Default curve is 100 * (n-1)*n/2 cumulative:
#   L2 = 100, L3 = 300, L4 = 600, L5 = 1000.


def test_xp_curve_thresholds() -> None:
    assert xp_for_level(1, DEFAULT_SETTINGS) == 0
    assert xp_for_level(2, DEFAULT_SETTINGS) == 100
    assert xp_for_level(3, DEFAULT_SETTINGS) == 300
    assert xp_for_level(4, DEFAULT_SETTINGS) == 600
    assert xp_for_level(5, DEFAULT_SETTINGS) == 1000


def test_just_below_threshold_does_not_level() -> None:
    player = make_player(level=1, xp=0, hp=20, max_hp=20)
    gains = apply_xp(player, 99, DEFAULT_SETTINGS)
    assert gains == []
    assert player.level == 1


def test_exact_threshold_levels_once() -> None:
    player = make_player(level=1, xp=0, hp=10, max_hp=20, atk=5, def_=1)
    gains = apply_xp(player, 100, DEFAULT_SETTINGS)
    assert len(gains) == 1
    assert player.level == 2
    # Growth table applied and a full heal granted on level-up.
    assert player.max_hp == 26
    assert player.atk == 7
    assert player.def_ == 2
    assert player.hp == player.max_hp


def test_just_above_threshold_levels_once() -> None:
    player = make_player(level=1, xp=0)
    gains = apply_xp(player, 101, DEFAULT_SETTINGS)
    assert len(gains) == 1
    assert player.level == 2
    assert player.xp == 101


def test_single_award_can_jump_multiple_levels() -> None:
    player = make_player(level=1, xp=0, max_hp=20, atk=5, def_=1)
    gains = apply_xp(player, 600, DEFAULT_SETTINGS)
    # 600 cumulative reaches level 4 (L2=100, L3=300, L4=600).
    assert player.level == 4
    assert [g.new_level for g in gains] == [2, 3, 4]
    # Three levels of growth stacked.
    assert player.max_hp == 20 + 3 * 6
    assert player.atk == 5 + 3 * 2
    assert player.def_ == 1 + 3 * 1


def test_growth_table_respects_settings() -> None:
    settings = make_settings(growth_max_hp=10, growth_atk=3, growth_def=2, xp_base=50)
    player = make_player(level=1, xp=0, max_hp=20, atk=5, def_=1)
    apply_xp(player, 50, settings)  # L2 at 50 with xp_base=50
    assert player.level == 2
    assert player.max_hp == 30
    assert player.atk == 8
    assert player.def_ == 3


# ---------------------------------------------------------------------------
# rest (inn) and heal (healer)
# ---------------------------------------------------------------------------


def test_rest_full_heals_and_charges() -> None:
    player = make_player(hp=5, max_hp=20, gold=50)
    assert rest(player, cost=15) is True
    assert player.hp == 20
    assert player.gold == 35


def test_rest_refused_when_unaffordable() -> None:
    player = make_player(hp=5, max_hp=20, gold=10)
    assert rest(player, cost=15) is False
    assert player.hp == 5
    assert player.gold == 10


def test_heal_charges_only_for_hp_restored() -> None:
    player = make_player(hp=15, max_hp=20, gold=100)
    result = heal(player, amount=10, cost_per_hp=2)
    # Only 5 HP were missing.
    assert result.healed == 5
    assert result.cost == 10
    assert player.hp == 20
    assert player.gold == 90


def test_heal_bounded_by_affordability() -> None:
    player = make_player(hp=2, max_hp=20, gold=7)
    result = heal(player, amount=10, cost_per_hp=2)
    # 7 gold buys 3 HP at 2/hp.
    assert result.healed == 3
    assert result.cost == 6
    assert player.hp == 5
    assert player.gold == 1


def test_heal_noop_when_full() -> None:
    player = make_player(hp=20, max_hp=20, gold=100)
    result = heal(player, amount=10, cost_per_hp=2)
    assert result.healed == 0
    assert result.cost == 0
    assert player.gold == 100
