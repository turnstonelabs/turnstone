"""The Wyrm Below — the v0.2 endgame, legacy reset, and the Herald feed.

Drives the challenge verb against the shipped pack: the level gate, the win
path (Hall of Legends + reincarnation), defeat, and the stalemate flight, plus
the run-days bookkeeping. Also pins the boss exclusion from random selection
and proves the new level_up / defeat beats reach OTHER players' Herald.

Negative-test discipline (the level gate):
  The challenge gate is pinned by ``test_challenge_under_level_refused``. To
  confirm the assertion has teeth, the implementer temporarily removed the
  ``if player.level < min_level`` refusal in Game._challenge (letting an
  under-level hero spend a turn and fight the Wyrm); the test then FAILED on
  the unchanged-turns assertion (a turn was consumed and the refusal line was
  absent). The guard was restored. This test is the standing regression.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import (
    fixed_clock,
    satchel_ids,
    set_satchel,
    utc,
)
from understone.engine.models import Mode
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
# tests/conftest.py now, shared with the descend suite; they are imported above.


def _at_dungeon(game: Game, name: str) -> object:
    """Place an already-joined player inside the dungeon menu, at the deep floor.

    The challenge verb now gates on depth as well as level: the Wyrm will not
    stir until the hero has plumbed the deep to its floor. These challenge
    tests exercise the win/lose/flee paths, not the gate, so the helper puts
    the hero at the bottom (deepest_rung == the rung count). The depth gate
    itself is exercised by the dedicated tests in test_descend.py.
    """
    player = game.players[name]
    player.mode = Mode.MENU
    player.at_location = "dungeon"
    player.deepest_rung = len(game.world.settings.dungeon_tiers)
    return player


# ---------------------------------------------------------------------------
# Boss exclusion from random selection
# ---------------------------------------------------------------------------


def test_boss_never_in_any_tier_band(tmp_path: Path, clock: object) -> None:
    """The Wyrm Below is never returned by monsters_for_tier_band, any band."""
    game = _game(tmp_path, clock)
    world = game.world
    tiers = [m.tier for m in world.monsters]
    lo, hi = min(tiers), max(tiers)
    for band_lo in range(lo, hi + 2):
        for band_hi in range(band_lo, hi + 2):
            band = world.monsters_for_tier_band(band_lo, band_hi)
            assert all(not m.boss for m in band)
            assert all(m.monster_id != "wyrm_below" for m in band)


# ---------------------------------------------------------------------------
# The level gate (negative-tested; see module docstring)
# ---------------------------------------------------------------------------


def test_challenge_under_level_refused(tmp_path: Path, clock: object) -> None:
    """An under-level hero is turned away in-fiction, spending no turn.

    See the module docstring for the revert-and-observe-failure check proving
    the gate has teeth.
    """
    game = _game(tmp_path, clock)
    game.join("Brak")
    player = _at_dungeon(game, "Brak")
    assert player.level < game.world.settings.wyrm_min_level
    before_turns = player.turns_left
    before_events = len(game.events)

    out = game.action("Brak", "challenge", "", "")

    assert "sixth circle" in out.lower()  # names the threshold in-fiction
    assert player.turns_left == before_turns  # no turn spent
    assert player.level == 1  # nothing reset
    assert len(game.events) == before_events  # no public news
    assert player.mode is Mode.MENU  # still standing at the dungeon


def test_challenge_at_level_threshold_is_allowed(tmp_path: Path, clock: object) -> None:
    """Exactly at the threshold the challenge proceeds (spends a turn)."""
    game = _game(tmp_path, clock)
    game.join("Brak")
    player = _at_dungeon(game, "Brak")
    player.level = game.world.settings.wyrm_min_level
    player.atk, player.def_, player.hp, player.max_hp = 500, 100, 500, 500
    before_turns = player.turns_left

    out = game.action("Brak", "challenge", "", "")

    assert "sixth circle" not in out.lower()  # not refused
    assert player.turns_left == before_turns - 1  # a turn was spent


def test_challenge_at_zero_turns_refused_clean(tmp_path: Path) -> None:
    """At the level gate but out of turns, the challenge is refused with no effect.

    A wyrm-eligible hero with an empty daily budget (and no day-roll to refill
    it) is turned away in-fiction: no turn drops below zero, no Hall row is
    cut, no public beat is written, wins are untouched — and the no-op player
    row is still committed (the refusal branch upserts + commits), so a store
    reopen sees the unchanged hero.
    """
    clk = _MutableClock(utc(2026, 6, 12, 10, 0))
    world = load_world(PACK)
    store = Store(tmp_path / "game.db")
    game = Game(world, store, clock=clk, rng=GameRNG(seed=7))  # type: ignore[arg-type]
    game.join("Brak")
    player = _at_dungeon(game, "Brak")
    player.level = game.world.settings.wyrm_min_level  # eligible
    player.turns_left = 0  # but spent for the day (same day: no refill)
    events_before = len(game.events)
    hall_before = len(game.store.top_hall(50))

    out = game.action("Brak", "challenge", "", "")

    assert "tomorrow" in out.lower()  # the "too spent ... today" refusal
    assert "sixth circle" not in out.lower()  # not the level gate
    assert player.turns_left == 0  # never spent below zero
    assert player.wins == 0  # no win recorded
    assert len(game.events) == events_before  # no public feed beat
    assert len(game.store.top_hall(50)) == hall_before  # no Hall row
    assert player.mode is Mode.MENU  # still standing at the dungeon

    # The refusal branch commits the (unchanged) row: a reopen sees the hero.
    game.store.close()
    reopened = Game(world, Store(tmp_path / "game.db"), clock=clk)  # type: ignore[arg-type]
    assert reopened.players["Brak"].turns_left == 0
    assert reopened.players["Brak"].wins == 0


# ---------------------------------------------------------------------------
# Win path: Hall of Legends + legacy reset
# ---------------------------------------------------------------------------


def test_challenge_win_resets_with_legacy(tmp_path: Path, clock: object) -> None:
    """A win records the run, heralds it, and reincarnates the hero with a ★."""
    game = _game(tmp_path, clock)
    game.join("Brak")
    player = _at_dungeon(game, "Brak")
    # Mid-run state that must be wiped by the reset.
    player.level, player.xp = 12, 5000
    player.atk, player.def_, player.hp, player.max_hp = 500, 100, 500, 500
    player.gold = 999
    player.weapon_id, player.armor_id = "war_axe", "chainmail"
    # State that must SURVIVE the reset.
    player.turns_left = 4
    player.log_cursor = 1
    player.bestow_spent = 7
    events_before = len(game.events)
    settings = game.world.settings

    out = game.action("Brak", "challenge", "", "")

    # Win narration and the immortalised run.
    assert "freed the vale" in out.lower()
    assert "hall of legends" in out.lower()
    # The legacy reset wipes xp/gold, so the Wyrm win must NOT narrate a reward
    # the hero never keeps (the old engine appended "+400 XP, +250 gold." to the
    # kill line, which _wyrm_won echoed verbatim). The boss's reward never lands.
    boss = game.world.monster_by_id(game.world.settings.boss_monster)
    assert boss is not None
    assert f"+{boss.xp} XP" not in out  # i.e. "+400 XP"
    assert f"+{boss.gold} gold" not in out  # i.e. "+250 gold"
    assert "+400 XP" not in out and "+250 gold" not in out
    hall = game.store.top_hall(5)
    assert len(hall) == 1
    assert hall[0].name == "Brak"
    assert hall[0].level_at_win == 12  # the level at the moment of the kill
    assert hall[0].run_days == 0  # same UTC day as the join under the frozen clock

    # A public news beat was written (all-caps herald moment).
    assert len(game.events) == events_before + 1
    assert game.events[-1].kind == "wyrm_win"
    assert "WYRM" in game.events[-1].text

    # Reincarnation: stats/gold/gear/position back to first-day values.
    assert player.wins == 1
    assert player.level == 1
    assert player.xp == 0
    assert player.gold == settings.starting_gold
    assert player.weapon_id == settings.starting_weapon
    assert player.armor_id == settings.starting_armor
    assert player.hp == player.max_hp
    assert (player.x, player.y) == game.world.spawn
    assert player.mode is Mode.TILE
    assert player.at_location == ""
    # The daily clock and the log cursor were deliberately left alone.
    assert player.turns_left == 4 - 1  # only the one challenge turn was spent
    assert player.log_cursor == 1
    assert player.bestow_spent == 7


def test_challenge_win_legacy_reset_spares_the_vault(tmp_path: Path, clock: object) -> None:
    """The vault SURVIVES a Wyrm-win rebirth; carried gold resets to starting.

    Banked gold is the one wealth (besides the ★) a legacy reset does not clear:
    the strongbox is the inn's, not the reborn hero's. This deposits gold into
    the vault through the inn, drives a Wyrm WIN, and asserts ``banked`` is
    UNCHANGED while ``gold`` drops back to ``starting_gold``.

    Negative-check (the revert-and-observe-failure discipline of this module):
    the implementer temporarily added ``player.banked = 0`` to
    Game._reset_with_legacy; this test then FAILED on the unchanged-``banked``
    assertion (the vault was wiped by the rebirth). The line was restored, so
    this test is the standing regression that the vault outlives the reset.
    """
    game = _game(tmp_path, clock)
    game.join("Brak")
    player = game.players["Brak"]
    # Bank some gold through the real inn path, then stand at the dungeon floor.
    player.gold = 200
    player.mode = Mode.MENU
    player.at_location = "inn"
    game.action("Brak", "deposit", "", "", amount=120)
    assert player.banked == 120 and player.gold == 80  # vault holds; hand drained

    player = _at_dungeon(game, "Brak")
    player.level = game.world.settings.wyrm_min_level
    player.atk, player.def_, player.hp, player.max_hp = 500, 100, 500, 500

    out = game.action("Brak", "challenge", "", "")

    assert "freed the vale" in out.lower()  # a genuine win drove the reset
    assert player.wins == 1
    assert player.banked == 120  # the vault is untouched by the rebirth
    assert player.gold == game.world.settings.starting_gold  # carried wealth resets


def test_challenge_win_star_in_rank_and_hall(tmp_path: Path, clock: object) -> None:
    """After a win, door_rank shows the ★ and renders the Hall of Legends."""
    game = _game(tmp_path, clock)
    game.join("Brak")
    player = _at_dungeon(game, "Brak")
    player.level = game.world.settings.wyrm_min_level
    player.atk, player.def_, player.hp, player.max_hp = 500, 100, 500, 500
    game.action("Brak", "challenge", "", "")

    out = game.rank("Brak")
    assert "★" in out
    assert "Hall of Legends" in out
    assert "Brak" in out


def test_two_wins_render_two_stars(tmp_path: Path, clock: object) -> None:
    """A second Wyrm kill stacks a second ★ on the leaderboard name."""
    game = _game(tmp_path, clock)
    game.join("Brak")
    for _ in range(2):
        player = _at_dungeon(game, "Brak")
        player.level = game.world.settings.wyrm_min_level
        player.atk, player.def_, player.hp, player.max_hp = 500, 100, 500, 500
        game.action("Brak", "challenge", "", "")
    assert game.players["Brak"].wins == 2
    assert "★★" in game.rank("Brak")


# ---------------------------------------------------------------------------
# Lose path and flight
# ---------------------------------------------------------------------------


def test_challenge_loss_bounces_and_heralds(tmp_path: Path, clock: object) -> None:
    """A defeat drops the hero to 1 HP at the spawn and heralds the devouring."""
    game = _game(tmp_path, clock)
    game.join("Brak")
    player = _at_dungeon(game, "Brak")
    player.level = game.world.settings.wyrm_min_level
    player.atk, player.def_, player.hp, player.max_hp = 5, 1, 20, 20  # outmatched
    events_before = len(game.events)

    out = game.action("Brak", "challenge", "", "")

    assert player.hp == 1
    assert (player.x, player.y) == game.world.spawn
    assert player.mode is Mode.TILE
    assert player.at_location == ""
    assert player.wins == 0  # a loss is not a win
    assert len(game.events) == events_before + 1
    devoured = game.events[-1]
    assert devoured.kind == "wyrm_lose"
    # Either phrasing of the devouring names the hero and the Wyrm.
    assert "Brak" in devoured.text and "Wyrm" in devoured.text
    assert "lays you low" in out.lower() or "wyrm" in out.lower()


def _doomed_wyrm_challenger(game: Game, name: str) -> object:
    """Stand *name* at the floor, wyrm-eligible, and doomed to a GRINDING loss.

    The stats — modest atk and def, hp 50 below max_hp 80, well off the spawn —
    make the Wyrm bout a genuine multi-round lethal loss (not a one-shot where
    no blow lands before the save). hp 50 is none of the potion heal values
    (15/40/70), so a death-save that sets hp to the potion's heal is unmistakable.
    """
    player = _at_dungeon(game, name)
    player.level = game.world.settings.wyrm_min_level
    player.x, player.y = 35, 25  # away from the spawn (a save never moves them)
    player.atk, player.def_, player.hp, player.max_hp = 6, 12, 50, 80
    return player


def test_challenge_loss_with_potion_survives_no_legacy_reset(tmp_path: Path, clock: object) -> None:
    """A lethal Wyrm bout with a potion is SURVIVED — no bounce, no legacy reset.

    The universal death-save reaches the Wyrm: a carried draught is drunk instead
    of the devouring. A save is NOT a win, so NOTHING resets — level, gold, and
    ``deepest_rung`` all stand — and it is NOT the devouring either, so the hero
    keeps their place at the dungeon. The PUBLIC beat is the survival one
    (``wyrm_flee``, "driven back, alive but unproven"), NEVER "devoured". The
    turn is still spent and the draught is consumed.
    """
    game = _game(tmp_path, clock)
    game.join("Brak")
    player = _doomed_wyrm_challenger(game, "Brak")
    potion = game.world.item_by_id("greater_potion")
    assert potion is not None
    _set_satchel(game, player, ["greater_potion"])
    floor = len(game.world.settings.dungeon_tiers)
    spawn = game.world.spawn
    before_turns = player.turns_left
    before_level, before_gold = player.level, player.gold
    events_before = len(game.events)

    out = game.action("Brak", "challenge", "", "")

    # Survived standing: hp at the potion's value, no bounce, draught spent.
    assert player.hp == min(player.max_hp, potion.heal)
    assert (player.x, player.y) != spawn  # NOT bounced to the spawn
    assert player.mode is Mode.MENU  # still standing at the dungeon
    assert _satchel_ids(game, player) == []  # the draught was spent
    assert "death's edge" in out.lower()  # the spliced survival line
    assert player.turns_left == before_turns - 1  # the challenge still cost a turn
    # No win, so NO legacy reset: level, gold, and depth all stand.
    assert player.wins == 0
    assert player.level == before_level
    assert player.gold == before_gold
    assert player.deepest_rung == floor  # depth untouched (no reset to 0)
    # The PUBLIC beat is the survival one, NOT the devouring.
    assert len(game.events) == events_before + 1
    beat = game.events[-1]
    assert beat.kind == "wyrm_flee"
    assert beat.kind != "wyrm_lose"
    assert "fled" in beat.text.lower() or "ran" in beat.text.lower()


def test_challenge_loss_potion_negative_without_save_devours(
    tmp_path: Path, clock: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NEGATIVE TEST: with the death-save disabled, the same potion-carrier is devoured.

    The mechanical equivalent of reverting the added ``_death_save`` call in
    ``_wyrm_lost``: we stub ``_death_save`` to always decline, then run the exact
    scenario of the survival test. The potion-carrier must now bounce to the
    spawn at 1 HP with the draught UNSPENT and the PUBLIC beat back to
    ``wyrm_lose`` (devoured) — proving the death-save (not some other path) is
    what saves them at the Wyrm. Restoring the real method (automatic when the
    patch lifts) restores the survival behaviour.
    """
    game = _game(tmp_path, clock)
    game.join("Brak")
    player = _doomed_wyrm_challenger(game, "Brak")
    _set_satchel(game, player, ["greater_potion"])
    floor = len(game.world.settings.dungeon_tiers)
    spawn = game.world.spawn

    monkeypatch.setattr(Game, "_death_save", lambda self, pl, lines: False)
    out = game.action("Brak", "challenge", "", "")

    assert player.hp == 1  # devoured, not saved
    assert (player.x, player.y) == spawn
    assert player.mode is Mode.TILE
    assert player.deepest_rung == floor  # a defeat keeps depth (no reset, no advance)
    assert _satchel_ids(game, player) == ["greater_potion"]  # the draught is UNSPENT
    assert "death's edge" not in out.lower()  # no save, no dramatic line
    assert game.events[-1].kind == "wyrm_lose"  # the devouring beat, not the survival one


def test_challenge_stalemate_counts_as_flight(tmp_path: Path, clock: object) -> None:
    """A 50-round stalemate resolves as a flight: a wyrm_flee news beat.

    With atk == boss def (no kill possible in the round cap) and enough HP to
    outlast the boss's chip damage, resolve_fight returns FLED deterministically.
    """
    game = _game(tmp_path, clock)
    game.join("Brak")
    player = _at_dungeon(game, "Brak")
    player.level = game.world.settings.wyrm_min_level
    player.atk, player.def_, player.hp, player.max_hp = 8, 24, 200, 200
    events_before = len(game.events)

    game.action("Brak", "challenge", "", "")

    assert player.wins == 0
    assert player.hp >= 1  # never killed by a flight
    assert len(game.events) == events_before + 1
    assert game.events[-1].kind == "wyrm_flee"
    assert "fled" in game.events[-1].text.lower() or "ran" in game.events[-1].text.lower()


# ---------------------------------------------------------------------------
# run_days from a frozen, advanced clock
# ---------------------------------------------------------------------------


class _MutableClock:
    """A clock whose reported moment can be advanced between calls."""

    def __init__(self, moment: object) -> None:
        self.moment = moment

    def __call__(self) -> object:
        return self.moment


def test_run_days_counts_whole_days(tmp_path: Path) -> None:
    """Joining, advancing the clock three days, then winning records run_days==3."""
    clk = _MutableClock(utc(2026, 6, 12, 10, 0))
    world = load_world(PACK)
    store = Store(tmp_path / "game.db")
    game = Game(world, store, clock=clk, rng=GameRNG(seed=7))  # type: ignore[arg-type]
    game.join("Brak")
    player = _at_dungeon(game, "Brak")
    player.level = game.world.settings.wyrm_min_level
    player.atk, player.def_, player.hp, player.max_hp = 500, 100, 500, 500

    clk.moment = utc(2026, 6, 15, 12, 0)  # three days (and a couple hours) later
    game.action("Brak", "challenge", "", "")

    hall = game.store.top_hall(1)
    assert hall[0].run_days == 3


def test_top_hall_orders_most_recent_first(tmp_path: Path) -> None:
    """Two heroes slay the Wyrm at advancing times; the latest tops the Hall.

    Pins ``ORDER BY id DESC`` in ``Store.top_hall`` — the most recently cut
    run is at index 0, regardless of name or level-at-win order.
    """
    clk = _MutableClock(utc(2026, 6, 12, 10, 0))
    world = load_world(PACK)
    store = Store(tmp_path / "game.db")
    game = Game(world, store, clock=clk, rng=GameRNG(seed=7))  # type: ignore[arg-type]

    def _win(name: str) -> None:
        game.join(name)
        hero = _at_dungeon(game, name)
        hero.level = game.world.settings.wyrm_min_level
        hero.atk, hero.def_, hero.hp, hero.max_hp = 500, 100, 500, 500
        game.action(name, "challenge", "", "")

    _win("Early")
    clk.moment = utc(2026, 6, 13, 10, 0)  # a day later
    _win("Later")

    hall = game.store.top_hall(5)
    assert len(hall) == 2
    assert hall[0].name == "Later"  # most recent run is first
    assert hall[1].name == "Early"


# ---------------------------------------------------------------------------
# Shared-feed proof: level_up and defeat reach ANOTHER player's Herald
# ---------------------------------------------------------------------------


def test_multi_level_jump_is_one_feed_beat_naming_final_level(
    tmp_path: Path, clock: object
) -> None:
    """A single award crossing two thresholds posts ONE level_up beat, at the top.

    With xp parked just under the level-3 line while still level 1, one forest
    kill vaults the hero past both the level-2 and level-3 thresholds. The
    public feed must carry exactly one level_up beat — a multi-level jump is one
    notable moment, not a flood — and that beat must name the FINAL level (3),
    not the intermediate one.
    """
    game = _game(tmp_path, clock)
    game.join("Climber")
    climber = game.players["Climber"]
    climber.x, climber.y = 35, 25  # forest_near zone
    climber.atk, climber.def_, climber.hp, climber.max_hp = 100, 50, 100, 100
    # Level 1 but xp just under L3 (300): the smallest forest reward (8) crosses
    # both L2 (100) and L3 (300) in this one award.
    climber.level, climber.xp = 1, 295
    events_before = len(game.events)

    game.action("Climber", "fight", "", "")

    assert climber.level == 3  # vaulted two levels on the single kill
    new_events = game.events[events_before:]
    level_ups = [e for e in new_events if e.kind == "level_up"]
    assert len(level_ups) == 1  # one beat, not one per level crossed
    assert "level 3" in level_ups[0].text.lower()  # names the final level
    assert "level 2" not in level_ups[0].text.lower()  # not the intermediate


def test_level_up_appears_in_other_players_herald(tmp_path: Path, clock: object) -> None:
    """A level-up by one hero is news in another hero's Herald."""
    game = _game(tmp_path, clock)
    game.join("Riser")
    game.join("Watcher")
    watcher = game.players["Watcher"]
    watcher.log_cursor = game._latest_event_id()  # start Watcher caught up

    riser = game.players["Riser"]
    riser.x, riser.y = 35, 25  # forest_near zone
    riser.atk, riser.def_, riser.hp, riser.max_hp = 100, 50, 100, 100
    riser.xp = 95  # one win (>= 8 xp) crosses the level-2 threshold of 100
    game.action("Riser", "fight", "", "")
    assert riser.level >= 2  # the fight pushed Riser over the line

    out = game.log("Watcher")
    assert "Riser" in out
    assert "level 2" in out.lower()


def test_defeat_appears_in_other_players_herald(tmp_path: Path, clock: object) -> None:
    """A defeat by a regular monster is news in another hero's Herald."""
    game = _game(tmp_path, clock)
    game.join("Faller")
    game.join("Watcher")
    watcher = game.players["Watcher"]
    watcher.log_cursor = game._latest_event_id()

    faller = game.players["Faller"]
    faller.x, faller.y = 35, 25  # forest_near zone
    faller.atk, faller.def_, faller.hp, faller.max_hp = 1, 0, 2, 20  # certain to fall
    game.action("Faller", "fight", "", "")
    assert faller.hp == 1  # bounced

    out = game.log("Watcher")
    assert "Faller" in out
    assert "dragged back" in out.lower() or "fell to" in out.lower() or "bested" in out.lower()


# ---------------------------------------------------------------------------
# Movement events at the façade: no turn, no public feed
# ---------------------------------------------------------------------------


def test_move_events_cost_no_turn_and_write_no_feed(tmp_path: Path, clock: object) -> None:
    """A walk that fires non-combat events spends no turn and posts no Herald news.

    Walks Brak back and forth across the forest_near zone (encounter_rate 0.25)
    enough that some non-fight event almost certainly fires; whatever happens,
    no turn is consumed and no public event is appended.
    """
    game = _game(tmp_path, clock)
    game.join("Brak")
    player = game.players["Brak"]
    player.x, player.y = 35, 25  # inside forest_near
    before_turns = player.turns_left
    before_events = len(game.events)

    for _ in range(12):
        game.move("Brak", "", "east", 1)
        game.move("Brak", "", "west", 1)

    assert player.turns_left == before_turns  # movement never costs a turn
    assert len(game.events) == before_events  # walk texture is private
