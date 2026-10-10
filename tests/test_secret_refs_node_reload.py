"""Node reload: the reply names aliases dropped over their api_key reference and
asks the secret store again before loading."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from tests._secret_refs_helpers import install_fake_resolver
from turnstone.core.healthcheck import HealthTrackerRegistry
from turnstone.core.model_registry import ModelConfig, ModelRegistry, load_model_registry

if TYPE_CHECKING:
    import pytest


def _cfg(alias: str) -> ModelConfig:
    return ModelConfig(
        alias=alias, base_url="http://x", api_key="k", model=alias, provider="openai"
    )


def _app_state(registry: ModelRegistry) -> Any:
    return SimpleNamespace(
        registry=registry,
        health_registry=HealthTrackerRegistry(),
        config_store=None,
        node_id="node-a",
    )


def test_reload_reply_carries_skipped_aliases(
    monkeypatch: Any, tmp_path: Any, sqlite_backend_factory: Any
) -> None:
    from turnstone.server import internal_model_reload

    storage = sqlite_backend_factory(str(tmp_path / "reload.db"))
    old_reg = ModelRegistry({"a": _cfg("a")}, default="a")
    skipped = {"bad": "secret://vault/x#k: 'x' not found in mount 'secret'"}
    new_reg = ModelRegistry({"a": _cfg("a"), "b": _cfg("b")}, default="a", skipped_aliases=skipped)
    request = SimpleNamespace(app=SimpleNamespace(state=_app_state(old_reg)))
    monkeypatch.setattr("turnstone.core.model_registry.load_model_registry", lambda **_kw: new_reg)
    monkeypatch.setattr("turnstone.core.storage._registry.get_storage", lambda: storage)
    monkeypatch.setattr("turnstone.server._broadcast_agent_tool_schema_refresh", lambda _s: None)

    response = internal_model_reload(request)  # type: ignore[arg-type]
    assert response.status_code == 200
    payload = json.loads(response.body)
    assert payload["status"] == "ok"
    assert payload["skipped"] == skipped


def test_reload_picks_up_a_value_rotated_in_the_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, sqlite_backend_factory: Any
) -> None:
    """A Sync asks the store again inside the cache TTL: that is how a rotation lands."""
    import turnstone.core.config as cfg_mod
    from turnstone.core import model_registry as mr
    from turnstone.server import internal_model_reload

    monkeypatch.setattr(cfg_mod, "load_config", lambda section=None: {})
    monkeypatch.setattr(mr, "load_config", lambda section=None: {})
    backend = install_fake_resolver(monkeypatch, {"/openai": "sk-v1"})
    storage = sqlite_backend_factory(str(tmp_path / "reload.db"))
    storage.create_model_definition(
        definition_id="def-a",
        alias="a",
        model="gpt-5",
        provider="openai",
        base_url="https://api.openai.com/v1",
        api_key="secret://fake/openai",
        context_window=8192,
    )
    registry = load_model_registry(storage=storage, strict=True)
    assert registry.get_config("a").api_key == "sk-v1"
    app_state = _app_state(registry)
    request = SimpleNamespace(app=SimpleNamespace(state=app_state))
    monkeypatch.setattr("turnstone.core.storage._registry.get_storage", lambda: storage)
    monkeypatch.setattr("turnstone.server._broadcast_agent_tool_schema_refresh", lambda _s: None)

    backend.values["/openai"] = "sk-v2"
    payload = json.loads(internal_model_reload(request).body)  # type: ignore[arg-type]
    assert payload["status"] == "ok" and "skipped" not in payload
    assert app_state.registry.get_config("a").api_key == "sk-v2"
    assert storage.get_model_definition_by_alias("a")["api_key"] == "secret://fake/openai"


def test_noop_reload_still_reports_skipped_aliases(
    monkeypatch: Any, tmp_path: Any, sqlite_backend_factory: Any
) -> None:
    from turnstone.server import internal_model_reload

    storage = sqlite_backend_factory(str(tmp_path / "reload.db"))
    old_reg = ModelRegistry({"a": _cfg("a")}, default="a")
    new_reg = ModelRegistry({"a": _cfg("a")}, default="a", skipped_aliases={"bad": "reason"})
    request = SimpleNamespace(app=SimpleNamespace(state=_app_state(old_reg)))
    monkeypatch.setattr("turnstone.core.model_registry.load_model_registry", lambda **_kw: new_reg)
    monkeypatch.setattr("turnstone.core.storage._registry.get_storage", lambda: storage)

    payload = json.loads(internal_model_reload(request).body)  # type: ignore[arg-type]
    assert payload["noop"] is True
    assert payload["skipped"] == {"bad": "reason"}
