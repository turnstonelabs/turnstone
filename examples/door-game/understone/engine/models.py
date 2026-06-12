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


@dataclass(frozen=True, slots=True)
class Monster:
    """A static monster definition from the content pack."""

    tier: int
    name: str
    hp: int
    atk: int
    def_: int
    xp: int
    gold: int


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
class Settings:
    """Economy and progression parameters sourced from the content pack."""

    daily_turns: int
    rest_cost: int
    heal_cost_per_hp: int
    starting_gold: int
    starting_weapon: str
    starting_armor: str
    xp_base: int
    growth_max_hp: int
    growth_atk: int
    growth_def: int
    bestow_daily_budget: int
    dungeon_tiers: tuple[int, ...]
