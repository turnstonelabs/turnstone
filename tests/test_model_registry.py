"""Tests for turnstone.core.model_registry — model registry, loading, session integration."""

from __future__ import annotations

import dataclasses
import json
import logging
import threading
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from tests._oidc_test_helpers import keyed_app_state
from tests._session_helpers import scripted_chat_client
from turnstone.core import model_registry as mr_module
from turnstone.core.model_registry import (
    DynamicAuthKeyError,
    ModelConcurrencyConfigError,
    ModelConfig,
    ModelRegistry,
    UnknownModelAliasError,
    _resolve_env_vars,
    detect_model,
    load_model_registry,
    stored_definition_error,
)
from turnstone.core.model_remedies import CHAT_MODEL_REMEDY
from turnstone.core.model_turn import lane_diagnostics, resolve_model_binding
from turnstone.core.oauth.context import OAuthContext, oauth_context
from turnstone.core.providers import list_known_models
from turnstone.core.trajectory import Turn
from turnstone.core.workstream import WorkstreamKind
from turnstone.prompts import ClientType

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
        assert cfg.max_concurrency == 0

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

    def test_max_concurrency_is_strict_and_not_binding_identity(self) -> None:
        unlimited = ModelConfig(alias="x", base_url="x", api_key="x", model="x")
        limited = dataclasses.replace(unlimited, max_concurrency=2)
        assert limited.max_concurrency == 2
        assert unlimited == limited

        for invalid in (-1, 2_147_483_648, True, 1.0, "1", None):
            with pytest.raises(ModelConcurrencyConfigError, match="max_concurrency"):
                dataclasses.replace(unlimited, max_concurrency=invalid)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# ModelRegistry
# ---------------------------------------------------------------------------


class TestModelRegistry:
    def _make_registry(
        self,
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
        with pytest.raises(UnknownModelAliasError, match="Unknown model alias") as exc_info:
            reg.get_config("nonexistent")
        assert exc_info.value.alias == "nonexistent"
        with pytest.raises(ValueError, match="Unknown model alias"):
            reg.get_client("nonexistent")

    def test_client_construction_failure_is_value_error(self) -> None:
        # Environment failures inside SDK construction (e.g. httpx2 raising
        # FileNotFoundError for an SSL_CERT_FILE bundle since deleted) must
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
    def test_empty_models_keep_a_configured_default(self) -> None:
        # The default names what configuration says even when nothing loaded
        # (#1359); resolving it raises rather than picking another model.
        reg = ModelRegistry(models={}, default="x")
        assert reg.default == "x"
        with pytest.raises(UnknownModelAliasError):
            reg.resolve()

    def test_empty_models_allowed_when_default_unset(self) -> None:
        # Degraded "no models configured yet" state — a server boots into this
        # and models are added live via the admin panel.
        reg = ModelRegistry(models={}, default="")
        assert reg.count == 0
        assert reg.list_aliases() == []

    def test_unloaded_default_kept(self) -> None:
        models = {"a": ModelConfig("a", "x", "x", "x")}
        reg = ModelRegistry(models=models, default="bad")
        assert reg.default == "bad"
        with pytest.raises(UnknownModelAliasError) as exc_info:
            reg.resolve()
        assert exc_info.value.alias == "bad"

    def test_routing_aliases_are_keyword_only(self) -> None:
        # The fallback list that sat third is gone (#1359); a caller still
        # passing it by position fails loudly instead of it becoming agent_model.
        models = {"a": ModelConfig("a", "x", "x", "x")}
        with pytest.raises(TypeError):
            ModelRegistry(models, "a", ["bad"])  # type: ignore[misc]
        reg = ModelRegistry(models=models, default="a")
        with pytest.raises(TypeError):
            reg.reload(models, "a", ["bad"], app_state=_KEYED_STATE)  # type: ignore[misc]
        assert reg.agent_model is None

    def test_unloaded_agent_and_task_models_kept(self) -> None:
        models = {"a": ModelConfig("a", "x", "x", "x")}
        reg = ModelRegistry(models=models, default="a", agent_model="bad", task_model="worse")
        assert reg.agent_model == "bad"
        assert reg.task_model == "worse"


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
                    "max_concurrency": 2,
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
        _, model, cfg, _ = reg.resolve()
        assert model == "gpt-4o"
        assert cfg.max_concurrency == 2

    @pytest.mark.parametrize("invalid", [-1, 2_147_483_648, True, 1.0, "1", None])
    def test_config_rejects_invalid_max_concurrency(self, invalid: Any) -> None:
        fake_cfg = {
            "models": {
                "local": {
                    "base_url": "http://localhost:8000/v1",
                    "model": "m",
                    "max_concurrency": invalid,
                }
            }
        }
        with (
            patch("turnstone.core.model_registry.load_config", return_value=fake_cfg),
            pytest.raises(ModelConcurrencyConfigError, match="max_concurrency"),
        ):
            load_model_registry()

    def test_config_context_window_zero_inherits_cli_window(self) -> None:
        """``context_window = 0`` in a [models.*] entry is the auto-detect
        sentinel: it must never stay a literal 0 (which would zero every
        downstream budget — judge lowering, session compaction). A caller
        that supplies a bootstrap window — the CLI's ``--context-window`` or
        its own detection — is the resolution; the server passes 0 and the
        per-definition chain pinned in TestContextWindowAutoDetect runs."""
        fake_cfg: dict[str, Any] = {
            "models": {
                "local": {
                    "base_url": "http://localhost:8000/v1",
                    "model": "local-model",
                    "context_window": 0,  # auto-detect
                },
                "claude": {"provider": "anthropic", "model": _ANTHROPIC_ID},
            },
            "model": {"default": "local"},
        }
        with patch("turnstone.core.model_registry.load_config", return_value=fake_cfg):
            reg = load_model_registry(
                base_url="http://localhost:8000/v1",
                api_key="dummy",
                model="local-model",
                context_window=40_000,  # the CLI's own bootstrap window
            )
        _, _, cfg, _ = reg.resolve("local")
        assert cfg.context_window == 40_000  # inherited, not the literal 0
        # A capability-table provider is resolved from the table even in the
        # CLI: the bootstrap window applies to local servers only.
        from turnstone.core.providers import create_provider

        expected = create_provider("anthropic").get_capabilities(_ANTHROPIC_ID).context_window
        assert reg.get_config("claude").context_window == expected

    def test_fallback_key_is_ignored_with_one_warning(self, monkeypatch: Any, caplog: Any) -> None:
        # [model] fallback was removed with the failover chain (#1359): a
        # config.toml that still sets it loads, nothing reads the list, and the
        # process says so once however often the registry reloads.
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
        monkeypatch.setattr(mr_module, "_REMOVED_MODEL_KEYS_WARNED", set())
        with (
            caplog.at_level(logging.WARNING),
            patch("turnstone.core.model_registry.load_config", return_value=fake_cfg),
        ):
            reg = load_model_registry("http://x/v1", "x", "x")
            load_model_registry("http://x/v1", "x", "x")
        assert reg.has_alias("fallback1")
        assert not hasattr(reg, "fallback")
        warned = [r for r in caplog.records if "model_registry.fallback_ignored" in r.getMessage()]
        assert len(warned) == 1
        assert "failover chain was removed" in warned[0].getMessage()

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

    def test_unloaded_agent_model_kept(self) -> None:
        # Kept as configured, not dropped: resolving it fails instead of the
        # task agent quietly running on another model (#1359).
        fake_cfg: dict[str, Any] = {
            "model": {"agent_model": "nonexistent"},
        }
        with patch("turnstone.core.model_registry.load_config", return_value=fake_cfg):
            reg = load_model_registry("http://x/v1", "x", "x")
        assert reg.agent_model == "nonexistent"
        assert not reg.has_alias("nonexistent")

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

    def test_unloaded_task_model_kept(self) -> None:
        # Kept, and it outranks agent_model as configured: no fall-through to
        # another model because this one didn't load (#1359).
        fake_cfg: dict[str, Any] = {
            "models": {"cheap": {"base_url": "http://c/v1", "model": "c"}},
            "model": {"task_model": "alsonope", "agent_model": "cheap"},
        }
        with patch("turnstone.core.model_registry.load_config", return_value=fake_cfg):
            reg = load_model_registry("http://x/v1", "x", "x")
        assert reg.task_model == "alsonope"
        assert reg.resolve_agent_alias("task") == "alsonope"

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

    def test_unloaded_default_is_kept(self) -> None:
        # A configured default that names no loaded model stays the default;
        # whatever relies on it fails instead of running another model (#1359).
        fake_cfg: dict[str, Any] = {
            "model": {"default": "nonexistent"},
        }
        with patch("turnstone.core.model_registry.load_config", return_value=fake_cfg):
            reg = load_model_registry("http://x/v1", "x", "x")
        assert reg.default == "nonexistent"
        assert reg.has_alias("default")  # the CLI-synthesized entry still loads
        with pytest.raises(UnknownModelAliasError) as exc_info:
            reg.resolve()
        assert exc_info.value.alias == "nonexistent"

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
                    "max_concurrency": 4,
                }
            ]
        )
        with patch("turnstone.core.model_registry.load_config", return_value={}):
            reg = load_model_registry("http://x/v1", "x", "x", storage=storage)
        cfg = reg.get_config("hot-model")
        assert cfg.temperature == 1.5
        assert cfg.max_tokens == 4096
        assert cfg.reasoning_effort == "high"
        assert cfg.max_concurrency == 4

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

    def test_db_sampling_params_that_are_not_numbers_load_as_unset(self, caplog: Any) -> None:
        """A stored temperature or max_tokens that isn't a number, or is outside the range
        the console accepts, is ignored as in config.toml, so a strict load (a node's boot
        and Sync) keeps the definition."""
        base = {
            "model": "m",
            "provider": "openai",
            "base_url": "",
            "api_key": "",
            "context_window": 32768,
            "capabilities": "{}",
            "enabled": True,
        }
        storage = _MockStorage(
            [
                {**base, "alias": "garbled", "temperature": "hot", "max_tokens": float("inf")},
                {**base, "alias": "out-of-range", "temperature": float("nan"), "max_tokens": 0},
                {**base, "alias": "too-hot", "temperature": 2.5, "max_tokens": 4096},
            ]
        )
        with (
            patch("turnstone.core.model_registry.load_config", return_value={}),
            caplog.at_level(logging.WARNING),
        ):
            reg = load_model_registry("http://x/v1", "x", "x", storage=storage, strict=True)
        warned = [r.getMessage() for r in caplog.records]
        for alias in ("garbled", "out-of-range"):
            cfg = reg.get_config(alias)
            assert cfg.temperature is None
            assert cfg.max_tokens is None
            for field in ("temperature", "max_tokens"):
                assert any(
                    "model_registry.override_invalid" in m and alias in m and field in m
                    for m in warned
                ), (alias, field)
        too_hot = reg.get_config("too-hot")
        assert too_hot.temperature is None
        assert too_hot.max_tokens == 4096

    def test_db_sampling_params_at_the_range_edges_load(self) -> None:
        """The console's own bounds stay valid: temperature 0 and 2, max_tokens 1."""
        storage = _MockStorage(
            [
                {
                    "alias": "edges",
                    "model": "m",
                    "provider": "openai",
                    "base_url": "",
                    "api_key": "",
                    "context_window": 32768,
                    "capabilities": "{}",
                    "enabled": True,
                    "temperature": 2.0,
                    "max_tokens": 1,
                }
            ]
        )
        with patch("turnstone.core.model_registry.load_config", return_value={}):
            reg = load_model_registry("http://x/v1", "x", "x", storage=storage, strict=True)
        cfg = reg.get_config("edges")
        assert cfg.temperature == 2.0
        assert cfg.max_tokens == 1

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


class TestStoredDefinitionError:
    def test_names_the_field_the_placeholder_and_what_to_do(self) -> None:
        key_error = stored_definition_error({"api_key": "sk-${OPENAI_API_KEY}"})
        assert key_error.startswith("api_key contains ${OPENAI_API_KEY}, which")
        assert "enter the key itself" in key_error and "secret://" in key_error
        url_error = stored_definition_error({"base_url": "https://${HOST}/v1"})
        assert url_error.startswith("base_url contains ${HOST}, which")
        assert "enter the URL itself" in url_error
        assert "config.toml, where a URL that carries a credential belongs" in url_error

    def test_an_api_key_reference_is_left_to_its_resolver(self) -> None:
        assert stored_definition_error({"api_key": "secret://file/${TEAM}"}) == ""
        # A base_url has no resolver, so a reference-shaped one is checked like any other.
        assert stored_definition_error({"base_url": "secret://file/${HOST}/v1"})

    @pytest.mark.parametrize(
        "value", ["${A}", "x${A_1}y", "${A}${A}", "$A", "${}", "${1A}", "${A", "plain", ""]
    )
    def test_refuses_exactly_what_the_expansion_would_change(self, value: str) -> None:
        """The check and ``_resolve_env_vars`` share one pattern, so a stored
        value is refused when, and only when, expanding it would change it."""
        with patch.dict("os.environ", {"A": "1", "A_1": "2"}):
            would_expand = _resolve_env_vars(value) != value
        assert bool(stored_definition_error({"api_key": value})) is would_expand


class TestStoredPlaceholders:
    """A database row is never expanded from the environment: one whose api_key
    or base_url holds a ${VAR} is skipped and named, even with the variable set,
    while config.toml entries and the CLI's default key keep their expansion."""

    @staticmethod
    def _row(**overrides: Any) -> dict[str, Any]:
        row: dict[str, Any] = {
            "alias": "row",
            "model": "m1",
            "provider": "openai-compatible",
            "base_url": "https://models.example.com/v1",
            "api_key": "sk-literal",
            "context_window": 0,
            "capabilities": "{}",
            "enabled": True,
        }
        row.update(overrides)
        return row

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("api_key", "${TS_STORED_VALUE}"),
            ("base_url", "https://models.example.com/v1/${TS_STORED_VALUE}"),
        ],
    )
    def test_row_is_skipped_named_and_never_probed(
        self, monkeypatch: pytest.MonkeyPatch, field: str, value: str
    ) -> None:
        monkeypatch.setenv("TS_STORED_VALUE", "env-value")
        seen: list[tuple[str, str]] = []

        def record(provider: str, base_url: str, api_key: str, *a: Any, **kw: Any) -> Any:
            seen.append((base_url, api_key))
            return {"reachable": True, "model_found": True, "context_window": 4096}

        monkeypatch.setattr(mr_module, "probe_model_endpoint", record)
        storage = _MockStorage([self._row(**{field: value}), self._row(alias="ok")])
        with patch("turnstone.core.model_registry.load_config", return_value={}):
            reg = load_model_registry(
                storage=storage, allow_empty=True, detect_context_windows=True
            )
        assert reg.list_aliases() == ["ok"]
        assert reg.skipped_aliases["row"].startswith(field + " contains ${TS_STORED_VALUE}")
        assert seen and set(seen) == {("https://models.example.com/v1", "sk-literal")}

    def test_config_entries_keep_their_expansion(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TS_CONFIG_KEY", "sk-from-env")
        monkeypatch.setenv("TS_CONFIG_URL", "https://models.example.com/v1")
        entry = {
            "model": "m1",
            "provider": "openai-compatible",
            "base_url": "${TS_CONFIG_URL}",
            "api_key": "${TS_CONFIG_KEY}",
            "context_window": 8192,
        }
        fake_cfg: dict[str, Any] = {"models": {"cfg": entry}}
        with patch("turnstone.core.model_registry.load_config", return_value=fake_cfg):
            reg = load_model_registry(allow_empty=True)
        cfg = reg.get_config("cfg")
        assert (cfg.base_url, cfg.api_key) == ("https://models.example.com/v1", "sk-from-env")

    def test_the_cli_default_key_keeps_its_expansion(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A config.toml entry without a key takes the CLI's, which the operator supplies."""
        monkeypatch.setenv("TS_CLI_KEY", "sk-from-env")
        entry = {"model": "m1", "base_url": "http://x/v1", "context_window": 8192}
        fake_cfg: dict[str, Any] = {"models": {"cfg": entry}}
        with patch("turnstone.core.model_registry.load_config", return_value=fake_cfg):
            reg = load_model_registry("http://x/v1", "${TS_CLI_KEY}", "x")
        assert reg.get_config("cfg").api_key == "sk-from-env"

    def test_only_a_row_still_skipped_after_config_toml_is_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A row whose alias config.toml serves is of no consequence: no ERROR for it."""
        entry = {
            "model": "m1",
            "provider": "openai-compatible",
            "base_url": "https://models.example.com/v1",
            "api_key": "sk-from-config",
            "context_window": 8192,
        }
        fake_cfg: dict[str, Any] = {"models": {"served": entry}}
        storage = _MockStorage(
            [
                self._row(alias="served", api_key="${TS_STORED_VALUE}", context_window=8192),
                self._row(alias="absent", api_key="${TS_STORED_VALUE}", context_window=8192),
            ]
        )
        with (
            patch("turnstone.core.model_registry.load_config", return_value=fake_cfg),
            caplog.at_level(logging.ERROR),
        ):
            reg = load_model_registry(storage=storage, allow_empty=True)
        assert set(reg.skipped_aliases) == {"absent"}
        logged = [
            _rendered(r) for r in caplog.records if "model_registry.alias_skipped" in _rendered(r)
        ]
        assert len(logged) == 1 and "absent" in logged[0]


# ---------------------------------------------------------------------------
# Registry-owned rerank lanes
# ---------------------------------------------------------------------------


class TestRerankLaneRegistry:
    @staticmethod
    def _cfg(
        alias: str = "rr",
        *,
        url: str = "http://rerank.example/rerank",
        model: str = "bge",
        key: str = "secret",
        max_concurrency: int = 2,
    ) -> ModelConfig:
        return ModelConfig(
            alias,
            url,
            key,
            model,
            capabilities={"supports_rerank": True},
            max_concurrency=max_concurrency,
        )

    def test_resolve_reuses_runtime_and_stable_admission_without_llm_client(self) -> None:
        cfg = self._cfg()
        reg = ModelRegistry({"rr": cfg}, "rr")

        first = reg.resolve_rerank_lane("rr", instruction="rank", config_version=4)
        second = reg.resolve_rerank_lane("rr", instruction="rank", config_version=5)

        assert first.runtime is second.runtime
        assert first.admission is second.admission is reg.get_admission("rr")
        assert first.config_version == 4
        assert second.config_version == 5
        assert first.admission.limit == 2
        assert reg._clients == {}
        assert reg._providers == {}
        entry = reg._rerank_runtimes["rr"]
        assert "secret" not in repr(entry)
        assert "secret" not in repr(entry.config)
        reg.shutdown()

    def test_instruction_change_rotates_and_closes_old_runtime(self) -> None:
        reg = ModelRegistry({"rr": self._cfg()}, "rr")
        old = reg.resolve_rerank_lane("rr", instruction="old")

        new = reg.resolve_rerank_lane("rr", instruction="new")

        assert new.runtime is not old.runtime
        assert old.runtime.snapshot().retired
        assert old.runtime.snapshot().closed
        assert not new.runtime.snapshot().retired
        reg.shutdown()

    def test_cap_only_reload_preserves_runtime_and_resizes_gate(self) -> None:
        reg = ModelRegistry({"rr": self._cfg(max_concurrency=1)}, "rr")
        old = reg.resolve_rerank_lane("rr", instruction="rank")

        reg.reload(
            {"rr": self._cfg(max_concurrency=4)},
            "rr",
            app_state=_KEYED_STATE,
        )
        new = reg.resolve_rerank_lane("rr", instruction="rank")

        assert new.runtime is old.runtime
        assert new.admission is old.admission
        assert new.admission.limit == 4
        assert new.registry_generation == 1
        assert not old.runtime.snapshot().retired
        reg.shutdown()

    @pytest.mark.parametrize(
        "replacement",
        [
            {"url": "http://other.example/rerank"},
            {"model": "other"},
            {"key": "different"},
        ],
    )
    def test_relevant_reload_eagerly_retires_runtime(self, replacement: dict[str, str]) -> None:
        reg = ModelRegistry({"rr": self._cfg()}, "rr")
        old = reg.resolve_rerank_lane("rr")

        reg.reload(
            {"rr": self._cfg(**replacement)},
            "rr",
            app_state=_KEYED_STATE,
        )

        assert old.runtime.snapshot().retired
        assert old.runtime.snapshot().closed
        assert reg._rerank_runtimes == {}
        new = reg.resolve_rerank_lane("rr")
        assert new.runtime is not old.runtime
        reg.shutdown()

    def test_relevant_reload_lets_active_call_drain_before_close(self) -> None:
        from turnstone.core.rerank import RerankHit, rerank

        class _BlockingClient:
            def __init__(self) -> None:
                self.entered = threading.Event()
                self.release = threading.Event()
                self.close_calls = 0

            def rerank(self, query: str, documents: list[str], **kwargs: Any) -> list[RerankHit]:
                del query, kwargs
                self.entered.set()
                assert self.release.wait(5)
                return [RerankHit(i, 1.0) for i in range(len(documents))]

            def close(self) -> None:
                self.close_calls += 1

        client = _BlockingClient()
        reg = ModelRegistry({"rr": self._cfg()}, "rr")
        with patch("turnstone.core.rerank.resolve_rerank_client", return_value=client):
            old = reg.resolve_rerank_lane("rr")

        outcome: list[list[RerankHit]] = []
        worker = threading.Thread(
            target=lambda: outcome.append(rerank(old, "q", ["d"], timeout=2.0)),
            daemon=True,
        )
        worker.start()
        assert client.entered.wait(2)

        reg.reload(
            {"rr": self._cfg(url="http://other.example/rerank")},
            "rr",
            app_state=_KEYED_STATE,
        )
        assert old.runtime.snapshot().retired
        assert not old.runtime.snapshot().closed
        assert client.close_calls == 0

        client.release.set()
        worker.join(2)
        assert not worker.is_alive()
        assert outcome and outcome[0][0].index == 0
        assert old.runtime.snapshot().closed
        assert client.close_calls == 1
        reg.shutdown()

    def test_resolving_new_role_alias_retires_previous_alias(self) -> None:
        a = self._cfg("a", url="http://a.example/rerank")
        b = self._cfg("b", url="http://b.example/rerank")
        reg = ModelRegistry({"a": a, "b": b}, "a")
        old = reg.resolve_rerank_lane("a")

        current = reg.resolve_rerank_lane("b")

        assert old.runtime.snapshot().closed
        assert set(reg._rerank_runtimes) == {"b"}
        assert current.admission is reg.get_admission("b")
        reg.shutdown()

    def test_deactivate_and_shutdown_are_idempotent(self) -> None:
        reg = ModelRegistry({"rr": self._cfg()}, "rr")
        lane = reg.resolve_rerank_lane("rr")

        reg.deactivate_rerank_runtime()
        reg.deactivate_rerank_runtime()
        reg.shutdown()
        reg.shutdown()

        assert lane.runtime.snapshot().retired
        assert lane.runtime.snapshot().closed
        assert reg._rerank_runtimes == {}

    @pytest.mark.parametrize("operation", ["reload", "shutdown"])
    def test_rerank_transport_closes_when_llm_client_close_raises(
        self,
        operation: str,
    ) -> None:
        class _RerankClient:
            def __init__(self) -> None:
                self.close_calls = 0

            def close(self) -> None:
                self.close_calls += 1

        class _FailingLLMClient:
            def close(self) -> None:
                raise RuntimeError("llm close failed")

        rr_cfg = self._cfg()
        llm_cfg = ModelConfig("llm", "http://old.example/v1", "key", "model")
        reg = ModelRegistry({"llm": llm_cfg, "rr": rr_cfg}, "llm")
        rerank_client = _RerankClient()
        with patch(
            "turnstone.core.rerank.resolve_rerank_client",
            return_value=rerank_client,
        ):
            lane = reg.resolve_rerank_lane("rr")
        reg._clients["llm"] = _FailingLLMClient()

        with pytest.raises(RuntimeError, match="llm close failed"):
            if operation == "reload":
                reg.reload(
                    {
                        "llm": dataclasses.replace(
                            llm_cfg,
                            base_url="http://new.example/v1",
                        ),
                        "rr": dataclasses.replace(
                            rr_cfg,
                            base_url="http://new-rerank.example/rerank",
                        ),
                    },
                    "llm",
                    app_state=_KEYED_STATE,
                )
            else:
                reg.shutdown()

        assert rerank_client.close_calls == 1
        assert lane.runtime.snapshot().retired
        assert lane.runtime.snapshot().closed
        assert reg._rerank_runtimes == {}


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

    def test_reload_keeps_an_unloaded_default(self) -> None:
        # A reload whose default didn't load installs it as configured; a
        # create that relies on it fails instead of using "a" (#1359).
        models_a = {"a": ModelConfig("a", "x", "x", "m")}
        reg = ModelRegistry(models=models_a, default="a")
        reg.reload(models_a, "nonexistent", app_state=_KEYED_STATE)
        assert reg.has_alias("a")
        assert reg.default == "nonexistent"
        with pytest.raises(UnknownModelAliasError):
            reg.resolve()

    def test_reload_to_empty_keeps_the_default(self) -> None:
        models_a = {"a": ModelConfig("a", "x", "x", "m")}
        reg = ModelRegistry(models=models_a, default="a")
        reg.reload({}, "a", app_state=_KEYED_STATE)
        assert reg.count == 0
        assert reg.default == "a"

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
        keyless = SimpleNamespace(oauth_context=OAuthContext(token_store=None))
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
        keyless = SimpleNamespace(oauth_context=OAuthContext(token_store=None))
        reg.reload({"b": ModelConfig("b", "http://y/v1", "key", "m")}, "b", app_state=keyless)
        assert reg.has_alias("b")


class TestProfileMismatchVisibility:
    """``profile_mismatched_aliases`` and its reload-chokepoint warning: a
    persisted row whose mode names the other grant dialect stays loadable
    but can never mint, and every swap must say so."""

    @staticmethod
    def _mixed_models() -> dict[str, ModelConfig]:
        return {
            "plain": ModelConfig("plain", "http://x/v1", "key", "m"),
            "gw-entra": ModelConfig(
                "gw-entra",
                "http://gw/v1",
                "",
                "m",
                auth_mode="entra_obo",
                obo_audience="api://gw",
            ),
            "gw-app": ModelConfig(
                "gw-app",
                "http://gw/v1",
                "",
                "m",
                auth_mode="entra_app",
                obo_audience="api://gw",
            ),
            "gw-kc": ModelConfig(
                "gw-kc",
                "http://gw/v1",
                "",
                "m",
                auth_mode="rfc8693_obo",
                obo_audience="api://gw",
            ),
        }

    def test_helper_returns_mismatched_rows_sorted(self) -> None:
        from turnstone.core.model_registry import profile_mismatched_aliases

        assert profile_mismatched_aliases(self._mixed_models(), "rfc8693") == [
            ("gw-app", "entra_app", "entra"),
            ("gw-entra", "entra_obo", "entra"),
        ]
        assert profile_mismatched_aliases(self._mixed_models(), "entra") == [
            ("gw-kc", "rfc8693_obo", "rfc8693")
        ]

    def test_helper_skips_static_and_unmapped_modes(self) -> None:
        from turnstone.core.model_registry import profile_mismatched_aliases

        # Direct construction bypasses load-path validation, standing in for
        # a future dynamic mode nobody has paired yet: not a PROFILE
        # mismatch — the write validator and dispatch own that class.
        models = {
            "plain": ModelConfig("plain", "http://x/v1", "key", "m"),
            "gw-next": ModelConfig(
                "gw-next",
                "http://gw/v1",
                "",
                "m",
                auth_mode="future_mode",
                obo_audience="api://gw",
            ),
        }
        assert profile_mismatched_aliases(models, "rfc8693") == []

    def test_reload_warns_per_mismatched_row(self, caplog: pytest.LogCaptureFixture) -> None:
        import logging

        reg = ModelRegistry(models={"a": ModelConfig("a", "http://x/v1", "key", "m")}, default="a")
        state = keyed_app_state()
        oauth_context(state).oidc_config = SimpleNamespace(
            enabled=True, obo_grant_profile="rfc8693"
        )
        models = {
            "gw-entra": ModelConfig(
                "gw-entra",
                "http://gw/v1",
                "",
                "m",
                auth_mode="entra_obo",
                obo_audience="api://gw",
            ),
        }
        with caplog.at_level(logging.WARNING):
            reg.reload(models, "gw-entra", app_state=state)
        blob = " ".join(r.getMessage() for r in caplog.records)
        assert "gw-entra" in blob
        assert "grant_profile_mismatch" in blob
        assert "'rfc8693'" in blob and "'entra'" in blob

    def test_no_mismatch_warning_when_oidc_disabled(self, caplog: pytest.LogCaptureFixture) -> None:
        """OIDC-disabled deployments must NOT get the mismatch warning: the
        loaded config defaults obo_grant_profile even when OIDC is off, and
        the runtime refuses at the enabled check first — so the warning
        would name a remedy (flip the profile) that cannot make the alias
        mint, contradicting the heartbeat's oidc_not_enabled cause.
        """
        import logging

        from turnstone.core.model_registry import warn_profile_mismatched_aliases

        models = {
            "gw-kc": ModelConfig(
                "gw-kc",
                "http://gw/v1",
                "",
                "m",
                auth_mode="rfc8693_obo",
                obo_audience="api://gw",
            ),
        }
        for oidc in (
            None,
            SimpleNamespace(enabled=False, obo_grant_profile="entra"),
        ):
            caplog.clear()
            with caplog.at_level(logging.WARNING):
                warn_profile_mismatched_aliases(
                    models, SimpleNamespace(oauth_context=OAuthContext(oidc_config=oidc))
                )
            assert not [r for r in caplog.records if "will not mint" in r.getMessage()]

    def test_mismatch_warning_names_the_mode_correct_cause(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The warning's cause token must match what the alias's mint
        actually records: the app-identity mint refuses a non-entra profile
        as unsupported_grant_profile, the delegated legs as
        grant_profile_mismatch — an operator greps the runtime heartbeat
        for exactly the token the boot warning named.
        """
        import logging

        from turnstone.core.model_registry import warn_profile_mismatched_aliases

        state = SimpleNamespace(
            oauth_context=OAuthContext(
                oidc_config=SimpleNamespace(enabled=True, obo_grant_profile="rfc8693")
            )
        )
        with caplog.at_level(logging.WARNING):
            warn_profile_mismatched_aliases(self._mixed_models(), state)
        by_alias = {
            alias: r.getMessage()
            for r in caplog.records
            for alias in ("gw-app", "gw-entra")
            if f"'{alias}'" in r.getMessage()
        }
        assert "unsupported_grant_profile" in by_alias["gw-app"]
        assert "unsupported_grant_profile" not in by_alias["gw-entra"]
        assert "grant_profile_mismatch" in by_alias["gw-entra"]


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
    kind: WorkstreamKind = WorkstreamKind.INTERACTIVE,
    user_id: str = "",
    ws_id: str | None = None,
    judge_config: Any | None = None,
    config_store: Any | None = None,
    workstream_lease: Any | None = None,
    client_type: Any | None = None,
    parent_ws_id: str | None = None,
) -> Any:
    """Create a ChatSession with one factory-shaped atomic model binding.

    Registry-backed sessions receive every constructor facet from the same
    :func:`resolve_model_binding` result, mirroring all production factories.
    Storeless sessions use an explicit mock client/model pair. *client_type*
    defaults to the session's own default (the CLI).
    """
    from turnstone.core.session import ChatSession

    binding = None
    if registry is not None:
        effective_alias = model_alias or registry.default
        binding = resolve_model_binding(registry, effective_alias)
        session_client = binding.lane.client
        session_model = binding.lane.model
        registry_generation = binding.registry_generation
        binding_config = binding.config
        if binding_config is None:
            raise RuntimeError(f"test registry binding for {effective_alias!r} has no config")
        context_window = binding_config.context_window
    else:
        effective_alias = None
        session_client = MagicMock()
        session_model = "test-model"
        registry_generation = None
        context_window = 32768

    return ChatSession(
        client=session_client,
        model=session_model,
        ui=_FakeUI(),
        instructions=None,
        temperature=0.5,
        max_tokens=4096,
        tool_timeout=30,
        registry=registry,
        model_alias=effective_alias,
        registry_generation=registry_generation,
        context_window=context_window,
        reasoning_effort=reasoning_effort,
        kind=kind,
        user_id=user_id,
        ws_id=ws_id,
        judge_config=judge_config,
        config_store=config_store,
        model_binding=binding,
        workstream_lease=workstream_lease,
        client_type=client_type or ClientType.CLI,
        parent_ws_id=parent_ws_id,
    )


def _make_durable_session(**kwargs: Any) -> Any:
    """Create a direct session with production's parent-before-row order."""
    from turnstone.core.storage import get_storage

    session = _make_session(**kwargs)
    get_storage().register_workstream(
        session.ws_id,
        user_id=session._user_id,
        kind=session._kind,
    )
    return session


def _binding(session: Any) -> Any:
    return session._model_binding


def _lane(session: Any) -> Any:
    return _binding(session).lane


def _client(session: Any) -> Any:
    return _lane(session).client


def _provider(session: Any) -> Any:
    return _lane(session).provider


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
        old_binding = _binding(session)

        def _boom(provider: str, **kwargs: Any) -> Any:
            raise FileNotFoundError("/etc/ssl/missing-ca.pem")

        monkeypatch.setattr(mr_module, "create_client", _boom)
        session.handle_command("/model gw")

        info = session.ui.infos[-1]
        assert "Unknown model alias" not in info
        assert "failed to construct" in info
        assert "details in server log" in info
        assert _binding(session) is old_binding
        assert session.model == "default-model"
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
        old_binding = _binding(session)

        session.handle_command("/model gw")

        info = session.ui.infos[-1]
        assert "Unknown model alias" not in info
        assert "bogus" in info  # the real api_surface cause, verbatim
        assert _binding(session) is old_binding
        assert session.model == "default-model"
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
        output_guard = MagicMock()
        session._output_guard_judge = output_guard
        output_guard_cancel = threading.Event()
        session._output_guard_judge_cancel = output_guard_cancel
        old_limiter = session._output_guard_judge_rl

        session.handle_command("/model alt")

        assert "Switched to" in session.ui.infos[-1]
        assert session._judge is None
        assert session._output_guard_judge is None
        assert output_guard_cancel.is_set()
        output_guard.retire.assert_called_once_with()
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

    @pytest.mark.parametrize(
        ("kind", "expected"),
        [(WorkstreamKind.COORDINATOR, "high"), (WorkstreamKind.INTERACTIVE, None)],
    )
    def test_model_switch_keeps_the_coordinator_effort_rung(
        self,
        kind: WorkstreamKind,
        expected: str | None,
        tmp_db: Any,
        sqlite_backend_factory: Any,
    ) -> None:
        """A coordinator resolves effort through ``coordinator.reasoning_effort``
        at creation; a ``/model`` switch keeps that rung, and saves it. Other
        kinds never read it."""
        from turnstone.core.config_store import ConfigStore

        cs = ConfigStore(sqlite_backend_factory(str(tmp_db), create_tables=True))
        cs.set("coordinator.reasoning_effort", "high", changed_by="test")
        reg = ModelRegistry(
            models={
                "a": ModelConfig("a", "x", "x", "a-model"),
                "b": ModelConfig("b", "y", "y", "b-model"),
            },
            default="a",
        )
        session = _make_durable_session(
            registry=reg,
            model_alias="a",
            kind=kind,
            user_id="u1",
            config_store=cs,
            client_type=ClientType.WEB,
            reasoning_effort="high",
        )
        session.handle_command("/model b")
        assert "Switched to" in session.ui.infos[-1]
        assert session.reasoning_effort == expected
        assert session._config_for_save()["reasoning_effort"] == (expected or "")

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

    def test_model_show_agent_model_info(self) -> None:
        reg = ModelRegistry(
            models={
                "a": ModelConfig("a", "x", "x", "m-a"),
                "b": ModelConfig("b", "y", "y", "m-b"),
            },
            default="a",
            agent_model="b",
        )
        session = _make_session(registry=reg, model_alias="a")
        session.handle_command("/model")
        info = session.ui.infos[-1]
        assert "Fallback" not in info
        assert "Agent model: b" in info


class TestSessionReopenModelBinding:
    @staticmethod
    def _reopen_with_config(
        registry: ModelRegistry,
        config: dict[str, str],
    ) -> tuple[Any, Any]:
        storage = MagicMock()
        persisted_row = {
            "ws_id": "saved-workstream",
            "user_id": "",
            "name": "saved",
            "kind": WorkstreamKind.INTERACTIVE,
            "state": "closed",
            "parent_ws_id": None,
            "project_id": None,
            "persona": "",
            "fork_reservation_token": "saved-workstream-incarnation",
        }
        storage.get_workstream.return_value = persisted_row
        storage.ensure_workstream_incarnation_snapshot.return_value = persisted_row
        storage.load_workstream_config.return_value = dict(config)
        factory_lanes: list[Any] = []

        def factory(
            ui: Any,
            model_alias: str | None = None,
            ws_id: str | None = None,
            **kwargs: Any,
        ) -> Any:
            session = _make_session(
                registry=registry,
                model_alias=model_alias or registry.default,
                kind=kwargs.get("kind", WorkstreamKind.INTERACTIVE),
                ws_id=ws_id,
                workstream_lease=kwargs.get("workstream_lease"),
            )
            session._nudges_enabled = MagicMock(return_value=False)
            factory_lanes.append(_lane(session))
            return session

        manager = _make_manager(
            factory,
            storage=storage,
            model_validator=registry.has_alias,
        )
        with (
            patch(
                "turnstone.core.session.read_message_turns",
                return_value=[Turn.user("restored")],
            ),
            patch(
                "turnstone.core.session.ChatSession._read_workstream_config",
                return_value=config,
            ),
        ):
            reopened = manager.open("saved-workstream")
        assert reopened is not None
        assert reopened.session is not None
        assert len(factory_lanes) == 1
        return reopened.session, factory_lanes[0]

    def test_deleted_saved_alias_holds_on_a_coherent_stand_in(self) -> None:
        """Rehydrate never pairs a retired model id with the default backend:
        the default binding stays whole as a stand-in, and the session is held
        on its saved model, which it reports and saves (#1359)."""
        from turnstone.core.session import _HeldModelClient

        reg = ModelRegistry(
            models={
                "default": ModelConfig(
                    "default",
                    "http://default.example/v1",
                    "k",
                    "default-model",
                    context_window=48000,
                )
            },
            default="default",
        )
        session, factory_lane = self._reopen_with_config(
            reg,
            {"model_alias": "deleted", "model": "retired-model"},
        )

        # The stand-in keeps the factory's facets, but its client refuses
        # every call, so nothing can run on the default.
        assert _lane(session).provider is factory_lane.provider
        assert _lane(session).alias == "default"
        assert _lane(session).model == "default-model"
        assert isinstance(_client(session), _HeldModelClient)
        assert _provider(session) is reg.get_provider("default")
        assert _binding(session).config is reg.get_config("default")
        assert _binding(session).registry_generation == reg.generation
        assert session.context_window == 48000
        assert session.model_held
        assert session.model_alias == "deleted"
        assert session.model == "retired-model"
        assert session.bound_model_alias == "default"
        saved = session._config_for_save()
        assert (saved["model_alias"], saved["model"]) == ("deleted", "retired-model")

    def test_a_row_saved_before_aliases_holds_until_switched(self) -> None:
        """A row naming only a backend model id can't be bound to a definition
        without guessing which one (or which provider) served it, so it holds
        with no alias instead of running on the default (#1359). Nothing
        matches an empty alias: not even a loaded alias named like the old id
        picks it up, so only /model ends the hold, and saves keep the row's
        shape."""
        from turnstone.core.session import ModelNotLoadedError

        reg = ModelRegistry(
            models={
                "default": ModelConfig(
                    "default", "http://default.example/v1", "k", "default-model"
                ),
                # Shares the old id's name, but may be any model on any provider now.
                "onprem-model": ModelConfig(
                    "onprem-model", "http://elsewhere.example/v1", "k", "another-model"
                ),
            },
            default="default",
        )
        session, _factory_lane = self._reopen_with_config(reg, {"model": "onprem-model"})

        session._refresh_model_from_registry()  # what the next send runs first
        assert session.model_held
        assert (session.model_alias, session.model) == (None, "onprem-model")
        assert session.bound_model_alias == "default"
        saved = session._config_for_save()
        assert (saved["model_alias"], saved["model"]) == ("", "onprem-model")
        with pytest.raises(ModelNotLoadedError) as exc_info:
            session._raise_if_held()
        assert "'onprem-model' was saved without a model alias" in str(exc_info.value)
        assert "/model <alias>" in str(exc_info.value)

        session.handle_command("/model default")

        assert not session.model_held
        assert session.model_alias == "default"

    def test_a_model_call_no_check_stops_never_reaches_the_stand_in(self) -> None:
        """The hold is structural: a held session's binding carries a client
        that refuses every call, so a model call made without the explicit
        hold check (a future path, the task agent) fails with the hold's
        message and never reaches the stand-in it was built on."""
        from turnstone.core.model_turn import model_turn
        from turnstone.core.session import ModelNotLoadedError

        reg = ModelRegistry(
            models={
                "default": ModelConfig(
                    "default",
                    "http://default.example/v1",
                    "k",
                    "default-model",
                    provider="openai-compatible",
                )
            },
            default="default",
        )
        stand_in_create = MagicMock()
        reg.get_client("default").chat.completions.create = stand_in_create
        session, _factory_lane = self._reopen_with_config(
            reg,
            {"model_alias": "deleted", "model": "retired-model"},
        )
        assert session.model_held

        with pytest.raises(ModelNotLoadedError) as exc_info:
            model_turn(session._primary_lane(), [Turn.user("unchecked")])
        assert "This workstream's model 'deleted' isn't loaded" in str(exc_info.value)

        _call_id, task_result = session._exec_task(
            {"call_id": "task-1", "prompt": "Complete the work."}
        )
        assert "deleted" in task_result

        stand_in_create.assert_not_called()
        # Diagnostics still read the held lane.
        assert lane_diagnostics(session._primary_lane()).alias == "default"

    def test_held_reopen_rebinds_its_own_model_when_it_loads(self) -> None:
        """The next refresh after the saved model loads again binds it in place
        of the stand-in and ends the hold, with that model's window."""
        default_cfg = ModelConfig(
            "default", "http://default.example/v1", "k", "default-model", context_window=48000
        )
        reg = ModelRegistry(models={"default": default_cfg}, default="default")
        session, _factory_lane = self._reopen_with_config(
            reg,
            {"model_alias": "returning", "model": "returning-model"},
        )
        assert session.model_held
        session._refresh_model_from_registry()  # still missing: stays held
        assert session.model_held
        assert _lane(session).alias == "default"

        returning_cfg = ModelConfig(
            "returning",
            "http://returning.example/v1",
            "k",
            "returning-model",
            context_window=96000,
        )
        reg.reload(
            {"default": default_cfg, "returning": returning_cfg},
            "default",
            app_state=_KEYED_STATE,
        )
        session._refresh_model_from_registry()

        assert not session.model_held
        assert _lane(session).alias == "returning"
        assert _client(session) is reg.get_client("returning")
        assert session.context_window == 96000
        assert session._config_for_save()["model_alias"] == "returning"

    def test_available_saved_alias_restores_coherent_saved_binding(self) -> None:
        """Rehydrate replaces the whole default binding with the saved alias."""
        reg = ModelRegistry(
            models={
                "default": ModelConfig(
                    "default",
                    "http://default.example/v1",
                    "k",
                    "default-model",
                ),
                "saved": ModelConfig(
                    "saved",
                    "http://saved.example/v1",
                    "k",
                    "saved-model",
                    context_window=64000,
                    provider="openai-compatible",
                ),
            },
            default="default",
        )
        session, factory_lane = self._reopen_with_config(
            reg,
            {"model_alias": "saved", "model": "saved-model"},
        )

        restored_binding = _binding(session)
        assert restored_binding.lane is factory_lane
        assert restored_binding.lane.alias == "saved"
        assert restored_binding.lane.model == "saved-model"
        assert restored_binding.lane.client is reg.get_client("saved")
        assert restored_binding.lane.provider is reg.get_provider("saved")
        assert restored_binding.lane.capabilities is not None
        assert restored_binding.config is reg.get_config("saved")
        assert restored_binding.registry_generation == reg.generation
        assert session.context_window == 64000


class TestSessionForkModel:
    """A fork restores as a reopen does: it continues on its source's model
    when that model is loaded and is held on it otherwise. A held fork whose
    request names a model then switches to it as ``/model`` would, with that
    model's own sampling settings (the request is the switch), unless the
    source's model is out only for now (#1359)."""

    @staticmethod
    def _models(*aliases: str) -> dict[str, ModelConfig]:
        models = {
            "default": ModelConfig("default", "http://default.example/v1", "k", "d-model"),
            "chan": ModelConfig(
                "chan",
                "http://chan.example/v1",
                "k",
                "c-model",
                temperature=0.2,
                reasoning_effort="low",
                max_tokens=4000,
            ),
            "src": ModelConfig("src", "http://src.example/v1", "k", "s-model"),
        }
        return {alias: models[alias] for alias in aliases}

    def _registry(self, *aliases: str) -> ModelRegistry:
        return ModelRegistry(models=self._models(*aliases), default="default")

    @staticmethod
    def _snapshot(alias: str, model: str | None = None) -> Any:
        from turnstone.core.storage import ForkCloneSnapshot

        return ForkCloneSnapshot(
            turns=(Turn.user("copied"),),
            config={
                "model_alias": alias,
                "model": model or f"{alias}-saved",
                "temperature": "0.9",
                "reasoning_effort": "max",
                "max_tokens": "123",
            },
            project_id=None,
        )

    @pytest.fixture
    def store(self, tmp_db: Any, sqlite_backend_factory: Any) -> Any:
        # Every production fork is built by the node's store-backed factory,
        # whose resolvers a switch re-resolves the sampling settings through.
        from turnstone.core.config_store import ConfigStore

        return ConfigStore(sqlite_backend_factory(str(tmp_db), create_tables=True))

    def _session(self, reg: ModelRegistry, build_alias: str, store: Any) -> Any:
        """A session built the way the create handler builds a fork's: on the
        source's model while it is loaded, else on the requested one."""
        session = _make_session(registry=reg, model_alias=build_alias, config_store=store)
        session._nudges_enabled = MagicMock(return_value=False)
        return session

    @staticmethod
    def _sampling(session: Any) -> tuple[Any, ...]:
        return (session.temperature, session.reasoning_effort, session.max_tokens)

    def test_without_a_requested_model_the_fork_is_held(self, store: Any) -> None:
        session = self._session(self._registry("default", "chan"), "default", store)

        session.adopt_fork_snapshot("source-ws", self._snapshot("gone"))

        assert session.model_held
        assert session.model_alias == "gone"
        assert session.model == "gone-saved"
        # The held model's own settings, for when it loads again.
        assert self._sampling(session) == (0.9, "max", 123)
        assert session._config_for_save()["model_alias"] == "gone"

    def test_a_requested_model_runs_with_its_own_settings(self, store: Any) -> None:
        session = self._session(self._registry("default", "chan"), "chan", store)

        session.adopt_fork_snapshot("source-ws", self._snapshot("gone"), switch_to="chan")

        assert not session.model_held
        assert session.model_alias == "chan"
        assert session.model == "c-model"
        # The source's settings were chosen for its model (#1272).
        assert self._sampling(session) == (0.2, "low", 4000)
        saved = session._config_for_save()
        assert (saved["model_alias"], saved["reasoning_effort"]) == ("chan", "low")

    def test_a_loaded_source_model_wins_over_the_request(self, store: Any) -> None:
        session = self._session(self._registry("default", "chan", "src"), "src", store)

        session.adopt_fork_snapshot("source-ws", self._snapshot("src"), switch_to="chan")

        assert not session.model_held
        assert session.model_alias == "src"
        assert self._sampling(session) == (0.9, "max", 123)

    def test_a_source_model_that_leaves_mid_create_gives_way_to_the_request(
        self, store: Any
    ) -> None:
        """The handler builds on the source's model while it is loaded; if a
        reload removes it before adoption, the fork switches to the loaded
        model its request names, never keeping the retired client."""
        reg = self._registry("default", "chan", "src")
        session = self._session(reg, "src", store)
        reg.reload(self._models("default", "chan"), "default", app_state=_KEYED_STATE)

        session.adopt_fork_snapshot("source-ws", self._snapshot("src"), switch_to="chan")

        assert not session.model_held
        assert session.model_alias == "chan"
        assert _client(session) is reg.get_client("chan")
        assert self._sampling(session) == (0.2, "low", 4000)

    def test_a_requested_model_that_leaves_mid_create_leaves_the_fork_held(
        self, store: Any
    ) -> None:
        """Built on the requested model, which a reload then removes: the fork
        holds on its source's model with the clone's settings instead of
        recording a model that isn't loaded."""
        reg = self._registry("default", "chan")
        session = self._session(reg, "chan", store)
        reg.reload(self._models("default"), "default", app_state=_KEYED_STATE)

        session.adopt_fork_snapshot("source-ws", self._snapshot("src"), switch_to="chan")

        assert session.model_held
        assert session.model_alias == "src"
        assert self._sampling(session) == (0.9, "max", 123)
        assert session._config_for_save()["model_alias"] == "src"

    def test_a_source_model_out_only_for_now_keeps_the_fork_held(self, store: Any) -> None:
        """Its secret store didn't answer, so it may load by itself: the fork
        waits for it rather than moving to the requested model for good."""
        reg = self._registry("default", "chan")
        reg.retryable_skips = frozenset({"src"})
        session = self._session(reg, "chan", store)

        session.adopt_fork_snapshot("source-ws", self._snapshot("src"), switch_to="chan")

        assert session.model_held
        assert session.model_alias == "src"
        assert self._sampling(session) == (0.9, "max", 123)

    def test_a_source_saved_before_aliases_holds_until_switched(self, store: Any) -> None:
        session = self._session(self._registry("default", "chan"), "default", store)

        session.adopt_fork_snapshot("source-ws", self._snapshot("", model="legacy-model"))

        assert session.model_held
        assert (session.model_alias, session.model) == (None, "legacy-model")
        assert self._sampling(session) == (0.9, "max", 123)

    def test_a_source_saved_before_aliases_takes_the_requested_model(self, store: Any) -> None:
        """A legacy source names only a backend model id, which can't be looked
        up: the request switches the fork, with the named model's settings."""
        session = self._session(self._registry("default", "chan"), "chan", store)

        session.adopt_fork_snapshot(
            "source-ws", self._snapshot("", model="legacy-model"), switch_to="chan"
        )

        assert not session.model_held
        assert session.model_alias == "chan"
        assert self._sampling(session) == (0.2, "low", 4000)

    def test_a_source_model_that_cannot_be_built_gives_way_to_the_request(
        self, store: Any, monkeypatch: Any
    ) -> None:
        """A source model listed but unbuildable can't run either: the fork
        switches to its requested model."""
        session = self._session(self._registry("default", "chan", "src"), "chan", store)
        monkeypatch.setattr(
            mr_module, "create_client", MagicMock(side_effect=FileNotFoundError("/gone/ca.pem"))
        )

        session.adopt_fork_snapshot("source-ws", self._snapshot("src"), switch_to="chan")

        assert not session.model_held
        assert session.model_alias == "chan"
        assert self._sampling(session) == (0.2, "low", 4000)

    def test_without_a_request_an_unbuildable_source_model_holds(
        self, store: Any, monkeypatch: Any
    ) -> None:
        """Nothing runs on the stand-in when the source's model is listed but
        its client can't be built: the fork holds, naming the cause."""
        from turnstone.core.session import ModelNotLoadedError

        reg = self._registry("default", "chan", "src")
        session = self._session(reg, "default", store)
        monkeypatch.setattr(
            mr_module, "create_client", MagicMock(side_effect=FileNotFoundError("/gone/ca.pem"))
        )

        session.adopt_fork_snapshot("source-ws", self._snapshot("src"))

        assert session.model_held
        assert session.model_alias == "src"
        assert session._rebind_failed_key == ("src", reg.generation)
        with pytest.raises(ModelNotLoadedError) as exc_info:
            session._utility_completion([Turn.user("summarize")])
        assert "couldn't be built" in str(exc_info.value)

    def test_a_requested_model_that_cannot_be_built_leaves_the_fork_held(
        self, store: Any, monkeypatch: Any
    ) -> None:
        reg = self._registry("default", "chan")
        session = self._session(reg, "default", store)
        monkeypatch.setattr(
            mr_module, "create_client", MagicMock(side_effect=FileNotFoundError("/gone/ca.pem"))
        )

        session.adopt_fork_snapshot("source-ws", self._snapshot("gone"), switch_to="chan")

        assert session.model_held
        assert session.model_alias == "gone"
        assert self._sampling(session) == (0.9, "max", 123)


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
        old_binding = _binding(session)
        old_lane = _lane(session)

        # Same generation + same model id: the refresh must be a no-op.
        session._refresh_model_from_registry()
        assert _binding(session) is old_binding
        assert _lane(session) is old_lane

        # In-place swap: NEW base_url, SAME backend model id — the registry
        # closes and drops the cached client.
        reg.reload(
            {"gw": ModelConfig("gw", "http://b.example/v1", "k", "test-model")},
            "gw",
            app_state=_KEYED_STATE,
        )
        session._refresh_model_from_registry()

        assert _binding(session) is not old_binding
        assert _lane(session) is not old_lane
        assert _client(session) is not old_lane.client
        assert _client(session) is reg.get_client("gw")
        assert str(_client(session).base_url) == "http://b.example/v1/"
        assert session._registry_generation == reg.generation

    def test_atomic_construction_binding_refreshes_after_reload_window(self) -> None:
        """A factory binding stays coherent across a pre-constructor reload.

        Construction receives the old snapshot as one object; the first refresh
        then replaces that whole binding with the current registry snapshot.
        """
        from turnstone.core.session import ChatSession

        reg = ModelRegistry(
            models={"gw": ModelConfig("gw", "http://a.example/v1", "k", "test-model")},
            default="gw",
        )
        factory_binding = resolve_model_binding(reg, "gw")
        # The reload lands in the construction window: same backend model
        # id, moved base_url.
        reg.reload(
            {"gw": ModelConfig("gw", "http://b.example/v1", "k", "test-model")},
            "gw",
            app_state=_KEYED_STATE,
        )
        session = ChatSession(
            client=factory_binding.lane.client,
            model=factory_binding.lane.model,
            ui=_FakeUI(),
            instructions=None,
            temperature=0.5,
            max_tokens=4096,
            tool_timeout=30,
            registry=reg,
            model_alias="gw",
            registry_generation=factory_binding.registry_generation,
            model_binding=factory_binding,
        )

        constructed_binding = _binding(session)
        assert constructed_binding.lane.client is factory_binding.lane.client
        assert constructed_binding.lane.provider is factory_binding.lane.provider
        assert constructed_binding.lane.model == factory_binding.lane.model
        assert constructed_binding.config is factory_binding.config
        assert constructed_binding.registry_generation == factory_binding.registry_generation

        session._refresh_model_from_registry()

        assert _binding(session) is not constructed_binding
        assert _client(session) is not factory_binding.lane.client
        assert _client(session) is reg.get_client("gw")
        assert _binding(session).config is reg.get_config("gw")
        assert session._registry_generation == reg.generation

    def test_constructor_rejects_binding_from_a_different_registry(self) -> None:
        """A binding and auth registry may never name different authorities."""
        from turnstone.core.session import ChatSession

        registry_a = ModelRegistry(
            models={"gw": ModelConfig("gw", "http://a.example/v1", "a", "model-a")},
            default="gw",
        )
        registry_b = ModelRegistry(
            models={"gw": ModelConfig("gw", "http://b.example/v1", "b", "model-b")},
            default="gw",
        )
        binding = resolve_model_binding(registry_a, "gw")

        with pytest.raises(ValueError, match="binding registry"):
            ChatSession(
                client=binding.lane.client,
                model=binding.lane.model,
                ui=_FakeUI(),
                instructions=None,
                temperature=0.5,
                max_tokens=4096,
                tool_timeout=30,
                registry=registry_b,
                model_alias="gw",
                model_binding=binding,
            )

    def test_constructor_rejects_duplicate_handles_that_disagree_with_binding(self) -> None:
        """Legacy constructor arguments cannot tear an atomic binding."""
        from turnstone.core.session import ChatSession

        registry = ModelRegistry(
            models={"gw": ModelConfig("gw", "http://a.example/v1", "k", "model-a")},
            default="gw",
        )
        binding = resolve_model_binding(registry, "gw")
        common = {
            "ui": _FakeUI(),
            "instructions": None,
            "temperature": 0.5,
            "max_tokens": 4096,
            "tool_timeout": 30,
            "registry": registry,
            "model_alias": "gw",
            "model_binding": binding,
        }

        with pytest.raises(ValueError, match="binding handles"):
            ChatSession(client=object(), model=binding.lane.model, **common)
        with pytest.raises(ValueError, match="binding handles"):
            ChatSession(client=binding.lane.client, model="other-model", **common)
        with pytest.raises(ValueError, match="binding alias"):
            ChatSession(
                client=binding.lane.client,
                model=binding.lane.model,
                **{**common, "model_alias": "other"},
            )

    def test_legacy_constructor_rejects_registry_handles_it_would_replace(self) -> None:
        """Omitting model_binding must not silently redirect explicit handles."""
        from turnstone.core.session import ChatSession

        registry = ModelRegistry(
            models={"gw": ModelConfig("gw", "http://a.example/v1", "k", "registry-model")},
            default="gw",
        )

        with pytest.raises(ValueError, match="explicit client/model handles"):
            ChatSession(
                client=object(),
                model="caller-model",
                ui=_FakeUI(),
                instructions=None,
                temperature=0.5,
                max_tokens=4096,
                tool_timeout=30,
                registry=registry,
                model_alias="gw",
            )

    def test_constructor_derives_registry_from_atomic_binding(self) -> None:
        """Omitting the duplicate registry argument keeps auth on binding A."""
        from turnstone.core.session import ChatSession

        registry = ModelRegistry(
            models={"gw": ModelConfig("gw", "http://a.example/v1", "k", "model-a")},
            default="gw",
        )
        binding = resolve_model_binding(registry, "gw")

        session = ChatSession(
            client=binding.lane.client,
            model=binding.lane.model,
            ui=_FakeUI(),
            instructions=None,
            temperature=0.5,
            max_tokens=4096,
            tool_timeout=30,
            model_alias="gw",
            model_binding=binding,
        )

        assert session._registry is registry
        assert _binding(session).lane.registry is registry
        assert _binding(session).config is binding.config

    def test_primary_lane_derivation_cannot_overwrite_a_concurrent_rebind(self) -> None:
        """Sampling projection is read-only even when a reload lands inside it."""
        reg = ModelRegistry(
            models={"gw": ModelConfig("gw", "http://a.example/v1", "k", "test-model")},
            default="gw",
        )
        session = _make_session(registry=reg, model_alias="gw")
        old_binding = _binding(session)
        old_lane = _lane(session)
        session.temperature = 0.75
        reg.reload(
            {"gw": ModelConfig("gw", "http://b.example/v1", "k", "test-model")},
            "gw",
            app_state=_KEYED_STATE,
        )

        real_replace = dataclasses.replace
        rebind_landed = False

        def interleaved_replace(value: Any, /, **changes: Any) -> Any:
            nonlocal rebind_landed
            if value is old_lane and not rebind_landed:
                rebind_landed = True
                bind_result = session._bind_model_from_registry("gw")
                assert bind_result is not None
            return real_replace(value, **changes)

        with patch("turnstone.core.session.dataclasses.replace", side_effect=interleaved_replace):
            derived = session._primary_lane()

        current = _binding(session)
        assert rebind_landed is True
        assert derived.client is old_lane.client
        assert derived.temperature == 0.75
        assert current is not old_binding
        assert current.lane is not old_lane
        assert current.lane.client is reg.get_client("gw")
        assert str(current.lane.client.base_url) == "http://b.example/v1/"
        assert current.config is reg.get_config("gw")
        assert current.registry_generation == reg.generation
        assert session._primary_lane().client is current.lane.client

    def test_concurrent_rebinds_publish_in_registry_order(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A delayed old resolver cannot overwrite a newer binding snapshot."""
        import turnstone.core.session as session_module

        reg = ModelRegistry(
            models={"gw": ModelConfig("gw", "http://a.example/v1", "k", "test-model")},
            default="gw",
        )
        session = _make_session(registry=reg, model_alias="gw")
        original_resolve = session_module.resolve_model_binding
        old_resolved = threading.Event()
        release_old = threading.Event()
        second_entered_resolver = threading.Event()
        calls_lock = threading.Lock()
        calls = 0

        def delayed_resolve(*args: Any, **kwargs: Any) -> Any:
            nonlocal calls
            candidate = original_resolve(*args, **kwargs)
            with calls_lock:
                calls += 1
                call_number = calls
            if call_number == 1:
                old_resolved.set()
                assert release_old.wait(2.0)
            else:
                second_entered_resolver.set()
            return candidate

        monkeypatch.setattr(session_module, "resolve_model_binding", delayed_resolve)
        first = threading.Thread(target=session._bind_model_from_registry, args=("gw",))
        first.start()
        assert old_resolved.wait(2.0)

        reg.reload(
            {"gw": ModelConfig("gw", "http://b.example/v1", "k", "test-model")},
            "gw",
            app_state=_KEYED_STATE,
        )
        second = threading.Thread(target=session._bind_model_from_registry, args=("gw",))
        second.start()

        # The second resolver cannot pass the session publication lock while
        # the first candidate is paused.  Without serialization it publishes
        # generation 1 and the delayed generation 0 overwrites it afterward.
        assert not second_entered_resolver.wait(0.1)
        release_old.set()
        first.join(2.0)
        second.join(2.0)

        assert not first.is_alive()
        assert not second.is_alive()
        assert second_entered_resolver.is_set()
        assert session._registry_generation == reg.generation
        assert str(_client(session).base_url) == "http://b.example/v1/"

    def test_stale_refresh_cannot_overwrite_explicit_cross_alias_switch(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A refresh CAS is invalid once /model replaces its observed binding."""
        reg = ModelRegistry(
            models={
                "a": ModelConfig(
                    "a",
                    "http://a.example/v1",
                    "k",
                    "model-a",
                    context_window=11_111,
                ),
                "b": ModelConfig(
                    "b",
                    "http://b.example/v1",
                    "k",
                    "model-b",
                    context_window=22_222,
                ),
            },
            default="a",
        )
        session = _make_session(registry=reg, model_alias="a")
        reg.reload(
            {
                "a": ModelConfig(
                    "a",
                    "http://a-new.example/v1",
                    "k",
                    "model-a",
                    context_window=33_333,
                ),
                "b": reg.get_config("b"),
            },
            "a",
            app_state=_KEYED_STATE,
        )
        real_bind = session._bind_model_from_registry
        refresh_waiting = threading.Event()
        release_refresh = threading.Event()

        def delayed_refresh_bind(alias: str, **kwargs: Any) -> Any:
            if kwargs.get("expected_binding") is not None:
                refresh_waiting.set()
                assert release_refresh.wait(2.0)
            return real_bind(alias, **kwargs)

        monkeypatch.setattr(session, "_bind_model_from_registry", delayed_refresh_bind)
        refresh = threading.Thread(target=session._refresh_model_from_registry)
        refresh.start()
        assert refresh_waiting.wait(2.0)

        session.handle_command("/model b")
        release_refresh.set()
        refresh.join(2.0)

        assert not refresh.is_alive()
        assert session.model_alias == "b"
        assert session.model == "model-b"
        assert session.context_window == 22_222
        assert str(_client(session).base_url) == "http://b.example/v1/"

    def test_alias_deletion_race_keeps_old_binding_without_raise(self) -> None:
        """A deletion landing mid-rebind must neither raise out of send nor
        half-swap; the next refresh self-heals."""
        reg = ModelRegistry(
            models={"gw": ModelConfig("gw", "http://a.example/v1", "k", "test-model")},
            default="gw",
        )
        session = _make_session(registry=reg, model_alias="gw")
        old_binding = _binding(session)

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

        assert _binding(session) is old_binding
        assert session.model == "test-model"

        # Unpatched, the next send's refresh completes the rebind.
        session._refresh_model_from_registry()
        assert _binding(session) is not old_binding
        assert _client(session) is reg.get_client("gw")
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

        old_lane = _lane(session)
        bind_result = session._bind_model_from_registry("gw")

        assert bind_result is not None
        cfg, binding_changed = bind_result
        assert cfg is reg.get_config("gw")
        assert binding_changed is False
        assert _lane(session) is old_lane
        assert _client(session) is reg._clients["gw"]
        assert _provider(session) is reg._providers["gw"]
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
        assert _client(session) is reg.get_client("b")
        assert session._registry_generation == reg.generation

    def test_explicit_intent_judge_refreshes_only_when_its_alias_changes(self) -> None:
        """Judge freshness follows its explicit alias, not registry churn.

        The primary binding stays byte-identical throughout.  An unrelated
        alias edit must retain the cached judge and its pinned lane, while an
        edit to ``judge.model``'s alias replaces the judge at the next
        ``_ensure_judge`` evaluation boundary.
        """
        from turnstone.core.judge import JudgeConfig

        reg = ModelRegistry(
            models={
                "gw": ModelConfig("gw", "http://primary.example/v1", "k", "primary-model"),
                "intent": ModelConfig("intent", "http://intent-a.example/v1", "k", "intent-model"),
                "other": ModelConfig("other", "http://other-a.example/v1", "k", "other-model"),
            },
            default="gw",
        )
        session = _make_session(
            registry=reg,
            model_alias="gw",
            judge_config=JudgeConfig(model="intent"),
        )
        primary_lane = _lane(session)
        original = session._ensure_judge()
        assert original is not None
        original_judge_lane = original._lane

        reg.reload(
            {
                "gw": ModelConfig("gw", "http://primary.example/v1", "k", "primary-model"),
                "intent": ModelConfig("intent", "http://intent-a.example/v1", "k", "intent-model"),
                "other": ModelConfig("other", "http://other-b.example/v1", "k", "other-model"),
            },
            "gw",
            app_state=_KEYED_STATE,
        )
        session._refresh_model_from_registry()

        assert _lane(session) is primary_lane
        assert session._ensure_judge() is original
        assert original._lane is original_judge_lane

        reg.reload(
            {
                "gw": ModelConfig("gw", "http://primary.example/v1", "k", "primary-model"),
                "intent": ModelConfig("intent", "http://intent-b.example/v1", "k", "intent-model"),
                "other": ModelConfig("other", "http://other-b.example/v1", "k", "other-model"),
            },
            "gw",
            app_state=_KEYED_STATE,
        )
        session._refresh_model_from_registry()
        replacement = session._ensure_judge()

        assert _lane(session) is primary_lane
        assert replacement is not None
        assert replacement is not original
        assert replacement._lane is not original_judge_lane
        assert replacement._lane.alias == "intent"
        assert str(replacement._lane.client.base_url) == "http://intent-b.example/v1/"

    def test_live_output_guard_alias_replaces_only_guard_and_resets_limiter(
        self, tmp_db: Any, sqlite_backend_factory
    ) -> None:
        """A live guard-route edit is selective and restores its budget.

        An unrelated registry generation first proves that both cached judges
        and the partially consumed limiter survive.  Changing only
        ``judge.output_guard_model`` then replaces the guard, leaves the intent
        judge pinned, and installs a full limiter for the new guard model.
        """
        from turnstone.core.config_store import ConfigStore
        from turnstone.core.judge import JudgeConfig

        storage = sqlite_backend_factory(str(tmp_db), create_tables=True)
        config_store = ConfigStore(storage)
        config_store.set("judge.output_guard_llm", True, changed_by="test")
        config_store.set("judge.output_guard_model", "guard-a", changed_by="test")

        reg = ModelRegistry(
            models={
                "gw": ModelConfig("gw", "http://primary.example/v1", "k", "primary-model"),
                "intent": ModelConfig("intent", "http://intent.example/v1", "k", "intent-model"),
                "guard-a": ModelConfig(
                    "guard-a", "http://guard-a.example/v1", "k", "guard-a-model"
                ),
                "guard-b": ModelConfig(
                    "guard-b", "http://guard-b.example/v1", "k", "guard-b-model"
                ),
                "other": ModelConfig("other", "http://other-a.example/v1", "k", "other-model"),
            },
            default="gw",
        )
        session = _make_session(
            registry=reg,
            model_alias="gw",
            judge_config=JudgeConfig(
                model="intent",
                output_guard_llm=True,
                output_guard_model="guard-a",
            ),
            config_store=config_store,
        )
        intent = session._ensure_judge()
        guard = session._ensure_output_guard_judge()
        assert intent is not None
        assert guard is not None
        guard_retire = MagicMock(wraps=guard.retire)
        guard.retire = guard_retire
        limiter = session._output_guard_judge_rl
        cancel_event = session._output_guard_judge_cancel
        for _ in range(5):
            assert limiter.consume()
        assert limiter.tokens < limiter.burst

        reg.reload(
            {
                "gw": ModelConfig("gw", "http://primary.example/v1", "k", "primary-model"),
                "intent": ModelConfig("intent", "http://intent.example/v1", "k", "intent-model"),
                "guard-a": ModelConfig(
                    "guard-a", "http://guard-a.example/v1", "k", "guard-a-model"
                ),
                "guard-b": ModelConfig(
                    "guard-b", "http://guard-b.example/v1", "k", "guard-b-model"
                ),
                "other": ModelConfig("other", "http://other-b.example/v1", "k", "other-model"),
            },
            "gw",
            app_state=_KEYED_STATE,
        )
        session._refresh_model_from_registry()

        assert session._ensure_judge() is intent
        assert session._ensure_output_guard_judge() is guard
        assert session._output_guard_judge_rl is limiter
        assert limiter.tokens < limiter.burst

        config_store.set("judge.output_guard_model", "guard-b", changed_by="test")
        replacement = session._ensure_output_guard_judge()
        replacement_limiter = session._output_guard_judge_rl

        assert session._ensure_judge() is intent
        assert replacement is not None
        assert replacement is not guard
        assert replacement._lane.alias == "guard-b"
        guard_retire.assert_called_once_with()
        assert replacement_limiter is not limiter
        assert replacement_limiter.tokens == replacement_limiter.burst
        assert cancel_event is not None
        assert cancel_event.is_set()
        assert session._output_guard_judge_cancel is not cancel_event

    def test_live_output_guard_timeout_replaces_frozen_guard(
        self, tmp_db: Any, sqlite_backend_factory
    ) -> None:
        """A timeout-only admin edit cannot leave the old JudgeConfig cached."""
        from turnstone.core.config_store import ConfigStore
        from turnstone.core.judge import JudgeConfig

        storage = sqlite_backend_factory(str(tmp_db), create_tables=True)
        config_store = ConfigStore(storage)
        config_store.set("judge.output_guard_llm", True, changed_by="test")
        config_store.set("judge.output_guard_llm_timeout", 12.0, changed_by="test")
        reg = ModelRegistry(
            models={"gw": ModelConfig("gw", "http://primary.example/v1", "k", "model")},
            default="gw",
        )
        session = _make_session(
            registry=reg,
            model_alias="gw",
            judge_config=JudgeConfig(output_guard_llm=True, output_guard_llm_timeout=12.0),
            config_store=config_store,
        )
        guard = session._ensure_output_guard_judge()
        assert guard is not None
        assert guard._config.output_guard_llm_timeout == 12.0
        retire = MagicMock(wraps=guard.retire)
        guard.retire = retire
        cancel_event = session._output_guard_judge_cancel
        limiter = session._output_guard_judge_rl

        original_is_current = guard.binding_is_current
        updated_during_check = False

        def update_timeout_during_check(binding: Any, config: JudgeConfig) -> bool:
            nonlocal updated_during_check
            if not updated_during_check:
                updated_during_check = True
                config_store.set("judge.output_guard_llm_timeout", 7.0, changed_by="test")
            return original_is_current(binding, config)

        guard.binding_is_current = update_timeout_during_check  # type: ignore[method-assign]
        replacement = session._ensure_output_guard_judge()

        assert updated_during_check is True
        assert replacement is not None
        assert replacement is not guard
        assert replacement._config.output_guard_llm_timeout == 7.0
        retire.assert_called_once_with()
        assert cancel_event is not None
        assert cancel_event.is_set()
        assert session._output_guard_judge_rl is not limiter

    def test_stop_cancels_and_rotates_output_guard_generation(self) -> None:
        """Stop aborts a guard request without poisoning the next send."""
        from turnstone.core.judge import JudgeConfig

        reg = ModelRegistry(
            models={"gw": ModelConfig("gw", "http://primary.example/v1", "k", "model")},
            default="gw",
        )
        session = _make_session(
            registry=reg,
            model_alias="gw",
            judge_config=JudgeConfig(output_guard_llm=True),
        )
        guard = session._ensure_output_guard_judge()
        assert guard is not None
        cancel_event = session._output_guard_judge_cancel
        assert cancel_event is not None
        limiter = session._output_guard_judge_rl
        assert limiter.consume() is True
        remaining_tokens = limiter.tokens
        retire = MagicMock(wraps=guard.retire)
        guard.retire = retire

        session.cancel()

        assert cancel_event.is_set()
        retire.assert_called_once_with()
        assert session._output_guard_judge is None
        assert session._output_guard_judge_cancel is None
        assert session._output_guard_judge_rl is limiter
        assert limiter.tokens == remaining_tokens

        session._claim_generation()
        replacement = session._ensure_output_guard_judge()
        assert replacement is not None
        assert replacement is not guard
        replacement_cancel = session._output_guard_judge_cancel
        assert replacement_cancel is not None
        assert not replacement_cancel.is_set()
        assert session._output_guard_judge_rl is limiter

    def test_close_cancels_retires_and_cannot_resurrect_output_guard(self) -> None:
        """Session teardown aborts the exact installed guard generation."""
        from turnstone.core.judge import JudgeConfig

        reg = ModelRegistry(
            models={"gw": ModelConfig("gw", "http://primary.example/v1", "k", "model")},
            default="gw",
        )
        session = _make_session(
            registry=reg,
            model_alias="gw",
            judge_config=JudgeConfig(output_guard_llm=True),
        )
        guard = session._ensure_output_guard_judge()
        assert guard is not None
        cancel_event = session._output_guard_judge_cancel
        assert cancel_event is not None
        retire = MagicMock(wraps=guard.retire)
        guard.retire = retire

        session.close()

        assert cancel_event.is_set()
        retire.assert_called_once_with()
        assert session._output_guard_judge is None
        assert session._ensure_output_guard_judge() is None

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
        session = _make_session(registry=reg, model_alias="gw")
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

    def test_noop_rebind_keeps_exact_lane_and_capabilities(self, caplog: Any) -> None:
        """A no-op keeps the exact lane; a real swap commits a new one."""
        import logging

        caps_override = {"supports_web_search": False}
        reg = ModelRegistry(
            models={
                "gw": ModelConfig(
                    "gw",
                    "http://a.example/v1",
                    "k",
                    "test-model",
                    capabilities=dict(caps_override),
                ),
                "other": ModelConfig("other", "http://o.example/v1", "k", "o-model"),
            },
            default="gw",
        )
        session = _make_session(registry=reg, model_alias="gw")
        old_lane = _lane(session)
        old_caps = old_lane.capabilities
        assert old_caps is not None

        reg.reload(
            {
                "gw": ModelConfig(
                    "gw",
                    "http://a.example/v1",
                    "k",
                    "test-model",
                    capabilities=dict(caps_override),
                ),
                "other": ModelConfig("other", "http://moved.example/v1", "k", "o-model"),
            },
            "gw",
            app_state=_KEYED_STATE,
        )
        with caplog.at_level(logging.INFO):
            session._refresh_model_from_registry()

        assert session._registry_generation == reg.generation  # stamped anyway
        assert not any("model_updated" in r.getMessage() for r in caplog.records)
        assert _lane(session) is old_lane
        assert _lane(session).capabilities is old_caps

        # Contrast: a swap that moves THIS alias's connection target logs.
        reg.reload(
            {
                "gw": ModelConfig(
                    "gw",
                    "http://b.example/v1",
                    "k",
                    "test-model",
                    capabilities=dict(caps_override),
                ),
                "other": ModelConfig("other", "http://moved.example/v1", "k", "o-model"),
            },
            "gw",
            app_state=_KEYED_STATE,
        )
        with caplog.at_level(logging.INFO):
            session._refresh_model_from_registry()
        assert any("model_updated" in r.getMessage() for r in caplog.records)
        assert _lane(session) is not old_lane
        assert _lane(session).capabilities is not old_caps

    def test_reload_changing_sessions_alias_still_resets_judges(self) -> None:
        """The gate is "binding actually changed", not "never reset": moving
        this session's alias must drop the judges and the limiter."""
        reg = ModelRegistry(
            models={"gw": ModelConfig("gw", "http://a.example/v1", "k", "test-model")},
            default="gw",
        )
        session = _make_session(registry=reg, model_alias="gw")
        session._bind_model_from_registry("gw")
        output_guard = MagicMock()
        session._output_guard_judge = output_guard
        output_guard_cancel = threading.Event()
        session._output_guard_judge_cancel = output_guard_cancel
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
        assert output_guard_cancel.is_set()
        output_guard.retire.assert_called_once_with()
        assert session._output_guard_judge_rl is not limiter

    def test_rebind_during_intent_judge_construction_cannot_publish_stale_candidate(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A constructor that captured lane A cannot publish after lane B wins."""
        from turnstone.core.judge import JudgeConfig

        reg = ModelRegistry(
            models={"gw": ModelConfig("gw", "http://a.example/v1", "k", "test-model")},
            default="gw",
        )
        session = _make_session(
            registry=reg,
            model_alias="gw",
            judge_config=JudgeConfig(),
        )
        captured = threading.Event()
        release = threading.Event()
        instances: list[Any] = []

        class _BlockingIntentJudge:
            def __init__(self, *, session_binding: Any, **_kwargs: Any) -> None:
                self.binding = session_binding
                instances.append(self)
                if len(instances) == 1:
                    captured.set()
                    assert release.wait(2.0)

            def binding_is_current(self, binding: Any, _config: Any = None) -> bool:
                return self.binding is binding

        monkeypatch.setattr("turnstone.core.judge.IntentJudge", _BlockingIntentJudge)
        results: list[Any] = []
        worker = threading.Thread(target=lambda: results.append(session._ensure_judge()))
        worker.start()
        assert captured.wait(2.0)

        reg.reload(
            {"gw": ModelConfig("gw", "http://b.example/v1", "k", "test-model")},
            "gw",
            app_state=_KEYED_STATE,
        )
        bind = session._bind_model_from_registry("gw")
        assert bind is not None
        rebound = _binding(session)
        release.set()
        worker.join(2.0)

        assert not worker.is_alive()
        assert len(instances) == 2
        assert instances[0].binding is not rebound
        assert results == [instances[1]]
        assert session._judge is instances[1]
        assert instances[1].binding is rebound
        assert instances[1].binding_is_current(session._model_binding)

    def test_intent_alias_reload_during_construction_retries_before_publication(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An explicit judge alias reload is visible without a primary rebind."""
        import turnstone.core.judge as judge_module
        from turnstone.core.judge import JudgeConfig

        reg = ModelRegistry(
            models={
                "gw": ModelConfig("gw", "http://primary.example/v1", "k", "primary"),
                "intent": ModelConfig("intent", "http://intent-a.example/v1", "k", "judge"),
            },
            default="gw",
        )
        session = _make_session(
            registry=reg,
            model_alias="gw",
            judge_config=JudgeConfig(model="intent"),
        )
        primary_binding = _binding(session)
        captured = threading.Event()
        release = threading.Event()
        real_resolve = judge_module.resolve_model_binding
        intent_resolutions = 0

        def delayed_resolve(*args: Any, **kwargs: Any) -> Any:
            nonlocal intent_resolutions
            candidate = real_resolve(*args, **kwargs)
            alias = args[1] if len(args) > 1 else kwargs.get("alias")
            if alias == "intent":
                intent_resolutions += 1
                if intent_resolutions == 1:
                    captured.set()
                    assert release.wait(2.0)
            return candidate

        monkeypatch.setattr(judge_module, "resolve_model_binding", delayed_resolve)
        results: list[Any] = []
        worker = threading.Thread(target=lambda: results.append(session._ensure_judge()))
        worker.start()
        assert captured.wait(2.0)

        reg.reload(
            {
                "gw": ModelConfig("gw", "http://primary.example/v1", "k", "primary"),
                "intent": ModelConfig("intent", "http://intent-b.example/v1", "k", "judge"),
            },
            "gw",
            app_state=_KEYED_STATE,
        )
        release.set()
        worker.join(2.0)

        assert not worker.is_alive()
        assert _binding(session) is primary_binding
        assert len(results) == 1
        judge = results[0]
        assert judge is not None
        assert judge is session._judge
        assert str(judge._lane.client.base_url) == "http://intent-b.example/v1/"
        assert intent_resolutions >= 3

    def test_intent_alias_reload_after_candidate_check_retries_before_publication(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The publication lock rechecks an independently routed candidate."""
        from turnstone.core.judge import IntentJudge, JudgeConfig

        reg = ModelRegistry(
            models={
                "gw": ModelConfig("gw", "http://primary.example/v1", "k", "primary"),
                "intent": ModelConfig("intent", "http://intent-a.example/v1", "k", "judge"),
            },
            default="gw",
        )
        session = _make_session(
            registry=reg,
            model_alias="gw",
            judge_config=JudgeConfig(model="intent"),
        )
        primary_binding = _binding(session)
        checked = threading.Event()
        release_check = threading.Event()
        real_is_current = IntentJudge.binding_is_current
        check_calls = 0

        def pause_after_first_check(
            judge: IntentJudge,
            binding: Any,
            config: JudgeConfig | None = None,
        ) -> bool:
            nonlocal check_calls
            result = real_is_current(judge, binding, config)
            check_calls += 1
            if check_calls == 1:
                checked.set()
                assert release_check.wait(2.0)
            return result

        monkeypatch.setattr(IntentJudge, "binding_is_current", pause_after_first_check)
        results: list[Any] = []
        worker = threading.Thread(target=lambda: results.append(session._ensure_judge()))
        worker.start()
        assert checked.wait(2.0)

        with session._model_binding_lock:
            release_check.set()
            reg.reload(
                {
                    "gw": ModelConfig("gw", "http://primary.example/v1", "k", "primary"),
                    "intent": ModelConfig("intent", "http://intent-b.example/v1", "k", "judge"),
                },
                "gw",
                app_state=_KEYED_STATE,
            )
        worker.join(2.0)

        assert not worker.is_alive()
        assert _binding(session) is primary_binding
        assert len(results) == 1
        judge = results[0]
        assert judge is not None
        assert str(judge._lane.client.base_url) == "http://intent-b.example/v1/"
        assert check_calls >= 3

    def test_intent_alias_reload_after_cached_check_replaces_before_reuse(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A cached judge is revalidated after waiting for publication."""
        from turnstone.core.judge import IntentJudge, JudgeConfig

        reg = ModelRegistry(
            models={
                "gw": ModelConfig("gw", "http://primary.example/v1", "k", "primary"),
                "intent": ModelConfig("intent", "http://intent-a.example/v1", "k", "judge"),
            },
            default="gw",
        )
        session = _make_session(
            registry=reg,
            model_alias="gw",
            judge_config=JudgeConfig(model="intent"),
        )
        original = session._ensure_judge()
        assert original is not None
        checked = threading.Event()
        release_check = threading.Event()
        real_is_current = IntentJudge.binding_is_current
        check_calls = 0

        def pause_after_first_check(
            judge: IntentJudge,
            binding: Any,
            config: JudgeConfig | None = None,
        ) -> bool:
            nonlocal check_calls
            result = real_is_current(judge, binding, config)
            check_calls += 1
            if check_calls == 1:
                checked.set()
                assert release_check.wait(2.0)
            return result

        monkeypatch.setattr(IntentJudge, "binding_is_current", pause_after_first_check)
        results: list[Any] = []
        worker = threading.Thread(target=lambda: results.append(session._ensure_judge()))
        worker.start()
        assert checked.wait(2.0)

        with session._model_binding_lock:
            release_check.set()
            reg.reload(
                {
                    "gw": ModelConfig("gw", "http://primary.example/v1", "k", "primary"),
                    "intent": ModelConfig("intent", "http://intent-b.example/v1", "k", "judge"),
                },
                "gw",
                app_state=_KEYED_STATE,
            )
        worker.join(2.0)

        assert not worker.is_alive()
        assert len(results) == 1
        replacement = results[0]
        assert replacement is not None
        assert replacement is not original
        assert str(replacement._lane.client.base_url) == "http://intent-b.example/v1/"
        assert check_calls >= 3

    def test_output_guard_alias_reload_during_construction_retries_before_publication(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A guard alias reload cannot admit one request on a retired lane."""
        import turnstone.core.output_guard_judge as guard_module
        from turnstone.core.judge import JudgeConfig

        reg = ModelRegistry(
            models={
                "gw": ModelConfig("gw", "http://primary.example/v1", "k", "primary"),
                "guard": ModelConfig("guard", "http://guard-a.example/v1", "k", "judge"),
            },
            default="gw",
        )
        session = _make_session(
            registry=reg,
            model_alias="gw",
            judge_config=JudgeConfig(output_guard_llm=True, output_guard_model="guard"),
        )
        primary_binding = _binding(session)
        captured = threading.Event()
        release = threading.Event()
        real_resolve = guard_module.resolve_model_binding
        guard_resolutions = 0

        def delayed_resolve(*args: Any, **kwargs: Any) -> Any:
            nonlocal guard_resolutions
            candidate = real_resolve(*args, **kwargs)
            alias = args[1] if len(args) > 1 else kwargs.get("alias")
            if alias == "guard":
                guard_resolutions += 1
                if guard_resolutions == 1:
                    captured.set()
                    assert release.wait(2.0)
            return candidate

        monkeypatch.setattr(guard_module, "resolve_model_binding", delayed_resolve)
        results: list[Any] = []
        worker = threading.Thread(
            target=lambda: results.append(session._ensure_output_guard_judge())
        )
        worker.start()
        assert captured.wait(2.0)

        reg.reload(
            {
                "gw": ModelConfig("gw", "http://primary.example/v1", "k", "primary"),
                "guard": ModelConfig("guard", "http://guard-b.example/v1", "k", "judge"),
            },
            "gw",
            app_state=_KEYED_STATE,
        )
        release.set()
        worker.join(2.0)

        assert not worker.is_alive()
        assert _binding(session) is primary_binding
        assert len(results) == 1
        guard = results[0]
        assert guard is not None
        assert guard is session._output_guard_judge
        assert str(guard._lane.client.base_url) == "http://guard-b.example/v1/"
        assert guard_resolutions == 2


class TestSessionModelHold:
    """A live session whose model a reload removed is HELD on it (#1359): its
    turns refuse before any model call, with a message naming the model and
    the remedy for that surface, and nothing runs on another model. The
    session keeps reporting and saving its own model, and rebinds by itself
    once the registry lists the model again.
    """

    @staticmethod
    def _dead_client_error() -> RuntimeError:
        return RuntimeError("Cannot send a request, as the client has been closed.")

    # provider="openai-compatible" pins the Chat Completions surface, the
    # one the patched ``chat.completions.create`` stubs below speak.
    def _registry(self) -> ModelRegistry:
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
        )

    def _delete_gw(self, reg: ModelRegistry) -> None:
        reg.reload(
            {
                "other": ModelConfig(
                    "other", "http://o.example/v1", "k", "o-model", provider="openai-compatible"
                )
            },
            "other",
            app_state=_KEYED_STATE,
        )

    def test_removed_alias_holds_the_turn_before_any_call(self, tmp_db: str, caplog: Any) -> None:
        """The send records the message, then refuses without dialing the
        retired client or any other model."""
        import logging

        from turnstone.core.session import ModelNotLoadedError

        reg = self._registry()
        session = _make_durable_session(registry=reg, model_alias="gw")
        dead_create = MagicMock(side_effect=self._dead_client_error())
        _client(session).chat.completions.create = dead_create
        other_create = MagicMock()
        reg.get_client("other").chat.completions.create = other_create

        self._delete_gw(reg)
        with caplog.at_level(logging.WARNING), pytest.raises(ModelNotLoadedError):
            session.send("hello")
        session._refresh_model_from_registry()  # repeat: the warning stays deduped

        dead_create.assert_not_called()
        other_create.assert_not_called()
        assert session.model_held
        assert session.model_alias == "gw"
        assert session.model == "test-model"
        assert session.messages[-1].role == "user"  # kept for /retry
        message = session.ui.errors[-1]
        assert "This workstream's model 'gw' isn't loaded" in message
        assert "/model" in message  # interactive lanes route slash commands
        assert "'other'" not in message  # never steers to another model
        assert "Available" not in message
        held_warns = [r for r in caplog.records if "session.model_held" in r.getMessage()]
        assert len(held_warns) == 1  # once per (alias, generation)

    def test_coordinator_hold_names_slash_model_remedy(self, tmp_db: str) -> None:
        """A coordinator's composer runs ``/model``, so its refusal names it,
        and the ``/model`` readout repeats the refusal. Built WEB, as the
        console factory builds every coordinator."""
        from turnstone.core.session import ModelNotLoadedError

        reg = self._registry()
        session = _make_durable_session(
            registry=reg,
            model_alias="gw",
            kind=WorkstreamKind.COORDINATOR,
            user_id="u1",
            client_type=ClientType.WEB,
        )
        _client(session).chat.completions.create = MagicMock(side_effect=self._dead_client_error())

        self._delete_gw(reg)
        with pytest.raises(ModelNotLoadedError):
            session.send("hello")

        message = session.ui.errors[-1]
        assert "This coordinator's model 'gw' isn't loaded" in message
        assert "Switch to another model with /model <alias>, or ask an admin" in message
        session.handle_command("/model")
        assert message in session.ui.infos[-1]

    def test_a_secret_store_outage_suggests_trying_again(self, tmp_db: str) -> None:
        from turnstone.core.session import ModelNotLoadedError

        reg = self._registry()
        session = _make_durable_session(registry=reg, model_alias="gw", client_type=ClientType.WEB)
        self._delete_gw(reg)
        reg.retryable_skips = frozenset({"gw"})

        with pytest.raises(ModelNotLoadedError) as exc_info:
            session.send("hello")

        assert exc_info.value.retryable
        message = session.ui.errors[-1]
        assert "secret store" in message
        assert "Try again shortly" in message

    @pytest.mark.parametrize(
        ("client_type", "child", "node", "retryable", "says", "never_says"),
        [
            (
                ClientType.WEB,
                False,
                True,
                False,
                [
                    "This workstream's model 'gw' isn't loaded",
                    "Switch to another model with /model <alias>, or ask an admin",
                ],
                [],
            ),
            (
                ClientType.WEB,
                False,
                True,
                True,
                ["Try again shortly, or switch to another model with /model <alias>."],
                ["admin"],
            ),
            # Chat routes no slash commands and has no /retry: the channels' own
            # advice, and the message is kept, so it needn't be sent again.
            (
                ClientType.CHAT,
                False,
                True,
                False,
                [
                    "This conversation's model 'gw' isn't loaded",
                    f"{CHAT_MODEL_REMEDY}; your message is kept",
                ],
                ["/model", "try again", "Try again"],
            ),
            (
                ClientType.CHAT,
                False,
                True,
                True,
                ["It should load again shortly; your message is kept"],
                ["/model", "Try again", "admin"],
            ),
            # Unattended: nobody can run /model in the run.
            (
                ClientType.SCHEDULED,
                False,
                True,
                False,
                ["This workstream's model 'gw'", "Ask an admin to check its definition."],
                ["/model"],
            ),
            (ClientType.SCHEDULED, False, True, True, ["Try again shortly."], ["/model", "admin"]),
            # A coordinator's child: the coordinator's model reads its errors and
            # must never be steered to another model.
            (
                ClientType.WEB,
                True,
                True,
                False,
                ["Ask an admin to check its definition."],
                ["/model", "switch"],
            ),
            (ClientType.WEB, True, True, True, ["Try again shortly."], ["/model", "switch"]),
            # The CLI process reads its models from config.toml once: waiting
            # never helps.
            (
                ClientType.CLI,
                False,
                False,
                False,
                ["/model <alias>", "check [models.gw] in config.toml and restart."],
                ["shortly", "admin"],
            ),
            (
                ClientType.CLI,
                False,
                False,
                True,
                ["restart to load it again."],
                ["shortly", "admin"],
            ),
            # A node workstream labelled cli: its models are the node's.
            (
                ClientType.CLI,
                False,
                True,
                False,
                ["Switch to another model with /model <alias>, or ask an admin"],
                ["config.toml", "restart"],
            ),
        ],
    )
    def test_each_surface_gets_a_remedy_it_can_use(
        self,
        client_type: ClientType,
        child: bool,
        node: bool,
        retryable: bool,
        says: list[str],
        never_says: list[str],
        tmp_db: Any,
        sqlite_backend_factory: Any,
    ) -> None:
        """The refusal's remedy fits whoever reads it, and ``/model`` prints the
        same text."""
        from turnstone.core.config_store import ConfigStore
        from turnstone.core.session import ModelNotLoadedError

        reg = self._registry()
        session = _make_session(
            registry=reg,
            model_alias="gw",
            client_type=client_type,
            parent_ws_id="coordinator-ws" if child else None,
            config_store=(
                ConfigStore(sqlite_backend_factory(str(tmp_db), create_tables=True))
                if node
                else None
            ),
        )
        self._delete_gw(reg)
        if retryable:
            reg.retryable_skips = frozenset({"gw"})
        session._refresh_model_from_registry()

        with pytest.raises(ModelNotLoadedError) as exc_info:
            session._utility_completion([Turn.user("summarize")])
        message = str(exc_info.value)
        for text in says:
            assert text in message
        for text in never_says:
            assert text not in message
        assert "'other'" not in message
        session.handle_command("/model")
        assert message in session.ui.infos[-1]

    def test_the_cli_names_a_dotted_alias_as_config_toml_writes_it(self) -> None:
        """Unquoted, a dot would name a nested table that doesn't exist."""
        from turnstone.core.model_registry import models_table

        assert models_table("gw") == "[models.gw]"
        assert models_table("gpt-4.1") == '[models."gpt-4.1"]'

    def test_saves_keep_the_held_model(self) -> None:
        reg = self._registry()
        session = _make_session(registry=reg, model_alias="gw")
        self._delete_gw(reg)
        session._refresh_model_from_registry()

        saved = session._config_for_save()
        assert saved["model_alias"] == "gw"
        assert saved["model"] == "test-model"

    def test_model_switch_ends_the_hold(self) -> None:
        reg = self._registry()
        session = _make_session(registry=reg, model_alias="gw")
        self._delete_gw(reg)
        session._refresh_model_from_registry()
        session.handle_command("/model")
        assert "This workstream's model 'gw' isn't loaded" in session.ui.infos[-1]

        session.handle_command("/model other")

        assert not session.model_held
        assert session.model_alias == "other"
        assert _client(session) is reg.get_client("other")
        assert session._config_for_save()["model_alias"] == "other"

    def test_model_calls_refuse_before_any_backend_work(self, monkeypatch: Any) -> None:
        """A held session's utility and primary model calls refuse before the
        stand-in's capacity slot is taken or a credential is resolved, and so
        does a ``model_turn`` that no hold check stopped."""
        from turnstone.core import session as session_mod
        from turnstone.core.model_turn import lane_call_client, model_turn
        from turnstone.core.session import ModelNotLoadedError

        reg = self._registry()
        session = _make_session(registry=reg, model_alias="gw")
        create = MagicMock()
        _client(session).chat.completions.create = create
        self._delete_gw(reg)
        session._refresh_model_from_registry()
        lane = session._primary_lane()
        assert lane.admission is not None  # every alias carries one
        acquire = MagicMock(wraps=lane.admission.acquire)
        monkeypatch.setattr(lane.admission, "acquire", acquire)
        resolve_token = MagicMock(return_value=None)
        monkeypatch.setattr(session_mod, "resolve_model_backend_auth_token", resolve_token)

        with pytest.raises(ModelNotLoadedError):
            session._utility_completion([Turn.user("summarize")])
        with pytest.raises(ModelNotLoadedError):
            session._primary_model_turn(
                session_mod._StreamTurnConsumer(session, 0), lambda wire, _lane: wire
            )
        with pytest.raises(ModelNotLoadedError):
            model_turn(lane, [Turn.user("unchecked")])
        with pytest.raises(ModelNotLoadedError):
            lane_call_client(lane)

        create.assert_not_called()
        acquire.assert_not_called()
        resolve_token.assert_not_called()

    def test_the_hold_reads_loaded_again_once_the_model_returns(self) -> None:
        """Between the model's return and the next send, which rebinds it, a
        model call and ``/model`` both say it is back; the send then runs on it."""
        from turnstone.core.session import ModelNotLoadedError

        reg = self._registry()
        session = _make_session(registry=reg, model_alias="gw")
        self._delete_gw(reg)
        session._refresh_model_from_registry()
        reg.reload(
            {
                "gw": ModelConfig(
                    "gw", "http://a.example/v1", "k", "test-model", provider="openai-compatible"
                ),
                "other": ModelConfig(
                    "other", "http://o.example/v1", "k", "o-model", provider="openai-compatible"
                ),
            },
            "gw",
            app_state=_KEYED_STATE,
        )

        with pytest.raises(ModelNotLoadedError) as exc_info:
            session._utility_completion([Turn.user("summarize")])
        loaded_again = "This workstream's model 'gw' is loaded again; send a message"
        assert loaded_again in str(exc_info.value)
        session.handle_command("/model")
        assert loaded_again in session.ui.infos[-1]
        assert session.model_held  # only a bind ends the hold

    def test_the_next_send_after_the_model_returns_runs_on_it(self, tmp_db: str) -> None:
        """The hold's whole round trip through ``send()``: refused while the
        model is gone, then served by it once a reload lists it again."""
        from turnstone.core.session import ModelNotLoadedError

        reg = self._registry()
        session = _make_durable_session(registry=reg, model_alias="gw")
        self._delete_gw(reg)
        with pytest.raises(ModelNotLoadedError):
            session.send("hello")
        assert session.model_held

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
        served = MagicMock(side_effect=scripted_chat_client({"content": "back"}))
        reg.get_client("gw").chat.completions.create = served
        other_create = MagicMock()
        reg.get_client("other").chat.completions.create = other_create

        session.send("hello again")

        assert served.called  # the turn (and the title it launches)
        other_create.assert_not_called()
        assert not session.model_held
        assert _client(session) is reg.get_client("gw")
        assert session.messages[-1].role == "assistant"

    def test_a_held_session_retries_a_failed_build_once_per_registry_change(
        self, monkeypatch: Any, caplog: Any
    ) -> None:
        """A failed build isn't retried at every send, which would rebuild it
        under the registry-wide client lock for each held session; the next
        registry change retries it, and a successful bind ends the hold."""
        reg = self._registry()
        session = _make_session(registry=reg, model_alias="gw")
        self._delete_gw(reg)
        session._refresh_model_from_registry()
        real_create_client = mr_module.create_client
        broken = MagicMock(side_effect=FileNotFoundError("/gone/ca.pem"))
        monkeypatch.setattr(mr_module, "create_client", broken)
        models = {
            "gw": ModelConfig(
                "gw", "http://b.example/v1", "k", "test-model", provider="openai-compatible"
            ),
            "other": ModelConfig(
                "other", "http://o.example/v1", "k", "o-model", provider="openai-compatible"
            ),
        }
        reg.reload(models, "gw", app_state=_KEYED_STATE)
        with caplog.at_level(logging.WARNING):
            session._refresh_model_from_registry()
            session._refresh_model_from_registry()  # latched: no second build
        assert broken.call_count == 1
        warned = [
            r
            for r in caplog.records
            if "model_refresh_client_construction_failed" in r.getMessage()
        ]
        assert len(warned) == 1

        # The cause is fixed outside the registry: nothing retries it yet.
        monkeypatch.setattr(mr_module, "create_client", real_create_client)
        session._refresh_model_from_registry()
        assert session.model_held

        reg.reload(models, "gw", app_state=_KEYED_STATE)  # a registry change
        session._refresh_model_from_registry()

        assert not session.model_held
        assert _client(session) is reg.get_client("gw")

    def test_a_live_session_whose_rebuild_fails_holds_and_dials_nothing(
        self, tmp_db: str, monkeypatch: Any
    ) -> None:
        """A reload that retires the session's client but can't build the new
        one leaves no client to send on: the session holds, naming the cause,
        and no backend is dialed or charged."""
        from turnstone.core.session import ModelNotLoadedError

        reg = self._registry()
        session = _make_durable_session(registry=reg, model_alias="gw")
        retired_create = MagicMock()
        _client(session).chat.completions.create = retired_create
        monkeypatch.setattr(
            mr_module, "create_client", MagicMock(side_effect=FileNotFoundError("/gone/ca.pem"))
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

        with pytest.raises(ModelNotLoadedError):
            session.send("hello")

        assert session.model_held
        assert session.model_alias == "gw"
        retired_create.assert_not_called()
        assert "couldn't be built" in session.ui.errors[-1]

    def test_a_model_removed_between_rounds_refuses_the_next_round(self) -> None:
        """Each model round refreshes first, so a reload between the rounds of
        one send holds the session and the next round dials nothing."""
        from turnstone.core import session as session_mod
        from turnstone.core.session import ModelNotLoadedError

        reg = self._registry()
        session = _make_session(registry=reg, model_alias="gw")
        dialed = MagicMock()
        _client(session).chat.completions.create = dialed
        self._delete_gw(reg)  # mid-send: no send-start refresh runs

        with pytest.raises(ModelNotLoadedError):
            session._primary_model_turn(
                session_mod._StreamTurnConsumer(session, 0), lambda wire, _lane: wire
            )

        assert session.model_held
        dialed.assert_not_called()

    def _ladder(self, session: Any, backoff: Any) -> tuple[list[Any], Any]:
        """Run the creation ladder once: the first attempt fails, *backoff*
        stands in for the sleep before the retry, and the retry fails for good;
        returns the lanes the attempts ran on and the error the ladder raised."""
        from turnstone.core.session import _StreamTurnConsumer

        session._generation = 7
        lanes: list[Any] = []

        def dispatch(lane: Any, *args: Any, **kwargs: Any) -> Any:
            lanes.append(lane)
            raise ConnectionError("attempt failed")

        with (
            patch("turnstone.core.session.model_turn", side_effect=dispatch),
            patch.object(session, "_stop_retrying", side_effect=lambda e, n, lane: n >= 1),
            patch.object(session, "_backoff_or_cancelled", side_effect=backoff),
            pytest.raises(Exception) as exc_info,
        ):
            session._model_turn_with_retry(
                session._primary_lane(),
                None,
                _StreamTurnConsumer(session, 7),
                lambda wire, _lane: wire,
                7,
            )
        return lanes, exc_info.value

    def test_a_reload_during_a_retry_backoff_moves_the_retry_to_the_new_client(self) -> None:
        reg = self._registry()
        session = _make_session(registry=reg, model_alias="gw")
        retired = _client(session)

        def rotate_gw(*_args: Any) -> None:
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

        lanes, error = self._ladder(session, rotate_gw)

        assert isinstance(error, ConnectionError)
        assert [lane.client for lane in lanes] == [retired, reg.get_client("gw")]
        assert reg.get_client("gw") is not retired

    def _move_gw_to_b(self, reg: ModelRegistry) -> None:
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

    def _round_with_health(
        self,
        dispatch: Any,
        *,
        principal_id: str | None = None,
        failures: tuple[int, int] = (0, 0),
    ) -> tuple[Any, Any, Any]:
        """One model round on gw at a.example whose retry backoff moves gw to
        b.example; *dispatch* stands in for ``model_turn``, and *failures*
        seeds each backend's tracker. Returns the two trackers and the error
        the round raised."""
        from turnstone.core import session as session_mod
        from turnstone.core.healthcheck import HealthTrackerRegistry

        reg = self._registry()
        session = _make_session(registry=reg, model_alias="gw")
        health = HealthTrackerRegistry(failure_threshold=5)
        tracker_a = health.get_tracker("openai-compatible", "http://a.example/v1")
        tracker_b = health.get_tracker("openai-compatible", "http://b.example/v1")
        for tracker, count in zip((tracker_a, tracker_b), failures, strict=True):
            for _ in range(count):
                tracker.record_failure()
        session._health_registry = health
        session._generation = 7
        with (
            patch("turnstone.core.session.model_turn", side_effect=dispatch),
            patch.object(session, "_stop_retrying", side_effect=lambda e, n, lane: n >= 1),
            patch.object(
                session, "_backoff_or_cancelled", side_effect=lambda *_a: self._move_gw_to_b(reg)
            ),
            pytest.raises(Exception) as exc_info,
        ):
            session._primary_model_turn(
                session_mod._StreamTurnConsumer(session, 7),
                lambda wire, _lane: wire,
                7,
                principal_id=principal_id,
            )
        return tracker_a, tracker_b, exc_info.value

    def test_a_retry_on_a_rebound_lane_charges_the_backend_it_dialed(self) -> None:
        dialed: list[str] = []

        def dispatch(lane: Any, *args: Any, **kwargs: Any) -> Any:
            dialed.append(str(lane.client.base_url))
            raise ConnectionError("attempt failed")

        tracker_a, tracker_b, error = self._round_with_health(dispatch)

        assert isinstance(error, ConnectionError)
        assert dialed == ["http://a.example/v1/", "http://b.example/v1/"]
        assert tracker_a.consecutive_failures == 0
        assert tracker_b.consecutive_failures == 1

    def test_a_retry_on_a_rebound_lane_credits_the_backend_it_dialed(self) -> None:
        attempts: list[Any] = []

        def dispatch(lane: Any, *args: Any, **kwargs: Any) -> Any:
            attempts.append(lane)
            if len(attempts) == 1:
                raise ConnectionError("attempt failed")
            kwargs["cancel_ref"].append(MagicMock())  # the stream arms: a success
            raise RuntimeError("died after arming")

        tracker_a, tracker_b, error = self._round_with_health(dispatch, failures=(2, 1))

        assert isinstance(error, RuntimeError)
        assert tracker_b.consecutive_failures == 0  # credited
        assert tracker_a.consecutive_failures == 2  # untouched

    def test_a_retry_on_a_rebound_lane_keeps_the_turns_principal(self) -> None:
        """The rebound lane is pinned to the user who started the turn, not to
        whoever acts on the session by the time the retry runs."""
        tokens: list[Any] = []

        def dispatch(lane: Any, *args: Any, **kwargs: Any) -> Any:
            tokens.append(lane.backend_auth_resolver(lane.alias, lane.backend_auth_config))
            raise ConnectionError("attempt failed")

        with patch(
            "turnstone.core.session.resolve_model_backend_auth_token",
            side_effect=lambda alias, cfg, *, principal_id, **_kw: f"token-{principal_id}",
        ):
            _a, _b, error = self._round_with_health(dispatch, principal_id="user-a")

        assert isinstance(error, ConnectionError)
        assert tokens == ["token-user-a", "token-user-a"]

    def test_a_reload_during_a_retry_backoff_that_removes_the_model_refuses(self) -> None:
        from turnstone.core.session import ModelNotLoadedError

        reg = self._registry()
        session = _make_session(registry=reg, model_alias="gw")

        lanes, error = self._ladder(session, lambda *_args: self._delete_gw(reg))

        assert isinstance(error, ModelNotLoadedError)
        assert len(lanes) == 1  # the retry never dialed
        assert session.model_held

    def test_an_earlier_build_failure_is_not_reported_after_a_fix(self, monkeypatch: Any) -> None:
        """A construction failure latched at an earlier registry state doesn't
        outlive the reload that fixed it: the refusal reads "loaded again"."""
        from turnstone.core.session import ModelNotLoadedError

        reg = self._registry()
        session = _make_session(registry=reg, model_alias="gw")
        self._delete_gw(reg)
        session._refresh_model_from_registry()
        models = {
            "gw": ModelConfig(
                "gw", "http://b.example/v1", "k", "test-model", provider="openai-compatible"
            ),
            "other": ModelConfig(
                "other", "http://o.example/v1", "k", "o-model", provider="openai-compatible"
            ),
        }
        real_create_client = mr_module.create_client
        monkeypatch.setattr(
            mr_module, "create_client", MagicMock(side_effect=FileNotFoundError("/gone/ca.pem"))
        )
        reg.reload(models, "gw", app_state=_KEYED_STATE)
        session._refresh_model_from_registry()
        assert session._rebind_failed_key == ("gw", reg.generation)

        # The admin fixes the definition: a new registry state.
        monkeypatch.setattr(mr_module, "create_client", real_create_client)
        reg.reload(
            {**models, "gw": dataclasses.replace(models["gw"], base_url="http://c.example/v1")},
            "gw",
            app_state=_KEYED_STATE,
        )

        with pytest.raises(ModelNotLoadedError) as exc_info:
            session._utility_completion([Turn.user("summarize")])
        assert "is loaded again" in str(exc_info.value)
        assert "couldn't be built" not in str(exc_info.value)

    def test_recreated_broken_alias_reports_construction_cause(
        self, tmp_db: str, monkeypatch: Any
    ) -> None:
        """A re-created alias whose client can't be built keeps the hold, and
        the refusal names the construction cause, not "isn't loaded"."""
        from turnstone.core.session import ModelNotLoadedError

        reg = self._registry()
        session = _make_durable_session(registry=reg, model_alias="gw")
        _client(session).chat.completions.create = MagicMock(side_effect=self._dead_client_error())

        self._delete_gw(reg)
        session._refresh_model_from_registry()
        assert session.model_held

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
        assert session.model_held
        assert session._rebind_failed_key == ("gw", reg.generation)

        with pytest.raises(ModelNotLoadedError):
            session.send("hello")

        message = session.ui.errors[-1]
        assert "couldn't be built" in message
        # The cause is path-scrubbed: the exception type plus a server-log
        # pointer, since SDK text can embed filesystem paths.
        assert "FileNotFoundError" in message
        assert "details in server log" in message
        assert "isn't loaded" not in message

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
        assert session.model_held

        reg.reload(
            {
                "gw": ModelConfig("gw", "http://a.example/v1", "k", "test-model"),
                "other": ModelConfig("other", "http://o.example/v1", "k", "o-model"),
            },
            "gw",
            app_state=_KEYED_STATE,
        )
        session._refresh_model_from_registry()

        assert not session.model_held
        assert _client(session) is reg.get_client("gw")
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
        assert _client(session) is reg.get_client("gw")
        assert session._registry_generation == reg.generation


class TestSessionNoFailover:
    def test_primary_failure_never_runs_another_model(self, tmp_db: str) -> None:
        # The failover chain is gone (#1359): a turn whose model fails reports
        # that failure and never runs on another model.  provider=
        # "openai-compatible" pins the Chat Completions surface the patched
        # ``chat.completions.create`` stubs speak.
        reg = ModelRegistry(
            models={
                "primary": ModelConfig(
                    "primary", "http://p/v1", "k", "p-model", provider="openai-compatible"
                ),
                "other": ModelConfig(
                    "other", "http://o/v1", "k", "o-model", provider="openai-compatible"
                ),
            },
            default="primary",
        )
        session = _make_durable_session(registry=reg, model_alias="primary")
        _client(session).chat.completions.create = MagicMock(
            side_effect=ConnectionError("Primary down")
        )
        other_create = MagicMock()
        reg.get_client("other").chat.completions.create = other_create

        with pytest.raises(ConnectionError):
            session.send("hi")

        other_create.assert_not_called()
        assert not any("falling back" in i.lower() for i in session.ui.infos)

    def test_without_a_registry_the_failure_is_reported(self, tmp_db: str) -> None:
        session = _make_durable_session()
        _client(session).chat.completions.create = MagicMock(side_effect=ConnectionError("Down"))
        with pytest.raises(ConnectionError):
            session.send("hi")


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
        """Patch a registry-resolved or primary-lane client to capture kwargs.

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
        # the exact primary lane resolved for the session.
        reg = self._three_model_registry()
        session = _make_session(registry=reg, model_alias="main")
        captured = self._capture_on(_client(session))
        session._run_agent([Turn.user("x")], label="plan")
        assert captured["model"] == "main-model"

    def test_task_effort_inherits_session_when_unset(self) -> None:
        # Task with no task_effort override must inherit whatever the SESSION
        # is configured for — assert against an explicit value rather than
        # the constructor default so the invariant is unambiguous if someone
        # changes ChatSession's default later.
        reg = self._three_model_registry()
        session = _make_session(registry=reg, model_alias="main", reasoning_effort="low")
        captured = self._capture_on(_client(session))
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
        captured = self._capture_on(_client(session))
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

    def test_session_fallback_uses_exact_primary_lane_and_pinned_auth(self) -> None:
        """A sub-agent keeps the primary lane and pins its auth resolver."""
        import turnstone.core.session as session_module

        reg = self._three_model_registry()  # no agent_model / plan_model set
        session = _make_session(registry=reg, model_alias="main")
        primary_lane = session._primary_lane()
        primary_caps = primary_lane.capabilities
        assert primary_caps is not None
        self._capture_on(primary_lane.client)

        with patch.object(
            session_module,
            "model_turn",
            wraps=session_module.model_turn,
        ) as model_turn_spy:
            session._run_agent([Turn.user("x")], label="plan")

        assert model_turn_spy.call_count == 1
        used_lane = model_turn_spy.call_args.args[0]
        assert used_lane == dataclasses.replace(
            primary_lane,
            backend_auth_resolver=used_lane.backend_auth_resolver,
        )
        assert used_lane.backend_auth_resolver is not None
        assert used_lane.capabilities is primary_caps
        assert used_lane.client is _client(session)
        assert used_lane.alias == "main"

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


def _make_manager(
    session_factory: Any,
    *,
    storage: Any | None = None,
    model_validator: Any | None = None,
) -> Any:
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
    return SessionManager(
        adapter,
        storage=storage if storage is not None else MagicMock(),
        max_active=10,
        event_emitter=adapter,
        model_validator=model_validator,
    )


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
    def test_gateway_context_length(self) -> None:
        """OpenRouter-style listings carry ``context_length`` at the top level
        and under ``top_provider``; neither is vLLM's or llama.cpp's key."""
        from turnstone.core.model_registry import _extract_context_window

        m = MagicMock()
        m.id = "openai/gpt-5.6-luna"
        m.model_dump.return_value = {
            "id": "openai/gpt-5.6-luna",
            "context_length": 1_050_000,
            "top_provider": {"context_length": 1_050_000, "max_completion_tokens": 128_000},
        }
        assert _extract_context_window(m, "openai") == 1_050_000

        m.model_dump.return_value = {"id": "x", "top_provider": {"context_length": 200_000}}
        assert _extract_context_window(m, "openai") == 200_000

        m.model_dump.return_value = {"id": "x", "context_length": True, "top_provider": {}}
        assert _extract_context_window(m, "openai") is None

    @pytest.mark.parametrize(
        ("card", "expected"),
        [
            ({"id": "x", "context_window": 131072, "max_completion_tokens": 8192}, 131072),
            ({"id": "x", "max_context_length": 262144}, 262144),
            ({"id": "x", "max_model_len": 65536, "context_length": 131072}, 65536),
            ({"id": "x", "owned_by": "ollama"}, None),
        ],
        ids=["groq", "mistral", "vllm-wins-over-gateway-key", "ollama-reports-nothing"],
    )
    def test_provider_key_matrix(self, card: dict[str, Any], expected: int | None) -> None:
        from turnstone.core.model_registry import _extract_context_window

        m = MagicMock()
        m.id = card["id"]
        m.model_dump.return_value = card
        assert _extract_context_window(m, "openai") == expected

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


class TestDefaultNeverMovesOnALoad:
    """#1359: the default comes from configuration and the defined models, never
    from the set that loaded, so a definition left out of a load (a refused
    secret reference, a stored ``${VAR}``) fails what relies on it instead of
    handing the default to the next model."""

    @staticmethod
    def _row(alias: str, **overrides: Any) -> dict[str, Any]:
        row: dict[str, Any] = {
            "alias": alias,
            "model": "m-" + alias,
            "provider": "openai-compatible",
            "base_url": "https://models.example.com/v1",
            "api_key": "sk-literal",
            "context_window": 8192,
            "capabilities": "{}",
            "enabled": True,
        }
        row.update(overrides)
        return row

    def test_pick_default_alias(self) -> None:
        from turnstone.core.model_registry import pick_default_alias

        assert pick_default_alias("chosen", ["a", "default"]) == "chosen"
        assert pick_default_alias("", ["a", "default"]) == "default"
        assert pick_default_alias("", ["b", "a"]) == "b"
        assert pick_default_alias("", []) == ""

    def test_first_defined_stays_default_when_left_out(self) -> None:
        storage = _MockStorage(
            [self._row("alpha", api_key="${TS_STORED_VALUE}"), self._row("beta")]
        )
        with patch("turnstone.core.model_registry.load_config", return_value={}):
            reg = load_model_registry(storage=storage, allow_empty=True)
        assert reg.list_aliases() == ["beta"]
        assert "alpha" in reg.skipped_aliases
        assert reg.default == "alpha"
        with pytest.raises(UnknownModelAliasError) as exc_info:
            reg.resolve()
        assert exc_info.value.alias == "alpha"
        # The category every reader may see, the same one task_agent reports.
        assert exc_info.value.reason == "its definition couldn't be loaded"
        assert exc_info.value.retryable is False

    def test_configured_default_left_out_stays_default(self) -> None:
        storage = _MockStorage(
            [self._row("alpha"), self._row("beta", api_key="${TS_STORED_VALUE}")]
        )
        fake_cfg: dict[str, Any] = {"model": {"default": "beta"}}
        with patch("turnstone.core.model_registry.load_config", return_value=fake_cfg):
            reg = load_model_registry(storage=storage, allow_empty=True)
        assert reg.default == "beta"
        assert reg.has_alias("alpha")

    def test_the_retryable_mark_follows_the_reason_kept(self) -> None:
        # A stored row's key fails retryably, then the config.toml entry that
        # overrides it fails for good: retrying can't help, so nothing says so.
        from turnstone.core.http_headers import HeaderValueError
        from turnstone.core.secret_refs import SecretBackendError

        def _materialize(value: object) -> str:
            if value == "secret://store/x":
                raise SecretBackendError("store unreachable", retryable=True)
            if value == "BAD":
                raise HeaderValueError("literal key cannot be sent")
            return str(value)

        storage = _MockStorage([self._row("x", api_key="secret://store/x"), self._row("y")])
        fake_cfg: dict[str, Any] = {
            "models": {
                "x": {"model": "m-x", "base_url": "https://models.example.com/v1", "api_key": "BAD"}
            }
        }
        with (
            patch("turnstone.core.model_registry.load_config", return_value=fake_cfg),
            patch("turnstone.core.model_registry.materialize_api_key", _materialize),
        ):
            reg = load_model_registry(storage=storage, allow_empty=True)
        assert "x" in reg.skipped_aliases
        assert "x" not in reg.retryable_skips
        assert reg.unavailable_reason("x") == ("its definition couldn't be loaded", False)

    def test_config_tomls_kind_replaces_a_stored_rows(self) -> None:
        # The default pick reads the definition in force: config.toml's entry,
        # once it takes over the alias.
        rerank = '{"supports_rerank": true}'
        chat_entry = {"model": "m-a", "base_url": "https://models.example.com/v1", "api_key": "k"}
        storage = _MockStorage([self._row("a", capabilities=rerank)])
        with patch(
            "turnstone.core.model_registry.load_config",
            return_value={"models": {"a": chat_entry}},
        ):
            reg = load_model_registry(storage=storage, allow_empty=True)
        assert reg.default == "a"

        storage = _MockStorage([self._row("a"), self._row("b")])
        rerank_entry = {
            **chat_entry,
            "model": "bge-reranker",
            "capabilities": {"supports_rerank": True},
        }
        with patch(
            "turnstone.core.model_registry.load_config",
            return_value={"models": {"a": rerank_entry}},
        ):
            reg = load_model_registry(storage=storage, allow_empty=True)
        assert reg.default == "b"

    def test_nothing_loaded_still_names_the_default(self) -> None:
        storage = _MockStorage([self._row("only", api_key="${TS_STORED_VALUE}")])
        with patch("turnstone.core.model_registry.load_config", return_value={}):
            reg = load_model_registry(storage=storage, allow_empty=True)
        assert reg.count == 0
        assert reg.default == "only"

    def test_a_model_named_default_wins_over_order(self) -> None:
        fake_cfg: dict[str, Any] = {
            "models": {
                "zeta": {"base_url": "http://z/v1", "model": "z"},
                "default": {"base_url": "http://d/v1", "model": "d"},
            }
        }
        with patch("turnstone.core.model_registry.load_config", return_value=fake_cfg):
            reg = load_model_registry("http://x/v1", "x", "x")
        assert reg.default == "default"

    def test_a_malformed_config_entry_counts_as_defined(self) -> None:
        # The operator's first [models.*] entry is the default even when it
        # can't load; the next entry doesn't inherit the role.
        fake_cfg: dict[str, Any] = {
            "models": {
                "first": {"base_url": "http://f/v1"},
                "second": {"base_url": "http://s/v1", "model": "s"},
            }
        }
        with patch("turnstone.core.model_registry.load_config", return_value=fake_cfg):
            reg = load_model_registry("http://x/v1", "x", "x")
        assert reg.default == "first"
        assert reg.list_aliases() == ["second"]

    def test_the_pick_skips_rerankers_and_speech_models(self) -> None:
        # The setting a pick feeds outranks config.toml defaults, so a model
        # that can't hold a chat never becomes the default on its own.
        storage = _MockStorage(
            [
                self._row("a-rerank", capabilities='{"supports_rerank": true}'),
                self._row("b-whisper", model="whisper-1"),
                self._row("c-voice", capabilities='{"supports_speech_synthesis": true}'),
                self._row("d-chat"),
            ]
        )
        with patch("turnstone.core.model_registry.load_config", return_value={}):
            reg = load_model_registry(storage=storage, allow_empty=True)
        assert reg.default == "d-chat"

    def test_an_omni_model_counts_as_chat(self) -> None:
        storage = _MockStorage(
            [
                self._row(
                    "a-omni",
                    model="gpt-4o-audio",
                    capabilities='{"supports_audio_input": true, "supports_transcription": true}',
                ),
                self._row("b-chat"),
            ]
        )
        with patch("turnstone.core.model_registry.load_config", return_value={}):
            reg = load_model_registry(storage=storage, allow_empty=True)
        assert reg.default == "a-omni"

    def test_only_non_chat_models_leave_no_default(self) -> None:
        storage = _MockStorage([self._row("rerank", capabilities='{"supports_rerank": true}')])
        with patch("turnstone.core.model_registry.load_config", return_value={}):
            reg = load_model_registry(storage=storage, allow_empty=True)
        assert reg.default == ""

    def test_a_configured_default_is_taken_as_given(self) -> None:
        storage = _MockStorage(
            [self._row("rerank", capabilities='{"supports_rerank": true}'), self._row("chat")]
        )
        fake_cfg: dict[str, Any] = {"model": {"default": "rerank"}}
        with patch("turnstone.core.model_registry.load_config", return_value=fake_cfg):
            reg = load_model_registry(storage=storage, allow_empty=True)
        assert reg.default == "rerank"

    def test_an_unloaded_routing_alias_is_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        fake_cfg: dict[str, Any] = {"model": {"default": "gone", "task_model": "lost"}}
        with (
            patch("turnstone.core.model_registry.load_config", return_value=fake_cfg),
            caplog.at_level(logging.WARNING),
        ):
            load_model_registry("http://x/v1", "x", "x")
        logged = [
            _rendered(r)
            for r in caplog.records
            if "model_registry.routing_alias_not_loaded" in _rendered(r)
        ]
        assert any("gone" in line for line in logged)
        assert any("lost" in line for line in logged)


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
# Routing settings read at use time (effective_default, resolve_agent_*)
# ---------------------------------------------------------------------------


class _FakeCS:
    """Minimal ConfigStore stand-in: dict-backed get()."""

    def __init__(self, **values: str) -> None:
        self._values = dict(values)

    def get(self, key: str, default: Any = None) -> Any:
        return self._values.get(key, default if default is not None else "")

    def set(self, key: str, value: str) -> None:
        self._values[key] = value


class TestRoutingSettingsAtUseTime:
    """The ``model.*`` routing settings apply where they are read and are never
    written into the registry, so a changed or cleared setting takes effect at
    once, and an alias that isn't loaded is never swapped for another (#1359)."""

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

    def test_without_a_config_store_the_loader_values_apply(self) -> None:
        reg = self._registry(task_model="fast", task_effort="low")
        assert reg.effective_default(None) == "default"
        assert reg.resolve_agent_alias("task", None) == "fast"
        assert reg.resolve_agent_effort("task", None) == "low"

    def test_settings_override_the_loader_values_without_touching_them(self) -> None:
        reg = self._registry(task_model="fast", task_effort="low")
        generation = reg.generation
        cs = _FakeCS(
            **{
                "model.default_alias": "smart",
                "model.task_alias": "smart",
                "model.task_effort": "minimal",
            }
        )
        assert reg.effective_default(cs) == "smart"
        assert reg.resolve_agent_alias("task", cs) == "smart"
        assert reg.resolve_agent_effort("task", cs) == "minimal"
        assert (reg.default, reg.task_model, reg.task_effort) == ("default", "fast", "low")
        assert reg.generation == generation

    def test_a_cleared_setting_takes_effect_at_once(self) -> None:
        # The stale-override bug: a node layered the setting on its live
        # default, so a cleared setting kept its old value until a Sync.
        reg = self._registry()
        cs = _FakeCS(**{"model.default_alias": "smart", "model.task_alias": "smart"})
        assert reg.effective_default(cs) == "smart"
        cs.set("model.default_alias", "")
        cs.set("model.task_alias", "")
        assert reg.effective_default(cs) == "default"
        assert reg.resolve_agent_alias("task", cs) is None

    def test_an_unloaded_setting_is_kept_not_dropped(self) -> None:
        reg = self._registry(task_model="fast")
        cs = _FakeCS(**{"model.default_alias": "nonexistent", "model.task_alias": "gone"})
        assert reg.effective_default(cs) == "nonexistent"
        assert reg.resolve_agent_alias("task", cs) == "gone"
        with pytest.raises(UnknownModelAliasError):
            reg.resolve(reg.effective_default(cs))

    def test_blank_settings_are_unset(self) -> None:
        reg = self._registry(task_model="fast", task_effort="low")
        cs = _FakeCS(
            **{"model.default_alias": "  ", "model.task_alias": "", "model.task_effort": ""}
        )
        assert reg.effective_default(cs) == "default"
        assert reg.resolve_agent_alias("task", cs) == "fast"
        assert reg.resolve_agent_effort("task", cs) == "low"

    def test_task_settings_apply_only_to_the_task_kind(self) -> None:
        reg = self._registry(agent_model="smart")
        cs = _FakeCS(**{"model.task_alias": "fast", "model.task_effort": "low"})
        assert reg.resolve_agent_alias("task", cs) == "fast"
        assert reg.resolve_agent_alias("agent", cs) == "smart"
        assert reg.resolve_agent_effort("agent", cs) is None


# ---------------------------------------------------------------------------
# Auth-mode classification maps — drift guards
# ---------------------------------------------------------------------------


def test_model_auth_mode_profile_map_matches_mint_legs() -> None:
    """The registry's pairing map and the mint-leg registry agree by test,
    not by import: model_registry deliberately spells profile names as
    literals to keep the mint stack off its import graph, so this is the
    seam that catches a rename or an unclassified mode.
    """
    from turnstone.core.oauth.grants import OBO_GRANT_PROFILES

    # Every dynamic mode names its required profile — a mode missing here is
    # never posture-approvable and never mints, which is fail-closed but
    # must be a deliberate state, not an oversight.
    assert set(mr_module.MODEL_AUTH_MODE_PROFILES) == set(mr_module.DYNAMIC_MODEL_AUTH_MODES)
    # And every named profile has a real mint leg.
    assert set(mr_module.MODEL_AUTH_MODE_PROFILES.values()) <= OBO_GRANT_PROFILES


def test_auth_mode_classification_sets_are_subsets_of_dynamic() -> None:
    assert mr_module.SCOPES_MODEL_AUTH_MODES <= mr_module.DYNAMIC_MODEL_AUTH_MODES
    assert mr_module.APP_IDENTITY_MODEL_AUTH_MODES <= mr_module.DYNAMIC_MODEL_AUTH_MODES
    # The scopes-reading and app-identity classes are disjoint: an app mode
    # that read user-facing exchange scopes would have no coherent principal.
    assert not (mr_module.SCOPES_MODEL_AUTH_MODES & mr_module.APP_IDENTITY_MODEL_AUTH_MODES)


def test_obo_scopes_normalizers_agree_across_modules() -> None:
    """The registry, console, and mint each own their scopes-normalization
    POLICY (refuse-vs-strip on control garbage), but their SPELLING must
    agree — or the console stores a value the mint keys its cache row under
    differently than the session heartbeat's rebuild. All three now
    delegate to ``sanitize_backend_auth_scopes``; this corpus pins the
    delegation and the per-layer policies wrapped around it.
    """
    from turnstone.core.model_oauth import _normalized_mint_scopes

    corpus = [
        "",
        "aud-gw",
        "aud-gw openid",
        "  aud-gw   openid  ",
        "aud-gw\topenid",
        "aud-gw\n  openid",
        "aud-gw openid",  # already normalized — idempotence
    ]
    for raw in corpus:
        registry_value = mr_module._normalize_auth_mode("gw", "rfc8693_obo", "api://gw", raw)[2]
        shared = mr_module.sanitize_backend_auth_scopes(raw)
        # The console's stored spelling IS the shared transform's output
        # (its parser delegates), so pinning registry == mint == shared
        # covers all three write/read surfaces.
        assert registry_value == _normalized_mint_scopes(raw) == shared, raw
    # Interior NON-whitespace controls are where the policies deliberately
    # split: the registry refuses to LOAD what the write paths would have
    # stripped before storing — and the stripping paths still agree with
    # the shared transform.
    dirty = "aud-gw" + chr(1) + "openid"
    with pytest.raises(mr_module.ModelAuthConfigError):
        mr_module._normalize_auth_mode("gw", "rfc8693_obo", "api://gw", dirty)
    assert _normalized_mint_scopes(dirty) == "aud-gwopenid"
    assert mr_module.sanitize_backend_auth_scopes(dirty) == "aud-gwopenid"
    # The C0 separator block counts as Python whitespace, so a bare
    # str.split() would swallow it before the guard could refuse; the
    # registry must refuse it like every other control byte, and the
    # SANITIZE must strip it like every other control — never promote it to
    # a separator that splits one token into two valid-looking scopes —
    # while the sanctioned separators (tab/newline/CR, blessed in the
    # corpus above) keep collapsing.
    for sep_byte in (chr(0x1C), chr(0x1D), chr(0x1E), chr(0x1F)):
        with pytest.raises(mr_module.ModelAuthConfigError):
            mr_module._normalize_auth_mode(
                "gw", "rfc8693_obo", "api://gw", f"aud-gw{sep_byte}openid"
            )
        assert mr_module.sanitize_backend_auth_scopes(f"aud-gw{sep_byte}openid") == "aud-gwopenid"


def test_control_bearing_alias_refuses_to_load() -> None:
    """The alias is the identity every mint-cache, cooldown, cause and purge
    key derives from, and the key builders strip control characters as a
    raw-caller seam — so a control-bearing alias would silently collide with
    its stripped twin, merging two definitions onto one identity. The load
    refuses it like any other backend-auth text garbage, for every mode.
    """
    for mode in ("static", "rfc8693_obo"):
        with pytest.raises(mr_module.ModelAuthConfigError, match="alias contains control"):
            mr_module._normalize_auth_mode(
                "gw" + chr(1), mode, "api://gw" if mode != "static" else "", ""
            )


# ---------------------------------------------------------------------------
# context_window = 0 → per-definition auto-detection (#1052)
# ---------------------------------------------------------------------------


def _rendered(record: logging.LogRecord) -> str:
    """Message text plus its args, however structlog captured them.

    Under the test configuration the record carries the console-rendered
    line with ``positional_args=(...)`` appended and empty ``args``; under a
    plain stdlib configuration the args are formatted into the message.
    Concatenating both makes the substring checks below hold either way.
    """
    return f"{record.getMessage()} {record.args!r}"


# Model ids the commercial tables know, whatever they hold today.
_COMMERCIAL_ID = list_known_models("openai")[0]
_ANTHROPIC_ID = list_known_models("anthropic")[0]


def _probe_result(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "reachable": True,
        "model_found": True,
        "available_models": ["local-model"],
        "context_window": 262144,
        "server_type": "vllm",
        "error": None,
    }
    base.update(overrides)
    return base


class TestContextWindowAutoDetect:
    """``0`` is the Models tab's auto-detect sentinel. It must resolve to a
    real number per definition — never stay 0 (zeroes every downstream
    budget) and never inherit whatever the node's bootstrap endpoint said,
    because the node no longer has one."""

    @staticmethod
    def _local_cfg(**entry: Any) -> dict[str, Any]:
        local: dict[str, Any] = {
            "base_url": "http://localhost:8000/v1",
            "model": "local-model",
            "context_window": 0,
        }
        local.update(entry)
        return {"models": {"local": local}}

    def test_table_provider_resolves_offline(self) -> None:
        from turnstone.core.providers import create_provider

        fake_cfg: dict[str, Any] = {
            "models": {
                "claude": {
                    "provider": "anthropic",
                    "model": _ANTHROPIC_ID,
                    "context_window": 0,
                },
                "gpt": {
                    "provider": "openai",
                    "base_url": "https://api.openai.com/v1",
                    "model": _COMMERCIAL_ID,
                },
            }
        }
        with (
            patch("turnstone.core.model_registry.load_config", return_value=fake_cfg),
            patch("turnstone.core.model_registry.probe_model_endpoint") as probe,
        ):
            reg = load_model_registry(detect_context_windows=True)
        probe.assert_not_called()
        claude = reg.get_config("claude").context_window
        gpt = reg.get_config("gpt").context_window
        assert claude == create_provider("anthropic").get_capabilities(_ANTHROPIC_ID).context_window
        assert gpt == create_provider("openai").get_capabilities(_COMMERCIAL_ID).context_window
        assert claude > 0 and gpt > 0

    def test_unlisted_commercial_model_falls_back_with_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A model id the table does not list gets the conservative fallback
        and a warning — never the provider default, which can exceed the
        real window (a 128k fine-tune budgeted at 200k hard-fails turns)."""
        fake_cfg: dict[str, Any] = {
            "models": {
                "new": {
                    "provider": "openai",
                    "base_url": "https://api.openai.com/v1",
                    "model": "ft:gpt-4o-mini-2024-07-18:acme::abc",
                    "context_window": 0,
                },
                "gemini": {"provider": "google", "model": "gemini-3-something"},
            }
        }
        with (
            patch("turnstone.core.model_registry.load_config", return_value=fake_cfg),
            caplog.at_level(logging.WARNING),
        ):
            reg = load_model_registry(detect_context_windows=True)
        assert reg.get_config("new").context_window == mr_module.FALLBACK_CONTEXT_WINDOW
        assert reg.get_config("gemini").context_window == mr_module.FALLBACK_CONTEXT_WINDOW
        assert any(
            "'new'" in _rendered(r) and "no capability entry" in _rendered(r)
            for r in caplog.records
        )
        assert any("'gemini'" in _rendered(r) for r in caplog.records)

    def test_explicit_value_is_taken_verbatim(self) -> None:
        with (
            patch(
                "turnstone.core.model_registry.load_config",
                return_value=self._local_cfg(context_window=131072),
            ),
            patch("turnstone.core.model_registry.probe_model_endpoint") as probe,
        ):
            reg = load_model_registry(detect_context_windows=True)
        probe.assert_not_called()
        assert reg.get_config("local").context_window == 131072

    def test_local_detection_off_falls_back(self) -> None:
        with (
            patch("turnstone.core.model_registry.load_config", return_value=self._local_cfg()),
            patch("turnstone.core.model_registry.probe_model_endpoint") as probe,
        ):
            reg = load_model_registry()
        probe.assert_not_called()
        assert reg.get_config("local").context_window == mr_module.FALLBACK_CONTEXT_WINDOW

    def test_local_probe_asks_the_definitions_own_endpoint(self) -> None:
        with (
            patch("turnstone.core.model_registry.load_config", return_value=self._local_cfg()),
            patch(
                "turnstone.core.model_registry.probe_model_endpoint",
                return_value=_probe_result(),
            ) as probe,
        ):
            reg = load_model_registry(detect_context_windows=True)
        # Endpoint metadata only, on the loader's shorter budget, with the
        # definition's own key (create_client supplies the local placeholder).
        probe.assert_called_once_with(
            "openai-compatible",
            "http://localhost:8000/v1",
            "",
            target_model="local-model",
            static_table=False,
            timeout=mr_module._LOADER_PROBE_TIMEOUT,
            workspace_id="",
        )
        assert reg.get_config("local").context_window == 262144

    def test_local_probe_single_model_server_accepts_any_name(self) -> None:
        """A one-model server is this model whatever it calls itself — the
        common vLLM case where the served id is a path, not the alias name."""
        result = _probe_result(model_found=False, available_models=["/models/qwen"])
        with (
            patch("turnstone.core.model_registry.load_config", return_value=self._local_cfg()),
            patch("turnstone.core.model_registry.probe_model_endpoint", return_value=result),
        ):
            reg = load_model_registry(detect_context_windows=True)
        assert reg.get_config("local").context_window == 262144

    def test_local_probe_multi_model_needs_a_match(self, caplog: pytest.LogCaptureFixture) -> None:
        result = _probe_result(model_found=False, available_models=["a", "b"])
        with (
            patch("turnstone.core.model_registry.load_config", return_value=self._local_cfg()),
            patch("turnstone.core.model_registry.probe_model_endpoint", return_value=result),
            caplog.at_level(logging.WARNING),
        ):
            reg = load_model_registry(detect_context_windows=True)
        assert reg.get_config("local").context_window == mr_module.FALLBACK_CONTEXT_WINDOW
        assert any(
            "'local'" in _rendered(r) and "not in the endpoint's model list" in _rendered(r)
            for r in caplog.records
        )

    def test_local_probe_failure_warns_and_falls_back(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        result = _probe_result(reachable=False, context_window=None, error="connection refused")
        with (
            patch("turnstone.core.model_registry.load_config", return_value=self._local_cfg()),
            patch("turnstone.core.model_registry.probe_model_endpoint", return_value=result),
            caplog.at_level(logging.WARNING),
        ):
            reg = load_model_registry(detect_context_windows=True)
        assert reg.get_config("local").context_window == mr_module.FALLBACK_CONTEXT_WINDOW
        assert any(
            "'local'" in _rendered(r) and "connection refused" in _rendered(r)
            for r in caplog.records
        )

    def test_local_probe_runs_on_every_load(self) -> None:
        """A backend restarted with a different window is picked up by the
        next hot-reload — nothing is cached across loads."""
        with (
            patch("turnstone.core.model_registry.load_config", return_value=self._local_cfg()),
            patch(
                "turnstone.core.model_registry.probe_model_endpoint",
                side_effect=[_probe_result(), _probe_result(context_window=16384)],
            ) as probe,
        ):
            first = load_model_registry(detect_context_windows=True)
            second = load_model_registry(detect_context_windows=True)
        assert probe.call_count == 2
        assert first.get_config("local").context_window == 262144
        assert second.get_config("local").context_window == 16384

    def test_local_probe_miss_is_retried_on_next_load(self) -> None:
        down = _probe_result(reachable=False, context_window=None, error="down")
        with (
            patch("turnstone.core.model_registry.load_config", return_value=self._local_cfg()),
            patch(
                "turnstone.core.model_registry.probe_model_endpoint",
                side_effect=[down, _probe_result()],
            ) as probe,
        ):
            first = load_model_registry(detect_context_windows=True)
            second = load_model_registry(detect_context_windows=True)
        assert probe.call_count == 2
        assert first.get_config("local").context_window == mr_module.FALLBACK_CONTEXT_WINDOW
        assert second.get_config("local").context_window == 262144

    def test_reload_probes_first_and_takes_a_new_window(self) -> None:
        """A hot-reload asks the endpoint again, so a backend restarted with
        a smaller window is picked up rather than served from the prior."""
        running = ModelConfig(
            alias="local",
            base_url="http://localhost:8000/v1",
            api_key="",
            model="local-model",
            provider="openai-compatible",
            context_window=262144,
            context_window_detected=True,
        )
        with (
            patch("turnstone.core.model_registry.load_config", return_value=self._local_cfg()),
            patch(
                "turnstone.core.model_registry.probe_model_endpoint",
                return_value=_probe_result(context_window=16384),
            ) as probe,
        ):
            reg = load_model_registry(detect_context_windows=True, prior={"local": running})
        probe.assert_called_once()
        assert reg.get_config("local").context_window == 16384

    def test_reload_keeps_the_detected_window_when_the_probe_misses(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A backend mid-restart during a hot-reload must not shrink live
        sessions to the fallback: the definition keeps the window the running
        registry detected, while endpoint and model are unchanged."""
        running = ModelConfig(
            alias="local",
            base_url="http://localhost:8000/v1",
            api_key="",
            model="local-model",
            provider="openai-compatible",
            context_window=262144,
            context_window_detected=True,
        )
        down = _probe_result(reachable=False, context_window=None, error="connection refused")
        with (
            patch("turnstone.core.model_registry.load_config", return_value=self._local_cfg()),
            patch("turnstone.core.model_registry.probe_model_endpoint", return_value=down),
            caplog.at_level(logging.INFO),
        ):
            reg = load_model_registry(detect_context_windows=True, prior={"local": running})
        kept = reg.get_config("local")
        assert kept.context_window == 262144
        assert kept.context_window_detected is True
        assert any("keeping the previously detected" in _rendered(r) for r in caplog.records)
        assert not any(
            r.levelno >= logging.WARNING and "'local'" in _rendered(r) for r in caplog.records
        )

    def test_identical_definitions_share_one_probe(self) -> None:
        cfg = self._local_cfg()
        cfg["models"]["twin"] = dict(cfg["models"]["local"])
        cfg["models"]["other"] = dict(cfg["models"]["local"], model="other-model")
        with (
            patch("turnstone.core.model_registry.load_config", return_value=cfg),
            patch(
                "turnstone.core.model_registry.probe_model_endpoint",
                return_value=_probe_result(),
            ) as probe,
        ):
            reg = load_model_registry(detect_context_windows=True)
        assert probe.call_count == 2  # local + twin share; other is distinct
        assert reg.get_config("local").context_window == 262144
        assert reg.get_config("twin").context_window == 262144

    @pytest.mark.parametrize(
        "prior_window",
        [mr_module.FALLBACK_CONTEXT_WINDOW, 8192],
        ids=["prior-fallback", "prior-explicit"],
    )
    def test_prior_that_was_not_detected_is_not_carried_on_a_miss(self, prior_window: int) -> None:
        """A prior fallback is not a resolution, and an explicit prior window
        (the operator just switched the definition to auto-detect) is not
        either: when the probe misses, neither is carried forward."""
        previous = ModelConfig(
            alias="local",
            base_url="http://localhost:8000/v1",
            api_key="",
            model="local-model",
            provider="openai-compatible",
            context_window=prior_window,
        )
        down = _probe_result(reachable=False, context_window=None, error="connection refused")
        with (
            patch("turnstone.core.model_registry.load_config", return_value=self._local_cfg()),
            patch("turnstone.core.model_registry.probe_model_endpoint", return_value=down) as probe,
        ):
            reg = load_model_registry(detect_context_windows=True, prior={"local": previous})
        probe.assert_called_once()
        assert reg.get_config("local").context_window == mr_module.FALLBACK_CONTEXT_WINDOW

    def test_probe_exception_is_a_miss_not_a_crash(self, caplog: pytest.LogCaptureFixture) -> None:
        with (
            patch("turnstone.core.model_registry.load_config", return_value=self._local_cfg()),
            patch(
                "turnstone.core.model_registry.probe_model_endpoint",
                side_effect=RuntimeError("can't start new thread"),
            ),
            caplog.at_level(logging.WARNING),
        ):
            reg = load_model_registry(detect_context_windows=True)
        assert reg.get_config("local").context_window == mr_module.FALLBACK_CONTEXT_WINDOW
        assert any("raised RuntimeError" in _rendered(r) for r in caplog.records)

    def test_probe_miss_with_changed_model_does_not_carry_forward(self) -> None:
        running = ModelConfig(
            alias="local",
            base_url="http://localhost:8000/v1",
            api_key="",
            model="previous-model",
            provider="openai-compatible",
            context_window=262144,
            context_window_detected=True,
        )
        down = _probe_result(reachable=False, context_window=None, error="connection refused")
        with (
            patch("turnstone.core.model_registry.load_config", return_value=self._local_cfg()),
            patch("turnstone.core.model_registry.probe_model_endpoint", return_value=down),
        ):
            reg = load_model_registry(detect_context_windows=True, prior={"local": running})
        assert reg.get_config("local").context_window == mr_module.FALLBACK_CONTEXT_WINDOW

    def test_probe_is_bounded_by_its_deadline(
        self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A probe that outlives its deadline is abandoned on its daemon
        thread — boot and a console sync wait at most one probe budget,
        whatever the endpoint (or the resolver) does."""
        release = threading.Event()
        workers: list[threading.Thread] = []

        def slow_probe(*_a: Any, **_kw: Any) -> dict[str, Any]:
            workers.append(threading.current_thread())
            release.wait(5)
            return _probe_result()

        monkeypatch.setattr(mr_module, "_LOADER_PROBE_TIMEOUT", 0.05)
        try:
            with (
                patch("turnstone.core.model_registry.load_config", return_value=self._local_cfg()),
                patch("turnstone.core.model_registry.probe_model_endpoint", side_effect=slow_probe),
                caplog.at_level(logging.WARNING),
            ):
                reg = load_model_registry(detect_context_windows=True)
        finally:
            release.set()
            for worker in workers:
                worker.join(5)
        assert reg.get_config("local").context_window == mr_module.FALLBACK_CONTEXT_WINDOW
        assert any("did not answer within" in _rendered(r) for r in caplog.records)

    def test_rerank_only_definition_is_not_probed(self, caplog: pytest.LogCaptureFixture) -> None:
        """A reranker has no chat window; it takes the fallback silently."""
        cfg = self._local_cfg(capabilities={"supports_rerank": True})
        with (
            patch("turnstone.core.model_registry.load_config", return_value=cfg),
            patch("turnstone.core.model_registry.probe_model_endpoint") as probe,
            caplog.at_level(logging.WARNING),
        ):
            reg = load_model_registry(detect_context_windows=True)
        probe.assert_not_called()
        assert reg.get_config("local").context_window == mr_module.FALLBACK_CONTEXT_WINDOW
        assert not any("'local'" in _rendered(r) for r in caplog.records)

    def test_dynamic_auth_lane_is_not_probed(self, caplog: pytest.LogCaptureFixture) -> None:
        cfg = self._local_cfg(auth_mode="rfc8693_obo", obo_audience="api://gw")
        with (
            patch("turnstone.core.model_registry.load_config", return_value=cfg),
            patch("turnstone.core.model_registry.probe_model_endpoint") as probe,
            caplog.at_level(logging.WARNING),
        ):
            reg = load_model_registry(detect_context_windows=True)
        probe.assert_not_called()
        assert reg.get_config("local").context_window == mr_module.FALLBACK_CONTEXT_WINDOW
        assert any("per-call credential" in _rendered(r) for r in caplog.records)

    def test_db_row_goes_through_the_same_chain(self) -> None:
        from turnstone.core.providers import create_provider

        storage = _MockStorage(
            [
                {
                    "alias": "claude",
                    "model": "claude-opus-4-6",
                    "provider": "anthropic",
                    "base_url": "",
                    "api_key": "sk-db",
                    "context_window": 0,
                    "capabilities": "{}",
                    "enabled": True,
                },
                {
                    "alias": "local",
                    "model": "local-model",
                    "provider": "openai",
                    "base_url": "http://localhost:8000/v1",
                    "api_key": "",
                    "context_window": 0,
                    "capabilities": "{}",
                    "enabled": True,
                },
            ]
        )
        with (
            patch("turnstone.core.model_registry.load_config", return_value={}),
            patch(
                "turnstone.core.model_registry.probe_model_endpoint",
                return_value=_probe_result(),
            ) as probe,
        ):
            reg = load_model_registry(storage=storage, detect_context_windows=True)
        expected = create_provider("anthropic").get_capabilities("claude-opus-4-6").context_window
        assert reg.get_config("claude").context_window == expected
        assert reg.get_config("local").context_window == 262144
        probe.assert_called_once()

    def test_non_finite_context_window_is_treated_as_auto(self) -> None:
        """TOML admits ``inf``; int() raises OverflowError, which must degrade
        to auto-detect, not abort the load."""
        assert mr_module._coerce_context_window(float("inf"), "x") == 0
        assert mr_module._coerce_context_window(-5, "x") == 0
        assert mr_module._coerce_context_window(True, "x") == 0
        assert mr_module._coerce_context_window(131072.0, "x") == 131072

    def test_invalid_context_window_is_treated_as_auto(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        fake_cfg: dict[str, Any] = {
            "models": {
                "claude": {
                    "provider": "anthropic",
                    "model": "claude-opus-4-6",
                    "context_window": "lots",
                }
            }
        }
        with (
            patch("turnstone.core.model_registry.load_config", return_value=fake_cfg),
            caplog.at_level(logging.WARNING),
        ):
            reg = load_model_registry()
        assert reg.get_config("claude").context_window > 0
        assert any("invalid context_window" in _rendered(r) for r in caplog.records)


class TestLocalProviderPlaceholderKey:
    """The server used to resolve ``--api-key or $OPENAI_API_KEY or "dummy"``
    and hand the result to every definition with an empty key. That default
    is gone, so ``create_client`` keeps the same precedence for local
    providers — explicit key, then the SDK's env var, then the placeholder —
    and the registry, the loader's probe and the console Detect button all
    inherit it. Commercial providers stay on the SDK's own env-var rule."""

    def test_openai_compatible_placeholder_when_env_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from turnstone.core.providers import LOCAL_PLACEHOLDER_API_KEY, create_client

        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        client = create_client("openai-compatible", base_url="http://localhost:8000/v1", api_key="")
        assert client.api_key == LOCAL_PLACEHOLDER_API_KEY

    def test_openai_compatible_env_var_wins_over_placeholder(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from turnstone.core.providers import create_client

        monkeypatch.setenv("OPENAI_API_KEY", "s3cret")
        client = create_client("openai-compatible", base_url="http://localhost:8000/v1", api_key="")
        assert client.api_key == "s3cret"

    def test_explicit_key_wins_over_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from turnstone.core.providers import create_client

        monkeypatch.setenv("OPENAI_API_KEY", "s3cret")
        client = create_client(
            "openai-compatible", base_url="http://localhost:8000/v1", api_key="row-key"
        )
        assert client.api_key == "row-key"

    def test_anthropic_compatible_placeholder_when_env_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from turnstone.core.providers import LOCAL_PLACEHOLDER_API_KEY, create_client

        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        client = create_client("anthropic-compatible", base_url="http://localhost:8000", api_key="")
        assert client.api_key == LOCAL_PLACEHOLDER_API_KEY

    def test_commercial_provider_keeps_sdk_rule(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No placeholder for api.openai.com: with neither key nor env var the
        SDK refuses, exactly as before."""
        from turnstone.core.providers import create_client

        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        with pytest.raises(Exception, match="api_key"):
            create_client("openai", base_url="https://api.openai.com/v1", api_key="")

    def test_openai_compatible_requires_base_url(self) -> None:
        """Without a base_url the SDK would default to the commercial API and
        carry a local server's prompts there; refuse at construction, as the
        anthropic-compatible arm already does."""
        from turnstone.core.providers import create_client

        with pytest.raises(ValueError, match="openai-compatible requires base_url"):
            create_client("openai-compatible", base_url="", api_key="k")

    def test_registry_passes_the_definition_key_through(self) -> None:
        cfg = ModelConfig(
            alias="m",
            base_url="http://localhost:8000/v1",
            api_key="",
            model="x",
            provider="openai-compatible",
        )
        reg = ModelRegistry(models={"m": cfg}, default="m")
        with patch("turnstone.core.model_registry.create_client") as cc:
            reg.get_client("m")
        cc.assert_called_once_with(
            "openai-compatible", base_url="http://localhost:8000/v1", api_key="", workspace_id=""
        )


class TestProbeEndpointOnly:
    """The loader's probe must never lift a commercial table window onto a
    local server that happens to serve a commercial model id."""

    @staticmethod
    def _fake_client(model_id: str) -> MagicMock:
        model = MagicMock()
        model.id = model_id
        model.model_dump.return_value = {"id": model_id, "owned_by": "llama.cpp"}
        fast = MagicMock()
        fast.models.list.return_value = MagicMock(data=[model])
        client = MagicMock()
        client.with_options.return_value = fast
        return client

    def test_static_table_off_reports_no_window(self) -> None:
        with patch(
            "turnstone.core.providers.create_client", return_value=self._fake_client(_COMMERCIAL_ID)
        ):
            probed = mr_module.probe_model_endpoint(
                "openai-compatible",
                "http://localhost:8000/v1",
                "",
                _COMMERCIAL_ID,
                static_table=False,
            )
        assert probed["model_found"] is True
        assert probed["context_window"] is None

    def test_static_table_on_is_the_detect_button_behaviour(self) -> None:
        from turnstone.core.providers import lookup_model_capabilities

        with patch(
            "turnstone.core.providers.create_client", return_value=self._fake_client(_COMMERCIAL_ID)
        ):
            probed = mr_module.probe_model_endpoint(
                "openai-compatible", "http://localhost:8000/v1", "", _COMMERCIAL_ID
            )
        known = lookup_model_capabilities("openai", _COMMERCIAL_ID)
        assert known is not None
        assert probed["context_window"] == known["context_window"]

    def test_gateway_listing_resolves_through_the_loader(self) -> None:
        """A gateway definition (OpenRouter shape) with context_window = 0
        gets the gateway's number from its own listing — no static table."""
        model = MagicMock()
        model.id = "openai/gpt-5.6-luna"
        model.model_dump.return_value = {
            "id": "openai/gpt-5.6-luna",
            "context_length": 1_050_000,
            "top_provider": {"context_length": 1_050_000},
        }
        fast = MagicMock()
        fast.models.list.return_value = MagicMock(data=[model])
        client = MagicMock()
        client.with_options.return_value = fast
        fake_cfg: dict[str, Any] = {
            "models": {
                "router": {
                    "provider": "openai",
                    "base_url": "https://openrouter.ai/api/v1",
                    "api_key": "k",
                    "model": "openai/gpt-5.6-luna",
                    "context_window": 0,
                }
            }
        }
        with (
            patch("turnstone.core.model_registry.load_config", return_value=fake_cfg),
            patch("turnstone.core.providers.create_client", return_value=client),
        ):
            reg = load_model_registry(detect_context_windows=True)
        router = reg.get_config("router")
        assert router.provider == "openai-compatible"
        assert router.context_window == 1_050_000
        assert router.context_window_detected is True

    def test_loader_probe_leaves_commercial_id_unresolved(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        fake_cfg: dict[str, Any] = {
            "models": {
                "gw": {
                    "base_url": "http://localhost:8000/v1",
                    "model": _COMMERCIAL_ID,
                    "context_window": 0,
                }
            }
        }
        with (
            patch("turnstone.core.model_registry.load_config", return_value=fake_cfg),
            patch(
                "turnstone.core.providers.create_client",
                return_value=self._fake_client(_COMMERCIAL_ID),
            ),
            caplog.at_level(logging.WARNING),
        ):
            reg = load_model_registry(detect_context_windows=True)
        assert reg.get_config("gw").context_window == mr_module.FALLBACK_CONTEXT_WINDOW
        assert any("does not report a context window" in _rendered(r) for r in caplog.records)


class TestEntryWithoutEndpoint:
    """The server used to hand its own ``--base-url`` / ``[api]`` endpoint
    down to every entry that named neither ``base_url`` nor ``provider``.
    That endpoint is gone and the default provider without a base_url is
    the commercial OpenAI API, so such an entry — almost certainly written
    for a local server — is refused rather than retargeted. The CLI still
    passes its own ``base_url`` and inherits as before."""

    def test_server_load_skips_and_warns(self, caplog: pytest.LogCaptureFixture) -> None:
        fake_cfg: dict[str, Any] = {
            "models": {
                "local": {"model": "qwen", "context_window": 8192},
                "claude": {"provider": "anthropic", "model": "claude-opus-4-6"},
            },
        }
        with (
            patch("turnstone.core.model_registry.load_config", return_value=fake_cfg),
            caplog.at_level(logging.WARNING),
        ):
            reg = load_model_registry()
        assert not reg.has_alias("local")
        assert reg.has_alias("claude")  # a commercial provider needs no base_url
        assert any(
            "'local'" in _rendered(r) and "neither base_url nor provider" in _rendered(r)
            for r in caplog.records
        )

    def test_empty_or_unset_base_url_is_skipped(
        self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A base_url that resolves to nothing — an empty value or an unset
        ${VAR} — would send the entry to the provider's public endpoint."""
        monkeypatch.delenv("TURNSTONE_TEST_UNSET_URL", raising=False)
        fake_cfg: dict[str, Any] = {
            "models": {
                "env": {"model": "qwen", "base_url": "${TURNSTONE_TEST_UNSET_URL}"},
                "blank": {"model": "qwen", "base_url": ""},
                "ok": {"model": "qwen", "base_url": "http://localhost:8000/v1"},
            },
        }
        with (
            patch("turnstone.core.model_registry.load_config", return_value=fake_cfg),
            caplog.at_level(logging.WARNING),
        ):
            reg = load_model_registry()
        assert reg.list_aliases() == ["ok"]
        assert sum("empty base_url" in _rendered(r) for r in caplog.records) == 2

    def test_local_provider_without_base_url_is_skipped_in_both_branches(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An openai-compatible definition with no endpoint would fail on a
        user's first turn; the loader refuses it, config entry or DB row."""
        storage = _MockStorage(
            [
                {
                    "alias": "row",
                    "model": "qwen",
                    "provider": "openai-compatible",
                    "base_url": "",
                    "api_key": "",
                    "context_window": 8192,
                    "capabilities": "{}",
                    "enabled": True,
                }
            ]
        )
        fake_cfg: dict[str, Any] = {
            "models": {"entry": {"model": "qwen", "provider": "openai-compatible"}},
        }
        with (
            patch("turnstone.core.model_registry.load_config", return_value=fake_cfg),
            caplog.at_level(logging.WARNING),
        ):
            reg = load_model_registry(storage=storage, allow_empty=True)
        assert reg.list_aliases() == []
        assert sum("without a base_url" in _rendered(r) for r in caplog.records) == 2

    def test_db_row_with_unset_url_variable_is_skipped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Resolving to nothing would send the row to the provider's public
        endpoint; a stored ${VAR} is skipped whether or not it is set."""
        monkeypatch.delenv("TURNSTONE_TEST_UNSET_URL", raising=False)
        storage = _MockStorage(
            [
                {
                    "alias": "row",
                    "model": _COMMERCIAL_ID,
                    "provider": "openai",
                    "base_url": "${TURNSTONE_TEST_UNSET_URL}",
                    "api_key": "",
                    "context_window": 8192,
                    "capabilities": "{}",
                    "enabled": True,
                }
            ]
        )
        with patch("turnstone.core.model_registry.load_config", return_value={}):
            reg = load_model_registry(storage=storage, allow_empty=True)
        assert reg.list_aliases() == []
        assert reg.skipped_aliases["row"].startswith(
            "base_url contains ${TURNSTONE_TEST_UNSET_URL}"
        )

    def test_table_provider_entry_that_relied_on_api_section_loads_with_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Empty base_url is the normal shape for a commercial provider, so the
        entry loads — but the log says it now targets the public endpoint."""
        fake_cfg: dict[str, Any] = {
            "api": {"base_url": "http://localhost:8000/v1"},
            "models": {"claude": {"provider": "anthropic", "model": _ANTHROPIC_ID}},
        }
        with (
            patch("turnstone.core.model_registry.load_config", return_value=fake_cfg),
            caplog.at_level(logging.WARNING),
        ):
            reg = load_model_registry()
        assert reg.get_config("claude").base_url == ""
        assert any(
            "'claude'" in _rendered(r) and "public endpoint" in _rendered(r) for r in caplog.records
        )

    def test_openai_entry_that_relied_on_api_section_is_refused(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """provider = "openai" with no base_url is the commercial API. While the
        file still carries [api] base_url the entry was written to inherit it,
        so it is refused; without [api] the same entry is the commercial API."""
        entry: dict[str, Any] = {"provider": "openai", "model": _COMMERCIAL_ID}
        with (
            patch(
                "turnstone.core.model_registry.load_config",
                return_value={
                    "api": {"base_url": "http://localhost:8000/v1"},
                    "models": {"m": entry},
                },
            ),
            caplog.at_level(logging.WARNING),
        ):
            reg = load_model_registry(allow_empty=True)
        assert reg.list_aliases() == []
        assert any("[api] base_url" in _rendered(r) for r in caplog.records)

        with patch(
            "turnstone.core.model_registry.load_config", return_value={"models": {"m": entry}}
        ):
            reg = load_model_registry()
        assert reg.get_config("m").provider == "openai"
        assert reg.get_config("m").base_url == ""

    def test_cli_load_inherits_without_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        fake_cfg: dict[str, Any] = {
            "models": {"local": {"model": "qwen", "context_window": 8192}},
        }
        with (
            patch("turnstone.core.model_registry.load_config", return_value=fake_cfg),
            caplog.at_level(logging.WARNING),
        ):
            reg = load_model_registry(base_url="http://localhost:8000/v1")
        assert reg.get_config("local").base_url == "http://localhost:8000/v1"
        assert not any("neither base_url nor provider" in _rendered(r) for r in caplog.records)


def test_unknown_provider_does_not_crash_the_load(caplog: pytest.LogCaptureFixture) -> None:
    """A typo'd provider has always failed at first use, not at boot; the
    window resolver must keep it that way."""
    fake_cfg: dict[str, Any] = {
        "models": {"typo": {"provider": "openia", "model": "gpt-5", "context_window": 0}}
    }
    with (
        patch("turnstone.core.model_registry.load_config", return_value=fake_cfg),
        caplog.at_level(logging.WARNING),
    ):
        reg = load_model_registry(detect_context_windows=True)
    assert reg.get_config("typo").context_window == mr_module.FALLBACK_CONTEXT_WINDOW
    assert any("not recognised" in _rendered(r) for r in caplog.records)


class TestAnthropicWorkspaceId:
    """``server_compat.anthropic_workspace_id`` → the ``anthropic-workspace-id``
    header on every request the alias's client makes."""

    @staticmethod
    def _probe(client: Any) -> dict[str, Any]:
        """Issue one Messages call and one model listing through a real
        HTTPX2 mock transport; return the workspace header each carried."""
        import httpx2

        seen: dict[str, Any] = {}

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen[request.url.path] = request.headers.get("anthropic-workspace-id")
            if request.url.path.endswith("/models"):
                return httpx2.Response(
                    200,
                    json={"data": [], "has_more": False, "first_id": None, "last_id": None},
                    request=request,
                )
            return httpx2.Response(
                200,
                json={
                    "id": "m",
                    "type": "message",
                    "role": "assistant",
                    "model": "m",
                    "content": [],
                    "stop_reason": "end_turn",
                    "stop_sequence": None,
                    "usage": {"input_tokens": 1, "output_tokens": 0},
                },
                request=request,
            )

        wired = client.with_options(
            http_client=httpx2.Client(transport=httpx2.MockTransport(handler)), max_retries=0
        )
        wired.messages.create(model="m", max_tokens=8, messages=[{"role": "user", "content": "hi"}])
        wired.models.list()
        return seen

    @pytest.mark.parametrize(
        ("provider", "base_url"),
        [("anthropic", ""), ("anthropic-compatible", "http://localhost:8000")],
    )
    def test_create_client_pins_the_header_on_anthropic_protocol_lanes(
        self, provider: str, base_url: str
    ) -> None:
        from turnstone.core.providers import create_client

        client = create_client(
            provider, base_url=base_url, api_key="k", workspace_id="wrkspc_01ABC"
        )
        try:
            seen = self._probe(client)
        finally:
            client.close()
        assert seen == {"/v1/messages": "wrkspc_01ABC", "/v1/models": "wrkspc_01ABC"}

    def test_create_client_without_a_workspace_sends_no_header(self) -> None:
        from turnstone.core.providers import create_client

        client = create_client("anthropic", base_url="", api_key="k")
        try:
            seen = self._probe(client)
        finally:
            client.close()
        assert seen == {"/v1/messages": None, "/v1/models": None}

    def test_header_survives_the_minted_credential_copy(self) -> None:
        """The OBO path issues calls on ``with_options(api_key=...)``; the
        client-level header must ride that copy too."""
        from turnstone.core.providers import create_client

        client = create_client("anthropic", base_url="", api_key="k", workspace_id="wrkspc_01ABC")
        try:
            seen = self._probe(client.with_options(api_key="minted"))
        finally:
            client.close()
        assert seen == {"/v1/messages": "wrkspc_01ABC", "/v1/models": "wrkspc_01ABC"}

    def test_openai_lanes_ignore_the_keyword(self) -> None:
        from turnstone.core.providers import create_client

        client = create_client(
            "openai",
            base_url="https://api.openai.example/v1",
            api_key="k",
            workspace_id="wrkspc_01ABC",
        )
        try:
            assert "anthropic-workspace-id" not in client.default_headers
        finally:
            client.close()

    def test_registry_passes_the_scope_from_server_compat(self) -> None:
        cfg = ModelConfig(
            alias="a",
            base_url="",
            api_key="k",
            model="m",
            provider="anthropic",
            server_compat={"anthropic_workspace_id": "wrkspc_01ABC"},
        )
        reg = ModelRegistry(models={"a": cfg}, default="a")
        with patch("turnstone.core.model_registry.create_client") as factory:
            reg.get_client("a")
        factory.assert_called_once_with(
            "anthropic", base_url="", api_key="k", workspace_id="wrkspc_01ABC"
        )

    def test_reload_rebuilds_the_client_when_the_scope_changes(self) -> None:
        """A workspace edit changes what every request carries, so the cached
        client (and its pinned default header) must be retired like a key or
        base_url change."""
        models = {"a": ModelConfig("a", "", "key", "m", provider="anthropic")}
        reg = ModelRegistry(models=models, default="a")
        reg.get_client("a")

        scoped = ModelConfig(
            "a",
            "",
            "key",
            "m",
            provider="anthropic",
            server_compat={"anthropic_workspace_id": "wrkspc_01ABC"},
        )
        reg.reload({"a": scoped}, "a", app_state=_KEYED_STATE)

        assert "a" not in reg._clients

    @pytest.mark.parametrize(
        ("value", "reason"),
        [
            ("wrkspc_01ABC", None),
            ("x" * 128, None),
            ("x" * 129, "longer than 128 characters"),
            ("wrkspc 01", "contains whitespace, control or non-ASCII characters"),
            ("wrkspc\t01", "contains whitespace, control or non-ASCII characters"),
            ("wrkspc_é", "contains whitespace, control or non-ASCII characters"),
        ],
    )
    def test_workspace_id_error_is_the_header_rule(self, value: str, reason: str | None) -> None:
        from turnstone.core.model_registry import anthropic_workspace_id_error

        assert anthropic_workspace_id_error(value) == reason

    def test_loader_refuses_an_unsendable_scope_and_drops_an_empty_one(self) -> None:
        from turnstone.core.model_registry import ModelAuthConfigError

        def cfg_with(scope: Any) -> dict[str, Any]:
            return {
                "models": {
                    "claude": {
                        "model": "claude-sonnet-4-6",
                        "provider": "anthropic",
                        "api_key": "k",
                        "capabilities": {"server_compat": {"anthropic_workspace_id": scope}},
                    }
                },
                "model": {"default": "claude"},
            }

        with (
            patch("turnstone.core.model_registry.load_config", return_value=cfg_with("bad id")),
            pytest.raises(ModelAuthConfigError, match="anthropic_workspace_id contains"),
        ):
            load_model_registry(base_url="", api_key="", model="")

        with patch("turnstone.core.model_registry.load_config", return_value=cfg_with("")):
            reg = load_model_registry(base_url="", api_key="", model="")
        _, _, cfg, _ = reg.resolve()
        assert cfg.server_compat == {}

        with patch(
            "turnstone.core.model_registry.load_config", return_value=cfg_with("wrkspc_01ABC")
        ):
            reg = load_model_registry(base_url="", api_key="", model="")
        _, _, cfg, _ = reg.resolve()
        assert cfg.server_compat == {"anthropic_workspace_id": "wrkspc_01ABC"}

    def test_loader_drops_a_scope_the_provider_never_sends(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A stored scope on a non-Anthropic provider is inert: the console
        refuses new staging, but an already-stored value must not make the
        alias unloadable, so the loader drops it with a warning (the same
        tolerance a stale audience gets on a static row)."""
        fake_cfg: dict[str, Any] = {
            "models": {
                "gpt": {
                    "model": "gpt-5",
                    "provider": "openai",
                    "api_key": "k",
                    "capabilities": {"server_compat": {"anthropic_workspace_id": "wrkspc_01ABC"}},
                }
            },
            "model": {"default": "gpt"},
        }
        with (
            patch("turnstone.core.model_registry.load_config", return_value=fake_cfg),
            caplog.at_level(logging.WARNING),
        ):
            reg = load_model_registry(base_url="", api_key="", model="")
        _, _, cfg, _ = reg.resolve()
        assert cfg.server_compat == {}
        warned = [r for r in caplog.records if "anthropic_workspace_id" in r.getMessage()]
        assert len(warned) == 1

        # Dropped before any shape check: a value no client sends must not be
        # able to take every other alias down with a load-time refusal.
        fake_cfg["models"]["gpt"]["capabilities"]["server_compat"]["anthropic_workspace_id"] = (
            "my workspace"
        )
        with patch("turnstone.core.model_registry.load_config", return_value=fake_cfg):
            reg = load_model_registry(base_url="", api_key="", model="")
        _, _, cfg, _ = reg.resolve()
        assert cfg.server_compat == {}

    @staticmethod
    def _scoped_entry(scope: str) -> dict[str, Any]:
        return {
            "base_url": "http://localhost:8000",
            "provider": "anthropic-compatible",
            "model": "m",
            "context_window": 0,
            "capabilities": {"server_compat": {"anthropic_workspace_id": scope}},
        }

    def test_auto_detect_probes_each_workspace_separately(self) -> None:
        """Two definitions that differ only by workspace may be served by
        different backends behind one gateway, so the probe identity includes
        the scope and each alias gets its own detected window."""
        fake_cfg = {
            "models": {
                "ws-a": self._scoped_entry("wrkspc_A"),
                "ws-b": self._scoped_entry("wrkspc_B"),
            }
        }
        windows = {"wrkspc_A": 65536, "wrkspc_B": 8192}

        def probe(*_args: Any, **kwargs: Any) -> dict[str, Any]:
            return _probe_result(context_window=windows[kwargs["workspace_id"]])

        with (
            patch("turnstone.core.model_registry.load_config", return_value=fake_cfg),
            patch("turnstone.core.model_registry.probe_model_endpoint", side_effect=probe) as spy,
        ):
            reg = load_model_registry(detect_context_windows=True)
        assert spy.call_count == 2
        assert reg.get_config("ws-a").context_window == 65536
        assert reg.get_config("ws-b").context_window == 8192

    def test_reload_carry_over_never_crosses_a_workspace(self) -> None:
        """The detected-window carry-over is keyed on the endpoint identity;
        a definition whose scope changed is a different endpoint, so a probe
        miss falls back instead of inheriting the previous workspace's window."""
        running = ModelConfig(
            alias="ws",
            base_url="http://localhost:8000",
            api_key="",
            model="m",
            provider="anthropic-compatible",
            context_window=65536,
            context_window_detected=True,
            server_compat={"anthropic_workspace_id": "wrkspc_A"},
        )
        down = _probe_result(reachable=False, context_window=None, error="connection refused")

        def load(scope: str) -> int:
            fake_cfg = {"models": {"ws": self._scoped_entry(scope)}}
            with (
                patch("turnstone.core.model_registry.load_config", return_value=fake_cfg),
                patch("turnstone.core.model_registry.probe_model_endpoint", return_value=down),
            ):
                reg = load_model_registry(detect_context_windows=True, prior={"ws": running})
            return reg.get_config("ws").context_window

        assert load("wrkspc_A") == 65536  # same scope: the detected window is kept
        assert load("wrkspc_B") == mr_module.FALLBACK_CONTEXT_WINDOW


def test_session_built_judges_carry_the_session_usage_recorder() -> None:
    """Judges the session constructs record through ``_record_aux_usage``:
    both ``_ensure_judge`` and ``_ensure_output_guard_judge`` hand the sink
    over, so judge spend reaches the usage rows the way sub-agent spend does,
    attributed to each judge's own alias model rather than the session model."""
    from turnstone.core.judge import JudgeConfig

    reg = ModelRegistry(
        models={
            "gw": ModelConfig("gw", "http://primary.example/v1", "k", "primary-model"),
            "intent": ModelConfig("intent", "http://intent.example/v1", "k", "intent-model"),
        },
        default="gw",
    )
    session = _make_session(
        registry=reg,
        model_alias="gw",
        judge_config=JudgeConfig(
            model="intent",
            output_guard_llm=True,
            output_guard_model="intent",
        ),
    )

    intent = session._ensure_judge()
    guard = session._ensure_output_guard_judge()

    assert intent is not None
    assert guard is not None
    assert intent._record_usage == session._record_aux_usage
    assert guard._record_usage == session._record_aux_usage
    assert intent._model == "intent-model"
    assert guard._model == "intent-model"
