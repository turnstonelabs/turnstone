"""Core data models for the game engine.

All models are plain dataclasses. ``Player`` is mutable (the engine applies
deltas in place); the static content models (``Monster``, ``Item``,
``TerrainDef``, ``LocationDef``, ``Zone``) are frozen.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Mode(StrEnum):
    """Which interaction surface the player is currently on."""

    TILE = "tile"
    MENU = "menu"


class Slot(StrEnum):
    """Equipment / item slot kinds."""

    WEAPON = "weapon"
    ARMOR = "armor"
    CONSUMABLE = "consumable"
    # v0.10 forge ore: a crafting MATERIAL carried in the satchel and spent at
    # the forge. It is never equipped, never quaffed (no atk/def/heal), and
    # never sold or bought — ore is earned in combat, not traded.
    MATERIAL = "material"


@dataclass(slots=True)
class Player:
    """A single adventurer's durable state.

    Coordinates are map cells; ``mode`` and ``at_location`` track whether
    the player is on the overworld or inside a location menu. Turn fields
    gate the daily action budget; bestow fields gate the daily fortune pool.
    """

    name: str
    x: int
    y: int
    hp: int
    max_hp: int
    level: int
    xp: int
    gold: int
    atk: int
    def_: int
    weapon_id: str
    armor_id: str
    turns_left: int
    turn_day: int
    mode: Mode
    at_location: str
    created_at: str
    last_seen: str
    log_cursor: int
    bestow_spent: int
    bestow_day: int
    wins: int = 0
    posts_sent: int = 0
    post_day: int = 0
    gambles: int = 0
    gamble_day: int = 0
    # v0.7 "depth below" retention columns: how far the dungeon has been
    # plumbed (0 = never descended; N = cleared rung N, 1-indexed), the
    # carried satchel (see below), and the enhancement plus on whichever
    # weapon/armour is CURRENTLY equipped in each slot.
    deepest_rung: int = 0
    # v0.10 STACK-BASED satchel: comma-joined "id:qty" stacks ('' = empty),
    # e.g. "minor_potion:3,iron_ore:5". ``satchel_max`` caps DISTINCT stacks,
    # not total items; per-stack qty is unbounded. Replaces the v0.7 flat id
    # list. The "id:qty" wire format is owned by understone.engine.satchel
    # (decode_satchel/encode_satchel); every reader goes through that codec.
    satchel: str = ""
    weapon_plus: int = 0
    armor_plus: int = 0
    # v0.10 the Vault: gold banked at the inn. SAFE from ambush (the steal only
    # ever touches carried ``gold``) and SURVIVES the Wyrm-win legacy reset (a
    # small persistent reward across runs, like a win ★).
    banked: int = 0


@dataclass(frozen=True, slots=True)
class Monster:
    """A static monster definition from the content pack.

    ``boss`` monsters are the fixed endgame foe (the Wyrm Below): they are
    excluded from random tier-band selection and only ever faced through the
    deliberate ``challenge`` verb.
    """

    tier: int
    name: str
    hp: int
    atk: int
    def_: int
    xp: int
    gold: int
    monster_id: str = ""
    boss: bool = False
    # v0.7 weighted forest encounters: ``weight`` biases the random pick (a
    # low weight surfaces seldom), ``rare`` marks a named beast that fires a
    # public Herald flash and drops a guaranteed draught on the kill. Rung
    # guardians ignore both (a rung is a fixed foe, never a weighted roll).
    weight: int = 10
    rare: bool = False


@dataclass(frozen=True, slots=True)
class Item:
    """A static item / equipment definition from the content pack."""

    item_id: str
    name: str
    slot: Slot
    atk: int
    def_: int
    heal: int
    price: int


@dataclass(frozen=True, slots=True)
class TerrainDef:
    """A terrain kind: its glyph, walkability, encounter rate, colour role."""

    key: str
    glyph: str
    walkable: bool
    encounter_rate: float
    color: str


@dataclass(frozen=True, slots=True)
class LocationDef:
    """A named location placed on the map (inn, shop, healer, dungeon)."""

    key: str
    kind: str
    name: str
    x: int
    y: int
    glyph: str
    color: str
    actions: tuple[str, ...]
    flavor: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class Zone:
    """A rectangular region that biases which monster tiers spawn."""

    key: str
    x0: int
    y0: int
    x1: int
    y1: int
    tier_lo: int
    tier_hi: int

    def contains(self, x: int, y: int) -> bool:
        """Return whether ``(x, y)`` falls inside this zone's rectangle."""
        return self.x0 <= x <= self.x1 and self.y0 <= y <= self.y1


@dataclass(frozen=True, slots=True)
class WorldEvent:
    """One row of the weighted overworld encounter table.

    ``kind`` is one of ``fight``/``gold``/``heal``/``trap``/``lore``.
    ``weight`` biases random selection. ``lo``/``hi`` bound the rolled amount
    for the value-bearing kinds (gold/heal/trap); they are unused for
    ``fight`` (the foe comes from the zone band) and ``lore`` (pure flavour).
    """

    kind: str
    weight: int
    text: str
    lo: int
    hi: int


@dataclass(frozen=True, slots=True)
class Settings:
    """Economy and progression parameters sourced from the content pack."""

    daily_turns: int
    rest_cost: int
    heal_cost_per_hp: int
    starting_gold: int
    starting_weapon: str
    starting_armor: str
    start_hp: int
    start_atk: int
    start_def: int
    xp_base: int
    growth_max_hp: int
    growth_atk: int
    growth_def: int
    bestow_daily_budget: int
    dungeon_tiers: tuple[int, ...]
    boss_monster: str
    wyrm_min_level: int
    ambush_min_level: int
    ambush_level_band: int
    ambush_gold_pct: int
    post_daily_cap: int
    gamble_max_bet: int
    gamble_daily_cap: int
    # v0.7 "depth below": the carried-potion satchel size, the forge cost
    # ladder (base * (current_plus + 1)) and its enhancement ceiling, and the
    # consumable item a rare beast is guaranteed to drop on its kill.
    satchel_max: int
    forge_base_cost: int
    forge_max_plus: int
    rare_drop_item: str
    # v0.10 the ore-gated forge: the world's forge MATERIAL item id (validated
    # to slot=material), the ore each +1 step costs (need = (plus + 1) *
    # per_plus), and the two ore sources — a guaranteed drop on a won dungeon
    # rung and a chance of one ore on a won forest fight. Ore is combat-earned,
    # never purchasable; the forge spends gold AND ore.
    forge_ore_item: str
    forge_ore_per_plus: int
    ore_dungeon_drop: int
    ore_forest_chance: float
    # v0.8 "worlds without authors": the Watch's per-world CRT palette. One of
    # the names in WATCH_THEMES; defaults to "phosphor" (the original green), so
    # a pack that omits it looks exactly as the Vale always has.
    watch_theme: str = "phosphor"
