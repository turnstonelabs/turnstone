"""Thread-safe FIFO queue for metacognitive nudges with channel filtering.

Replaces the dual ``_pending_user_advisories`` / ``_pending_tool_advisories``
list pair with a single channel-tagged queue per session.  Producers
(`_queue_user_advisory`, `_queue_tool_advisory`,
`CoordinatorIdleObserver`, the watch dispatcher) all enqueue onto
the same queue with an explicit ``channel``; consumers
(`_emit_pending_user_nudges` on user-message attach,
`_collect_advisories` on tool-result batch,
`IdleNudgeWatcher` on workstream-IDLE) drain by channel filter and emit
each drained nudge as a first-class ``{"role": "system"}`` turn.

Channels:
    * ``"user"`` — only drains at user-turn seams.
    * ``"tool"`` — only drains at tool-result seams.
    * ``"any"``  — drains at whichever seam fires first (used for
      wake-trigger-driven nudges that should not be pinned to a
      specific drain seam) AND counts toward the idle-wake gate
      (:data:`WAKE_PENDING`).
    * ``"quiet"`` — drains at whichever seam fires first, but does NOT
      count toward the idle-wake gate.  A user cancel demotes pending
      ``"any"`` entries here: the external event (watch fire,
      background-shell exit) is still delivered at the next seam, but it
      must not wake the workstream the user just stopped.

Drain preserves FIFO order; non-matching entries stay queued.  Each
entry can carry an optional ``valid_until`` predicate that drain
evaluates outside the queue lock; entries whose predicate returns
``False`` are dropped (logged at ``info`` — normal lifecycle outcome,
e.g. ``idle_children`` after every child closed) and entries whose
predicate raises are dropped (logged at ``warning`` with ``exc_info``
— a misbehaving predicate).  Operations are atomic under an internal
:class:`threading.Lock`.
"""

from __future__ import annotations

import threading
from collections import deque
from typing import TYPE_CHECKING, Any, Literal, NamedTuple

from turnstone.core.log import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

log = get_logger(__name__)

Channel = Literal["user", "tool", "any", "quiet"]
_VALID_CHANNELS: frozenset[str] = frozenset({"user", "tool", "any", "quiet"})

# Module-level filter constants — most callers want one of these and
# pre-allocating spares us a frozenset construction at every drain seam.
USER_DRAIN: frozenset[str] = frozenset({"user", "any", "quiet"})
TOOL_DRAIN: frozenset[str] = frozenset({"tool", "any", "quiet"})
# The idle-wake GATE (``IdleNudgeWatcher``): which pending channels justify
# waking an idle workstream.  Deliberately excludes ``"quiet"`` — entries a
# user cancel demoted must ride the next legitimate seam/wake, never cause
# one, or Stop is followed seconds later by an autonomous resume.
WAKE_PENDING: frozenset[str] = frozenset({"user", "any"})
# The quiet channel, named once: the demotion target for external events a
# user cancel must not let re-wake the workstream, and the ride-along drain
# the wake path uses after its WAKE_PENDING pass.
QUIET_CHANNEL: Channel = "quiet"
QUIET_DRAIN: frozenset[str] = frozenset({QUIET_CHANNEL})


class Entry(NamedTuple):
    """One queued nudge.  Public so consumers of
    :meth:`NudgeQueue.drain_entries` can give entries back via
    :meth:`NudgeQueue.requeue` — which preserves ``valid_until`` AND the
    original ``seq`` (a plain :meth:`NudgeQueue.enqueue` would assign a
    fresh seq and re-order a recovered older notice after newer events).
    ``seq`` is the queue-global insertion number multi-channel drains sort
    on to restore chronology."""

    nudge_type: str
    text: str
    channel: Channel
    seq: int = 0
    valid_until: Callable[[], bool] | None = None
    # Producer-supplied optional fields that ride alongside ``text`` when
    # drained — used by ``watch_triggered`` to carry ``watch_name`` /
    # ``command`` / ``poll_count`` / ``max_polls`` / ``is_final`` into the
    # rendered reminder dict; they ride as sibling keys on the
    # ``watch_triggered`` system turn, surfaced in the operator bubble.
    # Other producers leave it ``None`` and consumers see only
    # ``{type, text}``.  Atomicity guarantee: text + metadata land on the
    # same enqueue call, so a concurrent drain can't observe text without
    # the matching metadata.
    metadata: dict[str, Any] | None = None


class NudgeQueue:
    """Single-session FIFO queue with channel-tagged entries."""

    def __init__(self) -> None:
        self._items: deque[Entry] = deque()
        self._seq = 0
        self._lock = threading.Lock()

    def enqueue(
        self,
        nudge_type: str,
        text: str,
        channel: Channel,
        *,
        valid_until: Callable[[], bool] | None = None,
        metadata: dict[str, Any] | None = None,
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

        ``metadata`` carries optional producer-specific fields that
        drain returns alongside ``(nudge_type, text)``.  ``watch_triggered``
        uses it for ``watch_name`` / ``command`` / ``poll_count`` /
        ``max_polls`` / ``is_final`` so the frontend can render a
        structured card; other producers leave it ``None``.
        """
        if channel not in _VALID_CHANNELS:
            raise ValueError(f"channel={channel!r}; expected one of {sorted(_VALID_CHANNELS)}")
        with self._lock:
            self._seq += 1
            self._items.append(Entry(nudge_type, text, channel, self._seq, valid_until, metadata))

    def drain(
        self, channels: frozenset[str] | set[str]
    ) -> list[tuple[str, str, dict[str, Any] | None]]:
        """Drain entries whose channel is in ``channels``.

        Entries with non-matching channels stay in the queue, in order.
        Returns ``(nudge_type, text, metadata)`` tuples in insertion
        order; ``metadata`` is the producer-supplied dict (or ``None``
        when unset).

        Entries with a ``valid_until`` predicate get re-checked outside
        the queue lock; falsy / raising predicates drop the entry
        without delivering it.  Already-removed-from-queue either way —
        dropped entries don't ride a future drain.
        """
        return [(e.nudge_type, e.text, e.metadata) for e in self.drain_entries(channels)]

    def drain_entries(self, channels: frozenset[str] | set[str]) -> list[Entry]:
        """Like :meth:`drain` but returns the surviving :class:`Entry`
        records whole — ``seq`` for cross-channel chronology merges and
        ``valid_until`` so a consumer that must give an entry back (the
        wake path's failed-send re-enqueue) can do so without stripping
        its staleness predicate.
        """
        with self._lock:
            if not self._items:
                return []
            # Fast path: every entry matches → swap deque rather than
            # walk + partition + per-entry append.  This is the common
            # case in practice since the chat loop's drain seams use
            # ``USER_DRAIN`` / ``TOOL_DRAIN`` (channel + "any") and
            # most queues hold only one channel's entries at a time.
            if all(entry.channel in channels for entry in self._items):
                candidates: list[Entry] = list(self._items)
                self._items = deque()
            else:
                kept: deque[Entry] = deque()
                candidates = []
                for entry in self._items:
                    if entry.channel in channels:
                        candidates.append(entry)
                    else:
                        kept.append(entry)
                self._items = kept
        # Predicates evaluate outside the lock — they may do storage
        # I/O or other work that shouldn't block other producers /
        # the drain consumer's other queues.  Drop-level distinction:
        # a ``False`` return is a normal lifecycle outcome (the
        # producer's snapshot is stale — e.g. ``idle_children`` after
        # every child closed) and logs at ``info``; a raised exception
        # is a wiring bug (predicate is misbehaving) and stays at
        # ``warning`` with ``exc_info`` so the traceback surfaces.
        out: list[Entry] = []
        for entry in candidates:
            if entry.valid_until is None:
                out.append(entry)
                continue
            try:
                if entry.valid_until():
                    out.append(entry)
                    continue
                log.info(
                    "nudge_queue.predicate_dropped",
                    extra={
                        "nudge_type": entry.nudge_type,
                        "channel": entry.channel,
                        "reason": "predicate_false",
                        "text_len": len(entry.text),
                    },
                )
            except Exception:
                log.warning(
                    "nudge_queue.predicate_dropped",
                    extra={
                        "nudge_type": entry.nudge_type,
                        "channel": entry.channel,
                        "reason": "predicate_raised",
                        "text_len": len(entry.text),
                    },
                    exc_info=True,
                )
        return out

    def __len__(self) -> int:
        """Current depth.  Used by the future IdleNudgeWatcher gate."""
        with self._lock:
            return len(self._items)

    def clear(self) -> int:
        """Drop every entry regardless of channel; return the count cleared.

        No longer on the cancel path — abandoned generations use
        :meth:`clear_channels` + :meth:`demote_channel` so external events
        survive.  Kept for tests and for full-reset callers that truly mean
        "everything".
        """
        with self._lock:
            n = len(self._items)
            self._items.clear()
            return n

    def requeue(self, entry: Entry, *, channel: Channel | None = None) -> None:
        """Give a drained :class:`Entry` back to the queue, KEEPING its seq.

        A plain :meth:`enqueue` would assign a fresh (higher) seq, so a
        failed delivery's re-queued OLDER notice would sort after events
        that arrived during the failed attempt — running poll counters
        backwards at the next seq-merged wake.  Insertion is positioned by
        seq so plain FIFO drains stay chronological too.  ``channel``
        overrides the entry's channel (the wake path demotes ``"any"`` →
        ``"quiet"``); ``valid_until`` and ``metadata`` ride unchanged.
        """
        dst = channel if channel is not None else entry.channel
        if dst not in _VALID_CHANNELS:
            raise ValueError(f"channel={dst!r}; expected one of {sorted(_VALID_CHANNELS)}")
        restored = entry._replace(channel=dst)
        with self._lock:
            for i, existing in enumerate(self._items):
                if existing.seq > restored.seq:
                    self._items.insert(i, restored)
                    return
            self._items.append(restored)

    def demote_channel(self, src: Channel, dst: Channel) -> int:
        """Atomically re-tag every ``src``-channel entry as ``dst``; return
        the count.  Order, text, metadata and ``valid_until`` are preserved
        — only drain/wake eligibility changes.  The cancel path uses this to
        take ``"any"`` entries out of the idle-wake gate (→ ``"quiet"``)
        without dropping the external events they announce.
        """
        if dst not in _VALID_CHANNELS:
            raise ValueError(f"channel={dst!r}; expected one of {sorted(_VALID_CHANNELS)}")
        with self._lock:
            demoted = 0
            for i, entry in enumerate(self._items):
                if entry.channel == src:
                    self._items[i] = entry._replace(channel=dst)
                    demoted += 1
            return demoted

    def clear_channels(self, channels: frozenset[str] | set[str]) -> int:
        """Drop entries whose channel is in ``channels``; return the count.

        The abandoned-generation paths use this instead of :meth:`clear`:
        ``"tool"``/``"user"`` advisories are generation-scoped commentary
        (a stale ``repeat`` nudge must not bleed into the next send), but
        ``"any"``-channel entries are EXTERNAL events — a watch fire or a
        background-shell exit that happened during the doomed generation
        still happened, and dropping it would silently break the "you will
        be notified" contract those producers promised the model.
        """
        with self._lock:
            kept = deque(e for e in self._items if e.channel not in channels)
            n = len(self._items) - len(kept)
            self._items = kept
            return n

    def count_by_type(self, nudge_type: str, channel: Channel | None = None) -> int:
        """Return the number of queued entries matching ``nudge_type``.

        With ``channel=None`` counts across all channels; with a specific
        channel filters to that channel only.  Walks ``_items`` once
        under the lock without materialising tuples — cheaper than
        ``len(pending(channel=...))`` for callers that only need the
        count (e.g. the watch dispatcher's soft-cap pre-check).
        Producer-side soft caps that pair this with
        :meth:`drop_oldest_by_type` should pass the same ``channel`` to
        both halves so the count snapshot and the drop walk over the
        same entry set.
        """
        with self._lock:
            if channel is None:
                return sum(1 for e in self._items if e.nudge_type == nudge_type)
            return sum(
                1 for e in self._items if e.nudge_type == nudge_type and e.channel == channel
            )

    def drop_oldest_by_type(self, nudge_type: str, channel: Channel | None = None) -> bool:
        """Remove the earliest-enqueued entry whose type matches ``nudge_type``.

        With ``channel=None`` searches across all channels; with a specific
        channel filters to that channel only.  Returns ``True`` if an
        entry was dropped, ``False`` if no matching entry was found.
        The call itself is atomic under the queue lock; producers that
        need an atomic count-and-drop pair (no interleave with concurrent
        drains) should use :meth:`cap_at_or_drop_oldest` instead.
        """
        with self._lock:
            for i, entry in enumerate(self._items):
                if entry.nudge_type != nudge_type:
                    continue
                if channel is not None and entry.channel != channel:
                    continue
                del self._items[i]
                return True
        return False

    def cap_at_or_drop_oldest(
        self,
        nudge_type: str,
        max_depth: int,
        channel: Channel | None = None,
    ) -> bool:
        """If queued ``nudge_type`` entries reach ``max_depth``, drop the
        earliest matching entry — under a single lock acquisition so a
        concurrent drain can't slip between the count and the drop.

        Returns ``True`` iff a drop happened.  Producers with a per-type
        soft cap call this on the enqueue path; ``max_depth <= 0`` is a
        defensive no-op returning ``False``.
        """
        if max_depth <= 0:
            return False
        with self._lock:
            oldest_index = -1
            count = 0
            for i, entry in enumerate(self._items):
                if entry.nudge_type != nudge_type:
                    continue
                if channel is not None and entry.channel != channel:
                    continue
                if oldest_index == -1:
                    oldest_index = i
                count += 1
                if count >= max_depth:
                    del self._items[oldest_index]
                    return True
            return False

    def pending(self, channel: Channel | None = None) -> list[tuple[str, str]]:
        """Non-mutating snapshot for tests / introspection.

        With ``channel=None`` returns every queued entry as
        ``(nudge_type, text)`` tuples in insertion order; with a
        specific channel filters to that channel only.  Production
        code that wants to *consume* entries should call :meth:`drain`
        instead — pending entries are by definition unconsumed and
        will redraw at the next matching seam.

        ``metadata`` is intentionally NOT projected here — tests that
        need to assert producer-specific fields call
        :meth:`pending_with_metadata` (or :meth:`drain` directly).
        """
        with self._lock:
            if channel is None:
                return [(e.nudge_type, e.text) for e in self._items]
            return [(e.nudge_type, e.text) for e in self._items if e.channel == channel]

    def pending_with_metadata(
        self, channel: Channel | None = None
    ) -> list[tuple[str, str, dict[str, Any] | None]]:
        """Non-mutating snapshot including each entry's ``metadata``.

        Used by tests / introspection paths that need to assert
        producer-specific optional fields (e.g. the watch dispatcher's
        ``watch_name`` / ``command`` / ``poll_count`` payload).  Production
        consumers should still call :meth:`drain`.
        """
        with self._lock:
            if channel is None:
                return [(e.nudge_type, e.text, e.metadata) for e in self._items]
            return [(e.nudge_type, e.text, e.metadata) for e in self._items if e.channel == channel]

    def has_pending(self, channels: frozenset[str] | set[str]) -> bool:
        """Short-circuiting existence check.

        Returns ``True`` as soon as a queued entry's channel matches
        ``channels``.  Cheaper than :meth:`pending` for callers that
        only need a boolean — used by
        :class:`turnstone.core.idle_nudge_watcher.IdleNudgeWatcher` to
        gate wake dispatch on whether ``USER_DRAIN`` would actually
        deliver anything before paying the worker-thread spawn.  No
        list allocation, lock released on first match.
        """
        with self._lock:
            return any(e.channel in channels for e in self._items)
