"""Combat resolution tests.

Pins determinism (a fixed seed yields identical results twice, log and
deltas), each outcome (win/lose/flee), xp/gold crediting on victory, and
the defeat contract: the result flags a spawn bounce with no xp/gold and a
zero hp delta (the façade applies hp=1 and the move).
"""

from __future__ import annotations

from tests.conftest import make_monster, make_player
from understone.engine.combat import Outcome, resolve_fight, resolve_flee
from understone.engine.rng import GameRNG

# A strong adventurer vs a Field Rat wins on every probed seed.
_WIN_SEED = 1
# A fragile adventurer vs a Stone Wyrm loses on every probed seed.
_LOSE_SEED = 0
# Flee outcomes (probed): seed 1 escapes clean, seed 0 is caught.
_FLEE_CLEAN_SEED = 1
_FLEE_CAUGHT_SEED = 0


def _strong_player() -> object:
    return make_player(hp=20, max_hp=20, atk=5, def_=1, xp=0, gold=50)


def _wyrm() -> object:
    return make_monster(tier=5, name="Stone Wyrm", hp=60, atk=18, def_=6, xp=140, gold=60)


def test_fight_is_deterministic_under_fixed_seed() -> None:
    r1 = resolve_fight(GameRNG(seed=7), make_player(), make_monster())
    r2 = resolve_fight(GameRNG(seed=7), make_player(), make_monster())
    assert r1.log == r2.log
    assert (r1.outcome, r1.xp_delta, r1.gold_delta, r1.hp_delta) == (
        r2.outcome,
        r2.xp_delta,
        r2.gold_delta,
        r2.hp_delta,
    )


def test_win_credits_xp_and_gold() -> None:
    player = make_player(hp=20, max_hp=20, atk=5, def_=1)
    monster = make_monster(hp=6, atk=3, def_=0, xp=8, gold=3)
    result = resolve_fight(GameRNG(seed=_WIN_SEED), player, monster)
    assert result.outcome is Outcome.WIN
    assert result.xp_delta == 8
    assert result.gold_delta == 3
    # hp_delta is non-positive (you may take a scratch) and never fatal here.
    assert result.hp_delta <= 0
    assert not result.bounce_to_spawn


def test_win_deltas_are_exact_for_pinned_seed() -> None:
    player = make_player(hp=20, max_hp=20, atk=5, def_=1)
    monster = make_monster(hp=6, atk=3, def_=0, xp=8, gold=3)
    result = resolve_fight(GameRNG(seed=_WIN_SEED), player, monster)
    # Pinned from a determinism probe; guards against silent damage drift.
    assert result.hp_delta == -1
    assert len(result.log) == 5


def test_loss_flags_bounce_without_rewards() -> None:
    result = resolve_fight(GameRNG(seed=_LOSE_SEED), _strong_player_loses(), _wyrm())
    assert result.outcome is Outcome.LOSE
    assert result.bounce_to_spawn is True
    assert result.xp_delta == 0
    assert result.gold_delta == 0
    # Combat does not set hp to 1 itself — that is the façade's job.
    assert result.hp_delta == 0


def _strong_player_loses() -> object:
    return make_player(hp=12, max_hp=12, atk=4, def_=0)


def test_flee_can_escape_clean() -> None:
    player = make_player(hp=20, max_hp=20, def_=1)
    monster = make_monster(atk=8, def_=2)
    result = resolve_flee(GameRNG(seed=_FLEE_CLEAN_SEED), player, monster)
    assert result.outcome is Outcome.FLED
    assert result.hp_delta == 0


def test_flee_caught_costs_hp_but_never_kills() -> None:
    player = make_player(hp=20, max_hp=20, def_=1)
    monster = make_monster(atk=8, def_=2)
    result = resolve_flee(GameRNG(seed=_FLEE_CAUGHT_SEED), player, monster)
    assert result.outcome is Outcome.FLED
    assert result.hp_delta < 0
    # A caught flight cannot drop the player to or below zero.
    assert player.hp + result.hp_delta >= 1


def test_flee_caught_never_kills_at_low_hp() -> None:
    player = make_player(hp=1, max_hp=20, def_=0)
    monster = make_monster(atk=40, def_=0)
    result = resolve_flee(GameRNG(seed=_FLEE_CAUGHT_SEED), player, monster)
    # At 1 HP the most a failed flee can cost is 0 (cannot go below 1).
    assert result.hp_delta == 0
