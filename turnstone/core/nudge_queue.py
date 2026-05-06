"""Thread-safe FIFO queue for metacognitive nudges with channel filtering.

Replaces the dual ``_pending_user_advisories`` / ``_pending_tool_advisories``
list pair with a single channel-tagged queue per session.  Producers
(`_queue_user_advisory`, `_queue_tool_advisory`, the future
`CoordinatorIdleObserver`, the future watch dispatcher) all enqueue onto
the same queue with an explicit ``channel``; consumers
(`_attach_pending_user_reminders` on user-message attach,
`_collect_advisories` on tool-result wrap, the future
`IdleNudgeWatcher` on workstream-IDLE) drain by channel filter.

Channels:
    * ``"user"`` — only drains at user-turn seams.
    * ``"tool"`` — only drains at tool-result seams.
    * ``"any"``  — drains at whichever seam fires first (used for
      wake-trigger-driven nudges that should not be pinned to a
      specific drain seam).

Drain preserves FIFO order; non-matching entries stay queued.  All
operations are atomic under an internal :class:`threading.Lock`.
"""

from __future__ import annotations

import threading
from collections import deque
from typing import Literal, NamedTuple

Channel = Literal["user", "tool", "any"]
_VALID_CHANNELS: frozenset[str] = frozenset({"user", "tool", "any"})

# Module-level filter constants — most callers want one of these and
# pre-allocating spares us a frozenset construction at every drain seam.
USER_DRAIN: frozenset[str] = frozenset({"user", "any"})
TOOL_DRAIN: frozenset[str] = frozenset({"tool", "any"})


class _Entry(NamedTuple):
    nudge_type: str
    text: str
    channel: Channel


class NudgeQueue:
    """Single-session FIFO queue with channel-tagged entries."""

    def __init__(self) -> None:
        self._items: deque[_Entry] = deque()
        self._lock = threading.Lock()

    def enqueue(self, nudge_type: str, text: str, channel: Channel) -> None:
        """Append a nudge.  ``channel`` MUST be in :data:`_VALID_CHANNELS`.

        ``channel`` is required so producers ingesting untrusted text
        (future child workstream names, watch payloads) must pick a
        seam consciously rather than silently routing to whichever
        seam drains first via an implicit default.
        """
        if channel not in _VALID_CHANNELS:
            raise ValueError(f"channel={channel!r}; expected one of {sorted(_VALID_CHANNELS)}")
        with self._lock:
            self._items.append(_Entry(nudge_type, text, channel))

    def drain(self, channels: frozenset[str] | set[str]) -> list[tuple[str, str]]:
        """Drain entries whose channel is in ``channels``.

        Entries with non-matching channels stay in the queue, in order.
        Returns ``(nudge_type, text)`` tuples in insertion order.
        """
        with self._lock:
            if not self._items:
                return []
            kept: deque[_Entry] = deque()
            out: list[tuple[str, str]] = []
            for entry in self._items:
                if entry.channel in channels:
                    out.append((entry.nudge_type, entry.text))
                else:
                    kept.append(entry)
            self._items = kept
        return out

    def __len__(self) -> int:
        """Current depth.  Used by the future IdleNudgeWatcher gate."""
        with self._lock:
            return len(self._items)

    def clear(self) -> int:
        """Drop every entry; return the count cleared.  Used in cancel paths."""
        with self._lock:
            n = len(self._items)
            self._items.clear()
            return n

    def pending(self, channel: Channel | None = None) -> list[tuple[str, str]]:
        """Non-mutating snapshot for tests / introspection.

        With ``channel=None`` returns every queued entry as
        ``(nudge_type, text)`` tuples in insertion order; with a
        specific channel filters to that channel only.  Production
        code that wants to *consume* entries should call :meth:`drain`
        instead — pending entries are by definition unconsumed and
        will redraw at the next matching seam.
        """
        with self._lock:
            if channel is None:
                return [(e.nudge_type, e.text) for e in self._items]
            return [(e.nudge_type, e.text) for e in self._items if e.channel == channel]

    def has_pending(self, channels: frozenset[str] | set[str]) -> bool:
        """Short-circuiting existence check.

        Returns ``True`` as soon as a queued entry's channel matches
        ``channels``.  Cheaper than :meth:`pending` for callers that
        only need a boolean (e.g. the wake-path defense in
        :meth:`ChatSession.deliver_wake_nudge_from_queue`) — no list
        allocation, lock released on first match.
        """
        with self._lock:
            return any(e.channel in channels for e in self._items)
