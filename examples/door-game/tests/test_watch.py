"""Watch-page payload builders and the watch-URL advertisement.

These are pure-unit tests of :mod:`understone.watch` (no network): the static
world payload's shape and legend completeness, the dynamic state payload's
player/herald/hall content under a frozen clock, and the join/help "Watch the
Vale live" line that appears only when a Game carries a watch URL.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from tests.conftest import fixed_clock, utc
from understone import server as understone_server
from understone import watch
from understone.engine.log import Event
from understone.engine.rng import GameRNG
from understone.game import Game
from understone.persistence import Store
from understone.screen.palette import Color
from understone.world.loader import load_world

if TYPE_CHECKING:
    from understone.engine.world import World

PACK = Path(__file__).resolve().parents[1] / "understone" / "world" / "data"


@pytest.fixture
def world() -> World:
    return load_world(PACK)


@pytest.fixture
def clock() -> object:
    return fixed_clock(utc(2026, 6, 12, 10, 30))


def _game(tmp_path: Path, clock: object, watch_url: str | None = None) -> Game:
    world = load_world(PACK)
    store = Store(tmp_path / "watch.db")
    return Game(  # type: ignore[arg-type]
        world, store, clock=clock, rng=GameRNG(seed=7), watch_url=watch_url
    )


# ---------------------------------------------------------------------------
# World payload (static)
# ---------------------------------------------------------------------------


def test_world_payload_shape(world: World) -> None:
    payload = watch.build_world_payload(world)
    assert payload["name"] == world.name
    assert payload["width"] == world.width
    assert payload["height"] == world.height
    rows = payload["glyph_rows"]
    assert isinstance(rows, list)
    assert len(rows) == world.height
    assert all(isinstance(r, str) and len(r) == world.width for r in rows)


def test_world_payload_legend_is_complete(world: World) -> None:
    payload = watch.build_world_payload(world)
    rows = payload["glyph_rows"]
    legend = payload["legend"]
    assert isinstance(rows, list)
    assert isinstance(legend, dict)
    # Contract: every glyph that appears in the rows has a colour in the legend.
    glyphs = {ch for row in rows for ch in row}
    assert glyphs <= set(legend)
    # And every legend colour is a real palette colour name (no stray roles).
    valid = {c.value for c in Color}
    assert set(legend.values()) <= valid


def test_world_payload_locations_present(world: World) -> None:
    payload = watch.build_world_payload(world)
    locations = payload["locations"]
    assert isinstance(locations, list)
    assert len(locations) == len(world.locations)
    by_name = {loc["name"]: loc for loc in locations}
    # The dungeon mouth rides in the locations overlay with its glyph + colour.
    deep = by_name["The Understone Deep"]
    assert deep["glyph"] == "∩"
    assert deep["color"] == "dungeon"
    assert (deep["x"], deep["y"]) == (70, 12)


def test_world_payload_carries_reskinned_glyphs(world: World) -> None:
    """The v0.6 re-skin reaches the Watch: ≋ water in the rows, ⌂/✚/∩ buildings.

    Water rides the base terrain (glyph_rows + legend); the buildings ride the
    locations overlay. If a glyph reverts, the live map drifts from the frames.
    """
    payload = watch.build_world_payload(world)
    rows = payload["glyph_rows"]
    assert isinstance(rows, list)
    glyphs = {ch for row in rows for ch in row}
    assert "≋" in glyphs  # water in the base map
    assert "~" not in glyphs  # the old water glyph is gone
    legend = payload["legend"]
    assert isinstance(legend, dict)
    assert "≋" in legend
    by_name = {loc["name"]: loc["glyph"] for loc in payload["locations"]}  # type: ignore[index,union-attr]
    assert by_name["The Sleeping Drake"] == "⌂"
    assert by_name["The Quiet Shrine"] == "✚"
    assert by_name["The Understone Deep"] == "∩"


# ---------------------------------------------------------------------------
# v0.9 colour-role split — the payload now carries the EXPANDED vocabulary, so
# distinct terrain/building types read by hue on the Watch and not just by glyph.
# These pin the literal fixes: road no longer shares grass's colour, forest no
# longer shares tree's, the town buildings each carry their own role, and the
# Cinder slag is lava (orange), no longer water (blue).
# ---------------------------------------------------------------------------

CINDER = Path(__file__).resolve().parents[1] / "understone" / "world" / "packs" / "cinder-wastes"


def _terrain_kinds(world: World) -> dict[str, str]:
    """Return the distinct terrain kinds in *world* as ``{key: colour role}``.

    ``world.terrain`` is the painted 2-D grid (one ``TerrainDef`` per cell); the
    distinct kinds are recovered by deduplicating it on ``key``. Every kind in a
    shipped world appears on the map, so this sees all of them.
    """
    kinds: dict[str, str] = {}
    for row in world.terrain:
        for cell in row:
            kinds[cell.key] = cell.color
    return kinds


def _legend_for_terrain_key(world: World, key: str) -> str:
    """Return the legend colour the payload carries for terrain ``key``.

    Resolves the terrain key to its glyph, then reads that glyph's colour out of
    the built payload's legend — so the assertion is on what the Watch receives,
    not on the raw JSON.
    """
    payload = watch.build_world_payload(world)
    legend = payload["legend"]
    assert isinstance(legend, dict)
    glyph = next(cell.glyph for row in world.terrain for cell in row if cell.key == key)
    return legend[glyph]


def test_vale_payload_road_is_not_floor(world: World) -> None:
    """REGRESSION (the literal bug the slice fixes): road has its OWN colour.

    Before v0.9 the Vale road shared ``floor`` with grass, so a path was
    indistinguishable from open ground on the Watch. The road now carries
    ``road``; grass keeps ``floor``; they must differ.
    """
    road = _legend_for_terrain_key(world, "road")
    grass = _legend_for_terrain_key(world, "grass")
    assert road == "road"
    assert grass == "floor"
    assert road != grass


def test_vale_payload_forest_is_not_tree(world: World) -> None:
    """REGRESSION: forest has its OWN colour, no longer shared with tree.

    Dense forest scrub used to share ``tree`` with the tree wall, so the two
    read identically. Forest now carries ``forest``; tree keeps ``tree``.
    """
    forest = _legend_for_terrain_key(world, "forest")
    tree = _legend_for_terrain_key(world, "tree")
    assert forest == "forest"
    assert tree == "tree"
    assert forest != tree


def test_vale_payload_buildings_carry_distinct_roles(world: World) -> None:
    """Each Vale town building rides its own role (inn/shop/healer), not ``town``."""
    payload = watch.build_world_payload(world)
    by_name = {loc["name"]: loc["color"] for loc in payload["locations"]}  # type: ignore[index,union-attr]
    assert by_name["The Sleeping Drake"] == "inn"
    assert by_name["Gravel & Sons Outfitters"] == "shop"
    assert by_name["The Quiet Shrine"] == "healer"
    assert by_name["The Understone Deep"] == "dungeon"
    # No two distinct buildings share a colour role.
    roles = list(by_name.values())
    assert len(set(roles)) == len(roles)


def test_cinder_payload_slag_is_lava_not_water() -> None:
    """The Cinder slag carries ``lava`` (orange), never ``water`` (blue) again.

    This is the Cinder half of the bug: molten slag shared ``water``, so the
    lava rendered BLUE on the Watch. After the remap the legend carries ``lava``
    and ``water`` appears NOWHERE in the Cinder payload (no water in this world).
    """
    cinder = load_world(CINDER)
    payload = watch.build_world_payload(cinder)
    legend = payload["legend"]
    assert isinstance(legend, dict)
    assert _legend_for_terrain_key(cinder, "slag") == "lava"
    assert "water" not in legend.values()


def test_cinder_payload_carries_expanded_roles() -> None:
    """The Cinder terrain reads by hue: ash→barren, basalt→road, cinder→scrub.

    Cinder-fields use ``scrub`` (dusky ember-brown), NOT ``forest`` (green) —
    a volcanic waste must not render as lush woods. ``forest`` is for green
    worlds; ``scrub`` is its barren counterpart.
    """
    cinder = load_world(CINDER)
    assert _legend_for_terrain_key(cinder, "ash") == "barren"
    assert _legend_for_terrain_key(cinder, "basalt") == "road"
    assert _legend_for_terrain_key(cinder, "cinder") == "scrub"
    legend = watch.build_world_payload(cinder)["legend"]
    assert isinstance(legend, dict)
    assert "forest" not in legend.values()  # no green woods in a volcanic waste
    # Obsidian spire reuses the wall role (a rock barrier), same as caldera.
    assert _legend_for_terrain_key(cinder, "spire") == "wall"
    assert _legend_for_terrain_key(cinder, "caldera") == "wall"


def test_both_worlds_terrain_roles_are_distinct_per_world() -> None:
    """No two DISTINCT terrain types share a colour role within a world.

    The point of the slice: after the remap each terrain kind reads by its own
    hue. (A role MAY be shared by two types that are deliberately the same
    barrier — spire/caldera both ``wall`` in Cinder — so this checks distinct
    KEYS that map to the same role are only the intended wall pair.)
    """
    for world_dir, allowed_shared in (
        (PACK, set()),
        (CINDER, {("caldera", "spire")}),
    ):
        w = load_world(world_dir)
        by_role: dict[str, list[str]] = {}
        for key, role in _terrain_kinds(w).items():
            by_role.setdefault(role, []).append(key)
        for role, keys in by_role.items():
            if len(keys) > 1:
                pair = tuple(sorted(keys))
                assert pair in allowed_shared, f"unexpected shared role {role!r}: {keys}"


# ---------------------------------------------------------------------------
# State payload (dynamic)
# ---------------------------------------------------------------------------


def test_state_payload_includes_joined_player(tmp_path: Path, clock: object) -> None:
    game = _game(tmp_path, clock)
    game.join("Brandr")
    payload = watch.build_state_payload(game)
    players = payload["players"]
    assert isinstance(players, list)
    brandr = next(p for p in players if p["name"] == "Brandr")
    assert brandr["level"] == 1
    assert brandr["wins"] == 0
    assert brandr["hp"] == brandr["max_hp"]
    assert brandr["mode"] == "tile"
    assert (brandr["x"], brandr["y"]) == game.world.spawn
    # v0.10: a fresh hero shows their starting gold on hand, nothing banked, and
    # an empty satchel.
    assert brandr["gold"] == game.world.settings.starting_gold
    assert brandr["banked"] == 0
    assert brandr["satchel"] == []


def test_state_payload_surfaces_gold_banked_and_satchel(tmp_path: Path, clock: object) -> None:
    """A joined hero with a stocked satchel and banked gold shows the right values.

    The lobby TV surfaces the whole shared world, so each player's purse (gold
    on hand + vault) and satchel stacks (name + qty, resolved via the pack) ride
    the state payload.
    """
    game = _game(tmp_path, clock)
    game.join("Brandr")
    player = game.players["Brandr"]
    player.gold = 120
    player.banked = 300
    game._satchel_set_stacks(player, [("iron_ore", 5), ("minor_potion", 2)])

    payload = watch.build_state_payload(game)
    brandr = next(p for p in payload["players"] if p["name"] == "Brandr")  # type: ignore[union-attr]
    assert brandr["gold"] == 120
    assert brandr["banked"] == 300
    # Stacks resolve their display name from the pack, preserving stow order.
    assert brandr["satchel"] == [
        {"name": "Iron Ore", "qty": 5},
        {"name": "Minor Potion", "qty": 2},
    ]


def test_state_payload_satchel_unknown_id_falls_back_to_raw(tmp_path: Path, clock: object) -> None:
    """A satchel id no longer in the pack falls back to the raw id, never blank."""
    game = _game(tmp_path, clock)
    game.join("Brandr")
    game.players["Brandr"].satchel = "ghost_item:2"  # not in the pack

    payload = watch.build_state_payload(game)
    brandr = next(p for p in payload["players"] if p["name"] == "Brandr")  # type: ignore[union-attr]
    assert brandr["satchel"] == [{"name": "ghost_item", "qty": 2}]


def test_state_payload_reports_all_players_including_menu(tmp_path: Path, clock: object) -> None:
    game = _game(tmp_path, clock)
    game.join("Brandr")
    game.join("Sigrun")
    # Put Sigrun in a MENU surface; the Watch still shows her on the board.
    sigrun = game.players["Sigrun"]
    from understone.engine.models import Mode

    sigrun.mode = Mode.MENU
    sigrun.at_location = "inn"
    payload = watch.build_state_payload(game)
    names = {p["name"] for p in payload["players"]}  # type: ignore[union-attr]
    assert names == {"Brandr", "Sigrun"}
    menu = next(p for p in payload["players"] if p["name"] == "Sigrun")  # type: ignore[union-attr]
    assert menu["mode"] == "menu"


def test_state_payload_ts_comes_from_clock(tmp_path: Path, clock: object) -> None:
    game = _game(tmp_path, clock)
    payload = watch.build_state_payload(game)
    assert payload["ts"] == "2026-06-12T10:30:00+00:00"


def test_state_payload_herald_is_last_15_oldest_first(tmp_path: Path, clock: object) -> None:
    game = _game(tmp_path, clock)
    # Replace the resident feed with 20 synthetic events in ascending id order.
    game.events = [
        Event(
            event_id=i,
            ts=f"2026-06-12T10:{i:02d}:00+00:00",
            kind="join",
            actor=f"Hero{i}",
            text=f"event {i}",
        )
        for i in range(1, 21)
    ]
    payload = watch.build_state_payload(game)
    herald = payload["herald"]
    assert isinstance(herald, list)
    assert len(herald) == 15
    # Oldest-first: the window is events 6..20, in ascending order.
    assert herald[0]["text"] == "event 6"
    assert herald[-1]["text"] == "event 20"


def test_state_payload_herald_full_window_despite_sparse_ids(tmp_path: Path, clock: object) -> None:
    """Id gaps must not shrink the feed (regression: the window is a list
    tail, not id arithmetic — AUTOINCREMENT ids may be non-contiguous)."""
    game = _game(tmp_path, clock)
    game.events = [
        Event(
            event_id=i * 7,  # sparse, non-contiguous ids
            ts=f"2026-06-12T10:{i:02d}:00+00:00",
            kind="join",
            actor=f"Hero{i}",
            text=f"event {i}",
        )
        for i in range(1, 21)
    ]
    herald = watch.build_state_payload(game)["herald"]
    assert len(herald) == 15
    assert herald[0]["text"] == "event 6"
    assert herald[-1]["text"] == "event 20"


def test_state_payload_herald_handles_short_feed(tmp_path: Path, clock: object) -> None:
    game = _game(tmp_path, clock)
    game.events = [
        Event(
            event_id=1,
            ts="2026-06-12T10:00:00+00:00",
            kind="join",
            actor="Solo",
            text="only one",
        )
    ]
    payload = watch.build_state_payload(game)
    herald = payload["herald"]
    assert isinstance(herald, list)
    assert [e["text"] for e in herald] == ["only one"]


def test_state_payload_hall_capped_at_five(tmp_path: Path, clock: object) -> None:
    game = _game(tmp_path, clock)
    # Seven immortalised runs; the Watch shows only the five most recent.
    for i in range(7):
        game.store.insert_hall_row(f"Hero{i}", f"2026-06-{10 + i:02d}T12:00:00+00:00", i, 6 + i)
    game.store.commit()
    payload = watch.build_state_payload(game)
    hall = payload["hall"]
    assert isinstance(hall, list)
    assert len(hall) == 5
    # Newest first (store ordering): Hero6 leads.
    assert hall[0]["name"] == "Hero6"
    assert hall[0]["level_at_win"] == 12


# ---------------------------------------------------------------------------
# Watch-URL advertisement (join banner + help manual)
# ---------------------------------------------------------------------------


def test_join_advertises_watch_url_when_set(tmp_path: Path, clock: object) -> None:
    game = _game(tmp_path, clock, watch_url="http://127.0.0.1:8077/watch")
    out = game.join("Brandr")
    assert "Watch the Vale live: http://127.0.0.1:8077/watch" in out


def test_join_omits_watch_line_when_unset(tmp_path: Path, clock: object) -> None:
    game = _game(tmp_path, clock)
    out = game.join("Brandr")
    assert "Watch the Vale live" not in out


def test_resume_advertises_watch_url_when_set(tmp_path: Path, clock: object) -> None:
    game = _game(tmp_path, clock, watch_url="http://127.0.0.1:8077/watch")
    game.join("Brandr")
    again = game.join("Brandr")
    assert "Welcome back" in again
    assert "Watch the Vale live: http://127.0.0.1:8077/watch" in again


def test_help_advertises_watch_url_when_set(tmp_path: Path) -> None:
    # door_help reads the module game; install one carrying a watch URL.
    world = load_world(PACK)
    store = Store(tmp_path / "help.db")
    understone_server._set_game(Game(world, store, watch_url="http://127.0.0.1:8077/watch"))
    try:
        manual = understone_server.door_help()
        assert "Watch the Vale live: http://127.0.0.1:8077/watch" in manual
    finally:
        understone_server._GAME.store.close()  # type: ignore[union-attr]
        understone_server._GAME = None


def test_help_omits_watch_line_when_unset(tmp_path: Path) -> None:
    world = load_world(PACK)
    store = Store(tmp_path / "help.db")
    understone_server._set_game(Game(world, store))
    try:
        manual = understone_server.door_help()
        assert "Watch the Vale live" not in manual
    finally:
        understone_server._GAME.store.close()  # type: ignore[union-attr]
        understone_server._GAME = None


# ---------------------------------------------------------------------------
# WATCH_HTML lockstep guards (the JS twin of texture.py + the v0.6 glow-up)
#
# The inline page reproduces logic that lives in Python; these guard the two
# invariants most prone to silent drift — the texture selection formula and the
# other-player marker — plus the presence of the day-phase machinery.
# ---------------------------------------------------------------------------


def test_watch_html_derives_texture_formula_from_constants() -> None:
    """The page's JS index string is DERIVED from texture._HASH_X / _HASH_Y.

    Not a hard-coded "x * 31 + y * 17" snapshot: the expected substring is built
    from the live constants, so a Python-side retune that the watch builder
    fails to track trips here instead of silently shipping a stale formula.
    """
    from understone.screen import texture

    expected = f"x * {texture._HASH_X} + y * {texture._HASH_Y}"
    assert expected in watch.WATCH_HTML


def test_watch_html_js_selection_agrees_with_textured() -> None:
    """The JS selection arithmetic, replayed in Python, matches ``textured``.

    The page computes ``variants[(x * _HASH_X + y * _HASH_Y) % len]``. Replaying
    that exact formula here from the SAME constants and the SAME VARIANTS rows
    and asserting it equals ``texture.textured`` over a full screen grid proves
    both implementations select identically — a stronger lockstep than a string
    match, since it pins the result, not the source text.
    """
    from understone.screen import texture

    for base, choices in texture.VARIANTS.items():
        for x in range(24):
            for y in range(16):
                js_pick = choices[(x * texture._HASH_X + y * texture._HASH_Y) % len(choices)]
                assert texture.textured(base, x, y) == js_pick


def test_watch_html_variants_match_texture_table() -> None:
    """Every base->variants row in texture.VARIANTS appears in the JS VARIANTS map.

    Glyphs ride into the inline JS as ``\\uXXXX`` escapes, so compare against the
    escaped form. A new variant added to Python but not the page trips this.
    """
    from understone.screen import texture

    html = watch.WATCH_HTML
    for base, choices in texture.VARIANTS.items():
        for glyph in {base, *choices}:
            token = glyph if glyph.isascii() else f"\\u{ord(glyph):04x}"
            assert token in html, f"variant glyph {glyph!r} missing from WATCH_HTML"


def test_watch_html_uses_other_player_marker() -> None:
    """Players on the lobby TV wear the ☻ marker (escaped) — no bare '@' marker paint."""
    assert "\\u263b" in watch.WATCH_HTML


def test_watch_html_renders_gold_banked_and_satchel() -> None:
    """The Adventurers panel JS references each player's gold, vault, and satchel."""
    html = watch.WATCH_HTML
    # The roster sub-lines read these state fields by name.
    assert "p.gold" in html
    assert "p.banked" in html
    assert "p.satchel" in html
    # The satchel line has a dedicated renderer with an empty-bag note.
    assert "satchelText" in html
    assert "satchel empty" in html
    assert "vault" in html


def test_watch_html_has_day_phase_machinery() -> None:
    """The dusk/dawn glow-up is wired: the tint classes and the UTC-hour read."""
    html = watch.WATCH_HTML
    assert "applyDayPhase" in html
    assert "getUTCHours" in html
    assert ".map-frame.night" in html
    assert ".map-frame.twilight" in html
    assert "Noto Sans Mono" in html


# ---------------------------------------------------------------------------
# PALETTE completeness — the v0.9 invariant that kills the "silent fallback"
# bug class. The road bug existed because a Color role with no hex in the JS
# PALETTE map fell back to default; this pins that EVERY role has a hex.
# ---------------------------------------------------------------------------


def _watch_palette_keys() -> set[str]:
    """Parse the JS ``var PALETTE = { ... }`` map out of WATCH_HTML, return its keys.

    The map uses bare (unquoted) JS identifier keys — ``road: "#b89a6a",`` — so
    this slices the object literal and collects every ``key:`` token. Keeping the
    parse here (not a hard-coded list) means the test reads whatever the page
    actually ships, so a typo'd or dropped key surfaces as a missing role.
    """
    html = watch.WATCH_HTML
    start = html.index("var PALETTE = {")
    body = html[start : html.index("};", start)]
    # Each entry is `<ident>: "<hex>"`; capture the identifier before the colon.
    return set(re.findall(r"(\w+)\s*:\s*\"#", body))


def test_watch_palette_covers_every_color_role() -> None:
    """EVERY Color enum value has an entry in the JS PALETTE map — no fallbacks.

    This is the literal fix for the road bug: a shipped role with no hex paints
    as ``default`` silently. Asserting ``{c.value} <= palette_keys`` means adding
    a Color without a Watch hex trips here instead of shipping a grey/green road.
    """
    palette_keys = _watch_palette_keys()
    roles = {c.value for c in Color}
    missing = roles - palette_keys
    assert not missing, f"Color roles with no PALETTE hex (silent fallback): {sorted(missing)}"


def test_watch_palette_distinct_new_terrain_hexes() -> None:
    """The expanded terrain roles carry DISTINCT hexes (the point of the slice).

    A guard that the seven new roles didn't accidentally collapse onto one hex
    (which would re-introduce the very "two types, one colour" bug v0.9 fixes).
    Parsed straight from the shipped map.
    """
    html = watch.WATCH_HTML
    start = html.index("var PALETTE = {")
    body = html[start : html.index("};", start)]
    pairs = dict(re.findall(r"(\w+)\s*:\s*\"(#[0-9a-fA-F]{6})\"", body))
    new_roles = ["road", "forest", "lava", "barren", "inn", "shop", "healer"]
    hexes = [pairs[r] for r in new_roles]
    assert all(r in pairs for r in new_roles), "a new v0.9 role is missing its hex"
    assert len(set(hexes)) == len(hexes), f"new roles share a hex: {hexes}"
    # The molten role must NOT reuse water's blue (the Cinder slag bug).
    assert pairs["lava"] != pairs["water"]
