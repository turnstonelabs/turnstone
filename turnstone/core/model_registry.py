"""Model registry — named model configurations with fallback routing.

Manages multiple OpenAI-compatible API backends so workstreams can select
their model at creation time or switch mid-session.  Supports a fallback
chain for resilience when the primary model is unreachable.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any

from openai import OpenAI

from turnstone.core.config import load_config

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Model configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelConfig:
    """Immutable configuration for a single model endpoint."""

    alias: str
    base_url: str
    api_key: str = field(repr=False)
    model: str
    context_window: int = 131072


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class ModelRegistry:
    """Holds named model configurations with thread-safe lazy client creation.

    Args:
        models: Mapping of alias → ModelConfig.
        default: Alias of the default model.
        fallback: Ordered list of aliases to try when the primary model fails.
        agent_model: Optional alias for plan/task sub-agents.
    """

    def __init__(
        self,
        models: dict[str, ModelConfig],
        default: str,
        fallback: list[str] | None = None,
        agent_model: str | None = None,
    ) -> None:
        if not models:
            raise ValueError("ModelRegistry requires at least one model config")
        if default not in models:
            raise ValueError(f"Default model '{default}' not found in registry")
        if fallback:
            for alias in fallback:
                if alias not in models:
                    raise ValueError(f"Fallback model '{alias}' not found in registry")
        if agent_model and agent_model not in models:
            raise ValueError(f"Agent model '{agent_model}' not found in registry")

        self._models = dict(models)
        self.default = default
        self.fallback = list(fallback) if fallback else []
        self.agent_model = agent_model
        self._clients: dict[str, OpenAI] = {}
        self._client_lock = threading.Lock()

    # -- query methods -------------------------------------------------------

    def get_client(self, alias: str) -> OpenAI:
        """Get or lazily create an OpenAI client for *alias*. Thread-safe."""
        if alias not in self._models:
            raise ValueError(f"Unknown model alias: {alias}")
        with self._client_lock:
            if alias not in self._clients:
                cfg = self._models[alias]
                self._clients[alias] = OpenAI(
                    base_url=cfg.base_url,
                    api_key=cfg.api_key,
                )
            return self._clients[alias]

    def get_config(self, alias: str) -> ModelConfig:
        """Return the ModelConfig for *alias*."""
        if alias not in self._models:
            raise ValueError(f"Unknown model alias: {alias}")
        return self._models[alias]

    def has_alias(self, alias: str) -> bool:
        """Check if *alias* exists in the registry."""
        return alias in self._models

    def list_aliases(self) -> list[str]:
        """Return all registered model aliases."""
        return list(self._models.keys())

    def resolve(self, alias: str | None = None) -> tuple[OpenAI, str, ModelConfig]:
        """Resolve *alias* to ``(client, model_name, config)``.

        Uses the default alias when *alias* is ``None``.
        """
        alias = alias or self.default
        cfg = self.get_config(alias)
        return self.get_client(alias), cfg.model, cfg

    @property
    def count(self) -> int:
        """Number of registered models."""
        return len(self._models)

    # -- lifecycle -----------------------------------------------------------

    def shutdown(self) -> None:
        """Close all OpenAI client connections."""
        with self._client_lock:
            for client in self._clients.values():
                client.close()
            self._clients.clear()


# ---------------------------------------------------------------------------
# Loading from config
# ---------------------------------------------------------------------------


def load_model_registry(
    base_url: str,
    api_key: str,
    model: str,
    context_window: int = 131072,
) -> ModelRegistry:
    """Build a ModelRegistry from CLI args and ``config.toml``.

    Precedence:

    1. ``[models.*]`` sections in config.toml define named models.
    2. CLI ``--base-url`` / ``--api-key`` / ``--model`` always create a
       ``"default"`` entry (overrides any ``[models.default]`` section).
    3. ``[model].default``, ``[model].fallback``, ``[model].agent_model``
       control routing.
    4. If no ``[models.*]`` sections exist, a single-entry registry is built
       from the CLI args.
    """
    cfg = load_config()
    models_section: dict[str, Any] = cfg.get("models", {})
    model_section: dict[str, Any] = cfg.get("model", {})

    configs: dict[str, ModelConfig] = {}

    # Build configs from [models.*] sections
    for alias, entry in models_section.items():
        if not isinstance(entry, dict):
            continue
        model_name = entry.get("model", "")
        if not model_name:
            log.warning("Model entry '%s' has no model name, skipping", alias)
            continue
        configs[alias] = ModelConfig(
            alias=alias,
            base_url=entry.get("base_url", base_url),
            api_key=entry.get("api_key", api_key),
            model=model_name,
            context_window=entry.get("context_window", context_window),
        )

    # Ensure a "default" entry from CLI args
    configs["default"] = ModelConfig(
        alias="default",
        base_url=base_url,
        api_key=api_key,
        model=model,
        context_window=context_window,
    )

    # Determine default alias
    default_alias = model_section.get("default", "default")
    if default_alias not in configs:
        log.warning("Configured default model '%s' not found, using 'default'", default_alias)
        default_alias = "default"

    # Fallback chain
    fallback_raw = model_section.get("fallback", [])
    fallback: list[str] = []
    if isinstance(fallback_raw, list):
        for alias in fallback_raw:
            if alias in configs:
                fallback.append(alias)
            else:
                log.warning("Fallback alias '%s' not found in models, ignoring", alias)

    # Agent model
    agent_model = model_section.get("agent_model")
    if agent_model and agent_model not in configs:
        log.warning("Configured agent_model '%s' not found, ignoring", agent_model)
        agent_model = None

    return ModelRegistry(
        models=configs,
        default=default_alias,
        fallback=fallback,
        agent_model=agent_model,
    )
