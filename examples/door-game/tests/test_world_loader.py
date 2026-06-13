"""Content-pack loader tests.

Asserts the shipped pack loads, and that representative malformed packs
each raise :class:`WorldLoadError` with a readable message: a bad legend
character, a location placed on non-walkable terrain, a row-width / height
mismatch, and an economy setting outside its sanity band.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from understone.errors import WorldLoadError
from understone.world.loader import load_world

SHIPPED = Path(__file__).resolve().parents[1] / "understone" / "world" / "data"


def test_shipped_pack_loads() -> None:
    world = load_world(SHIPPED)
    assert world.name == "The Vale of Understone"
    assert world.width == 96
    assert world.height == 48
    assert world.is_walkable(*world.spawn)
    assert len(world.locations) == 4
    assert len(world.zones) == 2
    # Tiers 1..5 are the random foes; tier 6 is the boss (the Wyrm Below).
    assert {m.tier for m in world.monsters} == {1, 2, 3, 4, 5, 6}
    boss = world.monster_by_id(world.settings.boss_monster)
    assert boss is not None and boss.boss and boss.name == "the Wyrm Below"


def _clone_pack(tmp_path: Path) -> Path:
    dest = tmp_path / "pack"
    shutil.copytree(SHIPPED, dest)
    return dest


def _rewrite(path: Path, mutate: Any) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    mutate(data)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_bad_legend_char_rejected(tmp_path: Path) -> None:
    pack = _clone_pack(tmp_path)

    def mutate(data: dict[str, Any]) -> None:
        # Splice an unknown glyph into the middle of a terrain row.
        row = list(data["terrain_rows"][24])
        row[40] = "Z"
        data["terrain_rows"][24] = "".join(row)

    _rewrite(pack / "world.json", mutate)
    with pytest.raises(WorldLoadError, match="not in the legend"):
        load_world(pack)


def test_location_on_non_walkable_rejected(tmp_path: Path) -> None:
    pack = _clone_pack(tmp_path)

    def mutate(data: dict[str, Any]) -> None:
        # Move the inn onto a tree-border tile (col 0 is the tree frame).
        for loc in data["locations"]:
            if loc["key"] == "inn":
                loc["x"] = 0
                loc["y"] = 24

    _rewrite(pack / "world.json", mutate)
    with pytest.raises(WorldLoadError, match="non-walkable"):
        load_world(pack)


def test_dimension_mismatch_rejected(tmp_path: Path) -> None:
    pack = _clone_pack(tmp_path)

    def mutate(data: dict[str, Any]) -> None:
        # Truncate one row so its width no longer matches the declared width.
        data["terrain_rows"][10] = data["terrain_rows"][10][:-5]

    _rewrite(pack / "world.json", mutate)
    with pytest.raises(WorldLoadError, match="wide but width is"):
        load_world(pack)


def test_height_mismatch_rejected(tmp_path: Path) -> None:
    pack = _clone_pack(tmp_path)

    def mutate(data: dict[str, Any]) -> None:
        data["terrain_rows"] = data["terrain_rows"][:-1]

    _rewrite(pack / "world.json", mutate)
    with pytest.raises(WorldLoadError, match="rows but height is"):
        load_world(pack)


def test_settings_out_of_band_rejected(tmp_path: Path) -> None:
    pack = _clone_pack(tmp_path)

    def mutate(data: dict[str, Any]) -> None:
        data["settings"]["daily_turns"] = 0  # band is 1..100

    _rewrite(pack / "world.json", mutate)
    with pytest.raises(WorldLoadError, match="daily_turns"):
        load_world(pack)


def test_start_hp_zero_rejected(tmp_path: Path) -> None:
    """A starting HP of 0 is out of band (1..500): a hero must begin alive."""
    pack = _clone_pack(tmp_path)

    def mutate(data: dict[str, Any]) -> None:
        data["settings"]["start_hp"] = 0  # band is 1..500

    _rewrite(pack / "world.json", mutate)
    with pytest.raises(WorldLoadError, match="start_hp"):
        load_world(pack)


def test_unknown_starting_item_rejected(tmp_path: Path) -> None:
    pack = _clone_pack(tmp_path)

    def mutate(data: dict[str, Any]) -> None:
        data["settings"]["starting_weapon"] = "no_such_blade"

    _rewrite(pack / "world.json", mutate)
    with pytest.raises(WorldLoadError, match="not a known item id"):
        load_world(pack)


def test_missing_pack_file_rejected(tmp_path: Path) -> None:
    pack = _clone_pack(tmp_path)
    (pack / "monsters.json").unlink()
    with pytest.raises(WorldLoadError, match="missing pack file"):
        load_world(pack)


def test_monster_nonpositive_hp_rejected(tmp_path: Path) -> None:
    pack = _clone_pack(tmp_path)

    def mutate(data: list[dict[str, Any]]) -> None:
        data[0]["hp"] = 0  # a monster with no hit points is unkillable nonsense

    _rewrite(pack / "monsters.json", mutate)
    with pytest.raises(WorldLoadError, match=r"monsters\.json\[0\] hp must be >= 1"):
        load_world(pack)


def test_monster_negative_stat_rejected(tmp_path: Path) -> None:
    pack = _clone_pack(tmp_path)

    def mutate(data: list[dict[str, Any]]) -> None:
        data[1]["gold"] = -5

    _rewrite(pack / "monsters.json", mutate)
    with pytest.raises(WorldLoadError, match=r"monsters\.json\[1\] gold must be >= 0"):
        load_world(pack)


def test_item_negative_price_rejected(tmp_path: Path) -> None:
    pack = _clone_pack(tmp_path)

    def mutate(data: list[dict[str, Any]]) -> None:
        data[1]["price"] = -10  # a negative price would pay the player to take it

    _rewrite(pack / "items.json", mutate)
    with pytest.raises(WorldLoadError, match=r"items\.json\[1\] price must be >= 0"):
        load_world(pack)


def test_dungeon_tier_without_monster_rejected(tmp_path: Path) -> None:
    pack = _clone_pack(tmp_path)

    def mutate(data: dict[str, Any]) -> None:
        # Tier 9 has no monster in the pack, so the gauntlet rung is unfillable.
        data["settings"]["dungeon_tiers"] = [4, 9]

    _rewrite(pack / "world.json", mutate)
    with pytest.raises(WorldLoadError, match=r"dungeon_tiers\[1\] = 9 has no monster"):
        load_world(pack)


def test_dungeon_tiers_empty_rejected(tmp_path: Path) -> None:
    pack = _clone_pack(tmp_path)

    def mutate(data: dict[str, Any]) -> None:
        data["settings"]["dungeon_tiers"] = []

    _rewrite(pack / "world.json", mutate)
    with pytest.raises(WorldLoadError, match="dungeon_tiers must be a non-empty list"):
        load_world(pack)


def test_dungeon_tier_backed_only_by_boss_rejected(tmp_path: Path) -> None:
    """A boss-only tier is unfillable: the gauntlet excludes boss monsters.

    Tier 6 in the shipped pack holds only the Wyrm Below (a boss). A gauntlet
    rung at tier 6 would draw from monsters_for_tier_band, which filters bosses
    out, so the rung silently does nothing — the loader must reject it instead.
    """
    pack = _clone_pack(tmp_path)

    def mutate(data: dict[str, Any]) -> None:
        data["settings"]["dungeon_tiers"] = [4, 6]  # 6 is the boss-only tier

    _rewrite(pack / "world.json", mutate)
    with pytest.raises(WorldLoadError, match=r"dungeon_tiers\[1\] = 6 has no monster"):
        load_world(pack)


# ---------------------------------------------------------------------------
# v0.2 loader rejections: the event table and the Wyrm settings
# ---------------------------------------------------------------------------


def test_events_without_fight_rejected(tmp_path: Path) -> None:
    pack = _clone_pack(tmp_path)

    def mutate(data: dict[str, Any]) -> None:
        # Strip every fight row; a walk could then never spawn a monster.
        data["events"] = [e for e in data["events"] if e["kind"] != "fight"]

    _rewrite(pack / "events.json", mutate)
    with pytest.raises(WorldLoadError, match="at least one 'fight' entry"):
        load_world(pack)


def test_event_zero_weight_rejected(tmp_path: Path) -> None:
    pack = _clone_pack(tmp_path)

    def mutate(data: dict[str, Any]) -> None:
        data["events"][0]["weight"] = 0

    _rewrite(pack / "events.json", mutate)
    with pytest.raises(WorldLoadError, match="weight must be > 0"):
        load_world(pack)


def test_event_min_exceeds_max_rejected(tmp_path: Path) -> None:
    pack = _clone_pack(tmp_path)

    def mutate(data: dict[str, Any]) -> None:
        # Find a value-bearing row and invert its band.
        for event in data["events"]:
            if event["kind"] == "gold":
                event["min"], event["max"] = 9, 2
                break

    _rewrite(pack / "events.json", mutate)
    with pytest.raises(WorldLoadError, match="min 9 exceeds max 2"):
        load_world(pack)


def test_event_amount_out_of_band_rejected(tmp_path: Path) -> None:
    pack = _clone_pack(tmp_path)

    def mutate(data: dict[str, Any]) -> None:
        for event in data["events"]:
            if event["kind"] == "heal":
                event["max"] = 500  # heal band is 1..100
                break

    _rewrite(pack / "events.json", mutate)
    with pytest.raises(WorldLoadError, match=r"heal amount .* is out of band"):
        load_world(pack)


def test_event_nonfight_blank_text_rejected(tmp_path: Path) -> None:
    pack = _clone_pack(tmp_path)

    def mutate(data: dict[str, Any]) -> None:
        for event in data["events"]:
            if event["kind"] == "lore":
                event["text"] = "   "
                break

    _rewrite(pack / "events.json", mutate)
    with pytest.raises(WorldLoadError, match="requires non-empty 'text'"):
        load_world(pack)


def test_boss_monster_unknown_rejected(tmp_path: Path) -> None:
    pack = _clone_pack(tmp_path)

    def mutate(data: dict[str, Any]) -> None:
        data["settings"]["boss_monster"] = "no_such_wyrm"

    _rewrite(pack / "world.json", mutate)
    with pytest.raises(WorldLoadError, match="is not a known monster id"):
        load_world(pack)


def test_boss_monster_not_flagged_boss_rejected(tmp_path: Path) -> None:
    pack = _clone_pack(tmp_path)

    def mutate(data: list[dict[str, Any]]) -> None:
        # Give a plain monster an id and point boss_monster at it; it lacks the
        # boss flag, so it must be rejected as the endgame foe.
        data[0]["id"] = "field_rat"

    _rewrite(pack / "monsters.json", mutate)

    def point(data: dict[str, Any]) -> None:
        data["settings"]["boss_monster"] = "field_rat"

    _rewrite(pack / "world.json", point)
    with pytest.raises(WorldLoadError, match='must be flagged "boss": true'):
        load_world(pack)


def test_wyrm_min_level_out_of_band_rejected(tmp_path: Path) -> None:
    pack = _clone_pack(tmp_path)

    def mutate(data: dict[str, Any]) -> None:
        data["settings"]["wyrm_min_level"] = 0  # band is 1..50

    _rewrite(pack / "world.json", mutate)
    with pytest.raises(WorldLoadError, match="wyrm_min_level"):
        load_world(pack)


# ---------------------------------------------------------------------------
# v0.5 social settings: ambush / post / gamble economy bands
# ---------------------------------------------------------------------------


def test_ambush_gold_pct_out_of_band_rejected(tmp_path: Path) -> None:
    """The steal percentage is a 0..100 band; 101 is rejected by name."""
    pack = _clone_pack(tmp_path)

    def mutate(data: dict[str, Any]) -> None:
        data["settings"]["ambush_gold_pct"] = 101  # band is 0..100

    _rewrite(pack / "world.json", mutate)
    with pytest.raises(WorldLoadError, match="ambush_gold_pct"):
        load_world(pack)


def test_ambush_level_band_out_of_band_rejected(tmp_path: Path) -> None:
    pack = _clone_pack(tmp_path)

    def mutate(data: dict[str, Any]) -> None:
        data["settings"]["ambush_level_band"] = 11  # band is 0..10

    _rewrite(pack / "world.json", mutate)
    with pytest.raises(WorldLoadError, match="ambush_level_band"):
        load_world(pack)


def test_gamble_max_bet_out_of_band_rejected(tmp_path: Path) -> None:
    """A max bet of 0 is below the 1..10000 floor: the house needs a real stake."""
    pack = _clone_pack(tmp_path)

    def mutate(data: dict[str, Any]) -> None:
        data["settings"]["gamble_max_bet"] = 0  # band is 1..10000

    _rewrite(pack / "world.json", mutate)
    with pytest.raises(WorldLoadError, match="gamble_max_bet"):
        load_world(pack)


def test_post_daily_cap_out_of_band_rejected(tmp_path: Path) -> None:
    pack = _clone_pack(tmp_path)

    def mutate(data: dict[str, Any]) -> None:
        data["settings"]["post_daily_cap"] = 51  # band is 0..50

    _rewrite(pack / "world.json", mutate)
    with pytest.raises(WorldLoadError, match="post_daily_cap"):
        load_world(pack)


def test_missing_social_setting_rejected(tmp_path: Path) -> None:
    """A pack that predates the social settings fails loudly (no silent default)."""
    pack = _clone_pack(tmp_path)

    def mutate(data: dict[str, Any]) -> None:
        del data["settings"]["ambush_min_level"]

    _rewrite(pack / "world.json", mutate)
    with pytest.raises(WorldLoadError, match="ambush_min_level"):
        load_world(pack)


# ---------------------------------------------------------------------------
# v0.4 loader hardening: glyphs, map size, count caps, and name lengths
#
# Packs are now routinely untrusted LLM output, so the loader bands the shapes
# that could tear a frame, balloon memory, or impersonate a player. Each
# rejection still names the file and field at fault.
# ---------------------------------------------------------------------------


def test_box_drawing_terrain_glyph_rejected(tmp_path: Path) -> None:
    """A terrain glyph may not be a frame box-drawing line (it would tear borders)."""
    pack = _clone_pack(tmp_path)

    def mutate(data: dict[str, Any]) -> None:
        data["."]["glyph"] = "─"  # the horizontal frame run

    _rewrite(pack / "terrain.json", mutate)
    with pytest.raises(WorldLoadError, match=r"terrain\.json.* box-drawing"):
        load_world(pack)


def test_player_marker_terrain_glyph_rejected(tmp_path: Path) -> None:
    """A terrain glyph may not be '@' — that is the player's own marker."""
    pack = _clone_pack(tmp_path)

    def mutate(data: dict[str, Any]) -> None:
        data["."]["glyph"] = "@"

    _rewrite(pack / "terrain.json", mutate)
    with pytest.raises(WorldLoadError, match=r"terrain\.json.* reserved for player markers"):
        load_world(pack)


def test_other_player_marker_terrain_glyph_rejected(tmp_path: Path) -> None:
    """A terrain glyph may not be '☻' — the v0.6 other-player marker."""
    pack = _clone_pack(tmp_path)

    def mutate(data: dict[str, Any]) -> None:
        data["."]["glyph"] = "☻"

    _rewrite(pack / "terrain.json", mutate)
    with pytest.raises(WorldLoadError, match=r"terrain\.json.* reserved for player markers"):
        load_world(pack)


def test_ampersand_terrain_glyph_now_accepted(tmp_path: Path) -> None:
    """'&' is no longer an actor marker (☻ took that role), so it is pack-legal.

    The load itself is the assertion — it must not raise the actor-marker
    rejection. A grass cell then carries the new glyph.
    """
    pack = _clone_pack(tmp_path)

    def mutate(data: dict[str, Any]) -> None:
        data["."]["glyph"] = "&"

    _rewrite(pack / "terrain.json", mutate)
    world = load_world(pack)  # no WorldLoadError: '&' is admitted
    grass = next(
        world.terrain_at(x, y)
        for y in range(world.height)
        for x in range(world.width)
        if world.terrain_at(x, y).key == "grass"
    )
    assert grass.glyph == "&"


def test_wide_cjk_terrain_glyph_rejected(tmp_path: Path) -> None:
    """A Wide (EAW=W) ideograph would render two columns and tear the frame."""
    pack = _clone_pack(tmp_path)

    def mutate(data: dict[str, Any]) -> None:
        data["."]["glyph"] = "龍"

    _rewrite(pack / "terrain.json", mutate)
    with pytest.raises(WorldLoadError, match=r"terrain\.json.* exactly one column"):
        load_world(pack)


def test_fullwidth_terrain_glyph_rejected(tmp_path: Path) -> None:
    """A Fullwidth (EAW=F) Latin letter is two columns and is rejected."""
    pack = _clone_pack(tmp_path)

    def mutate(data: dict[str, Any]) -> None:
        data["."]["glyph"] = "Ａ"  # U+FF21 FULLWIDTH LATIN CAPITAL LETTER A

    _rewrite(pack / "terrain.json", mutate)
    with pytest.raises(WorldLoadError, match=r"terrain\.json.* exactly one column"):
        load_world(pack)


def test_reskinned_shipped_pack_glyphs() -> None:
    """The shipped pack carries the v0.6 re-skin and still loads cleanly.

    The load-bearing guard for the re-skin: water is ≋ and the three lettered
    buildings became ⌂/✚/∩. If a data edit reverts a glyph, this trips.
    """
    world = load_world(SHIPPED)
    waters = {
        world.terrain_at(x, y).glyph
        for y in range(world.height)
        for x in range(world.width)
        if world.terrain_at(x, y).key == "water"
    }
    assert waters == {"≋"}
    by_key = {loc.key: loc.glyph for loc in world.locations}
    assert by_key["inn"] == "⌂"
    assert by_key["healer"] == "✚"
    assert by_key["dungeon"] == "∩"
    assert by_key["shop"] == "$"  # the shop glyph is unchanged


def test_multichar_location_glyph_rejected(tmp_path: Path) -> None:
    """A location glyph must be exactly one character."""
    pack = _clone_pack(tmp_path)

    def mutate(data: dict[str, Any]) -> None:
        data["inn"]["glyph"] = "In"  # two characters

    _rewrite(pack / "locations.json", mutate)
    with pytest.raises(WorldLoadError, match=r"locations\.json.* single character"):
        load_world(pack)


def test_oversized_map_rejected(tmp_path: Path) -> None:
    """A 300x300 map is past the dimension ceiling (8..256)."""
    pack = _clone_pack(tmp_path)

    def mutate(data: dict[str, Any]) -> None:
        data["width"] = 300
        data["height"] = 300

    _rewrite(pack / "world.json", mutate)
    with pytest.raises(WorldLoadError, match=r"world\.json width = 300 is out of band"):
        load_world(pack)


def test_too_many_events_rejected(tmp_path: Path) -> None:
    """An event table over the 500-row cap is rejected before it is decoded."""
    pack = _clone_pack(tmp_path)

    def mutate(data: dict[str, Any]) -> None:
        filler = {"kind": "lore", "weight": 1, "text": "filler"}
        data["events"] = [filler.copy() for _ in range(501)]

    _rewrite(pack / "events.json", mutate)
    with pytest.raises(WorldLoadError, match=r"events\.json defines 501 events; the limit is 500"):
        load_world(pack)


def test_overlong_monster_name_rejected(tmp_path: Path) -> None:
    """A 49-character monster name is one past the 48-char display limit."""
    pack = _clone_pack(tmp_path)

    def mutate(data: list[dict[str, Any]]) -> None:
        data[0]["name"] = "x" * 49

    _rewrite(pack / "monsters.json", mutate)
    with pytest.raises(WorldLoadError, match=r"monsters\.json\[0\] name is 49 characters"):
        load_world(pack)
