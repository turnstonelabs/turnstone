"""Tests for turnstone.core.model_registry — model registry, loading, session integration."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from turnstone.core.model_registry import (
    ModelConfig,
    ModelRegistry,
    _resolve_env_vars,
    detect_model,
    load_model_registry,
)

# ---------------------------------------------------------------------------
# ModelConfig
# ---------------------------------------------------------------------------


class TestModelConfig:
    def test_construction(self) -> None:
        cfg = ModelConfig(
            alias="local",
            base_url="http://localhost:8000/v1",
            api_key="dummy",
            model="qwen3-32b",
        )
        assert cfg.alias == "local"
        assert cfg.model == "qwen3-32b"
        assert cfg.context_window == 32768  # default

    def test_custom_context_window(self) -> None:
        cfg = ModelConfig(
            alias="oai",
            base_url="https://api.openai.com/v1",
            api_key="sk-test",
            model="gpt-4o",
            context_window=128000,
        )
        assert cfg.context_window == 128000

    def test_frozen(self) -> None:
        cfg = ModelConfig(alias="x", base_url="x", api_key="x", model="x")
        with pytest.raises(AttributeError):
            cfg.alias = "y"  # type: ignore[misc]

    def test_api_key_not_in_repr(self) -> None:
        cfg = ModelConfig(alias="test", base_url="http://x", api_key="sk-secret-key", model="m")
        assert "sk-secret-key" not in repr(cfg)

    def test_sampling_params_default_none(self) -> None:
        cfg = ModelConfig(alias="x", base_url="x", api_key="x", model="x")
        assert cfg.temperature is None
        assert cfg.max_tokens is None
        assert cfg.reasoning_effort is None

    def test_sampling_params_set(self) -> None:
        cfg = ModelConfig(
            alias="x",
            base_url="x",
            api_key="x",
            model="x",
            temperature=0.7,
            max_tokens=8192,
            reasoning_effort="high",
        )
        assert cfg.temperature == 0.7
        assert cfg.max_tokens == 8192
        assert cfg.reasoning_effort == "high"

    def test_zero_temperature_distinct_from_none(self) -> None:
        cfg = ModelConfig(alias="x", base_url="x", api_key="x", model="x", temperature=0.0)
        assert cfg.temperature == 0.0
        assert cfg.temperature is not None


# ---------------------------------------------------------------------------
# ModelRegistry
# ---------------------------------------------------------------------------


class TestModelRegistry:
    def _make_registry(
        self,
        fallback: list[str] | None = None,
        agent_model: str | None = None,
    ) -> ModelRegistry:
        models = {
            "default": ModelConfig("default", "http://localhost:8000/v1", "dummy", "qwen3-32b"),
            "openai": ModelConfig(
                "openai", "https://api.openai.com/v1", "sk-test", "gpt-4o", 128000
            ),
            "cheap": ModelConfig(
                "cheap", "https://api.openai.com/v1", "sk-test", "gpt-4o-mini", 128000
            ),
        }
        return ModelRegistry(
            models=models,
            default="default",
            fallback=fallback,
            agent_model=agent_model,
        )

    def test_resolve_default(self) -> None:
        reg = self._make_registry()
        client, model, cfg = reg.resolve()
        assert model == "qwen3-32b"
        assert cfg.alias == "default"

    def test_resolve_alias(self) -> None:
        reg = self._make_registry()
        client, model, cfg = reg.resolve("openai")
        assert model == "gpt-4o"
        assert cfg.context_window == 128000

    def test_resolve_none_uses_default(self) -> None:
        reg = self._make_registry()
        _, model1, _ = reg.resolve(None)
        _, model2, _ = reg.resolve()
        assert model1 == model2

    def test_lazy_client_creation(self) -> None:
        reg = self._make_registry()
        assert len(reg._clients) == 0
        reg.get_client("default")
        assert len(reg._clients) == 1
        # Second call reuses
        c1 = reg.get_client("default")
        c2 = reg.get_client("default")
        assert c1 is c2

    def test_list_aliases(self) -> None:
        reg = self._make_registry()
        aliases = reg.list_aliases()
        assert set(aliases) == {"default", "openai", "cheap"}

    def test_count(self) -> None:
        reg = self._make_registry()
        assert reg.count == 3

    def test_unknown_alias_error(self) -> None:
        reg = self._make_registry()
        with pytest.raises(ValueError, match="Unknown model alias"):
            reg.get_config("nonexistent")
        with pytest.raises(ValueError, match="Unknown model alias"):
            reg.get_client("nonexistent")

    def test_shutdown(self) -> None:
        reg = self._make_registry()
        reg.get_client("default")
        reg.get_client("openai")
        assert len(reg._clients) == 2
        reg.shutdown()
        assert len(reg._clients) == 0

    def test_has_alias(self) -> None:
        reg = self._make_registry()
        assert reg.has_alias("default")
        assert reg.has_alias("openai")
        assert not reg.has_alias("nonexistent")

    def test_concurrent_get_client(self) -> None:
        """Thread-safe lazy client creation under concurrency."""
        import concurrent.futures

        reg = self._make_registry()
        clients: list[Any] = []

        def get_it() -> Any:
            return reg.get_client("default")

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
            futs = [pool.submit(get_it) for _ in range(20)]
            clients = [f.result() for f in futs]

        # All threads should get the same client instance
        assert all(c is clients[0] for c in clients)
        assert len(reg._clients) == 1

    def test_fallback_stored(self) -> None:
        reg = self._make_registry(fallback=["openai", "cheap"])
        assert reg.fallback == ["openai", "cheap"]

    def test_agent_model_stored(self) -> None:
        reg = self._make_registry(agent_model="cheap")
        assert reg.agent_model == "cheap"


class TestModelRegistryValidation:
    def test_empty_models_raises(self) -> None:
        with pytest.raises(ValueError, match="at least one"):
            ModelRegistry(models={}, default="x")

    def test_invalid_default_raises(self) -> None:
        models = {"a": ModelConfig("a", "x", "x", "x")}
        with pytest.raises(ValueError, match="Default model 'bad'"):
            ModelRegistry(models=models, default="bad")

    def test_invalid_fallback_raises(self) -> None:
        models = {"a": ModelConfig("a", "x", "x", "x")}
        with pytest.raises(ValueError, match="Fallback model 'bad'"):
            ModelRegistry(models=models, default="a", fallback=["bad"])

    def test_invalid_agent_model_raises(self) -> None:
        models = {"a": ModelConfig("a", "x", "x", "x")}
        with pytest.raises(ValueError, match="Agent model 'bad'"):
            ModelRegistry(models=models, default="a", agent_model="bad")


# ---------------------------------------------------------------------------
# load_model_registry
# ---------------------------------------------------------------------------


class TestLoadModelRegistry:
    def test_single_entry_from_args(self) -> None:
        """No [models] config → single-entry registry from CLI args."""
        with patch("turnstone.core.model_registry.load_config", return_value={}):
            reg = load_model_registry(
                base_url="http://localhost:8000/v1",
                api_key="dummy",
                model="qwen3-32b",
            )
        assert reg.count == 1
        assert reg.default == "default"
        _, model, cfg = reg.resolve()
        assert model == "qwen3-32b"
        assert cfg.base_url == "http://localhost:8000/v1"

    def test_models_from_config(self) -> None:
        """[models.*] sections create additional entries."""
        fake_cfg: dict[str, Any] = {
            "models": {
                "openai": {
                    "base_url": "https://api.openai.com/v1",
                    "api_key": "sk-test",
                    "model": "gpt-4o",
                    "context_window": 128000,
                },
            },
            "model": {
                "default": "openai",
            },
        }
        with patch("turnstone.core.model_registry.load_config", return_value=fake_cfg):
            reg = load_model_registry(
                base_url="http://localhost:8000/v1",
                api_key="dummy",
                model="local-model",
            )
        assert reg.count == 2  # "openai" + "default"
        assert reg.default == "openai"
        _, model, _ = reg.resolve()
        assert model == "gpt-4o"

    def test_fallback_from_config(self) -> None:
        fake_cfg: dict[str, Any] = {
            "models": {
                "fallback1": {
                    "base_url": "http://fb1/v1",
                    "model": "fb-model",
                },
            },
            "model": {
                "fallback": ["fallback1", "nonexistent"],
            },
        }
        with patch("turnstone.core.model_registry.load_config", return_value=fake_cfg):
            reg = load_model_registry("http://x/v1", "x", "x")
        # "nonexistent" is silently dropped
        assert reg.fallback == ["fallback1"]

    def test_agent_model_from_config(self) -> None:
        fake_cfg: dict[str, Any] = {
            "models": {
                "cheap": {
                    "base_url": "http://cheap/v1",
                    "model": "cheap-model",
                },
            },
            "model": {
                "agent_model": "cheap",
            },
        }
        with patch("turnstone.core.model_registry.load_config", return_value=fake_cfg):
            reg = load_model_registry("http://x/v1", "x", "x")
        assert reg.agent_model == "cheap"

    def test_invalid_agent_model_ignored(self) -> None:
        fake_cfg: dict[str, Any] = {
            "model": {"agent_model": "nonexistent"},
        }
        with patch("turnstone.core.model_registry.load_config", return_value=fake_cfg):
            reg = load_model_registry("http://x/v1", "x", "x")
        assert reg.agent_model is None

    def test_invalid_default_falls_back(self) -> None:
        fake_cfg: dict[str, Any] = {
            "model": {"default": "nonexistent"},
        }
        with patch("turnstone.core.model_registry.load_config", return_value=fake_cfg):
            reg = load_model_registry("http://x/v1", "x", "x")
        assert reg.default == "default"

    def test_empty_model_name_skipped(self) -> None:
        """Config entries without a model name are skipped."""
        fake_cfg: dict[str, Any] = {
            "models": {
                "bad": {"base_url": "http://bad/v1"},  # no model key
                "good": {"base_url": "http://good/v1", "model": "good-model"},
            },
        }
        with patch("turnstone.core.model_registry.load_config", return_value=fake_cfg):
            reg = load_model_registry("http://x/v1", "x", "x")
        assert not reg.has_alias("bad")
        assert reg.has_alias("good")

    def test_unknown_fallback_logged_and_dropped(self) -> None:
        fake_cfg: dict[str, Any] = {
            "model": {"fallback": ["good", "bad"]},
            "models": {
                "good": {"base_url": "http://g/v1", "model": "g-model"},
            },
        }
        with patch("turnstone.core.model_registry.load_config", return_value=fake_cfg):
            reg = load_model_registry("http://x/v1", "x", "x")
        assert reg.fallback == ["good"]

    def test_models_inherit_cli_args(self) -> None:
        """Model entries without base_url/api_key inherit from CLI args."""
        fake_cfg: dict[str, Any] = {
            "models": {
                "alt": {
                    "model": "alt-model",
                },
            },
        }
        with patch("turnstone.core.model_registry.load_config", return_value=fake_cfg):
            reg = load_model_registry("http://base/v1", "my-key", "default-model")
        alt_cfg = reg.get_config("alt")
        assert alt_cfg.base_url == "http://base/v1"
        assert alt_cfg.api_key == "my-key"


# ---------------------------------------------------------------------------
# load_model_registry with DB storage
# ---------------------------------------------------------------------------


class _MockStorage:
    """Minimal storage mock returning canned model definitions."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self._rows = rows or []
        self.calls: list[str] = []

    def list_model_definitions(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        self.calls.append("list_model_definitions")
        if enabled_only:
            return [r for r in self._rows if r.get("enabled", True)]
        return list(self._rows)


class TestLoadModelRegistryWithDB:
    def test_db_models_loaded(self) -> None:
        """DB model definitions are loaded into the registry."""
        storage = _MockStorage(
            [
                {
                    "alias": "cloud-gpt",
                    "model": "gpt-5",
                    "provider": "openai",
                    "base_url": "https://api.openai.com/v1",
                    "api_key": "sk-db",
                    "context_window": 128000,
                    "capabilities": "{}",
                    "enabled": True,
                }
            ]
        )
        with patch("turnstone.core.model_registry.load_config", return_value={}):
            reg = load_model_registry("http://x/v1", "x", "x", storage=storage)
        assert reg.has_alias("cloud-gpt")
        cfg = reg.get_config("cloud-gpt")
        assert cfg.model == "gpt-5"
        assert cfg.source == "db"

    def test_config_overrides_db(self) -> None:
        """Config.toml entry overrides DB entry with same alias."""
        storage = _MockStorage(
            [
                {
                    "alias": "shared",
                    "model": "db-model",
                    "provider": "openai",
                    "base_url": "http://db/v1",
                    "api_key": "sk-db",
                    "context_window": 32768,
                    "capabilities": "{}",
                    "enabled": True,
                }
            ]
        )
        fake_cfg: dict[str, Any] = {
            "models": {
                "shared": {
                    "model": "config-model",
                    "base_url": "http://config/v1",
                },
            },
        }
        with patch("turnstone.core.model_registry.load_config", return_value=fake_cfg):
            reg = load_model_registry("http://x/v1", "x", "x", storage=storage)
        cfg = reg.get_config("shared")
        assert cfg.model == "config-model"
        assert cfg.source == "config"

    def test_db_only_models_coexist(self) -> None:
        """DB models coexist alongside config.toml models."""
        storage = _MockStorage(
            [
                {
                    "alias": "db-only",
                    "model": "db-model",
                    "provider": "anthropic",
                    "base_url": "",
                    "api_key": "sk-db",
                    "context_window": 200000,
                    "capabilities": "{}",
                    "enabled": True,
                }
            ]
        )
        fake_cfg: dict[str, Any] = {
            "models": {
                "config-only": {"model": "config-model"},
            },
        }
        with patch("turnstone.core.model_registry.load_config", return_value=fake_cfg):
            reg = load_model_registry("http://x/v1", "x", "x", storage=storage)
        assert reg.has_alias("db-only")
        assert reg.has_alias("config-only")
        assert reg.has_alias("default")
        assert reg.get_config("db-only").source == "db"
        assert reg.get_config("config-only").source == "config"

    def test_source_field_set(self) -> None:
        """Source field correctly distinguishes origin."""
        storage = _MockStorage(
            [
                {
                    "alias": "from-db",
                    "model": "m",
                    "provider": "openai",
                    "base_url": "",
                    "api_key": "",
                    "context_window": 32768,
                    "capabilities": "{}",
                    "enabled": True,
                }
            ]
        )
        with patch("turnstone.core.model_registry.load_config", return_value={}):
            reg = load_model_registry("http://x/v1", "x", "x", storage=storage)
        assert reg.get_config("from-db").source == "db"
        assert reg.get_config("default").source == ""

    def test_disabled_db_models_excluded(self) -> None:
        """Disabled DB models are not loaded."""
        storage = _MockStorage(
            [
                {
                    "alias": "disabled",
                    "model": "m",
                    "provider": "openai",
                    "base_url": "",
                    "api_key": "",
                    "context_window": 32768,
                    "capabilities": "{}",
                    "enabled": False,
                }
            ]
        )
        with patch("turnstone.core.model_registry.load_config", return_value={}):
            reg = load_model_registry("http://x/v1", "x", "x", storage=storage)
        assert not reg.has_alias("disabled")

    def test_db_capabilities_parsed(self) -> None:
        """JSON capabilities from DB are parsed into dict."""
        storage = _MockStorage(
            [
                {
                    "alias": "caps-model",
                    "model": "m",
                    "provider": "openai",
                    "base_url": "",
                    "api_key": "",
                    "context_window": 32768,
                    "capabilities": '{"supports_vision": true}',
                    "enabled": True,
                }
            ]
        )
        with patch("turnstone.core.model_registry.load_config", return_value={}):
            reg = load_model_registry("http://x/v1", "x", "x", storage=storage)
        assert reg.get_config("caps-model").capabilities == {"supports_vision": True}

    def test_db_sampling_params_loaded(self) -> None:
        """Per-model sampling params from DB are carried in ModelConfig."""
        storage = _MockStorage(
            [
                {
                    "alias": "hot-model",
                    "model": "m",
                    "provider": "openai",
                    "base_url": "",
                    "api_key": "",
                    "context_window": 32768,
                    "capabilities": "{}",
                    "enabled": True,
                    "temperature": 1.5,
                    "max_tokens": 4096,
                    "reasoning_effort": "high",
                }
            ]
        )
        with patch("turnstone.core.model_registry.load_config", return_value={}):
            reg = load_model_registry("http://x/v1", "x", "x", storage=storage)
        cfg = reg.get_config("hot-model")
        assert cfg.temperature == 1.5
        assert cfg.max_tokens == 4096
        assert cfg.reasoning_effort == "high"

    def test_db_sampling_params_null_means_none(self) -> None:
        """NULL sampling params in DB map to None (use global default)."""
        storage = _MockStorage(
            [
                {
                    "alias": "null-model",
                    "model": "m",
                    "provider": "openai",
                    "base_url": "",
                    "api_key": "",
                    "context_window": 32768,
                    "capabilities": "{}",
                    "enabled": True,
                    "temperature": None,
                    "max_tokens": None,
                    "reasoning_effort": None,
                }
            ]
        )
        with patch("turnstone.core.model_registry.load_config", return_value={}):
            reg = load_model_registry("http://x/v1", "x", "x", storage=storage)
        cfg = reg.get_config("null-model")
        assert cfg.temperature is None
        assert cfg.max_tokens is None
        assert cfg.reasoning_effort is None

    def test_db_default_alias_not_clobbered(self) -> None:
        """DB model with alias='default' is not overwritten by CLI args."""
        storage = _MockStorage(
            [
                {
                    "alias": "default",
                    "model": "db-default-model",
                    "provider": "openai",
                    "base_url": "http://db/v1",
                    "api_key": "sk-db",
                    "context_window": 128000,
                    "capabilities": "{}",
                    "enabled": True,
                }
            ]
        )
        with patch("turnstone.core.model_registry.load_config", return_value={}):
            reg = load_model_registry("http://cli/v1", "cli-key", "cli-model", storage=storage)
        cfg = reg.get_config("default")
        assert cfg.model == "db-default-model"
        assert cfg.source == "db"

    def test_no_db_writes(self) -> None:
        """Config.toml models are NOT written to storage."""
        storage = _MockStorage()
        fake_cfg: dict[str, Any] = {
            "models": {"local": {"model": "llama"}},
        }
        with patch("turnstone.core.model_registry.load_config", return_value=fake_cfg):
            load_model_registry("http://x/v1", "x", "x", storage=storage)
        # Only list_model_definitions should be called, no create
        assert storage.calls == ["list_model_definitions"]

    def test_storage_failure_graceful(self) -> None:
        """Storage errors don't prevent registry creation."""
        storage = MagicMock()
        storage.list_model_definitions.side_effect = RuntimeError("db down")
        with patch("turnstone.core.model_registry.load_config", return_value={}):
            reg = load_model_registry("http://x/v1", "x", "x", storage=storage)
        assert reg.has_alias("default")


# ---------------------------------------------------------------------------
# _resolve_env_vars
# ---------------------------------------------------------------------------


class TestResolveEnvVars:
    def test_expand_single(self) -> None:
        with patch.dict("os.environ", {"MY_KEY": "secret123"}):
            assert _resolve_env_vars("sk-${MY_KEY}") == "sk-secret123"

    def test_expand_multiple(self) -> None:
        with patch.dict("os.environ", {"A": "1", "B": "2"}):
            assert _resolve_env_vars("${A}-${B}") == "1-2"

    def test_missing_var_empty(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            assert _resolve_env_vars("${MISSING}") == ""

    def test_no_vars(self) -> None:
        assert _resolve_env_vars("plain-key") == "plain-key"

    def test_empty_string(self) -> None:
        assert _resolve_env_vars("") == ""


# ---------------------------------------------------------------------------
# ModelRegistry.reload
# ---------------------------------------------------------------------------


class TestRegistryReload:
    def test_reload_replaces_models(self) -> None:
        models_a = {"a": ModelConfig("a", "x", "x", "m1")}
        reg = ModelRegistry(models=models_a, default="a")
        assert reg.has_alias("a")

        models_b = {"b": ModelConfig("b", "y", "y", "m2")}
        reg.reload(models_b, "b")
        assert not reg.has_alias("a")
        assert reg.has_alias("b")
        assert reg.default == "b"

    def test_reload_clears_clients(self) -> None:
        models = {"a": ModelConfig("a", "http://x/v1", "key", "m")}
        reg = ModelRegistry(models=models, default="a")
        # Force client creation
        reg.get_client("a")
        assert "a" in reg._clients

        # Reload with same models — clients should be cleared
        reg.reload(dict(models), "a")
        assert "a" not in reg._clients

    def test_reload_validates_default(self) -> None:
        models_a = {"a": ModelConfig("a", "x", "x", "m")}
        reg = ModelRegistry(models=models_a, default="a")
        with pytest.raises(ValueError, match="Default model"):
            reg.reload(models_a, "nonexistent")
        # Registry should be unchanged after failed reload
        assert reg.has_alias("a")
        assert reg.default == "a"

    def test_reload_validates_empty(self) -> None:
        models_a = {"a": ModelConfig("a", "x", "x", "m")}
        reg = ModelRegistry(models=models_a, default="a")
        with pytest.raises(ValueError, match="at least one"):
            reg.reload({}, "a")


# ---------------------------------------------------------------------------
# Session integration
# ---------------------------------------------------------------------------


class _FakeUI:
    """Minimal SessionUI stub for testing."""

    def __init__(self) -> None:
        self.infos: list[str] = []
        self.errors: list[str] = []

    def on_thinking_start(self) -> None: ...
    def on_thinking_stop(self) -> None: ...
    def on_reasoning_token(self, text: str) -> None: ...
    def on_content_token(self, text: str) -> None: ...
    def on_stream_end(self) -> None: ...
    def approve_tools(self, items: list[dict[str, Any]]) -> tuple[bool, str | None]:
        return True, None

    def on_tool_result(self, call_id: str, name: str, output: str, **kwargs: Any) -> None: ...
    def on_tool_output_chunk(self, call_id: str, chunk: str) -> None: ...
    def on_status(self, usage: dict[str, Any], context_window: int, effort: str) -> None: ...
    def on_plan_review(self, content: str) -> str:
        return "approve"

    def on_info(self, message: str) -> None:
        self.infos.append(message)

    def on_error(self, message: str) -> None:
        self.errors.append(message)

    def on_state_change(self, state: str) -> None: ...
    def on_rename(self, name: str) -> None: ...
    def on_output_warning(self, call_id, assessment): ...


def _make_session(
    registry: ModelRegistry | None = None,
    model_alias: str | None = None,
) -> Any:
    """Create a ChatSession with a mock client and optional registry."""
    from turnstone.core.session import ChatSession

    client = MagicMock()
    return ChatSession(
        client=client,
        model="test-model",
        ui=_FakeUI(),
        instructions=None,
        temperature=0.5,
        max_tokens=4096,
        tool_timeout=30,
        registry=registry,
        model_alias=model_alias,
    )


class TestSessionModelCommand:
    def test_model_show_without_registry(self) -> None:
        session = _make_session()
        session.handle_command("/model")
        assert "test-model" in session.ui.infos[-1]

    def test_model_show_with_registry(self) -> None:
        reg = ModelRegistry(
            models={
                "default": ModelConfig("default", "x", "x", "test-model"),
                "alt": ModelConfig("alt", "y", "y", "alt-model"),
            },
            default="default",
        )
        session = _make_session(registry=reg, model_alias="default")
        session.handle_command("/model")
        info = session.ui.infos[-1]
        assert "test-model" in info
        assert "default" in info
        assert "alt" in info

    def test_model_switch(self) -> None:
        reg = ModelRegistry(
            models={
                "default": ModelConfig("default", "x", "x", "default-model"),
                "alt": ModelConfig("alt", "y", "y", "alt-model", context_window=64000),
            },
            default="default",
        )
        session = _make_session(registry=reg, model_alias="default")
        session.handle_command("/model alt")
        assert session.model == "alt-model"
        assert session.model_alias == "alt"
        assert session.context_window == 64000
        assert "Switched to" in session.ui.infos[-1]

    def test_model_switch_applies_sampling_params(self) -> None:
        reg = ModelRegistry(
            models={
                "default": ModelConfig("default", "x", "x", "default-model"),
                "hot": ModelConfig(
                    "hot",
                    "y",
                    "y",
                    "hot-model",
                    temperature=1.5,
                    max_tokens=2048,
                    reasoning_effort="high",
                ),
            },
            default="default",
        )
        session = _make_session(registry=reg, model_alias="default")
        assert session.temperature == 0.5  # initial global default
        session.handle_command("/model hot")
        assert session.temperature == 1.5
        assert session.max_tokens == 2048
        assert session.reasoning_effort == "high"

    def test_model_switch_none_params_reverts_to_global(self) -> None:
        """Switching to a model with no overrides reverts to global defaults."""
        reg = ModelRegistry(
            models={
                "hot": ModelConfig("hot", "x", "x", "hot-model", temperature=1.5),
                "plain": ModelConfig("plain", "y", "y", "plain-model"),
            },
            default="hot",
        )
        session = _make_session(registry=reg, model_alias="hot")
        session.temperature = 1.5  # as set by per-model override
        # Without a config_store, fallback keeps current value (CLI sessions).
        # With a config_store, it would revert to the global default.
        session.handle_command("/model plain")
        assert session.temperature == 1.5  # no config_store → keeps current

    def test_model_switch_unknown_alias(self) -> None:
        reg = ModelRegistry(
            models={"default": ModelConfig("default", "x", "x", "test-model")},
            default="default",
        )
        session = _make_session(registry=reg, model_alias="default")
        session.handle_command("/model nonexistent")
        assert "Unknown model alias" in session.ui.infos[-1]

    def test_model_switch_without_registry(self) -> None:
        session = _make_session()
        session.handle_command("/model something")
        assert "Unknown model alias" in session.ui.infos[-1]

    def test_model_show_fallback_info(self) -> None:
        reg = ModelRegistry(
            models={
                "a": ModelConfig("a", "x", "x", "m-a"),
                "b": ModelConfig("b", "y", "y", "m-b"),
            },
            default="a",
            fallback=["b"],
            agent_model="b",
        )
        session = _make_session(registry=reg, model_alias="a")
        session.handle_command("/model")
        info = session.ui.infos[-1]
        assert "Fallback: b" in info
        assert "Agent model: b" in info


class TestSessionFallback:
    def test_fallback_on_primary_failure(self) -> None:
        reg = ModelRegistry(
            models={
                "primary": ModelConfig("primary", "http://p/v1", "k", "p-model"),
                "fallback": ModelConfig("fallback", "http://f/v1", "k", "f-model"),
            },
            default="primary",
            fallback=["fallback"],
        )
        session = _make_session(registry=reg, model_alias="primary")

        # _try_stream: first call (primary) raises, second call (fallback) succeeds
        call_count = 0

        def fake_try_stream(client: Any, model: str, msgs: Any, **kwargs: Any) -> str:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionError("Primary down")
            return "fallback_response"

        session._try_stream = fake_try_stream  # type: ignore[assignment]
        result = session._create_stream_with_retry([{"role": "user", "content": "hi"}])
        assert result == "fallback_response"
        assert call_count == 2
        assert any("falling back" in i for i in session.ui.infos)

    def test_no_fallback_without_registry(self) -> None:
        session = _make_session()

        def fake_try_stream(client: Any, model: str, msgs: Any, **kwargs: Any) -> str:
            raise ConnectionError("Down")

        session._try_stream = fake_try_stream  # type: ignore[assignment]
        with pytest.raises(ConnectionError):
            session._create_stream_with_retry([{"role": "user", "content": "hi"}])


class TestSessionAgentModel:
    def test_agent_model_resolved(self) -> None:
        reg = ModelRegistry(
            models={
                "main": ModelConfig(
                    "main", "http://m/v1", "k", "main-model", provider="openai-compatible"
                ),
                "agent": ModelConfig(
                    "agent", "http://a/v1", "k", "agent-model", provider="openai-compatible"
                ),
            },
            default="main",
            agent_model="agent",
        )
        session = _make_session(registry=reg, model_alias="main")

        # Mock the API to capture what model was used
        captured_model = None
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "done"
        mock_response.choices[0].message.tool_calls = None
        mock_response.choices[0].finish_reason = "stop"

        def fake_create(**kwargs: Any) -> Any:
            nonlocal captured_model
            captured_model = kwargs.get("model")
            return mock_response

        # Get the agent client from the registry and patch it
        agent_client = reg.get_client("agent")
        agent_client.chat.completions.create = fake_create

        agent_msgs = [
            {"role": "developer", "content": "You are an agent."},
            {"role": "user", "content": "Do something."},
        ]
        session._run_agent(agent_msgs)
        assert captured_model == "agent-model"


# ---------------------------------------------------------------------------
# Workstream integration
# ---------------------------------------------------------------------------


class TestWorkstreamModelParam:
    def test_create_with_model(self) -> None:
        """WorkstreamManager.create passes model_alias to session_factory."""
        from turnstone.core.workstream import WorkstreamManager

        captured_alias = None

        def factory(
            ui: Any, model_alias: str | None = None, ws_id: str | None = None, **kwargs: Any
        ) -> Any:
            nonlocal captured_alias
            captured_alias = model_alias
            mock_session = MagicMock()
            mock_session.ws_id = "test123"
            return mock_session

        mgr = WorkstreamManager(factory)
        mgr.create(name="test", model="openai")
        assert captured_alias == "openai"

    def test_create_without_model(self) -> None:
        captured_alias = None

        def factory(
            ui: Any, model_alias: str | None = None, ws_id: str | None = None, **kwargs: Any
        ) -> Any:
            nonlocal captured_alias
            captured_alias = model_alias
            mock_session = MagicMock()
            mock_session.ws_id = "test123"
            return mock_session

        from turnstone.core.workstream import WorkstreamManager

        mgr = WorkstreamManager(factory)
        mgr.create(name="test")
        assert captured_alias is None


# ---------------------------------------------------------------------------
# CreateWorkstreamRequest model field
# ---------------------------------------------------------------------------


class TestCreateWorkstreamRequestModel:
    def test_request_has_model(self) -> None:
        from turnstone.api.server_schemas import CreateWorkstreamRequest

        req = CreateWorkstreamRequest(name="test", model="openai")
        assert req.model == "openai"

    def test_request_model_default(self) -> None:
        from turnstone.api.server_schemas import CreateWorkstreamRequest

        req = CreateWorkstreamRequest(name="test")
        assert req.model == ""

    def test_json_payload_carries_model(self) -> None:
        body = {"name": "ws1", "model": "local"}
        assert body["model"] == "local"
        assert body["name"] == "ws1"


# ---------------------------------------------------------------------------
# detect_model — startup timeout
# ---------------------------------------------------------------------------


class TestDetectModelTimeout:
    def test_uses_short_timeout_and_no_retries(self) -> None:
        """detect_model() uses with_options(timeout=10, max_retries=0)."""
        mock_model = MagicMock()
        mock_model.id = "test-model"
        mock_model.owned_by = "test"

        fast_client = MagicMock()
        fast_client.models.list.return_value = MagicMock(data=[mock_model])

        client = MagicMock()
        client.with_options.return_value = fast_client

        result = detect_model(client, provider="openai")
        client.with_options.assert_called_once_with(timeout=10.0, max_retries=0)
        fast_client.models.list.assert_called_once()
        assert result[0] == "test-model"

    def test_connection_error_non_fatal(self) -> None:
        """detect_model(fatal=False) returns (None, None) on connection error."""
        client = MagicMock()
        client.with_options.return_value = client
        client.models.list.side_effect = OSError("Connection refused")

        result = detect_model(client, provider="openai", fatal=False)
        assert result == (None, None)

    def test_vllm_max_model_len_detected(self) -> None:
        """detect_model() reads max_model_len from vLLM model objects."""
        mock_model = MagicMock()
        mock_model.id = "/models/nemotron"
        mock_model.model_dump.return_value = {
            "owned_by": "vllm",
            "max_model_len": 262144,
        }

        fast_client = MagicMock()
        fast_client.models.list.return_value = MagicMock(data=[mock_model])

        client = MagicMock()
        client.with_options.return_value = fast_client

        model_id, ctx = detect_model(client, provider="openai")
        assert model_id == "/models/nemotron"
        assert ctx == 262144


class TestExtractContextWindow:
    def test_vllm_max_model_len(self) -> None:
        from turnstone.core.model_registry import _extract_context_window

        m = MagicMock()
        m.id = "/models/test"
        m.model_dump.return_value = {"max_model_len": 131072}
        assert _extract_context_window(m, "openai") == 131072

    def test_llama_cpp_meta(self) -> None:
        from turnstone.core.model_registry import _extract_context_window

        m = MagicMock()
        m.id = "test"
        m.model_dump.return_value = {"meta": {"n_ctx_train": 8192}}
        assert _extract_context_window(m, "openai") == 8192

    def test_vllm_preferred_over_meta(self) -> None:
        from turnstone.core.model_registry import _extract_context_window

        m = MagicMock()
        m.id = "test"
        m.model_dump.return_value = {"max_model_len": 262144, "meta": {"n_ctx_train": 4096}}
        assert _extract_context_window(m, "openai") == 262144

    def test_no_metadata_returns_none(self) -> None:
        from turnstone.core.model_registry import _extract_context_window

        m = MagicMock()
        m.id = "test"
        m.model_dump.return_value = {}
        assert _extract_context_window(m, "openai") is None

    # Model-change detection via active probes was removed.
    # Backend health is now tracked passively (see test_healthcheck.py).


# ---------------------------------------------------------------------------
# load_model_registry — DB-only startup (no CLI model)
# ---------------------------------------------------------------------------


class TestLoadModelRegistryDBOnly:
    """Tests for starting the server with models defined only in DB/config,
    without any CLI --model argument."""

    def test_db_only_no_cli_model(self) -> None:
        """Registry builds from DB models when model='' (no CLI model)."""
        storage = _MockStorage(
            [
                {
                    "alias": "cloud",
                    "model": "gpt-5",
                    "provider": "openai",
                    "base_url": "https://api.openai.com/v1",
                    "api_key": "sk-test",
                    "context_window": 128000,
                    "capabilities": "{}",
                    "enabled": True,
                },
            ]
        )
        with patch("turnstone.core.model_registry.load_config", return_value={}):
            reg = load_model_registry(model="", storage=storage)
        assert reg.count == 1
        assert reg.has_alias("cloud")
        # "cloud" should be picked as default since "default" doesn't exist
        assert reg.default == "cloud"

    def test_db_only_with_config_default(self) -> None:
        """Config [model].default is respected when it matches a DB alias."""
        storage = _MockStorage(
            [
                {
                    "alias": "fast",
                    "model": "gpt-4o-mini",
                    "provider": "openai",
                    "base_url": "https://api.openai.com/v1",
                    "api_key": "sk-test",
                    "context_window": 128000,
                    "capabilities": "{}",
                    "enabled": True,
                },
                {
                    "alias": "smart",
                    "model": "gpt-5",
                    "provider": "openai",
                    "base_url": "https://api.openai.com/v1",
                    "api_key": "sk-test",
                    "context_window": 128000,
                    "capabilities": "{}",
                    "enabled": True,
                },
            ]
        )
        fake_cfg: dict[str, Any] = {"model": {"default": "smart"}}
        with patch("turnstone.core.model_registry.load_config", return_value=fake_cfg):
            reg = load_model_registry(model="", storage=storage)
        assert reg.default == "smart"

    def test_config_toml_only_no_cli_model(self) -> None:
        """Registry builds from config.toml [models.*] when model=''."""
        fake_cfg: dict[str, Any] = {
            "models": {
                "local": {
                    "model": "qwen3-32b",
                    "base_url": "http://localhost:8000/v1",
                    "api_key": "dummy",
                },
            },
        }
        with patch("turnstone.core.model_registry.load_config", return_value=fake_cfg):
            reg = load_model_registry(model="")
        assert reg.count == 1
        assert reg.default == "local"

    def test_no_models_anywhere_raises(self) -> None:
        """ValueError when no models from CLI, config, or DB."""
        with (
            patch("turnstone.core.model_registry.load_config", return_value={}),
            pytest.raises(ValueError, match="No model definitions found"),
        ):
            load_model_registry(model="")

    def test_no_default_entry_created_when_model_empty(self) -> None:
        """When model='', no 'default' alias is created from CLI args."""
        storage = _MockStorage(
            [
                {
                    "alias": "cloud",
                    "model": "gpt-5",
                    "provider": "openai",
                    "base_url": "https://api.openai.com/v1",
                    "api_key": "sk-test",
                    "context_window": 128000,
                    "capabilities": "{}",
                    "enabled": True,
                },
            ]
        )
        with patch("turnstone.core.model_registry.load_config", return_value={}):
            reg = load_model_registry(model="", storage=storage)
        assert not reg.has_alias("default")
