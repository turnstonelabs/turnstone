"""The shared event log — a world-wide feed players catch up on.

Events are append-only and ordered by insertion. Each player tracks a
cursor (the id of the last event they have seen); ``since`` returns the
slice after a cursor and the new cursor to persist.

An event carries a ``target``: empty means PUBLIC (the broadsheet and the
lobby TV), a player name means a PRIVATE note that only that player reads in
their own catch-up. Targeted rows ride the same id order as public ones, so
the cursor advances identically whether or not a private note was shown.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Event:
    """A single logged happening in the shared world.

    ``target`` is the empty string for public events (heralded to everyone)
    or a player's name for a private note delivered only to that player.
    """

    event_id: int
    ts: str
    kind: str
    actor: str
    text: str
    target: str = ""


def since(events: list[Event], cursor: int) -> tuple[list[Event], int]:
    """Return events newer than ``cursor`` and the cursor to store next.

    Events are kept in ascending id order (the store hydrates the newest tail
    and reverses it to ascending; appends are monotonic), so the last fresh
    event carries the highest id; that becomes the new cursor.
    When nothing is new the input cursor is returned, so advancing is
    idempotent.
    """
    fresh = [e for e in events if e.event_id > cursor]
    if not fresh:
        return [], cursor
    return fresh, fresh[-1].event_id


def since_visible(events: list[Event], cursor: int, viewer: str) -> tuple[list[Event], int]:
    """Like :func:`since`, but hide private notes not addressed to *viewer*.

    Returns the events newer than ``cursor`` that *viewer* may read — every
    public event (empty ``target``) plus the private notes addressed to them —
    and the new cursor. The cursor advances to the highest id PAST the old
    cursor regardless of visibility, so a private note for someone else is
    consumed (never re-scanned) without ever being shown here.
    """
    fresh = [e for e in events if e.event_id > cursor]
    if not fresh:
        return [], cursor
    new_cursor = fresh[-1].event_id
    visible = [e for e in fresh if not e.target or e.target == viewer]
    return visible, new_cursor
