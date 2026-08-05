"""One model turn over a Turn-IR trajectory — the plant-call primitive.

``model_turn()`` is the single lower-and-sample surface: lower a
``list[Turn]`` to wire dicts, invoke the provider once, and re-ingest the
response as an assistant :class:`~turnstone.core.trajectory.Turn` carrying
the provider-native lane.  Any lane that samples the model belongs here;
a call site that still builds raw OpenAI-dict messages and invokes the
provider directly is migration debt, tracked on #827 (transport retirement #831,
main loop #832).  Grep for callers — not this docstring — for current
coverage, and mirror wire-shaping changes into any straggler until that
list is empty.

Contract, held deliberately narrow:

* **Policy-free.**  No retry, no deadline, no tool execution, no usage
  recording inside — those belong to each caller.  The callers are
  different organs (a judge is not a sub-agent is not a title generator);
  the plant call is the one thing they share.  Three carve-outs, each
  about a call that is already dead rather than about policy: the
  drain-retry loop re-issues a mid-stream death; an aborted ``cancel_ref``
  raises ``DeadlineCancelledError`` rather than dispatch — the caller
  still owns the deadline, this only refuses to spend on a call already
  abandoned; and ``on_chunk`` DISABLES that drain retry — a
  partially-surfaced stream is dead to silent re-issue, because a UI
  already rendered its tokens and only the streaming caller can finalize
  a display per attempt (see ``Raises`` on :func:`model_turn`).
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

import random
import time
import uuid
from dataclasses import dataclass, fields, replace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence
    from types import EllipsisType

    from turnstone.core.model_registry import ModelRegistry

from turnstone.core.deadline import DeadlineCancelledError
from turnstone.core.history_decoration import attach_vllm_chat_reasoning_field
from turnstone.core.log import get_logger
from turnstone.core.lowering import (
    restore_provider_tool_ids,
    sanitize_tool_call_arguments,
)

# The provider FACTORY rides the same re-export seam: the session's
# no-registry default construction is the one provider-construction site
# left outside the registry, and routing it through here keeps the
# provider package a plant-layer-only import.
from turnstone.core.providers import create_provider as create_provider

# Protocol names imported at runtime (not TYPE_CHECKING) and re-exported with
# the explicit ``as`` idiom: post-#832 ``ChatSession`` types its provider
# handles, chunk callback, and capabilities against THIS module, so the
# provider package stays a single-import-site dependency of the plant layer.
from turnstone.core.providers._protocol import (
    LLMProvider as LLMProvider,
)
from turnstone.core.providers._protocol import (
    ModelCapabilities as ModelCapabilities,
)
from turnstone.core.providers._protocol import (
    StreamChunk as StreamChunk,
)
from turnstone.core.providers._protocol import (
    UsageInfo as UsageInfo,
)
from turnstone.core.providers._protocol import (
    drain_stream,
    has_reasoning_bearing_block,
    thinking_off_template_kwargs,
)
from turnstone.core.providers._protocol import (
    merge_usage as merge_usage,
)
from turnstone.core.storage._utils import (
    _CLIENT_TOOL_CALL_BLOCK_TYPES,
    strip_orphan_client_tool_blocks,
)
from turnstone.core.trajectory import ProviderNative, ToolCall, Turn, dicts_from_turns

log = get_logger(__name__)

# Mid-stream death re-issue budget — parity with the SDK request-level
# retry (openai/anthropic default ``max_retries=2``) that covered the
# whole body read on the retired non-streaming transport.
_DRAIN_RETRIES = 2
# Base for the exponential inter-attempt delay (0.5s → 1s, ±50% jitter) —
# the SDK retry's pacing, minus Retry-After (an in-band stream failure
# carries no header to honor).  An instant re-issue is guaranteed to
# re-hit a still-active rate limit or overload, and at fleet scale
# synchronized re-issues amplify the very condition being retried
# through.  Module-level so tests can zero it.
_DRAIN_RETRY_BASE_DELAY = 0.5

# Native-reasoning block membership lives in providers._protocol
# (REASONING_BEARING_BLOCK_TYPES + has_reasoning_bearing_block, beside the
# drain's double-reasoning check); this layer consumes the shared
# predicate in :func:`synth_reasoning_block`.


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
    cfg: Any | EllipsisType = ...,
) -> ModelCapabilities:
    """Provider static capabilities, merged with registry alias overrides.

    Only keys that name real :class:`ModelCapabilities` fields are applied —
    unknown keys in an operator's ``capabilities`` JSON are ignored rather
    than raising, because the registry accepts free-form dicts, and a
    malformed non-dict value degrades to "no overrides" (inherited from the
    judges' old mirror — capability resolution must never crash a judge
    turn).  *cfg* accepts a pre-fetched ModelConfig so a caller resolving
    several lane facets reads ONE config generation (see
    :func:`resolve_lane`); ``None`` means "fetched and missed" (no second
    lookup), and the ``...`` sentinel default means "fetch defensively for
    me".
    NOTE the window landmine documented on #826:
    ``ModelConfig.context_window`` is a separate top-level column and is
    deliberately NOT merged here; callers that need the operator window
    must read it off the config themselves.
    """
    caps = provider.get_capabilities(model)
    if cfg is ...:
        cfg = _get_config_or_none(registry, alias)
    if cfg is not None:
        caps = apply_capability_overrides(caps, getattr(cfg, "capabilities", None))
    return caps


_CAPABILITY_BOOL_STRINGS = {
    "true": True,
    "yes": True,
    "on": True,
    "1": True,
    "false": False,
    "no": False,
    "off": False,
    "0": False,
    "": False,
}


def apply_capability_overrides(caps: ModelCapabilities, overrides_raw: Any) -> ModelCapabilities:
    """Field-filtered merge of an operator ``capabilities`` dict onto *caps*.

    The ONE merge shared by the request path (:func:`resolve_capabilities`)
    and the admin-UI effort-ladder projection — callers holding a raw
    overrides dict use this directly instead of faking a ModelConfig.
    Unknown keys are ignored (the registry accepts free-form dicts) and a
    non-dict value degrades to "no overrides".

    Values landing on BOOL-defaulted fields are coerced: the capabilities
    dict is hand-edited JSON, and a string ``"false"`` is truthy — left
    raw it would silently FLIP every downstream truthiness read (a
    ``server_parses_reasoning: "false"`` typo turning the inline tag scan
    off is the #940 leak reopened by punctuation).  Recognized spellings
    map to their boolean, ints pass through ``bool()`` (0/1 rows), and an
    unrecognized value drops the key — the field keeps its default, the
    same degrade-not-crash posture as the non-dict case.
    """
    if isinstance(overrides_raw, dict) and overrides_raw:
        by_name = {f.name: f for f in fields(type(caps))}
        overrides: dict[str, Any] = {}
        for key, value in overrides_raw.items():
            fld = by_name.get(key)
            if fld is None:
                continue
            # bool check FIRST — bool is an int subclass, so the int arm
            # below would otherwise claim real booleans.
            if isinstance(fld.default, bool) and not isinstance(value, bool):
                if isinstance(value, int):
                    value = bool(value)
                elif isinstance(value, str) and value.strip().lower() in _CAPABILITY_BOOL_STRINGS:
                    value = _CAPABILITY_BOOL_STRINGS[value.strip().lower()]
                else:
                    continue
            overrides[key] = value
        if overrides:
            caps = replace(caps, **overrides)
    return caps


# Providers whose request shape carries an ``extra_body`` dict, so
# operator ``server_compat`` pins and template-kwarg reasoning levers
# reach the wire through it.  Real Anthropic and Google keep their own
# param paths inside their providers and must never be handed one.  THE
# membership test — shared by :func:`provider_extra_params` and the
# callers that layer their own pins onto a resolved lane, so the two
# cannot disagree about which lanes accept extra_body at all.
EXTRA_BODY_PROVIDERS: tuple[str, ...] = ("openai", "openai-compatible", "anthropic-compatible")


def provider_extra_params(
    provider: LLMProvider,
    registry: ModelRegistry | None,
    alias: str,
    *,
    cfg: Any | EllipsisType = ...,
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

    if provider.provider_name not in EXTRA_BODY_PROVIDERS:
        return None
    if cfg is ...:
        cfg = _get_config_or_none(registry, alias)
    server_compat = getattr(cfg, "server_compat", None) if cfg is not None else None
    extra = merge_server_compat(None, server_compat if isinstance(server_compat, dict) else {})
    return extra or None


def _server_type_of(cfg: Any) -> str:
    """``server_compat.server_type`` off a fetched ModelConfig (``""`` on miss).

    Reads ``cfg.server_compat`` (the dedicated dataclass field hoisted by
    both model_registry loader paths) — NOT ``cfg.capabilities``.  The ONE
    reader of the field path: the Phase 5 gate in
    :func:`maybe_attach_vllm_chat_reasoning` and the synth-block source
    tagging in :func:`synth_reasoning_block` both go through here, so a
    loader shape change cannot desync them.
    """
    sc = getattr(cfg, "server_compat", None)
    if isinstance(sc, dict):
        return str(sc.get("server_type") or "")
    return ""


def _store_get_or_none(config_store: Any, key: str) -> Any | None:
    """Best-effort ConfigStore read for a lane rung: value or ``None``.

    A broken store degrades the rung to unset — settings lookups must
    never crash lane resolution.  The registered defaults for the
    sampling keys ARE the unset sentinels (``None`` / ``""``), so a
    never-stored key falls through the ladder instead of manufacturing
    a wire value (``ConfigStore.get`` returns the SettingDef default on
    a miss, never ``None`` for a registered key — the admin UI shows
    that same default, so sentinel defaults keep the UI and the wire in
    agreement).
    """
    try:
        return config_store.get(key)
    except Exception:
        log.debug("config_store %s lookup failed", key, exc_info=True)
        return None


def resolve_temperature_setting(cfg: Any | None, config_store: Any | None) -> float | None:
    """The operator rungs of the temperature assignment scheme.

    ``ModelConfig.temperature`` (the alias's per-model value; ``0.0`` is
    a valid explicit choice) → stored global ``model.temperature`` →
    ``None``.  ``None`` means no operator spoke: the field is omitted
    from the wire and the inference engine's own default applies.  Code
    never supplies a number here — the ONE resolver shared by
    :func:`resolve_lane`, the session factories, and the ``/model``
    switch, so every surface samples identically on the same alias.
    """
    temperature = getattr(cfg, "temperature", None) if cfg is not None else None
    if temperature is None and config_store is not None:
        temperature = _store_get_or_none(config_store, "model.temperature")
    return temperature


def resolve_effort_setting(
    cfg: Any | None,
    config_store: Any | None,
    *,
    role_key: str = "",
) -> str | None:
    """The operator rungs of the reasoning-effort assignment scheme.

    ``ModelConfig.reasoning_effort`` → *role_key* setting (a role-scoped
    override such as ``coordinator.reasoning_effort``) → stored global
    ``model.reasoning_effort`` → ``None``.  Empty string at any rung
    means "unset" (it is a valid settings choice meaning fall through),
    distinct from the explicit ``"none"`` effort value.  The in-code
    model-definition rung (``caps.default_reasoning_effort``) applies at
    the call site (:func:`model_turn`), NOT here — a lane's resolved
    effort is operator intent only.
    """
    effort = getattr(cfg, "reasoning_effort", None) if cfg is not None else None
    if not effort and config_store is not None and role_key:
        effort = _store_get_or_none(config_store, role_key)
    if not effort and config_store is not None:
        effort = _store_get_or_none(config_store, "model.reasoning_effort")
    return effort or None


def resolve_replay_reasoning_to_model(
    registry: ModelRegistry | None,
    alias: str,
    *,
    caps: ModelCapabilities | None = None,
    cfg: Any | EllipsisType = ...,
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
    *cfg* follows the shared sentinel contract (``...`` = fetch for me,
    ``None`` = fetched-and-missed) so ``model_turn`` reads ONE config
    generation per call across both per-call flags.
    """
    if cfg is ...:
        cfg = _get_config_or_none(registry, alias)
    if cfg is None:
        return False
    operator_on = bool(getattr(cfg, "replay_reasoning_to_model", False))
    if caps is None:
        return operator_on
    return operator_on and bool(caps.supports_reasoning_replay)


def maybe_attach_vllm_chat_reasoning(
    messages: list[dict[str, Any]],
    provider: LLMProvider,
    registry: ModelRegistry | None,
    alias: str,
    *,
    cfg: Any | EllipsisType = ...,
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
    if cfg is ...:
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

    *temperature* / *reasoning_effort* are the lane's OPERATOR-resolved
    sampling knobs — the assignment scheme's operator rungs only
    (per-model alias value → stored global setting → ``None``; see
    :func:`resolve_temperature_setting` / :func:`resolve_effort_setting`).
    ``None`` means no operator spoke.  For temperature that is terminal:
    the field is omitted from the wire and the inference engine's own
    default applies.  For effort, :func:`model_turn` applies exactly one
    more rung below the lane — the in-code model definition
    (``caps.default_reasoning_effort``) — before omitting; there is
    deliberately NO caller-supplied default rung (a code-chosen effort
    token is unvetted on local vocabularies).  House rule: code never
    pins either knob; callers pass an explicit value only when relaying
    an operator/user-resolved knob (the session's own value on the
    session-model lanes).
    """

    provider: LLMProvider
    client: Any
    model: str
    alias: str = ""
    capabilities: ModelCapabilities | None = None
    extra_params: dict[str, Any] | None = None
    registry: ModelRegistry | None = None
    temperature: float | None = None
    reasoning_effort: str | None = None
    # Runtime credential resolver supplied by the host that owns OAuth state.
    # Kept on the lane because this synchronous module has no manager singleton.
    backend_auth_resolver: Callable[[str], str | None] | None = None


def lane_thinking_suppressed(lane: ModelLane) -> bool:
    """True when *lane* gets the bounded-artifact no-reasoning posture.

    THE gate for :func:`lane_without_thinking` — exposed so a caller that
    relays its own effort knob (``_utility_completion``'s web-fetch
    relay) can zero the caller rung under exactly the same condition the
    lane rungs are zeroed under, instead of re-deriving it.  False when
    the backend segregates reasoning (``server_parses_reasoning`` —
    reasoning then costs the artifact nothing and the operator's knobs
    stand), when the lane carries no resolved capabilities, or on
    providers that take no ``extra_body`` (real Anthropic/Google shape
    their own thinking params).
    """
    caps = lane.capabilities
    return (
        caps is not None
        and not caps.server_parses_reasoning
        and lane.provider.provider_name in EXTRA_BODY_PROVIDERS
    )


def lane_without_thinking(lane: ModelLane) -> ModelLane:
    """*lane* with every reasoning request it owns turned OFF.

    For the bounded-artifact lanes (the session's ``_utility_completion``:
    title, compaction, web-fetch extraction): a server that does not
    segregate reasoning leaves it in ``content``, and when it arrives
    UNMARKED — no tags, no ``reasoning_content`` — the drain seam cannot
    lift it out, so it lands in the artifact (#940).  Asking for no
    reasoning is the only lever that survives that, so when
    :func:`lane_thinking_suppressed` holds, EVERY channel this lane
    controls goes silent:

    - the declared template toggle is pinned ``False``
      (:func:`thinking_off_template_kwargs` — the alias's OWN key, never
      a guessed one), layered into the already-resolved ``extra_params``
      (no second config fetch) over any operator ``server_compat``
      thinking flag — that flag speaks for the answering lane, and these
      calls are not it.  The provider's own injection only
      ``setdefault``s, so the pin also beats the ``adaptive`` branch's
      unconditional ``true``;
    - the lane's operator effort rung and the model definition's
      ``default_reasoning_effort`` rung are cleared, so ``model_turn``
      resolves NO effective effort and neither the flat
      ``reasoning_effort`` param (effort-passthrough boxes) nor the
      graded template ``effort_param`` is emitted — an effort value
      beside a pinned-off toggle re-requests the reasoning the pin just
      declined, and on toggle-less templates the effort key alone is a
      reasoning request.  Where a box reasons unconditionally (no toggle,
      no effort semantics), omitting the knobs at least never asks for
      MORE — the remediation there is server-side
      (``server_parses_reasoning`` once a parser is configured).

    Callers relaying a caller-rung effort must zero it under the same
    predicate — see :func:`lane_thinking_suppressed`.
    """
    if not lane_thinking_suppressed(lane):
        return lane
    caps = lane.capabilities
    assert caps is not None  # lane_thinking_suppressed guarantees it
    extra = lane.extra_params
    off = thinking_off_template_kwargs(caps.thinking_mode, caps.thinking_param)
    if off:
        extra = dict(lane.extra_params or {})
        raw_ctk = extra.get("chat_template_kwargs")
        ctk = dict(raw_ctk) if isinstance(raw_ctk, dict) else {}
        ctk.update(off)
        extra["chat_template_kwargs"] = ctk
    return replace(
        lane,
        extra_params=extra,
        reasoning_effort=None,
        capabilities=replace(caps, default_reasoning_effort=""),
    )


def resolve_lane(
    provider: LLMProvider,
    client: Any,
    model: str,
    *,
    alias: str = "",
    registry: ModelRegistry | None = None,
    capabilities: ModelCapabilities | None = None,
    extra_params: dict[str, Any] | None | EllipsisType = ...,
    config_store: Any | None = None,
    backend_auth_resolver: Callable[[str], str | None] | None = None,
) -> ModelLane:
    """Build a :class:`ModelLane`, resolving what the caller didn't supply.

    *capabilities* / *extra_params* accept pre-resolved values so callers
    that already ran the resolution (the session's cached primary caps, an
    agent run's per-alias resolution) don't pay for or drift from a second
    pass.  ``...`` (the sentinel default) means "resolve for me" —
    ``None`` is a valid resolved value for *extra_params*.

    The lane sampling knobs resolve through the shared operator rungs
    (:func:`resolve_temperature_setting` / :func:`resolve_effort_setting`
    — the SAME resolvers the session factories and the ``/model`` switch
    use, so a judge or single-shot lane samples exactly like the main
    loop on the same model): the alias's per-model value, else the
    operator's stored global setting when *config_store* is supplied,
    else ``None``.  The registered defaults for both settings are the
    unset sentinels, so an untouched install resolves ``None`` — the
    providers then OMIT the field and the inference engine's own default
    applies.

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
        temperature=resolve_temperature_setting(cfg, config_store),
        reasoning_effort=resolve_effort_setting(cfg, config_store),
        backend_auth_resolver=backend_auth_resolver,
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
    cfg: Any | EllipsisType = ...,
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
    if has_reasoning_bearing_block(provider_blocks):
        # Routine on native-reasoning lanes: reasoning_delta text is the
        # MIRROR of the native block there, so bailing is the correct
        # no-op, not an anomaly.  The genuinely anomalous shape —
        # inline-EXTRACTED text alongside a native block — is logged at
        # the drain, where extraction is distinguishable.
        return provider_blocks
    block: dict[str, Any] = {"type": "reasoning_text", "text": text}
    if cfg is ...:
        cfg = _get_config_or_none(registry, alias)
    server_type = _server_type_of(cfg) if cfg is not None else ""
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
    cfg: Any | EllipsisType = ...,
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
        provider_blocks, reasoning_parts, registry=registry, alias=alias, cfg=cfg
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

    *wire_msgs* is the exact lowered list handed to ``create_streaming``
    (post every pass, including the caller's ``prepare_wire``) — the
    main loop's token-table calibration reads it so the chars-per-token
    estimate is computed against what the provider actually counted,
    surviving lowerings the caller cannot see (#832; the successor of
    the session's ``_wire_msgs`` message-dict carrier).

    *producer* is the SERVING lane's provider name — the same identity
    stamped on ``turn.native`` when a native lane exists, carried
    separately so a native-less turn still records who produced it (the
    storage row's ``producer`` column; pre-fold this read the session's
    PRIMARY binding and mislabeled fallback-served turns).
    """

    turn: Turn
    finish_reason: str
    usage: UsageInfo | None
    tool_calls: list[dict[str, Any]]
    wire_msgs: list[dict[str, Any]] | None = None
    producer: str = ""

    @property
    def content(self) -> str:
        """The assistant text — convenience mirror of ``turn.text``."""
        return self.turn.text


def cap_tool_calls(result: ModelTurnResult, max_calls: int) -> tuple[list[dict[str, Any]], Turn]:
    """Degenerate-repetition guard shared by the bounded tool loops (eval,
    optimizer analyst): cap the mirror at *max_calls* and return the turn to
    append.  A capped turn is rebuilt WITHOUT its native lane — a full lane
    beside a truncated mirror would replay orphan native tool blocks the
    mirror no longer carries (the same native↔mirror rule
    :func:`finalize_provider_blocks` enforces).
    """
    capped = result.tool_calls[:max_calls]
    turn = result.turn
    if len(result.tool_calls) > len(capped):
        turn = Turn.assistant(result.content, tool_calls=turn.tool_calls[: len(capped)])
    return capped, turn


def _tee_chunks(
    chunks: Iterator[StreamChunk],
    on_chunk: Callable[[StreamChunk], None],
) -> Iterator[StreamChunk]:
    """Surface each chunk to *on_chunk* before the drain accumulates it.

    Sits UPSTREAM of ``drain_stream``'s own ``transport_guarded`` wrapper, so
    the callback observes exactly the chunk sequence the assembler consumes —
    no more (transport deaths raise at ``next()`` and never reach the
    callback) and no fewer (a callback raise at chunk N, the cancellation
    path, discards N from display and assembly alike, matching the
    interactive loop's check-before-dispatch ordering).  The callback's
    exceptions propagate untouched: ``GenerationCancelled`` is a
    ``BaseException``, invisible to the drain-retry arm by design.
    """
    for sc in chunks:
        on_chunk(sc)
        yield sc


def _raise_if_aborted(cancel_ref: Any, lane: ModelLane) -> None:
    """Refuse to go on with a call whose caller has already gone away (#972).

    Duck-typed on the same ``aborted`` predicate the drain-retry gate
    reads, so a ``None`` ref — most lanes — and a plain-list ref stay
    legal.  One definition, two call sites in :func:`model_turn`: entry,
    and immediately before ``create_streaming``.  Nothing interrupts the
    credential resolve that runs between them — a mint already under way
    completes even when the abort lands inside it.  The second read is
    what turns such an abort into a skipped request rather than a sent
    one, which is why it is not redundant with the first.

    The raised message is control flow, not prose.  ``_is_ctx_overflow``
    classifies an exception class it does not recognize by TEXT, so
    context-window vocabulary here would read downstream as a real
    overflow and send the compaction lane subdividing.
    """
    if not getattr(cancel_ref, "aborted", False):
        return
    log.debug("model_turn.abort_before_dispatch", model=lane.model, alias=lane.alias)
    raise DeadlineCancelledError("cancel_ref aborted before dispatch")


def model_turn(
    lane: ModelLane,
    turns: Sequence[Turn],
    *,
    tools: list[dict[str, Any]] | None = None,
    max_tokens: int = 4096,
    temperature: float | None = None,
    reasoning_effort: str | None = None,
    mint: Callable[[str], str] | None = None,
    wire_id_map: dict[str, str] | None = None,
    resolve_attachments: Callable[[list[str]], dict[str, Any]] | None = None,
    cancel_ref: list[Any] | None = None,
    backend_auth_token: str | None = None,
    deferred_names: frozenset[str] | None = None,
    prepare_wire: Callable[[list[dict[str, Any]]], list[dict[str, Any]]] | None = None,
    on_chunk: Callable[[StreamChunk], None] | None = None,
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

    *temperature* / *reasoning_effort* ``None`` (the defaults) inherit the
    lane's operator-resolved knobs (per-model config → stored global
    setting).  When no operator spoke, effort falls through the in-code
    model definition (``caps.default_reasoning_effort``), then both knobs
    are OMITTED from the wire — the inference engine's default rules.
    House rule: never pin either knob in code — there is deliberately no
    caller-supplied default rung, because a code-chosen effort value is
    an unvetted token on local vocabularies (``effort_passthrough``
    forwards it verbatim) and flips template thinking toggles the
    operator never engaged.  Pass an explicit value only to relay an
    operator- or user-resolved knob (the session's own knobs, a CLI flag).

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

    *cancel_ref* is the abort seam: the provider appends the live SDK
    stream object (which has ``.close()``) before the first chunk, so a
    deadline daemon can abort the blocked HTTP read from another thread
    instead of abandoning it (transport is a drained ``create_streaming``
    — see :func:`drain_stream`).  Its ``aborted`` predicate gates every
    dispatch, not only a re-issue (:func:`_raise_if_aborted`): an
    abandoned call is not worth a request, and on a delegated-auth alias
    not worth the credential resolve either — hence one read at entry,
    ahead of lowering and ``lane.backend_auth_resolver``, and one
    immediately before ``create_streaming``, which is the read that
    actually keeps bytes off the wire.  The entry read costs a pending
    credential failure, which a pending abort now masks; right, because
    the caller is gone.  Neither read closes anything: a resolve already
    under way finishes despite an abort landing inside it, and an abort
    landing after the second read reaches the in-flight call the way it
    always did — ``cancel_ref.append`` closes a handle that has not
    arrived yet, ``abort`` closes one that has.

    The raise is :class:`~turnstone.core.deadline.DeadlineCancelledError`,
    the deadline module's abandonment vocabulary.  ``GenerationCancelled``
    is deliberately not raised here: it subclasses ``BaseException``, so
    the ``except Exception`` arms around these calls cannot see it, and
    it lives in ``session``, which imports this module.  A caller whose
    ref means something narrower than "this call is abandoned" owns the
    translation — compaction does exactly that, its ``except`` arm
    re-checking the session and raising ``GenerationCancelled`` before it
    reads the error at all, which is what keeps a Stop mid-summary off
    the red-error path.

    *prepare_wire* is the caller's OWN deterministic lowering, composed
    after the seam passes and before the Phase-5 attach — the main loop's
    system-message prepend, sender labels, capability-sensitive system-turn
    fold, empty-user drop, and orphan repair live here (#832), each a
    ``lowering.py``-composed pass.  It must be pure lowering: no learned
    selection, no provider calls, no side effects (the session's debug
    request dump, a read-only latch, is the tolerated exception).  The
    exact post-``prepare_wire``, post-attach list that went to the wire is
    returned as ``ModelTurnResult.wire_msgs`` for caller-side calibration.

    *deferred_names* passes through to ``create_streaming`` — the
    tool-search deferred-loading set is per-call state (it grows as the
    session discovers tools), which is why it is a parameter here and not
    a ``ModelLane`` field.

    *on_chunk* is the streaming surface (#832): each normalized
    :class:`StreamChunk` is surfaced to the caller as it arrives — via a
    tee UPSTREAM of the drain, so the callback sees exactly the sequence
    the assembler consumes — and the fully assembled result is returned as
    usual.  Chunk→UI translation is entirely the caller's business; the
    canonical Turn always comes from the drain's assembly, never from
    anything the callback accumulated.  A callback raise aborts the call
    with that exception (the interactive cancellation path rides this:
    ``GenerationCancelled`` is a ``BaseException`` and passes the retry
    arm untouched).  **With *on_chunk* present the mid-stream drain retry
    below is DISABLED** — the third and final policy carve-out: a
    partially-surfaced stream must never be silently re-issued behind a
    UI that already rendered its tokens; the streaming caller owns
    re-issue, finalizing its display per attempt.

    Raises whatever the provider raises — retry/deadline/fallback policy
    is the caller's — EXCEPT transient mid-stream deaths: a failure the
    provider's own ``retryable_error_names`` recognizes, raised while
    DRAINING (``IncompleteStreamError`` and friends), is re-issued in
    place up to ``_DRAIN_RETRIES`` times (zero with *on_chunk*, above).  The retired non-streaming
    transport read the whole body inside the SDK's retried request, so
    single-shot callers never saw a mid-body wire blip; this loop is
    that retry's new home (request-time failures still get the SDK's own
    policy inside ``create_streaming`` and propagate unchanged).  An
    aborted *cancel_ref* suppresses retries — a deadline that closed the
    stream must not have the request resurrected behind its back — and,
    read before each dispatch, raises
    :class:`~turnstone.core.deadline.DeadlineCancelledError` instead of
    issuing the request at all (see *cancel_ref* above).

    *backend_auth_token* is a delegated-user or app-identity credential for a
    dynamically authenticated backend. When set, the call is issued on
    ``lane.client.with_options(api_key=...)`` — a copy that reuses the
    client's connection pool but swaps the credential, so the SDK emits it as
    its own auth header (``x-api-key`` for Anthropic, ``Authorization: Bearer``
    for OpenAI-style).  This is deliberately NOT header injection via
    ``extra_headers``: the Anthropic SDK does not let ``extra_headers``
    override its ``x-api-key``, so an injected header is silently dropped.
    ``None`` leaves the lane's static client credential in place. When the
    explicit argument is absent, ``lane.backend_auth_resolver`` resolves it
    once before the drain-retry loop.
    """
    if mint is not None and wire_id_map is None:
        raise ValueError(
            "model_turn: mint requires wire_id_map — minted ids are "
            "unrestorable on the wire without the recovery map"
        )
    _raise_if_aborted(cancel_ref, lane)
    # ONE config fetch per plant call feeds both live per-call flags — a
    # registry hot-reload cannot hand the replay gate and the attach gate
    # different config generations within a single request.
    cfg = _get_config_or_none(lane.registry, lane.alias)
    wire = restore_provider_tool_ids(
        sanitize_tool_call_arguments(dicts_from_turns(list(turns))),
        wire_id_map if wire_id_map is not None else {},
    )
    if prepare_wire is not None:
        wire = prepare_wire(wire)
    wire = maybe_attach_vllm_chat_reasoning(wire, lane.provider, lane.registry, lane.alias, cfg=cfg)
    # The effort assignment scheme's lower rungs: explicit relay → lane
    # (operator) → in-code model definition → None.  None/unset knobs are
    # OMITTED from the wire so the inference engine's default rules
    # (house rule: a Python-level constant anywhere on this path is a
    # hidden pin).  Direct keyword call (not **kwargs) so strict mypy
    # checks the module's most important invocation against the Protocol.
    effective_effort = (
        reasoning_effort
        or lane.reasoning_effort
        or (lane.capabilities.default_reasoning_effort if lane.capabilities else None)
        or None
    )
    resolved_backend_auth = backend_auth_token
    if resolved_backend_auth is None and lane.backend_auth_resolver is not None:
        resolved_backend_auth = lane.backend_auth_resolver(lane.alias)
    # Bind once: SDK ``with_options`` preserves the base transport/pool while
    # replacing only the provider credential. A drain retry reuses this client.
    call_client = (
        lane.client.with_options(api_key=resolved_backend_auth)
        if resolved_backend_auth
        else lane.client
    )
    # A partially-surfaced stream is never silently re-issued (see the
    # on_chunk docstring section) — the streaming caller owns re-issue.
    drain_retries = 0 if on_chunk is not None else _DRAIN_RETRIES
    attempt = 0
    while True:
        # Last read before the wire — it covers everything the entry read is
        # too early to see: the lowering, and on a delegated-auth alias the
        # credential resolve, which can block.
        _raise_if_aborted(cancel_ref, lane)
        # ``create_streaming`` stays OUTSIDE the try: every adapter issues
        # the HTTP request eagerly in its body (inside the SDK's own
        # request-level retry), so an exception from it is a request-time
        # failure that already got its retries; only drain-time failures
        # are mid-stream deaths the SDK could never see.
        # Dynamic backends bind the token as the client's api_key so the SDK
        # emits it as its own auth header; with_options reuses the pool.
        # (extra_headers can't override the Anthropic SDK's x-api-key.)
        chunks = lane.provider.create_streaming(
            client=call_client,
            model=lane.model,
            messages=wire,
            tools=tools,
            max_tokens=max_tokens,
            temperature=temperature if temperature is not None else lane.temperature,
            reasoning_effort=effective_effort,
            extra_params=lane.extra_params,
            deferred_names=deferred_names,
            cancel_ref=cancel_ref,
            capabilities=lane.capabilities,
            replay_reasoning_to_model=resolve_replay_reasoning_to_model(
                lane.registry, lane.alias, caps=lane.capabilities, cfg=cfg
            ),
            resolve_attachments=resolve_attachments,
        )
        try:
            result = drain_stream(
                _tee_chunks(chunks, on_chunk) if on_chunk else chunks,
                # A lane with no declared capabilities keeps the
                # passthrough-server default: scan.
                scan_inline_reasoning=not (
                    lane.capabilities.server_parses_reasoning if lane.capabilities else False
                ),
            )
            break
        except Exception as exc:
            attempt += 1
            if (
                attempt > drain_retries
                or bool(getattr(cancel_ref, "aborted", False))
                or type(exc).__name__ not in lane.provider.retryable_error_names
            ):
                raise
            delay = _DRAIN_RETRY_BASE_DELAY * (2 ** (attempt - 1)) * (0.5 + random.random())
            log.warning(
                "model_turn.drain_retry",
                error_type=type(exc).__name__,
                attempt=attempt,
                model=lane.model,
                retry_in=round(delay, 2),
            )
            if delay > 0:
                time.sleep(delay)
            if bool(getattr(cancel_ref, "aborted", False)):
                # The deadline abandoned this worker while it was backing off.
                # The loop-top read would stop the re-issue anyway; this arm
                # exists to die with the ORIGINAL transport failure rather than
                # the abandonment error, so the cause of the death survives.
                raise

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
        cfg=cfg,
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
        wire_msgs=wire,
        producer=lane.provider.provider_name,
    )
