"""Shared box-drawing glyphs and the title-in-border helper.

The frame renderer and the menu renderer both draw a single box with a
centred title in the top border. The glyph set and that border logic live
here so the two renderers cannot drift apart.
"""

from __future__ import annotations

TL = "┌"  # top-left corner
TR = "┐"  # top-right corner
BL = "└"  # bottom-left corner
BR = "┘"  # bottom-right corner
H = "─"  # horizontal run
V = "│"  # vertical edge


def border_with_title(inner: int, title: str) -> str:
    """Build the top border ``┌──title──┐`` with the title centred in the run.

    *inner* is the interior width (between the corners). The title is wrapped
    in single spaces and centred; if it does not fit the run it is truncated.
    An empty/blank title yields a plain horizontal run.
    """
    label = title.strip()
    if not label:
        return TL + (H * inner) + TR
    framed = f" {label} "
    if len(framed) > inner:
        framed = framed[:inner]
    pad = inner - len(framed)
    left = pad // 2
    right = pad - left
    middle = (H * left) + framed + (H * right)
    return TL + middle + TR
