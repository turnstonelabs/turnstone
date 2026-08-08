"""Universal perception fallback for attachments the active model can't ingest.

When the primary model lacks native support for an attachment's modality — and
can't be shown a degraded-but-native form either (a non-vision model can't read
rasterized PDF pages) — a separately-configured "perception" model perceives the
attachment and its description/transcript is sent as a text part.  This mirrors
the speech-to-text fallback in :mod:`turnstone.core.audio`: a model-role alias
(``perception.model_alias``) plus a module-level memo so the perceive call — an
extra LLM round-trip — runs once per attachment, not once per conversation turn.

It is a *bottom-tier, universal* safety net:

* vision: native ``supports_pdf``/``supports_vision`` → rasterize-to-vision-primary
  (PDF) → **perception** (if the perception model has vision) → extract-text / placeholder.
* audio: native ``supports_audio_input`` → STT transcription role → **perception**
  (if the perception model has audio input) → placeholder.

A vision-capable primary still receives the real image / rasterized pages, and a
configured STT model still wins for audio — perception only fills the remaining
gap.  Point it at an omni model (text+vision+audio) to cover every modality from
one alias; a vision-only model covers image/PDF and is simply skipped for audio.

The call goes through :func:`turnstone.core.model_turn.model_turn` (the shared
plant-call seam, #827), so any provider works: the trajectory carries the
attachment by reference and the pre-built OpenAI-shaped parts (``image_url`` /
``input_audio``) materialize at the provider translator via the
``resolve_attachments`` callback, exactly like the main loop's wire path.
"""

from __future__ import annotations

import threading
from typing import Any

from turnstone.core.deadline import DeadlineCancelledError
from turnstone.core.log import get_logger
from turnstone.core.model_turn import ModelLane, ResolvedModelBinding, model_turn
from turnstone.core.providers._protocol import refuse_aborted_request
from turnstone.core.trajectory import AttachmentRef, Role, TextBlock, Turn

log = get_logger(__name__)

# The single by-reference id inside a perception trajectory.  Perception
# trajectories are ephemeral one-shot requests (never persisted, never
# displayed), so the id only needs intra-request consistency between the
# placeholder and the resolver mapping.
_PERCEPTION_REF_ID = "perception-input"

# Placeholder ``kind`` per part shape — cosmetic for an ephemeral trajectory
# (the resolver replaces the placeholder wholesale) but kept honest for
# debuggability.
_PART_KINDS = {"image_url": "image", "input_audio": "audio"}

# Config key naming the model used for perception fallbacks.
PERCEPTION_SETTING = "perception.model_alias"

_DESCRIBE_PROMPT = (
    "You are a perception backend for another AI model that cannot perceive this "
    "attachment. Convey it in full, faithful detail: transcribe all text and "
    "speech verbatim, and describe any figures, tables, diagrams, layout, or "
    "non-speech audio. Do not summarize away or omit content — the reader relies "
    "entirely on your output to understand the attachment."
)


class PerceptionBackendError(RuntimeError):
    """A configured perception backend failed during the perceive call."""


def describe(
    *,
    lane: ModelLane,
    parts: list[dict[str, Any]],
    prompt: str = _DESCRIBE_PROMPT,
    cancel_ref: Any = None,
) -> str:
    """Perceive ``parts`` via the perception model, returning the text.

    ``parts`` are pre-built OpenAI-shaped content parts — ``image_url`` for
    image/PDF-page perception, ``input_audio`` for audio.  The trajectory
    carries them by reference; ``model_turn`` hands the resolver to the
    provider translator, which materializes the placeholder into these exact
    parts (one ref may expand to many, e.g. a rasterized PDF).

    ``lane`` is the caller's already-resolved binding snapshot, so the
    modality gate and the plant call cannot observe different registry
    generations.  Raises
    :class:`PerceptionBackendError` if the backend call fails.  Never
    caches — see :func:`describe_cached`.
    """
    if not parts:
        return ""
    kind = _PART_KINDS.get(str(parts[0].get("type", "")), "document")
    turns = [
        Turn(
            role=Role.USER,
            content=(
                TextBlock(prompt),
                AttachmentRef(attachment_id=_PERCEPTION_REF_ID, kind=kind),
            ),
        )
    ]
    try:
        result = model_turn(
            lane,
            turns,
            max_tokens=4096,
            resolve_attachments=lambda _ids: {_PERCEPTION_REF_ID: parts},
            cancel_ref=cancel_ref,
        )
    except DeadlineCancelledError:
        raise
    except Exception as exc:
        raise PerceptionBackendError(f"perception backend failed: {exc}") from exc
    return (result.content or "").strip()


# -- perception memoization (no-native-modality wire fallback) ----------------
# Mirrors audio.transcribe_cached: the wire resolver re-materializes every
# attachment on every send, so without this memo an attachment perceived early
# in a conversation would be re-perceived (an extra LLM round-trip) on every
# subsequent turn.
_CACHE_MAX = 256
_cache_lock = threading.Lock()
_cache: dict[tuple[str, str, int, str], str] = {}


def _cache_key(
    *,
    principal_id: str,
    binding: ResolvedModelBinding,
    content_hash: str,
) -> tuple[str, str, int, str]:
    """Partition content by authorizing identity and exact registry binding."""
    return (
        principal_id,
        binding.lane.alias,
        binding.registry_generation,
        content_hash,
    )


def _clear_perception_cache_for_test() -> None:
    with _cache_lock:
        _cache.clear()


def describe_cached(
    *,
    binding: ResolvedModelBinding,
    principal_id: str,
    content_hash: str,
    parts: list[dict[str, Any]],
    prompt: str = _DESCRIBE_PROMPT,
    cancel_ref: Any = None,
) -> str:
    """Memoized :func:`describe` for the wire fallback.

    Keyed by ``(principal_id, alias, registry_generation, content_hash)``.  The
    complete binding keeps the lane used for a miss and the generation used for
    lookup inseparable.  Principal partitioning prevents one user's OBO result
    from reaching another, while generation partitioning prevents an alias
    reload from reusing output produced by an older backend/auth policy. Returns
    ``""`` on a backend failure (a placeholder is rendered upstream) and does
    *not* cache failures. Cancellation propagates as control flow so Stop can
    abort the parent turn.
    A completed-but-EMPTY description memoizes like any other result — one
    perceive per key, ever (an all-reasoning pass pins the placeholder; the
    remediation is server-side: a reasoning parser or the template thinking
    toggle on the perception alias) — under one guard: an empty result NEVER
    overwrites a concurrently memoized real description.
    """
    refuse_aborted_request(cancel_ref)
    lane = binding.lane
    key = _cache_key(
        principal_id=principal_id,
        binding=binding,
        content_hash=content_hash,
    )
    with _cache_lock:
        cached = _cache.get(key)
        cache_hit = key in _cache
    if cache_hit:
        refuse_aborted_request(cancel_ref)
        return cached or ""
    try:
        text = describe(
            lane=lane,
            parts=parts,
            prompt=prompt,
            cancel_ref=cancel_ref,
        )
    except PerceptionBackendError as exc:
        refuse_aborted_request(cancel_ref)
        log.warning("perception fallback failed (alias=%s): %s", lane.alias, exc)
        return ""
    refuse_aborted_request(cancel_ref)
    with _cache_lock:
        refuse_aborted_request(cancel_ref)
        # Re-check under the lock: the describe call ran unlocked, and a
        # concurrent racer may have memoized a REAL description — an empty
        # result must never clobber it (the memo has no invalidation
        # path, so a clobber would pin the placeholder despite a billed,
        # successful perceive).
        existing = _cache.get(key)
        if existing:
            return existing
        if key not in _cache and len(_cache) >= _CACHE_MAX:
            _cache.pop(next(iter(_cache)), None)
        _cache[key] = text
    return text


def describe_peek(
    *,
    principal_id: str,
    binding: ResolvedModelBinding,
    content_hash: str,
) -> str | None:
    """Return the principal-and-binding-scoped memo without computing.

    Lets the wire resolver skip the expensive parts build (a PDF rasterize) when
    the description is already memoized from an earlier send — :func:`describe_cached`
    ignores ``parts`` on a hit, so building them first would be pure waste.
    """
    with _cache_lock:
        return _cache.get(
            _cache_key(
                principal_id=principal_id,
                binding=binding,
                content_hash=content_hash,
            )
        )
