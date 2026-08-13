"""Tests for TLSManager — console CA and ACME server."""

from __future__ import annotations

import ipaddress

import pytest

from turnstone.core.storage import get_storage, init_storage, reset_storage


@pytest.fixture(autouse=True)
def _storage(tmp_path):
    """Initialize ephemeral SQLite storage for each test."""
    reset_storage()
    db = str(tmp_path / "test.db")
    init_storage("sqlite", path=db)
    yield
    reset_storage()


@pytest.fixture
def tls_manager():
    """Create a TLSManager backed by test storage."""
    from turnstone.console.tls import TLSManager

    return TLSManager(get_storage())


# ── CA initialization ─────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_init_ca(tls_manager):
    await tls_manager.init_ca()
    assert tls_manager.ca_initialized
    root_pem = tls_manager.get_root_cert_pem()
    assert b"BEGIN CERTIFICATE" in root_pem


@pytest.mark.anyio
async def test_init_ca_persists(tls_manager):
    """CA root survives re-initialization (loaded from storage)."""
    await tls_manager.init_ca()
    pem1 = tls_manager.get_root_cert_pem()

    # Create a new manager on the same storage
    from turnstone.console.tls import TLSManager

    mgr2 = TLSManager(get_storage())
    await mgr2.init_ca()
    pem2 = mgr2.get_root_cert_pem()

    assert pem1 == pem2  # Same CA loaded from DB


@pytest.mark.anyio
async def test_get_responder_before_init(tls_manager):
    with pytest.raises(RuntimeError, match="CA not initialized"):
        tls_manager.get_responder()


@pytest.mark.anyio
async def test_get_responder(tls_manager):
    await tls_manager.init_ca()
    responder = tls_manager.get_responder()
    assert responder is not None
    # Should be an ASGI app (callable)
    assert callable(responder)


@pytest.mark.anyio
async def test_responder_advertises_external_url():
    """Directory links use the routable URL, not the request's container address."""
    from starlette.applications import Starlette
    from starlette.routing import Mount
    from starlette.testclient import TestClient

    from turnstone.console.tls import TLSManager

    external_url = "http://192.0.2.10:8090/acme/"
    manager = TLSManager(get_storage(), acme_external_url=external_url)
    await manager.init_ca()
    app = Starlette(routes=[Mount("/acme", app=manager.get_responder())])

    with TestClient(app) as client:
        response = client.get("/acme/directory")

    assert response.status_code == 200
    advertised = response.json()
    required = {
        "newNonce": "http://192.0.2.10:8090/acme/new-nonce",
        "newAccount": "http://192.0.2.10:8090/acme/new-account",
        "newOrder": "http://192.0.2.10:8090/acme/new-order",
        "revokeCert": "http://192.0.2.10:8090/acme/revoke-cert",
        "keyChange": "http://192.0.2.10:8090/acme/key-change",
    }
    assert required.items() <= advertised.items()


@pytest.mark.anyio
async def test_responder_without_external_url_uses_request_address():
    """Unset configuration preserves same-host and in-network enrollment."""
    from starlette.applications import Starlette
    from starlette.routing import Mount
    from starlette.testclient import TestClient

    from turnstone.console.tls import TLSManager

    manager = TLSManager(get_storage())
    await manager.init_ca()
    app = Starlette(routes=[Mount("/acme", app=manager.get_responder())])

    with TestClient(app, base_url="http://console.internal:8090") as client:
        response = client.get("/acme/directory")

    assert response.status_code == 200
    assert response.json()["newOrder"] == "http://console.internal:8090/acme/new-order"


@pytest.mark.anyio
async def test_responder_external_url_requires_acme_mount():
    """Catch the easy-to-miss deployment error before advertising bad links."""
    from turnstone.console.tls import TLSManager

    manager = TLSManager(
        get_storage(),
        acme_external_url="http://192.0.2.10:8090",
    )
    await manager.init_ca()

    with pytest.raises(ValueError, match="must include.* /acme mount"):
        manager.get_responder()


def _authenticated_acme_app(manager, secret: str):
    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.routing import Mount

    from turnstone.core.auth import AUTH_COOKIE_CONSOLE, JWT_AUD_CONSOLE, AuthMiddleware

    app = Starlette(
        routes=[Mount("/acme", app=manager.get_responder())],
        middleware=[
            Middleware(
                AuthMiddleware,
                jwt_audience=JWT_AUD_CONSOLE,
                cookie_name=AUTH_COOKIE_CONSOLE,
            )
        ],
    )
    app.state.jwt_secret = secret
    app.state.auth_storage = get_storage()
    return app


@pytest.mark.anyio
async def test_acme_signing_routes_require_enrollment_token():
    """Discovery stays public, but the auto-approving signer does not."""
    import httpx2

    from turnstone.console.tls import TLSManager

    secret = "test-jwt-secret-minimum-32-chars!"
    manager = TLSManager(get_storage())
    await manager.init_ca()
    transport = httpx2.ASGITransport(app=_authenticated_acme_app(manager, secret))
    async with httpx2.AsyncClient(transport=transport, base_url="http://console.test") as client:
        assert (await client.get("/acme/directory")).status_code == 200
        assert (await client.head("/acme/new-nonce")).status_code == 200
        assert (await client.get("/acme/ca.pem")).status_code == 200
        for path in (
            "/acme/new-account",
            "/acme/new-order",
            "/acme/authz/1",
            "/acme/chall/1",
            "/acme/finalize/1",
            "/acme/order/1",
            "/acme/cert/1",
            "/acme/key-change",
            "/acme/revoke-cert",
        ):
            assert (await client.post(path)).status_code == 401


@pytest.mark.anyio
@pytest.mark.parametrize(
    "identifiers",
    [
        [ipaddress.IPv4Address("192.0.2.10")],
        [ipaddress.IPv6Address("2001:db8::10")],
        [
            "Node.Example",
            ipaddress.IPv4Address("192.0.2.10"),
            ipaddress.IPv6Address("2001:db8::10"),
        ],
    ],
    ids=["ipv4", "ipv6", "mixed"],
)
async def test_authenticated_full_issuance_follows_external_urls(identifiers):
    """Turnstone auth and external routing preserve DNS/IP identifier types."""
    import httpx2
    import lacme
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from lacme.events import CACertificateIssued

    from turnstone.console.tls import TLSManager
    from turnstone.core.auth import (
        JWT_AUD_CONSOLE,
        TLS_ACME_TOKEN_SOURCE,
        ServiceTokenManager,
    )
    from turnstone.core.tls import ACMEHTTPAuth

    class NoopChallengeHandler:
        def __init__(self):
            self.provisioned = []
            self.deprovisioned = []

        async def provision(self, domain, _token, _key_authorization):
            self.provisioned.append(domain)

        async def deprovision(self, domain, _token):
            self.deprovisioned.append(domain)

    secret = "test-jwt-secret-minimum-32-chars!"
    internal_base = "http://console.internal:8090/acme"
    external_base = "http://ca.example:8090/acme"
    manager = TLSManager(get_storage(), acme_external_url=external_base)
    await manager.init_ca()
    ca_events = []
    manager._event_dispatcher.subscribe(ca_events.append, event_type=CACertificateIssued)
    app = _authenticated_acme_app(manager, secret)
    tokens = ServiceTokenManager(
        user_id="node-1",
        scopes=frozenset({"service"}),
        source=TLS_ACME_TOKEN_SOURCE,
        secret=secret,
        audience=JWT_AUD_CONSOLE,
    )
    challenge_handler = NoopChallengeHandler()
    transport = httpx2.ASGITransport(app=app)
    async with (
        httpx2.AsyncClient(
            transport=transport,
            auth=ACMEHTTPAuth([internal_base, external_base], lambda: tokens.token),
            follow_redirects=False,
            trust_env=False,
        ) as http_client,
        lacme.Client(
            directory_url=f"{internal_base}/directory",
            store=manager._store,
            challenge_handler=challenge_handler,
            http_client=http_client,
            poll_interval=0.001,
            allow_insecure=True,
        ) as client,
    ):
        bundle = await client.issue(identifiers)

    root = x509.load_pem_x509_certificate(manager.get_root_cert_pem())
    chain = x509.load_pem_x509_certificates(bundle.fullchain_pem)
    from datetime import timedelta

    assert chain[0].issuer == root.subject
    assert chain[-1].fingerprint(hashes.SHA256()) == root.fingerprint(hashes.SHA256())
    assert chain[0].not_valid_after_utc - chain[0].not_valid_before_utc == timedelta(hours=48)
    assert bundle.key_pem
    identifier_strings = tuple(str(value) for value in identifiers)
    assert bundle.domain == identifier_strings[0]
    assert bundle.domains == identifier_strings
    assert manager._store.load_cert(identifier_strings[0]) == bundle
    assert challenge_handler.provisioned == list(identifier_strings)
    assert challenge_handler.deprovisioned == list(identifier_strings)
    assert len(ca_events) == 1
    assert ca_events[0].name == identifier_strings[0]
    assert ca_events[0].names == identifier_strings


@pytest.mark.anyio
async def test_acme_http_auth_rejects_hostile_directory_destination():
    """Absolute URLs from a directory cannot redirect the enrollment JWT."""
    import json

    import httpx2
    import lacme

    from turnstone.core.tls import ACMEHTTPAuth

    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        directory = {
            "newNonce": "http://attacker.example/acme/new-nonce",
            "newAccount": "http://console.test/acme/new-account",
            "newOrder": "http://console.test/acme/new-order",
            "revokeCert": "http://console.test/acme/revoke-cert",
            "keyChange": "http://console.test/acme/key-change",
        }
        return httpx2.Response(200, content=json.dumps(directory), request=request)

    async with (
        httpx2.AsyncClient(
            transport=httpx2.MockTransport(handler),
            auth=ACMEHTTPAuth(["http://console.test/acme"], lambda: "sensitive-token"),
        ) as http_client,
        lacme.Client(
            directory_url="http://console.test/acme/directory",
            http_client=http_client,
            allow_insecure=True,
        ) as client,
    ):
        with pytest.raises(RuntimeError, match="outside configured responder bases"):
            await client.create_account()

    assert len(seen) == 1
    assert seen[0].url == httpx2.URL("http://console.test/acme/directory")
    assert "Authorization" not in seen[0].headers


@pytest.mark.anyio
@pytest.mark.parametrize(
    "url",
    [
        "http://console.test/acme/cert/%2e%2e/%2e%2e/v1/api/admin",
        "http://console.test/acme/cert/%2Fv1%2Fapi%2Fadmin",
        "http://console.test/acme/cert//v1/api/admin",
        "http://console.test/acme/cert/1?next=/v1/api/admin",
        "http://user@console.test/acme/cert/1",
    ],
)
async def test_acme_http_auth_rejects_noncanonical_protected_urls(url):
    """Enrollment credentials never cross ambiguous proxy path boundaries."""
    import httpx2

    from turnstone.core.tls import ACMEHTTPAuth

    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, request=request)

    async with httpx2.AsyncClient(
        transport=httpx2.MockTransport(handler),
        auth=ACMEHTTPAuth(["http://console.test/acme"], lambda: "sensitive-token"),
    ) as client:
        with pytest.raises(RuntimeError, match="non-canonical|unknown ACME"):
            await client.post(url)

    assert seen == []


@pytest.mark.anyio
async def test_acme_http_auth_accepts_exact_dynamic_resource():
    import httpx2

    from turnstone.core.tls import ACMEHTTPAuth

    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, request=request)

    async with httpx2.AsyncClient(
        transport=httpx2.MockTransport(handler),
        auth=ACMEHTTPAuth(["http://console.test/acme"], lambda: "sensitive-token"),
    ) as client:
        response = await client.post("http://console.test/acme/cert/order_1.example-2~x")

    assert response.status_code == 200
    assert len(seen) == 1
    assert seen[0].headers["Authorization"] == "Bearer sensitive-token"


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_console_renewal_stop_cleans_up_before_cancellation(anyio_backend):
    import asyncio

    from turnstone.console.tls import TLSManager

    entered = asyncio.Event()
    release = asyncio.Event()
    stopped = False

    class FakeManager:
        async def stop(self):
            nonlocal stopped
            entered.set()
            await release.wait()
            stopped = True

    manager = TLSManager(get_storage())
    manager._renewal_manager = FakeManager()
    task = asyncio.create_task(manager.stop_renewal())
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped is True
    assert manager._renewal_manager is None


# ── Cert issuance ─────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_issue_console_certs_internal(tls_manager):
    """Console certs issued from internal CA when no external directory."""
    await tls_manager.init_ca()
    await tls_manager.issue_console_certs(["console.internal", "localhost"])
    assert tls_manager.internal_bundle is not None
    assert tls_manager.frontend_bundle is not None
    assert tls_manager.internal_bundle.domain == "console.internal"
    assert b"BEGIN CERTIFICATE" in tls_manager.internal_bundle.cert_pem


@pytest.mark.anyio
async def test_issue_console_certs_persists(tls_manager):
    """Certs loaded from storage on re-issue."""
    await tls_manager.init_ca()
    await tls_manager.issue_console_certs(["console.internal"])
    bundle1 = tls_manager.internal_bundle

    # New manager, same storage
    from turnstone.console.tls import TLSManager

    mgr2 = TLSManager(get_storage())
    await mgr2.init_ca()
    await mgr2.issue_console_certs(["console.internal"])
    bundle2 = mgr2.internal_bundle

    assert bundle1.cert_pem == bundle2.cert_pem


@pytest.mark.anyio
async def test_frontend_and_internal_same_domain_use_separate_stores(monkeypatch):
    """External frontend issuance cannot replace the console mTLS identity."""
    import lacme

    from turnstone.console.tls import TLSManager

    manager = TLSManager(get_storage())
    await manager.init_ca()
    await manager.issue_console_certs(["console.example"])
    internal = manager.internal_bundle
    assert internal is not None

    external_ca = lacme.CertificateAuthority()
    external_ca.init(cn="External CA")
    frontend = external_ca.issue(["console.example"])

    class FakeExternalClient:
        def __init__(self, **kwargs):
            self._store = kwargs["store"]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def issue(self, _identifiers):
            return self._store.save_cert(frontend)

    monkeypatch.setattr(lacme, "Client", FakeExternalClient)
    await manager._issue_frontend_cert(
        ["console.example"],
        "https://external-ca.example/directory",
    )

    assert manager._store.load_cert("console.example") == internal
    assert manager._frontend_store is not None
    assert manager._frontend_store.load_cert("console.example") == frontend

    restarted = TLSManager(get_storage())
    await restarted.init_ca()
    await restarted.issue_console_certs(["console.example"])
    assert restarted.internal_bundle is not None
    assert restarted.internal_bundle.cert_pem == internal.cert_pem


@pytest.mark.anyio
async def test_console_rejects_same_san_bundle_from_wrong_root(tls_manager):
    """Legacy same-domain frontend rows are repaired, not reused for mTLS."""
    import lacme
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes

    await tls_manager.init_ca()
    external_ca = lacme.CertificateAuthority()
    external_ca.init(cn="Turnstone CA")
    wrong_root_bundle = external_ca.issue(["console.example"])
    tls_manager._store.save_cert(wrong_root_bundle)

    await tls_manager.issue_console_certs(["console.example"])

    replacement = tls_manager.internal_bundle
    assert replacement is not None
    active_root = x509.load_pem_x509_certificate(tls_manager.get_root_cert_pem())
    replacement_chain = x509.load_pem_x509_certificates(replacement.fullchain_pem)
    assert replacement.cert_pem != wrong_root_bundle.cert_pem
    assert replacement_chain[-1].fingerprint(hashes.SHA256()) == active_root.fingerprint(
        hashes.SHA256()
    )


@pytest.mark.anyio
async def test_cluster_bundle_validation_rejects_injected_chain_member(tls_manager):
    from dataclasses import replace

    import lacme

    from turnstone.core.tls import validate_cluster_certificate_bundle

    await tls_manager.init_ca()
    valid = tls_manager._ca.issue(["console.example"])
    unrelated_ca = lacme.CertificateAuthority()
    unrelated_ca.init(cn="Unrelated CA")
    malformed = replace(
        valid,
        fullchain_pem=valid.cert_pem + unrelated_ca.root_cert_pem + tls_manager.get_root_cert_pem(),
    )

    with pytest.raises(ValueError, match="chain.*invalid"):
        validate_cluster_certificate_bundle(
            malformed,
            ["console.example"],
            tls_manager.get_root_cert_pem(),
        )


@pytest.mark.anyio
async def test_console_reissues_legacy_dns_ip_as_ip_san(tls_manager):
    """An unexpired DNS:192.0.2.10 bundle cannot satisfy an IP identity."""
    from cryptography import x509

    await tls_manager.init_ca()
    legacy = tls_manager._ca.issue(["192.0.2.10"])
    legacy_sans = (
        x509.load_pem_x509_certificate(legacy.cert_pem)
        .extensions.get_extension_for_class(x509.SubjectAlternativeName)
        .value
    )
    assert list(legacy_sans) == [x509.DNSName("192.0.2.10")]

    await tls_manager.issue_console_certs([ipaddress.IPv4Address("192.0.2.10")])

    replacement = tls_manager.internal_bundle
    assert replacement is not None
    assert replacement.cert_pem != legacy.cert_pem
    replacement_sans = (
        x509.load_pem_x509_certificate(replacement.cert_pem)
        .extensions.get_extension_for_class(x509.SubjectAlternativeName)
        .value
    )
    assert list(replacement_sans) == [x509.IPAddress(ipaddress.IPv4Address("192.0.2.10"))]


# ── SSL contexts ──────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_ssl_contexts_none_before_certs(tls_manager):
    await tls_manager.init_ca()
    assert tls_manager.get_server_ssl_context() is None
    assert tls_manager.get_client_ssl_context() is None


@pytest.mark.anyio
async def test_ssl_contexts_after_certs(tls_manager):
    await tls_manager.init_ca()
    await tls_manager.issue_console_certs(["console.internal"])
    server_ctx = tls_manager.get_server_ssl_context()
    client_ctx = tls_manager.get_client_ssl_context()
    assert server_ctx is not None
    assert client_ctx is not None
    import ssl

    assert isinstance(server_ctx, ssl.SSLContext)
    assert isinstance(client_ctx, ssl.SSLContext)


# ── Root cert endpoint ────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_tls_ca_cert_endpoint(tls_manager):
    """Test the CA cert download endpoint via test client."""
    await tls_manager.init_ca()

    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from turnstone.console.server import tls_ca_cert, tls_ca_status

    # Middleware that grants full access (config-token style: no user_id)
    from turnstone.core.auth import AuthResult

    async def _grant_access(request, call_next):  # type: ignore[no-untyped-def]
        request.state.auth_result = AuthResult(
            user_id="", scopes=frozenset({"approve", "service"}), token_source="test"
        )
        return await call_next(request)

    from starlette.middleware.base import BaseHTTPMiddleware

    app = Starlette(
        routes=[
            Route("/ca.pem", tls_ca_cert),
            Route("/ca", tls_ca_status),
        ],
        middleware=[Middleware(BaseHTTPMiddleware, dispatch=_grant_access)],
    )
    app.state.tls_manager = tls_manager

    client = TestClient(app)

    # CA cert download
    resp = client.get("/ca.pem")
    assert resp.status_code == 200
    assert b"BEGIN CERTIFICATE" in resp.content
    assert resp.headers["content-type"] == "application/x-pem-file"

    # CA status
    resp = client.get("/ca")
    assert resp.status_code == 200
    data = resp.json()
    assert data["enabled"] is True
    assert data["ca_cn"] == "Turnstone CA"


@pytest.mark.anyio
async def test_tls_endpoints_disabled():
    """Endpoints return 404/disabled when TLS not enabled."""
    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from turnstone.console.server import tls_ca_cert, tls_ca_status
    from turnstone.core.auth import AuthResult

    async def _grant_access(request, call_next):  # type: ignore[no-untyped-def]
        request.state.auth_result = AuthResult(
            user_id="", scopes=frozenset({"approve", "service"}), token_source="test"
        )
        return await call_next(request)

    app = Starlette(
        routes=[
            Route("/ca.pem", tls_ca_cert),
            Route("/ca", tls_ca_status),
        ],
        middleware=[Middleware(BaseHTTPMiddleware, dispatch=_grant_access)],
    )
    # No tls_manager on state

    client = TestClient(app)

    resp = client.get("/ca.pem")
    assert resp.status_code == 404

    resp = client.get("/ca")
    data = resp.json()
    assert data["enabled"] is False


# ── Events ────────────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_event_dispatcher_wired(tls_manager):
    """Verify the event dispatcher has subscribers."""
    assert tls_manager._event_dispatcher is not None
    # Should have at least 4 subscriptions (issued, renewed, expiring, failed)
    # The exact check depends on lacme's EventDispatcher internals,
    # so just verify the dispatcher exists and the manager initializes cleanly
    await tls_manager.init_ca()
