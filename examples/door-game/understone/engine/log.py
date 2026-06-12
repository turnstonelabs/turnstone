"""The shared event log — a world-wide feed players catch up on.

Events are append-only and ordered by insertion. Each player tracks a
cursor (the id of the last event they have seen); ``since`` returns the
slice after a cursor and the new cursor to persist.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Event:
    """A single logged happening in the shared world."""

    event_id: int
    ts: str
    kind: str
    actor: str
    text: str


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
