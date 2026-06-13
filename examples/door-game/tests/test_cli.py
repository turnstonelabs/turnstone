"""Tests for the pack-authoring command surface.

Covers the validate/newpack functions directly (sound and broken packs, the
scaffold round-trip, AUTHORING.md generation from the live loader bands, and
the refuse-non-empty guard), the ``server.main`` argv dispatch (validate routes
through and bare invocation still reaches serve without binding a port), and
one end-to-end subprocess smoke of ``python -m understone validate``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from io import StringIO
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from understone import cli, server
from understone.world import loader

if TYPE_CHECKING:
    from collections.abc import Callable

EXAMPLE_DIR = Path(__file__).resolve().parents[1]
SHIPPED = EXAMPLE_DIR / "understone" / "world" / "data"

# The six content files a scaffolded pack must carry, plus the manual.
_PACK_JSONS = {
    "terrain.json",
    "monsters.json",
    "items.json",
    "locations.json",
    "events.json",
    "world.json",
}


# ---------------------------------------------------------------------------
# cli_validate
# ---------------------------------------------------------------------------


def test_cli_validate_sound_pack_reports_and_returns_zero() -> None:
    out, err = StringIO(), StringIO()
    rc = cli.cli_validate(SHIPPED, out=out, err=err)

    assert rc == 0
    report = out.getvalue()
    assert "This pack is sound. The door stands open." in report
    # The report surfaces the headline facts the brief calls for.
    assert "The Vale of Understone" in report
    assert "96x48" in report
    assert "1 boss" in report
    assert "% fight" in report
    assert err.getvalue() == ""


def test_cli_validate_broken_pack_names_field_and_returns_two(tmp_path: Path) -> None:
    # A pack whose daily_turns is out of band: the loader names the field.
    pack = _clone_shipped(tmp_path)
    _patch_world(pack, _break_daily_turns)

    out, err = StringIO(), StringIO()
    rc = cli.cli_validate(pack, out=out, err=err)

    assert rc == 2
    message = err.getvalue()
    assert message.startswith("The pack is flawed:")
    assert "daily_turns" in message  # the offending field is named
    assert out.getvalue() == ""


def test_cli_validate_missing_directory_returns_two(tmp_path: Path) -> None:
    out, err = StringIO(), StringIO()
    rc = cli.cli_validate(tmp_path / "nope", out=out, err=err)
    assert rc == 2
    assert "The pack is flawed:" in err.getvalue()


# ---------------------------------------------------------------------------
# cli_newpack
# ---------------------------------------------------------------------------


def test_cli_newpack_writes_template_and_manual(tmp_path: Path) -> None:
    dest = tmp_path / "mypack"
    out, err = StringIO(), StringIO()
    rc = cli.cli_newpack(dest, out=out, err=err)

    assert rc == 0
    present = {p.name for p in dest.iterdir()}
    assert present >= _PACK_JSONS  # the six content files are all there
    assert "AUTHORING.md" in present
    # Next-steps guidance points the author at the validate verb.
    assert "understone validate" in out.getvalue()


def test_cli_newpack_scaffold_validates(tmp_path: Path) -> None:
    """The load-bearing test: a freshly scaffolded pack loads cleanly.

    newpack -> load_world round-trip. If the template the scaffolder copies
    ever drifts out of the loader's bands, this fails immediately.
    """
    dest = tmp_path / "mypack"
    assert cli.cli_newpack(dest, out=StringIO(), err=StringIO()) == 0

    world = loader.load_world(dest)
    assert world.name == "The Vale of Understone"
    assert world.width == 96


def test_cli_newpack_authoring_md_renders_live_band(tmp_path: Path) -> None:
    """AUTHORING.md's bands are generated from the loader, not hand-copied.

    The daily_turns band is read straight from the live loader table and must
    appear verbatim in the scaffolded manual — proving generation from source.
    """
    dest = tmp_path / "mypack"
    cli.cli_newpack(dest, out=StringIO(), err=StringIO())

    manual = (dest / "AUTHORING.md").read_text(encoding="utf-8")
    lo, hi = loader.SETTINGS_BANDS["daily_turns"]
    assert lo is not None and hi is not None
    assert f"`{lo}..{hi}`" in manual
    assert "daily_turns" in manual


def test_cli_newpack_authoring_md_has_width_rule_and_live_palette(tmp_path: Path) -> None:
    """AUTHORING.md documents the one-column rule and renders the live palette.

    The width section states the Western-monospace assumption, and the safe
    palette is generated from ``textwidth.SAFE_PALETTE`` (same can't-drift
    pattern as the bands table) — every glyph appears, in a backticked cell.
    """
    from understone.engine.textwidth import SAFE_PALETTE

    dest = tmp_path / "mypack"
    cli.cli_newpack(dest, out=StringIO(), err=StringIO())
    manual = (dest / "AUTHORING.md").read_text(encoding="utf-8")

    assert "## Glyph width" in manual
    assert "exactly one terminal column" in manual
    assert "Western monospace" in manual  # the stated assumption
    assert "Safe glyph palette" in manual
    for glyph in SAFE_PALETTE:
        assert f"`{glyph}`" in manual, f"palette glyph {glyph!r} missing from manual"


def test_cli_newpack_authoring_md_documents_action_sets(tmp_path: Path) -> None:
    """AUTHORING.md documents each building's real verb menu.

    The per-building menus are an explicit table: the inn's `gamble` (v0.8) and
    the v0.10 vault verbs `deposit`/`withdraw`, the shop's `forge`, and so on.
    This pins the table rows and the "quaff anywhere" note so a doc regression
    trips.
    """
    dest = tmp_path / "mypack"
    cli.cli_newpack(dest, out=StringIO(), err=StringIO())
    manual = (dest / "AUTHORING.md").read_text(encoding="utf-8")

    assert "| `inn` | `rest`, `deposit`, `withdraw`, `gamble`, `leave` |" in manual
    assert "| `shop` | `buy`, `sell`, `forge`, `leave` |" in manual
    assert "| `healer` | `heal`, `leave` |" in manual
    assert "| `dungeon` | `descend`, `challenge`, `leave` |" in manual
    assert "`quaff`" in manual and "legal **anywhere**" in manual
    # The vault is described where its verbs are listed.
    assert "VAULT" in manual and "SAFE from ambush" in manual


def test_cli_newpack_authoring_md_documents_ore_forge(tmp_path: Path) -> None:
    """AUTHORING.md documents the v0.10 ore-gated forge: material slot + settings.

    The forge ore is a `material` item earned in combat; the four ore settings
    (item, per-plus, dungeon drop, forest chance) are documented, and the band
    figures are generated from the live loader so they cannot drift.
    """
    dest = tmp_path / "mypack"
    cli.cli_newpack(dest, out=StringIO(), err=StringIO())
    manual = (dest / "AUTHORING.md").read_text(encoding="utf-8")

    assert "`material`" in manual  # the new slot
    assert "forge_ore_item" in manual
    assert "ore_forest_chance" in manual  # the float setting (prose, not the band table)
    # The two banded ore settings carry their LIVE bands.
    lo, hi = loader.SETTINGS_BANDS["ore_dungeon_drop"]
    assert f"`{lo}..{hi}`" in manual
    assert "earns in combat" in manual or "earned in combat" in manual


def test_cli_newpack_authoring_md_states_color_advisory_and_spawn_walkable(
    tmp_path: Path,
) -> None:
    """AUTHORING.md states color is advisory (loader does not validate it) and
    that spawn must be on walkable terrain — both v0.8 honesty fixes."""
    dest = tmp_path / "mypack"
    cli.cli_newpack(dest, out=StringIO(), err=StringIO())
    manual = (dest / "AUTHORING.md").read_text(encoding="utf-8")

    # color is documented as advisory / not validated (it matches loader behaviour).
    assert "advisory and not validated" in manual
    # spawn's walkability requirement is now stated where spawn is introduced.
    assert "must be on walkable terrain" in manual


def test_cli_newpack_authoring_md_color_roles_generated_from_enum(tmp_path: Path) -> None:
    """AUTHORING.md's colour-role vocabulary is generated from the Color enum.

    The v0.9 fix: the assignable roles were hand-listed (and went stale — road
    and the per-building roles were missing). They are now generated from
    ``Color.assignable()`` — the single source for the overlay-vs-assignable
    split — so the manual lists exactly what the Watch can paint and cannot
    drift. This asserts the NEW roles appear, that every assignable enum role
    appears, and that the non-assignable roles (overlays + DEFAULT) are NOT
    offered as author-assignable.
    """
    from understone.screen.palette import Color

    dest = tmp_path / "mypack"
    cli.cli_newpack(dest, out=StringIO(), err=StringIO())
    manual = (dest / "AUTHORING.md").read_text(encoding="utf-8")

    # A sampling of the new v0.9 roles is offered in the manual, backticked.
    for role in ("road", "forest", "lava", "barren", "inn", "shop", "healer"):
        assert f"`{role}`" in manual, f"new colour role {role!r} missing from manual"

    # EVERY assignable enum role appears (generated, so the full set is present).
    color_section = manual[manual.index("`color` — a palette role string") :].split("###", 1)[0]
    for role in Color.assignable():
        assert f"`{role.value}`" in manual, f"assignable role {role.value!r} missing from manual"

    # The non-assignable roles (runtime overlays + the DEFAULT fallback) are NOT
    # offered as terrain/location colours.
    non_assignable = {c for c in Color} - set(Color.assignable())
    assert Color.DEFAULT in non_assignable  # the fallback is not author-pickable
    for role in non_assignable:
        assert f"`{role.value}`" not in color_section, (
            f"non-assignable role {role.value!r} wrongly offered as author-assignable"
        )


def test_cli_newpack_authoring_md_has_validate_coverage_split(tmp_path: Path) -> None:
    """AUTHORING.md honestly separates machine-enforced rules from eyeball-only.

    The v0.8 subsection lists what `validate` DOES catch (including the two new
    enforcements — rare-as-guardian and single-boss) and what it does NOT (chief
    among them: location menu `actions` contents are unvalidated).
    """
    dest = tmp_path / "mypack"
    cli.cli_newpack(dest, out=StringIO(), err=StringIO())
    manual = (dest / "AUTHORING.md").read_text(encoding="utf-8")

    assert "What `validate` checks, and what it cannot" in manual
    # The newly-enforced rules are named in the DOES-catch list.
    assert "Exactly one boss" in manual
    assert "fixed rung guardian) must" in manual  # rare-as-guardian enforcement
    # The eyeball-only short list names the actions gap and the flavour caveat.
    assert "Location menu `actions` contents" in manual
    assert "Flavour and narration quality" in manual


def test_cli_newpack_refuses_non_empty_dir(tmp_path: Path) -> None:
    dest = tmp_path / "occupied"
    dest.mkdir()
    (dest / "keep.txt").write_text("mine", encoding="utf-8")

    out, err = StringIO(), StringIO()
    rc = cli.cli_newpack(dest, out=out, err=err)

    assert rc == 2
    assert "non-empty" in err.getvalue()
    # The pre-existing file is untouched (nothing was scaffolded over it).
    assert (dest / "keep.txt").read_text(encoding="utf-8") == "mine"
    assert not (dest / "AUTHORING.md").exists()


def test_cli_newpack_into_empty_existing_dir_succeeds(tmp_path: Path) -> None:
    """An existing but empty directory is a fine scaffold target."""
    dest = tmp_path / "empty"
    dest.mkdir()
    assert cli.cli_newpack(dest, out=StringIO(), err=StringIO()) == 0
    assert (dest / "AUTHORING.md").exists()


# ---------------------------------------------------------------------------
# server.main argv dispatch
# ---------------------------------------------------------------------------


def test_main_validate_dispatch_returns_status(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    # A broken pack routed through main exits 2; a sound one exits 0.
    pack = _clone_shipped(tmp_path)
    _patch_world(pack, _break_daily_turns)

    with pytest.raises(SystemExit) as broken:
        server.main(["validate", str(pack)])
    assert broken.value.code == 2

    with pytest.raises(SystemExit) as sound:
        server.main(["validate", str(SHIPPED)])
    assert sound.value.code == 0
    assert "The door stands open." in capsys.readouterr().out


def test_main_newpack_dispatch(tmp_path: Path) -> None:
    dest = tmp_path / "viamain"
    with pytest.raises(SystemExit) as exc:
        server.main(["newpack", str(dest)])
    assert exc.value.code == 0
    assert (dest / "AUTHORING.md").exists()


def test_main_worlds_dispatch(capsys: pytest.CaptureFixture) -> None:
    """`understone worlds` routes through main, exits 0, and lists the Vale."""
    with pytest.raises(SystemExit) as exc:
        server.main(["worlds"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "vale" in out
    assert "The Vale of Understone" in out
    assert "UNDERSTONE_WORLD=" in out


def test_bare_invocation_resolves_to_serve_without_side_effects() -> None:
    """Parsing no argv yields the serve path, and parsing has no side effects.

    The transport launch (_serve) is reachable, but argument parsing neither
    loads a world nor binds a port — so this asserts the resolved command
    without ever calling _serve.
    """
    args = server._build_parser().parse_args([])
    assert args.cmd is None  # None => the serve branch in main()
    assert callable(server._serve)


def test_subprocess_validate_packaged_world_exits_zero() -> None:
    """End-to-end smoke: `python -m understone validate <packaged dir>` exits 0."""
    result = subprocess.run(
        [sys.executable, "-m", "understone", "validate", str(SHIPPED)],
        cwd=EXAMPLE_DIR,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "The door stands open." in result.stdout


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _clone_shipped(tmp_path: Path) -> Path:
    dest = tmp_path / "pack"
    shutil.copytree(SHIPPED, dest)
    return dest


def _patch_world(pack: Path, mutate: Callable[[dict[str, Any]], None]) -> None:
    path = pack / "world.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    mutate(data)
    path.write_text(json.dumps(data), encoding="utf-8")


def _break_daily_turns(data: dict[str, Any]) -> None:
    """Set daily_turns out of its 1..100 band so the pack fails to load."""
    data["settings"]["daily_turns"] = 0
