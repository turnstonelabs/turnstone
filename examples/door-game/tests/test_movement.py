"""Movement resolution tests.

Covers edge clipping on all four sides, blocking terrain, the two input
forms (``"NNEE"`` vs heading+distance) and their equivalence, location
entry flipping to MENU, the MAX_STEPS cap, and a stubbed always-encounter
RNG interrupting a walk with a pending fight.
"""

from __future__ import annotations

from tests.conftest import (
    FOREST,
    GRASS,
    WALL,
    WATER,
    LocationDef,
    Zone,
    make_player,
    make_world,
)
from understone.engine.models import Mode, WorldEvent
from understone.engine.movement import MAX_STEPS, parse_directions, resolve_move
from understone.engine.rng import GameRNG


class _NeverRNG(GameRNG):
    """An RNG whose chance() never fires (no wandering encounters)."""

    def __init__(self) -> None:
        super().__init__(seed=0)

    def chance(self, probability: float) -> bool:  # noqa: ARG002
        return False


class _AlwaysRNG(GameRNG):
    """An RNG whose chance() always fires (forces an encounter).

    The seed still drives ``weighted_index``/``randint``, so different seeds
    select different event rows while every encounter roll fires.
    """

    def __init__(self, seed: int = 0) -> None:
        super().__init__(seed=seed)

    def chance(self, probability: float) -> bool:  # noqa: ARG002
        return True


# ---------------------------------------------------------------------------
# parse_directions
# ---------------------------------------------------------------------------


def test_parse_steps_string() -> None:
    assert parse_directions("NNEE", "", 1) == ["N", "N", "E", "E"]


def test_parse_heading_distance() -> None:
    assert parse_directions("", "east", 3) == ["E", "E", "E"]


def test_parse_clamps_to_max_steps() -> None:
    assert parse_directions("NNNNNNNNNNNN", "", 1) == ["N"] * MAX_STEPS
    assert parse_directions("", "north", 99) == ["N"] * MAX_STEPS


def test_parse_rejects_unknown_direction() -> None:
    try:
        parse_directions("NQ", "", 1)
    except ValueError as exc:
        assert "Q" in str(exc)
    else:  # pragma: no cover - failure path
        raise AssertionError("expected ValueError")


# ---------------------------------------------------------------------------
# Edge clipping (all four sides)
# ---------------------------------------------------------------------------


def test_clip_north_edge() -> None:
    world = make_world()
    player = make_player(x=5, y=0)
    result = resolve_move(world, player, _NeverRNG(), heading="north", distance=3)
    assert player.y == 0
    assert result.steps_taken == 0
    assert result.blocked


def test_clip_south_edge() -> None:
    world = make_world()
    player = make_player(x=5, y=10)
    result = resolve_move(world, player, _NeverRNG(), heading="south", distance=3)
    assert player.y == 10
    assert result.blocked


def test_clip_west_edge() -> None:
    world = make_world()
    player = make_player(x=0, y=5)
    result = resolve_move(world, player, _NeverRNG(), heading="west", distance=3)
    assert player.x == 0
    assert result.blocked


def test_clip_east_edge() -> None:
    world = make_world()
    player = make_player(x=10, y=5)
    result = resolve_move(world, player, _NeverRNG(), heading="east", distance=3)
    assert player.x == 10
    assert result.blocked


def test_partial_move_then_clip() -> None:
    world = make_world()
    player = make_player(x=8, y=5)
    result = resolve_move(world, player, _NeverRNG(), heading="east", distance=5)
    # 8 -> 9 -> 10, then edge.
    assert player.x == 10
    assert result.steps_taken == 2
    assert result.blocked


# ---------------------------------------------------------------------------
# Blocking terrain
# ---------------------------------------------------------------------------


def test_blocked_by_wall() -> None:
    grid = [[GRASS for _ in range(11)] for _ in range(11)]
    grid[5][6] = WALL
    world = make_world(grid=grid)
    player = make_player(x=5, y=5)
    result = resolve_move(world, player, _NeverRNG(), heading="east", distance=2)
    assert player.x == 5
    assert result.blocked
    assert "wall" in result.blocked_reason


def test_blocked_by_water() -> None:
    grid = [[GRASS for _ in range(11)] for _ in range(11)]
    grid[4][5] = WATER
    world = make_world(grid=grid)
    player = make_player(x=5, y=5)
    result = resolve_move(world, player, _NeverRNG(), heading="north", distance=2)
    assert player.y == 5
    assert result.blocked
    assert "water" in result.blocked_reason


# ---------------------------------------------------------------------------
# Input-form equivalence and direction correctness
# ---------------------------------------------------------------------------


def test_nnee_lands_at_expected_cell() -> None:
    world = make_world()
    player = make_player(x=5, y=5)
    resolve_move(world, player, _NeverRNG(), steps="NNEE")
    # Two north (y-2), two east (x+2).
    assert (player.x, player.y) == (7, 3)


def test_heading_equivalent_to_steps() -> None:
    world_a = make_world()
    player_a = make_player(x=5, y=5)
    resolve_move(world_a, player_a, _NeverRNG(), steps="EEE")

    world_b = make_world()
    player_b = make_player(x=5, y=5)
    resolve_move(world_b, player_b, _NeverRNG(), heading="east", distance=3)

    assert (player_a.x, player_a.y) == (player_b.x, player_b.y)


def test_max_steps_truncates_long_walk() -> None:
    world = make_world(width=40, height=11)
    player = make_player(x=0, y=5)
    result = resolve_move(world, player, _NeverRNG(), heading="east", distance=99)
    assert result.steps_taken == MAX_STEPS
    assert player.x == MAX_STEPS


# ---------------------------------------------------------------------------
# Location entry flips to MENU
# ---------------------------------------------------------------------------


def test_entering_location_flips_menu_mode() -> None:
    loc = LocationDef(
        key="inn",
        kind="inn",
        name="The Sleeping Drake",
        x=7,
        y=5,
        glyph="I",
        color="town",
        actions=("rest", "leave"),
    )
    world = make_world(locations=[loc])
    player = make_player(x=5, y=5)
    result = resolve_move(world, player, _NeverRNG(), heading="east", distance=4)
    assert player.mode is Mode.MENU
    assert player.at_location == "inn"
    assert result.entered_location == "inn"
    # Stopped on the door at x=7 even though distance asked for 4.
    assert (player.x, player.y) == (7, 5)


# ---------------------------------------------------------------------------
# Encounter interrupt
# ---------------------------------------------------------------------------


def test_always_encounter_stops_with_pending_fight() -> None:
    grid = [[FOREST for _ in range(11)] for _ in range(11)]
    zone = Zone(key="wood", x0=0, y0=0, x1=10, y1=10, tier_lo=1, tier_hi=2)
    world = make_world(grid=grid, zones=[zone])
    player = make_player(x=5, y=5)
    result = resolve_move(world, player, _AlwaysRNG(), heading="east", distance=5)
    assert result.pending_fight == (1, 2)
    # The encounter fires on the first entered cell.
    assert result.steps_taken == 1
    assert player.x == 6


def test_no_zone_means_no_encounter() -> None:
    grid = [[FOREST for _ in range(11)] for _ in range(11)]
    world = make_world(grid=grid, zones=[])
    player = make_player(x=5, y=5)
    result = resolve_move(world, player, _AlwaysRNG(), heading="east", distance=3)
    assert result.pending_fight is None
    assert result.steps_taken == 3


# ---------------------------------------------------------------------------
# Weighted non-combat overworld events (v0.2)
# ---------------------------------------------------------------------------


def _event_world(*events: WorldEvent) -> object:
    """An all-forest, fully-zoned world carrying a crafted event table."""
    grid = [[FOREST for _ in range(11)] for _ in range(11)]
    zone = Zone(key="wood", x0=0, y0=0, x1=10, y1=10, tier_lo=1, tier_hi=2)
    return make_world(grid=grid, zones=[zone], events=list(events))


def test_event_fight_stops_the_walk() -> None:
    """A fight-kind event sets pending_fight and halts the walk like v0.1."""
    world = _event_world(WorldEvent("fight", 1, "", 0, 0))
    player = make_player(x=5, y=5)
    result = resolve_move(world, player, _AlwaysRNG(), heading="east", distance=5)
    assert result.pending_fight == (1, 2)
    assert result.event is None
    assert result.steps_taken == 1  # stopped on the first triggering cell


def test_event_gold_credits_and_continues() -> None:
    """A gold event credits the rolled amount and does NOT stop the walk."""
    world = _event_world(WorldEvent("gold", 1, "a coin-purse", 5, 5))
    player = make_player(x=5, y=5, gold=10)
    result = resolve_move(world, player, _AlwaysRNG(), heading="east", distance=3)
    assert result.event is not None
    assert result.event.kind == "gold"
    assert result.event.amount == 5  # min == max == 5, so deterministic
    assert player.gold == 15
    assert result.pending_fight is None
    assert result.steps_taken == 3  # the walk ran to completion


def test_event_heal_caps_at_max_hp() -> None:
    """A heal event never overfills: hp is clamped to max_hp."""
    world = _event_world(WorldEvent("heal", 1, "a spring", 50, 50))
    player = make_player(x=5, y=5, hp=18, max_hp=20)
    result = resolve_move(world, player, _AlwaysRNG(), heading="east", distance=1)
    assert player.hp == 20  # +50 requested, capped at the 2 missing
    assert result.event is not None and result.event.amount == 2


def test_event_trap_floors_hp_at_one_and_spares_gold() -> None:
    """A trap event never kills (floors at 1 HP) and never touches gold."""
    world = _event_world(WorldEvent("trap", 1, "old briars", 500, 500))
    player = make_player(x=5, y=5, hp=10, max_hp=20, gold=42)
    result = resolve_move(world, player, _AlwaysRNG(), heading="east", distance=1)
    assert player.hp == 1  # huge trap, but floored
    assert player.gold == 42  # gold untouched
    assert result.event is not None and result.event.amount == 9  # only 9 could be taken


def test_event_lore_mutates_nothing() -> None:
    """A lore event changes no state and reports a zero amount."""
    world = _event_world(WorldEvent("lore", 1, "an old waystone", 0, 0))
    player = make_player(x=5, y=5, hp=15, max_hp=20, gold=7)
    before = (player.hp, player.gold)
    result = resolve_move(world, player, _AlwaysRNG(), heading="east", distance=2)
    assert (player.hp, player.gold) == before
    assert result.event is not None and result.event.kind == "lore"
    assert result.event.amount == 0
    assert result.steps_taken == 2


def test_at_most_one_event_per_walk() -> None:
    """Once any event fires, no further cells roll for the rest of the walk.

    Two distinct gold rolls would credit 2 gold (1 each); a single fired event
    credits exactly 1, proving the walk stops rolling after the first trigger.
    """
    world = _event_world(WorldEvent("gold", 1, "a coin", 1, 1))
    player = make_player(x=5, y=5, gold=0)
    resolve_move(world, player, _AlwaysRNG(), heading="east", distance=5)
    assert player.gold == 1  # exactly one event, not five


def test_each_event_kind_reachable_with_crafted_table() -> None:
    """Equal weights make every kind in a crafted table reachable from movement."""
    table = [
        WorldEvent("fight", 1, "", 0, 0),
        WorldEvent("gold", 1, "g", 1, 1),
        WorldEvent("heal", 1, "h", 1, 1),
        WorldEvent("trap", 1, "t", 1, 1),
        WorldEvent("lore", 1, "l", 0, 0),
    ]
    zone = Zone(key="wood", x0=0, y0=0, x1=0, y1=0, tier_lo=1, tier_hi=2)
    grid = [[FOREST for _ in range(11)] for _ in range(11)]
    world = make_world(grid=grid, zones=[zone], events=table)

    seen: set[str] = set()
    for seed in range(60):
        player = make_player(x=0, y=1, hp=10, max_hp=20)  # one step north into the zone cell
        result = resolve_move(world, player, _AlwaysRNG(seed), steps="N")
        if result.pending_fight is not None:
            seen.add("fight")
        elif result.event is not None:
            seen.add(result.event.kind)
    assert seen == {"fight", "gold", "heal", "trap", "lore"}
