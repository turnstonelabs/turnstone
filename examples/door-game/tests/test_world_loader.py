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
    with pytest.raises(WorldLoadError, match=r"dungeon_tiers\[1\] = 9 has no non-boss monster"):
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
    The message says "no NON-boss monster" (not merely "no monster"): the boss
    is present at that tier, it just cannot fill a rung, and the wording must
    point the author at exactly that.
    """
    pack = _clone_pack(tmp_path)

    def mutate(data: dict[str, Any]) -> None:
        data["settings"]["dungeon_tiers"] = [4, 6]  # 6 is the boss-only tier

    _rewrite(pack / "world.json", mutate)
    with pytest.raises(WorldLoadError, match=r"dungeon_tiers\[1\] = 6 has no non-boss monster"):
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


# ---------------------------------------------------------------------------
# v0.7 loader rejections: the satchel/forge bands, rare_drop_item, monster weight
# ---------------------------------------------------------------------------


def test_rare_drop_item_unknown_rejected(tmp_path: Path) -> None:
    """A rare_drop_item that names no item is rejected with the item-id message."""
    pack = _clone_pack(tmp_path)

    def mutate(data: dict[str, Any]) -> None:
        data["settings"]["rare_drop_item"] = "no_such_draught"

    _rewrite(pack / "world.json", mutate)
    with pytest.raises(WorldLoadError, match="rare_drop_item = 'no_such_draught' is not a known"):
        load_world(pack)


def test_rare_drop_item_non_consumable_rejected(tmp_path: Path) -> None:
    """A rare_drop_item that names a weapon (not a consumable) is rejected.

    The drop goes straight into the satchel to be quaffed, so a weapon or
    armour id is incoherent — the loader pins the slot.
    """
    pack = _clone_pack(tmp_path)

    def mutate(data: dict[str, Any]) -> None:
        data["settings"]["rare_drop_item"] = "iron_sword"  # a weapon, not a draught

    _rewrite(pack / "world.json", mutate)
    with pytest.raises(WorldLoadError, match="rare_drop_item = 'iron_sword' must be a consumable"):
        load_world(pack)


def test_satchel_max_out_of_band_rejected(tmp_path: Path) -> None:
    """satchel_max above its 1..10 band is a load error."""
    pack = _clone_pack(tmp_path)

    def mutate(data: dict[str, Any]) -> None:
        data["settings"]["satchel_max"] = 11

    _rewrite(pack / "world.json", mutate)
    with pytest.raises(WorldLoadError, match=r"satchel_max = 11 is out of band \(1\.\.10\)"):
        load_world(pack)


def test_forge_max_plus_out_of_band_rejected(tmp_path: Path) -> None:
    """forge_max_plus above its 0..10 band is a load error."""
    pack = _clone_pack(tmp_path)

    def mutate(data: dict[str, Any]) -> None:
        data["settings"]["forge_max_plus"] = 11

    _rewrite(pack / "world.json", mutate)
    with pytest.raises(WorldLoadError, match=r"forge_max_plus = 11 is out of band \(0\.\.10\)"):
        load_world(pack)


def test_forge_base_cost_out_of_band_rejected(tmp_path: Path) -> None:
    """forge_base_cost below its floor of 1 is a load error."""
    pack = _clone_pack(tmp_path)

    def mutate(data: dict[str, Any]) -> None:
        data["settings"]["forge_base_cost"] = 0

    _rewrite(pack / "world.json", mutate)
    with pytest.raises(WorldLoadError, match=r"forge_base_cost = 0 is out of band \(1\.\.10000\)"):
        load_world(pack)


def test_forge_ore_item_unknown_rejected(tmp_path: Path) -> None:
    """A forge_ore_item that names no item is rejected with the item-id message."""
    pack = _clone_pack(tmp_path)

    def mutate(data: dict[str, Any]) -> None:
        data["settings"]["forge_ore_item"] = "no_such_ore"

    _rewrite(pack / "world.json", mutate)
    with pytest.raises(WorldLoadError, match="forge_ore_item = 'no_such_ore' is not a known"):
        load_world(pack)


def test_forge_ore_item_non_material_rejected(tmp_path: Path) -> None:
    """A forge_ore_item that names a non-material (a potion) is rejected.

    Ore is carried in the satchel and spent at the forge, never equipped or
    quaffed, so a consumable/weapon/armour id is incoherent — the loader pins
    the slot to ``material`` (mirroring the rare_drop_item consumable check).
    """
    pack = _clone_pack(tmp_path)

    def mutate(data: dict[str, Any]) -> None:
        data["settings"]["forge_ore_item"] = "greater_potion"  # a draught, not ore

    _rewrite(pack / "world.json", mutate)
    with pytest.raises(
        WorldLoadError, match="forge_ore_item = 'greater_potion' must be a material"
    ):
        load_world(pack)


def test_forge_ore_per_plus_out_of_band_rejected(tmp_path: Path) -> None:
    """forge_ore_per_plus above its 0..10 band is a load error."""
    pack = _clone_pack(tmp_path)

    def mutate(data: dict[str, Any]) -> None:
        data["settings"]["forge_ore_per_plus"] = 11

    _rewrite(pack / "world.json", mutate)
    with pytest.raises(WorldLoadError, match=r"forge_ore_per_plus = 11 is out of band \(0\.\.10\)"):
        load_world(pack)


def test_ore_dungeon_drop_out_of_band_rejected(tmp_path: Path) -> None:
    """ore_dungeon_drop above its 0..20 band is a load error."""
    pack = _clone_pack(tmp_path)

    def mutate(data: dict[str, Any]) -> None:
        data["settings"]["ore_dungeon_drop"] = 21

    _rewrite(pack / "world.json", mutate)
    with pytest.raises(WorldLoadError, match=r"ore_dungeon_drop = 21 is out of band \(0\.\.20\)"):
        load_world(pack)


def test_ore_forest_chance_out_of_band_rejected(tmp_path: Path) -> None:
    """ore_forest_chance outside 0.0..1.0 is a load error (it is a probability)."""
    pack = _clone_pack(tmp_path)

    def mutate(data: dict[str, Any]) -> None:
        data["settings"]["ore_forest_chance"] = 1.5

    _rewrite(pack / "world.json", mutate)
    with pytest.raises(
        WorldLoadError, match=r"ore_forest_chance = 1.5 is out of band \(0.0..1.0\)"
    ):
        load_world(pack)


def test_monster_zero_weight_rejected(tmp_path: Path) -> None:
    """A monster weight of 0 is rejected (the weighted pick needs a positive total)."""
    pack = _clone_pack(tmp_path)

    def mutate(data: list[dict[str, Any]]) -> None:
        data[0]["weight"] = 0

    _rewrite(pack / "monsters.json", mutate)
    with pytest.raises(WorldLoadError, match=r"monsters\.json\[0\] weight must be > 0"):
        load_world(pack)


def test_shipped_pack_carries_rares_and_weights() -> None:
    """The shipped pack parses the v0.7 rare beasts with their low weights."""
    world = load_world(SHIPPED)
    rares = [m for m in world.monsters if m.rare]
    names = {m.name for m in rares}
    assert names == {"the Gilded Stag", "the Hollow Knight"}
    assert all(m.weight == 1 for m in rares)  # rares surface seldom
    # The rare_drop_item resolves to a consumable.
    drop = world.item_by_id(world.settings.rare_drop_item)
    assert drop is not None and drop.slot.value == "consumable"
    # The new economy settings land on their shipped values.
    assert world.settings.satchel_max == 3
    assert world.settings.forge_base_cost == 60
    assert world.settings.forge_max_plus == 3
    assert world.settings.dungeon_tiers == (3, 4, 5)
    # v0.10 ore-forge settings resolve, and the forge ore is a material item.
    assert world.settings.forge_ore_item == "iron_ore"
    ore = world.item_by_id(world.settings.forge_ore_item)
    assert ore is not None and ore.slot.value == "material"
    assert world.settings.forge_ore_per_plus == 1
    assert world.settings.ore_dungeon_drop == 2
    assert world.settings.ore_forest_chance == 0.2


def test_monster_weight_and_rare_default_when_omitted(tmp_path: Path) -> None:
    """A monster spec without weight/rare loads as weight 10, rare False.

    Both fields are optional with defaults, so an unannotated common monster
    (the shipped Field Rat) parses to the default weight and the non-rare flag.
    """
    world = load_world(SHIPPED)
    rat = next(m for m in world.monsters if m.name == "Field Rat")
    assert rat.weight == 10  # the default biasing weight
    assert rat.rare is False


# ---------------------------------------------------------------------------
# v0.8 loader hardening: rare-as-rung-guardian and the single-boss invariant
#
# AUTHORING states both as rules; v0.8 makes them machine-checked. A rare in
# the lead slot of a dungeon tier would be silently promoted to a fixed rung
# guardian (and pulled from the rare pool); a stray second boss would validate
# clean yet make "the one endgame foe" a lie.
# ---------------------------------------------------------------------------


def test_rare_as_first_dungeon_tier_monster_rejected(tmp_path: Path) -> None:
    """A rare in the FIRST slot of a dungeon tier becomes a fixed guardian — rejected.

    Tier 3 backs a ``dungeon_tiers`` rung and its first monster (the Forest
    Wolf) is the rung guardian (``band[0]``). Flagging that lead monster rare
    would quietly turn the rare into the fixed, repeatable guardian and remove
    it from the weighted rare roll, so the loader rejects it by name.
    """
    pack = _clone_pack(tmp_path)

    def mutate(data: list[dict[str, Any]]) -> None:
        wolf = next(m for m in data if m["name"] == "Forest Wolf")  # first tier-3
        wolf["rare"] = True

    _rewrite(pack / "monsters.json", mutate)
    with pytest.raises(
        WorldLoadError,
        match=r"'Forest Wolf' is rare but is the first tier-3 monster.*fixed guardian",
    ):
        load_world(pack)


def test_rare_after_guardian_in_dungeon_tier_accepted(tmp_path: Path) -> None:
    """A rare placed AFTER the guardian in the same dungeon tier loads cleanly.

    The shipped pack already does exactly this (the Hollow Knight is the third
    tier-3 entry, behind the Forest Wolf guardian). Inserting another rare also
    after the guardian must not trip the new check — only the LEAD slot of a
    dungeon tier is constrained.
    """
    pack = _clone_pack(tmp_path)

    def mutate(data: list[dict[str, Any]]) -> None:
        # Splice a second tier-3 rare in just before the boss (well after the
        # tier-3 guardian), so the tier's first non-boss monster is unchanged.
        extra = {
            "tier": 3,
            "name": "the Ashen Stalker",
            "hp": 26,
            "atk": 10,
            "def": 3,
            "xp": 55,
            "gold": 75,
            "weight": 1,
            "rare": True,
        }
        data.insert(len(data) - 1, extra)

    _rewrite(pack / "monsters.json", mutate)
    world = load_world(pack)  # no WorldLoadError: the rare is not the lead foe
    tier3 = world.monsters_for_tier_band(3, 3)
    assert tier3[0].name == "Forest Wolf"  # the guardian is still the non-rare lead
    assert any(m.name == "the Ashen Stalker" and m.rare for m in tier3)


def test_two_bosses_rejected(tmp_path: Path) -> None:
    """Two ``boss``-flagged monsters are rejected: a world has exactly one boss."""
    pack = _clone_pack(tmp_path)

    def mutate(data: list[dict[str, Any]]) -> None:
        # Give the Field Rat the boss flag too; now two monsters claim the role.
        rat = next(m for m in data if m["name"] == "Field Rat")
        rat["boss"] = True
        rat["id"] = "field_rat"

    _rewrite(pack / "monsters.json", mutate)
    with pytest.raises(WorldLoadError, match=r"flags 2 monsters as .boss.* true"):
        load_world(pack)


def test_single_boss_accepted() -> None:
    """The shipped pack carries exactly one boss and loads — the single-boss path.

    The positive half of the invariant: the Wyrm Below is the only boss, so the
    load succeeds and the boss count is exactly one.
    """
    world = load_world(SHIPPED)
    bosses = [m for m in world.monsters if m.boss]
    assert len(bosses) == 1
    assert bosses[0].name == "the Wyrm Below"
