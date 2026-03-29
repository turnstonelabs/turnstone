"""LLM provider adapters — pluggable backends for different APIs."""

from __future__ import annotations

import threading
from typing import Any

from turnstone.core.providers._openai import OpenAIProvider
from turnstone.core.providers._protocol import (
    CompletionResult,
    LLMProvider,
    ModelCapabilities,
    StreamChunk,
    ToolCallDelta,
    UsageInfo,
)

__all__ = [
    "CompletionResult",
    "LLMProvider",
    "ModelCapabilities",
    "OpenAIProvider",
    "StreamChunk",
    "ToolCallDelta",
    "UsageInfo",
    "create_client",
    "create_provider",
    "list_known_models",
    "lookup_model_capabilities",
]

# Singleton instances (stateless, safe to share)
_provider_lock = threading.Lock()
_openai_provider = OpenAIProvider()
_anthropic_provider: LLMProvider | None = None


def create_provider(provider_name: str) -> LLMProvider:
    """Return a provider adapter for the given provider name. Thread-safe."""
    global _anthropic_provider  # noqa: PLW0603
    if provider_name in ("openai", "openai-compatible"):
        return _openai_provider
    if provider_name == "anthropic":
        with _provider_lock:
            if _anthropic_provider is None:
                from turnstone.core.providers._anthropic import AnthropicProvider

                _anthropic_provider = AnthropicProvider()
            return _anthropic_provider
    raise ValueError(
        f"Unknown provider: {provider_name!r}. Supported: openai, anthropic, openai-compatible"
    )


def create_client(provider_name: str, *, base_url: str, api_key: str) -> Any:
    """Create an SDK client for the given provider."""
    if provider_name in ("openai", "openai-compatible"):
        from openai import OpenAI

        if base_url:
            return OpenAI(base_url=base_url, api_key=api_key)
        return OpenAI(api_key=api_key)
    if provider_name == "anthropic":
        from turnstone.core.providers._anthropic import _ensure_anthropic

        anthropic = _ensure_anthropic()
        kwargs: dict[str, str] = {"api_key": api_key}
        if base_url and base_url != "https://api.anthropic.com":
            kwargs["base_url"] = base_url
        return anthropic.Anthropic(**kwargs)
    raise ValueError(
        f"Unknown provider: {provider_name!r}. Supported: openai, anthropic, openai-compatible"
    )


def lookup_model_capabilities(provider: str, model: str) -> dict[str, Any] | None:
    """Return static capabilities for a known model, or ``None`` if unknown.

    The returned dict has JSON-friendly values (tuples converted to lists).
    Returns ``None`` for ``openai-compatible`` (no static table for local models).
    """
    import dataclasses

    if provider == "openai-compatible":
        return None
    prov = create_provider(provider)
    caps = prov.get_capabilities(model)
    default = prov.get_capabilities("")
    if caps is default:
        return None
    result = dataclasses.asdict(caps)
    # Convert tuples to lists for JSON serialisation
    for key, val in result.items():
        if isinstance(val, tuple):
            result[key] = list(val)
    return result


def list_known_models(provider: str) -> list[str]:
    """Return the model name prefixes in the static capability table."""
    if provider == "openai":
        from turnstone.core.providers._openai import _OPENAI_CAPABILITIES

        return sorted(_OPENAI_CAPABILITIES.keys())
    if provider == "anthropic":
        from turnstone.core.providers._anthropic import _ANTHROPIC_CAPABILITIES

        return sorted(_ANTHROPIC_CAPABILITIES.keys())
    return []
