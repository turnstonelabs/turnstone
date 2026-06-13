"""Deterministic terrain texturing — vary a terrain glyph by map position.

A field of identical `.`s reads flat; swapping in an occasional `,` or `'`
gives the overworld a hand-stippled BBS texture without storing anything on the
map. The variation is a PURE FUNCTION OF THE CELL COORDINATE, so it is stable
across redraws (a cell always picks the same variant) and reproducible — the
model never sees it, only the renderer.

Only *terrain* cells are textured. The player marker, the other-player marker,
and location glyphs are painted on top and are never varied, so the eye can
always find them.

LOCKSTEP CONTRACT. The Watch page (``understone.watch.WATCH_HTML``) paints its
own base map in JavaScript and reproduces the EXACT same selection — the same
``VARIANTS`` rows and the same ``(x * _HASH_X + y * _HASH_Y) % n`` index. The
page builds that index string FROM the :data:`_HASH_X` / :data:`_HASH_Y`
constants here (``understone.watch`` imports them), so a retune of either
number moves the JS with it; only the ``VARIANTS`` table must still be mirrored
by hand, or the live map and the tool frames will drift apart.
"""

from __future__ import annotations

# The position hash multipliers. The variant index for a cell is
# ``(x * _HASH_X + y * _HASH_Y) % len`` — two odd, coprime constants chosen so
# neighbouring cells spread across the variant row rather than banding. The
# Watch JS builds its own copy of this formula FROM these same two numbers
# (``understone.watch`` imports them), so a retune here moves the page in
# lockstep; a guard test pins the agreement.
_HASH_X = 31
_HASH_Y = 17

# Base glyph -> the ordered string of glyphs it may render as. The base glyph
# is index 0, so a cell that hashes to 0 is unchanged. Glyphs not listed here
# are never varied. Keep in lockstep with the Watch JS VARIANTS map.
VARIANTS: dict[str, str] = {
    ".": ".,'",
    "≋": "≋≈",
}


def textured(glyph: str, x: int, y: int) -> str:
    """Return the variant of *glyph* for cell ``(x, y)``, or *glyph* unchanged.

    When *glyph* has a :data:`VARIANTS` row, the cell coordinate selects one of
    its variants by ``(x * _HASH_X + y * _HASH_Y) % len`` — a fixed,
    position-only hash so the choice is stable per cell and identical to the
    Watch's. Glyphs with no row (every actor and location glyph, and any
    un-listed terrain) are returned as-is.
    """
    choices = VARIANTS.get(glyph)
    if choices is None:
        return glyph
    return choices[(x * _HASH_X + y * _HASH_Y) % len(choices)]
