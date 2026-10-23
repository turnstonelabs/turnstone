"""Token-bucket rate limiter for the turnstone HTTP server.

Rate limiting per signed-in user, or per client address on public paths such as
sign-in, with configurable burst and refill rate.  Thread-safe.  Zero external
dependencies.
"""

from __future__ import annotations

import ipaddress
import re
import threading
import time
from collections import OrderedDict

from turnstone.core.log import get_logger

log = get_logger(__name__)

_NetworkType = ipaddress.IPv4Network | ipaddress.IPv6Network


class TokenBucket:
    """Single token bucket for one client.

    ``consume()`` and ``retry_after`` are thread-safe via an internal
    lock — multiple workers (e.g. ``ChatSession._batch_evaluate_outputs``'s
    pool) can call ``consume()`` concurrently without racing on
    ``tokens`` / ``last_refill``.  ``RateLimiter`` holds its own outer
    lock for the bucket-dict it owns; the per-bucket lock here protects
    direct consumers that bypass ``RateLimiter``.
    """

    def __init__(self, rate: float, burst: int) -> None:
        self.rate = rate  # tokens per second
        self.burst = burst  # max tokens
        self.tokens = float(burst)
        self.last_refill = time.monotonic()
        self._lock = threading.Lock()

    def consume(self) -> bool:
        """Try to consume one token. Returns True if allowed."""
        with self._lock:
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
        with self._lock:
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


def bucket_key(
    user_id: str,
    direct_ip: str | None,
    forwarded_for: str,
    trusted_proxies: frozenset[_NetworkType],
) -> str | None:
    """Name the bucket a request draws on, or ``None`` when there's nothing to key on.

    A request that carries a user draws on that user's bucket wherever it comes from,
    so the callers a proxy such as the console forwards each keep their own instead of
    sharing the proxy's address.  A request to a public path, such as sign-in or the
    auth status, carries no user even from a signed-in caller, and draws on its client
    address's bucket, read through ``X-Forwarded-For`` only from trusted proxies.
    """
    if user_id:
        return f"user:{user_id}"
    if direct_ip is None:
        return None
    return "ip:" + resolve_client_ip(direct_ip, forwarded_for, trusted_proxies)


# A cancel stops work, so it's never refused: a Stop has to land however drained its caller's
# bucket is.  Only the cancel routes' full shape matches, since other routes end in a
# parameter that can read "cancel".
_CANCEL_PATH = re.compile(r"/v1/api/(?:workstreams|watches)/[^/]+/cancel")


class RateLimiter:
    """Rate limiter keeping one token bucket per key (see :func:`bucket_key`).

    A new key past ``MAX_BUCKETS`` evicts the least recently used bucket rather than
    being refused, so many addresses on public paths can't lock signed-in users out;
    an evicted key starts over with a full bucket, as it would after :meth:`cleanup`.
    """

    # The page ("/") and its static files are exempt too: one page load fetches more than 60
    # files, each revalidated on every load, and a refused script or stylesheet isn't retried.
    EXEMPT_PATHS: frozenset[str] = frozenset({"/", "/health", "/metrics", "/openapi.json", "/docs"})
    EXEMPT_PREFIXES: tuple[str, ...] = ("/static/", "/shared/")
    MAX_BUCKETS: int = 100_000

    def __init__(
        self,
        enabled: bool = False,
        rate: float = 10.0,
        burst: int = 50,
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
        self._buckets: OrderedDict[str, TokenBucket] = OrderedDict()
        self._lock = threading.Lock()

    def exempt(self, path: str) -> bool:
        """Whether a request to *path* is never limited."""
        return (
            path in self.EXEMPT_PATHS
            or path.startswith(self.EXEMPT_PREFIXES)
            or _CANCEL_PATH.fullmatch(path) is not None
        )

    def check(self, key: str, path: str) -> tuple[bool, float]:
        """Check if a request drawing on *key*'s bucket is allowed.

        Returns (allowed, retry_after_seconds).
        """
        if not self.enabled or self.exempt(path):
            return True, 0.0

        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                if len(self._buckets) >= self.MAX_BUCKETS:
                    self._buckets.popitem(last=False)
                bucket = TokenBucket(self.rate, self.burst)
                self._buckets[key] = bucket
            else:
                self._buckets.move_to_end(key)
            allowed = bucket.consume()
            retry_after = 0.0 if allowed else bucket.retry_after

        return allowed, retry_after

    def cleanup(self, max_age: float = 3600.0) -> int:
        """Remove stale buckets. Returns count removed."""
        now = time.monotonic()
        with self._lock:
            stale = [key for key, b in self._buckets.items() if (now - b.last_refill) > max_age]
            for key in stale:
                del self._buckets[key]
        return len(stale)
