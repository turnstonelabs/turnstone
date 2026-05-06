"""Unit tests for :class:`NudgeQueue`."""

from __future__ import annotations

import threading

import pytest

from turnstone.core.nudge_queue import TOOL_DRAIN, USER_DRAIN, NudgeQueue


class TestEnqueueDrain:
    def test_enqueue_drain_fifo_order(self):
        q = NudgeQueue()
        q.enqueue("a", "1", "user")
        q.enqueue("b", "2", "tool")
        q.enqueue("c", "3", "any")
        # Drain everything regardless of channel — preserves insertion order.
        out = q.drain({"user", "tool", "any"})
        # Drain returns ``(nudge_type, text, metadata)``; producers
        # without ``metadata`` see ``None`` in the third slot.
        assert out == [("a", "1", None), ("b", "2", None), ("c", "3", None)]
        assert len(q) == 0

    def test_drain_filter_keeps_non_matching(self):
        q = NudgeQueue()
        q.enqueue("a", "x", "user")
        q.enqueue("b", "y", "tool")
        # Drain only user → tool entry stays.
        out = q.drain(USER_DRAIN)
        assert out == [("a", "x", None)]
        assert len(q) == 1
        # Now drain tool — gets the remaining entry.
        out = q.drain(TOOL_DRAIN)
        assert out == [("b", "y", None)]
        assert len(q) == 0

    def test_any_channel_drains_on_either_seam(self):
        q = NudgeQueue()
        q.enqueue("c", "z", "any")
        # User-seam drain pulls "any".
        assert q.drain(USER_DRAIN) == [("c", "z", None)]
        assert len(q) == 0
        # Re-enqueue and prove tool-seam also drains "any".
        q.enqueue("d", "w", "any")
        assert q.drain(TOOL_DRAIN) == [("d", "w", None)]
        assert len(q) == 0

    def test_drain_empty_filter_no_op(self):
        q = NudgeQueue()
        q.enqueue("a", "1", "user")
        # Empty filter drains nothing.
        assert q.drain(set()) == []
        assert len(q) == 1

    def test_drain_empty_queue_returns_empty_list(self):
        q = NudgeQueue()
        # Fast-path: no items → no kept-deque allocation, just `[]`.
        assert q.drain(USER_DRAIN) == []
        assert q.drain({"user", "tool", "any"}) == []
        assert len(q) == 0

    def test_drain_preserves_order_across_partial_drain(self):
        q = NudgeQueue()
        q.enqueue("a", "1", "user")
        q.enqueue("b", "2", "tool")
        q.enqueue("c", "3", "user")
        q.enqueue("d", "4", "tool")
        # Drain user — should get "a" then "c" in order; "b","d" stay.
        assert q.drain({"user"}) == [("a", "1", None), ("c", "3", None)]
        # Tool drain follows insertion order on remaining.
        assert q.drain({"tool"}) == [("b", "2", None), ("d", "4", None)]


class TestLenAndClear:
    def test_len_does_not_mutate(self):
        q = NudgeQueue()
        q.enqueue("a", "1", "user")
        assert len(q) == 1
        assert len(q) == 1  # second call still 1; not consumed
        assert q.pending() == [("a", "1")]

    def test_len_empty_is_zero(self):
        q = NudgeQueue()
        assert len(q) == 0

    def test_clear_returns_count(self):
        q = NudgeQueue()
        assert q.clear() == 0
        q.enqueue("a", "1", "user")
        q.enqueue("b", "2", "tool")
        q.enqueue("c", "3", "any")
        assert q.clear() == 3
        assert len(q) == 0

    def test_clear_empty_returns_zero(self):
        q = NudgeQueue()
        assert q.clear() == 0


class TestDropOldestByType:
    def test_drop_oldest_by_type_removes_earliest_match(self):
        """Drop the FIRST entry of the matching type; later matches stay."""
        q = NudgeQueue()
        q.enqueue("other", "first", "any")
        q.enqueue("target", "older", "any")
        q.enqueue("target", "newer", "any")
        # "older" is the earliest target — drop it.
        assert q.drop_oldest_by_type("target") is True
        assert q.pending() == [("other", "first"), ("target", "newer")]

    def test_drop_oldest_by_type_no_match_returns_false(self):
        """Empty queue and unmatched-type cases both return False."""
        q = NudgeQueue()
        # Empty.
        assert q.drop_oldest_by_type("target") is False
        # Non-matching items only.
        q.enqueue("other", "1", "any")
        q.enqueue("other", "2", "tool")
        assert q.drop_oldest_by_type("target") is False
        # Queue is unaffected.
        assert q.pending() == [("other", "1"), ("other", "2")]

    def test_drop_oldest_by_type_only_drops_one(self):
        """Multiple matching entries → only the first is removed."""
        q = NudgeQueue()
        q.enqueue("target", "1", "any")
        q.enqueue("target", "2", "any")
        q.enqueue("target", "3", "any")
        assert q.drop_oldest_by_type("target") is True
        assert q.pending() == [("target", "2"), ("target", "3")]

    def test_drop_oldest_by_type_channel_filter(self):
        """With ``channel`` set, drop walks only that channel.  Pairs with
        :meth:`count_by_type(..., channel=...)` so producer-side soft caps
        operate on a consistent entry set.
        """
        q = NudgeQueue()
        q.enqueue("target", "user-1", "user")
        q.enqueue("target", "any-1", "any")
        q.enqueue("target", "any-2", "any")
        # Drop the oldest "any"-channel target — leaves the user one
        # untouched even though it's earlier in insertion order.
        assert q.drop_oldest_by_type("target", channel="any") is True
        assert q.pending() == [
            ("target", "user-1"),
            ("target", "any-2"),
        ]
        # And a channel with no matches returns False without touching
        # the queue.
        assert q.drop_oldest_by_type("target", channel="tool") is False
        assert q.pending() == [
            ("target", "user-1"),
            ("target", "any-2"),
        ]


class TestCapAtOrDropOldest:
    def test_below_cap_no_drop(self):
        q = NudgeQueue()
        for i in range(3):
            q.enqueue("target", f"t-{i}", "any")
        # 3 entries, cap=5 → no drop.
        assert q.cap_at_or_drop_oldest("target", 5, channel="any") is False
        assert q.count_by_type("target") == 3

    def test_at_cap_drops_oldest(self):
        q = NudgeQueue()
        for i in range(5):
            q.enqueue("target", f"t-{i}", "any")
        # 5 entries, cap=5 → drop the oldest ("t-0"), leaving 4.
        assert q.cap_at_or_drop_oldest("target", 5, channel="any") is True
        remaining = q.pending()
        assert ("target", "t-0") not in remaining
        assert len(remaining) == 4
        assert remaining[0] == ("target", "t-1")  # FIFO drop-oldest preserved

    def test_above_cap_drops_only_one(self):
        q = NudgeQueue()
        for i in range(7):
            q.enqueue("target", f"t-{i}", "any")
        # 7 entries, cap=5 → drop only ONE per call (soft-cap regulates over time).
        assert q.cap_at_or_drop_oldest("target", 5, channel="any") is True
        assert q.count_by_type("target") == 6

    def test_channel_filter_respected(self):
        q = NudgeQueue()
        for i in range(3):
            q.enqueue("target", f"any-{i}", "any")
        for i in range(3):
            q.enqueue("target", f"user-{i}", "user")
        # 3 "any"-channel entries; cap=3 on channel="any" → drop oldest "any" only.
        assert q.cap_at_or_drop_oldest("target", 3, channel="any") is True
        # User-channel entries untouched.
        assert q.count_by_type("target", channel="user") == 3
        assert q.count_by_type("target", channel="any") == 2

    def test_other_types_ignored(self):
        q = NudgeQueue()
        for i in range(5):
            q.enqueue("other", f"o-{i}", "any")
        q.enqueue("target", "t-0", "any")
        # Only one "target" entry; cap=1 on "target" → drop it.  "other"
        # entries are untouched even though queue holds 6 total.
        assert q.cap_at_or_drop_oldest("target", 1, channel="any") is True
        assert q.count_by_type("target") == 0
        assert q.count_by_type("other") == 5

    def test_zero_or_negative_cap_no_op(self):
        q = NudgeQueue()
        q.enqueue("target", "t-0", "any")
        assert q.cap_at_or_drop_oldest("target", 0, channel="any") is False
        assert q.cap_at_or_drop_oldest("target", -1, channel="any") is False
        assert q.count_by_type("target") == 1

    def test_no_match_returns_false(self):
        q = NudgeQueue()
        q.enqueue("other", "o-0", "any")
        assert q.cap_at_or_drop_oldest("target", 1, channel="any") is False
        assert q.count_by_type("other") == 1


class TestCountByType:
    def test_count_by_type_no_channel(self):
        """Count across all channels with ``channel=None``."""
        q = NudgeQueue()
        q.enqueue("target", "1", "user")
        q.enqueue("other", "x", "any")
        q.enqueue("target", "2", "any")
        q.enqueue("target", "3", "tool")
        assert q.count_by_type("target") == 3
        assert q.count_by_type("other") == 1
        assert q.count_by_type("missing") == 0

    def test_count_by_type_with_channel_filter(self):
        """Filter narrows the count to one channel — used by producer-side
        soft caps that pair with ``drop_oldest_by_type(..., channel=...)``.
        """
        q = NudgeQueue()
        q.enqueue("target", "u-1", "user")
        q.enqueue("target", "a-1", "any")
        q.enqueue("target", "a-2", "any")
        q.enqueue("target", "t-1", "tool")
        assert q.count_by_type("target", channel="any") == 2
        assert q.count_by_type("target", channel="user") == 1
        assert q.count_by_type("target", channel="tool") == 1

    def test_count_by_type_empty_queue(self):
        q = NudgeQueue()
        assert q.count_by_type("anything") == 0
        assert q.count_by_type("anything", channel="any") == 0


class TestPending:
    def test_pending_no_filter_returns_all_in_order(self):
        q = NudgeQueue()
        q.enqueue("a", "1", "user")
        q.enqueue("b", "2", "tool")
        q.enqueue("c", "3", "any")
        # All three, in insertion order, as (nudge_type, text) tuples.
        assert q.pending() == [("a", "1"), ("b", "2"), ("c", "3")]

    def test_pending_channel_filter(self):
        q = NudgeQueue()
        q.enqueue("a", "1", "user")
        q.enqueue("b", "2", "tool")
        q.enqueue("c", "3", "user")
        q.enqueue("d", "4", "any")
        assert q.pending("user") == [("a", "1"), ("c", "3")]
        assert q.pending("tool") == [("b", "2")]
        assert q.pending("any") == [("d", "4")]

    def test_pending_does_not_mutate(self):
        q = NudgeQueue()
        q.enqueue("a", "1", "user")
        q.enqueue("b", "2", "tool")
        # Two pending calls return same content; nothing consumed.
        first = q.pending()
        second = q.pending()
        assert first == second
        assert len(q) == 2


class TestMetadata:
    """Producer-supplied ``metadata`` rides alongside ``(type, text)`` on
    drain.  Today only ``watch_triggered`` populates it; the wire shape
    accommodates future producers (e.g. structured tool_error context)
    without another schema bump.
    """

    def test_drain_returns_metadata_when_set(self):
        q = NudgeQueue()
        meta = {"watch_name": "w1", "command": "ls", "poll_count": 2}
        q.enqueue("watch_triggered", "$ ls\nfile.txt", "any", metadata=meta)
        out = q.drain({"any"})
        assert out == [("watch_triggered", "$ ls\nfile.txt", meta)]

    def test_drain_returns_none_when_metadata_unset(self):
        q = NudgeQueue()
        q.enqueue("idle_children", "kids", "any")  # no metadata kwarg
        out = q.drain({"any"})
        assert out == [("idle_children", "kids", None)]

    def test_pending_with_metadata_projects_third_field(self):
        q = NudgeQueue()
        q.enqueue("a", "1", "user")
        q.enqueue("watch_triggered", "out", "any", metadata={"watch_name": "w"})
        snapshot = q.pending_with_metadata()
        assert snapshot == [
            ("a", "1", None),
            ("watch_triggered", "out", {"watch_name": "w"}),
        ]
        # ``pending`` (without metadata) keeps the legacy 2-tuple shape.
        assert q.pending() == [("a", "1"), ("watch_triggered", "out")]

    def test_metadata_survives_partial_drain(self):
        """A ``user``-channel drain leaves an unaffected ``tool``-channel
        entry — its metadata must still be present on the next drain."""
        q = NudgeQueue()
        q.enqueue("user_thing", "u", "user")
        q.enqueue("watch_triggered", "w-out", "tool", metadata={"watch_name": "w1"})
        # User drain doesn't touch the tool entry.
        assert q.drain({"user"}) == [("user_thing", "u", None)]
        # Tool drain still has the metadata.
        assert q.drain({"tool"}) == [
            ("watch_triggered", "w-out", {"watch_name": "w1"}),
        ]

    def test_metadata_with_valid_until_predicate(self):
        """Metadata + ``valid_until`` co-exist on the same entry; the
        predicate gate runs as before, and on a True result the metadata
        rides the drained tuple.
        """
        q = NudgeQueue()
        q.enqueue(
            "watch_triggered",
            "w-out",
            "any",
            valid_until=lambda: True,
            metadata={"watch_name": "w1", "is_final": True},
        )
        out = q.drain({"any"})
        assert out == [
            ("watch_triggered", "w-out", {"watch_name": "w1", "is_final": True}),
        ]


class TestHasPending:
    def test_has_pending_returns_false_on_empty_queue(self):
        q = NudgeQueue()
        assert q.has_pending({"user", "any"}) is False
        assert q.has_pending({"tool"}) is False

    def test_has_pending_short_circuits_on_first_match(self):
        q = NudgeQueue()
        q.enqueue("a", "1", "tool")
        q.enqueue("b", "2", "user")
        # First entry doesn't match, second does — true after walking 2.
        assert q.has_pending({"user"}) is True

    def test_has_pending_returns_false_when_no_match(self):
        q = NudgeQueue()
        q.enqueue("a", "1", "tool")
        q.enqueue("b", "2", "tool")
        assert q.has_pending({"user", "any"}) is False

    def test_has_pending_matches_any_channel(self):
        q = NudgeQueue()
        q.enqueue("a", "1", "any")
        # USER_DRAIN-shaped filter pulls "any" entries.
        assert q.has_pending(USER_DRAIN) is True
        # TOOL_DRAIN-shaped filter also pulls "any" entries.
        assert q.has_pending(TOOL_DRAIN) is True

    def test_has_pending_does_not_mutate(self):
        q = NudgeQueue()
        q.enqueue("a", "1", "user")
        q.enqueue("b", "2", "tool")
        before = q.pending()
        q.has_pending({"user"})
        q.has_pending({"tool"})
        q.has_pending(set())
        assert q.pending() == before


class TestValidation:
    def test_invalid_channel_raises(self):
        q = NudgeQueue()
        with pytest.raises(ValueError, match="channel"):
            q.enqueue("a", "1", "wake")  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            q.enqueue("b", "2", "")  # type: ignore[arg-type]
        # Queue is unaffected by the failed enqueues.
        assert len(q) == 0

    def test_channel_is_required(self):
        q = NudgeQueue()
        # No default — caller MUST pick a seam consciously.
        with pytest.raises(TypeError):
            q.enqueue("a", "1")  # type: ignore[call-arg]


class TestValidUntil:
    """``valid_until`` predicate: drain re-checks freshness; falsy /
    raising predicates drop the entry without delivery.
    """

    def test_valid_until_true_delivers(self):
        q = NudgeQueue()
        q.enqueue("a", "1", "any", valid_until=lambda: True)
        out = q.drain({"any"})
        assert out == [("a", "1", None)]

    def test_valid_until_false_drops_silently(self):
        q = NudgeQueue()
        q.enqueue("a", "1", "any", valid_until=lambda: False)
        out = q.drain({"any"})
        assert out == []
        # Already removed from queue (drain partition removes BEFORE
        # predicate check — falsy doesn't return to queue).
        assert len(q) == 0

    def test_valid_until_exception_drops_silently(self):
        q = NudgeQueue()

        def boom() -> bool:
            raise RuntimeError("predicate crash")

        q.enqueue("a", "1", "any", valid_until=boom)
        out = q.drain({"any"})
        assert out == []
        # Crash-on-predicate is treated as "no longer valid" — drop, not propagate.
        assert len(q) == 0

    def test_valid_until_evaluated_outside_lock(self):
        """The predicate may do non-trivial work (e.g. storage I/O)
        without blocking other producers.  Verify the predicate runs
        outside the queue's internal lock by enqueueing from inside
        the predicate — would deadlock if the lock was still held.
        """
        q = NudgeQueue()

        def reentrant() -> bool:
            # If the lock is held during predicate eval, this enqueue
            # would block forever (RLock would let it through, but the
            # queue uses a plain Lock).
            q.enqueue("inner", "from-predicate", "any", valid_until=lambda: True)
            return True

        q.enqueue("outer", "1", "any", valid_until=reentrant)
        out = q.drain({"any"})
        # Outer's predicate ran outside the lock, enqueued "inner";
        # outer's True return delivered "outer".  "inner" was enqueued
        # AFTER the partition snapshot, so it stays in the queue.
        assert out == [("outer", "1", None)]
        assert q.pending() == [("inner", "from-predicate")]

    def test_valid_until_only_evaluated_for_matching_channel(self):
        """A non-matching entry's predicate must NOT fire — that would
        be wasted work (or worse, a side-effecting predicate would run
        when the entry is supposed to stay queued).
        """
        q = NudgeQueue()
        calls = []

        def track() -> bool:
            calls.append(1)
            return True

        # Tool-channel entry; we drain user-channel.  Predicate must not run.
        q.enqueue("a", "1", "tool", valid_until=track)
        q.drain({"user", "any"})
        assert calls == []
        # Entry stays queued.
        assert q.pending("tool") == [("a", "1")]

    def test_valid_until_default_none_always_delivers(self):
        # No predicate → entry behaves identically to pre-PR-3 entries.
        q = NudgeQueue()
        q.enqueue("a", "1", "any")  # no valid_until kwarg
        assert q.drain({"any"}) == [("a", "1", None)]


class TestConcurrency:
    def test_concurrent_enqueue_drain_no_loss(self):
        """16 producer threads × 64 nudges = 1024 total; one consumer
        drains in a loop until producers finish + queue empty.  Verify
        every produced item is observed exactly once.
        """
        q = NudgeQueue()
        producers = 16
        per_producer = 64
        total = producers * per_producer

        produced: set[tuple[str, str]] = set()
        produced_lock = threading.Lock()
        observed: list[tuple[str, str]] = []
        observed_lock = threading.Lock()
        done_event = threading.Event()

        def produce(pid: int) -> None:
            for i in range(per_producer):
                key = (f"p{pid}", f"i{i}")
                with produced_lock:
                    produced.add(key)
                q.enqueue(key[0], key[1], "user")

        def consume() -> None:
            while not done_event.is_set() or len(q) > 0:
                drained = q.drain({"user"})
                if drained:
                    with observed_lock:
                        # Drop the trailing ``metadata`` slot — every
                        # entry here was enqueued without metadata, so
                        # the comparison set / count matches the produced
                        # ``(type, text)`` shape.
                        observed.extend((nt, txt) for nt, txt, _meta in drained)

        consumer = threading.Thread(target=consume, daemon=True)
        consumer.start()
        threads = [threading.Thread(target=produce, args=(i,)) for i in range(producers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        done_event.set()
        consumer.join(timeout=5.0)
        assert not consumer.is_alive(), "consumer didn't finish in time"

        # Every produced key observed; no duplicates.
        assert set(observed) == produced
        assert len(observed) == total
        assert len(q) == 0
