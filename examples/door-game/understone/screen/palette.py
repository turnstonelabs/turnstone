"""Colour vocabulary for cells.

Colours are *stored* on cells but never rendered in v1 — the text
renderer emits glyphs only. The enum exists so a future ANSI renderer can
map roles to SGR codes without touching the grid model.
"""

from __future__ import annotations

from enum import Enum


class Color(Enum):
    """Semantic colour roles for grid cells."""

    DEFAULT = "default"
    WALL = "wall"
    FLOOR = "floor"
    PLAYER = "player"
    OTHER_PLAYER = "other_player"
    MONSTER = "monster"
    ITEM = "item"
    WATER = "water"
    TREE = "tree"
    TOWN = "town"
    DUNGEON = "dungeon"
