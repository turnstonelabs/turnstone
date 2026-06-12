"""Frame rendering — wrap a grid in a single box border with a title and status.

Deterministic and glyph-only (no ANSI). The title is centred within the
top border run; the status line is printed under the closed box.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from understone.screen.box import BL, BR, H, V, border_with_title

if TYPE_CHECKING:
    from understone.screen.grid import CellGrid


def render_frame(grid: CellGrid, *, title: str, status: str) -> str:
    """Render *grid* inside a box, with *title* in the top border and *status* below.

    The inner width equals the grid's column count. The title is centred
    in the horizontal run of the top border; if it does not fit it is
    truncated to the available run.
    """
    inner = grid.cols
    top = border_with_title(inner, title)
    bottom = BL + (H * inner) + BR
    lines = [top]
    for r in range(grid.rows):
        lines.append(V + grid.row_glyphs(r) + V)
    lines.append(bottom)
    lines.append(status)
    return "\n".join(lines)
