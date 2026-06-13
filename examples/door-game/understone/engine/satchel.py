"""The satchel wire codec — the one home for the ``"id:qty"`` stack encoding.

A player's satchel is stored as a single string: comma-joined ``id:qty`` stacks,
e.g. ``"minor_potion:3,iron_ore:5"``; an empty string is an empty bag. This
module is the SINGLE source of truth for that format. Three readers carried a
byte-identical decode loop (the game façade, the Watch payload builder, and the
balance simulator); they all delegate here so the format is described — and
parsed — in exactly one place.

The codec is pure and stdlib-only: it knows the wire shape and nothing else.
It does NOT collapse duplicate ids into one stack, resolve ids against a content
pack, or enforce the distinct-stack cap — those are stack *semantics* the
callers own. The codec only encodes and decodes.
"""

from __future__ import annotations


def decode_satchel(s: str) -> list[tuple[str, int]]:
    """Decode the ``"id:qty"`` satchel string into ordered ``(item_id, qty)`` stacks.

    Splits on ``","`` and skips empty chunks (so an empty string, a leading or
    trailing comma, and a doubled comma all yield no spurious stack). Each chunk
    is partitioned on ``":"``:

    * a chunk with no colon (a bare id) parses as quantity ``1`` — a colonless
      fragment is treated as a single item, never silently dropped;
    * a chunk whose quantity is present but not an integer, or is ``<= 0``, is
      skipped;
    * a chunk with an empty id is skipped.

    Order is preserved (first-stowed first), which fixes which potion a heal tie
    resolves to. The codec collapses nothing — callers own stack semantics.
    """
    stacks: list[tuple[str, int]] = []
    for chunk in s.split(","):
        if not chunk:
            continue
        item_id, sep, qty_str = chunk.partition(":")
        if not item_id:
            continue
        if not sep:
            # A bare id with no colon is a single item (defensive: never drop it).
            stacks.append((item_id, 1))
            continue
        try:
            qty = int(qty_str)
        except ValueError:
            continue
        if qty > 0:
            stacks.append((item_id, qty))
    return stacks


def encode_satchel(stacks: list[tuple[str, int]]) -> str:
    """Encode ``(item_id, qty)`` stacks back into the comma-joined ``"id:qty"`` string.

    Any stack at quantity ``<= 0`` is dropped, so the encoding never emits
    ``"id:0"``; this is the single home for the drop-at-empty rule, letting
    callers decrement freely and rely on a spent-to-zero stack falling away.
    """
    return ",".join(f"{item_id}:{qty}" for item_id, qty in stacks if qty > 0)
