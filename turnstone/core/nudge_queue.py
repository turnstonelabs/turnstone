"""Thread-safe FIFO queue for metacognitive nudges with channel filtering.

Replaces the dual ``_pending_user_advisories`` / ``_pending_tool_advisories``
list pair with a single channel-tagged queue per session.  Producers
(`_queue_user_advisory`, `_queue_tool_advisory`,
`CoordinatorIdleObserver`, the future watch dispatcher) all enqueue onto
the same queue with an explicit ``channel``; consumers
(`_attach_pending_user_reminders` on user-message attach,
`_collect_advisories` on tool-result wrap,
`IdleNudgeWatcher` on workstream-IDLE) drain by channel filter.

Channels:
    * ``"user"`` — only drains at user-turn seams.
    * ``"tool"`` — only drains at tool-result seams.
    * ``"any"``  — drains at whichever seam fires first (used for
      wake-trigger-driven nudges that should not be pinned to a
      specific drain seam).

Drain preserves FIFO order; non-matching entries stay queued.  Each
entry can carry an optional ``valid_until`` predicate that drain
evaluates outside the queue lock; entries whose predicate returns
``False`` (or raises) are silently dropped without delivery — used by
producers whose payload becomes stale if the underlying state changes
between enqueue and drain (e.g. ``idle_children`` re-checks the active
child set, dropping the nudge if every child finished while the queue
sat).  Operations are atomic under an internal :class:`threading.Lock`.
"""

from __future__ import annotations

import threading
from collections import deque
from typing import TYPE_CHECKING, Literal, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Callable

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
    valid_until: Callable[[], bool] | None = None


class NudgeQueue:
    """Single-session FIFO queue with channel-tagged entries."""

    def __init__(self) -> None:
        self._items: deque[_Entry] = deque()
        self._lock = threading.Lock()

    def enqueue(
        self,
        nudge_type: str,
        text: str,
        channel: Channel,
        *,
        valid_until: Callable[[], bool] | None = None,
    ) -> None:
        """Append a nudge.  ``channel`` MUST be in :data:`_VALID_CHANNELS`.

        ``channel`` is required so producers ingesting untrusted text
        (future child workstream names, watch payloads) must pick a
        seam consciously rather than silently routing to whichever
        seam drains first via an implicit default.

        If ``valid_until`` is provided, drain re-evaluates it before
        delivering the entry; a falsy result drops the entry silently
        (the producer's signal that the snapshot it enqueued is now
        stale).  The predicate is called outside the queue lock so
        producers can do non-trivial work (e.g. re-querying storage
        for active children) without blocking other producers.
        """
        if channel not in _VALID_CHANNELS:
            raise ValueError(f"channel={channel!r}; expected one of {sorted(_VALID_CHANNELS)}")
        with self._lock:
            self._items.append(_Entry(nudge_type, text, channel, valid_until))

    def drain(self, channels: frozenset[str] | set[str]) -> list[tuple[str, str]]:
        """Drain entries whose channel is in ``channels``.

        Entries with non-matching channels stay in the queue, in order.
        Returns ``(nudge_type, text)`` tuples in insertion order.

        Entries with a ``valid_until`` predicate get re-checked outside
        the queue lock; falsy / raising predicates drop the entry
        without delivering it.  Already-removed-from-queue either way —
        dropped entries don't ride a future drain.
        """
        with self._lock:
            if not self._items:
                return []
            kept: deque[_Entry] = deque()
            candidates: list[_Entry] = []
            for entry in self._items:
                if entry.channel in channels:
                    candidates.append(entry)
                else:
                    kept.append(entry)
            self._items = kept
        # Predicates evaluate outside the lock — they may do storage
        # I/O or other work that shouldn't block other producers /
        # the drain consumer's other queues.
        out: list[tuple[str, str]] = []
        for entry in candidates:
            if entry.valid_until is None:
                out.append((entry.nudge_type, entry.text))
                continue
            try:
                if entry.valid_until():
                    out.append((entry.nudge_type, entry.text))
            except Exception:
                # Predicate raising is treated as "no longer valid" —
                # drop silently rather than letting one bad predicate
                # poison the whole drain batch.
                pass
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
