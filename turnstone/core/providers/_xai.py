"""xAI / Grok provider — wraps the OpenAI-compatible Responses surface.

xAI exposes ``/v1/responses`` and ``/v1/chat/completions`` at
``https://api.x.ai/v1`` with OpenAI-compatible wire shapes; the
comparison page on docs.x.ai marks Chat Completions as deprecated, so
this adapter targets Responses only.

The class is a thin subclass of :class:`OpenAIResponsesProvider`:

* No tool-call fidelity-lane override is required.  xAI's ``ToolCall``
  proto carries no analog to Gemini's ``thought_signature`` — only
  ``id`` / ``type`` / ``status`` / ``error_message`` / ``function``
  round-trip through tool calls.
* Encrypted reasoning replay (``include=["reasoning.encrypted_content"]``)
  inherits unchanged from the base class — the wire shape mirrors
  OpenAI o-series.
* ``parallel_tool_calls`` and ``tool_choice`` shapes match OpenAI
  exactly, so no per-request rewriting.

Two xAI-specific extensions over the inherited Responses behaviour:

1. **Hidden server-side tool outputs.**  xAI executes ``web_search`` /
   ``x_search`` / ``code_execution`` / ``collections_search`` on its
   servers but omits their outputs from the response body by default;
   callers must opt in via ``include=["<tool>_call_output"]``.  We
   inject the appropriate ``*_call_output`` strings whenever the
   capability row declares matching ``server_side_tools``.
2. **Prompt-cache hinting.**  The ``x-grok-conv-id`` request header
   maximises cache-hit rate on multi-turn conversations.  This module
   does not populate it; callers thread it via ``extra_headers`` on
   :meth:`create_streaming` / :meth:`create_completion` once they
   know the workstream id.

A static :data:`GROK_CAPABILITIES` table covers the five chat models
listed at docs.x.ai/developers/models (May 2026).  Aliases such as
``grok-4.3-latest`` resolve via the existing longest-prefix lookup.
Bare family aliases (``grok-4``, ``grok-3``) fall through to a
conservative default so undocumented IDs do not silently inherit
reasoning-replay behaviour.
"""

from __future__ import annotations

from typing import Any

from turnstone.core.providers._openai_common import resolve_server_side_tools
from turnstone.core.providers._openai_responses import OpenAIResponsesProvider
from turnstone.core.providers._protocol import ModelCapabilities, _lookup_capabilities

# Default endpoint used when no base_url is configured.
XAI_DEFAULT_BASE_URL = "https://api.x.ai/v1"


# ---------------------------------------------------------------------------
# Capability table — chat models from docs.x.ai/developers/models (May 2026).
# ---------------------------------------------------------------------------

GROK_CAPABILITIES: dict[str, ModelCapabilities] = {
    # grok-4.3 — flagship reasoning model.  Default effort is "low" per
    # docs.x.ai/developers/model-capabilities/text/reasoning; "none"
    # disables reasoning entirely (zero thinking tokens).
    "grok-4.3": ModelCapabilities(
        context_window=1_000_000,
        max_output_tokens=64_000,
        reasoning_effort_values=("none", "low", "medium", "high"),
        default_reasoning_effort="low",
        supports_web_search=True,
        supports_vision=True,
        supports_reasoning_replay=True,
        server_side_tools=("web_search",),
    ),
    # grok-4.20 reasoning variant — dated snapshot, always reasons.
    "grok-4.20-0309-reasoning": ModelCapabilities(
        context_window=1_000_000,
        max_output_tokens=64_000,
        supports_web_search=True,
        supports_vision=True,
        supports_reasoning_replay=True,
        server_side_tools=("web_search",),
    ),
    # grok-4.20 non-reasoning variant — dated snapshot, never reasons.
    "grok-4.20-0309-non-reasoning": ModelCapabilities(
        context_window=1_000_000,
        max_output_tokens=64_000,
        supports_web_search=True,
        supports_vision=True,
        server_side_tools=("web_search",),
    ),
    # grok-4.20 multi-agent — effort controls *agent count*, not depth.
    "grok-4.20-multi-agent-0309": ModelCapabilities(
        context_window=1_000_000,
        max_output_tokens=64_000,
        reasoning_effort_values=("low", "medium", "high", "xhigh"),
        default_reasoning_effort="low",
        supports_web_search=True,
        supports_vision=True,
        supports_reasoning_replay=True,
        server_side_tools=("web_search",),
    ),
    # grok-build — coding-focused, smaller context, no reasoning.
    "grok-build-0.1": ModelCapabilities(
        context_window=256_000,
        max_output_tokens=64_000,
        supports_web_search=True,
        server_side_tools=("web_search",),
    ),
}

# Conservative default for unknown / family-alias model IDs (grok-4,
# grok-3, grok-4-fast, etc.).  Capabilities the caller cannot verify
# without a live call (vision, reasoning replay) stay off; web search
# stays on because it is the only documented xAI server-side tool we
# inject today and undocumented IDs are likely future grok variants
# that still support it.  If the API rejects the request, the error
# surfaces to the caller directly.
_GROK_DEFAULT = ModelCapabilities(
    context_window=256_000,
    max_output_tokens=64_000,
    supports_web_search=True,
    server_side_tools=("web_search",),
)


def lookup_grok_capabilities(model: str) -> ModelCapabilities:
    """Find capabilities for *model* by longest prefix match."""
    return _lookup_capabilities(model, GROK_CAPABILITIES, _GROK_DEFAULT)


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class XAIProvider(OpenAIResponsesProvider):
    """Provider for xAI / Grok models via the OpenAI-compatible Responses API.

    Subclasses :class:`OpenAIResponsesProvider` and adds two narrow
    behaviours specific to xAI's surface; see the module docstring.
    """

    @property
    def provider_name(self) -> str:
        return "xai"

    def get_capabilities(self, model: str) -> ModelCapabilities:
        return lookup_grok_capabilities(model)

    # -- request kwargs ------------------------------------------------------

    def _build_kwargs(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        max_tokens: int,
        temperature: float,
        reasoning_effort: str,
        deferred_names: frozenset[str] | None,
        capabilities: ModelCapabilities | None = None,
        replay_reasoning_to_model: bool = True,
    ) -> dict[str, Any]:
        """Add ``include=["<tool>_call_output"]`` entries on top of the
        base Responses kwargs.

        xAI omits server-side tool outputs from the response body by
        default; the matching ``*_call_output`` include string must be
        sent for the caller to see what the tool actually did.  The
        base ``OpenAIResponsesProvider`` already adds
        ``reasoning.encrypted_content`` to ``include[]`` when
        replay is enabled, so we merge into the existing list rather
        than replace it.
        """
        kwargs = super()._build_kwargs(
            model,
            messages,
            tools,
            max_tokens,
            temperature,
            reasoning_effort,
            deferred_names,
            capabilities=capabilities,
            replay_reasoning_to_model=replay_reasoning_to_model,
        )
        caps = capabilities or self.get_capabilities(model)
        effective_tools = resolve_server_side_tools(caps)
        if not effective_tools:
            return kwargs
        includes = list(kwargs.get("include") or [])
        for tool_type in effective_tools:
            output_include = f"{tool_type}_call_output"
            if output_include not in includes:
                includes.append(output_include)
        if includes:
            kwargs["include"] = includes
        return kwargs
