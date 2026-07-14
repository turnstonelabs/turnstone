"""Google-specific provider adapter using OpenAI-compatible interface.

Shares the core mechanics of OpenAI Chat Completions but with Google-specific
defaults (large context window, vision support).  Uses the Gemini
``/v1beta/openai/`` endpoint which is wire-compatible with the OpenAI SDK.

The caller must provide a ``base_url`` pointing at the Gemini endpoint
(e.g. ``https://generativelanguage.googleapis.com/v1beta/openai/``);
:func:`~turnstone.core.providers.create_client` fills in this default
automatically when ``provider_name="google"`` and no URL is given.

Gemini requires provider-specific fields (e.g. ``thought_signature``)
to survive the tool-call → tool-result round-trip.  This adapter captures
the raw SDK tool-call objects via ``provider_blocks`` and reconstructs
them in ``_prepare_messages`` — the same fidelity pattern used by the
Anthropic provider.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

from turnstone.core.lowering import legalize_tool_call_entry
from turnstone.core.providers._openai_chat import OpenAIChatCompletionsProvider
from turnstone.core.providers._openai_common import sanitize_messages
from turnstone.core.providers._protocol import (
    ModelCapabilities,
    StreamChunk,
    ToolCallDelta,
    accumulate_tool_call_delta,
)

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
    # Gemini's OpenAI-compat endpoint accepts a flat ``reasoning_effort``
    # (2.5 family: thinking_budget 1024/1024/8192/24576 for minimal/low/
    # medium/high; 3.x family: thinking_level of the same name).  The
    # declared values are the safe set across ALL current Gemini models:
    # "none" is excluded because 2.5 Pro and the 3.x family reject
    # disabling thinking — and ``resolve_reasoning_effort`` never
    # forwards the knob's "none" anyway (the param is omitted and the
    # server default applies).  Off-list knob values (xhigh, max) snap
    # to the default "high".  Encoded from
    # ai.google.dev/gemini-api/docs/openai (2026-07); not live-verified —
    # the static-caps pattern for commercial providers.
    reasoning_effort_values=("minimal", "low", "medium", "high"),
    default_reasoning_effort="high",
)


class GoogleProvider(OpenAIChatCompletionsProvider):
    """Provider for Google models using the OpenAI-compatible endpoint.

    Overrides message preparation (``_prepare_messages`` reconstructs the
    raw tool calls from ``_provider_content``) and registers an
    ``on_tool_call_delta`` capture with the base stream iterator to
    preserve Gemini-specific fields (``thought_signature``) through the
    round-trip via the ``provider_blocks`` / ``_provider_content``
    fidelity lane.
    """

    @property
    def provider_name(self) -> str:
        return "google"

    def get_capabilities(self, model: str) -> ModelCapabilities:
        # Returns a single default instance for all Google models.
        # lookup_model_capabilities() relies on the identity check
        # (caps is default) to correctly return None for Google,
        # signalling "no static per-model entry".
        return _GOOGLE_DEFAULT

    # -- message preparation (round-trip fidelity) ---------------------------

    def _prepare_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Reconstruct tool_calls from ``_provider_content`` before sending.

        When ``_provider_content`` is present on an assistant message, it
        contains the raw tool-call dicts (including ``thought_signature``).
        We replace the normalised ``tool_calls`` with the raw versions and
        strip ``_provider_content`` so it never reaches the wire.
        """
        cleaned: list[dict[str, Any]] = []
        for msg in messages:
            pc = msg.get("_provider_content")
            if msg.get("role") == "assistant" and pc and isinstance(pc, list):
                # Rebuild the message without _provider_content
                msg = {k: v for k, v in msg.items() if k != "_provider_content"}
                # Extract raw tool-call dicts from provider_blocks.
                # Only type=="function" is expected today; if Gemini adds
                # other tool types (e.g. code_execution) they will need
                # their own round-trip handling here.
                raw_tcs = [b for b in pc if isinstance(b, dict) and b.get("type") == "function"]
                # Swap ONLY when the raw lane is a faithful counterpart of
                # the mirror: every raw dict carries an id (a blank id
                # predates the capture-time blank-id gate — swapping it in
                # would resurrect the blank id on every replay of that
                # historical row), and the raw list is the same length as
                # the mirror (a shorter list — a corrupted lane whose
                # non-dict elements the extraction filtered — would DROP
                # mirrored calls whose tool results remain in history and
                # orphan them).  A turn failing either check keeps the
                # sanitized mirror — losing the raw lane, exactly what the
                # capture-time gate now produces for new degenerate turns.
                if (
                    raw_tcs
                    and len(raw_tcs) == len(msg.get("tool_calls") or [])
                    and all(b.get("id") for b in raw_tcs)
                ):
                    # The raw dicts carry the model's ORIGINAL arguments;
                    # the mirror this swap replaces may have been legalized
                    # upstream (lowering.sanitize_tool_call_arguments), so
                    # re-apply the SAME per-entry legalizer — otherwise the
                    # fidelity swap resurrects a malformed arguments value
                    # on every replay.  Copy-on-write per offending entry;
                    # ids and ``thought_signature`` stay untouched.
                    msg["tool_calls"] = [legalize_tool_call_entry(b) or b for b in raw_tcs]
            cleaned.append(msg)
        return sanitize_messages(cleaned)

    # -- streaming -----------------------------------------------------------

    def _iter_stream(
        self,
        stream: Any,
        *,
        finish_reason_optional: bool = False,
        on_tool_call_delta: Callable[[ToolCallDelta, Any], None] | None = None,
    ) -> Iterator[StreamChunk]:
        """Wrap the base iterator to capture raw tool-call metadata.

        Registers an ``on_tool_call_delta`` capture with the base
        iterator to accumulate provider-specific fields (e.g.
        ``thought_signature``) from each raw SDK tool-call delta, then
        delegates all chunk processing to the base class.  The
        accumulated raw tool-call dicts are emitted as
        ``provider_blocks`` on the final chunk so the session stores
        them as ``_provider_content``.

        The capture receives the SAME slot the base's
        :class:`ToolCallSlotter` assigned to the normalized
        ``tool_calls`` mirror — one slotter drives both lanes, so call
        identity cannot desync between them (a desync — one fused raw
        dict vs two mirror calls on an index-degenerate stream — would
        fail ``_prepare_messages``' length gate and silently drop
        ``thought_signature`` from the replay).
        """
        raw_tool_calls: dict[int, dict[str, Any]] = {}

        def _capture(tcd: ToolCallDelta, raw_delta: Any) -> None:
            # The normalized delta carries the base's slot AND its
            # id/name/arguments extraction verbatim — the raw lane
            # accumulates the exact bytes the mirror sees.
            raw_tc = accumulate_tool_call_delta(raw_tool_calls, tcd)
            # Capture provider-specific extras (e.g. thought_signature)
            extras = getattr(raw_delta, "__pydantic_extra__", None)
            if extras:
                for k, v in extras.items():
                    if k not in ("index", "id", "type", "function"):
                        raw_tc.setdefault(k, v)
            if on_tool_call_delta is not None:
                on_tool_call_delta(tcd, raw_delta)

        # Delegate all chunk processing to the base class
        for sc in super()._iter_stream(
            stream,
            finish_reason_optional=finish_reason_optional,
            on_tool_call_delta=_capture,
        ):
            # Attach provider_blocks on the finish-reason chunk
            if sc.finish_reason and raw_tool_calls:
                sc.provider_blocks = [raw_tool_calls[i] for i in sorted(raw_tool_calls)]
            yield sc
