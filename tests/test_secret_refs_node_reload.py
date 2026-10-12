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


def test_the_refresh_timer_tick_picks_up_a_rotation_without_a_sync(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, sqlite_backend_factory: Any
) -> None:
    import turnstone.core.config as cfg_mod
    from turnstone.core import model_registry as mr
    from turnstone.server import _refresh_models_from_store

    monkeypatch.setattr(cfg_mod, "load_config", lambda section=None: {})
    monkeypatch.setattr(mr, "load_config", lambda section=None: {})
    backend = install_fake_resolver(monkeypatch, {"/openai": "sk-v1"})
    storage = sqlite_backend_factory(str(tmp_path / "reload.db"))
    storage.create_model_definition(
        definition_id="def-a",
        alias="a",
        model="gpt-5",
        provider="openai-compatible",
        base_url="https://llm.example.com/v1",
        api_key="secret://fake/openai",
        context_window=8192,
    )
    app_state = _app_state(load_model_registry(storage=storage, strict=True))
    monkeypatch.setattr("turnstone.core.storage._registry.get_storage", lambda: storage)
    monkeypatch.setattr("turnstone.server._broadcast_agent_tool_schema_refresh", lambda _s: None)

    backend.values["/openai"] = "sk-v2"
    _refresh_models_from_store(app_state)
    assert app_state.registry.get_config("a").api_key == "sk-v2"


def test_a_failed_refresh_tick_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    from turnstone.server import _refresh_models_from_store

    with caplog.at_level(logging.WARNING, logger="turnstone.server"):
        _refresh_models_from_store(SimpleNamespace(registry=None))
    assert "server.model_refresh_failed" in caplog.text


def _two_definitions(storage: Any) -> None:
    for alias, ref in (("a", "secret://fake/openai"), ("b", "secret://fake/other")):
        storage.create_model_definition(
            definition_id=f"def-{alias}",
            alias=alias,
            model="gpt-5",
            provider="openai-compatible",
            base_url="https://llm.example.com/v1",
            api_key=ref,
            context_window=8192,
        )


def test_a_tick_keeps_a_refused_model_running_and_applies_the_rest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, sqlite_backend_factory: Any
) -> None:
    import turnstone.core.config as cfg_mod
    from turnstone.core import model_registry as mr
    from turnstone.server import _refresh_models_from_store, internal_model_reload

    monkeypatch.setattr(cfg_mod, "load_config", lambda section=None: {})
    monkeypatch.setattr(mr, "load_config", lambda section=None: {})
    backend = install_fake_resolver(monkeypatch, {"/openai": "sk-v1", "/other": "sk-o1"})
    storage = sqlite_backend_factory(str(tmp_path / "reload.db"))
    _two_definitions(storage)
    app_state = _app_state(load_model_registry(storage=storage, strict=True))
    monkeypatch.setattr("turnstone.core.storage._registry.get_storage", lambda: storage)
    monkeypatch.setattr("turnstone.server._broadcast_agent_tool_schema_refresh", lambda _s: None)

    del backend.values["/openai"]  # the secret is deleted, or a policy now denies it
    backend.values["/other"] = "sk-o2"  # while another key is rotated
    _refresh_models_from_store(app_state)
    assert app_state.registry.list_aliases() == ["a", "b"]
    assert app_state.registry.get_config("a").api_key == "sk-v1"
    assert app_state.registry.get_config("b").api_key == "sk-o2"
    request = SimpleNamespace(app=SimpleNamespace(state=app_state))
    payload = json.loads(internal_model_reload(request).body)  # type: ignore[arg-type]
    assert app_state.registry.list_aliases() == ["b"]  # Sync applies the refusal
    assert "not found" in payload["skipped"]["a"]


def test_a_tick_detects_only_windows_it_has_not_detected_yet(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, sqlite_backend_factory: Any
) -> None:
    import turnstone.core.config as cfg_mod
    from turnstone.core import model_registry as mr
    from turnstone.server import _refresh_models_from_store, internal_model_reload

    monkeypatch.setattr(cfg_mod, "load_config", lambda section=None: {})
    monkeypatch.setattr(mr, "load_config", lambda section=None: {})
    install_fake_resolver(monkeypatch, {"/openai": "sk-v1"})
    probes: list[str] = []

    def probe(cfg: Any) -> tuple[int | None, str]:
        probes.append(cfg.alias)
        return 65536, ""

    monkeypatch.setattr(mr, "_probe_context_window", probe)

    def local(alias: str, model: str) -> None:
        storage.create_model_definition(
            definition_id=f"def-{alias}",
            alias=alias,
            model=model,
            provider="openai-compatible",
            base_url="http://llm.example.com:8000/v1",
            api_key="secret://fake/openai",
            context_window=0,
        )

    storage = sqlite_backend_factory(str(tmp_path / "reload.db"))
    local("known", "qwen")
    app_state = _app_state(
        load_model_registry(storage=storage, strict=True, detect_context_windows=True)
    )
    monkeypatch.setattr("turnstone.core.storage._registry.get_storage", lambda: storage)
    monkeypatch.setattr("turnstone.server._broadcast_agent_tool_schema_refresh", lambda _s: None)
    probes.clear()

    local("fresh", "llama")  # saved without a Sync
    _refresh_models_from_store(app_state)
    assert probes == ["fresh"]  # the known window is kept, the new one detected
    assert app_state.registry.get_config("fresh").context_window == 65536
    assert app_state.registry.get_config("known").context_window == 65536
    probes.clear()
    internal_model_reload(SimpleNamespace(app=SimpleNamespace(state=app_state)))  # type: ignore[arg-type]
    probed = sorted(probes)
    assert probed == ["fresh", "known"]  # an operator's Sync re-probes them all
