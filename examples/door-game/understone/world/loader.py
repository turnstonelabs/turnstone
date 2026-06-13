"""Parse and validate a content pack into a runtime :class:`World`.

The pack is a directory of JSON files:

* ``terrain.json``   — terrain kinds keyed by legend character.
* ``monsters.json``  — monster definitions by tier.
* ``items.json``     — equipment / consumable definitions.
* ``locations.json`` — location kinds (name, glyph, actions, flavour).
* ``events.json``    — the weighted overworld encounter table.
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
    WorldEvent,
    Zone,
)
from understone.engine.textwidth import is_grid_safe
from understone.engine.world import World
from understone.errors import WorldLoadError

# Sanity bands for economy settings: (min, max) inclusive, or (min, None).
# heal_cost_per_hp may be 0: in that config ALL healing in the world is free
# (the healer included), so a free bestow-heal is economically coherent.
SETTINGS_BANDS: dict[str, tuple[int, int | None]] = {
    "daily_turns": (1, 100),
    "rest_cost": (0, None),
    "heal_cost_per_hp": (0, None),
    "starting_gold": (0, None),
    "start_hp": (1, 500),
    "start_atk": (0, 100),
    "start_def": (0, 100),
    "xp_base": (1, None),
    "bestow_daily_budget": (0, 500),
    "wyrm_min_level": (1, 50),
    "ambush_min_level": (1, 50),
    "ambush_level_band": (0, 10),
    "ambush_gold_pct": (0, 100),
    "post_daily_cap": (0, 50),
    "gamble_max_bet": (1, 10000),
    "gamble_daily_cap": (0, 100),
    "satchel_max": (1, 10),
    "forge_base_cost": (1, 10000),
    "forge_max_plus": (0, 10),
    # v0.10 the ore-gated forge: ore per +1 step, and the guaranteed ore drop on
    # a won dungeon rung. (forge_ore_item is a cross-ref, ore_forest_chance is a
    # float — both validated below, outside this int-band loop.)
    "forge_ore_per_plus": (0, 10),
    "ore_dungeon_drop": (0, 20),
}

# Per-kind amount bands for the overworld event table (inclusive).
EVENT_AMOUNT_BANDS: dict[str, tuple[int, int]] = {
    "gold": (1, 500),
    "trap": (1, 500),
    "heal": (1, 100),
}
_EVENT_KINDS = frozenset({"fight", "gold", "heal", "trap", "lore"})

# The legal Watch CRT palettes a pack may choose via ``settings.watch_theme``.
# "phosphor" is the original green and the default; the Watch's JS THEME table
# (understone.watch) carries the matching CSS custom-property values for each.
# This is the loader band for watch_theme — an unknown name is a load error.
WATCH_THEMES = frozenset({"phosphor", "amber", "ice", "ember"})
DEFAULT_WATCH_THEME = "phosphor"

# Map-dimension band (inclusive). The floor keeps a map wide enough to frame a
# town; the ceiling caps the work a frame redraw and a row-decode must do on
# untrusted pack input.
MAP_DIM_MIN = 8
MAP_DIM_MAX = 256

# Upper bound on each content list, so an oversized (or generated-runaway) pack
# fails loudly at load rather than ballooning memory.
MAX_COUNTS: dict[str, int] = {
    "monsters": 500,
    "items": 500,
    "events": 500,
    "locations": 500,
    "zones": 500,
}

# Display names render inside frames, menus, and the Herald, so cap their width.
MAX_NAME_LEN = 48

# Box-drawing glyphs the frame and Herald renderers own; a map glyph must never
# be one of these (it would tear the borders) — the double bar is the Herald
# rule, the rest are the map/menu frame. '@' and '☻' are the player and
# other-player markers, so a map glyph must not impersonate an actor either.
_BOX_DRAWING_GLYPHS = frozenset("┌┐└┘─│═")
_ACTOR_GLYPHS = frozenset("@☻")
RESERVED_GLYPHS = _BOX_DRAWING_GLYPHS | _ACTOR_GLYPHS


def _check_glyph(glyph: str, where: str, *, role: str = "glyph") -> None:
    """Validate a single map glyph (terrain, location, or legend key).

    A glyph must render as exactly one terminal column (the grid contract in
    :mod:`understone.engine.textwidth`: one printable code point, no fullwidth
    runes, no combining marks) and must be neither a frame box-drawing line nor
    a player marker, so it cannot tear the rendered border or masquerade as an
    adventurer. *role* names the field for the author-facing message.
    """
    if len(glyph) != 1:
        raise WorldLoadError(f"{where} {role} must be a single character, got {glyph!r}")
    if not is_grid_safe(glyph):
        raise WorldLoadError(
            f"{where} {role} {glyph!r} must render exactly one column "
            "(no emoji, no fullwidth, no combining marks)"
        )
    if glyph in _BOX_DRAWING_GLYPHS:
        raise WorldLoadError(
            f"{where} {role} {glyph!r} is a box-drawing character reserved for frame borders"
        )
    if glyph in _ACTOR_GLYPHS:
        raise WorldLoadError(
            f"{where} {role} {glyph!r} is reserved for player markers ('@' you, '☻' others)"
        )


def _check_name(name: str, where: str, *, role: str = "name") -> None:
    """Validate a display name: printable and within :data:`MAX_NAME_LEN`."""
    if not name.isprintable():
        raise WorldLoadError(f"{where} {role} {name!r} must be printable")
    if len(name) > MAX_NAME_LEN:
        raise WorldLoadError(
            f"{where} {role} is {len(name)} characters; the limit is {MAX_NAME_LEN}"
        )


def _check_count(items: list[Any], name: str, where: str) -> None:
    """Reject a content list longer than its :data:`MAX_COUNTS` cap.

    *name* keys the cap (and is the logical content kind shown in the manual);
    *where* names the actual JSON source for the author-facing message, since
    locations and zones live inside ``world.json`` rather than their own file.
    """
    cap = MAX_COUNTS[name]
    if len(items) > cap:
        raise WorldLoadError(f"{where} defines {len(items)} {name}; the limit is {cap}")


def load_world(pack_dir: str | Path) -> World:
    """Load and validate the content pack at *pack_dir* into a ``World``."""
    root = Path(pack_dir)
    if not root.is_dir():
        raise WorldLoadError(f"content pack directory not found: {root}")

    terrain_defs = _load_terrain(root)
    monsters = _load_monsters(root)
    items = _load_items(root)
    location_kinds = _load_location_kinds(root)
    events = _load_events(root)
    return _load_map(root, terrain_defs, monsters, items, location_kinds, events)


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
        where = f"terrain.json[{key!r}]"
        _check_glyph(key, where, role="legend key")
        glyph = str(_require(spec, "glyph", where))
        _check_glyph(glyph, where)
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
    _check_count(raw, "monsters", "monsters.json")
    out: list[Monster] = []
    for i, spec in enumerate(raw):
        where = f"monsters.json[{i}]"
        name = str(_require(spec, "name", where))
        _check_name(name, where)
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
        weight = int(spec.get("weight", 10))
        if weight <= 0:
            raise WorldLoadError(f"{where} weight must be > 0, got {weight}")
        out.append(
            Monster(
                tier=int(_require(spec, "tier", where)),
                name=name,
                hp=hp,
                atk=atk,
                def_=def_,
                xp=xp,
                gold=gold,
                monster_id=str(spec.get("id", "")),
                boss=bool(spec.get("boss", False)),
                weight=weight,
                rare=bool(spec.get("rare", False)),
            )
        )
    if not out:
        raise WorldLoadError("monsters.json defines no monsters")
    bosses = [m for m in out if m.boss]
    if len(bosses) > 1:
        names = ", ".join(repr(m.name) for m in bosses)
        raise WorldLoadError(
            f'monsters.json flags {len(bosses)} monsters as "boss": true ({names}); '
            "a world has exactly one boss — the single endgame foe settings.boss_monster names"
        )
    return out


def _load_items(root: Path) -> list[Item]:
    raw = _read_json(root, "items.json")
    if not isinstance(raw, list):
        raise WorldLoadError("items.json must be a list of item objects")
    _check_count(raw, "items", "items.json")
    out: list[Item] = []
    for i, spec in enumerate(raw):
        where = f"items.json[{i}]"
        name = str(_require(spec, "name", where))
        _check_name(name, where)
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
                name=name,
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
        _check_name(str(_require(spec, "name", where)), where)
        _check_glyph(str(_require(spec, "glyph", where)), where)
        _require(spec, "actions", where)
    return raw


def _load_events(root: Path) -> list[WorldEvent]:
    """Parse and validate the weighted overworld event table.

    Requires at least one ``fight`` entry (else a walk could never spawn a
    monster), strictly-positive weights, ``min <= max`` within the per-kind
    amount band, and non-empty text on every non-fight entry. Each failure
    names the file, the row index, and the offending field.
    """
    raw = _read_json(root, "events.json")
    if not isinstance(raw, dict):
        raise WorldLoadError("events.json must be an object with an 'events' list")
    rows = _require(raw, "events", "events.json")
    if not isinstance(rows, list) or not rows:
        raise WorldLoadError("events.json 'events' must be a non-empty list of event objects")
    _check_count(rows, "events", "events.json")

    out: list[WorldEvent] = []
    has_fight = False
    for i, spec in enumerate(rows):
        where = f"events.json[{i}]"
        if not isinstance(spec, dict):
            raise WorldLoadError(f"{where} must be an object")
        kind = str(_require(spec, "kind", where))
        if kind not in _EVENT_KINDS:
            valid = ", ".join(sorted(_EVENT_KINDS))
            raise WorldLoadError(f"{where} kind {kind!r} is not one of: {valid}")
        weight = int(_require(spec, "weight", where))
        if weight <= 0:
            raise WorldLoadError(f"{where} weight must be > 0, got {weight}")
        lo, hi = _decode_event_amount(spec, kind, where)
        text = str(spec.get("text", ""))
        if kind != "fight" and not text.strip():
            raise WorldLoadError(f"{where} kind {kind!r} requires non-empty 'text'")
        if kind == "fight":
            has_fight = True
        out.append(WorldEvent(kind=kind, weight=weight, text=text, lo=lo, hi=hi))

    if not has_fight:
        raise WorldLoadError("events.json must contain at least one 'fight' entry")
    return out


def _decode_event_amount(spec: dict[str, Any], kind: str, where: str) -> tuple[int, int]:
    """Return the ``(lo, hi)`` amount band for an event row, validated.

    Value-bearing kinds (gold/heal/trap) must declare ``min``/``max`` within
    the per-kind band with ``min <= max``; fight/lore carry no amount.
    """
    band = EVENT_AMOUNT_BANDS.get(kind)
    if band is None:
        return 0, 0
    band_lo, band_hi = band
    lo = int(_require(spec, "min", where))
    hi = int(_require(spec, "max", where))
    if lo > hi:
        raise WorldLoadError(f"{where} min {lo} exceeds max {hi}")
    if lo < band_lo or hi > band_hi:
        raise WorldLoadError(
            f"{where} {kind} amount {lo}..{hi} is out of band ({band_lo}..{band_hi})"
        )
    return lo, hi


def _load_map(
    root: Path,
    terrain_defs: dict[str, TerrainDef],
    monsters: list[Monster],
    items: list[Item],
    location_kinds: dict[str, dict[str, Any]],
    events: list[WorldEvent],
) -> World:
    raw = _read_json(root, "world.json")
    if not isinstance(raw, dict):
        raise WorldLoadError("world.json must be an object")

    name = str(_require(raw, "name", "world.json"))
    width = int(_require(raw, "width", "world.json"))
    height = int(_require(raw, "height", "world.json"))
    for label, dim in (("width", width), ("height", height)):
        if not MAP_DIM_MIN <= dim <= MAP_DIM_MAX:
            raise WorldLoadError(
                f"world.json {label} = {dim} is out of band ({MAP_DIM_MIN}..{MAP_DIM_MAX})"
            )

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
        events=events,
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
    _check_count(placements, "locations", "world.json locations")
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
    _check_count(zones_raw, "zones", "world.json zones")
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
    for field_name, (lo, hi) in SETTINGS_BANDS.items():
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
    boss_monster = _decode_boss_monster(spec, monsters)
    rare_drop_item = _decode_rare_drop_item(spec, items)
    forge_ore_item = _decode_forge_ore_item(spec, items)
    ore_forest_chance = _decode_ore_forest_chance(spec)
    watch_theme = _decode_watch_theme(spec)

    return Settings(
        daily_turns=values["daily_turns"],
        rest_cost=values["rest_cost"],
        heal_cost_per_hp=values["heal_cost_per_hp"],
        starting_gold=values["starting_gold"],
        starting_weapon=starting_weapon,
        starting_armor=starting_armor,
        start_hp=values["start_hp"],
        start_atk=values["start_atk"],
        start_def=values["start_def"],
        xp_base=values["xp_base"],
        growth_max_hp=growth_max_hp,
        growth_atk=growth_atk,
        growth_def=growth_def,
        bestow_daily_budget=values["bestow_daily_budget"],
        dungeon_tiers=dungeon_tiers,
        boss_monster=boss_monster,
        wyrm_min_level=values["wyrm_min_level"],
        ambush_min_level=values["ambush_min_level"],
        ambush_level_band=values["ambush_level_band"],
        ambush_gold_pct=values["ambush_gold_pct"],
        post_daily_cap=values["post_daily_cap"],
        gamble_max_bet=values["gamble_max_bet"],
        gamble_daily_cap=values["gamble_daily_cap"],
        satchel_max=values["satchel_max"],
        forge_base_cost=values["forge_base_cost"],
        forge_max_plus=values["forge_max_plus"],
        rare_drop_item=rare_drop_item,
        forge_ore_item=forge_ore_item,
        forge_ore_per_plus=values["forge_ore_per_plus"],
        ore_dungeon_drop=values["ore_dungeon_drop"],
        ore_forest_chance=ore_forest_chance,
        watch_theme=watch_theme,
    )


def _decode_boss_monster(spec: dict[str, Any], monsters: list[Monster]) -> str:
    """Resolve and validate the endgame boss monster id.

    The id must name a monster in the pack, and that monster must carry the
    ``boss`` flag (so the endgame foe is never a stray random encounter).
    """
    boss_id = str(_require(spec, "boss_monster", "world.json settings"))
    by_id = {m.monster_id: m for m in monsters if m.monster_id}
    monster = by_id.get(boss_id)
    if monster is None:
        raise WorldLoadError(
            f"world.json settings.boss_monster = {boss_id!r} is not a known monster id"
        )
    if not monster.boss:
        raise WorldLoadError(
            f'world.json settings.boss_monster = {boss_id!r} must be flagged "boss": true'
        )
    return boss_id


def _decode_rare_drop_item(spec: dict[str, Any], items: list[Item]) -> str:
    """Resolve and validate the item a rare beast drops on its kill.

    The id must name an item in the pack AND that item must be a consumable
    (it goes straight into the satchel to be quaffed later, so a weapon or
    armour id would be incoherent). Mirrors the ``starting_weapon`` check but
    adds the slot constraint.
    """
    drop_id = str(_require(spec, "rare_drop_item", "world.json settings"))
    by_id = {it.item_id: it for it in items}
    item = by_id.get(drop_id)
    if item is None:
        raise WorldLoadError(
            f"world.json settings.rare_drop_item = {drop_id!r} is not a known item id"
        )
    if item.slot is not Slot.CONSUMABLE:
        raise WorldLoadError(
            f"world.json settings.rare_drop_item = {drop_id!r} must be a consumable item, "
            f"not {item.slot.value!r}"
        )
    return drop_id


def _decode_forge_ore_item(spec: dict[str, Any], items: list[Item]) -> str:
    """Resolve and validate the world's forge ore — the material the forge spends.

    The id must name an item in the pack AND that item must be a ``material``
    (it is carried in the satchel and spent at the forge, never equipped or
    quaffed, so a weapon/armour/consumable id would be incoherent). Mirrors the
    ``rare_drop_item`` check but pins the slot to :attr:`Slot.MATERIAL`.
    """
    ore_id = str(_require(spec, "forge_ore_item", "world.json settings"))
    by_id = {it.item_id: it for it in items}
    item = by_id.get(ore_id)
    if item is None:
        raise WorldLoadError(
            f"world.json settings.forge_ore_item = {ore_id!r} is not a known item id"
        )
    if item.slot is not Slot.MATERIAL:
        raise WorldLoadError(
            f"world.json settings.forge_ore_item = {ore_id!r} must be a material item, "
            f"not {item.slot.value!r}"
        )
    return ore_id


def _decode_ore_forest_chance(spec: dict[str, Any]) -> float:
    """Resolve and validate the per-win forest ore chance (a 0.0..1.0 float).

    The chance a won forest fight yields one ore. A float, so it is validated
    here rather than through the integer :data:`SETTINGS_BANDS` loop, mirroring
    the ``encounter_rate`` probability check in terrain.
    """
    chance = float(_require(spec, "ore_forest_chance", "world.json settings"))
    if not 0.0 <= chance <= 1.0:
        raise WorldLoadError(
            f"world.json settings.ore_forest_chance = {chance} is out of band (0.0..1.0)"
        )
    return chance


def _decode_watch_theme(spec: dict[str, Any]) -> str:
    """Resolve and validate the Watch CRT palette name (optional, defaulted).

    ``watch_theme`` is OPTIONAL: a pack that omits it keeps the original
    :data:`DEFAULT_WATCH_THEME` ("phosphor"), so no author is forced to set it
    and an existing pack is unchanged. When present it must name one of
    :data:`WATCH_THEMES`; an unknown palette is a load error naming the legal
    set, since the Watch's JS would have no variables to apply for it.
    """
    raw = spec.get("watch_theme", DEFAULT_WATCH_THEME)
    theme = str(raw)
    if theme not in WATCH_THEMES:
        legal = ", ".join(sorted(WATCH_THEMES))
        raise WorldLoadError(
            f"world.json settings.watch_theme = {theme!r} is not a known theme; "
            f"choose one of: {legal}"
        )
    return theme


def _decode_dungeon_tiers(spec: dict[str, Any], monsters: list[Monster]) -> tuple[int, ...]:
    """Parse the ordered dungeon-gauntlet tier ladder, one foe per tier.

    Each tier must be backed by at least one NON-BOSS monster in the pack, or
    the gauntlet would silently skip that rung: the gauntlet draws from
    ``monsters_for_tier_band``, which excludes boss monsters, so a boss-only
    tier loads cleanly yet has no fightable foe at runtime.

    Each tier's *first* non-boss monster in file order is its fixed rung
    guardian (``monsters_for_tier_band(t, t)[0]``, the engine's deterministic
    pick), so that monster must NOT be ``rare``: a rare in the lead slot of a
    dungeon tier would be promoted to a fixed, repeatable guardian and pulled
    out of the weighted rare pool entirely. The rule is checked here, where the
    tier ladder and the monster order meet.
    """
    raw_tiers = _require(spec, "dungeon_tiers", "world.json settings")
    if not (isinstance(raw_tiers, list) and raw_tiers):
        raise WorldLoadError("world.json settings.dungeon_tiers must be a non-empty list of tiers")
    available = {m.tier for m in monsters if not m.boss}
    tiers: list[int] = []
    for i, value in enumerate(raw_tiers):
        tier = int(value)
        if tier not in available:
            raise WorldLoadError(
                f"world.json settings.dungeon_tiers[{i}] = {tier} has no non-boss monster "
                "in the pack (the dungeon gauntlet excludes the boss, so a boss-only tier "
                "leaves the rung unfillable)"
            )
        guardian = next(m for m in monsters if m.tier == tier and not m.boss)
        if guardian.rare:
            raise WorldLoadError(
                f"monsters.json: {guardian.name!r} is rare but is the first tier-{tier} "
                f"monster, so it would become the fixed guardian of dungeon rung tier {tier} "
                "— put a non-rare monster first in that tier"
            )
        tiers.append(tier)
    return tuple(tiers)
