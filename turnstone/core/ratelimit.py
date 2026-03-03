"""Token-bucket rate limiter for the turnstone HTTP server.

Per-IP rate limiting with configurable burst and refill rate.
Thread-safe. Zero external dependencies.
"""

from __future__ import annotations

import threading
import time


class TokenBucket:
    """Single token bucket for one client."""

    def __init__(self, rate: float, burst: int) -> None:
        self.rate = rate  # tokens per second
        self.burst = burst  # max tokens
        self.tokens = float(burst)
        self.last_refill = time.monotonic()

    def consume(self) -> bool:
        """Try to consume one token. Returns True if allowed."""
        now = time.monotonic()
        elapsed = now - self.last_refill
        self.tokens = min(self.burst, self.tokens + elapsed * self.rate)
        self.last_refill = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False

    @property
    def retry_after(self) -> float:
        """Seconds until next token is available."""
        if self.tokens >= 1.0:
            return 0.0
        return (1.0 - self.tokens) / self.rate


class RateLimiter:
    """Per-IP rate limiter using token buckets."""

    EXEMPT_PATHS: frozenset[str] = frozenset({"/health", "/metrics"})
    MAX_BUCKETS: int = 100_000

    def __init__(
        self,
        enabled: bool = False,
        rate: float = 10.0,
        burst: int = 20,
    ) -> None:
        if enabled and rate <= 0:
            raise ValueError(f"rate must be > 0 when enabled, got {rate}")
        if enabled and burst < 1:
            raise ValueError(f"burst must be >= 1 when enabled, got {burst}")
        self.enabled = enabled
        self.rate = rate
        self.burst = burst
        self._buckets: dict[str, TokenBucket] = {}
        self._lock = threading.Lock()

    def check(self, client_ip: str, path: str) -> tuple[bool, float]:
        """Check if request is allowed.

        Returns (allowed, retry_after_seconds).
        """
        if not self.enabled:
            return True, 0.0
        if path in self.EXEMPT_PATHS:
            return True, 0.0

        with self._lock:
            bucket = self._buckets.get(client_ip)
            if bucket is None:
                if len(self._buckets) >= self.MAX_BUCKETS:
                    return False, 1.0
                bucket = TokenBucket(self.rate, self.burst)
                self._buckets[client_ip] = bucket
            allowed = bucket.consume()
            retry_after = 0.0 if allowed else bucket.retry_after

        return allowed, retry_after

    def cleanup(self, max_age: float = 3600.0) -> int:
        """Remove stale buckets. Returns count removed."""
        now = time.monotonic()
        with self._lock:
            stale = [ip for ip, b in self._buckets.items() if (now - b.last_refill) > max_age]
            for ip in stale:
                del self._buckets[ip]
        return len(stale)
