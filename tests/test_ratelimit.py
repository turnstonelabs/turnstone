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


# ---------------------------------------------------------------------------
# resolve_client_ip / parse_trusted_proxies
# ---------------------------------------------------------------------------


class TestResolveClientIp:
    """X-Forwarded-For parsing with trusted proxy validation."""

    def test_no_trusted_proxies_returns_direct(self):
        from turnstone.core.ratelimit import resolve_client_ip

        result = resolve_client_ip("192.168.1.1", "10.0.0.1", frozenset())
        assert result == "192.168.1.1"

    def test_no_xff_returns_direct(self):
        from turnstone.core.ratelimit import parse_trusted_proxies, resolve_client_ip

        trusted = parse_trusted_proxies("127.0.0.0/8")
        result = resolve_client_ip("127.0.0.1", "", trusted)
        assert result == "127.0.0.1"

    def test_trusted_proxy_extracts_xff(self):
        from turnstone.core.ratelimit import parse_trusted_proxies, resolve_client_ip

        trusted = parse_trusted_proxies("127.0.0.1/32")
        result = resolve_client_ip("127.0.0.1", "1.2.3.4", trusted)
        assert result == "1.2.3.4"

    def test_untrusted_direct_ignores_xff(self):
        """If the direct client is not a trusted proxy, XFF is ignored (anti-spoof)."""
        from turnstone.core.ratelimit import parse_trusted_proxies, resolve_client_ip

        trusted = parse_trusted_proxies("10.0.0.0/8")
        result = resolve_client_ip("203.0.113.5", "1.2.3.4", trusted)
        assert result == "203.0.113.5"

    def test_chained_proxies(self):
        """XFF: 'client, proxy1, proxy2' with proxy1+proxy2 trusted → returns client."""
        from turnstone.core.ratelimit import parse_trusted_proxies, resolve_client_ip

        trusted = parse_trusted_proxies("10.0.0.0/8")
        result = resolve_client_ip("10.0.0.3", "1.2.3.4, 10.0.0.1, 10.0.0.2", trusted)
        assert result == "1.2.3.4"

    def test_all_trusted_returns_direct(self):
        """If all XFF entries are trusted proxies, fall back to direct IP."""
        from turnstone.core.ratelimit import parse_trusted_proxies, resolve_client_ip

        trusted = parse_trusted_proxies("10.0.0.0/8")
        result = resolve_client_ip("10.0.0.3", "10.0.0.1, 10.0.0.2", trusted)
        assert result == "10.0.0.3"

    def test_invalid_direct_ip_returns_direct(self):
        from turnstone.core.ratelimit import parse_trusted_proxies, resolve_client_ip

        trusted = parse_trusted_proxies("10.0.0.0/8")
        result = resolve_client_ip("not-an-ip", "1.2.3.4", trusted)
        assert result == "not-an-ip"

    def test_invalid_xff_entry_skipped(self):
        from turnstone.core.ratelimit import parse_trusted_proxies, resolve_client_ip

        trusted = parse_trusted_proxies("10.0.0.0/8")
        result = resolve_client_ip("10.0.0.1", "garbage, 1.2.3.4", trusted)
        assert result == "1.2.3.4"

    def test_ipv6_trusted_proxy(self):
        from turnstone.core.ratelimit import parse_trusted_proxies, resolve_client_ip

        trusted = parse_trusted_proxies("::1/128")
        result = resolve_client_ip("::1", "2001:db8::1", trusted)
        assert result == "2001:db8::1"


class TestParseTrustedProxies:
    def test_empty_string(self):
        from turnstone.core.ratelimit import parse_trusted_proxies

        assert parse_trusted_proxies("") == frozenset()

    def test_single_cidr(self):
        from turnstone.core.ratelimit import parse_trusted_proxies

        result = parse_trusted_proxies("10.0.0.0/8")
        assert len(result) == 1

    def test_multiple_cidrs(self):
        from turnstone.core.ratelimit import parse_trusted_proxies

        result = parse_trusted_proxies("10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16")
        assert len(result) == 3

    def test_single_ip_becomes_host_network(self):
        from turnstone.core.ratelimit import parse_trusted_proxies

        result = parse_trusted_proxies("127.0.0.1")
        assert len(result) == 1

    def test_invalid_entry_skipped(self):
        from turnstone.core.ratelimit import parse_trusted_proxies

        result = parse_trusted_proxies("10.0.0.0/8, not-valid, 172.16.0.0/12")
        assert len(result) == 2

    def test_constructor_parses_trusted_proxies(self):
        limiter = RateLimiter(enabled=True, rate=10.0, burst=5, trusted_proxies="10.0.0.0/8")
        assert len(limiter.trusted_proxies) == 1
