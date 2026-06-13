"""Shared test fixtures and builders.

These builders construct engine objects directly (no JSON loader) so the
engine tests stay independent of the content pack. Later chunks add
fixtures that load the shipped world and build the game façade.
"""

from __future__ import annotations

from collections import Counter
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
    WorldEvent,
    Zone,
)
from understone.engine.world import World

if TYPE_CHECKING:
    from collections.abc import Callable

    from understone.game import Game

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
    start_hp=20,
    start_atk=3,
    start_def=0,
    xp_base=100,
    growth_max_hp=6,
    growth_atk=2,
    growth_def=1,
    bestow_daily_budget=25,
    dungeon_tiers=(4, 5),
    boss_monster="wyrm_below",
    wyrm_min_level=6,
    ambush_min_level=3,
    ambush_level_band=2,
    ambush_gold_pct=25,
    post_daily_cap=5,
    gamble_max_bet=50,
    gamble_daily_cap=5,
    satchel_max=3,
    forge_base_cost=60,
    forge_max_plus=3,
    rare_drop_item="minor_potion",
    forge_ore_item="iron_ore",
    forge_ore_per_plus=1,
    ore_dungeon_drop=2,
    ore_forest_chance=0.2,
    watch_theme="phosphor",
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
        "start_hp": DEFAULT_SETTINGS.start_hp,
        "start_atk": DEFAULT_SETTINGS.start_atk,
        "start_def": DEFAULT_SETTINGS.start_def,
        "xp_base": DEFAULT_SETTINGS.xp_base,
        "growth_max_hp": DEFAULT_SETTINGS.growth_max_hp,
        "growth_atk": DEFAULT_SETTINGS.growth_atk,
        "growth_def": DEFAULT_SETTINGS.growth_def,
        "bestow_daily_budget": DEFAULT_SETTINGS.bestow_daily_budget,
        "dungeon_tiers": DEFAULT_SETTINGS.dungeon_tiers,
        "boss_monster": DEFAULT_SETTINGS.boss_monster,
        "wyrm_min_level": DEFAULT_SETTINGS.wyrm_min_level,
        "ambush_min_level": DEFAULT_SETTINGS.ambush_min_level,
        "ambush_level_band": DEFAULT_SETTINGS.ambush_level_band,
        "ambush_gold_pct": DEFAULT_SETTINGS.ambush_gold_pct,
        "post_daily_cap": DEFAULT_SETTINGS.post_daily_cap,
        "gamble_max_bet": DEFAULT_SETTINGS.gamble_max_bet,
        "gamble_daily_cap": DEFAULT_SETTINGS.gamble_daily_cap,
        "satchel_max": DEFAULT_SETTINGS.satchel_max,
        "forge_base_cost": DEFAULT_SETTINGS.forge_base_cost,
        "forge_max_plus": DEFAULT_SETTINGS.forge_max_plus,
        "rare_drop_item": DEFAULT_SETTINGS.rare_drop_item,
        "forge_ore_item": DEFAULT_SETTINGS.forge_ore_item,
        "forge_ore_per_plus": DEFAULT_SETTINGS.forge_ore_per_plus,
        "ore_dungeon_drop": DEFAULT_SETTINGS.ore_dungeon_drop,
        "ore_forest_chance": DEFAULT_SETTINGS.ore_forest_chance,
        "watch_theme": DEFAULT_SETTINGS.watch_theme,
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
        "wins": 0,
        "posts_sent": 0,
        "post_day": 0,
        "gambles": 0,
        "gamble_day": 0,
    }
    fields.update(overrides)
    return Player(**fields)  # type: ignore[arg-type]


def make_monster(**overrides: object) -> Monster:
    """Build a Monster at tier-1 defaults."""
    fields = {
        "tier": 1,
        "name": "Field Rat",
        "hp": 6,
        "atk": 3,
        "def_": 0,
        "xp": 8,
        "gold": 3,
        "monster_id": "",
        "boss": False,
    }
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
    events: list[WorldEvent] | None = None,
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
        events=events,
    )


def _default_items() -> list[Item]:
    return [
        Item("rusty_dagger", "Rusty Dagger", Slot.WEAPON, 2, 0, 0, 0),
        Item("short_sword", "Short Sword", Slot.WEAPON, 5, 0, 0, 40),
        Item("cloth_tunic", "Cloth Tunic", Slot.ARMOR, 0, 1, 0, 0),
        Item("leather_armor", "Leather Armor", Slot.ARMOR, 0, 3, 0, 50),
        Item("minor_potion", "Minor Potion", Slot.CONSUMABLE, 0, 0, 15, 12),
        Item("iron_ore", "Iron Ore", Slot.MATERIAL, 0, 0, 0, 0),
    ]


def fixed_clock(moment: datetime) -> Callable[[], datetime]:
    """Return a clock callable that always reports *moment*."""

    def _clock() -> datetime:
        return moment

    return _clock


def utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    """Construct a tz-aware UTC datetime."""
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Satchel test helpers (the v0.10 stack encoding)
# ---------------------------------------------------------------------------
# The satchel is stack-based ("id:qty"); these wrap the game façade's stack
# helpers so a test can seed/read a bag as a flat id list (duplicate ids
# collapse to one stack), keeping the assertions readable. Shared by the
# descend and Wyrm suites.


def set_satchel(game: Game, player: object, ids: list[str]) -> None:
    """Seed *player*'s satchel from a flat id list (duplicates -> one stack qty)."""
    counts = Counter(ids)
    stacks = [(item_id, counts[item_id]) for item_id in dict.fromkeys(ids)]
    game._satchel_set_stacks(player, stacks)  # type: ignore[arg-type]


def satchel_ids(game: Game, player: object) -> list[str]:
    """Return the satchel as a flat id list, each stack expanded by its qty."""
    out: list[str] = []
    for item_id, qty in game._satchel_stacks(player):  # type: ignore[arg-type]
        out.extend([item_id] * qty)
    return out


@pytest.fixture
def small_world() -> World:
    """An 11x11 all-grass world with the default content tables."""
    return make_world()
