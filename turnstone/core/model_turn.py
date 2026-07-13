"""One model turn over a Turn-IR trajectory — the plant-call primitive.

``model_turn()`` is the single lower-and-sample surface: lower a
``list[Turn]`` to wire dicts, invoke the provider once, and re-ingest the
response as an assistant :class:`~turnstone.core.trajectory.Turn` carrying
the provider-native lane.  Any lane that samples the model belongs here;
a call site that still builds messages and hits ``create_completion``
directly is migration debt, tracked on #827 (transport retirement #831,
main loop #832).  Grep for callers — not this docstring — for current
coverage, and mirror wire-shaping changes into any straggler until that
list is empty.

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

import uuid
from dataclasses import dataclass, fields, replace
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
from turnstone.core.storage._utils import (
    _CLIENT_TOOL_CALL_BLOCK_TYPES,
    strip_orphan_client_tool_blocks,
)
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
def _get_config_or_none(registry: ModelRegistry | None, alias: str) -> Any | None:
    """One defensive ``get_config`` fetch shared by the lane resolvers.

    A miss (alias deleted or registry hot-reloaded mid-lookup) degrades to
    ``None`` — resolution paths must never crash a caller's construction:
    pre-extraction, a judge constructor read caps off the already-fetched
    ModelConfig and could not fail here, and a raise would silently
    downgrade the judge to the session model.
    """
    if not registry or not alias:
        return None
    try:
        return registry.get_config(alias)
    except Exception:
        log.debug("get_config failed for alias=%s; resolving without it", alias, exc_info=True)
        return None


def resolve_capabilities(
    provider: LLMProvider,
    model: str,
    alias: str,
    registry: ModelRegistry | None,
    *,
    cfg: Any | None = None,
) -> ModelCapabilities:
    """Provider static capabilities, merged with registry alias overrides.

    Only keys that name real :class:`ModelCapabilities` fields are applied —
    unknown keys in an operator's ``capabilities`` JSON are ignored rather
    than raising, because the registry accepts free-form dicts, and a
    malformed non-dict value degrades to "no overrides" (inherited from the
    judges' old mirror — capability resolution must never crash a judge
    turn).  *cfg* accepts a pre-fetched ModelConfig so a caller resolving
    several lane facets reads ONE config generation (see
    :func:`resolve_lane`); omitted, the config is fetched defensively.
    NOTE the window landmine documented on #826:
    ``ModelConfig.context_window`` is a separate top-level column and is
    deliberately NOT merged here; callers that need the operator window
    must read it off the config themselves.
    """
    caps = provider.get_capabilities(model)
    if cfg is None:
        cfg = _get_config_or_none(registry, alias)
    if cfg is not None:
        overrides_raw = getattr(cfg, "capabilities", None)
        if isinstance(overrides_raw, dict) and overrides_raw:
            names = {f.name for f in fields(type(caps))}
            overrides = {k: v for k, v in overrides_raw.items() if k in names}
            if overrides:
                caps = replace(caps, **overrides)
    return caps


def provider_extra_params(
    provider: LLMProvider,
    registry: ModelRegistry | None,
    alias: str,
    *,
    cfg: Any | None = None,
) -> dict[str, Any] | None:
    """Operator ``server_compat["extra_body"]`` pins for the OpenAI-shaped
    lanes (and the anthropic-compatible lane, whose SDK also takes
    ``extra_body``).  Real Anthropic and Google keep their own param paths
    inside their providers.  Reasoning params (``enable_thinking`` /
    ``effort_param``) are NOT built here — the providers add them via
    ``merge_reasoning_template_kwargs`` from capabilities + the effort knob.
    *cfg* accepts a pre-fetched ModelConfig (see :func:`resolve_lane`).
    """
    from turnstone.core.server_compat import merge_server_compat

    if provider.provider_name not in ("openai", "openai-compatible", "anthropic-compatible"):
        return None
    if cfg is None:
        cfg = _get_config_or_none(registry, alias)
    server_compat = getattr(cfg, "server_compat", None) if cfg is not None else None
    extra = merge_server_compat(None, server_compat if isinstance(server_compat, dict) else {})
    return extra or None


def _server_type_of(cfg: Any) -> str:
    """``server_compat.server_type`` off a fetched ModelConfig (``""`` on miss).

    Reads ``cfg.server_compat`` (the dedicated dataclass field hoisted by
    both model_registry loader paths) — NOT ``cfg.capabilities``.  The ONE
    reader of the field path: :func:`resolve_server_type` and the Phase 5
    gate in :func:`maybe_attach_vllm_chat_reasoning` both go through here,
    so a loader shape change cannot desync them.
    """
    sc = getattr(cfg, "server_compat", None)
    if isinstance(sc, dict):
        return str(sc.get("server_type") or "")
    return ""


def resolve_server_type(registry: ModelRegistry | None, alias: str) -> str:
    """``server_compat.server_type`` for an alias (``""`` on any miss).

    Best-effort lookup — synth-block source tagging is informational,
    never load-bearing.
    """
    cfg = _get_config_or_none(registry, alias)
    return _server_type_of(cfg) if cfg is not None else ""


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
    cfg = _get_config_or_none(registry, alias)
    if cfg is None:
        return messages
    # Both gate fields read off the single ``cfg`` fetch (no second
    # ``get_config`` round-trip); the field path is owned by _server_type_of.
    if _server_type_of(cfg) != "vllm":
        return messages
    if not bool(getattr(cfg, "replay_reasoning_to_model", False)):
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

    *temperature* is the lane's inherited sampling temperature (the alias's
    ``ModelConfig.temperature``, or ``None`` for the provider default).
    House rule: code never pins a temperature — many modern models
    misbehave below 1.0, so the model's own configuration is the source of
    truth; callers pass an explicit value only when relaying an
    operator-resolved knob (the session's, for the agent seam).
    """

    provider: LLMProvider
    client: Any
    model: str
    alias: str = ""
    capabilities: ModelCapabilities | None = None
    extra_params: dict[str, Any] | None = None
    registry: ModelRegistry | None = None
    temperature: float | None = None


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

    All resolved facets read ONE defensively-fetched ModelConfig, so a
    registry hot-reload mid-resolution cannot mix config generations, and
    an alias that raced away degrades every facet to its miss behavior
    instead of raising into the caller's constructor.
    """
    cfg = _get_config_or_none(registry, alias)
    caps = capabilities or resolve_capabilities(provider, model, alias, registry, cfg=cfg)
    extra = (
        provider_extra_params(provider, registry, alias, cfg=cfg)
        if extra_params is ...
        else extra_params
    )
    return ModelLane(
        provider=provider,
        client=client,
        model=model,
        alias=alias,
        capabilities=caps,
        extra_params=extra,
        registry=registry,
        temperature=getattr(cfg, "temperature", None) if cfg is not None else None,
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


def backfill_blank_native_tool_ids(
    provider_blocks: list[dict[str, Any]],
    mirror_calls: list[dict[str, Any]],
) -> bool:
    """Manufacture ids for BLANK-id native client tool blocks from the
    uuid-back-filled mirror, restoring the native↔mirror id agreement that
    the blank-id drop rule otherwise enforces by discarding the lane.

    Pairing is positional: the native client tool blocks and the
    ``tool_calls`` mirror are built from the same response in the same
    iteration on every lane (the #825 pairing invariant, the same 1:1
    ordering the durable-subturn re-mint design relies on).  Only a BLANK
    id is ever written — a non-blank provider id may sit under a reasoning
    signature and is never rewritten, which inherently protects the
    signed-lane providers (Anthropic never emits blank ids; the observed
    blank-id servers are Chat-Completions locals and Google's OpenAI-compat
    endpoint, whose fidelity blocks are plain mirror-shaped dicts).

    Returns ``True`` when the pairing matched and every client block now
    carries an id — the caller may then keep the full native lane
    (``thought_signature`` survives, unblocking the Gemini judge's evidence
    loop).  Returns ``False`` on any count mismatch, leaving the caller to
    the total reasoning_text-only drop, which remains the safe fallback the
    #825 review converged on.
    """
    client_blocks = [
        b
        for b in provider_blocks
        if isinstance(b, dict) and b.get("type") in _CLIENT_TOOL_CALL_BLOCK_TYPES
    ]
    if len(client_blocks) != len(mirror_calls):
        return False
    for block, tc in zip(client_blocks, mirror_calls, strict=True):
        if not block.get("id"):
            block["id"] = tc["id"]
    return all(b.get("id") for b in client_blocks)


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
    mirror, so an id-bearing native block still carrying a blank id
    desyncs from the mirror and the results on replay (Anthropic orphans
    the result and 400s; the Google swap re-fills a fresh id and drops the
    real result) — and on the Messages translator a partially-surviving
    lane REPLACES the rebuilt content wholesale, so a lane missing its
    ``tool_use`` would orphan every mirrored call.  ``model_turn`` first
    attempts the pairwise repair (:func:`backfill_blank_native_tool_ids`,
    manufactured ids written into the blank native blocks) and only passes
    ``had_blank_ids=True`` when the repair could not pair; on that path
    the ONLY block kept is the loose-text ``reasoning_text`` synth: it
    carries no id, it is shape-invalid on the Messages translator by
    design, and real-world blank-id servers are Chat-Completions locals
    whose reasoning IS that loose text.

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
    temperature: float | None = None,
    reasoning_effort: str = "medium",
    mint: Callable[[str], str] | None = None,
    wire_id_map: dict[str, str] | None = None,
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

    *temperature* ``None`` (the default) inherits the lane's resolved
    value — the alias's ``ModelConfig.temperature``, or the provider
    default when unset.  House rule: never pin a temperature in code; pass
    an explicit float only to relay an operator-resolved knob.

    *resolve_attachments* materializes by-reference ``AttachmentRef``
    content at the provider translator (``{type: kind, attachment_id}``
    placeholders → inline parts; one id may expand to several parts, e.g.
    a rasterized PDF).  Turn IR never carries inline media bytes — a lane
    with non-text content passes refs plus this resolver, exactly like the
    main loop's wire path.

    *mint* rewrites each returned tool call's id (provider-original →
    caller-scoped) before the Turn is built; the native blocks keep the
    provider ids verbatim (they are never rewritten — they may sit under a
    reasoning signature).  Every ``minted → original`` pair is recorded
    into *wire_id_map*, which is therefore REQUIRED with *mint* (caller-
    owned, threaded back in on the next call so the restore pass can undo
    the mint on the wire — minted ids without the map are unrestorable and
    would orphan every tool result).  Blank provider ids are
    uuid-back-filled first, then :func:`backfill_blank_native_tool_ids`
    repairs the native lane's blank ids pairwise; only when that repair
    can't pair does the finalize gate drop the lane to its
    ``reasoning_text`` synth (see :func:`finalize_provider_blocks`).

    Raises whatever the provider raises — retry/deadline/fallback policy
    is the caller's.
    """
    if mint is not None and wire_id_map is None:
        raise ValueError(
            "model_turn: mint requires wire_id_map — minted ids are "
            "unrestorable on the wire without the recovery map"
        )
    wire = restore_provider_tool_ids(
        sanitize_tool_call_arguments(dicts_from_turns(list(turns))),
        wire_id_map if wire_id_map is not None else {},
    )
    wire = maybe_attach_vllm_chat_reasoning(wire, lane.provider, lane.registry, lane.alias)
    call_kwargs: dict[str, Any] = {
        "client": lane.client,
        "model": lane.model,
        "messages": wire,
        "tools": tools,
        "max_tokens": max_tokens,
        "reasoning_effort": reasoning_effort,
        "extra_params": lane.extra_params,
        "capabilities": lane.capabilities,
        "replay_reasoning_to_model": resolve_replay_reasoning_to_model(
            lane.registry, lane.alias, caps=lane.capabilities
        ),
    }
    if resolve_attachments is not None:
        call_kwargs["resolve_attachments"] = resolve_attachments
    effective_temperature = temperature if temperature is not None else lane.temperature
    if effective_temperature is not None:
        call_kwargs["temperature"] = effective_temperature
    result = lane.provider.create_completion(**call_kwargs)

    raw_calls: list[dict[str, Any]] = list(result.tool_calls or [])
    # Record blanks BEFORE the uuid back-fill: a back-filled id exists only
    # in the tool_calls mirror until the pairwise native repair below runs.
    had_blank_ids = any(not tc.get("id") for tc in raw_calls)
    ensure_tool_call_ids(raw_calls)
    if had_blank_ids and backfill_blank_native_tool_ids(result.provider_blocks, raw_calls):
        # The native client blocks now carry the manufactured ids — the
        # mirror, the native lane, and (via the map-restored wire) the tool
        # results agree again, so the lane can be kept: thought_signature
        # survives on Google's blank-id compat responses instead of the
        # turn degrading to loose reasoning text.
        had_blank_ids = False
    if mint is not None:
        assert wire_id_map is not None  # enforced by the guard above
        for tc in raw_calls:
            original_id = tc["id"]
            minted = mint(original_id)
            if minted != original_id:
                tc["id"] = minted
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
