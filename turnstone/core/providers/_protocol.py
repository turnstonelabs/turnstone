"""LLM provider protocol — the contract every backend adapter must implement.

Defines normalized data types for streaming chunks, completion results, and
token usage so that ``ChatSession`` can work with any LLM backend without
knowing provider-specific details.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Iterator


@dataclass
class ToolCallDelta:
    """Incremental tool call update within a streaming chunk."""

    index: int
    id: str = ""
    name: str = ""
    arguments_delta: str = ""


@dataclass
class UsageInfo:
    """Normalized token usage."""

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


@dataclass
class StreamChunk:
    """Normalized streaming chunk, provider-agnostic."""

    content_delta: str = ""
    reasoning_delta: str = ""
    tool_call_deltas: list[ToolCallDelta] = field(default_factory=list)
    usage: UsageInfo | None = None
    finish_reason: str | None = None
    is_first: bool = False


@dataclass
class CompletionResult:
    """Normalized non-streaming completion result."""

    content: str
    tool_calls: list[dict[str, Any]] | None = None
    finish_reason: str = "stop"
    usage: UsageInfo | None = None


@dataclass(frozen=True)
class ModelCapabilities:
    """Describes what a specific model supports — used by providers to
    adjust API parameters (temperature, token param name, thinking mode, etc.).
    """

    context_window: int = 200000
    max_output_tokens: int = 64000
    supports_temperature: bool = True
    supports_streaming: bool = True
    supports_tools: bool = True
    token_param: str = "max_completion_tokens"
    thinking_mode: str = "none"  # "none" | "manual" | "adaptive"
    supports_effort: bool = False
    effort_levels: tuple[str, ...] = ()
    reasoning_effort_values: tuple[str, ...] = ()
    default_reasoning_effort: str = "medium"


def _lookup_capabilities(
    model: str,
    table: dict[str, ModelCapabilities],
    default: ModelCapabilities,
) -> ModelCapabilities:
    """Find capabilities by longest prefix match."""
    best_match = ""
    for prefix in table:
        if (model == prefix or model.startswith(prefix + "-")) and len(prefix) > len(best_match):
            best_match = prefix
    return table[best_match] if best_match else default


@runtime_checkable
class LLMProvider(Protocol):
    """Protocol that every LLM backend adapter must implement.

    Translates between turnstone's internal OpenAI-like message format
    and the provider's native API.
    """

    @property
    def provider_name(self) -> str:
        """Return provider identifier (``"openai"``, ``"anthropic"``, etc.)."""
        ...

    def get_capabilities(self, model: str) -> ModelCapabilities:
        """Return capabilities for the given model ID."""
        ...

    def create_streaming(
        self,
        *,
        client: Any,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.5,
        reasoning_effort: str = "medium",
        extra_params: dict[str, Any] | None = None,
    ) -> Iterator[StreamChunk]:
        """Create a streaming request, yielding normalized StreamChunks."""
        ...

    def create_completion(
        self,
        *,
        client: Any,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.5,
        reasoning_effort: str = "medium",
        extra_params: dict[str, Any] | None = None,
    ) -> CompletionResult:
        """Create a non-streaming request, returning a normalized result."""
        ...

    def convert_tools(
        self,
        tools: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Convert internal tool schemas (OpenAI format) to provider format."""
        ...

    @property
    def retryable_error_names(self) -> frozenset[str]:
        """Exception class names that should trigger retry."""
        ...
