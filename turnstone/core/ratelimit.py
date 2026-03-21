"""Token-bucket rate limiter for the turnstone HTTP server.

Per-IP rate limiting with configurable burst and refill rate.
Thread-safe. Zero external dependencies.
"""

from __future__ import annotations

import ipaddress
import threading
import time

from turnstone.core.log import get_logger

log = get_logger(__name__)

_NetworkType = ipaddress.IPv4Network | ipaddress.IPv6Network


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


def parse_trusted_proxies(raw: str) -> frozenset[_NetworkType]:
    """Parse a comma-separated list of IPs/CIDRs into a frozen set of networks."""
    if not raw or not raw.strip():
        return frozenset()
    nets: list[_NetworkType] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        try:
            nets.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            log.warning("Ignoring invalid trusted_proxies entry: %r", entry)
    return frozenset(nets)


def _normalize_ip(
    addr: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """Collapse ``::ffff:x.x.x.x`` to its IPv4 form for dual-stack compatibility."""
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    return addr


def resolve_client_ip(
    direct_ip: str,
    forwarded_for: str,
    trusted_proxies: frozenset[_NetworkType],
) -> str:
    """Extract real client IP from X-Forwarded-For, only trusting known proxies.

    Uses the rightmost-untrusted approach: walks the XFF header right-to-left
    and returns the first IP not in the trusted set.  If the direct client IP
    is not a trusted proxy, XFF is ignored entirely (prevents spoofing).

    IPv4-mapped IPv6 addresses (``::ffff:x.x.x.x``) are normalized to IPv4
    before checking against trusted proxies for dual-stack compatibility.
    """
    if not trusted_proxies or not forwarded_for:
        try:
            return str(_normalize_ip(ipaddress.ip_address(direct_ip)))
        except ValueError:
            return direct_ip
    try:
        addr = _normalize_ip(ipaddress.ip_address(direct_ip))
    except ValueError:
        return direct_ip
    if not any(addr in net for net in trusted_proxies):
        return str(addr)
    parts = [p.strip() for p in forwarded_for.split(",") if p.strip()]
    for ip_str in reversed(parts):
        try:
            ip = _normalize_ip(ipaddress.ip_address(ip_str))
        except ValueError:
            continue
        if not any(ip in net for net in trusted_proxies):
            return str(ip)
    return str(addr)


class RateLimiter:
    """Per-IP rate limiter using token buckets."""

    EXEMPT_PATHS: frozenset[str] = frozenset({"/health", "/metrics", "/openapi.json", "/docs"})
    MAX_BUCKETS: int = 100_000

    def __init__(
        self,
        enabled: bool = False,
        rate: float = 10.0,
        burst: int = 20,
        trusted_proxies: str = "",
    ) -> None:
        if enabled and rate <= 0:
            raise ValueError(f"rate must be > 0 when enabled, got {rate}")
        if enabled and burst < 1:
            raise ValueError(f"burst must be >= 1 when enabled, got {burst}")
        self.enabled = enabled
        self.rate = rate
        self.burst = burst
        self.trusted_proxies: frozenset[_NetworkType] = parse_trusted_proxies(trusted_proxies)
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
