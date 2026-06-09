"""/health surfaces the node's TLS state when tls.enabled is configured.

A node that falls back to plain HTTP after a failed TLS init must be
observable (tls: "fallback"), and default plain-HTTP deployments must keep
an unchanged payload shape (no "tls" key).
"""

from __future__ import annotations

import queue
import threading
from unittest.mock import MagicMock

import pytest


@pytest.fixture()
def make_client():
    from starlette.testclient import TestClient

    from turnstone.server import create_app

    clients = []

    def _make(tls_state: str | None = None):
        mock_mgr = MagicMock()
        mock_mgr.list_all.return_value = []
        mock_mgr.max_active = 10
        app = create_app(
            workstreams=mock_mgr,
            global_queue=queue.Queue(),
            global_listeners=[],
            global_listeners_lock=threading.Lock(),
            skip_permissions=False,
            jwt_secret="test-jwt-secret-minimum-32-chars!",
        )
        if tls_state is not None:
            app.state.tls_state = tls_state
        client = TestClient(app, raise_server_exceptions=False)
        clients.append(client)
        return client

    yield _make
    for c in clients:
        c.close()


def test_health_no_tls_key_by_default(make_client):
    """mTLS disabled (default): payload shape unchanged — no tls key."""
    resp = make_client().get("/health")
    assert resp.status_code == 200
    assert "tls" not in resp.json()


def test_health_tls_active(make_client):
    resp = make_client(tls_state="active").get("/health")
    assert resp.status_code == 200
    assert resp.json()["tls"] == "active"


def test_health_tls_fallback_visible(make_client):
    """The silent-downgrade case must be observable in /health."""
    resp = make_client(tls_state="fallback").get("/health")
    assert resp.status_code == 200
    assert resp.json()["tls"] == "fallback"
