"""Tests for TLS admin API endpoints and CLI commands."""

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
    """Create an initialized TLSManager."""
    import asyncio

    from turnstone.console.tls import TLSManager

    mgr = TLSManager(get_storage())
    asyncio.run(mgr.init_ca())
    # Issue a test cert
    asyncio.run(mgr.issue_console_certs(["test.internal", "localhost"]))
    return mgr


# ── Admin API endpoints ───────────────────────────────────────────────────────


def _make_app(tls_manager):
    """Create a minimal Starlette app with TLS endpoints."""
    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.routing import Route

    from turnstone.console.server import (
        tls_ca_cert,
        tls_ca_status,
        tls_delete_cert,
        tls_list_certs,
        tls_renew_cert,
    )
    from turnstone.core.auth import AuthResult

    async def _grant_access(request, call_next):  # type: ignore[no-untyped-def]
        request.state.auth_result = AuthResult(
            user_id="",
            scopes=frozenset({"approve", "service"}),
            token_source="test",
        )
        return await call_next(request)

    app = Starlette(
        routes=[
            Route("/ca", tls_ca_status),
            Route("/ca.pem", tls_ca_cert),
            Route("/certs", tls_list_certs),
            Route("/certs/{domain}/renew", tls_renew_cert, methods=["POST"]),
            Route("/certs/{domain}", tls_delete_cert, methods=["DELETE"]),
        ],
        middleware=[Middleware(BaseHTTPMiddleware, dispatch=_grant_access)],
    )
    app.state.tls_manager = tls_manager
    return app


def test_list_certs(tls_manager):
    from starlette.testclient import TestClient

    client = TestClient(_make_app(tls_manager))
    resp = client.get("/certs")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["certs"]) >= 1
    assert data["certs"][0]["domain"] == "test.internal"
    assert data["certs"][0]["renewable"] is True
    assert data["certs"][0]["deletable"] is False


def test_renew_cert(tls_manager):
    from datetime import timedelta

    from cryptography import x509
    from starlette.testclient import TestClient

    client = TestClient(_make_app(tls_manager))
    resp = client.post("/certs/test.internal/renew")
    assert resp.status_code == 200
    data = resp.json()
    assert data["domain"] == "test.internal"
    renewed = tls_manager._store.load_cert("test.internal")
    assert renewed is not None
    leaf = x509.load_pem_x509_certificate(renewed.cert_pem)
    assert leaf.not_valid_after_utc - leaf.not_valid_before_utc == timedelta(hours=48)


def test_renew_cert_not_found(tls_manager):
    from starlette.testclient import TestClient

    client = TestClient(_make_app(tls_manager))
    resp = client.post("/certs/nonexistent.internal/renew")
    assert resp.status_code == 404


def test_renew_remote_cert_conflict(tls_manager):
    from starlette.testclient import TestClient

    remote = tls_manager._ca.issue(["remote.internal"])
    old_key = remote.key_pem
    client = TestClient(_make_app(tls_manager))

    resp = client.post("/certs/remote.internal/renew")

    assert resp.status_code == 409
    assert tls_manager._store.load_cert("remote.internal").key_pem == old_key


def test_delete_expired_remote_cert(tls_manager):
    from starlette.testclient import TestClient

    tls_manager._ca.issue(["retired.internal"], validity_hours=0)
    client = TestClient(_make_app(tls_manager))

    resp = client.delete("/certs/retired.internal")

    assert resp.status_code == 200
    assert resp.json()["deleted"] == "retired.internal"
    # Verify it's gone
    resp = client.get("/certs")
    domains = [c["domain"] for c in resp.json()["certs"]]
    assert "retired.internal" not in domains


def test_delete_active_cert_conflict(tls_manager):
    from starlette.testclient import TestClient

    client = TestClient(_make_app(tls_manager))

    resp = client.delete("/certs/test.internal")

    assert resp.status_code == 409
    assert tls_manager._store.load_cert("test.internal") is not None


def test_delete_cert_not_found(tls_manager):
    from starlette.testclient import TestClient

    client = TestClient(_make_app(tls_manager))
    resp = client.delete("/certs/nonexistent.internal")
    assert resp.status_code == 404


# ── Auth enforcement ──────────────────────────────────────────────────────────


def _make_app_no_auth(tls_manager):
    """Create app without auth middleware — simulates unauthenticated requests."""
    from starlette.applications import Starlette
    from starlette.routing import Route

    from turnstone.console.server import (
        tls_ca_cert,
        tls_ca_status,
        tls_delete_cert,
        tls_list_certs,
        tls_renew_cert,
    )

    app = Starlette(
        routes=[
            Route("/ca", tls_ca_status),
            Route("/ca.pem", tls_ca_cert),
            Route("/certs", tls_list_certs),
            Route("/certs/{domain}/renew", tls_renew_cert, methods=["POST"]),
            Route("/certs/{domain}", tls_delete_cert, methods=["DELETE"]),
        ],
    )
    app.state.tls_manager = tls_manager
    return app


def _make_app_read_only(tls_manager):
    """Create app with read-only auth — should be rejected by admin endpoints."""
    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.routing import Route

    from turnstone.console.server import (
        tls_ca_cert,
        tls_ca_status,
        tls_delete_cert,
        tls_list_certs,
        tls_renew_cert,
    )
    from turnstone.core.auth import AuthResult

    async def _grant_read(request, call_next):  # type: ignore[no-untyped-def]
        request.state.auth_result = AuthResult(
            user_id="viewer",
            scopes=frozenset({"read"}),
            token_source="jwt",
        )
        return await call_next(request)

    app = Starlette(
        routes=[
            Route("/ca", tls_ca_status),
            Route("/ca.pem", tls_ca_cert),
            Route("/certs", tls_list_certs),
            Route("/certs/{domain}/renew", tls_renew_cert, methods=["POST"]),
            Route("/certs/{domain}", tls_delete_cert, methods=["DELETE"]),
        ],
        middleware=[Middleware(BaseHTTPMiddleware, dispatch=_grant_read)],
    )
    app.state.tls_manager = tls_manager
    return app


def test_unauthenticated_list_certs_401(tls_manager):
    from starlette.testclient import TestClient

    client = TestClient(_make_app_no_auth(tls_manager))
    resp = client.get("/certs")
    assert resp.status_code == 401


def test_unauthenticated_renew_401(tls_manager):
    from starlette.testclient import TestClient

    client = TestClient(_make_app_no_auth(tls_manager))
    resp = client.post("/certs/test.internal/renew")
    assert resp.status_code == 401


def test_unauthenticated_delete_401(tls_manager):
    from starlette.testclient import TestClient

    client = TestClient(_make_app_no_auth(tls_manager))
    resp = client.delete("/certs/test.internal")
    assert resp.status_code == 401


def test_read_only_renew_403(tls_manager):
    from starlette.testclient import TestClient

    client = TestClient(_make_app_read_only(tls_manager))
    resp = client.post("/certs/test.internal/renew")
    assert resp.status_code == 403


def test_read_only_delete_403(tls_manager):
    from starlette.testclient import TestClient

    client = TestClient(_make_app_read_only(tls_manager))
    resp = client.delete("/certs/test.internal")
    assert resp.status_code == 403


# ── CLI bootstrap ─────────────────────────────────────────────────────────────


def test_cli_bootstrap(tmp_path):
    """Test offline CA bootstrap."""
    import argparse

    from turnstone.admin import _cmd_tls_bootstrap

    out = tmp_path / "certs"
    args = argparse.Namespace(out=str(out), issue=["app.internal", "pg.internal"])
    _cmd_tls_bootstrap(args)

    assert (out / "ca.pem").exists()
    assert b"BEGIN CERTIFICATE" in (out / "ca.pem").read_bytes()
    # Check certs were issued
    assert (out / "certs" / "app.internal").exists()
    assert (out / "certs" / "pg.internal").exists()


def test_cli_bootstrap_no_issue(tmp_path):
    """Bootstrap with no --issue creates CA only."""
    import argparse

    from turnstone.admin import _cmd_tls_bootstrap

    out = tmp_path / "certs"
    args = argparse.Namespace(out=str(out), issue=[])
    _cmd_tls_bootstrap(args)

    assert (out / "ca.pem").exists()
    # No certs dir
    certs_dir = out / "certs"
    if certs_dir.exists():
        assert len(list(certs_dir.iterdir())) == 0


def test_cli_bootstrap_uses_filestore_path_for_ipv6(tmp_path):
    """The CLI accepts IPv6 and lets FileStore encode its non-portable key."""
    import argparse

    import lacme
    from cryptography import x509

    from turnstone.admin import _cmd_tls_bootstrap

    out = tmp_path / "certs"
    args = argparse.Namespace(out=str(out), issue=["2001:0db8::10"])
    _cmd_tls_bootstrap(args)

    bundle = lacme.FileStore(out).load_cert("2001:db8::10")
    assert bundle is not None
    assert bundle.cert_path is not None
    assert bundle.cert_path.parent != out / "certs" / "2001:db8::10"
    sans = (
        x509.load_pem_x509_certificate(bundle.cert_pem)
        .extensions.get_extension_for_class(x509.SubjectAlternativeName)
        .value
    )
    assert list(sans) == [x509.IPAddress(ipaddress.IPv6Address("2001:db8::10"))]


@pytest.mark.parametrize(
    ("domain", "sans", "expected"),
    [
        ("node-1", ["node-1.internal"], ["node-1", "node-1.internal"]),
        (
            "2001:0db8::10",
            ["node-1.internal", "192.0.2.10"],
            [
                ipaddress.IPv6Address("2001:db8::10"),
                "node-1.internal",
                ipaddress.IPv4Address("192.0.2.10"),
            ],
        ),
    ],
    ids=["dns", "mixed-ip"],
)
def test_cli_issue_closes_sync_client(monkeypatch, tmp_path, domain, sans, expected):
    """The synchronous HTTPX2-backed ACME client is closed after issuance."""
    import argparse
    from types import SimpleNamespace

    import lacme

    state: dict[str, object] = {}
    monkeypatch.setenv("TURNSTONE_JWT_SECRET", "test-jwt-secret-minimum-32-chars!")

    class FakeSyncClient:
        def __init__(self, **kwargs):
            state["kwargs"] = kwargs

        def __enter__(self):
            state["entered"] = True
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            import asyncio

            state["exited"] = True
            asyncio.run(state["kwargs"]["http_client"].aclose())

        def issue(self, domains):
            state["domains"] = domains
            return SimpleNamespace(cert_pem=b"cert", fullchain_pem=b"chain", key_pem=b"key")

    monkeypatch.setattr(lacme, "SyncClient", FakeSyncClient)
    from turnstone.admin import _cmd_tls_issue

    out = tmp_path / "issued"
    args = argparse.Namespace(
        console_url="http://console:8090",
        domain=domain,
        san=sans,
        out=str(out),
    )
    _cmd_tls_issue(args)

    kwargs = state.pop("kwargs")
    assert kwargs["directory_url"] == "http://console:8090/acme/directory"
    assert kwargs["allow_insecure"] is True
    assert kwargs["http_client"].is_closed
    assert state == {
        "entered": True,
        "domains": expected,
        "exited": True,
    }
    assert (out / "cert.pem").read_bytes() == b"cert"
    assert (out / "fullchain.pem").read_bytes() == b"chain"
    assert (out / "key.pem").read_bytes() == b"key"


def test_cli_issue_closes_sync_client_on_failure(monkeypatch, tmp_path):
    """The synchronous client also closes when certificate issuance fails."""
    import argparse

    import lacme

    state: dict[str, object] = {}
    monkeypatch.setenv("TURNSTONE_JWT_SECRET", "test-jwt-secret-minimum-32-chars!")

    class FailingSyncClient:
        def __init__(self, **kwargs):
            state["http_client"] = kwargs["http_client"]

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            import asyncio

            state["exited"] = True
            asyncio.run(state["http_client"].aclose())

        def issue(self, _domains):
            raise RuntimeError("issuance failed")

    monkeypatch.setattr(lacme, "SyncClient", FailingSyncClient)
    from turnstone.admin import _cmd_tls_issue

    args = argparse.Namespace(
        console_url="http://console:8090",
        domain="node-1",
        san=[],
        out=str(tmp_path / "issued"),
    )
    with pytest.raises(RuntimeError, match="issuance failed"):
        _cmd_tls_issue(args)

    assert state["exited"] is True
    assert state["http_client"].is_closed


def test_cli_issue_against_authenticated_auto_approve_responder(monkeypatch, tmp_path):
    """The real lacme SyncClient completes mixed DNS/IP Turnstone issuance."""
    import argparse
    import asyncio

    import httpx2
    from cryptography import x509
    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.routing import Mount

    from turnstone.admin import _cmd_tls_issue
    from turnstone.console.tls import TLSManager
    from turnstone.core import tls as tls_module
    from turnstone.core.auth import AUTH_COOKIE_CONSOLE, JWT_AUD_CONSOLE, AuthMiddleware

    secret = "test-jwt-secret-minimum-32-chars!"
    base = "http://console.test/acme"
    manager = TLSManager(get_storage(), acme_external_url=base)
    asyncio.run(manager.init_ca())
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

    def build_client(
        console_url,
        *,
        external_url="",
        token_provider,
    ):
        bases = [f"{console_url.rstrip('/')}/acme"]
        if external_url:
            bases.append(external_url.rstrip("/"))
        return httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            auth=tls_module.ACMEHTTPAuth(bases, token_provider),
            follow_redirects=False,
            trust_env=False,
        )

    monkeypatch.setenv("TURNSTONE_JWT_SECRET", secret)
    monkeypatch.delenv("TURNSTONE_ACME_EXTERNAL_URL", raising=False)
    monkeypatch.setattr(tls_module, "build_acme_http_client", build_client)
    out = tmp_path / "issued"

    _cmd_tls_issue(
        argparse.Namespace(
            console_url="http://console.test",
            domain="node.example",
            san=["192.0.2.10", "2001:db8::10"],
            out=str(out),
        )
    )

    leaf = x509.load_pem_x509_certificate((out / "cert.pem").read_bytes())
    sans = list(leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value)
    assert sans == [
        x509.DNSName("node.example"),
        x509.IPAddress(ipaddress.IPv4Address("192.0.2.10")),
        x509.IPAddress(ipaddress.IPv6Address("2001:db8::10")),
    ]
    assert (out / "key.pem").stat().st_mode & 0o777 == 0o600


def test_cli_ca_cert_preserves_trusted_https(monkeypatch, tmp_path):
    import argparse
    from types import SimpleNamespace

    import httpx2

    from turnstone.admin import _cmd_tls_ca_cert

    seen = {}

    def fake_get(url, **kwargs):
        seen["url"] = url
        seen["kwargs"] = kwargs
        return SimpleNamespace(content=b"trusted-ca", raise_for_status=lambda: None)

    monkeypatch.setattr(httpx2, "get", fake_get)
    output = tmp_path / "ca.pem"

    _cmd_tls_ca_cert(
        argparse.Namespace(
            console_url="https://console.example:8443",
            out=str(output),
        )
    )

    assert seen == {
        "url": "https://console.example:8443/acme/ca.pem",
        "kwargs": {"follow_redirects": False, "trust_env": False},
    }
    assert output.read_bytes() == b"trusted-ca"


def test_atomic_write_refuses_destination_symlink(tmp_path):
    from turnstone.admin import _atomic_write_file

    victim = tmp_path / "victim"
    victim.write_bytes(b"preserve")
    link = tmp_path / "key.pem"
    link.symlink_to(victim)

    with pytest.raises(RuntimeError, match="Refusing to replace symlink"):
        _atomic_write_file(link, b"secret", mode=0o600)

    assert victim.read_bytes() == b"preserve"


def test_atomic_write_refuses_symlinked_output_directory(tmp_path):
    from turnstone.admin import _atomic_write_file

    real_dir = tmp_path / "real"
    real_dir.mkdir()
    linked_dir = tmp_path / "linked"
    linked_dir.symlink_to(real_dir, target_is_directory=True)

    with pytest.raises(RuntimeError, match="symlink components"):
        _atomic_write_file(linked_dir / "key.pem", b"secret", mode=0o600)

    assert not (real_dir / "key.pem").exists()


# ── Config parsing ────────────────────────────────────────────────────────────


def test_database_ssl_config_map():
    """Database SSL keys are in the config map."""
    from turnstone.core.config import _CONFIG_MAP

    db_map = _CONFIG_MAP["database"]
    assert "sslmode" in db_map
    assert "sslrootcert" in db_map
    assert "sslcert" in db_map
    assert "sslkey" in db_map


# ── Auth enforcement ──────────────────────────────────────────────────────────


def test_tls_endpoints_require_auth(tls_manager):
    """TLS admin endpoints return 401 without auth."""
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from turnstone.console.server import tls_ca_status, tls_list_certs

    # No auth middleware — request.state.auth_result will be missing
    app = Starlette(
        routes=[
            Route("/ca", tls_ca_status),
            Route("/certs", tls_list_certs),
        ]
    )
    app.state.tls_manager = tls_manager

    client = TestClient(app)
    resp = client.get("/ca")
    assert resp.status_code == 401

    resp = client.get("/certs")
    assert resp.status_code == 401


# ── SDK TLS params ────────────────────────────────────────────────────────────


def test_sdk_client_cert_requires_both():
    """SDK raises ValueError if only one of client_cert/client_key provided."""
    from turnstone.sdk._base import _BaseClient

    with pytest.raises(ValueError, match="Both client_cert and client_key"):
        _BaseClient(
            base_url="http://localhost:8080",
            client_cert="/path/to/cert.pem",
        )

    with pytest.raises(ValueError, match="Both client_cert and client_key"):
        _BaseClient(
            base_url="http://localhost:8080",
            client_key="/path/to/key.pem",
        )
