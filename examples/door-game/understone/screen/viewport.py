"""Viewport window maths — pure integer arithmetic, no state.

Computes the top-left corner of a view window over a larger map, centred
on a focus point but clamped to map edges so the window never wraps and
never runs off the map. At edges the focus point sits off-centre.
"""

from __future__ import annotations


def compute_window(
    map_w: int,
    map_h: int,
    view_w: int,
    view_h: int,
    cx: int,
    cy: int,
) -> tuple[int, int]:
    """Return ``(x0, y0)`` top-left map coords for a view centred on ``(cx, cy)``.

    The window is clamped so ``[x0, x0 + view_w)`` stays within
    ``[0, map_w)`` (and likewise for the vertical axis). When the map is
    smaller than the view the origin pins to ``0``.
    """
    x0 = _clamp_axis(map_w, view_w, cx)
    y0 = _clamp_axis(map_h, view_h, cy)
    return x0, y0


def _clamp_axis(map_size: int, view_size: int, center: int) -> int:
    """Clamp one axis: centre on ``center`` then pull inside the map edges."""
    if view_size >= map_size:
        return 0
    origin = center - view_size // 2
    max_origin = map_size - view_size
    if origin < 0:
        return 0
    if origin > max_origin:
        return max_origin
    return origin
