"""Parse and validate a content pack into a runtime :class:`World`.

The pack is a directory of JSON files:

* ``terrain.json``   — terrain kinds keyed by legend character.
* ``monsters.json``  — monster definitions by tier.
* ``items.json``     — equipment / consumable definitions.
* ``locations.json`` — location kinds (name, glyph, actions, flavour).
* ``world.json``     — the map: dimensions, spawn, legend-compressed
  ``terrain_rows``, location placements, zones, and economy ``settings``.

Every validation failure raises :class:`WorldLoadError` with a message
aimed at a pack author (which file, which field, what was expected).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from understone.engine.models import (
    Item,
    LocationDef,
    Monster,
    Settings,
    Slot,
    TerrainDef,
    Zone,
)
from understone.engine.world import World
from understone.errors import WorldLoadError

# Sanity bands for economy settings: (min, max) inclusive, or (min, None).
# heal_cost_per_hp may be 0: in that config ALL healing in the world is free
# (the healer included), so a free bestow-heal is economically coherent.
_SETTINGS_BANDS: dict[str, tuple[int, int | None]] = {
    "daily_turns": (1, 100),
    "rest_cost": (0, None),
    "heal_cost_per_hp": (0, None),
    "starting_gold": (0, None),
    "xp_base": (1, None),
    "bestow_daily_budget": (0, 500),
}


def load_world(pack_dir: str | Path) -> World:
    """Load and validate the content pack at *pack_dir* into a ``World``."""
    root = Path(pack_dir)
    if not root.is_dir():
        raise WorldLoadError(f"content pack directory not found: {root}")

    terrain_defs = _load_terrain(root)
    monsters = _load_monsters(root)
    items = _load_items(root)
    location_kinds = _load_location_kinds(root)
    return _load_map(root, terrain_defs, monsters, items, location_kinds)


def _read_json(root: Path, name: str) -> Any:
    path = root / name
    if not path.is_file():
        raise WorldLoadError(f"missing pack file: {name}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise WorldLoadError(f"{name} is not valid JSON: {exc}") from exc


def _require(obj: dict[str, Any], key: str, where: str) -> Any:
    if key not in obj:
        raise WorldLoadError(f"{where} is missing required field {key!r}")
    return obj[key]


def _load_terrain(root: Path) -> dict[str, TerrainDef]:
    raw = _read_json(root, "terrain.json")
    if not isinstance(raw, dict):
        raise WorldLoadError("terrain.json must be an object keyed by legend character")
    out: dict[str, TerrainDef] = {}
    for key, spec in raw.items():
        if len(key) != 1:
            raise WorldLoadError(f"terrain.json legend key {key!r} must be a single character")
        where = f"terrain.json[{key!r}]"
        glyph = str(_require(spec, "glyph", where))
        if len(glyph) != 1:
            raise WorldLoadError(f"{where} glyph must be a single character, got {glyph!r}")
        rate = float(_require(spec, "encounter_rate", where))
        if not 0.0 <= rate <= 1.0:
            raise WorldLoadError(f"{where} encounter_rate must be within 0.0..1.0, got {rate}")
        out[key] = TerrainDef(
            key=str(_require(spec, "key", where)),
            glyph=glyph,
            walkable=bool(_require(spec, "walkable", where)),
            encounter_rate=rate,
            color=str(_require(spec, "color", where)),
        )
    if not out:
        raise WorldLoadError("terrain.json defines no terrain kinds")
    return out


def _load_monsters(root: Path) -> list[Monster]:
    raw = _read_json(root, "monsters.json")
    if not isinstance(raw, list):
        raise WorldLoadError("monsters.json must be a list of monster objects")
    out: list[Monster] = []
    for i, spec in enumerate(raw):
        where = f"monsters.json[{i}]"
        hp = int(_require(spec, "hp", where))
        if hp < 1:
            raise WorldLoadError(f"{where} hp must be >= 1, got {hp}")
        atk = int(_require(spec, "atk", where))
        def_ = int(_require(spec, "def", where))
        xp = int(_require(spec, "xp", where))
        gold = int(_require(spec, "gold", where))
        for label, val in (("atk", atk), ("def", def_), ("xp", xp), ("gold", gold)):
            if val < 0:
                raise WorldLoadError(f"{where} {label} must be >= 0, got {val}")
        out.append(
            Monster(
                tier=int(_require(spec, "tier", where)),
                name=str(_require(spec, "name", where)),
                hp=hp,
                atk=atk,
                def_=def_,
                xp=xp,
                gold=gold,
            )
        )
    if not out:
        raise WorldLoadError("monsters.json defines no monsters")
    return out


def _load_items(root: Path) -> list[Item]:
    raw = _read_json(root, "items.json")
    if not isinstance(raw, list):
        raise WorldLoadError("items.json must be a list of item objects")
    out: list[Item] = []
    for i, spec in enumerate(raw):
        where = f"items.json[{i}]"
        slot_raw = str(_require(spec, "slot", where))
        try:
            slot = Slot(slot_raw)
        except ValueError as exc:
            valid = ", ".join(s.value for s in Slot)
            raise WorldLoadError(f"{where} slot {slot_raw!r} is not one of: {valid}") from exc
        atk = int(spec.get("atk", 0))
        def_ = int(spec.get("def", 0))
        heal = int(spec.get("heal", 0))
        price = int(_require(spec, "price", where))
        for label, val in (("atk", atk), ("def", def_), ("heal", heal), ("price", price)):
            if val < 0:
                raise WorldLoadError(f"{where} {label} must be >= 0, got {val}")
        out.append(
            Item(
                item_id=str(_require(spec, "id", where)),
                name=str(_require(spec, "name", where)),
                slot=slot,
                atk=atk,
                def_=def_,
                heal=heal,
                price=price,
            )
        )
    if not out:
        raise WorldLoadError("items.json defines no items")
    return out


def _load_location_kinds(root: Path) -> dict[str, dict[str, Any]]:
    raw = _read_json(root, "locations.json")
    if not isinstance(raw, dict):
        raise WorldLoadError("locations.json must be an object keyed by location key")
    for key, spec in raw.items():
        where = f"locations.json[{key!r}]"
        _require(spec, "kind", where)
        _require(spec, "name", where)
        _require(spec, "glyph", where)
        glyph = str(spec["glyph"])
        if len(glyph) != 1:
            raise WorldLoadError(f"{where} glyph must be a single character, got {glyph!r}")
        _require(spec, "actions", where)
    return raw


def _load_map(
    root: Path,
    terrain_defs: dict[str, TerrainDef],
    monsters: list[Monster],
    items: list[Item],
    location_kinds: dict[str, dict[str, Any]],
) -> World:
    raw = _read_json(root, "world.json")
    if not isinstance(raw, dict):
        raise WorldLoadError("world.json must be an object")

    name = str(_require(raw, "name", "world.json"))
    width = int(_require(raw, "width", "world.json"))
    height = int(_require(raw, "height", "world.json"))
    if width <= 0 or height <= 0:
        raise WorldLoadError(f"world.json dimensions must be positive, got {width}x{height}")

    legend = _require(raw, "legend", "world.json")
    if not isinstance(legend, dict):
        raise WorldLoadError(
            "world.json legend must be an object mapping characters to terrain keys"
        )
    terrain_by_legend = _resolve_legend(legend, terrain_defs)

    rows = _require(raw, "terrain_rows", "world.json")
    terrain = _decode_rows(rows, width, height, terrain_by_legend)

    spawn = _decode_spawn(raw, width, height)
    locations = _decode_locations(raw, width, height, terrain, location_kinds)
    _ensure_walkable(terrain, locations, spawn)
    zones = _decode_zones(raw, width, height, monsters)
    settings = _decode_settings(raw, items, monsters)

    return World(
        name=name,
        width=width,
        height=height,
        spawn=spawn,
        terrain=terrain,
        locations=locations,
        zones=zones,
        monsters=monsters,
        items=items,
        settings=settings,
    )


def _resolve_legend(
    legend: dict[str, Any], terrain_defs: dict[str, TerrainDef]
) -> dict[str, TerrainDef]:
    resolved: dict[str, TerrainDef] = {}
    by_key = {t.key: t for t in terrain_defs.values()}
    for char, terrain_key in legend.items():
        if len(char) != 1:
            raise WorldLoadError(f"world.json legend key {char!r} must be a single character")
        key = str(terrain_key)
        if key not in by_key:
            valid = ", ".join(sorted(by_key))
            raise WorldLoadError(
                f"world.json legend maps {char!r} to unknown terrain {key!r}; known: {valid}"
            )
        resolved[char] = by_key[key]
    return resolved


def _decode_rows(
    rows: Any,
    width: int,
    height: int,
    legend: dict[str, TerrainDef],
) -> list[list[TerrainDef]]:
    if not isinstance(rows, list):
        raise WorldLoadError("world.json terrain_rows must be a list of strings")
    if len(rows) != height:
        raise WorldLoadError(f"world.json terrain_rows has {len(rows)} rows but height is {height}")
    grid: list[list[TerrainDef]] = []
    for y, row in enumerate(rows):
        if not isinstance(row, str):
            raise WorldLoadError(f"world.json terrain_rows[{y}] must be a string")
        if len(row) != width:
            raise WorldLoadError(
                f"world.json terrain_rows[{y}] is {len(row)} wide but width is {width}"
            )
        decoded: list[TerrainDef] = []
        for x, char in enumerate(row):
            if char not in legend:
                raise WorldLoadError(
                    f"world.json terrain_rows[{y}][{x}] uses {char!r}, which is not in the legend"
                )
            decoded.append(legend[char])
        grid.append(decoded)
    return grid


def _decode_spawn(raw: dict[str, Any], width: int, height: int) -> tuple[int, int]:
    spawn = _require(raw, "spawn", "world.json")
    if not (isinstance(spawn, list) and len(spawn) == 2):
        raise WorldLoadError("world.json spawn must be a [x, y] pair")
    sx, sy = int(spawn[0]), int(spawn[1])
    if not (0 <= sx < width and 0 <= sy < height):
        raise WorldLoadError(f"world.json spawn ({sx},{sy}) is outside the {width}x{height} map")
    return sx, sy


def _decode_locations(
    raw: dict[str, Any],
    width: int,
    height: int,
    terrain: list[list[TerrainDef]],
    location_kinds: dict[str, dict[str, Any]],
) -> list[LocationDef]:
    placements = _require(raw, "locations", "world.json")
    if not isinstance(placements, list):
        raise WorldLoadError("world.json locations must be a list of placements")
    out: list[LocationDef] = []
    seen: set[tuple[int, int]] = set()
    for i, place in enumerate(placements):
        where = f"world.json locations[{i}]"
        key = str(_require(place, "key", where))
        if key not in location_kinds:
            valid = ", ".join(sorted(location_kinds))
            raise WorldLoadError(f"{where} references unknown location {key!r}; known: {valid}")
        x = int(_require(place, "x", where))
        y = int(_require(place, "y", where))
        if not (0 <= x < width and 0 <= y < height):
            raise WorldLoadError(f"{where} position ({x},{y}) is outside the map")
        if (x, y) in seen:
            raise WorldLoadError(f"{where} stacks a second location on ({x},{y})")
        seen.add((x, y))
        kind = location_kinds[key]
        out.append(
            LocationDef(
                key=key,
                kind=str(kind["kind"]),
                name=str(kind["name"]),
                x=x,
                y=y,
                glyph=str(kind["glyph"]),
                color=str(kind.get("color", "town")),
                actions=tuple(str(a) for a in kind["actions"]),
                flavor=tuple(str(f) for f in kind.get("flavor", ())),
            )
        )
    return out


def _ensure_walkable(
    terrain: list[list[TerrainDef]],
    locations: list[LocationDef],
    spawn: tuple[int, int],
) -> None:
    sx, sy = spawn
    if not terrain[sy][sx].walkable:
        raise WorldLoadError(
            f"world.json spawn ({sx},{sy}) sits on non-walkable {terrain[sy][sx].key!r} terrain"
        )
    for loc in locations:
        if not terrain[loc.y][loc.x].walkable:
            raise WorldLoadError(
                f"location {loc.key!r} sits on non-walkable {terrain[loc.y][loc.x].key!r} "
                f"terrain at ({loc.x},{loc.y})"
            )


def _decode_zones(
    raw: dict[str, Any], width: int, height: int, monsters: list[Monster]
) -> list[Zone]:
    zones_raw = raw.get("zones", [])
    if not isinstance(zones_raw, list):
        raise WorldLoadError("world.json zones must be a list")
    tiers = {m.tier for m in monsters}
    out: list[Zone] = []
    for i, spec in enumerate(zones_raw):
        where = f"world.json zones[{i}]"
        rect = _require(spec, "rect", where)
        if not (isinstance(rect, list) and len(rect) == 4):
            raise WorldLoadError(f"{where} rect must be [x0, y0, x1, y1]")
        x0, y0, x1, y1 = (int(v) for v in rect)
        if not (0 <= x0 <= x1 < width and 0 <= y0 <= y1 < height):
            raise WorldLoadError(f"{where} rect {rect} is malformed or out of bounds")
        lo = int(_require(spec, "tier_lo", where))
        hi = int(_require(spec, "tier_hi", where))
        if lo > hi:
            raise WorldLoadError(f"{where} tier_lo {lo} exceeds tier_hi {hi}")
        if not any(lo <= t <= hi for t in tiers):
            raise WorldLoadError(f"{where} tier band {lo}..{hi} matches no monster tier")
        out.append(
            Zone(
                key=str(_require(spec, "key", where)),
                x0=x0,
                y0=y0,
                x1=x1,
                y1=y1,
                tier_lo=lo,
                tier_hi=hi,
            )
        )
    return out


def _decode_settings(raw: dict[str, Any], items: list[Item], monsters: list[Monster]) -> Settings:
    spec = _require(raw, "settings", "world.json")
    if not isinstance(spec, dict):
        raise WorldLoadError("world.json settings must be an object")

    values: dict[str, int] = {}
    for field_name, (lo, hi) in _SETTINGS_BANDS.items():
        value = int(_require(spec, field_name, "world.json settings"))
        if value < lo or (hi is not None and value > hi):
            band = f"{lo}..{hi}" if hi is not None else f">= {lo}"
            raise WorldLoadError(
                f"world.json settings.{field_name} = {value} is out of band ({band})"
            )
        values[field_name] = value

    growth = _require(spec, "growth", "world.json settings")
    if not isinstance(growth, dict):
        raise WorldLoadError("world.json settings.growth must be an object")
    growth_max_hp = int(_require(growth, "max_hp", "world.json settings.growth"))
    growth_atk = int(_require(growth, "atk", "world.json settings.growth"))
    growth_def = int(_require(growth, "def", "world.json settings.growth"))
    for label, val in (("max_hp", growth_max_hp), ("atk", growth_atk), ("def", growth_def)):
        if val < 0:
            raise WorldLoadError(f"world.json settings.growth.{label} must be >= 0, got {val}")

    item_ids = {it.item_id for it in items}
    starting_weapon = str(_require(spec, "starting_weapon", "world.json settings"))
    starting_armor = str(_require(spec, "starting_armor", "world.json settings"))
    for label, item_id in (
        ("starting_weapon", starting_weapon),
        ("starting_armor", starting_armor),
    ):
        if item_id not in item_ids:
            raise WorldLoadError(
                f"world.json settings.{label} = {item_id!r} is not a known item id"
            )

    dungeon_tiers = _decode_dungeon_tiers(spec, monsters)

    return Settings(
        daily_turns=values["daily_turns"],
        rest_cost=values["rest_cost"],
        heal_cost_per_hp=values["heal_cost_per_hp"],
        starting_gold=values["starting_gold"],
        starting_weapon=starting_weapon,
        starting_armor=starting_armor,
        xp_base=values["xp_base"],
        growth_max_hp=growth_max_hp,
        growth_atk=growth_atk,
        growth_def=growth_def,
        bestow_daily_budget=values["bestow_daily_budget"],
        dungeon_tiers=dungeon_tiers,
    )


def _decode_dungeon_tiers(spec: dict[str, Any], monsters: list[Monster]) -> tuple[int, ...]:
    """Parse the ordered dungeon-gauntlet tier ladder, one foe per tier.

    Each tier must be backed by at least one monster in the pack, or the
    gauntlet would silently skip that rung.
    """
    raw_tiers = _require(spec, "dungeon_tiers", "world.json settings")
    if not (isinstance(raw_tiers, list) and raw_tiers):
        raise WorldLoadError("world.json settings.dungeon_tiers must be a non-empty list of tiers")
    available = {m.tier for m in monsters}
    tiers: list[int] = []
    for i, value in enumerate(raw_tiers):
        tier = int(value)
        if tier not in available:
            raise WorldLoadError(
                f"world.json settings.dungeon_tiers[{i}] = {tier} has no monster in the pack"
            )
        tiers.append(tier)
    return tuple(tiers)
