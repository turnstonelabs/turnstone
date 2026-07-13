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
from typing import TYPE_CHECKING, Any

from turnstone.core.log import get_logger
from turnstone.core.model_turn import model_turn, resolve_lane
from turnstone.core.trajectory import AttachmentRef, Role, TextBlock, Turn

if TYPE_CHECKING:
    from turnstone.core.providers._protocol import LLMProvider

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
    provider: LLMProvider,
    client: Any,
    model: str,
    parts: list[dict[str, Any]],
    prompt: str = _DESCRIBE_PROMPT,
) -> str:
    """Perceive ``parts`` via the perception model, returning the text.

    ``parts`` are pre-built OpenAI-shaped content parts — ``image_url`` for
    image/PDF-page perception, ``input_audio`` for audio.  The trajectory
    carries them by reference; ``model_turn`` hands the resolver to the
    provider translator, which materializes the placeholder into these exact
    parts (one ref may expand to many, e.g. a rasterized PDF).  Temperature
    is not pinned (house rule) — the perception model's own configuration
    governs sampling.  Raises :class:`PerceptionBackendError` if the backend
    call fails.  Never caches — see :func:`describe_cached`.
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
    lane = resolve_lane(provider, client, model)
    try:
        result = model_turn(
            lane,
            turns,
            max_tokens=4096,
            resolve_attachments=lambda _ids: {_PERCEPTION_REF_ID: parts},
        )
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
_cache: dict[str, str] = {}


def _clear_perception_cache_for_test() -> None:
    with _cache_lock:
        _cache.clear()


def describe_cached(
    *,
    provider: LLMProvider,
    client: Any,
    model: str,
    alias: str,
    content_hash: str,
    parts: list[dict[str, Any]],
    prompt: str = _DESCRIBE_PROMPT,
) -> str:
    """Memoized, non-raising :func:`describe` for the wire fallback.

    Keyed by ``(alias, content_hash)``.  Returns ``""`` on a backend failure (a
    placeholder is rendered upstream) and does *not* cache failures, so a
    transient outage doesn't poison the memo.
    """
    key = f"{alias}:{content_hash}"
    with _cache_lock:
        if key in _cache:
            return _cache[key]
    try:
        text = describe(
            provider=provider,
            client=client,
            model=model,
            parts=parts,
            prompt=prompt,
        )
    except PerceptionBackendError as exc:
        log.warning("perception fallback failed (alias=%s): %s", alias, exc)
        return ""
    with _cache_lock:
        if key not in _cache and len(_cache) >= _CACHE_MAX:
            _cache.pop(next(iter(_cache)), None)
        _cache[key] = text
    return text


def describe_peek(*, alias: str, content_hash: str) -> str | None:
    """Return the memoized description for ``(alias, content_hash)`` without
    computing, or ``None`` if absent.

    Lets the wire resolver skip the expensive parts build (a PDF rasterize) when
    the description is already memoized from an earlier send — :func:`describe_cached`
    ignores ``parts`` on a hit, so building them first would be pure waste.
    """
    with _cache_lock:
        return _cache.get(f"{alias}:{content_hash}")
