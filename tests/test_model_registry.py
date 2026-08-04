"""Tests for turnstone.core.model_registry — model registry, loading, session integration."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from tests._oidc_test_helpers import keyed_app_state
from tests._session_helpers import scripted_chat_client
from turnstone.core import model_registry as mr_module
from turnstone.core.model_registry import (
    KEY_GUARD_DEFERRED_TO_LIFESPAN,
    DynamicAuthKeyError,
    ModelConfig,
    ModelRegistry,
    _resolve_env_vars,
    detect_model,
    load_model_registry,
)
from turnstone.core.trajectory import Turn
from turnstone.core.workstream import WorkstreamKind

# ``reload`` requires ``app_state`` so its dynamic-auth key guard cannot be
# skipped. Mechanics tests below exercise reload behavior, not key policy,
# so they pass the shared keyed posture; the guard itself is tested in
# TestReloadKeyGuard.
_KEYED_STATE = keyed_app_state()

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

    def test_reasoning_flags_default(self) -> None:
        cfg = ModelConfig(alias="x", base_url="x", api_key="x", model="x")
        assert cfg.surface_persisted_reasoning is True
        assert cfg.replay_reasoning_to_model is False

    def test_reasoning_flags_set(self) -> None:
        cfg = ModelConfig(
            alias="x",
            base_url="x",
            api_key="x",
            model="x",
            surface_persisted_reasoning=False,
            replay_reasoning_to_model=True,
        )
        assert cfg.surface_persisted_reasoning is False
        assert cfg.replay_reasoning_to_model is True


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
        client, model, cfg, _ = reg.resolve()
        assert model == "qwen3-32b"
        assert cfg.alias == "default"

    def test_resolve_alias(self) -> None:
        reg = self._make_registry()
        client, model, cfg, generation = reg.resolve("openai")
        assert model == "gpt-4o"
        # The generation rides the same locked snapshot as the binding.
        assert generation == reg.generation
        assert cfg.context_window == 128000

    def test_resolve_none_uses_default(self) -> None:
        reg = self._make_registry()
        _, model1, _, _ = reg.resolve(None)
        _, model2, _, _ = reg.resolve()
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

    def test_client_construction_failure_is_value_error(self) -> None:
        # Environment failures inside SDK construction (e.g. httpx raising
        # FileNotFoundError for a CA bundle deleted by a venv rebuild) must
        # surface as ValueError so routes answer 503-with-message instead
        # of an opaque 500.
        reg = self._make_registry()
        with (
            patch(
                "turnstone.core.model_registry.create_client",
                side_effect=FileNotFoundError(2, "No such file", "/gone/cacert.pem"),
            ),
            pytest.raises(ValueError, match="'default'.*FileNotFoundError") as excinfo,
        ):
            reg.get_client("default")
        assert isinstance(excinfo.value.__cause__, FileNotFoundError)
        # The message is echoed in 503 bodies: exception TYPE only — the
        # raw exception text can embed filesystem paths and must stay in
        # the server log.
        assert "/gone/cacert.pem" not in str(excinfo.value)
        assert "No such file" not in str(excinfo.value)
        # Nothing half-constructed may be cached — a later call with a
        # repaired environment must construct for real.
        assert "default" not in reg._clients

    def test_client_construction_value_error_passes_through(self) -> None:
        # create_client's own misconfig ValueErrors already carry
        # remediation text and must not be double-wrapped.
        reg = self._make_registry()
        with (
            patch(
                "turnstone.core.model_registry.create_client",
                side_effect=ValueError("anthropic-compatible requires base_url"),
            ),
            pytest.raises(ValueError, match="^anthropic-compatible requires base_url$"),
        ):
            reg.get_client("default")

    def test_provider_leg_failure_in_resolve_binding_is_construction_error(self) -> None:
        """Re-typed so the bind path cannot read it as the alias vanishing."""
        from turnstone.core.model_registry import ModelClientConstructionError

        reg = ModelRegistry(
            models={
                "gw": ModelConfig(
                    "gw",
                    "http://gw.example/v1",
                    "k",
                    "gw-model",
                    provider="openai-compatible",
                    server_compat={"api_surface": "bogus"},
                )
            },
            default="gw",
        )
        with pytest.raises(ModelClientConstructionError, match="bogus"):
            reg.resolve_binding("gw")

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

    def test_task_model_default_none(self) -> None:
        reg = self._make_registry()
        assert reg.task_model is None
        assert reg.task_effort is None

    def test_resolve_agent_alias_falls_back_to_agent_model(self) -> None:
        reg = self._make_registry(agent_model="cheap")
        assert reg.resolve_agent_alias("plan") == "cheap"
        assert reg.resolve_agent_alias("task") == "cheap"

    def test_resolve_agent_alias_per_kind_overrides(self) -> None:
        models = {
            "default": ModelConfig("default", "http://x/v1", "k", "m"),
            "fast": ModelConfig("fast", "http://x/v1", "k", "m"),
            "shared": ModelConfig("shared", "http://x/v1", "k", "m"),
        }
        reg = ModelRegistry(
            models=models,
            default="default",
            agent_model="shared",
            task_model="fast",
        )
        assert reg.resolve_agent_alias("task") == "fast"

    def test_resolve_agent_alias_returns_none_when_unconfigured(self) -> None:
        reg = self._make_registry()
        assert reg.resolve_agent_alias("plan") is None
        assert reg.resolve_agent_alias("task") is None

    def test_resolve_agent_effort_task_returns_none_to_inherit(self) -> None:
        reg = self._make_registry()
        assert reg.resolve_agent_effort("task") is None

    def test_resolve_agent_effort_task_override(self) -> None:
        models = {"a": ModelConfig("a", "x", "x", "x")}
        reg = ModelRegistry(models=models, default="a", task_effort="low")
        assert reg.resolve_agent_effort("task") == "low"


class TestModelRegistryValidation:
    def test_empty_models_with_default_raises(self) -> None:
        # A stray default that can't resolve is a bug, not a valid state.
        with pytest.raises(ValueError, match="not found in empty registry"):
            ModelRegistry(models={}, default="x")

    def test_empty_models_allowed_when_default_unset(self) -> None:
        # Degraded "no models configured yet" state — a server boots into this
        # and models are added live via the admin panel.
        reg = ModelRegistry(models={}, default="")
        assert reg.count == 0
        assert reg.list_aliases() == []

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
        _, model, cfg, _ = reg.resolve()
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
        # The CLI ``"default"`` shim is suppressed once ``[models.*]``
        # populates configs — only the explicit alias survives.
        assert reg.count == 1
        assert reg.has_alias("openai")
        assert not reg.has_alias("default")
        assert reg.default == "openai"
        _, model, _, _ = reg.resolve()
        assert model == "gpt-4o"

    def test_config_context_window_zero_inherits_detected(self) -> None:
        """``context_window = 0`` in a [models.*] entry is the auto-detect
        sentinel: it must inherit the CLI/detected window, not stay a literal 0
        (which would zero every downstream budget — judge lowering, session
        compaction).  The DB loader normalizes 0->inherit; the config path must
        match it (``.get(k, 0) or context_window``, not ``.get(k, default)``)."""
        fake_cfg: dict[str, Any] = {
            "models": {
                "local": {
                    "base_url": "http://localhost:8000/v1",
                    "model": "local-model",
                    "context_window": 0,  # auto-detect
                },
            },
            "model": {"default": "local"},
        }
        with patch("turnstone.core.model_registry.load_config", return_value=fake_cfg):
            reg = load_model_registry(
                base_url="http://localhost:8000/v1",
                api_key="dummy",
                model="local-model",
                context_window=40_000,  # the CLI-detected window
            )
        _, _, cfg, _ = reg.resolve("local")
        assert cfg.context_window == 40_000  # inherited, not the literal 0

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

    def test_task_model_from_config(self) -> None:
        fake_cfg: dict[str, Any] = {
            "models": {
                "fast": {"base_url": "http://f/v1", "model": "f"},
            },
            "model": {
                "task_model": "fast",
                "task_effort": "low",
            },
        }
        with patch("turnstone.core.model_registry.load_config", return_value=fake_cfg):
            reg = load_model_registry("http://x/v1", "x", "x")
        assert reg.task_model == "fast"
        assert reg.task_effort == "low"

    def test_invalid_task_model_ignored(self) -> None:
        fake_cfg: dict[str, Any] = {
            "model": {"task_model": "alsonope"},
        }
        with patch("turnstone.core.model_registry.load_config", return_value=fake_cfg):
            reg = load_model_registry("http://x/v1", "x", "x")
        assert reg.task_model is None

    def test_invalid_effort_values_dropped_with_warning(self) -> None:
        """Typos in task_effort shouldn't silently flow to providers."""
        fake_cfg: dict[str, Any] = {
            "model": {"task_effort": "extreme"},
        }
        with patch("turnstone.core.model_registry.load_config", return_value=fake_cfg):
            reg = load_model_registry("http://x/v1", "x", "x")
        assert reg.task_effort is None

    def test_valid_effort_values_accepted(self) -> None:
        for level in ("none", "minimal", "low", "medium", "high", "xhigh", "max"):
            fake_cfg: dict[str, Any] = {"model": {"task_effort": level}}
            with patch("turnstone.core.model_registry.load_config", return_value=fake_cfg):
                reg = load_model_registry("http://x/v1", "x", "x")
            assert reg.task_effort == level, f"level={level} not accepted"

    def test_empty_or_whitespace_effort_treated_as_unset(self) -> None:
        """Operators write `task_effort = ""` to make "unset" explicit;
        warning on benign empty values would be noise."""
        for value in ("", "  ", "\t"):
            fake_cfg: dict[str, Any] = {"model": {"task_effort": value}}
            with patch("turnstone.core.model_registry.load_config", return_value=fake_cfg):
                reg = load_model_registry("http://x/v1", "x", "x")
            assert reg.task_effort is None, f"empty value {value!r} not treated as unset"

    def test_effort_normalised_to_lowercase(self) -> None:
        fake_cfg: dict[str, Any] = {"model": {"task_effort": " Low "}}
        with patch("turnstone.core.model_registry.load_config", return_value=fake_cfg):
            reg = load_model_registry("http://x/v1", "x", "x")
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

    def test_rerank_calibration_caps_survive_db_load(self) -> None:
        """Phase 3: the three reranker-calibration capability keys round-trip
        through the DB load and stay in ``cfg.capabilities`` (the raw dict the
        BM25 floor reads), independent of any dataclass field filtering."""
        storage = _MockStorage(
            [
                {
                    "alias": "reranker",
                    "model": "bge-reranker",
                    "provider": "openai-compatible",
                    "base_url": "http://localhost:9999/rerank",
                    "api_key": "sk-db",
                    "context_window": 0,
                    "capabilities": json.dumps(
                        {
                            "supports_rerank": True,
                            "rerank_threshold": 0.33,
                            "rerank_scale": "probability (0-1)",
                            "rerank_separated": True,
                        }
                    ),
                    "enabled": True,
                }
            ]
        )
        with patch("turnstone.core.model_registry.load_config", return_value={}):
            reg = load_model_registry("http://x/v1", "x", "x", storage=storage)
        caps = reg.get_config("reranker").capabilities
        assert caps["rerank_threshold"] == 0.33
        assert caps["rerank_scale"] == "probability (0-1)"
        assert caps["rerank_separated"] is True

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
        """DB models coexist alongside config.toml models.

        The CLI ``"default"`` shim is suppressed when DB / config models
        already populate the registry — see
        ``test_cli_default_shim_skipped_when_db_models_present``.
        """
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
        assert not reg.has_alias("default")
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
        # The CLI default shim is suppressed when the DB row populates
        # configs, so only the DB-sourced alias exists here.
        with patch("turnstone.core.model_registry.load_config", return_value={}):
            reg = load_model_registry("http://x/v1", "x", "x", storage=storage)
        assert reg.get_config("from-db").source == "db"
        assert not reg.has_alias("default")

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

    def test_db_reasoning_flags_loaded(self) -> None:
        """Per-model reasoning flags from DB are carried in ModelConfig."""
        storage = _MockStorage(
            [
                {
                    "alias": "anth-thinking",
                    "model": "claude-opus-4-7",
                    "provider": "anthropic",
                    "base_url": "",
                    "api_key": "sk-anth",
                    "context_window": 200000,
                    "capabilities": "{}",
                    "enabled": True,
                    "surface_persisted_reasoning": False,
                    "replay_reasoning_to_model": True,
                }
            ]
        )
        with patch("turnstone.core.model_registry.load_config", return_value={}):
            reg = load_model_registry("http://x/v1", "x", "x", storage=storage)
        cfg = reg.get_config("anth-thinking")
        assert cfg.surface_persisted_reasoning is False
        assert cfg.replay_reasoning_to_model is True

    def test_db_reasoning_flags_default_when_absent(self) -> None:
        """Pre-052 rows without the columns degrade to dataclass defaults."""
        storage = _MockStorage(
            [
                {
                    "alias": "legacy-row",
                    "model": "gpt-5",
                    "provider": "openai",
                    "base_url": "",
                    "api_key": "",
                    "context_window": 32768,
                    "capabilities": "{}",
                    "enabled": True,
                    # surface_persisted_reasoning + replay_reasoning_to_model intentionally absent
                }
            ]
        )
        with patch("turnstone.core.model_registry.load_config", return_value={}):
            reg = load_model_registry("http://x/v1", "x", "x", storage=storage)
        cfg = reg.get_config("legacy-row")
        assert cfg.surface_persisted_reasoning is True
        assert cfg.replay_reasoning_to_model is False

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

    def test_db_model_empty_api_key_falls_back_to_cli(self) -> None:
        """DB model with empty api_key inherits the CLI/api_key fallback."""
        storage = _MockStorage(
            [
                {
                    "alias": "cloud",
                    "model": "gpt-5",
                    "provider": "openai",
                    "base_url": "https://api.openai.com/v1",
                    "api_key": "",
                    "context_window": 32768,
                    "capabilities": "{}",
                    "enabled": True,
                }
            ]
        )
        with patch("turnstone.core.model_registry.load_config", return_value={}):
            reg = load_model_registry("http://x/v1", "cli-fallback-key", "x", storage=storage)
        cfg = reg.get_config("cloud")
        assert cfg.api_key == "cli-fallback-key"

    def test_db_model_explicit_api_key_overrides_cli(self) -> None:
        """DB model with its own api_key uses it, not the CLI fallback."""
        storage = _MockStorage(
            [
                {
                    "alias": "cloud",
                    "model": "gpt-5",
                    "provider": "openai",
                    "base_url": "https://api.openai.com/v1",
                    "api_key": "sk-db-specific",
                    "context_window": 32768,
                    "capabilities": "{}",
                    "enabled": True,
                }
            ]
        )
        with patch("turnstone.core.model_registry.load_config", return_value={}):
            reg = load_model_registry("http://x/v1", "cli-fallback-key", "x", storage=storage)
        cfg = reg.get_config("cloud")
        assert cfg.api_key == "sk-db-specific"


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
        reg.reload(models_b, "b", app_state=_KEYED_STATE)
        assert not reg.has_alias("a")
        assert reg.has_alias("b")
        assert reg.default == "b"

    def test_reload_keeps_clients_when_connection_target_unchanged(self) -> None:
        """Selective teardown: a model edit that leaves base_url / api_key /
        provider intact (e.g. admin tweaks the underlying ``model`` name or
        ``temperature``) keeps the cached HTTP client warm — no need to
        re-establish TLS+pool when the endpoint is the same."""
        models = {"a": ModelConfig("a", "http://x/v1", "key", "m1", provider="openai")}
        reg = ModelRegistry(models=models, default="a")
        reg.get_client("a")
        client_before = reg._clients["a"]
        provider_before = reg.get_provider("a")

        # Same endpoint (base_url, api_key, provider), only ``model`` changed.
        new_models = {"a": ModelConfig("a", "http://x/v1", "key", "m2", provider="openai")}
        reg.reload(new_models, "a", app_state=_KEYED_STATE)

        assert "a" in reg._clients
        assert reg._clients["a"] is client_before
        assert "a" in reg._providers
        assert reg._providers["a"] is provider_before

    def test_reload_drops_client_when_base_url_changes(self) -> None:
        """A ``base_url`` change drops the cached client (different
        endpoint = new connection) but keeps the cached provider —
        ``LLMProvider`` is keyed only on the provider string, which
        didn't change."""
        models = {"a": ModelConfig("a", "http://x/v1", "key", "m", provider="openai")}
        reg = ModelRegistry(models=models, default="a")
        reg.get_client("a")
        provider_before = reg.get_provider("a")

        new_models = {"a": ModelConfig("a", "http://y/v1", "key", "m", provider="openai")}
        reg.reload(new_models, "a", app_state=_KEYED_STATE)

        assert "a" not in reg._clients
        assert "a" in reg._providers
        assert reg._providers["a"] is provider_before

    def test_reload_drops_client_when_auth_mode_changes(self) -> None:
        """Client construction chooses a placeholder from auth_mode, so a mode
        change must rebuild even when URL and stored api_key are unchanged."""
        models = {
            "a": ModelConfig(
                "a",
                "http://x/v1",
                "",
                "m",
                provider="openai",
                auth_mode="static",
            )
        }
        reg = ModelRegistry(models=models, default="a")
        reg._clients["a"] = MagicMock()

        new_models = {
            "a": ModelConfig(
                "a",
                "http://x/v1",
                "",
                "m",
                provider="openai",
                auth_mode="entra_app",
                obo_audience="api://gateway",
            )
        }
        reg.reload(new_models, "a", app_state=_KEYED_STATE)

        assert "a" not in reg._clients

    def test_reload_drops_provider_when_provider_string_changes(self) -> None:
        """A provider-type swap (e.g. openai → anthropic) drops both the
        client AND the provider so the next resolve picks up the right
        ``LLMProvider`` implementation against the new SDK."""
        models = {"a": ModelConfig("a", "http://x/v1", "key", "m", provider="openai")}
        reg = ModelRegistry(models=models, default="a")
        reg.get_client("a")
        reg.get_provider("a")

        new_models = {"a": ModelConfig("a", "http://x/v1", "key", "m", provider="anthropic")}
        reg.reload(new_models, "a", app_state=_KEYED_STATE)

        assert "a" not in reg._clients
        assert "a" not in reg._providers

    def test_reload_drops_clients_for_removed_aliases(self) -> None:
        """Aliases removed from the registry must release their cached
        clients — otherwise a deleted endpoint's connection pool would
        outlive the alias indefinitely."""
        models = {
            "a": ModelConfig("a", "http://x/v1", "key", "m"),
            "b": ModelConfig("b", "http://y/v1", "key", "m"),
        }
        reg = ModelRegistry(models=models, default="a")
        reg.get_client("a")
        reg.get_client("b")

        # Drop "b" entirely.
        new_models = {"a": ModelConfig("a", "http://x/v1", "key", "m")}
        reg.reload(new_models, "a", app_state=_KEYED_STATE)

        assert "a" in reg._clients  # unchanged endpoint, kept warm
        assert "b" not in reg._clients

    def test_reload_validates_default(self) -> None:
        models_a = {"a": ModelConfig("a", "x", "x", "m")}
        reg = ModelRegistry(models=models_a, default="a")
        with pytest.raises(ValueError, match="Default model"):
            reg.reload(models_a, "nonexistent", app_state=_KEYED_STATE)
        # Registry should be unchanged after failed reload
        assert reg.has_alias("a")
        assert reg.default == "a"

    def test_reload_to_empty_with_default_raises(self) -> None:
        models_a = {"a": ModelConfig("a", "x", "x", "m")}
        reg = ModelRegistry(models=models_a, default="a")
        with pytest.raises(ValueError, match="not found in empty registry"):
            reg.reload({}, "a", app_state=_KEYED_STATE)

    def test_reload_to_empty_degraded(self) -> None:
        # Reloading down to zero models (default unset) is allowed: the
        # registry drops to the degraded state and lookups raise until models
        # return (e.g. an admin removes every model definition at runtime).
        models_a = {"a": ModelConfig("a", "x", "x", "m")}
        reg = ModelRegistry(models=models_a, default="a")
        reg.reload({}, "", app_state=_KEYED_STATE)
        assert reg.count == 0


class TestReloadKeyGuard:
    """The dynamic-auth key guard pinned at ``reload``, the one swap
    chokepoint every call site routes through."""

    @staticmethod
    def _static_models() -> dict[str, ModelConfig]:
        return {"a": ModelConfig("a", "http://x/v1", "key", "m")}

    @staticmethod
    def _dynamic_models() -> dict[str, ModelConfig]:
        return {
            "a": ModelConfig("a", "http://x/v1", "key", "m"),
            "gw": ModelConfig(
                "gw",
                "http://gw/v1",
                "",
                "m",
                auth_mode="entra_obo",
                obo_audience="api://gateway",
            ),
        }

    def test_reload_refuses_dynamic_auth_without_key(self) -> None:
        reg = ModelRegistry(models=self._static_models(), default="a")
        keyless = SimpleNamespace(mcp_token_store=None)
        with pytest.raises(DynamicAuthKeyError, match="dynamic model auth"):
            reg.reload(self._dynamic_models(), "a", app_state=keyless)
        # Refusal must not mutate: the old registry keeps serving.
        assert reg.list_aliases() == ["a"]
        assert not reg.has_dynamic_auth()

    def test_reload_allows_dynamic_auth_with_key(self) -> None:
        reg = ModelRegistry(models=self._static_models(), default="a")
        reg.reload(self._dynamic_models(), "a", app_state=_KEYED_STATE)
        assert reg.has_dynamic_auth()

    def test_reload_all_static_permitted_keyless(self) -> None:
        # The guard fires on dynamic auth being present, not on a missing key.
        reg = ModelRegistry(models=self._static_models(), default="a")
        keyless = SimpleNamespace(mcp_token_store=None)
        reg.reload({"b": ModelConfig("b", "http://y/v1", "key", "m")}, "b", app_state=keyless)
        assert reg.has_alias("b")

    def test_reload_boot_sentinel_defers_key_guard(self) -> None:
        """Boot defers to ``initialize_mcp_crypto_state``, not a bypass."""
        reg = ModelRegistry(models=self._static_models(), default="a")
        reg.reload(self._dynamic_models(), "a", app_state=KEY_GUARD_DEFERRED_TO_LIFESPAN)
        assert reg.has_dynamic_auth()


# ---------------------------------------------------------------------------
# Session integration
# ---------------------------------------------------------------------------


class _FakeUI:
    """Minimal SessionUI stub for testing."""

    def __init__(self) -> None:
        self.infos: list[str] = []
        self.errors: list[str] = []

    def on_turn_start(self) -> None: ...
    def on_turn_committed(self) -> None: ...
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
    def on_info(self, message: str) -> None:
        self.infos.append(message)

    def on_error(self, message: str) -> None:
        self.errors.append(message)

    def on_state_change(self, state: str) -> None: ...
    def on_rename(self, name: str) -> None: ...
    def on_output_warning(self, call_id, assessment): ...
    def record_output_assessment(
        self,
        call_id,
        assessment,
        *,
        tier="heuristic",
        reasoning="",
        judge_model="",
        latency_ms=0,
        confidence=0.0,
    ): ...


def _make_session(
    registry: ModelRegistry | None = None,
    model_alias: str | None = None,
    reasoning_effort: str = "medium",
    client: Any | None = None,
    kind: WorkstreamKind = WorkstreamKind.INTERACTIVE,
    user_id: str = "",
) -> Any:
    """Create a ChatSession with a mock client and optional registry.

    Pass ``client=registry.get_client(alias)`` to mirror the factories,
    which resolve the client from the registry before construction.
    """
    from turnstone.core.session import ChatSession

    return ChatSession(
        client=client if client is not None else MagicMock(),
        model="test-model",
        ui=_FakeUI(),
        instructions=None,
        temperature=0.5,
        max_tokens=4096,
        tool_timeout=30,
        registry=registry,
        model_alias=model_alias,
        reasoning_effort=reasoning_effort,
        kind=kind,
        user_id=user_id,
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

    def test_model_switch_construction_failure_surfaces_real_cause(self, monkeypatch: Any) -> None:
        """An alias that exists but cannot construct is not "unknown", and
        the binding stays untouched."""

        reg = ModelRegistry(
            models={
                "default": ModelConfig("default", "x", "x", "default-model"),
                "gw": ModelConfig("gw", "http://gw.example/v1", "k", "gw-model"),
            },
            default="default",
        )
        session = _make_session(registry=reg, model_alias="default")
        old_client = session.client

        def _boom(provider: str, **kwargs: Any) -> Any:
            raise FileNotFoundError("/etc/ssl/missing-ca.pem")

        monkeypatch.setattr(mr_module, "create_client", _boom)
        session.handle_command("/model gw")

        info = session.ui.infos[-1]
        assert "Unknown model alias" not in info
        assert "failed to construct" in info
        assert "details in server log" in info
        assert session.client is old_client
        assert session.model == "test-model"
        assert session.model_alias == "default"

    def test_model_switch_provider_leg_failure_surfaces_real_cause(self) -> None:
        """The provider leg (api_surface selection) is a construction failure
        too, not an unknown alias."""
        reg = ModelRegistry(
            models={
                "default": ModelConfig("default", "x", "x", "default-model"),
                "gw": ModelConfig(
                    "gw",
                    "http://gw.example/v1",
                    "k",
                    "gw-model",
                    provider="openai-compatible",
                    server_compat={"api_surface": "bogus"},
                ),
            },
            default="default",
        )
        session = _make_session(registry=reg, model_alias="default")
        old_client = session.client

        session.handle_command("/model gw")

        info = session.ui.infos[-1]
        assert "Unknown model alias" not in info
        assert "bogus" in info  # the real api_surface cause, verbatim
        assert session.client is old_client
        assert session.model == "test-model"
        assert session.model_alias == "default"

    def test_model_switch_resets_judges(self) -> None:
        """The switch drops the judges, which cache the previous binding."""
        reg = ModelRegistry(
            models={
                "default": ModelConfig("default", "x", "x", "default-model"),
                "alt": ModelConfig("alt", "y", "y", "alt-model"),
            },
            default="default",
        )
        session = _make_session(registry=reg, model_alias="default")
        session._judge = object()
        session._output_guard_judge = object()
        old_limiter = session._output_guard_judge_rl

        session.handle_command("/model alt")

        assert "Switched to" in session.ui.infos[-1]
        assert session._judge is None
        assert session._output_guard_judge is None
        # The limiter budget is tied to the judge model — a swap renews it.
        assert session._output_guard_judge_rl is not old_limiter

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

    def test_model_switch_storeless_keeps_explicit_knobs(self) -> None:
        """On a STORE-LESS session (the CLI), the current knobs are the
        user's explicit flags — the only authority that exists — so a
        switch to an override-free alias keeps them (mirroring the
        max_tokens fallback).  With a ConfigStore the shared resolvers
        re-resolve for the new alias instead (unset → None → wire
        omission), so per-model overrides don't leak between aliases."""
        reg = ModelRegistry(
            models={
                "hot": ModelConfig("hot", "x", "x", "hot-model", temperature=1.5),
                "plain": ModelConfig("plain", "y", "y", "plain-model"),
            },
            default="hot",
        )
        session = _make_session(registry=reg, model_alias="hot")
        session.temperature = 0.9  # user's explicit --temperature flag
        session.reasoning_effort = "high"  # user's explicit /reason choice
        session.handle_command("/model plain")
        assert session.temperature == 0.9
        assert session.reasoning_effort == "high"
        # A per-model override on the TARGET alias still wins over the
        # carried knob.
        session.handle_command("/model hot")
        assert session.temperature == 1.5

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


class TestSessionRegistryGenerationPropagation:
    """An in-place ``reload()`` must reach live sessions even when the alias
    keeps its backend model id: sessions cache the generation their client
    came from and re-resolve on any mismatch.
    """

    def test_reload_with_changed_base_url_same_model_id_rebinds_client(self) -> None:
        reg = ModelRegistry(
            models={"gw": ModelConfig("gw", "http://a.example/v1", "k", "test-model")},
            default="gw",
        )
        session = _make_session(registry=reg, model_alias="gw")
        # Bind the registry's real client, as the factories do.
        session.client = reg.get_client("gw")
        old_client = session.client

        # Same generation + same model id: the refresh must be a no-op.
        session._refresh_model_from_registry()
        assert session.client is old_client

        # In-place swap: NEW base_url, SAME backend model id — the registry
        # closes and drops the cached client.
        reg.reload(
            {"gw": ModelConfig("gw", "http://b.example/v1", "k", "test-model")},
            "gw",
            app_state=_KEYED_STATE,
        )
        session._refresh_model_from_registry()

        assert session.client is not old_client
        assert session.client is reg.get_client("gw")
        assert str(session.client.base_url).startswith("http://b.example")
        assert session._registry_generation == reg.generation

    def test_construction_window_reload_detected_on_first_send(self) -> None:
        """A reload landing between the factory's resolve and construction is
        caught by the first send, because the generation is passed in beside
        the client rather than sampled inside ``__init__``.
        """
        from turnstone.core.session import ChatSession

        reg = ModelRegistry(
            models={"gw": ModelConfig("gw", "http://a.example/v1", "k", "test-model")},
            default="gw",
        )
        # Factory sequence: the resolve returns the paired generation.
        factory_client, _model, _cfg, pre_resolve_generation = reg.resolve("gw")
        # The reload lands in the construction window: same backend model
        # id, moved base_url.
        reg.reload(
            {"gw": ModelConfig("gw", "http://b.example/v1", "k", "test-model")},
            "gw",
            app_state=_KEYED_STATE,
        )
        session = ChatSession(
            client=factory_client,
            model="test-model",
            ui=_FakeUI(),
            instructions=None,
            temperature=0.5,
            max_tokens=4096,
            tool_timeout=30,
            registry=reg,
            model_alias="gw",
            registry_generation=pre_resolve_generation,
        )

        session._refresh_model_from_registry()

        assert session.client is not factory_client
        assert session.client is reg.get_client("gw")
        assert session._registry_generation == reg.generation

    def test_alias_deletion_race_keeps_old_binding_without_raise(self) -> None:
        """A deletion landing mid-rebind must neither raise out of send nor
        half-swap; the next refresh self-heals."""
        reg = ModelRegistry(
            models={"gw": ModelConfig("gw", "http://a.example/v1", "k", "test-model")},
            default="gw",
        )
        session = _make_session(registry=reg, model_alias="gw")
        session.client = reg.get_client("gw")
        old_client = session.client
        old_provider = session._provider
        old_generation = session._registry_generation

        reg.reload(
            {"gw": ModelConfig("gw", "http://b.example/v1", "k", "test-model")},
            "gw",
            app_state=_KEYED_STATE,
        )
        # The deletion race: the alias vanishes before the bind's locked
        # snapshot, so the resolve raises and nothing is assigned.
        with patch.object(
            reg, "resolve_binding", side_effect=ValueError("Unknown model alias: gw")
        ):
            session._refresh_model_from_registry()  # must not raise

        assert session.client is old_client
        assert session._provider is old_provider
        assert session._registry_generation == old_generation
        assert session.model == "test-model"

        # Unpatched, the next send's refresh completes the rebind.
        session._refresh_model_from_registry()
        assert session.client is reg.get_client("gw")
        assert session._registry_generation == reg.generation

    def test_bind_reads_client_and_provider_under_one_lock_acquisition(self) -> None:
        """Client, config and provider come from one lock acquisition, so a
        concurrent ``reload()`` cannot tear the committed binding."""
        reg = ModelRegistry(
            models={"gw": ModelConfig("gw", "http://a.example/v1", "k", "test-model")},
            default="gw",
        )
        session = _make_session(registry=reg, model_alias="gw")

        class CountingLock:
            def __init__(self, inner: Any) -> None:
                self._inner = inner
                self.acquisitions = 0

            def __enter__(self) -> Any:
                self.acquisitions += 1
                return self._inner.__enter__()

            def __exit__(self, *exc: Any) -> Any:
                return self._inner.__exit__(*exc)

        counting = CountingLock(reg._client_lock)
        reg._client_lock = counting  # type: ignore[assignment]

        cfg = session._bind_model_from_registry("gw")

        assert cfg is not None
        assert session.client is reg._clients["gw"]
        assert counting.acquisitions == 1

    def test_model_switch_stamps_current_generation(self) -> None:
        """Switching after a reload stamps the current generation, so the
        next send's compare is a no-op instead of a spurious rebind."""
        reg = ModelRegistry(
            models={
                "a": ModelConfig("a", "http://a/v1", "k", "m-a"),
                "b": ModelConfig("b", "http://b/v1", "k", "m-b"),
            },
            default="a",
        )
        session = _make_session(registry=reg, model_alias="a")
        reg.reload(
            {
                "a": ModelConfig("a", "http://a/v1", "k", "m-a"),
                "b": ModelConfig("b", "http://b/v1", "k", "m-b"),
            },
            "a",
            app_state=_KEYED_STATE,
        )

        session.handle_command("/model b")

        assert session.model == "m-b"
        assert session.client is reg.get_client("b")
        assert session._registry_generation == reg.generation

    def test_unrelated_alias_reload_keeps_judges_and_limiter_budget(self) -> None:
        """A rebind resolving to the identical binding stamps the generation
        and leaves the judges and the output-guard limiter untouched."""
        reg = ModelRegistry(
            models={
                "gw": ModelConfig("gw", "http://a.example/v1", "k", "test-model"),
                "other": ModelConfig("other", "http://o.example/v1", "k", "o-model"),
            },
            default="gw",
        )
        session = _make_session(registry=reg, model_alias="gw")
        session._bind_model_from_registry("gw")  # establish the baseline binding
        guard = MagicMock()
        judge = MagicMock()
        session._output_guard_judge = guard
        session._judge = judge
        limiter = session._output_guard_judge_rl

        # The session's own row is byte-identical; only the unrelated alias
        # moves, so the selective teardown keeps gw's pooled client.
        reg.reload(
            {
                "gw": ModelConfig("gw", "http://a.example/v1", "k", "test-model"),
                "other": ModelConfig("other", "http://moved.example/v1", "k", "o-model"),
            },
            "gw",
            app_state=_KEYED_STATE,
        )
        session._refresh_model_from_registry()

        assert session._registry_generation == reg.generation  # stamped
        assert session._output_guard_judge is guard  # no reset
        assert session._output_guard_judge_rl is limiter  # no refill
        assert session._judge is judge

    def test_first_unrelated_reload_after_construction_keeps_limiter(self) -> None:
        """Construction seeds ``_bound_model_cfg``, so even the first
        generation-only rebind compares as unchanged."""
        reg = ModelRegistry(
            models={
                "gw": ModelConfig("gw", "http://a.example/v1", "k", "test-model"),
                "other": ModelConfig("other", "http://o.example/v1", "k", "o-model"),
            },
            default="gw",
        )
        # Mirror the factories: the client is resolved from the registry
        # before construction, so client identity holds across the rebind.
        session = _make_session(registry=reg, model_alias="gw", client=reg.get_client("gw"))
        guard = MagicMock()
        judge = MagicMock()
        session._output_guard_judge = guard
        session._judge = judge
        limiter = session._output_guard_judge_rl

        # No explicit bind: the first refresh below is the session's first
        # rebind since construction.
        reg.reload(
            {
                "gw": ModelConfig("gw", "http://a.example/v1", "k", "test-model"),
                "other": ModelConfig("other", "http://moved.example/v1", "k", "o-model"),
            },
            "gw",
            app_state=_KEYED_STATE,
        )
        session._refresh_model_from_registry()

        assert session._registry_generation == reg.generation  # stamped
        assert session._output_guard_judge is guard  # no reset
        assert session._output_guard_judge_rl is limiter  # no refill on the FIRST edit
        assert session._judge is judge

    def test_noop_rebind_is_silent_and_keeps_capabilities_cache(self, caplog: Any) -> None:
        """A generation-only rebind stamps silently and keeps the
        capabilities memo warm; a real swap still logs."""
        import logging

        reg = ModelRegistry(
            models={
                "gw": ModelConfig("gw", "http://a.example/v1", "k", "test-model"),
                "other": ModelConfig("other", "http://o.example/v1", "k", "o-model"),
            },
            default="gw",
        )
        session = _make_session(registry=reg, model_alias="gw", client=reg.get_client("gw"))
        caps_sentinel = object()
        session._cached_capabilities = caps_sentinel

        reg.reload(
            {
                "gw": ModelConfig("gw", "http://a.example/v1", "k", "test-model"),
                "other": ModelConfig("other", "http://moved.example/v1", "k", "o-model"),
            },
            "gw",
            app_state=_KEYED_STATE,
        )
        with caplog.at_level(logging.INFO):
            session._refresh_model_from_registry()

        assert session._registry_generation == reg.generation  # stamped anyway
        assert not any("model_updated" in r.getMessage() for r in caplog.records)
        assert session._cached_capabilities is caps_sentinel  # memo kept

        # Contrast: a swap that moves THIS alias's connection target logs.
        reg.reload(
            {
                "gw": ModelConfig("gw", "http://b.example/v1", "k", "test-model"),
                "other": ModelConfig("other", "http://moved.example/v1", "k", "o-model"),
            },
            "gw",
            app_state=_KEYED_STATE,
        )
        with caplog.at_level(logging.INFO):
            session._refresh_model_from_registry()
        assert any("model_updated" in r.getMessage() for r in caplog.records)
        assert session._cached_capabilities is None  # real change drops the memo

    def test_reload_changing_sessions_alias_still_resets_judges(self) -> None:
        """The gate is "binding actually changed", not "never reset": moving
        this session's alias must drop the judges and the limiter."""
        reg = ModelRegistry(
            models={"gw": ModelConfig("gw", "http://a.example/v1", "k", "test-model")},
            default="gw",
        )
        session = _make_session(registry=reg, model_alias="gw")
        session._bind_model_from_registry("gw")
        session._output_guard_judge = MagicMock()
        session._judge = MagicMock()
        limiter = session._output_guard_judge_rl

        reg.reload(
            {"gw": ModelConfig("gw", "http://b.example/v1", "k", "test-model")},
            "gw",
            app_state=_KEYED_STATE,
        )
        session._refresh_model_from_registry()

        assert session._judge is None
        assert session._output_guard_judge is None
        assert session._output_guard_judge_rl is not limiter


class TestSessionRemovedAliasDegradedTurns:
    """An alias removed by a reload leaves the session holding a closed
    client. The refresh latches the diagnosis but the send still proceeds
    to the stream attempt, so a configured fallback carries the turn; only
    a terminal no-fallback failure surfaces the latched cause, worded per
    surface because /model routes on the interactive lanes only.
    """

    @staticmethod
    def _dead_client_error() -> RuntimeError:
        return RuntimeError("Cannot send a request, as the client has been closed.")

    # provider="openai-compatible" pins the Chat Completions surface, the
    # one the patched ``chat.completions.create`` stubs below speak.
    def _registry(self, fallback: list[str] | None = None) -> ModelRegistry:
        return ModelRegistry(
            models={
                "gw": ModelConfig(
                    "gw", "http://a.example/v1", "k", "test-model", provider="openai-compatible"
                ),
                "other": ModelConfig(
                    "other", "http://o.example/v1", "k", "o-model", provider="openai-compatible"
                ),
            },
            default="gw",
            fallback=fallback,
        )

    def _delete_gw(self, reg: ModelRegistry, fallback: list[str] | None = None) -> None:
        reg.reload(
            {
                "other": ModelConfig(
                    "other", "http://o.example/v1", "k", "o-model", provider="openai-compatible"
                )
            },
            "other",
            fallback,
            app_state=_KEYED_STATE,
        )

    def test_fallback_carries_turn_after_alias_deletion(self, caplog: Any) -> None:
        """Deleting a live session's alias degrades the turn onto the
        configured fallback instead of killing every subsequent send."""
        import logging

        reg = self._registry(fallback=["other"])
        fb_client = reg.get_client("other")
        fb_client.chat.completions.create = scripted_chat_client({"content": "carried"})
        session = _make_session(registry=reg, model_alias="gw")
        session.client.chat.completions.create = MagicMock(side_effect=self._dead_client_error())

        self._delete_gw(reg, fallback=["other"])
        with caplog.at_level(logging.WARNING):
            session.send("hello")
            session._refresh_model_from_registry()  # repeat: warning stays deduped

        assert not session.ui.errors, session.ui.errors
        assert any("falling back to other" in i for i in session.ui.infos)
        removed_warns = [
            r for r in caplog.records if "model_refresh_alias_removed" in r.getMessage()
        ]
        assert len(removed_warns) == 1  # once per (alias, generation)

    def test_no_fallback_turn_errors_with_removed_cause_and_model_remedy(self) -> None:
        """With no fallback the error names the alias-removed cause, not the
        raw closed-transport symptom."""
        reg = self._registry()
        session = _make_session(registry=reg, model_alias="gw")
        session.client.chat.completions.create = MagicMock(side_effect=self._dead_client_error())

        self._delete_gw(reg)
        with pytest.raises(RuntimeError):
            session.send("hello")

        assert session.ui.errors, "terminal failure must surface an error"
        message = session.ui.errors[-1]
        assert "removed from the registry" in message
        assert "/model" in message  # interactive lanes route slash commands
        assert "other" in message  # the remedy lists what is available

    def test_coordinator_error_omits_slash_model_remedy(self) -> None:
        """The coordinator routes no slash commands, so its error carries
        recreate-or-adjust wording instead."""
        reg = self._registry()
        session = _make_session(
            registry=reg, model_alias="gw", kind=WorkstreamKind.COORDINATOR, user_id="u1"
        )
        session.client.chat.completions.create = MagicMock(side_effect=self._dead_client_error())

        self._delete_gw(reg)
        with pytest.raises(RuntimeError):
            session.send("hello")

        assert session.ui.errors
        message = session.ui.errors[-1]
        assert "removed from the registry" in message
        assert "/model" not in message
        assert "adjust the workstream model" in message

    def test_recreated_broken_alias_reports_construction_cause(self, monkeypatch: Any) -> None:
        """A re-created alias reports the construction cause, never a stale
        "removed" diagnosis: the latch clears on the has_alias pass."""

        reg = self._registry()
        session = _make_session(registry=reg, model_alias="gw")
        session.client.chat.completions.create = MagicMock(side_effect=self._dead_client_error())

        self._delete_gw(reg)
        session._refresh_model_from_registry()
        assert session._registry_alias_removed == "gw"

        # Admin re-creates gw, but its client cannot be built.
        monkeypatch.setattr(
            mr_module,
            "create_client",
            MagicMock(side_effect=FileNotFoundError("/gone/cacert.pem")),
        )
        reg.reload(
            {
                "gw": ModelConfig(
                    "gw", "http://b.example/v1", "k", "test-model", provider="openai-compatible"
                ),
                "other": ModelConfig(
                    "other", "http://o.example/v1", "k", "o-model", provider="openai-compatible"
                ),
            },
            "gw",
            app_state=_KEYED_STATE,
        )
        session._refresh_model_from_registry()
        assert session._registry_alias_removed is None  # cleared on has_alias pass
        assert session._rebind_failed_key == ("gw", reg.generation)

        with pytest.raises(RuntimeError):
            session.send("hello")

        assert session.ui.errors
        message = session.ui.errors[-1]
        assert "could not be rebuilt" in message
        # The cause is path-scrubbed: the exception type plus a server-log
        # pointer, since SDK text can embed filesystem paths.
        assert "FileNotFoundError" in message
        assert "details in server log" in message
        assert "removed from the registry" not in message

    def test_recreated_alias_recovers_on_next_refresh(self) -> None:
        """Re-creating the alias bumps the generation, so the next refresh
        rebinds and sends flow again without a restart."""
        reg = ModelRegistry(
            models={
                "gw": ModelConfig("gw", "http://a.example/v1", "k", "test-model"),
                "other": ModelConfig("other", "http://o.example/v1", "k", "o-model"),
            },
            default="gw",
        )
        session = _make_session(registry=reg, model_alias="gw")
        reg.reload(
            {"other": ModelConfig("other", "http://o.example/v1", "k", "o-model")},
            "other",
            app_state=_KEYED_STATE,
        )
        session._refresh_model_from_registry()
        assert session._registry_alias_removed == "gw"

        reg.reload(
            {
                "gw": ModelConfig("gw", "http://a.example/v1", "k", "test-model"),
                "other": ModelConfig("other", "http://o.example/v1", "k", "o-model"),
            },
            "gw",
            app_state=_KEYED_STATE,
        )
        session._refresh_model_from_registry()

        assert session._registry_alias_removed is None
        assert session.client is reg.get_client("gw")
        assert session._registry_generation == reg.generation


class TestSessionConstructionFailureLatch:
    """A rebind whose client construction fails must not retry per send:
    construction runs under the registry-wide client lock. The refresh
    records the attempted (alias, generation), warns once per key, and
    re-attempts only when the registry actually changes.
    """

    def test_construction_attempted_once_per_generation(
        self, monkeypatch: Any, caplog: Any
    ) -> None:
        import logging

        reg = ModelRegistry(
            models={"gw": ModelConfig("gw", "http://a.example/v1", "k", "test-model")},
            default="gw",
        )
        session = _make_session(registry=reg, model_alias="gw")
        calls = {"n": 0}

        def _boom(provider: str, **kwargs: Any) -> Any:
            calls["n"] += 1
            raise FileNotFoundError("/gone/cacert.pem")

        monkeypatch.setattr(mr_module, "create_client", _boom)
        reg.reload(
            {"gw": ModelConfig("gw", "http://b.example/v1", "k", "test-model")},
            "gw",
            app_state=_KEYED_STATE,
        )
        with caplog.at_level(logging.WARNING):
            session._refresh_model_from_registry()  # attempts, fails, latches
            session._refresh_model_from_registry()  # latched: no attempt
            session._refresh_model_from_registry()

        assert calls["n"] == 1
        session_warns = [
            r
            for r in caplog.records
            if "model_refresh_client_construction_failed" in r.getMessage()
        ]
        assert len(session_warns) == 1  # once per (alias, generation)
        assert session._rebind_failed_key == ("gw", reg.generation)

    def test_generation_change_retries_and_success_clears_latch(
        self, monkeypatch: Any, caplog: Any
    ) -> None:
        import logging

        reg = ModelRegistry(
            models={"gw": ModelConfig("gw", "http://a.example/v1", "k", "test-model")},
            default="gw",
        )
        session = _make_session(registry=reg, model_alias="gw")
        real_create_client = mr_module.create_client
        state = {"broken": True, "calls": 0}

        def _flaky(provider: str, **kwargs: Any) -> Any:
            state["calls"] += 1
            if state["broken"]:
                raise FileNotFoundError("/gone/cacert.pem")
            return real_create_client(provider, **kwargs)

        monkeypatch.setattr(mr_module, "create_client", _flaky)
        reg.reload(
            {"gw": ModelConfig("gw", "http://b.example/v1", "k", "test-model")},
            "gw",
            app_state=_KEYED_STATE,
        )
        with caplog.at_level(logging.WARNING):
            session._refresh_model_from_registry()
            session._refresh_model_from_registry()  # latched
        assert state["calls"] == 1

        # A further reload (still broken) is a NEW generation: exactly one
        # more attempt and one more warning.
        reg.reload(
            {"gw": ModelConfig("gw", "http://c.example/v1", "k", "test-model")},
            "gw",
            app_state=_KEYED_STATE,
        )
        with caplog.at_level(logging.WARNING):
            session._refresh_model_from_registry()
            session._refresh_model_from_registry()  # latched again
        assert state["calls"] == 2
        session_warns = [
            r
            for r in caplog.records
            if "model_refresh_client_construction_failed" in r.getMessage()
        ]
        assert len(session_warns) == 2

        # Environment repaired + another reload: the rebind succeeds and
        # clears the latch.
        state["broken"] = False
        reg.reload(
            {"gw": ModelConfig("gw", "http://d.example/v1", "k", "test-model")},
            "gw",
            app_state=_KEYED_STATE,
        )
        session._refresh_model_from_registry()
        assert session._rebind_failed_key is None
        assert session.client is reg.get_client("gw")
        assert session._registry_generation == reg.generation


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

        # Scripted client records kwargs; read the model off its calls.
        fake_create = scripted_chat_client({"content": "done"})

        # Get the agent client from the registry and patch it
        agent_client = reg.get_client("agent")
        agent_client.chat.completions.create = fake_create

        agent_msgs = [
            Turn.system("You are an agent."),
            Turn.user("Do something."),
        ]
        session._run_agent(agent_msgs)
        assert fake_create.calls[-1].get("model") == "agent-model"

    @staticmethod
    def _capture_on(client: Any) -> dict[str, Any]:
        """Patch *client* (registry-resolved or session.client) to capture kwargs.

        Rides the shared scripted client; the returned dict mirrors the
        LAST call's kwargs (existing reader contract).
        """
        captured: dict[str, Any] = {}
        scripted = scripted_chat_client({"content": "done"})

        def fake_create(**kwargs: Any) -> Any:
            captured.update(kwargs)
            return scripted(**kwargs)

        client.chat.completions.create = fake_create
        return captured

    def _capture(self, reg: ModelRegistry, alias: str) -> dict[str, Any]:
        return self._capture_on(reg.get_client(alias))

    @staticmethod
    def _captured_effort(captured: dict[str, Any]) -> str | None:
        """Pull reasoning_effort out of provider-specific shapes.

        Chat Completions delivers it as a top-level ``reasoning_effort`` kwarg
        (when the model's caps permit it).  Operators who route reasoning_effort
        through ``chat_template_kwargs`` (gpt-oss-style local templates) get
        it inside ``extra_body.chat_template_kwargs``.
        """
        if "reasoning_effort" in captured:
            return captured["reasoning_effort"]
        eb = captured.get("extra_body") or {}
        ctk = eb.get("chat_template_kwargs") or {}
        return ctk.get("reasoning_effort")

    @staticmethod
    def _effort_caps() -> dict[str, Any]:
        """Capabilities that allow Chat-Completions reasoning_effort to flow."""
        return {
            "reasoning_effort_values": [
                "minimal",
                "low",
                "medium",
                "high",
                "max",
            ],
        }

    def _three_model_registry(self, **kwargs: Any) -> ModelRegistry:
        caps = self._effort_caps()
        return ModelRegistry(
            models={
                "main": ModelConfig(
                    "main",
                    "http://m/v1",
                    "k",
                    "main-model",
                    provider="openai-compatible",
                    capabilities=dict(caps),
                ),
                "smart": ModelConfig(
                    "smart",
                    "http://s/v1",
                    "k",
                    "smart-model",
                    provider="openai-compatible",
                    capabilities=dict(caps),
                ),
                "fast": ModelConfig(
                    "fast",
                    "http://f/v1",
                    "k",
                    "fast-model",
                    provider="openai-compatible",
                    capabilities=dict(caps),
                ),
            },
            default="main",
            **kwargs,
        )

    def test_task_model_overrides_agent_model(self) -> None:
        reg = self._three_model_registry(agent_model="smart", task_model="fast")
        session = _make_session(registry=reg, model_alias="main")
        captured = self._capture(reg, "fast")
        session._run_agent([Turn.user("x")], label="task")
        assert captured["model"] == "fast-model"

    def test_plan_falls_back_to_agent_model(self) -> None:
        reg = self._three_model_registry(agent_model="fast")
        session = _make_session(registry=reg, model_alias="main")
        captured = self._capture(reg, "fast")
        session._run_agent([Turn.user("x")], label="plan")
        assert captured["model"] == "fast-model"

    def test_plan_uses_session_model_when_no_overrides(self) -> None:
        # No agent_model/plan_model configured — _run_agent falls through to
        # session.client (the test's MagicMock) and session.model ("test-model").
        reg = self._three_model_registry()
        session = _make_session(registry=reg, model_alias="main")
        captured = self._capture_on(session.client)
        session._run_agent([Turn.user("x")], label="plan")
        assert captured["model"] == "test-model"

    def test_task_effort_inherits_session_when_unset(self) -> None:
        # Task with no task_effort override must inherit whatever the SESSION
        # is configured for — assert against an explicit value rather than
        # the constructor default so the invariant is unambiguous if someone
        # changes ChatSession's default later.
        reg = self._three_model_registry()
        session = _make_session(registry=reg, model_alias="main", reasoning_effort="low")
        captured = self._capture_on(session.client)
        session._run_agent([Turn.user("x")], label="task")
        assert self._captured_effort(captured) == "low"

    def test_agent_model_routes_both_plan_and_task(self) -> None:
        """Back-compat invariant via _run_agent: with only the legacy
        agent_model knob set, both plan and task labels must route through it."""
        reg = self._three_model_registry(agent_model="fast")
        session = _make_session(registry=reg, model_alias="main")

        plan_captured = self._capture(reg, "fast")
        session._run_agent([Turn.user("x")], label="plan")
        assert plan_captured["model"] == "fast-model"

        task_captured = self._capture(reg, "fast")
        session._run_agent([Turn.user("y")], label="task")
        assert task_captured["model"] == "fast-model"

    def test_explicit_effort_wins_over_registry(self) -> None:
        reg = self._three_model_registry(task_effort="low")
        session = _make_session(registry=reg, model_alias="main")
        captured = self._capture_on(session.client)
        session._run_agent([Turn.user("x")], label="task", reasoning_effort="minimal")
        assert self._captured_effort(captured) == "minimal"

    # -- per-call agent_alias override (LLM passes model="<alias>") ----------

    def test_run_agent_uses_explicit_alias_override(self) -> None:
        """agent_alias kwarg routes the agent call to the chosen client/model."""
        reg = self._three_model_registry()
        session = _make_session(registry=reg, model_alias="main")
        captured = self._capture(reg, "fast")
        session._run_agent([Turn.user("x")], label="task", agent_alias="fast")
        assert captured["model"] == "fast-model"

    def test_session_fallback_inherits_primary_alias_for_caps(self) -> None:
        """When _run_agent has no registry agent route, it must fall back to
        the session's primary alias for capability and server_compat lookup —
        otherwise per-model caps (reasoning_effort_values, server_compat) get
        silently dropped on the agent path."""
        reg = self._three_model_registry()  # no agent_model / plan_model set
        session = _make_session(registry=reg, model_alias="main")
        # Probe the lane resolution: extra_params now resolve INSIDE
        # resolve_lane (single config fetch) rather than via the session's
        # pre-resolution wrapper, so spy on the module seam; capability
        # resolution still routes through the session wrapper.
        from unittest.mock import patch

        import turnstone.core.model_turn as mt

        captured_lane_alias: list[str | None] = []
        captured_resolve_alias: list[str | None] = []
        original_lane = mt.resolve_lane
        original_resolve = session._resolve_capabilities

        def spy_lane(*args: Any, **kwargs: Any) -> Any:
            captured_lane_alias.append(kwargs.get("alias"))
            return original_lane(*args, **kwargs)

        def spy_resolve(*args: Any, **kwargs: Any) -> Any:
            # _resolve_capabilities(provider, model, alias)
            alias = args[2] if len(args) >= 3 else kwargs.get("alias")
            captured_resolve_alias.append(alias)
            return original_resolve(*args, **kwargs)

        session._resolve_capabilities = spy_resolve  # type: ignore[method-assign]

        self._capture_on(session.client)  # patch client.chat.completions.create
        with patch("turnstone.core.session.resolve_lane", side_effect=spy_lane):
            session._run_agent([Turn.user("x")], label="plan")

        assert captured_lane_alias and captured_lane_alias[-1] == "main", (
            f"agent fallback path did not inherit primary alias for the lane: "
            f"{captured_lane_alias!r}"
        )
        assert captured_resolve_alias and captured_resolve_alias[-1] == "main", (
            f"agent fallback path did not inherit primary alias for caps: "
            f"{captured_resolve_alias!r}"
        )

    def test_invalid_alias_raises_in_run_agent(self) -> None:
        """Defence-in-depth: _prepare_* validates first, but _run_agent
        rejects unknown aliases too rather than silently falling back."""
        reg = self._three_model_registry()
        session = _make_session(registry=reg, model_alias="main")
        with pytest.raises(ValueError, match="Unknown agent_alias"):
            session._run_agent([Turn.user("x")], label="plan", agent_alias="bogus")


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
    return SessionManager(adapter, storage=MagicMock(), max_active=10, event_emitter=adapter)


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

    def test_no_models_with_allow_empty_returns_empty_registry(self) -> None:
        """allow_empty=True degrades to an empty registry instead of raising.

        This is the boot-critical path turnstone-server uses: a node starts and
        registers with no models, then picks them up live from the admin panel.
        """
        with patch("turnstone.core.model_registry.load_config", return_value={}):
            reg = load_model_registry(model="", allow_empty=True)
        assert reg.count == 0
        assert reg.default == ""
        assert reg.list_aliases() == []

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

    def test_cli_default_shim_skipped_when_db_models_present(self) -> None:
        """An auto-detected ``--model`` does NOT synthesise a ``default``
        alias when the DB already contributes models.

        Regression for the silent bypass of ``model.task_alias`` /
        ``model.plan_alias``: a synthesised ``default`` aliased to whatever
        ``--base-url`` was at boot leaks into the LLM-visible alias list,
        and the LLM picks it for ``task_agent(model="default")`` — which
        then routes around the operator-configured per-role default.
        """
        storage = _MockStorage(
            [
                {
                    "alias": "gh200",
                    "model": "deepseek-ai/DeepSeek-V4-Flash",
                    "provider": "openai",
                    "base_url": "http://gh200:8000/v1",
                    "api_key": "sk-gh200",
                    "context_window": 1048576,
                    "capabilities": "{}",
                    "enabled": True,
                }
            ]
        )
        with patch("turnstone.core.model_registry.load_config", return_value={}):
            reg = load_model_registry(
                base_url="http://flatspark:8000/v1",
                api_key="sk-flatspark",
                model="qwen3.6-35B-A3B",  # populated by ``detect_model``
                storage=storage,
            )
        assert reg.has_alias("gh200")
        assert not reg.has_alias("default")

    def test_cli_default_shim_skipped_when_config_models_present(self) -> None:
        """Same shim suppression when only ``[models.*]`` populates configs."""
        fake_cfg: dict[str, Any] = {
            "models": {"local": {"model": "qwen3-32b"}},
        }
        with patch("turnstone.core.model_registry.load_config", return_value=fake_cfg):
            reg = load_model_registry("http://x/v1", "x", "fallback-model")
        assert reg.has_alias("local")
        assert not reg.has_alias("default")

    def test_cli_default_shim_still_fires_when_registry_empty(self) -> None:
        """Single-model CLI mode (no DB, no config.toml [models.*]) keeps
        the back-compat ``default`` alias."""
        with patch("turnstone.core.model_registry.load_config", return_value={}):
            reg = load_model_registry("http://x/v1", "x", "lone-model")
        assert reg.has_alias("default")
        assert reg.get_config("default").model == "lone-model"


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

        result = _effective_routing(None, self._models(), "default", "fast", "low")
        assert result == ("default", "fast", "low")

    def test_cs_alias_overrides_base(self) -> None:
        from turnstone.server import _effective_routing

        cs = _FakeCS(**{"model.task_alias": "smart"})
        result = _effective_routing(cs, self._models(), "default", "fast", "low")
        assert result == ("default", "smart", "low")

    def test_cs_alias_silently_dropped_when_unknown(self) -> None:
        from turnstone.server import _effective_routing

        cs = _FakeCS(**{"model.task_alias": "nonexistent"})
        result = _effective_routing(cs, self._models(), "default", "smart", None)
        assert result == ("default", "smart", None)  # falls back to base

    def test_cs_empty_string_treated_as_unset(self) -> None:
        from turnstone.server import _effective_routing

        cs = _FakeCS(
            **{
                "model.default_alias": "",
                "model.task_alias": "",
                "model.task_effort": "",
            }
        )
        result = _effective_routing(cs, self._models(), "default", "fast", "low")
        assert result == ("default", "fast", "low")

    def test_cs_effort_overrides_base(self) -> None:
        from turnstone.server import _effective_routing

        cs = _FakeCS(**{"model.task_effort": "minimal"})
        result = _effective_routing(cs, self._models(), "default", None, "high")
        assert result == ("default", None, "minimal")


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

        reg = self._registry(task_model="fast")
        cs = _FakeCS(**{"model.task_alias": "fast"})
        # Patch reload to detect calls
        called = {"count": 0}
        original_reload = reg.reload
        reg.reload = lambda *a, **kw: (
            called.update(count=called["count"] + 1)
            or original_reload(  # type: ignore[method-assign]
                *a, **kw
            )
        )

        assert _apply_routing_overrides(reg, cs, _KEYED_STATE) is False
        assert called["count"] == 0

    def test_reload_when_cs_differs(self) -> None:
        from turnstone.server import _apply_routing_overrides

        reg = self._registry()  # task_model=None
        cs = _FakeCS(**{"model.task_alias": "fast"})
        assert _apply_routing_overrides(reg, cs, _KEYED_STATE) is True
        assert reg.task_model == "fast"

    def test_no_reload_when_cs_is_none(self) -> None:
        from turnstone.server import _apply_routing_overrides

        reg = self._registry()
        assert _apply_routing_overrides(reg, None, _KEYED_STATE) is False

    def test_unknown_alias_does_not_trigger_reload(self) -> None:
        """Invalid CS aliases are silently dropped — no spurious reload."""
        from turnstone.server import _apply_routing_overrides

        reg = self._registry()
        cs = _FakeCS(**{"model.task_alias": "nonexistent"})
        assert _apply_routing_overrides(reg, cs, _KEYED_STATE) is False
        assert reg.task_model is None  # unchanged
