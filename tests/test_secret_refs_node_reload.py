"""Node reload: the reply names aliases dropped over their api_key reference and
asks the secret store again before loading."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from turnstone.core.healthcheck import HealthTrackerRegistry
from turnstone.core.model_registry import ModelConfig, ModelRegistry


def _cfg(alias: str) -> ModelConfig:
    return ModelConfig(
        alias=alias, base_url="http://x", api_key="k", model=alias, provider="openai"
    )


def test_reload_reply_carries_skipped_aliases_and_invalidates_the_cache(
    monkeypatch: Any, tmp_path: Any, sqlite_backend_factory: Any
) -> None:
    from turnstone.server import internal_model_reload

    storage = sqlite_backend_factory(str(tmp_path / "reload.db"))
    old_reg = ModelRegistry({"a": _cfg("a")}, default="a")
    new_reg = ModelRegistry({"a": _cfg("a"), "b": _cfg("b")}, default="a")
    new_reg.skipped_aliases = {"bad": "secret://vault/x#k: 'x' not found in mount 'secret'"}

    app_state = SimpleNamespace(
        registry=old_reg,
        health_registry=HealthTrackerRegistry(),
        config_store=None,
        node_id="node-a",
    )
    request = SimpleNamespace(app=SimpleNamespace(state=app_state))
    calls: list[str] = []
    import turnstone.core.secret_refs as pkg

    monkeypatch.setattr(pkg, "invalidate_cache", lambda: calls.append("invalidate"))
    monkeypatch.setattr("turnstone.core.model_registry.load_model_registry", lambda **_kw: new_reg)
    monkeypatch.setattr("turnstone.core.storage._registry.get_storage", lambda: storage)
    monkeypatch.setattr("turnstone.server._broadcast_agent_tool_schema_refresh", lambda _s: None)

    response = internal_model_reload(request)  # type: ignore[arg-type]
    assert response.status_code == 200
    payload = json.loads(response.body)
    assert payload["status"] == "ok"
    assert payload["skipped"] == new_reg.skipped_aliases
    assert calls == ["invalidate"]


def test_noop_reload_still_reports_skipped_aliases(
    monkeypatch: Any, tmp_path: Any, sqlite_backend_factory: Any
) -> None:
    from turnstone.server import internal_model_reload

    storage = sqlite_backend_factory(str(tmp_path / "reload.db"))
    old_reg = ModelRegistry({"a": _cfg("a")}, default="a")
    new_reg = ModelRegistry({"a": _cfg("a")}, default="a")
    new_reg.skipped_aliases = {"bad": "reason"}
    app_state = SimpleNamespace(
        registry=old_reg, health_registry=HealthTrackerRegistry(), config_store=None, node_id=""
    )
    request = SimpleNamespace(app=SimpleNamespace(state=app_state))
    monkeypatch.setattr("turnstone.core.model_registry.load_model_registry", lambda **_kw: new_reg)
    monkeypatch.setattr("turnstone.core.storage._registry.get_storage", lambda: storage)

    payload = json.loads(internal_model_reload(request).body)  # type: ignore[arg-type]
    assert payload["noop"] is True
    assert payload["skipped"] == {"bad": "reason"}
