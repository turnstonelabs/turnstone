"""The one-glyph-one-column contract for everything drawn on the grid.

Every surface Understone paints — the bordered text frames, the golden frames
the screen tests pin, and the Watch's CSS ``1ch``-per-cell map — assumes each
map glyph occupies *exactly one* terminal column. A glyph that renders two
columns (a CJK ideograph, an emoji) shoves the row right and tears the
box-drawing border; a zero-width combining mark stacks onto its neighbour and
desynchronises the column count the other way. :func:`is_grid_safe` is the
single predicate that admits a character to the grid, and :data:`SAFE_PALETTE`
is the curated set of glyphs known to satisfy it with period CP437 flavour.

THE WESTERN-MONOSPACE ASSUMPTION. Width here is judged for the Western
monospace metrics every Understone surface actually uses — the pinned Watch
font stack and the monospace of a chat client's code block. Under those
metrics the East-Asian-Width *Ambiguous* class renders single-column, and
Ambiguous is the CP437 heartland: ``█ ♣ ↑ ∩ ≈ ★`` are all EAW=A. So the rule
bars only the genuinely double-width classes — Wide (``W``) and Fullwidth
(``F``) — and admits Ambiguous, Narrow, Neutral, and Halfwidth. The trade is
deliberate: on a CJK-width terminal an Ambiguous glyph would take two columns,
but Understone's surfaces are not those terminals.
"""

from __future__ import annotations

import unicodedata

# East-Asian-Width classes that render two columns under Western monospace and
# would therefore tear a frame; everything else (Na/N/H/A) renders one column.
_DOUBLE_WIDTH_EAW = frozenset({"W", "F"})

# Unicode general categories that carry no column of their own — combining
# marks (Mn/Mc/Me) stack onto a neighbour, format/control codes (Cf/Cc) are
# invisible — so a single such code point is not a paintable cell.
_ZERO_WIDTH_CATEGORIES = frozenset({"Mn", "Mc", "Me", "Cf", "Cc"})


def is_grid_safe(ch: str) -> bool:
    """Return whether *ch* may occupy a single grid cell.

    A grid-safe character is exactly one code point, is printable, is not an
    East-Asian Wide or Fullwidth glyph (the only classes that render two
    columns under the Western monospace metrics our surfaces use — see the
    module docstring), and is not a combining mark or format/control code (a
    zero-width code point that would desynchronise the column count).
    """
    if len(ch) != 1:
        return False
    if not ch.isprintable():
        return False
    if unicodedata.east_asian_width(ch) in _DOUBLE_WIDTH_EAW:
        return False
    return unicodedata.category(ch) not in _ZERO_WIDTH_CATEGORIES


# A curated set of single-column glyphs with BBS / CP437 character, grouped by
# the role an author is likely to want them for. Every entry is grid-safe AND
# free of the loader's reserved markers (two tests assert both), so a pack
# author can pull any of these for terrain, structures, or actors without
# risking a torn frame or colliding with the '@'/'☻' player markers. The black
# smiling face (☻) is the other-player marker and so is NOT here; its white
# twin (☺) is a free being glyph. The grouping is documentation; the set is
# what callers iterate.
SAFE_PALETTE: tuple[str, ...] = (
    # terrain
    "≋",
    "≈",
    "░",
    "▒",
    "▓",
    "♣",
    "↑",
    "▲",
    ".",
    ",",
    "'",
    '"',
    "=",
    "~",
    "§",
    "ø",
    "¤",
    "Ω",
    # structures
    "⌂",
    "✚",
    "∩",
    "†",
    "‡",
    "$",
    "◊",
    "☖",
    # beings
    "☺",
    "¶",
    # misc
    "•",
    "⁂",
    "★",
)
