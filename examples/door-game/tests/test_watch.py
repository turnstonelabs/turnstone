"""Watch-page payload builders and the watch-URL advertisement.

These are pure-unit tests of :mod:`understone.watch` (no network): the static
world payload's shape and legend completeness, the dynamic state payload's
player/herald/hall content under a frozen clock, and the join/help "Watch the
Vale live" line that appears only when a Game carries a watch URL.
"""

from __future__ import annotations

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
    from understone.screen.palette import Color

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


def test_watch_html_has_day_phase_machinery() -> None:
    """The dusk/dawn glow-up is wired: the tint classes and the UTC-hour read."""
    html = watch.WATCH_HTML
    assert "applyDayPhase" in html
    assert "getUTCHours" in html
    assert ".map-frame.night" in html
    assert ".map-frame.twilight" in html
    assert "Noto Sans Mono" in html
