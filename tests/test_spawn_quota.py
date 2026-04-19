"""Unit tests for SpawnBudget + TokenBucket (turnstone/core/spawn_quota.py)."""

from __future__ import annotations

import math
import time

from turnstone.core.spawn_quota import SpawnBudget, TokenBucket

# ---------------------------------------------------------------------------
# SpawnBudget
# ---------------------------------------------------------------------------


def test_budget_below_cap_allows_spawn():
    b = SpawnBudget(5)
    res = b.check(active=2)
    assert res.allowed is True
    assert res.budget == 5
    assert res.active == 2
    assert res.remaining == 3


def test_budget_at_cap_rejects_spawn():
    b = SpawnBudget(3)
    res = b.check(active=3)
    assert res.allowed is False
    assert res.remaining == 0


def test_budget_over_cap_reports_zero_remaining():
    """A stale active count above the cap still clamps remaining to 0."""
    b = SpawnBudget(3)
    res = b.check(active=5)
    assert res.allowed is False
    assert res.remaining == 0
    assert res.active == 5


def test_budget_negative_active_normalised():
    """A negative active value (shouldn't happen in practice) normalises to 0."""
    b = SpawnBudget(5)
    res = b.check(active=-3)
    assert res.allowed is True
    assert res.active == 0
    assert res.remaining == 5


def test_budget_set_mutates_cap_live():
    b = SpawnBudget(5)
    b.set_budget(10)
    assert b.budget == 10
    assert b.check(active=7).allowed is True


def test_budget_negative_constructor_clamps_to_zero():
    """A defensive floor — budget=-1 shouldn't mean "infinite spawns"."""
    b = SpawnBudget(-5)
    assert b.budget == 0
    assert b.check(active=0).allowed is False


# ---------------------------------------------------------------------------
# TokenBucket
# ---------------------------------------------------------------------------


def test_bucket_starts_full():
    """Fresh buckets grant ``burst`` immediately — the rate limit is for
    pacing a runaway, not the first wave."""
    tb = TokenBucket(tokens_per_minute=6.0, burst=5)
    # Five acquires in a row succeed.
    for _ in range(5):
        assert tb.acquire().allowed is True
    # Sixth exhausts the bucket.
    ack = tb.acquire()
    assert ack.allowed is False
    assert ack.retry_after_seconds > 0.0


def test_bucket_empty_reports_retry_after():
    tb = TokenBucket(tokens_per_minute=60.0, burst=1)  # 1 token/sec
    tb.acquire()  # drains
    ack = tb.acquire()
    assert ack.allowed is False
    # 1 token/sec → deficit 1.0 → retry ~1.0s
    assert math.isclose(ack.retry_after_seconds, 1.0, rel_tol=0.2)


def test_bucket_zero_rate_reports_infinite_retry():
    """A disabled rate (tokens_per_minute=0) shouldn't promise a retry."""
    tb = TokenBucket(tokens_per_minute=0.0, burst=2)
    tb.acquire()
    tb.acquire()
    ack = tb.acquire()
    assert ack.allowed is False
    assert ack.retry_after_seconds == float("inf")


def test_bucket_refills_over_time():
    tb = TokenBucket(tokens_per_minute=600.0, burst=1)  # 10 tokens/sec
    tb.acquire()  # empty
    assert tb.acquire().allowed is False
    time.sleep(0.15)  # ~1.5 tokens refilled; clamps to burst=1
    ack = tb.acquire()
    assert ack.allowed is True


def test_bucket_refill_clamps_to_burst():
    tb = TokenBucket(tokens_per_minute=6000.0, burst=3)  # 100/sec — saturates fast
    time.sleep(0.05)  # easily enough to refill past burst
    for _ in range(3):
        assert tb.acquire().allowed is True
    # Fourth acquire must fail even after the long idle — burst caps retention.
    assert tb.acquire().allowed is False


def test_bucket_set_rate_narrows_burst_immediately():
    tb = TokenBucket(tokens_per_minute=6.0, burst=10)  # starts with 10 tokens
    tb.set_rate(tokens_per_minute=6.0, burst=3)  # clamp down
    # Three succeed then exhausted.
    for _ in range(3):
        assert tb.acquire().allowed is True
    assert tb.acquire().allowed is False


def test_bucket_set_rate_widening_does_not_grant_free_tokens():
    """A widened burst shouldn't retroactively fill the bucket — operators
    adjusting quotas shouldn't accidentally green-light a burst."""
    tb = TokenBucket(tokens_per_minute=0.0, burst=2)
    tb.acquire()
    tb.acquire()  # bucket drained
    tb.set_rate(tokens_per_minute=0.0, burst=10)  # widen
    ack = tb.acquire()
    assert ack.allowed is False  # still empty


def test_bucket_tokens_property_is_snapshot():
    tb = TokenBucket(tokens_per_minute=0.0, burst=5)
    assert tb.tokens == 5.0
    tb.acquire()
    assert tb.tokens == 4.0
