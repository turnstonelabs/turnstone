"""A 2-D grid of single-glyph cells.

The grid is the renderer's input surface: callers paint terrain and actors
into cells, then hand the grid to ``text_renderer`` for framing.
"""

from __future__ import annotations

from dataclasses import dataclass

from understone.screen.palette import Color


@dataclass(frozen=True, slots=True)
class Cell:
    """A single rendered position: exactly one glyph plus a colour role."""

    glyph: str
    color: Color

    def __post_init__(self) -> None:
        if len(self.glyph) != 1:
            raise ValueError(f"cell glyph must be exactly one character, got {self.glyph!r}")


_BLANK = Cell(" ", Color.DEFAULT)


class CellGrid:
    """A mutable ``rows`` x ``cols`` grid of cells."""

    def __init__(self, rows: int, cols: int) -> None:
        if rows <= 0 or cols <= 0:
            raise ValueError(f"grid must be positive, got {rows}x{cols}")
        self.rows = rows
        self.cols = cols
        self._cells: list[list[Cell]] = [[_BLANK for _ in range(cols)] for _ in range(rows)]

    def blank(self) -> None:
        """Reset every cell to the blank cell."""
        for r in range(self.rows):
            for c in range(self.cols):
                self._cells[r][c] = _BLANK

    def set(self, r: int, c: int, cell: Cell) -> None:
        """Paint *cell* at row *r*, column *c* (bounds-checked)."""
        if not (0 <= r < self.rows and 0 <= c < self.cols):
            raise IndexError(f"cell ({r},{c}) out of bounds for {self.rows}x{self.cols}")
        self._cells[r][c] = cell

    def get(self, r: int, c: int) -> Cell:
        """Return the cell at row *r*, column *c* (bounds-checked)."""
        if not (0 <= r < self.rows and 0 <= c < self.cols):
            raise IndexError(f"cell ({r},{c}) out of bounds for {self.rows}x{self.cols}")
        return self._cells[r][c]

    def row_glyphs(self, r: int) -> str:
        """Return row *r* as a string of its glyphs."""
        if not (0 <= r < self.rows):
            raise IndexError(f"row {r} out of bounds for {self.rows} rows")
        return "".join(cell.glyph for cell in self._cells[r])
