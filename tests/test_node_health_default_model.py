"""A node's health is degraded while its default model isn't loaded (#1359).

``/health``, the global snapshot and every ``health_changed`` event carry one
summary, so the console shows the node's own verdict.
"""

from __future__ import annotations

import queue
import threading
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

from starlette.responses import JSONResponse

from turnstone.core.healthcheck import HealthTrackerRegistry
from turnstone.core.model_registry import ModelConfig, ModelRegistry

if TYPE_CHECKING:
    import pytest


def _registry(*aliases: str) -> ModelRegistry:
    configs = {
        alias: ModelConfig(
            alias=alias,
            base_url=f"http://{alias}.example.com/v1",
            api_key="sk-test",
            model="m",
            context_window=8192,
            provider="openai-compatible",
            source="db",
        )
        for alias in aliases
    }
    return ModelRegistry(configs, default=aliases[0])


class _Settings:
    """The one setting the summary reads, ``model.default_alias``."""

    def __init__(self, default: str = "") -> None:
        self.default = default

    def get(self, key: str) -> Any:
        return self.default if key == "model.default_alias" else None

    def reload(self, *, if_changed: bool = False) -> bool:
        return True


def _state(registry: Any, settings: _Settings | None = None, health: Any = None) -> SimpleNamespace:
    return SimpleNamespace(
        registry=registry,
        config_store=settings or _Settings(),
        health_registry=health,
        global_queue=queue.Queue(),
    )


def _events(state: SimpleNamespace) -> list[dict[str, Any]]:
    events = []
    while not state.global_queue.empty():
        events.append(state.global_queue.get_nowait())
    return events


def test_a_loaded_default_is_ok() -> None:
    from turnstone.server import _health_summary

    assert _health_summary(_state(_registry("a"))) == {
        "status": "ok",
        "backend": {"status": "up"},
        "default_model": {"alias": "a", "loaded": True},
    }


def test_a_default_that_isnt_loaded_is_degraded() -> None:
    from turnstone.server import _health_summary

    assert _health_summary(_state(_registry("a"), _Settings("gone"))) == {
        "status": "degraded",
        "backend": {"status": "up"},
        "default_model": {"alias": "gone", "loaded": False},
    }


def test_no_default_at_all_is_degraded() -> None:
    from turnstone.server import _health_summary

    empty = SimpleNamespace(effective_default=lambda _cs: "", has_alias=lambda _a: False)
    summary = _health_summary(_state(empty))
    assert summary["status"] == "degraded"
    assert summary["default_model"] == {"alias": "", "loaded": False}


def test_the_default_backend_down_is_degraded() -> None:
    from turnstone.server import _health_summary

    health = HealthTrackerRegistry(failure_threshold=1)
    health.get_tracker(provider="openai-compatible", base_url="http://a.example.com/v1")
    health.get_tracker_for_alias(_registry("a"), "a").record_failure()  # type: ignore[union-attr]

    summary = _health_summary(_state(_registry("a"), health=health))

    assert summary["status"] == "degraded"
    assert summary["backend"] == {"status": "down"}
    assert summary["default_model"] == {"alias": "a", "loaded": True}


def test_the_health_endpoint_carries_the_summary() -> None:
    from starlette.testclient import TestClient

    from turnstone.server import create_app

    mgr = MagicMock()
    mgr.list_all.return_value = []
    mgr.max_active = 10
    app = create_app(
        workstreams=mgr,
        global_queue=queue.Queue(),
        global_listeners=[],
        global_listeners_lock=threading.Lock(),
        skip_permissions=False,
        jwt_secret="test-jwt-secret-minimum-32-chars!",
    )
    app.state.registry = _registry("a")
    app.state.config_store = _Settings("gone")
    client = TestClient(app, raise_server_exceptions=False)
    try:
        resp = client.get("/health")
    finally:
        client.close()

    # Always 200: the chart's probes read the status code, not the body.
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["default_model"] == {"alias": "gone", "loaded": False}


def test_a_reload_sends_the_summary_only_when_it_changed() -> None:
    from turnstone.server import _send_health

    state = _state(_registry("a"))
    _send_health(state)
    _send_health(state)
    assert [e["status"] for e in _events(state)] == ["ok"]

    state.config_store.default = "gone"
    _send_health(state)
    assert _events(state) == [
        {
            "type": "health_changed",
            "status": "degraded",
            "backend": {"status": "up"},
            "default_model": {"alias": "gone", "loaded": False},
        }
    ]


def test_a_backend_transition_always_sends_the_summary() -> None:
    from turnstone.server import _emit_health_changed

    state = _state(_registry("a"))
    _emit_health_changed("degraded", state.global_queue, state)
    _emit_health_changed("healthy", state.global_queue, state)

    events = _events(state)
    assert [e["backend_status"] for e in events] == ["degraded", "healthy"]
    # The verdict follows the default's backend, not the one that changed.
    assert {e["status"] for e in events} == {"ok"}


def test_a_settings_reload_naming_an_unloaded_default_sends_degraded() -> None:
    from turnstone.server import config_reload

    state = _state(_registry("a"))
    state.config_store.default = "added-not-synced"

    config_reload(SimpleNamespace(app=SimpleNamespace(state=state)))  # type: ignore[arg-type]

    events = _events(state)
    assert [e["type"] for e in events] == ["health_changed", "settings_changed"]
    assert events[0]["status"] == "degraded"
    assert events[0]["default_model"] == {"alias": "added-not-synced", "loaded": False}


def test_a_model_reload_that_loads_the_default_sends_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    import turnstone.server as server

    state = _state(_registry("a"), _Settings("b"))
    server._send_health(state)
    assert _events(state)[0]["status"] == "degraded"

    def _sync(app_state: Any, **_kw: Any) -> JSONResponse:
        app_state.registry = _registry("a", "b")
        return JSONResponse({"status": "ok"})

    monkeypatch.setattr(server, "_reload_model_registry_locked", _sync)
    server.internal_model_reload(SimpleNamespace(app=SimpleNamespace(state=state)))  # type: ignore[arg-type]

    events = _events(state)
    assert [e["status"] for e in events] == ["ok"]
    assert events[0]["default_model"] == {"alias": "b", "loaded": True}
