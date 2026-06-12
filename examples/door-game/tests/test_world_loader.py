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
    assert {m.tier for m in world.monsters} == {1, 2, 3, 4, 5}


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
