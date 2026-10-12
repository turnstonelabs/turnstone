"""``GET /v1/api/admin/model-capabilities`` — static capability lookup for the model form.

A model with a table row comes back ``known`` with that row.  An id with no row
stays ``known: false`` (the registry's fallback window and Detect's result depend
on it) and, on a commercial provider, carries the default row its requests run
with as ``defaults``, which the form's capability tiles display (#1366).
"""

from __future__ import annotations

from typing import Any

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route
from starlette.testclient import TestClient

from tests._coord_test_helpers import _AuthMiddleware
from turnstone.console.server import admin_model_capabilities


def _get(provider: str, model: str) -> Any:
    app = Starlette(
        routes=[Route("/v1/api/admin/model-capabilities", admin_model_capabilities)],
        middleware=[Middleware(_AuthMiddleware)],
    )
    client = TestClient(app)
    client.headers.update({"X-Test-User": "admin", "X-Test-Perms": "admin.models"})
    resp = client.get(
        "/v1/api/admin/model-capabilities", params={"provider": provider, "model": model}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_listed_model_is_known_without_defaults() -> None:
    body = _get("anthropic", "claude-haiku-5-5")
    assert body["known"] is True
    assert body["capabilities"]["context_window"] == 1000000
    assert body["defaults"] == {}


def test_unlisted_model_carries_the_default_row() -> None:
    body = _get("anthropic", "claude-unknown-9")
    assert body["known"] is False
    assert body["capabilities"] == {}
    defaults = body["defaults"]
    assert defaults["thinking_mode"] == "adaptive"
    assert defaults["supports_temperature"] is False
    assert defaults["supports_effort"] is True
    assert defaults["effort_levels"] == ["low", "medium", "high"]


def test_local_lane_has_no_defaults() -> None:
    body = _get("anthropic-compatible", "qwen3.6-27b")
    assert body["known"] is False
    assert body["capabilities"] == {}
    assert body["defaults"] == {}
