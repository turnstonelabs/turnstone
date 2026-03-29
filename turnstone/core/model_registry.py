"""Model registry — named model configurations with fallback routing.

Manages multiple LLM API backends so workstreams can select their model at
creation time or switch mid-session.  Supports a fallback chain for
resilience when the primary model is unreachable.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from turnstone.core.config import load_config
from turnstone.core.log import get_logger
from turnstone.core.providers import LLMProvider, create_client, create_provider

log = get_logger(__name__)


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
    context_window: int = 32768
    provider: str = "openai"
    capabilities: dict[str, Any] = field(default_factory=dict)
    source: str = ""  # "config", "db", or "" (CLI default)


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
        self._clients: dict[str, Any] = {}
        self._providers: dict[str, LLMProvider] = {}
        self._client_lock = threading.Lock()

    # -- query methods -------------------------------------------------------

    def get_client(self, alias: str) -> Any:
        """Get or lazily create an API client for *alias*. Thread-safe."""
        with self._client_lock:
            if alias not in self._models:
                raise ValueError(f"Unknown model alias: {alias}")
            if alias not in self._clients:
                cfg = self._models[alias]
                self._clients[alias] = create_client(
                    cfg.provider, base_url=cfg.base_url, api_key=cfg.api_key
                )
            return self._clients[alias]

    def get_provider(self, alias: str) -> LLMProvider:
        """Get the ``LLMProvider`` for *alias*. Thread-safe, cached."""
        with self._client_lock:
            if alias not in self._models:
                raise ValueError(f"Unknown model alias: {alias}")
            if alias not in self._providers:
                cfg = self._models[alias]
                self._providers[alias] = create_provider(cfg.provider)
            return self._providers[alias]

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

    def resolve(self, alias: str | None = None) -> tuple[Any, str, ModelConfig]:
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

    @property
    def models(self) -> dict[str, ModelConfig]:
        """Return a copy of the models dict (public accessor for reload)."""
        return dict(self._models)

    # -- lifecycle -----------------------------------------------------------

    def reload(
        self,
        models: dict[str, ModelConfig],
        default: str,
        fallback: list[str] | None = None,
        agent_model: str | None = None,
    ) -> None:
        """Hot-reload all model configs. Thread-safe; clears cached clients.

        Validates arguments before mutating state so a bad reload
        does not leave the registry in an inconsistent state.
        """
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
        with self._client_lock:
            self._models = dict(models)
            self.default = default
            self.fallback = list(fallback) if fallback else []
            self.agent_model = agent_model
            for client in self._clients.values():
                if hasattr(client, "close"):
                    client.close()
            self._clients.clear()
            self._providers.clear()

    def shutdown(self) -> None:
        """Close all cached client connections."""
        with self._client_lock:
            for client in self._clients.values():
                if hasattr(client, "close"):
                    client.close()
            self._clients.clear()
            self._providers.clear()


# ---------------------------------------------------------------------------
# Loading from config
# ---------------------------------------------------------------------------


def _resolve_env_vars(value: str) -> str:
    """Expand ``${VAR}`` patterns in *value* using environment variables.

    Unresolved variables are replaced with empty strings.
    """
    import os
    import re

    def _replace(m: re.Match[str]) -> str:
        return os.environ.get(m.group(1), "")

    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", _replace, value)


def load_model_registry(
    base_url: str,
    api_key: str,
    model: str,
    context_window: int = 32768,
    provider: str = "openai",
    storage: Any | None = None,
) -> ModelRegistry:
    """Build a ModelRegistry from CLI args, ``config.toml``, and database.

    Precedence (highest to lowest):

    1. ``[models.*]`` sections in config.toml define named models
       (``source="config"``).  These override DB entries with the same
       alias in-memory only — the DB rows are never modified.
    2. Database model definitions (``source="db"``), loaded when
       *storage* is provided.
    3. CLI ``--base-url`` / ``--api-key`` / ``--model`` always create a
       ``"default"`` entry.
    4. ``[model].default``, ``[model].fallback``, ``[model].agent_model``
       control routing.
    """
    import json as _json

    cfg = load_config()
    models_section: dict[str, Any] = cfg.get("models", {})
    model_section: dict[str, Any] = cfg.get("model", {})

    configs: dict[str, ModelConfig] = {}

    # 1. Load DB model definitions (lowest priority, overridden by config.toml)
    if storage is not None:
        try:
            for row in storage.list_model_definitions(enabled_only=True):
                alias = row["alias"]
                caps: dict[str, Any] = {}
                if row.get("capabilities"):
                    try:
                        parsed = _json.loads(row["capabilities"])
                        if isinstance(parsed, dict):
                            caps = parsed
                    except (_json.JSONDecodeError, TypeError):
                        pass
                row_provider = row.get("provider", "openai")
                row_model = row["model"]
                # 0 = auto-detect: inherit CLI-detected context_window,
                # same fallback chain as config.toml models
                row_ctx = row.get("context_window", 0) or context_window
                configs[alias] = ModelConfig(
                    alias=alias,
                    base_url=_resolve_env_vars(row.get("base_url", "")),
                    api_key=_resolve_env_vars(row.get("api_key", "")),
                    model=row_model,
                    context_window=row_ctx,
                    provider=row_provider,
                    capabilities=caps,
                    source="db",
                )
        except Exception:
            log.warning("Failed to load model definitions from storage", exc_info=True)

    # 2. Build configs from [models.*] sections (overrides DB for same alias)
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
            provider=entry.get("provider", "openai"),
            capabilities=entry.get("capabilities", {})
            if isinstance(entry.get("capabilities"), dict)
            else {},
            source="config",
        )

    # 3. Ensure a "default" entry from CLI args (only if not already defined
    # by config.toml or DB — those take precedence)
    if "default" not in configs:
        configs["default"] = ModelConfig(
            alias="default",
            base_url=base_url,
            api_key=api_key,
            model=model,
            context_window=context_window,
            provider=provider,
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


# ---------------------------------------------------------------------------
# Model auto-detection
# ---------------------------------------------------------------------------


def _select_best_model(model_ids: list[str], provider: str) -> str:
    """Pick the best default model from a list of available model IDs.

    - **anthropic**: latest Opus model (highest generation number).
    - **openai**: latest base GPT-N.N model (not mini/nano/pro variants).
    - **other** (local servers): first model in the list.
    """
    import re

    if provider == "anthropic":
        # Prefer opus, then sonnet, then haiku — highest generation first
        opus = [m for m in model_ids if "opus" in m]
        if opus:
            opus.sort(reverse=True)
            return opus[0]
        sonnet = [m for m in model_ids if "sonnet" in m]
        if sonnet:
            sonnet.sort(reverse=True)
            return sonnet[0]
        return model_ids[0]

    if provider == "openai":
        # Prefer base gpt-N.N (not mini/nano/pro/codex/chat variants)
        base_pattern = re.compile(r"^gpt-(\d+(?:\.\d+)?)(?:-\d+)?$")
        base_models: list[tuple[float, str]] = []
        for m in model_ids:
            match = base_pattern.match(m)
            if match:
                version = float(match.group(1))
                base_models.append((version, m))
        if base_models:
            base_models.sort(key=lambda x: x[0], reverse=True)
            return base_models[0][1]
        return model_ids[0]

    return model_ids[0]


def detect_model(
    client: Any,
    log_fn: Any = print,
    provider: str = "openai",
    *,
    fatal: bool = True,
) -> tuple[str | None, int | None]:
    """Auto-detect the model and context window from the API's models endpoint.

    Returns ``(model_id, context_window)`` where *context_window* is
    ``None`` when the backend does not expose it.

    For multi-model APIs (Anthropic, OpenAI), selects a sensible default:
    latest Opus for Anthropic, latest base GPT model for OpenAI.
    For local single-model servers (vLLM, llama.cpp), uses the first model.

    Calls ``log_fn`` for informational messages (defaults to ``print``).

    When *fatal* is ``True`` (default), raises ``SystemExit`` on failure.
    When ``False``, returns ``(None, None)`` so the server can start in
    degraded mode (useful for cluster deployments where the LLM backend
    may not be available at startup).
    """
    try:
        # Use a short timeout for startup detection — the default OpenAI client
        # timeout is 600s read which blocks the main thread for minutes when the
        # backend is unreachable (TCP SYN dropped → kernel retransmit timeout).
        # Disable retries (default 2) to avoid compounding the delay.
        fast_client = client.with_options(timeout=10.0, max_retries=0)
        models = fast_client.models.list()
        if not models.data:
            if fatal:
                log_fn("Error: No models found at server. Use --model to specify.")
                raise SystemExit(1)
            log_fn("Warning: No models found at server — starting in degraded mode.")
            return None, None

        all_ids = [x.id for x in models.data]
        selected_id = _select_best_model(all_ids, provider)
        m = next(x for x in models.data if x.id == selected_id)

        if len(models.data) > 1:
            log_fn(f"Available models: {', '.join(all_ids)}")
            log_fn(f"Using: {m.id} (override with --model)")

        ctx: int | None = None
        if provider == "anthropic":
            from turnstone.core.providers._anthropic import AnthropicProvider

            ctx = AnthropicProvider().get_capabilities(m.id).context_window
        else:
            # OpenAI-compatible: extract from backend metadata (llama.cpp, vLLM)
            meta = m.model_dump().get("meta")
            if isinstance(meta, dict):
                n_ctx = meta.get("n_ctx_train")
                if isinstance(n_ctx, int) and n_ctx > 0:
                    ctx = n_ctx
        return m.id, ctx
    except SystemExit:
        raise
    except Exception as e:
        if fatal:
            log_fn(f"Error: Could not connect to server: {e}")
            log_fn("Is the model server running? Start it or use --base-url to point elsewhere.")
            raise SystemExit(1) from e
        log_fn(f"Warning: Could not connect to LLM backend: {e}")
        log_fn("Starting in degraded mode — requests will fail until backend is reachable.")
        return None, None


def probe_model_endpoint(
    provider: str,
    base_url: str,
    api_key: str,
    target_model: str = "",
) -> dict[str, Any]:
    """Stateless probe of a model endpoint.

    Creates a temporary SDK client, calls ``/v1/models``, and returns
    reachability status, available model IDs, detected context window,
    and server type.  Used by the admin *Detect* button — never persists
    state or stores the API key.
    """
    from turnstone.core.providers import create_client

    result: dict[str, Any] = {
        "reachable": False,
        "model_found": None,
        "available_models": [],
        "context_window": None,
        "server_type": None,
        "error": None,
    }
    client = None
    try:
        client = create_client(provider, base_url=base_url, api_key=api_key)
        fast = client.with_options(timeout=10.0, max_retries=0)
        models = fast.models.list()
        if not models.data:
            result["reachable"] = True
            result["error"] = "No models found at endpoint"
            return result

        all_ids = [m.id for m in models.data]
        result["reachable"] = True
        result["available_models"] = all_ids

        # Determine which model to inspect for context_window
        if target_model:
            result["model_found"] = target_model in all_ids
            inspect_id = target_model if result["model_found"] else all_ids[0]
        else:
            inspect_id = all_ids[0]

        inspect_obj = next((m for m in models.data if m.id == inspect_id), None)

        # --- context window detection ---
        if provider == "anthropic":
            from turnstone.core.providers import lookup_model_capabilities

            known = lookup_model_capabilities("anthropic", inspect_id)
            if known is not None:
                result["context_window"] = known["context_window"]
            result["server_type"] = "anthropic"
        else:
            # OpenAI-compatible path
            _detect_openai_compat(result, inspect_obj, inspect_id, base_url)
    except Exception as exc:
        err_msg = str(exc)
        if len(err_msg) > 500:
            err_msg = err_msg[:500] + "..."
        result["error"] = err_msg
    finally:
        if client is not None and hasattr(client, "close"):
            client.close()
    return result


def _detect_openai_compat(
    result: dict[str, Any],
    model_obj: Any,
    model_id: str,
    base_url: str,
) -> None:
    """Fill context_window and server_type for an OpenAI-compatible endpoint."""

    meta: dict[str, Any] | None = None
    owned_by: str = ""
    if model_obj is not None:
        dumped = model_obj.model_dump()
        raw_meta = dumped.get("meta")
        if isinstance(raw_meta, dict):
            meta = raw_meta
        owned_by = str(dumped.get("owned_by", ""))

    # Context window: prefer backend metadata, fall back to static table
    # (only for known models — the default 200k would be misleading for local servers)
    if meta is not None:
        n_ctx = meta.get("n_ctx_train")
        if isinstance(n_ctx, int) and n_ctx > 0:
            result["context_window"] = n_ctx
    if result["context_window"] is None:
        from turnstone.core.providers import lookup_model_capabilities

        known = lookup_model_capabilities("openai", model_id)
        if known is not None:
            result["context_window"] = known["context_window"]

    # Server type heuristics
    if base_url and "api.openai.com" in base_url:
        result["server_type"] = "openai"
    elif meta is not None and "n_ctx_train" in meta:
        result["server_type"] = "llama.cpp"
    elif "sglang" in owned_by.lower():
        result["server_type"] = "sglang"
    elif "/" in (model_id or ""):
        result["server_type"] = "vllm"
    else:
        result["server_type"] = "openai-compatible"
