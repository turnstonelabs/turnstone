"""Shared test fixtures and builders.

These builders construct engine objects directly (no JSON loader) so the
engine tests stay independent of the content pack. Later chunks add
fixtures that load the shipped world and build the game façade.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from understone.engine.models import (
    Item,
    LocationDef,
    Mode,
    Monster,
    Player,
    Settings,
    Slot,
    TerrainDef,
    Zone,
)
from understone.engine.world import World

if TYPE_CHECKING:
    from collections.abc import Callable

# ---------------------------------------------------------------------------
# Terrain kinds for synthetic test worlds
# ---------------------------------------------------------------------------

GRASS = TerrainDef(key="grass", glyph=".", walkable=True, encounter_rate=0.0, color="floor")
WALL = TerrainDef(key="wall", glyph="█", walkable=False, encounter_rate=0.0, color="wall")
WATER = TerrainDef(key="water", glyph="~", walkable=False, encounter_rate=0.0, color="water")
FOREST = TerrainDef(key="forest", glyph="↑", walkable=True, encounter_rate=1.0, color="tree")
SAFE_FOREST = TerrainDef(key="forest", glyph="↑", walkable=True, encounter_rate=0.0, color="tree")


DEFAULT_SETTINGS = Settings(
    daily_turns=10,
    rest_cost=15,
    heal_cost_per_hp=2,
    starting_gold=20,
    starting_weapon="rusty_dagger",
    starting_armor="cloth_tunic",
    xp_base=100,
    growth_max_hp=6,
    growth_atk=2,
    growth_def=1,
    bestow_daily_budget=25,
    dungeon_tiers=(4, 5),
)


def make_settings(**overrides: object) -> Settings:
    """Return DEFAULT_SETTINGS with field overrides for band testing."""
    base = {
        "daily_turns": DEFAULT_SETTINGS.daily_turns,
        "rest_cost": DEFAULT_SETTINGS.rest_cost,
        "heal_cost_per_hp": DEFAULT_SETTINGS.heal_cost_per_hp,
        "starting_gold": DEFAULT_SETTINGS.starting_gold,
        "starting_weapon": DEFAULT_SETTINGS.starting_weapon,
        "starting_armor": DEFAULT_SETTINGS.starting_armor,
        "xp_base": DEFAULT_SETTINGS.xp_base,
        "growth_max_hp": DEFAULT_SETTINGS.growth_max_hp,
        "growth_atk": DEFAULT_SETTINGS.growth_atk,
        "growth_def": DEFAULT_SETTINGS.growth_def,
        "bestow_daily_budget": DEFAULT_SETTINGS.bestow_daily_budget,
        "dungeon_tiers": DEFAULT_SETTINGS.dungeon_tiers,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def make_player(**overrides: object) -> Player:
    """Build a Player at sane defaults; override any field by keyword."""
    fields = {
        "name": "Tester",
        "x": 5,
        "y": 5,
        "hp": 20,
        "max_hp": 20,
        "level": 1,
        "xp": 0,
        "gold": 50,
        "atk": 5,
        "def_": 1,
        "weapon_id": "rusty_dagger",
        "armor_id": "cloth_tunic",
        "turns_left": 10,
        "turn_day": 0,
        "mode": Mode.TILE,
        "at_location": "",
        "created_at": "2026-01-01T00:00:00+00:00",
        "last_seen": "2026-01-01T00:00:00+00:00",
        "log_cursor": 0,
        "bestow_spent": 0,
        "bestow_day": 0,
    }
    fields.update(overrides)
    return Player(**fields)  # type: ignore[arg-type]


def make_monster(**overrides: object) -> Monster:
    """Build a Monster at tier-1 defaults."""
    fields = {"tier": 1, "name": "Field Rat", "hp": 6, "atk": 3, "def_": 0, "xp": 8, "gold": 3}
    fields.update(overrides)
    return Monster(**fields)  # type: ignore[arg-type]


def make_world(
    *,
    grid: list[list[TerrainDef]] | None = None,
    width: int = 11,
    height: int = 11,
    spawn: tuple[int, int] = (5, 5),
    locations: list[LocationDef] | None = None,
    zones: list[Zone] | None = None,
    monsters: list[Monster] | None = None,
    items: list[Item] | None = None,
    settings: Settings | None = None,
) -> World:
    """Build a small synthetic World (all-grass by default)."""
    if grid is None:
        grid = [[GRASS for _ in range(width)] for _ in range(height)]
    return World(
        name="Test Vale",
        width=width,
        height=height,
        spawn=spawn,
        terrain=grid,
        locations=locations or [],
        zones=zones or [],
        monsters=monsters or [make_monster()],
        items=items or _default_items(),
        settings=settings or DEFAULT_SETTINGS,
    )


def _default_items() -> list[Item]:
    return [
        Item("rusty_dagger", "Rusty Dagger", Slot.WEAPON, 2, 0, 0, 0),
        Item("short_sword", "Short Sword", Slot.WEAPON, 5, 0, 0, 40),
        Item("cloth_tunic", "Cloth Tunic", Slot.ARMOR, 0, 1, 0, 0),
        Item("leather_armor", "Leather Armor", Slot.ARMOR, 0, 3, 0, 50),
        Item("minor_potion", "Minor Potion", Slot.CONSUMABLE, 0, 0, 15, 12),
    ]


def fixed_clock(moment: datetime) -> Callable[[], datetime]:
    """Return a clock callable that always reports *moment*."""

    def _clock() -> datetime:
        return moment

    return _clock


def utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    """Construct a tz-aware UTC datetime."""
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


@pytest.fixture
def small_world() -> World:
    """An 11x11 all-grass world with the default content tables."""
    return make_world()
