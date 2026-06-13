"""Deterministic terrain texturing (understone.screen.texture).

Pins the contract the Watch JS mirrors: a textured glyph is a pure function of
its cell coordinate (stable per cell), an un-listed glyph is returned
untouched, and the selection formula is ``(x * _HASH_X + y * _HASH_Y) % n``
derived from the module's hash constants. The formula is asserted against those
constants so a retune moves the test with it and a drift is caught.
"""

from __future__ import annotations

from understone.screen.texture import _HASH_X, _HASH_Y, VARIANTS, textured


def test_untextured_glyph_is_unchanged() -> None:
    """A glyph with no VARIANTS row passes through verbatim (actors, walls)."""
    for ch in "█@☻⌂$":
        assert textured(ch, 3, 7) == ch


def test_same_coord_same_variant() -> None:
    """Texturing is position-only and stable: one cell always picks one glyph."""
    first = textured(".", 12, 5)
    for _ in range(5):
        assert textured(".", 12, 5) == first


def test_variant_is_always_in_the_row() -> None:
    """Every selected glyph is one of the declared variants for its base."""
    choices = VARIANTS["."]
    for x in range(20):
        for y in range(20):
            assert textured(".", x, y) in choices


def test_a_row_uses_more_than_one_variant() -> None:
    """Across a row the hash spreads — the texture is not a single repeated glyph."""
    seen = {textured(".", x, 0) for x in range(len(VARIANTS["."]) * 4)}
    assert len(seen) > 1


def test_formula_matches_the_hash_constants() -> None:
    """The selection index is (x * _HASH_X + y * _HASH_Y) % len — the JS twin's formula.

    Derived from the live ``_HASH_X`` / ``_HASH_Y`` constants (not the literal
    31/17) and checked against the live VARIANTS rows, so it stays a formula
    test that tracks a retune rather than a snapshot a table or constant edit
    could silently invalidate.
    """
    for base, choices in VARIANTS.items():
        n = len(choices)
        for x, y in [(0, 0), (1, 0), (0, 1), (12, 5), (7, 13), (255, 255)]:
            assert textured(base, x, y) == choices[(x * _HASH_X + y * _HASH_Y) % n]


def test_origin_cell_is_the_base_glyph() -> None:
    """Cell (0,0) hashes to index 0, which is the base glyph (variants[0])."""
    for base, choices in VARIANTS.items():
        assert textured(base, 0, 0) == choices[0]
        assert choices[0] == base
