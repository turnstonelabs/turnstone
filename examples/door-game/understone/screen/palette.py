"""Colour vocabulary for cells.

Colours are *stored* on cells and rendered by the live Watch page, which maps
each role to a hue (see ``watch.PALETTE``). The text frame renderer stays
monochrome — it emits glyphs only — so a cell's colour rides the grid model
untouched until a colour-aware renderer (the Watch today, an ANSI terminal
later) reads it.
"""

from __future__ import annotations

from enum import Enum


class Color(Enum):
    """Semantic colour roles for grid cells.

    One global vocabulary, shared by every world — there are no per-world or
    per-theme palettes. Roles are split into two families: the runtime overlay
    colours an actor or item wears (``PLAYER``/``OTHER_PLAYER``/``MONSTER``/
    ``ITEM``) and the author-assignable terrain/location roles a pack paints its
    map with (everything else). A future colour renderer maps each role to a
    hue; the Watch already does (see ``watch.PALETTE``).
    """

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
    # Expanded terrain/location roles (v0.9) — so distinct types read by hue and
    # not only by glyph. ROAD splits paths off FLOOR; FOREST is lush dense
    # vegetation; SCRUB is its barren counterpart — rough, non-lush dense terrain
    # (volcanic cinder, desert scrub) that must NOT read as green woods; LAVA
    # gives molten ground its own orange (no longer mis-sharing WATER's blue);
    # BARREN gives open wasteland ground a taupe; INN/SHOP/HEALER give each town
    # building its own hue (TOWN stays as a generic fallback).
    ROAD = "road"
    FOREST = "forest"
    SCRUB = "scrub"
    LAVA = "lava"
    BARREN = "barren"
    INN = "inn"
    SHOP = "shop"
    HEALER = "healer"

    @classmethod
    def assignable(cls) -> list[Color]:
        """The roles a pack may paint terrain or a location with.

        One source of truth for the overlay-vs-assignable split. Excludes the
        runtime overlay colours an actor/item wears (``PLAYER``/
        ``OTHER_PLAYER``/``MONSTER``/``ITEM``) and the ``DEFAULT`` fallback —
        none of which an author assigns. Consumers (the authoring manual and
        its test) read this so the documented vocabulary can never drift from
        the enum. Returned in definition order.
        """
        overlay = {cls.DEFAULT, cls.PLAYER, cls.OTHER_PLAYER, cls.MONSTER, cls.ITEM}
        return [role for role in cls if role not in overlay]
