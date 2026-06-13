"""Tests for the per-pack Watch CRT theme (v0.8).

Covers the loader band (each of the four legal themes loads; an unknown theme
is rejected naming the legal set; an omitted theme defaults to phosphor), the
state-payload carrying the theme, and the WATCH_HTML page's JS THEME table —
including the load-bearing guard that the "phosphor" values byte-match the
original ``:root`` CSS, so the bundled Vale stays visually identical.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from understone import watch
from understone.errors import WorldLoadError
from understone.world.loader import (
    DEFAULT_WATCH_THEME,
    WATCH_THEMES,
    load_world,
)

SHIPPED = Path(__file__).resolve().parents[1] / "understone" / "world" / "data"

# The original :root CRT custom-property values (pre-v0.8). The "phosphor" theme
# MUST reproduce these byte-for-byte so the default Vale is pixel-identical.
_ORIGINAL_ROOT = {
    "--phosphor": "#7dffa0",
    "--phosphor-dim": "#2f7a46",
    "--amber": "#ffb44d",
    "--bg": "#050a06",
    "--panel": "#0a140d",
    "--edge": "#163a22",
}


def _pack_with_theme(tmp_path: Path, theme: Any) -> Path:
    """Clone the Vale into a temp pack with ``settings.watch_theme`` set/removed.

    ``theme`` set to a string writes that value; set to the sentinel ``...``
    DELETES the key entirely (to exercise the omitted-defaults path).
    """
    dest = tmp_path / "themed"
    shutil.copytree(SHIPPED, dest)
    world_json = dest / "world.json"
    data = json.loads(world_json.read_text(encoding="utf-8"))
    if theme is ...:
        data["settings"].pop("watch_theme", None)
    else:
        data["settings"]["watch_theme"] = theme
    world_json.write_text(json.dumps(data), encoding="utf-8")
    return dest


# ---------------------------------------------------------------------------
# loader band
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("theme", sorted(WATCH_THEMES))
def test_each_legal_theme_loads(tmp_path: Path, theme: str) -> None:
    pack = _pack_with_theme(tmp_path, theme)
    world = load_world(pack)
    assert world.settings.watch_theme == theme


def test_unknown_theme_rejected_naming_the_set(tmp_path: Path) -> None:
    pack = _pack_with_theme(tmp_path, "ultraviolet")
    with pytest.raises(WorldLoadError) as exc:
        load_world(pack)
    message = str(exc.value)
    assert "watch_theme" in message
    assert "ultraviolet" in message
    # The friendly message lists every legal theme so the author can fix it.
    for name in WATCH_THEMES:
        assert name in message


def test_omitted_theme_defaults_to_phosphor(tmp_path: Path) -> None:
    pack = _pack_with_theme(tmp_path, ...)  # delete the key entirely
    world = load_world(pack)
    assert world.settings.watch_theme == DEFAULT_WATCH_THEME == "phosphor"


def test_shipped_vale_is_phosphor() -> None:
    """The bundled Vale ships the phosphor theme (its green is unchanged)."""
    world = load_world(SHIPPED)
    assert world.settings.watch_theme == "phosphor"


# ---------------------------------------------------------------------------
# payload + WATCH_HTML
# ---------------------------------------------------------------------------


def test_world_payload_carries_theme(tmp_path: Path) -> None:
    pack = _pack_with_theme(tmp_path, "ice")
    world = load_world(pack)
    payload = watch.build_world_payload(world)
    assert payload["theme"] == "ice"


def test_shipped_payload_theme_is_phosphor() -> None:
    world = load_world(SHIPPED)
    payload = watch.build_world_payload(world)
    assert payload["theme"] == "phosphor"


def test_watch_html_has_theme_table_and_all_names() -> None:
    """The page carries a JS THEME table keyed by every legal theme name."""
    html = watch.WATCH_HTML
    assert "var THEMES" in html
    assert "applyTheme" in html
    for name in WATCH_THEMES:
        # Each theme is a JS object key, e.g. ``phosphor: {``.
        assert f"{name}: {{" in html, f"theme {name!r} missing from THEME table"


def test_watch_html_phosphor_values_byte_match_original_root() -> None:
    """The "phosphor" theme reproduces the original :root values exactly.

    This is the load-bearing guard for "the Vale looks identical": every
    original custom-property value still appears in the page (in the :root block
    AND the THEME table), so swapping in the phosphor theme is a no-op repaint.
    """
    html = watch.WATCH_HTML
    for prop, value in _ORIGINAL_ROOT.items():
        # The value lives both in the :root CSS and the phosphor theme entry.
        assert html.count(value) >= 2, f"{prop} value {value} not byte-matched twice"
        # And the phosphor theme maps the property to exactly that value.
        assert f'"{prop}": "{value}"' in html, f"phosphor {prop} != {value}"


def test_watch_html_applies_theme_on_world_fetch() -> None:
    """The page applies the theme when world.json arrives (in paintMap)."""
    html = watch.WATCH_HTML
    assert "applyTheme(world.theme)" in html
    # It swaps CSS custom properties on the document root.
    assert "documentElement.style.setProperty" in html
