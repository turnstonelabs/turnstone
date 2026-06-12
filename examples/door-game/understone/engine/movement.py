"""Overworld movement resolution.

Movement walks tile by tile so each intermediate cell is checked for
walls/edges and rolls an encounter. When a roll fires it weighted-picks one
row from the world's event table. A ``fight`` row STOPS the walk (a wandering
monster bars the path); the value-bearing rows (gold/heal/trap) and pure
``lore`` are applied immediately and the walk continues — but only one event
fires per walk, so once any row has fired no further cells roll.

The walk stops early on the first of: running out of steps, hitting a blocked
cell, stepping onto a location door (flips to MENU), or a ``fight`` encounter.

Movement spends no daily turns — only fighting does. Gold/heal/trap deltas are
applied straight to the player here (movement already mutates the player's
position), floored/capped so a trap never kills and a spring never overfills.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from understone.engine.models import Mode

if TYPE_CHECKING:
    from understone.engine.models import Player, WorldEvent
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
class MoveEvent:
    """A non-fight overworld event already applied to the player.

    ``kind`` is ``gold``/``heal``/``trap``/``lore``; ``text`` is the pack's
    flavour line; ``amount`` is the rolled magnitude (0 for ``lore``). The
    player's hp/gold have already been mutated by ``resolve_move`` — this
    record exists only so the façade can narrate what happened.
    """

    kind: str
    text: str
    amount: int = 0


@dataclass(slots=True)
class MoveResult:
    """Outcome of a movement attempt.

    ``steps_taken`` counts cells actually entered. ``blocked`` is set when a
    wall/edge stopped the walk. ``entered_location`` carries a location key
    when the walk ended on a door. ``pending_fight`` carries an opponent
    tier band when a ``fight`` encounter interrupted the walk. ``event``
    carries a non-fight overworld event (already applied) when one fired.
    """

    steps_taken: int
    blocked: bool = False
    blocked_reason: str = ""
    entered_location: str | None = None
    pending_fight: tuple[int, int] | None = None
    event: MoveEvent | None = None
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
    fired = False  # at most one overworld event per walk

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

        if fired:
            continue
        band = _encounter_band(world, nx, ny)
        if band is None:
            continue
        terrain = world.terrain_at(nx, ny)
        if not rng.chance(terrain.encounter_rate):
            continue
        fired = True
        picked = _pick_event(world, rng)
        if picked is None or picked.kind == "fight":
            result.pending_fight = band
            break
        result.event = _apply_event(player, rng, picked)

    return result


def _pick_event(world: World, rng: GameRNG) -> WorldEvent | None:
    """Weighted-pick one row from the world's event table, or ``None``.

    Returns ``None`` only when the pack ships no event table at all, in which
    case the caller falls back to the legacy always-a-fight behaviour.
    """
    weights = world.event_weights()
    if not weights:
        return None
    return world.events[rng.weighted_index(weights)]


def _apply_event(player: Player, rng: GameRNG, event: WorldEvent) -> MoveEvent:
    """Apply a non-fight event to *player* and return a record for narration.

    ``gold`` credits a rolled amount; ``heal`` adds hp capped at ``max_hp``;
    ``trap`` subtracts hp floored at 1 (a trap never kills, and never touches
    gold); ``lore`` mutates nothing. Amounts roll over ``[lo, hi]``.
    """
    if event.kind == "lore":
        return MoveEvent(kind="lore", text=event.text)
    amount = rng.randint(event.lo, event.hi)
    if event.kind == "gold":
        player.gold += amount
    elif event.kind == "heal":
        amount = min(amount, player.max_hp - player.hp)
        player.hp += amount
    elif event.kind == "trap":
        amount = min(amount, max(player.hp - 1, 0))
        player.hp -= amount
    return MoveEvent(kind=event.kind, text=event.text, amount=amount)


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
