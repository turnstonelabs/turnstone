"""Tests for turnstone.core.ratelimit — token-bucket rate limiter."""

from __future__ import annotations

from unittest.mock import patch

from turnstone.core.ratelimit import RateLimiter, TokenBucket

# ---------------------------------------------------------------------------
# TestTokenBucket
# ---------------------------------------------------------------------------


class TestTokenBucket:
    def test_initial_burst_allows(self):
        bucket = TokenBucket(rate=10.0, burst=5)
        for _ in range(5):
            assert bucket.consume() is True

    def test_exhausted_rejects(self):
        bucket = TokenBucket(rate=10.0, burst=2)
        assert bucket.consume() is True
        assert bucket.consume() is True
        assert bucket.consume() is False

    def test_refill_over_time(self):
        with patch("turnstone.core.ratelimit.time.monotonic") as mock_time:
            mock_time.return_value = 1000.0
            bucket = TokenBucket(rate=10.0, burst=2)

            # Drain all tokens
            assert bucket.consume() is True
            assert bucket.consume() is True
            assert bucket.consume() is False

            # Advance time by 0.2s => 2.0 tokens refilled (rate=10/s)
            mock_time.return_value = 1000.2
            assert bucket.consume() is True
            assert bucket.consume() is True
            assert bucket.consume() is False

    def test_retry_after_calculation(self):
        with patch("turnstone.core.ratelimit.time.monotonic") as mock_time:
            mock_time.return_value = 1000.0
            bucket = TokenBucket(rate=5.0, burst=1)

            assert bucket.consume() is True
            assert bucket.consume() is False

            # 0 tokens remaining, rate=5/s => 1.0/5.0 = 0.2s
            retry = bucket.retry_after
            assert 0.19 <= retry <= 0.21


# ---------------------------------------------------------------------------
# TestRateLimiter
# ---------------------------------------------------------------------------


class TestRateLimiter:
    def test_disabled_allows_everything(self):
        limiter = RateLimiter(enabled=False, rate=1.0, burst=1)
        for _ in range(100):
            allowed, retry = limiter.check("1.2.3.4", "/api/send")
            assert allowed is True
            assert retry == 0.0

    def test_exempt_paths_bypass(self):
        limiter = RateLimiter(enabled=True, rate=1.0, burst=1)
        # Exhaust the bucket on a normal path
        limiter.check("1.2.3.4", "/api/send")
        limiter.check("1.2.3.4", "/api/send")

        # Exempt paths should still pass
        allowed, retry = limiter.check("1.2.3.4", "/health")
        assert allowed is True
        assert retry == 0.0

        allowed, retry = limiter.check("1.2.3.4", "/metrics")
        assert allowed is True
        assert retry == 0.0

    def test_per_ip_isolation(self):
        limiter = RateLimiter(enabled=True, rate=1.0, burst=1)

        # Exhaust IP A
        allowed_a, _ = limiter.check("10.0.0.1", "/api/send")
        assert allowed_a is True
        allowed_a, _ = limiter.check("10.0.0.1", "/api/send")
        assert allowed_a is False

        # IP B should still have its own bucket
        allowed_b, _ = limiter.check("10.0.0.2", "/api/send")
        assert allowed_b is True

    def test_burst_then_reject(self):
        limiter = RateLimiter(enabled=True, rate=10.0, burst=3)
        results = [limiter.check("1.2.3.4", "/api/send")[0] for _ in range(5)]
        assert results == [True, True, True, False, False]

    def test_cleanup_removes_stale(self):
        with patch("turnstone.core.ratelimit.time.monotonic") as mock_time:
            mock_time.return_value = 1000.0
            limiter = RateLimiter(enabled=True, rate=10.0, burst=5)

            # Create buckets for two IPs
            limiter.check("10.0.0.1", "/api/send")
            limiter.check("10.0.0.2", "/api/send")

            # Advance time past max_age for both
            mock_time.return_value = 5000.0
            removed = limiter.cleanup(max_age=3600.0)
            assert removed == 2

            # Internal state should be empty
            assert len(limiter._buckets) == 0

    def test_cleanup_keeps_recent(self):
        with patch("turnstone.core.ratelimit.time.monotonic") as mock_time:
            mock_time.return_value = 1000.0
            limiter = RateLimiter(enabled=True, rate=10.0, burst=5)

            limiter.check("10.0.0.1", "/api/send")

            # Only 60s later — well within max_age
            mock_time.return_value = 1060.0
            limiter.check("10.0.0.2", "/api/send")

            mock_time.return_value = 1060.0
            removed = limiter.cleanup(max_age=3600.0)
            # 10.0.0.1 last_refill=1000, age=60 < 3600 => kept
            # 10.0.0.2 last_refill=1060, age=0 < 3600 => kept
            assert removed == 0
            assert len(limiter._buckets) == 2
