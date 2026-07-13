"""One model turn over a Turn-IR trajectory — the plant-call primitive.

``model_turn()`` is the single lower-and-sample surface: lower a
``list[Turn]`` to wire dicts, invoke the provider once, and re-ingest the
response as an assistant :class:`~turnstone.core.trajectory.Turn` carrying
the provider-native lane.  Every out-of-main-loop lane (task-agent
sub-harness, intent judge, output-guard judge, utility completions,
perception, eval) runs its model calls through here, so message shaping
cannot drift between lanes (#827).

Contract, held deliberately narrow:

* **Policy-free.**  No retry, no deadline, no tool execution, no usage
  recording inside — those belong to each caller.  The callers are
  different organs (a judge is not a sub-agent is not a title generator);
  the plant call is the one thing they share.
* **Providers stay codegen.**  The provider boundary keeps taking lowered
  wire dicts; Turn IR does not enter the provider Protocol, and
  ``lowering.py`` remains the only wire-mutation owner.  This module
  composes the existing passes; it does not add new wire mutation.
* **Live operator toggles are not snapshotted.**  :class:`ModelLane`
  binds what is stable across a loop (provider, client, model,
  capabilities, extra_params) and carries the registry reference;
  ``model_turn`` re-resolves the per-call operator flags
  (``replay_reasoning_to_model``, the vLLM reasoning attach) on every
  call, preserving mid-session admin-toggle semantics exactly as the
  pre-extraction session methods did.
"""

from __future__ import annotations

import contextlib
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from types import EllipsisType

    from turnstone.core.model_registry import ModelRegistry
    from turnstone.core.providers._protocol import (
        LLMProvider,
        ModelCapabilities,
        UsageInfo,
    )

from turnstone.core.history_decoration import attach_vllm_chat_reasoning_field
from turnstone.core.log import get_logger
from turnstone.core.lowering import (
    restore_provider_tool_ids,
    sanitize_tool_call_arguments,
)
from turnstone.core.storage._utils import strip_orphan_client_tool_blocks
from turnstone.core.trajectory import ProviderNative, ToolCall, Turn, dicts_from_turns

log = get_logger(__name__)

# Block types that carry model reasoning natively.  Anthropic emits
# ``thinking``/``redacted_thinking`` blocks, OpenAI Responses emits
# ``reasoning`` items, and ``reasoning_text`` is our own synthetic
# path-3 block (see :func:`synth_reasoning_block`).
REASONING_BEARING_BLOCK_TYPES: frozenset[str] = frozenset(
    {"thinking", "redacted_thinking", "reasoning", "reasoning_text"}
)


# --------------------------------------------------------------------------- #
# Lane resolution — the ONE place capability / extra-params / flag lookup
# happens.  ``ChatSession`` delegates its wrappers here; the judges build
# lanes directly (their #826-era mirror resolver is gone).
# --------------------------------------------------------------------------- #
def resolve_capabilities(
    provider: LLMProvider,
    model: str,
    alias: str,
    registry: ModelRegistry | None,
) -> ModelCapabilities:
    """Provider static capabilities, merged with registry alias overrides.

    Only keys that name real :class:`ModelCapabilities` fields are applied —
    unknown keys in an operator's ``capabilities`` JSON are ignored rather
    than raising, because the registry accepts free-form dicts.  NOTE the
    window landmine documented on #826: ``ModelConfig.context_window`` is a
    separate top-level column and is deliberately NOT merged here; callers
    that need the operator window must read it off the config themselves.
    """
    import dataclasses

    caps = provider.get_capabilities(model)
    if registry and alias:
        cfg = registry.get_config(alias)
        if cfg.capabilities:
            fields = {f.name for f in dataclasses.fields(type(caps))}
            overrides = {k: v for k, v in cfg.capabilities.items() if k in fields}
            if overrides:
                caps = dataclasses.replace(caps, **overrides)
    return caps


def provider_extra_params(
    provider: LLMProvider,
    registry: ModelRegistry | None,
    alias: str,
) -> dict[str, Any] | None:
    """Operator ``server_compat["extra_body"]`` pins for the OpenAI-shaped
    lanes (and the anthropic-compatible lane, whose SDK also takes
    ``extra_body``).  Real Anthropic and Google keep their own param paths
    inside their providers.  Reasoning params (``enable_thinking`` /
    ``effort_param``) are NOT built here — the providers add them via
    ``merge_reasoning_template_kwargs`` from capabilities + the effort knob.
    """
    from turnstone.core.server_compat import merge_server_compat

    if provider.provider_name not in ("openai", "openai-compatible", "anthropic-compatible"):
        return None
    server_compat: dict[str, Any] = {}
    if registry and alias:
        with contextlib.suppress(ValueError, KeyError):
            server_compat = registry.get_config(alias).server_compat
    extra = merge_server_compat(None, server_compat)
    return extra or None


def resolve_server_type(registry: ModelRegistry | None, alias: str) -> str:
    """``server_compat.server_type`` for an alias (``""`` on any miss).

    Reads ``cfg.server_compat`` (the dedicated dataclass field hoisted by
    both model_registry loader paths) — NOT ``cfg.capabilities``.  The
    Phase 5 gate in :func:`maybe_attach_vllm_chat_reasoning` reads the same
    field path directly off its own ``get_config`` fetch; if you change one
    reader, change the other.
    """
    if not registry or not alias:
        return ""
    try:
        sc = registry.get_config(alias).server_compat
        if isinstance(sc, dict):
            return str(sc.get("server_type") or "")
    except Exception:
        # Best-effort lookup — synth-block source tagging is informational,
        # never load-bearing.
        log.debug(
            "resolve_server_type lookup failed for alias=%s; defaulting to empty",
            alias,
            exc_info=True,
        )
    return ""


def resolve_replay_reasoning_to_model(
    registry: ModelRegistry | None,
    alias: str,
    *,
    caps: ModelCapabilities | None = None,
) -> bool:
    """Operator ``ModelConfig.replay_reasoning_to_model`` for an alias.

    Miss-fallback is ``False``: with no registry / alias, or a raising
    lookup, the provider-side strip path runs — replaying reasoning text
    against an unknown operator preference is the worse default, and
    ``False`` matches the ``model_definitions`` server default so cold
    workstreams behave like unconfigured ones.

    With *caps* provided the operator flag is AND-gated with
    ``caps.supports_reasoning_replay`` (mirrors the gate in
    ``OpenAIResponsesProvider._build_kwargs``); omitted, the operator flag
    passes through unchanged for callers that haven't threaded caps.
    """
    if not registry or not alias:
        return False
    try:
        operator_on = bool(registry.get_config(alias).replay_reasoning_to_model)
    except Exception:
        return False
    if caps is None:
        return operator_on
    return operator_on and bool(caps.supports_reasoning_replay)


def maybe_attach_vllm_chat_reasoning(
    messages: list[dict[str, Any]],
    provider: LLMProvider,
    registry: ModelRegistry | None,
    alias: str,
) -> list[dict[str, Any]]:
    """Phase 5 of reasoning persistence: attach vLLM's non-standard
    ``reasoning`` field to outgoing assistant messages so a vLLM-served
    reasoning model threads CoT across turns.

    Three gates, all required: Chat-Completions provider (Responses and
    Anthropic have their own replay paths with loud-failure dual-gates);
    ``server_compat.server_type == "vllm"`` (canonical OpenAI / llama.cpp /
    sglang never see the field); operator ``replay_reasoning_to_model``.
    The static ``supports_reasoning_replay`` capability gate guarding
    Paths 1+2 is intentionally NOT applied here — vLLM's chat template
    silently drops ``reasoning`` when the template doesn't read
    ``reasoning_content``, so the gate would add friction without
    preventing the silent-failure misconfiguration it can't detect.

    Returns *messages* unchanged when any gate fails.
    """
    from turnstone.core.providers._openai_chat import OpenAIChatCompletionsProvider

    if not isinstance(provider, OpenAIChatCompletionsProvider):
        return messages
    if not registry or not alias:
        return messages
    try:
        cfg = registry.get_config(alias)
    except Exception:
        return messages
    # Both gate fields read off the single ``cfg`` fetch (no second
    # ``get_config`` round-trip); field path mirrors resolve_server_type.
    sc = cfg.server_compat if isinstance(cfg.server_compat, dict) else None
    if not isinstance(sc, dict) or sc.get("server_type") != "vllm":
        return messages
    if not bool(cfg.replay_reasoning_to_model):
        return messages
    return attach_vllm_chat_reasoning_field(messages)


@dataclass(frozen=True)
class ModelLane:
    """A resolved model lane — what one loop's plant calls have in common.

    Binds the per-run-stable half of a call (provider, client, model,
    capabilities, extra_params) and carries *registry* so ``model_turn``
    can re-resolve the live per-call operator flags.  Frozen: a lane is a
    binding, not a mutable session.  Build one per loop (a ``_run_agent``
    invocation, a judge construction, a utility call site) — model
    fallback/retry across lanes is just a different ``ModelLane``.

    *alias* is the registry alias used for config resolution, ``""`` when
    the lane runs outside the registry (then every registry-backed pass
    degrades to its documented miss behavior).
    """

    provider: LLMProvider
    client: Any
    model: str
    alias: str = ""
    capabilities: ModelCapabilities | None = None
    extra_params: dict[str, Any] | None = None
    registry: ModelRegistry | None = None


def resolve_lane(
    provider: LLMProvider,
    client: Any,
    model: str,
    *,
    alias: str = "",
    registry: ModelRegistry | None = None,
    capabilities: ModelCapabilities | None = None,
    extra_params: dict[str, Any] | None | EllipsisType = ...,
) -> ModelLane:
    """Build a :class:`ModelLane`, resolving what the caller didn't supply.

    *capabilities* / *extra_params* accept pre-resolved values so callers
    that already ran the resolution (the session's cached primary caps, an
    agent run's per-alias resolution) don't pay for or drift from a second
    pass.  ``...`` (the sentinel default) means "resolve for me" —
    ``None`` is a valid resolved value for *extra_params*.
    """
    caps = capabilities or resolve_capabilities(provider, model, alias, registry)
    extra = (
        provider_extra_params(provider, registry, alias) if extra_params is ... else extra_params
    )
    return ModelLane(
        provider=provider,
        client=client,
        model=model,
        alias=alias,
        capabilities=caps,
        extra_params=extra,
        registry=registry,
    )


# --------------------------------------------------------------------------- #
# Re-ingest helpers — response → assistant Turn with the native lane.
# --------------------------------------------------------------------------- #
def ensure_tool_call_ids(tool_calls: list[dict[str, Any]] | dict[int, dict[str, Any]]) -> None:
    """Fill in missing tool call IDs with synthetic UUIDs.

    Some local servers (llama.cpp, older vLLM) omit or leave the id blank;
    an empty tool_call_id corrupts subsequent turns because the matching
    tool-result message can't reference the call.
    """
    items = tool_calls.values() if isinstance(tool_calls, dict) else tool_calls
    for tc in items:
        if not tc.get("id"):
            tc["id"] = f"call_{uuid.uuid4().hex}"


def synth_reasoning_block(
    provider_blocks: list[dict[str, Any]],
    reasoning_parts: list[str],
    *,
    registry: ModelRegistry | None = None,
    alias: str = "",
) -> list[dict[str, Any]]:
    """Stamp captured loose reasoning text as a synthetic ``reasoning_text``
    block when no reasoning-bearing block already exists.

    Anthropic (native ``thinking``) and OpenAI Responses (native
    ``reasoning`` items) need no synthesis.  The Chat-Completions lanes
    (vLLM ``--reasoning-parser``, llama.cpp ``reasoning_format``, Gemini's
    OpenAI-compat ``reasoning_content``) surface reasoning only as loose
    text; without this synth it would be visible live and invisible on
    reload.

    Tests for reasoning-bearing types specifically and APPENDS rather than
    replacing: GoogleProvider attaches raw tool_call dicts as
    ``provider_blocks`` for ``thought_signature`` round-trip, and an
    earlier any-blocks bail-out silently lost reasoning on Google turns.
    The synthetic block is ``type="reasoning_text"`` (NOT ``"thinking"``)
    so cross-model resumption onto Anthropic drops it at the shape filter
    instead of 400ing on an unsigned thinking block.  ``source`` tags the
    originating server type — informational metadata for UI rehydration.
    """
    text = "".join(reasoning_parts)
    if not text.strip():
        return provider_blocks
    for b in provider_blocks:
        if isinstance(b, dict) and b.get("type") in REASONING_BEARING_BLOCK_TYPES:
            return provider_blocks
    block: dict[str, Any] = {"type": "reasoning_text", "text": text}
    server_type = resolve_server_type(registry, alias)
    if server_type:
        block["source"] = server_type
    return [*provider_blocks, block]


def finalize_provider_blocks(
    provider_blocks: list[dict[str, Any]],
    reasoning_parts: list[str],
    *,
    has_tool_calls: bool,
    had_blank_ids: bool = False,
    registry: ModelRegistry | None = None,
    alias: str = "",
) -> list[dict[str, Any]]:
    """Finalize an assistant turn's provider-native block lane.

    Synthesizes the path-3 ``reasoning_text`` block when reasoning arrived
    only as loose text, then enforces the native↔tool_calls mirror in
    memory — a truncation that cleared ``tool_calls`` can leave an orphan
    client ``tool_use`` in the captured blocks, which a same-provider
    replay would send with no matching ``tool_result`` (same gate as
    ``storage._utils.normalize_native_for_save``, the save-time
    chokepoint).

    *had_blank_ids* is the OTHER direction of that mirror: the
    :func:`ensure_tool_call_ids` back-fill reaches only the ``tool_calls``
    mirror, so an id-bearing native block still carries the blank id
    verbatim and any replay of it desyncs from the mirror and the results
    (Anthropic orphans the result and 400s; the Google swap re-fills a
    fresh id and drops the real result) — and on the Messages translator a
    partially-surviving lane REPLACES the rebuilt content wholesale, so a
    lane missing its ``tool_use`` would orphan every mirrored call.  On a
    blank-id turn the ONLY block kept is the loose-text ``reasoning_text``
    synth: it carries no id, it is shape-invalid on the Messages
    translator by design, and real-world blank-id servers are
    Chat-Completions locals whose reasoning IS that loose text.

    The ONE builder every harness shares: the main-loop stream accumulator,
    the sub-agent loop, and (via :func:`model_turn`) the judges finalize
    their captured blocks here, so native-lane assembly cannot drift.
    Returns a possibly-empty list; callers attach it only when non-empty.
    """
    provider_blocks = synth_reasoning_block(
        provider_blocks, reasoning_parts, registry=registry, alias=alias
    )
    if not provider_blocks:
        return provider_blocks
    if had_blank_ids:
        return [
            b for b in provider_blocks if isinstance(b, dict) and b.get("type") == "reasoning_text"
        ]
    if not has_tool_calls:
        return strip_orphan_client_tool_blocks(provider_blocks)
    return provider_blocks


# --------------------------------------------------------------------------- #
# The arrow.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class ModelTurnResult:
    """One plant call's outcome.

    *turn* is the canonical product — an assistant Turn with minted tool
    ids and the finalized native lane, ready to append to the caller's
    trajectory.  *tool_calls* is the raw wire-shaped mirror (the same
    post-mint dict objects) kept for execution dispatch, which consumes
    ``function.name`` / ``function.arguments`` dicts everywhere today.
    *finish_reason* / *usage* are transport facts, not trajectory content
    — which is why they ride the result, not the Turn.
    """

    turn: Turn
    finish_reason: str
    usage: UsageInfo | None
    tool_calls: list[dict[str, Any]]

    @property
    def content(self) -> str:
        """The assistant text — convenience mirror of ``turn.text``."""
        return self.turn.text


def model_turn(
    lane: ModelLane,
    turns: Sequence[Turn],
    *,
    tools: list[dict[str, Any]] | None = None,
    max_tokens: int = 4096,
    temperature: float = 0.5,
    reasoning_effort: str = "medium",
    mint: Callable[[str], str] | None = None,
    wire_id_map: dict[str, str] | None = None,
    extra_headers: dict[str, str] | None = None,
    resolve_attachments: Callable[[list[str]], dict[str, Any]] | None = None,
) -> ModelTurnResult:
    """Advance a trajectory by one model turn: lower, sample, re-ingest.

    Lowering runs the standard seam passes on every call —
    ``dicts_from_turns`` → ``sanitize_tool_call_arguments`` (a local model
    can emit unterminated/non-object ``arguments`` that a strict renderer
    400s on every replay) → ``restore_provider_tool_ids`` (map minted
    ``::`` sub-tool ids back to provider originals so the native
    ``tool_use`` block, the ``tool_calls`` mirror, and the ``tool_result``
    agree on the wire) → the Phase 5 vLLM reasoning attach.  Re-lowering
    per call is deliberate: the passes are deterministic and copy-on-write,
    so a caller's retry loop just calls again.

    *mint* rewrites each returned tool call's id (provider-original →
    caller-scoped) before the Turn is built; the native blocks keep the
    provider ids verbatim (they are never rewritten — they may sit under a
    reasoning signature).  Every ``minted → original`` pair is recorded
    into *wire_id_map* (caller-owned, threaded back in on the next call so
    the restore pass can undo the mint on the wire).  Blank provider ids
    are uuid-back-filled first; the pre-back-fill blank state feeds the
    finalize gate (see :func:`finalize_provider_blocks`).

    Raises whatever the provider raises — retry/deadline/fallback policy
    is the caller's.
    """
    wire = restore_provider_tool_ids(
        sanitize_tool_call_arguments(dicts_from_turns(list(turns))),
        wire_id_map if wire_id_map is not None else {},
    )
    wire = maybe_attach_vllm_chat_reasoning(wire, lane.provider, lane.registry, lane.alias)
    result = lane.provider.create_completion(
        client=lane.client,
        model=lane.model,
        messages=wire,
        tools=tools,
        max_tokens=max_tokens,
        temperature=temperature,
        reasoning_effort=reasoning_effort,
        extra_params=lane.extra_params,
        capabilities=lane.capabilities,
        replay_reasoning_to_model=resolve_replay_reasoning_to_model(
            lane.registry, lane.alias, caps=lane.capabilities
        ),
        extra_headers=extra_headers,
        resolve_attachments=resolve_attachments,
    )

    raw_calls: list[dict[str, Any]] = list(result.tool_calls or [])
    # Record blanks BEFORE the uuid back-fill: a back-filled id exists only
    # in the tool_calls mirror — the native blocks keep the blank provider
    # id verbatim, so the finalize gate must drop the blocks the back-fill
    # desyncs.
    had_blank_ids = any(not tc.get("id") for tc in raw_calls)
    ensure_tool_call_ids(raw_calls)
    if mint is not None:
        for tc in raw_calls:
            original_id = tc["id"]
            minted = mint(original_id)
            if minted != original_id:
                tc["id"] = minted
                if wire_id_map is not None:
                    # Recovery is by MAP ONLY — never string-split the mint
                    # (parent and original are provider-controlled strings
                    # that may themselves contain the delimiter).
                    wire_id_map[minted] = original_id

    tool_calls = tuple(
        ToolCall(
            id=tc["id"],
            name=tc.get("function", {}).get("name", ""),
            arguments=tc.get("function", {}).get("arguments", ""),
        )
        for tc in raw_calls
    )
    # Carry the provider-native lane (thinking blocks, signatures, Responses
    # reasoning items, synthesized ``reasoning_text``) so a multi-turn caller
    # keeps its reasoning continuity instead of re-reasoning each turn.
    # ``producer`` is the lane's own provider: a loop is pinned to one
    # provider, so blocks always replay to the backend that produced them
    # (translators' per-block shape filters drop anything foreign).
    native_blocks = finalize_provider_blocks(
        result.provider_blocks,
        [result.reasoning],
        has_tool_calls=bool(raw_calls),
        had_blank_ids=had_blank_ids,
        registry=lane.registry,
        alias=lane.alias,
    )
    native = (
        ProviderNative(producer=lane.provider.provider_name, blocks=tuple(native_blocks))
        if native_blocks
        else None
    )
    return ModelTurnResult(
        turn=Turn.assistant(result.content or "", tool_calls=tool_calls, native=native),
        finish_reason=result.finish_reason,
        usage=result.usage,
        tool_calls=raw_calls,
    )
