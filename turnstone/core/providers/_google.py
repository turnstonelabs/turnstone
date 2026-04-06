"""Google-specific provider adapter using OpenAI-compatible interface.

Shares the core mechanics of OpenAI Chat Completions but with Google-specific
defaults (large context window, vision support).  Uses the Gemini
``/v1beta/openai/`` endpoint which is wire-compatible with the OpenAI SDK.

The caller must provide a ``base_url`` pointing at the Gemini endpoint
(e.g. ``https://generativelanguage.googleapis.com/v1beta/openai/``);
:func:`~turnstone.core.providers.create_client` fills in this default
automatically when ``provider_name="google"`` and no URL is given.
"""

from __future__ import annotations

from turnstone.core.providers._openai_chat import OpenAIChatCompletionsProvider
from turnstone.core.providers._protocol import ModelCapabilities

# Default endpoint used when no base_url is configured.
GOOGLE_DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"

# Baseline capabilities for Google models.  Since Google updates models
# frequently, we use a single generous default rather than maintaining a
# static per-model table.  The values below are safe for Gemini 2.5 Pro
# (the most capable model at time of writing) and degrade gracefully for
# smaller models — the API simply ignores over-specified max_tokens.
_GOOGLE_DEFAULT = ModelCapabilities(
    context_window=2_000_000,
    max_output_tokens=65_536,
    supports_temperature=True,
    supports_vision=True,
    # Gemini's OpenAI-compat endpoint accepts max_tokens (not
    # max_completion_tokens which is OpenAI Responses-specific).
    token_param="max_tokens",
)


class GoogleProvider(OpenAIChatCompletionsProvider):
    """Provider for Google models using the OpenAI-compatible endpoint."""

    @property
    def provider_name(self) -> str:
        return "google"

    def get_capabilities(self, model: str) -> ModelCapabilities:
        # Returns a single default instance for all Google models.
        # lookup_model_capabilities() relies on the identity check
        # (caps is default) to correctly return None for Google,
        # signalling "no static per-model entry".
        return _GOOGLE_DEFAULT
