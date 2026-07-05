"""``POST /v1/api/admin/models/effort-ladder`` — live modal projection.

Pure computation over (provider, model, unsaved capability overrides,
api_surface); every malformed input must land as a 400, never a 500 —
the body is operator-typed form state.
"""

from __future__ import annotations

from typing import Any

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route
from starlette.testclient import TestClient

from tests._coord_test_helpers import _AuthMiddleware
from turnstone.console.server import admin_effort_ladder


def _make_client() -> TestClient:
    app = Starlette(
        routes=[Route("/v1/api/admin/models/effort-ladder", admin_effort_ladder, methods=["POST"])],
        middleware=[Middleware(_AuthMiddleware)],
    )
    client = TestClient(app)
    client.headers.update({"X-Test-User": "admin", "X-Test-Perms": "admin.models"})
    return client


def _post(client: TestClient, body: Any) -> Any:
    return client.post("/v1/api/admin/models/effort-ladder", json=body)


def test_valid_request_returns_ladder() -> None:
    resp = _post(
        _make_client(),
        {
            "provider": "anthropic-compatible",
            "model": "qwen3.6-27b",
            "capabilities": {"thinking_mode": "manual", "thinking_param": "enable_thinking"},
        },
    )
    assert resp.status_code == 200, resp.text
    ladder = {r["value"]: r["effective"] for r in resp.json()["ladder"]}
    assert ladder["none"] == "off"
    assert ladder["high"] == "on+high"


def test_api_surface_switches_projection() -> None:
    body = {
        "provider": "openai-compatible",
        "model": "m",
        "capabilities": {
            "thinking_mode": "manual",
            "reasoning_effort_values": ["low", "medium", "high"],
        },
    }
    client = _make_client()
    chat = {r["value"]: r["effective"] for r in _post(client, body).json()["ladder"]}
    body["api_surface"] = "responses"
    responses = {r["value"]: r["effective"] for r in _post(client, body).json()["ladder"]}
    assert chat["medium"] == "on+medium"  # toggle + flat on the chat surface
    assert responses["medium"] == "medium"  # flat only on the responses surface


def test_non_dict_json_body_is_400_not_500() -> None:
    client = _make_client()
    for body in (None, [], "x", 7):
        resp = _post(client, body)
        assert resp.status_code == 400, (body, resp.status_code, resp.text)


def test_unknown_provider_is_400() -> None:
    resp = _post(_make_client(), {"provider": "nope", "model": "m"})
    assert resp.status_code == 400


def test_missing_model_is_400() -> None:
    resp = _post(_make_client(), {"provider": "openai", "model": ""})
    assert resp.status_code == 400


def test_non_dict_capabilities_is_400() -> None:
    resp = _post(_make_client(), {"provider": "openai", "model": "m", "capabilities": [1]})
    assert resp.status_code == 400


def test_garbage_capability_value_types_are_400() -> None:
    """Wrong-typed override values raise inside the resolver → clean 400."""
    resp = _post(
        _make_client(),
        {
            "provider": "anthropic",
            "model": "claude-fable-5",
            "capabilities": {"supports_effort": True, "effort_levels": 5},
        },
    )
    assert resp.status_code == 400


def test_requires_admin_models_permission() -> None:
    client = _make_client()
    client.headers.update({"X-Test-Perms": "read"})
    resp = _post(client, {"provider": "openai", "model": "m"})
    assert resp.status_code in (401, 403)
