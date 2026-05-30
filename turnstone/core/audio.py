"""Speech-to-text and text-to-speech over the OpenAI audio wire protocol.

STT/TTS are model *roles* (settings ``stt.model_alias`` / ``tts.model_alias``),
resolved the same way as ``judge.model``.  A role resolves to a registry alias
whose client is an OpenAI-SDK-compatible client (provider ``openai`` /
``openai-compatible`` / ``google`` / ``xai``); the same ``client.audio.*`` calls
then work against OpenAI, a local vLLM / vLLM-Omni server, or any compatible
backend — selected purely by the alias's ``base_url``.  Anthropic models have no
audio API, so they are capability-gated out of these roles (Claude stays valid
as the agent model).

No local in-process models and no silent fallbacks: an unconfigured or failing
backend is surfaced as a typed error the endpoint maps to 503 / 502.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Setting key + capability flag per media role.  Kept deliberately small; the
# perception/eval roles (vision_eval/av_eval/intent_eval) are a later slice.
_ROLE_SETTING: dict[str, str] = {
    "stt": "audio.stt_model_alias",
    "tts": "audio.tts_model_alias",
}
_ROLE_CAPABILITY: dict[str, str] = {
    "stt": "supports_transcription",
    "tts": "supports_speech_synthesis",
}

# Known-model-name hints for capability inference. The explicit
# ``capabilities`` flag is ALWAYS canonical (see ``model_supports_role``); these
# only fill the gap so a stock OpenAI audio model alias works without an
# operator hand-ticking a box. A substring match against the lowercased model
# name marks eligibility.
#
# IMPORTANT: these lists are mirrored verbatim in the admin UI
# (``_audioModelEligible`` / ``AUDIO_*_MODEL_HINTS`` in
# ``turnstone/console/static/admin.js``) so the Models -> Roles dropdown offers
# exactly the aliases the endpoints will accept. Keep the two in sync;
# ``tests/test_audio.py`` pins these values so a change here is deliberate.
_AUDIO_MODEL_HINTS: dict[str, tuple[str, ...]] = {
    "stt": ("transcribe", "whisper", "-asr"),
    "tts": ("tts-", "-tts"),
}

# response_format -> Content-Type for synthesized audio.
_MEDIA_TYPES: dict[str, str] = {
    "mp3": "audio/mpeg",
    "opus": "audio/ogg",
    "aac": "audio/aac",
    "flac": "audio/flac",
    "wav": "audio/wav",
    "pcm": "audio/pcm",
}

_DEFAULT_VOICE = "alloy"


@dataclass(frozen=True)
class TranscriptionResult:
    transcript: str
    model_alias: str
    model: str


@dataclass(frozen=True)
class SpeechResult:
    audio_bytes: bytes
    media_type: str
    model_alias: str
    model: str


class AudioUnavailableError(RuntimeError):
    """No capable backend is configured for the requested role (maps to 503)."""


class AudioBackendError(RuntimeError):
    """A configured audio backend failed during execution (maps to 502)."""


def _infer_audio_capability(model: str, role: str) -> bool:
    """Best-effort capability default for well-known audio model names.

    The explicit ``capabilities`` flag always wins (see ``model_supports_role``);
    this only fills the gap so a stock ``gpt-4o-mini-transcribe`` /
    ``gpt-4o-mini-tts`` / ``whisper-1`` alias works without an operator
    hand-ticking a capability box. Rules live in :data:`_AUDIO_MODEL_HINTS`
    (mirrored in admin.js).
    """
    name = (model or "").strip().lower()
    if not name:
        return False
    return any(hint in name for hint in _AUDIO_MODEL_HINTS.get(role, ()))


def model_supports_role(cfg: Any, role: str) -> bool:
    """Whether the alias's model is eligible for a media *role*.

    Explicit ``capabilities[<flag>]`` wins; otherwise fall back to a
    known-model-name inference for OpenAI audio models.
    """
    flag = _ROLE_CAPABILITY.get(role)
    if not flag:
        return False
    caps = getattr(cfg, "capabilities", None) or {}
    if flag in caps:
        return bool(caps.get(flag))
    return _infer_audio_capability(getattr(cfg, "model", ""), role)


def resolve_role_alias(*, config_store: Any | None, registry: Any | None, role: str) -> str | None:
    """Return the configured, capability-eligible alias for *role*, or ``None``.

    Resolution: ``<role>.model_alias`` setting → must exist in the registry →
    must be capability-eligible for the role.  Any miss returns ``None`` so the
    caller surfaces a 503 (or hides the affordance) rather than calling a
    backend that can't serve audio.
    """
    if registry is None or config_store is None:
        return None
    key = _ROLE_SETTING.get(role)
    if not key:
        return None
    alias = (config_store.get(key) or "").strip()
    if not alias or not registry.has_alias(alias):
        return None
    if not model_supports_role(registry.get_config(alias), role):
        return None
    return alias


def transcribe(
    *, registry: Any, alias: str, data: bytes, filename: str, prompt: str = ""
) -> TranscriptionResult:
    """Transcribe ``data`` using the STT role alias's audio backend.

    ``prompt`` (when non-empty) is forwarded as the transcription ``prompt``
    parameter to bias the model toward domain vocabulary / instructions; it is
    omitted entirely when blank so backends that don't accept it aren't sent it.
    """
    try:
        client, model, _cfg = registry.resolve(alias)
    except Exception as exc:  # unknown/removed alias
        raise AudioUnavailableError(f"STT model alias {alias!r} is not available") from exc
    kwargs: dict[str, Any] = {
        "model": model,
        "file": (filename or "speech.webm", data),
        "response_format": "json",
    }
    if prompt:
        kwargs["prompt"] = prompt
    try:
        resp = client.audio.transcriptions.create(**kwargs)
        transcript = (getattr(resp, "text", "") or "").strip()
    except Exception as exc:
        raise AudioBackendError(f"Transcription backend failed: {exc}") from exc
    return TranscriptionResult(transcript=transcript, model_alias=alias, model=model)


def synthesize(
    *, registry: Any, alias: str, text: str, voice: str, response_format: str = "mp3"
) -> SpeechResult:
    """Synthesize ``text`` to speech using the TTS role alias's audio backend."""
    try:
        client, model, _cfg = registry.resolve(alias)
    except Exception as exc:
        raise AudioUnavailableError(f"TTS model alias {alias!r} is not available") from exc
    try:
        resp = client.audio.speech.create(
            model=model,
            voice=voice or _DEFAULT_VOICE,
            input=text,
            response_format=response_format,
        )
        audio_bytes = resp.read() if hasattr(resp, "read") else bytes(getattr(resp, "content", b""))
    except Exception as exc:
        raise AudioBackendError(f"TTS backend failed: {exc}") from exc
    return SpeechResult(
        audio_bytes=audio_bytes,
        media_type=_MEDIA_TYPES.get(response_format, "audio/mpeg"),
        model_alias=alias,
        model=model,
    )
