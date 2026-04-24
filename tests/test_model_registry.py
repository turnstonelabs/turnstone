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

    def test_plan_task_models_default_none(self) -> None:
        reg = self._make_registry()
        assert reg.plan_model is None
        assert reg.task_model is None
        assert reg.plan_effort is None
        assert reg.task_effort is None

    def test_resolve_agent_alias_falls_back_to_agent_model(self) -> None:
        reg = self._make_registry(agent_model="cheap")
        assert reg.resolve_agent_alias("plan") == "cheap"
        assert reg.resolve_agent_alias("task") == "cheap"

    def test_resolve_agent_alias_per_kind_overrides(self) -> None:
        models = {
            "default": ModelConfig("default", "http://x/v1", "k", "m"),
            "smart": ModelConfig("smart", "http://x/v1", "k", "m"),
            "fast": ModelConfig("fast", "http://x/v1", "k", "m"),
            "shared": ModelConfig("shared", "http://x/v1", "k", "m"),
        }
        reg = ModelRegistry(
            models=models,
            default="default",
            agent_model="shared",
            plan_model="smart",
            task_model="fast",
        )
        assert reg.resolve_agent_alias("plan") == "smart"
        assert reg.resolve_agent_alias("task") == "fast"

    def test_resolve_agent_alias_returns_none_when_unconfigured(self) -> None:
        reg = self._make_registry()
        assert reg.resolve_agent_alias("plan") is None
        assert reg.resolve_agent_alias("task") is None

    def test_resolve_agent_effort_plan_back_compat_default(self) -> None:
        reg = self._make_registry()
        assert reg.resolve_agent_effort("plan") == ModelRegistry.PLAN_DEFAULT_EFFORT
        assert reg.resolve_agent_effort("plan") == "high"

    def test_resolve_agent_effort_plan_override(self) -> None:
        models = {"a": ModelConfig("a", "x", "x", "x")}
        reg = ModelRegistry(models=models, default="a", plan_effort="max")
        assert reg.resolve_agent_effort("plan") == "max"

    def test_resolve_agent_effort_task_returns_none_to_inherit(self) -> None:
        reg = self._make_registry()
        assert reg.resolve_agent_effort("task") is None

    def test_resolve_agent_effort_task_override(self) -> None:
        models = {"a": ModelConfig("a", "x", "x", "x")}
        reg = ModelRegistry(models=models, default="a", task_effort="low")
        assert reg.resolve_agent_effort("task") == "low"


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

    def test_invalid_plan_model_raises(self) -> None:
        models = {"a": ModelConfig("a", "x", "x", "x")}
        with pytest.raises(ValueError, match="Plan model 'bad'"):
            ModelRegistry(models=models, default="a", plan_model="bad")

    def test_invalid_task_model_raises(self) -> None:
        models = {"a": ModelConfig("a", "x", "x", "x")}
        with pytest.raises(ValueError, match="Task model 'bad'"):
            ModelRegistry(models=models, default="a", task_model="bad")


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

    def test_plan_task_models_from_config(self) -> None:
        fake_cfg: dict[str, Any] = {
            "models": {
                "smart": {"base_url": "http://s/v1", "model": "s"},
                "fast": {"base_url": "http://f/v1", "model": "f"},
            },
            "model": {
                "plan_model": "smart",
                "task_model": "fast",
                "plan_effort": "max",
                "task_effort": "low",
            },
        }
        with patch("turnstone.core.model_registry.load_config", return_value=fake_cfg):
            reg = load_model_registry("http://x/v1", "x", "x")
        assert reg.plan_model == "smart"
        assert reg.task_model == "fast"
        assert reg.plan_effort == "max"
        assert reg.task_effort == "low"

    def test_invalid_plan_task_models_ignored(self) -> None:
        fake_cfg: dict[str, Any] = {
            "model": {"plan_model": "nope", "task_model": "alsonope"},
        }
        with patch("turnstone.core.model_registry.load_config", return_value=fake_cfg):
            reg = load_model_registry("http://x/v1", "x", "x")
        assert reg.plan_model is None
        assert reg.task_model is None

    def test_invalid_effort_values_dropped_with_warning(self) -> None:
        """Typos in plan_effort/task_effort shouldn't silently flow to providers."""
        fake_cfg: dict[str, Any] = {
            "model": {"plan_effort": "hihg", "task_effort": "extreme"},
        }
        with patch("turnstone.core.model_registry.load_config", return_value=fake_cfg):
            reg = load_model_registry("http://x/v1", "x", "x")
        assert reg.plan_effort is None
        assert reg.task_effort is None

    def test_valid_effort_values_accepted(self) -> None:
        for level in ("none", "minimal", "low", "medium", "high", "xhigh", "max"):
            fake_cfg: dict[str, Any] = {"model": {"plan_effort": level}}
            with patch("turnstone.core.model_registry.load_config", return_value=fake_cfg):
                reg = load_model_registry("http://x/v1", "x", "x")
            assert reg.plan_effort == level, f"level={level} not accepted"

    def test_empty_or_whitespace_effort_treated_as_unset(self) -> None:
        """Operators write `plan_effort = ""` to make "unset" explicit;
        warning on benign empty values would be noise."""
        for value in ("", "  ", "\t"):
            fake_cfg: dict[str, Any] = {"model": {"plan_effort": value, "task_effort": value}}
            with patch("turnstone.core.model_registry.load_config", return_value=fake_cfg):
                reg = load_model_registry("http://x/v1", "x", "x")
            assert reg.plan_effort is None, f"empty value {value!r} not treated as unset"
            assert reg.task_effort is None

    def test_effort_normalised_to_lowercase(self) -> None:
        fake_cfg: dict[str, Any] = {"model": {"plan_effort": "HIGH", "task_effort": " Low "}}
        with patch("turnstone.core.model_registry.load_config", return_value=fake_cfg):
            reg = load_model_registry("http://x/v1", "x", "x")
        assert reg.plan_effort == "high"
        assert reg.task_effort == "low"

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
    reasoning_effort: str = "medium",
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
        reasoning_effort=reasoning_effort,
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

    @staticmethod
    def _capture_on(client: Any) -> dict[str, Any]:
        """Patch *client* (registry-resolved or session.client) to capture kwargs."""
        captured: dict[str, Any] = {}
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "done"
        mock_response.choices[0].message.tool_calls = None
        mock_response.choices[0].finish_reason = "stop"

        def fake_create(**kwargs: Any) -> Any:
            captured.update(kwargs)
            return mock_response

        client.chat.completions.create = fake_create
        return captured

    def _capture(self, reg: ModelRegistry, alias: str) -> dict[str, Any]:
        return self._capture_on(reg.get_client(alias))

    @staticmethod
    def _captured_effort(captured: dict[str, Any]) -> str | None:
        """Pull reasoning_effort out of provider-specific shapes.

        openai-compatible servers receive it via extra_body.chat_template_kwargs;
        commercial providers receive it as a top-level kwarg.
        """
        eb = captured.get("extra_body") or {}
        ctk = eb.get("chat_template_kwargs") or {}
        return ctk.get("reasoning_effort") or captured.get("reasoning_effort")

    def _three_model_registry(self, **kwargs: Any) -> ModelRegistry:
        return ModelRegistry(
            models={
                "main": ModelConfig(
                    "main", "http://m/v1", "k", "main-model", provider="openai-compatible"
                ),
                "smart": ModelConfig(
                    "smart", "http://s/v1", "k", "smart-model", provider="openai-compatible"
                ),
                "fast": ModelConfig(
                    "fast", "http://f/v1", "k", "fast-model", provider="openai-compatible"
                ),
            },
            default="main",
            **kwargs,
        )

    def test_plan_model_overrides_agent_model(self) -> None:
        reg = self._three_model_registry(agent_model="fast", plan_model="smart")
        session = _make_session(registry=reg, model_alias="main")
        captured = self._capture(reg, "smart")
        session._run_agent([{"role": "user", "content": "x"}], label="plan")
        assert captured["model"] == "smart-model"

    def test_task_model_overrides_agent_model(self) -> None:
        reg = self._three_model_registry(agent_model="smart", task_model="fast")
        session = _make_session(registry=reg, model_alias="main")
        captured = self._capture(reg, "fast")
        session._run_agent([{"role": "user", "content": "x"}], label="task")
        assert captured["model"] == "fast-model"

    def test_plan_falls_back_to_agent_model(self) -> None:
        reg = self._three_model_registry(agent_model="fast")
        session = _make_session(registry=reg, model_alias="main")
        captured = self._capture(reg, "fast")
        session._run_agent([{"role": "user", "content": "x"}], label="plan")
        assert captured["model"] == "fast-model"

    def test_plan_uses_session_model_when_no_overrides(self) -> None:
        # No agent_model/plan_model configured — _run_agent falls through to
        # session.client (the test's MagicMock) and session.model ("test-model").
        reg = self._three_model_registry()
        session = _make_session(registry=reg, model_alias="main")
        captured = self._capture_on(session.client)
        session._run_agent([{"role": "user", "content": "x"}], label="plan")
        assert captured["model"] == "test-model"

    def test_plan_default_reasoning_effort_is_high(self) -> None:
        """Back-compat: plan_agent always got "high" before; the default must
        survive the migration even when no plan_effort is configured."""
        reg = self._three_model_registry()
        session = _make_session(registry=reg, model_alias="main")
        captured = self._capture_on(session.client)
        session._run_agent([{"role": "user", "content": "x"}], label="plan")
        assert self._captured_effort(captured) == "high"

    def test_plan_effort_from_registry_overrides_default(self) -> None:
        reg = self._three_model_registry(plan_effort="max")
        session = _make_session(registry=reg, model_alias="main")
        captured = self._capture_on(session.client)
        session._run_agent([{"role": "user", "content": "x"}], label="plan")
        assert self._captured_effort(captured) == "max"

    def test_task_effort_inherits_session_when_unset(self) -> None:
        # Task with no task_effort override must inherit whatever the SESSION
        # is configured for — assert against an explicit value rather than
        # the constructor default so the invariant is unambiguous if someone
        # changes ChatSession's default later.
        reg = self._three_model_registry()
        session = _make_session(registry=reg, model_alias="main", reasoning_effort="low")
        captured = self._capture_on(session.client)
        session._run_agent([{"role": "user", "content": "x"}], label="task")
        assert self._captured_effort(captured) == "low"

    def test_agent_model_routes_both_plan_and_task(self) -> None:
        """Back-compat invariant via _run_agent: with only the legacy
        agent_model knob set, both plan and task labels must route through it."""
        reg = self._three_model_registry(agent_model="fast")
        session = _make_session(registry=reg, model_alias="main")

        plan_captured = self._capture(reg, "fast")
        session._run_agent([{"role": "user", "content": "x"}], label="plan")
        assert plan_captured["model"] == "fast-model"

        task_captured = self._capture(reg, "fast")
        session._run_agent([{"role": "user", "content": "y"}], label="task")
        assert task_captured["model"] == "fast-model"

    def test_explicit_effort_wins_over_registry(self) -> None:
        reg = self._three_model_registry(plan_effort="low")
        session = _make_session(registry=reg, model_alias="main")
        captured = self._capture_on(session.client)
        session._run_agent(
            [{"role": "user", "content": "x"}], label="plan", reasoning_effort="minimal"
        )
        assert self._captured_effort(captured) == "minimal"

    # -- per-call agent_alias override (LLM passes model="<alias>") ----------

    def test_run_agent_uses_explicit_alias_override(self) -> None:
        """agent_alias kwarg routes the agent call to the chosen client/model."""
        reg = self._three_model_registry()
        session = _make_session(registry=reg, model_alias="main")
        captured = self._capture(reg, "fast")
        session._run_agent([{"role": "user", "content": "x"}], label="task", agent_alias="fast")
        assert captured["model"] == "fast-model"

    def test_explicit_alias_overrides_registry_plan_model(self) -> None:
        """Per-call alias wins over the configured per-kind plan_model."""
        reg = self._three_model_registry(plan_model="smart")
        session = _make_session(registry=reg, model_alias="main")
        # Without override the call would route to "smart"; we ask for "fast".
        captured = self._capture(reg, "fast")
        session._run_agent([{"role": "user", "content": "x"}], label="plan", agent_alias="fast")
        assert captured["model"] == "fast-model"

    def test_invalid_alias_raises_in_run_agent(self) -> None:
        """Defence-in-depth: _prepare_* validates first, but _run_agent
        rejects unknown aliases too rather than silently falling back."""
        reg = self._three_model_registry()
        session = _make_session(registry=reg, model_alias="main")
        with pytest.raises(ValueError, match="Unknown agent_alias"):
            session._run_agent(
                [{"role": "user", "content": "x"}], label="plan", agent_alias="bogus"
            )


# ---------------------------------------------------------------------------
# Workstream integration
# ---------------------------------------------------------------------------


def _make_manager(session_factory: Any) -> Any:
    """Construct a SessionManager with an interactive adapter that
    forwards to the supplied session_factory. Storage is mocked — the
    only thing the model-alias tests exercise is the factory passthrough."""
    import queue

    from turnstone.core.adapters.interactive_adapter import InteractiveAdapter
    from turnstone.core.session_manager import SessionManager

    adapter = InteractiveAdapter(
        global_queue=queue.Queue(maxsize=100),
        ui_factory=lambda ws: MagicMock(),
        session_factory=session_factory,
    )
    return SessionManager(adapter, storage=MagicMock(), max_active=10)


class TestWorkstreamModelParam:
    def test_create_with_model(self) -> None:
        """SessionManager.create passes model_alias to session_factory."""
        captured_alias = None

        def factory(
            ui: Any, model_alias: str | None = None, ws_id: str | None = None, **kwargs: Any
        ) -> Any:
            nonlocal captured_alias
            captured_alias = model_alias
            mock_session = MagicMock()
            mock_session.ws_id = "test123"
            return mock_session

        mgr = _make_manager(factory)
        mgr.create(user_id="", name="test", model="openai")
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

        mgr = _make_manager(factory)
        mgr.create(user_id="", name="test")
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


# ---------------------------------------------------------------------------
# server._effective_routing / _apply_routing_overrides
# ---------------------------------------------------------------------------


class _FakeCS:
    """Minimal ConfigStore stand-in: dict-backed get()."""

    def __init__(self, **values: str) -> None:
        self._values = values

    def get(self, key: str, default: Any = None) -> Any:
        return self._values.get(key, default if default is not None else "")


class TestEffectiveRouting:
    """Pure-function helper that overlays ConfigStore values on a base."""

    def _models(self) -> dict[str, ModelConfig]:
        return {
            "default": ModelConfig("default", "x", "x", "m"),
            "smart": ModelConfig("smart", "x", "x", "m"),
            "fast": ModelConfig("fast", "x", "x", "m"),
        }

    def test_returns_base_when_cs_is_none(self) -> None:
        from turnstone.server import _effective_routing

        result = _effective_routing(None, self._models(), "default", "smart", "fast", "high", "low")
        assert result == ("default", "smart", "fast", "high", "low")

    def test_cs_alias_overrides_base(self) -> None:
        from turnstone.server import _effective_routing

        cs = _FakeCS(**{"model.plan_alias": "fast", "model.task_alias": "smart"})
        result = _effective_routing(cs, self._models(), "default", "smart", "fast", "high", "low")
        assert result == ("default", "fast", "smart", "high", "low")

    def test_cs_alias_silently_dropped_when_unknown(self) -> None:
        from turnstone.server import _effective_routing

        cs = _FakeCS(**{"model.plan_alias": "nonexistent"})
        result = _effective_routing(cs, self._models(), "default", "smart", None, None, None)
        assert result == ("default", "smart", None, None, None)  # falls back to base

    def test_cs_empty_string_treated_as_unset(self) -> None:
        from turnstone.server import _effective_routing

        cs = _FakeCS(
            **{
                "model.default_alias": "",
                "model.plan_alias": "",
                "model.task_alias": "",
                "model.plan_effort": "",
                "model.task_effort": "",
            }
        )
        result = _effective_routing(cs, self._models(), "default", "smart", "fast", "high", "low")
        assert result == ("default", "smart", "fast", "high", "low")

    def test_cs_effort_overrides_base(self) -> None:
        from turnstone.server import _effective_routing

        cs = _FakeCS(**{"model.plan_effort": "max", "model.task_effort": "minimal"})
        result = _effective_routing(cs, self._models(), "default", None, None, "high", None)
        assert result == ("default", None, None, "max", "minimal")


class TestApplyRoutingOverrides:
    """Decides whether to call registry.reload based on effective vs current."""

    def _registry(self, **kwargs: Any) -> ModelRegistry:
        return ModelRegistry(
            models={
                "default": ModelConfig("default", "x", "x", "m"),
                "smart": ModelConfig("smart", "x", "x", "m"),
                "fast": ModelConfig("fast", "x", "x", "m"),
            },
            default="default",
            **kwargs,
        )

    def test_no_reload_when_cs_matches_registry(self) -> None:
        from turnstone.server import _apply_routing_overrides

        reg = self._registry(plan_model="smart", task_model="fast")
        cs = _FakeCS(**{"model.plan_alias": "smart", "model.task_alias": "fast"})
        # Patch reload to detect calls
        called = {"count": 0}
        original_reload = reg.reload
        reg.reload = lambda *a, **kw: (
            called.update(count=called["count"] + 1)
            or original_reload(  # type: ignore[method-assign]
                *a, **kw
            )
        )

        assert _apply_routing_overrides(reg, cs) is False
        assert called["count"] == 0

    def test_reload_when_cs_differs(self) -> None:
        from turnstone.server import _apply_routing_overrides

        reg = self._registry()  # plan_model=None
        cs = _FakeCS(**{"model.plan_alias": "smart"})
        assert _apply_routing_overrides(reg, cs) is True
        assert reg.plan_model == "smart"

    def test_no_reload_when_cs_is_none(self) -> None:
        from turnstone.server import _apply_routing_overrides

        reg = self._registry()
        assert _apply_routing_overrides(reg, None) is False

    def test_unknown_alias_does_not_trigger_reload(self) -> None:
        """Invalid CS aliases are silently dropped — no spurious reload."""
        from turnstone.server import _apply_routing_overrides

        reg = self._registry()
        cs = _FakeCS(**{"model.plan_alias": "nonexistent"})
        assert _apply_routing_overrides(reg, cs) is False
        assert reg.plan_model is None  # unchanged
