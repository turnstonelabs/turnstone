"""The v0.7 "depth below" retention mechanics over the shipped world.

Drives the game façade against a temp store, a frozen clock, and a seeded RNG
to pin the four retention features and their interactions:

* the RUNG LADDER — descend advances ``deepest_rung`` one guardian at a time,
  reaching the floor opens the Wyrm's door, and a loss mid-descent PRESERVES
  the depth (you re-enter where you left off);
* the WYRM DEPTH GATE — the challenge is refused for a shallow hero with a
  message distinct from the level gate, and the level gate is checked FIRST;
* the SATCHEL and THE DEATH-SAVE — buying a potion stows it, quaff drinks the
  strongest, and a lethal loss with a potion in the bag is survived instead of
  bounced (the central rule), proven on both the fight and the ambush path;
* the FORGE — a +1 edge raises the live stat and costs scaled gold, capped,
  with EXACT accounting verified across forge -> buy -> sell;
* RARE BEASTS — a weighted pick surfaces them seldom, a rare kill heralds and
  drops a draught, and a full satchel blocks the drop without losing the kill.

Negative-test discipline (THE DEATH-SAVE):
  ``test_death_save_negative_without_satchel_check`` documents the revert: with
  the ``_death_save`` call removed from ``_apply_fight`` (so the lethal branch
  always bounces), the same potion-carrying hero who survives in
  ``test_death_save_fight_survives_at_potion_value`` instead wakes at the spawn
  at 1 HP with the potion UNSPENT. The implementer made that edit by hand,
  observed the survive-test fail (player bounced, potion still carried, no
  dramatic line), and restored the call. This pair is the standing regression.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from tests.conftest import (
    fixed_clock,
    make_monster,
    make_world,
    satchel_ids,
    set_satchel,
    utc,
)
from understone.engine.models import Mode, Zone
from understone.engine.rng import GameRNG
from understone.game import Game
from understone.persistence import Store
from understone.world.loader import load_world

PACK = Path(__file__).resolve().parents[1] / "understone" / "world" / "data"

# Module-local aliases for the shared satchel helpers, keeping the existing
# call sites (_set_satchel / _satchel_ids) unchanged.
_set_satchel = set_satchel
_satchel_ids = satchel_ids


@pytest.fixture
def clock() -> object:
    return fixed_clock(utc(2026, 6, 12, 10, 0))


def _game(tmp_path: Path, clock: object, seed: int = 7) -> Game:
    world = load_world(PACK)
    store = Store(tmp_path / "game.db")
    return Game(world, store, clock=clock, rng=GameRNG(seed=seed))  # type: ignore[arg-type]


# The flat-id-list satchel helpers (_set_satchel / _satchel_ids) live in
# tests/conftest.py now, shared with the Wyrm suite; they are imported above.


def _strong_at_dungeon(game: Game, name: str) -> object:
    """Join *name*, make them unbeatable, and stand them in the dungeon menu."""
    game.join(name)
    player = game.players[name]
    player.level, player.atk, player.def_ = 20, 200, 100
    player.hp = player.max_hp = 500
    player.mode = Mode.MENU
    player.at_location = "dungeon"
    return player


# ---------------------------------------------------------------------------
# A0) Forge ore — the combat-earned material that feeds the forge
# ---------------------------------------------------------------------------


def test_descend_win_drops_ore_into_satchel(tmp_path: Path, clock: object) -> None:
    """A won dungeon rung grants ore_dungeon_drop ore into the satchel."""
    game = _game(tmp_path, clock)
    player = _strong_at_dungeon(game, "Delver")
    expect = game.world.settings.ore_dungeon_drop
    ore_id = game.world.settings.forge_ore_item

    out = game.action("Delver", "descend", "", "")

    assert player.deepest_rung == 1  # the rung was cleared
    assert game._satchel_find(player, ore_id) == (0, expect)
    ore = game.world.item_by_id(ore_id)
    assert ore is not None and ore.name in out  # the find is narrated


def test_descend_win_ore_bumps_existing_stack(tmp_path: Path, clock: object) -> None:
    """A second cleared rung bumps the existing ore stack rather than opening a new one."""
    game = _game(tmp_path, clock)
    player = _strong_at_dungeon(game, "Delver")
    drop = game.world.settings.ore_dungeon_drop
    ore_id = game.world.settings.forge_ore_item

    game.action("Delver", "descend", "", "")
    game.action("Delver", "descend", "", "")

    assert player.deepest_rung == 2
    assert game._satchel_find(player, ore_id) == (0, 2 * drop)  # one stack, doubled
    assert game._satchel_distinct(player) == 1


def test_descend_win_ore_dropped_when_satchel_full(tmp_path: Path, clock: object) -> None:
    """When the bag has no ore stack AND is full of other kinds, ore is dropped.

    This pins the ore-when-satchel-full edge: the grant goes through the same
    add-chokepoint, so a full bag (distinct-stack cap reached, no ore stack to
    bump) cannot take the ore — it is narrated as dropped, NEVER an error, and
    the cleared rung still stands.
    """
    game = _game(tmp_path, clock)
    player = _strong_at_dungeon(game, "Delver")
    cap = game.world.settings.satchel_max
    ore_id = game.world.settings.forge_ore_item
    # Fill the distinct-stack cap with NON-ore kinds, so an ore drop has no stack
    # to bump and no free slot to open.
    _set_satchel(game, player, ["minor_potion", "greater_potion", "elixir_of_the_vale"])
    assert game._satchel_distinct(player) == cap
    assert game._satchel_find(player, ore_id) is None

    out = game.action("Delver", "descend", "", "")

    assert player.deepest_rung == 1  # the rung still cleared — no error
    assert game._satchel_find(player, ore_id) is None  # the ore found no room
    assert game._satchel_distinct(player) == cap  # bag unchanged
    assert "no room to pocket the ore" in out.lower()


def test_forest_win_drops_ore_on_a_successful_roll(
    tmp_path: Path, clock: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A won forest fight grants one ore when the ore_forest_chance roll succeeds.

    The roll is the injected RNG's ``chance``; forcing it True makes the wiring
    deterministic (the chance itself is a tuning value, exercised by the sim).
    """
    game = _game(tmp_path, clock)
    game.join("Hunter")
    player = game.players["Hunter"]
    player.x, player.y = 35, 25  # forest_near
    player.atk, player.def_, player.hp, player.max_hp = 200, 100, 500, 500
    ore_id = game.world.settings.forge_ore_item
    # Force the forest ore roll to succeed (chance(p) -> True for any p).
    monkeypatch.setattr(game.rng, "chance", lambda _p: True)

    game.action("Hunter", "fight", "", "")

    assert game._satchel_find(player, ore_id) == (0, 1)  # one ore from the win


def test_forest_loss_grants_no_ore(
    tmp_path: Path, clock: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A LOST forest fight grants no ore even if the roll would succeed."""
    game = _game(tmp_path, clock)
    player = _doomed_fighter(game, "Doomed")  # will lose (and has no potion)
    ore_id = game.world.settings.forge_ore_item
    monkeypatch.setattr(game.rng, "chance", lambda _p: True)

    game.action("Doomed", "fight", "", "")

    assert game._satchel_find(player, ore_id) is None  # ore is a WIN-only reward


# ---------------------------------------------------------------------------
# A) The rung ladder
# ---------------------------------------------------------------------------


def test_descend_advances_one_rung_at_a_time(tmp_path: Path, clock: object) -> None:
    """Each descent fights the NEXT rung and advances depth by exactly one."""
    game = _game(tmp_path, clock)
    player = _strong_at_dungeon(game, "Delver")

    out1 = game.action("Delver", "descend", "", "")
    assert "Forest Wolf" in out1  # rung 1 = tier-3 guardian
    assert player.deepest_rung == 1

    out2 = game.action("Delver", "descend", "", "")
    assert "Cave Troll" in out2  # rung 2 = tier-4 guardian
    assert player.deepest_rung == 2
    assert "Forest Wolf" not in out2  # a descent fights ONE rung, not a gauntlet


def test_reaching_the_floor_opens_the_wyrm_door(tmp_path: Path, clock: object) -> None:
    """Clearing the last rung narrates the Wyrm's door and unlocks the challenge."""
    game = _game(tmp_path, clock)
    player = _strong_at_dungeon(game, "Delver")
    floor = len(game.world.settings.dungeon_tiers)

    out = ""
    for _ in range(floor):
        out = game.action("Delver", "descend", "", "")

    assert player.deepest_rung == floor
    assert "wyrm's door" in out.lower()
    assert "challenge" in out.lower()


def test_descend_at_bottom_costs_no_turn(tmp_path: Path, clock: object) -> None:
    """Already at the floor, descend points at the challenge and spends nothing."""
    game = _game(tmp_path, clock)
    player = _strong_at_dungeon(game, "Delver")
    floor = len(game.world.settings.dungeon_tiers)
    player.deepest_rung = floor
    before_turns = player.turns_left

    out = game.action("Delver", "descend", "", "")

    assert "plumbed the deep" in out.lower()
    assert "challenge" in out.lower()
    assert player.turns_left == before_turns  # no fight, no turn
    assert player.deepest_rung == floor


def test_descend_costs_a_turn_and_refuses_when_spent(tmp_path: Path, clock: object) -> None:
    """A descent spends a daily turn; with none left it is refused like a fight."""
    game = _game(tmp_path, clock)
    player = _strong_at_dungeon(game, "Delver")
    before = player.turns_left
    game.action("Delver", "descend", "", "")
    assert player.turns_left == before - 1

    player.turns_left = 0
    rung_before = player.deepest_rung
    out = game.action("Delver", "descend", "", "")
    assert "too weary" in out.lower()
    assert player.deepest_rung == rung_before  # no rung gained on a refusal


def test_lose_mid_descent_persists_depth(tmp_path: Path, clock: object) -> None:
    """A loss bounces to the spawn but PRESERVES the depth already earned."""
    game = _game(tmp_path, clock)
    player = _strong_at_dungeon(game, "Delver")
    # Clear the first rung, then become weak so the second rung floors us.
    game.action("Delver", "descend", "", "")
    assert player.deepest_rung == 1
    player.mode = Mode.MENU
    player.at_location = "dungeon"
    player.atk, player.def_, player.hp, player.max_hp = 1, 0, 5, 5

    out = game.action("Delver", "descend", "", "")

    assert player.hp == 1
    assert (player.x, player.y) == game.world.spawn
    assert player.mode is Mode.TILE
    assert player.deepest_rung == 1  # the cleared rung is NOT lost
    assert "Cave Troll" in out  # we re-entered at rung 2 and fell there


def _doomed_descender(game: Game, name: str) -> object:
    """Stand *name* at rung 2 (deepest_rung == 1) and make the next rung lethal.

    The next descent faces the Cave Troll (the tier-4 guardian). The stats —
    weak atk, modest def, hp 30 below max_hp 60 — drag the loss out over many
    counter-rounds rather than a one-shot: a GENUINE grinding lethal loss (the
    death-save must not be vindicated by an artefact where no blow ever lands).
    The starting hp (30) is none of the potion heal values (15/40/70), so a
    death-save that sets hp to the potion's heal is unmistakable.
    """
    game.join(name)
    player = game.players[name]
    player.deepest_rung = 1  # already cleared rung 1; next descent is rung 2
    player.mode = Mode.MENU
    player.at_location = "dungeon"
    # Stand AWAY from the spawn so a death-save (which never moves the fighter)
    # is distinguishable from the lose-bounce (which sends them to the spawn).
    player.x, player.y = 35, 25
    player.atk, player.def_, player.hp, player.max_hp = 2, 8, 30, 60
    return player


def test_descend_loss_with_potion_survives_keeping_depth(tmp_path: Path, clock: object) -> None:
    """A lethal descent with a potion is SURVIVED standing, depth UNCHANGED.

    The universal death-save reaches the deep: a draught in the satchel is drunk
    instead of the spawn bounce. The hero keeps their place at the dungeon (no
    bounce), hp set to the potion's heal, the potion spent, ``deepest_rung``
    unchanged (the rung was not cleared, but the depth already earned stands),
    the dramatic line spliced in, and the turn still spent.
    """
    game = _game(tmp_path, clock)
    player = _doomed_descender(game, "Delver")
    potion = game.world.item_by_id("greater_potion")
    assert potion is not None
    _set_satchel(game, player, ["greater_potion"])
    spawn = game.world.spawn
    before_turns = player.turns_left
    before_events = len(game.events)

    out = game.action("Delver", "descend", "", "")

    assert "Cave Troll" in out  # the genuine rung-2 lethal bout was fought
    assert player.hp == min(player.max_hp, potion.heal)  # stood at the potion's value
    assert (player.x, player.y) != spawn  # NOT bounced to the spawn
    assert player.mode is Mode.MENU  # still standing at the dungeon
    assert player.deepest_rung == 1  # depth kept; the rung was not cleared
    assert _satchel_ids(game, player) == []  # the draught was spent
    assert "death's edge" in out.lower()  # the spliced survival line
    assert player.turns_left == before_turns - 1  # the descent still cost a turn
    # A death-save is not a defeat: no public "defeat" beat was heralded.
    new = game.events[before_events:]
    assert all(e.kind != "defeat" for e in new)


def test_descend_loss_without_potion_bounces_as_before(tmp_path: Path, clock: object) -> None:
    """Without a potion, the same lethal descent bounces to the spawn, depth kept.

    The companion to the survival test (and the standing negative for the deep
    save): an empty satchel falls through ``_death_save`` to the standard spawn
    bounce — 1 HP at the spawn, ``deepest_rung`` preserved, a public defeat beat.
    """
    game = _game(tmp_path, clock)
    player = _doomed_descender(game, "Delver")  # empty satchel
    spawn = game.world.spawn
    before_events = len(game.events)

    out = game.action("Delver", "descend", "", "")

    assert "Cave Troll" in out
    assert player.hp == 1  # bounced, not saved
    assert (player.x, player.y) == spawn
    assert player.mode is Mode.TILE
    assert player.deepest_rung == 1  # the cleared rung is still NOT lost
    assert "death's edge" not in out.lower()  # no save, no dramatic line
    new = game.events[before_events:]
    assert any(e.kind == "defeat" for e in new)  # the defeat beat fired


def test_descend_progress_in_status(tmp_path: Path, clock: object) -> None:
    """door_status shows the deep progress as 'rung N/total'."""
    game = _game(tmp_path, clock)
    _strong_at_dungeon(game, "Delver")
    floor = len(game.world.settings.dungeon_tiers)
    game.action("Delver", "descend", "", "")

    out = game.status("Delver")
    assert f"rung 1/{floor}" in out


# ---------------------------------------------------------------------------
# B) The Wyrm depth gate (distinct from the level gate; level checked first)
# ---------------------------------------------------------------------------


def test_challenge_refused_when_shallow_with_depth_message(tmp_path: Path, clock: object) -> None:
    """A high-level but shallow hero is refused with the DEPTH message, no turn."""
    game = _game(tmp_path, clock)
    game.join("Hero")
    player = game.players["Hero"]
    player.level = game.world.settings.wyrm_min_level  # clears the level gate
    player.deepest_rung = 0  # but has not plumbed the deep
    player.mode = Mode.MENU
    player.at_location = "dungeon"
    before_turns = player.turns_left
    before_events = len(game.events)

    out = game.action("Hero", "challenge", "", "")

    assert "plumbed the deep" in out.lower()
    assert "circle" not in out.lower()  # NOT the level-gate phrasing
    assert player.turns_left == before_turns  # no turn spent
    assert len(game.events) == before_events  # no public beat
    assert player.mode is Mode.MENU  # still at the dungeon


def test_level_gate_precedes_depth_gate(tmp_path: Path, clock: object) -> None:
    """A low-level shallow hero hears the LEVEL message, not the depth one."""
    game = _game(tmp_path, clock)
    game.join("Greenhorn")
    player = game.players["Greenhorn"]
    assert player.level < game.world.settings.wyrm_min_level
    player.deepest_rung = 0  # also shallow
    player.mode = Mode.MENU
    player.at_location = "dungeon"

    out = game.action("Greenhorn", "challenge", "", "")

    assert "circle" in out.lower()  # the level gate fires first
    assert "plumbed the deep" not in out.lower()


def test_challenge_allowed_at_level_and_floor(tmp_path: Path, clock: object) -> None:
    """At the level gate AND the deep floor, the challenge proceeds (spends a turn)."""
    game = _game(tmp_path, clock)
    game.join("Champion")
    player = game.players["Champion"]
    player.level = game.world.settings.wyrm_min_level
    player.deepest_rung = len(game.world.settings.dungeon_tiers)  # at the floor
    player.atk, player.def_, player.hp, player.max_hp = 500, 100, 500, 500
    player.mode = Mode.MENU
    player.at_location = "dungeon"
    before_turns = player.turns_left

    out = game.action("Champion", "challenge", "", "")

    assert "circle" not in out.lower()
    assert "plumbed the deep" not in out.lower()
    assert player.turns_left == before_turns - 1  # the challenge ran


def test_legacy_reset_clears_depth(tmp_path: Path, clock: object) -> None:
    """Slaying the Wyrm resets deepest_rung to 0 — the reborn hero earns it again."""
    game = _game(tmp_path, clock)
    game.join("Champion")
    player = game.players["Champion"]
    player.level = game.world.settings.wyrm_min_level
    player.deepest_rung = len(game.world.settings.dungeon_tiers)
    player.atk, player.def_, player.hp, player.max_hp = 500, 100, 500, 500
    player.mode = Mode.MENU
    player.at_location = "dungeon"

    game.action("Champion", "challenge", "", "")

    assert player.wins == 1
    assert player.deepest_rung == 0  # the deep must be plumbed anew


# ---------------------------------------------------------------------------
# C) The satchel: buy -> stow, the cap, and quaff
# ---------------------------------------------------------------------------


def _at_shop(game: Game, name: str) -> object:
    game.join(name)
    player = game.players[name]
    player.mode = Mode.MENU
    player.at_location = "shop"
    return player


def test_buy_potion_stows_into_satchel(tmp_path: Path, clock: object) -> None:
    """Buying a consumable adds it to the satchel and spends the gold."""
    game = _game(tmp_path, clock)
    player = _at_shop(game, "Buyer")
    player.gold = 100
    item = game.world.item_by_id("minor_potion")
    assert item is not None

    out = game.action("Buyer", "buy", "", "minor_potion")

    assert "satchel" in out.lower()
    assert _satchel_ids(game, player) == ["minor_potion"]
    assert player.gold == 100 - item.price
    # The potion was NOT applied on buy (HP unchanged from full).
    assert player.hp == player.max_hp


def test_satchel_potions_stack_into_one_slot(tmp_path: Path, clock: object) -> None:
    """Buying three of one potion makes ONE stack of qty 3, not three slots."""
    game = _game(tmp_path, clock)
    player = _at_shop(game, "Buyer")
    player.gold = 10000
    for _ in range(3):
        game.action("Buyer", "buy", "", "minor_potion")
    stacks = game._satchel_stacks(player)
    assert stacks == [("minor_potion", 3)]  # one distinct slot, qty 3
    assert game._satchel_distinct(player) == 1


def test_satchel_distinct_cap_refuses_fourth_kind_but_tops_up_existing(
    tmp_path: Path, clock: object
) -> None:
    """satchel_max caps DISTINCT stacks: three kinds fill the bag, a fourth kind
    is refused, but topping up an existing stack still fits."""
    game = _game(tmp_path, clock)
    player = _at_shop(game, "Buyer")
    cap = game.world.settings.satchel_max  # 3
    player.gold = 10000
    # Three DISTINCT kinds fill the distinct-stack cap (the shop sells exactly 3).
    for item_id in ("minor_potion", "greater_potion", "elixir_of_the_vale"):
        game.action("Buyer", "buy", "", item_id)
    assert game._satchel_distinct(player) == cap
    # A FOURTH distinct kind (ore, dropped straight in) is refused — bag full.
    assert game._satchel_try_add(player, "iron_ore") is False
    assert game._satchel_distinct(player) == cap
    # But topping up an EXISTING stack still works (qty is unbounded).
    assert game._satchel_try_add(player, "minor_potion") is True
    assert game._satchel_find(player, "minor_potion") == (0, 2)
    assert game._satchel_distinct(player) == cap  # still three kinds


def test_quaff_drinks_strongest_and_caps_at_max_hp(tmp_path: Path, clock: object) -> None:
    """quaff drinks the highest-heal potion, heals, caps at max_hp, removes it."""
    game = _game(tmp_path, clock)
    game.join("Drinker")
    player = game.players["Drinker"]
    # Carry a weak and a strong potion; the strong one must be chosen.
    _set_satchel(game, player, ["minor_potion", "greater_potion"])
    player.max_hp = 100
    player.hp = 90  # greater_potion heals 40, but the cap clamps the gain to 10

    out = game.action("Drinker", "quaff", "", "")

    assert "greater potion" in out.lower()  # the STRONGEST was drunk
    assert player.hp == 100  # capped at max_hp
    assert _satchel_ids(game, player) == ["minor_potion"]  # only the strong one left


def test_quaff_decrements_stack_and_removes_at_zero(tmp_path: Path, clock: object) -> None:
    """Quaffing a 2-deep potion stack decrements it; a second quaff empties it."""
    game = _game(tmp_path, clock)
    game.join("Drinker")
    player = game.players["Drinker"]
    _set_satchel(game, player, ["minor_potion", "minor_potion"])  # one stack, qty 2
    player.max_hp = 100
    player.hp = 10

    game.action("Drinker", "quaff", "", "")
    assert game._satchel_find(player, "minor_potion") == (0, 1)  # decremented, not dropped
    player.hp = 10

    game.action("Drinker", "quaff", "", "")
    assert game._satchel_find(player, "minor_potion") is None  # the stack is gone at 0
    assert game._satchel_stacks(player) == []


def test_quaff_empty_satchel_refuses(tmp_path: Path, clock: object) -> None:
    """quaff with an empty satchel is a friendly refusal."""
    game = _game(tmp_path, clock)
    game.join("Drinker")
    out = game.action("Drinker", "quaff", "", "")
    assert "no draught" in out.lower()


def test_quaff_at_full_hp_refuses_and_keeps_potion(tmp_path: Path, clock: object) -> None:
    """At full HP, quaff refuses rather than waste the draught."""
    game = _game(tmp_path, clock)
    game.join("Drinker")
    player = game.players["Drinker"]
    _set_satchel(game, player, ["minor_potion"])
    assert player.hp == player.max_hp

    out = game.action("Drinker", "quaff", "", "")

    assert "already hale" in out.lower()
    assert _satchel_ids(game, player) == ["minor_potion"]  # not wasted


def test_satchel_listed_in_status(tmp_path: Path, clock: object) -> None:
    """door_status lists the satchel contents."""
    game = _game(tmp_path, clock)
    game.join("Drinker")
    player = game.players["Drinker"]
    _set_satchel(game, player, ["minor_potion"])

    out = game.status("Drinker")
    assert "satchel" in out.lower()
    assert "Minor Potion" in out


# ---------------------------------------------------------------------------
# D) THE DEATH-SAVE (the central rule; negative-tested below)
# ---------------------------------------------------------------------------


def _doomed_fighter(game: Game, name: str) -> object:
    """Join *name*, stand them in a forest, and make a fight certain to kill."""
    game.join(name)
    player = game.players[name]
    player.x, player.y = 35, 25  # forest_near zone
    player.atk, player.def_, player.hp, player.max_hp = 1, 0, 2, 60
    return player


def test_death_save_fight_survives_at_potion_value(tmp_path: Path, clock: object) -> None:
    """A lethal fight with a potion in the satchel is SURVIVED, not bounced.

    See the module docstring for the revert-and-observe-failure check (paired
    with ``test_death_save_negative_without_satchel_check``).
    """
    game = _game(tmp_path, clock)
    player = _doomed_fighter(game, "Doomed")
    potion = game.world.item_by_id("greater_potion")
    assert potion is not None
    _set_satchel(game, player, ["greater_potion"])
    spawn = game.world.spawn
    before_events = len(game.events)

    out = game.action("Doomed", "fight", "", "")

    # Survived standing: hp set to the potion's heal (clamped to max_hp), NO
    # spawn bounce, the potion consumed, and the dramatic line present.
    assert player.hp == min(player.max_hp, potion.heal)
    assert (player.x, player.y) != spawn  # never moved to the spawn
    assert _satchel_ids(game, player) == []  # the draught was spent
    assert "death's edge" in out.lower()  # the spliced line
    # A death-save is NOT a defeat: no public "defeat" beat was heralded.
    new = game.events[before_events:]
    assert all(e.kind != "defeat" for e in new)


def test_death_save_negative_without_satchel_check(
    tmp_path: Path, clock: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NEGATIVE TEST: with the satchel check disabled, the same fight BOUNCES.

    This is the mechanical equivalent of reverting the ``_death_save`` call: we
    stub ``_death_save`` to always decline, then run the exact scenario of the
    survive-test. The hero must now wake at the spawn at 1 HP with the potion
    UNSPENT — proving the death-save (not some other path) is what saves them.
    Restoring the real method (automatic when the patch lifts) restores the
    survival behaviour.
    """
    game = _game(tmp_path, clock)
    player = _doomed_fighter(game, "Doomed")
    _set_satchel(game, player, ["greater_potion"])
    spawn = game.world.spawn

    monkeypatch.setattr(Game, "_death_save", lambda self, pl, lines: False)
    out = game.action("Doomed", "fight", "", "")

    assert player.hp == 1  # bounced, not saved
    assert (player.x, player.y) == spawn
    assert _satchel_ids(game, player) == ["greater_potion"]  # potion NOT spent
    assert "death's edge" not in out.lower()  # no dramatic line


def test_death_save_uses_strongest_potion(tmp_path: Path, clock: object) -> None:
    """The death-save spends the STRONGEST carried potion, leaving the rest."""
    game = _game(tmp_path, clock)
    player = _doomed_fighter(game, "Doomed")
    elixir = game.world.item_by_id("elixir_of_the_vale")
    assert elixir is not None
    _set_satchel(game, player, ["minor_potion", "elixir_of_the_vale"])

    game.action("Doomed", "fight", "", "")

    assert player.hp == min(player.max_hp, elixir.heal)  # the elixir, not the minor
    assert _satchel_ids(game, player) == ["minor_potion"]  # the weak one remains


def test_death_save_fires_from_a_stacked_potion(tmp_path: Path, clock: object) -> None:
    """The death-save works off a multi-deep potion stack, decrementing it by one."""
    game = _game(tmp_path, clock)
    player = _doomed_fighter(game, "Doomed")
    potion = game.world.item_by_id("greater_potion")
    assert potion is not None
    _set_satchel(game, player, ["greater_potion", "greater_potion", "greater_potion"])

    game.action("Doomed", "fight", "", "")

    assert player.hp == min(player.max_hp, potion.heal)  # saved at the potion's value
    assert game._satchel_find(player, "greater_potion") == (0, 2)  # one drunk, two left


def test_ambush_attacker_death_save(tmp_path: Path, clock: object) -> None:
    """A lethal ambush counter-strike is survived if the ATTACKER carries a potion."""
    game = _game(tmp_path, clock)
    game.join("Robber")
    game.join("Sleeper")
    settings = game.world.settings
    attacker = game.players["Robber"]
    victim = game.players["Sleeper"]
    # Both eligible and near in level; the sleeper is a deadly wake-up.
    attacker.level = victim.level = settings.ambush_min_level
    attacker.atk, attacker.def_, attacker.hp, attacker.max_hp = 1, 0, 2, 60
    # Stand the attacker AWAY from the spawn so a death-save (which never moves
    # the fighter) is distinguishable from the lose-bounce (which sends them to
    # the spawn). Both heroes are level-3+ and near in level, so the ambush is
    # legal; the field tile is unmanned so the band/sleep checks still pass.
    attacker.x, attacker.y = 35, 25
    victim.atk, victim.def_, victim.hp = 50, 0, 30
    victim.turn_day = 0  # asleep (has not acted today)
    potion = game.world.item_by_id("greater_potion")
    assert potion is not None
    _set_satchel(game, attacker, ["greater_potion"])
    spawn = game.world.spawn

    game.action("Robber", "ambush", "Sleeper", "")

    assert attacker.hp == min(attacker.max_hp, potion.heal)  # saved
    assert (attacker.x, attacker.y) != spawn  # not bounced — stood their ground
    assert _satchel_ids(game, attacker) == []  # potion spent


def test_ambush_victim_never_quaffs(tmp_path: Path, clock: object) -> None:
    """The sleeping ambush VICTIM never auto-quaffs — they are asleep.

    Even with a potion in the victim's satchel, a winning ambush robs them and
    drops them to 1 HP at the spawn; their draught is untouched.
    """
    game = _game(tmp_path, clock)
    game.join("Robber")
    game.join("Sleeper")
    settings = game.world.settings
    attacker = game.players["Robber"]
    victim = game.players["Sleeper"]
    attacker.level = victim.level = settings.ambush_min_level
    attacker.atk, attacker.def_, attacker.hp, attacker.max_hp = 200, 100, 500, 500
    victim.atk, victim.def_, victim.hp = 1, 0, 3
    victim.gold = 100
    victim.turn_day = 0  # asleep
    _set_satchel(game, victim, ["greater_potion"])

    game.action("Robber", "ambush", "Sleeper", "")

    assert victim.hp == 1  # robbed and floored, NOT death-saved
    assert (victim.x, victim.y) == game.world.spawn
    assert _satchel_ids(game, victim) == ["greater_potion"]  # the sleeper's potion is untouched


# ---------------------------------------------------------------------------
# E) The forge (exact accounting across forge -> buy -> sell)
# ---------------------------------------------------------------------------


def _ore_id(game: Game) -> str:
    return game.world.settings.forge_ore_item


def test_forge_plus_one_raises_stat_and_costs_scaled_gold_and_ore(
    tmp_path: Path, clock: object
) -> None:
    """forge +1 raises atk by 1 and costs base*(0+1) gold + 1 ore; +2 costs more of both."""
    game = _game(tmp_path, clock)
    player = _at_shop(game, "Smith")
    base = game.world.settings.forge_base_cost
    player.gold = 10000
    _set_satchel(game, player, [_ore_id(game)] * 20)
    atk0 = player.atk

    out1 = game.action("Smith", "forge", "weapon", "")
    assert player.weapon_plus == 1
    assert player.atk == atk0 + 1
    assert "+1" in out1
    after_first = player.gold
    assert after_first == 10000 - base * 1  # base * (0 + 1)
    assert game._satchel_find(player, _ore_id(game)) == (0, 19)  # 1 ore spent

    out2 = game.action("Smith", "forge", "weapon", "")
    assert player.weapon_plus == 2
    assert player.atk == atk0 + 2
    assert "+2" in out2
    assert player.gold == after_first - base * 2  # base * (1 + 1), dearer
    assert game._satchel_find(player, _ore_id(game)) == (0, 17)  # 2 more ore spent


def test_forge_armor_raises_def(tmp_path: Path, clock: object) -> None:
    """forge armour raises def_ by one per tier."""
    game = _game(tmp_path, clock)
    player = _at_shop(game, "Smith")
    player.gold = 10000
    _set_satchel(game, player, [_ore_id(game)] * 20)
    def0 = player.def_

    game.action("Smith", "forge", "armour", "")
    assert player.armor_plus == 1
    assert player.def_ == def0 + 1


def test_forge_caps_at_max_plus(tmp_path: Path, clock: object) -> None:
    """At the forge cap, a further forge is refused without mutation."""
    game = _game(tmp_path, clock)
    player = _at_shop(game, "Smith")
    cap = game.world.settings.forge_max_plus
    player.gold = 100000
    _set_satchel(game, player, [_ore_id(game)] * 50)
    for _ in range(cap):
        game.action("Smith", "forge", "weapon", "")
    assert player.weapon_plus == cap
    atk_at_cap, gold_at_cap = player.atk, player.gold
    ore_at_cap = game._satchel_find(player, _ore_id(game))

    out = game.action("Smith", "forge", "weapon", "")

    assert "no finer edge" in out.lower()
    assert player.weapon_plus == cap  # not over the cap
    assert player.atk == atk_at_cap  # no stat change
    assert player.gold == gold_at_cap  # no gold spent
    assert game._satchel_find(player, _ore_id(game)) == ore_at_cap  # no ore spent


def test_forge_unaffordable_gold_refuses(tmp_path: Path, clock: object) -> None:
    """A hero who cannot pay the forge gold is refused without mutation."""
    game = _game(tmp_path, clock)
    player = _at_shop(game, "Smith")
    player.gold = 1  # far below the base cost
    _set_satchel(game, player, [_ore_id(game)] * 20)  # ore is fine; gold is not
    atk0 = player.atk

    out = game.action("Smith", "forge", "weapon", "")

    assert player.weapon_plus == 0
    assert player.atk == atk0
    assert player.gold == 1
    assert game._satchel_find(player, _ore_id(game)) == (0, 20)  # ore untouched
    assert "gold" in out.lower()


def test_forge_without_ore_refuses_naming_both(tmp_path: Path, clock: object) -> None:
    """Plenty of gold but no ore: forge is refused, naming the gold AND ore cost."""
    game = _game(tmp_path, clock)
    player = _at_shop(game, "Smith")
    player.gold = 10000  # rich, but the satchel holds no ore
    atk0 = player.atk
    ore = game.world.item_by_id(_ore_id(game))
    assert ore is not None

    out = game.action("Smith", "forge", "weapon", "")

    assert player.weapon_plus == 0  # no mutation
    assert player.atk == atk0
    assert player.gold == 10000
    assert ore.name in out  # the message names the ore requirement
    assert "0 " + ore.name in out  # and that the hero holds none


def test_forge_invalid_target_is_friendly(tmp_path: Path, clock: object) -> None:
    """A missing/unknown forge target asks which slot, without mutation."""
    game = _game(tmp_path, clock)
    player = _at_shop(game, "Smith")
    player.gold = 10000

    out = game.action("Smith", "forge", "", "")

    assert "weapon" in out.lower() and "armour" in out.lower()
    assert player.weapon_plus == 0 and player.armor_plus == 0


def test_forge_then_buy_zeroes_plus_and_removes_phantom_atk(tmp_path: Path, clock: object) -> None:
    """Buying a new weapon after forging zeroes the plus AND removes phantom atk.

    The bug-prone path: a +2 blade's enhancement rode the OLD weapon. Swapping
    to a new blade must subtract the old base bonus AND the old +2, then equip
    the new base bonus — leaving atk exactly (unenhanced new weapon), with
    weapon_plus back to 0. Verified by reconstructing the expected atk from the
    base bonuses alone.
    """
    game = _game(tmp_path, clock)
    player = _at_shop(game, "Smith")
    player.gold = 100000
    _set_satchel(game, player, [_ore_id(game)] * 20)
    # Establish a known starting point: equip the short sword fresh.
    starter = game.world.item_by_id(game.world.settings.starting_weapon)
    short = game.world.item_by_id("short_sword")
    iron = game.world.item_by_id("iron_sword")
    assert starter is not None and short is not None and iron is not None
    base_human = game.world.settings.start_atk  # un-weaponed atk baseline

    game.action("Smith", "buy", "", "short_sword")
    assert player.weapon_id == "short_sword"
    assert player.weapon_plus == 0
    assert player.atk == base_human + short.atk

    # Forge the short sword to +2.
    game.action("Smith", "forge", "weapon", "")
    game.action("Smith", "forge", "weapon", "")
    assert player.weapon_plus == 2
    assert player.atk == base_human + short.atk + 2

    # Buy the iron sword: the +2 must vanish with the short sword.
    game.action("Smith", "buy", "", "iron_sword")
    assert player.weapon_id == "iron_sword"
    assert player.weapon_plus == 0  # the new blade is unenhanced
    assert player.atk == base_human + iron.atk  # NO phantom +2 left behind


def test_forge_then_sell_zeroes_plus_and_removes_phantom_atk(tmp_path: Path, clock: object) -> None:
    """Selling a forged weapon zeroes its plus and removes the phantom atk.

    A +1 short sword sold back must drop the short sword's base bonus AND the
    +1, falling to the starter blade with weapon_plus at 0 — exact accounting.
    """
    game = _game(tmp_path, clock)
    player = _at_shop(game, "Smith")
    player.gold = 100000
    _set_satchel(game, player, [_ore_id(game)] * 20)
    starter = game.world.item_by_id(game.world.settings.starting_weapon)
    short = game.world.item_by_id("short_sword")
    assert starter is not None and short is not None
    base_human = game.world.settings.start_atk

    game.action("Smith", "buy", "", "short_sword")
    game.action("Smith", "forge", "weapon", "")
    assert player.weapon_plus == 1
    assert player.atk == base_human + short.atk + 1

    game.action("Smith", "sell", "", "")

    assert player.weapon_id == game.world.settings.starting_weapon
    assert player.weapon_plus == 0  # the slot's enhancement is gone with the blade
    assert player.atk == base_human + starter.atk  # back to the starter, no phantom


def test_forge_plus_shown_in_status(tmp_path: Path, clock: object) -> None:
    """door_status shows the forged plus on the equipped weapon name."""
    game = _game(tmp_path, clock)
    player = _at_shop(game, "Smith")
    player.gold = 100000
    _set_satchel(game, player, [_ore_id(game)] * 20)
    game.action("Smith", "buy", "", "iron_sword")
    game.action("Smith", "forge", "weapon", "")
    game.action("Smith", "forge", "weapon", "")

    out = game.status("Smith")
    assert "Iron Sword +2" in out


# ---------------------------------------------------------------------------
# F) Rare named monsters (weighted pick + Herald flash + guaranteed draught)
# ---------------------------------------------------------------------------


def test_rare_kill_heralds_and_drops_draught(tmp_path: Path, clock: object) -> None:
    """Killing a rare emits a public rare_kill beat AND drops a draught.

    Seeds are scanned to land a Gilded Stag (the tier-2 rare) encounter, then
    the kill is checked for the Herald flash and the satchel drop.
    """
    game = _game(tmp_path, clock)
    game.join("Hunter")
    player = game.players["Hunter"]
    player.x, player.y = 35, 25  # forest_near (holds the Gilded Stag)
    player.atk, player.def_, player.hp, player.max_hp = 200, 100, 500, 500
    drop_id = game.world.settings.rare_drop_item
    before_events = len(game.events)

    # Walk until the weighted pick surfaces the rare (it is weight 1, so seldom).
    out = ""
    for _ in range(400):
        player.turns_left = 5  # keep a turn available
        out = game.action("Hunter", "fight", "", "")
        if "Gilded Stag" in out and "falls" in out:
            break
    else:  # pragma: no cover - the loop is expected to find a rare
        pytest.fail("no Gilded Stag encounter surfaced in 400 seeded fights")

    # The actor's own frame carries the guaranteed-drop line; the "A rare ...
    # has fallen" flash is a PUBLIC Herald beat (it rides the feed, not the
    # fighter's reply), so it is asserted on the event log below.
    assert "satchel" in out.lower()  # the draught dropped
    assert drop_id in _satchel_ids(game, player)
    new = game.events[before_events:]
    assert any(e.kind == "rare_kill" for e in new)
    rare_beat = next(e for e in new if e.kind == "rare_kill")
    assert "rare" in rare_beat.text.lower()
    assert "Gilded Stag" in rare_beat.text and "Hunter" in rare_beat.text


def test_rare_kill_full_satchel_blocks_drop_but_kill_lands(tmp_path: Path, clock: object) -> None:
    """A full satchel blocks the rare drop, but the kill (xp/gold/herald) still lands."""
    game = _game(tmp_path, clock)
    game.join("Hunter")
    player = game.players["Hunter"]
    player.x, player.y = 35, 25
    player.atk, player.def_, player.hp, player.max_hp = 200, 100, 500, 500
    cap = game.world.settings.satchel_max
    # Fill the DISTINCT-stack cap with three different kinds (the rare drop is a
    # fourth kind, greater_potion, with no stack to bump — so it has no room).
    _set_satchel(game, player, ["minor_potion", "elixir_of_the_vale", _ore_id(game)])
    assert game._satchel_distinct(player) == cap
    before_events = len(game.events)

    out = ""
    for _ in range(400):
        player.turns_left = 5
        before_gold = player.gold
        out = game.action("Hunter", "fight", "", "")
        if "Gilded Stag" in out and "falls" in out:
            break
    else:  # pragma: no cover
        pytest.fail("no Gilded Stag encounter surfaced in 400 seeded fights")

    assert "no room" in out.lower()  # the drop was blocked
    assert game._satchel_distinct(player) == cap  # still exactly full (distinct kinds)
    # The rare drop (a fourth, distinct kind) never landed.
    assert game._satchel_find(player, game.world.settings.rare_drop_item) is None
    assert player.gold > before_gold  # the kill still paid out
    new = game.events[before_events:]
    assert any(e.kind == "rare_kill" for e in new)  # and still heralded


def test_rung_guardian_is_the_fixed_band_zero_never_the_rare(tmp_path: Path, clock: object) -> None:
    """A rung is the fixed band[0] guardian, never the tier's rare beast.

    The rung foe is deterministic (band[0]), independent of the RNG: every
    dungeon tier's first non-boss monster is non-rare, and descending the first
    rung always faces the Forest Wolf rather than the tier-3 rare (the Hollow
    Knight) — the rare only surfaces through the WEIGHTED forest pick.
    """
    game = _game(tmp_path, clock)
    world = game.world
    for tier in world.settings.dungeon_tiers:
        guardian = world.monsters_for_tier_band(tier, tier)[0]
        assert not guardian.rare, f"rung tier {tier} guardian {guardian.name!r} is rare"

    # Descending the first rung faces the fixed guardian, never the tier-3 rare,
    # regardless of seed (band[0] is not a weighted roll). Each seed gets its
    # own DB file so the runs are independent.
    for seed in (1, 7, 13, 42, 99):
        sub_dir = tmp_path / f"seed_{seed}"
        sub_dir.mkdir()
        sub = _game(sub_dir, clock, seed=seed)
        player = _strong_at_dungeon(sub, "Delver")
        out = sub.action("Delver", "descend", "", "")
        assert "Forest Wolf" in out
        assert "Hollow Knight" not in out
        assert player.deepest_rung == 1
        sub.store.close()


def test_weighted_pick_surfaces_common_far_more_than_rare(tmp_path: Path, clock: object) -> None:
    """The weighted forest pick draws a common foe far more often than a rare.

    A crafted single-tier band — a weight-10 "Common Beast" and a weight-1
    "Rare Beast" — is sampled many times through the façade's ``_pick_monster``
    under a seeded RNG. Over the sample the common must dominate roughly 10:1,
    and BOTH must appear (the rare surfaces, just seldom). This pins that the
    weighted draw (not a flat uniform pick) governs random encounters.
    """
    common = make_monster(name="Common Beast", tier=2, weight=10, rare=False)
    rare = make_monster(name="Rare Beast", tier=2, weight=1, rare=True)
    zone = Zone(key="wood", x0=0, y0=0, x1=10, y1=10, tier_lo=2, tier_hi=2)
    world = make_world(monsters=[common, rare], zones=[zone])
    store = Store(tmp_path / "weighted.db")
    game = Game(world, store, clock=clock, rng=GameRNG(seed=7))  # type: ignore[arg-type]
    player = _join_at(game, "Forager", 5, 5)

    counts: Counter[str] = Counter()
    for _ in range(2000):
        picked = game._pick_monster(player)
        assert picked is not None
        counts[picked.name] += 1

    assert counts["Common Beast"] > 0 and counts["Rare Beast"] > 0  # both surface
    # The common is ~10x the rare; a generous 4x floor keeps the test stable
    # under RNG variance while still proving the weighting is in force.
    assert counts["Common Beast"] > counts["Rare Beast"] * 4
    store.close()


def _join_at(game: Game, name: str, x: int, y: int) -> object:
    """Join *name* and place them at (x, y) on the overworld (test helper)."""
    game.join(name)
    player = game.players[name]
    player.x, player.y = x, y
    return player
