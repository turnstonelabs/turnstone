"""Runtime world model — terrain, locations, zones, content tables, settings.

Built by ``world.loader`` from JSON. The engine queries this for
walkability, encounter rates, location lookups, and tier-banded monster
selection. It holds no mutable game state.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from understone.engine.models import (
        Item,
        LocationDef,
        Monster,
        Settings,
        TerrainDef,
        WorldEvent,
        Zone,
    )


class World:
    """An immutable-after-construction view of the game map and content."""

    def __init__(
        self,
        *,
        name: str,
        width: int,
        height: int,
        spawn: tuple[int, int],
        terrain: list[list[TerrainDef]],
        locations: list[LocationDef],
        zones: list[Zone],
        monsters: list[Monster],
        items: list[Item],
        settings: Settings,
        events: list[WorldEvent] | None = None,
    ) -> None:
        self.name = name
        self.width = width
        self.height = height
        self.spawn = spawn
        self.terrain = terrain
        self.locations = locations
        self.zones = zones
        self.monsters = monsters
        self.items = items
        self.settings = settings
        self.events: list[WorldEvent] = events or []
        self._event_weights: list[int] = [e.weight for e in self.events]
        self._loc_by_xy: dict[tuple[int, int], LocationDef] = {
            (loc.x, loc.y): loc for loc in locations
        }
        self._loc_by_key: dict[str, LocationDef] = {loc.key: loc for loc in locations}
        self._item_by_id: dict[str, Item] = {it.item_id: it for it in items}
        self._monster_by_id: dict[str, Monster] = {
            m.monster_id: m for m in monsters if m.monster_id
        }

    def in_bounds(self, x: int, y: int) -> bool:
        """Return whether ``(x, y)`` is inside the map rectangle."""
        return 0 <= x < self.width and 0 <= y < self.height

    def terrain_at(self, x: int, y: int) -> TerrainDef:
        """Return the terrain definition at ``(x, y)`` (caller bounds-checks)."""
        return self.terrain[y][x]

    def location_at(self, x: int, y: int) -> LocationDef | None:
        """Return the location placed at ``(x, y)``, if any."""
        return self._loc_by_xy.get((x, y))

    def location_by_key(self, key: str) -> LocationDef | None:
        """Return the location with the given key, if any."""
        return self._loc_by_key.get(key)

    def item_by_id(self, item_id: str) -> Item | None:
        """Return the item with the given id, if any."""
        return self._item_by_id.get(item_id)

    def monster_by_id(self, monster_id: str) -> Monster | None:
        """Return the monster with the given id, if any (boss lookup)."""
        return self._monster_by_id.get(monster_id)

    def event_weights(self) -> list[int]:
        """Return the parallel weight list for the overworld event table."""
        return self._event_weights

    def is_walkable(self, x: int, y: int) -> bool:
        """Return whether a player may stand on ``(x, y)``.

        Out-of-bounds is never walkable. A location tile is always walkable
        regardless of its underlying terrain (you can step onto the door).
        """
        if not self.in_bounds(x, y):
            return False
        if (x, y) in self._loc_by_xy:
            return True
        return self.terrain[y][x].walkable

    def zone_for(self, x: int, y: int) -> Zone | None:
        """Return the first zone whose rectangle contains ``(x, y)``."""
        for zone in self.zones:
            if zone.contains(x, y):
                return zone
        return None

    def monsters_for_tier_band(self, lo: int, hi: int) -> list[Monster]:
        """Return non-boss monsters whose tier falls within ``[lo, hi]``.

        Boss monsters (the Wyrm Below) are never returned: they are the fixed
        endgame foe, faced only through the deliberate ``challenge`` verb, and
        must never surface as a random encounter or a dungeon-gauntlet rung.
        """
        return [m for m in self.monsters if lo <= m.tier <= hi and not m.boss]
