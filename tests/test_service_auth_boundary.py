"""Tests for the invariants that protect the console ↔ node
service-auth boundary from silent drift.

Covers:

- ``_effective_user_filter`` on ``turnstone.console.server`` — the
  three-way return (None / caller_uid / DENY_EMPTY_SUB) and the four
  decision branches (admin, service-scope, blank-sub non-service,
  normal uid).
- ``_effective_user_filter`` on ``turnstone.server`` — mirror of the
  above minus the admin bypass (node-side has no admin concept).
- ``_verify_collector_service_scope`` — 409 probe OK path,
  403 drift → ``collector_scope_error`` set + ERROR log,
  transient failures → no refuse-to-serve.
- ``cluster_snapshot`` + ``cluster_events_sse`` endpoints gate on
  ``collector_scope_error`` and return 503 with a remediation hint.
- ``_NodeDashboardCache.get`` logs 4xx at WARNING with status + body
  preview.
- Cross-module identity of the ``DENY_EMPTY_SUB`` sentinel.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest

from turnstone.core.auth import AuthResult

# ---------------------------------------------------------------------------
# Shared builders
# ---------------------------------------------------------------------------


def _request_with_auth(
    *,
    user_id: str = "",
    scopes: frozenset[str] = frozenset(),
    permissions: frozenset[str] = frozenset(),
) -> MagicMock:
    """Build a MagicMock Request with an AuthResult on ``request.state``.

    Matches the shape the auth middleware attaches so the helpers
    under test exercise the real auth-reading path.
    """
    request = MagicMock()
    request.state.auth_result = AuthResult(
        user_id=user_id,
        scopes=scopes,
        token_source="test",
        permissions=permissions,
    )
    return request


# ---------------------------------------------------------------------------
# _effective_user_filter — the console edition was deleted alongside the
# row-level ownership gates (trusted-team unification).  Only the server
# edition survives — it still differentiates service callers (cluster-
# wide) from scoped users (tenant-pinned aggregates on node endpoints).
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _effective_user_filter — server edition (service, uid, DENY — no admin)
# ---------------------------------------------------------------------------


class TestServerEffectiveUserFilter:
    def test_service_scope_returns_none(self):
        from turnstone.server import _effective_user_filter

        req = _request_with_auth(user_id="console-proxy", scopes=frozenset({"service"}))
        assert _effective_user_filter(req) is None

    def test_scoped_caller_returns_uid(self):
        from turnstone.server import _effective_user_filter

        req = _request_with_auth(user_id="alice", scopes=frozenset({"read"}))
        assert _effective_user_filter(req) == "alice"

    def test_blank_sub_non_service_returns_deny(self):
        from turnstone.server import DENY_EMPTY_SUB, _effective_user_filter

        req = _request_with_auth(user_id="", scopes=frozenset({"read"}))
        assert _effective_user_filter(req) is DENY_EMPTY_SUB

    def test_server_has_no_admin_bypass(self):
        """Server-side has no admin-permissions concept — ``admin.users``
        must NOT bypass the tenant filter on node endpoints.  Only the
        service scope crosses tenants."""
        from turnstone.server import _effective_user_filter

        req = _request_with_auth(
            user_id="alice",
            scopes=frozenset({"read"}),
            permissions=frozenset({"admin.users"}),
        )
        # Admin perm is ignored; caller is a scoped user.
        assert _effective_user_filter(req) == "alice"


# ---------------------------------------------------------------------------
# Boot self-check — _verify_collector_service_scope
# ---------------------------------------------------------------------------


def _scope_probe_app(
    *,
    services: list[dict] | None = None,
    token: str = "probe-token",
) -> MagicMock:
    """Build a MagicMock ``app`` with the state the self-check reads."""
    storage = MagicMock()
    storage.list_services.return_value = services or []
    token_mgr = SimpleNamespace(token=token)
    app = MagicMock()
    app.state.auth_storage = storage
    app.state.collector_token_mgr = token_mgr
    app.state.collector_scope_error = ""
    return app


class TestVerifyCollectorServiceScope:
    @pytest.mark.anyio
    async def test_409_probe_leaves_scope_error_empty(self):
        """A 409 response means the scope gate passed; the probe's
        deliberately-wrong node_id tripped the identity check only
        after auth was accepted.  This is the happy path."""
        from turnstone.console.server import _verify_collector_service_scope

        app = _scope_probe_app(
            services=[
                {"service_id": "node-1", "url": "http://node-1:8001"},
            ]
        )

        def handler(request: httpx.Request) -> httpx.Response:
            # Caller MUST probe with expected_node_id set to the
            # sentinel so the server returns 409 before opening a
            # stream.
            assert "expected_node_id=_scope-probe_" in str(request.url)
            assert request.headers["authorization"] == "Bearer probe-token"
            return httpx.Response(409, text='{"error":"node_id mismatch"}')

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        await _verify_collector_service_scope(app, client)
        assert app.state.collector_scope_error == ""

    @pytest.mark.anyio
    async def test_403_probe_sets_scope_error_and_logs_error(self, caplog):
        """403 from the probe means the collector token is missing the
        ``service`` scope (or the JWT audience is misconfigured).  The
        probe must (1) set ``app.state.collector_scope_error`` non-empty
        with a remediation hint and (2) log at ERROR so operators see
        the drift at boot rather than chasing empty-dashboard reports."""
        from turnstone.console.server import _verify_collector_service_scope

        app = _scope_probe_app(services=[{"service_id": "node-1", "url": "http://node-1:8001"}])

        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(403, text='{"error":"service scope required"}')

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with caplog.at_level(logging.ERROR, logger="turnstone.console.server"):
            await _verify_collector_service_scope(app, client)

        err = app.state.collector_scope_error
        assert err, "403 probe must populate collector_scope_error"
        assert "collector token rejected" in err
        assert "HTTP 403" in err
        assert any(
            rec.levelno == logging.ERROR and "collector_scope_probe.drift" in rec.getMessage()
            for rec in caplog.records
        ), "403 drift must log at ERROR so operators see it at boot"

    @pytest.mark.anyio
    async def test_401_also_sets_scope_error(self):
        """401 (JWT audience / secret mismatch) is the same configuration
        class as 403 — refuse to serve."""
        from turnstone.console.server import _verify_collector_service_scope

        app = _scope_probe_app(services=[{"service_id": "node-1", "url": "http://node-1:8001"}])

        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(401, text="unauthorized")

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        await _verify_collector_service_scope(app, client)
        assert "HTTP 401" in app.state.collector_scope_error

    @pytest.mark.anyio
    async def test_no_registered_nodes_skips_silently(self, caplog):
        """Single-node or pre-discovery states have no upstream to
        probe.  The self-check must NOT refuse to serve — the dashboard
        simply has no cluster data to render yet."""
        from turnstone.console.server import _verify_collector_service_scope

        app = _scope_probe_app(services=[])
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: httpx.Response(500)))
        with caplog.at_level(logging.INFO, logger="turnstone.console.server"):
            await _verify_collector_service_scope(app, client)
        assert app.state.collector_scope_error == ""

    @pytest.mark.anyio
    async def test_network_error_does_not_refuse(self):
        """Transient httpx.ConnectError during probe is a "cluster is
        coming up" state; it must NOT be confused with scope drift.
        Leave ``collector_scope_error`` empty and log a warning."""
        from turnstone.console.server import _verify_collector_service_scope

        app = _scope_probe_app(services=[{"service_id": "node-1", "url": "http://node-1:8001"}])

        def handler(_req: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        await _verify_collector_service_scope(app, client)
        assert app.state.collector_scope_error == ""


# ---------------------------------------------------------------------------
# Gated dashboard endpoints — cluster_snapshot + cluster_events_sse
# ---------------------------------------------------------------------------


class TestClusterDashboardGate:
    @pytest.mark.anyio
    async def test_cluster_snapshot_503_when_scope_error(self):
        from turnstone.console.server import cluster_snapshot

        request = MagicMock()
        request.app.state.collector_scope_error = "collector token rejected by node-1"
        resp = await cluster_snapshot(request)
        assert resp.status_code == 503
        import json

        body = json.loads(resp.body)
        assert body["reason"] == "collector_scope_drift"
        assert "collector token rejected" in body["error"]

    @pytest.mark.anyio
    async def test_cluster_snapshot_200_when_scope_ok(self):
        from turnstone.console.server import cluster_snapshot

        request = MagicMock()
        request.app.state.collector_scope_error = ""
        request.app.state.collector.get_snapshot.return_value = {"nodes": []}
        resp = await cluster_snapshot(request)
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Dashboard cache — 4xx log-warning floor (0d)
# ---------------------------------------------------------------------------


class TestDashboardCache4xxLogLevel:
    @pytest.mark.anyio
    async def test_403_logged_at_warning_with_preview(self, caplog):
        """A 4xx from the upstream dashboard fetch must log at WARNING
        with the upstream body preview — silence here hides auth/scope
        drift behind an empty dashboard."""
        from turnstone.console.server import _NodeDashboardCache

        cache = _NodeDashboardCache()

        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(403, text='{"error":"service scope required"}')

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with caplog.at_level(logging.WARNING, logger="turnstone.console.server"):
            payload = await cache.get("node-1", "http://node-1:8001", client, {})

        assert payload is None  # 4xx → no payload cached
        matches = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING
            and "dashboard_cache" in r.getMessage()
            and "403" in r.getMessage()
        ]
        assert matches, "4xx from dashboard fetch must log at WARNING"
        assert "service scope required" in matches[0].getMessage()

    @pytest.mark.anyio
    async def test_200_does_not_log(self, caplog):
        """The happy path stays quiet — only 4xx raises the log floor."""
        from turnstone.console.server import _NodeDashboardCache

        cache = _NodeDashboardCache()

        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"workstreams": []})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with caplog.at_level(logging.WARNING, logger="turnstone.console.server"):
            payload = await cache.get("node-1", "http://node-1:8001", client, {})

        assert payload == {"workstreams": []}
        assert not [r for r in caplog.records if "dashboard_cache" in r.getMessage()]

    @pytest.mark.anyio
    async def test_4xx_does_not_cache_payload_none(self):
        """On 4xx the dashboard cache must skip the TTL write so an
        operator scope fix shows up on the next request instead of
        after the cache expires.  Regression lock for the per-node
        ``asyncio.Lock`` already handling hot-loop protection."""
        from turnstone.console.server import _NodeDashboardCache

        cache = _NodeDashboardCache()
        calls = {"n": 0}

        def handler(_req: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(403, text="forbidden")
            return httpx.Response(200, json={"workstreams": [{"ws_id": "ws-1"}]})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        first = await cache.get("node-1", "http://node-1:8001", client, {})
        second = await cache.get("node-1", "http://node-1:8001", client, {})
        assert first is None
        assert second == {"workstreams": [{"ws_id": "ws-1"}]}
        assert calls["n"] == 2, "4xx must bypass the cache so the retry reaches upstream"


# ---------------------------------------------------------------------------
# _fetch_live_block — 4xx log floor on the direct-fetch fallback (0d)
# ---------------------------------------------------------------------------


class TestFetchLiveBlock4xxLogLevel:
    @pytest.mark.anyio
    async def test_direct_fallback_logs_warning_on_4xx(self, caplog, monkeypatch):
        """Test harnesses / legacy embeddings skip the dashboard cache
        and fall through to the direct-fetch path inside
        ``_fetch_live_block``.  4xx there must surface at WARNING —
        the silence the cache path previously had also applied here."""
        from turnstone.console.server import _fetch_live_block

        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(401, text='{"error":"JWT audience mismatch"}')

        # Build the request with explicit state so _proxy_auth_headers
        # takes the empty-headers fallback (no auth_result, no
        # jwt_secret, no service-token manager); we're exercising the
        # 4xx branch, not the token-mint path.
        request = MagicMock()
        request.state = SimpleNamespace(auth_result=None)
        request.app.state = SimpleNamespace(
            proxy_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            proxy_token_mgr=None,
            jwt_secret="",
            dashboard_cache=None,  # force the direct-fetch branch
            coord_mgr=None,
        )

        # Shim _get_server_url so we don't need the full cluster-
        # router wiring to resolve node_id → URL.  monkeypatch handles
        # the restore automatically.
        monkeypatch.setattr(
            "turnstone.console.server._get_server_url",
            lambda _req, _nid: "http://node-1:8001",
        )

        with caplog.at_level(logging.WARNING, logger="turnstone.console.server"):
            live = await _fetch_live_block(
                request, {"node_id": "node-1", "kind": "interactive"}, "ws-abc"
            )

        assert live is None
        matches = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING and "proxy.live_block.4xx" in r.getMessage()
        ]
        assert matches, "4xx from the direct fetch must log at WARNING"
        assert "401" in matches[0].getMessage()
        assert "JWT audience mismatch" in matches[0].getMessage()


# ---------------------------------------------------------------------------
# _proxy_sse — 4xx log floor on the streaming path (0d)
# ---------------------------------------------------------------------------


class TestProxySseNon200LogLevel:
    @pytest.mark.anyio
    async def test_non_200_upstream_logs_warning_with_preview(self, caplog):
        """Non-200 on a service-auth SSE proxy hop is operator-
        actionable; the browser already sees the error event, but
        operators need the drift in ops logs too."""
        from starlette.requests import Request

        from turnstone.console.server import _proxy_sse

        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(403, text='{"error":"service scope required\\n"}')

        sse_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        proxy_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/node/n/api/events",
            "headers": [],
            "query_string": b"",
            "app": MagicMock(
                state=SimpleNamespace(proxy_sse_client=sse_client, proxy_client=proxy_client)
            ),
        }

        async def _receive():
            return {"type": "http.request", "body": b""}

        request = Request(scope, receive=_receive)
        # MagicMock on app.state.proxy_sse_client above is covered by
        # the SimpleNamespace; auth headers fall through to the
        # fallback empty-dict path since _proxy_auth_headers sees no
        # auth_result.

        with caplog.at_level(logging.WARNING, logger="turnstone.console.server"):
            response = await _proxy_sse(request, "http://node-1:8001", "events", api_prefix="api")
            # Drain the streaming body so the async gen executes.
            async for _ in response.body_iterator:  # type: ignore[attr-defined]
                pass

        matches = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING and "proxy.sse.non_200" in r.getMessage()
        ]
        assert matches, "non-200 from SSE proxy must log at WARNING"
        # Control-char scrub replaces the literal \n byte with a
        # space so the preview can't forge a log-line break.
        assert "\n" not in matches[0].getMessage().split("body=", 1)[-1]


# ---------------------------------------------------------------------------
# Gated cluster_events_sse — 503 on scope error (0a)
# ---------------------------------------------------------------------------


class TestClusterEventsSseGate:
    @pytest.mark.anyio
    async def test_cluster_events_sse_503_when_scope_error(self):
        from turnstone.console.server import cluster_events_sse

        request = MagicMock()
        request.app.state.collector_scope_error = (
            "collector token rejected by node-1 — upstream_body=<<<forbidden>>>"
        )
        resp = await cluster_events_sse(request)
        assert resp.status_code == 503
        import json

        body = json.loads(resp.body)
        assert body["reason"] == "collector_scope_drift"


# ---------------------------------------------------------------------------
# Cross-module identity of DENY_EMPTY_SUB (q-4)
# ---------------------------------------------------------------------------


class TestDenySentinelSharedIdentity:
    def test_core_and_server_share_one_sentinel(self):
        """The sentinel is compared with ``is``; a future refactor
        that re-introduced per-module duplicates would silently break
        the identity check.  Lock the cross-module invariant.

        Only the server + core surfaces consume the sentinel after the
        trusted-team unification — the console no longer gates on
        row ownership, so its ``_effective_user_filter`` was removed."""
        from turnstone.core.auth import DENY_EMPTY_SUB as CORE_DENY
        from turnstone.server import DENY_EMPTY_SUB as SERVER_DENY

        assert CORE_DENY is SERVER_DENY


# ---------------------------------------------------------------------------
# _bounded_body_preview control-char scrub (sec-1)
# ---------------------------------------------------------------------------


class TestBoundedBodyPreviewScrub:
    def test_control_chars_replaced_with_space(self):
        """CR/LF/NUL/TAB in upstream bodies must not appear raw in
        logs or in the operator-facing 503 ``collector_scope_error``
        — otherwise an attacker-controllable upstream can forge
        additional log lines or embed fake remediation text."""
        from turnstone.console.server import _bounded_body_preview

        preview = _bounded_body_preview("line-a\nline-b\r\nNUL\x00TAB\t")
        assert "\n" not in preview
        assert "\r" not in preview
        assert "\x00" not in preview
        assert "\t" not in preview
        # Structure is preserved with spaces, so operators can still
        # read the body preview meaningfully.
        assert "line-a" in preview
        assert "line-b" in preview

    def test_accepts_bytes_and_decodes(self):
        from turnstone.console.server import _bounded_body_preview

        preview = _bounded_body_preview(b"hello\nworld")
        assert preview == "hello world"

    def test_caps_at_requested_length(self):
        from turnstone.console.server import _bounded_body_preview

        preview = _bounded_body_preview("x" * 1000, cap=50)
        assert len(preview) == 50


# ---------------------------------------------------------------------------
# coordinator_metrics DENY short-circuit — shape matches happy path (bug-1)
# ---------------------------------------------------------------------------


class TestCoordinatorMetricsDenyShape:
    def test_zero_payload_matches_success_keys(self):
        """The DENY short-circuit in coordinator_metrics must emit the
        same key set as the success path so strict-schema consumers
        don't break on the blank-sub branch."""
        from turnstone.console.server import _coordinator_metrics_payload

        zero = _coordinator_metrics_payload(ws_id="a" * 32)
        happy = _coordinator_metrics_payload(
            ws_id="a" * 32,
            spawns_total=5,
            spawns_last_hour=2,
            child_state_counts={"idle": 3},
            judge_fallback_rate=0.1,
            intent_verdicts_sample=10,
        )
        assert set(zero.keys()) == set(happy.keys()), (
            "DENY payload key set must match success payload — "
            "otherwise a future field addition silently drifts"
        )
        # The zero payload carries ws_id through so consumers that
        # key on it don't drop the response.
        assert zero["ws_id"] == "a" * 32


# ---------------------------------------------------------------------------
# Probe URL allowlist (sec-3)
# ---------------------------------------------------------------------------


class TestProbeUrlAllowlist:
    def test_rejects_non_http_scheme(self):
        """Probe URL picker must reject non-http(s) schemes so a
        poisoned service-registry entry can't redirect the probe
        through a ``file://`` or ``gs://`` transport."""
        from turnstone.console.server import _probe_candidate_url

        url, nid = _probe_candidate_url([{"service_id": "node-x", "url": "file:///etc/passwd"}])
        assert (url, nid) == ("", "")

    def test_rejects_link_local_host(self):
        """169.254.0.0/16 is the cloud metadata range; a poisoned
        entry pointing there would turn the probe into an SSRF to
        IMDS."""
        from turnstone.console.server import _probe_candidate_url

        url, nid = _probe_candidate_url(
            [{"service_id": "node-x", "url": "http://169.254.169.254:80"}]
        )
        assert (url, nid) == ("", "")

    def test_accepts_loopback_for_dev(self):
        """Single-box dev setups register the node at 127.0.0.1 — the
        allowlist must let that through."""
        from turnstone.console.server import _probe_candidate_url

        url, nid = _probe_candidate_url([{"service_id": "node-x", "url": "http://127.0.0.1:8001"}])
        assert nid == "node-x"
        assert url == "http://127.0.0.1:8001"

    def test_skips_malformed_entries(self):
        """Entries missing url or service_id are skipped so the loop
        falls through to the next candidate."""
        from turnstone.console.server import _probe_candidate_url

        url, nid = _probe_candidate_url(
            [
                {"service_id": "", "url": "http://node-a:8001"},
                {"service_id": "node-b", "url": ""},
                {"service_id": "node-c", "url": "http://node-c:8001"},
            ]
        )
        assert nid == "node-c"
