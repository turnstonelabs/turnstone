"""Screen-layer tests: viewport maths, frame rendering, menu rendering.

Golden discipline: the golden files under ``tests/golden`` are authored by
hand (correct borders/centring, eyeballed) and are NOT machine-dumped
renderer output. Every golden comparison is paired with structural asserts
that hold independent of the exact golden bytes, so a renderer regression
that happens to match a stale golden still trips a structural check.
"""

from __future__ import annotations

from pathlib import Path

from understone.screen.grid import Cell, CellGrid
from understone.screen.menus import render_menu
from understone.screen.palette import Color
from understone.screen.text_renderer import render_frame
from understone.screen.viewport import compute_window

GOLDEN = Path(__file__).parent / "golden"


# ---------------------------------------------------------------------------
# viewport.compute_window
# ---------------------------------------------------------------------------


def test_window_centers_when_interior() -> None:
    # 100x100 map, 48x16 view, focus at (50, 50): centred.
    x0, y0 = compute_window(100, 100, 48, 16, 50, 50)
    assert x0 == 50 - 48 // 2
    assert y0 == 50 - 16 // 2


def test_window_clamps_nw_corner() -> None:
    x0, y0 = compute_window(100, 100, 48, 16, 0, 0)
    assert (x0, y0) == (0, 0)


def test_window_clamps_ne_corner() -> None:
    x0, y0 = compute_window(100, 100, 48, 16, 99, 0)
    assert x0 == 100 - 48
    assert y0 == 0


def test_window_clamps_sw_corner() -> None:
    x0, y0 = compute_window(100, 100, 48, 16, 0, 99)
    assert x0 == 0
    assert y0 == 100 - 16


def test_window_clamps_se_corner() -> None:
    x0, y0 = compute_window(100, 100, 48, 16, 99, 99)
    assert x0 == 100 - 48
    assert y0 == 100 - 16


def test_window_view_larger_than_map_pins_origin() -> None:
    x0, y0 = compute_window(10, 8, 48, 16, 5, 4)
    assert (x0, y0) == (0, 0)


# ---------------------------------------------------------------------------
# Shared small-grid builders for the golden frames
# ---------------------------------------------------------------------------

_FLOOR = Cell(".", Color.FLOOR)
_PLAYER = Cell("@", Color.PLAYER)


def _floor_grid(rows: int, cols: int) -> CellGrid:
    grid = CellGrid(rows, cols)
    for r in range(rows):
        for c in range(cols):
            grid.set(r, c, _FLOOR)
    return grid


def _spawn_grid() -> CellGrid:
    """9x5 floor with the player centred at (row 2, col 4)."""
    grid = _floor_grid(5, 9)
    grid.set(2, 4, _PLAYER)
    return grid


def _edge_nw_grid() -> CellGrid:
    """9x5 floor with the player pinned to the NW corner (row 0, col 0)."""
    grid = _floor_grid(5, 9)
    grid.set(0, 0, _PLAYER)
    return grid


# ---------------------------------------------------------------------------
# text_renderer.render_frame
# ---------------------------------------------------------------------------


def test_render_frame_matches_golden_spawn() -> None:
    frame = render_frame(_spawn_grid(), title="Vale", status="[ status ]")
    expected = (GOLDEN / "viewport_spawn.txt").read_text(encoding="utf-8")
    assert frame == expected.rstrip("\n")


def test_render_frame_matches_golden_edge_nw() -> None:
    frame = render_frame(_edge_nw_grid(), title="Vale", status="[ status ]")
    expected = (GOLDEN / "viewport_edge_nw.txt").read_text(encoding="utf-8")
    assert frame == expected.rstrip("\n")


def test_render_frame_structural_invariants() -> None:
    frame = render_frame(_spawn_grid(), title="Vale", status="[ status ]")
    lines = frame.split("\n")
    # Top border, 5 grid rows, bottom border, status = 8 lines.
    assert len(lines) == 8
    # Title substring lives in the top border.
    assert "Vale" in lines[0]
    # Uniform width across the box (top border through bottom border).
    box_lines = lines[:-1]
    widths = {len(line) for line in box_lines}
    assert len(widths) == 1, f"box rows ragged: {widths}"
    # Exactly one '@' and it sits at the centre column of the interior.
    body = lines[1:-2]
    at_positions = [(r, line.index("@")) for r, line in enumerate(body) if "@" in line]
    assert len(at_positions) == 1
    _, col = at_positions[0]
    # Interior centre: 1 (left border) + cols//2 = 1 + 4 = 5.
    assert col == 1 + 9 // 2
    # Status line is preserved verbatim as the last line.
    assert lines[-1] == "[ status ]"


def test_render_frame_under_size_budget() -> None:
    grid = _floor_grid(16, 48)
    grid.set(8, 24, _PLAYER)
    frame = render_frame(grid, title="The Vale of Understone", status="[ a long status line here ]")
    assert len(frame) < 2048


# ---------------------------------------------------------------------------
# menus.render_menu
# ---------------------------------------------------------------------------


def test_render_menu_matches_golden_inn() -> None:
    menu = render_menu(
        "The Sleeping Drake",
        ["A warm hearth crackles.", "A bed costs 15 gold."],
        ["(R)est", "(L)eave"],
        "[ status ]",
    )
    expected = (GOLDEN / "menu_inn.txt").read_text(encoding="utf-8")
    assert menu == expected.rstrip("\n")


def test_render_menu_structural_invariants() -> None:
    menu = render_menu(
        "The Sleeping Drake",
        ["A warm hearth crackles.", "A bed costs 15 gold."],
        ["(R)est", "(L)eave"],
        "[ status ]",
    )
    lines = menu.split("\n")
    assert "The Sleeping Drake" in lines[0]
    assert lines[-1] == "[ status ]"
    box = lines[:-1]
    widths = {len(line) for line in box}
    assert len(widths) == 1, f"menu box ragged: {widths}"
    # Option line is present inside the body.
    assert any("(R)est" in line and "(L)eave" in line for line in lines)
