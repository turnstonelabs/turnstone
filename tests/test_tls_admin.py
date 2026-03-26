"""Tests for TLS admin API endpoints and CLI commands."""

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
            scopes=frozenset({"approve"}),
            token_source="config",
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


def test_renew_cert(tls_manager):
    from starlette.testclient import TestClient

    client = TestClient(_make_app(tls_manager))
    resp = client.post("/certs/test.internal/renew")
    assert resp.status_code == 200
    data = resp.json()
    assert data["domain"] == "test.internal"


def test_renew_cert_not_found(tls_manager):
    from starlette.testclient import TestClient

    client = TestClient(_make_app(tls_manager))
    resp = client.post("/certs/nonexistent.internal/renew")
    assert resp.status_code == 404


def test_delete_cert(tls_manager):
    from starlette.testclient import TestClient

    client = TestClient(_make_app(tls_manager))
    resp = client.delete("/certs/test.internal")
    assert resp.status_code == 200
    assert resp.json()["deleted"] == "test.internal"
    # Verify it's gone
    resp = client.get("/certs")
    domains = [c["domain"] for c in resp.json()["certs"]]
    assert "test.internal" not in domains


def test_delete_cert_not_found(tls_manager):
    from starlette.testclient import TestClient

    client = TestClient(_make_app(tls_manager))
    resp = client.delete("/certs/nonexistent.internal")
    assert resp.status_code == 404


# ── CLI bootstrap ─────────────────────────────────────────────────────────────


def test_cli_bootstrap(tmp_path):
    """Test offline CA bootstrap."""
    import argparse

    from turnstone.admin import _cmd_tls_bootstrap

    out = tmp_path / "certs"
    args = argparse.Namespace(out=str(out), issue=["redis.internal", "pg.internal"])
    _cmd_tls_bootstrap(args)

    assert (out / "ca.pem").exists()
    assert b"BEGIN CERTIFICATE" in (out / "ca.pem").read_bytes()
    # Check certs were issued
    assert (out / "certs" / "redis.internal").exists()
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


# ── Config parsing ────────────────────────────────────────────────────────────


def test_redis_tls_config_map():
    """Redis TLS keys are in the config map."""
    from turnstone.core.config import _CONFIG_MAP

    redis_map = _CONFIG_MAP["redis"]
    assert "tls" in redis_map
    assert "tls_ca" in redis_map
    assert "tls_cert" in redis_map
    assert "tls_key" in redis_map


def test_database_ssl_config_map():
    """Database SSL keys are in the config map."""
    from turnstone.core.config import _CONFIG_MAP

    db_map = _CONFIG_MAP["database"]
    assert "sslmode" in db_map
    assert "sslrootcert" in db_map
    assert "sslcert" in db_map
    assert "sslkey" in db_map
