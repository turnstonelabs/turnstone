"""Tests for bundled-world discovery and the ``worlds`` listing.

Covers the discovery helper (the Vale leads, alternate packs follow
alphabetically, non-pack directories are skipped) and the ``cli_worlds``
listing it backs: a sound fixture pack reports "sound", a deliberately-flawed
fixture pack reports "flawed", and the Vale is always listed first. The
``packs/`` directory is monkeypatched to a temp fixture tree so these tests
never depend on the real (separately-authored) second world.
"""

from __future__ import annotations

import json
import shutil
from io import StringIO
from pathlib import Path
from typing import TYPE_CHECKING, Any

from understone import cli
from understone import world as world_pkg
from understone.world import VALE_SLUG, bundled_world_dirs

if TYPE_CHECKING:
    import pytest

SHIPPED = Path(__file__).resolve().parents[1] / "understone" / "world" / "data"


def _make_packs(tmp_path: Path, *, sound: list[str], flawed: dict[str, Any]) -> Path:
    """Build a temp ``packs/`` tree: sound slugs plus flawed-world slugs.

    Each sound slug is a verbatim copy of the shipped Vale; each flawed slug is
    a copy whose ``world.json`` is patched with the given settings overrides so
    it fails to load. Returns the packs root to monkeypatch ``PACKS_DIR`` onto.
    """
    packs = tmp_path / "packs"
    packs.mkdir()
    for slug in sound:
        shutil.copytree(SHIPPED, packs / slug)
    for slug, overrides in flawed.items():
        dest = packs / slug
        shutil.copytree(SHIPPED, dest)
        world_json = dest / "world.json"
        data = json.loads(world_json.read_text(encoding="utf-8"))
        data["settings"].update(overrides)
        world_json.write_text(json.dumps(data), encoding="utf-8")
    return packs


# ---------------------------------------------------------------------------
# bundled_world_dirs discovery
# ---------------------------------------------------------------------------


def test_bundled_world_dirs_vale_leads_then_alpha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    packs = _make_packs(tmp_path, sound=["zephyr", "ashfall"], flawed={})
    monkeypatch.setattr(world_pkg, "PACKS_DIR", packs)

    found = bundled_world_dirs()
    slugs = [slug for slug, _ in found]
    # The Vale is always first; alternates follow alphabetically.
    assert slugs == [VALE_SLUG, "ashfall", "zephyr"]
    # The Vale entry points at the packaged data dir, not a packs subdir.
    assert found[0][1] == world_pkg.PACKAGED_WORLD_DIR


def test_bundled_world_dirs_skips_non_pack_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    packs = _make_packs(tmp_path, sound=["real"], flawed={})
    # A README placeholder and a directory with no world.json are NOT worlds.
    (packs / "README.md").write_text("placeholder", encoding="utf-8")
    (packs / "empty_dir").mkdir()
    monkeypatch.setattr(world_pkg, "PACKS_DIR", packs)

    slugs = [slug for slug, _ in bundled_world_dirs()]
    assert slugs == [VALE_SLUG, "real"]


def test_bundled_world_dirs_handles_absent_packs_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing packs/ directory yields just the Vale (never raises)."""
    monkeypatch.setattr(world_pkg, "PACKS_DIR", tmp_path / "does_not_exist")
    found = bundled_world_dirs()
    assert [slug for slug, _ in found] == [VALE_SLUG]


# ---------------------------------------------------------------------------
# cli_worlds listing
# ---------------------------------------------------------------------------


def test_cli_worlds_lists_vale_sound_first(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(world_pkg, "PACKS_DIR", tmp_path / "empty")
    out, err = StringIO(), StringIO()
    rc = cli.cli_worlds(out=out, err=err)

    assert rc == 0
    text = out.getvalue()
    lines = [ln for ln in text.splitlines() if ln.strip()]
    # The very first listing line is the Vale, reported sound, with its size.
    assert lines[0].split()[0] == VALE_SLUG
    assert "The Vale of Understone" in lines[0]
    assert "96x48" in lines[0]
    assert "sound" in lines[0]
    # The serve hint closes the listing.
    assert "UNDERSTONE_WORLD=" in text
    assert "the default Vale needs no setting" in text


def test_cli_worlds_reports_sound_alternate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    packs = _make_packs(tmp_path, sound=["mirefen"], flawed={})
    monkeypatch.setattr(world_pkg, "PACKS_DIR", packs)
    out = StringIO()
    cli.cli_worlds(out=out)

    text = out.getvalue()
    line = next(ln for ln in text.splitlines() if ln.strip().startswith("mirefen"))
    assert "sound" in line
    assert "flawed" not in line


def test_cli_worlds_flags_flawed_alternate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # daily_turns 0 is out of its 1..100 band: the pack fails to load.
    packs = _make_packs(tmp_path, sound=["sound_one"], flawed={"broken": {"daily_turns": 0}})
    monkeypatch.setattr(world_pkg, "PACKS_DIR", packs)
    out = StringIO()
    rc = cli.cli_worlds(out=out)

    assert rc == 0  # a flawed pack is reported, never fatal
    text = out.getvalue()
    broken_line = next(ln for ln in text.splitlines() if ln.strip().startswith("broken"))
    assert "flawed:" in broken_line
    assert "daily_turns" in broken_line  # the offending field surfaces
    # The sound pack alongside it still reports sound — one bad pack doesn't
    # poison the survey.
    sound_line = next(ln for ln in text.splitlines() if ln.strip().startswith("sound_one"))
    assert "sound" in sound_line


def test_cli_worlds_vale_sorts_before_flawed_alternate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even with an alphabetically-earlier flawed pack, the Vale leads."""
    packs = _make_packs(tmp_path, sound=[], flawed={"aaa_broken": {"start_hp": 0}})
    monkeypatch.setattr(world_pkg, "PACKS_DIR", packs)
    out = StringIO()
    cli.cli_worlds(out=out)

    lines = [ln for ln in out.getvalue().splitlines() if ln.strip()]
    assert lines[0].split()[0] == VALE_SLUG
    assert lines[1].strip().startswith("aaa_broken")
    assert "flawed:" in lines[1]


# ---------------------------------------------------------------------------
# the REAL bundled alternate world (no monkeypatch): The Cinder Wastes
#
# The tests above stub PACKS_DIR to a fixture tree so they never depend on the
# separately-authored pack. These two exercise the actual shipped packs/ — the
# bundled Cinder Wastes must discover, load, validate, and appear in the listing
# as sound, so a broken or unbundled alternate trips here.
# ---------------------------------------------------------------------------

CINDER = Path(__file__).resolve().parents[1] / "understone" / "world" / "packs" / "cinder-wastes"


def test_bundled_cinder_wastes_loads_and_validates() -> None:
    """The bundled Cinder Wastes loads through the (strict v0.8) loader cleanly.

    It is LLM-authored from AUTHORING.md alone, so this is the dogfood proof
    that the manual + validator produce a pack the real loader accepts — and,
    after v0.8, one that passes the stricter rare-as-guardian and single-boss
    checks (its rares sit after their guardians; it has exactly one boss).
    """
    from understone.world.loader import load_world

    world = load_world(CINDER)
    assert world.name == "The Cinder Wastes"
    assert world.settings.watch_theme == "ember"  # the thematic ember CRT palette
    bosses = [m for m in world.monsters if m.boss]
    assert len(bosses) == 1 and bosses[0].name == "the Magma Wyrm"
    # The boss id resolves and is the declared endgame foe.
    assert world.settings.boss_monster == "magma_wyrm"


def test_cli_worlds_lists_bundled_cinder_wastes_sound() -> None:
    """`understone worlds` discovers the real bundled Cinder Wastes as sound.

    No monkeypatch: this runs against the actual packs/ directory, so it asserts
    the genuinely-shipped second world appears in the listing (alongside the
    fixture-based listing tests above, which stay).
    """
    out = StringIO()
    rc = cli.cli_worlds(out=out)

    assert rc == 0
    line = next(ln for ln in out.getvalue().splitlines() if ln.strip().startswith("cinder-wastes"))
    assert "The Cinder Wastes" in line
    assert "sound" in line
    assert "flawed" not in line
