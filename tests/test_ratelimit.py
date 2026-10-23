"""Tests for turnstone.core.ratelimit — token-bucket rate limiter."""

from __future__ import annotations

import queue
import threading
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from starlette.testclient import TestClient

from turnstone.core.auth import JWT_AUD_SERVER, create_jwt
from turnstone.core.ratelimit import RateLimiter, TokenBucket, bucket_key, parse_trusted_proxies

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
            allowed, retry = limiter.check("1.2.3.4", "/api/workstreams/abc/send")
            assert allowed is True
            assert retry == 0.0

    def test_exempt_paths_bypass(self):
        limiter = RateLimiter(enabled=True, rate=1.0, burst=1)
        # Exhaust the bucket on a normal path
        limiter.check("1.2.3.4", "/api/workstreams/abc/send")
        limiter.check("1.2.3.4", "/api/workstreams/abc/send")

        # Exempt paths should still pass
        allowed, retry = limiter.check("1.2.3.4", "/health")
        assert allowed is True
        assert retry == 0.0

        allowed, retry = limiter.check("1.2.3.4", "/metrics")
        assert allowed is True
        assert retry == 0.0

    def test_the_page_and_its_static_files_bypass(self):
        limiter = RateLimiter(enabled=True, rate=0.001, burst=1)
        assert limiter.check("user:u1", "/api/workstreams") == (True, 0.0)

        assert limiter.check("user:u1", "/") == (True, 0.0)
        assert limiter.check("user:u1", "/static/app.js") == (True, 0.0)
        assert limiter.check("user:u1", "/shared/katex-0.19.0/katex.min.js") == (True, 0.0)
        # Only the mounts themselves: a path that merely starts the same way counts.
        assert limiter.check("user:u1", "/staticky")[0] is False

    def test_a_cancel_bypasses(self):
        limiter = RateLimiter(enabled=True, rate=0.001, burst=1)
        assert limiter.check("user:u1", "/v1/api/workstreams") == (True, 0.0)

        assert limiter.check("user:u1", "/v1/api/workstreams/abc/cancel") == (True, 0.0)
        assert limiter.check("user:u1", "/v1/api/watches/w1/cancel") == (True, 0.0)
        # Only the cancel routes' full shape: other routes end in a parameter that can
        # read "cancel".
        assert limiter.check("user:u1", "/v1/api/memories/cancel")[0] is False
        assert limiter.check("user:u1", "/v1/api/admin/settings/cancel")[0] is False
        assert limiter.check("user:u1", "/v1/api/workstreams/a/b/cancel")[0] is False

    def test_a_full_table_evicts_the_least_recently_used_bucket(self):
        """A new key past MAX_BUCKETS isn't refused: the bucket used longest ago goes."""
        limiter = RateLimiter(enabled=True, rate=0.001, burst=1)
        limiter.MAX_BUCKETS = 2
        assert limiter.check("ip:203.0.113.1", "/x") == (True, 0.0)
        assert limiter.check("user:alice", "/x") == (True, 0.0)
        assert limiter.check("ip:203.0.113.1", "/x")[0] is False  # used again, so kept

        assert limiter.check("user:bob", "/x") == (True, 0.0)
        assert list(limiter._buckets) == ["ip:203.0.113.1", "user:bob"]
        # The evicted key starts over with a full bucket.
        assert limiter.check("user:alice", "/x") == (True, 0.0)

    def test_per_ip_isolation(self):
        limiter = RateLimiter(enabled=True, rate=1.0, burst=1)

        # Exhaust IP A
        allowed_a, _ = limiter.check("10.0.0.1", "/api/workstreams/abc/send")
        assert allowed_a is True
        allowed_a, _ = limiter.check("10.0.0.1", "/api/workstreams/abc/send")
        assert allowed_a is False

        # IP B should still have its own bucket
        allowed_b, _ = limiter.check("10.0.0.2", "/api/workstreams/abc/send")
        assert allowed_b is True

    def test_burst_then_reject(self):
        limiter = RateLimiter(enabled=True, rate=10.0, burst=3)
        results = [limiter.check("1.2.3.4", "/api/workstreams/abc/send")[0] for _ in range(5)]
        assert results == [True, True, True, False, False]

    def test_cleanup_removes_stale(self):
        with patch("turnstone.core.ratelimit.time.monotonic") as mock_time:
            mock_time.return_value = 1000.0
            limiter = RateLimiter(enabled=True, rate=10.0, burst=5)

            # Create buckets for two IPs
            limiter.check("10.0.0.1", "/api/workstreams/abc/send")
            limiter.check("10.0.0.2", "/api/workstreams/abc/send")

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

            limiter.check("10.0.0.1", "/api/workstreams/abc/send")

            # Only 60s later — well within max_age
            mock_time.return_value = 1060.0
            limiter.check("10.0.0.2", "/api/workstreams/abc/send")

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


# ---------------------------------------------------------------------------
# bucket_key: whose bucket a request draws on
# ---------------------------------------------------------------------------


class TestBucketKey:
    def test_a_signed_in_request_draws_on_its_users_bucket(self):
        assert bucket_key("u1", "203.0.113.5", "", frozenset()) == "user:u1"

    def test_a_user_bucket_ignores_x_forwarded_for(self):
        trusted = parse_trusted_proxies("10.0.0.0/8")
        assert bucket_key("u1", "10.0.0.2", "203.0.113.5", trusted) == "user:u1"

    def test_a_request_before_sign_in_draws_on_its_address(self):
        assert bucket_key("", "203.0.113.5", "", frozenset()) == "ip:203.0.113.5"

    def test_an_address_bucket_reads_a_trusted_proxys_x_forwarded_for(self):
        trusted = parse_trusted_proxies("10.0.0.0/8")
        assert bucket_key("", "10.0.0.2", "203.0.113.5", trusted) == "ip:203.0.113.5"

    def test_no_user_and_no_peer_address_has_no_bucket(self):
        assert bucket_key("", None, "203.0.113.5", frozenset()) is None


# ---------------------------------------------------------------------------
# RateLimitMiddleware on the node app (#1464)
# ---------------------------------------------------------------------------

_SECRET = "test-jwt-secret-minimum-32-chars!"
# Signed in: needs a token with read scope.  Before sign-in: a public path.
_SIGNED_IN_PATH = "/v1/api/workstreams"
_PUBLIC_PATH = "/v1/api/auth/status"


def _bearer(user_id: str, source: str = "console-proxy", **claims: Any) -> dict[str, str]:
    """A node-audience token, minted as the console's proxy re-mints a caller's."""
    token = create_jwt(
        user_id=user_id,
        scopes=frozenset({"read", "write"}),
        source=source,
        secret=_SECRET,
        audience=JWT_AUD_SERVER,
        extra_claims=claims or None,
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def node_app(monkeypatch: pytest.MonkeyPatch) -> Any:
    """The node app with a burst of 2 that doesn't refill within a test."""
    import turnstone.server as srv_mod
    from turnstone.core.metrics import MetricsCollector

    monkeypatch.setattr(srv_mod, "_metrics", MetricsCollector())
    mgr = MagicMock()
    mgr.list_all.return_value = []
    mgr.max_active = 10
    return srv_mod.create_app(
        workstreams=mgr,
        global_queue=queue.Queue(),
        global_listeners=[],
        global_listeners_lock=threading.Lock(),
        skip_permissions=False,
        jwt_secret=_SECRET,
        rate_limiter=RateLimiter(enabled=True, rate=0.001, burst=2, trusted_proxies="10.0.0.0/8"),
    )


def _client(app: Any, address: str) -> TestClient:
    return TestClient(app, raise_server_exceptions=False, client=(address, 50000))


def _statuses(
    client: TestClient, path: str, n: int, headers: dict[str, str] | None = None
) -> list[int]:
    return [client.get(path, headers=headers).status_code for _ in range(n)]


class TestRateLimitMiddlewareBuckets:
    def test_two_users_behind_one_address_get_a_bucket_each(self, node_app):
        """Every caller the console forwards arrives from the console's address."""
        console = _client(node_app, "10.0.0.2")
        assert _statuses(console, _SIGNED_IN_PATH, 3, _bearer("alice")) == [200, 200, 429]
        assert _statuses(console, _SIGNED_IN_PATH, 3, _bearer("bob")) == [200, 200, 429]

    def test_one_user_from_two_addresses_shares_a_bucket(self, node_app):
        direct = _client(node_app, "203.0.113.5")
        console = _client(node_app, "10.0.0.2")
        signed_in_directly = _bearer("alice", source="password")
        assert _statuses(direct, _SIGNED_IN_PATH, 2, signed_in_directly) == [200, 200]
        assert _statuses(console, _SIGNED_IN_PATH, 1, _bearer("alice")) == [429]

    def test_a_coordinator_draws_on_its_creators_bucket(self, node_app):
        console = _client(node_app, "10.0.0.2")
        coordinator = _bearer("alice", source="coordinator", coord_ws_id="c" * 32)
        assert _statuses(console, _SIGNED_IN_PATH, 2, coordinator) == [200, 200]
        assert _statuses(console, _SIGNED_IN_PATH, 1, _bearer("alice")) == [429]
        assert _statuses(console, _SIGNED_IN_PATH, 1, _bearer("bob")) == [200]

    def test_a_request_before_sign_in_draws_on_its_address(self, node_app):
        """A public path carries no user, so its bucket is its client address's,
        apart from any signed-in user's at that address."""
        first = _client(node_app, "203.0.113.5")
        assert _statuses(first, _PUBLIC_PATH, 3) == [200, 200, 429]
        assert _statuses(_client(node_app, "198.51.100.7"), _PUBLIC_PATH, 1) == [200]
        assert _statuses(first, _SIGNED_IN_PATH, 1, _bearer("alice")) == [200]

    def test_an_address_bucket_reads_x_forwarded_for_from_a_trusted_proxy(self, node_app):
        proxy = _client(node_app, "10.0.0.2")
        one = {"X-Forwarded-For": "203.0.113.5"}
        other = {"X-Forwarded-For": "198.51.100.7"}
        assert _statuses(proxy, _PUBLIC_PATH, 3, one) == [200, 200, 429]
        assert _statuses(proxy, _PUBLIC_PATH, 1, other) == [200]

    def test_x_forwarded_for_gives_a_user_no_new_bucket(self, node_app):
        proxy = _client(node_app, "10.0.0.2")
        via_one = {**_bearer("alice"), "X-Forwarded-For": "203.0.113.5"}
        via_other = {**_bearer("alice"), "X-Forwarded-For": "198.51.100.7"}
        assert _statuses(proxy, _SIGNED_IN_PATH, 2, via_one) == [200, 200]
        assert _statuses(proxy, _SIGNED_IN_PATH, 1, via_other) == [429]

    def test_the_page_and_its_static_files_are_never_limited(self, node_app):
        """One page load fetches more files than the burst, and none draws on a bucket."""
        browser = _client(node_app, "203.0.113.5")
        page = _statuses(browser, "/", 5)
        scripts = _statuses(browser, "/static/app.js", 5)
        shared = _statuses(browser, "/shared/utils.js", 5)
        assert page == [200] * 5
        assert scripts == [200] * 5
        assert shared == [200] * 5
        # The address's bucket is still full.
        assert _statuses(browser, _PUBLIC_PATH, 3) == [200, 200, 429]

    def test_a_cancel_is_never_limited(self, node_app):
        """A Stop lands however drained its caller's bucket is, and doesn't drain it."""
        console = _client(node_app, "10.0.0.2")
        alice = _bearer("alice")
        cancels = [
            console.post(f"/v1/api/workstreams/{'a' * 32}/cancel", headers=alice).status_code
            for _ in range(5)
        ]
        assert 429 not in cancels
        assert _statuses(console, _SIGNED_IN_PATH, 3, alice) == [200, 200, 429]
        cancel_after = console.post(f"/v1/api/workstreams/{'a' * 32}/cancel", headers=alice)
        assert cancel_after.status_code != 429


def _route_paths(routes: list[Any], prefix: str = "") -> list[str]:
    """Every route path on an app, mounts expanded, with each path parameter filled in."""
    import re

    from starlette.routing import Mount, Route

    found: list[str] = []
    for route in routes:
        if isinstance(route, Mount):
            found.extend(_route_paths(route.routes, prefix + route.path))
        elif isinstance(route, Route):
            found.append(re.sub(r"\{[^}]+\}", "x", prefix + route.path))
    return found


def test_every_cancel_route_on_the_node_is_exempt(node_app):
    """A cancel route added later is exempt too, or this fails."""
    cancels = [path for path in _route_paths(node_app.routes) if path.endswith("/cancel")]
    assert cancels, "found no cancel routes on the node app"
    limiter = RateLimiter(enabled=True)
    limited = [path for path in cancels if not limiter.exempt(path)]
    assert limited == []


def test_every_static_mount_on_the_node_is_exempt(node_app):
    """A static mount added later is exempt too, or this fails."""
    from starlette.routing import Mount
    from starlette.staticfiles import StaticFiles

    def _static_mounts(routes: list[Any], prefix: str = "") -> list[str]:
        found: list[str] = []
        for route in routes:
            if isinstance(route, Mount) and isinstance(route.app, StaticFiles):
                found.append(prefix + route.path)
            elif isinstance(route, Mount):
                found.extend(_static_mounts(route.routes, prefix + route.path))
        return found

    mounts = _static_mounts(node_app.routes)
    assert mounts, "found no static mounts on the node app"
    limiter = RateLimiter(enabled=True, rate=0.001, burst=1)
    assert limiter.check("user:u1", _SIGNED_IN_PATH) == (True, 0.0)
    limited = [mount for mount in mounts if not limiter.check("user:u1", f"{mount}/app.js")[0]]
    assert limited == []
