"""Overworld movement resolution.

Movement walks tile by tile so each intermediate cell is checked for
walls/edges and rolls an encounter. The walk stops early on the first of:
running out of steps, hitting a blocked cell, stepping onto a location
door (flips to MENU), or triggering a wandering-monster encounter.

Movement spends no daily turns — only fighting does.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from understone.engine.models import Mode

if TYPE_CHECKING:
    from understone.engine.models import Player
    from understone.engine.rng import GameRNG
    from understone.engine.world import World

MAX_STEPS = 8

_DELTAS: dict[str, tuple[int, int]] = {
    "N": (0, -1),
    "S": (0, 1),
    "E": (1, 0),
    "W": (-1, 0),
}

_HEADINGS: dict[str, str] = {
    "north": "N",
    "south": "S",
    "east": "E",
    "west": "W",
    "n": "N",
    "s": "S",
    "e": "E",
    "w": "W",
}


@dataclass(slots=True)
class MoveResult:
    """Outcome of a movement attempt.

    ``steps_taken`` counts cells actually entered. ``blocked`` is set when a
    wall/edge stopped the walk. ``entered_location`` carries a location key
    when the walk ended on a door. ``pending_fight`` carries an opponent
    tier band when an encounter interrupted the walk.
    """

    steps_taken: int
    blocked: bool = False
    blocked_reason: str = ""
    entered_location: str | None = None
    pending_fight: tuple[int, int] | None = None
    path_notes: list[str] = field(default_factory=list)


def parse_directions(steps: str, heading: str, distance: int) -> list[str]:
    """Translate either input form into a clamped list of cardinal steps.

    The ``steps`` string (e.g. ``"NNEE"``) takes precedence when non-empty;
    otherwise ``heading`` + ``distance`` is expanded. Either way the result
    is clamped to ``MAX_STEPS``. Unknown direction characters are rejected.
    """
    raw = steps.strip().upper()
    if raw:
        dirs: list[str] = []
        for ch in raw:
            if ch not in _DELTAS:
                raise ValueError(f"unknown direction {ch!r} (use N/S/E/W)")
            dirs.append(ch)
        return dirs[:MAX_STEPS]

    head = heading.strip().lower()
    if not head:
        return []
    if head not in _HEADINGS:
        raise ValueError(f"unknown heading {heading!r} (use north/south/east/west)")
    count = max(0, min(distance, MAX_STEPS))
    return [_HEADINGS[head]] * count


def resolve_move(
    world: World,
    player: Player,
    rng: GameRNG,
    *,
    steps: str = "",
    heading: str = "",
    distance: int = 1,
    max_steps: int = MAX_STEPS,
) -> MoveResult:
    """Walk *player* across *world* one cell at a time, mutating position.

    Stops at the first blocking edge/wall, location door, or encounter.
    Returns a :class:`MoveResult` describing where and why the walk ended.
    """
    directions = parse_directions(steps, heading, distance)[:max_steps]
    result = MoveResult(steps_taken=0)

    for direction in directions:
        dx, dy = _DELTAS[direction]
        nx, ny = player.x + dx, player.y + dy

        if not world.in_bounds(nx, ny):
            result.blocked = True
            result.blocked_reason = "the edge of the known world"
            break
        if not world.is_walkable(nx, ny):
            terrain = world.terrain_at(nx, ny)
            result.blocked = True
            result.blocked_reason = _blocked_phrase(terrain.key)
            break

        player.x, player.y = nx, ny
        result.steps_taken += 1

        location = world.location_at(nx, ny)
        if location is not None:
            player.mode = Mode.MENU
            player.at_location = location.key
            result.entered_location = location.key
            break

        band = _encounter_band(world, nx, ny)
        if band is not None:
            terrain = world.terrain_at(nx, ny)
            if rng.chance(terrain.encounter_rate):
                result.pending_fight = band
                break

    return result


def _encounter_band(world: World, x: int, y: int) -> tuple[int, int] | None:
    """Return the tier band for an encounter at ``(x, y)``, or ``None``.

    Encounters only happen inside a zone; open terrain with no zone is safe.
    """
    zone = world.zone_for(x, y)
    if zone is None:
        return None
    return (zone.tier_lo, zone.tier_hi)


def _blocked_phrase(terrain_key: str) -> str:
    """Return an in-fiction phrase for being blocked by *terrain_key*."""
    phrases = {
        "water": "deep water",
        "tree": "an impassable thicket",
        "wall": "a sheer wall",
    }
    return phrases.get(terrain_key, "rough ground")
