"""The one-glyph-one-column grid contract (understone.engine.textwidth).

Pins the accept/reject boundary of :func:`is_grid_safe` and proves every
:data:`SAFE_PALETTE` entry clears it. The acceptances include the
East-Asian-Width *Ambiguous* CP437 glyphs the game leans on (``█ ♣ ↑ ∩ ≈ ★``),
which render single-column under the Western monospace our surfaces use; the
rejections are the genuinely double-width and zero-width classes that tear a
frame.
"""

from __future__ import annotations

import unicodedata

import pytest

from understone.engine.textwidth import SAFE_PALETTE, is_grid_safe
from understone.world.loader import RESERVED_GLYPHS

# Single-column glyphs that must be admitted: plain ASCII, a Latin accent that
# is one composed code point, and the Ambiguous-width CP437 set the re-skin uses.
_ACCEPTED = ["a", "Z", "ö", "☻", "≋", "█", "∩", "★", "♣", "↑", ".", "$", " "]

# Must be rejected, with the reason each one trips the gate.
_REJECTED = {
    "龍": "wide CJK ideograph (EAW=W) — two columns",
    "🌲": "emoji (EAW=W) — two columns",
    "Ａ": "fullwidth Latin A (EAW=F) — two columns",
    "é": "decomposed e + combining acute — two code points",
    "́": "a lone combining acute — zero width",
    "👨‍👩": "ZWJ sequence — multiple code points",
    "ab": "two characters",
    "": "empty string",
    "\t": "a control character",
}


@pytest.mark.parametrize("ch", _ACCEPTED)
def test_is_grid_safe_accepts(ch: str) -> None:
    assert is_grid_safe(ch) is True


@pytest.mark.parametrize("text", list(_REJECTED), ids=list(_REJECTED.values()))
def test_is_grid_safe_rejects(text: str) -> None:
    assert is_grid_safe(text) is False


def test_safe_palette_is_all_grid_safe() -> None:
    """Every curated palette glyph clears the gate — the appendix can't ship a dud."""
    bad = [g for g in SAFE_PALETTE if not is_grid_safe(g)]
    assert bad == [], f"palette has non-grid-safe glyphs: {bad}"


def test_safe_palette_has_no_reserved_glyphs() -> None:
    """No palette glyph is a loader-reserved marker — the 'author-usable' promise.

    The appendix tells a pack author to pull any palette glyph for terrain,
    structures, or actors, but the loader rejects the box-drawing frame lines
    and the '@'/'☻' player markers (``loader.RESERVED_GLYPHS``). A palette entry
    that is also reserved would hand the author a glyph that load-fails — the
    exact doc-vs-enforcement trap. Guarding the intersection keeps "all tested
    safe AND author-usable" enforced, not merely asserted on width.
    """
    collisions = set(SAFE_PALETTE) & RESERVED_GLYPHS
    assert collisions == set(), f"palette offers loader-reserved glyphs: {sorted(collisions)}"


def test_safe_palette_has_no_duplicates() -> None:
    """The palette is a set in spirit; a dupe would be an authoring slip."""
    assert len(SAFE_PALETTE) == len(set(SAFE_PALETTE))


def test_ambiguous_width_glyphs_are_accepted() -> None:
    """Document the load-bearing call: EAW=Ambiguous is admitted, not barred.

    These are the CP437 glyphs the game depends on; if a future tightening
    barred Ambiguous, the whole re-skin would vanish from the map.
    """
    for ch in "█♣↑∩≈★":
        assert unicodedata.east_asian_width(ch) == "A"
        assert is_grid_safe(ch) is True
