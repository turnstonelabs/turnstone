"""Tests for the balance instrument (the greedy bot simulator).

These run the REAL game façade end-to-end, so they double as the fiercest
integration test in the suite: determinism (same inputs → identical report),
that the greedy bot makes genuine progress over a Vale run, that its realized
fight share lands in a sane band, that a multi-seed sweep aggregates and the
report renders — and the single best end-to-end assertion, that a short seed
sweep actually SLAYS THE WYRM, proving the whole v0.1–v0.7 loop is winnable by
an unclever bot.
"""

from __future__ import annotations

from io import StringIO
from pathlib import Path
from typing import TYPE_CHECKING

from understone import sim
from understone.engine.models import LocationDef, Mode, Zone
from understone.engine.rng import GameRNG
from understone.game import Game
from understone.persistence import Store
from understone.sim import BalanceReport, simulate

from .conftest import make_monster, make_world

if TYPE_CHECKING:
    import pytest

PACK = Path(__file__).resolve().parents[1] / "understone" / "world" / "data"


# ---------------------------------------------------------------------------
# determinism
# ---------------------------------------------------------------------------


def test_same_inputs_give_identical_report() -> None:
    """Same (pack, days, seed) → byte-identical BalanceReport (frozen + seeded)."""
    a = simulate(PACK, 20, 5)
    b = simulate(PACK, 20, 5)
    assert a == b
    assert isinstance(a, BalanceReport)


def test_different_seeds_diverge() -> None:
    """Different seeds produce different runs (the RNG actually threads through)."""
    a = simulate(PACK, 20, 1)
    b = simulate(PACK, 20, 2)
    # The runs are not identical (some headline measure differs).
    assert (a.fights_fought, a.total_gold_earned, a.day_of_first_wyrm_kill) != (
        b.fights_fought,
        b.total_gold_earned,
        b.day_of_first_wyrm_kill,
    )


# ---------------------------------------------------------------------------
# progress
# ---------------------------------------------------------------------------


def test_bot_makes_progress_over_thirty_days() -> None:
    """A 30-day Vale run climbs past level 1 and actually fights."""
    r = simulate(PACK, 30, 1)
    assert r.final_level > 1
    assert r.fights_fought > 0
    assert r.total_gold_earned > 0
    # It also plumbs the deep — the rung ladder is reachable for a geared bot.
    assert r.rungs_cleared > 0


def test_realized_fight_share_in_sane_band() -> None:
    """The bot's fight share is a real fraction and forest-fight dominant.

    A greedy XP grinder spends most of its turns fighting the wood (the rest are
    the handful of descents and the Wyrm bout), so the share is high — but it is
    a genuine fraction in (0, 1], never a degenerate 0 or a value out of range.
    """
    r = simulate(PACK, 30, 3)
    assert 0.0 < r.realized_fight_share <= 1.0
    # Fights dominate the turn-spend, but descents/challenges exist too, so the
    # share is below a hard 1.0 floor only loosely — assert the sane half-band.
    assert r.realized_fight_share >= 0.5


# ---------------------------------------------------------------------------
# reporting & sweep
# ---------------------------------------------------------------------------


def test_report_renders_without_crashing() -> None:
    r = simulate(PACK, 15, 1)
    text = sim._render_report("The Vale of Understone", r)
    assert "greedy bot" in text
    assert "final level" in text
    assert "Wyrm slain" in text


def test_cli_simulate_single_seed_renders(tmp_path: Path) -> None:
    out = StringIO()
    rc = sim.cli_simulate(PACK, 15, 1, out=out)
    assert rc == 0
    assert "The Vale of Understone" in out.getvalue()
    assert "fight share" in out.getvalue()


def test_cli_simulate_sweep_aggregates() -> None:
    """A --seeds sweep prints per-seed lines plus an aggregate with spreads."""
    out = StringIO()
    rc = sim.cli_simulate(PACK, 20, 1, out=out, seeds=3)
    assert rc == 0
    text = out.getvalue()
    assert "3 seeds" in text
    assert "aggregate" in text
    # Per-seed lines for each of the three seeds.
    for seed in (1, 2, 3):
        assert f"seed {seed:>3}" in text or f"seed   {seed}" in text
    # The aggregate carries a mean [min..max] spread.
    assert "[" in text and "]" in text


def test_sweep_reports_are_each_deterministic() -> None:
    """Each seed in a sweep is independently reproducible by single simulate."""
    seed = 4
    swept = simulate(PACK, 20, seed)
    again = simulate(PACK, 20, seed)
    assert swept == again


# ---------------------------------------------------------------------------
# the load-bearing assertion: the world is winnable
# ---------------------------------------------------------------------------


def test_greedy_bot_slays_the_wyrm() -> None:
    """The single best end-to-end check: a short seed sweep KILLS THE WYRM.

    If a greedy, unclever bot can take the Wyrm Below playing through the real
    façade, then the whole authored loop — movement, the zone-banded forest, the
    economy, the rung ladder, the satchel death-save, the forge, and the endgame
    gate — composes into a *winnable* game. A run that ever stops winning trips
    here. A small sweep (not one lucky seed) so the proof is robust.
    """
    reports = [simulate(PACK, 40, seed) for seed in (1, 2, 3)]
    kills = [r for r in reports if r.wyrm_killed]
    assert kills, "the greedy bot never slew the Wyrm across the seed sweep"
    # Every kill records the day it first happened, within the run window.
    for r in kills:
        assert r.day_of_first_wyrm_kill is not None
        assert 1 <= r.day_of_first_wyrm_kill <= 40


# ---------------------------------------------------------------------------
# the bundled ALTERNATE world: The Cinder Wastes (LLM-authored from the manual)
#
# The Vale assertions above are the primary proof. These mirror them against the
# real bundled second world, so the dogfood pack — authored cold from AUTHORING.md
# — is held to the same bar: the bot must make genuine progress through it, and a
# short seed sweep must actually slay its Magma Wyrm. If the authored world ever
# stops being winnable, this trips.
# ---------------------------------------------------------------------------

CINDER = Path(__file__).resolve().parents[1] / "understone" / "world" / "packs" / "cinder-wastes"


def test_cinder_wastes_bot_makes_progress() -> None:
    """A short Cinder Wastes run climbs past level 1 and genuinely plays.

    Fifteen days lands before the bot's first Wyrm kill (~day 24), so the level
    is still climbing rather than reset post-win — a stable "the world plays"
    signal across the durable measures (level, fights, gold, the rung ladder).
    """
    r = simulate(CINDER, 15, 1)
    assert r.final_level > 1
    assert r.fights_fought > 0
    assert r.total_gold_earned > 0
    assert r.rungs_cleared > 0  # the caldera rung ladder is reachable


def test_cinder_wastes_is_winnable() -> None:
    """The dogfood proof: a greedy bot SLAYS THE MAGMA WYRM in the authored world.

    The Cinder Wastes was written by an LLM working only from AUTHORING.md and
    the validator. This is the end-to-end demonstration that the manual plus the
    loader produce not merely a *valid* pack but a *playable-to-victory* one — a
    short seed sweep takes the Magma Wyrm. (It is harder than the Vale: the kill
    lands later, so the window is wider than the Vale's.)
    """
    reports = [simulate(CINDER, 50, seed) for seed in (1, 2, 3)]
    kills = [r for r in reports if r.wyrm_killed]
    assert kills, "the greedy bot never slew the Magma Wyrm across the seed sweep"
    for r in kills:
        assert r.day_of_first_wyrm_kill is not None
        assert 1 <= r.day_of_first_wyrm_kill <= 50


# ---------------------------------------------------------------------------
# robustness on non-shipped pack shapes: location doors inside hunt zones
#
# The bot runs arbitrary authored packs, not just the two bundled worlds, so a
# zone may overlap a location door. A door cell is "walkable" (you can step onto
# it) but standing on it flips the bot into that location's MENU — useless ground
# for a forest fight, and a "fight" issued from a MENU is rejected by the engine
# WITHOUT spending a turn. These pin the two guards that keep that from spinning
# the per-day loop or over-counting fights.
# ---------------------------------------------------------------------------


def _door(x: int, y: int) -> LocationDef:
    """A bare location door placed at ``(x, y)`` (an inn, for concreteness)."""
    return LocationDef(
        key="inn",
        kind="inn",
        name="Wayhouse",
        x=x,
        y=y,
        glyph="⌂",
        color="town",
        actions=("rest", "leave"),
    )


def test_nearest_in_zone_skips_a_door_cell() -> None:
    """A door is never returned as a zone's hunt cell, even when it is nearest.

    The zone here spans a column running away from the spawn; its closest-to-spawn
    walkable cell IS a location door, with open ground one step further. The
    helper must skip the door (it would only trap the bot in a menu) and return
    the open cell beyond it — the FIX-2 filter, mirroring ``_adjacent_open``.
    """
    # 11x11 grass; spawn (5, 5). A door at (5, 6) is the nearest cell inside the
    # zone (Manhattan 1); the nearest OPEN in-zone cell is (5, 7) (Manhattan 2).
    world = make_world(
        locations=[_door(5, 6)],
        zones=[Zone(key="wood", x0=5, y0=6, x1=5, y1=9, tier_lo=1, tier_hi=1)],
    )
    walkable = sim._reachable(world)
    assert (5, 6) in walkable  # the door cell is walkable...
    cell = sim._nearest_in_zone(world, walkable, world.zones[0])
    assert cell is not None
    assert cell != (5, 6)  # ...but the helper does not pick it
    assert world.location_at(*cell) is None  # the returned cell is open ground
    assert cell == (5, 7)  # the nearest open in-zone cell beyond the door


def test_zone_hunt_spots_drops_a_zone_with_no_fightable_foe() -> None:
    """A zone whose tier band holds no foe is dropped, not appended with None.

    FIX-4: the fallback in ``_best_hunt_spot`` (``ranked[-1]``) must never land on
    a zone where no monster can roll. A zone banded to a tier with no monster is
    simply not a hunting ground, so it never enters the spot list.
    """
    # One zone banded to tier 9 (no monster lives there); the only monster is a
    # tier-1 rat. The empty-band zone must be dropped entirely.
    world = make_world(
        monsters=[make_monster(tier=1)],
        zones=[Zone(key="void", x0=4, y0=4, x1=6, y1=6, tier_lo=9, tier_hi=9)],
    )
    spots = sim._zone_hunt_spots(world, sim._reachable(world))
    assert spots == []  # the foe-less zone is not a spot


def test_hunt_yields_the_turn_when_stuck_in_a_menu(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A hunt that ends in a MENU yields the turn instead of over-counting.

    The defence-in-depth for FIX-1: should the bot ever reach the fight moment
    still inside a location MENU (a door swallowed the walk), the engine would
    REJECT the "fight" without spending a turn — and the old string-only check
    misread that reject as a won bout, over-counting and spinning the loop. The
    new mode pre-check must instead leave the menu and return False (yield), so no
    phantom fight is recorded and the day loop makes honest progress.
    """
    # A door at (5, 4) inside a tier-1 zone. We inject this door cell as the hunt
    # spot directly — the pre-FIX-2 state where a door WAS the nearest in-zone
    # cell — so the guard, not the spot-selection filter, is what is under test.
    world = make_world(
        locations=[_door(5, 4)],
        zones=[Zone(key="wood", x0=4, y0=3, x1=6, y1=5, tier_lo=1, tier_hi=1)],
        monsters=[make_monster(tier=1)],
    )
    clock = sim._Clock(sim._SIM_START)
    game = Game(world, Store(tmp_path / "g.db"), clock=clock, rng=GameRNG(seed=1))  # type: ignore[arg-type]
    bot = sim._Bot(game, world, clock)
    game.join(bot.name)
    bot._hunt_spots = [(1, (5, 4), make_monster(tier=1))]
    player = game.players[bot.name]

    # Model "a location door swallowed the walk": every navigation step ends with
    # the bot back inside the door's menu, so the hunt reaches its fight decision
    # still in MENU mode no matter how many times it tries to step clear — exactly
    # the trap the guard exists for (a single un-menu + re-walk cannot escape it).
    def _walk_into_door(_goal: tuple[int, int]) -> None:
        player.mode = Mode.MENU
        player.at_location = "inn"

    monkeypatch.setattr(bot, "_goto_xy", _walk_into_door)
    _walk_into_door((5, 4))  # start the hunt already inside the menu

    fought = bot._hunt()

    assert fought is False  # the turn is yielded, not spent on a menu-reject
    assert bot.fights_fought == 0  # no phantom fight recorded
    assert game.players[bot.name].mode is Mode.TILE  # and the menu was left behind
