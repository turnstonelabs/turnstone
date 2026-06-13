"""The pack-authoring command surface — validate a pack and scaffold a new one.

This module is deliberately pure: it imports only the loader and the standard
library, takes no part in argument parsing (``server.main`` owns the argparse
front end), and writes to the streams it is handed. That keeps the authoring
loop — ``newpack`` then ``validate`` — testable as plain function calls.

Three entry points back the three verbs:

* :func:`cli_validate` loads a pack and, on success, prints a human-readable
  report; on failure it prints the loader's author-facing message and returns
  a non-zero code. This is the feedback half of the loop.
* :func:`cli_newpack` scaffolds a new pack: it copies the bundled world as a
  starting template and writes an ``AUTHORING.md`` manual whose bands table is
  generated from the loader's own band data, so the documented limits can
  never drift from the enforced ones.
* :func:`cli_worlds` lists the bundled worlds — the default Vale plus every
  alternate pack shipped under ``world/packs/`` — loading each so it can report
  whether it is sound or flawed, the discovery seam for "worlds without authors".
"""

from __future__ import annotations

import shutil
import sys
from typing import TYPE_CHECKING, TextIO

from understone.engine.textwidth import SAFE_PALETTE
from understone.errors import WorldLoadError
from understone.world import PACKAGED_WORLD_DIR, bundled_world_dirs, loader

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


def cli_worlds(out: TextIO | None = None, err: TextIO | None = None) -> int:
    """List every bundled world, reporting each as sound or flawed; return 0.

    Discovers the worlds through :func:`~understone.world.bundled_world_dirs`
    (the default Vale first, then the alternate packs alphabetically) and loads
    each one. Each world is one line — its slug, name, ``WxH``, and either
    ``sound`` or ``flawed: <short reason>`` — so a shipped pack that has gone
    out of band is visible at a glance rather than only failing at serve time.
    A flawed world is reported, not fatal: the listing always returns 0 and
    always ends with the hint for serving an alternate. *err* is accepted for a
    uniform signature with the other verbs; the listing writes only to *out*.
    """
    out = out if out is not None else sys.stdout
    for slug, world_dir in bundled_world_dirs():
        print(_world_line(slug, world_dir), file=out)
    print("", file=out)
    print(
        "Serve one with UNDERSTONE_WORLD=<path> (or the default Vale needs no setting).",
        file=out,
    )
    return 0


def _world_line(slug: str, world_dir: Path) -> str:
    """Render one ``worlds`` listing line for the world at *world_dir*.

    Loads the world to report its real name, dimensions, and soundness. A pack
    that fails to load is summarised as ``flawed: <reason>`` using the loader's
    own author-facing message (truncated to keep the listing to one line per
    world), never raised — the listing surveys every bundled world even when one
    is broken.
    """
    try:
        world = loader.load_world(world_dir)
    except WorldLoadError as exc:
        return f"  {slug:<10} flawed: {_short_reason(str(exc))}"
    return f"  {slug:<10} {world.name} — {world.width}x{world.height} — sound"


# How much of a loader error message the one-line ``worlds`` summary keeps.
_FLAW_REASON_MAX = 70


def _short_reason(message: str) -> str:
    """Trim a loader error to a single readable clause for the worlds listing."""
    flattened = " ".join(message.split())
    if len(flattened) <= _FLAW_REASON_MAX:
        return flattened
    return flattened[: _FLAW_REASON_MAX - 1].rstrip() + "…"


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
    """Build the AUTHORING.md manual, bands table and glyph palette included.

    Both the bands section and the safe-glyph palette are generated from live
    source — the loader's own band tables and ``textwidth.SAFE_PALETTE`` — so
    the documented limits and the suggested glyphs are exactly what the loader
    enforces and admits, and cannot silently drift from it.
    """
    md = _AUTHORING_TEMPLATE.replace("{{BANDS}}", _render_bands())
    md = md.replace("{{PALETTE}}", _render_palette())
    md = md.replace("{{COLOR_ROLES}}", _render_color_roles())
    return md.replace("{{VALIDATE_COVERAGE}}", _render_validate_coverage())


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
        "* Map glyphs (terrain, location, legend keys): exactly one terminal "
        "column (one printable code point, no fullwidth runes, no combining "
        "marks — see the width rule above), and never one of "
        + ", ".join(f"`{g}`" for g in _reserved_glyph_list())
        + " (the frame box-drawing lines and the `@`/`☻` player markers)."
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
        "zone tier band, `lore` is pure flavour text.)\n"
    )

    parts.append("### Watch theme (`world.json` → `settings.watch_theme`)\n")
    legal = ", ".join(f"`{name}`" for name in sorted(loader.WATCH_THEMES))
    parts.append(
        f"OPTIONAL. The CRT palette the live Watch page paints your world in, "
        f"one of: {legal}. It defaults to `{loader.DEFAULT_WATCH_THEME}` (the "
        f"original green phosphor), so you may leave it out entirely — a pack "
        f"that omits it looks exactly as the bundled Vale always has. Set it to "
        f"give your world its own colour: `amber` is a warm gold monitor, `ice` "
        f"a cold pale blue, `ember` a hot red/orange. An unknown name is a load "
        f"error naming the legal set."
    )

    parts.append("\n### The ore-gated forge (`world.json` → `settings`)\n")
    ore_per = loader.SETTINGS_BANDS["forge_ore_per_plus"]
    dungeon = loader.SETTINGS_BANDS["ore_dungeon_drop"]
    parts.append(
        "Forging a +1 edge now costs both GOLD and ORE — a `material` item the "
        "hero earns in combat, never buys. Four settings bind it:"
    )
    parts.append(
        "* `forge_ore_item` — REQUIRED. The item id of your world's forge ore; "
        "it must name an `items.json` entry whose `slot` is `material` (an "
        "unknown id or a non-material slot is a load error). The Vale uses "
        "`iron_ore`."
    )
    parts.append(
        f"* `forge_ore_per_plus` — band `{ore_per[0]}..{ore_per[1]}`. Ore per +1 "
        f"step: a +N forge costs `(current_plus + 1) * forge_ore_per_plus` ore. "
        f"{_forge_ore_worked_example()}"
    )
    parts.append(
        f"* `ore_dungeon_drop` — band `{dungeon[0]}..{dungeon[1]}`. Ore granted "
        f"on every WON dungeon rung — the reliable source. The Vale drops 2."
    )
    parts.append(
        "* `ore_forest_chance` — a `0.0`..`1.0` probability (a float, validated "
        "outside the integer band table). The chance a WON forest fight yields "
        "one ore — the occasional bonus source. The Vale uses `0.2`."
    )
    parts.append(
        "\nOre rides the satchel as a stack, so it shares the `satchel_max` "
        "DISTINCT-stack budget with potions (per-stack quantity is unbounded). "
        "Tune the two sources so a hero who descends steadily earns enough ore "
        "to forge without grinding — the `simulate` bot will tell you if the "
        "gate stalls a winnable run."
    )

    return "\n".join(parts)


def _forge_ore_worked_example() -> str:
    """Render the per-step ore costs from the bundled Vale's live forge settings.

    The starter template :func:`cli_newpack` copies IS the bundled Vale, so the
    worked figures are computed from its actual ``forge_ore_per_plus`` and
    ``forge_max_plus`` rather than hardcoded — a retune of the template moves
    the manual with it. The steps are ``per_plus * (i + 1)`` for each ``i`` in
    ``range(forge_max_plus)``; the total is what it costs to max one slot.
    """
    settings = loader.load_world(PACKAGED_WORLD_DIR).settings
    per_plus = settings.forge_ore_per_plus
    max_plus = settings.forge_max_plus
    steps = [per_plus * (i + 1) for i in range(max_plus)]
    if not steps:
        return (
            f"At the template's value of {per_plus}, slots cannot be forged (`forge_max_plus` 0)."
        )
    ladder = ", ".join(str(cost) for cost in steps)
    total = sum(steps)
    return (
        f"At the template's value of {per_plus}, the steps cost {ladder} ore "
        f"({total} ore to max a slot at `forge_max_plus` {max_plus})."
    )


def _reserved_glyph_list() -> list[str]:
    """Return the reserved glyphs in a stable, readable order for the manual."""
    box = [g for g in "┌┐└┘─│═" if g in loader.RESERVED_GLYPHS]
    actors = [g for g in "@☻" if g in loader.RESERVED_GLYPHS]
    return box + actors


def _render_palette() -> str:
    """Render the safe-glyph appendix straight from ``textwidth.SAFE_PALETTE``.

    The glyphs are emitted in their declared order, wrapped in backticks so the
    monospace renders them as discrete cells. Generated from the live constant,
    so the suggested palette is exactly the set the loader's width gate admits.
    """
    glyphs = " ".join(f"`{g}`" for g in SAFE_PALETTE)
    return (
        "Any single-column glyph the loader accepts is fair game, but these "
        "carry the period BBS / CP437 flavour and are all guaranteed safe:\n\n"
        f"{glyphs}"
    )


def _render_color_roles() -> str:
    """Render the author-assignable colour roles, generated from the Color enum.

    The Watch knows how to paint exactly the roles in ``screen.palette.Color``;
    ``Color.assignable()`` is the single source for which of those an author may
    put on terrain or a location (the runtime overlay roles an actor/item wears,
    and the DEFAULT fallback, are filtered out there). Generated from the enum,
    so the documented vocabulary can never drift from what the Watch can
    actually colour — the same can't-drift discipline as the bands and the
    safe-glyph palette. ``color`` itself stays advisory: the loader does not
    validate it, so a typo is harmless and an unknown role just paints as the
    default; these are simply the roles the Watch recognises.
    """
    from understone.screen.palette import Color

    return ", ".join(f"`{role.value}`" for role in Color.assignable())


def _render_validate_coverage() -> str:
    """Render the list of rules the loader actually enforces, generated from it.

    The figures that can drift (the number of banded settings, the name-length
    cap, the reserved glyphs) are read from the live loader so the list cannot
    fall out of step with what `validate` does; the prose names each family of
    check. This is the machine-enforced half of the honesty split in the manual
    — the eyeball-only half is hand-written below it, because "is the fiction
    any good" is exactly what the loader can never see.
    """
    settings_count = len(loader.SETTINGS_BANDS)
    reserved = ", ".join(f"`{g}`" for g in _reserved_glyph_list())
    bullets = [
        f"* **Economy and progression bands** — every one of the {settings_count} "
        "`settings` fields must sit in its allowed range (the table above), and "
        "`growth` must be present and non-negative.",
        "* **Glyph safety** — every terrain, location, and legend glyph must render "
        f"exactly one column and must not be a reserved marker ({reserved}).",
        "* **Map integrity** — `width`/`height` in band, every `terrain_rows` row "
        "exactly `width` long with `height` rows, and every row character in the "
        "`legend`.",
        "* **Walkability** — `spawn` and every placed location must sit on walkable "
        "terrain (and no two locations share a cell).",
        f"* **Display-name length** — every monster, item, and location name within "
        f"`{loader.MAX_NAME_LEN}` printable characters; content lists within their caps.",
        "* **The fight row** — `events.json` must hold at least one `fight` entry, "
        "with weights `> 0`, `min <= max`, and amounts in their per-kind band.",
        "* **Cross-references** — `legend` → terrain key, location placements → "
        "`locations.json` keys, `starting_weapon`/`starting_armor` → item ids, "
        '`boss_monster` → a monster flagged `"boss": true`, '
        "`rare_drop_item` → a consumable item id, and `forge_ore_item` → a "
        "`material` item id.",
        "* **Zone tiers** — every zone's tier band must overlap at least one monster tier.",
        "* **Dungeon ladder** — every `dungeon_tiers` tier must have a non-boss "
        "monster, and that tier's FIRST monster (its fixed rung guardian) must "
        "not be `rare`.",
        '* **Exactly one boss** — at most one monster may carry `"boss": true`.',
    ]
    return "\n".join(bullets)


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
  that is flagged `"boss": true`; `settings.rare_drop_item` must be an id from
  `items.json` whose `slot` is `consumable`; `settings.forge_ore_item` must be
  an id from `items.json` whose `slot` is `material`;
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
* `color` — a palette role string. It is **advisory and not validated**: the
  loader stores it but the text frame draws glyphs only (it is monochrome), so
  any string loads and an unrecognised role simply maps to the default at render
  time. Where colour DOES show is the live Watch page, which paints each role a
  distinct hue. The roles the Watch knows how to paint — pick the closest fit —
  are: {{COLOR_ROLES}}. A typo here is harmless, not a load error; it just
  paints as the default. The four runtime overlay colours (the hero, rival
  players, monsters, dropped items) are set by the engine, not assignable here.

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

Two optional fields tune random forest encounters. `weight` (default `10`,
must be `> 0`) biases the weighted draw within a zone band — a low weight
surfaces seldom — and `rare` (default `false`) marks a named beast that, on
its kill, fires a public Herald flash and drops the pack's `rare_drop_item`
into the slayer's satchel. Rung guardians ignore both (a rung always takes the
FIRST monster of its tier, never a weighted roll), so a rare should not be the
first entry of a tier that backs a `dungeon_tiers` rung.

```json
{"tier": 2, "name": "the Gilded Stag", "hp": 16, "atk": 6, "def": 2,
 "xp": 40, "gold": 60, "weight": 1, "rare": true}
```

### `items.json`

A list of equipment, consumables, and crafting materials. `slot` is `weapon`,
`armor`, `consumable`, or `material`. Weapons add `atk`, armour adds `def`,
consumables `heal`; a `material` carries none of these — it is the forge ORE,
carried in the satchel and spent at the forge.

```json
{"id": "short_sword", "name": "Short Sword", "slot": "weapon", "atk": 5, "price": 40}
```

The forge ore is a `material` item the player EARNS in combat (not the shop):
price it `0` — ore is never bought or sold — and point `settings.forge_ore_item`
at its id. A won dungeon rung always drops `settings.ore_dungeon_drop` of it, and
a won forest fight has a `settings.ore_forest_chance` chance of one.

```json
{"id": "iron_ore", "name": "Iron Ore", "slot": "material", "price": 0}
```

### `locations.json`

An object keyed by location key. Each entry is a building kind with a menu of
`actions` the player may take inside it.

```json
{
  "inn": {"kind": "inn", "name": "The Sleeping Drake", "glyph": "I",
          "color": "town", "actions": ["rest", "gamble", "leave"],
          "flavor": ["Lamplight pools on worn oak tables."]}
}
```

Give each building the menu that matches its role. The four building kinds and
the verbs the engine honours inside each are:

| `kind` | actions the engine understands |
| --- | --- |
| `inn` | `rest`, `deposit`, `withdraw`, `gamble`, `leave` |
| `shop` | `buy`, `sell`, `forge`, `leave` |
| `healer` | `heal`, `leave` |
| `dungeon` | `descend`, `challenge`, `leave` |

The inn's `deposit`/`withdraw` are the VAULT: a player banks gold into the inn
strongbox (`deposit amount=<gold>`) and draws it back (`withdraw amount=<gold>`).
Banked gold is SAFE from ambush — a sleeping-robber only ever lifts gold in hand
— and it SURVIVES the Wyrm-win legacy reset, so it is the one store of wealth
that carries across runs. Both cost no turn.

`quaff` (drink a satchel tonic) is legal **anywhere** and needs no menu entry.
The `actions` list is advisory — it is the menu the narrator offers, NOT a
validated whitelist (see "What `validate` checks" below): a verb the engine does
not back simply confuses the narrator, so give each building only the verbs from
its row above.

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

The binding file: `name`, `width`, `height`, `spawn` `[x, y]` (the hero's start
cell, which must be on walkable terrain), a `legend` mapping characters to
terrain keys, `terrain_rows` (one string per row, each exactly `width` long), a
`locations` list of `{"key", "x", "y"}` placements (each also on walkable
terrain), a `zones` list (rectangles that bias monster tiers), and a `settings`
object.

```json
{"key": "forest_near", "rect": [30, 18, 60, 36], "tier_lo": 1, "tier_hi": 2}
```

---

## The bands — the limits the loader enforces

These are generated from the loader's own tables, so they are exactly what
`validate` checks. A value outside its band is a load error.

{{BANDS}}

---

## Glyph width — the one-column rule

Every glyph drawn on the map must occupy **exactly one terminal column**. The
frames are box-drawing rectangles; a glyph that renders two columns (a CJK
ideograph like `龍`, an emoji like `🌲`, a fullwidth `Ａ`) shoves its row right
and tears the border, and a combining mark (a decomposed `é`, a lone accent)
stacks onto its neighbour and breaks the count the other way. The loader
rejects all of these at load.

What is admitted is judged for the **Western monospace** metrics every
Understone surface actually uses (the Watch's pinned font stack, a chat
client's code block): under those metrics the East-Asian "Ambiguous" width
class renders single-column, and that class is the CP437 heartland — `█`, `♣`,
`↑`, `∩`, `≈`, `★` all live there — so the rule admits it and bars only the
genuinely double-width Wide and Fullwidth classes.

### Safe glyph palette

{{PALETTE}}

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

**Glyphs.** Map glyphs must render as exactly one terminal column (see the
one-column rule above) and must never collide with the frame's box-drawing
lines or the `@`/`☻` player markers. Pick glyphs that read at a glance — the
bundled Vale uses `.` open ground, `≋` water, `♣` tree, `⌂` inn, `$` shop, `✚`
healer, `∩` dungeon — and lean on the safe palette for period flavour.

**Boss rules.** Exactly one monster carries `"boss": true` and an `id`, and
`settings.boss_monster` points at it. The boss is the only win condition and is
faced only through the `challenge` verb, gated by `settings.wyrm_min_level`. A
boss tier must NOT appear in `settings.dungeon_tiers`: the gauntlet excludes
boss monsters, so a boss-only rung would be unfillable — back every dungeon
tier with at least one ordinary monster.

**The deep, the satchel, and the forge.** `dungeon_tiers` is now a RUNG LADDER
fought one rung per `descend` — list the tiers shallow-to-deep, and make it long
enough to feel like a journey (the Vale uses three). The Wyrm gates on reaching
the floor as well as on level. Size the satchel with `satchel_max` — it caps the
DISTINCT stacks the bag holds (potions and ore each take a slot; per-stack
quantity is unbounded), and it is the death-save reserve, so keep it small (the
Vale carries 3). The forge is the late-game GOLD-AND-ORE sink: `forge_base_cost`
is the gold price of a +1 edge and scales up each tier (`base * (current_plus +
1)`), capped at `forge_max_plus`, and each step ALSO costs ore (see the ore-gated
forge above). Ore is won in the deep (and seldom in the forest), so the forge is
fed by descending — price the gold so a fully-forged piece is a multi-day saving,
and set the ore sources so a steady delver can afford it without a grind.

**Rare beasts.** A rare monster is a small legend: give it a low `weight` so it
surfaces seldom, stats and rewards a clear notch above its tier, and remember it
always drops `rare_drop_item` (a consumable) into the satchel. Keep rares OFF
the first slot of any `dungeon_tiers` tier, or they would become a fixed rung
guardian instead of a rare roll — `validate` now ENFORCES this, so a rare in a
dungeon tier's lead slot is a load error, not just bad form. Place the rare
anywhere after that tier's first ordinary monster.

**Location menus.** Give each building only the actions it can honour, drawn
from the per-kind table under `locations.json` above. An inn that offers `buy`
but no shop logic will confuse the narrator. This is the one major thing
`validate` does NOT check (see below): a wrong or invented verb loads fine and
only muddles the narration, so it is on you to match each menu to its building.

---

## The validate loop

Run `understone validate mypack` after every change. On success you get a
report — name, size, monster/item/zone/event counts, fight share, and the key
settings — ending in **"This pack is sound. The door stands open."** On
failure you get one precise line naming the file, the row, and the field.

The error messages are deliberately instructive: they are the authoring API.
Keep editing and re-validating until the door stands open, then point the
server at your pack with `UNDERSTONE_WORLD=mypack`.

### What `validate` checks, and what it cannot

`validate` runs your pack through the very loader the server uses, so a pack
that validates will load and serve. But the loader checks *structure and
references*, not *meaning* — it cannot read your fiction. Keep the split honest:

**`validate` DOES catch (a load error if wrong):**

{{VALIDATE_COVERAGE}}

**`validate` does NOT catch (the eyeball-only short list):**

* **Location menu `actions` contents.** The list is the narrator's menu, not a
  validated whitelist: a verb the engine does not back (a typo, or a fictional
  `pray`) loads fine and only confuses the narration. Match each building's menu
  to the per-kind table under `locations.json`.
* **Flavour and narration quality.** Names, `flavor` lines, event `text`, the
  feel of the tier curve and the economy — the loader checks they are present
  and in band, never whether they are *good*. That judgement is yours; the
  `simulate` bot can tell you a world is winnable and sanely paced, but only you
  can tell whether it is worth playing.
"""
