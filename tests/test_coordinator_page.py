"""Tests for the /coordinator/{ws_id} HTML page handler.

The handler serves the shared template with the ws_id injected as a
``data-ws-id`` attribute.  It does NOT enforce auth on the page itself —
auth gating happens on the API endpoints the page calls (an unauthenticated
visitor lands on the page but all API calls fail).
"""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from turnstone.console.server import coordinator_page


@pytest.fixture
def client():
    app = Starlette(routes=[Route("/coordinator/{ws_id}", coordinator_page, methods=["GET"])])
    return TestClient(app)


def test_valid_ws_id_injects_data_attr(client):
    ws_id = "a" * 32
    resp = client.get(f"/coordinator/{ws_id}")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    body = resp.text
    # ws_id is injected into the html data-ws-id attribute.
    assert f'data-ws-id="{ws_id}"' in body
    # Template placeholder is fully substituted.
    assert "{{WS_ID}}" not in body
    # Sanity: the shared static imports are wired.
    assert "/shared/base.css" in body
    assert "/static/coordinator/coordinator.js" in body


def test_non_hex_ws_id_returns_400(client):
    """Only hex chars are allowed to avoid HTML injection."""
    resp = client.get("/coordinator/not-hex-chars-here")
    assert resp.status_code == 400


def test_ws_id_too_long_returns_400(client):
    resp = client.get("/coordinator/" + "a" * 65)
    assert resp.status_code == 400


def test_uppercase_hex_rejected(client):
    # Our ws_ids are lowercase hex; reject mixed/upper to avoid surprises.
    resp = client.get("/coordinator/" + "A" * 32)
    assert resp.status_code == 400
