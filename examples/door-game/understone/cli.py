"""The pack-authoring command surface — validate a pack and scaffold a new one.

This module is deliberately pure: it imports only the loader and the standard
library, takes no part in argument parsing (``server.main`` owns the argparse
front end), and writes to the streams it is handed. That keeps the authoring
loop — ``newpack`` then ``validate`` — testable as plain function calls.

Two entry points back the two verbs:

* :func:`cli_validate` loads a pack and, on success, prints a human-readable
  report; on failure it prints the loader's author-facing message and returns
  a non-zero code. This is the feedback half of the loop.
* :func:`cli_newpack` scaffolds a new pack: it copies the bundled world as a
  starting template and writes an ``AUTHORING.md`` manual whose bands table is
  generated from the loader's own band data, so the documented limits can
  never drift from the enforced ones.
"""

from __future__ import annotations

import shutil
import sys
from typing import TYPE_CHECKING, TextIO

from understone.errors import WorldLoadError
from understone.world import PACKAGED_WORLD_DIR, loader

if TYPE_CHECKING:
    from pathlib import Path

    from understone.engine.world import World

# The six packaged content files copied verbatim as a new pack's template.
_PACK_FILES = (
    "terrain.json",
    "monsters.json",
    "items.json",
    "locations.json",
    "events.json",
    "world.json",
)


def cli_validate(pack_dir: Path, out: TextIO | None = None, err: TextIO | None = None) -> int:
    """Load *pack_dir* and report; return 0 if sound, 2 if it fails to load.

    On success a pack report is written to *out* and the function returns 0.
    On any :class:`WorldLoadError` the loader's message — which names the
    file, index, and field at fault — is written to *err* and the function
    returns 2. The author iterates against that message until the pack loads.

    *out*/*err* default to the live ``sys.stdout``/``sys.stderr`` resolved at
    call time, so a caller (or pytest's capture) may redirect them.
    """
    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr
    try:
        world = loader.load_world(pack_dir)
    except WorldLoadError as exc:
        print(f"The pack is flawed: {exc}", file=err)
        return 2
    print(_pack_report(world), file=out)
    return 0


def cli_newpack(dest: Path, out: TextIO | None = None, err: TextIO | None = None) -> int:
    """Scaffold a new content pack at *dest*; return 0, or 2 if *dest* is taken.

    Refuses to write into an existing non-empty directory (so an author never
    clobbers work in progress). Otherwise it creates *dest*, copies the six
    packaged content files as a starting template, and writes an
    ``AUTHORING.md`` manual generated from the live loader bands. The author
    then edits or regenerates the JSON and runs ``validate``.

    *out*/*err* default to the live ``sys.stdout``/``sys.stderr`` resolved at
    call time, so a caller (or pytest's capture) may redirect them.
    """
    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr
    if dest.exists() and dest.is_dir() and any(dest.iterdir()):
        print(f"refusing to scaffold into non-empty directory: {dest}", file=err)
        return 2
    if dest.exists() and not dest.is_dir():
        print(f"refusing to scaffold over a file: {dest}", file=err)
        return 2

    dest.mkdir(parents=True, exist_ok=True)
    for name in _PACK_FILES:
        shutil.copyfile(PACKAGED_WORLD_DIR / name, dest / name)
    (dest / "AUTHORING.md").write_text(build_authoring_md(), encoding="utf-8")

    print(f"Scaffolded a new pack at {dest}.", file=out)
    print("Six content files plus AUTHORING.md are in place; the template is the", file=out)
    print("shipped Vale of Understone, ready to edit or regenerate.", file=out)
    print(f"Next: edit or regenerate the JSON, then: understone validate {dest}", file=out)
    return 0


def _pack_report(world: World) -> str:
    """Render the success report for a loaded *world*.

    Counts and shares are computed from the runtime world so the figures match
    what the engine will actually run, not what the JSON nominally declares.
    """
    settings = world.settings
    boss_count = sum(1 for m in world.monsters if m.boss)
    fight_share = _fight_share_pct(world)

    lines = [
        f"{world.name} — {world.width}x{world.height}",
        f"  monsters : {len(world.monsters)} ({boss_count} boss)",
        f"  items    : {len(world.items)}",
        f"  zones    : {len(world.zones)}",
        f"  events   : {len(world.events)} ({fight_share}% fight by weight)",
        (
            "  settings : "
            f"{settings.daily_turns} turns/day, "
            f"bestow budget {settings.bestow_daily_budget}, "
            f"Wyrm gate level {settings.wyrm_min_level}"
        ),
        "",
        "This pack is sound. The door stands open.",
    ]
    return "\n".join(lines)


def _fight_share_pct(world: World) -> int:
    """Return the share of overworld encounter weight that is a ``fight``.

    Reported by weight, not row count, because weight is the draw probability
    the engine actually rolls against — it is the number an author tunes to hit
    the ~55% fight feel.
    """
    total = sum(e.weight for e in world.events)
    if total == 0:
        return 0
    fight = sum(e.weight for e in world.events if e.kind == "fight")
    return round(100 * fight / total)


def build_authoring_md() -> str:
    """Build the AUTHORING.md manual, bands table included.

    The bands section is generated by iterating the loader's own band tables
    (the public constants on :mod:`understone.world.loader`), so the documented
    limits are the enforced limits by construction and cannot silently drift.
    """
    return _AUTHORING_TEMPLATE.replace("{{BANDS}}", _render_bands())


def _render_bands() -> str:
    """Render the bands reference straight from the loader's band data."""
    parts: list[str] = []

    parts.append("### Map and counts\n")
    parts.append(
        f"* Map width and height: each `{loader.MAP_DIM_MIN}`..`{loader.MAP_DIM_MAX}` cells."
    )
    # monsters/items/events are their own files; locations and zones are lists
    # inside world.json, so name each cap's real source.
    count_source = {
        "monsters": "`monsters.json`",
        "items": "`items.json`",
        "events": "`events.json`",
        "locations": "`world.json` → `locations`",
        "zones": "`world.json` → `zones`",
    }
    for name, cap in loader.MAX_COUNTS.items():
        parts.append(f"* {count_source[name]}: at most `{cap}` entries.")
    parts.append(
        f"* Display names (monster, item, location): at most "
        f"`{loader.MAX_NAME_LEN}` printable characters."
    )
    parts.append(
        "* Map glyphs (terrain, location, legend keys): exactly one printable "
        "character, and never one of "
        + ", ".join(f"`{g}`" for g in _reserved_glyph_list())
        + " (the frame box-drawing lines and the `@`/`&` player markers)."
    )
    parts.append("")

    parts.append("### Economy and progression settings (`world.json` → `settings`)\n")
    parts.append("| field | allowed range |")
    parts.append("| --- | --- |")
    for field_name, (lo, hi) in loader.SETTINGS_BANDS.items():
        rng = f"{lo}..{hi}" if hi is not None else f"{lo} or more"
        parts.append(f"| `{field_name}` | `{rng}` |")
    parts.append("")

    parts.append("### Overworld event amounts (`events.json`, per kind)\n")
    parts.append("| kind | min..max amount |")
    parts.append("| --- | --- |")
    for kind, (lo, hi) in loader.EVENT_AMOUNT_BANDS.items():
        parts.append(f"| `{kind}` | `{lo}..{hi}` |")
    parts.append(
        "\n(`fight` and `lore` carry no amount; `fight` draws its foe from the "
        "zone tier band, `lore` is pure flavour text.)"
    )

    return "\n".join(parts)


def _reserved_glyph_list() -> list[str]:
    """Return the reserved glyphs in a stable, readable order for the manual."""
    box = [g for g in "┌┐└┘─│═" if g in loader.RESERVED_GLYPHS]
    actors = [g for g in "@&" if g in loader.RESERVED_GLYPHS]
    return box + actors


_AUTHORING_TEMPLATE = """\
# Authoring a world pack for Understone

A *world pack* is a directory of six JSON files that the server loads at start
to become the entire game world — its map, its monsters, its economy, its
endgame. There is no code to write: you describe a world as data, the loader
validates it hard, and the server runs it. This file is the manual; you can
follow it cold, by hand or with an LLM.

The loop is short:

1. `understone newpack mypack` — scaffold this template (you are reading the
   copy it wrote into `mypack/AUTHORING.md`).
2. Edit or regenerate the JSON files to describe your world.
3. `understone validate mypack` — the loader checks the pack and either prints
   a report ending **"This pack is sound. The door stands open."** or tells you
   exactly which file, row, and field is wrong.
4. Repeat step 2 until it is sound, then serve it:
   `UNDERSTONE_WORLD=mypack understone`.

The loader's error messages are written FOR you: every failure names the file,
the index, and the field, and says what was expected. Treat them as the
feedback loop — iterate until the report says the door stands open.

---

## The six files and how they fit together

| file | shape | holds |
| --- | --- | --- |
| `terrain.json` | object keyed by legend char | terrain kinds: glyph, walkability, encounter rate |
| `monsters.json` | list | monster stat blocks, tiered; one flagged the boss |
| `items.json` | list | weapons, armour, consumables for the shop |
| `locations.json` | object keyed by location key | building kinds: name, glyph, menu actions, flavour |
| `events.json` | object with an `events` list | the weighted overworld encounter table |
| `world.json` | object | the map, placements, zones, and `settings` that bind it all |

The cross-references the loader enforces:

* every character in `world.json` → `legend` must name a terrain `key` from
  `terrain.json`; every character in `terrain_rows` must be in that legend;
* every placement in `world.json` → `locations` must name a key defined in
  `locations.json`, and must sit on walkable terrain;
* `settings.starting_weapon` / `starting_armor` must be ids from
  `items.json`; `settings.boss_monster` must be an id from `monsters.json`
  that is flagged `"boss": true`;
* every tier in `settings.dungeon_tiers` must be backed by a NON-boss monster;
* every zone's tier band must overlap at least one monster tier.

---

## File-by-file schema

### `terrain.json`

An object whose keys are the single-character legend symbols used in the map.

```json
{
  ".": {"key": "grass", "glyph": ".", "walkable": true, "encounter_rate": 0.1, "color": "floor"}
}
```

* `key` — internal name the map legend resolves to.
* `glyph` — the single character drawn on the map (see glyph rules below).
* `walkable` — may a player stand here.
* `encounter_rate` — `0.0`..`1.0`, the per-step chance a walk rolls the event
  table on this terrain.
* `color` — a palette role string (`floor`, `tree`, `water`, `wall`, ...).

### `monsters.json`

A list of stat blocks. `tier` groups foes by difficulty; zones and the dungeon
gauntlet draw from tiers. Exactly one monster should be the boss.

```json
{"tier": 2, "name": "Goblin", "hp": 12, "atk": 5, "def": 1, "xp": 18, "gold": 7}
```

The boss adds an `id` and `"boss": true`, and is referenced by
`settings.boss_monster`:

```json
{"tier": 6, "name": "the Wyrm Below", "hp": 120, "atk": 24, "def": 8,
 "xp": 400, "gold": 250, "boss": true, "id": "wyrm_below"}
```

### `items.json`

A list of equipment and consumables. `slot` is `weapon`, `armor`, or
`consumable`. Weapons add `atk`, armour adds `def`, consumables `heal`.

```json
{"id": "short_sword", "name": "Short Sword", "slot": "weapon", "atk": 5, "price": 40}
```

### `locations.json`

An object keyed by location key. Each entry is a building kind with a menu of
`actions` the player may take inside it.

```json
{
  "inn": {"kind": "inn", "name": "The Sleeping Drake", "glyph": "I",
          "color": "town", "actions": ["rest", "leave"],
          "flavor": ["Lamplight pools on worn oak tables."]}
}
```

The verbs the engine understands are `rest` (inn), `buy`/`sell` (shop), `heal`
(healer), `descend`/`challenge` (dungeon), and `leave`. Give each building the
menu that matches its role.

### `events.json`

An object with an `events` list — the weighted overworld encounter table the
server rolls as a player walks.

```json
{"events": [
  {"kind": "fight", "weight": 82, "text": "Something snarls out of the brush."},
  {"kind": "gold", "weight": 8, "text": "a rotted coin-purse", "min": 4, "max": 12}
]}
```

* `kind` — `fight`, `gold`, `heal`, `trap`, or `lore`.
* `weight` — relative draw weight (`> 0`).
* `text` — required (non-empty) for every kind except `fight`.
* `min`/`max` — required for the value-bearing kinds (`gold`, `heal`, `trap`).

There MUST be at least one `fight` row, or a walk could never find a monster.

### `world.json`

The binding file: `name`, `width`, `height`, `spawn` `[x, y]`, a `legend`
mapping characters to terrain keys, `terrain_rows` (one string per row, each
exactly `width` long), a `locations` list of `{"key", "x", "y"}` placements,
a `zones` list (rectangles that bias monster tiers), and a `settings` object.

```json
{"key": "forest_near", "rect": [30, 18, 60, 36], "tier_lo": 1, "tier_hi": 2}
```

---

## The bands — the limits the loader enforces

These are generated from the loader's own tables, so they are exactly what
`validate` checks. A value outside its band is a load error.

{{BANDS}}

---

## Design guidance

**Turn economy.** `daily_turns` is the whole pacing lever: only fighting,
descending, and challenging the Wyrm spend a turn (moving, resting, shopping
are free). A small budget (the Vale uses 10) makes this a correspondence game
played a little each day. Set `rest_cost`, `heal_cost_per_hp`, and shop prices
so a day's gold roughly covers a day's recovery — too cheap and there is no
tension, too dear and a hero stalls.

**Tier curve.** Lay monster tiers as a rising staircase: each tier should be a
real step up in `hp`/`atk` and a real step up in `xp`/`gold`, so the reward of
pushing into a harder zone pays for the risk. Keep two or three foes per tier
for variety. The boss should tower over the top random tier — it is the climax.

**Encounter feel.** Aim for roughly 55% of overworld encounter WEIGHT on
`fight` rows; the rest is the texture of travel — small gold finds, healing
springs, harmless traps, and lore that hints at the endgame. (The validate
report prints your actual fight share so you can tune it.)

**Glyphs.** Map glyphs must be exactly one printable character and must never
collide with the frame's box-drawing lines or the `@`/`&` player markers (see
the bands above). Pick glyphs that read at a glance: `.` open ground, `~`
water, building letters like `I`/`$`/`+`/`>`.

**Boss rules.** Exactly one monster carries `"boss": true` and an `id`, and
`settings.boss_monster` points at it. The boss is the only win condition and is
faced only through the `challenge` verb, gated by `settings.wyrm_min_level`. A
boss tier must NOT appear in `settings.dungeon_tiers`: the gauntlet excludes
boss monsters, so a boss-only rung would be unfillable — back every dungeon
tier with at least one ordinary monster.

**Location menus.** Give each building only the actions it can honour. An inn
that offers `buy` but no shop logic will confuse the narrator; match the menu
to the building's role.

---

## The validate loop

Run `understone validate mypack` after every change. On success you get a
report — name, size, monster/item/zone/event counts, fight share, and the key
settings — ending in **"This pack is sound. The door stands open."** On
failure you get one precise line naming the file, the row, and the field.

The error messages are deliberately instructive: they are the authoring API.
Keep editing and re-validating until the door stands open, then point the
server at your pack with `UNDERSTONE_WORLD=mypack`.
"""
