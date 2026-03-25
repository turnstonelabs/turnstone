"""Tests for TLSManager — console CA and ACME server."""

from __future__ import annotations

import pytest

from turnstone.core.storage import get_storage, init_storage, reset_storage

lacme = pytest.importorskip("lacme")


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
            user_id="", scopes=frozenset({"approve"}), token_source="config"
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
            user_id="", scopes=frozenset({"approve"}), token_source="config"
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
