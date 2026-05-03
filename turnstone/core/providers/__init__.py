"""LLM provider adapters — pluggable backends for different APIs."""

from __future__ import annotations

import threading
from typing import Any

from turnstone.core.providers._openai import OpenAIProvider
from turnstone.core.providers._openai_chat import OpenAIChatCompletionsProvider
from turnstone.core.providers._openai_responses import OpenAIResponsesProvider
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
    "OpenAIChatCompletionsProvider",
    "OpenAIProvider",
    "OpenAIResponsesProvider",
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
_openai_responses_provider = OpenAIResponsesProvider()
_openai_chat_provider = OpenAIChatCompletionsProvider()
_anthropic_provider: LLMProvider | None = None
_google_provider: LLMProvider | None = None

# Backwards-compatible aliases (kept module-private; some tests reference them).
_openai_provider = _openai_responses_provider
_openai_compat_provider = _openai_chat_provider


_VALID_API_SURFACES = ("chat", "responses")


def create_provider(
    provider_name: str,
    *,
    api_surface: str | None = None,
) -> LLMProvider:
    """Return a provider adapter for the given provider name. Thread-safe.

    *api_surface* selects the OpenAI-compatible API surface for
    ``provider_name="openai-compatible"``:

    - ``"chat"`` (default) → Chat Completions (vLLM, llama.cpp, SGLang).
    - ``"responses"``      → Responses API (commercial OpenAI-compat
      endpoints like Mistral cloud, or local servers that expose the
      Responses surface).

    Ignored for non-OpenAI providers.  ``provider_name="openai"`` always
    uses the Responses API regardless of *api_surface*.

    Note: the ``OpenAIResponsesProvider`` singleton is reused for both
    cloud OpenAI and ``openai-compatible`` + responses, so its
    ``provider_name`` reports ``"openai"`` even when serving an
    openai-compatible config.  Code that needs to distinguish the two
    must read ``ModelConfig.provider`` and ``server_compat["api_surface"]``
    rather than ``provider.provider_name``.
    """
    global _anthropic_provider, _google_provider  # noqa: PLW0603
    if provider_name == "openai":
        return _openai_responses_provider
    if provider_name == "openai-compatible":
        normalised = (api_surface or "").strip().lower()
        if normalised and normalised not in _VALID_API_SURFACES:
            raise ValueError(
                f"Unknown api_surface: {api_surface!r}. Supported: {', '.join(_VALID_API_SURFACES)}"
            )
        if normalised == "responses":
            return _openai_responses_provider
        return _openai_chat_provider
    if provider_name == "anthropic":
        with _provider_lock:
            if _anthropic_provider is None:
                from turnstone.core.providers._anthropic import AnthropicProvider

                _anthropic_provider = AnthropicProvider()
            return _anthropic_provider
    if provider_name == "google":
        with _provider_lock:
            if _google_provider is None:
                from turnstone.core.providers._google import GoogleProvider

                _google_provider = GoogleProvider()
            return _google_provider
    raise ValueError(
        f"Unknown provider: {provider_name!r}. "
        "Supported: openai, anthropic, google, openai-compatible"
    )


def create_client(provider_name: str, *, base_url: str, api_key: str) -> Any:
    """Create an SDK client for the given provider."""
    if provider_name in ("openai", "openai-compatible", "google"):
        from openai import OpenAI

        if not base_url and provider_name == "google":
            from turnstone.core.providers._google import GOOGLE_DEFAULT_BASE_URL

            base_url = GOOGLE_DEFAULT_BASE_URL
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
        f"Unknown provider: {provider_name!r}. "
        "Supported: openai, anthropic, google, openai-compatible"
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
        from turnstone.core.providers._openai_common import OPENAI_CAPABILITIES

        return sorted(OPENAI_CAPABILITIES.keys())
    if provider == "anthropic":
        from turnstone.core.providers._anthropic import _ANTHROPIC_CAPABILITIES

        return sorted(_ANTHROPIC_CAPABILITIES.keys())
    # Google models change frequently — no static table.
    return []
