"""The satchel "id:qty" wire codec (understone.engine.satchel).

Pins the single-source codec the game façade, the Watch payload, and the
balance simulator all decode through. The format is comma-joined ``id:qty``
stacks; this proves a clean round-trip, the defensive bare-id => qty-1 rule, the
malformed/zero/empty fragments that are skipped, and that the encoder never
emits a zero-or-negative stack.
"""

from __future__ import annotations

import pytest

from understone.engine.satchel import decode_satchel, encode_satchel


def test_round_trips_id_qty_stacks() -> None:
    """The canonical "id:qty,id:qty" data decodes and re-encodes unchanged."""
    encoded = "minor_potion:3,iron_ore:5"
    stacks = decode_satchel(encoded)
    assert stacks == [("minor_potion", 3), ("iron_ore", 5)]
    assert encode_satchel(stacks) == encoded


def test_bare_id_decodes_as_qty_one() -> None:
    """A colonless chunk is a single item (defensive — never silently dropped)."""
    assert decode_satchel("minor_potion") == [("minor_potion", 1)]
    # Mixed with a normal stack, order preserved.
    assert decode_satchel("minor_potion,iron_ore:5") == [
        ("minor_potion", 1),
        ("iron_ore", 5),
    ]


@pytest.mark.parametrize(
    ("encoded", "reason"),
    [
        ("id:0", "zero quantity"),
        ("id:-1", "negative quantity"),
        ("id:abc", "non-integer quantity"),
        (":5", "empty id"),
        ("", "empty string"),
        ("minor_potion:3,", "trailing comma yields an empty chunk"),
        (",minor_potion:3", "leading comma yields an empty chunk"),
    ],
)
def test_skips_malformed_or_zero_fragments(encoded: str, reason: str) -> None:
    """A present-but-invalid or non-positive fragment is skipped; valid ones survive."""
    stacks = decode_satchel(encoded)
    assert all(item_id and qty > 0 for item_id, qty in stacks), reason
    # The only valid stack in the trailing/leading-comma cases is the potion.
    if "minor_potion:3" in encoded:
        assert stacks == [("minor_potion", 3)]
    else:
        assert stacks == []


def test_encode_drops_non_positive_stacks() -> None:
    """The encoder never emits "id:0" or a negative quantity."""
    assert encode_satchel([("minor_potion", 0)]) == ""
    assert encode_satchel([("minor_potion", -2)]) == ""
    assert encode_satchel([("minor_potion", 2), ("iron_ore", 0)]) == "minor_potion:2"
    assert encode_satchel([]) == ""
