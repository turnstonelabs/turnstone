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
]

# Singleton instances (stateless, safe to share)
_provider_lock = threading.Lock()
_openai_provider = OpenAIProvider()
_anthropic_provider: LLMProvider | None = None


def create_provider(provider_name: str) -> LLMProvider:
    """Return a provider adapter for the given provider name. Thread-safe."""
    global _anthropic_provider  # noqa: PLW0603
    if provider_name == "openai":
        return _openai_provider
    if provider_name == "anthropic":
        with _provider_lock:
            if _anthropic_provider is None:
                from turnstone.core.providers._anthropic import AnthropicProvider

                _anthropic_provider = AnthropicProvider()
            return _anthropic_provider
    raise ValueError(f"Unknown provider: {provider_name!r}. Supported: openai, anthropic")


def create_client(provider_name: str, *, base_url: str, api_key: str) -> Any:
    """Create an SDK client for the given provider."""
    if provider_name == "openai":
        from openai import OpenAI

        return OpenAI(base_url=base_url, api_key=api_key)
    if provider_name == "anthropic":
        from turnstone.core.providers._anthropic import _ensure_anthropic

        anthropic = _ensure_anthropic()
        kwargs: dict[str, str] = {"api_key": api_key}
        if base_url and base_url != "https://api.anthropic.com":
            kwargs["base_url"] = base_url
        return anthropic.Anthropic(**kwargs)
    raise ValueError(f"Unknown provider: {provider_name!r}. Supported: openai, anthropic")
