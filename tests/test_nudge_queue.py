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
        assert out == [("a", "1"), ("b", "2"), ("c", "3")]
        assert len(q) == 0

    def test_drain_filter_keeps_non_matching(self):
        q = NudgeQueue()
        q.enqueue("a", "x", "user")
        q.enqueue("b", "y", "tool")
        # Drain only user → tool entry stays.
        out = q.drain(USER_DRAIN)
        assert out == [("a", "x")]
        assert len(q) == 1
        # Now drain tool — gets the remaining entry.
        out = q.drain(TOOL_DRAIN)
        assert out == [("b", "y")]
        assert len(q) == 0

    def test_any_channel_drains_on_either_seam(self):
        q = NudgeQueue()
        q.enqueue("c", "z", "any")
        # User-seam drain pulls "any".
        assert q.drain(USER_DRAIN) == [("c", "z")]
        assert len(q) == 0
        # Re-enqueue and prove tool-seam also drains "any".
        q.enqueue("d", "w", "any")
        assert q.drain(TOOL_DRAIN) == [("d", "w")]
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
        assert q.drain({"user"}) == [("a", "1"), ("c", "3")]
        # Tool drain follows insertion order on remaining.
        assert q.drain({"tool"}) == [("b", "2"), ("d", "4")]


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
                        observed.extend(drained)

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
