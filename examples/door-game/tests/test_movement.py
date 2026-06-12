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
from understone.engine.models import Mode
from understone.engine.movement import MAX_STEPS, parse_directions, resolve_move
from understone.engine.rng import GameRNG


class _NeverRNG(GameRNG):
    """An RNG whose chance() never fires (no wandering encounters)."""

    def __init__(self) -> None:
        super().__init__(seed=0)

    def chance(self, probability: float) -> bool:  # noqa: ARG002
        return False


class _AlwaysRNG(GameRNG):
    """An RNG whose chance() always fires (forces an encounter)."""

    def __init__(self) -> None:
        super().__init__(seed=0)

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
